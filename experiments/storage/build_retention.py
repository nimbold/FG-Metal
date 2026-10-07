#!/usr/bin/env python3
"""Fail-closed FG-Metal build leases, artifactization, and automatic retirement.

New Step 11D.3 builds must use BuildLease under the dedicated cache root.
Successful builds are copied (never hard-linked) into the immutable-by-policy
SHA-256 store and deleted before completion returns. Legacy roots are an exact
allowlist used only for one-time reconciliation.
"""
from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import hashlib
import hmac
import json
import os
import plistlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()
CACHE_ROOT = HOME / "Library/Caches/FGMetal"
BUILD_ROOT = CACHE_ROOT / "builds"
ARTIFACT_ROOT = CACHE_ROOT / "artifacts/sha256"
STORAGE_ROOT = REPO / "experiments/storage"
LEASE_ROOT = STORAGE_ROOT / "build-leases"
MANIFEST_ROOT = STORAGE_ROOT / "build-manifests"
RECEIPT_ROOT = STORAGE_ROOT / "build-cleanup-receipts"
BUILD_MARKER = ".fgmetal-build-lease.json"
BUILD_EXECUTION_RECEIPT = "build-execution.json"
BUILD_LOCK = ".retention.lock"
GIB = 1024 ** 3
MAX_ACTIVE_DXVK = 1
MAX_ACTIVE_MOLTENVK = 1
MAX_ACTIVE_LARGE_BUILDS = 2
MAX_PRESERVED_DEBUG = 1
MAX_RETAINED = 3
PRESERVE_DEBUG_MAX_SECONDS = 7 * 24 * 60 * 60
MAX_FAILURE_CAPTURE_FILES = 64
MAX_FAILURE_CAPTURE_SCAN_ENTRIES = 4096
MAX_FAILURE_CAPTURE_FILE_BYTES = 64 * 1024 * 1024
MAX_FAILURE_CAPTURE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_COPY_ALL_FAILURE_ROOT_BYTES = 64 * 1024 * 1024
MAX_FAILURE_MANIFEST_BYTES = 1024 * 1024
MAX_FAILURE_COMMAND_BYTES = 64 * 1024
MAX_FAILURE_ERROR_BYTES = 16 * 1024
MAX_FAILURE_REASON_BYTES = 4096
ARTIFACT_EXTENSIONS = {".dll", ".dylib", ".so", ".exe", ".a", ".lib", ".metallib", ".bin"}
SHADER_EXTENSIONS = {".spv", ".glsl", ".vert", ".frag", ".geom", ".tesc", ".tese", ".comp", ".h", ".hpp", ".inc"}
KINDS = {"dxvk", "moltenvk", "wine", "reference"}
RESERVABLE_KINDS = {"dxvk", "moltenvk", "reference"}
SUPPORTED_BUILD_TYPES = {"release", "debug", "debugoptimized", "minsize", "targeted-symbols", "unstripped", "full-debug"}
FULL_DEBUG_BUILD_TYPES = {"debug", "debugoptimized", "unstripped", "full-debug"}
LARGE_BUILD_ROOT_MIN_BYTES = 8 * 1024 * 1024
LEGACY_BUILD_ROOTS = (
    Path("/private/tmp/fgmetal-step11d-build"),
    Path("/private/tmp/fgmetal-dxvk-macos-build-unmodified"),
    Path("/private/tmp/fgmetal-wine-11.17-vkmetal-build-x86"),
    Path("/private/tmp/fgmetal-wine-11.17-vkmetal-build"),
    Path("/Users/nima/Library/Developer/Xcode/DerivedData/MoltenVKPackaging-edscuptxhvkrwkdsuetlfjuipmyd"),
    Path("/private/tmp/fgmetal-step11d2-bridge-diagnostic-build-c-20261006"),
    Path("/private/tmp/fgmetal-step11d2-source-rebuild-check"),
    Path("/private/tmp/fg-step8b-build"),
    Path("/Users/nima/Library/Caches/FGMetalStep11C1R/build-d1"),
    Path("/Users/nima/Library/Caches/FGMetalStep11C1R/build-d1-repro"),
)
TEMP_ROOT = Path("/private/tmp")
LEGACY_STEP11D2_EVIDENCE_CORPUS = Path("/private/tmp/fgmetal-step11d2-bridge-diagnostic-c-20261006")
LEGACY_STEP11D2_EVIDENCE_CORPORA = {
    LEGACY_STEP11D2_EVIDENCE_CORPUS,
    Path("/private/tmp/fgmetal-step11d2-bridge-diagnostic-20261006"),
    Path("/private/tmp/fgmetal-step11d2-bridge-diagnostic-b-20261006"),
}
LEGACY_STEP11D2_REPORT_SHA256 = "9e43b796bd96586478e6fcd931155dbb18d38787fdb0c6aa17d0fe213477f014"
LEGACY_STEP11D2_HASHES_SHA256 = "eccbe768e28368050010cbecd1c4b0ee15d4b587b827f6d88447774148892c0b"
SPIRV_GENERATED_TABLE_ROOT = Path("/private/tmp/fgmetal-step11b/MoltenVK/External/SPIRV-Tools/build")
SPIRV_GENERATED_TABLE_FILES = {
    "build-version.inc", "core_tables_header.inc", "OpenCLDebugInfo100.h",
    "generators.inc", "DebugInfo.h", "core_tables_body.inc",
}

_KNOWN_PROTECTED_FRAMEGEN_ROOTS = {
    "fg-metal-clean-build-20261001",
    "fg-metal-step2-audit-build", "fg-metal-step2-review-build-20261001",
    "fgmetal-build-step7", "fgmetal-build-step7-full", "fg-metal-synthetic-review-arm64",
    "fg-step10b-synthetic-arm64-v4", "fg-step10b-synthetic-x86_64",
    "framegen-step2-review-build", "framegen-step10-build", "fg-metal-step10b6_1-build",
}

class RetentionError(RuntimeError):
    """The storage operation was refused or could not complete safely."""


@dataclasses.dataclass(frozen=True)
class DeclaredOutputRole:
    role: str
    relative_path: str
    artifact_name: str
    mandatory: bool = True
    is_macho: bool = True
    expected_architectures: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class NativeMoltenVKBuildRequest:
    # Pristine Git Source
    pristine_git_commit: str
    pristine_git_tree: str
    source_tree_sha256: str
    external_revisions: Mapping[str, str]
    vulkan_headers_revision: str
    vulkan_headers_tree_sha256: str

    # Instrumentation Patch
    patch_sha256: str
    patch_size_bytes: int
    modified_files: tuple[tuple[str, str, str], ...]  # (relative_path, pre_sha256, post_sha256)

    # Build System & Configuration
    build_system: str  # "xcode_native"
    project_relative_path: str  # "MoltenVKPackaging.xcodeproj"
    project_file_sha256: str
    target_or_scheme: str  # "MoltenVK-macOS-dylib"
    configuration: str  # "Release"
    architectures: tuple[str, ...]  # One explicitly declared thin Mach-O architecture.
    deployment_target: str  # "12.0"
    sdk_name: str  # "macosx"

    # Compiler / Toolchain
    xcode_version_string: str
    clang_version_string: str
    clang_binary_sha256: str
    sdk_version_string: str
    linker_version_string: str
    toolchain_identity_sha256: str

    # Environment & Command
    normalized_environment: Mapping[str, str]
    normalized_arguments: tuple[str, ...]

    # Declared Outputs
    declared_outputs: tuple[DeclaredOutputRole, ...]
    # Optional for older synthetic fixtures; mandatory for the real experiment.
    prepared_source_root_relative_path: str | None = None


