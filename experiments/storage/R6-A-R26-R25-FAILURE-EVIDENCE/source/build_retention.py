#!/usr/bin/env python3
"""Fail-closed FG-Metal build leases, artifactization, and automatic retirement.

New Step 11D.3 builds must use BuildLease under the dedicated cache root.
Successful builds are copied (never hard-linked) into the immutable-by-policy
SHA-256 store and deleted before completion returns. Legacy roots are an exact
allowlist used only for one-time reconciliation.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import dataclasses
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import plistlib
import platform
import re
import shutil
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

REPO = Path(__file__).resolve().parents[2]
R17_BOUND_REPO_ROOT = REPO.resolve()
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
ORPHANED_UNSTARTED_STATE = "ORPHANED_UNSTARTED_RETIRED"
ORPHANED_UNSTARTED_RECEIPT_DIR = "orphaned-unstarted"
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

# R17 binds these exact temporary roots to read-only ownership evidence. This
# is a one-run reconciliation, not a prefix rule and not BuildLease history.
# The roots remain in the storage inventory; only verified Firelink roots are
# outside FG-Metal scope. Exact path, directory identity, and report digests
# establish ownership. Descendant contents are measured live and are not used
# as an admission identity because a recursive metadata walk cannot provide an
# atomic snapshot of a mutable tree.
R17_OWNERSHIP_REPORT_DIGESTS = {
    "R6-A-R17-RETENTION-ROOT-OWNERSHIP.md":
        "b5fbe0786188cd85a41d318e85282e8636b087404d86bee1c74d13f791c6d780",
    "R6-A-R17-SCOPE-ACCOUNTING-AUDIT.md":
        "bbf01c70d3fd5a8243469d835a7cb05cf6783cd6dc5465786ca6770a310e95a4",
    "R6-A-R17-PROJECTED-PEAK-REVIEW.md":
        "c30d8dbd9a7d62848c2ddd900654e4894c6f9f21353118a509027589ef3cd614",
    "R6-A-R17-MINIMAL-RECLAMATION-REVIEW.md":
        "188f3a071ce687e649706dc5e72557e8e6c81da87380d4385e916a775951c8fd",
    "R6-A-R17-SCOPE-RETENTION-RECONCILIATION.md":
        "8c3369ca163a8f1467ccb5d6363a7a5e4736065faf2d06afd724373f7792207c",
}
R17_TEMP_ROOT_IDENTITIES: dict[str, dict[str, Any]] = {
    "/private/tmp/buildlease-vz-entitlement.RVW7oO": {
        "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
        "accounting_scope": "FG_METAL_PROJECT",
        "reason": "Exact preserved Virtualization entitlement probe; the current executable conflicts with the older no-products description.",
        "filesystem_identity": {"device": 16777229, "inode": 99994262,
                                "filesystem_id": 16777229, "mode": 448, "sticky": False, "owner_uid": 501},
    },
    "/private/tmp/firelink-aria2-release.gGghjc/aria2-1.37.0": {
        "classification": "UNRELATED_PROJECT_TEMP",
        "accounting_scope": "OTHER_PROJECT_PROTECTED",
        "reason": "Exact inspected Firelink aria2 release-build tree; preserve for its project owner.",
        "filesystem_identity": {"device": 16777229, "inode": 102051100,
                                "filesystem_id": 16777229, "mode": 493, "sticky": False, "owner_uid": 501},
    },
    "/private/tmp/firelink-aria2-validation.b8C6r1": {
        "classification": "UNRELATED_PROJECT_TEMP",
        "accounting_scope": "OTHER_PROJECT_PROTECTED",
        "reason": "Exact inspected Firelink aria2 validation tree; preserve for its project owner.",
        "filesystem_identity": {"device": 16777229, "inode": 102042172,
                                "filesystem_id": 16777229, "mode": 448, "sticky": False, "owner_uid": 501},
    },
    "/private/tmp/firelink-tauri3-alpha-target/debug/build": {
        "classification": "UNRELATED_PROJECT_TEMP",
        "accounting_scope": "OTHER_PROJECT_PROTECTED",
        "reason": "Exact inspected Firelink Tauri/Wry build-script outputs; preserve for their project owner.",
        "filesystem_identity": {"device": 16777229, "inode": 101046384,
                                "filesystem_id": 16777229, "mode": 493, "sticky": False, "owner_uid": 501},
    },
    "/private/tmp/firelink-tauri3-alpha5-1792487343268/src-tauri/target/debug/build": {
        "classification": "UNRELATED_PROJECT_TEMP",
        "accounting_scope": "OTHER_PROJECT_PROTECTED",
        "reason": "Exact inspected Firelink/Tauri plugin build-script outputs; preserve for their project owner.",
        "filesystem_identity": {"device": 16777229, "inode": 102077563,
                                "filesystem_id": 16777229, "mode": 493, "sticky": False, "owner_uid": 501},
    },
}

BUILD1_PARENT_RECEIPT_RELATIVE = Path("r6/provenance/R6-BASELINE-0-SOURCE-TO-BINARY.json")
BUILD1_PARENT_RECEIPT_SHA256 = "6fb7428f42724917845e55aa0defa0ffbe249b7422f05651eca24f82497c91a9"
BUILD1_FINAL_REVIEW_RELATIVE = Path("r6/provenance/R6-BASELINE-0-FINAL-RECEIPT-REVIEW.md")
BUILD1_FINAL_REVIEW_SHA256 = "0318598847ca579723c90f49fed2967ff80dc5cbafadbe7aaf5244a1a8de0ba1"
BUILD1_REVIEW_SUPPLEMENT_RELATIVE = Path(
    "r6/provenance/R6-BASELINE-0-BUILD-1-REVIEW-SUPPLEMENT.json")
BUILD1_REVIEW_SUPPLEMENT_SHA256 = "09c38a2ec17b8f9baed33d92a686d5144b628aa5f7c56159a599f39fff05c416"
BUILD1_INDEX_RELATIVE = Path("r6/provenance/R6-BASELINE-0-BUILD-1-INDEX.json")
BUILD1_INDEX_SHA256 = "557dfbd33a53b34248bda93cb786bf78eb2025c54021ce42fa63025f35195d9b"
BUILD1_DIRECTORY_MANIFEST_RELATIVE = Path(
    "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/evidence/build-state/"
    "R6-BASELINE-0-BUILD-1-BUILD-DIRECTORY-MANIFEST.json")
BUILD1_DIRECTORY_MANIFEST_SHA256 = "85f5d60a37d2705c3ee2bf3a54b76bd9658838dac52936fa4b02570ffc52fe28"
BUILD1_GENERATED_INPUTS_RELATIVE = Path(
    "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/evidence/generated/"
    "R6-BASELINE-0-GENERATED-INPUTS.json")
BUILD1_GENERATED_INPUTS_SHA256 = "cb1f85234f47bcc294b8a65cd46175238e90bb2105bd8de90162bd03cd97fc27"
BUILD1_ID = "R6-BASELINE-0-BUILD-1"
BUILD1_BASELINE_ID = "R6-BASELINE-0"

REJECTED_R5_BUILDROOT_EVIDENCE_ID = "R6-A-REJECTED-R5-BUILDROOT"
REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE = Path(
    "r6/provenance/R6-A-REJECTED-R5-BUILDROOT-EVIDENCE.json")
REJECTED_R5_BUILDROOT_AUTHORITY_SHA256 = "91b79a93c21fd52156039d14ade2756a32d7490a178399d99c613d3554b89c94"
REJECTED_R5_BUILDROOT_TREE_CONTENT_SHA256 = "5b9c69bbd99ef04daa843c5066e59ff19f45c69f78640dabbae57137e6510043"
REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE = "tests/present-accounting/present-accounting.exe"
REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256 = "aae5ee4bffbecd11eba50215bfa36814f650f8392bc61c1db8238d805eb8ac12"
REJECTED_R5_BUILDROOT_REQUIRED_AUTHORITY_REFERENCES = (
    {
        "path": "R6-A-BUILDROOT-RECONCILIATION.md",
        "sha256": "b66aacaff24619c3dae24c5bbe790761fb1e957ec22cd96be82d543b10d5e9e3",
        "size_bytes": 4143,
        "role": "Prior root disposition and explicit non-acceptance boundary",
    },
    {
        "path": "R6-A-BUILDROOT-INDEPENDENT-REVIEW.md",
        "sha256": "407818bbe6f5d45cbb3e4eee27ca1ed46cc19948f51cc25484c4caf7f7142090",
        "size_bytes": 5286,
        "role": "Prior independent forensic review; not an acceptance review",
    },
    {
        "path": "R6-A-FINAL-GPT6-ACCEPTANCE-REVIEW.md",
        "sha256": "612cc025d16d3cb9d8c07a9dc3bcf13e62e62260ca4e7af6d67917ca7a12ac77",
        "size_bytes": 14169,
        "role": "Failed final GPT-6 acceptance decision",
    },
    {
        "path": "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/"
                "R6-A-ACCOUNTING-SNAPSHOT-1-POST-REVIEW-EVIDENCE-INDEX-R8.json",
        "sha256": "b8d6b857d4dee0cc2aecdf2d7fbbbd9c4a9aaf24598b1b7ac2f1cd029fd97683",
        "size_bytes": 19840,
        "role": "Relevant immutable R8 evidence-index identity",
    },
    {
        "path": "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/R6-A-TEST-SUMMARY.json",
        "sha256": "9b9ef4da3bca527845ec9566ffa0470e0acaf89aaa66ff8035b653b4668c7069",
        "size_bytes": 6794,
        "role": "R8-indexed historical test summary with exact executable path and hash",
    },
    {
        "path": "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/reviews/R6-A-EXECUTION-REVIEW.md",
        "sha256": "827ce7eb4920fbe121622be4a9967efab0726f044fcafeaf795d3de60ea52af9",
        "size_bytes": 21384,
        "role": "R8-indexed prior path-and-hash observation; does not accept R6-A",
    },
    {
        "path": "R6-A-R5-TEST-SEMANTICS-APPLICABILITY.md",
        "sha256": "5f45c1f629e34e0986599af4ee429360596285980b7a76970fa3ba020ecda333",
        "size_bytes": 2316,
        "role": "R5 TEST semantic applicability artifact; semantics only",
    },
)
REJECTED_R5_BUILDROOT_R8_PATH = Path(
    "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/"
    "R6-A-ACCOUNTING-SNAPSHOT-1-POST-REVIEW-EVIDENCE-INDEX-R8.json")
REJECTED_R5_BUILDROOT_R8_REVISION = 8
REJECTED_R5_BUILDROOT_R8_DERIVATIVE_ID = "R6-A-ACCOUNTING-SNAPSHOT-1"
REJECTED_R5_BUILDROOT_R8_CLAIMS = (
    {
        "path": "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/R6-A-TEST-SUMMARY.json",
        "sha256": "9b9ef4da3bca527845ec9566ffa0470e0acaf89aaa66ff8035b653b4668c7069",
        "size_bytes": 6794,
    },
    {
        "path": "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/reviews/R6-A-EXECUTION-REVIEW.md",
        "sha256": "827ce7eb4920fbe121622be4a9967efab0726f044fcafeaf795d3de60ea52af9",
        "size_bytes": 21384,
    },
)

class RetentionError(RuntimeError):
    """The storage operation was refused or could not complete safely."""


@dataclasses.dataclass(frozen=True)
class ProtectedEvidenceRoot:
    """One exact fixed-roster evidence root retained at its bound pathname."""

    evidence_id: str
    relative_path: str
    inventory_sha256: str
    expected_file_count: int
    expected_logical_bytes: int
    tree_content_sha256: str | None = None
    critical_file_relative_path: str | None = None
    critical_file_sha256: str | None = None
    authority_relative_path: str | None = None
    authority_sha256: str | None = None


PROTECTED_EVIDENCE_ROOTS = (
    ProtectedEvidenceRoot(
        evidence_id="R6-A-BUILD-1-EXPLORATORY",
        relative_path="r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64",
        inventory_sha256="9148ae52e2f0500baee790c202a4e19936116bc3aa3f593f0e9a9ecd32e32604",
        expected_file_count=34,
        expected_logical_bytes=1_193_851,
    ),
    ProtectedEvidenceRoot(
        evidence_id="R6-A-BUILD-1-FINAL",
        relative_path="r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64-final",
        inventory_sha256="e38e5611d750e5594b39f0be81f07b5f7c4889c175a72e272411c252f0b78c2e",
        expected_file_count=665,
        expected_logical_bytes=113_970_767,
    ),
    ProtectedEvidenceRoot(
        evidence_id="R6-A-BUILD-1-GENERATED",
        relative_path="r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/evidence/generated/files",
        inventory_sha256="ddcd8b6abf146c132481ce4da6118a84dcf761a58a30380b222b42f6023974bd",
        expected_file_count=37,
        expected_logical_bytes=1_155_955,
    ),
    ProtectedEvidenceRoot(
        evidence_id=REJECTED_R5_BUILDROOT_EVIDENCE_ID,
        relative_path="r6/workspaces/R6-A-ACCOUNTING-SNAPSHOT-1-BUILD-WORKSPACE/build.x86_64",
        inventory_sha256="cabb6a4c177faf86024f9d1c6e62b3fb834913bbbd27ed004faffc1ffc264b9b",
        expected_file_count=667,
        expected_logical_bytes=127_359_989,
        tree_content_sha256=REJECTED_R5_BUILDROOT_TREE_CONTENT_SHA256,
        critical_file_relative_path=REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE,
        critical_file_sha256=REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256,
        authority_relative_path=REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE.as_posix(),
        authority_sha256=REJECTED_R5_BUILDROOT_AUTHORITY_SHA256,
    ),
)


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
    allowed_symlink_roots = tuple(Path(value).resolve(strict=False) for value in allowed_symlink_roots)
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

def _tree_hash(root: Path, *, reject_symlinks: bool = False,
               reject_external_hardlinks: bool = False,
               require_internal_symlinks: bool = False,
               include_root_git: bool = False) -> tuple[str, int]:
    root = Path(root)
    canonical_root = root.resolve(strict=True)
    if require_internal_symlinks and canonical_root != root:
        raise RetentionError(f"source tree root is not canonical: {root}")
    root_fd = _open_dir(root)
    root_dev = os.fstat(root_fd).st_dev
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    if root_fsid is None:
        os.close(root_fd)
        raise RetentionError(f"source tree filesystem identity is unavailable: {root}")
    digest = hashlib.sha256()
    count = 0
    regular_links: dict[tuple[int, int], tuple[int, int]] = {}
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    if not include_root_git and relative_dir == Path() and name == ".git":
                        continue
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = (relative_dir / name).as_posix()
                    digest.update(relative.encode("utf-8", "surrogateescape") + b"\0")
                    digest.update(oct(stat.S_IMODE(info.st_mode)).encode() + b"\0")
                    if stat.S_ISLNK(info.st_mode):
                        if reject_symlinks:
                            raise RetentionError(f"source tree contains a symlink: {relative}")
                        raw_target = os.readlink(name, dir_fd=parent_fd)
                        if require_internal_symlinks:
                            link_path = root / relative
                            try:
                                resolved_target = link_path.resolve(strict=True)
                            except OSError as error:
                                raise RetentionError(
                                    f"source tree symlink target is missing: {relative}") from error
                            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                            if ((info.st_dev, info.st_ino, info.st_mtime_ns, raw_target)
                                    != (after.st_dev, after.st_ino, after.st_mtime_ns,
                                        os.readlink(name, dir_fd=parent_fd))
                                    or (resolved_target != canonical_root
                                        and canonical_root not in resolved_target.parents)
                                    or resolved_target.stat().st_dev != root_dev
                                    or not (resolved_target.is_file() or resolved_target.is_dir())):
                                raise RetentionError(
                                    f"source tree symlink escapes or changed during hashing: {relative}")
                        digest.update(b"L\0" + os.fsencode(raw_target) + b"\n")
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
                            if reject_external_hardlinks:
                                key = (now.st_dev, now.st_ino)
                                previous = regular_links.get(key)
                                if previous is not None and previous[0] != now.st_nlink:
                                    raise RetentionError(f"source hardlink count changed while hashing: {relative}")
                                regular_links[key] = (now.st_nlink, (previous[1] if previous else 0) + 1)
                            digest.update(b"F\0" + _hash_fd(child_fd).encode() + b"\n")
                        finally:
                            os.close(child_fd)
                        count += 1
                    else:
                        raise RetentionError(f"source tree contains unsupported file type: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        if reject_external_hardlinks:
            for (device, inode), (reported_links, observed_links) in regular_links.items():
                if reported_links != observed_links:
                    raise RetentionError(
                        f"source file has hardlinks outside the admitted tree: {device}:{inode}")
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


def _protected_evidence_root_inventory(root: Path) -> tuple[list[dict[str, Any]], dict[str, tuple[int, int]], int]:
    """Hash exact protected-root membership without persisting incidental metadata."""
    root = Path(os.path.abspath(root))
    parent_fd = _open_dir(root.parent)
    root_fd: int | None = None
    root_identity: tuple[int, int] | None = None
    root_state: tuple[int, int, int, int] | None = None
    root_fsid: int | None = None
    rows: list[dict[str, Any]] = []
    file_identities: dict[str, tuple[int, int]] = {}
    file_states: dict[str, tuple[int, int, int, int, int]] = {}
    directory_states: dict[str, tuple[int, int, int, int]] = {}
    open_child_fds: set[int] = set()
    stack: list[tuple[int, Path]] = []
    try:
        root_info = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(root_info.st_mode):
            raise RetentionError(f"protected evidence root is not a real directory: {root}")
        root_fd = os.open(root.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        opened_root = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        if (root_fsid is None or (opened_root.st_dev, opened_root.st_ino) !=
                (root_info.st_dev, root_info.st_ino)):
            raise RetentionError(f"protected evidence root changed while opening: {root}")
        root_identity = (opened_root.st_dev, opened_root.st_ino)
        root_state = (opened_root.st_dev, opened_root.st_ino, opened_root.st_mtime_ns,
                      getattr(opened_root, "st_ctime_ns", 0))
        root_device = opened_root.st_dev
        logical_bytes = 0
        stack = [(root_fd, Path())]
        while stack:
            directory_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(directory_fd)):
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    relative = (relative_dir / name).as_posix()
                    if info.st_dev != root_device:
                        raise RetentionError(f"protected evidence crosses a filesystem boundary: {root / relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                        child_info = os.fstat(child_fd)
                        child_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
                        if ((child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino)
                                or child_fsid != root_fsid):
                            os.close(child_fd)
                            raise RetentionError(f"protected evidence directory changed while opening: {root / relative}")
                        rows.append({"path": relative, "kind": "directory"})
                        directory_states[relative] = (
                            child_info.st_dev, child_info.st_ino, child_info.st_mtime_ns,
                            getattr(child_info, "st_ctime_ns", 0))
                        open_child_fds.add(child_fd)
                        stack.append((child_fd, relative_dir / name))
                    elif stat.S_ISREG(info.st_mode):
                        file_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                        try:
                            before = os.fstat(file_fd)
                            file_fsid = getattr(os.fstatvfs(file_fd), "f_fsid", None)
                            digest = _hash_fd(file_fd)
                            after = os.fstat(file_fd)
                            if (not stat.S_ISREG(before.st_mode) or file_fsid != root_fsid
                                    or (before.st_dev, before.st_ino, before.st_size,
                                        before.st_mtime_ns, getattr(before, "st_ctime_ns", 0)) !=
                                    (after.st_dev, after.st_ino, after.st_size,
                                     after.st_mtime_ns, getattr(after, "st_ctime_ns", 0))):
                                raise RetentionError(f"protected evidence file changed while hashing: {root / relative}")
                        finally:
                            os.close(file_fd)
                        rows.append({"path": relative, "kind": "file", "size_bytes": before.st_size,
                                     "sha256": digest})
                        file_identities[relative] = (before.st_dev, before.st_ino)
                        file_states[relative] = (before.st_dev, before.st_ino, before.st_size,
                                                 before.st_mtime_ns, getattr(before, "st_ctime_ns", 0))
                        logical_bytes += before.st_size
                    else:
                        raise RetentionError(f"unsupported object in protected evidence: {root / relative}")
            finally:
                if directory_fd != root_fd:
                    open_child_fds.discard(directory_fd)
                    os.close(directory_fd)

        # Reopen every observed path after hashing. These identities are only
        # race guards for this call; they are deliberately absent from the
        # persisted content identity because BUILD-1 did not bind inodes.
        for relative, expected in sorted(directory_states.items()):
            directory_fd = _open_dir(root / relative)
            try:
                current = os.fstat(directory_fd)
                current_state = (current.st_dev, current.st_ino, current.st_mtime_ns,
                                 getattr(current, "st_ctime_ns", 0))
                if current_state != expected:
                    raise RetentionError(f"protected evidence directory was replaced during scan: {root / relative}")
            finally:
                os.close(directory_fd)
        for relative, expected in sorted(file_states.items()):
            relative_path = Path(relative)
            containing_fd = _open_dir(root / relative_path.parent)
            try:
                current = os.stat(relative_path.name, dir_fd=containing_fd, follow_symlinks=False)
                current_state = (current.st_dev, current.st_ino, current.st_size,
                                 current.st_mtime_ns, getattr(current, "st_ctime_ns", 0))
                if not stat.S_ISREG(current.st_mode) or current_state != expected:
                    raise RetentionError(f"protected evidence file was replaced during scan: {root / relative}")
            finally:
                os.close(containing_fd)
        current_root = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        current_root_state = (current_root.st_dev, current_root.st_ino, current_root.st_mtime_ns,
                              getattr(current_root, "st_ctime_ns", 0))
        if (not stat.S_ISDIR(current_root.st_mode)
                or (current_root.st_dev, current_root.st_ino) != root_identity
                or current_root_state != root_state):
            raise RetentionError(f"protected evidence root changed during scan: {root}")
        rows.sort(key=lambda row: row["path"])
        return rows, file_identities, logical_bytes
    except OSError as error:
        raise RetentionError(f"cannot safely inventory protected evidence root {root}: {error}") from error
    finally:
        for descriptor in open_child_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _protected_pinned_bytes(relative: Path, expected_sha256: str, label: str) -> bytes:
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RetentionError(f"protected evidence authority path is not canonical: {relative}")
    try:
        raw = _read_file_descriptor(REPO / relative)
    except (OSError, RetentionError) as error:
        raise RetentionError(f"protected evidence authority is unavailable ({label}): {error}") from error
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise RetentionError(f"protected evidence authority hash changed: {label}")
    return raw


def _protected_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError(f"protected evidence authority is invalid JSON: {label}") from error
    if not isinstance(value, dict):
        raise RetentionError(f"protected evidence authority is not an object: {label}")
    return value


def _protected_evidence_tree_content_sha256(rows: Iterable[Mapping[str, Any]]) -> str:
    """Hash regular-file content identity using the retained R5 audit record format."""
    digest = hashlib.sha256()
    for row in rows:
        if row.get("kind") != "file":
            continue
        relative = row.get("path")
        size_bytes = row.get("size_bytes")
        file_sha256 = row.get("sha256")
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/"))
                or not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0
                or not isinstance(file_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", file_sha256)):
            raise RetentionError("protected evidence content tree contains an invalid file identity")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _verify_rejected_r5_buildroot_authority(
        spec: ProtectedEvidenceRoot, root: Path, actual_rows: list[dict[str, Any]],
        inventory_sha256: str, actual_files: Mapping[str, tuple[int, str]],
        logical_bytes: int) -> None:
    """Verify immutable forensic authority for the one rejected R5 build root."""
    if (spec.evidence_id != REJECTED_R5_BUILDROOT_EVIDENCE_ID
            or spec.authority_relative_path != REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE.as_posix()
            or spec.authority_sha256 != REJECTED_R5_BUILDROOT_AUTHORITY_SHA256
            or spec.tree_content_sha256 != REJECTED_R5_BUILDROOT_TREE_CONTENT_SHA256
            or spec.critical_file_relative_path != REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE
            or spec.critical_file_sha256 != REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256):
        raise RetentionError("rejected R5 build-root roster authority differs from its fixed identity")

    authority_raw = _protected_pinned_bytes(
        REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE, REJECTED_R5_BUILDROOT_AUTHORITY_SHA256,
        "rejected R5 build-root authority")
    authority = _protected_json(authority_raw, "rejected R5 build-root authority")
    expected_reference_rows = [dict(row) for row in REJECTED_R5_BUILDROOT_REQUIRED_AUTHORITY_REFERENCES]
    if authority.get("authority_references") != expected_reference_rows:
        raise RetentionError("rejected R5 build-root authority references differ from the fixed record")
    reference_bytes: dict[str, bytes] = {}
    for reference in expected_reference_rows:
        relative = Path(reference["path"])
        raw = _protected_pinned_bytes(relative, reference["sha256"], reference["role"])
        if len(raw) != reference["size_bytes"]:
            raise RetentionError(f"rejected R5 build-root authority size changed: {relative}")
        reference_bytes[relative.as_posix()] = raw

    r8_reference = expected_reference_rows[3]
    r8 = _protected_json(reference_bytes[r8_reference["path"]], "R8 evidence index")
    expected_claims = [dict(row) for row in REJECTED_R5_BUILDROOT_R8_CLAIMS]
    expected_r8_identity = {
        "path": REJECTED_R5_BUILDROOT_R8_PATH.as_posix(),
        "sha256": r8_reference["sha256"],
        "revision": REJECTED_R5_BUILDROOT_R8_REVISION,
        "derivative_id": REJECTED_R5_BUILDROOT_R8_DERIVATIVE_ID,
        "indexed_historical_claims": expected_claims,
    }
    if (authority.get("r8_evidence_index") != expected_r8_identity
            or r8.get("schema_version") != 1
            or r8.get("revision") != REJECTED_R5_BUILDROOT_R8_REVISION
            or r8.get("derivative_id") != REJECTED_R5_BUILDROOT_R8_DERIVATIVE_ID
            or not isinstance(r8.get("artifacts"), list)):
        raise RetentionError("rejected R5 build-root authority does not bind the fixed R8 index identity")
    for expected_claim in expected_claims:
        matches = [row for row in r8["artifacts"]
                   if isinstance(row, dict) and row.get("path") == expected_claim["path"]]
        if len(matches) != 1 or any(matches[0].get(key) != value
                                   for key, value in expected_claim.items()):
            raise RetentionError("R8 index no longer binds the historical test-path evidence")

    failed_review = reference_bytes[expected_reference_rows[2]["path"]].decode("utf-8")
    if "FAIL — R6-A NOT ACCEPTED" not in failed_review:
        raise RetentionError("rejected R5 build-root authority no longer identifies the failed final review")
    summary = _protected_json(reference_bytes[expected_reference_rows[4]["path"]],
                              "R8-indexed historical test summary")
    windows_test = summary.get("test_binaries", {}).get("windows_x86_64_pe")
    if (summary.get("source_revision") != "R5"
            or summary.get("source_tree_sha256") != "488e953058160c2cc0796fc29d17086f4e0043c87eb6593ceabda93553241673"
            or not isinstance(windows_test, dict)
            or windows_test.get("path") != f"{spec.relative_path}/{REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE}"
            or windows_test.get("sha256") != REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256):
        raise RetentionError("R8-indexed test summary no longer binds the historical R5 executable")
    prior_execution_review = reference_bytes[expected_reference_rows[5]["path"]].decode("utf-8")
    if (f"{spec.relative_path}/{REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE}" not in prior_execution_review
            or REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256 not in prior_execution_review):
        raise RetentionError("R8-indexed execution review no longer binds the historical test executable")

    content_sha256 = _protected_evidence_tree_content_sha256(actual_rows)
    critical_identity = actual_files.get(REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE)
    if (authority.get("schema_version") != 1
            or authority.get("evidence_id") != REJECTED_R5_BUILDROOT_EVIDENCE_ID
            or authority.get("disposition") != "HISTORICAL_REJECTED_EXECUTION_EVIDENCE"
            or authority.get("R6_A_ACCEPTANCE") is not False
            or authority.get("execution_authorization") != "NONE"
            or authority.get("active_BuildLease") is not False
            or authority.get("R5_repair_admission") != "NOT_CREATED"
            or authority.get("exact_normalized_root_path") != str(root)):
        raise RetentionError("rejected R5 build-root authority classification or path changed")

    expected_accounting = {
        "policy": "FULLY_CHARGED",
        "storage_credit": False,
        "reservation_credit": False,
        "free_space_exclusion": False,
        "project_scope_exclusion": False,
        "BuildLease_exemption": False,
    }
    if authority.get("accounting") != expected_accounting:
        raise RetentionError("rejected R5 build-root authority changes full storage accounting")
    expected_identity = {
        "regular_file_count": spec.expected_file_count,
        "directory_count": sum(row["kind"] == "directory" for row in actual_rows),
        "logical_bytes": spec.expected_logical_bytes,
        "symlink_count": 0,
        "multiply_linked_regular_file_count": 0,
        "inventory_sha256": inventory_sha256,
        "tree_content_sha256": content_sha256,
        "critical_executable": {
            "relative_path": REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE,
            "sha256": REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256,
            "size_bytes": critical_identity[0] if critical_identity is not None else None,
        },
    }
    if (authority.get("identity") != expected_identity
            or authority.get("complete_inventory") != actual_rows
            or len(actual_files) != spec.expected_file_count
            or logical_bytes != spec.expected_logical_bytes
            or inventory_sha256 != spec.inventory_sha256
            or content_sha256 != spec.tree_content_sha256
            or critical_identity is None
            or critical_identity[1] != spec.critical_file_sha256):
        raise RetentionError(f"rejected R5 build-root inventory or content identity changed: {root}")
def _verify_protected_evidence_roots(*, include_file_identities: bool = False) -> list[dict[str, Any]]:
    """Verify the fixed BUILD-1 parent chain and the rejected R5 evidence tree."""
    if not PROTECTED_EVIDENCE_ROOTS:
        return []
    receipt_raw = _protected_pinned_bytes(
        BUILD1_PARENT_RECEIPT_RELATIVE, BUILD1_PARENT_RECEIPT_SHA256, "BUILD-1 parent receipt")
    review_raw = _protected_pinned_bytes(
        BUILD1_FINAL_REVIEW_RELATIVE, BUILD1_FINAL_REVIEW_SHA256, "BUILD-1 final review")
    supplement_raw = _protected_pinned_bytes(
        BUILD1_REVIEW_SUPPLEMENT_RELATIVE, BUILD1_REVIEW_SUPPLEMENT_SHA256,
        "BUILD-1 review supplement")
    index_raw = _protected_pinned_bytes(BUILD1_INDEX_RELATIVE, BUILD1_INDEX_SHA256, "BUILD-1 index")
    manifest_raw = _protected_pinned_bytes(
        BUILD1_DIRECTORY_MANIFEST_RELATIVE, BUILD1_DIRECTORY_MANIFEST_SHA256,
        "BUILD-1 directory manifest")
    generated_raw = _protected_pinned_bytes(
        BUILD1_GENERATED_INPUTS_RELATIVE, BUILD1_GENERATED_INPUTS_SHA256,
        "BUILD-1 generated-inputs record")
    receipt = _protected_json(receipt_raw, "BUILD-1 parent receipt")
    supplement = _protected_json(supplement_raw, "BUILD-1 review supplement")
    index = _protected_json(index_raw, "BUILD-1 index")
    manifest = _protected_json(manifest_raw, "BUILD-1 directory manifest")
    generated = _protected_json(generated_raw, "BUILD-1 generated-inputs record")

    receipt_hash = hashlib.sha256(receipt_raw).hexdigest()
    review_hash = hashlib.sha256(review_raw).hexdigest()
    supplement_hash = hashlib.sha256(supplement_raw).hexdigest()
    index_hash = hashlib.sha256(index_raw).hexdigest()
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    generated_hash = hashlib.sha256(generated_raw).hexdigest()
    if (receipt.get("baseline_id") != BUILD1_BASELINE_ID or receipt.get("build_id") != BUILD1_ID
            or receipt.get("closure_status") != "COMPLETE_PASS_WITH_NON_BLOCKING_CAVEATS"):
        raise RetentionError("protected evidence parent receipt identity or closure status changed")
    if (supplement.get("baseline_id") != BUILD1_BASELINE_ID or supplement.get("build_id") != BUILD1_ID
            or supplement.get("parent_result") != "PARENT_RECEIPT_CLOSED"
            or supplement.get("existing_indexes_replaced") is not False):
        raise RetentionError("protected evidence review supplement identity or closure changed")
    finalized_ref = supplement.get("finalized_receipt")
    index_ref = supplement.get("prior_build_1_index")
    review_ref = supplement.get("supplemental_review")
    if (finalized_ref != {"path": BUILD1_PARENT_RECEIPT_RELATIVE.as_posix(),
                          "sha256": receipt_hash, "size_bytes": len(receipt_raw)}
            or index_ref != {"path": BUILD1_INDEX_RELATIVE.as_posix(),
                             "sha256": index_hash, "size_bytes": len(index_raw)}
            or not isinstance(review_ref, dict)
            or review_ref.get("path") != BUILD1_FINAL_REVIEW_RELATIVE.as_posix()
            or review_ref.get("sha256") != review_hash or review_ref.get("size_bytes") != len(review_raw)):
        raise RetentionError("protected evidence review supplement does not bind its accepted parent and index")
    if (index.get("baseline_id") != BUILD1_BASELINE_ID or index.get("build_id") != BUILD1_ID
            or index.get("classification") != "R6_BASELINE_PASS"
            or index.get("index_kind") != "supplemental-build"):
        raise RetentionError("protected evidence BUILD-1 index identity changed")
    receipt_ref = index.get("source_to_binary_receipt")
    if (receipt_ref != {"closure_status": receipt.get("closure_status"),
                        "path": BUILD1_PARENT_RECEIPT_RELATIVE.as_posix(),
                        "sha256": receipt_hash, "size_bytes": len(receipt_raw)}):
        raise RetentionError("protected evidence BUILD-1 index does not bind the finalized parent receipt")
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, list) or index.get("artifact_count") != len(artifacts):
        raise RetentionError("protected evidence BUILD-1 index artifact inventory is malformed")
    manifest_ref = receipt.get("build_directory_manifest_ref")
    generated_ref = receipt.get("generated_inputs_ref")
    if (manifest_ref != {"path": BUILD1_DIRECTORY_MANIFEST_RELATIVE.as_posix(),
                         "sha256": manifest_hash, "size_bytes": len(manifest_raw)}
            or generated_ref != {"path": BUILD1_GENERATED_INPUTS_RELATIVE.as_posix(),
                                 "sha256": generated_hash, "size_bytes": len(generated_raw)}
            or generated.get("build_id") != BUILD1_ID):
        raise RetentionError("protected evidence parent receipt does not bind its manifests")
    final_spec = next((spec for spec in PROTECTED_EVIDENCE_ROOTS
                       if spec.evidence_id == "R6-A-BUILD-1-FINAL"), None)
    if final_spec is None:
        raise RetentionError("protected evidence roster omits the parent build directory")
    final_root = Path(os.path.abspath(REPO / final_spec.relative_path))
    if (manifest.get("build_id") != BUILD1_ID
            or manifest.get("build_directory") != str(final_root)
            or not isinstance(manifest.get("entries"), list)):
        raise RetentionError("protected evidence durable build-directory manifest identifies another root")
    manifest_files: dict[str, tuple[int, str]] = {}
    for row in manifest["entries"]:
        if not isinstance(row, dict):
            raise RetentionError("protected evidence build-directory manifest has a malformed entry")
        if row.get("type") != "file":
            continue
        relative = row.get("path")
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or "\\" in relative or any(part in {"", ".", ".."} for part in relative.split("/"))
                or not isinstance(row.get("size_bytes"), int) or row["size_bytes"] < 0
                or not isinstance(row.get("sha256"), str)):
            raise RetentionError("protected evidence build-directory manifest contains an unsafe file entry")
        if relative in manifest_files:
            raise RetentionError("protected evidence build-directory manifest repeats a file path")
        manifest_files[relative] = (row["size_bytes"], row["sha256"])
    if (len(manifest_files) != manifest.get("file_entry_count")
            or sum(size for size, _digest in manifest_files.values()) != manifest.get("total_regular_file_bytes")):
        raise RetentionError("protected evidence build-directory manifest counts are inconsistent")

    specs_by_id: dict[str, ProtectedEvidenceRoot] = {}
    specs_by_path: dict[str, ProtectedEvidenceRoot] = {}
    for spec in PROTECTED_EVIDENCE_ROOTS:
        normalized_rel = Path(spec.relative_path)
        if (not spec.evidence_id or normalized_rel.is_absolute()
                or any(part in {"", ".", ".."} for part in normalized_rel.parts)
                or normalized_rel.as_posix() != spec.relative_path
                or spec.evidence_id in specs_by_id or spec.relative_path in specs_by_path):
            raise RetentionError("protected evidence root roster contains an invalid or duplicate entry")
        specs_by_id[spec.evidence_id] = spec
        specs_by_path[spec.relative_path] = spec
    if set(specs_by_id) != {
            "R6-A-BUILD-1-EXPLORATORY", "R6-A-BUILD-1-FINAL", "R6-A-BUILD-1-GENERATED",
            REJECTED_R5_BUILDROOT_EVIDENCE_ID}:
        raise RetentionError("protected evidence root roster differs from the exact four-root preservation set")

    verified: list[dict[str, Any]] = []
    for spec in sorted(PROTECTED_EVIDENCE_ROOTS, key=lambda value: value.relative_path):
        root = Path(os.path.abspath(REPO / spec.relative_path))
        prefix = spec.relative_path + "/"
        expected_files: dict[str, tuple[int, str]] = {}
        is_rejected_r5_root = spec.evidence_id == REJECTED_R5_BUILDROOT_EVIDENCE_ID
        if is_rejected_r5_root != (spec.authority_relative_path is not None):
            raise RetentionError("protected evidence authority metadata is assigned to an unexpected root")
        if not is_rejected_r5_root:
            for indexed_row in artifacts:
                if not isinstance(indexed_row, dict):
                    raise RetentionError("protected evidence BUILD-1 index has a malformed artifact row")
                artifact_path = indexed_row.get("path")
                if not isinstance(artifact_path, str):
                    raise RetentionError("protected evidence BUILD-1 index has a pathless artifact row")
                if not artifact_path.startswith(prefix):
                    continue
                relative = artifact_path[len(prefix):]
                if (not relative or Path(artifact_path).is_absolute() or "\\" in artifact_path
                        or any(part in {"", ".", ".."} for part in artifact_path.split("/"))
                        or not isinstance(indexed_row.get("size_bytes"), int) or indexed_row["size_bytes"] < 0
                        or not isinstance(indexed_row.get("sha256"), str)):
                    raise RetentionError(f"protected evidence index has an unsafe path below {root}")
                if relative in expected_files:
                    raise RetentionError(f"protected evidence index repeats a path below {root}")
                expected_files[relative] = (indexed_row["size_bytes"], indexed_row["sha256"])
        actual_rows, file_identities, logical_bytes = _protected_evidence_root_inventory(root)
        actual_files = {row["path"]: (row["size_bytes"], row["sha256"])
                        for row in actual_rows if row["kind"] == "file"}
        if not is_rejected_r5_root and (
                actual_files != expected_files or len(actual_files) != spec.expected_file_count
                or logical_bytes != spec.expected_logical_bytes):
            raise RetentionError(f"protected evidence files differ from accepted BUILD-1 index: {root}")
        inventory_hash = hashlib.sha256(_canonical_json(actual_rows)).hexdigest()
        if inventory_hash != spec.inventory_sha256:
            raise RetentionError(f"protected evidence membership/content digest changed: {root}")
        tree_content_sha256 = None
        if is_rejected_r5_root:
            _verify_rejected_r5_buildroot_authority(
                spec, root, actual_rows, inventory_hash, actual_files, logical_bytes)
            tree_content_sha256 = _protected_evidence_tree_content_sha256(actual_rows)
        if spec.evidence_id == "R6-A-BUILD-1-FINAL":
            if manifest_files != actual_files:
                raise RetentionError("protected final build files differ from the durable directory manifest")
        row = {
            "evidence_id": spec.evidence_id,
            "path": str(root),
            "parent_receipt_sha256": receipt_hash if not is_rejected_r5_root else None,
            "final_review_sha256": review_hash if not is_rejected_r5_root else None,
            "review_supplement_sha256": supplement_hash if not is_rejected_r5_root else None,
            "build_index_sha256": index_hash if not is_rejected_r5_root else None,
            "build_directory_manifest_sha256": (
                manifest_hash if spec.evidence_id == "R6-A-BUILD-1-FINAL" else None),
            "root_inventory_sha256": inventory_hash,
            "file_count": len(actual_files),
            "logical_bytes": logical_bytes,
            "accounting_policy": "FULLY_CHARGED",
            "mutation_policy": "FAIL_CLOSED",
            "relocation_allowed": False,
            "cleanup_allowed": False,
            "cas_mutation_allowed": False,
        }
        if is_rejected_r5_root:
            row.update({
                "tree_content_sha256": tree_content_sha256,
                "critical_file_relative_path": spec.critical_file_relative_path,
                "critical_file_sha256": spec.critical_file_sha256,
                "authority_metadata_path": str(REPO / REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE),
                "authority_metadata_sha256": spec.authority_sha256,
                "storage_credit": False,
                "reservation_credit": False,
                "free_space_exclusion": False,
                "project_scope_exclusion": False,
                "build_lease_exemption": False,
            })
        if include_file_identities:
            row["_file_identities"] = file_identities
        verified.append(row)
    return verified


def _protected_paths_overlap(path: Path | str) -> bool:
    candidate = Path(os.path.abspath(os.fspath(path)))
    for spec in PROTECTED_EVIDENCE_ROOTS:
        protected = Path(os.path.abspath(REPO / spec.relative_path))
        if candidate == protected or candidate in protected.parents or protected in candidate.parents:
            return True
    return False


def _refuse_protected_path_mutation(path: Path | str, operation: str) -> None:
    if _protected_paths_overlap(path):
        raise RetentionError(f"{operation} overlaps protected in-place parent evidence: {path}")


def _protected_identity_projection(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep transient descriptor identities out of the persistent content contract."""
    return sorted(({key: value for key, value in row.items() if not key.startswith("_")}
                   for row in rows), key=lambda row: str(row.get("path")))


def _gate0_retention_state_projection(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical stable build-retention state stored by Gate 0."""
    protected_evidence = snapshot.get("protected_in_place_evidence", [])
    return {
        "active_build_count": int(snapshot.get("active_build_count", -1)),
        "preserved_build_count": int(snapshot.get("preserved_build_count", -1)),
        "retained_build_count": int(snapshot.get("retained_build_count", -1)),
        "active_dxvk_count": int(snapshot.get("active_dxvk_count", -1)),
        "active_moltenvk_count": int(snapshot.get("active_moltenvk_count", -1)),
        "unknown_count": len(snapshot.get("unknown", [])),
        "completed_pending_retirement_count": len(snapshot.get("completed_pending_retirement", [])),
        "failed_or_retiring_count": len(snapshot.get("failed_or_retiring", [])),
        "protected_evidence_root_count": len(protected_evidence),
        "protected_in_place_evidence": _protected_identity_projection(protected_evidence),
        "retention_fingerprint_sha256": snapshot.get("retention_fingerprint", {}).get("sha256"),
    }

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

_MACHO_MAGIC_PREFIXES = frozenset({
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
})
_MACHO_FAT_MAGIC_PREFIXES = frozenset({
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
})


def _is_macho(path: Path) -> bool:
    """Recognize linkable/final Mach-O outputs while excluding ordinary .o files."""
    try:
        with path.open("rb") as stream:
            header = stream.read(32)
        if len(header) < 4:
            return False
        magic = header[:4]
        if magic in _MACHO_FAT_MAGIC_PREFIXES:
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
    protected_before = _verify_protected_evidence_roots(include_file_identities=True)
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
    protected_after = _verify_protected_evidence_roots(include_file_identities=True)
    if (_canonical_json(_protected_identity_projection(protected_before)) !=
            _canonical_json(_protected_identity_projection(protected_after))):
        raise RetentionError("protected BUILD-1 evidence content or membership changed during CAS isolation")
    protected_before_ids = {f"{row['path']}/{relative}": identity
                            for row in protected_before
                            for relative, identity in row.get("_file_identities", {}).items()}
    protected_after_ids = {f"{row['path']}/{relative}": identity
                           for row in protected_after
                           for relative, identity in row.get("_file_identities", {}).items()}
    if protected_before_ids != protected_after_ids:
        raise RetentionError("CAS isolation replaced a file inside protected BUILD-1 evidence")
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
        session_record = lease.get("session_v3")
        workspace_mount = (session_record.get("workspace_mount_identity")
                           if isinstance(session_record, dict) else None)
        if (candidate.parts[0] == BUILD_SESSION_WORKSPACE_NAME
                and isinstance(workspace_mount, dict) and workspace_mount.get("attached") is True):
            _verify_session_workspace_mount(root, workspace_mount)
        workspace_path_seen = False
        expected_device = root_info.st_dev
        expected_fsid = root_fsid
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
            child_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
            if (component == BUILD_SESSION_WORKSPACE_NAME and isinstance(workspace_mount, dict)
                    and workspace_mount.get("attached") is True and not workspace_path_seen):
                if (child_info.st_dev != workspace_mount.get("device")
                        or child_fsid != workspace_mount.get("filesystem_id")):
                    os.close(child_fd)
                    raise RetentionError("leased session diagnostics crossed the declared workspace mount")
                workspace_path_seen = True
                expected_device = workspace_mount["device"]
                expected_fsid = workspace_mount["filesystem_id"]
            elif ((child_info.st_dev != expected_device) or child_fsid != expected_fsid):
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
        if (not stat.S_ISREG(file_info.st_mode) or file_info.st_dev != expected_device
                or getattr(os.fstatvfs(file_fd), "f_fsid", None) != expected_fsid):
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
        if (not isinstance(options, list) or not options
                or any(not isinstance(row, Mapping) or not isinstance(row.get("name"), str)
                       for row in options)):
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
    if build_system == "meson":
        # Meson emits intro-buildoptions.json as keyed option records, but its
        # record order can vary between otherwise identical setup processes.
        # Treat those records as a map for config-key purposes while retaining
        # their original order in the captured evidence.
        options = sorted(options, key=lambda row: (
            str(row.get("machine", "")), str(row.get("section", "")),
            str(row.get("name", "")), _canonical_json(row)))
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


def _host_buildlease_p2_admission_blockers_locked(lease_fd: int) -> list[str]:
    blockers: list[str] = []
    names = sorted(os.listdir(lease_fd))
    if len(names) > 4096:
        return ["Host BuildLease state is unknown; lease registry exceeds the bounded scan"]
    for name in names:
        if name == BUILD_LOCK:
            continue
        if not name.endswith(".json") or name in {"", ".json"} or "/" in name:
            blockers.append(
                f"Host BuildLease state is unknown; unexpected registry entry: {name!r}")
            continue
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0), dir_fd=lease_fd)
            file_info = os.fstat(descriptor)
            if (not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1
                    or file_info.st_uid != os.getuid()
                    or stat.S_IMODE(file_info.st_mode) != 0o600
                    or file_info.st_size > 1024 * 1024):
                raise RetentionError("lease record is not a bounded private regular file")
            chunks: list[bytes] = []
            size = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                if size > 1024 * 1024:
                    raise RetentionError("lease record exceeds the bounded read")
            current_info = os.stat(name, dir_fd=lease_fd, follow_symlinks=False)
            if ((current_info.st_dev, current_info.st_ino)
                    != (file_info.st_dev, file_info.st_ino)):
                raise RetentionError("lease record changed during descriptor read")
            value = json.loads(b"".join(chunks))
            build_id = value.get("build_id") if isinstance(value, dict) else None
            created_at = value.get("created_at_unix") if isinstance(value, dict) else None
            retired_at = value.get("retired_at_unix") if isinstance(value, dict) else None
            if (not isinstance(value, dict) or not _verify_seal(value)
                    or build_id != name[:-5]
                    or not isinstance(value.get("schema_version"), int)
                    or isinstance(value.get("schema_version"), bool)
                    or value.get("schema_version") != 1
                    or not isinstance(value.get("path"), str)
                    or not Path(value["path"]).is_absolute()
                    or not isinstance(value.get("owner_experiment"), str)
                    or not value["owner_experiment"]
                    or value.get("kind") not in KINDS
                    or not isinstance(created_at, (int, float))
                    or isinstance(created_at, bool)
                    or not math.isfinite(float(created_at))
                    or (retired_at is not None
                        and (not isinstance(retired_at, (int, float))
                             or isinstance(retired_at, bool)
                             or not math.isfinite(float(retired_at))))):
                raise RetentionError("lease record identity, schema, or authentication is invalid")
            state = value.get("state")
            if state == "ACTIVE":
                blockers.append(
                    f"active Host BuildLease prevents P2 admission: {build_id}")
            elif state in {"FAILED", "RETIRING"}:
                blockers.append(
                    f"Host BuildLease state is unresolved; P2 admission fails closed: {build_id}")
            elif state == ORPHANED_UNSTARTED_STATE:
                try:
                    _verify_orphan_closeout_receipt(value)
                    if value.get("retired_at_unix") is not None:
                        raise RetentionError("orphan terminal record also claims physical retirement")
                except (OSError, RetentionError, ValueError, TypeError) as error:
                    blockers.append(
                        f"Host BuildLease orphan closeout is invalid; P2 admission fails closed: {build_id}: {error}")
            elif state not in {"COMPLETED", "PRESERVE_DEBUG"}:
                raise RetentionError(f"lease record has unknown state {state!r}")
            if value.get("retired_at_unix") is not None and state == "ACTIVE":
                raise RetentionError("retired lease record still claims ACTIVE state")
            session = value.get("session_v3")
            if session is not None:
                if (not isinstance(session, Mapping)
                        or not isinstance(session.get("schema_version"), int)
                        or isinstance(session.get("schema_version"), bool)
                        or session.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
                        or session.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                        or session.get("state") not in {
                            "RESERVED", "ACTIVE", "RUNNING", "QUIESCING",
                            "PENDING_SUCCESS", "READY_TO_FINALIZE", "ACCEPTED", "FAILED",
                        }):
                    raise RetentionError("Host BuildLease v3 session identity or state is unknown")
                if state == "COMPLETED" and session.get("state") != "ACCEPTED":
                    raise RetentionError("completed Host BuildLease v3 session is not accepted")
        except (OSError, RetentionError, ValueError, TypeError, json.JSONDecodeError) as error:
            blockers.append(
                f"Host BuildLease state is unknown; P2 admission fails closed for {name}: {error}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return blockers


@contextmanager
def host_buildlease_p2_admission_guard():
    """Hold the read-only retention lock through the P2 marker commit.

    Callers must already hold storage_policy's shared allocation lock. The
    existing lock serializes against legacy adoption and lease state changes;
    BuildLease.reserve and legacy adoption both order locks allocation-first.
    This guard never creates the lease root/lock, calls status_snapshot(), or
    reconciles pending retirement. Missing or unsafe authority fails closed.
    """
    lease_fd: int | None = None
    lock_fd: int | None = None
    lock_acquired = False
    setup_error: str | None = None
    try:
        lease_fd = _open_dir(LEASE_ROOT)
        directory_info = os.fstat(lease_fd)
        if (not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_uid != os.getuid()
                or stat.S_IMODE(directory_info.st_mode) & 0o077):
            raise RetentionError("lease registry directory has an unknown owner, type, or mode")
        lock_fd = os.open(
            BUILD_LOCK, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0), dir_fd=lease_fd)
        lock_info = os.fstat(lock_fd)
        named_lock_info = os.stat(BUILD_LOCK, dir_fd=lease_fd, follow_symlinks=False)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600
                or (lock_info.st_dev, lock_info.st_ino)
                    != (named_lock_info.st_dev, named_lock_info.st_ino)):
            raise RetentionError("retention lock is missing, unsafe, or changed")
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        lock_acquired = True
    except (OSError, RetentionError) as error:
        setup_error = str(error)

    if setup_error is not None:
        if lock_fd is not None:
            os.close(lock_fd)
        if lease_fd is not None:
            os.close(lease_fd)
        yield [f"Host BuildLease state is unknown; P2 admission fails closed: {setup_error}"]
        return

    try:
        try:
            assert lease_fd is not None
            blockers = _host_buildlease_p2_admission_blockers_locked(lease_fd)
        except (OSError, RetentionError, ValueError, TypeError) as error:
            blockers = [f"Host BuildLease state is unknown; P2 admission fails closed: {error}"]
        yield blockers
    finally:
        if lock_acquired and lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        if lock_fd is not None:
            os.close(lock_fd)
        if lease_fd is not None:
            os.close(lease_fd)


def host_buildlease_p2_admission_blockers() -> list[str]:
    """Read the signed lease registry, failing closed on unknown state."""
    with host_buildlease_p2_admission_guard() as blockers:
        return list(blockers)


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
    try:
        _verify_named_retention_lock(lease_fd, fd)
    except BaseException:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        os.close(lease_fd)
        raise
    return lease_fd, fd


def _verify_named_retention_lock(lease_fd: int, lock_fd: int) -> None:
    """Prove the locked descriptor is still the authoritative named lock inode."""
    opened = os.fstat(lock_fd)
    try:
        named = os.stat(BUILD_LOCK, dir_fd=lease_fd, follow_symlinks=False)
    except OSError as error:
        raise RetentionError("authoritative retention lock pathname disappeared") from error
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or stat.S_IMODE(named.st_mode) != 0o600):
        raise RetentionError("named retention lock no longer identifies the exclusively locked inode")


def _process_start_time_unix(pid: int) -> float:
    """Return a process birth time from the system process table, or fail closed."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RetentionError("orphan owner PID is invalid")
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise RetentionError("process identity could not be established for orphan closeout") from error
    rendered = result.stdout.strip()
    if result.returncode != 0 or not rendered:
        raise RetentionError("process identity could not be established for orphan closeout")
    try:
        return datetime.strptime(rendered, "%a %b %d %H:%M:%S %Y").timestamp()
    except ValueError as error:
        raise RetentionError("system process start time has an unsupported representation") from error


def _verify_orphan_owner_absent(lease: Mapping[str, Any]) -> None:
    pid = lease.get("owner_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise RetentionError("orphan reservation has no authenticated owner PID")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError as error:
        raise RetentionError("orphan owner liveness is ambiguous") from error
    created = lease.get("created_at_unix")
    if (not isinstance(created, (int, float)) or isinstance(created, bool)
            or not math.isfinite(float(created))):
        raise RetentionError("orphan reservation creation time is invalid")
    process_started = _process_start_time_unix(pid)
    if process_started <= float(created):
        raise RetentionError("reservation owner process is live or PID reuse cannot be distinguished")


def _require_no_matching_orphan_controller(build_id: str, owner_experiment: str) -> None:
    """Reject any other process still advertising this exact lease/controller identity."""
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,command="],
                                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise RetentionError("controller/descendant process inventory is unavailable") from error
    if result.returncode != 0:
        raise RetentionError("controller/descendant process inventory is unavailable")
    needles = (build_id, owner_experiment)
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid, command = int(fields[0]), fields[1]
        if pid == os.getpid():
            continue
        if any(re.search(rf"(?<![A-Za-z0-9._-]){re.escape(needle)}(?![A-Za-z0-9._-])",
                         command) for needle in needles):
            raise RetentionError("another matching BuildLease controller or descendant is still live")


def _orphan_closeout_receipt_path(build_id: str) -> Path:
    return RECEIPT_ROOT / ORPHANED_UNSTARTED_RECEIPT_DIR / f"{build_id}.json"


def _verify_orphan_closeout_receipt(lease: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the additive historical receipt for a terminal never-started lease."""
    build_id = lease.get("build_id")
    expected_path = _orphan_closeout_receipt_path(str(build_id))
    if (lease.get("state") != ORPHANED_UNSTARTED_STATE
            or lease.get("orphan_closeout_receipt_path") != str(expected_path)
            or not isinstance(build_id, str)):
        raise RetentionError("orphan terminal lease does not name its canonical closeout receipt")
    receipt_raw = _read_file_descriptor(expected_path)
    try:
        receipt = json.loads(receipt_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError("orphan closeout receipt is malformed") from error
    if not isinstance(receipt, dict) or not _verify_seal(receipt):
        raise RetentionError("orphan closeout receipt is unsigned or malformed")
    original_raw_b64 = receipt.get("original_registry_record_b64")
    marker_raw_b64 = receipt.get("original_marker_record_b64")
    if not isinstance(original_raw_b64, str) or not isinstance(marker_raw_b64, str):
        raise RetentionError("orphan closeout receipt omitted exact historical record bytes")
    try:
        original_raw = base64.b64decode(original_raw_b64, validate=True)
        original_marker_raw = base64.b64decode(marker_raw_b64, validate=True)
        original = json.loads(original_raw)
        original_marker = json.loads(original_marker_raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RetentionError("orphan closeout receipt contains malformed historical bytes") from error
    if not isinstance(original, dict) or not isinstance(original_marker, dict):
        raise RetentionError("orphan closeout receipt historical records are not objects")
    original_sha = hashlib.sha256(original_raw).hexdigest()
    marker_sha = hashlib.sha256(original_marker_raw).hexdigest()
    session = original.get("session_v3") if isinstance(original, dict) else None
    identity = {
        "build_id": build_id,
        "path": lease.get("path"),
        "owner_experiment": lease.get("owner_experiment"),
        "build_device": lease.get("build_device"),
        "build_inode": lease.get("build_inode"),
        "build_filesystem_id": lease.get("build_filesystem_id"),
        "plan_sha256": session.get("plan_sha256") if isinstance(session, Mapping) else None,
        "admission_identity": session.get("admission_identity") if isinstance(session, Mapping) else None,
    }
    if (not _verify_seal(original) or not _verify_seal(original_marker)
            or _canonical_json(original) != _canonical_json(original_marker)
            or original_sha != receipt.get("original_registry_record_sha256")
            or marker_sha != receipt.get("original_marker_record_sha256")
            or receipt.get("result") != ORPHANED_UNSTARTED_STATE
            or receipt.get("outputs_accepted") is not False
            or receipt.get("active_lease_charge_released") is not True
            or receipt.get("released_reservation_bytes") != original.get("expected_bytes")
            or receipt.get("identity") != identity
            or lease.get("orphan_original_record_sha256") != original_sha
            or lease.get("orphan_closeout_receipt_sha256")
                != hashlib.sha256(receipt_raw).hexdigest()
            or original.get("state") != "ACTIVE"
            or original.get("build_id") != build_id
            or original.get("path") != lease.get("path")
            or original.get("owner_experiment") != lease.get("owner_experiment")
            or original.get("build_device") != lease.get("build_device")
            or original.get("build_inode") != lease.get("build_inode")
            or original.get("build_filesystem_id") != lease.get("build_filesystem_id")
            or original.get("session_started") not in (None, False)
            or not isinstance(session, Mapping)
            or session.get("state") != "RESERVED"
            or session.get("next_step_index") != 0
            or session.get("peak_allocated_bytes") != 0
            or session.get("step_receipts") != []
            or lease.get("session_v3") != session
            or any(lease.get(key) != value for key, value in identity.items()
                   if key not in {"plan_sha256", "admission_identity"})):
        raise RetentionError("orphan closeout receipt conflicts with terminal lease identity")
    return receipt


def close_orphaned_unstarted_reservation(
        build_id: str, *, expected_path: Path | str, expected_owner_experiment: str,
        expected_admission_identity: Mapping[str, Any], expected_plan_sha256: str,
        expected_record_sha256: str) -> dict[str, Any]:
    """Retire exactly one authenticated, orphaned, never-started v3 reservation.

    This is a one-way closeout, not a resume or adoption API. It preserves the
    original registry and marker bytes inside an authenticated additive receipt.
    """
    from storage_policy import (_acquire_storage_allocation_lock,
                                _release_storage_allocation_lock)

    if (not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", build_id)
            or not isinstance(expected_owner_experiment, str) or not expected_owner_experiment
            or not isinstance(expected_admission_identity, Mapping)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_record_sha256)):
        raise RetentionError("orphan closeout requires complete exact lease, admission, and plan identities")
    target = Path(os.path.abspath(os.fspath(expected_path)))
    if target != BUILD_ROOT / build_id:
        raise RetentionError("orphan closeout path is not the exact approved BuildLease root")

    allocation_lock = _acquire_storage_allocation_lock()
    lease_fd: int | None = None
    lock_fd: int | None = None
    root_fd: int | None = None
    try:
        lease_fd, lock_fd = _lock()
        _verify_named_retention_lock(lease_fd, lock_fd)
        name = f"{build_id}.json"
        try:
            original_raw = _read_at(lease_fd, name, limit=1024 * 1024)
        except OSError as error:
            raise RetentionError("orphan closeout registry record is missing or unreadable") from error
        try:
            lease = json.loads(original_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetentionError("orphan closeout registry record is malformed") from error
        if (not isinstance(lease, dict) or not _verify_seal(lease)
                or hashlib.sha256(original_raw).hexdigest() != expected_record_sha256):
            raise RetentionError("orphan closeout registry authentication or exact record digest failed")
        session = lease.get("session_v3")
        if (lease.get("schema_version") != 1 or lease.get("build_id") != build_id
                or lease.get("path") != str(target)
                or lease.get("owner_experiment") != expected_owner_experiment
                or lease.get("state") != "ACTIVE"
                or not isinstance(session, dict) or session.get("state") != "RESERVED"
                or session.get("next_step_index") != 0
                or session.get("peak_allocated_bytes") != 0
                or session.get("step_receipts") != []
                or not isinstance(lease.get("expected_bytes"), int)
                or isinstance(lease.get("expected_bytes"), bool)
                or lease.get("expected_bytes") <= 0
                or lease.get("estimated_bytes") != lease.get("expected_bytes")
                or session.get("plan_sha256") != expected_plan_sha256
                or session.get("admission_identity") != dict(expected_admission_identity)
                or lease.get("session_started") not in (None, False)
                or lease.get("session_started_at_unix") is not None
                or session.get("started_at_unix") is not None
                or session.get("workspace_mount_pending") is True
                or session.get("workspace_mount_identity") is not None
                or session.get("acceptance_state") is not None
                or session.get("final_source_verification") is not None
                or lease.get("build_manifest_id") is not None
                or lease.get("build_manifest_sha256") is not None
                or lease.get("retired_at_unix") is not None
                or lease.get("cleanup_receipt_path") is not None
                or lease.get("build_execution_receipt") is not None):
            raise RetentionError("orphan closeout rejected a started, changed, or already terminal lease")

        receipt_path = _orphan_closeout_receipt_path(build_id)
        normal_receipt = RECEIPT_ROOT / f"{build_id}.json"
        if (receipt_path.exists() or receipt_path.is_symlink()
                or normal_receipt.exists() or normal_receipt.is_symlink()):
            raise RetentionError("orphan closeout found a duplicate or conflicting terminal receipt")

        other_records = [entry for entry in os.listdir(lease_fd)
                         if entry.endswith(".json") and entry != name]
        if len(other_records) > 4096:
            raise RetentionError("orphan closeout cannot bound the competing lease scan")
        for entry in other_records:
            try:
                candidate = json.loads(_read_at(lease_fd, entry, limit=1024 * 1024))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise RetentionError("orphan closeout found an ambiguous competing lease record") from error
            if not isinstance(candidate, dict) or not _verify_seal(candidate):
                raise RetentionError("orphan closeout found an unauthenticated competing lease record")
            candidate_session = candidate.get("session_v3")
            if (candidate.get("state") == "ACTIVE"
                    and (candidate.get("owner_experiment") == expected_owner_experiment
                         or (isinstance(candidate_session, Mapping)
                             and (candidate_session.get("plan_sha256") == expected_plan_sha256
                                  or candidate_session.get("admission_identity")
                                      == dict(expected_admission_identity))))):
                raise RetentionError("orphan closeout found a competing active controller reservation")

        _verify_orphan_owner_absent(lease)
        _require_no_matching_orphan_controller(build_id, expected_owner_experiment)
        _require_no_live_process(target)
        parent_identity = _root_identity(BUILD_ROOT, create=False)
        if lease.get("approved_parent") != str(BUILD_ROOT) or lease.get("approved_parent_identity") != parent_identity:
            raise RetentionError("orphan closeout approved-parent identity changed")
        root_fd = _open_dir(target)
        root_info = os.fstat(root_fd)
        if ((root_info.st_dev, root_info.st_ino) != (lease.get("build_device"), lease.get("build_inode"))
                or getattr(os.fstatvfs(root_fd), "f_fsid", None) != lease.get("build_filesystem_id")):
            raise RetentionError("orphan closeout exact build-root identity changed")
        marker_raw = _read_at(root_fd, BUILD_MARKER, limit=1024 * 1024)
        try:
            marker = json.loads(marker_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetentionError("orphan closeout marker is malformed") from error
        if (not isinstance(marker, dict) or not _verify_seal(marker)
                or _canonical_json(marker) != _canonical_json(lease)):
            raise RetentionError("orphan closeout marker and authenticated reservation differ")
        if sorted(os.listdir(root_fd)) != [BUILD_MARKER]:
            raise RetentionError("orphan closeout found session, workspace, image, stage, output, or execution state")
        workspace = target / BUILD_SESSION_WORKSPACE_NAME
        if (os.path.ismount(workspace) or _session_image_row(target) is not None
                or (target / BUILD_SESSION_DIRECTORY).exists()
                or (target / BUILD_SESSION_WORKSPACE_IMAGE_NAME).exists()
                or (target / BUILD_EXECUTION_RECEIPT).exists()
                or (target / "build-command.json").exists() or (target / "build.log").exists()):
            raise RetentionError("orphan closeout found a mounted workspace or execution output")
        if any(path.exists() or path.is_symlink() for path in (
                MANIFEST_ROOT / f"{build_id}.json", MANIFEST_ROOT / f"{build_id}.failure")):
            raise RetentionError("orphan closeout found finalization, failure, or accepted-output evidence")

        # Re-read all state after the external liveness and filesystem checks. This
        # rejects record/marker ABA changes before the additive receipt is created.
        _verify_orphan_owner_absent(lease)
        _require_no_matching_orphan_controller(build_id, expected_owner_experiment)
        _require_no_live_process(target)
        _verify_named_retention_lock(lease_fd, lock_fd)
        if (_read_at(lease_fd, name, limit=1024 * 1024) != original_raw
                or _read_at(root_fd, BUILD_MARKER, limit=1024 * 1024) != marker_raw
                or (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino)
                    != (lease.get("build_device"), lease.get("build_inode"))
                or sorted(os.listdir(root_fd)) != [BUILD_MARKER]
                or os.path.ismount(workspace)
                or _session_image_row(target) is not None
                or normal_receipt.exists() or normal_receipt.is_symlink()
                or receipt_path.exists() or receipt_path.is_symlink()):
            raise RetentionError("orphan closeout state changed or receipt appeared during validation")
        if any(path.exists() or path.is_symlink() for path in (
                MANIFEST_ROOT / f"{build_id}.json", MANIFEST_ROOT / f"{build_id}.failure")):
            raise RetentionError("finalization or accepted-output evidence appeared during closeout validation")

        receipt_payload = {
            "schema_version": 1,
            "result": ORPHANED_UNSTARTED_STATE,
            "build_id": build_id,
            "path": str(target),
            "outputs_accepted": False,
            "active_lease_charge_released": True,
            "released_reservation_bytes": lease["expected_bytes"],
            "created_at_unix": time.time(),
            "original_registry_record_b64": base64.b64encode(original_raw).decode("ascii"),
            "original_registry_record_sha256": hashlib.sha256(original_raw).hexdigest(),
            "original_marker_record_b64": base64.b64encode(marker_raw).decode("ascii"),
            "original_marker_record_sha256": hashlib.sha256(marker_raw).hexdigest(),
            "identity": {
                "build_id": build_id, "path": str(target),
                "owner_experiment": expected_owner_experiment,
                "build_device": lease.get("build_device"),
                "build_inode": lease.get("build_inode"),
                "build_filesystem_id": lease.get("build_filesystem_id"),
                "plan_sha256": expected_plan_sha256,
                "admission_identity": dict(expected_admission_identity),
            },
        }
        receipt_raw = _write_json(receipt_path, receipt_payload, replace=False, sealed=True)
        terminal = dict(lease)
        terminal.update({
            "state": ORPHANED_UNSTARTED_STATE,
            "orphan_closeout_receipt_path": str(receipt_path),
            "orphan_closeout_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
            "orphan_original_record_sha256": hashlib.sha256(original_raw).hexdigest(),
        })
        terminal_raw = (json.dumps(_seal(terminal), indent=2, sort_keys=True) + "\n").encode()
        _write_at(lease_fd, name, terminal_raw, replace=True)
        _write_at(root_fd, BUILD_MARKER,
                  (json.dumps(_seal(terminal), indent=2, sort_keys=True) + "\n").encode(), replace=True)
        os.fsync(root_fd)
        os.fsync(lease_fd)
        terminal_readback = json.loads(_read_at(lease_fd, name, limit=1024 * 1024))
        marker_readback = json.loads(_read_at(root_fd, BUILD_MARKER, limit=1024 * 1024))
        if (not _verify_seal(terminal_readback) or not _verify_seal(marker_readback)
                or terminal_readback.get("state") != ORPHANED_UNSTARTED_STATE
                or _canonical_json(terminal_readback) != _canonical_json(marker_readback)
                or _verify_orphan_closeout_receipt(terminal_readback)
                    .get("original_registry_record_sha256")
                    != hashlib.sha256(original_raw).hexdigest()):
            raise RetentionError("orphan closeout terminal state failed authenticated readback")
        return _read_json(receipt_path, sealed=True)
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if lease_fd is not None and lock_fd is not None:
            _unlock(lease_fd, lock_fd)
        _release_storage_allocation_lock(allocation_lock)

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
    _refuse_protected_path_mutation(logical_path, "cleanup")
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
    for path_text, record in R17_TEMP_ROOT_IDENTITIES.items():
        disposition = _r17_temp_root_disposition(path_text)
        if disposition is None:
            continue
        rows.append({
            **disposition,
            "action": ("PRESERVE_HISTORICAL_EVIDENCE_AND_INVENTORY"
                       if disposition["accounting_scope"] == "FG_METAL_PROJECT"
                       else "PRESERVE_OTHER_PROJECT_AND_INVENTORY"),
            "root_owner": ("FG-Metal" if disposition["accounting_scope"] == "FG_METAL_PROJECT"
                           else "Firelink"),
        })
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


def _r17_ownership_reports_match() -> bool:
    # Synthetic controller tests replace REPO with an isolated fixture. The
    # absolute temporary-root review is valid only for the checkout whose
    # source reports were audited and whose exact roots were recorded.
    if Path(REPO).resolve(strict=False) != R17_BOUND_REPO_ROOT:
        return False
    for relative, expected_sha256 in R17_OWNERSHIP_REPORT_DIGESTS.items():
        try:
            if _hash_path(REPO / relative) != expected_sha256:
                return False
        except (OSError, RetentionError):
            return False
    return True


def _r17_temp_root_disposition(path: Path | str, *, root_fd: int | None = None) -> dict[str, Any] | None:
    """Return an exact-root ownership disposition backed by the R17 reviews.

    Descendant metadata is deliberately not an admission binding: recursive
    metadata scans cannot prove an atomic snapshot of a mutable directory tree.
    Current contents are still measured below and independently by storage
    inventory; the reviewed identity is the absolute path plus root filesystem
    identity and report digests.
    """
    candidate = Path(os.path.abspath(os.fspath(path)))
    record = R17_TEMP_ROOT_IDENTITIES.get(str(candidate))
    if record is None or not _r17_ownership_reports_match():
        return None
    owns_root_fd = root_fd is None
    try:
        if root_fd is None:
            root_fd = _open_dir(candidate)
        root_info = os.fstat(root_fd)
        identity = _fs_identity(root_fd)
        path_info = candidate.lstat()
        if (not stat.S_ISDIR(path_info.st_mode)
                or (path_info.st_dev, path_info.st_ino) != (root_info.st_dev, root_info.st_ino)
                or identity != record["filesystem_identity"]):
            return None
        stats = _tree_stats(root_fd, root_info.st_dev, candidate)
        path_info_after = candidate.lstat()
        if ((path_info_after.st_dev, path_info_after.st_ino)
                != (root_info.st_dev, root_info.st_ino)
                or _fs_identity(root_fd) != identity):
            return None
        return {
            "path": str(candidate),
            "classification": record["classification"],
            "accounting_scope": record["accounting_scope"],
            "reason": record["reason"],
            "filesystem_identity": identity,
            "root_mtime_ns": root_info.st_mtime_ns,
            "root_ctime_ns": root_info.st_ctime_ns,
            "logical_bytes": stats["logical_bytes"],
            "allocated_unique_inode_bytes": stats["allocated_unique_inode_bytes"],
            "best_recoverable_bytes_estimate": stats["best_recoverable_bytes_estimate"],
            "file_count": stats["file_count"],
            "ownership_review_sha256": R17_OWNERSHIP_REPORT_DIGESTS[
                "R6-A-R17-RETENTION-ROOT-OWNERSHIP.md"],
            "scope_accounting_review_sha256": R17_OWNERSHIP_REPORT_DIGESTS[
                "R6-A-R17-SCOPE-ACCOUNTING-AUDIT.md"],
            "reconciliation_sha256": R17_OWNERSHIP_REPORT_DIGESTS[
                "R6-A-R17-SCOPE-RETENTION-RECONCILIATION.md"],
        }
    except (OSError, RetentionError, ValueError):
        return None
    finally:
        if owns_root_fd and root_fd is not None:
            os.close(root_fd)


def _all_root_candidates() -> list[Path]:
    candidates = [path for path in LEGACY_BUILD_ROOTS if path.exists() or path.is_symlink()]
    verified_r17_roots: set[Path] = set()
    if Path(REPO).resolve(strict=False) == R17_BOUND_REPO_ROOT:
        for path_text in R17_TEMP_ROOT_IDENTITIES:
            path = Path(path_text)
            if path.exists() or path.is_symlink():
                candidates.append(path)
                if _r17_temp_root_disposition(path) is not None:
                    verified_r17_roots.add(path)
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
            if any(base == root or root in base.parents for root in verified_r17_roots):
                dirnames[:] = []
                continue
            if _is_wine_prefix_tree(base):
                dirnames[:] = []
                continue
            if base in excluded:
                dirnames[:] = []
                continue
            retained: list[str] = []
            for name in dirnames:
                child = base / name
                if child in verified_r17_roots:
                    continue
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


def _r17_missing_root_rows() -> list[dict[str, str]]:
    """Report every absent audited root while this checkout owns the R17 binding."""
    if Path(REPO).resolve(strict=False) != R17_BOUND_REPO_ROOT:
        return []
    missing: list[dict[str, str]] = []
    for path_text in R17_TEMP_ROOT_IDENTITIES:
        path = Path(path_text)
        if not path.exists() and not path.is_symlink():
            missing.append({
                "path": path_text,
                "reason": "R17-reviewed ownership root is missing; exact-root identity cannot be verified",
            })
    return missing

def _snapshot(*, protected_roots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if protected_roots is None:
        protected_roots = _verify_protected_evidence_roots()
    protected_by_path = {os.path.abspath(row["path"]): row for row in protected_roots}
    active, preserved, completed, failed, orphaned, unknown, nonlarge = [], [], [], [], [], [], []
    by_path: dict[str, dict[str, Any]] = {}
    unknown.extend(_r17_missing_root_rows())
    for lease_path in _lease_files():
        try:
            lease = _load_lease(lease_path.stem)
        except (OSError, RetentionError) as error:
            unknown.append({"path": str(lease_path), "reason": f"invalid lease: {error}"})
            continue
        if lease.get("build_id") != lease_path.stem or not isinstance(lease.get("path"), str):
            unknown.append({"path": str(lease_path), "reason": "lease identity mismatch"})
            continue
        lease_absolute = os.path.abspath(lease["path"])
        by_path[lease_absolute] = lease
        if _protected_paths_overlap(lease_absolute):
            unknown.append({"path": lease_absolute,
                            "reason": "active or retained build lease overlaps protected parent evidence"})
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
        elif lease.get("state") == ORPHANED_UNSTARTED_STATE:
            try:
                _verify_orphan_closeout_receipt(lease)
                normal_receipt = RECEIPT_ROOT / f"{lease_path.stem}.json"
                if (normal_receipt.exists() or normal_receipt.is_symlink()
                        or lease.get("build_manifest_id") is not None
                        or lease.get("build_manifest_sha256") is not None):
                    raise RetentionError("orphan terminal lease has conflicting completion evidence")
                orphaned.append(row)
            except (OSError, RetentionError, ValueError, TypeError) as error:
                unknown.append({"path": lease["path"],
                                "reason": f"orphan closeout receipt invalid: {error}"})
        else: unknown.append({"path": lease["path"], "reason": f"unrecognized state {lease.get('state')}"})
    for path in _all_root_candidates():
        absolute_path = os.path.abspath(path)
        if absolute_path in protected_by_path:
            continue
        if (absolute_path not in by_path
                and _r17_temp_root_disposition(path) is not None):
            # This exact reviewed root is retained and inventoried; it is not
            # an active/retired BuildLease and receives no mutation authority.
            continue
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
                elif lease.get("state") == ORPHANED_UNSTARTED_STATE:
                    if (_canonical_json(marker) != _canonical_json(lease)
                            or sorted(os.listdir(root_fd)) != [BUILD_MARKER]
                            or (path / BUILD_SESSION_WORKSPACE_NAME).exists()
                            or (path / BUILD_SESSION_WORKSPACE_IMAGE_NAME).exists()
                            or os.path.ismount(path / BUILD_SESSION_WORKSPACE_NAME)):
                        unknown.append({"path": str(path),
                                        "reason": "orphan terminal root acquired workspace or changed marker state"})
            finally:
                os.close(root_fd)
        except (OSError, RetentionError, json.JSONDecodeError) as error:
            unknown.append({"path": str(path), "reason": f"invalid root marker: {error}"})
    for key, lease in by_path.items():
        if lease.get("retired_at_unix") is None and not Path(key).exists():
            unknown.append({"path": key, "reason": "registered build root is missing without a retirement receipt"})
    return {"active": active, "preserved_debug": preserved, "completed_pending_retirement": completed,
            "failed_or_retiring": failed, "orphaned_unstarted_retired": orphaned,
            "unknown": unknown, "classified_nonlarge_roots": nonlarge,
            "protected_in_place_evidence": [dict(row) for row in protected_roots],
            "classified_non_retention_roots": _classified_non_retention_tmp_roots(),
            "active_build_count": len(active), "preserved_build_count": len(preserved),
            "retained_build_count": len(active) + len(preserved),
            "active_dxvk_count": sum(row.get("kind") == "dxvk" for row in active),
            "active_moltenvk_count": sum(row.get("kind") == "moltenvk" for row in active)}

def capacity_blockers(state: Mapping[str, Any] | None = None) -> list[str]:
    if state is None:
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
                              "accounting_scope": row.get("accounting_scope"),
                              "ownership_review_sha256": row.get("ownership_review_sha256"),
                              "scope_accounting_review_sha256": row.get("scope_accounting_review_sha256"),
                              "reconciliation_sha256": row.get("reconciliation_sha256"),
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
    protected_base_fields = (
        "evidence_id", "path", "parent_receipt_sha256", "final_review_sha256",
        "review_supplement_sha256", "build_index_sha256", "build_directory_manifest_sha256",
        "root_inventory_sha256", "file_count", "logical_bytes", "accounting_policy",
        "mutation_policy", "relocation_allowed", "cleanup_allowed", "cas_mutation_allowed")
    protected_extension_fields = (
        "tree_content_sha256", "critical_file_relative_path", "critical_file_sha256",
        "authority_metadata_path", "authority_metadata_sha256", "storage_credit",
        "reservation_credit", "free_space_exclusion", "project_scope_exclusion",
        "build_lease_exemption")
    protected = sorted(({
        **{key: row.get(key) for key in protected_base_fields},
        **{key: row[key] for key in protected_extension_fields if key in row},
    } for row in state.get("protected_in_place_evidence", [])), key=lambda row: str(row["path"]))
    material = {"build_root_identity": _root_identity(BUILD_ROOT, create=True),
                "retained_builds": sorted(retained, key=lambda row: str(row["path"])),
                "unknown_roots": unknown, "excluded_roots": excluded_roots, "small_roots": nonlarge,
                "protected_in_place_evidence": protected}
    return {"sha256": hashlib.sha256(_canonical_json(material)).hexdigest(), **material}

def status_snapshot() -> dict[str, Any]:
    protected_before = _verify_protected_evidence_roots()
    reconciliation_error = None
    lease_fd, lock_fd = _lock()
    try:
        try:
            _retire_pending_locked(protected_roots_verified=True)
        except BaseException as error:
            reconciliation_error = repr(error)
        # Keep BUILD_LOCK through root enumeration and its final fingerprint.
        # BuildLease reserve/complete/retire operations use this same lock, so a
        # supported build writer cannot replace a prefix-pruned path mid-scan.
        protected_after = _verify_protected_evidence_roots()
        if _canonical_json(_protected_identity_projection(protected_before)) != _canonical_json(
                _protected_identity_projection(protected_after)):
            raise RetentionError("protected BUILD-1 evidence changed during retention reconciliation")
        state = _snapshot(protected_roots=protected_after)
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
        state["protected_evidence_root_count"] = len(state.get("protected_in_place_evidence", []))
        state["active_lease_markers"] = len(state.get("active", []))
        state["retention_fingerprint"] = _retention_fingerprint(state)
        protected_final = _verify_protected_evidence_roots()
        if _canonical_json(_protected_identity_projection(protected_after)) != _canonical_json(
                _protected_identity_projection(protected_final)):
            raise RetentionError("protected BUILD-1 evidence changed while finalizing retention status")
        state["gate_blockers"] = capacity_blockers(state)
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

    # 2. Clean up intermediate staging and scan products directory for undeclared binary escapes
    xcframework_staging = build / "Build/Products/XCFrameworkStaging"
    if xcframework_staging.is_dir():
        shutil.rmtree(xcframework_staging, ignore_errors=True)
    ext_staging = build / "MoltenVKSource/External/build/Intermediates/XCFrameworkStaging"
    if ext_staging.is_dir():
        shutil.rmtree(ext_staging, ignore_errors=True)

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
    lease_path = lease.get("path")
    if isinstance(lease_path, str):
        _refuse_protected_path_mutation(lease_path, "retirement")
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

        allow_live_host_v3_descendants = _session_has_verified_host_v3_quiescence(lease)
        if not quarantine_exists:
            if not allow_live_host_v3_descendants:
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
                allowed_roots = [Path(value) for value in lease.get("allowed_external_symlink_roots", [])]
                if path is not None:
                    allowed_roots.append(path.resolve(strict=False))
                current_stats = _tree_stats(target_fd, opened.st_dev, parent / quarantine,
                                            allowed_symlink_roots=allowed_roots)
                if current_stats != stats:
                    raise RetentionError("quarantined build tree changed after its pre-retirement inventory")
        finally:
            os.close(target_fd)

        if not allow_live_host_v3_descendants:
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


def _discard_unaccepted_host_v3_completion(lease: Mapping[str, Any]) -> None:
    """Remove only this uncommitted v3 manifest/evidence namespace after rejection."""
    build_id = str(lease.get("build_id", ""))
    if (not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", build_id)
            or lease.get("state") not in {"ACTIVE", "FAILED"}
            or lease.get("build_manifest_id") is not None):
        raise RetentionError("cannot discard completion evidence after a manifest was committed")
    parent_fd = _open_dir(MANIFEST_ROOT)
    try:
        manifest_name = f"{build_id}.json"
        try:
            manifest_info = os.stat(manifest_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            manifest_info = None
        if manifest_info is not None:
            if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_nlink != 1:
                raise RetentionError("unaccepted v3 manifest path is not one regular file")
            os.unlink(manifest_name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        evidence_name = f"{build_id}.evidence"
        try:
            evidence_info = os.stat(evidence_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            evidence_info = None
        if evidence_info is not None:
            if not stat.S_ISDIR(evidence_info.st_mode):
                raise RetentionError("unaccepted v3 evidence path is not a real directory")
            _remove_verified_tree_at(
                parent_fd, evidence_name, expected_device=evidence_info.st_dev,
                expected_inode=evidence_info.st_ino, logical_path=MANIFEST_ROOT / evidence_name,
                expected_filesystem_id=getattr(os.fstatvfs(parent_fd), "f_fsid", None))
    finally:
        os.close(parent_fd)


def _record_unaccepted_session_failure(lease: dict[str, Any], reason: str) -> None:
    """Write controller-only failure evidence without reading writable workspace outputs."""
    build_id = str(lease.get("build_id", ""))
    root = MANIFEST_ROOT / f"{build_id}.failure"
    root_fd = _open_dir(root, create=True)
    os.close(root_fd)
    message = reason[:MAX_FAILURE_ERROR_BYTES]
    payload = {"schema_version": 1, "build_id": build_id,
               "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
               "executor_version": BUILD_SESSION_SCHEMA_VERSION,
               "acceptance_state": lease.get("session_v3", {}).get("acceptance_state"),
               "outputs_accepted": False,
               "failure_reason": message,
               "failure_reason_sha256": hashlib.sha256(
                   reason.encode("utf-8", "replace")).hexdigest(),
               "failed_at_unix": lease.get("failed_at_unix", time.time()),
               "failure_evidence_policy": "controller-only; no output copied from writable or uncertain image"}
    manifest_path = root / "failure-manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise RetentionError("unaccepted v3 failure manifest already exists")
    _write_json(manifest_path, payload, replace=False)
    files = _inventory_evidence(root)
    raw = _read_file_descriptor(manifest_path)
    lease.update({"failure_evidence_root": str(root),
                  "failure_evidence_path": str(manifest_path),
                  "failure_evidence_sha256": hashlib.sha256(raw).hexdigest(),
                  "failure_evidence_files": files,
                  "failure_evidence_index_sha256": hashlib.sha256(_canonical_json(files)).hexdigest()})
    _store_lease(lease)


def _session_has_verified_host_v3_quiescence(lease: Mapping[str, Any]) -> bool:
    session = lease.get("session_v3")
    return bool(isinstance(session, Mapping)
                and session.get("executor_mode") == BUILD_SESSION_EXECUTOR_MODE
                and session.get("executor_version") == BUILD_SESSION_SCHEMA_VERSION
                and session.get("state") == "ACCEPTED"
                and session.get("write_quiescence_state") == "WRITABLE_IMAGE_DETACHED_READONLY_EVIDENCE_HASHED"
                and session.get("acceptance_state") == "HOST_BUILDLEASE_V3_WRITER_QUIESCED"
                and lease.get("state") in {"COMPLETED", "RETIRING"}
                and isinstance(lease.get("build_manifest_id"), str)
                and isinstance(lease.get("build_manifest_sha256"), str))

def _recover_failed_session_workspace_detach(lease: dict[str, Any]) -> None:
    """Retry an exact v3 workspace detach before cleanup, leaving uncertainty blocking."""
    session = lease.get("session_v3")
    if not isinstance(session, dict):
        return
    detach_uncertain = lease.get("workspace_detach_uncertain") is True
    candidates = (session.get("readonly_mount_identity"),
                  session.get("readonly_detached_identity"),
                  session.get("detached_workspace_identity"),
                  session.get("workspace_mount_identity"))
    if not detach_uncertain and not any(
            isinstance(item, dict) and item.get("attached") is True for item in candidates):
        return
    if (session.get("process_containment_uncertain") is True
            or session.get("output_capture_uncertain") is True):
        raise RetentionError("failed BuildLease still has uncertain process or output containment")
    path = Path(str(lease.get("path", "")))
    _refuse_protected_path_mutation(path, "failed session workspace detach recovery")
    _require_no_live_process(path)
    try:
        readonly_mount = session.get("readonly_mount_identity")
        readonly_detached = session.get("readonly_detached_identity")
        detached_workspace = session.get("detached_workspace_identity")
        writable_mount = session.get("workspace_mount_identity")
        if isinstance(readonly_mount, dict) and readonly_mount.get("attached") is True:
            if _session_image_row(path) is None:
                detached = _verify_session_workspace_detached(path, readonly_mount)
            else:
                detached = _detach_session_workspace_readonly(path, readonly_mount)
        elif isinstance(readonly_detached, dict):
            detached = _verify_session_workspace_detached(path, readonly_detached)
        elif isinstance(detached_workspace, dict) and _session_image_row(path) is not None:
            row = _session_image_row(path)
            if row is None or row.get("writeable") is not False:
                raise RetentionError("failed BuildLease has an unknown or writable reattached image")
            _best_effort_detach_unverified_readonly_image(path)
            detached = _verify_session_workspace_detached(path, detached_workspace)
        elif isinstance(detached_workspace, dict):
            detached = _verify_session_workspace_detached(path, detached_workspace)
        elif isinstance(writable_mount, dict) and writable_mount.get("attached") is True:
            detached = _detach_session_workspace(path, writable_mount)
        elif _session_image_row(path) is not None:
            raise RetentionError("failed BuildLease has an attached image without a recoverable identity")
        else:
            # Crash may have happened before image creation/identity persistence. No attached
            # filesystem remains, and this rejected lease contains no acceptable outputs.
            detached = {"attached": False, "image_path": str(path / BUILD_SESSION_WORKSPACE_IMAGE_NAME),
                        "mountpoint": str(path / BUILD_SESSION_WORKSPACE_NAME),
                        "detached_at_unix": time.time(), "recovery_identity_unavailable": True}
    except BaseException as error:
        lease["workspace_detach_uncertain"] = True
        lease["workspace_detach_failure"] = repr(error)[:MAX_FAILURE_ERROR_BYTES]
        _store_lease(lease)
        raise RetentionError(
            f"failed BuildLease workspace remains blocking until detach can be verified: {error}") from error
    session["workspace_mount_identity"] = detached
    session["workspace_detached_at_unix"] = detached.get("detached_at_unix")
    if isinstance(session.get("readonly_mount_identity"), dict):
        session["readonly_mount_identity"] = dict(detached)
        session["readonly_detached_identity"] = dict(detached)
    session["write_quiescence_state"] = "UNKNOWN_OR_FAILED"
    session["acceptance_state"] = "REJECTED_NO_OUTPUT_PROMOTION"
    lease["session_v3"] = session
    lease.pop("workspace_detach_uncertain", None)
    lease.pop("workspace_detach_failure", None)
    _store_lease(lease)
    root_fd = _open_dir(path)
    try:
        marker = _read_marker(root_fd)
        if marker.get("build_id") != lease.get("build_id") or marker.get("state") != "FAILED":
            raise RetentionError("failed BuildLease marker changed during workspace detach recovery")
        marker.update(lease)
        _write_marker(root_fd, marker)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _reject_interrupted_host_v3_session(lease: dict[str, Any]) -> None:
    """Convert an orphaned active session to durable failure; never resume or promote it."""
    session = lease.get("session_v3")
    if (lease.get("state") != "ACTIVE" or not isinstance(session, dict)
            or lease.get("build_manifest_id") is not None):
        raise RetentionError("interrupted BuildLease recovery found a non-active or already accepted session")
    reason = "controller disappeared before a committed host BuildLease v3 completion"
    failed_at = time.time()
    session.update({"state": "FAILED", "acceptance_state": "REJECTED_CONTROLLER_CRASH_UNKNOWN",
                    "write_quiescence_state": "UNKNOWN_OR_FAILED", "failed_at_unix": failed_at,
                    "failure_reason": reason,
                    "failure_reason_sha256": hashlib.sha256(reason.encode()).hexdigest()})
    lease.update({"state": "FAILED", "session_v3": session, "failed_at_unix": failed_at,
                  "failed_command": ["host-buildlease-v3", "controller-crash-recovery"],
                  "return_code": -1, "workspace_detach_uncertain": True})
    _store_lease(lease)
    path = Path(str(lease.get("path", "")))
    root_fd = _open_dir(path)
    try:
        marker = _read_marker(root_fd)
        if marker.get("build_id") != lease.get("build_id") or marker.get("state") != "ACTIVE":
            raise RetentionError("interrupted v3 session marker changed during crash recovery")
        marker.update(lease)
        marker["state"] = "FAILED"
        _write_marker(root_fd, marker)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    session_dir = path / BUILD_SESSION_DIRECTORY
    for name in (BUILD_SESSION_TERMINAL, "session-terminal.pending.json"):
        terminal_path = session_dir / name
        try:
            info = terminal_path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RetentionError("interrupted v3 terminal path is not one regular controller receipt")
        terminal_path.unlink()
    _discard_unaccepted_host_v3_completion(lease)
    _record_unaccepted_session_failure(lease, reason)


def _retire_pending_locked(*, protected_roots_verified: bool = False) -> list[dict[str, Any]]:
    if not protected_roots_verified:
        _verify_protected_evidence_roots()
    receipts = []
    for path in _lease_files():
        lease = _load_lease(path.stem)
        if lease.get("retired_at_unix") is not None:
            continue
        if lease.get("state") == "COMPLETED":
            receipts.append(_retire(lease, reason="cap-preflight-retire-completed"))
        elif lease.get("state") == "FAILED":
            _recover_failed_session_workspace_detach(lease)
            receipts.append(_retire(lease, reason="cap-preflight-retire-failed"))
        elif lease.get("state") == "ACTIVE" and isinstance(lease.get("session_v3"), dict):
            session = lease["session_v3"]
            if (session.get("state") == "RESERVED"
                    and lease.get("session_started") is not True):
                # A never-started reservation has no recoverable in-memory
                # capability. Leave it charged and blocking until an explicit,
                # reviewed orphan closeout proves it safe to terminalize.
                continue
            _reject_interrupted_host_v3_session(lease)
            lease = _load_lease(path.stem)
            _recover_failed_session_workspace_detach(lease)
            receipts.append(_retire(lease, reason="crashed-host-v3-session-rejected"))
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
    """Admit an existing root under shared capacity and lease serialization."""
    from storage_policy import (_acquire_storage_allocation_lock,
                                _release_storage_allocation_lock,
                                conservative_project_usage_bytes, disk_free_bytes,
                                measure_project_usage, projected_storage_admission)

    allocation_lock = _acquire_storage_allocation_lock()
    try:
        gate_blockers = _gate_blockers(active_experiment_id=owner_experiment)
        if gate_blockers:
            raise RetentionError("Gate 0 blocks legacy BuildLease adoption: "
                                 + "; ".join(gate_blockers))
        project_usage = measure_project_usage()
        admission = projected_storage_admission(
            current_free_bytes=disk_free_bytes(REPO),
            current_scoped_bytes=conservative_project_usage_bytes(project_usage),
            peak_additional_bytes=0,
        )
        if not admission["admitted"]:
            raise RetentionError("shared capacity blocks legacy BuildLease adoption: "
                                 + "; ".join(admission["blockers"]))
        return _adopt_legacy_under_storage_allocation_lock(
            path, kind=kind, owner_experiment=owner_experiment, purpose=purpose,
            expected_expiry_unix=expected_expiry_unix,
            preserve_debug_reason=preserve_debug_reason)
    finally:
        _release_storage_allocation_lock(allocation_lock)


def _adopt_legacy_under_storage_allocation_lock(
        path: Path, *, kind: str, owner_experiment: str, purpose: str,
        expected_expiry_unix: float | None = None,
        preserve_debug_reason: str | None = None) -> dict[str, Any]:
    path = Path(os.path.abspath(path))
    _refuse_protected_path_mutation(path, "legacy adoption")
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


def _darwin_user_dirs() -> list[Path]:
    dirs: list[Path] = []
    for var in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            out = subprocess.check_output(["getconf", var], text=True).strip()
            if out:
                p = Path(out).resolve()
                if p.is_dir() and p not in dirs:
                    dirs.append(p)
        except (OSError, subprocess.SubprocessError):
            pass
    return dirs


def _sandbox_profile_text(build_root: Path) -> str:
    root = build_root.resolve(strict=True)

    def sbpl_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    profile = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow sysctl-read)",
        "(allow system-info)",
        "(allow file-read*)",
        '(allow file-write* (literal "/dev/null") (literal "/private/dev/null") (literal "/dev/zero") (literal "/private/dev/zero"))',
        f"(allow file-write* (subpath {sbpl_quote(str(root))}))",
    ]
    try:
        if str(root).startswith("/private/tmp"):
            alt_tmp = "/tmp" + str(root)[len("/private/tmp"):]
            profile.append(f"(allow file-write* (subpath {sbpl_quote(alt_tmp)}))")
    except Exception:
        pass

    for user_dir in _darwin_user_dirs():
        profile.append(f"(allow file-write* (subpath {sbpl_quote(str(user_dir))}))")
        s = str(user_dir)
        if s.startswith("/private/var/folders"):
            profile.append(f"(allow file-write* (subpath {sbpl_quote('/var/folders' + s[len('/private/var/folders'):])}))")

    mach_services = [
        "com.apple.system.opendirectoryd.libinfo",
        "com.apple.system.opendirectoryd.membership",
        "com.apple.FileCoordination",
    ]
    for service in mach_services:
        profile.append(f"(allow mach-lookup (global-name {sbpl_quote(service)}))")

    return "\n".join(profile) + "\n"


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


BUILD_SESSION_SCHEMA_VERSION = 3
BUILD_SESSION_DIRECTORY = ".build-session-v3"
BUILD_SESSION_PLAN = "session-plan.json"
BUILD_SESSION_TERMINAL = "session-terminal.json"
BUILD_SESSION_TERMINAL_HEADROOM_BYTES = 1024 * 1024
BUILD_SESSION_OUTPUT_DRAIN_SECONDS = 5.0
BUILD_SESSION_PROCESS_TERM_SECONDS = 2.0
BUILD_SESSION_PROCESS_QUIESCE_SECONDS = 8.0
BUILD_SESSION_PROCESS_EVIDENCE_SAMPLE_PIDS = 256
BUILD_SESSION_WORKSPACE_NAME = "workspace"
BUILD_SESSION_WORKSPACE_IMAGE_NAME = ".build-session-v3-workspace.sparseimage"
BUILD_SESSION_CONTROLLER_HEADROOM_BYTES = 32 * 1024 * 1024
BUILD_SESSION_TOTAL_LOG_LIMIT_BYTES = 8 * 1024 * 1024
BUILD_SESSION_PLAN_LIMIT_BYTES = 256 * 1024
BUILD_SESSION_MAX_STEPS = 32
BUILD_SESSION_CONTROLLER_EVIDENCE_LIMIT_BYTES = 16 * 1024 * 1024
BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS = BUILD_SESSION_MAX_STEPS * 4 + 16
BUILD_SESSION_VM_BACKEND_IMPLEMENTED = False
BUILD_SESSION_HOST_V3_POLICY_ACCEPTED = True
BUILD_SESSION_HOST_V3_TEST_BYPASS = False
BUILD_SESSION_EXECUTOR_MODE = "HOST_BUILDLEASE_V3"
BUILD_SESSION_HOST_V3_POLICY_AMENDMENT_PATH = (
    REPO / "R6-BUILDLEASE-V3-HOST-EXECUTION-POLICY-AMENDMENT.md")
BUILD_SESSION_HOST_V3_POLICY_AMENDMENT_SHA256 = (
    "83f57849e4ec634bfb70c6f9accb0ad5a813ebb8bbb2aaac5cea84c76049b28c")
BUILD_SESSION_HOST_V3_LEGACY_POLICY_AMENDMENT_SHA256 = (
    "ea434e13293726fc8c4990fd9df56ede5fd3e53b38a8f8576e6fde1f1ed23a92")
BUILD_SESSION_HOST_V3_DUAL_GRAPH_POLICY_AMENDMENT_SHA256 = (
    "83f57849e4ec634bfb70c6f9accb0ad5a813ebb8bbb2aaac5cea84c76049b28c")
BUILD_SESSION_DUAL_GRAPH_CAPABILITY = 1
BUILD_SESSION_DUAL_GRAPH_ROLES = ("WINDOWS_PRODUCT", "NATIVE_VERIFICATION")
BUILD_SESSION_DUAL_GRAPH_IDS = ("windows-product-r6a", "native-verification-r6a")
BUILD_SESSION_DUAL_GRAPH_PATH = os.pathsep.join((
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin",
    "/opt/homebrew/Cellar/glslang/16.6.0/bin",
    "/opt/homebrew/Cellar/pkgconf/3.0.10/bin",
    "/opt/homebrew/Cellar/ninja/1.13.2/bin",
    "/opt/homebrew/Cellar/meson/1.12.1/bin",
))
BUILD_SESSION_DUAL_GRAPH_PATH_TOOL_RECEIPTS = {
    "meson": "meson",
    "ninja": "ninja",
    "git": "git",
    "x86_64-w64-mingw32-gcc": "mingw_cc",
    "x86_64-w64-mingw32-g++": "mingw_cxx",
    "x86_64-w64-mingw32-ar": "mingw_ar",
    "x86_64-w64-mingw32-windres": "mingw_windres",
    "x86_64-w64-mingw32-strip": "mingw_strip",
    "x86_64-w64-mingw32-ld": "mingw_linker",
    "x86_64-w64-mingw32-as": "mingw_assembler",
    "python3": "native_python3",
    "touch": "native_touch",
    "false": "native_false",
    "clang": "native_cc",
    "clang++": "native_cxx",
    "ld": "native_linker",
    "ar": "native_ar",
    "ranlib": "native_ranlib",
    "glslang": "native_glslang",
    "glslangValidator": "native_glslang",
    "pkgconf": "native_pkg_config",
    "pkg-config": "native_pkg_config",
}
BUILD_SESSION_DUAL_GRAPH_PATH_ABSENT_EXECUTABLES = ("edid-decode", "rc")
BUILD_SESSION_DUAL_GRAPH_DIRECTORIES = {
    "windows-product-r6a": "build.win64-r6a",
    "native-verification-r6a": "build.native-r6a",
}
BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE = Path(
    "stage/native-verification/runtime")
BUILD_SESSION_DUAL_GRAPH_WINDOWS_PE_STAGE_RELATIVE = Path(
    "stage/windows-pe/present-accounting-production-path.exe")
BUILD_SESSION_DUAL_GRAPH_WINDOWS_STAGE_ACTION = "STAGE_DECLARED_WINDOWS_PE_OUTPUT"
BUILD_SESSION_DUAL_GRAPH_NATIVE_STAGE_ACTION = "STAGE_NATIVE_SDL2_RUNTIME_CLOSURE"
BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES = {
    "SDL2_COMPAT": "libSDL2-2.0.0.dylib",
    "SDL3": "libSDL3.0.dylib",
}
BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS = {
    "SDL2_COMPAT": {
        "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347",
    },
    "SDL3": {
        "path": "/opt/homebrew/Cellar/sdl3/3.4.18/lib/libSDL3.0.dylib",
        "sha256": "5fdf17e340d9123e3d57c60c4c3c41777d04fe233d09bcd00874de09281746ab",
    },
}
BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_BASENAME = "libSDL2-2.0.0.dylib"
BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_RESOLUTION_CLAIM = (
    "STATIC_LOAD_COMMANDS_AND_STAGED_HASHES_BOUND; DYLD_RESOLUTION_UNPROVEN")
BUILD_SESSION_DUAL_GRAPH_TARGETS = {
    "DEFAULT_PRODUCTION_OUTPUTS": None,
    "R6A_PRODUCTION_PATH_PE": "present-accounting-production-path",
    "R6A_PRODUCTION_PATH_MACHO": "present-accounting-production-path",
    "R6A_PRESENT_ACCOUNTING_UNIT": "present-accounting-unit",
    "R6A_PRESENT_ACCOUNTING_MODEL": "present-accounting-model",
}
BUILD_SESSION_DUAL_GRAPH_NATIVE_TEST_BINDINGS = {
    "r6a-production-path-macho": {
        "test_class": "PRODUCTION_PATH",
        "producer_target": "R6A_PRODUCTION_PATH_MACHO",
        "requires_sdl2_runtime": True,
    },
    "r6a-present-accounting-unit": {
        "test_class": "UNIT",
        "producer_target": "R6A_PRESENT_ACCOUNTING_UNIT",
        "requires_sdl2_runtime": False,
    },
    "r6a-present-accounting-model": {
        "test_class": "MODEL",
        "producer_target": "R6A_PRESENT_ACCOUNTING_MODEL",
        "requires_sdl2_runtime": False,
    },
}
BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS = {
    "WINDOWS_PRODUCT": {
        "meson": "meson",
        "ninja": "ninja",
        "git": "git",
        "mingw_cc": "x86_64-w64-mingw32-gcc",
        "mingw_cxx": "x86_64-w64-mingw32-g++",
        "mingw_ar": "x86_64-w64-mingw32-ar",
        "mingw_windres": "x86_64-w64-mingw32-windres",
        "mingw_strip": "x86_64-w64-mingw32-strip",
        "mingw_linker": "x86_64-w64-mingw32-ld",
        "mingw_assembler": "x86_64-w64-mingw32-as",
    },
    "NATIVE_VERIFICATION": {
        "meson": "meson",
        "ninja": "ninja",
        "git": "git",
        "meson_python": "python3.14",
        "native_python3": "python3",
        "native_cc": "clang",
        "native_cxx": "clang++",
        "native_linker": "ld",
        "native_ar": "ar",
        "native_ranlib": "ranlib",
        "native_touch": "touch",
        "native_false": "false",
        "native_glslang": "glslang",
        "native_pkg_config": "pkgconf",
    },
}
BUILD_SESSION_DUAL_GRAPH_TOOL_PINS = {
    "meson": {"path": "/opt/homebrew/Cellar/meson/1.12.1/bin/meson",
              "sha256": "0a0a2edc05d1a8ae7c84885f00dc5746ee1a57cc2c746e2b05a3b7dfbca2ed7d"},
    "ninja": {"path": "/opt/homebrew/Cellar/ninja/1.13.2/bin/ninja",
              "sha256": "1474a6ef89624613732e0fc9e6d9fe5338b8885916cf64ab8c1c6097123f3ef6"},
    "git": {"path": "/usr/bin/git",
            "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "sandbox": {"path": "/usr/bin/sandbox-exec",
                "sha256": "58839ef01b4eef8aac0d2aa8f9d1c074ae45aafe3533965b030672450064acc8"},
    "mingw_cc": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-gcc",
                 "sha256": "79708a2ac50166252c813e9f9cd38ef8c03aa82d68949aabd97cad53224fae80"},
    "mingw_cxx": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-g++",
                  "sha256": "9ee663107fe2bc1acfecde511fb1936041bade5764bc51feeaed74d0b6c55b80"},
    "mingw_ar": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-ar",
                 "sha256": "763da0f341169ca08d94f66107ae00c64d875ba6755cfa9743caae13b188fb2b"},
    "mingw_windres": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-windres",
                      "sha256": "bc9c851e741de962d8e72c8c166a2993296937a66532531f47417ba485d16d8a"},
    "mingw_strip": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-strip",
                    "sha256": "4b17d36f67fc816d74ae64abc332ee6519909083486b07858568a54542266edc"},
    "mingw_linker": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-ld",
                     "sha256": "7548a92bc617896817361c7c6aac807e406bc07db213126a387bce2604090935"},
    "mingw_assembler": {"path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64/bin/x86_64-w64-mingw32-as",
                        "sha256": "90666077066960ddc926996a42691fa4443ee0ac3017ae501efc6b7b0f08015b"},
    "meson_python": {"path": "/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/bin/python3.14",
                     "sha256": "1b5cb11d163e5ba34e2a05fc533c0ed89574d4425f9749a9d89405f651980e0d"},
    "native_cc": {"path": "/usr/bin/clang",
                  "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_cxx": {"path": "/usr/bin/clang++",
                   "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_linker": {"path": "/usr/bin/ld",
                      "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_ar": {"path": "/usr/bin/ar",
                  "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_ranlib": {"path": "/usr/bin/ranlib",
                      "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_python3": {"path": "/usr/bin/python3",
                       "sha256": "34129c71a01a74f7f3b2443521519b2e5447553fa187f5fcafaaf8c42cc192e2"},
    "native_touch": {"path": "/usr/bin/touch",
                     "sha256": "83567bc0eb633230f8fb4d5c6ce9ec7a2084d5eeecd53a261d7932f38489d83b"},
    "native_false": {"path": "/usr/bin/false",
                     "sha256": "ccdd13063a974daa3ffcb707ac70104fb642eaf4aab3b621e816f36e2e8b59ab"},
    "native_glslang": {"path": "/opt/homebrew/Cellar/glslang/16.6.0/bin/glslang",
                       "sha256": "6d4f9f2ba82d0762fedbb9214447318342ebf10781c4f91d07cafab94989fb36"},
    "native_pkg_config": {"path": "/opt/homebrew/Cellar/pkgconf/3.0.10/bin/pkgconf",
                          "sha256": "9ab5b4565b097dc4c58515dbdf131adb67386ece44c48488c94670ca478b5ede"},
}
BUILD_SESSION_DUAL_GRAPH_MESON_PYTHON_SHEBANG = "/opt/homebrew/opt/python@3.14/bin/python3.14"
BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS = {
    "MINGW_TOOLCHAIN_TREE": {"kind": "tree_internal_symlinks",
                             "path": "/opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64",
                             "sha256": "b40aeaee50bb549fa0136fb22736d61ca512c8b7013465732f7c12eade5f93bd",
                             "file_count": 4485},
    "MESON_PYTHON_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/Python",
                              "sha256": "ea96cfe3ee0190f17c4f907a321f8aadbca35eee47285e329ad243347be6a8b9"},
    "MESON_PACKAGE_TREE": {"kind": "tree", "path": "/opt/homebrew/Cellar/meson/1.12.1/lib/python3.14/site-packages/mesonbuild",
                            "sha256": "f8fd8b940647492cf5978d0c99c8dd65f3008970ee55615942d135ab278e39ef"},
    "MESON_DISTRIBUTION_METADATA": {"kind": "tree", "path": "/opt/homebrew/Cellar/meson/1.12.1/lib/python3.14/site-packages/meson-1.12.1.dist-info",
                                    "sha256": "faf546f79259b17f5b8e8ada54f4a50888c27988f989003440865b1227bfed91"},
    "SDL2_PKGCONFIG": {"kind": "file", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2-compat.pc",
                        "sha256": "f69e0edd247c23783d339e41d1227751a2a999542fe17b382417e74cbf832caa"},
    "SDL2_PKGCONFIG_CELLAR_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2.pc",
        "target": "sdl2-compat.pc",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2-compat.pc",
        "sha256": "f69e0edd247c23783d339e41d1227751a2a999542fe17b382417e74cbf832caa"},
    "SDL2_PKGCONFIG_GLOBAL_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/lib/pkgconfig/sdl2.pc",
        "target": "../../Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2.pc",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2-compat.pc",
        "sha256": "f69e0edd247c23783d339e41d1227751a2a999542fe17b382417e74cbf832caa"},
    "SDL2_COMPAT_PKGCONFIG_GLOBAL_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/lib/pkgconfig/sdl2-compat.pc",
        "target": "../../Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2-compat.pc",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2-compat.pc",
        "sha256": "f69e0edd247c23783d339e41d1227751a2a999542fe17b382417e74cbf832caa"},
    "SDL2_COMPAT_LIBRARY": {
        "kind": "file", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2_TEST_ARCHIVE": {
        "kind": "file", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2_test.a",
        "sha256": "7df6a2eab7cd2c357370b5b1c240eba8260c7c124cfb5f7532cf9d7ce6ae8cec"},
    "SDL2_LIB_ALIAS_2_0_0": {
        "kind": "symlink", "path": "/opt/homebrew/lib/libSDL2-2.0.0.dylib",
        "target": "../Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2_LIB_ALIAS_2_0": {
        "kind": "symlink", "path": "/opt/homebrew/lib/libSDL2-2.0.dylib",
        "target": "../Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.dylib",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2_LIB_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/lib/libSDL2.dylib",
        "target": "../Cellar/sdl2-compat/2.32.74/lib/libSDL2.dylib",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2_TEST_LIB_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/lib/libSDL2_test.a",
        "target": "../Cellar/sdl2-compat/2.32.74/lib/libSDL2_test.a",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2_test.a",
        "sha256": "7df6a2eab7cd2c357370b5b1c240eba8260c7c124cfb5f7532cf9d7ce6ae8cec"},
    "SDL2_MAIN_LIB_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/lib/libSDL2main.a",
        "target": "../Cellar/sdl2-compat/2.32.74/lib/libSDL2main.a",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2main.a",
        "sha256": "8e49faa4979c4cbbe67770f997410cdae2b2b29128c899404902821320959624"},
    "SDL2_CELLAR_ALIAS_2_0": {
        "kind": "symlink", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.dylib",
        "target": "libSDL2-2.0.0.dylib",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2_CELLAR_ALIAS": {
        "kind": "symlink", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2.dylib",
        "target": "libSDL2-2.0.dylib",
        "resolved_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.0.dylib",
        "sha256": "2e0b8bd67ab49579fac23e48c6e727afe1e1bff002d883c7220bb0643ff28347"},
    "SDL2MAIN_ARCHIVE": {"kind": "file", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2main.a",
                          "sha256": "8e49faa4979c4cbbe67770f997410cdae2b2b29128c899404902821320959624"},
    "SDL2_HEADERS": {"kind": "tree", "path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/include/SDL2",
                      "sha256": "2c9b80aff783ad4f78b1bdab14d0c28c026ead83286b8c1dd545ec5ca7ec144d"},
    "GLSLANG_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/glslang/16.6.0/lib/libglslang.16.6.0.dylib",
                         "sha256": "2715978bb96917ad13467db0e5daed850a77cb7dc90bde8768d7cfbb5037b42c"},
    "GLSLANG_RESOURCE_LIMITS_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/glslang/16.6.0/lib/libglslang-default-resource-limits.16.6.0.dylib",
                                         "sha256": "8f40cf6fed194addb24002877c329ac0b9a3ba1af2da10f3b71ef91d92115d1d"},
    "GLSLANG_SPIRV_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/glslang/16.6.0/lib/libSPIRV.16.6.0.dylib",
                              "sha256": "31c5a105638051a16c5882f8a17b39ea555c205a61a553e75d038ea9adfe1a17"},
    "SPIRV_TOOLS_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/spirv-tools/1.4.363.0/lib/libSPIRV-Tools.dylib",
                            "sha256": "df1a0a3a8733eae75ad0e08c539c85897f7e03801b18a83ce98aedab7144bba9"},
    "SPIRV_TOOLS_OPT_RUNTIME": {"kind": "file", "path": "/opt/homebrew/Cellar/spirv-tools/1.4.363.0/lib/libSPIRV-Tools-opt.dylib",
                                "sha256": "84b3f6495d64c655f1208a2b04dd36c98d966d18daa45d82e6c2e3a03e34e272"},
    "SDK_SETTINGS": {"kind": "file", "path": "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/SDKSettings.plist",
                      "sha256": "90a738dbf3fd88e7f6d35646e19d3a8a716923b9773ce1b67260466d9ceed370"},
    "XCODE_INFO": {"kind": "file", "path": "/Applications/Xcode.app/Contents/Info.plist",
                    "sha256": "e3fe249ecea86908345d71c08e7c3da131ae09188a8caa100ac616f20098ecb7"},
    "SYSTEM_VERSION": {"kind": "file", "path": "/System/Library/CoreServices/SystemVersion.plist",
                       "sha256": "bd61e19dd67dd97ce9a27c79a2148d42cc9685a71c6f17ff27d62b819f39f983"},
    "XCODE_SELECT": {"kind": "file", "path": "/usr/bin/xcode-select",
                     "sha256": "49cf1e59551a8a401d86ac444aa6b2a9ab7ec6d3882c98c004dab02a0e484e53"},
    "XCRUN": {"kind": "file", "path": "/usr/bin/xcrun",
              "sha256": "bbe7671518a3905f9d3fce54312fbc77c2876b0d62aca74a0688fa869d31a8a9"},
    "XCODEBUILD": {"kind": "file",
                   "path": "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild",
                   "sha256": "d37fbcb4198c2474d847e5d7d4bf8394a1a9443890ad0153a76bc3e1d5576ec1"},
    "XCODE_CLANG": {"kind": "file",
                    "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang",
                    "sha256": "ee029330bf58ab6e481385f4237cc54602eaa8b81526242e5c5ab5b76e8c5a7e"},
    "XCODE_CLANGXX": {"kind": "file",
                      "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang",
                      "sha256": "ee029330bf58ab6e481385f4237cc54602eaa8b81526242e5c5ab5b76e8c5a7e"},
    "XCODE_LD": {"kind": "file",
                 "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld",
                 "sha256": "809ad9e3c137cdf6d997b4e67dd3dd2589f6bb5de612dc298504e87bc4936c7e"},
    "XCODE_AR": {"kind": "file",
                 "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ar",
                 "sha256": "ba81a5e9c4a1fd399c7693367559e341e823a411eecef83221c3caf9f25b581b"},
    "XCODE_RANLIB": {"kind": "file",
                     "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/libtool",
                     "sha256": "6e0a9c96cc9896ee392ee691133e51cfff0216234d9a21b8dfa53bdc3ffbc410"},
    "XCODE_AS": {"kind": "file",
                 "path": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/as",
                 "sha256": "06770364632b454317238e79d670c094e27301c7322c3a488323bd42e4d0bcbc"},
}
BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS = {
    "clang": "XCODE_CLANG",
    "clang++": "XCODE_CLANGXX",
    "ld": "XCODE_LD",
    "ar": "XCODE_AR",
    "ranlib": "XCODE_RANLIB",
    "as": "XCODE_AS",
}
BUILD_SESSION_DUAL_GRAPH_NATIVE_HOST_PROFILE = {
    "host_architecture": "arm64",
    "os_product_version": "27.0.1",
    "os_build": "26A434",
    "xcode_version": "27.1",
    "xcode_build": "27A9275",
    "xcode_info_build": "27A9274",
    "developer_directory": "/Applications/Xcode.app/Contents/Developer",
    "xcrun_selected_tool_paths": {
        "clang": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang",
        "clang++": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang++",
        "ld": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld",
        "ar": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ar",
        "ranlib": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ranlib",
        "as": "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/as",
    },
    "sdk_path": "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk",
    "sdk_symlink_target": "MacOSX.sdk",
    "sdk_resolved_path": "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk",
    "sdk_version": "27.0",
    "sdk_settings_sha256": "90a738dbf3fd88e7f6d35646e19d3a8a716923b9773ce1b67260466d9ceed370",
    "xcode_info_path": "/Applications/Xcode.app/Contents/Info.plist",
    "system_version_path": "/System/Library/CoreServices/SystemVersion.plist",
    "sdl2_header_search_path": "/opt/homebrew/include/SDL2",
    "sdl2_header_symlink_target": "../Cellar/sdl2-compat/2.32.74/include/SDL2",
    "sdl2_header_real_path": "/opt/homebrew/Cellar/sdl2-compat/2.32.74/include/SDL2",
}
BUILD_SESSION_ENVIRONMENT_KEYS = {
    "PATH", "HOME", "LANG", "LC_ALL", "CC", "CXX", "AR", "RANLIB",
    "PKG_CONFIG_PATH", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET", "DXVK_WSI_DRIVER",
}
BUILD_SESSION_FORBIDDEN_ENV_KEYS = {
    "DESTDIR", "BASH_ENV", "ENV", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "MAKEFLAGS", "NINJAFLAGS", "MESON_ARGS", "CMAKE_ARGS", "CCACHE_DIR",
}
BUILD_SESSION_ALLOWED_SETUP_OPTIONS = {"--prefix=/usr", "--wrap-mode=nodownload"}


def _require_buildlease_vm_runtime(plan: Mapping[str, Any]) -> None:
    """Require the exact v3 executor plan and the reviewed R6 policy amendment."""
    if (plan.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
            or plan.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE):
        raise RetentionError("BuildLease accepts only an explicit HOST_BUILDLEASE_V3 execution plan")
    if BUILD_SESSION_HOST_V3_TEST_BYPASS:
        return
    if not BUILD_SESSION_HOST_V3_POLICY_ACCEPTED:
        raise RetentionError(
            "HOST_BUILDLEASE_V3 is implemented but remains disabled for R6 until the additive "
            "execution-policy amendment receives its fresh independent PASS")
    try:
        policy_fd = os.open(
            BUILD_SESSION_HOST_V3_POLICY_AMENDMENT_PATH,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            policy_stat = os.fstat(policy_fd)
            if (not stat.S_ISREG(policy_stat.st_mode)
                    or policy_stat.st_size > 1024 * 1024):
                raise RetentionError("reviewed BuildLease v3 policy amendment is not a bounded regular file")
            policy_hash = hashlib.sha256()
            policy_size = 0
            while True:
                block = os.read(policy_fd, 64 * 1024)
                if not block:
                    break
                policy_size += len(block)
                if policy_size > 1024 * 1024:
                    raise RetentionError("reviewed BuildLease v3 policy amendment exceeds its size limit")
                policy_hash.update(block)
        finally:
            os.close(policy_fd)
    except OSError as error:
        raise RetentionError("reviewed BuildLease v3 policy amendment is unavailable") from error
    if policy_hash.hexdigest() != BUILD_SESSION_HOST_V3_POLICY_AMENDMENT_SHA256:
        raise RetentionError("reviewed BuildLease v3 policy amendment hash does not match the reviewed bytes")


def _session_process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_session_process_group(process_group_id: int) -> None:
    """Terminate a session command group and require the group to disappear."""
    for selected_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process_group_id, selected_signal)
        except ProcessLookupError:
            return
        except OSError as error:
            if error.errno == errno.ESRCH:
                return
            raise RetentionError(f"could not signal BuildLease session process group: {error}") from error
        deadline = time.monotonic() + BUILD_SESSION_PROCESS_TERM_SECONDS
        while _session_process_group_exists(process_group_id):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        else:
            return
    if _session_process_group_exists(process_group_id):
        raise RetentionError("BuildLease session process group remained after TERM and KILL")


def _session_sid_members(session_id: int) -> set[int]:
    """Enumerate PIDs, then ask the kernel for SID; never trust ps's session column."""
    result = subprocess.run(["/bin/ps", "-A", "-o", "pid="], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, check=False, timeout=10)
    if result.returncode != 0:
        raise RetentionError("could not enumerate processes for BuildLease session containment")
    members: set[int] = set()
    for row in result.stdout.splitlines():
        try:
            pid = int(row.strip())
        except ValueError as error:
            raise RetentionError("process inventory returned a malformed PID") from error
        try:
            observed_sid = os.getsid(pid)
        except ProcessLookupError:
            continue
        except OSError as error:
            if error.errno == errno.ESRCH:
                continue
            raise RetentionError(f"cannot verify BuildLease process SID for PID {pid}: {error}") from error
        if observed_sid == session_id:
            members.add(pid)
    return members


def _session_process_state(pid: int, session_id: int) -> str | None:
    """Read only process state from ps after revalidating SID with the kernel."""
    try:
        if os.getsid(pid) != session_id:
            return None
    except ProcessLookupError:
        return None
    except OSError as error:
        if error.errno == errno.ESRCH:
            return None
        raise RetentionError(f"cannot revalidate BuildLease process SID for PID {pid}: {error}") from error
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            check=False, timeout=5)
    if result.returncode != 0 or not result.stdout.strip():
        # A process that still has the held session ID but disappeared from ps
        # is ambiguous.  Only an ESRCH race is safe to classify as gone.
        try:
            if os.getsid(pid) == session_id:
                raise RetentionError(f"process state is unavailable for live BuildLease PID {pid}")
        except ProcessLookupError:
            return None
        except OSError as error:
            if error.errno == errno.ESRCH:
                return None
            raise RetentionError(f"cannot revalidate BuildLease process PID {pid}: {error}") from error
        return None
    return result.stdout.strip().split()[0]


def _session_signal_pid(pid: int, session_id: int, selected_signal: int) -> bool:
    """Signal only after a fresh kernel SID check, avoiding unrelated PID reuse."""
    try:
        if os.getsid(pid) != session_id:
            return False
        os.kill(pid, selected_signal)
        return True
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise RetentionError(f"could not signal BuildLease session PID {pid}: {error}") from error


def _session_leader_exited(session_id: int) -> bool:
    """Observe exit without reaping the session leader, keeping its SID pinned."""
    try:
        result = os.waitid(os.P_PID, session_id, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise RetentionError("BuildLease session leader was reaped before descendant cleanup") from error
    return result is not None and result.si_pid == session_id


def _contain_session_processes(session_id: int, *, timeout: float | None = None) -> dict[str, Any]:
    """Freeze and kill every live process in one held-leader SID before it is reaped."""
    if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 1:
        raise RetentionError("BuildLease process session identity is invalid")
    bound = BUILD_SESSION_PROCESS_QUIESCE_SECONDS if timeout is None else timeout
    freeze_deadline = time.monotonic() + bound
    discovered_sample: set[int] = set()
    member_observation_count = 0
    member_sample_truncated = False
    member_snapshot_count = 0
    member_snapshots_digest = hashlib.sha256()

    def record_snapshot(members: set[int]) -> None:
        nonlocal member_observation_count, member_sample_truncated, member_snapshot_count
        ordered = sorted(members)
        snapshot_bytes = _canonical_json(ordered)
        member_snapshots_digest.update(len(snapshot_bytes).to_bytes(8, "big"))
        member_snapshots_digest.update(snapshot_bytes)
        member_snapshot_count += 1
        member_observation_count += len(ordered)
        sample_order = ([session_id] if session_id in members else []) + [
            pid for pid in ordered if pid != session_id]
        for pid in sample_order:
            if pid in discovered_sample:
                continue
            if len(discovered_sample) < BUILD_SESSION_PROCESS_EVIDENCE_SAMPLE_PIDS:
                discovered_sample.add(pid)
            else:
                member_sample_truncated = True

    stable_stopped_rounds = 0
    while time.monotonic() < freeze_deadline:
        members = _session_sid_members(session_id)
        record_snapshot(members)
        live: set[int] = set()
        all_stopped = True
        for pid in sorted(members):
            state = _session_process_state(pid, session_id)
            if state is None or state.startswith("Z"):
                continue
            live.add(pid)
            if not state.startswith(("T", "t")):
                all_stopped = False
                _session_signal_pid(pid, session_id, signal.SIGSTOP)
        after = _session_sid_members(session_id)
        record_snapshot(after)
        after_live = {pid for pid in after
                      if (state := _session_process_state(pid, session_id)) is not None
                      and not state.startswith("Z")}
        if (all_stopped and live == after_live and members == after):
            stable_stopped_rounds += 1
            if stable_stopped_rounds >= 2:
                break
        else:
            stable_stopped_rounds = 0
        time.sleep(0.05)
    else:
        raise RetentionError("BuildLease process SID did not reach a bounded stopped state")

    # All live members are stopped, so no member can fork between enumeration and KILL.
    members = _session_sid_members(session_id)
    record_snapshot(members)
    for pid in sorted(members):
        state = _session_process_state(pid, session_id)
        if state is not None and not state.startswith("Z"):
            _session_signal_pid(pid, session_id, signal.SIGKILL)

    kill_deadline = time.monotonic() + bound
    while time.monotonic() < kill_deadline:
        members = _session_sid_members(session_id)
        record_snapshot(members)
        survivors: set[int] = set()
        for pid in sorted(members):
            if pid == session_id and _session_leader_exited(session_id):
                continue
            state = _session_process_state(pid, session_id)
            if state is not None and not state.startswith("Z"):
                survivors.add(pid)
                _session_signal_pid(pid, session_id, signal.SIGKILL)
        if not survivors:
            remaining = _session_sid_members(session_id)
            record_snapshot(remaining)
            descendants = remaining - {session_id}
            if not descendants and _session_leader_exited(session_id):
                return {"session_id": session_id, "members_seen": sorted(discovered_sample),
                        "members_seen_sample_truncated": member_sample_truncated,
                        "members_seen_observation_count": member_observation_count,
                        "members_seen_snapshot_count": member_snapshot_count,
                        "members_seen_sha256": member_snapshots_digest.hexdigest(),
                        "survivors": [], "leader_held_unreaped": True}
        time.sleep(0.05)
    raise RetentionError("BuildLease process SID remained after STOP/KILL and bounded re-enumeration")


def _session_hdiutil() -> tuple[Path, str]:
    path = Path("/usr/bin/hdiutil")
    if sys.platform != "darwin" or path.is_symlink() or path.resolve(strict=True) != path:
        raise RetentionError("BuildLease fixed-cap workspace requires canonical macOS hdiutil")
    identity = _session_hash_executable(path, _hash_path(path))
    return path, identity["sha256"]


def _session_hdiutil_info() -> list[dict[str, Any]]:
    hdiutil, _digest = _session_hdiutil()
    result = subprocess.run([str(hdiutil), "info", "-plist"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=30)
    if result.returncode != 0:
        raise RetentionError("hdiutil could not verify attached BuildLease workspace images")
    try:
        parsed = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise RetentionError("hdiutil returned invalid mounted-image identity data") from error
    images = parsed.get("images") if isinstance(parsed, dict) else None
    if not isinstance(images, list):
        raise RetentionError("hdiutil info omitted its image inventory")
    return [row for row in images if isinstance(row, dict)]


def _session_diskutil() -> tuple[Path, str]:
    path = Path("/usr/sbin/diskutil")
    if sys.platform != "darwin" or path.is_symlink() or path.resolve(strict=True) != path:
        raise RetentionError("BuildLease workspace identity requires canonical macOS diskutil")
    identity = _session_hash_executable(path, _hash_path(path))
    return path, identity["sha256"]


def _session_diskutil_info(device_node: str) -> dict[str, Any]:
    diskutil, _digest = _session_diskutil()
    result = subprocess.run([str(diskutil), "info", "-plist", device_node],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=30)
    if result.returncode != 0:
        raise RetentionError("diskutil could not verify BuildLease workspace volume identity")
    try:
        parsed = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise RetentionError("diskutil returned invalid BuildLease volume identity data") from error
    if not isinstance(parsed, dict):
        raise RetentionError("diskutil info omitted BuildLease volume identity")
    return parsed


def _session_image_row(root: Path) -> dict[str, Any] | None:
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    try:
        canonical_image = str(image.resolve(strict=True))
    except OSError:
        return None
    matches = []
    for row in _session_hdiutil_info():
        image_path = row.get("image-path")
        if not isinstance(image_path, str):
            continue
        try:
            if str(Path(image_path).resolve(strict=True)) == canonical_image:
                matches.append(row)
        except OSError:
            continue
    if len(matches) > 1:
        raise RetentionError("BuildLease sparseimage is attached more than once")
    return matches[0] if matches else None


def _session_image_device(root: Path) -> str | None:
    row = _session_image_row(root)
    if row is None:
        return None
    entities = row.get("system-entities")
    if not isinstance(entities, list):
        raise RetentionError("hdiutil image record omitted its device entities")
    devices = [entity.get("dev-entry") for entity in entities
               if isinstance(entity, dict) and entity.get("content-hint") == "GUID_partition_scheme"
               and isinstance(entity.get("dev-entry"), str)]
    if len(devices) != 1:
        raise RetentionError("BuildLease sparseimage has no unique whole-image device")
    return devices[0]


def _session_image_entity(root: Path) -> dict[str, Any] | None:
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    expected_mountpoint = str(workspace.resolve(strict=True))
    row = _session_image_row(root)
    if row is None:
        return None
    entities = row.get("system-entities")
    if not isinstance(entities, list):
        raise RetentionError("hdiutil image record omitted its system entities")
    selected = []
    unexpected_mounts = []
    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("dev-entry"), str):
            continue
        mountpoint = entity.get("mount-point")
        if mountpoint is None:
            continue
        if isinstance(mountpoint, str) and str(Path(mountpoint).resolve(strict=False)) == expected_mountpoint:
            selected.append(entity)
        else:
            unexpected_mounts.append(entity)
    if unexpected_mounts:
        raise RetentionError("BuildLease workspace image is attached at an unexpected mount point")
    if len(selected) > 1:
        raise RetentionError("BuildLease workspace image is attached at multiple matching mount points")
    if not selected:
        return None
    return {**selected[0], "image-path": str((root / BUILD_SESSION_WORKSPACE_IMAGE_NAME).resolve(strict=True)),
            "image-writeable": row.get("writeable")}


def _session_image_entities(root: Path) -> list[dict[str, Any]]:
    row = _session_image_row(root)
    if row is None:
        return []
    system_entities = row.get("system-entities")
    if not isinstance(system_entities, list):
        raise RetentionError("hdiutil image record omitted its system entities")
    canonical_image = str((root / BUILD_SESSION_WORKSPACE_IMAGE_NAME).resolve(strict=True))
    return [{**entity, "image-path": canonical_image, "image-writeable": row.get("writeable")}
            for entity in system_entities
            if isinstance(entity, dict) and isinstance(entity.get("dev-entry"), str)]


def _session_image_content_identity(image: Path) -> dict[str, Any]:
    before = image.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RetentionError("BuildLease sparseimage is not one canonical regular file")
    digest = _hash_path(image)
    after = image.stat(follow_symlinks=False)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise RetentionError("BuildLease sparseimage changed while hashing its detached contents")
    return {"path": str(image), "device": after.st_dev, "inode": after.st_ino,
            "size_bytes": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def _verify_session_workspace_mount(root: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(root).resolve(strict=True)
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    if identity.get("mountpoint") != str(workspace) or identity.get("image_path") != str(image):
        raise RetentionError("BuildLease workspace mount identity names another path")
    image_info = image.stat(follow_symlinks=False)
    root_info = root.stat(follow_symlinks=False)
    if (not stat.S_ISREG(image_info.st_mode) or image_info.st_nlink != 1
            or (image_info.st_dev, image_info.st_ino)
                != (identity.get("image_device"), identity.get("image_inode"))
            or identity.get("mountpoint_dir_device") != root_info.st_dev
            or not isinstance(identity.get("mountpoint_dir_inode"), int)
            or isinstance(identity.get("mountpoint_dir_inode"), bool)
            or identity.get("mountpoint_dir_inode", 0) <= 0):
        raise RetentionError("BuildLease sparseimage backing identity changed")
    entity = _session_image_entity(root)
    if (entity is None or entity.get("dev-entry") != identity.get("device_node")
            or entity.get("image-path") != str(image)
            or entity.get("image-writeable") is not True):
        raise RetentionError("BuildLease workspace is not mounted from its exact sparseimage device")
    disk_info = _session_diskutil_info(str(entity["dev-entry"]))
    if (disk_info.get("VolumeUUID") != identity.get("volume_uuid")
            or disk_info.get("FilesystemType") != "hfs"
            or disk_info.get("Writable") is not True
            or disk_info.get("MountPoint") != str(workspace)):
        raise RetentionError("BuildLease workspace volume UUID changed")
    mount_info = workspace.stat(follow_symlinks=False)
    fs = os.statvfs(workspace)
    observed = {"device": mount_info.st_dev, "filesystem_id": getattr(fs, "f_fsid", None),
                "capacity_bytes": fs.f_blocks * fs.f_frsize}
    if (observed["device"] != identity.get("device")
            or observed["filesystem_id"] != identity.get("filesystem_id")
            or observed["capacity_bytes"] != identity.get("capacity_bytes")
            or observed["capacity_bytes"] <= 0):
        raise RetentionError("BuildLease workspace filesystem identity or capacity changed")
    hdiutil, digest = _session_hdiutil()
    if str(hdiutil) != identity.get("hdiutil_path") or digest != identity.get("hdiutil_sha256"):
        raise RetentionError("BuildLease hdiutil identity changed")
    diskutil, diskutil_digest = _session_diskutil()
    if (str(diskutil) != identity.get("diskutil_path")
            or diskutil_digest != identity.get("diskutil_sha256")):
        raise RetentionError("BuildLease diskutil identity changed")
    return {**dict(identity), **observed}


def _verify_session_workspace_detached(root: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    """Recognize a completed detach after a crash without trusting a missing mount alone."""
    root = Path(root).resolve(strict=True)
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    if (identity.get("mountpoint") != str(workspace)
            or identity.get("image_path") != str(image)
            or identity.get("image_device") is None or identity.get("image_inode") is None):
        raise RetentionError("BuildLease detached workspace identity is inconsistent")
    image_info = image.stat(follow_symlinks=False)
    workspace_info = workspace.stat(follow_symlinks=False)
    root_info = root.stat(follow_symlinks=False)
    if (not stat.S_ISREG(image_info.st_mode) or image_info.st_nlink != 1
            or (image_info.st_dev, image_info.st_ino)
                != (identity.get("image_device"), identity.get("image_inode"))
            or not stat.S_ISDIR(workspace_info.st_mode)
            or workspace_info.st_dev != root_info.st_dev
            or (workspace_info.st_dev, workspace_info.st_ino)
                != (identity.get("mountpoint_dir_device"), identity.get("mountpoint_dir_inode"))
            or workspace.resolve(strict=True) != workspace
            or stat.S_IMODE(workspace_info.st_mode) != 0o500
            or any(workspace.iterdir())):
        raise RetentionError("BuildLease detached workspace or backing image identity changed")
    hdiutil, digest = _session_hdiutil()
    diskutil, diskutil_digest = _session_diskutil()
    if (str(hdiutil) != identity.get("hdiutil_path")
            or digest != identity.get("hdiutil_sha256")
            or str(diskutil) != identity.get("diskutil_path")
            or diskutil_digest != identity.get("diskutil_sha256")
            or _session_image_row(root) is not None):
        raise RetentionError("BuildLease workspace image remains attached or its tool identity changed")
    image_identity = _session_image_content_identity(image)
    expected_digest = identity.get("detached_image_identity", {}).get("sha256") \
        if isinstance(identity.get("detached_image_identity"), Mapping) else None
    if expected_digest is not None and image_identity["sha256"] != expected_digest:
        raise RetentionError("BuildLease detached sparseimage contents changed")
    return {**dict(identity), "attached": False, "detached_at_unix": time.time(),
            "detached_image_identity": image_identity}


def _attach_session_workspace(root: Path, capacity_bytes: int) -> dict[str, Any]:
    """Create and mount a fixed-capacity HFS+ sparseimage below one lease root."""
    root = Path(root).resolve(strict=True)
    if capacity_bytes < 16 * 1024 * 1024:
        raise RetentionError("BuildLease workspace image is below the minimum supported capacity")
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    if image.exists() or image.is_symlink() or workspace.exists() or workspace.is_symlink():
        raise RetentionError("BuildLease workspace image or mount point already exists")
    workspace.mkdir(mode=0o500)
    workspace.chmod(0o500)
    mountpoint_info = workspace.stat(follow_symlinks=False)
    root_info = root.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(mountpoint_info.st_mode)
            or mountpoint_info.st_dev != root_info.st_dev):
        raise RetentionError("BuildLease workspace mount point is not a directory on the lease filesystem")
    hdiutil, digest = _session_hdiutil()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"}
    # hdiutil's size suffix "b" counts 512-byte sectors, not bytes. Floor to
    # sectors so the backing image and mounted filesystem can never exceed the
    # signed byte reservation; the mounted statvfs check below remains required.
    capacity_sectors = capacity_bytes // 512
    if capacity_sectors <= 0:
        raise RetentionError("BuildLease workspace image has no whole sectors within its cap")
    created = subprocess.run([str(hdiutil), "create", "-quiet", "-size", f"{capacity_sectors}b",
                              "-fs", "HFS+", "-type", "SPARSE", "-volname", "FGMetalBuildWorkspace",
                              str(image)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=False, timeout=120)
    if created.returncode != 0:
        raise RetentionError("hdiutil could not create a fixed-capacity BuildLease workspace")
    attached = False
    try:
        result = subprocess.run([str(hdiutil), "attach", "-quiet", "-nobrowse", "-noautoopen",
                                 "-mountpoint", str(workspace), str(image)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
        if result.returncode != 0:
            raise RetentionError("hdiutil could not attach the fixed-capacity BuildLease workspace")
        attached = True
        entity = _session_image_entity(root)
        if entity is None:
            raise RetentionError("hdiutil attached no verifiable BuildLease workspace device")
        disk_info = _session_diskutil_info(str(entity["dev-entry"]))
        image_info = image.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        fs = os.statvfs(workspace)
        capacity = fs.f_blocks * fs.f_frsize
        hdiutil_after, digest_after = _session_hdiutil()
        identity = {"image_path": str(image), "image_device": image_info.st_dev,
                    "image_inode": image_info.st_ino, "mountpoint": str(workspace),
                    "mountpoint_dir_device": mountpoint_info.st_dev,
                    "mountpoint_dir_inode": mountpoint_info.st_ino,
                    "device_node": entity.get("dev-entry"), "volume_uuid": disk_info.get("VolumeUUID"),
                    "device": workspace_info.st_dev, "filesystem_id": getattr(fs, "f_fsid", None),
                    "capacity_bytes": capacity, "requested_capacity_bytes": capacity_bytes,
                    "filesystem_type": disk_info.get("FilesystemType"),
                    "mountpoint_placeholder_mode": 0o500,
                    "disk_device": _session_image_device(root),
                    "hdiutil_path": str(hdiutil_after), "hdiutil_sha256": digest_after,
                    "diskutil_path": str(_session_diskutil()[0]),
                    "diskutil_sha256": _session_diskutil()[1],
                    "attached": True, "attached_at_unix": time.time()}
        if (not identity["device_node"] or not identity["disk_device"]
                or not identity["volume_uuid"] or identity["filesystem_type"] != "hfs"
                or disk_info.get("Writable") is not True or not identity["filesystem_id"]
                or capacity <= 0 or capacity > capacity_bytes or digest_after != digest):
            raise RetentionError("BuildLease workspace mount identity is incomplete or exceeds its cap")
        return _verify_session_workspace_mount(root, identity)
    except BaseException:
        if attached:
            try:
                entity = _session_image_entity(root)
                if entity is not None:
                    unmounted = subprocess.run(
                        [str(hdiutil), "unmount", "-quiet", entity["dev-entry"]],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        check=False, timeout=120)
                    if unmounted.returncode == 0:
                        disk_device = _session_image_device(root)
                        if disk_device is not None:
                            subprocess.run([str(hdiutil), "detach", "-quiet", disk_device],
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           check=False, timeout=120)
            except BaseException:
                pass
        raise


def _session_hdiutil_failure_detail(result: subprocess.CompletedProcess[bytes]) -> str:
    """Keep normal hdiutil failure diagnostics bounded without changing retry policy."""
    stderr = result.stderr or b""
    if not isinstance(stderr, bytes):
        stderr = str(stderr).encode("utf-8", "replace")
    preview = stderr[:2048].decode("utf-8", "replace")
    truncation = " (truncated)" if len(stderr) > 2048 else ""
    return f"returncode={result.returncode}; stderr={preview!r}{truncation}"


def _detach_session_workspace(root: Path, identity: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normal-unmount, then normally detach, the exact writable v3 workspace image."""
    root = Path(root).resolve(strict=True)
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    if identity is None:
        if _session_image_row(root) is not None:
            raise RetentionError("BuildLease workspace image is attached without a verified mount identity")
        return {"attached": False, "image_path": str(image), "mountpoint": str(workspace),
                "detached_at_unix": time.time()}
    if identity.get("attached") is not True:
        return _verify_session_workspace_detached(root, identity)
    attached_row = _session_image_row(root)
    if attached_row is None:
        # A crash may occur after successful detach but before the caller stores
        # the returned identity. Recovery remains failure-only and hashes the
        # detached image again; it never resumes or accepts the interrupted run.
        return _verify_session_workspace_detached(root, identity)
    mounted_entity = _session_image_entity(root)
    if mounted_entity is None:
        # Normal unmount can succeed while normal detach is busy (for example,
        # a process keeps cwd at the mount root). Retry only the normal detach
        # against the same verified whole-image device.
        image_info = image.stat(follow_symlinks=False)
        hdiutil, hdiutil_digest = _session_hdiutil()
        diskutil, diskutil_digest = _session_diskutil()
        if (attached_row.get("writeable") is not True
                or _session_image_device(root) != identity.get("disk_device")
                or (image_info.st_dev, image_info.st_ino)
                    != (identity.get("image_device"), identity.get("image_inode"))
                or str(hdiutil) != identity.get("hdiutil_path")
                or hdiutil_digest != identity.get("hdiutil_sha256")
                or str(diskutil) != identity.get("diskutil_path")
                or diskutil_digest != identity.get("diskutil_sha256")):
            raise RetentionError("unmounted BuildLease image no longer matches its writable identity")
        result = subprocess.run([str(hdiutil), "detach", "-quiet", str(identity["disk_device"])],
                                env={"PATH": "/usr/bin:/bin", "HOME": str(root),
                                     "LANG": "C", "LC_ALL": "C"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=False, timeout=120)
        if result.returncode != 0 or _session_image_row(root) is not None:
            raise RetentionError("BuildLease backing-image normal detach retry failed; outputs remain rejected")
        return _verify_session_workspace_detached(
            root, {**dict(identity), "attached": False,
                   "unmounted_at_unix": identity.get("unmounted_at_unix", time.time())})
    _verify_session_workspace_mount(root, identity)
    hdiutil, digest = _session_hdiutil()
    if identity is not None and (str(hdiutil) != identity.get("hdiutil_path")
                                 or digest != identity.get("hdiutil_sha256")):
        raise RetentionError("hdiutil identity changed before workspace detach")
    device_node = str(identity["device_node"])
    unmounted = subprocess.run([str(hdiutil), "unmount", "-quiet", device_node],
                            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if unmounted.returncode != 0:
        raise RetentionError(
            "BuildLease workspace normal unmount failed; no outputs may be accepted ("
            f"hdiutil {_session_hdiutil_failure_detail(unmounted)})")
    row = _session_image_row(root)
    if (row is None or row.get("writeable") is not True
            or _session_image_entity(root) is not None
            or _session_image_device(root) != identity.get("disk_device")):
        raise RetentionError("BuildLease normal unmount did not leave the exact image attached but unmounted")
    detached = subprocess.run([str(hdiutil), "detach", "-quiet", str(identity["disk_device"])],
                              env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if detached.returncode != 0 or _session_image_row(root) is not None:
        raise RetentionError("BuildLease backing-image normal detach failed; no outputs may be accepted")
    return _verify_session_workspace_detached(
        root, {**dict(identity), "attached": False, "unmounted_at_unix": time.time()})


def _attach_session_workspace_readonly_impl(root: Path, detached_identity: Mapping[str, Any]) -> dict[str, Any]:
    """Reattach only the verified detached image read-only for output enumeration."""
    root = Path(root).resolve(strict=True)
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    detached = _verify_session_workspace_detached(root, detached_identity)
    expected_image = detached.get("detached_image_identity")
    if not isinstance(expected_image, Mapping):
        raise RetentionError("BuildLease detached image lacks a content identity for read-only reattach")
    result = subprocess.run([str(_session_hdiutil()[0]), "attach", "-readonly", "-quiet", "-nobrowse",
                             "-noautoopen", "-mountpoint", str(workspace), str(image)],
                            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if result.returncode != 0:
        raise RetentionError("BuildLease exact backing image could not be reattached read-only")
    entity = _session_image_entity(root)
    row = _session_image_row(root)
    if entity is None or row is None or row.get("writeable") is not False:
        raise RetentionError("BuildLease reattached image is not the exact read-only mount")
    disk_info = _session_diskutil_info(str(entity["dev-entry"]))
    image_identity = _session_image_content_identity(image)
    if (image_identity != dict(expected_image)
            or entity.get("image-path") != str(image)
            or entity.get("image-writeable") is not False
            or disk_info.get("Writable") is not False
            or disk_info.get("FilesystemType") != detached_identity.get("filesystem_type")
            or disk_info.get("VolumeUUID") != detached_identity.get("volume_uuid")
            or disk_info.get("MountPoint") != str(workspace)):
        raise RetentionError("BuildLease read-only reattachment does not match detached image identity")
    workspace_info = workspace.stat(follow_symlinks=False)
    fs = os.statvfs(workspace)
    capacity = fs.f_blocks * fs.f_frsize
    if (capacity != detached_identity.get("capacity_bytes")
            or capacity <= 0 or workspace_info.st_dev == detached_identity.get("mountpoint_dir_device")):
        raise RetentionError("BuildLease read-only filesystem capacity or device identity is invalid")
    diskutil, diskutil_digest = _session_diskutil()
    hdiutil, hdiutil_digest = _session_hdiutil()
    if (str(diskutil) != detached_identity.get("diskutil_path")
            or diskutil_digest != detached_identity.get("diskutil_sha256")
            or str(hdiutil) != detached_identity.get("hdiutil_path")
            or hdiutil_digest != detached_identity.get("hdiutil_sha256")):
        raise RetentionError("BuildLease system disk-image tool identity changed during reattachment")
    return {"image_path": str(image), "image_device": detached_identity["image_device"],
            "image_inode": detached_identity["image_inode"], "image_size_bytes": image_identity["size_bytes"],
            "image_sha256": image_identity["sha256"], "mountpoint": str(workspace),
            "mountpoint_dir_device": detached_identity["mountpoint_dir_device"],
            "mountpoint_dir_inode": detached_identity["mountpoint_dir_inode"],
            "mountpoint_placeholder_mode": detached_identity.get("mountpoint_placeholder_mode", 0o500),
            "detached_image_identity": dict(expected_image),
            "device_node": entity["dev-entry"], "disk_device": _session_image_device(root),
            "volume_uuid": disk_info["VolumeUUID"], "filesystem_type": disk_info["FilesystemType"],
            "device": workspace_info.st_dev, "filesystem_id": getattr(fs, "f_fsid", None),
            "capacity_bytes": capacity, "readonly": True, "attached": True,
            "diskutil_writable": disk_info.get("Writable"),
            "hdiutil_writeable": row.get("writeable"),
            "hdiutil_path": str(hdiutil), "hdiutil_sha256": hdiutil_digest,
            "diskutil_path": str(diskutil), "diskutil_sha256": diskutil_digest,
            "attached_at_unix": time.time()}


def _best_effort_detach_unverified_readonly_image(root: Path) -> None:
    """Clean up an exact-path read-only attachment after failed verification; never force."""
    root = Path(root).resolve(strict=True)
    row = _session_image_row(root)
    if row is None or row.get("writeable") is not False:
        return
    disk_device = _session_image_device(root)
    if disk_device is None:
        return
    hdiutil, _digest = _session_hdiutil()
    subprocess.run([str(hdiutil), "detach", "-quiet", disk_device],
                   env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)


def _attach_session_workspace_readonly(root: Path, detached_identity: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _attach_session_workspace_readonly_impl(root, detached_identity)
    except BaseException:
        try:
            _best_effort_detach_unverified_readonly_image(root)
        except BaseException:
            pass
        raise


def _verify_session_workspace_readonly(root: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(root).resolve(strict=True)
    image = root / BUILD_SESSION_WORKSPACE_IMAGE_NAME
    workspace = root / BUILD_SESSION_WORKSPACE_NAME
    if (identity.get("mountpoint") != str(workspace) or identity.get("image_path") != str(image)
            or identity.get("readonly") is not True or identity.get("attached") is not True):
        raise RetentionError("BuildLease read-only attachment identity is inconsistent")
    image_info = image.stat(follow_symlinks=False)
    if (image_info.st_dev, image_info.st_ino) != (identity.get("image_device"), identity.get("image_inode")):
        raise RetentionError("BuildLease read-only attachment backing file identity changed")
    expected_image = identity.get("detached_image_identity")
    if not isinstance(expected_image, Mapping) or expected_image.get("path") != str(image):
        raise RetentionError("BuildLease read-only attachment lacks its detached image identity")
    entity = _session_image_entity(root)
    row = _session_image_row(root)
    if (entity is None or row is None or entity.get("dev-entry") != identity.get("device_node")
            or _session_image_device(root) != identity.get("disk_device")
            or entity.get("image-writeable") is not False or row.get("writeable") is not False):
        raise RetentionError("BuildLease read-only attachment is missing, writable, or substituted")
    disk_info = _session_diskutil_info(str(entity["dev-entry"]))
    if (disk_info.get("Writable") is not False or disk_info.get("VolumeUUID") != identity.get("volume_uuid")
            or disk_info.get("FilesystemType") != identity.get("filesystem_type")
            or disk_info.get("MountPoint") != str(workspace)):
        raise RetentionError("BuildLease diskutil read-only volume identity changed")
    fs = os.statvfs(workspace)
    workspace_info = workspace.stat(follow_symlinks=False)
    if (workspace_info.st_dev != identity.get("device")
            or getattr(fs, "f_fsid", None) != identity.get("filesystem_id")
            or fs.f_blocks * fs.f_frsize != identity.get("capacity_bytes")):
        raise RetentionError("BuildLease read-only mount filesystem identity or capacity changed")
    image_identity = _session_image_content_identity(image)
    hdiutil, hdiutil_digest = _session_hdiutil()
    diskutil, diskutil_digest = _session_diskutil()
    if (image_identity != dict(expected_image)
            or image_identity.get("sha256") != identity.get("image_sha256")
            or image_identity.get("size_bytes") != identity.get("image_size_bytes")
            or str(hdiutil) != identity.get("hdiutil_path")
            or hdiutil_digest != identity.get("hdiutil_sha256")
            or str(diskutil) != identity.get("diskutil_path")
            or diskutil_digest != identity.get("diskutil_sha256")):
        raise RetentionError("BuildLease backing image changed after read-only reattachment")
    return {**dict(identity), "diskutil_info": disk_info}


def _detach_session_workspace_readonly(root: Path, identity: Mapping[str, Any]) -> dict[str, Any]:
    """Normally detach the verified read-only evidence mount and recheck its image hash."""
    root = Path(root).resolve(strict=True)
    _verify_session_workspace_readonly(root, identity)
    hdiutil, digest = _session_hdiutil()
    if str(hdiutil) != identity.get("hdiutil_path") or digest != identity.get("hdiutil_sha256"):
        raise RetentionError("hdiutil identity changed before read-only evidence detach")
    result = subprocess.run([str(hdiutil), "detach", "-quiet", str(identity["disk_device"])],
                            env={"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C", "LC_ALL": "C"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120)
    if result.returncode != 0 or _session_image_row(root) is not None:
        raise RetentionError("BuildLease read-only evidence image could not be normally detached")
    detached = _verify_session_workspace_detached(
        root, {**dict(identity), "attached": False,
               "detached_image_identity": {"path": identity["image_path"],
                                            "device": identity["image_device"],
                                            "inode": identity["image_inode"],
                                            "size_bytes": identity["image_size_bytes"],
                                            "sha256": identity["image_sha256"]}})
    return {**dict(identity), "attached": False, "detached_at_unix": time.time(),
            "detached_image_identity": detached["detached_image_identity"]}


def _session_plan_copy(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze a caller plan as canonical JSON before it can reach reservation."""
    if not isinstance(plan, Mapping):
        raise RetentionError("BuildLease session plan must be a mapping")
    try:
        raw = _canonical_json(dict(plan))
        if len(raw) > BUILD_SESSION_PLAN_LIMIT_BYTES:
            raise RetentionError("BuildLease session plan exceeds its bounded controller-evidence size")
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise RetentionError(f"BuildLease session plan is not canonical JSON: {error}") from error
    if not isinstance(value, dict):
        raise RetentionError("BuildLease session plan must be an object")
    return value


def _session_relative_path(value: Any, label: str, *, allow_dot: bool = False) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RetentionError(f"{label} must be a canonical relative path")
    path = Path(value)
    if (path.is_absolute() or any(part in {"", ".."} for part in path.parts)
            or (not allow_dot and value in {".", "./"})
            or path.as_posix() != value):
        raise RetentionError(f"{label} must not contain absolute paths, aliases, or traversal")
    return path


def _session_dual_graph_enabled(plan: Mapping[str, Any]) -> bool:
    return ("dual_build_graphs" in plan or "build_graphs" in plan
            or "policy_sha256" in plan)


def _session_dual_graph_by_id(plan: Mapping[str, Any], graph_id: str) -> Mapping[str, Any]:
    graphs = plan.get("build_graphs")
    if not isinstance(graphs, list):
        raise RetentionError("dual-graph plan has no declared graph list")
    matches = [graph for graph in graphs
               if isinstance(graph, Mapping) and graph.get("graph_id") == graph_id]
    if len(matches) != 1:
        raise RetentionError(f"step references an undeclared or duplicate graph: {graph_id}")
    return matches[0]


def _session_validate_dual_graphs(value: dict[str, Any], source: Mapping[str, Any],
                                  inputs: list[Mapping[str, Any]]) -> None:
    """Validate the single reviewed R6-A pair; graph input never supplies commands."""
    if (value.get("dual_build_graphs") != BUILD_SESSION_DUAL_GRAPH_CAPABILITY
            or value.get("policy_sha256") != BUILD_SESSION_HOST_V3_DUAL_GRAPH_POLICY_AMENDMENT_SHA256):
        raise RetentionError("dual-graph capability or policy identity is not authorized")
    graphs = value.get("build_graphs")
    if not isinstance(graphs, list) or len(graphs) != 2:
        raise RetentionError("dual-graph plan must declare exactly two graphs")
    if [graph.get("graph_id") if isinstance(graph, dict) else None for graph in graphs] != list(
            BUILD_SESSION_DUAL_GRAPH_IDS):
        raise RetentionError("dual-graph plan must declare the fixed graph IDs in canonical order")

    expected_graphs = (
        {
            "graph_id": "windows-product-r6a",
            "graph_role": "WINDOWS_PRODUCT",
            "build_directory": "build.win64-r6a",
            "configure_profile": "R6A_WINDOWS_PRODUCT",
            "cross_file_input": "build-win64.txt",
            "targets": ["DEFAULT_PRODUCTION_OUTPUTS", "R6A_PRODUCTION_PATH_PE"],
            "machine_role": "CROSS_WINDOWS_X86_64",
            "expected_outputs": [{
                "output_id": "r6a-production-path-pe",
                "path": "tests/present-accounting/present-accounting-production-path.exe",
                "format": "PE32PLUS_X86_64",
                "producer_target": "R6A_PRODUCTION_PATH_PE",
            }],
            "stage_role": "WINDOWS_PRODUCT_OUTPUTS",
            "runtime_stage_role": "NONE",
            "executable_test_eligibility": False,
            "tool_receipt_ids": list(BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS["WINDOWS_PRODUCT"]),
            "runtime_receipt_ids": [],
        },
        {
            "graph_id": "native-verification-r6a",
            "graph_role": "NATIVE_VERIFICATION",
            "build_directory": "build.native-r6a",
            "configure_profile": "R6A_NATIVE_VERIFICATION_SDL2",
            "cross_file_input": None,
            "targets": ["R6A_PRODUCTION_PATH_MACHO", "R6A_PRESENT_ACCOUNTING_UNIT",
                        "R6A_PRESENT_ACCOUNTING_MODEL"],
            "machine_role": "NATIVE_DARWIN_HOST",
            "expected_outputs": [
                {
                    "output_id": "r6a-production-path-macho",
                    "path": "tests/present-accounting/present-accounting-production-path",
                    "format": "MACHO_NATIVE_ARCH",
                    "producer_target": "R6A_PRODUCTION_PATH_MACHO",
                },
                {
                    "output_id": "r6a-present-accounting-unit",
                    "path": "tests/present-accounting/present-accounting-unit",
                    "format": "MACHO_NATIVE_ARCH",
                    "producer_target": "R6A_PRESENT_ACCOUNTING_UNIT",
                },
                {
                    "output_id": "r6a-present-accounting-model",
                    "path": "tests/present-accounting/present-accounting-model",
                    "format": "MACHO_NATIVE_ARCH",
                    "producer_target": "R6A_PRESENT_ACCOUNTING_MODEL",
                },
            ],
            "stage_role": "NATIVE_VERIFICATION_EXECUTION_ONLY",
            "runtime_stage_role": "NATIVE_SDL2_RUNTIME_CLOSURE",
            "executable_test_eligibility": True,
            "tool_receipt_ids": list(BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS["NATIVE_VERIFICATION"]),
            "runtime_receipt_ids": ["SDL2_COMPAT", "SDL3"],
        },
    )
    input_by_path = {row.get("path"): row for row in inputs if isinstance(row, Mapping)}
    profile = value.get("native_host_profile")
    if profile != BUILD_SESSION_DUAL_GRAPH_NATIVE_HOST_PROFILE:
        raise RetentionError("native graph SDK, Xcode, OS, or host architecture profile is not authorized")
    host_dependencies = value.get("host_dependency_receipts")
    expected_dependencies = [
        {"input_id": input_id, **pin}
        for input_id, pin in BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS.items()
    ]
    if host_dependencies != expected_dependencies:
        raise RetentionError("native host dependency receipts do not match the fixed toolchain and SDK closure")
    if (profile.get("sdk_settings_sha256")
            != BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS["SDK_SETTINGS"]["sha256"]
            or BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS["SDK_SETTINGS"]["path"]
            != str(Path(profile["sdk_resolved_path"]) / "SDKSettings.plist")
            or BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS["XCODE_INFO"]["path"]
            != profile["xcode_info_path"]
            or BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS["XCODEBUILD"]["path"]
            != str(Path(profile["developer_directory"]) / "usr/bin/xcodebuild")
            or BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS["SYSTEM_VERSION"]["path"]
            != profile["system_version_path"]):
        raise RetentionError("native SDK/Xcode/system receipts are detached from the host profile")
    selected_tool_bin = (Path(profile["developer_directory"])
                         / "Toolchains/XcodeDefault.xctoolchain/usr/bin")
    for tool_name, input_id in BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS.items():
        expected_reported = str(selected_tool_bin / tool_name)
        resolved_name = {"clang++": "clang", "ranlib": "libtool"}.get(tool_name, tool_name)
        expected_resolved = str(selected_tool_bin / resolved_name)
        if (profile.get("xcrun_selected_tool_paths", {}).get(tool_name) != expected_reported
                or BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS[input_id]["path"] != expected_resolved):
            raise RetentionError(f"native xcrun {tool_name} path is detached from its exact tool receipt")
    all_tools = value.get("tools")
    if not isinstance(all_tools, Mapping) or set(all_tools) != set(BUILD_SESSION_DUAL_GRAPH_TOOL_PINS):
        raise RetentionError("dual-graph plan must use exactly the fixed, closed tool receipt set")
    for receipt_id, pin in BUILD_SESSION_DUAL_GRAPH_TOOL_PINS.items():
        receipt = all_tools.get(receipt_id)
        if (not isinstance(receipt, Mapping) or set(receipt) != {"path", "sha256"}
                or receipt.get("path") != pin["path"] or receipt.get("sha256") != pin["sha256"]):
            raise RetentionError(f"dual-graph tool receipt {receipt_id} does not match its fixed path and hash")
    mingw_root = Path(BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS[
        "MINGW_TOOLCHAIN_TREE"]["path"])
    for receipt_id in ("mingw_cc", "mingw_cxx", "mingw_ar", "mingw_windres",
                       "mingw_strip", "mingw_linker", "mingw_assembler"):
        tool_path = Path(BUILD_SESSION_DUAL_GRAPH_TOOL_PINS[receipt_id]["path"])
        if mingw_root not in tool_path.parents or tool_path.parent != mingw_root / "bin":
            raise RetentionError("MinGW executable receipt is detached from the closed compiler support tree")
    dependency_pins = BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS
    alias_targets = {
        "SDL2_PKGCONFIG_CELLAR_ALIAS": "SDL2_PKGCONFIG",
        "SDL2_PKGCONFIG_GLOBAL_ALIAS": "SDL2_PKGCONFIG",
        "SDL2_COMPAT_PKGCONFIG_GLOBAL_ALIAS": "SDL2_PKGCONFIG",
        "SDL2_LIB_ALIAS_2_0_0": "SDL2_COMPAT_LIBRARY",
        "SDL2_LIB_ALIAS_2_0": "SDL2_COMPAT_LIBRARY",
        "SDL2_LIB_ALIAS": "SDL2_COMPAT_LIBRARY",
        "SDL2_TEST_LIB_ALIAS": "SDL2_TEST_ARCHIVE",
        "SDL2_MAIN_LIB_ALIAS": "SDL2MAIN_ARCHIVE",
        "SDL2_CELLAR_ALIAS_2_0": "SDL2_COMPAT_LIBRARY",
        "SDL2_CELLAR_ALIAS": "SDL2_COMPAT_LIBRARY",
    }
    for alias_id, target_id in alias_targets.items():
        alias = dependency_pins[alias_id]
        target = dependency_pins[target_id]
        if alias["resolved_path"] != target["path"] or alias["sha256"] != target["sha256"]:
            raise RetentionError(f"SDL2 linker or pkg-config alias {alias_id} is detached from its exact target")
    if (dependency_pins["SDL2_COMPAT_LIBRARY"]["path"]
            != BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS["SDL2_COMPAT"]["path"]
            or dependency_pins["SDL2_COMPAT_LIBRARY"]["sha256"]
            != BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS["SDL2_COMPAT"]["sha256"]):
        raise RetentionError("SDL2 linker alias target differs from the staged runtime receipt")
    for graph, expected in zip(graphs, expected_graphs):
        if set(graph) != set(expected) | {"source_identity"}:
            raise RetentionError("dual-graph declaration has missing or unrecognized fields")
        for field, required in expected.items():
            if graph.get(field) != required:
                raise RetentionError(f"dual-graph {graph.get('graph_id')} has invalid {field}")
        if graph.get("source_identity") != dict(source):
            raise RetentionError("dual-graph source identities must exactly match the shared source identity")
        if graph["graph_role"] not in BUILD_SESSION_DUAL_GRAPH_ROLES:
            raise RetentionError("dual-graph role is unknown")
        if graph["cross_file_input"] is not None and graph["cross_file_input"] not in input_by_path:
            raise RetentionError("Windows graph cross file must be an exactly receipted source input")
        if graph["graph_role"] == "NATIVE_VERIFICATION" and graph["cross_file_input"] is not None:
            raise RetentionError("native verification graph may not use a cross file")
        if graph["graph_role"] == "WINDOWS_PRODUCT" and graph["cross_file_input"] != "build-win64.txt":
            raise RetentionError("Windows product graph requires the exact admitted build-win64.txt input")
        for receipt_id, expected_name in BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS[graph["graph_role"]].items():
            receipt = value.get("tools", {}).get(receipt_id)
            if (not isinstance(receipt, Mapping)
                    or Path(str(receipt.get("path", ""))).name.casefold() != expected_name.casefold()
                    or receipt.get("path") != BUILD_SESSION_DUAL_GRAPH_TOOL_PINS[receipt_id]["path"]
                    or receipt.get("sha256") != BUILD_SESSION_DUAL_GRAPH_TOOL_PINS[receipt_id]["sha256"]):
                raise RetentionError(
                    f"{graph['graph_role']} toolchain receipt {receipt_id} has the wrong tool identity")

    runtime_receipts = value.get("runtime_receipts")
    if not isinstance(runtime_receipts, list):
        raise RetentionError("dual-graph plan must bind its native runtime inputs")
    if [row.get("runtime_id") if isinstance(row, dict) else None for row in runtime_receipts] != [
            "SDL2_COMPAT", "SDL3"]:
        raise RetentionError("native runtime receipt list must bind the exact SDL2 compatibility closure")
    runtime_names = {"SDL2_COMPAT": "libSDL2-2.0.0.dylib", "SDL3": "libSDL3.0.dylib"}
    for row in runtime_receipts:
        if (not isinstance(row, dict) or set(row) != {"runtime_id", "path", "sha256"}
                or not isinstance(row.get("path"), str)
                or not Path(row["path"]).is_absolute()
                or str(Path(row["path"])) != row["path"]
                or Path(row["path"]).name != runtime_names[row["runtime_id"]]
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))
                or row["path"] != BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[row["runtime_id"]]["path"]
                or row["sha256"] != BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[row["runtime_id"]]["sha256"]):
            raise RetentionError("native runtime receipt is not an exact canonical file identity")


def _session_graph_format_identity(path: Path, expected_format: str) -> tuple[str, str | None]:
    """Parse only the closed PE32+ x86_64 and thin native Mach-O formats."""
    if expected_format == "PE32PLUS_X86_64":
        try:
            with path.open("rb") as stream:
                header = stream.read(64)
                if len(header) != 64 or header[:2] != b"MZ":
                    raise RetentionError("declared Windows output is not a DOS/PE image")
                pe_offset = int.from_bytes(header[0x3c:0x40], "little")
                if pe_offset < 64 or pe_offset > 1024 * 1024:
                    raise RetentionError("declared Windows output has an invalid PE header offset")
                stream.seek(pe_offset)
                pe_header = stream.read(26)
        except OSError as error:
            raise RetentionError(f"could not read declared Windows output: {error}") from error
        if (len(pe_header) < 26 or pe_header[:4] != b"PE\0\0"
                or int.from_bytes(pe_header[4:6], "little") != 0x8664
                or int.from_bytes(pe_header[24:26], "little") != 0x20b):
            raise RetentionError("declared Windows output is not PE32+ x86_64")
        return "PE32PLUS_X86_64", "x86_64"
    if expected_format == "MACHO_NATIVE_ARCH":
        architecture = _thin_macho_architecture(path)
        native = _session_native_architecture()
        if architecture != native or _session_macho_filetype(path) != 2:
            raise RetentionError("declared native output architecture differs from the host architecture")
        return "MACHO_NATIVE_ARCH", architecture
    raise RetentionError("session output format is not a closed Host-v3 format")


def _session_native_architecture() -> str:
    machine = platform.machine().lower()
    return {"aarch64": "arm64", "amd64": "x86_64"}.get(machine, machine)


def _session_macho_filetype(path: Path) -> int:
    """Read the filetype from one thin 64-bit Mach-O header."""
    import struct
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
    except OSError as error:
        raise RetentionError(f"cannot read Mach-O filetype for {path}: {error}") from error
    byte_order = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(header[:4])
    if len(header) < 16 or byte_order is None:
        raise RetentionError("native output must be a thin 64-bit Mach-O file")
    return struct.unpack(f"{byte_order}I", header[12:16])[0]


def _session_macho_dependency_identity(path: Path) -> dict[str, Any]:
    """Read thin Mach-O architecture, file type, dylib loads, install name, and rpaths."""
    import struct

    try:
        with path.open("rb") as stream:
            header = stream.read(32)
            if len(header) != 32:
                raise RetentionError("native output must have a complete thin 64-bit Mach-O header")
            byte_order = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(header[:4])
            if byte_order is None:
                raise RetentionError("native output must be a thin 64-bit Mach-O file")
            _magic, cpu_type, cpu_subtype, filetype, command_count, command_bytes, _flags, _reserved = \
                struct.unpack(f"{byte_order}IIIIIIII", header)
            if command_count > 65536 or command_bytes > 16 * 1024 * 1024:
                raise RetentionError("Mach-O load-command table exceeds the closed parser bound")
            commands_raw = stream.read(command_bytes)
            if len(commands_raw) != command_bytes:
                raise RetentionError("Mach-O load-command table is truncated")
    except OSError as error:
        raise RetentionError(f"could not read Mach-O dependency identity: {error}") from error

    dylib_names: dict[int, str] = {
        0xC: "LC_LOAD_DYLIB", 0x18 | 0x80000000: "LC_LOAD_WEAK_DYLIB",
        0x1F | 0x80000000: "LC_REEXPORT_DYLIB", 0x20: "LC_LAZY_LOAD_DYLIB",
        0x23 | 0x80000000: "LC_LOAD_UPWARD_DYLIB",
    }
    loads: list[dict[str, str]] = []
    rpaths: list[str] = []
    install_name: str | None = None
    offset = 0
    for _index in range(command_count):
        if offset + 8 > len(commands_raw):
            raise RetentionError("Mach-O load-command header is outside its declared table")
        command, command_size = struct.unpack_from(f"{byte_order}II", commands_raw, offset)
        if command_size < 8 or command_size % 4 != 0 or offset + command_size > len(commands_raw):
            raise RetentionError("Mach-O load command has an invalid bounded size")
        if command in dylib_names or command == 0xD:
            if command_size < 24:
                raise RetentionError("Mach-O dylib load command is truncated")
            name_offset = struct.unpack_from(f"{byte_order}I", commands_raw, offset + 8)[0]
            if name_offset < 24 or name_offset >= command_size:
                raise RetentionError("Mach-O dylib name offset is outside its load command")
            raw_name = commands_raw[offset + name_offset:offset + command_size]
            name_bytes = raw_name.split(b"\0", 1)[0]
            try:
                name = name_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RetentionError("Mach-O dylib path is not valid UTF-8") from error
            if not name or "\x00" in name:
                raise RetentionError("Mach-O dylib path is empty or invalid")
            if command == 0xD:
                if install_name is not None:
                    raise RetentionError("Mach-O image declares duplicate install names")
                install_name = name
            else:
                loads.append({"command": dylib_names[command], "path": name})
        elif command == (0x8000001C):  # LC_RPATH
            if command_size < 12:
                raise RetentionError("Mach-O rpath command is truncated")
            path_offset = struct.unpack_from(f"{byte_order}I", commands_raw, offset + 8)[0]
            if path_offset < 12 or path_offset >= command_size:
                raise RetentionError("Mach-O rpath offset is outside its load command")
            raw_path = commands_raw[offset + path_offset:offset + command_size]
            try:
                rpath = raw_path.split(b"\0", 1)[0].decode("utf-8")
            except UnicodeDecodeError as error:
                raise RetentionError("Mach-O rpath is not valid UTF-8") from error
            if not rpath:
                raise RetentionError("Mach-O rpath is empty")
            rpaths.append(rpath)
        offset += command_size
    if offset != len(commands_raw):
        raise RetentionError("Mach-O load-command sizes do not fill the declared table")
    architecture = {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(cpu_type, f"cpu:{cpu_type:#x}")
    return {"architecture": architecture, "cpu_subtype": cpu_subtype,
            "filetype": filetype, "install_name": install_name,
            "loads": loads, "rpaths": rpaths}


def _session_macho_dependency_bindings(macho: Mapping[str, Any],
                                       runtime_receipt_ids: Iterable[str]) -> list[dict[str, Any]]:
    """Require each non-system Mach-O load to name one exact staged runtime receipt."""
    allowed_runtime_ids = set(runtime_receipt_ids)
    bindings: list[dict[str, Any]] = []
    for load in macho.get("loads", []):
        load_path = load.get("path")
        if not isinstance(load_path, str) or not load_path:
            raise RetentionError("Mach-O load command has no canonical path identity")
        if load_path.startswith(("/System/Library/", "/usr/lib/")):
            bindings.append({"load_path": load_path, "resolution_class": "HOST_SYSTEM_RUNTIME"})
            continue
        matches = [runtime_id for runtime_id in sorted(allowed_runtime_ids)
                   if Path(load_path).name == BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES.get(runtime_id)]
        if len(matches) != 1:
            raise RetentionError(f"Mach-O load command is outside the admitted system/runtime closure: {load_path}")
        runtime_id = matches[0]
        pin = BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[runtime_id]
        bindings.append({"load_path": load_path, "resolution_class": "STAGED_RUNTIME_RECEIPT",
                         "runtime_id": runtime_id, "source_path": pin["path"],
                         "sha256": pin["sha256"],
                         "staged_path": (BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE /
                                         BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[runtime_id]).as_posix(),
                         "dyld_resolution_verified": False})
    return bindings


def _session_graph_directory_identity(workspace: Path, graph: Mapping[str, Any], *,
                                      must_exist: bool) -> dict[str, int] | None:
    """Open one fixed direct-child graph directory without following aliases."""
    relative = _session_relative_path(graph.get("build_directory"), "graph build directory")
    if len(relative.parts) != 1 or relative.as_posix() != BUILD_SESSION_DUAL_GRAPH_DIRECTORIES.get(
            str(graph.get("graph_id"))):
        raise RetentionError("graph build directory is not the exact fixed workspace child")
    workspace_fd = _open_dir(workspace)
    try:
        root_info = os.fstat(workspace_fd)
        root_fsid = getattr(os.fstatvfs(workspace_fd), "f_fsid", None)
        try:
            info = os.stat(relative.name, dir_fd=workspace_fd, follow_symlinks=False)
        except FileNotFoundError:
            if must_exist:
                raise RetentionError("declared build graph directory is missing")
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise RetentionError("declared build graph path is not a real directory")
        child_fd = os.open(relative.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=workspace_fd)
        try:
            child_info = os.fstat(child_fd)
            child_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
            if ((child_info.st_dev, child_info.st_ino) != (info.st_dev, info.st_ino)
                    or child_info.st_dev != root_info.st_dev or child_fsid != root_fsid):
                raise RetentionError("declared build graph directory escaped the workspace filesystem")
            return {"device": child_info.st_dev, "inode": child_info.st_ino,
                    "filesystem_id": child_fsid}
        finally:
            os.close(child_fd)
    finally:
        os.close(workspace_fd)


def _session_graph_output_identity(workspace: Path, graph: Mapping[str, Any],
                                   output: Mapping[str, Any]) -> dict[str, Any]:
    """Record one declared graph output using no-follow file access and a format parser."""
    graph_dir_identity = _session_graph_directory_identity(workspace, graph, must_exist=True)
    graph_root = _session_safe_path(workspace, Path(str(graph["build_directory"])),
                                    must_exist=True, directory=True)
    relative = _session_relative_path(output.get("path"), "graph output path")
    path = _session_safe_path(graph_root, relative, must_exist=True, directory=False)
    parent_fd = _open_dir(path.parent)
    fd = None
    try:
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            raise RetentionError("declared graph executable is not a regular executable file")
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise RetentionError("declared graph output changed while it was being hashed")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)
    actual_format, architecture = _session_graph_format_identity(path, str(output["format"]))
    macho_dependency_identity = (_session_macho_dependency_identity(path)
                                 if actual_format == "MACHO_NATIVE_ARCH" else None)
    runtime_receipt_ids = list(graph["runtime_receipt_ids"])
    macho_dependency_bindings = (_session_macho_dependency_bindings(
        macho_dependency_identity, runtime_receipt_ids)
        if macho_dependency_identity is not None else None)
    test_binding = (BUILD_SESSION_DUAL_GRAPH_NATIVE_TEST_BINDINGS.get(output["output_id"])
                    if graph["graph_role"] == "NATIVE_VERIFICATION" else None)
    current = path.stat(follow_symlinks=False)
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            or _hash_path(path) != digest):
        raise RetentionError("declared graph output changed during format and identity verification")
    return {
        "graph_id": graph["graph_id"], "graph_role": graph["graph_role"],
        "source_identity": graph["source_identity"],
        "build_directory": graph["build_directory"],
        "build_directory_identity": graph_dir_identity,
        "target_id": output["producer_target"], "output_id": output["output_id"],
        "path": f"{graph['build_directory']}/{relative.as_posix()}",
        "format": actual_format, "architecture": architecture,
        "macho_dependency_identity": macho_dependency_identity,
        "macho_dependency_bindings": macho_dependency_bindings,
        "runtime_dependency_contract": ({
            "graph_id": graph["graph_id"],
            "runtime_stage_role": graph["runtime_stage_role"],
            "test_class": test_binding["test_class"] if test_binding is not None else None,
            "requires_sdl2_runtime": (test_binding["requires_sdl2_runtime"]
                                      if test_binding is not None else False),
            "runtime_receipt_ids": runtime_receipt_ids,
            "runtime_receipts": [{"runtime_id": runtime_id,
                                  "source_path": BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[runtime_id]["path"],
                                  "sha256": BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[runtime_id]["sha256"],
                                  "staged_path": (BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE /
                                                  BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[runtime_id]).as_posix()}
                                 for runtime_id in runtime_receipt_ids],
            "test_eligible": graph["executable_test_eligibility"] and test_binding is not None,
            "loader_basename": (BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_BASENAME
                                if test_binding is not None
                                and test_binding["requires_sdl2_runtime"] else None),
            "resolution_claim": BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_RESOLUTION_CLAIM,
        } if graph["graph_role"] == "NATIVE_VERIFICATION" else None),
        "file_identity": {"device": before.st_dev, "inode": before.st_ino,
                          "mode": stat.S_IMODE(before.st_mode), "size_bytes": before.st_size,
                          "sha256": digest},
    }


def _session_regular_file_identity(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    """Hash one external tool/runtime input through a no-follow descriptor."""
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise RetentionError(f"{label} path is not a canonical real file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RetentionError(f"{label} input is not a regular file")
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or digest != expected_sha256):
            raise RetentionError(f"{label} input changed from its admitted identity")
        return {"path": str(path), "sha256": digest, "device": before.st_dev,
                "inode": before.st_ino, "size_bytes": before.st_size,
                "mode": stat.S_IMODE(before.st_mode)}
    finally:
        os.close(fd)


def _session_symlink_identity(path: Path, expected_target: str, expected_resolved_path: str,
                              expected_sha256: str, label: str) -> dict[str, Any]:
    """Bind one exact linker/pkg-config alias and its final regular-file target."""
    if path.parent.resolve(strict=True) != path.parent:
        raise RetentionError(f"{label} parent directory is not canonical")
    try:
        before = path.stat(follow_symlinks=False)
        raw_target = os.readlink(path)
    except OSError as error:
        raise RetentionError(f"{label} is not a readable symlink") from error
    if not stat.S_ISLNK(before.st_mode) or raw_target != expected_target:
        raise RetentionError(f"{label} target differs from its pinned linker alias")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RetentionError(f"{label} target is missing") from error
    if resolved != Path(expected_resolved_path) or resolved.is_symlink():
        raise RetentionError(f"{label} resolves outside its pinned target identity")
    target_identity = _session_regular_file_identity(resolved, expected_sha256, label)
    after = path.stat(follow_symlinks=False)
    if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_mode)
            or os.readlink(path) != raw_target):
        raise RetentionError(f"{label} changed while its target was verified")
    return {"path": str(path), "target": raw_target, "resolved_path": str(resolved),
            "sha256": target_identity["sha256"], "link_device": before.st_dev,
            "link_inode": before.st_ino, "link_mtime_ns": before.st_mtime_ns,
            "target_device": target_identity["device"], "target_inode": target_identity["inode"],
            "target_size_bytes": target_identity["size_bytes"],
            "target_mode": target_identity["mode"]}


def _session_closed_alias_directory_identity(directory: Path, prefix: str,
                                             expected_names: set[str], label: str) -> dict[str, Any]:
    """Reject unreceipted alternatives in a linker/pkg-config search directory."""
    if directory.is_symlink() or directory.resolve(strict=True) != directory or not directory.is_dir():
        raise RetentionError(f"{label} directory is not a canonical directory")
    before = directory.stat(follow_symlinks=False)
    matching = {entry.name for entry in directory.iterdir() if entry.name.startswith(prefix)}
    after = directory.stat(follow_symlinks=False)
    if ((before.st_dev, before.st_ino, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mtime_ns)
            or matching != expected_names):
        raise RetentionError(f"{label} directory contains a substituted or unreceipted alias")
    return {"path": str(directory), "device": before.st_dev, "inode": before.st_ino,
            "mtime_ns": before.st_mtime_ns, "mode": stat.S_IMODE(before.st_mode),
            "owner_uid": before.st_uid, "matching_names": sorted(matching)}


def _capture_session_native_toolchain_selection(
        plan: Mapping[str, Any], dependency_identities: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Probe active Xcode selection with fixed tools and a scrubbed environment."""
    profile = plan["native_host_profile"]
    if not isinstance(profile, Mapping):
        raise RetentionError("native toolchain selection lacks its typed host profile")
    xcode_select = dependency_identities.get("XCODE_SELECT")
    xcrun = dependency_identities.get("XCRUN")
    if not isinstance(xcode_select, Mapping) or not isinstance(xcrun, Mapping):
        raise RetentionError("native toolchain selection tools are not pinned host dependencies")

    # Never inherit DEVELOPER_DIR, SDKROOT, TOOLCHAINS, or any other user setting.
    base_environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
    }

    def probe(executable: Mapping[str, Any], arguments: list[str], label: str,
              environment: Mapping[str, str]) -> str:
        executable_path = str(executable["path"])
        try:
            result = subprocess.run([executable_path, *arguments], env=dict(environment),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, check=False, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RetentionError(f"native toolchain {label} probe could not run") from error
        output = result.stdout.strip()
        if (result.returncode != 0 or not output or "\n" in output or "\x00" in output):
            raise RetentionError(f"native toolchain {label} probe returned no single successful identity")
        return output

    active_developer_directory = probe(xcode_select, ["-p"], "active developer directory",
                                       base_environment)
    pinned_developer_directory = str(profile["developer_directory"])
    if active_developer_directory != pinned_developer_directory:
        raise RetentionError("active xcode-select developer directory differs from the pinned native profile")

    selected_environment = {
        **base_environment,
        "DEVELOPER_DIR": pinned_developer_directory,
        "SDKROOT": str(profile["sdk_resolved_path"]),
    }
    selected_tools: dict[str, dict[str, str]] = {}
    for tool_name, input_id in BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS.items():
        reported_path = probe(xcrun, ["--find", tool_name], f"selected {tool_name}",
                              selected_environment)
        try:
            resolved_path = Path(reported_path).resolve(strict=True)
        except OSError as error:
            raise RetentionError(f"xcrun selected an unavailable native {tool_name}") from error
        receipt_identity = dependency_identities[input_id]
        selected_identity = _session_regular_file_identity(
            resolved_path, str(receipt_identity["sha256"]), f"xcrun-selected native {tool_name}")
        selected_tools[tool_name] = {
            "reported_path": reported_path,
            "resolved_path": selected_identity["path"],
            "sha256": selected_identity["sha256"],
        }

    sdk_path = probe(xcrun, ["--sdk", "macosx", "--show-sdk-path"],
                     "selected macOS SDK path", selected_environment)
    try:
        sdk_resolved_path = str(Path(sdk_path).resolve(strict=True))
    except OSError as error:
        raise RetentionError("xcrun selected an unavailable macOS SDK") from error
    sdk_version = probe(xcrun, ["--sdk", "macosx", "--show-sdk-version"],
                         "selected macOS SDK version", selected_environment)

    for input_id in ("XCODE_SELECT", "XCRUN"):
        original = dependency_identities[input_id]
        after = _session_regular_file_identity(
            Path(str(original["path"])), str(original["sha256"]),
            f"native toolchain selection utility {input_id}")
        if ((after["device"], after["inode"], after["size_bytes"], after["sha256"])
                != (original["device"], original["inode"], original["size_bytes"], original["sha256"])):
            raise RetentionError(f"native toolchain selection utility {input_id} changed during probing")

    return {
        "active_developer_directory": active_developer_directory,
        "developer_dir_environment": selected_environment["DEVELOPER_DIR"],
        "sdkroot_environment": selected_environment["SDKROOT"],
        "sdk_path": sdk_path,
        "sdk_resolved_path": sdk_resolved_path,
        "sdk_version": sdk_version,
        "selected_tools": selected_tools,
    }


def _validate_session_native_toolchain_selection(
        plan: Mapping[str, Any], dependency_identities: Mapping[str, Mapping[str, Any]],
        observed: Mapping[str, Any]) -> dict[str, Any]:
    """Bind active xcode-select and xcrun results to exact profile/tool receipts."""
    profile = plan["native_host_profile"]
    if (observed.get("active_developer_directory") != profile["developer_directory"]
            or observed.get("developer_dir_environment") != profile["developer_directory"]
            or observed.get("sdkroot_environment") != profile["sdk_resolved_path"]
            or observed.get("sdk_path") != profile["sdk_path"]
            or observed.get("sdk_resolved_path") != profile["sdk_resolved_path"]
            or observed.get("sdk_version") != profile["sdk_version"]):
        raise RetentionError("observed Xcode/SDK selection differs from the pinned native host profile")
    selected_tools = observed.get("selected_tools")
    expected_selected_paths = profile.get("xcrun_selected_tool_paths")
    if (not isinstance(selected_tools, Mapping)
            or not isinstance(expected_selected_paths, Mapping)
            or set(selected_tools) != set(BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS)
            or set(expected_selected_paths) != set(BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS)):
        raise RetentionError("observed xcrun tool selection is incomplete")
    for tool_name, input_id in BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS.items():
        row = selected_tools.get(tool_name)
        receipt = dependency_identities[input_id]
        if (not isinstance(row, Mapping)
                or row.get("reported_path") != expected_selected_paths[tool_name]
                or row.get("resolved_path") != receipt.get("path")
                or row.get("sha256") != receipt.get("sha256")):
            raise RetentionError(f"xcrun-selected native {tool_name} differs from its pinned path/hash receipt")
    return dict(observed)


def _session_host_dependency_identities(
        plan: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Rehash the closed native compiler/package/SDK inputs and validate profile metadata."""
    if not _session_dual_graph_enabled(plan):
        return [], {}, {}
    identities: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for receipt in plan["host_dependency_receipts"]:
        input_id = receipt["input_id"]
        path = Path(receipt["path"])
        if receipt["kind"] == "symlink":
            identity = _session_symlink_identity(
                path, receipt["target"], receipt["resolved_path"], receipt["sha256"],
                f"native host dependency {input_id}")
        elif path.is_symlink() or path.resolve(strict=True) != path:
            raise RetentionError(f"native host dependency {input_id} is not a canonical path")
        elif receipt["kind"] == "file":
            identity = _session_regular_file_identity(path, receipt["sha256"],
                                                     f"native host dependency {input_id}")
        elif receipt["kind"] == "tree":
            if not path.is_dir():
                raise RetentionError(f"native host dependency {input_id} is not a directory")
            before = path.stat(follow_symlinks=False)
            digest, file_count = _tree_hash(
                path, reject_symlinks=True, reject_external_hardlinks=True)
            after = path.stat(follow_symlinks=False)
            if ((before.st_dev, before.st_ino, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns)
                    or digest != receipt["sha256"]):
                raise RetentionError(f"native host dependency {input_id} changed from its admitted identity")
            identity = {"path": str(path), "sha256": digest, "device": before.st_dev,
                        "inode": before.st_ino, "mode": stat.S_IMODE(before.st_mode),
                        "file_count": file_count}
        elif receipt["kind"] == "tree_internal_symlinks":
            if not path.is_dir():
                raise RetentionError(f"native host dependency {input_id} is not a directory")
            before = path.stat(follow_symlinks=False)
            digest, file_count = _tree_hash(
                path, reject_external_hardlinks=True, require_internal_symlinks=True,
                include_root_git=True)
            after = path.stat(follow_symlinks=False)
            if ((before.st_dev, before.st_ino, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns)
                    or digest != receipt["sha256"]
                    or file_count != receipt.get("file_count")):
                raise RetentionError(f"native host dependency {input_id} changed from its admitted identity")
            identity = {"path": str(path), "sha256": digest, "device": before.st_dev,
                        "inode": before.st_ino, "mode": stat.S_IMODE(before.st_mode),
                        "file_count": file_count}
        else:
            raise RetentionError(f"native host dependency {input_id} has an unsupported receipt kind")
        observed = {"input_id": input_id, "kind": receipt["kind"], **identity}
        identities.append(observed)
        by_id[input_id] = observed

    alias_groups = (
        ("SDL2 linker", "libSDL2", (
            "SDL2_LIB_ALIAS_2_0_0", "SDL2_LIB_ALIAS_2_0", "SDL2_LIB_ALIAS",
            "SDL2_TEST_LIB_ALIAS", "SDL2_MAIN_LIB_ALIAS"), set()),
        ("SDL2 Cellar pkg-config", "sdl2", ("SDL2_PKGCONFIG_CELLAR_ALIAS",),
         {"sdl2-compat.pc"}),
        ("SDL2 pkg-config", "sdl2", (
            "SDL2_PKGCONFIG_GLOBAL_ALIAS", "SDL2_COMPAT_PKGCONFIG_GLOBAL_ALIAS"), set()),
    )
    alias_directory_identities = []
    for label, prefix, receipt_ids, extra_names in alias_groups:
        paths = [Path(BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS[row]["path"])
                 for row in receipt_ids]
        if len({str(path.parent) for path in paths}) != 1:
            raise RetentionError(f"{label} aliases do not share one fixed search directory")
        directory = paths[0].parent
        expected_names = {path.name for path in paths} | extra_names
        alias_directory_identities.append(_session_closed_alias_directory_identity(
            directory, prefix, expected_names, label))
    identities.extend({"input_id": f"SDL2_ALIAS_DIRECTORY_{index}", "kind": "directory", **identity}
                      for index, identity in enumerate(alias_directory_identities))

    toolchain_selection = _validate_session_native_toolchain_selection(
        plan, by_id, _capture_session_native_toolchain_selection(plan, by_id))
    identities.append({"input_id": "NATIVE_TOOLCHAIN_SELECTION", "kind": "selection",
                       "identity": toolchain_selection})

    profile = plan["native_host_profile"]
    machine = _session_native_architecture()
    if sys.platform != "darwin" or profile["host_architecture"] != machine:
        raise RetentionError("native host profile differs from the running Darwin architecture")
    for field, receipt_id in (("sdk_settings_sha256", "SDK_SETTINGS"),):
        if profile[field] != by_id[receipt_id]["sha256"]:
            raise RetentionError("native SDK settings hash differs from its typed host dependency receipt")
    sdk_link = Path(profile["sdk_path"])
    header_link = Path(profile["sdl2_header_search_path"])
    if (not sdk_link.is_symlink() or os.readlink(sdk_link) != profile["sdk_symlink_target"]
            or sdk_link.resolve(strict=True) != Path(profile["sdk_resolved_path"])
            or not header_link.is_symlink()
            or os.readlink(header_link) != profile["sdl2_header_symlink_target"]
            or header_link.resolve(strict=True) != Path(profile["sdl2_header_real_path"])):
        raise RetentionError("native SDK or SDL2 header alias differs from its exact typed profile")
    developer_directory = Path(profile["developer_directory"])
    if (developer_directory.is_symlink() or developer_directory.resolve(strict=True) != developer_directory
            or not developer_directory.is_dir()):
        raise RetentionError("native Xcode developer directory is not its exact canonical profile path")

    def read_profile_plist(input_id: str) -> Mapping[str, Any]:
        receipt = next(row for row in plan["host_dependency_receipts"] if row["input_id"] == input_id)
        path = Path(receipt["path"])
        try:
            raw = path.read_bytes()
            parsed = plistlib.loads(raw)
        except (OSError, plistlib.InvalidFileException) as error:
            raise RetentionError(f"native host profile plist {input_id} is unreadable") from error
        if (not isinstance(parsed, Mapping) or hashlib.sha256(raw).hexdigest() != receipt["sha256"]
                or _session_regular_file_identity(path, receipt["sha256"],
                                                  f"native host profile {input_id}")["sha256"]
                    != hashlib.sha256(raw).hexdigest()):
            raise RetentionError(f"native host profile plist {input_id} changed while read")
        return parsed

    sdk_settings = read_profile_plist("SDK_SETTINGS")
    xcode_info = read_profile_plist("XCODE_INFO")
    system_version = read_profile_plist("SYSTEM_VERSION")
    expected_profile_values = (
        (sdk_settings.get("Version"), profile["sdk_version"]),
        (xcode_info.get("CFBundleShortVersionString"), profile["xcode_version"]),
        (xcode_info.get("DTXcodeBuild"), profile["xcode_info_build"]),
        (system_version.get("ProductVersion"), profile["os_product_version"]),
        (system_version.get("ProductBuildVersion"), profile["os_build"]),
    )
    if any(actual != expected for actual, expected in expected_profile_values):
        raise RetentionError("native SDK/Xcode/system plist values differ from the admitted host profile")
    return identities, dict(profile), toolchain_selection


def _session_validate_plan(plan: Mapping[str, Any], *, build_id: str, build_path: Path,
                           source_tree_hash: str, expected_bytes: int) -> dict[str, Any]:
    """Validate the finite v3 contract without launching or reserving anything."""
    value = _session_plan_copy(plan)
    dual_graph = _session_dual_graph_enabled(value)
    allowed_plan_keys = {
        "schema_version", "executor_mode", "admission_identity", "source_identity", "build_id", "build_workspace",
        "reservation_bytes", "stage_root", "output_root", "environment", "tools",
        "input_receipts", "steps",
    }
    if dual_graph:
        allowed_plan_keys |= {"policy_sha256", "dual_build_graphs", "build_graphs", "runtime_receipts",
                              "native_host_profile", "host_dependency_receipts"}
    if (set(value) - allowed_plan_keys
            or not (allowed_plan_keys - {"input_receipts"}).issubset(value)):
        raise RetentionError("session plan has missing or unrecognized top-level fields")
    if value.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION:
        raise RetentionError("unsupported BuildLease session plan version")
    if value.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE:
        raise RetentionError("BuildLease session plan must explicitly select HOST_BUILDLEASE_V3")
    admission = value.get("admission_identity")
    if (not isinstance(admission, dict)
            or not isinstance(admission.get("admission_id"), str)
            or not admission["admission_id"].strip()
            or not re.fullmatch(r"[0-9a-f]{64}", str(admission.get("sha256", "")))):
        raise RetentionError("session plan lacks a complete execution admission identity")
    source = value.get("source_identity")
    if not isinstance(source, dict):
        raise RetentionError("session plan lacks an exact source identity")
    source_path = Path(str(source.get("root", ""))).expanduser()
    if (not source_path.is_absolute() or str(source.get("root")) != str(source_path)
            or "\x00" in str(source_path)
            or str(source_path) != str(Path(os.path.abspath(source_path)))
            or str(source.get("sha256", "")) != source_tree_hash
            or not re.fullmatch(r"[0-9a-f]{40,64}", str(source.get("revision", "")))):
        raise RetentionError("session plan source revision, root, or digest is invalid")
    expected_workspace = build_path / BUILD_SESSION_WORKSPACE_NAME
    if value.get("build_id") != build_id or value.get("build_workspace") != str(expected_workspace):
        raise RetentionError("session plan is not bound to this exact BuildLease workspace")
    if expected_bytes <= BUILD_SESSION_CONTROLLER_HEADROOM_BYTES + 16 * 1024 * 1024:
        raise RetentionError("session reservation cannot hold its controller evidence headroom and fixed workspace")
    if value.get("reservation_bytes") != expected_bytes:
        raise RetentionError("session plan reservation differs from the admitted BuildLease amount")
    stage_relative = _session_relative_path(value.get("stage_root"), "stage_root")
    output_relative = _session_relative_path(value.get("output_root"), "output_root", allow_dot=True)
    if len(stage_relative.parts) != 1 or output_relative != Path("."):
        raise RetentionError("session stage must be one direct child and output root must be the admitted workspace")
    if dual_graph and stage_relative != Path("stage"):
        raise RetentionError("dual-graph staging must use the exact fixed stage root")
    environment = value.get("environment")
    if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not isinstance(item, str) or "\x00" in key + item
            for key, item in environment.items()):
        raise RetentionError("session environment must be an exact string mapping")
    if (set(environment) - BUILD_SESSION_ENVIRONMENT_KEYS
            or set(environment) & BUILD_SESSION_FORBIDDEN_ENV_KEYS
            or any(key.startswith(("DYLD_", "LD_", "MESON_", "NINJA_", "CMAKE_"))
                   for key in environment)):
        raise RetentionError("session environment contains an uncontrolled hook or redirection")
    if dual_graph:
        if set(environment) & {"CC", "CXX", "AR", "RANLIB", "PKG_CONFIG_PATH", "SDKROOT",
                               "MACOSX_DEPLOYMENT_TARGET", "DXVK_WSI_DRIVER"}:
            raise RetentionError("dual-graph compiler, SDK, pkg-config, and WSI variables are controller-derived")
    elif "DXVK_WSI_DRIVER" in environment:
        raise RetentionError("legacy single-graph environment may not select a dual-graph WSI driver")
    required_home = str(expected_workspace / ".home")
    if environment.get("HOME") != required_home:
        raise RetentionError("session HOME must be the private directory inside this BuildLease")
    search_path = environment.get("PATH")
    if not search_path or any(not Path(part).is_absolute() for part in search_path.split(os.pathsep)):
        raise RetentionError("session PATH must contain only explicit absolute directories")
    if dual_graph and search_path != BUILD_SESSION_DUAL_GRAPH_PATH:
        raise RetentionError("dual-graph PATH must equal the controller-owned closed tool search path")
    tools = value.get("tools")
    if not isinstance(tools, dict) or not {"meson", "ninja", "git", "sandbox"}.issubset(tools):
        raise RetentionError("session plan must bind Meson, Ninja, Git, and Seatbelt executable receipts")
    for name, receipt in tools.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
            raise RetentionError("session tool receipt name is invalid")
        if (not isinstance(receipt, dict) or set(receipt) != {"path", "sha256"}
                or not isinstance(receipt.get("path"), str)
                or not Path(receipt["path"]).is_absolute()
                or str(Path(receipt["path"])) != receipt["path"]
                or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256", "")))):
            raise RetentionError(f"session tool receipt is invalid: {name}")
        expected_names = {"meson": {"meson"}, "ninja": {"ninja", "ninja-build"},
                          "git": {"git"}, "sandbox": {"sandbox-exec"}}
        if name in expected_names and Path(receipt["path"]).name.casefold() not in expected_names[name]:
            raise RetentionError(f"session {name} tool path is not the direct registered executable")
        if name == "sandbox" and receipt["path"] != "/usr/bin/sandbox-exec":
            raise RetentionError("session Seatbelt launcher must be the canonical system sandbox-exec")
    inputs = value.get("input_receipts", [])
    if not isinstance(inputs, list):
        raise RetentionError("session input_receipts must be an ordered list")
    for row in inputs:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256", "")))):
            raise RetentionError("session input receipt is invalid")
        _session_relative_path(row["path"], "input receipt path")
    if dual_graph:
        _session_validate_dual_graphs(value, source, inputs)
    steps = value.get("steps")
    minimum_steps = 14 if dual_graph else 5
    if not isinstance(steps, list) or len(steps) < minimum_steps or len(steps) > BUILD_SESSION_MAX_STEPS:
        raise RetentionError("session plan must declare a finite configure/build/install/test/verification sequence")
    if len(inputs) > 128:
        raise RetentionError("session plan contains too many input receipts for bounded controller evidence")
    command_bytes = 0
    kinds = [step.get("kind") if isinstance(step, dict) else None for step in steps]
    if dual_graph:
        expected_steps = [
            {"kind": "configure", "graph_id": "windows-product-r6a", "cwd": "."},
            {"kind": "configure", "graph_id": "native-verification-r6a", "cwd": "."},
            {"kind": "build", "graph_id": "windows-product-r6a",
             "target_id": "DEFAULT_PRODUCTION_OUTPUTS", "cwd": "."},
            {"kind": "build", "graph_id": "windows-product-r6a",
             "target_id": "R6A_PRODUCTION_PATH_PE", "cwd": "."},
            {"kind": "build", "graph_id": "native-verification-r6a",
             "target_id": "R6A_PRODUCTION_PATH_MACHO", "cwd": "."},
            {"kind": "build", "graph_id": "native-verification-r6a",
             "target_id": "R6A_PRESENT_ACCOUNTING_UNIT", "cwd": "."},
            {"kind": "build", "graph_id": "native-verification-r6a",
             "target_id": "R6A_PRESENT_ACCOUNTING_MODEL", "cwd": "."},
            {"kind": "install", "graph_id": "windows-product-r6a", "cwd": "."},
            {"kind": "stage_windows_product", "graph_id": "windows-product-r6a",
             "output_id": "r6a-production-path-pe", "cwd": "."},
            {"kind": "stage_native_runtime", "graph_id": "native-verification-r6a",
             "runtime_ids": ["SDL2_COMPAT", "SDL3"], "cwd": "."},
            {"kind": "test", "graph_id": "native-verification-r6a",
             "output_id": "r6a-production-path-macho", "test_class": "PRODUCTION_PATH",
             "arguments": [], "cwd": "."},
            {"kind": "test", "graph_id": "native-verification-r6a",
             "output_id": "r6a-present-accounting-unit", "test_class": "UNIT",
             "arguments": [], "cwd": "."},
            {"kind": "test", "graph_id": "native-verification-r6a",
             "output_id": "r6a-present-accounting-model", "test_class": "MODEL",
             "arguments": [], "cwd": "."},
            {"kind": "source_verification"},
        ]
        if steps != expected_steps:
            raise RetentionError(
                "dual-graph steps must use the fixed graph provenance, target, stage, and native test sequence")
    elif (kinds[:3] != ["configure", "build", "install"] or kinds[-1] != "source_verification"
          or any(kind != "test" for kind in kinds[3:-1])):
        raise RetentionError("session plan step order must be configure, build, install, tests, source verification")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RetentionError(f"session step {index} is not an object")
        if step.get("kind") == "source_verification":
            if set(step) != {"kind"}:
                raise RetentionError("source verification step may not dispatch a command")
            continue
        allowed_fields = (set(step) if dual_graph else
                          ({"kind", "cwd", "executable", "arguments"}
                           if step.get("kind") == "test" else {"kind", "cwd", "argv"}))
        if set(step) - allowed_fields:
            raise RetentionError(f"session step {index} has unrecognized fields")
        if step.get("cwd", ".") != ".":
            raise RetentionError("session command cwd must be the admitted BuildLease workspace")
        if step.get("kind") == "test":
            if not dual_graph:
                _session_relative_path(step.get("executable"), "test executable path")
            arguments = step.get("arguments", [])
            if (not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments)):
                raise RetentionError("registered test arguments must be a string array")
            command_bytes += sum(len(arg.encode("utf-8", "surrogateescape")) for arg in arguments)
        elif not dual_graph:
            command = step.get("argv")
            if (not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command)
                    or any(any(char in token for char in ("\x00", "`", "$(", "\n", "\r")) for token in command)):
                raise RetentionError(f"session step {index} argv is invalid")
            command_bytes += sum(len(arg.encode("utf-8", "surrogateescape")) for arg in command)
    if command_bytes > 64 * 1024:
        raise RetentionError("session command plan exceeds its bounded receipt size")
    return value


def _session_measure_tree(root: Path, *, workspace_mount_identity: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Measure one root, optionally charging a fixed workspace mount exactly once."""
    root = Path(root).resolve(strict=True)
    if workspace_mount_identity is not None:
        if workspace_mount_identity.get("readonly") is True:
            _verify_session_workspace_readonly(root, workspace_mount_identity)
        else:
            _verify_session_workspace_mount(root, workspace_mount_identity)
    root_fd = _open_dir(root)
    root_info = os.fstat(root_fd)
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    if root_fsid is None:
        os.close(root_fd)
        raise RetentionError("session build root has no filesystem identity")
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    allocated: dict[tuple[int, int], int] = {}
    link_counts: dict[tuple[int, int], tuple[int, int]] = {}
    logical = 0
    count = 0
    workspace_mount_seen = False
    backing_image_key: tuple[int, int] | None = None
    backing_image_logical = 0
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    if relative_dir == Path() and name == BUILD_MARKER:
                        continue
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = relative_dir / name
                    if (workspace_mount_identity is not None and relative == Path(BUILD_SESSION_WORKSPACE_NAME)):
                        if not stat.S_ISDIR(info.st_mode):
                            raise RetentionError("BuildLease workspace mount point is not a directory")
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        try:
                            opened = os.fstat(child_fd)
                            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                    or opened.st_dev != workspace_mount_identity.get("device")
                                    or getattr(os.fstatvfs(child_fd), "f_fsid", None)
                                        != workspace_mount_identity.get("filesystem_id")):
                                raise RetentionError("BuildLease workspace mount changed during accounting")
                            workspace_mount_seen = True
                        finally:
                            os.close(child_fd)
                        continue
                    if info.st_dev != root_info.st_dev:
                        raise RetentionError(f"session output crosses a filesystem boundary: {relative}")
                    if stat.S_ISLNK(info.st_mode):
                        raise RetentionError(f"session output contains a symlink: {relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened = os.fstat(child_fd)
                        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                            os.close(child_fd)
                            raise RetentionError(f"session output directory changed while scanning: {relative}")
                        stack.append((child_fd, relative))
                    elif stat.S_ISREG(info.st_mode):
                        key = (info.st_dev, info.st_ino)
                        allocated[key] = getattr(info, "st_blocks", 0) * 512
                        if relative == Path(BUILD_SESSION_WORKSPACE_IMAGE_NAME):
                            backing_image_key = key
                            backing_image_logical = info.st_size
                        prior = link_counts.get(key)
                        if prior is not None and prior[0] != info.st_nlink:
                            raise RetentionError(f"session output hardlink count changed while scanning: {relative}")
                        link_counts[key] = (info.st_nlink, (prior[1] if prior else 0) + 1)
                        logical += info.st_size
                        count += 1
                    else:
                        raise RetentionError(f"session output contains a special file: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        for (device, inode), (reported, observed) in link_counts.items():
            if reported != observed:
                raise RetentionError(f"session output file has hardlinks outside its tree: {device}:{inode}")
        outer_allocated = sum(allocated.values())
        if workspace_mount_identity is None:
            return {"allocated_bytes": outer_allocated, "logical_bytes": logical, "file_count": count}
        if not workspace_mount_seen or backing_image_key is None:
            raise RetentionError("session workspace mount or backing image disappeared during accounting")
        backing_allocated = allocated[backing_image_key]
        workspace_path = Path(str(workspace_mount_identity["mountpoint"]))
        workspace_usage = _session_measure_tree(workspace_path)
        non_image_outer = outer_allocated - backing_allocated
        if non_image_outer < 0:
            raise RetentionError("session backing image allocation is inconsistent with the outer tree")
        return {"allocated_bytes": non_image_outer + max(backing_allocated,
                                                           workspace_usage["allocated_bytes"]),
                "logical_bytes": logical - backing_image_logical + workspace_usage["logical_bytes"],
                "file_count": count - 1 + workspace_usage["file_count"],
                "outer_allocated_bytes": outer_allocated,
                "outer_non_image_allocated_bytes": non_image_outer,
                "backing_image_allocated_bytes": backing_allocated,
                "workspace_allocated_bytes": workspace_usage["allocated_bytes"],
                "workspace_logical_bytes": workspace_usage["logical_bytes"],
                "workspace_file_count": workspace_usage["file_count"]}
    finally:
        while stack:
            pending_fd, _ = stack.pop()
            if pending_fd != root_fd:
                try:
                    os.close(pending_fd)
                except OSError:
                    pass
        os.close(root_fd)


def _session_tree_digest(root: Path, *, exclude_controller_state: bool = False) -> tuple[str, int]:
    """Hash outputs deterministically and reject symlinks/special files."""
    root_fd = _open_dir(root)
    root_info = os.fstat(root_fd)
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    if root_fsid is None:
        os.close(root_fd)
        raise RetentionError("session output root has no filesystem identity")
    digest = hashlib.sha256()
    count = 0
    link_counts: dict[tuple[int, int], tuple[int, int]] = {}
    stack: list[tuple[int, Path]] = [(root_fd, Path())]
    try:
        while stack:
            parent_fd, relative_dir = stack.pop()
            try:
                for name in sorted(os.listdir(parent_fd)):
                    if (exclude_controller_state and relative_dir == Path()
                            and name in {BUILD_MARKER, BUILD_SESSION_DIRECTORY}):
                        continue
                    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    relative = relative_dir / name
                    if info.st_dev != root_info.st_dev:
                        raise RetentionError(f"session output crosses a filesystem boundary: {relative}")
                    digest.update(relative.as_posix().encode("utf-8", "surrogateescape") + b"\0")
                    digest.update(oct(stat.S_IMODE(info.st_mode)).encode() + b"\0")
                    if stat.S_ISLNK(info.st_mode):
                        raise RetentionError(f"session output contains a symlink: {relative}")
                    if stat.S_ISDIR(info.st_mode):
                        digest.update(b"D\n")
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        opened = os.fstat(child_fd)
                        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                                or getattr(os.fstatvfs(child_fd), "f_fsid", None) != root_fsid):
                            os.close(child_fd)
                            raise RetentionError(f"session output directory changed while hashing: {relative}")
                        stack.append((child_fd, relative))
                    elif stat.S_ISREG(info.st_mode):
                        key = (info.st_dev, info.st_ino)
                        prior = link_counts.get(key)
                        if prior is not None and prior[0] != info.st_nlink:
                            raise RetentionError(f"session output hardlink count changed while hashing: {relative}")
                        link_counts[key] = (info.st_nlink, (prior[1] if prior else 0) + 1)
                        child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                        try:
                            before = os.fstat(child_fd)
                            if ((before.st_dev, before.st_ino, before.st_size)
                                    != (info.st_dev, info.st_ino, info.st_size)):
                                raise RetentionError(f"session output changed while opening: {relative}")
                            file_hash = _hash_fd(child_fd)
                            after = os.fstat(child_fd)
                            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                                raise RetentionError(f"session output changed while hashing: {relative}")
                            digest.update(b"F\0" + file_hash.encode() + b"\n")
                        finally:
                            os.close(child_fd)
                        count += 1
                    else:
                        raise RetentionError(f"session output contains a special file: {relative}")
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        for (device, inode), (reported, observed) in link_counts.items():
            if reported != observed:
                raise RetentionError(f"session output file has hardlinks outside its tree: {device}:{inode}")
        return digest.hexdigest(), count
    finally:
        while stack:
            pending_fd, _ = stack.pop()
            if pending_fd != root_fd:
                try:
                    os.close(pending_fd)
                except OSError:
                    pass
        os.close(root_fd)


def _session_safe_path(root: Path, relative: Path, *, must_exist: bool,
                       directory: bool | None = None) -> Path:
    """Resolve a relative session path while rejecting symlink components."""
    root = root.resolve(strict=True)
    candidate = root
    for index, part in enumerate(relative.parts):
        if part in {"", ".", ".."}:
            raise RetentionError("session path contains an alias or traversal component")
        candidate = candidate / part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if must_exist or index != len(relative.parts) - 1:
                raise RetentionError(f"session path is missing: {candidate}")
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RetentionError(f"session path contains a symlink: {candidate}")
        if index != len(relative.parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise RetentionError(f"session path parent is not a directory: {candidate}")
    if candidate != root and root not in candidate.parents:
        raise RetentionError("session path escaped its declared root")
    if must_exist:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RetentionError(f"session path is unavailable: {candidate}: {error}") from error
        if resolved != candidate:
            raise RetentionError(f"session path is not canonical: {candidate}")
        info = candidate.stat(follow_symlinks=False)
        if directory is True and not stat.S_ISDIR(info.st_mode):
            raise RetentionError(f"session path is not a directory: {candidate}")
        if directory is False and not stat.S_ISREG(info.st_mode):
            raise RetentionError(f"session path is not a regular file: {candidate}")
    return candidate


def _session_hash_executable(path: Path, expected_sha256: str, *, require_exec: bool = True) -> dict[str, Any]:
    selected = Path(os.path.abspath(path))
    if selected.is_symlink() or selected.resolve(strict=True) != selected:
        raise RetentionError(f"session executable is not canonical: {selected}")
    info = selected.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or (require_exec and not info.st_mode & 0o111):
        raise RetentionError(f"session executable is not a regular executable: {selected}")
    digest = _hash_path(selected)
    after = selected.stat(follow_symlinks=False)
    if ((info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)):
        raise RetentionError(f"session executable changed while hashing: {selected}")
    if digest != expected_sha256:
        raise RetentionError(f"session executable hash changed: {selected}")
    return {"path": str(selected), "sha256": digest, "device": info.st_dev,
            "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}


def _sandbox_session_command(command: list[str], build_root: Path,
                             environment: Mapping[str, str], *,
                             write_root: Path | None = None,
                             temporary_directory: Path | None = None) -> tuple[list[str], dict[str, str], str]:
    """Confine v3 command writes to its exact mounted workspace subtree."""
    if sys.platform != "darwin":
        raise RetentionError("HOST_BUILDLEASE_V3 requires macOS Seatbelt and hdiutil")
    if not command or any(not isinstance(arg, str) or "\x00" in arg for arg in command):
        raise RetentionError("HOST_BUILDLEASE_V3 command must be a non-empty argv array")
    sandbox_exec = Path("/usr/bin/sandbox-exec")
    if (sandbox_exec.is_symlink() or not sandbox_exec.is_file()
            or sandbox_exec.resolve(strict=True) != sandbox_exec):
        raise RetentionError("the verified system sandbox-exec launcher is unavailable")
    raw_workspace = Path(os.path.abspath(build_root))
    workspace = raw_workspace.resolve(strict=True)
    if raw_workspace != workspace or raw_workspace.is_symlink():
        raise RetentionError("HOST_BUILDLEASE_V3 workspace path is not canonical")
    raw_allowed_root = Path(os.path.abspath(write_root or workspace))
    allowed_root = raw_allowed_root.resolve(strict=True)
    if raw_allowed_root != allowed_root or raw_allowed_root.is_symlink():
        raise RetentionError("HOST_BUILDLEASE_V3 write root is not canonical")
    if (allowed_root != workspace and workspace not in allowed_root.parents):
        raise RetentionError("HOST_BUILDLEASE_V3 write root must be inside the exact workspace image")
    raw_temp_path = Path(os.path.abspath(temporary_directory or (allowed_root / ".tmp")))
    if raw_temp_path != allowed_root and allowed_root not in raw_temp_path.parents:
        raise RetentionError("HOST_BUILDLEASE_V3 temporary directory must be inside the write allowlist")
    cursor = allowed_root
    if cursor.is_symlink():
        raise RetentionError("HOST_BUILDLEASE_V3 write root contains a symlink")
    for part in raw_temp_path.relative_to(allowed_root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RetentionError("HOST_BUILDLEASE_V3 temporary path contains a symlink")
    temp_path = raw_temp_path.resolve(strict=False)
    if temp_path != raw_temp_path:
        raise RetentionError("HOST_BUILDLEASE_V3 temporary path is not canonical")
    if temp_path != allowed_root and allowed_root not in temp_path.parents:
        raise RetentionError("HOST_BUILDLEASE_V3 temporary directory must be inside the write allowlist")
    root_info = workspace.stat(follow_symlinks=False)
    allowed_info = allowed_root.stat(follow_symlinks=False)
    temp_info = temp_path.stat(follow_symlinks=False) if temp_path.exists() else None
    if (not stat.S_ISDIR(root_info.st_mode) or not stat.S_ISDIR(allowed_info.st_mode)
            or (temp_info is not None and not stat.S_ISDIR(temp_info.st_mode))
            or root_info.st_dev != allowed_info.st_dev
            or (temp_info is not None and allowed_info.st_dev != temp_info.st_dev)
            or any(path.is_symlink() for path in (workspace, allowed_root))
            or (temp_path.exists() and temp_path.is_symlink())):
        raise RetentionError("HOST_BUILDLEASE_V3 write/temp paths are not real directories on one image")

    home = (temp_path if write_root is not None else workspace / ".home").resolve(strict=False)
    if home != allowed_root and allowed_root not in home.parents:
        raise RetentionError("HOST_BUILDLEASE_V3 HOME must remain inside the exact write allowlist")
    xdg_paths = {
        "XDG_CACHE_HOME": home / ".cache",
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
    }
    for path in [home, *xdg_paths.values()]:
        if path.exists():
            info = path.stat(follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or info.st_dev != allowed_info.st_dev
                    or path.is_symlink()):
                raise RetentionError("HOST_BUILDLEASE_V3 HOME/cache paths escaped the mounted write root")

    def sbpl_quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    profile = "\n".join((
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow sysctl-read)",
        "(allow system-info)",
        "(allow file-read*)",
        '(allow file-write* (literal "/dev/null") (literal "/private/dev/null"))',
        f"(allow file-write* (subpath {sbpl_quote(str(allowed_root))}))",
    )) + "\n"
    selected_env = dict(environment)
    selected_env.update({"HOME": str(home), "TMPDIR": str(temp_path), "TMP": str(temp_path),
                         "TEMP": str(temp_path),
                         **{key: str(path) for key, path in xdg_paths.items()}})
    return [str(sandbox_exec), "-p", profile, *command], selected_env, str(sandbox_exec)


class BuildLease:
    """Reserve before mkdir; successful completion artifactizes and retires synchronously."""
    def __init__(self, build_id: str, *, kind: str, owner_experiment: str, purpose: str,
                 source_tree_hash: str, expected_bytes: int, metadata: Mapping[str, Any] | None = None,
                 preserve_debug_reason: str | None = None, runner_path: Path | str,
                 configuration_key_sha256: str | None = None,
                 session_plan: Mapping[str, Any] | None = None):
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
        self.session_plan = (_session_validate_plan(
            session_plan, build_id=build_id, build_path=self.path,
            source_tree_hash=source_tree_hash, expected_bytes=expected_bytes)
            if session_plan is not None else None)
        self.session_plan_sha256 = (hashlib.sha256(_canonical_json(self.session_plan)).hexdigest()
                                    if self.session_plan is not None else None)
        self.session = None
        self._session_construction_token = object()
        self._session_creation_lock = threading.Lock()
        self._lease_state_lock = threading.Lock()
        self.started = False
        self.reused_manifest = None
        self.lock_fds = None
        self.stop_event = None
        self.beat_thread = None
        self.storage_allocation_lock_file = None
        self.storage_admission_warnings: list[str] = []
        self.storage_watch_events: list[dict[str, Any]] = []
        self._free_warning_emitted = False

    def _assert_session_plan_binding(self) -> None:
        if self.session_plan is None:
            return
        observed = hashlib.sha256(_canonical_json(self.session_plan)).hexdigest()
        if observed != self.session_plan_sha256:
            raise RetentionError("BuildLease session plan changed after its signed admission binding")

    def _validate_session_inputs(self) -> dict[str, Any]:
        """Revalidate source, toolchain, and declared build inputs against the signed plan."""
        if self.session_plan is None:
            raise RetentionError("this BuildLease has no signed v3 session plan")
        plan = self.session_plan
        source = plan["source_identity"]
        source_root = Path(source["root"])
        if source_root.is_symlink() or source_root.resolve(strict=True) != source_root:
            raise RetentionError("session source root is not a canonical real directory")
        source_info = source_root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(source_info.st_mode):
            raise RetentionError("session source root is not a directory")
        source_hash, source_count = _tree_hash(
            source_root, reject_symlinks=True, reject_external_hardlinks=True)
        if source_hash != self.source_tree_hash:
            raise RetentionError("session source tree changed from the admitted digest")
        tool_identities: dict[str, dict[str, Any]] = {}
        for name, receipt in sorted(plan["tools"].items()):
            identity = _session_hash_executable(Path(receipt["path"]), receipt["sha256"])
            if identity["path"] != receipt["path"]:
                raise RetentionError(f"session {name} tool path is not canonical")
            tool_identities[name] = identity
        git = plan["tools"]["git"]
        result = subprocess.run(
            [git["path"], "-C", str(source_root), "rev-parse", "--verify", "HEAD"],
            env=dict(plan["environment"]), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=False, timeout=30)
        revision = result.stdout.strip()
        if result.returncode != 0 or revision != source["revision"]:
            raise RetentionError("session Git revision differs from the admitted source revision")
        input_identities: list[dict[str, Any]] = []
        for row in plan.get("input_receipts", []):
            relative = _session_relative_path(row["path"], "input receipt path")
            path = _session_safe_path(source_root, relative, must_exist=True, directory=False)
            digest = _hash_path(path)
            if digest != row["sha256"]:
                raise RetentionError(f"session build input changed: {relative}")
            info = path.stat(follow_symlinks=False)
            input_identities.append({"path": relative.as_posix(), "sha256": digest,
                                     "device": info.st_dev, "inode": info.st_ino})
        graph_tool_identities: dict[str, dict[str, Any]] = {}
        runtime_identities: list[dict[str, Any]] = []
        host_dependency_identities: list[dict[str, Any]] = []
        native_host_profile: dict[str, Any] = {}
        native_toolchain_selection: dict[str, Any] = {}
        if _session_dual_graph_enabled(plan):
            for graph in plan["build_graphs"]:
                graph_tool_identities[graph["graph_id"]] = {
                    tool_id: tool_identities[tool_id]
                    for tool_id in graph["tool_receipt_ids"]
                }
            for row in plan["runtime_receipts"]:
                runtime_identities.append(_session_regular_file_identity(
                    Path(row["path"]), row["sha256"], f"native {row['runtime_id']} runtime"))
            (host_dependency_identities, native_host_profile,
             native_toolchain_selection) = _session_host_dependency_identities(plan)
        path_dirs = []
        path_dir_identities = []
        for raw in plan["environment"]["PATH"].split(os.pathsep):
            path_dir = Path(raw)
            if path_dir.is_symlink() or path_dir.resolve(strict=True) != path_dir or not path_dir.is_dir():
                raise RetentionError(f"session PATH entry is not a canonical directory: {path_dir}")
            if path_dir.stat().st_mode & 0o022:
                raise RetentionError(f"session PATH entry is writable by group or other users: {path_dir}")
            if (path_dir == self.path or self.path in path_dir.parents
                    or path_dir == source_root or source_root in path_dir.parents):
                raise RetentionError("session PATH may not include the mutable build or source workspace")
            path_dirs.append(str(path_dir))
            path_info = path_dir.stat(follow_symlinks=False)
            path_dir_identities.append({"path": str(path_dir), "device": path_info.st_dev,
                                        "inode": path_info.st_ino, "mode": stat.S_IMODE(path_info.st_mode),
                                        "owner_uid": path_info.st_uid})
        for name in ("meson", "ninja", "git"):
            selected = shutil.which(name, path=plan["environment"]["PATH"])
            if selected is None or Path(selected).resolve(strict=True) != Path(plan["tools"][name]["path"]):
                raise RetentionError(f"session PATH resolves {name} to an unregistered executable")
        if _session_dual_graph_enabled(plan):
            if plan["environment"].get("PATH") != BUILD_SESSION_DUAL_GRAPH_PATH:
                raise RetentionError("dual-graph PATH changed from the controller-owned closed search path")
            meson_script = Path(plan["tools"]["meson"]["path"])
            try:
                with meson_script.open("rb") as meson_stream:
                    shebang = meson_stream.readline(4096).decode("utf-8").strip()
            except (OSError, UnicodeDecodeError) as error:
                raise RetentionError("native Meson launcher has no readable fixed Python interpreter line") from error
            python_alias = Path(BUILD_SESSION_DUAL_GRAPH_MESON_PYTHON_SHEBANG)
            python_path = Path(plan["tools"]["meson_python"]["path"])
            if (shebang != f"#!{python_alias}" or not python_alias.is_symlink()
                    or python_alias.resolve(strict=True) != python_path):
                raise RetentionError("native Meson launcher does not resolve to its bound Python interpreter")
            for command_name, receipt_id in BUILD_SESSION_DUAL_GRAPH_PATH_TOOL_RECEIPTS.items():
                selected = shutil.which(command_name, path=plan["environment"]["PATH"])
                if (selected is None
                        or Path(selected).resolve(strict=True) != Path(plan["tools"][receipt_id]["path"])):
                    raise RetentionError(
                        f"dual-graph PATH does not resolve {command_name} to its exact tool receipt")
            for command_name in BUILD_SESSION_DUAL_GRAPH_PATH_ABSENT_EXECUTABLES:
                if shutil.which(command_name, path=plan["environment"]["PATH"]) is not None:
                    raise RetentionError(
                        f"dual-graph PATH unexpectedly exposes unreceipted executable {command_name}")
        environment_tools: dict[str, str] = {}
        for variable in ("CC", "CXX", "AR", "RANLIB"):
            selected = plan["environment"].get(variable)
            if selected is None:
                continue
            match = next((row for row in tool_identities.values() if row["path"] == selected), None)
            if match is None:
                raise RetentionError(f"session {variable} is not bound to a registered tool receipt")
            environment_tools[variable] = match["sha256"]
        if (not _session_dual_graph_enabled(plan)
                and not {"CC", "CXX"}.issubset(environment_tools)):
            raise RetentionError("session must bind both C and C++ compiler executables")
        if _session_dual_graph_enabled(plan) and environment_tools:
            raise RetentionError("dual-graph compiler selection must be derived from the typed graph role")
        return {"source_root": str(source_root), "source_revision": revision,
                "source_tree_sha256": source_hash, "source_file_count": source_count,
                "source_device": source_info.st_dev, "source_inode": source_info.st_ino,
                "tools": tool_identities, "input_receipts": input_identities,
                "graph_tools": graph_tool_identities, "runtime_inputs": runtime_identities,
                "host_dependencies": host_dependency_identities,
                "native_host_profile": native_host_profile,
                "native_toolchain_selection": native_toolchain_selection,
                "path_directories": path_dirs, "path_directory_identities": path_dir_identities,
                "environment_tool_hashes": environment_tools}

    def start_session(self) -> "BuildLeaseSession":
        if not self._session_creation_lock.acquire(blocking=False):
            raise RetentionError("BuildLease session creation is already in progress")
        try:
            return self._start_session_locked()
        finally:
            self._session_creation_lock.release()

    def _start_session_locked(self) -> "BuildLeaseSession":
        if self.session_plan is None:
            raise RetentionError("BuildLease session API requires a separately bound v3 session plan")
        self._assert_session_plan_binding()
        if not self.started or self.reused_manifest is not None:
            raise RetentionError("session start requires this BuildLease's one active reservation")
        if self.session is not None:
            raise RetentionError("BuildLease session was already started")
        self._assert_live_session_reservation_capability()
        self.session = BuildLeaseSession(self, _construction_token=self._session_construction_token)
        try:
            self.session.start()
        except BaseException as error:
            # Failures before the durable start marker are still never-started
            # reservations. Keep them RESERVED for explicit orphan closeout after
            # this process exits; never synthesize a generic crash failure here.
            try:
                started_marker = _load_lease(self.build_id).get("session_started") is True
            except BaseException:
                started_marker = True
            if not started_marker:
                raise
            try:
                self.session._fail(repr(error), -1, command=["session-start"])
            except BaseException as capture_error:
                raise RetentionError(
                    f"session startup failed and failure capture could not finish: {capture_error}") from error
            raise
        return self.session

    def reserve(self) -> Path | None:
        from storage_policy import (_acquire_storage_allocation_lock, _experiment_manifest_path,
                                    _read_file_nofollow, _validated_manifest_estimates,
                                    _verify_manifest_signature, GATE0_ATTESTATION_PATH,
                                    conservative_project_usage_bytes, disk_free_bytes,
                                    measure_project_usage, projected_storage_admission)
        self.storage_allocation_lock_file = _acquire_storage_allocation_lock()
        try:
            manifest_path = _experiment_manifest_path(self.owner_experiment)
            manifest_raw = _read_file_nofollow(Path(manifest_path))
            manifest = json.loads(manifest_raw)
            if not _verify_manifest_signature(manifest):
                raise RetentionError("BuildLease owner manifest is unsigned or has been edited")
            if self.session_plan is not None:
                self._assert_session_plan_binding()
                if (manifest.get("build_session_plan_sha256") != self.session_plan_sha256
                        or manifest.get("execution_admission_identity")
                            != self.session_plan["admission_identity"]):
                    raise RetentionError(
                        "session plan is not cryptographically bound to the signed execution admission")
                self._validate_session_inputs()
            elif manifest.get("build_session_plan_sha256") is not None:
                raise RetentionError("signed session admission requires the separately versioned session API")
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
            current_project_usage = measure_project_usage()
            current_project = conservative_project_usage_bytes(current_project_usage)
            peak = estimates["estimated_projected_new_disk_bytes"]
            admission = projected_storage_admission(
                current_free_bytes=free_now,
                current_scoped_bytes=current_project,
                peak_additional_bytes=peak,
            )
            if not admission["admitted"]:
                raise RetentionError("live projected-peak check blocks the BuildLease reservation: "
                                     + "; ".join(admission["blockers"]))
            self.storage_admission_warnings = list(admission["warnings"])
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
            reusable = (None if self.session_plan is not None else find_canonical_equivalent(
                config_key_sha256=self.configuration_key_sha256, kind=self.kind,
                source_tree_hash=self.source_tree_hash,
                dependency_state=self.metadata.get("dependency_state")))
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
                         "storage_admission_warnings": self.storage_admission_warnings,
                         "storage_admission_peak_bytes": peak,
                         "requested_config_key_sha256": self.configuration_key_sha256}
                if self.session_plan is not None:
                    lease["session_started"] = False
                    lease["session_v3"] = {
                        "schema_version": BUILD_SESSION_SCHEMA_VERSION,
                        "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                        "plan_sha256": self.session_plan_sha256,
                        "admission_identity": self.session_plan["admission_identity"],
                        "source_identity": self.session_plan["source_identity"],
                        "state": "RESERVED",
                        "next_step_index": 0,
                        "step_receipts": [],
                        "peak_allocated_bytes": 0,
                    }
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

    def _assert_live_session_reservation_capability(self) -> None:
        """Validate this object's still-held descriptors immediately before v3 start."""
        if (self.session_plan is None or not self.started or self.reused_manifest is not None
                or self.path != BUILD_ROOT / self.build_id
                or not isinstance(self.lock_fds, tuple) or len(self.lock_fds) != 2):
            raise RetentionError("same-process session start lacks its live BuildLease capability")
        lease_fd, lock_fd = self.lock_fds
        try:
            os.fstat(lease_fd)
            os.fstat(lock_fd)
        except OSError as error:
            raise RetentionError("same-process BuildLease lock capability was lost before session start") from error
        _verify_named_retention_lock(lease_fd, lock_fd)
        record_raw = _read_at(lease_fd, f"{self.build_id}.json", limit=1024 * 1024)
        try:
            lease = json.loads(record_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RetentionError("same-process reservation record became malformed before start") from error
        session = lease.get("session_v3") if isinstance(lease, dict) else None
        if (not isinstance(lease, dict) or not _verify_seal(lease)
                or lease.get("build_id") != self.build_id or lease.get("path") != str(self.path)
                or lease.get("owner_pid") != os.getpid() or lease.get("state") != "ACTIVE"
                or lease.get("session_started") is not False
                or not isinstance(session, dict) or session.get("state") != "RESERVED"
                or session.get("plan_sha256") != self.session_plan_sha256
                or session.get("admission_identity") != self.session_plan["admission_identity"]
                or session.get("next_step_index") != 0 or session.get("peak_allocated_bytes") != 0
                or session.get("step_receipts") != []):
            raise RetentionError("same-process reservation identity or start boundary changed")
        root_fd = _open_dir(self.path)
        try:
            marker = _read_marker(root_fd)
            if _canonical_json(marker) != _canonical_json(lease):
                raise RetentionError("same-process reservation marker changed before session start")
        finally:
            os.close(root_fd)

    def execute_admitted_plan(self) -> dict[str, Any]:
        """Reserve, start, execute, and finalize one frozen v3 plan in this process.

        There is intentionally no callback or return boundary between a successful
        reservation and starting a session on this exact BuildLease object.
        """
        if self.session_plan is None:
            raise RetentionError("admitted-plan execution requires a frozen Host-v3 session plan")
        if self.started or self.lock_fds is not None or self.session is not None:
            raise RetentionError("admitted-plan execution requires a fresh BuildLease object")
        reserved_path = self.reserve()
        if reserved_path != self.path:
            raise RetentionError("admitted-plan reservation did not create its exact new lease")
        self._assert_live_session_reservation_capability()
        session = self.start_session()
        for step in session.plan["steps"]:
            if step.get("kind") == "source_verification":
                session.verify_source()
            else:
                session.run_next()
        return session.finalize()

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
        observed = _load_lease(self.build_id)
        if observed.get("state") != "ACTIVE" or observed.get("path") != str(self.path):
            raise RetentionError("build lease is no longer ACTIVE")
        # The first read is only a liveness check. Re-read under the state mutex so
        # an older snapshot can never replace a newer session state or receipt list.
        with self._lease_state_lock:
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
                marker.update(lease)
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
        if lease.get("session_v3") is not None and lease.get("state") != "ACTIVE":
            raise RetentionError("a v3 BuildLease session can be finalized only once")
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
        if preserve_debug and lease.get("session_v3") is not None:
            raise RetentionError("v3 session workspaces cannot be preserved as debug builds")
        is_host_v3_session = isinstance(lease.get("session_v3"), dict)
        execution = self._verified_execution_receipt(lease)
        if not is_host_v3_session:
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
        if not is_host_v3_session:
            execution_evidence = _copy_evidence(
                self.path / BUILD_EXECUTION_RECEIPT, evidence / "execution/build-execution.json")
        artifact_root = (Path(self.session_plan["build_workspace"])
                         if lease.get("session_v3") is not None and self.session_plan is not None
                         else self.path)
        if lease.get("metadata", {}).get("native_moltenvk_request"):
            native_req = _dict_to_native_request(lease["metadata"]["native_moltenvk_request"])
            outputs, identity = _copy_tree_artifacts_native_moltenvk(artifact_root, self.build_id, evidence, native_req)
        else:
            outputs, identity = _copy_tree_artifacts(artifact_root, self.build_id, evidence)
        if not is_host_v3_session:
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
        if is_host_v3_session:
            session_record = lease.get("session_v3")
            if (not isinstance(session_record, dict)
                    or session_record.get("state") != "READY_TO_FINALIZE"
                    or not isinstance(session_record.get("readonly_mount_identity"), dict)):
                raise RetentionError("v3 session lost its verified read-only image before artifact promotion")
            if (session_record.get("acceptance_state")
                    != "UNACCEPTED_PENDING_NORMAL_READONLY_DETACH_AND_MANIFEST"
                    or session_record.get("write_quiescence_state")
                    != "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED"):
                raise RetentionError("v3 session acceptance advanced before its read-only evidence detach")
            current_output = _session_safe_path(
                artifact_root,
                _session_relative_path(self.session_plan["output_root"], "output_root", allow_dot=True),
                must_exist=True, directory=True)
            output_tree, output_count = _session_tree_digest(current_output, exclude_controller_state=True)
            terminal_path = self.path / BUILD_SESSION_DIRECTORY / BUILD_SESSION_TERMINAL
            terminal = _read_json(terminal_path, sealed=True)
            if (terminal.get("state") != "PENDING_SUCCESS"
                    or terminal.get("output_tree_sha256") != output_tree
                    or terminal.get("output_file_count") != output_count):
                raise RetentionError("read-only BuildLease output changed before normal evidence detach")
            final_source = self._validate_session_inputs()
            if terminal.get("final_source_verification") != final_source:
                raise RetentionError("accepted source changed before host v3 output promotion")
            captured_source = identity.get("source_identity")
            if (not isinstance(captured_source, Mapping)
                    or captured_source.get("path") != final_source.get("source_root")
                    or captured_source.get("content_tree_sha256") != final_source.get("source_tree_sha256")):
                raise RetentionError("artifactized build source differs from the exact v3 source verification")
            readonly_mount = session_record["readonly_mount_identity"]
            _verify_session_workspace_readonly(self.path, readonly_mount)
            detached_readonly = _detach_session_workspace_readonly(
                self.path, readonly_mount)
            if detached_readonly.get("detached_image_identity") != session_record.get("detached_image_identity"):
                raise RetentionError("read-only evidence detach changed the exact accepted image contents")
            terminal.update({"state": "SUCCESS",
                             "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                             "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                             "acceptance_state": "HOST_BUILDLEASE_V3_WRITER_QUIESCED",
                             "write_quiescence_state": "WRITABLE_IMAGE_DETACHED_READONLY_EVIDENCE_HASHED",
                             "readonly_detached_identity": detached_readonly,
                             "readonly_detached_at_unix": detached_readonly.get("detached_at_unix"),
                             "accepted_at_unix": time.time()})
            _write_json(terminal_path, terminal, replace=True, sealed=True)
            session_record["terminal_sha256"] = _hash_path(terminal_path)
            execution = {**execution, **terminal}
            execution_evidence = _copy_evidence(
                terminal_path, evidence / "execution/session-terminal.json")
            identity["sandboxed_execution_evidence"] = execution_evidence
            identity["sandboxed_execution_executor_mode"] = BUILD_SESSION_EXECUTOR_MODE
            identity["sandboxed_execution_executor_version"] = BUILD_SESSION_SCHEMA_VERSION
            identity["sandboxed_execution_acceptance_state"] = "HOST_BUILDLEASE_V3_WRITER_QUIESCED"
            identity["sandboxed_execution_write_quiescence_state"] = (
                "WRITABLE_IMAGE_DETACHED_READONLY_EVIDENCE_HASHED")
            identity["host_buildlease_v3_final_source_verification"] = final_source
            identity["host_buildlease_v3_detached_image_identity"] = session_record["detached_image_identity"]
            identity["host_buildlease_v3_readonly_mount_identity"] = readonly_mount
            identity["host_buildlease_v3_readonly_detached_identity"] = detached_readonly
            identity["host_buildlease_v3_output_tree_sha256"] = output_tree
            identity["host_buildlease_v3_output_file_count"] = output_count
            if self.session is None:
                raise RetentionError("v3 completion lost its owning session persistence guard")
        else:
            identity["sandboxed_execution_executor_mode"] = "HOST_SEATBELT_V1"
        manifest, digest = _build_manifest(lease, outputs, identity, evidence,
                                           provenance_scope="captured during successful build completion")
        state = "PRESERVE_DEBUG" if preserve_debug else "COMPLETED"
        if is_host_v3_session:
            session_record = dict(lease["session_v3"])
            session_record["readonly_detached_identity"] = detached_readonly
            session_record["readonly_detached_at_unix"] = detached_readonly.get("detached_at_unix")
            session_record["acceptance_state"] = "HOST_BUILDLEASE_V3_WRITER_QUIESCED"
            session_record["write_quiescence_state"] = "WRITABLE_IMAGE_DETACHED_READONLY_EVIDENCE_HASHED"
            session_record["state"] = "ACCEPTED"
            lease["session_v3"] = session_record
        lease.update({"state": state, "build_manifest_id": manifest["build_manifest_id"],
                      "build_manifest_path": str(MANIFEST_ROOT / f"{self.build_id}.json"),
                      "build_manifest_sha256": digest, "completed_at_unix": time.time()})
        with self._lease_state_lock:
            _store_lease(lease)
            root_fd = _open_dir(self.path)
            try:
                marker = _read_marker(root_fd)
                marker.update(lease)
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
        if lease.get("session_v3") is not None:
            return self._verified_session_execution_receipt(lease)
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

    def _verified_session_execution_receipt(self, lease: Mapping[str, Any]) -> dict[str, Any]:
        """Verify ordered v3 evidence, explicitly refusing any legacy/session mixture."""
        _require_buildlease_vm_runtime(self.session_plan or {})
        session = lease.get("session_v3")
        if (not isinstance(session, dict) or session.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
                or session.get("state") != "READY_TO_FINALIZE"
                or session.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                or session.get("executor_version") != BUILD_SESSION_SCHEMA_VERSION
                or session.get("acceptance_state") != "UNACCEPTED_PENDING_NORMAL_READONLY_DETACH_AND_MANIFEST"
                or session.get("write_quiescence_state") != "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED"
                or session.get("plan_sha256") != self.session_plan_sha256
                or self.session_plan is None):
            raise RetentionError("v3 session is not ready for its single explicit finalization")
        if (self.path / BUILD_EXECUTION_RECEIPT).exists() or (self.path / "build-command.json").exists():
            raise RetentionError("legacy v1 and v3 session execution receipts may not be mixed")
        session_dir = self.path / BUILD_SESSION_DIRECTORY
        try:
            plan_receipt = _read_json(session_dir / BUILD_SESSION_PLAN, sealed=True)
            terminal = _read_json(session_dir / BUILD_SESSION_TERMINAL, sealed=True)
            storage_watch_events = session.get("storage_watch_events")
            if (not isinstance(storage_watch_events, list)
                    or len(storage_watch_events) > BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS
                    or terminal.get("storage_watch_events") != storage_watch_events):
                raise RetentionError("v3 session storage telemetry is missing, oversized, or changed")
            if (plan_receipt.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
                    or plan_receipt.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                    or plan_receipt.get("executor_version") != BUILD_SESSION_SCHEMA_VERSION
                    or plan_receipt.get("plan_sha256") != self.session_plan_sha256
                    or plan_receipt.get("plan") != self.session_plan
                    or plan_receipt.get("admission_identity") != self.session_plan["admission_identity"]
                    or plan_receipt.get("source_identity") != self.session_plan["source_identity"]
                    or plan_receipt.get("build_root_identity") != session.get("build_root_identity")
                    or terminal.get("plan_sha256") != self.session_plan_sha256
                    or terminal.get("build_id") != self.build_id
                    or terminal.get("state") != "PENDING_SUCCESS"
                    or terminal.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                    or terminal.get("executor_version") != BUILD_SESSION_SCHEMA_VERSION
                    or terminal.get("acceptance_state")
                        != "UNACCEPTED_PENDING_NORMAL_READONLY_DETACH_AND_MANIFEST"
                    or terminal.get("write_quiescence_state")
                        != "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED"
                    or terminal.get("admission_identity") != session.get("admission_identity")
                    or terminal.get("source_identity") != session.get("source_identity")
                    or terminal.get("build_root_identity") != session.get("build_root_identity")
                    or session.get("admission_identity") != self.session_plan["admission_identity"]
                    or session.get("source_identity") != self.session_plan["source_identity"]
                    or terminal.get("step_count") != len(self.session_plan["steps"])
                    or terminal.get("reservation_bytes") != self.expected_bytes
                    or terminal.get("step_receipts") != session.get("step_receipts")
                    or len(session.get("step_receipts", [])) != len(self.session_plan["steps"])
                    or session.get("terminal_sha256") != _hash_path(session_dir / BUILD_SESSION_TERMINAL)):
                raise RetentionError("v3 terminal receipt does not bind its signed plan and active lease")
            receipt_hashes: list[str] = []
            expected_inputs = self._validate_session_inputs()
            sandbox_profile_hashes: set[str] = set()
            sandbox_launcher_hashes: set[str] = set()
            last_sandbox_profile_hash: str | None = None
            dual_graph = _session_dual_graph_enabled(self.session_plan)
            last_stage_tree_identity = session.get("initial_stage_tree_identity") if dual_graph else None
            windows_pe_stage_identity: Mapping[str, Any] | None = None
            native_runtime_stage_identity: Mapping[str, Any] | None = None
            for index, step in enumerate(self.session_plan["steps"]):
                step_name = f"step-{index:03d}"
                result_path = session_dir / "steps" / f"{step_name}.result.json"
                command_path = session_dir / "steps" / f"{step_name}.command.json"
                command_receipt = _read_json(command_path, sealed=True)
                result_receipt = _read_json(result_path, sealed=True)
                receipt_row = session["step_receipts"][index]
                if (receipt_row.get("step_index") != index
                        or command_receipt.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
                        or command_receipt.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                        or command_receipt.get("executor_version") != BUILD_SESSION_SCHEMA_VERSION
                        or command_receipt.get("acceptance_state") != "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH"
                        or command_receipt.get("build_id") != self.build_id
                        or command_receipt.get("plan_sha256") != self.session_plan_sha256
                        or command_receipt.get("step_index") != index
                        or command_receipt.get("kind") != step["kind"]
                        or result_receipt.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION
                        or result_receipt.get("executor_mode") != BUILD_SESSION_EXECUTOR_MODE
                        or result_receipt.get("executor_version") != BUILD_SESSION_SCHEMA_VERSION
                        or result_receipt.get("acceptance_state") != "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH"
                        or result_receipt.get("build_id") != self.build_id
                        or result_receipt.get("plan_sha256") != self.session_plan_sha256
                        or result_receipt.get("step_index") != index
                        or result_receipt.get("kind") != step["kind"]
                        or result_receipt.get("status") != "SUCCESS"
                        or result_receipt.get("return_code") != 0
                        or command_receipt.get("source_observation") != result_receipt.get("source_observation")
                        or result_receipt.get("command_receipt_sha256") != _hash_path(command_path)
                        or receipt_row.get("command_receipt_sha256") != _hash_path(command_path)
                        or receipt_row.get("result_receipt_sha256") != _hash_path(result_path)
                        or result_receipt.get("peak_allocated_bytes", self.expected_bytes + 1) > self.expected_bytes):
                    raise RetentionError("v3 session step sequence is missing, duplicated, or reordered")
                if command_receipt.get("cwd") != self.session_plan["build_workspace"]:
                    raise RetentionError("v3 command receipt cwd differs from the admitted BuildLease")
                if step["kind"] == "source_verification":
                    if (command_receipt.get("command") is not None
                            or command_receipt.get("read_only") is not True
                            or result_receipt.get("log_path") is not None
                            or command_receipt.get("source_observation") != terminal.get("final_source_verification")
                            or result_receipt.get("source_observation") != terminal.get("final_source_verification")):
                        raise RetentionError("v3 final source verification is not read-only and exact")
                    if dual_graph:
                        if self.session is None:
                            raise RetentionError("dual-graph final receipt verifier is unavailable")
                        final_graph_outputs = self.session._verify_graph_outputs(session)
                        final_stage_tree = self.session._verify_stage_outputs(session, require_complete=True)
                        final_stage_identity = {
                            "windows_pe_stage_identity": session.get("windows_pe_stage_identity"),
                            "native_runtime_stage_identity": session.get("native_runtime_stage_identity"),
                            "stage_install_tree_identity": session.get("stage_install_tree_identity"),
                        }
                        if (command_receipt.get("input_tool_identities") != expected_inputs["tools"]
                                or command_receipt.get("input_receipts") != expected_inputs["input_receipts"]
                                or command_receipt.get("graph_tool_identities") != expected_inputs["graph_tools"]
                                or command_receipt.get("runtime_input_identities") != expected_inputs["runtime_inputs"]
                                or command_receipt.get("graph_directory_identities")
                                    != session.get("graph_directory_identities")
                                or command_receipt.get("graph_output_identities") != final_graph_outputs
                                or result_receipt.get("input_tool_identities") != expected_inputs["tools"]
                                or result_receipt.get("input_receipts") != expected_inputs["input_receipts"]
                                or result_receipt.get("graph_tool_identities") != expected_inputs["graph_tools"]
                                or result_receipt.get("runtime_input_identities") != expected_inputs["runtime_inputs"]
                                or result_receipt.get("graph_directory_identities")
                                    != session.get("graph_directory_identities")
                                or result_receipt.get("graph_output_identities") != final_graph_outputs
                                or command_receipt.get("stage_operation_identity") != final_stage_identity
                                or result_receipt.get("stage_operation_identity") != final_stage_identity
                                or command_receipt.get("stage_tree_identity") != final_stage_tree
                                or result_receipt.get("stage_tree_identity") != final_stage_tree
                                or terminal.get("graph_directory_identities")
                                    != session.get("graph_directory_identities")
                                or terminal.get("graph_output_identities") != final_graph_outputs
                                or terminal.get("stage_operation_identity") != final_stage_identity
                                or terminal.get("stage_tree_identity") != final_stage_tree):
                            raise RetentionError("dual-graph final receipt omits or changes graph/output/stage identities")
                else:
                    if self.session is None:
                        raise RetentionError("v3 command verifier is unavailable")
                    typed_stage = (dual_graph and step["kind"] in
                                   {"stage_windows_product", "stage_native_runtime"})
                    if typed_stage:
                        action = (BUILD_SESSION_DUAL_GRAPH_WINDOWS_STAGE_ACTION
                                  if step["kind"] == "stage_windows_product"
                                  else BUILD_SESSION_DUAL_GRAPH_NATIVE_STAGE_ACTION)
                        graph = _session_dual_graph_by_id(self.session_plan, str(step["graph_id"]))
                        expected_graph_directory = session.get("graph_directory_identities", {}).get(
                            graph["graph_id"])
                        prior_output_rows = []
                        for prior_graph in self.session_plan["build_graphs"]:
                            for output in prior_graph["expected_outputs"]:
                                producer_index = next((prior_index for prior_index, prior_step in
                                                       enumerate(self.session_plan["steps"][:index])
                                                       if prior_step.get("kind") == "build"
                                                       and prior_step.get("graph_id") == prior_graph["graph_id"]
                                                       and prior_step.get("target_id") == output["producer_target"]), None)
                                if producer_index is not None:
                                    prior_row = session.get("graph_outputs", {}).get(output["output_id"])
                                    if not isinstance(prior_row, dict):
                                        raise RetentionError("typed stage has no prior graph output receipt")
                                    prior_output_rows.append(prior_row)
                        stage_record = session.get("typed_stage_step_identities", {}).get(str(index))
                        if not isinstance(stage_record, Mapping):
                            raise RetentionError("typed stage step identity is absent from the active session")
                        expected_stage_operation = stage_record.get("stage_operation_identity")
                        expected_stage_tree = stage_record.get("stage_tree_identity")
                        stage_mismatches = [
                            name for name, matched in (
                                ("action", command_receipt.get("action") == action),
                                ("command", command_receipt.get("command") is None),
                                ("environment", command_receipt.get("environment_sha256") is None),
                                ("launcher_path", command_receipt.get("sandbox_launcher_path") is None),
                                ("launcher_hash", command_receipt.get("sandbox_launcher_sha256") is None),
                                ("sandbox_profile", command_receipt.get("sandbox_profile_sha256") is None),
                                ("source", command_receipt.get("source_observation")
                                 == session.get("source_start_identity")),
                                ("tools", command_receipt.get("input_tool_identities")
                                 == expected_inputs["tools"]),
                                ("inputs", command_receipt.get("input_receipts")
                                 == expected_inputs["input_receipts"]),
                                ("graph_tools", command_receipt.get("graph_tool_identities")
                                 == expected_inputs["graph_tools"]),
                                ("runtime_inputs", command_receipt.get("runtime_input_identities")
                                 == expected_inputs["runtime_inputs"]),
                                ("result_inputs_after", result_receipt.get("input_observation_after")
                                 == expected_inputs),
                                ("graph_provenance", command_receipt.get("graph_provenance")
                                 == self.session._graph_provenance(step)),
                                ("graph_directory_before",
                                 command_receipt.get("graph_directory_identity_before")
                                 == expected_graph_directory),
                                ("graph_outputs_before",
                                 command_receipt.get("graph_output_identities_before") == prior_output_rows),
                                ("stage_tree_before",
                                 command_receipt.get("stage_tree_identity_before") == last_stage_tree_identity),
                                ("pe_stage_before",
                                 command_receipt.get("windows_pe_stage_identity_before")
                                 == windows_pe_stage_identity),
                                ("runtime_stage_before",
                                 command_receipt.get("native_runtime_stage_identity_before")
                                 == native_runtime_stage_identity),
                                ("result_graph_provenance",
                                 result_receipt.get("graph_provenance")
                                 == self.session._graph_provenance(step)),
                                ("result_graph_directory",
                                 result_receipt.get("graph_directory_identity")
                                 == expected_graph_directory),
                                ("result_graph_outputs",
                                 result_receipt.get("graph_output_identities")
                                 == session.get("graph_step_output_rows", {}).get(str(index), [])),
                                ("stage_operation",
                                 result_receipt.get("stage_operation_identity") == expected_stage_operation),
                                ("stage_tree", result_receipt.get("stage_tree_identity") == expected_stage_tree),
                            ) if not matched
                        ]
                        if stage_mismatches:
                            raise RetentionError(
                                "typed stage receipt differs from its fixed operation or provenance: "
                                + ",".join(stage_mismatches))
                    else:
                        expected_command, registered = self.session._command_for_step(step, session)
                        if (command_receipt.get("command") != expected_command
                                or command_receipt.get("source_observation") is None
                                or command_receipt.get("source_observation") != session.get("source_start_identity")
                                or command_receipt.get("input_tool_identities") != expected_inputs["tools"]
                                or command_receipt.get("input_receipts") != expected_inputs["input_receipts"]):
                            raise RetentionError("v3 command receipt differs from its admitted argv or tool inputs")
                        if step["kind"] == "test":
                            if (registered is None
                                    or command_receipt.get("registered_executable_identity") != registered):
                                raise RetentionError("v3 registered test identity differs from its pre-execution binding")
                        elif command_receipt.get("registered_executable_identity") is not None:
                            raise RetentionError("v3 non-test step carries an unexpected test executable")
                        expected_write_root = None
                        expected_temporary_directory = None
                        if step["kind"] == "install":
                            stage_relative = _session_relative_path(self.session_plan["stage_root"], "stage_root")
                            expected_write_root = Path(self.session_plan["build_workspace"])
                            install_stage = _session_safe_path(
                                Path(self.session_plan["build_workspace"]), stage_relative,
                                must_exist=True, directory=True)
                            expected_temporary_directory = install_stage / ".buildlease-install-tmp"
                        expected_environment = (self.session._environment_for_step(step, session)
                                                if dual_graph else self.session_plan["environment"])
                        expected_sandboxed, expected_effective, expected_launcher = _sandbox_session_command(
                            expected_command, Path(self.session_plan["build_workspace"]),
                            expected_environment,
                            write_root=expected_write_root,
                            temporary_directory=expected_temporary_directory)
                        expected_launcher_identity = _session_hash_executable(
                            Path(expected_launcher), self.session_plan["tools"]["sandbox"]["sha256"])
                        if (command_receipt.get("sandbox_launcher_path") != expected_launcher
                                or command_receipt.get("sandbox_launcher_sha256")
                                    != expected_launcher_identity["sha256"]
                                or command_receipt.get("environment_sha256")
                                    != hashlib.sha256(_canonical_json(expected_effective)).hexdigest()
                                or command_receipt.get("sandbox_profile_sha256")
                                    != hashlib.sha256(expected_sandboxed[2].encode("utf-8")).hexdigest()):
                            raise RetentionError("v3 command launcher, environment, or profile differs from its contract")
                        if (dual_graph and step["kind"] == "test"
                                and step.get("test_class") == "PRODUCTION_PATH"):
                            expected_loader = self.session._native_loader_provenance(
                                session, expected_effective["DYLD_LIBRARY_PATH"])
                            if (command_receipt.get("native_loader_provenance") != expected_loader
                                    or result_receipt.get("native_loader_provenance") != expected_loader):
                                raise RetentionError(
                                    "native test loader receipt differs from its staged runtime and graph identities")
                        elif (command_receipt.get("native_loader_provenance") is not None
                              or result_receipt.get("native_loader_provenance") is not None):
                            raise RetentionError("non-native-test step carries an unexpected SDL2 loader receipt")
                        if dual_graph:
                            graph = _session_dual_graph_by_id(self.session_plan, str(step["graph_id"]))
                            expected_graph_directory = session.get("graph_directory_identities", {}).get(
                                graph["graph_id"])
                            expected_prior_outputs = []
                            for prior_graph in self.session_plan["build_graphs"]:
                                for output in prior_graph["expected_outputs"]:
                                    producer_index = next((prior_index for prior_index, prior_step in
                                                           enumerate(self.session_plan["steps"][:index])
                                                           if prior_step.get("kind") == "build"
                                                           and prior_step.get("graph_id") == prior_graph["graph_id"]
                                                           and prior_step.get("target_id") == output["producer_target"]), None)
                                    if producer_index is not None:
                                        expected_prior_outputs.append(
                                            session["graph_outputs"][output["output_id"]])
                            expected_post_outputs = session.get("graph_step_output_rows", {}).get(str(index), [])
                            expected_stage_tree = session.get("stage_step_tree_identities", {}).get(str(index))
                            if (command_receipt.get("graph_tool_identities") != expected_inputs["graph_tools"]
                                    or command_receipt.get("runtime_input_identities")
                                        != expected_inputs["runtime_inputs"]
                                    or command_receipt.get("graph_provenance")
                                        != self.session._graph_provenance(step)
                                    or command_receipt.get("graph_directory_identity_before")
                                        != (None if step["kind"] == "configure" else expected_graph_directory)
                                    or command_receipt.get("graph_output_identities_before")
                                        != expected_prior_outputs
                                    or command_receipt.get("stage_tree_identity_before")
                                        != last_stage_tree_identity
                                    or command_receipt.get("windows_pe_stage_identity_before")
                                        != windows_pe_stage_identity
                                    or command_receipt.get("native_runtime_stage_identity_before")
                                        != native_runtime_stage_identity
                                    or result_receipt.get("graph_provenance")
                                        != self.session._graph_provenance(step)
                                    or result_receipt.get("graph_directory_identity") != expected_graph_directory
                                    or result_receipt.get("graph_output_identities") != expected_post_outputs
                                    or result_receipt.get("stage_tree_identity") != expected_stage_tree
                                    or result_receipt.get("graph_tool_identities") != expected_inputs["graph_tools"]
                                    or result_receipt.get("runtime_input_identities") != expected_inputs["runtime_inputs"]):
                                raise RetentionError("dual-graph command/result receipts omit graph or output provenance")
                            if result_receipt.get("input_observation_after") != expected_inputs:
                                raise RetentionError("dual-graph result input identity after operation is not exact")
                        sandbox_profile_hashes.add(command_receipt["sandbox_profile_sha256"])
                        sandbox_launcher_hashes.add(command_receipt["sandbox_launcher_sha256"])
                        last_sandbox_profile_hash = command_receipt["sandbox_profile_sha256"]
                if step["kind"] == "test":
                    if self.session is None:
                        raise RetentionError("v3 test executable verifier is unavailable")
                    self.session._validate_test_executable(step, session, after_execution=True)
                log_path_text = result_receipt.get("log_path")
                if log_path_text is not None:
                    log_path = _session_safe_path(self.path, Path(log_path_text), must_exist=True, directory=False)
                    if _hash_path(log_path) != result_receipt.get("log_sha256"):
                        raise RetentionError("v3 session step log changed after its success receipt")
                    if log_path != session_dir / "steps" / f"{step_name}.log":
                        raise RetentionError("v3 step log is outside its exact receipt location")
                elif step["kind"] != "source_verification":
                    if not (dual_graph and step["kind"] in
                            {"stage_windows_product", "stage_native_runtime"}):
                        raise RetentionError("v3 command result is missing its bounded step log")
                    if result_receipt.get("log_path") is not None or result_receipt.get("log_sha256") is not None:
                        raise RetentionError("typed stage action may not claim a subprocess log")
                if dual_graph and step["kind"] == "test":
                    if command_receipt.get("action") is not None:
                        raise RetentionError("native production test must be a direct executable invocation")
                if dual_graph and step["kind"] != "source_verification":
                    last_stage_tree_identity = result_receipt.get("stage_tree_identity")
                    if step["kind"] == "stage_windows_product":
                        windows_pe_stage_identity = result_receipt.get("stage_operation_identity")
                    elif step["kind"] == "stage_native_runtime":
                        native_runtime_stage_identity = result_receipt.get("stage_operation_identity")
                receipt_hashes.append(_hash_path(result_path))
            if terminal.get("ordered_result_receipt_sha256") != receipt_hashes:
                raise RetentionError("v3 terminal receipt does not bind the exact ordered step receipts")
            if (not sandbox_profile_hashes or len(sandbox_launcher_hashes) != 1
                    or terminal.get("sandbox_profile_sha256") != last_sandbox_profile_hash
                    or terminal.get("sandbox_launcher_sha256") not in sandbox_launcher_hashes
                    or terminal.get("sandbox_launcher_path") != self.session_plan["tools"]["sandbox"]["path"]):
                raise RetentionError("v3 terminal sandbox evidence is not consistent across commands")
            root_fd = _open_dir(self.path)
            try:
                root_info = os.fstat(root_fd)
                root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
                marker = _read_marker(root_fd)
                observed_root_identity = {"device": root_info.st_dev, "inode": root_info.st_ino,
                                          "filesystem_id": root_fsid}
                if (marker.get("build_id") != self.build_id or marker.get("state") != "ACTIVE"
                        or (root_info.st_dev, root_info.st_ino, root_fsid) != (
                            lease.get("build_device"), lease.get("build_inode"),
                            lease.get("build_filesystem_id"))
                        or plan_receipt.get("build_root_identity") != observed_root_identity
                        or terminal.get("build_root_identity") != observed_root_identity
                        or session.get("build_root_identity") != observed_root_identity):
                    raise RetentionError("v3 session build root or active marker changed")
            finally:
                os.close(root_fd)
            workspace_mount = session.get("workspace_mount_identity")
            if not isinstance(workspace_mount, dict) or workspace_mount.get("attached") is not True:
                raise RetentionError("v3 original writable workspace mount identity is absent")
            if (plan_receipt.get("workspace_mount_identity") != workspace_mount
                    or terminal.get("workspace_mount_identity") != workspace_mount):
                raise RetentionError("v3 terminal does not bind the original writable workspace identity")
            detached_workspace = session.get("detached_workspace_identity")
            readonly_mount = session.get("readonly_mount_identity")
            if (not isinstance(detached_workspace, dict) or detached_workspace.get("attached") is not False
                    or not isinstance(session.get("detached_image_identity"), dict)
                    or detached_workspace.get("detached_image_identity") != session.get("detached_image_identity")
                    or terminal.get("detached_workspace_identity") != detached_workspace
                    or terminal.get("detached_image_identity") != session.get("detached_image_identity")
                    or not isinstance(readonly_mount, dict) or readonly_mount.get("attached") is not True
                    or readonly_mount.get("readonly") is not True
                    or terminal.get("readonly_mount_identity") != readonly_mount):
                raise RetentionError("v3 terminal does not bind normal detach and exact read-only reattachment")
            _verify_session_workspace_readonly(self.path, readonly_mount)
            final_inputs = self._validate_session_inputs()
            if terminal.get("final_source_verification") != final_inputs:
                raise RetentionError("source changed after the v3 session verification step")
            stage_relative = _session_relative_path(self.session_plan["stage_root"], "stage_root")
            output_relative = _session_relative_path(self.session_plan["output_root"],
                                                      "output_root", allow_dot=True)
            workspace = Path(self.session_plan["build_workspace"])
            stage = _session_safe_path(workspace, stage_relative, must_exist=True, directory=True)
            output = _session_safe_path(workspace, output_relative, must_exist=True, directory=True)
            stage_fd = _open_dir(stage)
            try:
                stage_identity = _fs_identity(stage_fd)
            finally:
                os.close(stage_fd)
            if (terminal.get("stage_root_identity") != stage_identity
                    or terminal.get("readonly_stage_root_identity") != stage_identity
                    or session.get("readonly_stage_root_identity") != stage_identity):
                raise RetentionError("v3 session stage root identity changed")
            output_fd = _open_dir(output)
            try:
                output_identity = _fs_identity(output_fd)
            finally:
                os.close(output_fd)
            if (terminal.get("output_root_identity") != output_identity
                    or terminal.get("readonly_output_root_identity") != output_identity
                    or session.get("readonly_output_root_identity") != output_identity):
                raise RetentionError("v3 session output root identity changed")
            output_tree_hash, output_file_count = _session_tree_digest(output, exclude_controller_state=True)
            if (terminal.get("output_tree_sha256") != output_tree_hash
                    or terminal.get("output_file_count") != output_file_count):
                raise RetentionError("v3 session output tree changed after finalization")
            if (terminal.get("peak_allocated_bytes") != session.get("peak_allocated_bytes")
                    or terminal.get("peak_allocated_bytes", self.expected_bytes + 1) > self.expected_bytes):
                raise RetentionError("v3 session cumulative peak does not fit its signed reservation")
            launcher_path = Path(terminal.get("sandbox_launcher_path", ""))
            launcher = _session_hash_executable(
                launcher_path, self.session_plan["tools"]["sandbox"]["sha256"])
            profile_sha = terminal.get("sandbox_profile_sha256")
            if not isinstance(profile_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", profile_sha):
                raise RetentionError("v3 session sandbox profile receipt is invalid")
        except (OSError, RetentionError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise RetentionError(f"v3 session success evidence is missing or invalid: {error}") from error
        return {**terminal, "sandbox_profile_sha256": profile_sha,
                "sandbox_launcher": launcher, "ordered_result_receipt_sha256": receipt_hashes}

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
            "storage_watch_events": [dict(event) for event in self.storage_watch_events],
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
        lease = _load_lease(self.build_id)
        session_record = lease.get("session_v3")
        if isinstance(session_record, dict):
            if (session_record.get("output_capture_uncertain") is True
                    or session_record.get("process_containment_uncertain") is True):
                raise RetentionError("cannot capture or retire a session with uncertain process or output containment")
            if session_record.get("state") != "FAILED":
                raise RetentionError("v3 session failure must be durably terminalized before BuildLease cleanup")
            if str(session_record.get("acceptance_state", "")).startswith("REJECTED_"):
                self._stop_heartbeat()
                session_record["state"] = "FAILED"
                session_record["failed_at_unix"] = time.time()
                session_record["failure_reason"] = error
                session_record["failure_reason_sha256"] = error_sha256
                session_record["acceptance_state"] = "REJECTED_NO_OUTPUT_PROMOTION"
                lease.update({"session_v3": session_record, "state": "FAILED",
                              "failed_command": command, "return_code": return_code,
                              "failed_at_unix": session_record["failed_at_unix"],
                              "workspace_detach_uncertain": True})
                _store_lease(lease)
                root_fd = _open_dir(self.path)
                try:
                    marker = _read_marker(root_fd)
                    marker.update(lease)
                    marker["state"] = "FAILED"
                    _write_marker(root_fd, marker)
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
                _discard_unaccepted_host_v3_completion(lease)
                _record_unaccepted_session_failure(lease, error or "host BuildLease v3 session rejected")
                self._release_lock()
                return {"status": "FAILED_UNACCEPTED_BLOCKING", "build_id": self.build_id,
                        "failure_evidence": "NOT_CAPTURED_FROM_WRITABLE_IMAGE"}
        _require_no_live_process(self.path)
        self._stop_heartbeat()
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

        session_prefix = (f"{BUILD_SESSION_WORKSPACE_NAME}/"
                          if isinstance(session_record, dict) else "")
        for relative in ("meson-logs/meson-log.txt", "meson-private/cmd_line.txt",
                         "meson-info/intro-buildoptions.json", "meson-info/intro-compilers.json",
                         "meson-info/intro-buildsystem_files.json", "build.ninja"):
            leased_relative = session_prefix + relative
            row = capture(leased_relative, evidence / "configuration" / relative, required=False)
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
            leased_relative = session_prefix + relative
            row = capture(leased_relative, evidence / "generated" / Path(relative), required=False)
            if row is None:
                generated_diagnostics.append({"relative_path": relative, "status": "MISSING"})
                continue
            copied.append(row)
            generated_diagnostics.append({"relative_path": relative, "status": "CAPTURED",
                                          "sha256": row["sha256"], "size_bytes": row["size_bytes"],
                                          "retained_path": row["retained_path"]})
        command_record = {"build_id": self.build_id, "command": command, "return_code": return_code,
                          "error": error, "failed_at_unix": time.time(), "configuration_and_logs": copied,
                          "generated_diagnostics": generated_diagnostics,
                          "storage_watch_events": [dict(event) for event in self.storage_watch_events]}
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
        if isinstance(session_record, dict):
            workspace_mount = session_record.get("workspace_mount_identity")
            try:
                detached_mount = _detach_session_workspace(self.path, workspace_mount)
            except BaseException as error:
                lease["workspace_detach_uncertain"] = True
                lease["workspace_detach_failure"] = repr(error)[:MAX_FAILURE_ERROR_BYTES]
                _store_lease(lease)
                raise RetentionError(
                    f"failed BuildLease workspace could not be detached; lease remains blocking: {error}") from error
            session_record["workspace_mount_identity"] = detached_mount
            session_record["workspace_detached_at_unix"] = detached_mount.get("detached_at_unix")
            lease["session_v3"] = session_record
            _store_lease(lease)
            root_fd = _open_dir(self.path)
            try:
                marker = _read_marker(root_fd)
                marker.update(lease)
                _write_marker(root_fd, marker)
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        receipt = _retire(lease, reason="failed-build-evidence-captured")
        self._release_lock()
        return receipt

    def _wait_for_build_process(self, process: subprocess.Popen[Any], *,
                                poll_interval_seconds: float = 1.0,
                                session: "BuildLeaseSession | None" = None,
                                session_output_error: threading.Event | None = None,
                                session_output_overflow: threading.Event | None = None) -> int:
        """Wait for a build while enforcing the live emergency free-space stop."""
        from storage_policy import FREE_HARD_STOP, FREE_WARNING, disk_free_bytes

        if poll_interval_seconds <= 0:
            raise ValueError("build storage monitor poll interval must be positive")
        started = time.monotonic()
        while True:
            if session is not None and type(process).__module__ == "subprocess":
                try:
                    observed_exit = os.waitid(
                        os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except (AttributeError, OSError, ChildProcessError) as error:
                    raise RetentionError(
                        f"cannot hold BuildLease session leader unreaped for descendant containment: {error}") from error
                if observed_exit is None or observed_exit.si_pid == 0:
                    return_code = None
                elif observed_exit.si_code == getattr(os, "CLD_EXITED", 1):
                    return_code = observed_exit.si_status
                else:
                    return_code = -observed_exit.si_status
            else:
                try:
                    return_code = process.wait(timeout=poll_interval_seconds)
                except subprocess.TimeoutExpired:
                    return_code = None
            if session_output_error is not None and session_output_error.is_set():
                raise RetentionError("session command output could not be durably captured")
            if session_output_overflow is not None and session_output_overflow.is_set():
                raise RetentionError("session command exceeded its strict aggregate log-size limit")
            free = disk_free_bytes(REPO)
            if session is not None:
                session._check_limits("during-step")
            event = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "free_bytes": free,
                "sampled_at_unix": time.time(),
            }
            if free < FREE_HARD_STOP:
                completed = return_code is not None
                event["action"] = (
                    "HARD_STOP_AFTER_PROCESS_COMPLETION" if completed else "HARD_STOP_TERMINATE"
                )
                if session is None:
                    self.storage_watch_events.append(event)
                else:
                    self._append_session_storage_event(event)
                raise RetentionError(
                    f"free space crossed the 25 GiB emergency hard stop during the build: {free}"
                )
            if free < FREE_WARNING and not self._free_warning_emitted:
                event["action"] = "WARNING_NO_NEW_EXPERIMENTS"
                self._free_warning_emitted = True
                if session is None:
                    self.storage_watch_events.append(event)
                else:
                    self._append_session_storage_event(event)
            if return_code is not None:
                event["action"] = "PROCESS_COMPLETED"
                if session is None:
                    self.storage_watch_events.append(event)
                else:
                    self._append_session_storage_event(event)
                return return_code

    def _append_session_storage_event(self, event: Mapping[str, Any]) -> None:
        """Keep v3 telemetry bounded while preserving the legacy v1 event stream."""
        if len(self.storage_watch_events) >= BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS:
            raise RetentionError("session storage telemetry exceeded its fixed event limit")
        self.storage_watch_events.append(dict(event))

    def run(self, command: Iterable[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
            preserve_debug: bool = False) -> dict[str, Any]:
        if self.session_plan is not None:
            raise RetentionError("a session-bound BuildLease must use start_session() and cannot use v1 run()")
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
                rc = self._wait_for_build_process(process)
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


class BuildLeaseSession:
    """A finite v3 command plan held by one ACTIVE BuildLease until finalize()."""

    def __init__(self, lease: BuildLease, *, _construction_token: object | None = None):
        if (_construction_token is not lease._session_construction_token
                or not lease._session_creation_lock.locked() or lease.session is not None):
            raise RetentionError("BuildLeaseSession can only be created once by its owning BuildLease")
        if (not lease.started or lease.reused_manifest is not None or lease.lock_fds is None
                or lease.session_plan is None):
            raise RetentionError("BuildLeaseSession requires its active reserved BuildLease and live serialization lock")
        try:
            for descriptor in lease.lock_fds:
                os.fstat(descriptor)
            reserved = _load_lease(lease.build_id)
        except (OSError, RetentionError) as error:
            raise RetentionError("BuildLeaseSession cannot verify its live reservation lock") from error
        reserved_session = reserved.get("session_v3")
        if (reserved.get("state") != "ACTIVE" or reserved.get("path") != str(lease.path)
                or reserved.get("session_started") is not False
                or not isinstance(reserved_session, dict) or reserved_session.get("state") != "RESERVED"
                or reserved_session.get("plan_sha256") != lease.session_plan_sha256):
            raise RetentionError("BuildLeaseSession is not the RESERVED session for its live BuildLease")
        lease._assert_session_plan_binding()
        self.lease = lease
        self.plan = lease.session_plan
        if self.plan is None:
            raise RetentionError("BuildLeaseSession requires a signed v3 session plan")
        self.plan_sha256 = lease.session_plan_sha256
        self.root = lease.path
        self.workspace = Path(self.plan["build_workspace"])
        self.session_dir = self.root / BUILD_SESSION_DIRECTORY
        self._active_process: subprocess.Popen[Any] | None = None
        self._operation_lock = threading.Lock()

    def _with_operation(self, name: str, operation: Callable[[], Any]) -> Any:
        if not self._operation_lock.acquire(blocking=False):
            raise RetentionError(f"v3 session {name} overlaps another active session operation")
        try:
            return operation()
        finally:
            self._operation_lock.release()

    def _store(self, lease_record: Mapping[str, Any]) -> None:
        with self.lease._lease_state_lock:
            selected = dict(lease_record)
            session_record = selected.get("session_v3")
            if isinstance(session_record, dict):
                session_record = dict(session_record)
                session_record["storage_watch_events"] = [
                    dict(event) for event in self.lease.storage_watch_events]
                selected["session_v3"] = session_record
            _store_lease(selected)
            root_fd = _open_dir(self.root)
            try:
                marker = _read_marker(root_fd)
                if marker.get("build_id") != self.lease.build_id or marker.get("state") != "ACTIVE":
                    raise RetentionError("session lease marker is no longer ACTIVE")
                marker.update(selected)
                _write_marker(root_fd, marker)
                os.fsync(root_fd)
            finally:
                os.close(root_fd)

    def _check_controller_evidence_budget(self) -> tuple[int, int]:
        """Bound evidence outside the image, especially logs that commands cannot write."""
        total_bytes = 0
        log_bytes = 0
        file_count = 0
        for directory, dirnames, filenames in os.walk(self.session_dir, followlinks=False):
            base = Path(directory)
            for name in list(dirnames):
                path = base / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise RetentionError("controller evidence contains a linked or special directory")
            for name in filenames:
                path = base / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RetentionError("controller evidence contains a non-regular or hardlinked file")
                file_count += 1
                total_bytes += info.st_size
                if path.suffix == ".log":
                    log_bytes += info.st_size
                elif info.st_size > BUILD_SESSION_PLAN_LIMIT_BYTES:
                    raise RetentionError("controller receipt exceeds its individual size cap")
        if file_count > BUILD_SESSION_MAX_STEPS * 3 + 16:
            raise RetentionError("controller evidence contains more files than the finite session plan allows")
        if total_bytes > BUILD_SESSION_CONTROLLER_EVIDENCE_LIMIT_BYTES:
            raise RetentionError("controller evidence exceeds its strict aggregate size cap")
        if log_bytes > BUILD_SESSION_TOTAL_LOG_LIMIT_BYTES:
            raise RetentionError("controller logs exceed their strict aggregate size cap")
        return total_bytes, log_bytes

    def _load(self, *, allow_running: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        _require_buildlease_vm_runtime(self.plan)
        lease_record = _load_lease(self.lease.build_id)
        session = lease_record.get("session_v3")
        if (lease_record.get("state") == "ACTIVE" and isinstance(session, dict)
                and (session.get("output_capture_uncertain") is True
                     or session.get("process_containment_uncertain") is True)):
            raise RetentionError("v3 session process or output containment is uncertain and remains durably RUNNING")
        try:
            self.lease._assert_session_plan_binding()
        except RetentionError as error:
            if (lease_record.get("state") == "ACTIVE" and isinstance(session, dict)
                    and session.get("state") != "FAILED"):
                try:
                    self._fail(str(error), -1, command=["session-plan-integrity"])
                except BaseException:
                    pass
            raise
        if (lease_record.get("state") != "ACTIVE" or lease_record.get("path") != str(self.root)
                or not isinstance(session, dict) or session.get("plan_sha256") != self.plan_sha256
                or session.get("schema_version") != BUILD_SESSION_SCHEMA_VERSION):
            raise RetentionError("v3 BuildLease session is no longer the same ACTIVE reservation")
        if session.get("state") != "ACTIVE" and not allow_running:
            raise RetentionError(f"v3 BuildLease session cannot continue from {session.get('state')}")
        return lease_record, session

    def _check_root_bindings(self, lease_record: Mapping[str, Any], session: Mapping[str, Any]) -> None:
        root_fd = _open_dir(self.root)
        try:
            root_info = os.fstat(root_fd)
            fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
            marker = _read_marker(root_fd)
            observed_root_identity = {"device": root_info.st_dev, "inode": root_info.st_ino,
                                     "filesystem_id": fsid}
            if (marker.get("build_id") != self.lease.build_id or marker.get("state") != "ACTIVE"
                    or marker.get("session_v3", {}).get("plan_sha256") != self.plan_sha256
                    or (root_info.st_dev, root_info.st_ino, fsid) != (
                        lease_record.get("build_device"), lease_record.get("build_inode"),
                        lease_record.get("build_filesystem_id"))
                    or session.get("build_root_identity") != observed_root_identity):
                raise RetentionError("session root or marker identity changed")
        finally:
            os.close(root_fd)
        stage_relative = _session_relative_path(self.plan["stage_root"], "stage_root")
        output_relative = _session_relative_path(self.plan["output_root"], "output_root", allow_dot=True)
        workspace_mount = session.get("workspace_mount_identity")
        if not isinstance(workspace_mount, dict) or workspace_mount.get("attached") is not True:
            raise RetentionError("session workspace mount is not in its exact attached state")
        quiescence = session.get("write_quiescence_state")
        readonly_mount = session.get("readonly_mount_identity")
        if quiescence == "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED":
            if (not isinstance(readonly_mount, dict) or readonly_mount.get("readonly") is not True
                    or readonly_mount.get("attached") is not True):
                raise RetentionError("session lost its verified read-only evidence attachment")
            _verify_session_workspace_readonly(self.root, readonly_mount)
            workspace_mount = readonly_mount
        else:
            _verify_session_workspace_mount(self.root, workspace_mount)
        stage = _session_safe_path(self.workspace, stage_relative, must_exist=True, directory=True)
        output = _session_safe_path(self.workspace, output_relative, must_exist=True, directory=True)
        stage_fd = _open_dir(stage)
        output_fd = _open_dir(output)
        try:
            if (_fs_identity(stage_fd) != session.get("stage_root_identity")
                    or _fs_identity(output_fd) != session.get("output_root_identity")):
                raise RetentionError("session stage/output root identity changed")
        finally:
            os.close(stage_fd)
            os.close(output_fd)

    def _check_limits(self, phase: str, *, extra_bytes: int = 0) -> int:
        from storage_policy import (conservative_project_usage_bytes, disk_free_bytes,
                                    measure_project_usage, projected_storage_admission)
        lease_record, session = self._load(allow_running=True)
        self._check_root_bindings(lease_record, session)
        workspace_mount = session.get("readonly_mount_identity")
        if (session.get("write_quiescence_state") != "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED"
                or not isinstance(workspace_mount, dict)
                or workspace_mount.get("readonly") is not True):
            workspace_mount = session.get("workspace_mount_identity")
        measured_row = _session_measure_tree(
            self.root, workspace_mount_identity=workspace_mount)
        controller_evidence_bytes, controller_log_bytes = self._check_controller_evidence_budget()
        measured = measured_row["allocated_bytes"]
        peak = max(int(session.get("peak_allocated_bytes", 0)), measured + extra_bytes)
        needed = peak
        if needed > self.lease.expected_bytes:
            raise RetentionError(
                f"session cumulative allocation exceeds its signed reservation during {phase}: "
                f"{needed} > {self.lease.expected_bytes}")
        admitted_peak = lease_record.get("storage_admission_peak_bytes")
        if (not isinstance(admitted_peak, int) or isinstance(admitted_peak, bool)
                or admitted_peak < self.lease.expected_bytes):
            raise RetentionError("session lease lacks its signed projected-admission peak")
        projected_additional = max(0, admitted_peak - measured) + extra_bytes
        current_free = disk_free_bytes(REPO)
        current_scoped = conservative_project_usage_bytes(measure_project_usage())
        admission = projected_storage_admission(
            current_free_bytes=current_free, current_scoped_bytes=current_scoped,
            peak_additional_bytes=projected_additional)
        event = {"phase": phase, "allocated_bytes": measured, "peak_allocated_bytes": peak,
                 "controller_evidence_bytes": controller_evidence_bytes,
                 "controller_log_bytes": controller_log_bytes,
                 "reservation_bytes": self.lease.expected_bytes,
                 "admitted_projected_peak_bytes": admitted_peak,
                 "projected_additional_bytes": projected_additional, "free_bytes": current_free,
                 "project_scoped_bytes": current_scoped, "sampled_at_unix": time.time(),
                 "warnings": list(admission["warnings"])}
        if phase not in {"during-step", "before-terminal-receipt"} or not admission["admitted"]:
            self.lease._append_session_storage_event(event)
        if not admission["admitted"]:
            raise RetentionError("session runtime storage limits block continuation: "
                                 + "; ".join(admission["blockers"]))
        if peak != session.get("peak_allocated_bytes"):
            session["peak_allocated_bytes"] = peak
            session["last_storage_check"] = event
            lease_record["session_v3"] = session
            self._store(lease_record)
        return peak

    def _create_install_temporary(self) -> tuple[Path, Path, dict[str, Any]]:
        stage_relative = _session_relative_path(self.plan["stage_root"], "stage_root")
        stage = _session_safe_path(self.workspace, stage_relative, must_exist=True, directory=True)
        temporary = stage / ".buildlease-install-tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RetentionError("install temporary path already exists beneath the declared stage root")
        temporary.mkdir(mode=0o700)
        for relative in (Path(".cache"), Path(".config"), Path(".local/share")):
            (temporary / relative).mkdir(mode=0o700, parents=True)
        temporary_fd = _open_dir(temporary)
        try:
            identity = _fs_identity(temporary_fd)
        finally:
            os.close(temporary_fd)
        return stage, temporary, identity

    def _remove_install_temporary(self, stage: Path, temporary: Path,
                                  identity: Mapping[str, Any]) -> None:
        stage_fd = _open_dir(stage)
        try:
            _remove_verified_tree_at(
                stage_fd, temporary.name, expected_device=identity["device"],
                expected_inode=identity["inode"], logical_path=temporary,
                expected_filesystem_id=identity["filesystem_id"])
        finally:
            os.close(stage_fd)

    def _validate_source(self) -> dict[str, Any]:
        identity = self.lease._validate_session_inputs()
        reserved_source = self.plan["source_identity"]
        if (identity.get("source_revision") != reserved_source["revision"]
                or identity.get("source_tree_sha256") != reserved_source["sha256"]):
            raise RetentionError("session source revision or digest changed")
        lease_record, session = self._load(allow_running=True)
        pinned = session.get("source_root_identity")
        if pinned != {"device": identity["source_device"], "inode": identity["source_inode"]}:
            raise RetentionError("session source root identity changed")
        if identity != session.get("source_start_identity"):
            raise RetentionError("session source, tool, input, or PATH identity changed after session creation")
        return identity

    def _validate_test_executable(self, step: Mapping[str, Any], session: dict[str, Any],
                                  *, after_execution: bool = False) -> dict[str, Any]:
        if _session_dual_graph_enabled(self.plan):
            graph = _session_dual_graph_by_id(self.plan, str(step.get("graph_id", "")))
            if (step.get("graph_id") != "native-verification-r6a"
                    or graph.get("graph_role") != "NATIVE_VERIFICATION"
                    or graph.get("executable_test_eligibility") is not True):
                raise RetentionError("only a native verification graph output is executable")
            output_rows = [row for row in graph["expected_outputs"]
                           if row["output_id"] == step.get("output_id")]
            if len(output_rows) != 1:
                raise RetentionError("test step references an undeclared native graph output")
            output = output_rows[0]
            test_binding = BUILD_SESSION_DUAL_GRAPH_NATIVE_TEST_BINDINGS.get(output["output_id"])
            if (not isinstance(test_binding, Mapping)
                    or step.get("test_class") != test_binding["test_class"]
                    or output.get("producer_target") != test_binding["producer_target"]):
                raise RetentionError("native test label and output target are not a fixed pair")
            if output["format"] != "MACHO_NATIVE_ARCH":
                raise RetentionError("Windows PE outputs can never be executed by a session test step")
            observed = _session_graph_output_identity(self.workspace, graph, output)
            registered = session.setdefault("graph_outputs", {}).get(output["output_id"])
            key = f"{graph['graph_id']}:{output['output_id']}"
            previous_test = session.setdefault("registered_tests", {}).get(key)
            if registered != observed:
                raise RetentionError("native test output differs from its declared graph producer identity")
            if after_execution:
                if previous_test != observed:
                    raise RetentionError("native test output changed while it ran")
            elif previous_test is None:
                session["registered_tests"][key] = observed
            elif previous_test != observed:
                raise RetentionError("native test output identity changed before launch")
            return {**observed, "executable_path": str(
                self.workspace / graph["build_directory"] / output["path"])}
        relative = _session_relative_path(step["executable"], "registered test executable")
        output_rel = _session_relative_path(self.plan["output_root"], "output_root", allow_dot=True)
        output = _session_safe_path(self.workspace, output_rel, must_exist=True, directory=True)
        executable = _session_safe_path(output, relative, must_exist=True, directory=False)
        info = executable.stat(follow_symlinks=False)
        if not info.st_mode & 0o111:
            raise RetentionError("registered test output is not executable")
        with executable.open("rb") as stream:
            if stream.read(2) == b"#!":
                raise RetentionError("interpreter-based test wrappers are not permitted")
        digest = _hash_path(executable)
        registered = session.setdefault("registered_tests", {}).get(relative.as_posix())
        observed = {"path": str(executable), "sha256": digest, "device": info.st_dev,
                    "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}
        if after_execution:
            if registered != observed:
                raise RetentionError("registered test executable changed while it ran")
        elif registered is None:
            session["registered_tests"][relative.as_posix()] = observed
        elif registered != observed:
            raise RetentionError("registered test executable identity changed before launch")
        return observed

    def _graph_provenance(self, step: Mapping[str, Any]) -> dict[str, Any] | None:
        if not _session_dual_graph_enabled(self.plan) or step.get("kind") == "source_verification":
            return None
        graph = _session_dual_graph_by_id(self.plan, str(step.get("graph_id", "")))
        target_id = step.get("target_id")
        output_id = step.get("output_id")
        runtime_ids = step.get("runtime_ids", [])
        if step.get("kind") == "test":
            output = next((row for row in graph["expected_outputs"]
                           if row["output_id"] == step.get("output_id")), None)
            target_id = output["producer_target"] if output is not None else None
        elif step.get("kind") == "stage_windows_product":
            output = next((row for row in graph["expected_outputs"]
                           if row["output_id"] == output_id), None)
            target_id = output["producer_target"] if output is not None else None
        return {"graph_id": graph["graph_id"], "graph_role": graph["graph_role"],
                "source_identity": graph["source_identity"],
                "build_directory": graph["build_directory"],
                "target_id": target_id,
                "output_id": output_id,
                "test_class": step.get("test_class"),
                "runtime_ids": runtime_ids,
                "stage_role": graph["stage_role"],
                "executable_test_eligibility": graph["executable_test_eligibility"]}

    def _graph_directory_identity(self, graph: Mapping[str, Any], *, must_exist: bool) -> dict[str, int] | None:
        identity = _session_graph_directory_identity(self.workspace, graph, must_exist=must_exist)
        expected = self._load(allow_running=True)[1].get("graph_directory_identities", {}).get(graph["graph_id"])
        if expected is not None and identity != expected:
            raise RetentionError("graph build directory identity changed between steps")
        return identity

    def _capture_graph_outputs(self, step: Mapping[str, Any], session: dict[str, Any]) -> list[dict[str, Any]]:
        if not _session_dual_graph_enabled(self.plan) or step.get("kind") != "build":
            return []
        graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
        target_id = step["target_id"]
        graph_identity = self._graph_directory_identity(graph, must_exist=True)
        identities: list[dict[str, Any]] = []
        for output in graph["expected_outputs"]:
            if output["producer_target"] != target_id:
                continue
            identity = _session_graph_output_identity(self.workspace, graph, output)
            if identity["build_directory_identity"] != graph_identity:
                raise RetentionError("graph output was produced under a changed build directory")
            prior = session.setdefault("graph_outputs", {}).get(output["output_id"])
            if prior is not None:
                raise RetentionError("graph output identity was already recorded by another producer step")
            session["graph_outputs"][output["output_id"]] = identity
            identities.append(identity)
        if target_id in {"R6A_PRODUCTION_PATH_PE", "R6A_PRODUCTION_PATH_MACHO"} and not identities:
            raise RetentionError("declared fixture target completed without its exact output identity")
        return identities

    def _verify_graph_outputs(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not _session_dual_graph_enabled(self.plan):
            return []
        expected_rows = self._verify_recorded_graph_outputs(session)
        all_output_ids = {row["output_id"] for graph in self.plan["build_graphs"]
                          for row in graph["expected_outputs"]}
        if set(session.get("graph_outputs", {})) != all_output_ids:
            raise RetentionError("session graph-output receipt has missing or unexpected identities")
        return expected_rows

    def _verify_recorded_graph_outputs(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Revalidate every output already produced, without requiring future graph outputs."""
        if not _session_dual_graph_enabled(self.plan):
            return []
        expected_rows: list[dict[str, Any]] = []
        recorded = session.get("graph_outputs", {})
        if not isinstance(recorded, Mapping):
            raise RetentionError("session graph-output receipt is malformed")
        for graph in self.plan["build_graphs"]:
            recorded_graph_identity = session.get("graph_directory_identities", {}).get(graph["graph_id"])
            if recorded_graph_identity is not None:
                identity = self._graph_directory_identity(graph, must_exist=True)
                if recorded_graph_identity != identity:
                    raise RetentionError("graph build directory identity changed after configure")
            for output in graph["expected_outputs"]:
                saved = recorded.get(output["output_id"])
                if saved is None:
                    continue
                if not isinstance(saved, dict) or recorded_graph_identity is None:
                    raise RetentionError("declared graph output is missing its configured graph identity")
                current = _session_graph_output_identity(self.workspace, graph, output)
                if saved != current:
                    raise RetentionError("graph output changed after its producer step")
                expected_rows.append(current)
        allowed_ids = {row["output_id"] for graph in self.plan["build_graphs"]
                       for row in graph["expected_outputs"]}
        if set(recorded) - allowed_ids:
            raise RetentionError("session graph-output receipt contains an undeclared output")
        return expected_rows

    def _stage_root_snapshot(self) -> dict[str, Any]:
        stage = _session_safe_path(
            self.workspace, _session_relative_path(self.plan["stage_root"], "stage_root"),
            must_exist=True, directory=True)
        stage_fd = _open_dir(stage)
        try:
            identity = _fs_identity(stage_fd)
        finally:
            os.close(stage_fd)
        tree_sha256, file_count = _session_tree_digest(stage)
        return {"stage_root_identity": identity, "tree_sha256": tree_sha256,
                "file_count": file_count}

    def _stage_install_snapshot(self, *, initial: bool = False) -> dict[str, Any]:
        stage = _session_safe_path(
            self.workspace, _session_relative_path(self.plan["stage_root"], "stage_root"),
            must_exist=True, directory=True)
        if initial and set(os.listdir(stage)) != {"usr"}:
            raise RetentionError("Windows Meson install must populate only the fixed /usr stage subtree")
        usr = _session_safe_path(stage, Path("usr"), must_exist=True, directory=True)
        usr_fd = _open_dir(usr)
        try:
            identity = _fs_identity(usr_fd)
        finally:
            os.close(usr_fd)
        tree_sha256, file_count = _session_tree_digest(usr)
        if file_count == 0:
            raise RetentionError("Windows Meson install produced no receipted product files")
        files: list[dict[str, Any]] = []
        for directory, dirnames, filenames in os.walk(usr, followlinks=False):
            base = Path(directory)
            for name in sorted(dirnames):
                info = (base / name).lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise RetentionError("Windows product install tree contains a linked or special directory")
            for name in sorted(filenames):
                path = base / name
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise RetentionError("Windows product install tree contains a linked or special file")
                digest = _hash_path(path)
                after = path.lstat()
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise RetentionError("Windows product file changed while its install receipt was captured")
                with path.open("rb") as stream:
                    magic = stream.read(4)
                suffix = path.suffix.casefold()
                if suffix in {".dll", ".exe"}:
                    file_format, architecture = _session_graph_format_identity(
                        path, "PE32PLUS_X86_64")
                elif magic in _MACHO_MAGIC_PREFIXES:
                    raise RetentionError("Windows product stage contains a native Mach-O artifact")
                else:
                    file_format, architecture = "DATA", None
                files.append({"graph_id": "windows-product-r6a",
                              "graph_role": "WINDOWS_PRODUCT",
                              "producer_target_id": "DEFAULT_PRODUCTION_OUTPUTS",
                              "path": path.relative_to(usr).as_posix(),
                              "format": file_format, "architecture": architecture,
                              "file_identity": {"device": before.st_dev, "inode": before.st_ino,
                                                "mode": stat.S_IMODE(before.st_mode),
                                                "size_bytes": before.st_size, "sha256": digest}})
        if len(files) != file_count:
            raise RetentionError("Windows product install tree file inventory differs from its tree digest")
        if _session_tree_digest(usr) != (tree_sha256, file_count):
            raise RetentionError("Windows product install tree changed while per-file identities were captured")
        return {"graph_id": "windows-product-r6a", "graph_role": "WINDOWS_PRODUCT",
                "producer_target_id": "DEFAULT_PRODUCTION_OUTPUTS",
                "stage_role": "WINDOWS_PRODUCT_OUTPUTS", "path": "stage/usr",
                "directory_identity": identity, "tree_sha256": tree_sha256,
                "file_count": file_count, "files": files,
                "identity_boundary": "FIXED_WINDOWS_INSTALL_GRAPH_AND_PER_FILE_HASH_FORMAT_RECEIPTS"}

    def _verify_windows_pe_stage(self, session: Mapping[str, Any]) -> dict[str, Any]:
        graph = _session_dual_graph_by_id(self.plan, "windows-product-r6a")
        output = graph["expected_outputs"][0]
        recorded_graph_output = session.get("graph_outputs", {}).get(output["output_id"])
        if not isinstance(recorded_graph_output, dict):
            raise RetentionError("Windows PE stage has no declared Windows graph producer output")
        current_graph_output = _session_graph_output_identity(self.workspace, graph, output)
        if current_graph_output != recorded_graph_output:
            raise RetentionError("Windows PE producer output changed before staging or acceptance")
        staged = _session_safe_path(
            self.workspace, BUILD_SESSION_DUAL_GRAPH_WINDOWS_PE_STAGE_RELATIVE,
            must_exist=True, directory=False)
        info = staged.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise RetentionError("staged Windows PE product is not a regular executable file")
        digest = _hash_path(staged)
        actual_format, architecture = _session_graph_format_identity(
            staged, "PE32PLUS_X86_64")
        identity = {
            "graph_id": graph["graph_id"], "graph_role": graph["graph_role"],
            "source_identity": graph["source_identity"],
            "stage_role": "WINDOWS_PE_NEVER_EXECUTE",
            "output_id": output["output_id"], "source_output_identity": recorded_graph_output,
            "path": BUILD_SESSION_DUAL_GRAPH_WINDOWS_PE_STAGE_RELATIVE.as_posix(),
            "format": actual_format, "architecture": architecture,
            "file_identity": {"device": info.st_dev, "inode": info.st_ino,
                              "mode": stat.S_IMODE(info.st_mode), "size_bytes": info.st_size,
                              "sha256": digest},
        }
        if identity["file_identity"]["sha256"] != recorded_graph_output["file_identity"]["sha256"]:
            raise RetentionError("staged Windows PE differs from its exact graph-produced bytes")
        return identity

    def _verify_stage_outputs(self, session: Mapping[str, Any], *, require_complete: bool = False) -> dict[str, Any]:
        stage_snapshot = self._stage_root_snapshot()
        saved_tree = session.get("stage_tree_identity")
        if saved_tree is not None and saved_tree != stage_snapshot:
            raise RetentionError("declared stage tree changed after its controller-owned operation")
        saved_install = session.get("stage_install_tree_identity")
        if saved_install is not None and saved_install != self._stage_install_snapshot():
            raise RetentionError("Windows product install outputs changed after the Meson install step")
        windows_saved = session.get("windows_pe_stage_identity")
        runtime_saved = session.get("native_runtime_stage_identity")
        if windows_saved is not None and windows_saved != self._verify_windows_pe_stage(session):
            raise RetentionError("staged Windows PE output changed after its typed operation")
        if runtime_saved is not None and runtime_saved != self._native_runtime_stage_snapshot():
            raise RetentionError("staged native runtime closure changed after its typed operation")
        if require_complete and (windows_saved is None or runtime_saved is None or saved_tree is None):
            raise RetentionError("dual-graph stage outputs are incomplete at read-only verification")
        return stage_snapshot

    def _stage_windows_product(self, session: dict[str, Any]) -> dict[str, Any]:
        if session.get("windows_pe_stage_identity") is not None:
            raise RetentionError("Windows product output was already staged")
        graph = _session_dual_graph_by_id(self.plan, "windows-product-r6a")
        output = graph["expected_outputs"][0]
        source_identity = session.get("graph_outputs", {}).get(output["output_id"])
        if not isinstance(source_identity, dict):
            raise RetentionError("Windows PE staging requires its exact graph output receipt")
        current_source = _session_graph_output_identity(self.workspace, graph, output)
        if current_source != source_identity:
            raise RetentionError("Windows PE producer output changed before typed staging")
        source_path = self.workspace / graph["build_directory"] / output["path"]
        destination = self.workspace / BUILD_SESSION_DUAL_GRAPH_WINDOWS_PE_STAGE_RELATIVE
        destination_dir = destination.parent
        if destination_dir.exists() or destination_dir.is_symlink():
            raise RetentionError("typed Windows PE destination already exists")
        destination_dir.mkdir(mode=0o700)
        parent_fd = _open_dir(source_path.parent)
        source_fd = None
        destination_fd = None
        try:
            source_fd = os.open(source_path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                dir_fd=parent_fd)
            before = os.fstat(source_fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (source_identity["file_identity"]["device"],
                        source_identity["file_identity"]["inode"],
                        source_identity["file_identity"]["size_bytes"],
                        source_path.stat(follow_symlinks=False).st_mtime_ns)):
                raise RetentionError("Windows PE producer changed immediately before stage copy")
            stage_parent_fd = _open_dir(destination_dir)
            try:
                destination_fd = os.open(
                    destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0), 0o500, dir_fd=stage_parent_fd)
                digest = hashlib.sha256()
                while True:
                    block = os.read(source_fd, 1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    view = memoryview(block)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise RetentionError("typed Windows PE stage copy made no progress")
                        view = view[written:]
                os.fchmod(destination_fd, 0o500)
                os.fsync(destination_fd)
            finally:
                if destination_fd is not None:
                    os.close(destination_fd)
                    destination_fd = None
                os.close(stage_parent_fd)
            after = os.fstat(source_fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or digest.hexdigest() != source_identity["file_identity"]["sha256"]):
                raise RetentionError("Windows PE producer changed while the controller staged it")
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
            os.close(parent_fd)
        staged_identity = self._verify_windows_pe_stage(session)
        session["windows_pe_stage_identity"] = staged_identity
        stage_snapshot = self._stage_root_snapshot()
        session["stage_tree_identity"] = stage_snapshot
        return staged_identity

    def _native_runtime_stage_snapshot(self) -> dict[str, Any]:
        runtime_dir = _session_safe_path(
            self.workspace, BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE,
            must_exist=True, directory=True)
        runtime_fd = _open_dir(runtime_dir)
        try:
            directory_identity = _fs_identity(runtime_fd)
            expected_names = {BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[row["runtime_id"]]
                              for row in self.plan["runtime_receipts"]}
            if set(os.listdir(runtime_fd)) != expected_names:
                raise RetentionError("native runtime staging directory has missing or unexpected files")
        finally:
            os.close(runtime_fd)
        rows = []
        for receipt in self.plan["runtime_receipts"]:
            filename = BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[receipt["runtime_id"]]
            path = runtime_dir / filename
            identity = _session_regular_file_identity(path, receipt["sha256"],
                                                     f"staged native {receipt['runtime_id']} runtime")
            macho_identity = _session_macho_dependency_identity(path)
            architecture = macho_identity["architecture"]
            native = _session_native_architecture()
            if architecture != native or macho_identity["filetype"] != 6:
                raise RetentionError("staged SDL runtime is not a native thin Mach-O dylib")
            dependency_bindings = _session_macho_dependency_bindings(
                macho_identity, [row["runtime_id"] for row in self.plan["runtime_receipts"]])
            if identity["mode"] & 0o222:
                raise RetentionError("staged native runtime files must be read-only")
            rows.append({"runtime_id": receipt["runtime_id"],
                         "path": BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE.joinpath(filename).as_posix(),
                         "format": "MACHO_DYLIB_NATIVE_ARCH", "architecture": architecture,
                         "file_identity": identity,
                         "macho_dependency_identity": macho_identity,
                         "macho_dependency_bindings": dependency_bindings,
                         "resolution_claim": BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_RESOLUTION_CLAIM})
        tree_sha256, file_count = _session_tree_digest(runtime_dir)
        if file_count != len(expected_names):
            raise RetentionError("native runtime stage contains an unexpected filesystem object")
        return {"graph_id": "native-verification-r6a",
                "stage_role": "NATIVE_SDL2_RUNTIME_CLOSURE",
                "runtime_directory_identity": directory_identity,
                "tree_sha256": tree_sha256, "file_count": file_count,
                "files": rows}

    def _native_loader_provenance(self, session: Mapping[str, Any],
                                  loader_path: str) -> dict[str, Any]:
        stage_identity = self._verify_native_runtime_stage(session)
        if loader_path != str(self.workspace / BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE):
            raise RetentionError("native SDL2 loader path is not the controller-derived runtime stage")
        output = session.get("graph_outputs", {}).get("r6a-production-path-macho")
        if not isinstance(output, Mapping) or output.get("graph_id") != "native-verification-r6a":
            raise RetentionError("native loader provenance has no bound native test graph output")
        return {
            "graph_id": "native-verification-r6a",
            "graph_role": "NATIVE_VERIFICATION",
            "output_id": "r6a-production-path-macho",
            "requested_basename": BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_BASENAME,
            "controller_derived_dyld_library_path": loader_path,
            "runtime_stage_identity": stage_identity,
            "output_macho_dependency_identity": output.get("macho_dependency_identity"),
            "output_macho_dependency_bindings": output.get("macho_dependency_bindings"),
            "resolution_claim": BUILD_SESSION_DUAL_GRAPH_NATIVE_LOADER_RESOLUTION_CLAIM,
            "dyld_resolution_verified": False,
        }

    def _stage_native_runtime(self, session: dict[str, Any]) -> dict[str, Any]:
        """Copy only the fixed SDL2/SDL3 closure into its controller-owned stage role."""
        if not _session_dual_graph_enabled(self.plan):
            raise RetentionError("native runtime staging requires the dual-graph capability")
        if session.get("native_runtime_stage_identity") is not None:
            raise RetentionError("native runtime closure was already staged")
        self._verify_windows_pe_stage(session)
        stage_root = _session_safe_path(
            self.workspace, _session_relative_path(self.plan["stage_root"], "stage_root"),
            must_exist=True, directory=True)
        runtime_parent = stage_root / "native-verification"
        if runtime_parent.exists() or runtime_parent.is_symlink():
            raise RetentionError("native runtime staging role must be absent before its typed operation")
        runtime_parent.mkdir(mode=0o700)
        runtime_dir = runtime_parent / "runtime"
        runtime_dir.mkdir(mode=0o700)
        runtime_fd = _open_dir(runtime_dir)
        try:
            for receipt in self.plan["runtime_receipts"]:
                runtime_id = receipt["runtime_id"]
                filename = BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[runtime_id]
                source_path = Path(receipt["path"])
                if source_path.is_symlink() or source_path.resolve(strict=True) != source_path:
                    raise RetentionError("native runtime source path is not a canonical real file")
                source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    before = os.fstat(source_fd)
                    if not stat.S_ISREG(before.st_mode):
                        raise RetentionError("native runtime source is not a regular file")
                    destination_fd = os.open(
                        filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o500, dir_fd=runtime_fd)
                    digest = hashlib.sha256()
                    try:
                        while True:
                            block = os.read(source_fd, 1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                            view = memoryview(block)
                            while view:
                                written = os.write(destination_fd, view)
                                if written <= 0:
                                    raise RetentionError("native runtime staging copy made no progress")
                                view = view[written:]
                        os.fchmod(destination_fd, 0o500)
                        os.fsync(destination_fd)
                    finally:
                        os.close(destination_fd)
                    after = os.fstat(source_fd)
                    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                            or digest.hexdigest() != receipt["sha256"]):
                        raise RetentionError("native runtime source changed while being staged")
                finally:
                    os.close(source_fd)
        finally:
            os.close(runtime_fd)
        snapshot = self._native_runtime_stage_snapshot()
        session["native_runtime_stage_identity"] = snapshot
        session["stage_tree_identity"] = self._stage_root_snapshot()
        return snapshot

    def _verify_native_runtime_stage(self, session: Mapping[str, Any]) -> dict[str, Any]:
        saved = session.get("native_runtime_stage_identity")
        if not isinstance(saved, dict):
            raise RetentionError("native SDL2 runtime closure has not been staged")
        current = self._native_runtime_stage_snapshot()
        if saved != current:
            raise RetentionError("staged native SDL2 runtime closure changed after its typed operation")
        return current

    def _environment_for_step(self, step: Mapping[str, Any],
                              session: Mapping[str, Any] | None = None) -> dict[str, str]:
        environment = dict(self.plan["environment"])
        if not _session_dual_graph_enabled(self.plan):
            return environment
        environment["PATH"] = BUILD_SESSION_DUAL_GRAPH_PATH
        environment.pop("DXVK_WSI_DRIVER", None)
        environment.pop("MACOSX_DEPLOYMENT_TARGET", None)
        if step.get("kind") == "test":
            graph = _session_dual_graph_by_id(self.plan, str(step.get("graph_id", "")))
            if graph["graph_role"] != "NATIVE_VERIFICATION":
                raise RetentionError("only the native verification graph may execute a test")
            profile = self.plan["native_host_profile"]
            environment["DEVELOPER_DIR"] = profile["developer_directory"]
            environment["SDKROOT"] = profile["sdk_resolved_path"]
            if session is None:
                session = self._load(allow_running=True)[1]
            self._verify_stage_outputs(session, require_complete=True)
            if step.get("test_class") == "PRODUCTION_PATH":
                environment["DXVK_WSI_DRIVER"] = "SDL2"
                self._verify_native_runtime_stage(session)
                environment["DYLD_LIBRARY_PATH"] = str(
                    self.workspace / BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE)
            elif step.get("test_class") not in {"UNIT", "MODEL"}:
                raise RetentionError("dual-graph test class is outside the fixed native test set")
            return environment
        if step.get("kind") not in {"configure", "build", "install"}:
            return environment
        graph = _session_dual_graph_by_id(self.plan, str(step.get("graph_id", "")))
        ids = graph["tool_receipt_ids"]
        if graph["graph_role"] == "WINDOWS_PRODUCT":
            environment.update({"CC": self.plan["tools"]["mingw_cc"]["path"],
                                "CXX": self.plan["tools"]["mingw_cxx"]["path"],
                                "AR": self.plan["tools"]["mingw_ar"]["path"]})
        else:
            profile = self.plan["native_host_profile"]
            environment["DEVELOPER_DIR"] = profile["developer_directory"]
            environment["SDKROOT"] = profile["sdk_resolved_path"]
            environment.update({"CC": self.plan["tools"]["native_cc"]["path"],
                                "CXX": self.plan["tools"]["native_cxx"]["path"],
                                "AR": self.plan["tools"]["native_ar"]["path"],
                                "RANLIB": self.plan["tools"]["native_ranlib"]["path"],
                                "PKG_CONFIG_PATH": str(Path(next(
                                    row["path"] for row in self.plan["host_dependency_receipts"]
                                    if row["input_id"] == "SDL2_PKGCONFIG")).parent)})
        if not set(ids).issubset(self.plan["tools"]):
            raise RetentionError("graph toolchain identities are incomplete")
        return environment

    def _command_for_dual_graph_step(self, step: Mapping[str, Any],
                                     session: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
        kind = step["kind"]
        graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
        graph_dir = self.workspace / graph["build_directory"]
        if kind == "configure":
            current_graph_identity = _session_graph_directory_identity(
                self.workspace, graph, must_exist=False)
            recorded_graph_identity = session.get("graph_directory_identities", {}).get(graph["graph_id"])
            if (current_graph_identity is not None
                    and current_graph_identity != recorded_graph_identity):
                raise RetentionError("declared graph build directory must be absent before its configure step")
            command = [self.plan["tools"]["meson"]["path"], "setup",
                       "--wrap-mode=nodownload", "--backend=ninja", "--prefix=/usr",
                       f"--buildtype={self.lease.metadata['build_type']}"]
            if graph["graph_role"] == "WINDOWS_PRODUCT":
                cross_file = self.plan["source_identity"]["root"] + "/" + graph["cross_file_input"]
                command.extend(["--cross-file", cross_file])
            else:
                command.extend([
                    "-Denable_dxgi=true", "-Denable_d3d11=true",
                    "-Denable_d3d8=false", "-Denable_d3d9=false", "-Denable_d3d10=false",
                    "-Dnative_sdl2=enabled", "-Dnative_sdl3=disabled", "-Dnative_glfw=disabled",
                ])
            command.extend([str(graph_dir), self.plan["source_identity"]["root"]])
            return command, None
        current_identity = self._graph_directory_identity(graph, must_exist=True)
        if session.get("graph_directory_identities", {}).get(graph["graph_id"]) != current_identity:
            raise RetentionError("graph build directory is not bound to its completed configure step")
        if kind == "build":
            target_id = step.get("target_id")
            if target_id not in graph["targets"] or target_id not in BUILD_SESSION_DUAL_GRAPH_TARGETS:
                raise RetentionError("Ninja target ID is outside the closed graph target map")
            target = BUILD_SESSION_DUAL_GRAPH_TARGETS[target_id]
            command = [self.plan["tools"]["ninja"]["path"], "-C", str(graph_dir)]
            if target is not None:
                command.append(target)
            return command, None
        if kind == "install":
            if graph["graph_role"] != "WINDOWS_PRODUCT":
                raise RetentionError("only the Windows product graph may stage product outputs")
            stage = str(self.workspace / _session_relative_path(self.plan["stage_root"], "stage_root"))
            return [self.plan["tools"]["meson"]["path"], "install", "-C", str(graph_dir),
                    "--destdir", stage, "--no-rebuild"], None
        if kind == "test":
            identity = self._validate_test_executable(step, session)
            args = step.get("arguments", [])
            if args:
                raise RetentionError("dual-graph production-path tests do not accept caller-supplied arguments")
            return [identity["executable_path"]], identity
        raise RetentionError(f"unsupported dual-graph step kind: {kind}")

    def start(self) -> None:
        _require_buildlease_vm_runtime(self.plan)
        lease_record, session = self._load(allow_running=True)
        if session.get("state") != "RESERVED" or session.get("next_step_index") != 0:
            raise RetentionError("v3 BuildLease session has already started or cannot be resumed")
        if lease_record.get("session_started") is not False:
            raise RetentionError("v3 BuildLease start lacks its exact never-started live reservation state")
        if self.lease.session_plan_sha256 != self.plan_sha256:
            raise RetentionError("session plan changed after reservation")
        if any((self.root / name).exists() or (self.root / name).is_symlink()
               for name in (BUILD_EXECUTION_RECEIPT, "build-command.json", "build.log")):
            raise RetentionError("v1 execution evidence already exists in a v3 session workspace")
        source_identity = self.lease._validate_session_inputs()
        stage_relative = _session_relative_path(self.plan["stage_root"], "stage_root")
        if len(stage_relative.parts) != 1:
            raise RetentionError("stage root must be one direct child of the leased workspace")
        lease_record["session_started"] = True
        lease_record["session_started_at_unix"] = time.time()
        self._store(lease_record)
        self.session_dir.mkdir(mode=0o700)
        (self.session_dir / "steps").mkdir(mode=0o700)
        session["workspace_mount_pending"] = True
        lease_record["session_v3"] = session
        self._store(lease_record)
        mount_capacity = self.lease.expected_bytes - BUILD_SESSION_CONTROLLER_HEADROOM_BYTES
        workspace_mount_identity = _attach_session_workspace(self.root, mount_capacity)
        session["workspace_mount_identity"] = workspace_mount_identity
        session.pop("workspace_mount_pending", None)
        lease_record["session_v3"] = session
        self._store(lease_record)
        if _session_dual_graph_enabled(self.plan):
            for graph in self.plan["build_graphs"]:
                if _session_graph_directory_identity(self.workspace, graph, must_exist=False) is not None:
                    raise RetentionError("dual-graph build directories must be absent before configure")
        stage = self.workspace / stage_relative
        if stage.exists() or stage.is_symlink():
            raise RetentionError("declared stage root must be absent at v3 session creation")
        stage.mkdir(mode=0o700)
        output_relative = _session_relative_path(self.plan["output_root"], "output_root", allow_dot=True)
        if output_relative != Path("."):
            if len(output_relative.parts) != 1:
                raise RetentionError("output root must be the lease root or one direct child")
            output = self.workspace / output_relative
            if output.exists() or output.is_symlink():
                raise RetentionError("declared output root must be absent at v3 session creation")
            output.mkdir(mode=0o700)
        (self.workspace / ".home").mkdir(mode=0o700)
        (self.workspace / ".tmp").mkdir(mode=0o700)
        for relative in (Path(".home/.cache"), Path(".home/.config"),
                         Path(".home/.local/share")):
            (self.workspace / relative).mkdir(mode=0o700, parents=True)
        for step in self.plan["steps"]:
            if step.get("kind") != "test":
                continue
            if _session_dual_graph_enabled(self.plan):
                graph = _session_dual_graph_by_id(self.plan, step["graph_id"])
                output = next(row for row in graph["expected_outputs"]
                              if row["output_id"] == step["output_id"])
                relative = Path(graph["build_directory"]) / _session_relative_path(
                    output["path"], "native test output path")
                candidate = self.workspace / relative
                current = self.workspace
            else:
                relative = _session_relative_path(step["executable"], "registered test executable")
                candidate = self.workspace / output_relative / relative
                current = self.workspace / output_relative
            missing = False
            for part in relative.parts:
                current = current / part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    missing = True
                    break
                if stat.S_ISLNK(info.st_mode):
                    raise RetentionError("registered test path contains a symlink before the build")
            if not missing:
                raise RetentionError(f"registered test executable already exists before the session: {candidate}")
        root_fd = _open_dir(self.root)
        output_fd = _open_dir(self.workspace if output_relative == Path(".") else self.workspace / output_relative)
        stage_fd = _open_dir(stage)
        try:
            root_info = os.fstat(root_fd)
            fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
            root_identity = {"device": root_info.st_dev, "inode": root_info.st_ino,
                             "filesystem_id": fsid}
            stage_identity = _fs_identity(stage_fd)
            output_identity = _fs_identity(output_fd)
        finally:
            os.close(root_fd)
            os.close(output_fd)
            os.close(stage_fd)
        session.update({"state": "ACTIVE", "started_at_unix": time.time(),
                        "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                        "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                        "build_root_identity": root_identity,
                        "stage_root_identity": stage_identity,
                        "output_root_identity": output_identity,
                        "workspace_mount_identity": workspace_mount_identity,
                        "source_root_identity": {"device": source_identity["source_device"],
                                                 "inode": source_identity["source_inode"]},
                        "source_start_identity": source_identity,
                        "input_tool_identities": source_identity["tools"],
                        "next_step_index": 0, "step_receipts": [], "registered_tests": {},
                        "peak_allocated_bytes": 0, "storage_watch_events": []})
        if _session_dual_graph_enabled(self.plan):
            session.update({
                "graph_directory_identities": {}, "graph_outputs": {},
                "graph_step_output_rows": {}, "stage_tree_identity": None,
                "stage_install_tree_identity": None, "windows_pe_stage_identity": None,
                "native_runtime_stage_identity": None, "typed_stage_step_identities": {},
                "stage_step_tree_identities": {},
                "initial_stage_tree_identity": self._stage_root_snapshot(),
            })
        lease_record["session_v3"] = session
        plan_receipt = {"schema_version": BUILD_SESSION_SCHEMA_VERSION,
                        "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                        "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                        "build_id": self.lease.build_id, "plan_sha256": self.plan_sha256,
                        "plan": self.plan, "admission_identity": self.plan["admission_identity"],
                        "source_identity": self.plan["source_identity"],
                        "build_root_identity": root_identity, "stage_root_identity": stage_identity,
                        "output_root_identity": output_identity,
                        "workspace_mount_identity": workspace_mount_identity,
                        "created_at_unix": session["started_at_unix"]}
        _write_json(self.session_dir / BUILD_SESSION_PLAN, plan_receipt, replace=False, sealed=True)
        self._store(lease_record)
        self._check_limits("session-start")

    def _command_for_step(self, step: Mapping[str, Any], session: dict[str, Any]) -> tuple[list[str], dict[str, Any] | None]:
        if _session_dual_graph_enabled(self.plan):
            return self._command_for_dual_graph_step(step, session)
        kind = step["kind"]
        if kind == "test":
            test_identity = self._validate_test_executable(step, session)
            args = step.get("arguments", [])
            if any(any(char in token for char in ("\x00", "`", "$(", "\n", "\r")) for token in args):
                raise RetentionError("registered test arguments contain forbidden command syntax")
            return [test_identity["path"], *args], test_identity
        command = list(step["argv"])
        if not command or any(any(char in token for char in ("\x00", "`", "$(", "\n", "\r"))
                                  for token in command):
            raise RetentionError("session command contains forbidden shell/interpreter syntax")
        kind_tool = {"configure": "meson", "install": "meson", "build": "ninja"}.get(kind)
        if kind_tool is None or command[0] != self.plan["tools"][kind_tool]["path"]:
            raise RetentionError(f"unregistered executable for v3 {kind} step")
        build = str(self.workspace)
        source = self.plan["source_identity"]["root"]
        if kind == "configure":
            if len(command) < 4 or command[1] != "setup":
                raise RetentionError("configure step must use the accepted Meson setup form")
            positionals: list[str] = []
            options: set[str] = set()
            args = command[2:]
            index = 0
            cross_files = {row["path"] for row in self.plan.get("input_receipts", [])}
            source_root = Path(source)
            while index < len(args):
                token = args[index]
                if token == "--strip":
                    key = "--strip"
                    if key in options:
                        raise RetentionError("Meson setup option is duplicated: --strip")
                    options.add(key)
                    index += 1
                    continue
                if token.startswith("-D"):
                    if token != f"-Dbuildtype={self.lease.metadata['build_type']}":
                        raise RetentionError(f"Meson define is not authorized for this session: {token}")
                    key = "buildtype"
                    value = self.lease.metadata["build_type"]
                elif token.startswith("--"):
                    key, separator, value = token.partition("=")
                    if not separator:
                        if index + 1 >= len(args):
                            raise RetentionError(f"Meson setup option lacks a value: {key}")
                        value = args[index + 1]
                        index += 1
                    if key == "--prefix":
                        if value != "/usr":
                            raise RetentionError("Meson install prefix must remain /usr under the declared DESTDIR")
                        key = "prefix"
                    elif key == "--backend":
                        if value != "ninja":
                            raise RetentionError("Meson backend must be Ninja")
                    elif key == "--buildtype":
                        if value != self.lease.metadata["build_type"]:
                            raise RetentionError("Meson build type differs from the signed BuildLease")
                        key = "buildtype"
                    elif key == "--wrap-mode":
                        if value != "nodownload":
                            raise RetentionError("Meson wrap mode must prohibit network downloads")
                        key = "wrap-mode"
                    elif key == "--cross-file":
                        selected = Path(value)
                        relative = (selected.relative_to(source_root).as_posix()
                                    if selected.is_absolute() and source_root in selected.parents
                                    else value)
                        if relative not in cross_files:
                            raise RetentionError("Meson cross file must be an exactly hashed admitted source input")
                        expected = str(source_root / relative)
                        if str(selected) != expected:
                            raise RetentionError("Meson cross file path must be canonical beneath the admitted source")
                        value = expected
                        key = "cross-file"
                    else:
                        raise RetentionError(f"Meson setup option is not authorized for this session: {key}")
                else:
                    positionals.append(token)
                    index += 1
                    continue
                if key in options:
                    raise RetentionError(f"Meson setup option is duplicated: {key}")
                options.add(key)
                index += 1
            if positionals != [build, source]:
                raise RetentionError("Meson setup must name this exact build workspace and source root")
        elif kind == "build":
            if command[1:] not in ([], ["-C", build]):
                raise RetentionError("Ninja build must target only the admitted BuildLease workspace")
        elif kind == "install":
            stage = str(self.workspace / _session_relative_path(self.plan["stage_root"], "stage_root"))
            if (len(command) not in {6, 7} or command[1] != "install"
                    or command[2:4] != ["-C", build]
                    or command[4:6] != ["--destdir", stage]
                    or (len(command) == 7 and command[6] != "--no-rebuild")):
                raise RetentionError("Meson install must stage only into the declared session stage root")
        return command, None

    def run_next(self) -> dict[str, Any]:
        return self._with_operation("run_next", self._run_next_locked)

    def _run_typed_stage_step(self, index: int, step: Mapping[str, Any],
                              lease_record: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        """Run one closed controller-owned copy operation without launching a command."""
        kind = step["kind"]
        action = (BUILD_SESSION_DUAL_GRAPH_WINDOWS_STAGE_ACTION
                  if kind == "stage_windows_product" else BUILD_SESSION_DUAL_GRAPH_NATIVE_STAGE_ACTION)
        command_path = self.session_dir / "steps" / f"step-{index:03d}.command.json"
        result_path = self.session_dir / "steps" / f"step-{index:03d}.result.json"
        try:
            if command_path.exists() or result_path.exists():
                raise RetentionError("duplicate or replayed typed stage evidence is not permitted")
            self._check_root_bindings(lease_record, session)
            source_before = self._validate_source()
            inputs_before = self.lease._validate_session_inputs()
            if source_before != inputs_before:
                raise RetentionError("typed staging input observations are inconsistent")
            graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
            graph_directory_identity = self._graph_directory_identity(graph, must_exist=True)
            graph_provenance = self._graph_provenance(step)
            graph_outputs_before = self._verify_recorded_graph_outputs(session)
            self._verify_stage_outputs(session)
            peak_before = self._check_limits("before-typed-stage-step", extra_bytes=32 * 1024)
            command_payload = {
                "schema_version": BUILD_SESSION_SCHEMA_VERSION,
                "build_id": self.lease.build_id,
                "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                "plan_sha256": self.plan_sha256, "step_index": index, "kind": kind,
                "action": action, "command": None, "cwd": str(self.workspace),
                "source_observation": source_before,
                "input_tool_identities": inputs_before["tools"],
                "input_receipts": inputs_before["input_receipts"],
                "graph_tool_identities": inputs_before["graph_tools"],
                "runtime_input_identities": inputs_before["runtime_inputs"],
                "graph_provenance": graph_provenance,
                "graph_directory_identity_before": graph_directory_identity,
                "graph_output_identities_before": graph_outputs_before,
                "stage_tree_identity_before": self._stage_root_snapshot(),
                "windows_pe_stage_identity_before": session.get("windows_pe_stage_identity"),
                "native_runtime_stage_identity_before": session.get("native_runtime_stage_identity"),
                "sandbox_launcher_path": None, "sandbox_launcher_sha256": None,
                "sandbox_profile_sha256": None, "environment_sha256": None,
                "registered_executable_identity": None,
                "started_at_unix": time.time(),
            }
            _write_json(command_path, command_payload, replace=False, sealed=True)
            session["state"] = "RUNNING"
            session["inflight_step_index"] = index
            lease_record["session_v3"] = session
            self._store(lease_record)
            self._check_root_bindings(_load_lease(self.lease.build_id), session)
            if self._validate_source() != source_before or self.lease._validate_session_inputs() != inputs_before:
                raise RetentionError("source, tool, or runtime input changed before typed staging")
            if kind == "stage_windows_product":
                stage_operation_identity = self._stage_windows_product(session)
            else:
                stage_operation_identity = self._stage_native_runtime(session)
            source_after = self._validate_source()
            inputs_after = self.lease._validate_session_inputs()
            if source_after != source_before or inputs_after != inputs_before:
                raise RetentionError("source, tool, or runtime input changed during typed staging")
            if self._graph_directory_identity(graph, must_exist=True) != graph_directory_identity:
                raise RetentionError("build graph directory changed during typed staging")
            graph_output_rows = self._verify_recorded_graph_outputs(session)
            stage_snapshot = self._verify_stage_outputs(
                session, require_complete=(kind == "stage_native_runtime"))
            if stage_snapshot != session.get("stage_tree_identity"):
                raise RetentionError("typed staging did not seal the complete stage tree identity")
            peak = self._check_limits("typed-stage-step-boundary", extra_bytes=32 * 1024)
            session.setdefault("graph_step_output_rows", {})[str(index)] = graph_output_rows
            session.setdefault("typed_stage_step_identities", {})[str(index)] = {
                "action": action, "stage_operation_identity": stage_operation_identity,
                "stage_tree_identity": stage_snapshot,
            }
            lease_record["session_v3"] = session
            self._store(lease_record)
            self._write_result(
                index, "SUCCESS", command_path, None, return_code=0,
                source_observation=source_before, peak_allocated_bytes=peak,
                input_observation=inputs_before, input_observation_after=inputs_after,
                graph_provenance=graph_provenance,
                graph_directory_identity=graph_directory_identity,
                graph_output_identities=graph_output_rows,
                stage_operation_identity=stage_operation_identity,
                stage_tree_identity=stage_snapshot)
            lease_record, session = self._load(allow_running=True)
            result_hash = _hash_path(result_path)
            command_hash = _hash_path(command_path)
            session["state"] = "ACTIVE"
            session.pop("inflight_step_index", None)
            session["next_step_index"] = index + 1
            session["step_receipts"] = list(session.get("step_receipts", [])) + [{
                "step_index": index, "command_receipt_sha256": command_hash,
                "result_receipt_sha256": result_hash}]
            lease_record["session_v3"] = session
            self._store(lease_record)
            return {"status": "STEP_SUCCEEDED_SESSION_ACTIVE", "step_index": index,
                    "kind": kind, "action": action, "peak_allocated_bytes": peak,
                    "lease_state": "ACTIVE"}
        except BaseException as error:
            try:
                current = _load_lease(self.lease.build_id)
                if current.get("state") == "ACTIVE" and current.get("session_v3", {}).get("state") != "FAILED":
                    self._fail(repr(error), -1, command=[action], step_index=index)
            except BaseException:
                pass
            raise

    def _run_next_locked(self) -> dict[str, Any]:
        _require_buildlease_vm_runtime(self.plan)
        lease_record, session = self._load(allow_running=True)
        if session.get("state") == "RUNNING":
            if (session.get("output_capture_uncertain") is True
                    or session.get("process_containment_uncertain") is True
                    or session.get("process_session_id") is not None):
                raise RetentionError(
                    "v3 session process or output containment is uncertain; the RUNNING lease remains blocking")
            self._fail("a previous session step has an uncertain result and cannot be resumed", -1)
            raise RetentionError("v3 session had an interrupted step and is non-resumable")
        if session.get("state") != "ACTIVE":
            raise RetentionError(f"v3 session cannot execute from state {session.get('state')}")
        index = session.get("next_step_index")
        if not isinstance(index, int) or index < 0 or index >= len(self.plan["steps"]):
            raise RetentionError("v3 session has no remaining command step")
        step = self.plan["steps"][index]
        if step["kind"] == "source_verification":
            raise RetentionError("the final source_verification step requires verify_source()")
        if (_session_dual_graph_enabled(self.plan)
                and step["kind"] in {"stage_windows_product", "stage_native_runtime"}):
            return self._run_typed_stage_step(index, step, lease_record, session)
        process = None
        log_path = self.session_dir / "steps" / f"step-{index:03d}.log"
        command_path = self.session_dir / "steps" / f"step-{index:03d}.command.json"
        result_path = self.session_dir / "steps" / f"step-{index:03d}.result.json"
        command: list[str] = []
        install_stage: Path | None = None
        install_temporary: Path | None = None
        install_temporary_identity: dict[str, Any] | None = None
        output_reader: threading.Thread | None = None
        output_errors: list[BaseException] = []
        output_error_event = threading.Event()
        output_overflow_event = threading.Event()
        output_eof_event = threading.Event()
        output_stop_event = threading.Event()
        process_containment: dict[str, Any] | None = None
        unexpected_descendants = False
        reader_live_uncertain = False
        process_containment_uncertain = False
        process_session_identity_mismatch = False
        native_loader_provenance: dict[str, Any] | None = None
        inputs_after_observation: dict[str, Any] | None = None
        graph_provenance: dict[str, Any] | None = None
        graph_directory_identity_before: dict[str, int] | None = None
        graph_output_rows: list[dict[str, Any]] = []
        graph_output_rows_before: list[dict[str, Any]] = []
        stage_tree_identity: dict[str, Any] | None = None
        stage_tree_identity_before: dict[str, Any] | None = None
        _evidence_bytes_before, log_bytes_before = self._check_controller_evidence_budget()
        remaining_log_bytes = max(0, BUILD_SESSION_TOTAL_LOG_LIMIT_BYTES - log_bytes_before)
        step_log_bytes = 0
        try:
            if command_path.exists() or result_path.exists():
                raise RetentionError("duplicate or replayed session step evidence is not permitted")
            source_observation = self._validate_source()
            inputs_observation = self.lease._validate_session_inputs()
            command, executable_identity = self._command_for_step(step, session)
            if step["kind"] == "test":
                command[0] = executable_identity.get("executable_path", executable_identity["path"])
            dual_graph = _session_dual_graph_enabled(self.plan)
            if dual_graph:
                graph_provenance = self._graph_provenance(step)
                selected_graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
                graph_output_rows_before = self._verify_recorded_graph_outputs(session)
                stage_tree_identity_before = self._stage_root_snapshot()
                if step["kind"] != "configure":
                    graph_directory_identity_before = self._graph_directory_identity(
                        selected_graph, must_exist=True)
            for role, receipt in self.plan["tools"].items():
                _session_hash_executable(Path(receipt["path"]), receipt["sha256"])
            step_environment = self._environment_for_step(step, session)
            self._check_limits("before-step", extra_bytes=32 * 1024)
            write_root = None
            temporary_directory = None
            if step["kind"] == "install":
                install_stage, install_temporary, install_temporary_identity = self._create_install_temporary()
                # Meson records install-log.txt in its build directory. Keep that
                # controller-owned record writable while all installation outputs
                # remain argv-bound to the exact stage root above.
                write_root = self.workspace
                temporary_directory = install_temporary
            # Install output destinations are stage-bound; every command remains
            # confined to the exact writable BuildLease image.
            sandboxed, effective_env, launcher = _sandbox_session_command(command, self.workspace,
                                                                            step_environment,
                                                                            write_root=write_root,
                                                                            temporary_directory=temporary_directory)
            expected_tmp = str(temporary_directory or (self.workspace / ".tmp"))
            expected_home = str(temporary_directory or (self.workspace / ".home"))
            expected_environment_keys = set(step_environment) | {
                "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"}
            dyld_keys = {key for key in effective_env if key.startswith("DYLD_")}
            native_loader_path = str(
                self.workspace / BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE)
            production_path_test = (
                dual_graph and step["kind"] == "test"
                and step.get("test_class") == "PRODUCTION_PATH")
            typed_loader_environment = (
                production_path_test
                and dyld_keys == {"DYLD_LIBRARY_PATH"}
                and effective_env.get("DYLD_LIBRARY_PATH") == native_loader_path)
            if (set(effective_env) != expected_environment_keys
                    or "DESTDIR" in effective_env
                    or any(key.startswith("LD_") for key in effective_env)
                    or (dyld_keys and not typed_loader_environment)
                    or (production_path_test
                        and effective_env.get("DXVK_WSI_DRIVER") != "SDL2")
                    or (dual_graph and step["kind"] == "test" and not production_path_test
                        and ("DXVK_WSI_DRIVER" in effective_env or dyld_keys))
                    or (dual_graph and step["kind"] != "test"
                        and "DXVK_WSI_DRIVER" in effective_env)
                    or effective_env.get("HOME") != expected_home
                    or effective_env.get("TMPDIR") != expected_tmp
                    or effective_env.get("TMP") != expected_tmp
                    or effective_env.get("TEMP") != expected_tmp
                    or effective_env.get("XDG_CACHE_HOME") != str(Path(expected_home) / ".cache")
                    or effective_env.get("XDG_CONFIG_HOME") != str(Path(expected_home) / ".config")
                    or effective_env.get("XDG_DATA_HOME") != str(Path(expected_home) / ".local" / "share")):
                raise RetentionError("effective session environment contains output redirection or hooks")
            if production_path_test:
                native_loader_provenance = self._native_loader_provenance(
                    session, effective_env["DYLD_LIBRARY_PATH"])
            if (len(sandboxed) < 4 or sandboxed[0] != self.plan["tools"]["sandbox"]["path"]
                    or sandboxed[1] != "-p" or sandboxed[3:] != command
                    or launcher != self.plan["tools"]["sandbox"]["path"]):
                raise RetentionError("session launcher must dispatch the exact direct command under Seatbelt")
            profile_sha256 = hashlib.sha256(sandboxed[2].encode("utf-8")).hexdigest()
            launcher_identity = _session_hash_executable(
                Path(launcher), self.plan["tools"]["sandbox"]["sha256"])
            launcher_sha256 = launcher_identity["sha256"]
            lease_record, session = self._load()
            self._check_root_bindings(lease_record, session)
            if executable_identity is not None:
                test_relative = (f"{step['graph_id']}:{step['output_id']}" if dual_graph
                                 else step["executable"])
                stored_executable_identity = (
                    {key: value for key, value in executable_identity.items()
                     if key != "executable_path"}
                    if dual_graph else executable_identity)
                prior_identity = session.setdefault("registered_tests", {}).get(test_relative)
                if prior_identity is not None and prior_identity != stored_executable_identity:
                    raise RetentionError("registered test executable identity changed before it was sealed")
                session["registered_tests"][test_relative] = stored_executable_identity
            command_receipt = {
                "schema_version": BUILD_SESSION_SCHEMA_VERSION, "build_id": self.lease.build_id,
                "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                "plan_sha256": self.plan_sha256, "step_index": index, "kind": step["kind"],
                "command": command, "cwd": str(self.workspace),
                "source_observation": source_observation,
                "input_tool_identities": inputs_observation["tools"],
                "input_receipts": inputs_observation["input_receipts"],
                "environment_sha256": hashlib.sha256(_canonical_json(effective_env)).hexdigest(),
                "sandbox_launcher_path": launcher, "sandbox_launcher_sha256": launcher_sha256,
                "sandbox_profile_sha256": profile_sha256,
                "registered_executable_identity": executable_identity,
                "started_at_unix": time.time(),
            }
            if dual_graph:
                command_receipt.update({
                    "graph_tool_identities": inputs_observation["graph_tools"],
                    "runtime_input_identities": inputs_observation["runtime_inputs"],
                    "graph_provenance": graph_provenance,
                    "graph_directory_identity_before": graph_directory_identity_before,
                    "graph_output_identities_before": graph_output_rows_before,
                    "stage_tree_identity_before": stage_tree_identity_before,
                    "windows_pe_stage_identity_before": session.get("windows_pe_stage_identity"),
                    "native_runtime_stage_identity_before": session.get("native_runtime_stage_identity"),
                })
                if native_loader_provenance is not None:
                    command_receipt["native_loader_provenance"] = native_loader_provenance
            _write_json(command_path, command_receipt, replace=False, sealed=True)
            session["state"] = "RUNNING"
            session["inflight_step_index"] = index
            session["sandbox_launcher_path"] = launcher
            session["sandbox_launcher_sha256"] = launcher_sha256
            session["sandbox_profile_sha256"] = profile_sha256
            lease_record["session_v3"] = session
            self._store(lease_record)
            with log_path.open("xb") as stream:
                self._validate_source()
                for receipt in self.plan["tools"].values():
                    _session_hash_executable(Path(receipt["path"]), receipt["sha256"])
                if executable_identity is not None:
                    self._validate_test_executable(step, session, after_execution=False)
                if dual_graph and step["kind"] != "configure":
                    selected_graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
                    if self._graph_directory_identity(selected_graph, must_exist=True) != graph_directory_identity_before:
                        raise RetentionError("graph build directory changed immediately before command dispatch")
                if dual_graph:
                    self._verify_recorded_graph_outputs(session)
                    self._verify_stage_outputs(session)
                _session_hash_executable(Path(launcher), launcher_sha256)
                self._check_root_bindings(_load_lease(self.lease.build_id), session)
                process = subprocess.Popen(sandboxed, cwd=self.workspace, env=effective_env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, close_fds=True, pass_fds=(),
                                           start_new_session=True)
                self._active_process = process
                session["process_session_id"] = process.pid
                lease_record["session_v3"] = session
                self._store(lease_record)
                if type(process).__module__ == "subprocess":
                    try:
                        observed_session_id = os.getsid(process.pid)
                    except OSError as sid_error:
                        raise RetentionError(
                            f"cannot verify the new BuildLease process session: {sid_error}") from sid_error
                    if observed_session_id != process.pid:
                        process_session_identity_mismatch = True
                        raise RetentionError(
                            "BuildLease command did not enter its dedicated process session")
                if process.stdout is None:
                    raise RetentionError("session command stdout pipe was not created")
                output_fd = process.stdout.fileno()
                os.set_blocking(output_fd, False)

                def copy_output() -> None:
                    nonlocal step_log_bytes
                    try:
                        while not output_stop_event.is_set():
                            readable, _writable, _exceptional = select.select([output_fd], [], [], 0.1)
                            if not readable:
                                continue
                            try:
                                chunk = os.read(output_fd, 64 * 1024)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                output_eof_event.set()
                                break
                            try:
                                writable = min(len(chunk), remaining_log_bytes - step_log_bytes)
                                if writable > 0:
                                    stream.write(chunk[:writable])
                                    stream.flush()
                                    step_log_bytes += writable
                                if writable < len(chunk):
                                    output_overflow_event.set()
                            except BaseException as error:
                                if not output_error_event.is_set():
                                    output_errors.append(error)
                                    output_error_event.set()
                                # Keep draining so a full pipe cannot stall the failed child.
                    except BaseException as error:
                        if not output_error_event.is_set():
                            output_errors.append(error)
                            output_error_event.set()
                    finally:
                        try:
                            process.stdout.close()
                        except OSError:
                            pass

                output_reader = threading.Thread(target=copy_output, daemon=True,
                                                 name=f"build-session-output-{self.lease.build_id}")
                output_reader.start()
                return_code = self.lease._wait_for_build_process(
                    process, session=self, session_output_error=output_error_event,
                    session_output_overflow=output_overflow_event)
                process_containment = _contain_session_processes(process.pid)
                process.wait(timeout=5)
                unexpected_descendants = set(process_containment.get("members_seen", [])) - {process.pid}
                output_reader.join(timeout=BUILD_SESSION_OUTPUT_DRAIN_SECONDS)
                if output_reader.is_alive():
                    raise RetentionError("session command descendants still held its stdout pipe after SID cleanup")
                if output_errors:
                    raise RetentionError(f"session command output capture failed: {output_errors[0]}")
                if not output_eof_event.is_set():
                    raise RetentionError("session command output pipe ended without a verified EOF")
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException as capture_error:
                    output_errors.append(capture_error)
                    output_error_event.set()
                    raise RetentionError(
                        f"session output log could not be made durable: {capture_error}") from capture_error
            if dual_graph:
                inputs_after_observation = self.lease._validate_session_inputs()
                if inputs_after_observation != inputs_observation:
                    raise RetentionError("source, tool, runtime, or SDK input changed during session operation")
            self._active_process = None
            if unexpected_descendants:
                raise RetentionError("session command spawned descendants; they were stopped and killed")
            if output_overflow_event.is_set():
                raise RetentionError("session command exceeded its strict aggregate log-size limit")
            if step_log_bytes > remaining_log_bytes:
                raise RetentionError("session controller log exceeded its strict aggregate size cap")
            lease_record, session = self._load(allow_running=True)
            session["log_bytes_written"] = log_bytes_before + step_log_bytes
            session["last_process_containment"] = process_containment
            session.pop("process_session_id", None)
            lease_record["session_v3"] = session
            self._store(lease_record)
            if install_stage is not None and install_temporary is not None and install_temporary_identity is not None:
                self._remove_install_temporary(install_stage, install_temporary, install_temporary_identity)
                install_temporary_identity = None
            if return_code != 0:
                self._write_result(index, "FAILURE", command_path, log_path,
                                   return_code=return_code, error="session command returned nonzero",
                                   input_observation=inputs_observation,
                                   input_observation_after=inputs_after_observation,
                                   graph_provenance=graph_provenance,
                                   graph_directory_identity=graph_directory_identity_before)
                self._fail("session command returned nonzero", return_code,
                           command=command, log_path=log_path, step_index=index)
                raise RetentionError(f"v3 session step {index} failed with exit {return_code}")
            if dual_graph:
                selected_graph = _session_dual_graph_by_id(self.plan, str(step["graph_id"]))
                if step["kind"] == "configure":
                    graph_directory_identity_after = _session_graph_directory_identity(
                        self.workspace, selected_graph, must_exist=True)
                    if graph_directory_identity_after is None:
                        raise RetentionError("successful graph configure did not create its declared directory")
                    existing = session.setdefault("graph_directory_identities", {}).get(selected_graph["graph_id"])
                    if existing is not None:
                        raise RetentionError("graph directory was configured more than once")
                    session["graph_directory_identities"][selected_graph["graph_id"]] = graph_directory_identity_after
                    graph_directory_identity_before = graph_directory_identity_after
                elif step["kind"] == "build":
                    graph_output_rows = self._capture_graph_outputs(step, session)
                elif step["kind"] == "install":
                    session["stage_install_tree_identity"] = self._stage_install_snapshot(initial=True)
                elif step["kind"] == "test":
                    executable_identity = self._validate_test_executable(
                        step, session, after_execution=True)
                    graph_output_rows = [{key: value for key, value in executable_identity.items()
                                          if key != "executable_path"}]
                self._verify_recorded_graph_outputs(session)
                stage_tree_identity = self._verify_stage_outputs(
                    session, require_complete=(step["kind"] == "test"))
                session.setdefault("graph_step_output_rows", {})[str(index)] = graph_output_rows
                session.setdefault("stage_step_tree_identities", {})[str(index)] = stage_tree_identity
                lease_record["session_v3"] = session
                self._store(lease_record)
            elif step["kind"] == "build":
                lease_record, session = self._load(allow_running=True)
                for planned_test in self.plan["steps"]:
                    if planned_test["kind"] == "test":
                        self._validate_test_executable(planned_test, session)
                lease_record["session_v3"] = session
                self._store(lease_record)
            if step["kind"] == "test" and not dual_graph:
                self._validate_test_executable(step, session, after_execution=True)
            self._validate_source()
            peak = self._check_limits("step-boundary", extra_bytes=32 * 1024)
            if dual_graph:
                self._write_result(index, "SUCCESS", command_path, log_path,
                                   return_code=0, source_observation=source_observation,
                                   peak_allocated_bytes=peak,
                                   input_observation=inputs_observation,
                                   input_observation_after=inputs_after_observation,
                                   graph_provenance=graph_provenance,
                                   graph_directory_identity=graph_directory_identity_before,
                                   graph_output_identities=graph_output_rows,
                                   stage_tree_identity=stage_tree_identity,
                                   native_loader_provenance=native_loader_provenance)
            else:
                self._write_result(index, "SUCCESS", command_path, log_path,
                                   return_code=0, source_observation=source_observation,
                                   peak_allocated_bytes=peak)
            lease_record, session = self._load(allow_running=True)
            result_hash = _hash_path(result_path)
            command_hash = _hash_path(command_path)
            session["state"] = "ACTIVE"
            session.pop("inflight_step_index", None)
            session["next_step_index"] = index + 1
            session["step_receipts"] = list(session.get("step_receipts", [])) + [
                {"step_index": index, "command_receipt_sha256": command_hash,
                 "result_receipt_sha256": result_hash}]
            lease_record["session_v3"] = session
            self._store(lease_record)
            return {"status": "STEP_SUCCEEDED_SESSION_ACTIVE", "step_index": index,
                    "kind": step["kind"], "peak_allocated_bytes": peak,
                    "lease_state": "ACTIVE"}
        except BaseException as error:
            if process is not None:
                try:
                    if process_containment is None:
                        if process_session_identity_mismatch:
                            raise RetentionError(
                                "cannot contain a BuildLease command outside its dedicated process session")
                        if type(process).__module__ == "subprocess":
                            try:
                                observed_session_id = os.getsid(process.pid)
                            except OSError as sid_error:
                                # ESRCH can mean the short-lived leader exited
                                # after its launch-bound SID was established.
                                # Keep its PID as the session identity so the
                                # containment routine can inspect the unreaped
                                # leader and enumerate any remaining members.
                                if sid_error.errno != errno.ESRCH:
                                    raise RetentionError(
                                        f"cannot reverify the BuildLease process session: {sid_error}") from sid_error
                            else:
                                if observed_session_id != process.pid:
                                    raise RetentionError(
                                        "cannot contain a BuildLease command outside its dedicated process session")
                        process_containment = _contain_session_processes(process.pid)
                    process.wait(timeout=5)
                except BaseException as termination_error:
                    process_containment_uncertain = True
                    error = RetentionError(f"{error}; process SID containment failed: {termination_error}")
                    try:
                        self._mark_process_containment_uncertain(error, step_index=index,
                                                                process_session_id=process.pid)
                    except BaseException:
                        pass
                    self._active_process = process
                    raise error
            if output_reader is not None and output_reader.ident is not None:
                output_stop_event.set()
                output_reader.join(timeout=5.0)
                if output_reader.is_alive():
                    reader_live_uncertain = True
                    error = RetentionError(f"{error}; session output reader did not stop after SID containment")
                    try:
                        self._mark_output_capture_uncertain(error, step_index=index)
                    except BaseException:
                        # The prelaunch RUNNING record remains a durable no-resume barrier.
                        pass
                    self._active_process = None
                    raise error
            capture_uncertain = (output_error_event.is_set() or bool(output_errors)
                                 or (not output_eof_event.is_set()
                                     and not output_overflow_event.is_set()))
            if process is not None and capture_uncertain:
                # A stopped reader is not proof that it drained the pipe. Keep
                # the lease RUNNING whenever a launched command lacks verified
                # EOF or its log could not be durably captured, even if the
                # controller reader thread has exited. An aggregate overflow is
                # a known bounded failure and remains eligible for terminal capture.
                reader_live_uncertain = True
                reason = ("session output log durability is uncertain"
                          if output_error_event.is_set() or output_errors
                          else "session output capture ended without verified EOF")
                error = RetentionError(f"{error}; {reason}")
                try:
                    self._mark_output_capture_uncertain(error, step_index=index)
                except BaseException:
                    # The prelaunch RUNNING record remains a durable no-resume barrier.
                    pass
                self._active_process = None
                raise error
            self._active_process = None
            if not process_containment_uncertain and not reader_live_uncertain and process_containment is not None:
                try:
                    current = _load_lease(self.lease.build_id)
                    current_session = current.get("session_v3")
                    if isinstance(current_session, dict) and current_session.get("state") == "RUNNING":
                        current_session["last_process_containment"] = process_containment
                        current_session["log_bytes_written"] = min(
                            BUILD_SESSION_TOTAL_LOG_LIMIT_BYTES,
                            int(current_session.get("log_bytes_written", 0)) + step_log_bytes)
                        current_session.pop("process_session_id", None)
                        current["session_v3"] = current_session
                        self._store(current)
                except BaseException:
                    pass
            if (install_stage is not None and install_temporary is not None
                    and install_temporary_identity is not None):
                try:
                    self._remove_install_temporary(install_stage, install_temporary,
                                                   install_temporary_identity)
                except BaseException as cleanup_error:
                    error = RetentionError(f"{error}; install temporary cleanup failed: {cleanup_error}")
            try:
                current = _load_lease(self.lease.build_id)
                if current.get("state") == "ACTIVE" and current.get("session_v3", {}).get("state") != "FAILED":
                    self._fail(repr(error), -1, command=command or ["session-step"],
                               log_path=(log_path if log_path.is_file()
                                         and (output_reader is None or not output_reader.is_alive()) else None),
                               step_index=index)
            except BaseException:
                pass
            raise

    def _write_result(self, index: int, status: str, command_path: Path, log_path: Path | None,
                      *, return_code: int, error: str | None = None,
                      source_observation: Mapping[str, Any] | None = None,
                      peak_allocated_bytes: int | None = None,
                      input_observation: Mapping[str, Any] | None = None,
                      input_observation_after: Mapping[str, Any] | None = None,
                      graph_provenance: Mapping[str, Any] | None = None,
                      graph_directory_identity: Mapping[str, Any] | None = None,
                      graph_directory_identities: Mapping[str, Any] | None = None,
                      graph_output_identities: list[Mapping[str, Any]] | None = None,
                      stage_operation_identity: Mapping[str, Any] | None = None,
                      stage_tree_identity: Mapping[str, Any] | None = None,
                      native_loader_provenance: Mapping[str, Any] | None = None) -> Path:
        result_path = self.session_dir / "steps" / f"step-{index:03d}.result.json"
        if result_path.exists():
            raise RetentionError("session step result receipt already exists")
        log_hash = _hash_path(log_path) if log_path is not None and log_path.is_file() else None
        payload = {"schema_version": BUILD_SESSION_SCHEMA_VERSION, "build_id": self.lease.build_id,
                   "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                   "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                   "plan_sha256": self.plan_sha256, "step_index": index,
                   "kind": self.plan["steps"][index]["kind"], "status": status,
                   "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                   "return_code": return_code, "command_receipt_sha256": _hash_path(command_path),
                   "log_path": (log_path.relative_to(self.root).as_posix() if log_hash else None),
                   "log_sha256": log_hash, "source_observation": source_observation,
                   "peak_allocated_bytes": peak_allocated_bytes, "completed_at_unix": time.time()}
        if input_observation is not None:
            payload.update({
                "input_tool_identities": input_observation.get("tools"),
                "input_receipts": input_observation.get("input_receipts"),
                "graph_tool_identities": input_observation.get("graph_tools"),
                "runtime_input_identities": input_observation.get("runtime_inputs"),
            })
        if input_observation_after is not None:
            payload["input_observation_after"] = dict(input_observation_after)
        if graph_provenance is not None:
            payload["graph_provenance"] = dict(graph_provenance)
        if graph_directory_identity is not None:
            payload["graph_directory_identity"] = dict(graph_directory_identity)
        if graph_directory_identities is not None:
            payload["graph_directory_identities"] = {
                key: dict(value) for key, value in graph_directory_identities.items()}
        if graph_output_identities is not None:
            payload["graph_output_identities"] = [dict(row) for row in graph_output_identities]
        if stage_operation_identity is not None:
            payload["stage_operation_identity"] = dict(stage_operation_identity)
        if stage_tree_identity is not None:
            payload["stage_tree_identity"] = dict(stage_tree_identity)
        if native_loader_provenance is not None:
            payload["native_loader_provenance"] = dict(native_loader_provenance)
        if error is not None:
            bounded, digest = _bounded_failure_error(error)
            payload["error"] = bounded
            if digest:
                payload["error_sha256"] = digest
        predicted_peak = self._check_limits(
            "before-step-result-write", extra_bytes=len(_canonical_json(payload)) + 8192)
        if peak_allocated_bytes is not None:
            payload["peak_allocated_bytes"] = max(peak_allocated_bytes, predicted_peak)
        _write_json(result_path, payload, replace=False, sealed=True)
        return result_path

    def verify_source(self) -> dict[str, Any]:
        return self._with_operation("verify_source", self._verify_source_locked)

    def _verify_source_locked(self) -> dict[str, Any]:
        _require_buildlease_vm_runtime(self.plan)
        lease_record, session = self._load(allow_running=True)
        if session.get("state") == "RUNNING":
            if session.get("output_capture_uncertain") is True:
                raise RetentionError("v3 session output capture is uncertain; source verification is blocked")
            self._fail("source verification cannot recover an uncertain step", -1)
            raise RetentionError("v3 session has an uncertain command result")
        if session.get("state") != "ACTIVE":
            raise RetentionError(f"v3 source verification cannot run from state {session.get('state')}")
        index = session.get("next_step_index")
        if index != len(self.plan["steps"]) - 1 or self.plan["steps"][index]["kind"] != "source_verification":
            raise RetentionError("source verification is allowed only at the final planned step")
        command_path = self.session_dir / "steps" / f"step-{index:03d}.command.json"
        result_path = self.session_dir / "steps" / f"step-{index:03d}.result.json"
        try:
            if command_path.exists() or result_path.exists():
                raise RetentionError("source verification receipt already exists")
            self._check_limits("before-source-verification")
            source_observation = self._validate_source()
            input_observation = self.lease._validate_session_inputs()
            graph_directory_identities: dict[str, Any] = {}
            graph_output_identities: list[dict[str, Any]] = []
            stage_operation_identity: dict[str, Any] | None = None
            stage_tree_identity: dict[str, Any] | None = None
            if _session_dual_graph_enabled(self.plan):
                if input_observation != source_observation:
                    raise RetentionError("final source and input observations are inconsistent")
                graph_directory_identities = dict(session.get("graph_directory_identities", {}))
                graph_output_identities = self._verify_graph_outputs(session)
                stage_tree_identity = self._verify_stage_outputs(session, require_complete=True)
                stage_operation_identity = {
                    "windows_pe_stage_identity": session.get("windows_pe_stage_identity"),
                    "native_runtime_stage_identity": session.get("native_runtime_stage_identity"),
                    "stage_install_tree_identity": session.get("stage_install_tree_identity"),
                }
            command_payload = {"schema_version": BUILD_SESSION_SCHEMA_VERSION,
                              "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                              "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                              "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                              "build_id": self.lease.build_id, "plan_sha256": self.plan_sha256,
                              "step_index": index, "kind": "source_verification", "command": None,
                              "cwd": str(self.workspace),
                              "read_only": True, "source_observation": source_observation,
                              "started_at_unix": time.time()}
            if _session_dual_graph_enabled(self.plan):
                command_payload.update({
                    "input_tool_identities": input_observation["tools"],
                    "input_receipts": input_observation["input_receipts"],
                    "graph_tool_identities": input_observation["graph_tools"],
                    "runtime_input_identities": input_observation["runtime_inputs"],
                    "graph_directory_identities": graph_directory_identities,
                    "graph_output_identities": graph_output_identities,
                    "stage_operation_identity": stage_operation_identity,
                    "stage_tree_identity": stage_tree_identity,
                })
            _write_json(command_path, command_payload, replace=False, sealed=True)
            if _session_dual_graph_enabled(self.plan):
                self._write_result(
                    index, "SUCCESS", command_path, None, return_code=0,
                    source_observation=source_observation,
                    peak_allocated_bytes=self._check_limits("source-verification-boundary"),
                    input_observation=input_observation,
                    graph_directory_identities=graph_directory_identities,
                    graph_output_identities=graph_output_identities,
                    stage_operation_identity=stage_operation_identity,
                    stage_tree_identity=stage_tree_identity)
            else:
                self._write_result(index, "SUCCESS", command_path, None, return_code=0,
                                   source_observation=source_observation,
                                   peak_allocated_bytes=self._check_limits("source-verification-boundary"))
            lease_record, session = self._load()
            session["state"] = "ACTIVE"
            session["next_step_index"] = index + 1
            session["final_source_verification"] = source_observation
            session["step_receipts"] = list(session.get("step_receipts", [])) + [{
                "step_index": index, "command_receipt_sha256": _hash_path(command_path),
                "result_receipt_sha256": _hash_path(result_path)}]
            lease_record["session_v3"] = session
            self._store(lease_record)
            return {"status": "SOURCE_VERIFIED_SESSION_ACTIVE", "source_identity": source_observation,
                    "lease_state": "ACTIVE"}
        except BaseException as error:
            self._fail(repr(error), -1, command=["source-verification"], step_index=index)
            raise

    def _fail(self, reason: str, return_code: int, *, command: list[str] | None = None,
              log_path: Path | None = None, step_index: int | None = None) -> None:
        """Durably terminalize first, then preserve bounded failure evidence and retire."""
        try:
            lease_record = _load_lease(self.lease.build_id)
        except (OSError, RetentionError):
            raise RetentionError("cannot terminalize a missing v3 session lease")
        session = lease_record.get("session_v3")
        if lease_record.get("state") != "ACTIVE" or not isinstance(session, dict):
            raise RetentionError("cannot fail a v3 session that is no longer ACTIVE")
        if session.get("state") == "FAILED":
            return
        if session.get("state") in {"QUIESCING", "READY_TO_FINALIZE", "ACCEPTED"}:
            session["acceptance_state"] = "REJECTED_INCOMPLETE_QUIESCENCE"
            session["write_quiescence_state"] = "UNKNOWN_OR_FAILED"
        session["state"] = "FAILED"
        session["failed_at_unix"] = time.time()
        session["failed_step_index"] = step_index
        session["failure_reason_sha256"] = hashlib.sha256(reason.encode("utf-8", "replace")).hexdigest()
        session["failure_reason"] = reason[:MAX_FAILURE_ERROR_BYTES]
        session["storage_watch_events"] = [dict(event) for event in self.lease.storage_watch_events]
        lease_record["session_v3"] = session
        lease_record["state"] = "FAILED"
        lease_record["failed_at_unix"] = session["failed_at_unix"]
        lease_record["failed_command"] = command or ["build-session", "failed"]
        lease_record["return_code"] = return_code
        self._store(lease_record)
        for path in (self.session_dir / BUILD_SESSION_TERMINAL,
                     self.session_dir / "session-terminal.pending.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        selected_command = command or ["build-session", "failed"]
        try:
            self.lease.fail(command=selected_command, return_code=return_code,
                            log_path=log_path, error=reason)
        except BaseException:
            # The signed FAILED session lease remains a durable no-resume barrier even if capture fails.
            raise

    def _mark_output_capture_uncertain(self, reason: BaseException, *, step_index: int) -> None:
        """Keep the active reservation blocking when its controller log writer cannot be stopped."""
        lease_record = _load_lease(self.lease.build_id)
        session = lease_record.get("session_v3")
        if (lease_record.get("state") != "ACTIVE" or not isinstance(session, dict)
                or session.get("state") != "RUNNING"):
            raise RetentionError("cannot block an output-uncertain session outside its RUNNING lease")
        message = str(reason)
        session.update({"state": "RUNNING", "output_capture_uncertain": True,
                        "output_capture_failure_reason": message[:MAX_FAILURE_ERROR_BYTES],
                        "output_capture_failure_reason_sha256": hashlib.sha256(
                            message.encode("utf-8", "replace")).hexdigest(),
                        "output_capture_failed_step_index": step_index,
                        "output_capture_marked_at_unix": time.time()})
        lease_record["session_v3"] = session
        self._store(lease_record)

    def _mark_process_containment_uncertain(self, reason: BaseException, *, step_index: int,
                                            process_session_id: int) -> None:
        """Keep the mount and reservation blocking if SID cleanup cannot be proven."""
        lease_record = _load_lease(self.lease.build_id)
        session = lease_record.get("session_v3")
        if (lease_record.get("state") != "ACTIVE" or not isinstance(session, dict)
                or session.get("state") != "RUNNING"):
            raise RetentionError("cannot block process-uncertain containment outside a RUNNING lease")
        message = str(reason)
        session.update({"state": "RUNNING", "process_containment_uncertain": True,
                        "process_session_id": process_session_id,
                        "process_containment_failure_reason": message[:MAX_FAILURE_ERROR_BYTES],
                        "process_containment_failure_reason_sha256": hashlib.sha256(
                            message.encode("utf-8", "replace")).hexdigest(),
                        "process_containment_failed_step_index": step_index,
                        "process_containment_marked_at_unix": time.time()})
        lease_record["session_v3"] = session
        self._store(lease_record)

    def finalize(self) -> dict[str, Any]:
        return self._with_operation("finalize", self._finalize_locked)

    def _finalize_locked(self) -> dict[str, Any]:
        _require_buildlease_vm_runtime(self.plan)
        lease_record, session = self._load(allow_running=True)
        if session.get("state") != "ACTIVE":
            raise RetentionError(f"v3 session cannot finalize from state {session.get('state')}")
        if session.get("next_step_index") != len(self.plan["steps"]):
            raise RetentionError("v3 session cannot finalize before every planned step succeeds")
        if not session.get("final_source_verification"):
            raise RetentionError("v3 session lacks its final read-only source verification")
        terminal_path = self.session_dir / BUILD_SESSION_TERMINAL
        pending_path = self.session_dir / "session-terminal.pending.json"
        try:
            if terminal_path.exists() or pending_path.exists():
                raise RetentionError("v3 session terminal receipt already exists")
            final_source = self._validate_source()
            if final_source != session["final_source_verification"]:
                raise RetentionError("source changed after the final verification step")
            self._check_root_bindings(lease_record, session)
            peak = self._check_limits("before-finalize")
            self.lease._stop_heartbeat()
            session["state"] = "QUIESCING"
            session["acceptance_state"] = "UNACCEPTED_UNTIL_NORMAL_UNMOUNT_AND_DETACH"
            session["write_quiescence_state"] = "WRITABLE_IMAGE_DETACH_IN_PROGRESS"
            session["quiescence_started_at_unix"] = time.time()
            lease_record["session_v3"] = session
            self._store(lease_record)

            # This is the only writable-phase acceptance boundary. No output hash,
            # artifact copy, or SUCCESS receipt is produced before both operations.
            detached_workspace = _detach_session_workspace(
                self.root, session["workspace_mount_identity"])
            session["detached_workspace_identity"] = detached_workspace
            session["detached_image_identity"] = detached_workspace["detached_image_identity"]
            session["write_quiescence_state"] = "WRITABLE_IMAGE_NORMALLY_UNMOUNTED_AND_DETACHED"
            session["writable_detached_at_unix"] = detached_workspace.get("detached_at_unix")
            lease_record["session_v3"] = session
            self._store(lease_record)

            readonly_mount = _attach_session_workspace_readonly(self.root, detached_workspace)
            session["readonly_mount_identity"] = readonly_mount
            lease_record["session_v3"] = session
            self._store(lease_record)

            final_source = self._validate_source()
            if final_source != session["final_source_verification"]:
                raise RetentionError("source changed between writable detach and read-only evidence verification")
            output_relative = _session_relative_path(self.plan["output_root"], "output_root", allow_dot=True)
            output_root = _session_safe_path(self.workspace, output_relative, must_exist=True, directory=True)
            output_tree, output_count = _session_tree_digest(output_root, exclude_controller_state=True)
            stage_relative = _session_relative_path(self.plan["stage_root"], "stage_root")
            stage = _session_safe_path(self.workspace, stage_relative, must_exist=True, directory=True)
            graph_directory_identities: dict[str, Any] = {}
            graph_output_identities: list[dict[str, Any]] = []
            stage_tree_identity: dict[str, Any] | None = None
            stage_operation_identity: dict[str, Any] | None = None
            if _session_dual_graph_enabled(self.plan):
                graph_directory_identities = dict(session.get("graph_directory_identities", {}))
                graph_output_identities = self._verify_graph_outputs(session)
                stage_tree_identity = self._verify_stage_outputs(session, require_complete=True)
                stage_operation_identity = {
                    "windows_pe_stage_identity": session.get("windows_pe_stage_identity"),
                    "native_runtime_stage_identity": session.get("native_runtime_stage_identity"),
                    "stage_install_tree_identity": session.get("stage_install_tree_identity"),
                }
            stage_fd = _open_dir(stage)
            output_fd = _open_dir(output_root)
            try:
                readonly_stage_identity = _fs_identity(stage_fd)
                readonly_output_identity = _fs_identity(output_fd)
            finally:
                os.close(stage_fd)
                os.close(output_fd)
            _verify_session_workspace_readonly(self.root, readonly_mount)
            ordered_results = [row["result_receipt_sha256"] for row in session["step_receipts"]]
            sandbox_profile_sha = session.get("sandbox_profile_sha256")
            launcher_path = session.get("sandbox_launcher_path")
            launcher_hash = session.get("sandbox_launcher_sha256")
            if (not isinstance(sandbox_profile_sha, str) or not launcher_path or not launcher_hash
                    or len(ordered_results) != len(self.plan["steps"])):
                raise RetentionError("v3 session lacks sandbox or ordered-step completion evidence")
            session["storage_watch_events"] = [dict(event) for event in self.lease.storage_watch_events]
            lease_record["session_v3"] = session
            self._store(lease_record)
            payload = {"schema_version": BUILD_SESSION_SCHEMA_VERSION, "state": "PENDING_SUCCESS",
                       "executor_mode": BUILD_SESSION_EXECUTOR_MODE,
                       "executor_version": BUILD_SESSION_SCHEMA_VERSION,
                       "acceptance_state": "UNACCEPTED_PENDING_NORMAL_READONLY_DETACH_AND_MANIFEST",
                       "write_quiescence_state": session["write_quiescence_state"],
                       "build_id": self.lease.build_id, "plan_sha256": self.plan_sha256,
                       "admission_identity": self.plan["admission_identity"],
                       "source_identity": self.plan["source_identity"],
                       "final_source_verification": final_source,
                       "build_root_identity": session["build_root_identity"],
                       "workspace_mount_identity": session["workspace_mount_identity"],
                       "detached_workspace_identity": detached_workspace,
                       "detached_image_identity": session["detached_image_identity"],
                       "readonly_mount_identity": readonly_mount,
                       "readonly_stage_root_identity": readonly_stage_identity,
                       "readonly_output_root_identity": readonly_output_identity,
                       "stage_root_identity": readonly_stage_identity,
                       "output_root_identity": readonly_output_identity,
                       "output_tree_sha256": output_tree, "output_file_count": output_count,
                       "step_count": len(self.plan["steps"]),
                       "step_receipts": session["step_receipts"],
                       "graph_directory_identities": graph_directory_identities,
                       "graph_output_identities": graph_output_identities,
                       "stage_operation_identity": stage_operation_identity,
                       "stage_tree_identity": stage_tree_identity,
                       "ordered_result_receipt_sha256": ordered_results,
                       "storage_watch_events": [dict(event) for event in self.lease.storage_watch_events],
                       "peak_allocated_bytes": peak,
                       "reservation_bytes": self.lease.expected_bytes,
                       "sandbox_launcher_path": launcher_path,
                       "sandbox_launcher_sha256": launcher_hash,
                       "sandbox_profile_sha256": sandbox_profile_sha,
                       "finalized_at_unix": time.time()}
            root_info = os.statvfs(self.root)
            terminal_size_estimate = len(json.dumps(_seal(payload), indent=2, sort_keys=True).encode())
            extra = ((terminal_size_estimate + root_info.f_frsize - 1) // root_info.f_frsize + 2) * root_info.f_frsize
            peak = self._check_limits("before-terminal-receipt", extra_bytes=extra)
            _write_json(pending_path, payload, replace=False, sealed=True)
            self._check_controller_evidence_budget()
            measure_root = lambda: _session_measure_tree(
                self.root, workspace_mount_identity=readonly_mount)["allocated_bytes"]
            observed_peak = measure_root()
            if observed_peak > self.lease.expected_bytes:
                raise RetentionError("session terminal evidence would exceed the signed reservation")
            payload["peak_allocated_bytes"] = max(peak, observed_peak)
            _write_json(pending_path, payload, replace=True, sealed=True)
            self._check_controller_evidence_budget()
            if measure_root() > self.lease.expected_bytes:
                raise RetentionError("session terminal receipt exceeds the signed reservation")
            session["state"] = "READY_TO_FINALIZE"
            session["peak_allocated_bytes"] = payload["peak_allocated_bytes"]
            session["output_tree_sha256"] = output_tree
            session["output_file_count"] = output_count
            session["readonly_stage_root_identity"] = readonly_stage_identity
            session["readonly_output_root_identity"] = readonly_output_identity
            session["acceptance_state"] = "UNACCEPTED_PENDING_NORMAL_READONLY_DETACH_AND_MANIFEST"
            lease_record["session_v3"] = session
            self._store(lease_record)
            session_fd = _open_dir(self.session_dir)
            try:
                _rename_exclusive(session_fd, pending_path.name, terminal_path.name)
            finally:
                os.close(session_fd)
            self._check_controller_evidence_budget()
            if measure_root() > self.lease.expected_bytes:
                raise RetentionError("session finalization crossed its signed reservation")
            lease_record = _load_lease(self.lease.build_id)
            session = lease_record["session_v3"]
            session["terminal_sha256"] = _hash_path(terminal_path)
            lease_record["session_v3"] = session
            self._store(lease_record)
            result = self.lease.complete()
            return result
        except BaseException as error:
            try:
                current = _load_lease(self.lease.build_id)
                if current.get("state") == "ACTIVE" and current.get("session_v3", {}).get("state") != "FAILED":
                    self._fail(repr(error), -1, command=["session-finalize"], step_index=None)
            except BaseException:
                pass
            raise


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