def compute_native_moltenvk_config_key(request: NativeMoltenVKBuildRequest) -> str:
    """Compute deterministic configuration key using strictly pre-build declared inputs."""
    supported_architectures = {"arm64", "x86_64"}
    if len(request.architectures) != 1 or request.architectures[0] not in supported_architectures:
        raise RetentionError("native MoltenVK builds require one supported, explicit architecture")
    if request.prepared_source_root_relative_path is not None:
        source_path = Path(request.prepared_source_root_relative_path)
        project_path = Path(request.project_relative_path)
        if (source_path.is_absolute() or "\\" in request.prepared_source_root_relative_path
                or any(part in {"", ".", ".."} for part in source_path.parts)
                or project_path.is_absolute() or "\\" in request.project_relative_path
                or not project_path.parts[:len(source_path.parts)] == source_path.parts
                or any(part in {"", ".", ".."} for part in project_path.parts)):
            raise RetentionError("native MoltenVK prepared-source/project paths must be canonical relative paths")
    for output in request.declared_outputs:
        if output.mandatory and output.is_macho and output.expected_architectures != request.architectures:
            raise RetentionError(
                f"output role {output.role!r} must declare the same single architecture as the build request")
    expected_toolchain_identity = native_toolchain_identity_sha256(
        xcode_version_string=request.xcode_version_string,
        clang_version_string=request.clang_version_string,
        clang_binary_sha256=request.clang_binary_sha256,
        sdk_version_string=request.sdk_version_string,
        linker_version_string=request.linker_version_string,
    )
    if request.toolchain_identity_sha256 != expected_toolchain_identity:
        raise RetentionError("native MoltenVK toolchain identity hash does not match its declared versions and compiler hash")
    payload = {
        "build_system": request.build_system,
        "pristine_git_commit": request.pristine_git_commit,
        "pristine_git_tree": request.pristine_git_tree,
        "source_tree_sha256": request.source_tree_sha256,
        "prepared_source_root_relative_path": request.prepared_source_root_relative_path,
        "external_revisions": sorted(request.external_revisions.items()),
        "vulkan_headers": {
            "revision": request.vulkan_headers_revision,
            "tree_sha256": request.vulkan_headers_tree_sha256,
        },
        "patch": {
            "sha256": request.patch_sha256,
            "size_bytes": request.patch_size_bytes,
            "modified_files": sorted(request.modified_files),
        },
        "project": {
            "path": request.project_relative_path,
            "sha256": request.project_file_sha256,
            "target": request.target_or_scheme,
            "configuration": request.configuration.casefold(),
            "architectures": sorted(request.architectures),
            "deployment_target": request.deployment_target,
            "sdk": request.sdk_name,
        },
        "toolchain": {
            "xcode_version": request.xcode_version_string,
            "clang_version": request.clang_version_string,
            "clang_binary_sha256": request.clang_binary_sha256,
            "sdk_version": request.sdk_version_string,
            "linker_version": request.linker_version_string,
            "toolchain_sha256": request.toolchain_identity_sha256,
        },
        "environment": sorted(request.normalized_environment.items()),
        "arguments": list(request.normalized_arguments),
        "declared_outputs": [
            {
                "role": out.role,
                "relative_path": out.relative_path,
                "artifact_name": out.artifact_name,
                "mandatory": out.mandatory,
                "is_macho": out.is_macho,
                "architectures": sorted(out.expected_architectures),
            }
            for out in sorted(request.declared_outputs, key=lambda x: x.role)
        ],
    }
    canonical_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def native_toolchain_identity_sha256(*, xcode_version_string: str, clang_version_string: str,
                                     clang_binary_sha256: str, sdk_version_string: str,
                                     linker_version_string: str) -> str:
    """Hash the native Xcode, compiler binary/version, SDK, and linker identity."""
    material = {
        "xcode_version": xcode_version_string,
        "clang_version": clang_version_string,
        "clang_binary_sha256": clang_binary_sha256,
        "sdk_version": sdk_version_string,
        "linker_version": linker_version_string,
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _dict_to_native_request(data: Mapping[str, Any]) -> NativeMoltenVKBuildRequest:
    outputs = []
    for out in data.get("declared_outputs", []):
        if isinstance(out, DeclaredOutputRole):
            outputs.append(out)
        else:
            outputs.append(DeclaredOutputRole(
                role=str(out["role"]),
                relative_path=str(out["relative_path"]),
                artifact_name=str(out["artifact_name"]),
                mandatory=bool(out.get("mandatory", True)),
                is_macho=bool(out.get("is_macho", True)),
                expected_architectures=tuple(out.get("expected_architectures", ())),
            ))
    modified = []
    for m in data.get("modified_files", []):
        modified.append((str(m[0]), str(m[1]), str(m[2])))

    return NativeMoltenVKBuildRequest(
        pristine_git_commit=str(data["pristine_git_commit"]),
        pristine_git_tree=str(data["pristine_git_tree"]),
        source_tree_sha256=str(data["source_tree_sha256"]),
        prepared_source_root_relative_path=(str(data["prepared_source_root_relative_path"])
                                            if data.get("prepared_source_root_relative_path") is not None else None),
        external_revisions=dict(data.get("external_revisions", {})),
        vulkan_headers_revision=str(data["vulkan_headers_revision"]),
        vulkan_headers_tree_sha256=str(data["vulkan_headers_tree_sha256"]),
        patch_sha256=str(data["patch_sha256"]),
        patch_size_bytes=int(data["patch_size_bytes"]),
        modified_files=tuple(modified),
        build_system=str(data.get("build_system", "xcode_native")),
        project_relative_path=str(data["project_relative_path"]),
        project_file_sha256=str(data["project_file_sha256"]),
        target_or_scheme=str(data["target_or_scheme"]),
        configuration=str(data["configuration"]),
        architectures=tuple(data["architectures"]),
        deployment_target=str(data.get("deployment_target", "12.0")),
        sdk_name=str(data.get("sdk_name", "macosx")),
        xcode_version_string=str(data.get("xcode_version_string", "")),
        clang_version_string=str(data.get("clang_version_string", "")),
        clang_binary_sha256=str(data.get("clang_binary_sha256", "")),
        sdk_version_string=str(data.get("sdk_version_string", "")),
        linker_version_string=str(data.get("linker_version_string", "")),
        toolchain_identity_sha256=str(data.get("toolchain_identity_sha256", "")),
        normalized_environment=dict(data.get("normalized_environment", {})),
        normalized_arguments=tuple(data.get("normalized_arguments", ())),
        declared_outputs=tuple(outputs),
    )


def _native_request_to_dict(req: NativeMoltenVKBuildRequest) -> dict[str, Any]:
    return {
        "pristine_git_commit": req.pristine_git_commit,
        "pristine_git_tree": req.pristine_git_tree,
        "source_tree_sha256": req.source_tree_sha256,
        "prepared_source_root_relative_path": req.prepared_source_root_relative_path,
        "external_revisions": dict(req.external_revisions),
        "vulkan_headers_revision": req.vulkan_headers_revision,
        "vulkan_headers_tree_sha256": req.vulkan_headers_tree_sha256,
        "patch_sha256": req.patch_sha256,
        "patch_size_bytes": req.patch_size_bytes,
        "modified_files": [list(x) for x in req.modified_files],
        "build_system": req.build_system,
        "project_relative_path": req.project_relative_path,
        "project_file_sha256": req.project_file_sha256,
        "target_or_scheme": req.target_or_scheme,
        "configuration": req.configuration,
        "architectures": list(req.architectures),
        "deployment_target": req.deployment_target,
        "sdk_name": req.sdk_name,
        "xcode_version_string": req.xcode_version_string,
        "clang_version_string": req.clang_version_string,
        "clang_binary_sha256": req.clang_binary_sha256,
        "sdk_version_string": req.sdk_version_string,
        "linker_version_string": req.linker_version_string,
        "toolchain_identity_sha256": req.toolchain_identity_sha256,
        "normalized_environment": dict(req.normalized_environment),
        "normalized_arguments": list(req.normalized_arguments),
        "declared_outputs": [
            {
                "role": o.role,
                "relative_path": o.relative_path,
                "artifact_name": o.artifact_name,
                "mandatory": o.mandatory,
                "is_macho": o.is_macho,
                "expected_architectures": list(o.expected_architectures),
            }
            for o in req.declared_outputs
        ],
    }

def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _key(*, create: bool = False) -> bytes:
    from storage_policy import _manifest_signing_key
    return _manifest_signing_key(create=create)

def _seal(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("lease_hmac_sha256", None)
    value["lease_hmac_sha256"] = hmac.new(_key(create=True), _canonical_json(value), hashlib.sha256).hexdigest()
    return value

def _verify_seal(payload: Mapping[str, Any]) -> bool:
    value = dict(payload)
    actual = value.pop("lease_hmac_sha256", None)
    if not isinstance(actual, str):
        return False
    try:
        expected = hmac.new(_key(), _canonical_json(value), hashlib.sha256).hexdigest()
    except (OSError, RuntimeError):
        return False
    return hmac.compare_digest(actual, expected)

def _open_dir(path: Path | str, *, create: bool = False) -> int:
    """Open every path component with O_NOFOLLOW and return a pinned directory fd."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child_fd = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                os.close(child_fd)
                raise RetentionError(f"not a real directory: {absolute}")
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise

def _rename_exclusive(directory_fd: int, source: str, target: str) -> None:
    import ctypes
    lib = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(lib, "renameatx_np", None)
    if renameatx_np is None:
        raise RetentionError("renameatx_np is unavailable; refusing unsafe replacement rename")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(directory_fd, os.fsencode(source), directory_fd, os.fsencode(target), 0x00000004)
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), target)
    os.fsync(directory_fd)

def _rename_swap_at(directory_fd: int, first: str, second: str) -> None:
    """Atomically exchange two basenames without discarding either entry."""
    import ctypes
    for name in (first, second):
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise RetentionError("atomic CAS exchange requires two safe basenames")
    lib = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(lib, "renameatx_np", None)
    if renameatx_np is None:
        raise RetentionError("renameatx_np is unavailable; refusing non-atomic CAS replacement")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    # Darwin's RENAME_SWAP atomically exchanges both directory entries.
    result = renameatx_np(directory_fd, os.fsencode(first), directory_fd,
                          os.fsencode(second), 0x00000002)
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), second)
    os.fsync(directory_fd)

def _unlink_pinned_regular_at(directory_fd: int, name: str, expected: os.stat_result) -> None:
    """Unlink a verified regular basename under the exclusive CAS-writer lock.

    Identity is checked through a pinned directory descriptor and a no-follow
    file descriptor. As with the build-tree retirer, this assumes cooperating
    same-UID writers honor the advisory lock; POSIX unlink has no conditional
    inode form that can close the final name-to-unlink race against a process
    deliberately ignoring that lock.
    """
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink < 1
            or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)):
        raise RetentionError(f"temporary CAS entry changed before descriptor-bound unlink: {name}")
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink < 1
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
                or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)):
            raise RetentionError(f"temporary CAS entry was replaced before descriptor-bound unlink: {name}")
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(descriptor)

def _rename_exclusive_between(source_fd: int, source: str, target_fd: int, target: str) -> None:
    """Atomically move a directory between pinned parents without replacing a target."""
    import ctypes
    lib = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(lib, "renameatx_np", None)
    if renameatx_np is None:
        raise RetentionError("renameatx_np is unavailable; refusing cross-parent adoption")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 0x00000004)
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), target)
    os.fsync(source_fd)
    if target_fd != source_fd:
        os.fsync(target_fd)

def _read_at(directory_fd: int, name: str, *, limit: int = 128 * 1024 * 1024) -> bytes:
    # Nonblocking open lets fstat reject a FIFO or other special file without
    # hanging the storage controller before it can enforce the regular-file rule.
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RetentionError(f"expected bounded regular file: {name}")
        chunks = []
        size = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
            size += len(block)
            if size > limit:
                raise RetentionError(f"file exceeds limit: {name}")
    finally:
        os.close(fd)

def _write_at(directory_fd: int, name: str, contents: bytes, *, replace: bool) -> None:
    if name in {"", ".", ".."} or "/" in name:
        raise ValueError("descriptor-relative filename must be a basename")
    temp = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
                 dir_fd=directory_fd)
    try:
        view = memoryview(contents)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        if replace:
            try:
                old = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                old = None
            if old is not None and not stat.S_ISREG(old.st_mode):
                raise RetentionError(f"refusing to replace non-regular file {name}")
            os.replace(temp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        else:
            _rename_exclusive(directory_fd, temp, name)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except OSError:
            pass
        raise

def _write_json(path: Path, payload: Mapping[str, Any], *, replace: bool = True, sealed: bool = False) -> bytes:
    parent_fd = _open_dir(path.parent, create=True)
    try:
        value = _seal(payload) if sealed else dict(payload)
        raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        _write_at(parent_fd, path.name, raw, replace=replace)
        if _read_at(parent_fd, path.name) != raw:
            raise RetentionError(f"durable JSON readback mismatch: {path}")
        return raw
    finally:
        os.close(parent_fd)

def _read_json(path: Path, *, sealed: bool = False) -> dict[str, Any]:
    parent_fd = _open_dir(path.parent)
    try:
        raw = _read_at(parent_fd, path.name)
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError(f"invalid JSON: {path}") from error
    if not isinstance(value, dict) or (sealed and not _verify_seal(value)):
        raise RetentionError(f"invalid or unsigned retention record: {path}")
    return value

def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 4 * 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)

def _hash_path(path: Path) -> str:
    parent_fd = _open_dir(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RetentionError(f"not a regular file: {path}")
            return _hash_fd(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)

def _fs_identity(fd: int) -> dict[str, Any]:
    info = os.fstat(fd)
    vfs = os.fstatvfs(fd)
    return {"device": info.st_dev, "inode": info.st_ino, "filesystem_id": getattr(vfs, "f_fsid", None),
            "mode": stat.S_IMODE(info.st_mode), "sticky": bool(info.st_mode & stat.S_ISVTX),
            "owner_uid": info.st_uid}

def _tree_stats(fd: int, root_device: int, root_path: Path | None = None, *,
                allowed_symlink_roots: Iterable[Path] = (),
                max_scan_entries: int | None = None,
                max_logical_bytes: int | None = None) -> dict[str, int]:
    """Count a build tree without following links or crossing filesystems."""
    if max_scan_entries is not None and max_scan_entries < 1:
        raise ValueError("max_scan_entries must be positive when provided")
    if max_logical_bytes is not None and max_logical_bytes < 0:
        raise ValueError("max_logical_bytes cannot be negative")
    root_path = Path(root_path or "/").resolve(strict=True)
    root_fsid = getattr(os.fstatvfs(fd), "f_fsid", None)
    if root_fsid is None:
        raise RetentionError("filesystem identity is unavailable for the build-tree root")
    allowed_symlink_roots = tuple(Path(value).resolve(strict=True) for value in allowed_symlink_roots)
    stack = [(fd, root_path)]
    inode_stats: dict[tuple[int, int], tuple[int, int, int]] = {}
    link_counts: dict[tuple[int, int], int] = {}
    logical = 0
    files = 0
    scanned_entries = 0
    try:
        while stack:
            directory_fd, directory_path = stack.pop()
            try:
                # Iterate incrementally: os.listdir(fd) allocates a complete name
                # list before a caller's entry bound can take effect.
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        name = entry.name
                        if directory_path == root_path and name == BUILD_MARKER:
                            # The authenticated state marker changes during RETIRING; its
                            # bytes are registry metadata, not reclaimable build payload.
                            continue
                        scanned_entries += 1
                        if max_scan_entries is not None and scanned_entries > max_scan_entries:
                            raise RetentionError(
                                "copy-all legacy failure evidence exceeds its bounded entry-count limit")
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raw_target = os.readlink(name, dir_fd=directory_fd)
                            resolved_target = (Path(raw_target) if os.path.isabs(raw_target)
                                               else directory_path / raw_target).resolve(strict=False)
                            if resolved_target != root_path and root_path not in resolved_target.parents:
                                approved_source_link = any(resolved_target == allowed or allowed in resolved_target.parents
                                                          for allowed in allowed_symlink_roots)
                                if not approved_source_link and not _canonical_artifact_target(resolved_target):
                                    raise RetentionError(f"symlink escapes the verified build root: {directory_path / name}")
                            continue
                        if info.st_dev != root_device:
                            raise RetentionError(f"filesystem transition in build tree: {name}")
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                            opened = os.fstat(child)
                            child_fsid = getattr(os.fstatvfs(child), "f_fsid", None)
                            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                    or child_fsid != root_fsid):
                                os.close(child)
                                raise RetentionError(f"directory or filesystem changed during build-tree scan: {name}")
                            stack.append((child, directory_path / name))
                        elif stat.S_ISREG(info.st_mode):
                            key = (info.st_dev, info.st_ino)
                            inode_stats[key] = (getattr(info, "st_blocks", 0) * 512,
                                                info.st_nlink, info.st_size)
                            link_counts[key] = link_counts.get(key, 0) + 1
                            logical += info.st_size
                            if max_logical_bytes is not None and logical > max_logical_bytes:
                                raise RetentionError(
                                    "copy-all legacy failure evidence exceeds its explicit small-root size limit; "
                                    "provide exact capture_paths")
                            files += 1
                        else:
                            raise RetentionError(f"special file in build tree: {name}")
            finally:
                if directory_fd != fd:
                    os.close(directory_fd)
    finally:
        while stack:
            pending_fd, _pending_path = stack.pop()
            if pending_fd != fd:
                try:
                    os.close(pending_fd)
                except OSError:
                    pass
    unique_allocated = sum(row[0] for row in inode_stats.values())
    # An inode is conservatively reclaimable only when all known links are inside this tree.
    recoverable_upper = sum(inode_stats[key][0] for key, count in link_counts.items()
                            if count == inode_stats[key][1])
    return {"file_count": files, "logical_bytes": logical,
            "allocated_unique_inode_bytes": unique_allocated,
            "best_recoverable_bytes_estimate": recoverable_upper}

def _tree_hash(root: Path) -> tuple[str, int]:
    root_fd = _open_dir(root)
    root_dev = os.fstat(root_fd).st_dev
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    if root_fsid is None:
        os.close(root_fd)
        raise RetentionError(f"source tree filesystem identity is unavailable: {root}")
    digest = hashlib.sha256()
    count = 0
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    if relative_dir == Path() and name == ".git":
                        continue
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = (relative_dir / name).as_posix()
                    digest.update(relative.encode("utf-8", "surrogateescape") + b"\0")
                    digest.update(oct(stat.S_IMODE(info.st_mode)).encode() + b"\0")
                    if stat.S_ISLNK(info.st_mode):
                        digest.update(b"L\0" + os.fsencode(os.readlink(name, dir_fd=parent_fd)) + b"\n")
                    elif stat.S_ISDIR(info.st_mode):
                        if info.st_dev != root_dev:
                            raise RetentionError(f"source tree crosses a filesystem boundary: {relative}")
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        child_info = os.fstat(child_fd)
                        if ((child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino)
                                or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                            os.close(child_fd)
                            raise RetentionError(f"source directory changed while hashing: {relative}")
                        digest.update(b"D\n")
                        stack.append((child_fd, relative_dir / name))
                    elif stat.S_ISREG(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        try:
                            now = os.fstat(child_fd)
                            if ((now.st_dev, now.st_ino, now.st_size) != (info.st_dev, info.st_ino, info.st_size)
                                    or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                                raise RetentionError(f"source file changed while hashing: {relative}")
                            digest.update(b"F\0" + _hash_fd(child_fd).encode() + b"\n")
                        finally:
                            os.close(child_fd)
                        count += 1
                    else:
                        raise RetentionError(f"source tree contains unsupported file type: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        return digest.hexdigest(), count
    finally:
        os.close(root_fd)

def _inventory_evidence(root: Path) -> list[dict[str, Any]]:
    """Return a descriptor-walked, exact inventory of immutable retained evidence."""
    root_fd = _open_dir(root)
    root_device = os.fstat(root_fd).st_dev
    rows: list[dict[str, Any]] = []
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = relative_dir / name
                    if info.st_dev != root_device:
                        raise RetentionError(f"evidence tree crosses filesystem boundary: {relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            os.close(child_fd)
                            raise RetentionError(f"evidence directory changed while opening: {relative}")
                        stack.append((child_fd, relative))
                    elif stat.S_ISREG(info.st_mode):
                        file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        try:
                            before = os.fstat(file_fd)
                            digest = _hash_fd(file_fd)
                            after = os.fstat(file_fd)
                            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                                 getattr(before, "st_ctime_ns", 0)) !=
                                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                                 getattr(after, "st_ctime_ns", 0))):
                                raise RetentionError(f"evidence file changed while hashing: {relative}")
                        finally:
                            os.close(file_fd)
                        rows.append({"path": relative.as_posix(), "sha256": digest,
                                     "size_bytes": before.st_size,
                                     "mode": oct(stat.S_IMODE(before.st_mode))})
                    else:
                        raise RetentionError(f"unsupported object in evidence tree: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        return rows
    finally:
        os.close(root_fd)

def _copy_bytes_to_store(source: Path, dest_fd: int, name: str) -> tuple[str, int, int]:
    source_parent = _open_dir(source.parent)
    source_fd = os.open(source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=source_parent)
    temp = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise RetentionError(f"artifact is not a regular file: {source}")
        out_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600, dir_fd=dest_fd)
        digest = hashlib.sha256()
        total = 0
        try:
            while True:
                block = os.read(source_fd, 4 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                total += len(block)
                view = memoryview(block)
                while view:
                    written = os.write(out_fd, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
            if total != before.st_size:
                raise RetentionError(f"artifact changed size during copy: {source}")
            os.fchmod(out_fd, stat.S_IMODE(before.st_mode) & 0o555)
            os.fsync(out_fd)
        except BaseException:
            os.close(out_fd)
            try:
                os.unlink(temp, dir_fd=dest_fd)
            except OSError:
                pass
            raise
        os.close(out_fd)
        got = digest.hexdigest()
        after = os.fstat(source_fd)
        if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
            raise RetentionError(f"artifact source identity changed during copy: {source}")
        _rename_exclusive(dest_fd, temp, name)
        published_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dest_fd)
        try:
            if _hash_fd(published_fd) != got:
                raise RetentionError(f"canonical artifact failed readback: {name}")
        finally:
            os.close(published_fd)
        return got, total, stat.S_IMODE(before.st_mode)
    except BaseException:
        try:
            os.unlink(temp, dir_fd=dest_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(source_fd)
        os.close(source_parent)

def _is_macho(path: Path) -> bool:
    """Recognize linkable/final Mach-O outputs while excluding ordinary .o files."""
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
        if len(header) < 4:
            return False
        magic = header[:4]
        if magic in {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}:
            return True
        byte_order = {b"\xfe\xed\xfa\xce": ">", b"\xce\xfa\xed\xfe": "<",
                      b"\xfe\xed\xfa\xcf": ">", b"\xcf\xfa\xed\xfe": "<"}.get(magic)
        if byte_order:
            import struct
            file_type = struct.unpack(f"{byte_order}I", header[12:16])[0]
            return file_type in {2, 3, 6, 7, 8, 9, 10}
        if magic == b"\x7fELF" and len(header) >= 20:
            import struct
            order = "<" if header[5] == 1 else ">" if header[5] == 2 else None
            if order:
                return struct.unpack(f"{order}H", header[16:18])[0] in {2, 3}
        return False
    except OSError:
        return False


def _thin_macho_architecture(path: Path) -> str:
    """Return the CPU architecture for one thin 64-bit Mach-O output."""
    import struct
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
            file_size = os.fstat(stream.fileno()).st_size
    except OSError as error:
        raise RetentionError(f"cannot read Mach-O header for {path}: {error}") from error
    magic = header[:4]
    fat_layouts = {
        b"\xca\xfe\xba\xbe": (">", 20), b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32), b"\xbf\xba\xfe\xca": ("<", 32),
    }
    if magic in fat_layouts:
        if len(header) < 8:
            raise RetentionError(f"fat Mach-O output has a truncated header: {path}")
        byte_order, entry_size = fat_layouts[magic]
        count = struct.unpack(f"{byte_order}I", header[4:8])[0]
        if count == 0 or count > 64:
            raise RetentionError(f"fat Mach-O output has a malformed architecture count: {path}")
        table_end = 8 + count * entry_size
        if len(header) < min(table_end, 32) or file_size < table_end:
            raise RetentionError(f"fat Mach-O output has a truncated architecture table: {path}")
        raise RetentionError(f"fat Mach-O output is unsupported; build one declared architecture: {path}")
    if len(header) < 32:
        raise RetentionError(f"Mach-O output has a truncated 64-bit header: {path}")
    byte_order = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(magic)
    if byte_order is None:
        raise RetentionError(f"native build output is not a thin 64-bit Mach-O: {path}")
    cputype = struct.unpack(f"{byte_order}I", header[4:8])[0]
    architecture = {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(cputype)
    if architecture is None:
        raise RetentionError(f"native build output has an unsupported CPU type 0x{cputype:x}: {path}")
    return architecture

def _canonical_artifact_target(path: Path) -> bool:
    """Permit only external symlinks whose target is already a verified CAS object."""
    try:
        relative = path.relative_to(ARTIFACT_ROOT)
        if len(relative.parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", relative.parts[0]):
            return False
        return _verify_artifact({"sha256": relative.parts[0], "canonical_artifact_path": str(path)})
    except (ValueError, OSError, RetentionError):
        return False

def _open_artifact_group(digest: str, *, create: bool = False) -> tuple[int, int]:
    """Open one CAS digest directory relative to the pinned store root."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RetentionError("canonical artifact digest is not a lowercase SHA-256")
    root_fd = _open_dir(ARTIFACT_ROOT, create=create)
    try:
        root_info = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if (root_fsid is None or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022):
            raise RetentionError("canonical artifact store root is not owned/private on a verified filesystem")
        try:
            group_fd = os.open(digest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(digest, 0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            group_fd = os.open(digest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        group_info = os.fstat(group_fd)
        group_fsid = getattr(os.fstatvfs(group_fd), "f_fsid", None)
        if (not stat.S_ISDIR(group_info.st_mode) or group_info.st_dev != root_info.st_dev
                or group_info.st_uid != os.getuid() or stat.S_IMODE(group_info.st_mode) & 0o022
                or group_fsid is None or group_fsid != root_fsid):
            os.close(group_fd)
            raise RetentionError("canonical artifact digest directory crossed its verified store filesystem")
        return root_fd, group_fd
    except BaseException:
        os.close(root_fd)
        raise

def canonical_artifact_digests() -> list[str]:
    """Enumerate every canonical CAS group through pinned, same-filesystem fds.

    Gate 0 uses this complete inventory before looking for legacy hardlinks. A
    stray entry or malformed group is an error, so an unreferenced retained
    output cannot evade migration merely because no current runner names it.
    """
    root_fd = _open_dir(ARTIFACT_ROOT)
    try:
        root_info = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if (root_fsid is None or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) & 0o022):
            raise RetentionError("canonical artifact store root is not owned/private on a verified filesystem")
        names = sorted(os.listdir(root_fd))
        digests: list[str] = []
        for digest in names:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RetentionError(f"canonical artifact store contains an unrecognized entry: {digest!r}")
            group_fd = os.open(digest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
            try:
                group_info = os.fstat(group_fd)
                group_fsid = getattr(os.fstatvfs(group_fd), "f_fsid", None)
                if (not stat.S_ISDIR(group_info.st_mode) or group_info.st_dev != root_info.st_dev
                        or group_info.st_uid != os.getuid() or stat.S_IMODE(group_info.st_mode) & 0o022
                        or group_fsid is None or group_fsid != root_fsid):
                    raise RetentionError(f"canonical artifact group crossed its verified store filesystem: {digest}")
                saved = json.loads(_read_at(group_fd, "manifest.json", limit=1024 * 1024))
                if (not isinstance(saved, dict) or saved.get("schema_version") not in {1, 2}
                        or saved.get("sha256") != digest):
                    raise RetentionError(f"canonical artifact group has an invalid manifest: {digest}")
                canonical = Path(saved.get("canonical_path", ""))
                if (canonical.parent != ARTIFACT_ROOT / digest or not canonical.name
                        or canonical.name in {".", ".."} or "/" in canonical.name or "\\" in canonical.name
                        or _artifact_size(saved) is None):
                    raise RetentionError(f"canonical artifact group has an unsafe payload identity: {digest}")
                digests.append(digest)
            finally:
                os.close(group_fd)
        return digests
    finally:
        os.close(root_fd)

def _artifact_size(manifest: Mapping[str, Any]) -> int | None:
    """Read a byte count from either supported CAS manifest schema."""
    size_bytes = manifest.get("size_bytes")
    byte_size = manifest.get("byte_size")
    if size_bytes is not None and byte_size is not None and size_bytes != byte_size:
        return None
    value = size_bytes if size_bytes is not None else byte_size
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value

def _verify_artifact_payload_at(group_fd: int, name: str, digest: str,
                               expected_size: int | None = None, *,
                               allow_legacy_hardlinks: bool = False) -> os.stat_result:
    """Hash one no-follow CAS payload on the already-pinned store filesystem."""
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise RetentionError("canonical artifact payload name is unsafe")
    group_info = os.fstat(group_fd)
    group_fsid = getattr(os.fstatvfs(group_fd), "f_fsid", None)
    payload_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=group_fd)
    try:
        before = os.fstat(payload_fd)
        payload_fsid = getattr(os.fstatvfs(payload_fd), "f_fsid", None)
        if before.st_nlink != 1 and not allow_legacy_hardlinks:
            raise RetentionError("non-legacy canonical artifact is hardlinked")
        if (not stat.S_ISREG(before.st_mode)
                or before.st_dev != group_info.st_dev
                or group_fsid is None or payload_fsid != group_fsid
                or (expected_size is not None and before.st_size != expected_size)):
            raise RetentionError("canonical artifact payload crossed its verified store filesystem or size")
        flags = fcntl.fcntl(payload_fd, fcntl.F_GETFL)
        fcntl.fcntl(payload_fd, fcntl.F_SETFL, flags & ~getattr(os, "O_NONBLOCK", 0))
        digest_actual = _hash_fd(payload_fd)
        after = os.fstat(payload_fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             getattr(before, "st_ctime_ns", 0)) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             getattr(after, "st_ctime_ns", 0))
                or digest_actual != digest):
            raise RetentionError("canonical artifact payload failed SHA-256 or stability verification")
        return after
    finally:
        os.close(payload_fd)

def _detach_temp_name_is_safe(canonical_name: str, temporary_name: Any) -> bool:
    return (isinstance(temporary_name, str)
            and re.fullmatch(rf"\.{re.escape(canonical_name)}\.[0-9a-f]{{32}}\.detach",
                             temporary_name) is not None)

def _verified_bound_recovery_temp(group_fd: int, canonical_name: str, digest: str,
                                  expected_size: int, prior: Mapping[str, Any],
                                  canonical_info: os.stat_result) -> bool:
    """Return whether a durable in-progress receipt already binds a complete copy."""
    if (prior.get("result") not in {"COPY_IN_PROGRESS", "ISOLATION_PREPARED"}
            or prior.get("previous_inode") != canonical_info.st_ino
            or not _detach_temp_name_is_safe(canonical_name, prior.get("temporary_name"))):
        return False
    temporary_name = prior["temporary_name"]
    try:
        temporary_info = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    expected = (prior.get("temporary_device"), prior.get("temporary_inode"))
    if (not all(isinstance(value, int) for value in expected)
            or (temporary_info.st_dev, temporary_info.st_ino) != expected
            or not stat.S_ISREG(temporary_info.st_mode) or temporary_info.st_nlink != 1
            or temporary_info.st_dev != os.fstat(group_fd).st_dev):
        raise RetentionError(f"recovery receipt does not bind its temporary inode: {digest}")
    if (prior.get("result") == "ISOLATION_PREPARED"
            and prior.get("isolated_inode") != temporary_info.st_ino):
        raise RetentionError(f"prepared isolation receipt does not bind its temporary inode: {digest}")
    try:
        verified = _verify_artifact_payload_at(group_fd, temporary_name, digest, expected_size)
    except (OSError, RetentionError) as error:
        if prior.get("result") == "ISOLATION_PREPARED":
            raise RetentionError(f"prepared isolation temporary no longer verifies: {digest}") from error
        return False
    return (verified.st_dev, verified.st_ino) == expected

def _bind_copy_planned_temp(directory_fd: int, receipt: Path, prior: Mapping[str, Any],
                            temporary_name: str, digest: str) -> tuple[dict[str, Any], os.stat_result]:
    """Bind a temp created just before a crash to a durable COPY_IN_PROGRESS receipt.

    COPY_PLANNED is persisted before O_EXCL creation. If power fails between
    creation and recording the new inode, the random receipted basename is
    reopened without following links, checked against the private CAS group,
    and durably rebound before recovery can remove or reuse it.
    """
    if (prior.get("result") != "COPY_PLANNED"
            or not _detach_temp_name_is_safe(Path(str(prior.get("canonical_artifact_path", ""))).name,
                                             temporary_name)):
        raise RetentionError(f"unbound CAS temporary is not backed by a COPY_PLANNED receipt: {digest}")
    group_info = os.fstat(directory_fd)
    group_fsid = getattr(os.fstatvfs(directory_fd), "f_fsid", None)
    if group_fsid is None:
        raise RetentionError(f"CAS filesystem identity is unavailable while rebinding temp: {digest}")
    before = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
    descriptor = os.open(temporary_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        after = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
        temp_fsid = getattr(os.fstatvfs(descriptor), "f_fsid", None)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_dev != group_info.st_dev or temp_fsid != group_fsid
                or opened.st_uid != group_info.st_uid or stat.S_IMODE(opened.st_mode) & 0o077):
            raise RetentionError(f"unbound CAS temporary is not a private group-local regular file: {digest}")
        rebound = dict(prior)
        rebound.update({"temporary_device": opened.st_dev, "temporary_inode": opened.st_ino,
                        "result": "COPY_IN_PROGRESS", "recovered_temp_binding_at_unix": time.time()})
        _write_json(receipt, rebound, replace=True, sealed=True)
        os.fsync(directory_fd)
        return rebound, opened
    finally:
        os.close(descriptor)

def detach_legacy_artifact_hardlinks(digests: Iterable[str], *,
                                     reason: str) -> list[dict[str, Any]]:
    """Copy verified schema-v1 hardlinked CAS objects onto independent inodes.

    Legacy CAS setup linked run outputs directly to the canonical path. New
    schema-v2 artifacts are already copied and must have link count one. This
    migration preserves path, size, and SHA-256 while severing those old aliases.
    """
    actions: list[dict[str, Any]] = []
    lease_fd, lock_fd = _lock()
    try:
        for digest in sorted(set(digests)):
            root_fd, group_fd = _open_artifact_group(digest)
            temporary_name: str | None = None
            temporary_identity: tuple[int, int] | None = None
            exchange_attempted = False
            try:
                saved = json.loads(_read_at(group_fd, "manifest.json", limit=1024 * 1024))
                if not isinstance(saved, dict) or saved.get("sha256") != digest:
                    raise RetentionError(f"artifact manifest does not bind digest {digest}")
                schema = saved.get("schema_version")
                canonical = Path(saved.get("canonical_path", ""))
                if (schema not in {1, 2} or canonical.parent != ARTIFACT_ROOT / digest
                        or not canonical.name or canonical.name in {".", ".."}
                        or "/" in canonical.name or "\\" in canonical.name):
                    raise RetentionError(f"artifact manifest is not a path-safe supported CAS object: {digest}")
                expected_size = _artifact_size(saved)
                if expected_size is None:
                    raise RetentionError(f"artifact size is invalid: {digest}")
                receipt = RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
                prior = (_read_json(receipt, sealed=True) if receipt.exists() or receipt.is_symlink() else None)
                named_temporary = prior.get("temporary_name") if prior is not None else None
                orphan_temps = [name for name in os.listdir(group_fd)
                                if _detach_temp_name_is_safe(canonical.name, name)
                                and name != named_temporary]
                if orphan_temps:
                    raise RetentionError(f"unreceipted CAS isolation temporary blocks migration for {digest}: "
                                         + ", ".join(sorted(orphan_temps)))
                original_info = _verify_artifact_payload_at(
                    group_fd, canonical.name, digest, expected_size,
                    allow_legacy_hardlinks=(schema == 1))

                if schema == 2:
                    if original_info.st_nlink != 1:
                        raise RetentionError(f"new canonical artifact is hardlinked: {digest}")
                    continue

                if prior is not None and (
                        prior.get("receipt_type") != "LEGACY_CAS_HARDLINK_ISOLATION"
                        or prior.get("sha256") != digest
                        or prior.get("canonical_artifact_path") != str(canonical)
                        or prior.get("size_bytes") != expected_size):
                    raise RetentionError(f"legacy artifact isolation receipt conflicts with canonical content: {digest}")

                if original_info.st_nlink == 1:
                    if prior is None:
                        continue
                    if prior.get("result") in {
                            "DETACHED_WITH_HASH_PRESERVED",
                            "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED"}:
                        identity_field = ("isolated_inode" if prior.get("result") == "DETACHED_WITH_HASH_PRESERVED"
                                          else "previous_inode")
                        if prior.get(identity_field) != original_info.st_ino:
                            raise RetentionError(f"completed isolation receipt does not bind the canonical inode: {digest}")
                        if prior.get("isolated_link_count", 1) != 1:
                            raise RetentionError(f"completed isolation receipt has an invalid link count: {digest}")
                        continue
                    if prior.get("result") in {"COPY_PLANNED", "COPY_IN_PROGRESS", "ISOLATION_PREPARED"} \
                            and prior.get("previous_inode") == original_info.st_ino:
                        old_temp_name = prior.get("temporary_name")
                        if old_temp_name is not None:
                            if not _detach_temp_name_is_safe(canonical.name, old_temp_name):
                                raise RetentionError(f"planned CAS temporary name is unsafe: {digest}")
                            try:
                                old_temp_info = os.stat(old_temp_name, dir_fd=group_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                old_temp_info = None
                            if old_temp_info is not None:
                                expected_temp = (prior.get("temporary_device"), prior.get("temporary_inode"))
                                if (not all(isinstance(value, int) for value in expected_temp)
                                        or (old_temp_info.st_dev, old_temp_info.st_ino) != expected_temp):
                                    if prior.get("result") != "COPY_PLANNED":
                                        raise RetentionError(
                                            f"unbound CAS temporary blocks alias-free recovery: {digest}")
                                    prior, old_temp_info = _bind_copy_planned_temp(
                                        group_fd, receipt, prior, old_temp_name, digest)
                                if (not stat.S_ISREG(old_temp_info.st_mode) or old_temp_info.st_nlink != 1):
                                    raise RetentionError(
                                        f"alias-free CAS temporary is not a private regular file: {digest}")
                                if prior.get("result") == "ISOLATION_PREPARED":
                                    # If the old canonical inode remains at the canonical
                                    # name after its aliases disappeared, the prepared
                                    # isolated inode is only a redundant temporary. Bind
                                    # and hash it before discarding it; the canonical
                                    # payload itself remains the original verified inode.
                                    prepared_temp = _verify_artifact_payload_at(
                                        group_fd, old_temp_name, digest, expected_size)
                                    if prepared_temp.st_ino != prior.get("isolated_inode"):
                                        raise RetentionError(
                                            f"prepared CAS temporary changed during alias-free recovery: {digest}")
                                _unlink_pinned_regular_at(group_fd, old_temp_name, old_temp_info)
                        recovered = dict(prior)
                        recovered.update({"isolated_inode": original_info.st_ino,
                                          "isolated_link_count": 1,
                                          "completed_at_unix": time.time(),
                                          "result": "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED"})
                        _write_json(receipt, recovered, replace=True, sealed=True)
                        actions.append({"sha256": digest, "path": str(canonical),
                                        "logical_bytes": expected_size,
                                        "state_before": "LEGACY_HARDLINKS_ALREADY_GONE",
                                        "action": "RECOVER_ALIAS_FREE_CAS_ARTIFACT",
                                        "result": "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED",
                                        "receipt_path": str(receipt)})
                        continue
                    if prior.get("result") != "ISOLATION_PREPARED":
                        raise RetentionError(f"prepared isolation receipt is in an invalid state: {digest}")
                    if (prior.get("isolated_inode") != original_info.st_ino
                            or prior.get("isolated_link_count") != 1
                            or not _detach_temp_name_is_safe(canonical.name, prior.get("temporary_name"))):
                        raise RetentionError(f"prepared isolation receipt does not bind the current canonical inode: {digest}")
                    old_temp_name = prior["temporary_name"]
                    try:
                        displaced = os.stat(old_temp_name, dir_fd=group_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        displaced = None
                    if displaced is not None:
                        expected_previous_device = prior.get("previous_device", os.fstat(group_fd).st_dev)
                        if (not isinstance(expected_previous_device, int)
                                or not stat.S_ISREG(displaced.st_mode)
                                or (displaced.st_dev, displaced.st_ino) !=
                                (expected_previous_device, prior.get("previous_inode"))):
                            raise RetentionError(f"prepared isolation displaced inode changed: {digest}")
                        # The old inode can have been modified through another
                        # legacy hardlink after the exchange. Retire only this
                        # receipt-bound directory entry; preserve all aliases.
                        _unlink_pinned_regular_at(group_fd, old_temp_name, displaced)
                    recovered = dict(prior)
                    recovered.update({"completed_at_unix": time.time(),
                                      "result": "DETACHED_WITH_HASH_PRESERVED"})
                    _write_json(receipt, recovered, replace=True, sealed=True)
                    actions.append({"sha256": digest, "path": str(canonical),
                                    "logical_bytes": expected_size,
                                    "state_before": f"LEGACY_HARDLINKED_{prior.get('previous_link_count')}",
                                    "action": "RECOVER_PREPARED_CAS_HARDLINK_ISOLATION",
                                    "result": "DETACHED_WITH_HASH_PRESERVED",
                                    "receipt_path": str(receipt)})
                    continue

                source_fd = os.open(canonical.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                    | getattr(os, "O_NONBLOCK", 0), dir_fd=group_fd)
                try:
                    source_info = os.fstat(source_fd)
                    if ((source_info.st_dev, source_info.st_ino, source_info.st_nlink) !=
                            (original_info.st_dev, original_info.st_ino, original_info.st_nlink)
                            or source_info.st_nlink < 2 or not stat.S_ISREG(source_info.st_mode)):
                        raise RetentionError(f"legacy canonical artifact changed before hardlink isolation: {digest}")
                    source_flags = fcntl.fcntl(source_fd, fcntl.F_GETFL)
                    fcntl.fcntl(source_fd, fcntl.F_SETFL, source_flags & ~getattr(os, "O_NONBLOCK", 0))

                    # Persist the random basename before creating it. On restart,
                    # this lets the controller recognize and reclaim/resume an
                    # interrupted copy rather than accumulate untracked large files.
                    temporary_name = (prior.get("temporary_name")
                                      if prior is not None and prior.get("result") in {
                                          "COPY_PLANNED", "COPY_IN_PROGRESS", "ISOLATION_PREPARED"} else None)
                    if temporary_name is not None and not _detach_temp_name_is_safe(canonical.name, temporary_name):
                        raise RetentionError(f"prepared CAS temporary name is unsafe: {digest}")
                    existing_temp_info = None
                    if temporary_name is not None:
                        try:
                            existing_temp_info = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            existing_temp_info = None
                    if existing_temp_info is not None:
                        if not stat.S_ISREG(existing_temp_info.st_mode) or existing_temp_info.st_nlink != 1:
                            raise RetentionError(f"interrupted CAS temporary is not a private regular file: {digest}")
                        expected_temp = (prior.get("temporary_device"), prior.get("temporary_inode")) \
                            if prior is not None else (None, None)
                        if (not all(isinstance(value, int) for value in expected_temp)
                                or (existing_temp_info.st_dev, existing_temp_info.st_ino) != expected_temp):
                            if prior is None or prior.get("result") != "COPY_PLANNED":
                                raise RetentionError(
                                    f"interrupted CAS temporary lacks a durable inode binding: {digest}")
                            prior, existing_temp_info = _bind_copy_planned_temp(
                                group_fd, receipt, prior, temporary_name, digest)
                            expected_temp = (prior["temporary_device"], prior["temporary_inode"])
                        try:
                            isolated = _verify_artifact_payload_at(
                                group_fd, temporary_name, digest, expected_size)
                        except (OSError, RetentionError) as error:
                            if prior is not None and prior.get("result") == "ISOLATION_PREPARED":
                                raise RetentionError(
                                    f"prepared CAS temporary no longer matches its durable artifact hash: {digest}") from error
                            # A partial copy is removable only while the signed
                            # plan still names it and descriptor/leaf identities agree.
                            stale_fd = os.open(temporary_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                               | getattr(os, "O_NONBLOCK", 0), dir_fd=group_fd)
                            try:
                                stale_info = os.fstat(stale_fd)
                                if (not stat.S_ISREG(stale_info.st_mode) or stale_info.st_nlink != 1
                                        or (stale_info.st_dev, stale_info.st_ino) !=
                                        expected_temp):
                                    raise RetentionError(f"interrupted CAS temporary changed during recovery: {digest}") from error
                            finally:
                                os.close(stale_fd)
                            _unlink_pinned_regular_at(group_fd, temporary_name, stale_info)
                            temporary_name = None
                            existing_temp_info = None
                        else:
                            if (prior is not None and prior.get("result") == "ISOLATION_PREPARED"
                                    and prior.get("isolated_inode") != isolated.st_ino):
                                raise RetentionError(f"prepared CAS temporary inode changed: {digest}")
                    if temporary_name is None or existing_temp_info is None:
                        temporary_name = f".{canonical.name}.{uuid.uuid4().hex}.detach"
                        plan = {
                            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
                            "sha256": digest, "canonical_artifact_path": str(canonical),
                            "size_bytes": expected_size, "previous_device": original_info.st_dev,
                            "previous_inode": original_info.st_ino,
                            "previous_link_count": original_info.st_nlink,
                            "temporary_name": temporary_name, "reason": reason,
                            "prepared_at_unix": time.time(), "result": "COPY_PLANNED",
                        }
                        _write_json(receipt, plan, replace=True, sealed=True)
                        output_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                            | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=group_fd)
                        try:
                            output_info = os.fstat(output_fd)
                            temporary_identity = (output_info.st_dev, output_info.st_ino)
                            progress = dict(plan)
                            progress.update({"temporary_device": output_info.st_dev,
                                             "temporary_inode": output_info.st_ino,
                                             "result": "COPY_IN_PROGRESS"})
                            _write_json(receipt, progress, replace=True, sealed=True)
                            os.fsync(group_fd)
                            hasher = hashlib.sha256()
                            copied = 0
                            os.lseek(source_fd, 0, os.SEEK_SET)
                            while True:
                                block = os.read(source_fd, 4 * 1024 * 1024)
                                if not block:
                                    break
                                hasher.update(block)
                                copied += len(block)
                                view = memoryview(block)
                                while view:
                                    written = os.write(output_fd, view)
                                    if written <= 0:
                                        raise OSError("short write while isolating legacy canonical artifact")
                                    view = view[written:]
                            if copied != expected_size or hasher.hexdigest() != digest:
                                raise RetentionError(f"legacy canonical artifact changed during hardlink isolation: {digest}")
                            os.fchmod(output_fd, stat.S_IMODE(source_info.st_mode))
                            os.fsync(output_fd)
                        finally:
                            os.close(output_fd)
                        existing_temp_info = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
                    else:
                        temporary_identity = (existing_temp_info.st_dev, existing_temp_info.st_ino)
                        isolated = _verify_artifact_payload_at(group_fd, temporary_name, digest, expected_size)

                    source_after = os.fstat(source_fd)
                    if ((source_info.st_dev, source_info.st_ino, source_info.st_size,
                         source_info.st_mtime_ns, getattr(source_info, "st_ctime_ns", 0)) !=
                        (source_after.st_dev, source_after.st_ino, source_after.st_size,
                         source_after.st_mtime_ns, getattr(source_after, "st_ctime_ns", 0))):
                        raise RetentionError(f"legacy canonical artifact changed during isolation: {digest}")
                finally:
                    os.close(source_fd)

                isolated = _verify_artifact_payload_at(group_fd, temporary_name, digest, expected_size)
                temporary_identity = (isolated.st_dev, isolated.st_ino)
                prepared = {
                    "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
                    "sha256": digest, "canonical_artifact_path": str(canonical),
                    "size_bytes": expected_size, "previous_device": original_info.st_dev,
                    "previous_inode": original_info.st_ino,
                    "previous_link_count": original_info.st_nlink,
                    "isolated_inode": isolated.st_ino, "isolated_link_count": isolated.st_nlink,
                    "temporary_device": isolated.st_dev, "temporary_inode": isolated.st_ino,
                    "temporary_name": temporary_name, "reason": reason,
                    "prepared_at_unix": time.time(), "result": "ISOLATION_PREPARED",
                }
                _write_json(receipt, prepared, replace=True, sealed=True)
                current = os.stat(canonical.name, dir_fd=group_fd, follow_symlinks=False)
                temp_current = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
                if ((current.st_dev, current.st_ino, current.st_nlink) !=
                        (original_info.st_dev, original_info.st_ino, original_info.st_nlink)
                        or (temp_current.st_dev, temp_current.st_ino) != temporary_identity):
                    raise RetentionError(f"legacy artifact names changed before atomic exchange: {digest}")
                exchange_attempted = True
                try:
                    _rename_swap_at(group_fd, temporary_name, canonical.name)
                except OSError as error:
                    raise RetentionError(f"atomic CAS isolation exchange failed for {digest}: {error}") from error

                current_after = os.stat(canonical.name, dir_fd=group_fd, follow_symlinks=False)
                displaced_after = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
                canonical_is_isolated = (stat.S_ISREG(current_after.st_mode)
                                         and (current_after.st_dev, current_after.st_ino) == temporary_identity)
                displaced_is_original = (stat.S_ISREG(displaced_after.st_mode)
                                         and (displaced_after.st_dev, displaced_after.st_ino) ==
                                         (original_info.st_dev, original_info.st_ino))
                if not (canonical_is_isolated and displaced_is_original):
                    # RENAME_SWAP never discards the substituted entry. If one
                    # side still has a known identity, swap back atomically so a
                    # raced destination is restored instead of overwritten.
                    can_rollback = canonical_is_isolated or displaced_is_original
                    if can_rollback:
                        _rename_swap_at(group_fd, temporary_name, canonical.name)
                    raise RetentionError(f"canonical artifact identity changed during atomic exchange: {digest}")

                previous_device = prepared.get("previous_device", os.fstat(group_fd).st_dev)
                if (not isinstance(previous_device, int) or not stat.S_ISREG(displaced_after.st_mode)
                        or (displaced_after.st_dev, displaced_after.st_ino) !=
                        (previous_device, original_info.st_ino)):
                    _rename_swap_at(group_fd, temporary_name, canonical.name)
                    raise RetentionError(f"canonical artifact displaced an unexpected inode: {digest}")
                _unlink_pinned_regular_at(group_fd, temporary_name, displaced_after)
                final_info = _verify_artifact_payload_at(group_fd, canonical.name, digest, expected_size)
                if final_info.st_nlink != 1 or final_info.st_ino != isolated.st_ino:
                    raise RetentionError(f"isolated canonical artifact still has hardlinks: {digest}")
                record = dict(prepared)
                record.update({"isolated_inode": final_info.st_ino,
                               "isolated_link_count": final_info.st_nlink,
                               "completed_at_unix": time.time(),
                               "result": "DETACHED_WITH_HASH_PRESERVED"})
                _write_json(receipt, record, replace=True, sealed=True)
                actions.append({"sha256": digest, "path": str(canonical),
                                "logical_bytes": expected_size,
                                "state_before": f"LEGACY_HARDLINKED_{original_info.st_nlink}",
                                "action": "DETACH_LEGACY_CAS_HARDLINKS",
                                "result": "DETACHED_WITH_HASH_PRESERVED",
                                "receipt_path": str(receipt)})
            finally:
                if (not exchange_attempted and temporary_name is not None
                        and temporary_identity is not None):
                    try:
                        temporary_info = os.stat(temporary_name, dir_fd=group_fd, follow_symlinks=False)
                        if ((temporary_info.st_dev, temporary_info.st_ino) == temporary_identity
                                and stat.S_ISREG(temporary_info.st_mode) and temporary_info.st_nlink == 1):
                            _unlink_pinned_regular_at(group_fd, temporary_name, temporary_info)
                    except FileNotFoundError:
                        pass
                os.close(group_fd)
                os.close(root_fd)
    finally:
        _unlock(lease_fd, lock_fd)
    return actions

def legacy_artifact_hardlink_estimate(digests: Iterable[str]) -> list[dict[str, Any]]:
    """Return verified schema-v1 CAS copies required to isolate linked payloads.

    This is a no-write preflight used to prove the copy fits within Gate 0's
    free-space and project budgets before the canonical inode is replaced.
    Hash verification is repeated by the mutating detach operation.
    """
    rows: list[dict[str, Any]] = []
    lease_fd, lock_fd = _lock()
    try:
        for digest in sorted(set(digests)):
            root_fd, group_fd = _open_artifact_group(digest)
            try:
                saved = json.loads(_read_at(group_fd, "manifest.json", limit=1024 * 1024))
                if not isinstance(saved, dict) or saved.get("sha256") != digest:
                    raise RetentionError(f"artifact manifest does not bind digest {digest}")
                canonical = Path(saved.get("canonical_path", ""))
                if (saved.get("schema_version") not in {1, 2} or canonical.parent != ARTIFACT_ROOT / digest
                        or not canonical.name or canonical.name in {".", ".."}
                        or "/" in canonical.name or "\\" in canonical.name):
                    raise RetentionError(f"artifact manifest schema or path is invalid: {digest}")
                expected_size = _artifact_size(saved)
                if expected_size is None:
                    raise RetentionError(f"legacy artifact size is invalid: {digest}")
                info = _verify_artifact_payload_at(
                    group_fd, canonical.name, digest, expected_size,
                    allow_legacy_hardlinks=saved.get("schema_version") == 1)
                if info.st_nlink > 1 and saved.get("schema_version") != 1:
                    raise RetentionError(f"non-legacy canonical artifact is hardlinked: {digest}")
                if info.st_nlink > 1:
                    recovery_pending = False
                    complete_temp = False
                    receipt = RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
                    if saved.get("schema_version") == 1 and (receipt.exists() or receipt.is_symlink()):
                        prior = _read_json(receipt, sealed=True)
                        if (prior.get("receipt_type") != "LEGACY_CAS_HARDLINK_ISOLATION"
                                or prior.get("sha256") != digest
                                or prior.get("canonical_artifact_path") != str(canonical)
                                or prior.get("size_bytes") != expected_size):
                            raise RetentionError(f"legacy isolation receipt conflicts during recovery estimate: {digest}")
                        if prior.get("previous_inode") != info.st_ino:
                            raise RetentionError(f"legacy isolation receipt does not bind the linked canonical inode: {digest}")
                        if prior.get("result") not in {"COPY_PLANNED", "COPY_IN_PROGRESS", "ISOLATION_PREPARED"}:
                            raise RetentionError(f"legacy isolation receipt conflicts with linked canonical state: {digest}")
                        recovery_pending = True
                        complete_temp = _verified_bound_recovery_temp(
                            group_fd, canonical.name, digest, expected_size, prior, info)
                    row = {"sha256": digest, "size_bytes": 0 if complete_temp else info.st_size,
                           "link_count": info.st_nlink,
                           "copy_required": not complete_temp,
                           "recovery_required": recovery_pending or complete_temp,
                           "canonical_artifact_path": str(canonical)}
                    rows.append(row)
                elif saved.get("schema_version") == 1:
                    receipt = RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
                    if receipt.exists() or receipt.is_symlink():
                        prior = _read_json(receipt, sealed=True)
                        if (prior.get("receipt_type") != "LEGACY_CAS_HARDLINK_ISOLATION"
                                or prior.get("sha256") != digest
                                or prior.get("canonical_artifact_path") != str(canonical)
                                or prior.get("size_bytes") != expected_size):
                            raise RetentionError(f"legacy isolation receipt conflicts during recovery estimate: {digest}")
                        if prior.get("result") == "DETACHED_WITH_HASH_PRESERVED":
                            if (prior.get("isolated_inode") != info.st_ino
                                    or prior.get("isolated_link_count") != 1):
                                raise RetentionError(f"completed isolation receipt no longer binds canonical inode: {digest}")
                        elif prior.get("result") == "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED":
                            if (prior.get("previous_inode") != info.st_ino
                                    or prior.get("isolated_link_count") != 1):
                                raise RetentionError(f"alias-free recovery receipt no longer binds canonical inode: {digest}")
                        elif (prior.get("result") == "ISOLATION_PREPARED"
                              and prior.get("isolated_inode") == info.st_ino):
                            if (prior.get("isolated_link_count") != 1
                                    or not _detach_temp_name_is_safe(canonical.name,
                                                                     prior.get("temporary_name"))):
                                raise RetentionError(f"prepared isolation receipt does not bind recovery target: {digest}")
                            rows.append({"sha256": digest, "size_bytes": 0,
                                         "link_count": info.st_nlink,
                                         "copy_required": False, "recovery_required": True,
                                         "canonical_artifact_path": str(canonical)})
                        elif (prior.get("result") in {"COPY_PLANNED", "COPY_IN_PROGRESS", "ISOLATION_PREPARED"}
                              and prior.get("previous_inode") == info.st_ino
                              and _detach_temp_name_is_safe(canonical.name, prior.get("temporary_name"))):
                            temp_name = prior["temporary_name"]
                            try:
                                temp_info = os.stat(temp_name, dir_fd=group_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                temp_info = None
                            if temp_info is not None:
                                expected_temporary = (prior.get("temporary_device"), prior.get("temporary_inode"))
                                if expected_temporary == (None, None) and prior.get("result") == "COPY_PLANNED":
                                    # An alias may disappear after the durable plan and
                                    # O_EXCL temp creation but before its inode binding.
                                    # The canonical payload is already verified and has
                                    # nlink=1, so no new copy is required. The detacher
                                    # will bind this candidate through no-follow fds and
                                    # verify owner, mode, device, and filesystem before
                                    # it may unlink anything.
                                    pass
                                elif (not all(isinstance(value, int) for value in expected_temporary)
                                      or (temp_info.st_dev, temp_info.st_ino) != expected_temporary):
                                    raise RetentionError(
                                        f"recovery receipt does not bind its temporary inode: {digest}")
                            rows.append({"sha256": digest, "size_bytes": 0,
                                         "link_count": info.st_nlink,
                                         "copy_required": False, "recovery_required": True,
                                         "canonical_artifact_path": str(canonical)})
                        else:
                            raise RetentionError(f"legacy isolation receipt has an unresolved state: {digest}")
            finally:
                os.close(group_fd)
                os.close(root_fd)
    finally:
        _unlock(lease_fd, lock_fd)
    return rows

def canonical_artifact_store_audit() -> dict[str, Any]:
    """Hash and fingerprint the complete CAS identity/link-count inventory.

    Legacy schema-v1 objects may still share an inode with retained source or
    runtime trees. Gate 0 isolates every artifact referenced by retained
    evidence, while this whole-store audit makes every remaining shared inode
    explicit and detects link-count/content changes before and after a run.
    """
    digests = canonical_artifact_digests()
    rows = legacy_artifact_hardlink_estimate(digests)
    normalized = [{key: row[key] for key in (
        "sha256", "size_bytes", "link_count", "copy_required", "recovery_required")}
        for row in rows]
    payload = _canonical_json({"canonical_artifact_digests": digests,
                               "shared_or_recovering_payloads": normalized})
    return {
        "artifact_count": len(digests),
        "shared_or_recovering_payload_count": len(normalized),
        "shared_payload_logical_bytes": sum(int(row["size_bytes"]) for row in normalized
                                              if row["copy_required"]),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

def _artifactize(path: Path) -> dict[str, Any]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RetentionError(f"refusing non-regular output: {path}")
    digest = _hash_path(path)
    group = ARTIFACT_ROOT / digest
    root_fd, group_fd = _open_artifact_group(digest, create=True)
    try:
        try:
            raw = _read_at(group_fd, "manifest.json", limit=1024 * 1024)
            saved = json.loads(raw)
        except FileNotFoundError:
            saved = None
        if saved is not None:
            canonical = Path(saved.get("canonical_path", ""))
            name = canonical.name
            if (saved.get("sha256") != digest or canonical.parent != group
                    or not name or name in {".", ".."} or "/" in name or "\\" in name):
                raise RetentionError(f"existing artifact manifest conflicts with {digest}")
            expected_size = _artifact_size(saved)
            if expected_size is None:
                raise RetentionError(f"existing artifact manifest has no supported payload size: {digest}")
            _verify_artifact_payload_at(group_fd, name, digest, expected_size)
        else:
            name = path.name
            try:
                info = os.stat(name, dir_fd=group_fd, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=group_fd)
                try:
                    payload_info = os.fstat(fd)
                    if (not stat.S_ISREG(payload_info.st_mode) or payload_info.st_nlink != 1
                            or _hash_fd(fd) != digest):
                        raise RetentionError(f"existing unmanifested CAS payload conflicts: {group / name}")
                finally:
                    os.close(fd)
                size, mode = info.st_size, stat.S_IMODE(info.st_mode)
            else:
                got, size, mode = _copy_bytes_to_store(path, group_fd, name)
                if got != digest:
                    raise RetentionError(f"artifact hash changed during canonical copy: {path}")
            canonical = group / name
            payload_info = _verify_artifact_payload_at(group_fd, name, digest, size)
            record = {"schema_version": 2, "sha256": digest, "canonical_path": str(canonical),
                      "size_bytes": size, "stored_mode": oct(stat.S_IMODE(payload_info.st_mode)),
                      "original_mode": oct(mode), "created_at_unix": time.time()}
            _write_at(group_fd, "manifest.json", (json.dumps(record, indent=2, sort_keys=True) + "\n").encode(),
                      replace=False)
            saved = record
        expected_size = _artifact_size(saved)
        if expected_size is None:
            raise RetentionError(f"canonical artifact manifest has no supported payload size: {digest}")
        payload_info = _verify_artifact_payload_at(group_fd, canonical.name, digest, expected_size)
        if not _verify_artifact({"sha256": digest, "canonical_artifact_path": str(canonical)}):
            raise RetentionError(f"canonical artifact failed descriptor-bound store verification: {canonical}")
        return {"sha256": digest, "canonical_artifact_path": str(canonical),
                "artifact_manifest_path": str(group / "manifest.json"), "size_bytes": payload_info.st_size,
                "artifact_name": canonical.name, "source_build_path": str(path),
                "source_build_hard_link_count": before.st_nlink}
    finally:
        os.close(group_fd)
        os.close(root_fd)

def _copy_evidence(source: Path, target: Path, *, max_bytes: int | None = None) -> dict[str, Any]:
    source_parent = _open_dir(source.parent)
    try:
        source_fd = os.open(source.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_NONBLOCK", 0), dir_fd=source_parent)
    finally:
        os.close(source_parent)
    try:
        return _copy_evidence_from_fd(source_fd, source, target, max_bytes=max_bytes)
    finally:
        os.close(source_fd)

def _copy_evidence_from_fd(source_fd: int, source: Path, target: Path, *,
                           max_bytes: int | None = None) -> dict[str, Any]:
    """Copy from an already pinned no-follow regular-file descriptor."""
    target_fd = _open_dir(target.parent, create=True)
    try:
        temp = f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        src_info = os.fstat(source_fd)
        if not stat.S_ISREG(src_info.st_mode):
            raise RetentionError(f"evidence input is not a regular file: {source}")
        if max_bytes is not None and src_info.st_size > max_bytes:
            raise RetentionError(f"evidence input exceeds its capture byte limit: {source}")
        if _exists_at(target_fd, target.name):
            existing_fd = os.open(target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=target_fd)
            try:
                existing_info = os.fstat(existing_fd)
                if not stat.S_ISREG(existing_info.st_mode):
                    raise RetentionError(f"existing evidence is not a regular file: {target}")
                existing_hash = _hash_fd(existing_fd)
            finally:
                os.close(existing_fd)
            if src_info.st_size != existing_info.st_size:
                raise RetentionError(f"existing evidence conflicts with source size: {target}")
            source_hash = _hash_fd(source_fd)
            if existing_hash != source_hash:
                raise RetentionError(f"existing evidence conflicts with source hash: {target}")
            return {"source_path": str(source), "retained_path": str(target), "sha256": source_hash,
                    "size_bytes": src_info.st_size, "mode": oct(stat.S_IMODE(src_info.st_mode))}
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600,
                      dir_fd=target_fd)
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                block = os.read(source_fd, 2 * 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                if max_bytes is not None and size > max_bytes:
                    raise RetentionError(f"evidence input grew beyond its capture byte limit: {source}")
                view = memoryview(block)
                while view:
                    written = os.write(out, view)
                    if written <= 0:
                        raise OSError("short evidence write")
                    view = view[written:]
            if size != src_info.st_size:
                raise RetentionError(f"evidence source changed size during copy: {source}")
            os.fsync(out)
        finally:
            os.close(out)
        after = os.fstat(source_fd)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                getattr(after, "st_ctime_ns", 0)) != (
                src_info.st_dev, src_info.st_ino, src_info.st_size, src_info.st_mtime_ns,
                getattr(src_info, "st_ctime_ns", 0)):
            raise RetentionError(f"evidence source changed during copy: {source}")
        if _hash_fd(source_fd) != digest.hexdigest():
            raise RetentionError(f"evidence source content changed during copy: {source}")
        _rename_exclusive(target_fd, temp, target.name)
        got = digest.hexdigest()
        published_fd = os.open(target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=target_fd)
        try:
            if _hash_fd(published_fd) != got:
                raise RetentionError(f"retained evidence failed hash verification: {target}")
        finally:
            os.close(published_fd)
        return {"source_path": str(source), "retained_path": str(target), "sha256": got,
                "size_bytes": size, "mode": oct(stat.S_IMODE(src_info.st_mode))}
    except BaseException:
        try:
            os.unlink(temp, dir_fd=target_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(target_fd)

def _open_leased_file_fd(lease: Mapping[str, Any], relative: str) -> int:
    """Open a regular build file beneath the exact leased root without path re-resolution."""
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise RetentionError(f"unsafe leased build-relative file path: {relative}")
    root = Path(str(lease["path"]))
    root_fd = _open_dir(root)
    directory_fds = [root_fd]
    try:
        root_info = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        marker = _read_marker(root_fd)
        if ((root_info.st_dev, root_info.st_ino) != (lease.get("build_device"), lease.get("build_inode"))
                or root_fsid != lease.get("build_filesystem_id")
                or marker.get("build_id") != lease.get("build_id")
                or marker.get("path") != str(root)
                or marker.get("state") != lease.get("state")):
            raise RetentionError("leased build root identity changed before evidence capture")
        parent_fd = root_fd
        for component in candidate.parts[:-1]:
            child_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            child_info = os.fstat(child_fd)
            if ((child_info.st_dev != root_info.st_dev)
                    or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                os.close(child_fd)
                raise RetentionError(f"leased diagnostic directory crosses its build filesystem: {relative}")
            directory_fds.append(child_fd)
            parent_fd = child_fd
        try:
            file_fd = os.open(candidate.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                              | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RetentionError(f"leased diagnostic path is a symlink: {relative}") from error
            raise
        file_info = os.fstat(file_fd)
        if (not stat.S_ISREG(file_info.st_mode) or file_info.st_dev != root_info.st_dev
                or getattr(os.fstatvfs(file_fd), "f_fsid", None) != root_fsid):
            os.close(file_fd)
            raise RetentionError(f"leased diagnostic is not a regular file on the build filesystem: {relative}")
        return file_fd
    finally:
        for fd in reversed(directory_fds):
            os.close(fd)

def _validated_failure_capture_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_FAILURE_CAPTURE_FILES:
        raise ValueError(f"failure_capture_paths must contain at most {MAX_FAILURE_CAPTURE_FILES} relative paths")
    result: list[str] = []
    for item in value:
        if (not isinstance(item, str) or not item or "\\" in item or "\x00" in item
                or item in {BUILD_MARKER, BUILD_EXECUTION_RECEIPT}):
            raise ValueError("failure capture paths must be non-empty POSIX-relative files")
        if len(item.encode("utf-8", "surrogateescape")) > 1024:
            raise ValueError("failure capture path exceeds its 1024-byte limit")
        relative = Path(item)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe failure capture path: {item}")
        normalized = relative.as_posix()
        if normalized != item or normalized in result:
            raise ValueError(f"failure capture path is non-normalized or duplicated: {item}")
        if sum(len(existing.encode("utf-8", "surrogateescape")) for existing in result) \
                + len(item.encode("utf-8", "surrogateescape")) > 16 * 1024:
            raise ValueError("failure capture paths exceed their aggregate 16 KiB limit")
        result.append(normalized)
    return result

def _validate_failure_command(command: Any) -> list[str]:
    if not isinstance(command, (list, tuple)) or not command or len(command) > 256:
        raise ValueError("failure command must contain between 1 and 256 arguments")
    values = []
    for item in command:
        if not isinstance(item, str) or "\x00" in item:
            raise ValueError("failure command arguments must be NUL-free strings")
        values.append(item)
    if len(_canonical_json(values)) > MAX_FAILURE_COMMAND_BYTES:
        raise ValueError("failure command exceeds its bounded evidence size")
    return values

def _bounded_failure_error(error: str | None) -> tuple[str | None, str | None]:
    if error is None:
        return None, None
    if not isinstance(error, str):
        raise ValueError("failure error detail must be text")
    raw = error.encode("utf-8", "surrogateescape")
    if len(raw) <= MAX_FAILURE_ERROR_BYTES:
        return error, None
    clipped = raw[:MAX_FAILURE_ERROR_BYTES].decode("utf-8", "replace")
    return clipped, hashlib.sha256(raw).hexdigest()

def _check_failure_manifest_size(record: Mapping[str, Any]) -> None:
    encoded = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_FAILURE_MANIFEST_BYTES:
        raise RetentionError("failure evidence manifest exceeds its 1 MiB limit")

def _write_evidence_bytes(target: Path, contents: bytes) -> dict[str, Any]:
    """Durably retain in-memory provenance without following destination links."""
    target_fd = _open_dir(target.parent, create=True)
    digest = hashlib.sha256(contents).hexdigest()
    try:
        if _exists_at(target_fd, target.name):
            existing = _read_at(target_fd, target.name, limit=max(len(contents), 1) + 1)
            if hashlib.sha256(existing).hexdigest() != digest or existing != contents:
                raise RetentionError(f"existing evidence conflicts with new provenance: {target}")
        else:
            _write_at(target_fd, target.name, contents, replace=False)
        return {"retained_path": str(target), "sha256": digest, "size_bytes": len(contents)}
    finally:
        os.close(target_fd)

def _empty_identity() -> dict[str, Any]:
    return {"build_system": "unknown", "source_identity": None, "patch_set": None,
            "cross_file": None, "meson_options": None, "compiler_identity": None,
            "linker_identity": None, "generated_shader_identity": [], "build_type": None}

def configuration_key_from_identity(identity: Mapping[str, Any], *, dependency_state: Any = None) -> str:
    """Compute the required pre-build key from frozen source/tool/configuration identity.

    Callers must resolve generated-shader identity from deterministic source inputs
    before reserving a lease; completion recomputes this key from captured evidence.
    """
    build_system = identity.get("build_system")
    if build_system == "xcode_native":
        raw_request = identity.get("native_moltenvk_request")
        if not isinstance(raw_request, Mapping):
            raise ValueError("xcode_native configuration identity requires a native_moltenvk_request mapping")
        return compute_native_moltenvk_config_key(_dict_to_native_request(raw_request))
    source = identity.get("source_identity")
    source_hash = source.get("content_tree_sha256") if isinstance(source, Mapping) else None
    if build_system not in {"meson", "xcode", "make"}:
        raise ValueError("configuration identity requires a recognized build system")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("configuration identity requires a complete source-tree SHA-256")
    patches = identity.get("patch_set")
    if not isinstance(patches, Mapping) or not patches:
        raise ValueError("configuration identity requires an explicit patch/source-state record")
    if build_system == "meson":
        options = identity.get("meson_options")
        compiler_hash = identity.get("compiler_identity_sha256")
        cross = identity.get("cross_file")
        if not isinstance(options, list) or not options:
            raise ValueError("Meson configuration identity requires recorded build options")
        if not any(isinstance(row, Mapping) and row.get("name") == "buildtype"
                   and isinstance(row.get("value"), str) and row.get("value") for row in options):
            raise ValueError("Meson configuration identity requires an explicit build type")
        if not isinstance(compiler_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", compiler_hash):
            raise ValueError("Meson configuration identity requires compiler evidence")
        if not (isinstance(cross, Mapping) and cross.get("status") in {"present", "absent"}
                and (cross.get("status") == "absent" or re.fullmatch(r"[0-9a-f]{64}", str(cross.get("sha256", ""))))):
            raise ValueError("Meson configuration identity must record a cross-file hash or explicit absence")
        if not identity.get("linker_identity"):
            raise ValueError("Meson configuration identity requires linker identity")
    elif build_system == "xcode":
        compiler_hash = hashlib.sha256(_canonical_json(identity.get("xcode_toolchain_identity"))).hexdigest()
        options = identity.get("xcode_build_requests")
        if not identity.get("xcode_project_sha256") or not identity.get("xcode_info_plist_sha256"):
            raise ValueError("Xcode configuration identity requires project and Info.plist hashes")
        if not isinstance(options, list) or not options:
            raise ValueError("Xcode configuration identity requires captured build requests")
        if not identity.get("xcode_version") or not identity.get("xcode_toolchain_identity"):
            raise ValueError("Xcode configuration identity requires Xcode and compiler/linker identities")
    else:
        compiler_hash = identity.get("compiler_identity_sha256")
        options = identity.get("make_configuration_sha256")
        if not identity.get("makefile_sha256") or not options:
            raise ValueError("Make configuration identity requires Makefile and configure-state hashes")
        if not isinstance(compiler_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", compiler_hash):
            raise ValueError("Make configuration identity requires compiler evidence")
    resolved_dependency = dependency_state if dependency_state is not None else identity.get("dependency_state")
    if resolved_dependency is None:
        raise ValueError("configuration identity requires a frozen dependency state")
    build_type = identity.get("build_type")
    if not isinstance(build_type, str) or not build_type.strip():
        raise ValueError("configuration identity requires an explicit build type")
    generated = identity.get("generated_shader_identity", [])
    if not isinstance(generated, list):
        raise ValueError("generated_shader_identity must be a list")
    generated_keys = []
    for row in generated:
        if not isinstance(row, Mapping) or not isinstance(row.get("sha256"), str):
            raise ValueError("generated shader identities require sha256 values")
        generated_keys.append((row.get("build_relative_path", row.get("path")), row["sha256"]))
    material = {"source_tree": source_hash,
                "patches": patches,
                "cross": identity.get("cross_file"),
                "options": options,
                "compiler": compiler_hash,
                "linker": identity.get("linker_identity"),
                "build_type": identity.get("build_type"),
                "build_system": identity.get("build_system"),
                "xcode_project": identity.get("xcode_project_sha256"),
                "xcode_info_plist": identity.get("xcode_info_plist_sha256"),
                "xcode_build_requests": identity.get("xcode_build_requests"),
                "xcode_version": identity.get("xcode_version"),
                "xcode_toolchain": identity.get("xcode_toolchain_identity"),
                "dependency_state": resolved_dependency,
                "generated_shaders": sorted(generated_keys)}
    return hashlib.sha256(_canonical_json(material)).hexdigest()

def _capture_untracked_source_path(source_root: Path, relative: Path, evidence: Path) -> dict[str, Any]:
    """Copy a Git-reported untracked file, link, or directory tree without following links."""
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise RetentionError(f"unsafe untracked source path: {relative}")
    candidate = source_root / relative
    try:
        initial = candidate.lstat()
    except OSError as error:
        raise RetentionError(f"untracked source path disappeared: {relative}") from error
    if stat.S_ISREG(initial.st_mode) and not stat.S_ISLNK(initial.st_mode):
        retained = _copy_evidence(candidate, evidence / "source-untracked" / relative)
        return {"path": relative.as_posix(), "type": "regular", "sha256": retained["sha256"],
                "retained_path": retained["retained_path"]}
    if stat.S_ISLNK(initial.st_mode):
        target_text = os.readlink(candidate)
        retained = _write_evidence_bytes(
            evidence / "source-untracked-links" / f"{relative.as_posix()}.symlink.json",
            _canonical_json({"path": relative.as_posix(), "target": target_text}) + b"\n")
        return {"path": relative.as_posix(), "type": "symlink", "target": target_text,
                "target_sha256": hashlib.sha256(os.fsencode(target_text)).hexdigest(),
                "sha256": retained["sha256"],
                "retained_path": retained["retained_path"]}
    if not stat.S_ISDIR(initial.st_mode):
        raise RetentionError(f"unsupported untracked source file type: {relative}")
    root_fd = _open_dir(candidate)
    root_info = os.fstat(root_fd)
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    if root_fsid is None:
        os.close(root_fd)
        raise RetentionError(f"untracked source directory has no filesystem identity: {relative}")
    entries: list[dict[str, Any]] = [{"path": ".", "type": "directory",
                                     "mode": oct(stat.S_IMODE(root_info.st_mode))}]
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, directory = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    child_relative = directory / name
                    evidence_relative = relative / child_relative
                    if info.st_dev != root_info.st_dev:
                        raise RetentionError(f"untracked source directory crosses a filesystem: {evidence_relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened = os.fstat(child_fd)
                        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                            os.close(child_fd)
                            raise RetentionError(f"untracked source directory changed: {evidence_relative}")
                        entries.append({"path": child_relative.as_posix(), "type": "directory",
                                        "mode": oct(stat.S_IMODE(info.st_mode))})
                        stack.append((child_fd, child_relative))
                    elif stat.S_ISREG(info.st_mode):
                        retained = _copy_evidence(candidate / child_relative,
                                                  evidence / "source-untracked" / relative / child_relative)
                        entries.append({"path": child_relative.as_posix(), "type": "regular",
                                        "sha256": retained["sha256"],
                                        "retained_path": retained["retained_path"],
                                        "size_bytes": retained["size_bytes"]})
                    elif stat.S_ISLNK(info.st_mode):
                        target_text = os.readlink(name, dir_fd=parent_fd)
                        retained = _write_evidence_bytes(
                            evidence / "source-untracked-links" / relative
                            / f"{child_relative.as_posix()}.symlink.json",
                            _canonical_json({"path": evidence_relative.as_posix(),
                                             "target": target_text}) + b"\n")
                        entries.append({"path": child_relative.as_posix(), "type": "symlink",
                                        "target": target_text,
                                        "target_sha256": hashlib.sha256(os.fsencode(target_text)).hexdigest(),
                                        "sha256": retained["sha256"],
                                        "retained_path": retained["retained_path"]})
                    else:
                        raise RetentionError(f"unsupported untracked source entry: {evidence_relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda row: row["path"])
    descriptor = {"path": relative.as_posix(), "type": "directory",
                  "tree_sha256": hashlib.sha256(_canonical_json(entries)).hexdigest(),
                  "entries": entries}
    identity_name = hashlib.sha256(relative.as_posix().encode()).hexdigest() + ".json"
    retained = _write_evidence_bytes(evidence / "source-untracked-manifests" / identity_name,
                                     (json.dumps(descriptor, indent=2, sort_keys=True) + "\n").encode())
    descriptor.update({"retained_path": retained["retained_path"], "sha256": retained["sha256"]})
    return descriptor

def _capture_identity(build: Path, evidence: Path) -> dict[str, Any]:
    log = build / "meson-logs/meson-log.txt"
    command_line = build / "meson-private/cmd_line.txt"
    options_file = build / "meson-info/intro-buildoptions.json"
    compilers_file = build / "meson-info/intro-compilers.json"
    identity: dict[str, Any] = _empty_identity()
    if (build.name.startswith("MoltenVKPackaging-") or (build / "Info.plist").is_file()
            or (build / "Build/Products/XCFrameworkStaging").is_dir()):
        info_plist = build / "Info.plist"
        try:
            import plistlib
            info = plistlib.loads(info_plist.read_bytes())
        except (OSError, ValueError):
            info = {}
        identity["build_system"] = "xcode"
        identity["xcode_info_plist_sha256"] = _hash_path(info_plist) if info_plist.is_file() else None
        if info_plist.is_file() and not info_plist.is_symlink():
            identity["xcode_info_plist_evidence"] = _copy_evidence(info_plist, evidence / "configuration/Info.plist")
        workspace = info.get("WorkspacePath") if isinstance(info, dict) else None
        project = Path(workspace).resolve(strict=True) if isinstance(workspace, str) and Path(workspace).exists() else None
        if project and project.is_dir():
            source_root = project.parent.resolve(strict=True)
            tree, count = _tree_hash(source_root)
            source = {"path": str(source_root), "content_tree_sha256": tree, "file_count": count,
                      "capture_scope": "artifactization-time snapshot"}
            commit = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if commit.returncode == 0:
                source["git_commit"] = commit.stdout.strip()
                tree_proc = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD^{tree}"], text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                source["git_tree"] = tree_proc.stdout.strip() if tree_proc.returncode == 0 else None
                staged = subprocess.run(["git", "-C", str(source_root), "diff", "--cached", "--binary", "HEAD"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                unstaged = subprocess.run(["git", "-C", str(source_root), "diff", "--binary"],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                untracked = subprocess.run(
                    ["git", "-C", str(source_root), "ls-files", "--others", "--exclude-standard", "-z"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                source["staged_patch_sha256"] = hashlib.sha256(staged).hexdigest()
                source["unstaged_patch_sha256"] = hashlib.sha256(unstaged).hexdigest()
                source["untracked_path_list_sha256"] = hashlib.sha256(untracked).hexdigest()
                source["patch_set_sha256"] = hashlib.sha256(staged + b"\0" + unstaged + b"\0" + untracked).hexdigest()
                if staged:
                    _write_evidence_bytes(evidence / "patches/xcode-source-staged.patch", staged)
                if unstaged:
                    _write_evidence_bytes(evidence / "patches/xcode-source-unstaged.patch", unstaged)
                untracked_rows = []
                for raw_name in untracked.split(b"\0"):
                    if not raw_name:
                        continue
                    rel = Path(os.fsdecode(raw_name))
                    untracked_rows.append(_capture_untracked_source_path(source_root, rel, evidence))
                source["untracked_files"] = untracked_rows
                source["untracked_files_sha256"] = hashlib.sha256(_canonical_json(untracked_rows)).hexdigest()
                identity["patch_set"] = {"staged_patch_sha256": source["staged_patch_sha256"],
                                          "unstaged_patch_sha256": source["unstaged_patch_sha256"],
                                          "patch_set_sha256": source["patch_set_sha256"],
                                          "untracked_files_sha256": source["untracked_files_sha256"],
                                          "capture_scope": "artifactization-time snapshot"}
            else:
                identity["patch_set"] = {"status": "source is not a Git checkout",
                                          "content_tree_sha256": tree,
                                          "capture_scope": "artifactization-time snapshot"}
            identity["source_identity"] = source
            pbx = project / "project.pbxproj"
            if pbx.is_file():
                identity["xcode_project_evidence"] = _copy_evidence(pbx, evidence / "configuration/project.pbxproj")
                identity["xcode_project_sha256"] = _hash_path(pbx)
            identity["xcode_project_path"] = str(project)
            identity["xcode_source_root_path"] = str(source_root)
        requests = []
        xcode_configurations = []
        build_data = build / "Build/Intermediates.noindex/XCBuildData"
        if build_data.is_dir() and not build_data.is_symlink():
            for request in sorted(build_data.glob("*/build-request.json")):
                if request.is_file() and not request.is_symlink():
                    request_value = json.loads(request.read_text(encoding="utf-8"))
                    parameters = request_value.get("parameters") if isinstance(request_value, dict) else None
                    configuration = parameters.get("configurationName") if isinstance(parameters, dict) else None
                    action = parameters.get("action") if isinstance(parameters, dict) else None
                    retained_request = _copy_evidence(request, evidence / "configuration" / request.parent.name
                                                      / "build-request.json")
                    retained_request.update({"configuration_name": configuration, "action": action})
                    requests.append(retained_request)
                    xcode_configurations.append(configuration)
        identity["xcode_build_requests"] = requests
        identity["xcode_configurations"] = xcode_configurations
        activity_logs = []
        for log in sorted((build / "Logs/Build").glob("*.xcactivitylog")):
            if log.is_file() and not log.is_symlink():
                activity_logs.append(_copy_evidence(log, evidence / "logs" / log.name))
        identity["xcode_activity_logs"] = activity_logs
        manifest_plist = build / "Logs/Build/LogStoreManifest.plist"
        if manifest_plist.is_file() and not manifest_plist.is_symlink():
            identity["xcode_log_manifest"] = _copy_evidence(manifest_plist, evidence / "logs/LogStoreManifest.plist")
        xcode = subprocess.run(["xcodebuild", "-version"], text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, check=False)
        identity["xcode_version"] = xcode.stdout.strip() if xcode.returncode == 0 else None
        identity["xcodebuild_version_command"] = ["xcodebuild", "-version"]
        xcode_tools = {}
        for key, command in (("clang", ["xcrun", "clang", "--version"]),
                             ("clang_path", ["xcrun", "--find", "clang"]),
                             ("sdk_version", ["xcrun", "--sdk", "macosx", "--show-sdk-version"]),
                             ("sdk_path", ["xcrun", "--sdk", "macosx", "--show-sdk-path"]),
                             ("linker", ["ld", "-v"])):
            try:
                result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, check=False, timeout=15)
                xcode_tools[key] = {"command": command, "returncode": result.returncode,
                                    "output": result.stdout[:4000]}
            except (OSError, subprocess.TimeoutExpired) as error:
                xcode_tools[key] = {"command": command, "error": repr(error)}
        identity["xcode_toolchain_identity"] = xcode_tools
        normalized_xcode_configurations = [value.casefold() for value in xcode_configurations
                                           if isinstance(value, str)]
        identity["build_type"] = (
            "release" if normalized_xcode_configurations and len(normalized_xcode_configurations) == len(xcode_configurations)
            and all(value == "release" for value in normalized_xcode_configurations) else
            "debug" if normalized_xcode_configurations and len(normalized_xcode_configurations) == len(xcode_configurations)
            and all(value == "debug" for value in normalized_xcode_configurations) else
            "unknown-xcode-configuration")
        identity["dependency_state"] = {"status": "captured",
                                         "xcode_version_sha256": hashlib.sha256(
                                             str(identity["xcode_version"]).encode()).hexdigest(),
                                         "toolchain_sha256": hashlib.sha256(
                                             _canonical_json(xcode_tools)).hexdigest()}
        framework = build / "Build/Products/Release/MoltenVK.framework"
        resources = []
        if framework.is_dir() and not framework.is_symlink():
            for product in sorted(framework.rglob("*")):
                if product.is_file() and not product.is_symlink() and not _is_macho(product):
                    resources.append(_copy_evidence(product, evidence / "framework" / product.relative_to(framework)))
        identity["framework_resources"] = resources
    log_text = ""
    if log.is_file() and not log.is_symlink():
        log_text = log.read_text(errors="replace")
        if identity["build_system"] != "xcode":
            identity["build_system"] = "meson"
        identity["meson_log_sha256"] = _hash_path(log)
        identity["meson_log_evidence"] = _copy_evidence(log, evidence / "logs/meson-log.txt")
    source_match = re.search(r"^Source dir:\s*(.+)$", log_text, re.MULTILINE)
    if source_match:
        source = Path(source_match.group(1).strip()).resolve(strict=True)
        if source.is_dir():
            tree, count = _tree_hash(source)
            source_info: dict[str, Any] = {"path": str(source), "content_tree_sha256": tree,
                                           "file_count": count, "capture_scope": "artifactization-time snapshot"}
            commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if commit.returncode == 0:
                source_info["git_commit"] = commit.stdout.strip()
                tree_proc = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                source_info["git_tree"] = tree_proc.stdout.strip() if tree_proc.returncode == 0 else None
                staged = subprocess.run(["git", "-C", str(source), "diff", "--cached", "--binary", "HEAD"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                unstaged = subprocess.run(["git", "-C", str(source), "diff", "--binary"],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                source_info["staged_patch_sha256"] = hashlib.sha256(staged).hexdigest()
                source_info["unstaged_patch_sha256"] = hashlib.sha256(unstaged).hexdigest()
                source_info["staged_patch_bytes"] = len(staged)
                source_info["unstaged_patch_bytes"] = len(unstaged)
                source_info["patch_set_sha256"] = hashlib.sha256(staged + b"\0" + unstaged).hexdigest()
                untracked = subprocess.run(
                    ["git", "-C", str(source), "ls-files", "--others", "--exclude-standard", "-z"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                untracked_rows = []
                for raw_name in untracked.split(b"\0"):
                    if not raw_name:
                        continue
                    rel = Path(os.fsdecode(raw_name))
                    untracked_rows.append(_capture_untracked_source_path(source, rel, evidence))
                source_info["untracked_files"] = untracked_rows
                source_info["untracked_files_sha256"] = hashlib.sha256(_canonical_json(untracked_rows)).hexdigest()
                submodules = subprocess.run(
                    ["git", "-C", str(source), "submodule", "status", "--recursive"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                source_info["submodule_status"] = submodules.decode(errors="replace") if submodules else ""
                source_info["submodule_status_sha256"] = hashlib.sha256(submodules).hexdigest()
                identity["patch_set"] = {
                    "staged_patch_sha256": source_info["staged_patch_sha256"],
                    "unstaged_patch_sha256": source_info["unstaged_patch_sha256"],
                    "patch_set_sha256": source_info["patch_set_sha256"],
                    "untracked_files_sha256": source_info["untracked_files_sha256"],
                    "submodule_status_sha256": source_info["submodule_status_sha256"],
                    "capture_scope": "artifactization-time snapshot",
                }
                if staged:
                    target = evidence / "patches/source-staged.patch"
                    _write_evidence_bytes(target, staged)
                    _write_evidence_bytes(target.with_suffix(".patch.json"),
                        (json.dumps({"sha256": hashlib.sha256(staged).hexdigest(), "size_bytes": len(staged),
                                     "capture_scope": "artifactization-time"}, indent=2, sort_keys=True) + "\n").encode())
                if unstaged:
                    target = evidence / "patches/source-unstaged.patch"
                    _write_evidence_bytes(target, unstaged)
                    _write_evidence_bytes(target.with_suffix(".patch.json"),
                        (json.dumps({"sha256": hashlib.sha256(unstaged).hexdigest(), "size_bytes": len(unstaged),
                                     "capture_scope": "artifactization-time"}, indent=2, sort_keys=True) + "\n").encode())
                status = subprocess.run(["git", "-C", str(source), "status", "--porcelain=v2", "--untracked-files=all"],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout
                source_info["status_porcelain_v2_sha256"] = hashlib.sha256(status).hexdigest()
            identity["source_identity"] = source_info
    if command_line.is_file() and not command_line.is_symlink():
        text = command_line.read_text(errors="replace")
        identity["meson_command_line"] = text
        identity["meson_command_line_sha256"] = _hash_path(command_line)
        identity["meson_command_line_evidence"] = _copy_evidence(command_line, evidence / "configuration/meson-cmd-line.txt")
    if options_file.is_file() and not options_file.is_symlink():
        raw = options_file.read_bytes()
        try:
            identity["meson_options"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
        identity["meson_options_sha256"] = hashlib.sha256(raw).hexdigest()
        identity["build_type"] = next((row.get("value") for row in identity["meson_options"] or []
                                       if row.get("name") == "buildtype"), None)
        identity["meson_options_evidence"] = _copy_evidence(options_file, evidence / "configuration/meson-build-options.json")
    if identity["build_system"] == "meson":
        identity["cross_file"] = {"status": "absent"}
    if compilers_file.is_file() and not compilers_file.is_symlink():
        raw = compilers_file.read_bytes()
        try:
            identity["compiler_identity"] = json.loads(raw)
        except json.JSONDecodeError:
            pass
        identity["compiler_identity_sha256"] = hashlib.sha256(raw).hexdigest()
        if isinstance(identity["compiler_identity"], dict):
            identity["compiler_versions"] = {
                machine: {name: row.get("version") for name, row in items.items() if isinstance(row, dict)}
                for machine, items in identity["compiler_identity"].items() if isinstance(items, dict)}
        identity["compiler_evidence"] = _copy_evidence(compilers_file, evidence / "configuration/meson-compilers.json")
    meson_info = build / "meson-info"
    dependency_evidence = None
    if meson_info.is_dir() and not meson_info.is_symlink():
        for name in ("intro-dependencies.json", "intro-targets.json", "intro-tests.json",
                     "intro-buildsystem_files.json"):
            source_file = meson_info / name
            if source_file.is_file() and not source_file.is_symlink():
                record = _copy_evidence(source_file, evidence / "configuration" / f"meson-{name}")
                identity[f"{name.replace('.', '_')}_evidence"] = record
                if name == "intro-dependencies.json":
                    dependency_evidence = record
    if identity["build_system"] == "meson":
        identity["dependency_state"] = ({"status": "captured", "intro_dependencies_sha256": dependency_evidence["sha256"]}
                                         if dependency_evidence else {"status": "explicitly-none"})
    for relative, evidence_name in ((Path("build.log"), "logs/build.log"),
                                   (Path("build-command.json"), "configuration/build-command.json"),
                                   (Path("build.ninja"), "configuration/build.ninja"),
                                   (Path(".ninja_log"), "logs/ninja-log.txt")):
        source_file = build / relative
        if source_file.is_file() and not source_file.is_symlink():
            identity[evidence_name.replace("/", "_").replace(".", "_") + "_evidence"] = _copy_evidence(
                source_file, evidence / evidence_name)
            if relative.name == "build.ninja":
                identity["build_ninja_evidence"] = identity[evidence_name.replace("/", "_").replace(".", "_") + "_evidence"]
    config_evidence = {key: value for key, value in identity.items()
                       if key.endswith("_sha256") and isinstance(value, str)}
    config_evidence.update({"command_line": identity.get("meson_command_line_sha256"),
                            "options": identity.get("meson_options_sha256"),
                            "compiler": identity.get("compiler_identity_sha256"),
                            "cross": (identity.get("cross_file") or {}).get("sha256")})
    identity["meson_configuration_sha256"] = hashlib.sha256(_canonical_json(config_evidence)).hexdigest()
    if identity["build_system"] == "unknown" and (build / "Makefile").is_file() and not (build / "Makefile").is_symlink():
        identity["build_system"] = "make"
        makefile = build / "Makefile"
        identity["makefile_sha256"] = _hash_path(makefile)
        identity["makefile_size_bytes"] = makefile.stat().st_size
        for name in ("config.status", "config.log", "compile_commands.json"):
            source_file = build / name
            if source_file.is_file() and not source_file.is_symlink():
                identity[f"{name.replace('.', '_')}_evidence"] = _copy_evidence(
                    source_file, evidence / f"configuration/{name}")
        make_text = makefile.read_text(errors="replace")
        source_match = re.search(r"^srcdir\s*=\s*(.+)$", make_text, re.MULTILINE)
        if source_match:
            source = Path(source_match.group(1).strip()).resolve(strict=True)
            if source.is_dir():
                tree, count = _tree_hash(source)
                source_info: dict[str, Any] = {"path": str(source), "content_tree_sha256": tree,
                    "file_count": count, "capture_scope": "artifactization-time snapshot; historical build-time input not asserted"}
                identity["source_identity"] = source_info
                identity["patch_set"] = {"status": "source is not a Git checkout; source tree hash retained",
                                          "source_tree_sha256": tree}
        compiler_commands = {}
        for key in ("CC", "CXX", "LD"):
            match = re.search(rf"^{key}\s*=\s*(.+)$", make_text, re.MULTILINE)
            if match:
                compiler_commands[key] = match.group(1).strip()
        identity["compiler_commands"] = compiler_commands
        identity["compiler_identity"] = {}
        for key, command in compiler_commands.items():
            try:
                version = subprocess.run([*command.split(), "--version"], text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         check=False, timeout=15)
                identity["compiler_identity"][key] = {"command": command, "returncode": version.returncode,
                                                       "version_output": version.stdout[:4000]}
            except (OSError, subprocess.TimeoutExpired) as error:
                identity["compiler_identity"][key] = {"command": command, "error": repr(error)}
        identity["compiler_identity_sha256"] = hashlib.sha256(
            _canonical_json(identity["compiler_identity"])).hexdigest()
        config_log = build / "config.log"
        identity["configuration_log_sha256"] = _hash_path(config_log) if config_log.is_file() else None
        identity["build_log_sha256"] = None
        identity["build_log_status"] = "legacy Make build stdout transcript absent; configuration log and compile_commands retained"
        identity["build_type"] = "unknown-legacy"
        identity["make_configuration_sha256"] = hashlib.sha256(_canonical_json({
            name: identity.get(f"{name.replace('.', '_')}_evidence", {}).get("sha256")
            for name in ("config.status", "config.log", "compile_commands.json")
        })).hexdigest()
    cross = re.search(r"cross_file\s*=\s*\['([^']+)'\]", log_text)
    if not cross and command_line.is_file():
        cross = re.search(r"--cross-file(?:=|\s+)([^\s]+)", command_line.read_text(errors="replace"))
    if cross:
        cross_value = Path(cross.group(1).strip("'\""))
        path = cross_value if cross_value.is_absolute() else build / cross_value
        if not path.is_file() or path.is_symlink():
            raise RetentionError(f"Meson identifies a cross file that cannot be captured: {path}")
        identity["cross_file"] = {"status": "present", **_copy_evidence(
            path, evidence / f"configuration/cross-{path.name}")}
    linker = sorted(set(re.findall(r"(?:linker|linker id|ld version)[^\n:]*:?\s*([^\n]+)",
                                   log_text, re.IGNORECASE)))
    identity["linker_identity"] = linker or None
    if identity["build_system"] == "meson" and not identity.get("linker_identity"):
        linker_version = subprocess.run(["ld", "-v"], text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, check=False, timeout=15)
        if linker_version.returncode == 0 and linker_version.stdout.strip():
            identity["linker_identity"] = [{"command": ["ld", "-v"],
                                            "output": linker_version.stdout.strip()[:4000]}]
    generated = []
    symlinks = []
    for directory, dirnames, filenames in os.walk(build, followlinks=False):
        base = Path(directory)
        for name in list(dirnames):
            path = base / name
            if path.is_symlink():
                target_text = os.readlink(path)
                symlinks.append({"build_relative_path": path.relative_to(build).as_posix(),
                                 "target": target_text,
                                 "target_sha256": hashlib.sha256(os.fsencode(target_text)).hexdigest()})
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        for name in sorted(filenames):
            path = base / name
            relative = path.relative_to(build).as_posix()
            if path.is_symlink():
                target_text = os.readlink(path)
                symlinks.append({"build_relative_path": relative, "target": target_text,
                                 "target_sha256": hashlib.sha256(os.fsencode(target_text)).hexdigest()})
                continue
            lower = relative.lower()
            in_meson_private_product = any(part.endswith(".p") for part in Path(relative).parts)
            shader_named = any(x in lower for x in ("shader", "generated", "gen/", "_frag", "_vert",
                                                     "_comp", "spirv", "spv_", "gitrevderived"))
            if path.suffix.lower() in SHADER_EXTENSIONS and (shader_named or in_meson_private_product):
                row = _copy_evidence(path, evidence / "generated-shaders" / relative)
                row["build_relative_path"] = relative
                generated.append(row)
    identity["generated_shader_identity"] = generated
    identity["build_symlink_identity"] = sorted(symlinks, key=lambda row: row["build_relative_path"])
    build_log = build / "build.log"
    if build_log.is_file() and not build_log.is_symlink():
        retained_build_log = identity.get("logs_build_log_evidence")
        if not isinstance(retained_build_log, dict):
            raise RetentionError("build.log exists but was not durably retained")
        identity["build_log_sha256"] = retained_build_log["sha256"]
        identity["build_log_status"] = "stdout transcript retained"
    else:
        identity["build_log_sha256"] = None
        identity["build_log_status"] = "no stdout build transcript present; configure log and Ninja execution log retained separately"
    ninja_log = build / ".ninja_log"
    if ninja_log.is_file() and not ninja_log.is_symlink():
        identity["ninja_log_sha256"] = _hash_path(ninja_log)
        retained_ninja_log = identity.get("logs_ninja-log_txt_evidence")
        if not isinstance(retained_ninja_log, dict) or retained_ninja_log.get("sha256") != identity["ninja_log_sha256"]:
            raise RetentionError(".ninja_log was not durably retained with its verified hash")
        identity["ninja_log_evidence"] = retained_ninja_log
    else:
        identity["ninja_log_sha256"] = None
    if identity["build_system"] == "meson":
        identity["ninja_execution_log_status"] = "captured" if identity.get("ninja_log_sha256") else "not-present"
    return identity

def _manifest_id(manifest: Mapping[str, Any]) -> str:
    value = dict(manifest)
    value.pop("build_manifest_id", None)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_json(value)).hexdigest()

def _manifest_checksum(manifest: Mapping[str, Any]) -> str:
    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_json(value)).hexdigest()

def _verify_artifact(row: Mapping[str, Any]) -> bool:
    try:
        digest = row["sha256"]
        path = Path(row["canonical_artifact_path"])
        directory = ARTIFACT_ROOT / digest
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or path.parent != directory:
            return False
        root_fd, group_fd = _open_artifact_group(digest)
        try:
            payload_info = _verify_artifact_payload_at(group_fd, path.name, digest)
            saved = json.loads(_read_at(group_fd, "manifest.json", limit=1024 * 1024))
        finally:
            os.close(group_fd)
            os.close(root_fd)
        return (saved.get("sha256") == digest and saved.get("canonical_path") == str(path)
                and _artifact_size(saved) is not None
                and _artifact_size(saved) == payload_info.st_size)
    except (OSError, KeyError, json.JSONDecodeError, RetentionError):
        return False

def _verify_build_manifest(build_id: str) -> tuple[dict[str, Any], str]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", build_id):
        raise RetentionError("build manifest ID is not path-safe")
    manifest_fd = _open_dir(MANIFEST_ROOT)
    try:
        raw = _read_at(manifest_fd, f"{build_id}.json", limit=64 * 1024 * 1024)
    finally:
        os.close(manifest_fd)
    manifest = json.loads(raw)
    if manifest.get("build_manifest_id") != _manifest_id(manifest):
        raise RetentionError(f"build manifest ID mismatch: {build_id}")
    if manifest.get("manifest_sha256") != _manifest_checksum(manifest):
        raise RetentionError(f"build manifest checksum mismatch: {build_id}")
    evidence_path = MANIFEST_ROOT / f"{build_id}.evidence"
    if manifest.get("evidence_path") != str(evidence_path):
        raise RetentionError(f"build manifest evidence path is not canonical: {build_id}")
    expected_evidence = manifest.get("evidence_files")
    if not isinstance(expected_evidence, list):
        raise RetentionError(f"build manifest lacks an evidence-file index: {build_id}")
    actual_evidence = _inventory_evidence(evidence_path)
    if actual_evidence != expected_evidence:
        raise RetentionError(f"retained evidence differs from its manifest index: {build_id}")
    evidence_index_hash = hashlib.sha256(_canonical_json(actual_evidence)).hexdigest()
    if manifest.get("evidence_index_sha256") != evidence_index_hash:
        raise RetentionError(f"retained evidence index hash mismatch: {build_id}")
    evidence_by_path = {str(evidence_path / row["path"]): row for row in actual_evidence}
    def check_identity_records(value: Any) -> None:
        if isinstance(value, dict):
            retained_path, digest = value.get("retained_path"), value.get("sha256")
            if isinstance(retained_path, str) and isinstance(digest, str):
                indexed = evidence_by_path.get(retained_path)
                if indexed is None or indexed.get("sha256") != digest:
                    raise RetentionError(f"build identity references unverified evidence: {retained_path}")
            for child in value.values():
                check_identity_records(child)
        elif isinstance(value, list):
            for child in value:
                check_identity_records(child)
    check_identity_records(manifest.get("build_identity", {}))
    artifact_hashes = {row.get("sha256") for row in manifest.get("artifacts", [])}
    for product in manifest.get("build_identity", {}).get("product_tree_manifests", []):
        product_path = product.get("path") if isinstance(product, dict) else None
        if not isinstance(product_path, str) or product_path != str(evidence_path / "products" /
                                                                      Path(product_path).name):
            raise RetentionError(f"product tree index path is not canonical: {build_id}")
        descriptor = evidence_by_path.get(product_path)
        if descriptor is None or descriptor.get("sha256") != product.get("sha256"):
            raise RetentionError(f"product tree index file is not integrity-bound: {build_id}")
        product_record = json.loads(_read_file_descriptor(Path(product_path)))
        entries = product_record.get("entries")
        if (product_record.get("tree_sha256") != product.get("tree_sha256")
                or not isinstance(entries, list)
                or hashlib.sha256(_canonical_json(entries)).hexdigest() != product.get("tree_sha256")):
            raise RetentionError(f"product tree structure hash mismatch: {build_id}")
        for entry in entries:
            if entry.get("type") == "canonical_artifact":
                if entry.get("sha256") not in artifact_hashes or not _verify_artifact(entry):
                    raise RetentionError(f"product tree names an unverified artifact: {entry.get('path')}")
            elif entry.get("type") == "evidence_file":
                evidence_row = evidence_by_path.get(entry.get("evidence_path"))
                if evidence_row is None or evidence_row.get("sha256") != entry.get("sha256"):
                    raise RetentionError(f"product tree names unverified retained file: {entry.get('path')}")
    if not manifest.get("artifacts") or not all(_verify_artifact(row) for row in manifest["artifacts"]):
        raise RetentionError(f"build manifest references missing/invalid canonical artifacts: {build_id}")
    identity = manifest.get("build_identity", {})
    _validate_completed_identity(identity, manifest["artifacts"], evidence_path)
    adopted = manifest.get("adopted_existing") is True
    if not adopted:
        requested_build_type = manifest.get("requested_build_type")
        actual_build_type = manifest.get("build_type")
        debug_estimate = manifest.get("estimated_debug_bytes")
        if (requested_build_type not in SUPPORTED_BUILD_TYPES or not isinstance(actual_build_type, str)
                or actual_build_type.casefold() != requested_build_type):
            raise RetentionError("completed build type differs from the signed pre-build type")
        if requested_build_type in FULL_DEBUG_BUILD_TYPES:
            if (not isinstance(manifest.get("preserve_debug_reason"), str)
                    or not manifest["preserve_debug_reason"].strip()
                    or not isinstance(debug_estimate, int) or isinstance(debug_estimate, bool)
                    or debug_estimate <= 0 or debug_estimate > manifest.get("estimated_bytes", 0)):
                raise RetentionError("completed full debug build lacks its signed reason or size estimate")
        elif debug_estimate is not None:
            raise RetentionError("non-debug build manifest unexpectedly declares a debug-size estimate")
    preserve_expiry = manifest.get("preserve_debug_expires_at_unix")
    if preserve_expiry is not None:
        completed_at = manifest.get("completed_at_unix")
        if (not isinstance(manifest.get("preserve_debug_reason"), str)
                or not manifest["preserve_debug_reason"].strip()
                or not isinstance(completed_at, (int, float)) or isinstance(completed_at, bool)
                or not isinstance(preserve_expiry, (int, float)) or isinstance(preserve_expiry, bool)
                or preserve_expiry <= completed_at
                or preserve_expiry > completed_at + PRESERVE_DEBUG_MAX_SECONDS):
            raise RetentionError("preserved build manifest lacks a reason or bounded expiry")
    if identity.get("legacy_debug_preservation") is True:
        completed_at = manifest.get("completed_at_unix")
        expiry = manifest.get("preserve_debug_expires_at_unix")
        estimate = manifest.get("estimated_bytes")
        if (not adopted or manifest.get("kind") != "reference"
                or not isinstance(manifest.get("preserve_debug_reason"), str)
                or not manifest["preserve_debug_reason"].strip()
                or not isinstance(completed_at, (int, float)) or isinstance(completed_at, bool)
                or not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                or expiry <= completed_at or expiry > completed_at + PRESERVE_DEBUG_MAX_SECONDS
                or not isinstance(estimate, int) or isinstance(estimate, bool) or estimate <= 0):
            raise RetentionError("legacy debug manifest lacks its reason, bounded expiry, or size estimate")
        requested_matches = (manifest.get("requested_config_key_sha256") is None
                             and manifest.get("configuration_key_provenance") == "legacy-debug-preservation-only"
                             and manifest.get("captured_config_key_sha256") is None
                             and manifest.get("config_key_sha256") is None)
    else:
        requested_matches = (
            manifest.get("requested_config_key_sha256") is None
            and manifest.get("configuration_key_provenance") == "artifactization-time retrospective"
            and manifest.get("captured_config_key_sha256") == identity.get("config_key_sha256")
        ) if adopted else manifest.get("requested_config_key_sha256") == identity.get("config_key_sha256")
    if manifest.get("config_key_sha256") != identity.get("config_key_sha256") or not requested_matches:
        raise RetentionError(f"build manifest configuration key is not bound consistently: {build_id}")
    return manifest, hashlib.sha256(raw).hexdigest()

def find_canonical_equivalent(*, config_key_sha256: str, kind: str,
                              source_tree_hash: str | None = None,
                              dependency_state: Any = None) -> dict[str, Any] | None:
    """Return a fully verified prior artifact manifest for an identical requested configuration."""
    if not re.fullmatch(r"[0-9a-f]{64}", config_key_sha256):
        raise ValueError("configuration key must be a lowercase SHA-256")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if not MANIFEST_ROOT.is_dir():
        return None
    for path in sorted(MANIFEST_ROOT.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            manifest, _ = _verify_build_manifest(path.stem)
        except (OSError, RetentionError, json.JSONDecodeError, KeyError):
            continue
        if manifest.get("config_key_sha256") != config_key_sha256 or manifest.get("kind") != kind:
            continue
        if source_tree_hash is not None and manifest.get("source_tree_hash_at_lease") != source_tree_hash:
            continue
        if dependency_state is not None and manifest.get("dependency_state") != dependency_state:
            continue
        return manifest
    return None

def _lease_files() -> list[Path]:
    return sorted(LEASE_ROOT.glob("*.json")) if LEASE_ROOT.is_dir() else []

def _load_lease(build_id: str) -> dict[str, Any]:
    return _read_json(LEASE_ROOT / f"{build_id}.json", sealed=True)

def _store_lease(lease: Mapping[str, Any]) -> dict[str, Any]:
    sealed = _seal(lease)
    _write_json(LEASE_ROOT / f"{lease['build_id']}.json", sealed)
    return sealed

def _remove_unmoved_adoption_record(build_id: str, expected: Mapping[str, Any]) -> None:
    """Remove only this provisional ACTIVE legacy lease after a namespace rollback."""
    lease_fd = _open_dir(LEASE_ROOT)
    try:
        name = f"{build_id}.json"
        current = json.loads(_read_at(lease_fd, name, limit=1024 * 1024))
        if (not _verify_seal(current) or current.get("build_id") != build_id
                or current.get("path") != expected.get("path")
                or current.get("build_device") != expected.get("build_device")
                or current.get("build_inode") != expected.get("build_inode")
                or current.get("state") != "ACTIVE" or not current.get("adopted_existing")):
            raise RetentionError("provisional adoption lease changed during rollback")
        os.unlink(name, dir_fd=lease_fd)
        os.fsync(lease_fd)
    finally:
        os.close(lease_fd)

def _rollback_failed_adoption(source_fd: int, source_name: str, build_parent_fd: int,
                              build_id: str, provisional_lease: Mapping[str, Any]) -> None:
    """Restore a namespace entry after detecting a replacement during adoption."""
    try:
        moved = os.stat(build_id, dir_fd=build_parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _remove_unmoved_adoption_record(build_id, provisional_lease)
        return
    if _exists_at(source_fd, source_name):
        raise RetentionError("adoption identity mismatch; source pathname is occupied, leaving the moved entry quarantined")
    if not (stat.S_ISDIR(moved.st_mode) or stat.S_ISLNK(moved.st_mode)):
        raise RetentionError("adoption identity mismatch; moved entry has an unsupported type")
    _rename_exclusive_between(build_parent_fd, build_id, source_fd, source_name)
    restored = os.stat(source_name, dir_fd=source_fd, follow_symlinks=False)
    if (restored.st_dev, restored.st_ino, stat.S_IFMT(restored.st_mode)) != (
            moved.st_dev, moved.st_ino, stat.S_IFMT(moved.st_mode)):
        raise RetentionError("adoption identity mismatch; restored entry changed during rollback")
    _remove_unmoved_adoption_record(build_id, provisional_lease)

def _remove_adoption_marker(root_fd: int, expected: Mapping[str, Any]) -> None:
    """Remove only the ACTIVE marker written by this adoption transaction."""
    try:
        current = _read_marker(root_fd)
    except FileNotFoundError:
        return
    for key in ("build_id", "path", "build_device", "build_inode", "state", "adopted_existing"):
        if current.get(key) != expected.get(key):
            raise RetentionError("adoption rollback found an unexpected build marker")
    if current.get("state") != "ACTIVE" or not current.get("adopted_existing"):
        raise RetentionError("adoption rollback marker is not the expected ACTIVE legacy marker")
    os.unlink(BUILD_MARKER, dir_fd=root_fd)
    os.fsync(root_fd)

def _write_marker(root_fd: int, lease: Mapping[str, Any]) -> None:
    _write_at(root_fd, BUILD_MARKER, (json.dumps(_seal(lease), indent=2, sort_keys=True) + "\n").encode(),
              replace=True)

def _read_marker(root_fd: int) -> dict[str, Any]:
    value = json.loads(_read_at(root_fd, BUILD_MARKER, limit=1024 * 1024))
    if not isinstance(value, dict) or not _verify_seal(value):
        raise RetentionError("build directory marker is missing/invalid")
    return value

def _lock() -> tuple[int, int]:
    lease_fd = _open_dir(LEASE_ROOT, create=True)
    fd = os.open(BUILD_LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=lease_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        os.close(lease_fd)
        raise RetentionError("retention lock is unsafe")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return lease_fd, fd

def _unlock(lease_fd: int, lock_fd: int) -> None:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    os.close(lease_fd)

def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False

def _root_identity(path: Path, *, create: bool) -> dict[str, Any]:
    fd = _open_dir(path, create=create)
    try:
        identity = _fs_identity(fd)
        sticky_system_tmp = identity["owner_uid"] == 0 and identity.get("sticky") is True
        if (identity["owner_uid"] not in {os.getuid(), 0}
                or (identity["mode"] & 0o022 and not sticky_system_tmp)):
            raise RetentionError(f"approved build parent has unsafe owner/mode: {path}")
        if path == BUILD_ROOT and (identity["owner_uid"] != os.getuid() or identity["mode"] & 0o077):
            raise RetentionError("dedicated build root must be user-owned and mode 0700 or stricter")
        return identity
    finally:
        os.close(fd)

def _remove_tree_contents(directory_fd: int, root_device: int, logical_path: Path,
                          root_filesystem_id: Any = None) -> None:
    """Remove children relative to a pinned directory, refusing type or inode changes."""
    if root_filesystem_id is None:
        root_filesystem_id = getattr(os.fstatvfs(directory_fd), "f_fsid", None)
        if root_filesystem_id is None:
            raise RetentionError(f"filesystem identity is unavailable while retiring: {logical_path}")
    elif getattr(os.fstatvfs(directory_fd), "f_fsid", None) != root_filesystem_id:
        raise RetentionError(f"filesystem identity changed while retiring: {logical_path}")
    for name in os.listdir(directory_fd):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        child_path = logical_path / name
        if stat.S_ISDIR(before.st_mode):
            if before.st_dev != root_device:
                raise RetentionError(f"refusing to remove mounted or foreign directory: {child_path}")
            child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                               | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                        or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_filesystem_id):
                    raise RetentionError(f"directory changed during retirement: {child_path}")
                _remove_tree_contents(child_fd, root_device, child_path, root_filesystem_id)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if ((current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) !=
                    (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))):
                raise RetentionError(f"directory entry changed before rmdir: {child_path}")
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(before.st_mode):
            if before.st_dev != root_device:
                raise RetentionError(f"refusing to remove a foreign file: {child_path}")
            child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                               | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if ((opened.st_dev, opened.st_ino, opened.st_size) != (before.st_dev, before.st_ino, before.st_size)
                        or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_filesystem_id):
                    raise RetentionError(f"file changed during retirement: {child_path}")
            finally:
                os.close(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
                    before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)):
                raise RetentionError(f"file entry changed before unlink: {child_path}")
            os.unlink(name, dir_fd=directory_fd)
        elif stat.S_ISLNK(before.st_mode):
            # The link itself is unlinked; its target is never opened or traversed.
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
                    before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode)):
                raise RetentionError(f"symlink changed before unlink: {child_path}")
            os.unlink(name, dir_fd=directory_fd)
        else:
            raise RetentionError(f"refusing to remove special file: {child_path}")
    os.fsync(directory_fd)

def _remove_verified_tree_at(parent_fd: int, name: str, *, expected_device: int,
                             expected_inode: int, logical_path: Path,
                             expected_filesystem_id: Any = None) -> None:
    """Descriptor-bound recursive removal under a private, lock-cooperating parent.

    POSIX exposes rmdir/unlink by parent fd plus name, not by child fd. The
    retention root is therefore required to be owner-private and every FG-Metal
    producer/mutator must hold BUILD_LOCK; each entry is revalidated immediately
    before unlink/rmdir. Same-UID processes that deliberately bypass this lock
    are outside the supported writer model.
    """
    initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISDIR(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != (expected_device, expected_inode)):
        raise RetentionError("final removal target differs from verified build inode")
    root_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        opened = os.fstat(root_fd)
        actual_filesystem_id = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if ((opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
                or (expected_filesystem_id is not None and actual_filesystem_id != expected_filesystem_id)):
            raise RetentionError("final removal root changed while opening")
        _remove_tree_contents(root_fd, expected_device, logical_path, actual_filesystem_id)
    finally:
        os.close(root_fd)
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected_device, expected_inode)):
        raise RetentionError("final removal root was replaced before rmdir")
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)

def _has_build_signature(path: Path) -> bool:
    """Recognize conventional compiler build roots even when their names omit 'build'."""
    strong_files = {"build.ninja", ".ninja_log", ".ninja_deps", "CMakeCache.txt", "config.status"}
    strong_dirs = {"meson-private", "CMakeFiles", "XCBuildData"}
    pending: list[tuple[Path, int]] = [(path, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as items:
                for item in items:
                    if item.is_symlink():
                        continue
                    if item.name in strong_files:
                        return True
                    if item.name in strong_dirs and item.is_dir(follow_symlinks=False):
                        return True
                    if depth < 3 and item.is_dir(follow_symlinks=False):
                        pending.append((Path(item.path), depth + 1))
        except OSError:
            raise RetentionError(f"cannot inspect candidate build root without following links: {directory}")
    return False


def _is_bison_source_dependency(path: Path) -> bool:
    """Classify the temporary in-source Bison dependency tree separately."""
    try:
        return (path.name.lower() == "fgmetal-bison-src"
                and all((path / name).is_file() for name in ("configure.ac", "Makefile.am", ".tarball-version"))
                and (path / "src/bison").is_file()
                and (path / "lib/libbison.a").is_file())
    except OSError:
        return False


def _is_protected_framegen_root(path: Path) -> bool:
    """Recognize earlier FrameGen/DXMT build roots from target/source identities."""
    name = path.name.lower()
    if (name in _KNOWN_PROTECTED_FRAMEGEN_ROOTS
            or name.startswith(("fg-metal-finalized.", "fg-metal-step1.", "fg-step10b-synthetic-",
                                "framegen-step", "dxmt-"))):
        return True
    # The CMake target/product identity is stronger than ambiguous temp naming.
    markers = (path / "framegen-core/CMakeFiles", path / "framegen-dxmt-synthetic-metal.app",
               path / "framegen-tests.dir", path / "tests/CMakeFiles/framegen-tests.dir")
    if any(marker.exists() for marker in markers):
        return True
    return False

def _is_legacy_step11d2_evidence_corpus(path: Path) -> bool:
    """Recognize the exact historical source/evidence archive, not its nested builds as live roots."""
    if path not in LEGACY_STEP11D2_EVIDENCE_CORPORA or path.is_symlink():
        return False
    try:
        return ((path / "REPORT.md").is_file() and not (path / "REPORT.md").is_symlink()
                and (path / "HASHES.json").is_file() and not (path / "HASHES.json").is_symlink()
                and _hash_path(path / "REPORT.md") == LEGACY_STEP11D2_REPORT_SHA256
                and _hash_path(path / "HASHES.json") == LEGACY_STEP11D2_HASHES_SHA256
                and (path / "source/bridge_protocol.h").is_file()
                and (path / "evidence").is_dir() and not (path / "evidence").is_symlink()
                and (path / "build").is_dir() and not (path / "build").is_symlink())
    except OSError:
        return False

def _is_spirv_generated_table_root(path: Path) -> bool:
    """Recognize the exact six generated SPIR-V source tables, not a compiler build tree."""
    if path != SPIRV_GENERATED_TABLE_ROOT or path.is_symlink():
        return False
    try:
        with os.scandir(path) as entries:
            items = list(entries)
        return (all(not item.is_symlink() and item.is_file(follow_symlinks=False) for item in items)
                and {item.name for item in items} == SPIRV_GENERATED_TABLE_FILES
                and (path.parent / "CMakeLists.txt").is_file()
                and (path.parent / "source").is_dir()
                and not _has_build_signature(path))
    except OSError:
        return False

def _registered_worktree_roots() -> list[Path]:
    """Return the repository and Git-registered worktrees used for root discovery."""
    roots = [REPO]
    proc = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=REPO,
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode:
        # Synthetic controller tests deliberately use a non-Git temporary root.
        if (REPO / ".git").exists():
            raise RetentionError(f"cannot enumerate registered worktrees: {proc.stderr[-1000:]}")
        return roots
    for line in proc.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree "))
        if candidate == REPO:
            continue
        if candidate.is_symlink():
            raise RetentionError(f"registered Git worktree path is a symlink: {candidate}")
        if not candidate.is_dir():
            raise RetentionError(f"registered Git worktree path is missing or unavailable: {candidate}")
        roots.append(candidate)
    return sorted(set(roots), key=str)


def _is_project_cache_target(path: Path) -> bool:
    """Whether an external alias is covered by the separately scanned cache roots."""
    cache_parent = HOME / "Library/Caches"
    if not cache_parent.is_dir():
        return False
    for root in cache_parent.iterdir():
        lower = root.name.lower()
        if (not lower.startswith(("fgmetal", "fg-metal")) and lower != "lsfg-metal"):
            continue
        if root.is_symlink() or not root.is_dir():
            raise RetentionError(f"project cache root is not a real directory: {root}")
        if path == root or root in path.parents:
            return True
    return False

def _worktree_build_root_candidates() -> list[Path]:
    """Find compiler build trees in the repo/worktrees without following links."""
    strong_files = {"build.ninja", ".ninja_log", ".ninja_deps", "CMakeCache.txt", "config.status"}
    strong_dirs = {"meson-private", "CMakeFiles", "XCBuildData"}
    candidates: set[Path] = set()

    def traversal_error(error: OSError) -> None:
        raise RetentionError(f"cannot completely scan registered worktree for build roots: {error}") from error

    for worktree in _registered_worktree_roots():
        evidence_store = worktree / "experiments/storage/build-manifests"
        for directory, dirnames, filenames in os.walk(
                worktree, followlinks=False, onerror=traversal_error):
            base = Path(directory)
            if _is_wine_prefix_tree(base):
                dirnames[:] = []
                continue
            if base == evidence_store:
                dirnames[:] = []
                continue
            kept: list[str] = []
            for name in dirnames:
                child = base / name
                if name == ".git":
                    continue
                if child.is_symlink():
                    # Internal aliases are covered by scanning their real target
                    # path. External directory aliases cannot be inspected without
                    # traversal, so retain them as UNKNOWN regardless of their name.
                    target_text = os.readlink(child)
                    target = Path(os.path.abspath(target_text if os.path.isabs(target_text)
                                                  else child.parent / target_text))
                    if (not _is_wine_drive_mapping(child, target_text)
                            and (target != worktree and worktree not in target.parents)
                            and not _is_project_cache_target(target)):
                        candidates.add(child)
                    continue
                kept.append(name)
            dirnames[:] = kept
            has_marker = bool(strong_files.intersection(filenames)) or any(
                name in strong_dirs and not (base / name).is_symlink() for name in dirnames)
            if has_marker:
                candidates.add(base)
                dirnames[:] = []
    return sorted(candidates, key=str)

def _legacy_step11d2_evidence_symlink_roots(path: Path) -> list[Path]:
    """Allow only the six recorded links to the exact pinned MoltenVK loader file."""
    if not _is_legacy_step11d2_evidence_corpus(path):
        return []
    target = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")
    expected_hash = "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f"
    if target.is_symlink() or not target.is_file() or _hash_path(target) != expected_hash:
        raise RetentionError("historical Step 11D.2 corpus loader target differs from its independently checked hash")
    links = []
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(directory)
        for name in [*dirnames, *filenames]:
            candidate = base / name
            if candidate.is_symlink():
                if os.readlink(candidate) != str(target):
                    raise RetentionError(f"historical Step 11D.2 corpus has an unapproved external symlink: {candidate}")
                links.append(candidate)
    if len(links) != 6:
        raise RetentionError(f"historical Step 11D.2 corpus loader-link count changed: {len(links)}")
    return [target]


def _classified_non_retention_tmp_roots() -> list[dict[str, Any]]:
    """Report source/dependency and protected workstream roots excluded from build deletion."""
    rows: list[dict[str, Any]] = []
    if not TEMP_ROOT.is_dir():
        return rows
    for path in TEMP_ROOT.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        if _is_legacy_step11d2_evidence_corpus(path):
            classification = "HISTORICAL-SOURCE-AND-EVIDENCE-CORPUS"
            reason = ("Step 11D.2 source, retained run evidence, and legacy diagnostic build variants; the nested "
                      "build outputs are historical archive material, not an active build lease. Their known hashes "
                      "and unexplained replacements are preserved by REPORT.md and HASHES.json.")
        elif _is_bison_source_dependency(path):
            classification = "SOURCE-DEPENDENCY-TREE"
            reason = "configured GNU Bison source/dependency tree; not a DXVK/MoltenVK experiment build root"
        elif _is_protected_framegen_root(path):
            classification = "PROTECTED-OTHER-WORKSTREAM"
            reason = "FrameGen/DXMT CMake source/build outputs are referenced by retained Step 10B evidence"
        elif (path.name.lower().startswith(("dxvk-", "framegen-"))
              and ((path / ".git").exists() or (path / "meson.build").is_file()
                   or (path / "CMakeLists.txt").is_file())):
            classification = "SOURCE-CHECKOUT"
            reason = "source checkout markers present; no generated build-tree marker at this root"
        else:
            continue
        try:
            root_fd = _open_dir(path)
            try:
                allowed = _legacy_step11d2_evidence_symlink_roots(path)
                stats = _tree_stats(root_fd, os.fstat(root_fd).st_dev, path,
                                    allowed_symlink_roots=allowed)
                root_stat = os.fstat(root_fd)
                root_fs_identity = _fs_identity(root_fd)
            finally:
                os.close(root_fd)
        except (OSError, RetentionError) as error:
            rows.append({"path": str(path), "classification": "UNKNOWN",
                         "reason": f"known external root could not be safely classified: {error}"})
            continue
        extra: dict[str, Any] = {}
        if classification == "HISTORICAL-SOURCE-AND-EVIDENCE-CORPUS":
            nested_builds = sorted(item for item in (path / "build").iterdir()
                                   if item.is_dir() and not item.is_symlink())
            nested_hash, nested_file_count = _tree_hash(path / "build")
            extra = {"nested_build_directory_count": len(nested_builds),
                     "nested_build_directory_names": [item.name for item in nested_builds],
                     "nested_build_content_tree_sha256": nested_hash,
                     "nested_build_file_count": nested_file_count}
        rows.append({"path": str(path), "classification": classification, "reason": reason,
                     "logical_bytes": stats["logical_bytes"],
                     "allocated_unique_inode_bytes": stats["allocated_unique_inode_bytes"],
                     "best_recoverable_bytes_estimate": stats["best_recoverable_bytes_estimate"],
                     "file_count": stats["file_count"], "filesystem_identity": root_fs_identity,
                     "root_mtime_ns": root_stat.st_mtime_ns,
                     "root_ctime_ns": getattr(root_stat, "st_ctime_ns", 0),
                     "action": "PRESERVE_AND_INVENTORY", **extra})
    return sorted(rows, key=lambda row: row["path"])

def _is_wine_prefix_tree(path: Path) -> bool:
    """Recognize a Wine prefix using a pinned root and no-follow child metadata."""
    root_fd: int | None = None
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RetentionError(f"cannot safely classify project-cache directory {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        return False
    if not stat.S_ISDIR(before.st_mode):
        return False
    try:
        root_fd = _open_dir(path)
        root_info = os.fstat(root_fd)
        if (root_info.st_dev, root_info.st_ino) != (before.st_dev, before.st_ino):
            raise RetentionError(f"directory changed while classifying Wine prefix: {path}")
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if root_fsid is None:
            raise RetentionError(f"filesystem identity unavailable while classifying Wine prefix: {path}")
        registry_info = os.stat("system.reg", dir_fd=root_fd, follow_symlinks=False)
        drive_info = os.stat("drive_c", dir_fd=root_fd, follow_symlinks=False)
        devices_info = os.stat("dosdevices", dir_fd=root_fd, follow_symlinks=False)
        for name, info in (("system.reg", registry_info), ("drive_c", drive_info),
                           ("dosdevices", devices_info)):
            expected_directory = name != "system.reg"
            if ((expected_directory and not stat.S_ISDIR(info.st_mode))
                    or (not expected_directory and not stat.S_ISREG(info.st_mode))):
                return False
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            if expected_directory:
                flags |= getattr(os, "O_DIRECTORY", 0)
            child_fd = os.open(name, flags, dir_fd=root_fd)
            try:
                opened = os.fstat(child_fd)
                child_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
                if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                        or opened.st_dev != root_info.st_dev or child_fsid != root_fsid):
                    raise RetentionError(
                        f"Wine-prefix signature entry changed filesystem or identity: {path / name}")
            finally:
                os.close(child_fd)
        current = path.lstat()
        if ((current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
                or getattr(os.fstatvfs(root_fd), "f_fsid", None) != root_fsid):
            raise RetentionError(f"directory was replaced while classifying Wine prefix: {path}")
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RetentionError(f"cannot safely classify project-cache directory {path}: {error}") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _is_wine_drive_mapping(path: Path, target_text: str) -> bool:
    """Recognize only the standard Wine drive links without following them."""
    return (path.parent.name == "dosdevices"
            and ((path.name == "z:" and target_text == "/")
                 or (path.name == "c:" and target_text == "../drive_c")))


def _known_readonly_cache_image_mount(mount: Path, cache_root: Path) -> bool:
    """Allow scanning a read-only image mount only when hdiutil binds it to an image in this cache."""
    if not os.path.ismount(mount):
        return False
    try:
        vfs = os.statvfs(mount)
        if not (vfs.f_flag & getattr(os, "ST_RDONLY", 1)):
            return False
        result = subprocess.run(["hdiutil", "info", "-plist"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            return False
        metadata = plistlib.loads(result.stdout)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    except Exception:
        return False
    for image in metadata.get("images", []):
        image_path_text = image.get("image-path")
        if not isinstance(image_path_text, str) or image.get("writeable") is not False:
            continue
        if not any(entity.get("mount-point") == str(mount)
                   for entity in image.get("system-entities", [])):
            continue
        image_path = Path(image_path_text)
        try:
            cache_real = cache_root.resolve(strict=True)
            image_parent = image_path.parent.resolve(strict=True)
            if (image_path.is_symlink()
                    or (image_parent != cache_real and cache_real not in image_parent.parents)):
                return False
            try:
                image_info = image_path.lstat()
            except FileNotFoundError:
                # hdiutil can keep an unlinked read-only image mounted.  The
                # hdiutil plist binding remains authoritative for scan scope.
                image_info = None
            if image_info is not None and not stat.S_ISREG(image_info.st_mode):
                return False
        except OSError:
            return False
        return True
    return False


def _verified_cache_scan_root(path: Path,
                              expected_identity: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """Read the no-follow directory identity and reject a replaced queued mount."""
    root_fd = _open_dir(path)
    try:
        root_info = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if root_fsid is None:
            raise RetentionError(f"project cache filesystem identity is unavailable: {path}")
        identity = (root_info.st_dev, root_info.st_ino, root_fsid)
        if expected_identity is not None and identity != expected_identity:
            raise RetentionError(f"project cache mount identity changed after discovery: {path}")
        return identity
    finally:
        os.close(root_fd)


def _all_root_candidates() -> list[Path]:
    candidates = [path for path in LEGACY_BUILD_ROOTS if path.exists() or path.is_symlink()]
    if BUILD_ROOT.exists() and not BUILD_ROOT.is_symlink():
        candidates.extend(BUILD_ROOT.iterdir())
    tmp_root = TEMP_ROOT
    if tmp_root.is_dir():
        def temp_scan_error(error: OSError) -> None:
            raise RetentionError(f"cannot completely scan temporary roots for compiler builds: {error}") from error

        excluded: set[Path] = set()
        for path in tmp_root.iterdir():
            if (not path.is_symlink() and path.is_dir()
                    and (_is_legacy_step11d2_evidence_corpus(path)
                         or _is_bison_source_dependency(path)
                         or _is_protected_framegen_root(path))):
                excluded.add(path)

        strong_files = {"build.ninja", ".ninja_log", ".ninja_deps", "CMakeCache.txt", "config.status"}
        strong_dirs = {"meson-private", "CMakeFiles", "XCBuildData"}
        for directory, dirnames, filenames in os.walk(tmp_root, followlinks=False, onerror=temp_scan_error):
            base = Path(directory)
            if _is_wine_prefix_tree(base):
                dirnames[:] = []
                continue
            if base in excluded:
                dirnames[:] = []
                continue
            retained: list[str] = []
            for name in dirnames:
                child = base / name
                if child.is_symlink():
                    target_text = os.readlink(child)
                    target = Path(os.path.abspath(target_text if os.path.isabs(target_text)
                                                  else child.parent / target_text))
                    if (not _is_wine_drive_mapping(child, target_text)
                            and (target != tmp_root and tmp_root not in target.parents)
                            and not _is_project_cache_target(target)):
                        candidates.append(child)
                    continue
                if child in excluded:
                    continue
                if _is_protected_framegen_root(child) or _is_bison_source_dependency(child):
                    continue
                retained.append(name)
            dirnames[:] = retained
            marker = bool(strong_files.intersection(filenames)) or any(
                name in strong_dirs and not (base / name).is_symlink() for name in filenames + dirnames)
            name_looks_build = (base == tmp_root and "build" in base.name.lower()) or (
                base.parent == tmp_root and "build" in base.name.lower()) or base.name.lower() == "build"
            if (base != tmp_root and (marker or name_looks_build)):
                candidates.append(base)
                if marker:
                    dirnames[:] = []
    # Build products have historically lived in several project cache roots,
    # not only the currently selected Step 11D-R cache.  Scan every FG-Metal
    # cache root for build-system markers so a nested root cannot evade the
    # global cap.  The canonical immutable artifact store is intentionally
    # pruned: its payloads are content-addressed evidence, and it is not an
    # approved build workspace.  Roots found outside BUILD_ROOT are detection
    # only and classify as UNKNOWN; this grants no deletion authority.
    cache_parent = HOME / "Library/Caches"
    project_cache_roots = []
    if cache_parent.is_dir():
        for path in cache_parent.iterdir():
            lower = path.name.lower()
            if not lower.startswith(("fgmetal", "fg-metal")) and lower != "lsfg-metal":
                continue
            if path.is_symlink() or not path.is_dir():
                raise RetentionError(f"project cache root is not a real directory: {path}")
            project_cache_roots.append(path)

    strong_files = {"build.ninja", ".ninja_log", ".ninja_deps", "CMakeCache.txt", "config.status"}
    strong_dirs = {"meson-private", "CMakeFiles", "XCBuildData"}
    for cache_root in project_cache_roots:
        scan_roots: list[tuple[Path, tuple[int, int, int] | None]] = [(cache_root, None)]
        scanned_roots: set[Path] = set()
        canonical_store = ARTIFACT_ROOT.resolve(strict=False) if cache_root == CACHE_ROOT else None
        while scan_roots:
            scan_root, expected_identity = scan_roots.pop()
            if scan_root in scanned_roots:
                continue
            scanned_roots.add(scan_root)
            root_identity = _verified_cache_scan_root(scan_root, expected_identity)
            root_device, _root_inode, root_fsid = root_identity

            def cache_scan_error(error: OSError) -> None:
                raise RetentionError(f"cannot completely scan project cache for compiler builds: {error}") from error

            for directory, dirnames, filenames in os.walk(scan_root, followlinks=False,
                                                           onerror=cache_scan_error):
                base = Path(directory)
                if _is_wine_prefix_tree(base):
                    dirnames[:] = []
                    continue
                retained = []
                for name in dirnames:
                    child = base / name
                    if canonical_store is not None and child == canonical_store:
                        continue
                    if child.is_symlink():
                        target_text = os.readlink(child)
                        target = Path(os.path.abspath(target_text if os.path.isabs(target_text)
                                                      else child.parent / target_text))
                        target_is_internal = target == cache_root or cache_root in target.parents
                        if (not target_is_internal
                                and not (name == "Applications" and target_text == "/Applications")
                                and not _is_wine_drive_mapping(child, target_text)):
                            candidates.append(child)
                        continue
                    child_fd = _open_dir(child)
                    try:
                        child_info = os.fstat(child_fd)
                        child_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
                        if child_info.st_dev != root_device or child_fsid != root_fsid:
                            if not _known_readonly_cache_image_mount(child, cache_root):
                                raise RetentionError(
                                    f"project cache scan encountered an unknown filesystem transition: {child}")
                            scan_roots.append((child, (child_info.st_dev, child_info.st_ino, child_fsid)))
                            continue
                    finally:
                        os.close(child_fd)
                    retained.append(name)
                dirnames[:] = retained
                if canonical_store is not None and base == canonical_store:
                    dirnames[:] = []
                    continue
                base_fd = _open_dir(base)
                try:
                    base_info = os.fstat(base_fd)
                    base_fsid = getattr(os.fstatvfs(base_fd), "f_fsid", None)
                    if base_info.st_dev != root_device or base_fsid != root_fsid:
                        raise RetentionError(f"project cache scan encountered a filesystem transition: {base}")
                finally:
                    os.close(base_fd)
                marker = bool(strong_files.intersection(filenames)) or any(
                    name in strong_dirs and not (base / name).is_symlink() for name in filenames + dirnames)
                name_looks_build = (base.parent == scan_root and "build" in base.name.lower()) \
                    or base.name.lower() == "build"
                if marker or (base != BUILD_ROOT and name_looks_build):
                    candidates.append(base)
                    if marker:
                        dirnames[:] = []
            if _verified_cache_scan_root(scan_root) != root_identity:
                raise RetentionError(f"project cache mount identity changed during scan: {scan_root}")
    derived = HOME / "Library/Developer/Xcode/DerivedData"
    if derived.is_dir():
        candidates.extend(path for path in derived.iterdir() if path.is_dir() and not path.is_symlink()
                          and path.name.lower().startswith("moltenvkpackaging-"))
    candidates.extend(_worktree_build_root_candidates())
    return sorted(set(candidates), key=str)

def _snapshot() -> dict[str, Any]:
    active, preserved, completed, failed, unknown, nonlarge = [], [], [], [], [], []
    by_path: dict[str, dict[str, Any]] = {}
    for lease_path in _lease_files():
        try:
            lease = _load_lease(lease_path.stem)
        except (OSError, RetentionError) as error:
            unknown.append({"path": str(lease_path), "reason": f"invalid lease: {error}"})
            continue
        if lease.get("build_id") != lease_path.stem or not isinstance(lease.get("path"), str):
            unknown.append({"path": str(lease_path), "reason": "lease identity mismatch"})
            continue
        by_path[os.path.abspath(lease["path"])] = lease
        row = {key: lease.get(key) for key in ("build_id", "path", "kind", "state", "owner_experiment",
              "estimated_bytes", "build_device", "build_inode", "build_filesystem_id", "build_manifest_id",
              "build_manifest_sha256", "requested_config_key_sha256", "preserve_debug_expires_at_unix")}
        if lease.get("retired_at_unix") is not None:
            receipt_path = RECEIPT_ROOT / f"{lease_path.stem}.json"
            try:
                if lease.get("cleanup_receipt_path") != str(receipt_path):
                    raise RetentionError("retired lease does not reference its canonical receipt path")
                receipt = _read_json(receipt_path, sealed=True)
                expected = {"result": "REMOVED", "state": "REMOVED", "build_id": lease_path.stem,
                            "path": lease.get("path"), "build_device": lease.get("build_device"),
                            "build_inode": lease.get("build_inode"),
                            "build_filesystem_id": lease.get("build_filesystem_id"),
                            "build_manifest_id": lease.get("build_manifest_id"),
                            "build_manifest_sha256": lease.get("build_manifest_sha256"),
                            "failure_evidence_index_sha256": lease.get("failure_evidence_index_sha256")}
                if any(receipt.get(key) != value for key, value in expected.items()):
                    raise RetentionError("retirement receipt does not bind the exact lease identity")
            except (OSError, RetentionError) as error:
                unknown.append({"path": lease["path"], "reason": f"retired root receipt invalid: {error}"})
            continue
        if lease.get("state") == "ACTIVE": active.append(row)
        elif lease.get("state") == "PRESERVE_DEBUG": preserved.append(row)
        elif lease.get("state") == "COMPLETED": completed.append(row)
        elif lease.get("state") in {"FAILED", "RETIRING"}: failed.append(row)
        else: unknown.append({"path": lease["path"], "reason": f"unrecognized state {lease.get('state')}"})
    for path in _all_root_candidates():
        if path.is_symlink() or not path.is_dir():
            unknown.append({"path": str(path), "reason": "root is symlink or not a directory"})
            continue
        lease = by_path.get(os.path.abspath(str(path)))
        if not lease:
            try:
                root_fd = _open_dir(path)
                try:
                    stats = _tree_stats(root_fd, os.fstat(root_fd).st_dev, path)
                    root_stat = os.fstat(root_fd)
                    root_fs_identity = _fs_identity(root_fd)
                finally:
                    os.close(root_fd)
            except (OSError, RetentionError) as error:
                unknown.append({"path": str(path), "reason": f"unleased build root cannot be safely classified: {error}"})
                continue
            if _is_spirv_generated_table_root(path):
                content_hash, _content_files = _tree_hash(path)
                nonlarge.append({"path": str(path), "classification": "SOURCE-GENERATED-SPIRV-TABLES",
                                 "reason": "exact generated SPIR-V source tables retained with their source checkout",
                                 "logical_bytes": stats["logical_bytes"],
                                 "allocated_unique_inode_bytes": stats["allocated_unique_inode_bytes"],
                                 "best_recoverable_bytes_estimate": stats["best_recoverable_bytes_estimate"],
                                 "file_count": stats["file_count"], "filesystem_identity": root_fs_identity,
                                 "root_mtime_ns": root_stat.st_mtime_ns,
                                 "root_ctime_ns": getattr(root_stat, "st_ctime_ns", 0),
                                 "content_tree_sha256": content_hash,
                                 "action": "PRESERVE_AS_SOURCE_DEPENDENCY"})
                continue
            if stats["logical_bytes"] >= LARGE_BUILD_ROOT_MIN_BYTES:
                unknown.append({"path": str(path), "reason": "unleased large build root",
                                "logical_bytes": stats["logical_bytes"]})
            else:
                name = path.name.lower()
                if "bridge-diagnostic" in name:
                    classification = "PRESERVE-FOR-DEBUG"
                    reason = "small Step 11D.2 bridge diagnostic input referenced by retained experiment evidence"
                elif "source-rebuild-check" in name:
                    classification = "FAILED"
                    reason = "partial source-rebuild diagnostic objects; preserve bounded diagnostics then retire"
                elif "wine-11.17-vkmetal-build" in name and stats["file_count"]:
                    classification = "FAILED"
                    reason = "small configure-only Wine build scratch; no compiled products"
                else:
                    classification = "COMPLETED-RECLAIMABLE"
                    reason = "small empty or Ninja metadata-only scratch; no compiled products"
                nonlarge.append({"path": str(path), "classification": classification,
                                 "reason": reason, "logical_bytes": stats["logical_bytes"],
                                 "allocated_unique_inode_bytes": stats["allocated_unique_inode_bytes"],
                                 "best_recoverable_bytes_estimate": stats["best_recoverable_bytes_estimate"],
                                 "file_count": stats["file_count"], "filesystem_identity": root_fs_identity,
                                 "root_mtime_ns": root_stat.st_mtime_ns,
                                 "root_ctime_ns": getattr(root_stat, "st_ctime_ns", 0)})
                # Small roots still require an explicit lease or reconciliation. The
                # large-root threshold is reporting only; it never exempts storage.
                unknown.append({"path": str(path), "reason": f"unleased small build root: {classification}",
                                "logical_bytes": stats["logical_bytes"], "details": reason})
            continue
        try:
            root_fd = _open_dir(path)
            try:
                marker = _read_marker(root_fd)
                root_info = os.fstat(root_fd)
                if (marker.get("build_id") != lease.get("build_id")
                        or marker.get("state") != lease.get("state")
                        or (root_info.st_dev, root_info.st_ino) != (lease.get("build_device"), lease.get("build_inode"))
                        or getattr(os.fstatvfs(root_fd), "f_fsid", None) != lease.get("build_filesystem_id")):
                    unknown.append({"path": str(path), "reason": "root marker disagrees with lease registry"})
            finally:
                os.close(root_fd)
        except (OSError, RetentionError, json.JSONDecodeError) as error:
            unknown.append({"path": str(path), "reason": f"invalid root marker: {error}"})
    for key, lease in by_path.items():
        if lease.get("retired_at_unix") is None and not Path(key).exists():
            unknown.append({"path": key, "reason": "registered build root is missing without a retirement receipt"})
    return {"active": active, "preserved_debug": preserved, "completed_pending_retirement": completed,
            "failed_or_retiring": failed, "unknown": unknown, "classified_nonlarge_roots": nonlarge,
            "classified_non_retention_roots": _classified_non_retention_tmp_roots(),
            "active_build_count": len(active), "preserved_build_count": len(preserved),
            "retained_build_count": len(active) + len(preserved),
            "active_dxvk_count": sum(row.get("kind") == "dxvk" for row in active),
            "active_moltenvk_count": sum(row.get("kind") == "moltenvk" for row in active)}

def capacity_blockers() -> list[str]:
    state = _snapshot()
    blockers = []
    if state["unknown"]: blockers.append("ambiguous or unleased build roots require reconciliation")
    if state["completed_pending_retirement"]: blockers.append("COMPLETED build roots remain pending automatic retirement")
    if state["failed_or_retiring"]: blockers.append("FAILED/RETIRING build roots require cleanup reconciliation")
    if state["active_build_count"] > MAX_ACTIVE_LARGE_BUILDS:
        blockers.append("global active large-build cap exceeded")
    if state["active_dxvk_count"] > MAX_ACTIVE_DXVK: blockers.append("active DXVK build cap exceeded")
    if state["active_moltenvk_count"] > MAX_ACTIVE_MOLTENVK: blockers.append("active MoltenVK diagnostic build cap exceeded")
    if state["preserved_build_count"] > MAX_PRESERVED_DEBUG: blockers.append("preserved reference/debug build cap exceeded")
    if state["retained_build_count"] > MAX_RETAINED: blockers.append("global retained build cap exceeded")
    return blockers

def _retention_fingerprint(state: Mapping[str, Any]) -> dict[str, Any]:
    retained = []
    for group in ("active", "preserved_debug", "completed_pending_retirement", "failed_or_retiring"):
        for row in state.get(group, []):
            retained.append({"group": group, **{key: row.get(key) for key in (
                "build_id", "path", "kind", "state", "owner_experiment", "estimated_bytes",
                "build_device", "build_inode", "build_filesystem_id", "build_manifest_id",
                "build_manifest_sha256", "requested_config_key_sha256", "preserve_debug_expires_at_unix")}})
    unknown = sorted(({"path": row.get("path"), "reason": row.get("reason"),
                       "logical_bytes": row.get("logical_bytes"), "allocated_unique_inode_bytes": row.get("allocated_unique_inode_bytes")}
                      for row in state.get("unknown", [])), key=lambda row: (str(row["path"]), str(row["reason"])))
    excluded_roots = sorted(({"path": row.get("path"), "classification": row.get("classification"),
                              "logical_bytes": row.get("logical_bytes"),
                              "allocated_unique_inode_bytes": row.get("allocated_unique_inode_bytes"),
                              "best_recoverable_bytes_estimate": row.get("best_recoverable_bytes_estimate"),
                              "file_count": row.get("file_count"), "filesystem_identity": row.get("filesystem_identity"),
                              "root_mtime_ns": row.get("root_mtime_ns"), "root_ctime_ns": row.get("root_ctime_ns"),
                              "nested_build_directory_count": row.get("nested_build_directory_count"),
                              "nested_build_directory_names": row.get("nested_build_directory_names"),
                              "nested_build_content_tree_sha256": row.get("nested_build_content_tree_sha256"),
                              "nested_build_file_count": row.get("nested_build_file_count"),
                              "nested_build_logical_bytes": row.get("nested_build_logical_bytes")}
                             for row in state.get("classified_non_retention_roots", [])),
                            key=lambda row: str(row["path"]))
    nonlarge = sorted(({"path": row.get("path"), "classification": row.get("classification"),
                        "logical_bytes": row.get("logical_bytes"),
                        "allocated_unique_inode_bytes": row.get("allocated_unique_inode_bytes"),
                        "best_recoverable_bytes_estimate": row.get("best_recoverable_bytes_estimate"),
                        "file_count": row.get("file_count"), "filesystem_identity": row.get("filesystem_identity"),
                        "root_mtime_ns": row.get("root_mtime_ns"), "root_ctime_ns": row.get("root_ctime_ns"),
                        "content_tree_sha256": row.get("content_tree_sha256")}
                       for row in state.get("classified_nonlarge_roots", [])), key=lambda row: str(row["path"]))
    material = {"build_root_identity": _root_identity(BUILD_ROOT, create=True),
                "retained_builds": sorted(retained, key=lambda row: str(row["path"])),
                "unknown_roots": unknown, "excluded_roots": excluded_roots, "small_roots": nonlarge}
    return {"sha256": hashlib.sha256(_canonical_json(material)).hexdigest(), **material}

def status_snapshot() -> dict[str, Any]:
    reconciliation_error = None
    lease_fd, lock_fd = _lock()
    try:
        try:
            _retire_pending_locked()
        except BaseException as error:
            reconciliation_error = repr(error)
        # Keep BUILD_LOCK through root enumeration and its final fingerprint.
        # BuildLease reserve/complete/retire operations use this same lock, so a
        # supported build writer cannot replace a prefix-pruned path mid-scan.
        state = _snapshot()
        state["policy"] = {"max_active_dxvk": MAX_ACTIVE_DXVK,
                           "max_active_moltenvk_diagnostic": MAX_ACTIVE_MOLTENVK,
                           "max_active_large_builds_total": MAX_ACTIVE_LARGE_BUILDS,
                           "active_large_builds_are_serialized_to_prevent_kind_aliasing": True,
                           "max_preserved_reference_debug": MAX_PRESERVED_DEBUG,
                           "max_global_retained": MAX_RETAINED,
                           "large_build_root_min_bytes": LARGE_BUILD_ROOT_MIN_BYTES,
                           "preserved_build_max_seconds": PRESERVE_DEBUG_MAX_SECONDS,
                           "completed_builds_auto_retire_at_completion": True}
        state["approved_new_build_root"] = str(BUILD_ROOT)
        state["approved_legacy_reconciliation_roots"] = [str(path) for path in LEGACY_BUILD_ROOTS]
        state["protected_or_source_root_count"] = len(state.get("classified_non_retention_roots", []))
        state["active_lease_markers"] = len(state.get("active", []))
        state["retention_fingerprint"] = _retention_fingerprint(state)
        state["gate_blockers"] = capacity_blockers()
        if reconciliation_error:
            state["gate_blockers"].append(f"automatic retirement reconciliation failed: {reconciliation_error}")
        state["gate_status"] = "PASS" if not state["gate_blockers"] else "BLOCKED"
        return state
    finally:
        _unlock(lease_fd, lock_fd)

def _gate_blockers(*, active_experiment_id: str | None = None) -> list[str]:
    from storage_policy import _gate0_blockers
    return _gate0_blockers(active_experiment_id=active_experiment_id)


def _verify_native_prepared_source(build: Path, request: NativeMoltenVKBuildRequest) -> dict[str, Any] | None:
    """Recheck the exact patched Git checkout and dependency pins after the build.

    Synthetic build-retention fixtures omit a prepared source root. Real native
    MoltenVK contracts must provide one, so completion proves the build did not
    silently switch source, patch contents, or dependency revisions.
    """
    relative_root = request.prepared_source_root_relative_path
    if relative_root is None:
        return None
    source_rel = Path(relative_root)
    if (source_rel.is_absolute() or "\\" in relative_root
            or any(part in {"", ".", ".."} for part in source_rel.parts)):
        raise RetentionError("native MoltenVK prepared-source path is not canonical")
    build_root = build.resolve(strict=True)
    source_root = build_root / source_rel
    cursor = build_root
    for part in source_rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RetentionError("native MoltenVK prepared source contains a symlink path component")
    if (not source_root.is_dir() or source_root.resolve(strict=True) != source_root
            or build_root not in source_root.parents):
        raise RetentionError("native MoltenVK prepared source is absent or escapes the leased build")

    def git(*arguments: str) -> bytes:
        result = subprocess.run(["/usr/bin/git", "-C", str(source_root), *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace")[-1000:]
            raise RetentionError(f"native MoltenVK source Git verification failed: {message}")
        return result.stdout

    top = Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip())
    if top.resolve(strict=True) != source_root:
        raise RetentionError("native MoltenVK source root is not the prepared Git checkout root")
    commit = os.fsdecode(git("rev-parse", "HEAD")).strip()
    tree = os.fsdecode(git("rev-parse", "HEAD^{tree}")).strip()
    if commit != request.pristine_git_commit or tree != request.pristine_git_tree:
        raise RetentionError("native MoltenVK prepared checkout differs from its pinned commit/tree")
    project_relative = Path(request.project_relative_path)
    if (project_relative.is_absolute() or "\\" in request.project_relative_path
            or any(part in {"", ".", ".."} for part in project_relative.parts)
            or (request.prepared_source_root_relative_path is not None
                and project_relative.parts[:len(source_rel.parts)] != source_rel.parts)):
        raise RetentionError("native MoltenVK project path is not canonical beneath its prepared source")
    project_file = build_root / project_relative / "project.pbxproj"
    if project_file.is_symlink() or not project_file.is_file():
        raise RetentionError("native MoltenVK project.pbxproj is absent or unsafe")
    project_fd = os.open(project_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        project_sha256 = _hash_fd(project_fd)
    finally:
        os.close(project_fd)
    if project_sha256 != request.project_file_sha256:
        raise RetentionError("native MoltenVK project file hash differs from the signed build request")

    expected_paths: dict[str, tuple[str, str, str]] = {}
    for relative, pre_sha, post_sha in request.modified_files:
        relative_path = Path(relative)
        if (relative_path.is_absolute() or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative_path.parts)
                or relative in expected_paths):
            raise RetentionError("native MoltenVK patch file list contains a non-canonical or duplicate path")
        expected_paths[relative] = (relative, pre_sha, post_sha)
        target = source_root / relative_path
        if target.is_symlink() or not target.is_file():
            raise RetentionError(f"native MoltenVK patched source file is absent or not regular: {relative}")
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RetentionError(f"native MoltenVK patched source is not a regular file: {relative}")
            actual_post_sha = _hash_fd(fd)
        finally:
            os.close(fd)
        if actual_post_sha != post_sha:
            raise RetentionError(f"native MoltenVK patched source hash mismatch: {relative}")

        base = subprocess.run(["/usr/bin/git", "-C", str(source_root), "show", f"HEAD:{relative}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if pre_sha == "ABSENT":
            if base.returncode == 0:
                raise RetentionError(f"native MoltenVK patch expected a new file but it exists in HEAD: {relative}")
        elif (base.returncode != 0
              or hashlib.sha256(base.stdout).hexdigest() != pre_sha):
            raise RetentionError(f"native MoltenVK pristine source hash mismatch: {relative}")

    status_raw = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    status_rows: list[tuple[str, str]] = []
    for record in status_raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RetentionError("native MoltenVK source has malformed Git status output")
        status_rows.append((os.fsdecode(record[3:]), os.fsdecode(record[:2])))
    expected_status = sorted((path, "??" if pre_sha == "ABSENT" else " M")
                             for path, pre_sha, _post_sha in request.modified_files)
    if sorted(status_rows) != expected_status:
        raise RetentionError("native MoltenVK source has unexpected changes after build: "
                             f"expected={expected_status!r}, actual={sorted(status_rows)!r}")

    external_paths = {
        "SPIRV-Cross": "SPIRV-Cross",
        "SPIRV-Tools": "SPIRV-Tools",
        "Volk": "Volk",
        "Vulkan-Headers": "Vulkan-Headers",
        "Vulkan-Tools": "Vulkan-Tools",
        "cereal": "cereal",
    }
    if set(request.external_revisions) != set(external_paths):
        raise RetentionError("native MoltenVK dependency revision map is incomplete or contains unknown dependencies")
    verified_revisions: dict[str, str] = {}
    for name, directory in external_paths.items():
        dependency = source_root / "External" / directory
        if dependency.is_symlink() or not dependency.is_dir():
            raise RetentionError(f"native MoltenVK dependency checkout is absent or linked: {name}")
        top_result = subprocess.run(["/usr/bin/git", "-C", str(dependency), "rev-parse", "--show-toplevel"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        result = subprocess.run(["/usr/bin/git", "-C", str(dependency), "rev-parse", "HEAD"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if top_result.returncode != 0 or result.returncode != 0:
            raise RetentionError(f"native MoltenVK dependency is not a Git checkout: {name}")
        if Path(os.fsdecode(top_result.stdout).strip()).resolve(strict=True) != dependency.resolve(strict=True):
            raise RetentionError(f"native MoltenVK dependency Git root differs from its pinned directory: {name}")
        revision = os.fsdecode(result.stdout).strip()
        if revision != request.external_revisions[name]:
            raise RetentionError(f"native MoltenVK dependency revision mismatch: {name}")
        status = subprocess.run(["/usr/bin/git", "-C", str(dependency), "status", "--porcelain=v1",
                                 "-z", "--untracked-files=all"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if status.returncode != 0 or status.stdout:
            raise RetentionError(f"native MoltenVK dependency working tree is not clean: {name}")
        verified_revisions[name] = revision
    if verified_revisions["Vulkan-Headers"] != request.vulkan_headers_revision:
        raise RetentionError("native MoltenVK Vulkan-Headers revision fields disagree")
    headers_tree_sha256, headers_file_count = _tree_hash(source_root / "External" / "Vulkan-Headers")
    if headers_tree_sha256 != request.vulkan_headers_tree_sha256:
        raise RetentionError("native MoltenVK Vulkan-Headers content tree hash mismatch")

    prepared_tree_sha256, prepared_file_count = _tree_hash(source_root)
    return {
        "status": "verified_prepared_source",
        "root_relative_path": relative_root,
        "pristine_git_commit": commit,
        "pristine_git_tree": tree,
        "project_file_sha256": project_sha256,
        "declared_pristine_source_tree_sha256": request.source_tree_sha256,
        "prepared_source_tree_sha256": prepared_tree_sha256,
        "prepared_source_file_count": prepared_file_count,
        "modified_files": [
            {"path": path, "pristine_sha256": pre_sha, "prepared_sha256": post_sha}
            for path, pre_sha, post_sha in sorted(request.modified_files)
        ],
        "git_status_after_build": [
            {"path": path, "status": status} for path, status in sorted(status_rows)
        ],
        "external_revisions": verified_revisions,
        "vulkan_headers_tree_sha256": headers_tree_sha256,
        "vulkan_headers_file_count": headers_file_count,
    }


def _copy_tree_artifacts_native_moltenvk(
    build: Path,
    build_id: str,
    evidence: Path,
    request: NativeMoltenVKBuildRequest,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconcile declared output roles, reject unexpected binaries, and canonicalize outputs."""
    prepared_source_verification = _verify_native_prepared_source(build, request)
    outputs: list[dict[str, Any]] = []

    # 1. Verify every declared output
    role_to_path: dict[str, Path] = {}
    for declared in request.declared_outputs:
        target_path = build / declared.relative_path
        if not target_path.exists():
            if declared.mandatory:
                raise RetentionError(f"mandatory output role '{declared.role}' missing at {declared.relative_path}")
            continue
        if declared.is_macho and not _is_macho(target_path):
            raise RetentionError(f"output role '{declared.role}' is not a valid Mach-O binary: {target_path}")
        if declared.is_macho:
            actual_architecture = _thin_macho_architecture(target_path)
            if (actual_architecture not in request.architectures
                    or actual_architecture not in declared.expected_architectures):
                raise RetentionError(
                    f"output role '{declared.role}' architecture mismatch: build={request.architectures}, "
                    f"declared={declared.expected_architectures}, actual={actual_architecture}")
        role_to_path[declared.role] = target_path
        artifact_record = _artifactize(target_path)
        artifact_record["role"] = declared.role
        if declared.is_macho:
            artifact_record["architectures"] = [actual_architecture]
        outputs.append(artifact_record)

    # 2. Scan products directory for undeclared binary escapes
    products_root = build / "Build/Products"
    if products_root.is_dir():
        for root, _, filenames in os.walk(products_root):
            for fname in filenames:
                fpath = Path(root) / fname
                if fpath.suffix.lower() in ARTIFACT_EXTENSIONS or _is_macho(fpath):
                    if not any(fpath.resolve() == p.resolve() for p in role_to_path.values()):
                        raise RetentionError(f"undeclared binary output discovered in build tree: {fpath}")

    # 3. Capture identity matching request
    identity: dict[str, Any] = {
        "build_system": "xcode_native",
        "native_moltenvk_request": _native_request_to_dict(request),
        "build_type": request.configuration.casefold(),
        "source_tree": request.source_tree_sha256,
        "source_identity": {
            "status": "verified_native_source_identity",
            "content_tree_sha256": request.source_tree_sha256,
            "pristine_git_commit": request.pristine_git_commit,
            "pristine_git_tree": request.pristine_git_tree,
            "patch_sha256": request.patch_sha256,
            "vulkan_headers_revision": request.vulkan_headers_revision,
            "vulkan_headers_tree_sha256": request.vulkan_headers_tree_sha256,
        },
        "prepared_source_verification": prepared_source_verification,
        "config_key_sha256": compute_native_moltenvk_config_key(request),
        "toolchain_identity": {
            "xcode_version": request.xcode_version_string,
            "clang_version": request.clang_version_string,
            "clang_binary_sha256": request.clang_binary_sha256,
            "sdk_version": request.sdk_version_string,
            "linker_version": request.linker_version_string,
            "toolchain_identity_sha256": request.toolchain_identity_sha256,
        },
    }
    return outputs, identity


def _copy_tree_artifacts(build: Path, build_id: str, evidence: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity = _capture_identity(build, evidence)
    outputs = []
    for directory, dirnames, filenames in os.walk(build, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if not (base / name).is_symlink())
        for name in sorted(filenames):
            path = base / name
            if path.name == BUILD_MARKER:
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                resolved = path.resolve(strict=True)
                if _canonical_artifact_target(resolved):
                    digest = resolved.relative_to(ARTIFACT_ROOT).parts[0]
                    outputs.append({"sha256": digest, "canonical_artifact_path": str(resolved),
                                    "artifact_manifest_path": str(ARTIFACT_ROOT / digest / "manifest.json"),
                                    "size_bytes": resolved.stat().st_size, "artifact_name": resolved.name,
                                    "source_build_path": str(path), "source_symlink_target": os.readlink(path),
                                    "source_build_hard_link_count": 0, "reused_canonical_artifact": True})
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if path.suffix.lower() not in ARTIFACT_EXTENSIONS and not _is_macho(path):
                continue
            outputs.append(_artifactize(path))
    if not outputs:
        raise RetentionError("completed build has no artifact outputs")
    outputs.sort(key=lambda row: (row["sha256"], row["canonical_artifact_path"]))
    output_by_source = {row.get("source_build_path"): row for row in outputs if row.get("source_build_path")}
    product_manifests = []
    products = build / "Build/Products"
    if products.is_dir() and not products.is_symlink():
        for name in ("Release", "XCFrameworkStaging"):
            product = products / name
            if product.is_dir() and not product.is_symlink():
                product_manifests.append(_capture_product_tree(product, evidence, output_by_source))
    identity["product_tree_manifests"] = product_manifests
    identity["meson_configuration_sha256"] = identity.get("meson_configuration_sha256") or hashlib.sha256(
        _canonical_json({"build_system": identity.get("build_system"),
                         "command_line": identity.get("meson_command_line_sha256"),
                         "options": identity.get("meson_options_sha256"),
                         "compiler": identity.get("compiler_identity_sha256"),
                         "cross_file": (identity.get("cross_file") or {}).get("sha256"),
                         "xcode_project": identity.get("xcode_project_sha256"),
                         "xcode_info": identity.get("xcode_info_plist_sha256"),
                         "xcode_requests": identity.get("xcode_build_requests"),
                         "xcode_version": identity.get("xcode_version"),
                         "xcode_toolchain": identity.get("xcode_toolchain_identity")})).hexdigest()
    return outputs, identity

def _capture_product_tree(product: Path, evidence: Path,
                          artifacts_by_source: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Retain a reconstructable Xcode product layout, including links and headers."""
    root_fd = _open_dir(product)
    root_info = os.fstat(root_fd)
    entries: list[dict[str, Any]] = []
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = relative_dir / name
                    if info.st_dev != root_info.st_dev:
                        raise RetentionError(f"product tree crosses a filesystem boundary: {relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            os.close(child_fd)
                            raise RetentionError(f"product directory changed while opening: {relative}")
                        entries.append({"path": relative.as_posix(), "type": "directory",
                                        "mode": oct(stat.S_IMODE(info.st_mode))})
                        stack.append((child_fd, relative))
                    elif stat.S_ISLNK(info.st_mode):
                        target = os.readlink(name, dir_fd=parent_fd)
                        resolved = (Path(target) if os.path.isabs(target) else product / relative_dir / target)
                        resolved = resolved.resolve(strict=True)
                        if product.resolve(strict=True) not in resolved.parents and resolved != product.resolve(strict=True):
                            if not _canonical_artifact_target(resolved):
                                raise RetentionError(f"product symlink escapes product tree and CAS: {relative}")
                        row: dict[str, Any] = {"path": relative.as_posix(), "type": "symlink", "target": target}
                        if _canonical_artifact_target(resolved):
                            row["canonical_artifact_sha256"] = resolved.parent.name
                        entries.append(row)
                    elif stat.S_ISREG(info.st_mode):
                        source = product / relative
                        artifact = artifacts_by_source.get(str(source))
                        if artifact is not None:
                            entries.append({"path": relative.as_posix(), "type": "canonical_artifact",
                                            "sha256": artifact["sha256"],
                                            "canonical_artifact_path": artifact["canonical_artifact_path"],
                                            "size_bytes": info.st_size})
                        else:
                            retained = _copy_evidence(source, evidence / "products" / product.name / relative)
                            entries.append({"path": relative.as_posix(), "type": "evidence_file",
                                            "evidence_path": retained["retained_path"],
                                            "sha256": retained["sha256"], "size_bytes": retained["size_bytes"]})
                    else:
                        raise RetentionError(f"unsupported product object type: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda row: row["path"])
    record = {"schema_version": 1, "source_product_path": str(product),
              "product_name": product.name, "entries": entries,
              "tree_sha256": hashlib.sha256(_canonical_json(entries)).hexdigest()}
    target = evidence / "products" / f"{product.name}.product-tree.json"
    retained = _write_evidence_bytes(target, (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())
    return {"path": str(target), "sha256": retained["sha256"], "tree_sha256": record["tree_sha256"],
            "entry_count": len(entries), "source_product_path": str(product)}

def _build_manifest(lease: Mapping[str, Any], artifacts: list[dict[str, Any]], identity: Mapping[str, Any],
                    evidence: Path, *, provenance_scope: str) -> tuple[dict[str, Any], str]:
    source = identity.get("source_identity") or {}
    if evidence != MANIFEST_ROOT / f"{lease['build_id']}.evidence":
        raise RetentionError("build evidence path is not canonical")
    _validate_completed_identity(identity, artifacts, evidence)
    evidence_files = _inventory_evidence(evidence)
    evidence_index_sha256 = hashlib.sha256(_canonical_json(evidence_files)).hexdigest()
    material = {"schema_version": 3, "build_id": lease["build_id"], "kind": lease["kind"],
                "owner_experiment": lease["owner_experiment"], "purpose": lease["purpose"],
                "source_tree_hash_at_lease": lease.get("source_tree_hash"),
                "source_identity": source,
                "patch_hashes": {key: value for key, value in source.items() if "patch" in key},
                "dependency_state": identity.get("dependency_state",
                    lease.get("metadata", {}).get("dependency_state")),
                "cross_file": identity.get("cross_file"), "meson_options": identity.get("meson_options"),
                "meson_configuration_sha256": identity.get("meson_configuration_sha256"),
                "compiler_identity": identity.get("compiler_identity"),
                "compiler_identity_sha256": identity.get("compiler_identity_sha256"),
                "compiler_versions": identity.get("compiler_versions"),
                "linker_identity": identity.get("linker_identity"),
                "build_type": identity.get("build_type"),
                "generated_shader_identity": identity.get("generated_shader_identity", []),
                "build_log_sha256": identity.get("build_log_sha256"),
                "build_log_status": identity.get("build_log_status"),
                "ninja_log_sha256": identity.get("ninja_log_sha256"),
                "build_identity": dict(identity),
                "artifacts": artifacts, "config_key_sha256": identity.get("config_key_sha256"),
                "adopted_existing": bool(lease.get("adopted_existing")),
                "configuration_key_provenance": (
                    "legacy-debug-preservation-only" if identity.get("legacy_debug_preservation") is True else
                    "artifactization-time retrospective" if lease.get("adopted_existing") else
                    "pre-build frozen request"),
                "captured_config_key_sha256": identity.get("config_key_sha256"),
                "requested_config_key_sha256": lease.get("requested_config_key_sha256"),
                "canonical_artifact_root": str(ARTIFACT_ROOT), "evidence_path": str(evidence),
                "evidence_files": evidence_files, "evidence_index_sha256": evidence_index_sha256,
                "provenance_scope": provenance_scope, "completed_at_unix": time.time(),
                "preserve_debug_reason": lease.get("preserve_debug_reason"),
                "preserve_debug_expires_at_unix": lease.get("preserve_debug_expires_at_unix"),
                "requested_build_type": lease.get("metadata", {}).get("build_type"),
                "estimated_debug_bytes": lease.get("metadata", {}).get("estimated_debug_bytes"),
                "estimated_bytes": lease.get("estimated_bytes")}
    material["build_manifest_id"] = _manifest_id(material)
    material["manifest_sha256"] = _manifest_checksum(material)
    final_raw = (json.dumps(material, indent=2, sort_keys=True) + "\n").encode()
    path = MANIFEST_ROOT / f"{lease['build_id']}.json"
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise RetentionError("existing build manifest path is not a regular file")
        existing, digest = _verify_build_manifest(lease["build_id"])
        comparable_existing = {key: value for key, value in existing.items()
                               if key not in {"completed_at_unix", "build_manifest_id", "manifest_sha256"}}
        comparable_current = {key: value for key, value in material.items()
                              if key not in {"completed_at_unix", "build_manifest_id", "manifest_sha256"}}
        if comparable_existing != comparable_current:
            raise RetentionError("existing verified build manifest conflicts with this completion attempt")
        return existing, digest
    _write_json(path, material, replace=False)
    verified, digest = _verify_build_manifest(lease["build_id"])
    if digest != hashlib.sha256(final_raw).hexdigest():
        raise RetentionError("build manifest durability/hash verification failed")
    return verified, digest

def _validate_completed_identity(identity: Mapping[str, Any], artifacts: list[dict[str, Any]],
                                 evidence: Path) -> None:
    """Require reconstructable, hash-bound provenance before a build can be retired."""
    if not artifacts or not all(_verify_artifact(row) for row in artifacts):
        raise RetentionError("completed build is missing a verified canonical output")
    if identity.get("legacy_debug_preservation") is True:
        if identity.get("build_system") != "legacy-debug-preservation":
            raise RetentionError("legacy debug preservation has a conflicting build identity")
        expected_hashes = identity.get("legacy_debug_artifact_hashes")
        actual_hashes = {row.get("artifact_name"): row.get("sha256") for row in artifacts}
        if not isinstance(expected_hashes, Mapping) or dict(expected_hashes) != actual_hashes:
            raise RetentionError("legacy debug manifest does not bind all canonical output hashes")
        context_rows = identity.get("legacy_debug_context_evidence")
        symbol_rows = identity.get("legacy_debug_symbols")
        if not isinstance(context_rows, list) or not context_rows or not isinstance(symbol_rows, list) or not symbol_rows:
            raise RetentionError("legacy debug preservation lacks run identity or symbol evidence")
        for row in [*context_rows, *symbol_rows]:
            retained, digest = row.get("retained_path"), row.get("sha256")
            if (not isinstance(retained, str) or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise RetentionError("legacy debug evidence has an invalid retained-file reference")
            target = Path(retained)
            try:
                resolved = target.resolve(strict=True)
                if resolved != target or (resolved != evidence and evidence.resolve(strict=True) not in resolved.parents):
                    raise RetentionError("legacy debug evidence escapes its canonical evidence directory")
                if _hash_path(target) != digest:
                    raise RetentionError("legacy debug evidence hash mismatch")
            except OSError as error:
                raise RetentionError(f"legacy debug evidence is missing: {target}") from error
        if any(not str(row.get("retained_path", "")).endswith((".o", ".obj")) for row in symbol_rows):
            raise RetentionError("legacy debug symbol evidence contains a non-object file")
        if any(not isinstance(identity.get(field), Mapping) or identity[field].get("status") is None
               for field in ("source_identity", "patch_set", "dependency_state", "compiler_identity", "linker_identity")):
            raise RetentionError("legacy debug manifest must explicitly state provenance limitations")
        return
    try:
        config_key = configuration_key_from_identity(
            identity, dependency_state=identity.get("dependency_state"))
    except (TypeError, ValueError) as error:
        raise RetentionError(f"completed build configuration identity is incomplete: {error}") from error
    if identity.get("config_key_sha256") != config_key:
        raise RetentionError("completed build configuration key does not match its captured identity")
    def require_record(value: Any, label: str) -> None:
        retained_value = value.get("retained_path", value.get("path")) if isinstance(value, Mapping) else None
        if (not isinstance(value, Mapping) or not isinstance(retained_value, str)
                or not isinstance(value.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])):
            raise RetentionError(f"completed build is missing retained {label} evidence")
        expected_parent = str(evidence.resolve(strict=True))
        retained = Path(retained_value)
        resolved = retained.resolve(strict=True)
        expected_root = Path(expected_parent)
        if resolved != retained or (resolved != expected_root and expected_root not in resolved.parents):
            raise RetentionError(f"completed build {label} evidence escaped its canonical evidence directory")
        if _hash_path(retained) != value["sha256"]:
            raise RetentionError(f"completed build {label} evidence hash mismatch")
    source = identity.get("source_identity")
    if not isinstance(source, Mapping) or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("content_tree_sha256", ""))):
        raise RetentionError("completed build lacks a verified source-tree identity")
    if identity.get("build_system") == "meson":
        for key, label in (("meson_log_evidence", "Meson configure log"),
                           ("meson_command_line_evidence", "Meson command line"),
                           ("meson_options_evidence", "Meson options"),
                           ("compiler_evidence", "compiler identity"),
                           ("build_ninja_evidence", "Ninja build graph")):
            require_record(identity.get(key), label)
        if identity.get("ninja_execution_log_status") != "captured":
            raise RetentionError("completed Meson build lacks its Ninja execution log")
        require_record(identity.get("ninja_log_evidence"), "Ninja execution log")
        cross = identity.get("cross_file")
        if not isinstance(cross, Mapping) or cross.get("status") not in {"present", "absent"}:
            raise RetentionError("completed Meson build lacks an explicit cross-file disposition")
        if cross.get("status") == "present":
            require_record(cross, "cross-file")
        if identity.get("build_log_status") not in {
            "stdout transcript retained",
            "no stdout build transcript present; configure log and Ninja execution log retained separately",
        }:
            raise RetentionError("completed Meson build has an invalid build-log disposition")
        dependency = identity.get("dependency_state")
        if not isinstance(dependency, Mapping) or dependency.get("status") not in {"captured", "explicitly-none"}:
            raise RetentionError("completed Meson build lacks a dependency-state record")
        if dependency.get("status") == "captured":
            require_record(identity.get("intro-dependencies_json_evidence"), "Meson dependency graph")
        if not isinstance(identity.get("generated_shader_identity"), list):
            raise RetentionError("completed Meson build lacks generated-shader identity")
        if not identity.get("linker_identity"):
            raise RetentionError("completed Meson build lacks linker version evidence")
    elif identity.get("build_system") == "xcode":
        require_record(identity.get("xcode_info_plist_evidence"), "Xcode Info.plist")
        require_record(identity.get("xcode_project_evidence"), "Xcode project")
        requests = identity.get("xcode_build_requests")
        if not isinstance(requests, list) or not requests:
            raise RetentionError("completed Xcode build lacks build request settings")
        for row in requests:
            require_record(row, "Xcode build request")
        configurations = identity.get("xcode_configurations")
        if (not isinstance(configurations, list) or not configurations
                or len(configurations) != len(requests)
                or any(not isinstance(value, str) or value.lower() != "release" for value in configurations)
                or any(not isinstance(row.get("configuration_name"), str)
                       or row["configuration_name"].lower() != "release" for row in requests)):
            raise RetentionError("completed Xcode build is not proven to use Release build requests")
        tools = identity.get("xcode_toolchain_identity")
        if not isinstance(tools, Mapping):
            raise RetentionError("completed Xcode build lacks compiler/linker identity")
        for tool in ("clang", "linker"):
            row = tools.get(tool)
            if not isinstance(row, Mapping) or row.get("returncode") != 0 or not row.get("output"):
                raise RetentionError(f"completed Xcode build lacks successful {tool} version evidence")
        activity_logs = identity.get("xcode_activity_logs")
        if not isinstance(activity_logs, list) or not activity_logs:
            raise RetentionError("completed Xcode build lacks an activity log")
        for row in activity_logs:
            require_record(row, "Xcode activity log")
        products = identity.get("product_tree_manifests")
        if not isinstance(products, list) or not products:
            raise RetentionError("completed Xcode build lacks a product-tree manifest")
        for row in products:
            require_record(row, "Xcode product tree")
        if identity.get("build_type") != "release":
            raise RetentionError("completed Xcode MoltenVK build is not identified as Release")
    elif identity.get("build_system") == "xcode_native":
        req_dict = identity.get("native_moltenvk_request")
        if not isinstance(req_dict, Mapping):
            raise RetentionError("completed native MoltenVK build lacks native request dictionary")
        req = _dict_to_native_request(req_dict)
        if identity.get("build_type") != req.configuration.casefold():
            raise RetentionError("completed native MoltenVK build configuration mismatch")
        tools = identity.get("toolchain_identity")
        if not isinstance(tools, Mapping) or not tools.get("clang_binary_sha256"):
            raise RetentionError("completed native MoltenVK build lacks verified toolchain identity")
    else:
        raise RetentionError(f"unsupported completed build system: {identity.get('build_system')}")

def _verify_failure_evidence(lease: Mapping[str, Any]) -> dict[str, Any]:
    build_id = str(lease.get("build_id", ""))
    root = MANIFEST_ROOT / f"{build_id}.failure"
    if lease.get("failure_evidence_root") != str(root):
        raise RetentionError("failed build evidence root is not canonical")
    expected = lease.get("failure_evidence_files")
    if not isinstance(expected, list):
        raise RetentionError("failed build evidence lacks a durable file index")
    actual = _inventory_evidence(root)
    if actual != expected or hashlib.sha256(_canonical_json(actual)).hexdigest() != lease.get(
            "failure_evidence_index_sha256"):
        raise RetentionError("failed build diagnostic evidence is incomplete or changed")
    manifest_path = root / "failure-manifest.json"
    if lease.get("failure_evidence_path") != str(manifest_path):
        raise RetentionError("failed build manifest path is not canonical")
    raw = _read_file_descriptor(manifest_path)
    if hashlib.sha256(raw).hexdigest() != lease.get("failure_evidence_sha256"):
        raise RetentionError("failed build diagnostic manifest hash changed")
    value = json.loads(raw)
    if value.get("build_id") != build_id:
        raise RetentionError("failed build diagnostic manifest identifies another build")
    return value

def _read_file_descriptor(path: Path) -> bytes:
    parent_fd = _open_dir(path.parent)
    try:
        return _read_at(parent_fd, path.name, limit=64 * 1024 * 1024)
    finally:
        os.close(parent_fd)

def _retire(lease: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Quarantine, revalidate, and descriptor-delete one private leased build root."""
    state = lease.get("state")
    if state not in {"COMPLETED", "FAILED", "RETIRING"}:
        raise RetentionError(f"build state is not retirement-eligible: {state}")
    origin_state = lease.get("retirement_from_state", state)
    if origin_state not in {"COMPLETED", "FAILED"}:
        raise RetentionError(f"invalid retirement origin state: {origin_state}")
    build_id = str(lease.get("build_id", ""))
    manifest: dict[str, Any] | None = None
    if origin_state == "COMPLETED":
        manifest, digest = _verify_build_manifest(build_id)
        if (lease.get("build_manifest_id") != manifest.get("build_manifest_id")
                or lease.get("build_manifest_sha256") != digest):
            raise RetentionError("lease does not bind the verified build manifest")
    else:
        _verify_failure_evidence(lease)

    path = Path(lease["path"])
    parent = Path(lease["approved_parent"])
    if parent != BUILD_ROOT or path.parent != BUILD_ROOT or path.name in {"", ".", ".."}:
        raise RetentionError("retirement target is outside the private approved build root")
    parent_fd = _open_dir(BUILD_ROOT)
    try:
        parent_identity = _fs_identity(parent_fd)
        if parent_identity != lease.get("approved_parent_identity"):
            raise RetentionError("approved root device/inode/filesystem identity changed")
        root_mode = parent_identity.get("mode", 0)
        if parent_identity.get("owner_uid") != os.getuid() or root_mode & 0o077:
            raise RetentionError("retirement parent is not private to the current user")

        receipt_path = RECEIPT_ROOT / f"{build_id}.json"
        quarantine = lease.get("quarantine_name")
        original_exists = _exists_at(parent_fd, path.name)
        quarantine_exists = isinstance(quarantine, str) and _exists_at(parent_fd, quarantine)
        if original_exists and quarantine_exists:
            raise RetentionError("both original and quarantine build roots exist")
        if not original_exists and not quarantine_exists:
            try:
                receipt = _read_json(receipt_path, sealed=True)
            except FileNotFoundError as error:
                raise RetentionError("build is absent without a durable retirement intent/receipt") from error
            if (receipt.get("build_id") != build_id or receipt.get("path") != str(path)
                    or receipt.get("build_manifest_id") != lease.get("build_manifest_id")
                    or receipt.get("build_manifest_sha256") != lease.get("build_manifest_sha256")
                    or receipt.get("build_device") != lease.get("build_device")
                    or receipt.get("build_inode") != lease.get("build_inode")
                    or receipt.get("build_filesystem_id") != lease.get("build_filesystem_id")):
                raise RetentionError("retirement receipt does not bind the exact build identity")
            if receipt.get("state") == "RETIREMENT_INTENT":
                if not lease.get("cleanup_attempted"):
                    raise RetentionError("build vanished without a recorded cleanup attempt")
                receipt.update({"state": "REMOVED", "result": "REMOVED",
                                "filesystem_free_after_bytes": shutil.disk_usage(REPO).free,
                                "completed_at_unix": time.time(),
                                "recovered_after_interrupted_finalization": True})
                receipt["measured_free_delta_bytes"] = (
                    receipt["filesystem_free_after_bytes"] - receipt["filesystem_free_before_bytes"])
                _write_json(receipt_path, receipt, replace=True, sealed=True)
            elif receipt.get("state") != "REMOVED" or receipt.get("result") != "REMOVED":
                raise RetentionError("build is absent without a matching successful receipt")
            _store_lease(dict(lease, state="COMPLETED", retired_at_unix=time.time(),
                              cleanup_receipt_path=str(receipt_path)))
            return _read_json(receipt_path, sealed=True)

        entry_name = quarantine if quarantine_exists else path.name
        entry = os.stat(entry_name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(entry.st_mode)
                or (entry.st_dev, entry.st_ino) != (lease.get("build_device"), lease.get("build_inode"))):
            raise RetentionError("target root type/device/inode differs from the signed build lease")
        target_fd = os.open(entry_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(target_fd)
            if ((opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
                    or getattr(os.fstatvfs(target_fd), "f_fsid", None) != lease.get("build_filesystem_id")):
                raise RetentionError("build root changed while opening for retirement")
            marker: dict[str, Any] | None = None
            try:
                marker = _read_marker(target_fd)
            except (FileNotFoundError, RetentionError, json.JSONDecodeError):
                if not lease.get("cleanup_attempted"):
                    raise RetentionError("build marker missing before a durable cleanup intent")
            if marker is not None and (marker.get("build_id") != build_id or marker.get("path") != str(path)
                                       or marker.get("state") not in {origin_state, "RETIRING"}):
                raise RetentionError("build ID/state marker verification failed")
            stats = lease.get("retirement_tree_stats")
            if not isinstance(stats, dict):
                stats = _tree_stats(target_fd, opened.st_dev, path,
                                    allowed_symlink_roots=[Path(value) for value in
                                                           lease.get("allowed_external_symlink_roots", [])])
        finally:
            os.close(target_fd)

        if state != "RETIRING":
            quarantine = f".retiring-{build_id}-{uuid.uuid4().hex}"
            retiring = dict(lease, state="RETIRING", retirement_from_state=origin_state,
                            retirement_reason=reason, retirement_started_unix=time.time(),
                            quarantine_name=quarantine, retirement_tree_stats=stats,
                            cleanup_attempted=False)
            _store_lease(retiring)
        else:
            retiring = dict(lease)
            retiring.setdefault("retirement_reason", reason)
            if (not isinstance(quarantine, str)
                    or not re.fullmatch(re.escape(f".retiring-{build_id}-") + r"[0-9a-f]{32}", quarantine)):
                raise RetentionError("retirement quarantine name is not build-bound")
            retiring.setdefault("retirement_tree_stats", stats)

        if not quarantine_exists:
            _require_no_live_process(path)
            target_fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                marker = _read_marker(target_fd)
                if (marker.get("build_id") != build_id or marker.get("path") != str(path)
                        or marker.get("state") not in {origin_state, "RETIRING"}):
                    raise RetentionError("build marker changed before RETIRING transition")
                marker.update({"state": "RETIRING", "retirement_started_unix": retiring["retirement_started_unix"]})
                _write_marker(target_fd, marker)
                os.fsync(target_fd)
                opened = os.fstat(target_fd)
                if ((opened.st_dev, opened.st_ino) != (lease.get("build_device"), lease.get("build_inode"))
                        or getattr(os.fstatvfs(target_fd), "f_fsid", None) != lease.get("build_filesystem_id")):
                    raise RetentionError("target changed before retirement namespace operation")
                _rename_exclusive(parent_fd, path.name, quarantine)
            finally:
                os.close(target_fd)

        moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(moved.st_mode)
                or (moved.st_dev, moved.st_ino) != (lease.get("build_device"), lease.get("build_inode"))):
            raise RetentionError("quarantined target identity differs from its lease")
        target_fd = os.open(quarantine, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            opened = os.fstat(target_fd)
            if ((opened.st_dev, opened.st_ino) != (moved.st_dev, moved.st_ino)
                    or getattr(os.fstatvfs(target_fd), "f_fsid", None) != lease.get("build_filesystem_id")):
                raise RetentionError("quarantine changed while opening")
            marker_exists = _exists_at(target_fd, BUILD_MARKER)
            if marker_exists:
                marker = _read_marker(target_fd)
                if (marker.get("build_id") != build_id or marker.get("path") != str(path)
                        or marker.get("state") != "RETIRING"):
                    raise RetentionError("quarantine marker does not identify this retiring build")
            elif not retiring.get("cleanup_attempted"):
                raise RetentionError("quarantine marker is missing before cleanup was recorded")
            # Scan before the durable intent on first attempt. After interruption, the
            # intent binds the original tree and the directory may be partially removed.
            intent = None
            try:
                intent = _read_json(receipt_path, sealed=True)
            except FileNotFoundError:
                pass
            if intent is None:
                current_stats = _tree_stats(target_fd, opened.st_dev, parent / quarantine,
                                            allowed_symlink_roots=[Path(value) for value in
                                                                   lease.get("allowed_external_symlink_roots", [])])
                if current_stats != stats:
                    raise RetentionError("quarantined build tree changed after its pre-retirement inventory")
        finally:
            os.close(target_fd)

        _require_no_live_process(parent / quarantine)
        free_before = shutil.disk_usage(REPO).free
        artifact_hashes = [row["sha256"] for row in manifest["artifacts"]] if manifest else []
        try:
            intent = _read_json(receipt_path, sealed=True)
        except FileNotFoundError:
            intent = {"schema_version": 2, "state": "RETIREMENT_INTENT", "result": "PENDING",
                      "build_id": build_id, "path": str(path), "quarantine_path": str(parent / quarantine),
                      "build_device": lease["build_device"], "build_inode": lease["build_inode"],
                      "build_filesystem_id": lease.get("build_filesystem_id"),
                      "category": "COMPLETED BUILD DIRECTORY" if origin_state == "COMPLETED"
                                  else "FAILED BUILD DIRECTORY",
                      "state_before": origin_state,
                      "action": "AUTO_RETIRED_AFTER_ARTIFACT_VERIFICATION" if origin_state == "COMPLETED"
                                else "AUTO_RETIRED_AFTER_FAILURE_EVIDENCE",
                      "logical_bytes": stats["logical_bytes"],
                      "allocated_unique_inode_bytes": stats["allocated_unique_inode_bytes"],
                      "best_recoverable_bytes_estimate": stats["best_recoverable_bytes_estimate"],
                      "hardlink_note": "only inodes whose full link count was inside the tree are counted recoverable",
                      "apfs_clone_note": "APFS shared extents are unknown; disk free-space delta is authoritative",
                      "filesystem_free_before_bytes": free_before,
                      "artifact_hashes_preserved": artifact_hashes,
                      "build_manifest_id": lease.get("build_manifest_id"),
                      "build_manifest_sha256": lease.get("build_manifest_sha256"),
                      "failure_evidence_index_sha256": lease.get("failure_evidence_index_sha256"),
                      "result": "PENDING", "created_at_unix": time.time()}
            _write_json(receipt_path, intent, replace=False, sealed=True)
        else:
            if (intent.get("state") not in {"RETIREMENT_INTENT", "REMOVED"}
                    or intent.get("build_id") != build_id or intent.get("path") != str(path)
                    or intent.get("build_device") != lease.get("build_device")
                    or intent.get("build_inode") != lease.get("build_inode")
                    or intent.get("build_manifest_id") != lease.get("build_manifest_id")
                    or intent.get("build_manifest_sha256") != lease.get("build_manifest_sha256")
                    or intent.get("build_filesystem_id") != lease.get("build_filesystem_id")):
                raise RetentionError("existing retirement receipt conflicts with build identity")
            if intent.get("quarantine_path") != str(parent / quarantine):
                raise RetentionError("existing retirement receipt names another quarantine directory")
            if intent.get("state") == "REMOVED":
                raise RetentionError("a removed build unexpectedly reappeared at its leased pathname")
        retiring.update({"cleanup_attempted": True, "cleanup_free_before_bytes": intent["filesystem_free_before_bytes"]})
        _store_lease(retiring)
        try:
            _remove_verified_tree_at(parent_fd, quarantine, expected_device=lease["build_device"],
                                     expected_inode=lease["build_inode"], logical_path=parent / quarantine,
                                     expected_filesystem_id=lease.get("build_filesystem_id"))
        except BaseException as error:
            _store_lease(dict(retiring, cleanup_error=repr(error)))
            raise RetentionError(f"cleanup failed for {build_id}: {error}") from error
        if _exists_at(parent_fd, quarantine):
            raise RetentionError("quarantine remains after descriptor-relative cleanup")
        final = dict(intent, state="REMOVED", result="REMOVED",
                     filesystem_free_after_bytes=shutil.disk_usage(REPO).free,
                     completed_at_unix=time.time())
        final["measured_free_delta_bytes"] = (
            final["filesystem_free_after_bytes"] - final["filesystem_free_before_bytes"])
        _write_json(receipt_path, final, replace=True, sealed=True)
        verified_receipt = _read_json(receipt_path, sealed=True)
        if verified_receipt.get("result") != "REMOVED":
            raise RetentionError("durable cleanup receipt failed readback")
        _store_lease(dict(retiring, state="COMPLETED", retired_at_unix=time.time(),
                          cleanup_receipt_path=str(receipt_path)))
        return verified_receipt
    finally:
        os.close(parent_fd)

def _retire_pending_locked() -> list[dict[str, Any]]:
    receipts = []
    for path in _lease_files():
        lease = _load_lease(path.stem)
        if lease.get("retired_at_unix") is not None:
            continue
        if lease.get("state") == "COMPLETED":
            receipts.append(_retire(lease, reason="cap-preflight-retire-completed"))
        elif lease.get("state") == "FAILED":
            receipts.append(_retire(lease, reason="cap-preflight-retire-failed"))
        elif lease.get("state") == "RETIRING":
            receipts.append(_retire(lease, reason="resume-retirement"))
        elif lease.get("state") == "PRESERVE_DEBUG":
            expires = lease.get("preserve_debug_expires_at_unix")
            if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= time.time():
                path = Path(lease["path"])
                _require_no_live_process(path)
                manifest, digest = _verify_build_manifest(lease["build_id"])
                if (manifest.get("build_manifest_id") != lease.get("build_manifest_id")
                        or digest != lease.get("build_manifest_sha256")):
                    raise RetentionError("expired debug build manifest does not match its lease")
                root_fd = _open_dir(path)
                try:
                    marker = _read_marker(root_fd)
                    if marker.get("build_id") != lease.get("build_id") or marker.get("state") != "PRESERVE_DEBUG":
                        raise RetentionError("expired debug build marker changed")
                    marker.update({"state": "COMPLETED", "debug_preservation_expired_unix": time.time()})
                    _write_marker(root_fd, marker)
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
                lease.update({"state": "COMPLETED", "debug_preservation_expired_unix": time.time()})
                _store_lease(lease)
                receipts.append(_retire(lease, reason="debug-preservation-expired"))
    return receipts

def _legacy_allowed_symlink_roots(path: Path) -> list[Path]:
    """Permit only declared Makefile source-tree links for the one Wine legacy root."""
    if path != Path("/private/tmp/fgmetal-wine-11.17-vkmetal-build-x86"):
        return []
    makefile = path / "Makefile"
    if not makefile.is_file() or makefile.is_symlink():
        raise RetentionError("Wine legacy build lacks its regular Makefile source declaration")
    text = makefile.read_text(errors="strict")
    match = re.search(r"^srcdir\s*=\s*(.+)$", text, re.MULTILINE)
    if not match:
        raise RetentionError("Wine legacy Makefile lacks an exact srcdir declaration")
    source = Path(match.group(1).strip()).resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise RetentionError("Wine legacy source tree is not a real directory")
    return [source]

def _check_legacy_adoption_capacity(path: Path, kind: str) -> None:
    """Count an unleased legacy root before moving it into the managed build root."""
    _retire_pending_locked()
    state = _snapshot()
    target = os.path.abspath(str(path))
    unrelated = [row for row in state["unknown"] if os.path.abspath(str(row.get("path", ""))) != target]
    if unrelated:
        raise RetentionError("legacy adoption refused while another build root is ambiguous")
    if state["active_build_count"] + 1 > MAX_ACTIVE_LARGE_BUILDS:
        raise RetentionError("legacy adoption would exceed the global active large-build cap")
    if kind == "dxvk" and state["active_dxvk_count"] + 1 > MAX_ACTIVE_DXVK:
        raise RetentionError("legacy adoption would exceed the active DXVK build cap")
    if kind == "moltenvk" and state["active_moltenvk_count"] + 1 > MAX_ACTIVE_MOLTENVK:
        raise RetentionError("legacy adoption would exceed the active MoltenVK diagnostic build cap")
    if state["retained_build_count"] + 1 > MAX_RETAINED:
        raise RetentionError("legacy adoption would exceed the global retained build cap")

def adopt_legacy(path: Path, *, kind: str, owner_experiment: str, purpose: str,
                 expected_expiry_unix: float | None = None,
                 preserve_debug_reason: str | None = None) -> dict[str, Any]:
    path = Path(os.path.abspath(path))
    if path not in LEGACY_BUILD_ROOTS:
        raise RetentionError(f"legacy path is not allowlisted: {path}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    lease_fd, lock_fd = _lock()
    try:
        _require_no_live_process(path)
        parent = path.parent
        parent_identity = _root_identity(parent, create=False)
        parent_fd = _open_dir(parent)
        try:
            if _fs_identity(parent_fd) != parent_identity:
                raise RetentionError("legacy source parent identity changed after verification")
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise RetentionError("legacy root is not a real directory")
            root_fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                              | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                opened = os.fstat(root_fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise RetentionError("legacy build identity changed while opening")
                allowed_symlink_roots = _legacy_allowed_symlink_roots(path)
                stats = _tree_stats(root_fd, info.st_dev, path,
                                    allowed_symlink_roots=allowed_symlink_roots)
                if _exists_at(root_fd, BUILD_MARKER):
                    marker = _read_marker(root_fd)
                    prior = _load_lease(marker["build_id"])
                    if (prior.get("path") != str(path) or prior.get("build_id") != marker.get("build_id")
                            or prior.get("build_inode") != opened.st_ino or prior.get("build_device") != opened.st_dev
                            or marker.get("path") != str(path)):
                        raise RetentionError("existing marker/lease does not identify this exact legacy root")
                    return {"status": "ALREADY_ADOPTED", "build_id": prior["build_id"], "path": str(path)}
                _check_legacy_adoption_capacity(path, kind)
                build_id = f"legacy-{path.name}-{uuid.uuid4().hex[:8]}"
                _root_identity(BUILD_ROOT, create=True)
                build_parent_fd = _open_dir(BUILD_ROOT)
                try:
                    build_parent_identity = _fs_identity(build_parent_fd)
                    private_mode = build_parent_identity.get("mode", 0)
                    build_filesystem_id = getattr(os.fstatvfs(root_fd), "f_fsid", None)
                    if (build_parent_identity.get("owner_uid") != os.getuid() or private_mode & 0o077
                            or build_parent_identity["device"] != info.st_dev
                            or build_parent_identity.get("filesystem_id") != build_filesystem_id):
                        raise RetentionError("legacy adoption requires the same filesystem and a private build root")
                    target_path = BUILD_ROOT / build_id
                    if _exists_at(build_parent_fd, build_id):
                        raise FileExistsError(target_path)
                    latest_source = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                    if (latest_source.st_dev, latest_source.st_ino) != (info.st_dev, info.st_ino):
                        raise RetentionError("legacy source root changed immediately before adoption")
                    lease = {"schema_version": 1, "build_id": build_id, "path": str(target_path),
                         "legacy_source_path": str(path), "kind": kind,
                         "owner_experiment": owner_experiment, "purpose": purpose, "state": "ACTIVE",
                         "created_at_unix": time.time(), "last_heartbeat_unix": time.time(),
                         "source_tree_hash": "unknown-at-build-time", "expected_bytes": stats["logical_bytes"],
                         "estimated_bytes": stats["allocated_unique_inode_bytes"], "approved_parent": str(BUILD_ROOT),
                         "approved_parent_identity": build_parent_identity, "build_device": info.st_dev,
                         "build_inode": info.st_ino, "adopted_existing": True, "owner_pid": None,
                         "preserve_debug_expires_at_unix": expected_expiry_unix,
                         "preserve_debug_reason": preserve_debug_reason,
                         "build_filesystem_id": build_filesystem_id,
                         "allowed_external_symlink_roots": [str(item) for item in allowed_symlink_roots]}
                    # The signed ACTIVE registry record precedes the namespace move.
                    _store_lease(lease)
                    try:
                        _rename_exclusive_between(parent_fd, path.name, build_parent_fd, build_id)
                    except BaseException:
                        if not _exists_at(build_parent_fd, build_id):
                            try:
                                current_source = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                current_source = None
                            if current_source is not None and (current_source.st_dev, current_source.st_ino) == (
                                    info.st_dev, info.st_ino):
                                _remove_unmoved_adoption_record(build_id, lease)
                        raise
                    try:
                        moved_fd = os.open(build_id, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=build_parent_fd)
                    except BaseException as error:
                        _rollback_failed_adoption(parent_fd, path.name, build_parent_fd, build_id, lease)
                        raise RetentionError(f"legacy namespace entry changed during adoption and was rolled back: {error}") from error
                    try:
                        moved = os.fstat(moved_fd)
                        if (moved.st_dev, moved.st_ino) != (info.st_dev, info.st_ino):
                            _rollback_failed_adoption(parent_fd, path.name, build_parent_fd, build_id, lease)
                            raise RetentionError("legacy root identity changed during private adoption; namespace was rolled back")
                        if getattr(os.fstatvfs(moved_fd), "f_fsid", None) != lease["build_filesystem_id"]:
                            _rollback_failed_adoption(parent_fd, path.name, build_parent_fd, build_id, lease)
                            raise RetentionError("legacy root filesystem identity changed during private adoption; namespace was rolled back")
                        moved_lease = dict(lease)
                        moved_lease["adopted_at_unix"] = time.time()
                        try:
                            _store_lease(moved_lease)
                            _write_marker(moved_fd, moved_lease)
                            os.fsync(moved_fd)
                        except BaseException as error:
                            try:
                                _remove_adoption_marker(moved_fd, moved_lease)
                                _rollback_failed_adoption(parent_fd, path.name, build_parent_fd,
                                                          build_id, lease)
                            except BaseException as rollback_error:
                                raise RetentionError(
                                    "legacy adoption persistence failed and namespace rollback also failed: "
                                    f"{error}; rollback: {rollback_error}") from rollback_error
                            raise RetentionError(
                                f"legacy adoption persistence failed; namespace and provisional lease were rolled back: {error}") from error
                    finally:
                        os.close(moved_fd)
                    return {"status": "ADOPTED", "build_id": build_id, "path": str(target_path),
                            "legacy_source_path": str(path), "logical_bytes": stats["logical_bytes"],
                            "allocated_bytes": stats["allocated_unique_inode_bytes"]}
                finally:
                    os.close(build_parent_fd)
            finally:
                os.close(root_fd)
        finally:
            os.close(parent_fd)
    finally:
        _unlock(lease_fd, lock_fd)

def complete_adopted(build_id: str, *, preserve_debug: bool = False,
                     legacy_debug_context_paths: Iterable[Path | str] = ()) -> dict[str, Any]:
    lease_fd, lock_fd = _lock()
    try:
        lease = _load_lease(build_id)
        if not lease.get("adopted_existing"):
            raise RetentionError("lease is not an ACTIVE adopted legacy root")
        if lease.get("state") in {"COMPLETED", "RETIRING"}:
            manifest, digest = _verify_build_manifest(build_id)
            if (manifest.get("build_manifest_id") != lease.get("build_manifest_id")
                    or digest != lease.get("build_manifest_sha256")):
                raise RetentionError("adopted completion lease does not bind its manifest")
            return _retire(lease, reason="idempotent-adopted-completion-retry")
        if lease.get("state") == "PRESERVE_DEBUG":
            manifest, digest = _verify_build_manifest(build_id)
            if (manifest.get("build_manifest_id") != lease.get("build_manifest_id")
                    or digest != lease.get("build_manifest_sha256")):
                raise RetentionError("adopted debug lease does not bind its manifest")
            return {"status": "PRESERVE_DEBUG", "build_manifest_id": manifest["build_manifest_id"]}
        if lease.get("state") != "ACTIVE":
            raise RetentionError(f"adopted lease cannot complete from state {lease.get('state')}")
        if preserve_debug:
            expiry = lease.get("preserve_debug_expires_at_unix")
            estimated = lease.get("estimated_bytes", 0)
            if (not lease.get("preserve_debug_reason")
                    or not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                    or expiry <= time.time() or expiry > time.time() + PRESERVE_DEBUG_MAX_SECONDS
                    or not isinstance(estimated, int) or isinstance(estimated, bool) or estimated <= 0):
                raise RetentionError("PRESERVE_DEBUG requires reason, estimated bytes, and expiry within seven days")
            if _snapshot()["preserved_build_count"] >= MAX_PRESERVED_DEBUG:
                raise RetentionError("the one reference/debug preservation slot is occupied")
        path = Path(lease["path"])
        _require_no_live_process(path)
        evidence = MANIFEST_ROOT / f"{build_id}.evidence"
        evidence_fd = _open_dir(evidence, create=True)
        os.close(evidence_fd)
        outputs, identity = _copy_tree_artifacts(path, build_id, evidence)
        if not outputs:
            raise RetentionError("build has no artifact outputs")
        dependency_state = lease.get("metadata", {}).get("dependency_state", identity.get("dependency_state"))
        legacy_debug = bool(preserve_debug and lease.get("adopted_existing") and lease.get("kind") == "reference"
                            and (identity.get("build_system") not in {"meson", "xcode", "make"}
                                 or not isinstance(identity.get("source_identity"), Mapping)))
        if legacy_debug:
            context_paths = [Path(item).expanduser().absolute() for item in legacy_debug_context_paths]
            if not context_paths:
                raise RetentionError("legacy PRESERVE_DEBUG artifactization requires retained run identity evidence")
            context_rows = []
            context_artifact_hashes: dict[str, str] = {}
            for index, source in enumerate(context_paths):
                resolved = source.resolve(strict=True)
                if (source != resolved or source.is_symlink() or not source.is_file()
                        or REPO.resolve() not in resolved.parents):
                    raise RetentionError(f"legacy debug context must be a regular repository evidence file: {source}")
                record = _copy_evidence(source, evidence / "provenance" / f"{index:02d}-{source.name}")
                context_rows.append(record)
                try:
                    context = json.loads(source.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    context = {}
                hashes = context.get("dll_sha256") if isinstance(context, dict) else None
                if isinstance(hashes, dict):
                    context_artifact_hashes.update({key: value for key, value in hashes.items()
                                                    if isinstance(key, str) and isinstance(value, str)})
            artifact_hashes = {row.get("artifact_name"): row.get("sha256") for row in outputs}
            if not artifact_hashes or any(context_artifact_hashes.get(name) != digest
                                          for name, digest in artifact_hashes.items()):
                raise RetentionError("legacy debug run evidence does not identify every retained binary output")
            symbol_rows = []
            root_fd = _open_dir(path)
            try:
                for name in sorted(os.listdir(root_fd)):
                    if not name.endswith((".o", ".obj")):
                        continue
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_dev != os.fstat(root_fd).st_dev:
                        raise RetentionError(f"legacy debug symbol is not a regular build-root file: {name}")
                    symbol_rows.append(_copy_evidence(path / name, evidence / "debug-symbols" / name))
            finally:
                os.close(root_fd)
            if not symbol_rows:
                raise RetentionError("legacy PRESERVE_DEBUG root has no captured object/symbol evidence")
            identity = {"build_system": "legacy-debug-preservation", "legacy_debug_preservation": True,
                        "legacy_debug_context_evidence": context_rows,
                        "legacy_debug_symbols": symbol_rows,
                        "legacy_debug_artifact_hashes": artifact_hashes,
                        "source_identity": {"status": "historical build-time source tree not captured in this build root"},
                        "patch_set": {"status": "historical patch set is identified by retained run evidence only"},
                        "dependency_state": {"status": "historical dependency state is not asserted"},
                        "compiler_identity": {"status": "historical compiler command provenance is not asserted"},
                        "linker_identity": {"status": "historical linker command provenance is not asserted"},
                        "build_type": "diagnostic; not a rebuild-reproducibility claim",
                        "build_log_status": "build transcript absent; retained runtime manifests bind the output hashes",
                        "generated_shader_identity": [], "config_key_sha256": None}
        else:
            if dependency_state is None:
                raise RetentionError("adopted build lacks dependency-state evidence; refusing completion")
            identity["dependency_state"] = dependency_state
            identity["config_key_sha256"] = configuration_key_from_identity(
                identity, dependency_state=dependency_state)
        manifest, digest = _build_manifest(lease, outputs, identity, evidence,
                                           provenance_scope="artifactization-time observation; historical build-time input not asserted")
        state = "PRESERVE_DEBUG" if preserve_debug else "COMPLETED"
        lease.update({"state": state, "build_manifest_id": manifest["build_manifest_id"],
                      "build_manifest_path": str(MANIFEST_ROOT / f"{build_id}.json"),
                      "build_manifest_sha256": digest, "completed_at_unix": time.time()})
        _store_lease(lease)
        root_fd = _open_dir(path)
        try:
            marker = _read_marker(root_fd)
            marker.update({"state": state, "build_manifest_id": manifest["build_manifest_id"],
                           "build_manifest_sha256": digest})
            _write_marker(root_fd, marker)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        if preserve_debug:
            return {"status": state, "build_manifest_id": manifest["build_manifest_id"],
                    "build_manifest_path": str(MANIFEST_ROOT / f"{build_id}.json")}
        return _retire(lease, reason="legacy-build-artifactized-and-reconciled")
    finally:
        _unlock(lease_fd, lock_fd)

def close_debug_preservation(build_id: str, *, reason: str) -> dict[str, Any]:
    """Artifactize has completed; release an early-closed debug lease and retire it."""
    if not reason.strip():
        raise ValueError("debug preservation closure requires a reason")
    lease_fd, lock_fd = _lock()
    try:
        lease = _load_lease(build_id)
        if lease.get("state") not in {"PRESERVE_DEBUG", "COMPLETED", "RETIRING"}:
            raise RetentionError("debug preservation can close only from PRESERVE_DEBUG or a recoverable retirement state")
        if not lease.get("preserve_debug_reason"):
            raise RetentionError("debug preservation lease has no recorded preservation reason")
        manifest, digest = _verify_build_manifest(build_id)
        if (manifest.get("build_manifest_id") != lease.get("build_manifest_id")
                or digest != lease.get("build_manifest_sha256")):
            raise RetentionError("debug preservation lease does not bind its artifact manifest")
        if lease.get("state") in {"COMPLETED", "RETIRING"}:
            if lease.get("debug_preservation_closed_reason") != reason:
                raise RetentionError("debug preservation closure retry does not match its saved reason")
            return _retire(lease, reason="debug-preservation-closed")
        path = Path(lease["path"])
        _require_no_live_process(path)
        root_fd = _open_dir(path)
        try:
            marker = _read_marker(root_fd)
            if (marker.get("build_id") != build_id
                    or marker.get("state") not in {"PRESERVE_DEBUG", "COMPLETED"}
                    or marker.get("build_manifest_id") != manifest.get("build_manifest_id")
                    or marker.get("build_manifest_sha256") != digest):
                raise RetentionError("debug root marker does not match its artifact manifest")
            if marker.get("state") == "PRESERVE_DEBUG":
                marker.update({"state": "COMPLETED", "debug_preservation_closed_at_unix": time.time(),
                               "debug_preservation_closed_reason": reason})
                _write_marker(root_fd, marker)
                os.fsync(root_fd)
        finally:
            os.close(root_fd)
        lease.update({"state": "COMPLETED", "debug_preservation_closed_at_unix": time.time(),
                      "debug_preservation_closed_reason": reason})
        _store_lease(lease)
        return _retire(lease, reason="debug-preservation-closed")
    finally:
        _unlock(lease_fd, lock_fd)

def fail_adopted(build_id: str, *, reason: str,
                 capture_paths: Iterable[str] | None = None) -> dict[str, Any]:
    """Capture legacy failure diagnostics, then retire the bulk build tree.

    ``capture_paths`` is an optional exact POSIX-relative allowlist for roots where
    only configuration/error inputs are needed. Omitted means retain all regular
    files and symlink descriptions, which is appropriate only for small roots.
    Evidence writes are idempotent so a retry after interruption can finish safely.
    """
    if not reason.strip() or len(reason.encode("utf-8", "surrogateescape")) > MAX_FAILURE_REASON_BYTES:
        raise ValueError(f"legacy failure/scratch classification requires a reason of at most {MAX_FAILURE_REASON_BYTES} bytes")
    selected = None if capture_paths is None else set(_validated_failure_capture_paths(capture_paths))
    lease_fd, lock_fd = _lock()
    try:
        lease = _load_lease(build_id)
        if not lease.get("adopted_existing") or lease.get("state") not in {"ACTIVE", "FAILED"}:
            raise RetentionError("legacy scratch root must be an ACTIVE or recoverably FAILED adopted lease")
        path = Path(lease["path"])
        _require_no_live_process(path)
        evidence = MANIFEST_ROOT / f"{build_id}.failure"
        failure_path = evidence / "failure-manifest.json"
        if lease.get("state") == "ACTIVE":
            evidence_fd = _open_dir(evidence / "configuration", create=True)
            os.close(evidence_fd)
            copied: list[dict[str, Any]] = []
            captured: set[str] = set()
            root_fd = _open_dir(path)
            try:
                root_info = os.fstat(root_fd)
                root_device = root_info.st_dev
                root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
                marker = _read_marker(root_fd)
                if ((root_device, root_info.st_ino) != (lease.get("build_device"), lease.get("build_inode"))
                        or root_fsid != lease.get("build_filesystem_id")
                        or marker.get("build_id") != build_id or marker.get("path") != str(path)
                        or marker.get("state") != "ACTIVE"):
                    raise RetentionError("adopted failure root identity or marker differs from its lease")
                if selected is None:
                    estimate = _tree_stats(root_fd, root_device, path,
                                           max_scan_entries=MAX_FAILURE_CAPTURE_SCAN_ENTRIES,
                                           max_logical_bytes=MAX_COPY_ALL_FAILURE_ROOT_BYTES)
                    if estimate["logical_bytes"] > MAX_COPY_ALL_FAILURE_ROOT_BYTES:
                        raise RetentionError(
                            "copy-all legacy failure evidence exceeds its explicit small-root size limit; "
                            "provide exact capture_paths")
                captured_bytes = 0
                captured_entries = 0
                if selected is not None:
                    for relative_name in sorted(selected):
                        relative = Path(relative_name)
                        directory_fds = [os.dup(root_fd)]
                        try:
                            parent_fd = directory_fds[-1]
                            for component in relative.parts[:-1]:
                                child_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                                   | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                                child_info = os.fstat(child_fd)
                                if (child_info.st_dev != root_device
                                        or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                                    os.close(child_fd)
                                    raise RetentionError(f"selected failure path crosses filesystem: {relative_name}")
                                directory_fds.append(child_fd)
                                parent_fd = child_fd
                            name = relative.parts[-1]
                            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                            captured_entries += 1
                            if captured_entries > MAX_FAILURE_CAPTURE_FILES:
                                raise RetentionError("failure diagnostics exceed the bounded evidence entry count")
                            target = evidence / "configuration" / "files" / relative
                            if stat.S_ISREG(info.st_mode):
                                source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                                    | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
                                try:
                                    source_info = os.fstat(source_fd)
                                    if ((source_info.st_dev, source_info.st_ino) != (info.st_dev, info.st_ino)
                                            or source_info.st_dev != root_device
                                            or getattr(os.fstatvfs(source_fd), "f_fsid", None) != root_fsid):
                                        raise RetentionError(f"selected diagnostic identity changed: {relative_name}")
                                    limit = min(MAX_FAILURE_CAPTURE_FILE_BYTES,
                                                MAX_FAILURE_CAPTURE_TOTAL_BYTES - captured_bytes)
                                    if source_info.st_size > limit:
                                        raise RetentionError("failure diagnostic exceeds per-file/total evidence size limit")
                                    copied_row = _copy_evidence_from_fd(source_fd, path / relative, target,
                                                                        max_bytes=limit)
                                finally:
                                    os.close(source_fd)
                                copied.append(copied_row)
                                captured_bytes += copied_row["size_bytes"]
                            elif stat.S_ISLNK(info.st_mode):
                                first_target = os.readlink(name, dir_fd=parent_fd)
                                after_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                                second_target = os.readlink(name, dir_fd=parent_fd)
                                if ((after_info.st_dev, after_info.st_ino) != (info.st_dev, info.st_ino)
                                        or first_target != second_target):
                                    raise RetentionError(f"selected symlink changed during diagnostic capture: {relative_name}")
                                target = evidence / "configuration" / "symlinks" / relative.parent / f"{relative.name}.json"
                                encoded_target = _canonical_json({"path": relative_name, "target": first_target}) + b"\n"
                                if (len(encoded_target) > MAX_FAILURE_CAPTURE_FILE_BYTES
                                        or len(encoded_target) > MAX_FAILURE_CAPTURE_TOTAL_BYTES - captured_bytes):
                                    raise RetentionError("symlink diagnostic exceeds the failure evidence size limit")
                                copied.append(_write_evidence_bytes(target, encoded_target))
                                captured_bytes += len(encoded_target)
                            else:
                                raise RetentionError(f"selected diagnostic is not a regular file or symlink: {relative_name}")
                            captured.add(relative_name)
                        finally:
                            for directory_fd in reversed(directory_fds):
                                os.close(directory_fd)
                else:
                    stack: list[tuple[int, Path]] = [(root_fd, Path())]
                    scanned_entries = 0
                    while stack:
                        parent_fd, relative_dir = stack.pop()
                        try:
                            for name in sorted(os.listdir(parent_fd)):
                                if relative_dir == Path() and name == BUILD_MARKER:
                                    continue
                                scanned_entries += 1
                                if scanned_entries > MAX_FAILURE_CAPTURE_SCAN_ENTRIES:
                                    raise RetentionError("copy-all legacy failure scan exceeds its bounded entry count")
                                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                                relative = relative_dir / name
                                relative_name = relative.as_posix()
                                if info.st_dev != root_device:
                                    raise RetentionError(f"legacy scratch evidence crosses filesystem: {relative}")
                                if stat.S_ISDIR(info.st_mode):
                                    child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                                    opened = os.fstat(child_fd)
                                    if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                            or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                                        os.close(child_fd)
                                        raise RetentionError(f"legacy scratch directory or filesystem changed while opening: {relative}")
                                    stack.append((child_fd, relative))
                                elif stat.S_ISREG(info.st_mode):
                                    captured_entries += 1
                                    if captured_entries > MAX_FAILURE_CAPTURE_FILES:
                                        raise RetentionError("copy-all failure diagnostics exceed the bounded evidence entry count")
                                    source_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                                        | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
                                    try:
                                        source_info = os.fstat(source_fd)
                                        if ((source_info.st_dev, source_info.st_ino) != (info.st_dev, info.st_ino)
                                                or source_info.st_dev != root_device
                                                or getattr(os.fstatvfs(source_fd), "f_fsid", None) != root_fsid):
                                            raise RetentionError(f"legacy diagnostic identity changed: {relative}")
                                        limit = min(MAX_FAILURE_CAPTURE_FILE_BYTES,
                                                    MAX_FAILURE_CAPTURE_TOTAL_BYTES - captured_bytes)
                                        if source_info.st_size > limit:
                                            raise RetentionError("failure diagnostic exceeds per-file/total evidence size limit")
                                        copied_row = _copy_evidence_from_fd(source_fd, path / relative,
                                                                            evidence / "configuration" / "files" / relative,
                                                                            max_bytes=limit)
                                    finally:
                                        os.close(source_fd)
                                    copied.append(copied_row)
                                    captured_bytes += copied_row["size_bytes"]
                                    captured.add(relative_name)
                                elif stat.S_ISLNK(info.st_mode):
                                    captured_entries += 1
                                    if captured_entries > MAX_FAILURE_CAPTURE_FILES:
                                        raise RetentionError("copy-all failure diagnostics exceed the bounded evidence entry count")
                                    first_target = os.readlink(name, dir_fd=parent_fd)
                                    after_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                                    second_target = os.readlink(name, dir_fd=parent_fd)
                                    if ((after_info.st_dev, after_info.st_ino) != (info.st_dev, info.st_ino)
                                            or first_target != second_target):
                                        raise RetentionError(f"legacy symlink changed during diagnostic capture: {relative}")
                                    encoded_target = _canonical_json({"path": relative_name, "target": first_target}) + b"\n"
                                    if (len(encoded_target) > MAX_FAILURE_CAPTURE_FILE_BYTES
                                            or len(encoded_target) > MAX_FAILURE_CAPTURE_TOTAL_BYTES - captured_bytes):
                                        raise RetentionError("symlink diagnostic exceeds the failure evidence size limit")
                                    target = evidence / "configuration" / "symlinks" / relative.parent / f"{relative.name}.json"
                                    copied.append(_write_evidence_bytes(target, encoded_target))
                                    captured_bytes += len(encoded_target)
                                    captured.add(relative_name)
                                else:
                                    raise RetentionError(f"unsupported legacy scratch object: {relative}")
                        finally:
                            if parent_fd != root_fd:
                                os.close(parent_fd)
            finally:
                os.close(root_fd)
            if selected is not None and captured != selected:
                missing = sorted(selected - captured)
                raise RetentionError(f"selected diagnostics were not found as regular files or symlinks: {missing}")
            command_record = {"schema_version": 1, "build_id": build_id,
                              "classification": "FAILED_OR_SCRATCH", "reason": reason,
                              "captured_at_unix": time.time(), "retained_files": copied,
                              "capture_policy": "full-small-root" if selected is None else "exact-relative-path-allowlist",
                              "capture_paths": None if selected is None else sorted(selected),
                              "source_logical_bytes": lease.get("expected_bytes"),
                              "source_estimated_allocated_bytes": lease.get("estimated_bytes")}
            _check_failure_manifest_size(command_record)
            failure_parent_fd = _open_dir(failure_path.parent, create=True)
            try:
                failure_already_exists = _exists_at(failure_parent_fd, failure_path.name)
            finally:
                os.close(failure_parent_fd)
            if failure_already_exists:
                previous = _read_json(failure_path)
                stable_fields = ("build_id", "classification", "reason", "retained_files", "capture_policy",
                                 "capture_paths", "source_logical_bytes", "source_estimated_allocated_bytes")
                if any(previous.get(key) != command_record.get(key) for key in stable_fields):
                    raise RetentionError("existing failure evidence conflicts with the requested diagnostic capture")
                command_record = previous
            else:
                _write_json(failure_path, command_record, replace=False)
            lease.update({"state": "FAILED", "failure_evidence_root": str(evidence),
                          "failure_evidence_path": str(failure_path),
                          "failure_evidence_sha256": _hash_path(failure_path),
                          "failed_command": ["legacy-root-reconciliation"], "return_code": None,
                          "failure_reason": reason, "failed_at_unix": time.time()})
        else:
            if lease.get("failure_evidence_path") != str(failure_path):
                raise RetentionError("recoverable FAILED lease does not name its canonical evidence manifest")
            command_record = _read_json(failure_path)
            if (command_record.get("build_id") != build_id
                    or command_record.get("classification") != "FAILED_OR_SCRATCH"
                    or command_record.get("reason") != reason
                    or _hash_path(failure_path) != lease.get("failure_evidence_sha256")):
                raise RetentionError("recoverable FAILED evidence manifest does not match its lease")
        evidence_files = _inventory_evidence(evidence)
        index_digest = hashlib.sha256(_canonical_json(evidence_files)).hexdigest()
        if lease.get("state") == "FAILED" and lease.get("failure_evidence_index_sha256") is not None:
            if lease.get("failure_evidence_index_sha256") != index_digest:
                raise RetentionError("recoverable FAILED evidence inventory changed")
        lease.update({"state": "FAILED", "failure_evidence_root": str(evidence),
                      "failure_evidence_files": evidence_files,
                      "failure_evidence_index_sha256": index_digest,
                      "failure_evidence_path": str(failure_path),
                      "failure_evidence_sha256": _hash_path(failure_path),
                      "failure_reason": reason})
        _store_lease(lease)
        root_fd = _open_dir(path)
        try:
            marker = _read_marker(root_fd)
            if marker.get("build_id") != build_id or marker.get("state") not in {"ACTIVE", "FAILED"}:
                raise RetentionError("legacy root marker changed before failure transition")
            if marker.get("state") == "ACTIVE":
                marker.update({"state": "FAILED", "failure_evidence_sha256": lease["failure_evidence_sha256"]})
                _write_marker(root_fd, marker)
                os.fsync(root_fd)
            elif marker.get("failure_evidence_sha256") != lease["failure_evidence_sha256"]:
                raise RetentionError("FAILED root marker does not bind its retained evidence")
        finally:
            os.close(root_fd)
        return _retire(lease, reason="legacy-scratch-evidence-captured")
    finally:
        _unlock(lease_fd, lock_fd)

def _validate_build_command_paths(command: list[str], cwd: Path, build_root: Path) -> None:
    """Accept only supported build tools and keep declared build paths in the lease."""
    if not command:
        raise RetentionError("build command is empty")
    root = build_root.resolve(strict=True)
    working = cwd.resolve(strict=True)

    def require_inside(value: str, label: str) -> None:
        selected = Path(value).expanduser()
        candidate = selected if selected.is_absolute() else working / selected
        resolved = candidate.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise RetentionError(f"{label} must remain inside the leased build root: {value}")

    executable = Path(command[0]).name.lower()
    supported = {"meson", "cmake", "cmake3", "ninja", "ninja-build", "make", "gmake", "xcodebuild"}
    if executable not in supported:
        raise RetentionError(f"unsupported build executable; arbitrary wrappers are not permitted: {executable}")

    if executable == "meson":
        if len(command) < 2 or command[1] == "install":
            raise RetentionError("Meson install mode is outside the leased build-output contract")
        try:
            setup_index = command.index("setup", 1)
        except ValueError:
            setup_index = -1
        if setup_index >= 0:
            args = command[setup_index + 1:]
            value_options = {"--cross-file", "--native-file", "--prefix", "--libdir", "--bindir",
                             "--includedir", "--datadir", "--mandir", "--infodir", "--localedir",
                             "--sysconfdir", "--localstatedir", "--sharedstatedir", "--licensedir",
                             "--backend", "--buildtype", "--wrap-mode", "--pkg-config-path",
                             "--cmake-prefix-path"}
            flag_options = {"--wipe", "--reconfigure", "--clearcache", "--vsenv", "--genvslite"}
            positionals: list[str] = []
            index = 0
            while index < len(args):
                token = args[index]
                if token == "--":
                    positionals.extend(args[index + 1:])
                    break
                if token in flag_options or token.startswith("-D") or "=" in token and token.startswith("--"):
                    index += 1
                    continue
                if token in value_options:
                    if index + 1 >= len(args):
                        raise RetentionError(f"Meson setup option lacks a value: {token}")
                    index += 2
                    continue
                if token.startswith("-"):
                    raise RetentionError(f"unrecognized Meson setup option cannot be checked safely: {token}")
                positionals.append(token)
                index += 1
            if not positionals:
                raise RetentionError("Meson setup command has no verifiable build directory")
            require_inside(positionals[0], "Meson build directory")
        if command[1] in {"compile", "test", "configure", "dist"}:
            _validate_directory_options(command[2:], require_inside, "Meson build directory",
                                        options={"-C", "--directory"})

    if executable in {"cmake", "cmake3"}:
        if any(token in {"--install", "-E"} for token in command[1:]):
            raise RetentionError("CMake install/file-operation modes are outside the leased build-output contract")
        index = 1
        while index < len(command):
            token = command[index]
            if token in {"-B", "--build"}:
                if index + 1 >= len(command):
                    raise RetentionError(f"CMake output option lacks a directory: {token}")
                require_inside(command[index + 1], "CMake build directory")
                index += 2
                continue
            if token.startswith("-B") and token != "-B":
                require_inside(token[2:], "CMake build directory")
            elif token.startswith("--build="):
                require_inside(token.split("=", 1)[1], "CMake build directory")
            index += 1

    if executable in {"ninja", "ninja-build", "make", "gmake"}:
        if any(token in {"install", "install/strip"} for token in command[1:]):
            raise RetentionError("build-tool install targets are outside the leased build-output contract")
        index = 1
        while index < len(command):
            token = command[index]
            if token in {"-C", "--directory"}:
                if index + 1 >= len(command):
                    raise RetentionError(f"build tool directory option lacks a path: {token}")
                require_inside(command[index + 1], "build tool directory")
                index += 2
                continue
            if token.startswith("-C") and token != "-C":
                require_inside(token[2:], "build tool directory")
            if token in {"-f", "--file"}:
                if index + 1 >= len(command):
                    raise RetentionError(f"build-tool makefile option lacks a path: {token}")
                require_inside(command[index + 1], "build tool makefile")
                index += 2
                continue
            if token.startswith("--file="):
                require_inside(token.split("=", 1)[1], "build tool makefile")
            index += 1

    if executable == "xcodebuild":
        index = 1
        while index < len(command):
            token = command[index]
            if token in {"-derivedDataPath", "-archivePath"}:
                if index + 1 >= len(command):
                    raise RetentionError(f"Xcode output option lacks a path: {token}")
                require_inside(command[index + 1], "Xcode output directory")
                index += 2
                continue
            if token.startswith("-derivedDataPath=") or token.startswith("-archivePath="):
                require_inside(token.split("=", 1)[1], "Xcode output directory")
            index += 1
        if not any(token in {"-derivedDataPath", "-derivedDataPath="} or token.startswith("-derivedDataPath=")
                   for token in command[1:]):
            raise RetentionError("xcodebuild requires an explicit derived-data path inside the leased root")


def _validate_directory_options(args: list[str], require_inside: Any, label: str,
                                *, options: set[str]) -> None:
    index = 0
    while index < len(args):
        token = args[index]
        if token in options:
            if index + 1 >= len(args):
                raise RetentionError(f"build-tool directory option lacks a path: {token}")
            require_inside(args[index + 1], label)
            index += 2
            continue
        matched = next((option for option in options if token.startswith(option) and token != option), None)
        if matched:
            require_inside(token[len(matched):], label)
        index += 1


def _capture_native_xcode_toolchain(environment: Mapping[str, str]) -> dict[str, str]:
    developer_dir_value = environment.get("DEVELOPER_DIR")
    if not developer_dir_value:
        raise RetentionError("native MoltenVK build environment must declare DEVELOPER_DIR")
    developer_dir = Path(developer_dir_value).expanduser().resolve(strict=True)
    xcodebuild = developer_dir / "usr/bin/xcodebuild"
    linker = developer_dir / "Toolchains/XcodeDefault.xctoolchain/usr/bin/ld"
    xcrun = Path("/usr/bin/xcrun")
    if not xcodebuild.is_file() or not linker.is_file() or not xcrun.is_file():
        raise RetentionError("declared Xcode toolchain is missing xcodebuild, ld, or /usr/bin/xcrun")

    def capture(command: list[str], label: str) -> str:
        try:
            result = subprocess.run(command, env=dict(environment), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RetentionError(f"could not capture native toolchain {label}: {error}") from error
        output = result.stdout.strip()
        if result.returncode != 0 or not output:
            raise RetentionError(f"native toolchain {label} probe failed (exit {result.returncode}): {output[:1000]}")
        return output

    xcode_version = capture([str(xcodebuild), "-version"], "Xcode version")
    clang_path_text = capture([str(xcrun), "--find", "clang"], "clang path")
    clang_path = Path(clang_path_text).resolve(strict=True)
    clang_version = capture([str(clang_path), "--version"], "clang version")
    sdk_version = capture([str(xcrun), "--sdk", "macosx", "--show-sdk-version"], "macOS SDK version")
    capture([str(xcrun), "--sdk", "macosx", "--show-sdk-path"], "macOS SDK path")
    linker_version = capture([str(linker), "-v"], "linker version")
    clang_hash = _hash_path(clang_path)
    return {
        "xcode_version_string": xcode_version,
        "clang_version_string": clang_version,
        "clang_binary_sha256": clang_hash,
        "sdk_version_string": sdk_version,
        "linker_version_string": linker_version,
        "toolchain_identity_sha256": native_toolchain_identity_sha256(
            xcode_version_string=xcode_version,
            clang_version_string=clang_version,
            clang_binary_sha256=clang_hash,
            sdk_version_string=sdk_version,
            linker_version_string=linker_version,
        ),
    }


def _validate_native_build_execution_binding(
        request: NativeMoltenVKBuildRequest, command: list[str],
        environment: Mapping[str, str] | None, cwd: Path, build_root: Path,
        *, observed_toolchain: Mapping[str, str] | None = None) -> dict[str, str]:
    """Require the executed native build to match the signed pre-build request."""
    if tuple(command) != request.normalized_arguments:
        raise RetentionError("native MoltenVK command differs from the signed normalized_arguments")
    if environment is None or dict(environment) != dict(request.normalized_environment):
        raise RetentionError("native MoltenVK environment differs from the signed normalized_environment")
    root = build_root.resolve(strict=True)
    working = cwd.resolve(strict=True)
    if working != root:
        raise RetentionError("native MoltenVK build cwd must be the leased build root")
    developer_dir = Path(environment["DEVELOPER_DIR"]).expanduser().resolve(strict=True)
    expected_xcodebuild = (developer_dir / "usr/bin/xcodebuild").resolve(strict=True)
    if not command or Path(command[0]).resolve(strict=True) != expected_xcodebuild:
        raise RetentionError("native MoltenVK command must invoke xcodebuild from the declared DEVELOPER_DIR")

    observed = dict(observed_toolchain or _capture_native_xcode_toolchain(environment))
    expected = {
        "xcode_version_string": request.xcode_version_string,
        "clang_version_string": request.clang_version_string,
        "clang_binary_sha256": request.clang_binary_sha256,
        "sdk_version_string": request.sdk_version_string,
        "linker_version_string": request.linker_version_string,
        "toolchain_identity_sha256": request.toolchain_identity_sha256,
    }
    if observed != expected:
        mismatches = sorted(key for key in expected if observed.get(key) != expected[key])
        raise RetentionError("native MoltenVK toolchain differs from the signed request: " + ", ".join(mismatches))
    return observed


def _sandbox_profile_text(build_root: Path) -> str:
    root = build_root.resolve(strict=True)

    def sbpl_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return ("(version 1)\n"
            "(deny default)\n"
            "(allow process*)\n"
            "(allow file-read*)\n"
            f"(allow file-write* (subpath {sbpl_quote(str(root))}))\n")


def _sandbox_build_command(command: list[str], build_root: Path,
                           env: Mapping[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Run build tools under macOS Seatbelt with writes confined to one lease."""
    if sys.platform != "darwin":
        raise RetentionError("BuildLease execution requires macOS Seatbelt write confinement")
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if (sandbox_exec.is_symlink() or not sandbox_exec.is_file()
            or sandbox_exec.resolve(strict=True) != sandbox_exec):
        raise RetentionError("the verified system sandbox-exec launcher is unavailable")
    root = build_root.resolve(strict=True)
    tmp_path = root / ".tmp"
    tmp_fd = _open_dir(tmp_path, create=True)
    try:
        tmp_info = os.fstat(tmp_fd)
        root_info = os.stat(root, follow_symlinks=False)
        if (not stat.S_ISDIR(tmp_info.st_mode) or tmp_info.st_dev != root_info.st_dev
                or tmp_path.is_symlink()):
            raise RetentionError("build temporary directory is not a real child on the leased filesystem")
    finally:
        os.close(tmp_fd)

    profile = _sandbox_profile_text(root)
    selected_env = dict(os.environ if env is None else env)
    selected_env.update({"TMPDIR": str(tmp_path), "TMP": str(tmp_path), "TEMP": str(tmp_path)})
    return [str(sandbox_exec), "-p", profile, *command], selected_env


class BuildLease:
    """Reserve before mkdir; successful completion artifactizes and retires synchronously."""
    def __init__(self, build_id: str, *, kind: str, owner_experiment: str, purpose: str,
                 source_tree_hash: str, expected_bytes: int, metadata: Mapping[str, Any] | None = None,
                 preserve_debug_reason: str | None = None, runner_path: Path | str,
                 configuration_key_sha256: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", build_id):
            raise ValueError("build_id is not path-safe")
        if kind not in KINDS or not owner_experiment.strip() or not purpose.strip() or expected_bytes < 0:
            raise ValueError("kind, owner, purpose, and non-negative expected_bytes are required")
        if kind not in RESERVABLE_KINDS:
            raise ValueError("new large build leases require a capped DXVK, MoltenVK, or reference/debug kind")
        if not re.fullmatch(r"[0-9a-f]{64}", source_tree_hash):
            raise ValueError("BuildLease requires a complete frozen source-tree SHA-256")
        if preserve_debug_reason is not None and not preserve_debug_reason.strip():
            raise ValueError("PRESERVE_DEBUG requires a non-empty reason")
        if kind == "reference" and not preserve_debug_reason:
            raise ValueError("reference builds require an explicit preservation reason")
        if preserve_debug_reason is not None:
            if expected_bytes <= 0:
                raise ValueError("debug authorization requires a positive estimated build size")
        self.build_id, self.kind, self.owner_experiment = build_id, kind, owner_experiment
        self.purpose, self.source_tree_hash, self.expected_bytes = purpose, source_tree_hash, expected_bytes
        self.metadata = dict(metadata or {})
        build_type = self.metadata.get("build_type")
        if (not isinstance(build_type, str) or build_type != build_type.strip()
                or build_type != build_type.casefold()
                or build_type.casefold() not in SUPPORTED_BUILD_TYPES):
            raise ValueError("BuildLease requires a supported, signed build_type")
        normalized_build_type = build_type.casefold()
        self.full_debug_build = normalized_build_type in FULL_DEBUG_BUILD_TYPES
        estimated_debug_bytes = self.metadata.get("estimated_debug_bytes")
        if self.full_debug_build:
            if (not preserve_debug_reason or not preserve_debug_reason.strip()
                    or not isinstance(estimated_debug_bytes, int) or isinstance(estimated_debug_bytes, bool)
                    or estimated_debug_bytes <= 0 or estimated_debug_bytes > expected_bytes):
                raise ValueError("full debug/unstripped builds require a reason and bounded estimated_debug_bytes")
        elif estimated_debug_bytes is not None:
            raise ValueError("estimated_debug_bytes is valid only for full debug/unstripped builds")
        self.failure_capture_paths = _validated_failure_capture_paths(
            self.metadata.get("failure_capture_paths"))
        if self.metadata.get("failure_capture_paths", []) != self.failure_capture_paths:
            raise ValueError("failure_capture_paths must use the canonical normalized list")
        if not isinstance(configuration_key_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", configuration_key_sha256):
            raise ValueError("a frozen configuration_key_sha256 is required before a large build can be reserved")
        if self.metadata.get("dependency_state") is None:
            raise ValueError("BuildLease requires a frozen dependency_state before reservation")
        selected_runner = Path(runner_path).expanduser().absolute()
        resolved_runner = selected_runner.resolve(strict=True)
        if (selected_runner.is_symlink() or selected_runner != resolved_runner or not resolved_runner.is_file()
                or REPO.resolve() not in resolved_runner.parents):
            raise ValueError("BuildLease runner must be a real, canonical file inside the repository")
        self.configuration_key_sha256 = configuration_key_sha256
        self.runner_path = str(resolved_runner)
        self.preserve_debug_reason = preserve_debug_reason
        self.path = BUILD_ROOT / build_id
        self.started = False
        self.reused_manifest = None
        self.lock_fds = None
        self.stop_event = None
        self.beat_thread = None
        self.storage_allocation_lock_file = None

    def reserve(self) -> Path | None:
        from storage_policy import (_acquire_storage_allocation_lock, _experiment_manifest_path,
                                    _read_file_nofollow, _validated_manifest_estimates,
                                    _verify_manifest_signature, GATE0_ATTESTATION_PATH,
                                    FREE_WARNING, FREE_MINIMUM, PROJECT_BUDGET,
                                    disk_free_bytes, measure_project_usage)
        self.storage_allocation_lock_file = _acquire_storage_allocation_lock()
        try:
            manifest_path = _experiment_manifest_path(self.owner_experiment)
            manifest_raw = _read_file_nofollow(Path(manifest_path))
            manifest = json.loads(manifest_raw)
            if not _verify_manifest_signature(manifest):
                raise RetentionError("BuildLease owner manifest is unsigned or has been edited")
            runner_hashes = manifest.get("authorized_runner_sha256")
            try:
                runner_hash = hashlib.sha256(_read_file_nofollow(Path(self.runner_path))).hexdigest()
            except OSError as error:
                raise RetentionError(f"BuildLease runner source cannot be safely read: {error}") from error
            try:
                estimates = _validated_manifest_estimates(manifest)
            except ValueError as error:
                raise RetentionError(f"BuildLease owner manifest has invalid estimates: {error}") from error
            if (manifest.get("experiment_id") != self.owner_experiment or manifest.get("decision") != "ALLOW"
                    or manifest.get("storage_policy_smoke") is True
                    or estimates["expected_build_bytes"] <= 0
                    or estimates["expected_build_bytes"] != self.expected_bytes
                    or manifest.get("build_source_tree_hash") != self.source_tree_hash
                    or manifest.get("build_configuration_key_sha256") != self.configuration_key_sha256
                    or manifest.get("build_kind") != self.kind
                    or manifest.get("build_type") != self.metadata.get("build_type")
                    or manifest.get("preserve_debug_reason") != self.preserve_debug_reason
                    or manifest.get("estimated_debug_bytes") != self.metadata.get("estimated_debug_bytes")
                    or manifest.get("failure_capture_paths", []) != self.failure_capture_paths
                    or not isinstance(runner_hashes, Mapping)
                    or runner_hashes.get(self.runner_path) != runner_hash
                    or manifest.get("dependency_state") != self.metadata.get("dependency_state")):
                raise RetentionError("BuildLease request differs from its signed experiment build contract")
            if self.metadata.get("native_moltenvk_request"):
                native_req = _dict_to_native_request(self.metadata["native_moltenvk_request"])
                expected_key = compute_native_moltenvk_config_key(native_req)
                if self.configuration_key_sha256 != expected_key:
                    raise RetentionError("BuildLease configuration_key_sha256 does not match computed native build request key")
            authorized = {str(Path(item).expanduser().resolve(strict=True))
                          for item in manifest.get("authorized_runner_paths", [])}
            if self.runner_path not in authorized:
                raise RetentionError("BuildLease runner is not listed in the signed experiment manifest")
            gate_attestation = GATE0_ATTESTATION_PATH.read_bytes()
            if manifest.get("gate0_attestation_sha256") != hashlib.sha256(gate_attestation).hexdigest():
                raise RetentionError("BuildLease manifest does not bind to the current Gate 0 attestation")
            blockers = _gate_blockers(active_experiment_id=self.owner_experiment)
            if blockers:
                raise RetentionError("Gate 0 blocks build start: " + "; ".join(blockers))
            free_now = disk_free_bytes(REPO)
            current_project = int(measure_project_usage()["allocated_inode_deduplicated_bytes"])
            peak = estimates["estimated_max_new_disk_bytes"]
            if free_now < FREE_MINIMUM or free_now - peak < FREE_WARNING:
                raise RetentionError("live free-space check blocks the declared build peak")
            if current_project + peak > PROJECT_BUDGET:
                raise RetentionError("live project-footprint check blocks the declared build peak")
        except BaseException:
            self._release_allocation_lock()
            raise
        try:
            self.lock_fds = _lock()
        except BaseException:
            self._release_allocation_lock()
            raise
        lease_fd, lock_fd = self.lock_fds
        try:
            _retire_pending_locked()
            reusable = find_canonical_equivalent(
                config_key_sha256=self.configuration_key_sha256, kind=self.kind,
                source_tree_hash=self.source_tree_hash,
                dependency_state=self.metadata.get("dependency_state"))
            if reusable is not None:
                self.reused_manifest = reusable
                self._release_lock()
                self._release_allocation_lock()
                return None
            blockers = capacity_blockers()
            if blockers:
                raise RetentionError("build cap blocks creation: " + "; ".join(blockers))
            snapshot = _snapshot()
            if snapshot["active_build_count"] + 1 > MAX_ACTIVE_LARGE_BUILDS:
                raise RetentionError("global active large-build cap would be exceeded")
            if self.kind == "dxvk" and snapshot["active_dxvk_count"] + 1 > MAX_ACTIVE_DXVK:
                raise RetentionError("active DXVK build cap would be exceeded")
            if self.kind == "moltenvk" and snapshot["active_moltenvk_count"] + 1 > MAX_ACTIVE_MOLTENVK:
                raise RetentionError("active MoltenVK diagnostic build cap would be exceeded")
            if snapshot["retained_build_count"] + 1 > MAX_RETAINED:
                raise RetentionError("global retained build cap would be exceeded")
            if self.path.parent != BUILD_ROOT:
                raise RetentionError("new build path is outside the approved root")
            parent_identity = _root_identity(BUILD_ROOT, create=True)
            parent_fd = _open_dir(BUILD_ROOT, create=True)
            try:
                if _exists_at(parent_fd, self.path.name):
                    raise FileExistsError(self.path)
                lease = {"schema_version": 1, "build_id": self.build_id, "path": str(self.path),
                         "kind": self.kind, "owner_experiment": self.owner_experiment,
                         "purpose": self.purpose, "state": "ACTIVE", "created_at_unix": time.time(),
                         "last_heartbeat_unix": time.time(), "source_tree_hash": self.source_tree_hash,
                         "expected_bytes": self.expected_bytes, "estimated_bytes": self.expected_bytes,
                         "approved_parent": str(BUILD_ROOT), "approved_parent_identity": parent_identity,
                         "metadata": self.metadata, "owner_pid": os.getpid(), "worktree": str(REPO),
                         "preserve_debug_reason": self.preserve_debug_reason,
                         "requested_config_key_sha256": self.configuration_key_sha256}
                # ACTIVE registry record precedes mkdir: a crash cannot leave an invisible build.
                _store_lease(lease)
                os.mkdir(self.path.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
                root_fd = os.open(self.path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                  | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                try:
                    info = os.fstat(root_fd)
                    lease.update({"build_device": info.st_dev, "build_inode": info.st_ino,
                                  "build_filesystem_id": getattr(os.fstatvfs(root_fd), "f_fsid", None)})
                    _store_lease(lease)
                    _write_marker(root_fd, lease)
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            finally:
                os.close(parent_fd)
            self.started = True
            self._start_heartbeat()
            self._release_allocation_lock()
            return self.path
        except BaseException:
            _unlock(lease_fd, lock_fd)
            self.lock_fds = None
            self._release_allocation_lock()
            raise

    def _release_allocation_lock(self) -> None:
        lock_file = self.storage_allocation_lock_file
        self.storage_allocation_lock_file = None
        if lock_file is not None:
            try:
                import storage_policy
                storage_policy._release_storage_allocation_lock(lock_file)
            except ImportError:
                lock_file.close()

    def _start_heartbeat(self) -> None:
        self.stop_event = threading.Event()
        def run():
            while not self.stop_event.wait(30):
                try:
                    self.heartbeat()
                except BaseException:
                    return
        self.beat_thread = threading.Thread(target=run, daemon=True, name=f"build-lease-{self.build_id}")
        self.beat_thread.start()

    def heartbeat(self) -> None:
        lease = _load_lease(self.build_id)
        if lease.get("state") != "ACTIVE" or lease.get("path") != str(self.path):
            raise RetentionError("build lease is no longer ACTIVE")
        lease["last_heartbeat_unix"] = time.time()
        _store_lease(lease)
        root_fd = _open_dir(self.path)
        try:
            marker = _read_marker(root_fd)
            if marker.get("build_id") != self.build_id or marker.get("state") != "ACTIVE":
                raise RetentionError("build marker changed")
            marker["last_heartbeat_unix"] = lease["last_heartbeat_unix"]
            _write_marker(root_fd, marker)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)

    def complete(self, *, preserve_debug: bool = False) -> dict[str, Any]:
        try:
            lease = _load_lease(self.build_id)
        except (OSError, RetentionError):
            raise RetentionError("cannot complete an unreserved build")
        if lease.get("path") != str(self.path):
            raise RetentionError("build lease path differs from this completion request")
        if lease.get("state") in {"COMPLETED", "RETIRING"}:
            manifest, digest = _verify_build_manifest(self.build_id)
            if (lease.get("build_manifest_id") != manifest.get("build_manifest_id")
                    or lease.get("build_manifest_sha256") != digest):
                raise RetentionError("completed lease does not bind its verified manifest")
            acquired_here = self.lock_fds is None
            if acquired_here:
                self.lock_fds = _lock()
            try:
                receipt = _retire(lease, reason="idempotent-completion-retry")
            finally:
                if acquired_here:
                    self._release_lock()
            return {"status": "COMPLETED_AND_AUTO_RETIRED", "build_manifest_id": manifest["build_manifest_id"],
                    "cleanup_receipt": receipt, "idempotent_replay": True}
        if lease.get("state") == "PRESERVE_DEBUG":
            manifest, digest = _verify_build_manifest(self.build_id)
            if (lease.get("build_manifest_id") != manifest.get("build_manifest_id")
                    or lease.get("build_manifest_sha256") != digest):
                raise RetentionError("preserved build does not bind its verified manifest")
            return {"status": "PRESERVE_DEBUG", "build_manifest_id": manifest["build_manifest_id"],
                    "build_manifest_path": str(MANIFEST_ROOT / f"{self.build_id}.json"),
                    "idempotent_replay": True}
        if not self.started or lease.get("state") != "ACTIVE":
            raise RetentionError(f"cannot complete a build in state {lease.get('state')}")
        execution = self._verified_execution_receipt(lease)
        _require_no_live_process(self.path)
        preserve_expiry = self.metadata.get("preserve_debug_expiry_unix")
        if preserve_debug and (not self.preserve_debug_reason or not isinstance(preserve_expiry, (int, float))
                               or isinstance(preserve_expiry, bool) or preserve_expiry <= time.time()
                               or preserve_expiry > time.time() + PRESERVE_DEBUG_MAX_SECONDS
                               or self.expected_bytes <= 0):
            raise RetentionError("PRESERVE_DEBUG requires a reason, estimated bytes, and expiry within seven days")
        if preserve_debug and _snapshot()["preserved_build_count"] >= MAX_PRESERVED_DEBUG:
            raise RetentionError("the one reference/debug preservation slot is occupied")
        self._stop_heartbeat()
        if lease.get("state") != "ACTIVE" or lease.get("build_inode") is None:
            raise RetentionError("active build lease lost its target identity")
        evidence = MANIFEST_ROOT / f"{self.build_id}.evidence"
        evidence_fd = _open_dir(evidence, create=True)
        os.close(evidence_fd)
        execution_evidence = _copy_evidence(
            self.path / BUILD_EXECUTION_RECEIPT, evidence / "execution/build-execution.json")
        if lease.get("metadata", {}).get("native_moltenvk_request"):
            native_req = _dict_to_native_request(lease["metadata"]["native_moltenvk_request"])
            outputs, identity = _copy_tree_artifacts_native_moltenvk(self.path, self.build_id, evidence, native_req)
        else:
            outputs, identity = _copy_tree_artifacts(self.path, self.build_id, evidence)
        identity["sandboxed_execution_evidence"] = execution_evidence
        identity["sandboxed_execution_status"] = "verified_successful_seatbelt_run"
        identity["sandboxed_execution_profile_sha256"] = execution["sandbox_profile_sha256"]
        if self.metadata.get("dependency_state") is not None:
            identity["dependency_state"] = self.metadata["dependency_state"]
        if str(identity.get("build_type", "")).casefold() != str(self.metadata.get("build_type", "")).casefold():
            raise RetentionError("completed build type differs from the signed pre-build type")
        identity["config_key_sha256"] = configuration_key_from_identity(
            identity, dependency_state=identity["dependency_state"])
        if identity.get("config_key_sha256") != self.configuration_key_sha256:
            raise RetentionError("completed build does not match the requested canonical configuration key")
        lease["preserve_debug_expires_at_unix"] = preserve_expiry if preserve_debug else None
        manifest, digest = _build_manifest(lease, outputs, identity, evidence,
                                           provenance_scope="captured during successful build completion")
        state = "PRESERVE_DEBUG" if preserve_debug else "COMPLETED"
        lease.update({"state": state, "build_manifest_id": manifest["build_manifest_id"],
                      "build_manifest_path": str(MANIFEST_ROOT / f"{self.build_id}.json"),
                      "build_manifest_sha256": digest, "completed_at_unix": time.time()})
        _store_lease(lease)
        root_fd = _open_dir(self.path)
        try:
            marker = _read_marker(root_fd)
            marker.update({"state": state, "build_manifest_id": manifest["build_manifest_id"],
                           "build_manifest_sha256": digest})
            _write_marker(root_fd, marker)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        if preserve_debug:
            self._release_lock()
            return {"status": state, "build_manifest_id": manifest["build_manifest_id"],
                    "build_manifest_path": str(MANIFEST_ROOT / f"{self.build_id}.json")}
        receipt = _retire(lease, reason="successful-build-completion")
        self._release_lock()
        return {"status": "COMPLETED_AND_AUTO_RETIRED", "build_manifest_id": manifest["build_manifest_id"],
                "cleanup_receipt": receipt}

    def _verified_execution_receipt(self, lease: Mapping[str, Any]) -> dict[str, Any]:
        """Require completion proof written only after BuildLease.run's sandboxed exit 0."""
        try:
            root_fd = _open_dir(self.path)
            try:
                root_info = os.fstat(root_fd)
                root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
                marker = _read_marker(root_fd)
                try:
                    proof = json.loads(_read_at(root_fd, BUILD_EXECUTION_RECEIPT))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RetentionError("successful execution receipt is not valid JSON") from error
                if not isinstance(proof, dict) or not _verify_seal(proof):
                    raise RetentionError("successful execution receipt signature is invalid")
                log_fd = os.open("build.log", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
                try:
                    log_info = os.fstat(log_fd)
                    if not stat.S_ISREG(log_info.st_mode):
                        raise RetentionError("build log is not a regular file")
                    log_hash = _hash_fd(log_fd)
                    log_after = os.fstat(log_fd)
                    if (log_info.st_dev, log_info.st_ino, log_info.st_size, log_info.st_mtime_ns) != (
                            log_after.st_dev, log_after.st_ino, log_after.st_size, log_after.st_mtime_ns):
                        raise RetentionError("build log changed while verifying completion proof")
                finally:
                    os.close(log_fd)
                expected_profile = _sandbox_profile_text(self.path)
                launcher = Path("/usr/bin/sandbox-exec")
                launcher_hash = _hash_path(launcher)
                parent_fd = _open_dir(self.path.parent)
                try:
                    current_root = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                finally:
                    os.close(parent_fd)
                if ((current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino)
                        or not stat.S_ISDIR(current_root.st_mode)):
                    raise RetentionError("leased build path no longer names the pinned build directory")
            finally:
                os.close(root_fd)
        except (OSError, RetentionError, json.JSONDecodeError) as error:
            raise RetentionError(f"successful sandboxed build execution evidence is missing or invalid: {error}") from error
        expected_profile_sha = hashlib.sha256(expected_profile.encode("utf-8")).hexdigest()
        expected = {
            "schema_version": 1,
            "build_id": self.build_id,
            "build_path": str(self.path),
            "build_device": root_info.st_dev,
            "build_inode": root_info.st_ino,
            "build_filesystem_id": root_fsid,
            "build_marker_id": marker.get("build_id"),
            "build_marker_state": marker.get("state"),
            "sandboxed": True,
            "return_code": 0,
            "sandbox_launcher_path": str(launcher),
            "sandbox_launcher_sha256": launcher_hash,
            "sandbox_profile_sha256": expected_profile_sha,
            "build_log_sha256": log_hash,
        }
        registered_identity = (lease.get("build_device"), lease.get("build_inode"),
                               lease.get("build_filesystem_id"))
        observed_identity = (root_info.st_dev, root_info.st_ino, root_fsid)
        if (observed_identity != registered_identity
                or any(proof.get(key) != value for key, value in expected.items())):
            raise RetentionError("successful execution receipt does not match the active build root or sandbox")
        command = proof.get("command")
        if (not isinstance(command, list) or not command
                or proof.get("command_sha256") != hashlib.sha256(_canonical_json(command)).hexdigest()
                or proof.get("started_at_unix", 0) > proof.get("completed_at_unix", 0)
                or proof.get("completed_at_unix", 0) > time.time() + 60):
            raise RetentionError("successful execution receipt command or time binding is invalid")
        native_request_data = lease.get("metadata", {}).get("native_moltenvk_request")
        if native_request_data:
            native_request = _dict_to_native_request(native_request_data)
            declared_environment = proof.get("declared_environment")
            effective_environment = proof.get("effective_environment")
            expected_effective = dict(native_request.normalized_environment)
            temporary_path = str(self.path / ".tmp")
            expected_effective.update({"TMPDIR": temporary_path, "TMP": temporary_path, "TEMP": temporary_path})
            if (declared_environment != dict(native_request.normalized_environment)
                    or effective_environment != expected_effective
                    or proof.get("declared_environment_sha256") != hashlib.sha256(
                        _canonical_json(declared_environment)).hexdigest()
                    or proof.get("effective_environment_sha256") != hashlib.sha256(
                        _canonical_json(effective_environment)).hexdigest()):
                raise RetentionError("successful native build receipt does not bind the declared/effective environment")
            if native_request.prepared_source_root_relative_path is not None:
                source_before = proof.get("prepared_source_before_execution")
                source_after = proof.get("prepared_source_after_execution")
                if (not isinstance(source_before, dict) or not isinstance(source_after, dict)
                        or source_before.get("prepared_source_tree_sha256")
                            != source_after.get("prepared_source_tree_sha256")
                        or source_before.get("prepared_source_file_count")
                            != source_after.get("prepared_source_file_count")):
                    raise RetentionError("successful native build receipt does not prove an unchanged prepared source tree")
                current_source = _verify_native_prepared_source(self.path, native_request)
                if current_source != source_after:
                    raise RetentionError("prepared source changed after the successful build receipt was recorded")
            observed_toolchain = _capture_native_xcode_toolchain(native_request.normalized_environment)
            if proof.get("native_toolchain_observation") != observed_toolchain:
                raise RetentionError("native Xcode toolchain changed between build launch and receipt verification")
        return proof

    def _record_successful_sandboxed_run(self, command: list[str], started_at: float,
                                         profile: str, launcher_sha256: str,
                                         declared_environment: Mapping[str, str] | None = None,
                                         effective_environment: Mapping[str, str] | None = None,
                                         native_toolchain_observation: Mapping[str, str] | None = None,
                                         prepared_source_before_execution: Mapping[str, Any] | None = None,
                                         prepared_source_after_execution: Mapping[str, Any] | None = None) -> None:
        root_fd = _open_dir(self.path)
        try:
            root_info = os.fstat(root_fd)
            marker = _read_marker(root_fd)
            fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        finally:
            os.close(root_fd)
        if marker.get("build_id") != self.build_id or marker.get("state") != "ACTIVE":
            raise RetentionError("cannot record a successful command for a changed build lease")
        payload = {
            "schema_version": 1,
            "build_id": self.build_id,
            "build_path": str(self.path),
            "build_device": root_info.st_dev,
            "build_inode": root_info.st_ino,
            "build_filesystem_id": fsid,
            "build_marker_id": marker.get("build_id"),
            "build_marker_state": marker.get("state"),
            "sandboxed": True,
            "return_code": 0,
            "command": command,
            "command_sha256": hashlib.sha256(_canonical_json(command)).hexdigest(),
            "sandbox_launcher_path": "/usr/bin/sandbox-exec",
            "sandbox_launcher_sha256": launcher_sha256,
            "sandbox_profile_sha256": hashlib.sha256(profile.encode("utf-8")).hexdigest(),
            "build_log_sha256": _hash_path(self.path / "build.log"),
            "started_at_unix": started_at,
            "completed_at_unix": time.time(),
        }
        if declared_environment is not None and effective_environment is not None:
            declared = dict(declared_environment)
            effective = dict(effective_environment)
            payload.update({
                "declared_environment": declared,
                "declared_environment_sha256": hashlib.sha256(_canonical_json(declared)).hexdigest(),
                "effective_environment": effective,
                "effective_environment_sha256": hashlib.sha256(_canonical_json(effective)).hexdigest(),
            })
        if native_toolchain_observation is not None:
            payload["native_toolchain_observation"] = dict(native_toolchain_observation)
        if prepared_source_before_execution is not None:
            payload["prepared_source_before_execution"] = dict(prepared_source_before_execution)
        if prepared_source_after_execution is not None:
            payload["prepared_source_after_execution"] = dict(prepared_source_after_execution)
        _write_json(self.path / BUILD_EXECUTION_RECEIPT, payload, replace=False, sealed=True)

    def fail(self, *, command: list[str], return_code: int, log_path: Path | None = None,
             error: str | None = None) -> dict[str, Any]:
        acquired_here = self.lock_fds is None
        if acquired_here:
            self.lock_fds = _lock()
        try:
            return self._fail_locked(command=command, return_code=return_code, log_path=log_path, error=error)
        finally:
            if acquired_here:
                self._release_lock()

    def _fail_locked(self, *, command: list[str], return_code: int, log_path: Path | None = None,
                     error: str | None = None) -> dict[str, Any]:
        if not self.started:
            raise RetentionError("cannot fail an unreserved build")
        command = _validate_failure_command(command)
        error, error_sha256 = _bounded_failure_error(error)
        self._stop_heartbeat()
        _require_no_live_process(self.path)
        lease = _load_lease(self.build_id)
        evidence = MANIFEST_ROOT / f"{self.build_id}.failure"
        evidence_fd = _open_dir(evidence / "configuration", create=True)
        os.close(evidence_fd)
        copied = []
        copied_bytes = 0
        generated_diagnostics: list[dict[str, Any]] = []

        def capture(relative: str, target: Path, *, required: bool) -> dict[str, Any] | None:
            nonlocal copied_bytes
            try:
                source_fd = _open_leased_file_fd(lease, relative)
            except FileNotFoundError:
                if required:
                    raise RetentionError(f"required failure log is missing from the leased build: {relative}")
                return None
            try:
                source_info = os.fstat(source_fd)
                limit = min(MAX_FAILURE_CAPTURE_FILE_BYTES,
                            MAX_FAILURE_CAPTURE_TOTAL_BYTES - copied_bytes)
                if source_info.st_size > limit:
                    raise RetentionError(f"failure diagnostic exceeds per-file/total evidence limit: {relative}")
                row = _copy_evidence_from_fd(source_fd, self.path / relative, target, max_bytes=limit)
            finally:
                os.close(source_fd)
            copied_bytes += row["size_bytes"]
            return row

        for relative in ("meson-logs/meson-log.txt", "meson-private/cmd_line.txt",
                         "meson-info/intro-buildoptions.json", "meson-info/intro-compilers.json",
                         "meson-info/intro-buildsystem_files.json", "build.ninja"):
            row = capture(relative, evidence / "configuration" / relative, required=False)
            if row is not None:
                copied.append(row)
        if log_path is not None:
            selected_log = Path(log_path).absolute()
            build_root = self.path.absolute()
            if selected_log == build_root or build_root not in selected_log.parents:
                raise RetentionError("failure build log must be a regular file inside the leased build root")
            log_relative = selected_log.relative_to(build_root).as_posix()
            row = capture(log_relative, evidence / "build.log", required=True)
            if row is not None:
                copied.append(row)
        for relative in self.failure_capture_paths:
            row = capture(relative, evidence / "generated" / Path(relative), required=False)
            if row is None:
                generated_diagnostics.append({"relative_path": relative, "status": "MISSING"})
                continue
            copied.append(row)
            generated_diagnostics.append({"relative_path": relative, "status": "CAPTURED",
                                          "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                                          "retained_path": row["retained_path"]})
        command_record = {"build_id": self.build_id, "command": command, "return_code": return_code,
                          "error": error, "failed_at_unix": time.time(), "configuration_and_logs": copied,
                          "generated_diagnostics": generated_diagnostics}
        try:
            build_command_fd = _open_leased_file_fd(lease, "build-command.json")
        except FileNotFoundError:
            build_command_fd = None
        if build_command_fd is not None:
            try:
                build_command_info = os.fstat(build_command_fd)
                command_limit = min(MAX_FAILURE_CAPTURE_FILE_BYTES,
                                    MAX_FAILURE_CAPTURE_TOTAL_BYTES - copied_bytes)
                if build_command_info.st_size > command_limit:
                    raise RetentionError("build command record exceeds the bounded failure evidence size")
                command_row = _copy_evidence_from_fd(build_command_fd, self.path / "build-command.json",
                                                     evidence / "build-command.json", max_bytes=command_limit)
            finally:
                os.close(build_command_fd)
            copied.append(command_row)
            copied_bytes += command_row["size_bytes"]
            command_record["configuration_and_logs"] = copied
        if error_sha256 is not None:
            command_record["error_sha256"] = error_sha256
            command_record["error_truncated"] = True
        _check_failure_manifest_size(command_record)
        failure_path = evidence / "failure-manifest.json"
        _write_json(failure_path, command_record, replace=False)
        evidence_files = _inventory_evidence(evidence)
        lease.update({"state": "FAILED", "failure_evidence_root": str(evidence),
                      "failure_evidence_path": str(failure_path),
                      "failure_evidence_sha256": _hash_path(failure_path),
                      "failure_evidence_files": evidence_files,
                      "failure_evidence_index_sha256": hashlib.sha256(_canonical_json(evidence_files)).hexdigest(),
                      "failed_command": command, "return_code": return_code, "failed_at_unix": time.time()})
        _store_lease(lease)
        root_fd = _open_dir(self.path)
        try:
            marker = _read_marker(root_fd)
            marker["state"] = "FAILED"
            _write_marker(root_fd, marker)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        receipt = _retire(lease, reason="failed-build-evidence-captured")
        self._release_lock()
        return receipt

    def run(self, command: Iterable[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
            preserve_debug: bool = False) -> dict[str, Any]:
        args = _validate_failure_command([str(x) for x in command])
        if not self.started:
            self.reserve()
        if self.reused_manifest is not None:
            return {"status": "REUSED_CANONICAL", "build_manifest_id": self.reused_manifest["build_manifest_id"],
                    "artifacts": self.reused_manifest["artifacts"], "build_tree_created": False}
        log = self.path / "build.log"
        process = None
        try:
            selected_cwd = Path(cwd or self.path).absolute()
            try:
                resolved_cwd = selected_cwd.resolve(strict=True)
                resolved_root = self.path.resolve(strict=True)
            except OSError as error:
                raise RetentionError(f"build working directory is not available: {error}") from error
            if (selected_cwd.is_symlink() or resolved_cwd != selected_cwd
                    or (resolved_cwd != resolved_root and resolved_root not in resolved_cwd.parents)):
                raise RetentionError("build command cwd must be a real directory inside its leased build root")
            _validate_build_command_paths(args, resolved_cwd, resolved_root)
            native_request_data = self.metadata.get("native_moltenvk_request")
            native_toolchain_observation = None
            prepared_source_before_execution = None
            prepared_source_after_execution = None
            if native_request_data:
                native_request = _dict_to_native_request(native_request_data)
                native_toolchain_observation = _validate_native_build_execution_binding(
                    native_request, args, env, resolved_cwd, resolved_root)
                if native_request.prepared_source_root_relative_path is not None:
                    prepared_source_before_execution = _verify_native_prepared_source(
                        resolved_root, native_request)
            sandboxed_args, sandboxed_env = _sandbox_build_command(args, resolved_root, env)
            def external_build_roots() -> set[str]:
                values = set()
                for candidate in _all_root_candidates():
                    candidate_path = Path(os.path.abspath(str(candidate)))
                    if candidate_path != resolved_root and resolved_root not in candidate_path.parents:
                        values.add(str(candidate_path))
                return values

            # Nested build-system directories inside the active leased tree
            # are already covered by this lease and are removed with it. Only
            # roots created outside the lease are escapes.
            preexisting_build_roots = external_build_roots()
            command_record = {"build_id": self.build_id, "command": args,
                              "cwd": str(selected_cwd), "started_at_unix": time.time()}
            if native_request_data:
                command_record.update({
                    "declared_environment": dict(env or {}),
                    "effective_sandbox_environment": sandboxed_env,
                    "native_toolchain_observation": native_toolchain_observation,
                })
            _write_json(self.path / "build-command.json", command_record, replace=False)
            build_started_at = time.time()
            with log.open("wb") as stream:
                process = subprocess.Popen(sandboxed_args, cwd=selected_cwd, env=sandboxed_env,
                                           stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                rc = process.wait()
                stream.flush()
                os.fsync(stream.fileno())
            if rc:
                # Close the supervisor's own handle before failure retirement.
                # The liveness check intentionally rejects every process with
                # an open file below the lease, including this process.
                self.fail(command=args, return_code=rc, log_path=log)
                raise subprocess.CalledProcessError(rc, args)
            if native_request_data and native_request.prepared_source_root_relative_path is not None:
                prepared_source_after_execution = _verify_native_prepared_source(
                    resolved_root, native_request)
                if (prepared_source_before_execution.get("prepared_source_tree_sha256")
                        != prepared_source_after_execution.get("prepared_source_tree_sha256")
                        or prepared_source_before_execution.get("prepared_source_file_count")
                        != prepared_source_after_execution.get("prepared_source_file_count")):
                    raise RetentionError("native build modified files in the prepared source tree")
            discovered_after_build = external_build_roots()
            escaped_roots = sorted(discovered_after_build - preexisting_build_roots)
            if escaped_roots:
                raise RetentionError("build command created compiler output roots outside its lease: "
                                     + ", ".join(escaped_roots))
            self._record_successful_sandboxed_run(
                args, build_started_at, sandboxed_args[2], _hash_path(Path(sandboxed_args[0])),
                declared_environment=env if native_request_data else None,
                effective_environment=sandboxed_env if native_request_data else None,
                native_toolchain_observation=native_toolchain_observation,
                prepared_source_before_execution=prepared_source_before_execution,
                prepared_source_after_execution=prepared_source_after_execution)
            return self.complete(preserve_debug=preserve_debug)
        except BaseException as error:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except BaseException:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except OSError: pass
                    process.wait()
            try:
                if _load_lease(self.build_id).get("state") == "ACTIVE":
                    self.fail(command=args, return_code=-1,
                              log_path=log if log.is_file() else None, error=repr(error))
            finally:
                self._release_lock()
            raise

    def _stop_heartbeat(self):
        if self.stop_event:
            self.stop_event.set()
        if self.beat_thread:
            self.beat_thread.join(timeout=2)

    def _release_lock(self):
        if self.lock_fds:
            _unlock(*self.lock_fds)
            self.lock_fds = None

    def __enter__(self):
        self.reserve()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is not None and self.started and _load_lease(self.build_id).get("state") == "ACTIVE":
                self.fail(command=["build-context"], return_code=-1, error=repr(exc))
            elif exc_type is None and self.started and _load_lease(self.build_id).get("state") == "ACTIVE":
                raise RetentionError("build lease exited without complete() or fail()")
        finally:
            self._stop_heartbeat()
            self._release_lock()
        return False

def reconcile_pending() -> list[dict[str, Any]]:
    lease_fd, lock_fd = _lock()
    try:
        return _retire_pending_locked()
    finally:
        _unlock(lease_fd, lock_fd)

def _require_no_live_process(path: Path) -> None:
    # Legacy adoption is blocked if any process currently has a cwd/open file below the root.
    proc = subprocess.run(["/usr/sbin/lsof", "-t", "+D", str(path)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode not in {0, 1}:
        raise RetentionError(f"lsof could not establish legacy build liveness: {proc.stderr.strip()}")
    if proc.stdout.strip():
        raise RetentionError(f"legacy build is still open by process IDs: {proc.stdout.strip()}")

def adopt_and_complete(path: Path, *, kind: str, owner_experiment: str, purpose: str) -> dict[str, Any]:
    _require_no_live_process(path)
    adopted = adopt_legacy(path, kind=kind, owner_experiment=owner_experiment, purpose=purpose)
    return complete_adopted(adopted["build_id"])

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    adopt = sub.add_parser("adopt")
    adopt.add_argument("--path", type=Path, required=True)
    adopt.add_argument("--kind", choices=sorted(KINDS), required=True)
    adopt.add_argument("--owner-experiment", required=True)
    adopt.add_argument("--purpose", required=True)
    complete = sub.add_parser("complete-adopted")
    complete.add_argument("--build-id", required=True)
    close_debug = sub.add_parser("close-debug-preservation")
    close_debug.add_argument("--build-id", required=True)
    close_debug.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status_snapshot(), indent=2, sort_keys=True))
        elif args.command == "reconcile":
            print(json.dumps(reconcile_pending(), indent=2, sort_keys=True))
        elif args.command == "adopt":
            print(json.dumps(adopt_and_complete(args.path, kind=args.kind,
                            owner_experiment=args.owner_experiment, purpose=args.purpose),
                            indent=2, sort_keys=True))
        elif args.command == "complete-adopted":
            print(json.dumps(complete_adopted(args.build_id), indent=2, sort_keys=True))
        elif args.command == "close-debug-preservation":
            print(json.dumps(close_debug_preservation(args.build_id, reason=args.reason), indent=2, sort_keys=True))
        return 0
    except BaseException as error:
        print(f"build retention refused/failed: {error}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
