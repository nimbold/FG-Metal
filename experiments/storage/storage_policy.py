#!/usr/bin/env python3
"""Shared storage gate and disposable Wine-prefix retention controls.

Runners must call ``begin_experiment`` before creating a prefix, build, or large
output.  A ``PrefixLease`` clones the one certified template and removes the
clone in ``finally`` after shutting down wineserver.  A cleanup failure is
explicitly marked and counts against the two-prefix retention limit.
"""
from __future__ import annotations

import ast
import json
import math
import os
import re
import stat
import shutil
import subprocess
import time
import uuid
import difflib
import gzip
import hashlib
import hmac
import secrets
import signal
import fcntl
import sys
import ctypes
import errno
import argparse
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, Sequence

GIB = 1024 ** 3
MIB = 1024 ** 2
FREE_HARD_STOP = 25 * GIB
FREE_WARNING = 50 * GIB
FREE_MINIMUM = 50 * GIB
FREE_PREFERRED = 60 * GIB
PROJECT_WARNING = 52 * GIB
PROJECT_BUDGET = 55 * GIB
GATE0_PROJECT_BUDGET = 55 * GIB
# Volume free-space can drift between full-tree scans; scoped allocation remains exact.
GATE0_FREE_DRIFT_TOLERANCE_BYTES = 2 * GIB
GATE0_MAX_AGE_SECONDS = 60 * 60
ACTIVE_PREFIX_LEASE_MARKER_MAX_BYTES = 16 * 1024
ACTIVE_PREFIX_LEASE_MARKER_MAX_ALLOCATED_BYTES = 64 * 1024
STORAGE_POLICY_SMOKE_ID_PREFIX = "step11d3-storage-policy-smoke-"
HISTORICAL_TERMINAL_RUN_ID = "step11d3b-baseline-01-20261014-130823-44892b9b"
HISTORICAL_TERMINAL_OUTPUT_BYTES = 725_688_320
HISTORICAL_TERMINAL_OUTPUT_LOGICAL_BYTES = 720_231_108
HISTORICAL_TERMINAL_ROOT_IDENTITY = (16_777_229, 98_341_479)
HISTORICAL_TERMINAL_POST_OUTPUT_BYTES = 598_573_056
HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES = 314_572_800
HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES = 160_968
HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256 = "9012ccacc5a64dc9a8d0f5198059f791a10d514a7857c5dda3fdd5e6c337e00d"
HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256 = "8b591c60099965513a7485890928f96af242a6ca3422a28ed20eb3440c140bcf"
HISTORICAL_TERMINAL_LSOF_PATH = "/usr/sbin/lsof"
HISTORICAL_TERMINAL_TRUSTED_FILES = {
    "app-trace.jsonl": "d57f5ec94bb2bdab51bebd87c97e3a7bc0650539dd5756897ae39a5aec87774c",
    "execution-result.json": "dca9ab61f70d36a2790243bd2a7b058c7dbda7e09f365f1f89d1f208aab3b4f6",
    "host-traces/host-trace.58921.jsonl": "f839fab2af872f597747a3d1d088e785b3ea4bf8237961e5377c6c49276866d2",
    "host-traces/host-trace.58942.jsonl": "03c760a06e261364788571da15264887e5476188829d601bb06b98cd15e7ce5f",
    "mvk/libMoltenVK.1.dylib": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
    "mvk/libMoltenVK.dylib": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
    "prefix-cleanup.json": "0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308",
    "prefix-registry-diff.txt.gz": "d9ba6648609b3e00d2a79c0be45ef4cf20fbd796fa4cf5c8f02d700344fc1bed",
    "prefix-state.json": "6fcc00dd8e06f4c9ad4777a62631697cdfc0fbe075cd21bdcfaefa5d5c3a2c53",
    "process-context.json": "154092c5ae04fcd942d1869014c1388e07574fb931f6c6aa5105c6bc78ede2a9",
    "process-runner.log": "140385ceda92b0660cc38e1d0add459ca6d7a26e51cad80336e26ec5c05e6b09",
    "run-manifest.json": "e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365",
    "run-result.json": "a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589",
    "runtime.log": "78c918cc0d77bf029349f7a975c9e13f200d56ce2ba94af62a5bb9fa7685f7c3",
    "trace-index.json": "1e03ffab47bf76a1d3a440b639b232cf15a9238955e4c64cfd23de2ba3694582",
    "trace-loader/libMoltenVK.dylib": "04bbfc075f6ab999d0538f4a5371f3abd65fb0f0e307be0dea56e47aaefd5157",
}
HISTORICAL_TERMINAL_SOURCE_EVIDENCE = {
    "controller.json": (
        "/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/"
        "z9-replay-1-20261014-130814-7dafc87e/controller.json",
        "1a7590578476931ceb9e816f7a1a15c507d63d6ec9abd36bcdedfec3771e638b",
    ),
    "timeline.jsonl": (
        "/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/"
        "z9-replay-1-20261014-130814-7dafc87e/timeline.jsonl",
        "680fcb513c01e2e56e442c100b33ed0df16a386c68e2f5f16669bcbe56eb63f8",
    ),
    "observer/observer-result.json": (
        "/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/"
        "z9-replay-1-20261014-130814-7dafc87e-observer/observer-result.json",
        "17fb45ab4077ad13c642d0a8cb0032441f4a4797898e7245f962ae5f9f00b0aa",
    ),
    "observer/observer-controller.json": (
        "/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/"
        "z9-replay-1-20261014-130814-7dafc87e-observer/observer-controller.json",
        "f82e9e985108281595f56ec81f833321611ae9dedcdaa1278026bd0727d97da2",
    ),
    "observer/observer-samples.jsonl": (
        "/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/"
        "z9-replay-1-20261014-130814-7dafc87e-observer/observer-samples.jsonl",
        "d4787506c269a74ee0d7f1099ab4234e83737df63feb53bef407121c24930603",
    ),
}
HISTORICAL_TERMINAL_BLOCKERS = [
    "signed experiment output exceeds its persistent reservation: " + HISTORICAL_TERMINAL_RUN_ID,
    "unrelated non-lease storage changed since Gate 0 certification",
    "signed experiment output exceeds its persistent reservation: " + HISTORICAL_TERMINAL_RUN_ID,
]
HISTORICAL_TERMINAL_REPOSITORY = Path(__file__).resolve().parents[2]
NORMAL_PERSISTENT_TARGET = 250 * MIB
NORMAL_TEMPORARY_TARGET = 3 * GIB
MIN_ADMISSION_UNCERTAINTY_MARGIN = 256 * MIB
ADMISSION_UNCERTAINTY_BASIS_POINTS = 1000
MAX_PRESERVED_PREFIXES = 2
SUPPORTED_BUILD_TYPES = {"release", "debug", "debugoptimized", "minsize", "targeted-symbols", "unstripped", "full-debug"}
FULL_DEBUG_BUILD_TYPES = {"debug", "debugoptimized", "unstripped", "full-debug"}
RUNNER_ADMISSION_CONTRACT = "prefixlease-scoped-managed-process-v16"
ROOT_CLASS_MANDATORY = "MANDATORY"
ROOT_CLASS_OPTIONAL_EPHEMERAL = "OPTIONAL_EPHEMERAL"
SIGNED_OUTPUT_MANIFEST_REQUIRED_FIELDS = frozenset({
    "experiment_id", "purpose", "candidate_hashes", "created_at_unix",
    "expected_duration_seconds", "expected_prefix_requirement", "expected_prefix_bytes",
    "expected_build_requirement", "expected_build_bytes", "estimated_max_new_disk_bytes",
    "estimated_uncertainty_margin_bytes", "estimated_projected_new_disk_bytes",
    "project_estimated_peak_bytes", "projected_free_bytes", "projected_scoped_project_bytes",
    "storage_admission_warnings", "estimated_persistent_bytes", "project_budget_bytes",
    "project_budget_remaining_before_bytes", "free_before_bytes", "project_usage_before",
    "output_dir", "decision", "blockers", "warnings", "cleanup_policy", "retention_policy",
    "storage_justification", "persistent_growth_over_2g_approved",
    "persistent_growth_over_2g_approval_reason", "internal_storage_setup",
    "storage_policy_smoke", "gate0_attestation_sha256", "authorized_runner_paths",
    "authorized_runner_sha256", "build_source_tree_hash", "build_configuration_key_sha256",
    "build_kind", "build_type", "dependency_state", "preserve_debug_reason",
    "estimated_debug_bytes", "failure_capture_paths", "native_architecture",
    "manifest_path", "manifest_hmac_sha256",
})

REPO = Path(__file__).resolve().parents[2]
WINE_RUNTIME_TREE_MANIFEST = REPO / "experiments/storage/runtime-manifests/step11d_r_active_runtime.json"
CACHE = Path.home() / "Library" / "Caches" / "FGMetal"
ARTIFACT_STORE = CACHE / "artifacts" / "sha256"
PREFIX_ROOT = CACHE / "prefixes"
PREFIX_TEMPLATE = PREFIX_ROOT / "wine-11.17-mvk142-pristine"
ACTIVE_LEASE_ROOT = PREFIX_ROOT / ".active-leases"
PREFIX_RETIRED_MARKERS_ROOT = PREFIX_ROOT / ".retired-leases"
INVENTORY_PATH = REPO / "experiments/storage/storage-inventory.json"
GATE0_ATTESTATION_PATH = REPO / "experiments/storage/storage-gate0-attestation.json"
STORAGE_CLEANUP_RECEIPT_PATH = REPO / "experiments/storage/storage-cleanup-receipt.json"
GATE0_SUPPLEMENTAL_EVIDENCE = (
    "g0c-hardlink-audit.json", "g0c-evidence-consolidation-plan.json",
    "g0c-evidence-consolidation-receipt.json", "g0c-launcher-independent-review.json",
    "g0c-launcher-tests.log", "g0c-checks-tests.log", "g0c-consolidation-tests.log",
)
SMOKE_CLAIM_ROOT = REPO / "experiments/storage/smoke-claims"
PREFIX_RECONCILIATION_ROOT = REPO / "experiments/storage/prefix-lease-reconciliation"
PREFIX_RECOVERY_RECEIPTS_ROOT = REPO / "experiments/storage/prefix-recovery-receipts"
STORAGE_CERTIFICATION_LOCK = CACHE / ".storage-certification.lock"
MANIFEST_SIGNING_KEY_PATH = CACHE / ".storage-manifest-signing-key"

_ACTIVE_LEASE: "PrefixLease | None" = None


def _is_storage_policy_smoke_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(
        rf"{re.escape(STORAGE_POLICY_SMOKE_ID_PREFIX)}[0-9]{{8}}-[0-9a-f]{{16}}", value) is not None


def _storage_smoke_claim_path(experiment_id: str) -> Path:
    if not _is_storage_policy_smoke_id(experiment_id):
        raise ValueError("invalid storage-smoke nonce ID")
    return SMOKE_CLAIM_ROOT / f"{experiment_id}.json"


def _read_storage_smoke_claim(experiment_id: str) -> tuple[dict[str, Any], bytes]:
    path = _storage_smoke_claim_path(experiment_id)
    raw = _read_file_nofollow(path, maximum_bytes=16 * 1024)
    claim = json.loads(raw)
    info = _stat_entry_nofollow(path)
    expected_keys = {
        "schema_version", "experiment_id", "claim_path", "preflight_attestation_path",
        "preflight_attestation_sha256", "preflight_created_at_unix", "claimed_at_unix",
        "claim_reason", "pid", "smoke_claim_hmac_sha256",
    }
    allocated_bytes = getattr(info, "st_blocks", 0) * 512 if info is not None else 0
    if (not isinstance(claim, dict)
            or info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16 * 1024 or allocated_bytes > 64 * 1024
            or set(claim) != expected_keys or raw != _canonical_pretty_json_bytes(claim)
            or not _verify_storage_evidence(claim, "smoke_claim_hmac_sha256")
            or claim.get("schema_version") != 1
            or claim.get("experiment_id") != experiment_id
            or claim.get("claim_path") != str(path)
            or claim.get("preflight_attestation_path") != str(GATE0_ATTESTATION_PATH)
            or not isinstance(claim.get("preflight_attestation_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", claim["preflight_attestation_sha256"])
            or not isinstance(claim.get("preflight_created_at_unix"), (int, float))
            or isinstance(claim.get("preflight_created_at_unix"), bool)
            or not math.isfinite(float(claim["preflight_created_at_unix"]))
            or not isinstance(claim.get("claimed_at_unix"), (int, float))
            or isinstance(claim.get("claimed_at_unix"), bool)
            or not math.isfinite(float(claim["claimed_at_unix"]))
            or not isinstance(claim.get("claim_reason"), str) or not claim["claim_reason"]
            or not isinstance(claim.get("pid"), int) or isinstance(claim.get("pid"), bool)
            or claim["pid"] <= 0):
        raise ValueError("storage-smoke nonce claim is missing, malformed, or unauthenticated")
    return claim, raw


def _write_storage_smoke_claim(experiment_id: str, preflight_bytes: bytes,
                              *, reason: str) -> tuple[dict[str, Any], bytes]:
    """Create an exclusive signed nonce-consumption record for a verified preflight."""
    path = _storage_smoke_claim_path(experiment_id)
    preflight = json.loads(preflight_bytes)
    if (not _verify_gate0_signature(preflight)
            or preflight.get("status") != "PREFLIGHT_PASS"
            or preflight.get("mode") != "preflight"
            or preflight.get("prefixlease_smoke_status") != "PENDING"
            or preflight.get("permitted_smoke_experiment_id") != experiment_id):
        raise ValueError("storage-smoke nonce claim requires its signed pending preflight")
    now = time.time()
    parent_fd = _open_directory_nofollow(path.parent, create=True, strict_directory_sync=True)
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        record = _seal_storage_evidence({
            "schema_version": 1,
            "experiment_id": experiment_id,
            "claim_path": str(path),
            "preflight_attestation_path": str(GATE0_ATTESTATION_PATH),
            "preflight_attestation_sha256": hashlib.sha256(preflight_bytes).hexdigest(),
            "preflight_created_at_unix": preflight["created_at_unix"],
            "claimed_at_unix": now,
            "claim_reason": reason,
            "pid": os.getpid(),
        }, "smoke_claim_hmac_sha256")
        raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while consuming storage-smoke nonce")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_fd)
        return record, raw
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _claim_storage_smoke_nonce(experiment_id: str, preflight_bytes: bytes) -> tuple[dict[str, Any], bytes]:
    """Durably consume a smoke ID with an exclusive signed claim before run allocation."""
    path = _storage_smoke_claim_path(experiment_id)
    preflight = json.loads(preflight_bytes)
    preflight_sha256 = hashlib.sha256(preflight_bytes).hexdigest()
    now = time.time()
    age = now - float(preflight.get("created_at_unix", 0))
    if (preflight.get("status") != "PREFLIGHT_PASS" or preflight.get("mode") != "preflight"
            or preflight.get("prefixlease_smoke_status") != "PENDING"
            or preflight.get("permitted_smoke_experiment_id") != experiment_id
            or age < 0 or age > GATE0_MAX_AGE_SECONDS
            or not _verify_gate0_signature(preflight)):
        raise ValueError("storage-smoke nonce is no longer authorized by a fresh signed preflight")
    return _write_storage_smoke_claim(experiment_id, preflight_bytes, reason="PrefixLease smoke allocation")


def _current_preflight_smoke_id() -> str | None:
    """Return the one nonce-bound smoke ID authorized by the live signed preflight."""
    try:
        raw = _read_file_nofollow(GATE0_ATTESTATION_PATH)
        attestation = json.loads(raw)
        created = float(attestation.get("created_at_unix", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    smoke_id = attestation.get("permitted_smoke_experiment_id")
    age = time.time() - created
    if (attestation.get("status") != "PREFLIGHT_PASS" or attestation.get("mode") != "preflight"
            or attestation.get("prefixlease_smoke_status") != "PENDING"
            or not _is_storage_policy_smoke_id(smoke_id)
            or age < 0 or age > GATE0_MAX_AGE_SECONDS
            or not _verify_gate0_signature(attestation)):
        return None
    try:
        if _stat_entry_nofollow(_storage_smoke_claim_path(smoke_id)) is not None:
            return None
    except FileNotFoundError:
        pass
    except OSError:
        return None
    return smoke_id


def disk_free_bytes(path: Path | str = REPO) -> int:
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    directory_fd = _open_directory_nofollow(target)
    try:
        space = os.fstatvfs(directory_fd)
        return space.f_bavail * space.f_frsize
    finally:
        os.close(directory_fd)


def conservative_project_usage_bytes(project_usage: Mapping[str, Any] | None = None) -> int:
    """Use live allocated bytes plus any fresh inventory-only mounted-image bound."""
    usage = project_usage if project_usage is not None else measure_project_usage()
    observed = usage.get("allocated_inode_deduplicated_bytes")
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
        raise ValueError("project usage lacks a valid allocated-byte measurement")
    try:
        inventory = json.loads(_read_file_nofollow(INVENTORY_PATH))
        totals = inventory.get("totals", {})
        upper_bound = totals.get("fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes", 0)
        attested_scoped = totals.get("fgmetal_scoped_allocated_inode_bytes", 0)
        if (not isinstance(upper_bound, int) or isinstance(upper_bound, bool) or upper_bound < 0
                or not isinstance(attested_scoped, int) or isinstance(attested_scoped, bool)
                or attested_scoped < 0):
            raise ValueError("inventory scoped allocation fields are invalid")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Normal admission separately requires a valid, current Gate 0 inventory.
        # Keep synthetic callers usable while refusing to undercount when one is present.
        return observed
    return max(observed + upper_bound, attested_scoped)


def projected_storage_admission(*, current_free_bytes: int, current_scoped_bytes: int,
                                peak_additional_bytes: int) -> dict[str, Any]:
    """Validate the complete declared peak against both normal-admission limits."""
    values = {
        "current_free_bytes": current_free_bytes,
        "current_scoped_bytes": current_scoped_bytes,
        "peak_additional_bytes": peak_additional_bytes,
    }
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in values.values()):
        raise ValueError("projected storage inputs must be non-negative integers")
    projected_free = current_free_bytes - peak_additional_bytes
    projected_scoped = current_scoped_bytes + peak_additional_bytes
    blockers: list[str] = []
    warnings: list[str] = []
    if current_free_bytes < FREE_HARD_STOP:
        blockers.append(f"current free space is below the {FREE_HARD_STOP // GIB} GiB emergency hard stop")
    if projected_free < FREE_MINIMUM:
        blockers.append(f"projected free disk is below the {FREE_MINIMUM // GIB} GiB normal minimum")
        warnings.append(f"projected free disk enters the below-{FREE_MINIMUM // GIB} GiB warning region")
    if projected_free < FREE_PREFERRED:
        warnings.append(f"projected free disk is below the {FREE_PREFERRED // GIB} GiB preferred target")
    if projected_scoped > PROJECT_BUDGET:
        blockers.append(f"projected scoped FG-Metal usage exceeds the {PROJECT_BUDGET // GIB} GiB ceiling")
    if projected_scoped > PROJECT_WARNING:
        warnings.append(f"projected scoped FG-Metal usage exceeds the {PROJECT_WARNING // GIB} GiB warning threshold")
    return {
        **values,
        "projected_free_bytes": projected_free,
        "projected_scoped_bytes": projected_scoped,
        "admitted": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


def _post_materialization_prefix_admission(*, current_free_bytes: int,
                                           current_scoped_bytes: int,
                                           manifest_estimates: Mapping[str, int]) -> dict[str, Any]:
    """Recheck a materialized prefix against its full signed peak estimate.

    Prefix templates are cloned with APFS copy-on-write. The current inode-based
    inventory cannot prove how much of the clone's shared storage may still be
    allocated later, so the complete declared peak, including the prefix and its
    uncertainty margin, remains possible demand after materialization.
    """
    return projected_storage_admission(
        current_free_bytes=current_free_bytes,
        current_scoped_bytes=current_scoped_bytes,
        peak_additional_bytes=manifest_estimates["estimated_projected_new_disk_bytes"],
    )


def admission_uncertainty_margin_bytes(declared_peak_bytes: int) -> int:
    """Add a 10 percent peak margin, with a 256 MiB minimum for small runs."""
    if (not isinstance(declared_peak_bytes, int) or isinstance(declared_peak_bytes, bool)
            or declared_peak_bytes < 0):
        raise ValueError("declared peak must be a non-negative integer")
    proportional = (declared_peak_bytes * ADMISSION_UNCERTAINTY_BASIS_POINTS + 9999) // 10000
    return max(MIN_ADMISSION_UNCERTAINTY_MARGIN, proportional)


@dataclass(frozen=True)
class DiscoveredProjectRoot:
    path: Path
    classification: str
    category: str
    expected_dev: int | None = None
    expected_ino: int | None = None


def _is_other_project_path(path: Path) -> bool:
    return any("dxmt" in part.lower() or "framegen" in part.lower()
               or "step10b6_1" in part.lower() for part in path.parts)


def _classified_project_roots() -> list[DiscoveredProjectRoot]:
    roots: list[DiscoveredProjectRoot] = []
    seen: set[str] = set()

    def add_root(path: Path, classification: str, category: str) -> None:
        key = str(path.resolve(strict=False))
        if key in seen:
            return
        seen.add(key)
        dev = None
        ino = None
        try:
            st = path.lstat()
            dev = st.st_dev
            ino = st.st_ino
        except OSError:
            pass
        roots.append(DiscoveredProjectRoot(
            path=path,
            classification=classification,
            category=category,
            expected_dev=dev,
            expected_ino=ino,
        ))

    add_root(REPO, ROOT_CLASS_MANDATORY, "main_repo")
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout
    for line in worktrees.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
            if path != REPO and path.is_dir():
                add_root(path, ROOT_CLASS_MANDATORY, "git_worktree")

    cache_root = Path.home() / "Library" / "Caches"
    for path in cache_root.iterdir() if cache_root.is_dir() else ():
        name = path.name.lower()
        if path.is_dir() and (
            name.startswith(("fgmetal", "fg-metal", "fgmetal-", "fg-metal-"))
            or name == "lsfg-metal"
        ):
            if path == CACHE:
                add_root(path, ROOT_CLASS_MANDATORY, "project_cache")
            else:
                add_root(path, ROOT_CLASS_OPTIONAL_EPHEMERAL, "project_cache_aux")

    temp_root = Path("/private/tmp")
    if temp_root.is_dir():
        for path in temp_root.iterdir():
            name = path.name.lower()
            if path.is_dir() and name.startswith(("fgmetal", "fg-metal", "step10a", "step11b")):
                if "-test-" in name or name.startswith("test-") or "synthetic" in name:
                    continue
                add_root(path, ROOT_CLASS_OPTIONAL_EPHEMERAL, "project_temp")
    xcode_root = Path.home() / "Library/Developer/Xcode/DerivedData"
    if xcode_root.is_dir():
        for path in xcode_root.iterdir():
            if path.is_dir() and not path.is_symlink() and path.name.lower().startswith("moltenvkpackaging-"):
                add_root(path, ROOT_CLASS_OPTIONAL_EPHEMERAL, "xcode_derived_data")
    return roots


def _project_roots() -> list[Path]:
    return [r.path for r in _classified_project_roots()]


def _measured_output_dir(path: Path | str, *, require_missing: bool = False) -> Path:
    """Validate and normalize an experiment output within measured project storage."""
    output_dir = Path(path).expanduser().absolute()
    resolved_output = output_dir.resolve(strict=False)
    repo_resolved = REPO.resolve()
    if resolved_output == repo_resolved or repo_resolved not in resolved_output.parents:
        raise ValueError(f"experiment output must resolve inside the measured repository: {output_dir}")
    experiments_root = (repo_resolved / "experiments").resolve()
    if resolved_output == experiments_root or experiments_root not in resolved_output.parents:
        raise ValueError(f"experiment output must be below the measured experiments directory: {output_dir}")
    if _is_other_project_path(resolved_output):
        raise ValueError(f"experiment output is inside an excluded protected workstream: {output_dir}")
    current = output_dir
    while current == REPO or REPO in current.parents:
        if current.is_symlink():
            raise ValueError(f"experiment output path may not traverse a symlink: {current}")
        if current == REPO:
            break
        current = current.parent
    if require_missing and (output_dir.exists() or output_dir.is_symlink()):
        raise FileExistsError(f"refusing to reuse an existing experiment output directory: {output_dir}")
    if not require_missing and output_dir.exists() and (output_dir.is_symlink() or not output_dir.is_dir()):
        raise ValueError(f"experiment output must be a real directory when present: {output_dir}")
    return output_dir


def _experiment_manifest_path(experiment_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", experiment_id):
        raise ValueError("experiment manifest ID contains unsupported path characters")
    manifest_root = REPO / "experiments/storage/manifests"
    path = manifest_root / f"{experiment_id}.json"
    current = path
    while current == REPO or REPO in current.parents:
        if current.is_symlink():
            raise ValueError(f"experiment manifest path traverses a symlink: {current}")
        if current == REPO:
            break
        current = current.parent
    resolved = path.resolve(strict=False)
    if REPO.resolve() not in resolved.parents or _is_other_project_path(resolved):
        raise ValueError(f"experiment manifest path escapes measured project storage: {path}")
    return path


def _storage_entry_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
        info.st_size, info.st_mtime_ns, getattr(info, "st_ctime_ns", 0),
        getattr(info, "st_blocks", 0),
    )


def _persistent_output_snapshot(path: Path) -> tuple[int, str]:
    """Measure and fingerprint a run output on the measured project filesystem."""
    root_info = _stat_entry_nofollow(path)
    if root_info is None:
        return 0, hashlib.sha256(b"missing-output\n").hexdigest()
    repo_info = REPO.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_dev != repo_info.st_dev):
        raise OSError("signed experiment output is not a directory on the measured project filesystem")
    allocated_by_inode: dict[tuple[int, int], int] = {}
    tree_digest = hashlib.sha256()
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        current_info = current.lstat()
        if not stat.S_ISDIR(current_info.st_mode) or current_info.st_dev != root_info.st_dev:
            raise OSError("signed experiment output crosses a filesystem boundary")
        current_row = {
            "path": "." if current == path else current.relative_to(path).as_posix(),
            "kind": "directory",
            "identity": _storage_entry_identity(current_info),
        }
        tree_digest.update(json.dumps(current_row, sort_keys=True, separators=(",", ":")).encode())
        tree_digest.update(b"\n")
        kept_directories: list[str] = []
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            child = current / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                tree_digest.update(json.dumps({
                    "path": child.relative_to(path).as_posix(), "kind": "symlink",
                    "identity": _storage_entry_identity(info), "target": os.readlink(child),
                }, sort_keys=True, separators=(",", ":")).encode())
                tree_digest.update(b"\n")
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_dev != root_info.st_dev:
                raise OSError("signed experiment output contains an unexpected filesystem boundary")
            kept_directories.append(name)
        dirnames[:] = kept_directories
        for name in filenames:
            child = current / name
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                tree_digest.update(json.dumps({
                    "path": child.relative_to(path).as_posix(), "kind": "symlink",
                    "identity": _storage_entry_identity(info), "target": os.readlink(child),
                }, sort_keys=True, separators=(",", ":")).encode())
                tree_digest.update(b"\n")
                continue
            if not stat.S_ISREG(info.st_mode):
                raise OSError("signed experiment output contains a non-regular file")
            tree_digest.update(json.dumps({
                "path": child.relative_to(path).as_posix(), "kind": "file",
                "identity": _storage_entry_identity(info),
            }, sort_keys=True, separators=(",", ":")).encode())
            tree_digest.update(b"\n")
            allocated_by_inode.setdefault(
                (info.st_dev, info.st_ino), getattr(info, "st_blocks", 0) * 512)
    after_info = _stat_entry_nofollow(path)
    if (after_info is None
            or _storage_entry_identity(after_info) != _storage_entry_identity(root_info)):
        raise OSError("signed experiment output root changed during allocation measurement")
    return sum(allocated_by_inode.values()), tree_digest.hexdigest()


def _persistent_output_allocated_bytes(path: Path) -> int:
    """Read allocated output bytes without hashing file contents on the live guard path."""
    root_info = _stat_entry_nofollow(path)
    if root_info is None:
        return 0
    repo_info = REPO.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_dev != repo_info.st_dev:
        raise OSError("signed experiment output is not a directory on the measured project filesystem")
    allocated_by_inode: dict[tuple[int, int], int] = {}
    pending = [path]
    while pending:
        current = pending.pop()
        current_info = current.lstat()
        if (not stat.S_ISDIR(current_info.st_mode)
                or current_info.st_dev != root_info.st_dev):
            raise OSError("signed experiment output crosses a filesystem boundary")
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise OSError("signed experiment output contains a symlink")
                if stat.S_ISDIR(info.st_mode):
                    if info.st_dev != root_info.st_dev:
                        raise OSError("signed experiment output crosses a filesystem boundary")
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    allocated_by_inode.setdefault(
                        (info.st_dev, info.st_ino), getattr(info, "st_blocks", 0) * 512)
                else:
                    raise OSError("signed experiment output contains a non-regular file")
    after_info = _stat_entry_nofollow(path)
    if (after_info is None
            or _storage_entry_identity(after_info) != _storage_entry_identity(root_info)):
        raise OSError("signed experiment output root changed during allocation measurement")
    return sum(allocated_by_inode.values())


def _signed_persistent_output_snapshot(
    *, exclude_experiment_id: str | None = None,
    expected_gate0_attestation_sha256: str | None = None,
    baseline_experiment_ids: set[str] | None = None,
) -> tuple[dict[str, Any], tuple[Path, ...], list[str]]:
    """Bind authenticated, reservation-limited run outputs for baseline comparison.

    These paths remain part of the whole-project allocation. They are excluded
    only from the unrelated-growth comparison while their signed manifest and
    output allocation remain stable and within the manifest's reservation.
    """
    blockers: list[str] = []
    root = REPO / "experiments/storage/manifests"
    try:
        directory_fd = _open_directory_nofollow(root)
    except OSError as error:
        return {}, (), [f"experiment manifest directory cannot be opened for storage accounting: {error}"]

    entries: list[tuple[str, tuple[int, ...] | None, str | None]] = []
    bindings: list[dict[str, Any]] = []
    output_paths: list[Path] = []
    try:
        root_info = os.fstat(directory_fd)
        names_before = sorted(name for name in os.listdir(directory_fd) if name.endswith(".json"))
        target_manifest_name = f"{HISTORICAL_TERMINAL_RUN_ID}.json"
        if (target_manifest_name not in names_before
                and REPO.resolve() == HISTORICAL_TERMINAL_REPOSITORY):
            blockers.append("historical terminal storage manifest is missing")
        for name in names_before:
            is_historical_target = name == target_manifest_name
            info = _stat_child_nofollow(directory_fd, name)
            if info is None:
                entries.append((name, None, None))
                blockers.append(f"experiment manifest changed during storage accounting: {name}")
                continue
            identity = _storage_entry_identity(info)
            digest: str | None = None
            entries.append((name, identity, digest))
            manifest_path = root / name
            allocated = getattr(info, "st_blocks", 0) * 512
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 1024 * 1024 or allocated > 2 * 1024 * 1024):
                if is_historical_target:
                    blockers.append("historical terminal storage manifest has an unsafe file identity")
                continue
            try:
                manifest, raw, read_info = _read_json_at(
                    directory_fd, name, maximum_bytes=1024 * 1024)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                if is_historical_target:
                    blockers.append("historical terminal storage manifest is unreadable")
                continue
            digest = hashlib.sha256(raw).hexdigest()
            after_info = _stat_child_nofollow(directory_fd, name)
            if (after_info is None
                    or _storage_entry_identity(read_info) != identity
                    or _storage_entry_identity(after_info) != identity):
                blockers.append(f"experiment manifest changed during storage accounting: {name}")
                continue
            entries[-1] = (name, identity, digest)
            try:
                experiment_id = manifest.get("experiment_id")
                if (not isinstance(experiment_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]+", experiment_id)
                        or not SIGNED_OUTPUT_MANIFEST_REQUIRED_FIELDS.issubset(manifest)
                        or name != f"{experiment_id}.json"
                        or manifest_path != _experiment_manifest_path(experiment_id)
                        or manifest.get("manifest_path") != str(manifest_path)
                        or not isinstance(manifest.get("gate0_attestation_sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", manifest["gate0_attestation_sha256"])
                        or manifest.get("decision") != "ALLOW"
                        or raw != _canonical_pretty_json_bytes(manifest)
                        or not _verify_manifest_signature(manifest)):
                    if is_historical_target:
                        blockers.append("historical terminal storage manifest identity or signature is invalid")
                    continue
                estimates = _validated_manifest_estimates(manifest)
                output_dir = _measured_output_dir(manifest.get("output_dir", ""))
                if str(output_dir) != manifest.get("output_dir"):
                    if is_historical_target:
                        blockers.append("historical terminal output path is not canonical")
                    continue
            except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
                if is_historical_target:
                    blockers.append("historical terminal storage manifest or output path is invalid")
                continue
            if experiment_id == exclude_experiment_id:
                if is_historical_target:
                    blockers.append("historical terminal output cannot be excluded as an active run")
                continue
            manifest_gate0_hash = manifest.get("gate0_attestation_sha256")
            if (expected_gate0_attestation_sha256 is not None
                    and experiment_id not in (baseline_experiment_ids or set())
                    and experiment_id != HISTORICAL_TERMINAL_RUN_ID
                    and manifest_gate0_hash != expected_gate0_attestation_sha256):
                blockers.append(
                    f"new signed experiment output does not bind the current Gate 0 attestation: {experiment_id}")
                continue

            output_info = _stat_entry_nofollow(output_dir)
            if output_info is not None and (
                    not stat.S_ISDIR(output_info.st_mode)
                    or output_info.st_uid != os.getuid()
                    or output_info.st_dev != REPO.lstat().st_dev):
                blockers.append(
                    f"signed experiment output path is not an owned real directory: {experiment_id}")
                continue
            try:
                output_allocated, output_tree_sha256 = _persistent_output_snapshot(output_dir)
            except (OSError, TypeError, ValueError) as error:
                blockers.append(
                    f"signed experiment output allocation cannot be measured: {experiment_id}: {error}")
                continue
            output_after = _stat_entry_nofollow(output_dir)
            output_identity = (_storage_entry_identity(output_info) if output_info is not None else None)
            if ((output_after is None) != (output_info is None)
                    or (output_after is not None
                        and _storage_entry_identity(output_after) != output_identity)):
                blockers.append(f"signed experiment output path changed during storage accounting: {experiment_id}")
                continue
            terminal_closure: dict[str, Any] | None = None
            if is_historical_target:
                terminal_closure, terminal_closure_blockers = _verify_historical_terminal_closure(
                    manifest, digest, output_dir,
                    allocated_bytes=output_allocated,
                    metadata_tree_sha256=output_tree_sha256,
                    allowed_active_experiment_id=exclude_experiment_id,
                )
                blockers.extend(terminal_closure_blockers)
                if terminal_closure is None:
                    if output_allocated > estimates["estimated_persistent_bytes"]:
                        blockers.append(
                            f"signed experiment output exceeds its persistent reservation: {experiment_id}")
                    continue
            if output_allocated > estimates["estimated_persistent_bytes"]:
                if terminal_closure is None:
                    blockers.append(
                        f"signed experiment output exceeds its persistent reservation: {experiment_id}")
                    continue
            binding = {
                "experiment_id": experiment_id,
                "manifest_path": str(manifest_path),
                "manifest_sha256": digest,
                "manifest_identity": list(identity),
                "manifest_gate0_attestation_sha256": manifest_gate0_hash,
                "output_dir": str(output_dir),
                "output_identity": (list(output_identity) if output_identity is not None else None),
                "output_allocated_bytes": output_allocated,
                "output_tree_sha256": output_tree_sha256,
                "output_reservation_bytes": estimates["estimated_persistent_bytes"],
            }
            if terminal_closure is not None:
                binding.update({
                    "historical_terminal_closure_path": terminal_closure["closure_path"],
                    "historical_terminal_closure_sha256": terminal_closure["closure_sha256"],
                    "historical_terminal_tree_sha256": terminal_closure["closure_tree_sha256"],
                })
            bindings.append(binding)
            output_paths.extend((manifest_path, output_dir))
        names_after = sorted(name for name in os.listdir(directory_fd) if name.endswith(".json"))
        final_root_info = os.fstat(directory_fd)
        if (names_after != names_before
                or _storage_entry_identity(final_root_info)[:2]
                != _storage_entry_identity(root_info)[:2]):
            blockers.append("experiment manifest directory changed during storage accounting")
    except OSError as error:
        blockers.append(f"experiment manifest directory changed during storage accounting: {error}")
    finally:
        os.close(directory_fd)

    ordered_outputs = sorted(Path(binding["output_dir"]) for binding in bindings)
    for index, left in enumerate(ordered_outputs):
        for right in ordered_outputs[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                blockers.append("signed experiment output reservations overlap")
                break
        if blockers and blockers[-1] == "signed experiment output reservations overlap":
            break

    snapshot = {
        "manifest_directory": str(root),
        "manifest_directory_identity": _storage_entry_identity(root_info),
        "manifest_entries": tuple(entries),
        "bindings": tuple(bindings),
    }
    return snapshot, tuple(dict.fromkeys(output_paths)), blockers


def _historical_terminal_run_paths() -> dict[str, Path]:
    run_id = HISTORICAL_TERMINAL_RUN_ID
    run_dir = REPO / "experiments/dxvk_macos_step11d3b/runs" / run_id
    return {
        "storage_manifest": _experiment_manifest_path(run_id),
        "run_manifest": run_dir / "run-manifest.json",
        "run_result": run_dir / "run-result.json",
        "cleanup_receipt": run_dir / "prefix-cleanup.json",
        "execution_result": run_dir / "execution-result.json",
        "trace_index": run_dir / "trace-index.json",
        "output_dir": run_dir,
        "report": REPO / "experiments/dxvk_macos_step11d3b/STEP-11D3-BR9R2-REPORT.md",
    }


def _historical_terminal_tree_snapshot(path: Path) -> dict[str, Any]:
    """Hash a terminal output through pinned, no-follow descriptors and revalidate its names."""
    path = Path(os.path.abspath(path))
    expected_output = _historical_terminal_run_paths()["output_dir"]
    if path != expected_output:
        raise ValueError("historical closure tree path is not the fixed Z9 run directory")
    parent_fd = _open_directory_nofollow(path.parent)
    root_fd: int | None = None
    try:
        parent_before = os.fstat(parent_fd)
        root_lstat = _stat_child_nofollow(parent_fd, path.name)
        if root_lstat is None or not stat.S_ISDIR(root_lstat.st_mode):
            raise OSError("historical closure output root is missing or is not a real directory")
        root_fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        root_info = os.fstat(root_fd)
        if (_storage_entry_identity(root_info) != _storage_entry_identity(root_lstat)
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_dev != REPO.lstat().st_dev
                or (root_info.st_dev, root_info.st_ino) != HISTORICAL_TERMINAL_ROOT_IDENTITY
                or root_info.st_uid != os.getuid()):
            raise OSError("historical closure output root identity is unexpected")

        entries: list[dict[str, Any]] = []
        allocated_by_inode: dict[tuple[int, int], int] = {}
        logical_bytes = 0

        def visit(directory_fd: int, relative: str, directory_info: os.stat_result) -> None:
            nonlocal logical_bytes
            names_before = sorted(os.listdir(directory_fd))
            entries.append({
                "path": relative or ".",
                "kind": "directory",
                "identity": list(_storage_entry_identity(directory_info)),
            })
            for name in names_before:
                if (not name or name in {".", ".."} or "/" in name or "\\" in name
                        or "\x00" in name):
                    raise OSError("historical closure tree contains a non-canonical name")
                child_relative = f"{relative}/{name}" if relative else name
                before = _stat_child_nofollow(directory_fd, name)
                if before is None or before.st_dev != root_info.st_dev:
                    raise OSError("historical closure tree changed or crossed a filesystem boundary")
                if stat.S_ISLNK(before.st_mode):
                    raise OSError("historical closure tree contains a symlink")
                if stat.S_ISDIR(before.st_mode):
                    child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                    try:
                        opened = os.fstat(child_fd)
                        if _storage_entry_identity(opened) != _storage_entry_identity(before):
                            raise OSError("historical closure directory changed while opening")
                        visit(child_fd, child_relative, opened)
                        after = _stat_child_nofollow(directory_fd, name)
                        if (after is None
                                or _storage_entry_identity(after) != _storage_entry_identity(opened)):
                            raise OSError("historical closure directory changed while enumerating")
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise OSError("historical closure tree contains a non-regular or linked file")
                descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                      | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
                try:
                    opened = os.fstat(descriptor)
                    if _storage_entry_identity(opened) != _storage_entry_identity(before):
                        raise OSError("historical closure file changed while opening")
                    digest = hashlib.sha256()
                    length = 0
                    while True:
                        block = os.read(descriptor, 4 * 1024 * 1024)
                        if not block:
                            break
                        digest.update(block)
                        length += len(block)
                    after_open = os.fstat(descriptor)
                    after_name = _stat_child_nofollow(directory_fd, name)
                    if (length != opened.st_size
                            or _storage_entry_identity(after_open) != _storage_entry_identity(opened)
                            or after_name is None
                            or _storage_entry_identity(after_name) != _storage_entry_identity(opened)):
                        raise OSError("historical closure file changed while hashing")
                finally:
                    os.close(descriptor)
                entries.append({
                    "path": child_relative,
                    "kind": "file",
                    "identity": list(_storage_entry_identity(opened)),
                    "sha256": digest.hexdigest(),
                })
                inode = (opened.st_dev, opened.st_ino)
                allocated_by_inode[inode] = getattr(opened, "st_blocks", 0) * 512
                logical_bytes += opened.st_size
            names_after = sorted(os.listdir(directory_fd))
            current_directory = os.fstat(directory_fd)
            if (names_after != names_before
                    or _storage_entry_identity(current_directory)
                    != _storage_entry_identity(directory_info)):
                raise OSError("historical closure directory listing changed during measurement")

        visit(root_fd, "", root_info)
        root_after = _stat_child_nofollow(parent_fd, path.name)
        parent_after = os.fstat(parent_fd)
        if (root_after is None
                or _storage_entry_identity(root_after) != _storage_entry_identity(root_info)
                or (parent_before.st_dev, parent_before.st_ino)
                != (parent_after.st_dev, parent_after.st_ino)):
            raise OSError("historical closure output root or parent changed during measurement")
        reopened_parent_fd = _open_directory_nofollow(path.parent)
        try:
            reopened_parent = os.fstat(reopened_parent_fd)
            if (reopened_parent.st_dev, reopened_parent.st_ino) != (parent_before.st_dev, parent_before.st_ino):
                raise OSError("historical closure output parent path was substituted")
        finally:
            os.close(reopened_parent_fd)
        entries.sort(key=lambda item: item["path"])
        digest = hashlib.sha256()
        for item in entries:
            digest.update(json.dumps(item, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8"))
            digest.update(b"\n")
        allocated_bytes = sum(allocated_by_inode.values())
        if (logical_bytes != HISTORICAL_TERMINAL_OUTPUT_LOGICAL_BYTES
                or allocated_bytes != HISTORICAL_TERMINAL_OUTPUT_BYTES):
            raise OSError("historical closure output totals differ from the provenance inventory")
        file_hashes = {item["path"]: item["sha256"] for item in entries if item["kind"] == "file"}
        if file_hashes != HISTORICAL_TERMINAL_TRUSTED_FILES:
            raise OSError("historical closure file set or content differs from the provenance inventory")
        return {
            "output_dir": str(path),
            "root_identity": list(_storage_entry_identity(root_info)),
            "parent_identity": [parent_before.st_dev, parent_before.st_ino],
            "entries": entries,
            "entry_count": len(entries),
            "file_count": len(file_hashes),
            "logical_bytes": logical_bytes,
            "allocated_bytes": allocated_bytes,
            "tree_sha256": digest.hexdigest(),
        }
    finally:
        if root_fd is not None:
            os.close(root_fd)
        os.close(parent_fd)


def _historical_terminal_copy_controller_evidence() -> dict[str, str]:
    destination_root = (REPO / "experiments/dxvk_macos_step11d3b/evidence/br9r2"
                        / "historical-closure-review/source-controller")
    copied: dict[str, str] = {}
    for relative, (source_raw, expected_sha256) in HISTORICAL_TERMINAL_SOURCE_EVIDENCE.items():
        source = Path(source_raw)
        contents = _read_file_nofollow(source, maximum_bytes=64 * 1024 * 1024)
        digest = hashlib.sha256(contents).hexdigest()
        if digest != expected_sha256:
            raise RuntimeError(f"transient historical controller evidence changed: {relative}")
        target = destination_root / relative
        parent_fd = _open_directory_nofollow(target.parent, create=True, strict_directory_sync=True)
        try:
            existing = _stat_child_nofollow(parent_fd, target.name)
            if existing is not None:
                existing_bytes, existing_identity = _read_file_from_directory_fd(
                    parent_fd, target.name, display_path=target, maximum_bytes=64 * 1024 * 1024)
                if (hashlib.sha256(existing_bytes).hexdigest() != expected_sha256
                        or (existing.st_dev, existing.st_ino) != existing_identity[:2]):
                    raise RuntimeError(f"refusing to replace conflicting copied evidence: {target}")
            else:
                descriptor = os.open(target.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
                try:
                    offset = 0
                    while offset < len(contents):
                        count = os.write(descriptor, contents[offset:])
                        if count <= 0:
                            raise OSError("short write while preserving controller evidence")
                        offset += count
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent_fd)
            copied[relative] = digest
        finally:
            os.close(parent_fd)
    controller_path = destination_root / "controller.json"
    controller = json.loads(_read_file_nofollow(controller_path, maximum_bytes=64 * 1024 * 1024))
    if (controller.get("run_id") != HISTORICAL_TERMINAL_RUN_ID
            or controller.get("status") != "INVALID_OR_INCOMPLETE"
            or controller.get("technical_valid") is not False
            or controller.get("finder_actions") != []
            or controller.get("measured_phases") != {}
            or controller.get("measured_sequence_completed") is not False):
        raise RuntimeError("preserved controller does not prove the recorded invalid, unexecuted run")
    return copied


def _historical_terminal_evidence(
    *, report_prefix_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    paths = _historical_terminal_run_paths()
    raw_by_name = {
        name: _read_file_nofollow(paths[name])
        for name in ("storage_manifest", "run_manifest", "run_result", "cleanup_receipt",
                     "execution_result", "trace_index")
    }
    decoded = {name: json.loads(raw) for name, raw in raw_by_name.items()}
    run_id = HISTORICAL_TERMINAL_RUN_ID
    storage_manifest = decoded["storage_manifest"]
    run_manifest = decoded["run_manifest"]
    run_result = decoded["run_result"]
    cleanup = decoded["cleanup_receipt"]
    execution = decoded["execution_result"]
    trace_index = decoded["trace_index"]
    manifest_path = paths["storage_manifest"]
    manifest_sha = hashlib.sha256(raw_by_name["storage_manifest"]).hexdigest()
    experiment_token = manifest_sha[:32]
    if (manifest_sha != "11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59"
            or storage_manifest.get("experiment_id") != run_id
            or storage_manifest.get("manifest_path") != str(manifest_path)
            or storage_manifest.get("output_dir") != str(paths["output_dir"])
            or storage_manifest.get("estimated_persistent_bytes") != HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES
            or storage_manifest.get("decision") != "ALLOW"
            or raw_by_name["storage_manifest"] != _canonical_pretty_json_bytes(storage_manifest)
            or not _verify_manifest_signature(storage_manifest)):
        raise RuntimeError("original signed storage manifest is missing, changed, or unauthenticated")
    if (run_manifest.get("experiment_id") != run_id
            or run_manifest.get("storage_manifest_path") != str(manifest_path)
            or hashlib.sha256(raw_by_name["run_manifest"]).hexdigest()
            != HISTORICAL_TERMINAL_TRUSTED_FILES["run-manifest.json"]):
        raise RuntimeError("historical run manifest identity or trusted hash changed")
    post = run_result.get("gate0_post")
    pre = run_result.get("gate0_pre")
    if (run_result.get("experiment_id") != run_id
            or run_result.get("status") != "FAIL"
            or run_result.get("is_valid") is not False
            or run_result.get("prefix_cleanup_pass") is not True
            or not isinstance(post, Mapping)
            or post.get("stage") != "POST"
            or post.get("gate0_status") != "BLOCKED"
            or post.get("gate0_blockers") != HISTORICAL_TERMINAL_BLOCKERS
            or not isinstance(pre, Mapping)
            or pre.get("stage") != "PRE"
            or pre.get("gate0_status") != "PASS"
            or pre.get("gate0_blockers") != []
            or hashlib.sha256(raw_by_name["run_result"]).hexdigest()
            != HISTORICAL_TERMINAL_TRUSTED_FILES["run-result.json"]):
        raise RuntimeError("original FAIL and Gate POST BLOCKED evidence is missing or changed")
    if (cleanup.get("experiment_id") != run_id
            or cleanup.get("status") != "PASS"
            or cleanup.get("prefix_deleted") is not True
            or cleanup.get("prefix_preserved") is not False
            or cleanup.get("receipt_finalization_complete") is not True
            or cleanup.get("active_lease_marker_removed_after_receipt") is not True
            or cleanup.get("active_lease_marker_removal_order") != "receipt_durable_before_marker_removal"
            or cleanup.get("expected_persistent_bytes") != HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES
            or cleanup.get("wineprefix_identity", {}).get("manifest_sha256") != manifest_sha
            or not _verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
            or hashlib.sha256(raw_by_name["cleanup_receipt"]).hexdigest()
            != HISTORICAL_TERMINAL_TRUSTED_FILES["prefix-cleanup.json"]):
        raise RuntimeError("historical PrefixLease cleanup receipt is missing or invalid")
    if (execution.get("experiment_id") != run_id
            or execution.get("experiment_token") != experiment_token
            or execution.get("timed_out") is not False
            or execution.get("process_returncode") != 0
            or execution.get("target_unix_pid") != 58921
            or hashlib.sha256(raw_by_name["execution_result"]).hexdigest()
            != HISTORICAL_TERMINAL_TRUSTED_FILES["execution-result.json"]):
        raise RuntimeError("historical execution result is not the expected terminal process record")
    if (trace_index.get("status") != "PASS"
            or trace_index.get("errors") != []
            or trace_index.get("experiment_token") != experiment_token
            or hashlib.sha256(raw_by_name["trace_index"]).hexdigest()
            != HISTORICAL_TERMINAL_TRUSTED_FILES["trace-index.json"]):
        raise RuntimeError("historical trace index is missing, changed, or invalid")

    report_raw = _read_file_nofollow(paths["report"], maximum_bytes=16 * 1024 * 1024)
    required_report_facts = (
        "R5 — INVALID / INCOMPLETE".encode("utf-8"),
        "T8 — insufficient timing evidence".encode("utf-8"),
        b"598,573,056",
        b"725,688,320",
        b"314,572,800",
    )
    if any(fact not in report_raw for fact in required_report_facts):
        raise RuntimeError("historical report no longer contains the immutable R5/T8 and allocation facts")
    if report_prefix_binding is None:
        report_prefix_bytes = len(report_raw)
        report_prefix_sha256 = hashlib.sha256(report_raw).hexdigest()
        if (report_prefix_bytes != HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES
                or report_prefix_sha256 != HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256):
            raise RuntimeError("pre-closure historical R5/T8 report bytes differ from their trusted snapshot")
    else:
        report_prefix_bytes = report_prefix_binding.get("bytes")
        report_prefix_sha256 = report_prefix_binding.get("sha256")
        if (report_prefix_binding.get("path") != str(paths["report"])
                or report_prefix_bytes != HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES
                or report_prefix_sha256 != HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256
                or not isinstance(report_prefix_bytes, int) or isinstance(report_prefix_bytes, bool)
                or report_prefix_bytes < 0 or not isinstance(report_prefix_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", report_prefix_sha256)
                or len(report_raw) < report_prefix_bytes
                or hashlib.sha256(report_raw[:report_prefix_bytes]).hexdigest() != report_prefix_sha256):
            raise RuntimeError("historical R5/T8 report prefix changed after closure")
    controller_hashes: dict[str, str] = {}
    controller_root = (REPO / "experiments/dxvk_macos_step11d3b/evidence/br9r2"
                       / "historical-closure-review/source-controller")
    for relative, (_source, expected_sha256) in HISTORICAL_TERMINAL_SOURCE_EVIDENCE.items():
        copied_path = controller_root / relative
        copied_bytes = _read_file_nofollow(copied_path, maximum_bytes=64 * 1024 * 1024)
        copied_sha256 = hashlib.sha256(copied_bytes).hexdigest()
        if copied_sha256 != expected_sha256:
            raise RuntimeError(f"durable controller evidence copy changed: {relative}")
        controller_hashes[relative] = copied_sha256
    controller = json.loads(_read_file_nofollow(
        controller_root / "controller.json", maximum_bytes=64 * 1024 * 1024))
    if (controller.get("run_id") != run_id
            or controller.get("status") != "INVALID_OR_INCOMPLETE"
            or controller.get("technical_valid") is not False
            or controller.get("finder_actions") != []
            or controller.get("measured_phases") != {}
            or controller.get("measured_sequence_completed") is not False):
        raise RuntimeError("durable controller copy no longer proves the original invalid run")
    return {
        "paths": {key: str(value) for key, value in paths.items()},
        "sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in raw_by_name.items()},
        "manifest_hmac_sha256": storage_manifest["manifest_hmac_sha256"],
        "experiment_token": experiment_token,
        "original_reservation_bytes": HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
        "gate0_pre_status": pre["gate0_status"],
        "gate0_pre_evidence_sha256": hashlib.sha256(json.dumps(
            pre, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "gate0_post_status": post["gate0_status"],
        "gate0_post_blockers": list(post["gate0_blockers"]),
        "gate0_post_evidence_sha256": hashlib.sha256(json.dumps(
            post, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "gate0_post_allocation_bytes": HISTORICAL_TERMINAL_POST_OUTPUT_BYTES,
        "gate0_post_allocation_provenance": "report reconstruction; not a numeric field in the embedded POST object",
        "run_status": run_result["status"],
        "r5_classification": "INVALID / INCOMPLETE",
        "t8_classification": "INSUFFICIENT TIMING EVIDENCE",
        "finder_treatment": "NOT EXECUTED",
        "report_prefix_bytes": report_prefix_bytes,
        "report_prefix_sha256": report_prefix_sha256,
        "cleanup_status": cleanup["status"],
        "cleanup_receipt_hmac_sha256": cleanup["cleanup_receipt_hmac_sha256"],
        "prefix_path": cleanup["prefix_path"],
        "prefix_was_deleted": cleanup["prefix_deleted"],
        "process_ids": sorted({58921, 58942}),
        "controller_evidence_sha256": controller_hashes,
    }


def _historical_terminal_liveness_check(
    evidence: Mapping[str, Any], *, allowed_active_experiment_id: str | None = None,
) -> dict[str, Any]:
    markers = _active_lease_markers()
    expected_allowed_marker = (
        ACTIVE_LEASE_ROOT / f"{allowed_active_experiment_id}.json"
        if allowed_active_experiment_id is not None
        and allowed_active_experiment_id != HISTORICAL_TERMINAL_RUN_ID
        and re.fullmatch(r"[A-Za-z0-9_-]+", allowed_active_experiment_id) else None)
    if markers and (expected_allowed_marker is None or markers != [expected_allowed_marker]):
        raise RuntimeError("active or interrupted PrefixLease markers prevent historical closure")
    unrelated_marker_count = 0 if markers and markers == [expected_allowed_marker] else len(markers)
    retention_directory = Path(__file__).resolve().parent
    if str(retention_directory) not in sys.path:
        sys.path.insert(0, str(retention_directory))
    import build_retention  # pylint: disable=import-outside-toplevel
    build_state = build_retention._snapshot()
    if any(build_state.get(key) for key in (
            "active", "completed_pending_retirement", "failed_or_retiring", "unknown")):
        raise RuntimeError("active, pending, failed, or ambiguous BuildLease state prevents closure")

    pid_checks = []
    for pid in evidence.get("process_ids", ()):
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            raise RuntimeError("historical process identity is malformed")
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,command="],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10, check=False)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise RuntimeError(f"historical process liveness could not be checked for pid {pid}")
        if result.stdout.strip():
            raise RuntimeError(f"historical process pid {pid} is still present")
        pid_checks.append({"pid": pid, "status": "GONE"})

    lsof_identity = _historical_terminal_lsof_identity()
    lsof_path = lsof_identity["path"]
    output_dir = Path(evidence["paths"]["output_dir"])
    prefix = Path(str(evidence["prefix_path"]))
    result = subprocess.run([lsof_path, "-nP", "-Fpcn"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=90, check=False)
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError("lsof could not inspect open process paths for historical quiescence")
    path_roots = {str(output_dir), str(prefix)}
    try:
        path_roots.add(str(prefix.resolve(strict=False)))
    except OSError:
        raise RuntimeError("historical prefix path cannot be normalized safely")
    current_pid: str | None = None
    matches: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            current_pid = line[1:]
        elif line.startswith("n"):
            open_name = line[1:].removesuffix(" (deleted)")
            normalized_name = os.path.normpath(open_name)
            if any(normalized_name == root or normalized_name.startswith(root + os.sep)
                   for root in path_roots):
                matches.append({"pid": current_pid or "unknown", "path": open_name})
    if matches:
        raise RuntimeError("a process still has a historical output or prefix path open")
    if _historical_terminal_lsof_identity() != lsof_identity:
        raise RuntimeError("trusted lsof identity changed during historical path liveness inspection")
    if prefix.exists() or prefix.is_symlink():
        prefix_info = prefix.lstat()
        if not stat.S_ISDIR(prefix_info.st_mode):
            raise RuntimeError("historical PrefixLease path was replaced with a non-directory")
        raise RuntimeError("deleted historical PrefixLease path unexpectedly exists")
    if evidence.get("prefix_was_deleted") is not True:
        raise RuntimeError("historical PrefixLease deletion is not authenticated")
    return {
        "active_prefix_markers": unrelated_marker_count,
        "build_retention_status": "QUIESCENT",
        "historical_processes": pid_checks,
        "output_and_prefix_lsof_status": "NO_MATCH",
        "prefix_status": "DELETED",
        "lsof": lsof_identity,
    }


def _historical_terminal_lsof_identity() -> dict[str, Any]:
    """Read the fixed, root-owned system lsof binary without consulting PATH."""
    path = Path(HISTORICAL_TERMINAL_LSOF_PATH)
    if str(path) != HISTORICAL_TERMINAL_LSOF_PATH or not path.is_absolute():
        raise RuntimeError("trusted lsof path is not canonical")
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        parent_info = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_uid != 0
                or stat.S_IMODE(parent_info.st_mode) & 0o022):
            raise RuntimeError("trusted lsof parent directory is not root-owned and non-writable")
        before = _stat_child_nofollow(parent_fd, path.name)
        if (before is None or not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or before.st_nlink != 1 or not (before.st_mode & 0o111)
                or stat.S_IMODE(before.st_mode) & 0o022):
            raise RuntimeError("trusted lsof binary is not a root-owned, non-writable executable")
        contents, identity = _read_file_from_directory_fd(
            parent_fd, path.name, display_path=path, maximum_bytes=64 * 1024 * 1024)
        after = _stat_child_nofollow(parent_fd, path.name)
        parent_after = os.fstat(parent_fd)
        if (after is None or _storage_entry_identity(after) != _storage_entry_identity(before)
                or _storage_entry_identity(before)[:2] != identity[:2]
                or (parent_after.st_dev, parent_after.st_ino) != (parent_info.st_dev, parent_info.st_ino)):
            raise RuntimeError("trusted lsof changed while its identity was read")
        return {
            "path": HISTORICAL_TERMINAL_LSOF_PATH,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "file_identity": list(identity),
            "owner_uid": before.st_uid,
            "mode": stat.S_IMODE(before.st_mode),
            "parent_identity": [parent_info.st_dev, parent_info.st_ino],
        }
    finally:
        os.close(parent_fd)


def _historical_terminal_closure_path() -> Path:
    return (REPO / "experiments/storage/historical-terminal-closures"
            / f"{HISTORICAL_TERMINAL_RUN_ID}.json")


def _historical_terminal_closure_correction_path() -> Path:
    return (REPO / "experiments/storage/historical-terminal-closures"
            / f"{HISTORICAL_TERMINAL_RUN_ID}.correction-1.json")


def _historical_terminal_source_hashes() -> dict[str, str]:
    names = ("storage_policy.py", "storage_gate0.py", "storage_inventory.py", "build_retention.py")
    return {
        name: hashlib.sha256(_read_file_nofollow(Path(__file__).resolve().parent / name)).hexdigest()
        for name in names
    }


def _open_historical_terminal_closure_directory(*, create: bool = False) -> tuple[int, tuple[int, int]]:
    """Open the counted, owned closure directory and pin its current path identity."""
    path = _historical_terminal_closure_path().parent
    repo_info = REPO.lstat()
    if not stat.S_ISDIR(repo_info.st_mode) or repo_info.st_uid != os.getuid():
        raise OSError("repository root is not an owned real directory")
    directory_fd = _open_directory_nofollow(
        path, create=create, strict_directory_sync=create)
    try:
        info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_dev != repo_info.st_dev or stat.S_IMODE(info.st_mode) & 0o022):
            raise OSError("historical closure directory is not owned, non-writable, and counted")
        identity = (info.st_dev, info.st_ino)
        reopened_fd = _open_directory_nofollow(path)
        try:
            reopened = os.fstat(reopened_fd)
            if (reopened.st_dev, reopened.st_ino) != identity:
                raise OSError("historical closure directory path identity changed")
        finally:
            os.close(reopened_fd)
        return directory_fd, identity
    except BaseException:
        os.close(directory_fd)
        raise


def _read_historical_terminal_closure_file(
    directory_fd: int, expected_name: str, directory_identity: tuple[int, int],
) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    info = _stat_child_nofollow(directory_fd, expected_name)
    if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or getattr(info, "st_blocks", 0) * 512 > 1024 * 1024):
        return None, None, "historical terminal closure file identity or permissions are unsafe"
    try:
        record, raw, read_info = _read_json_at(directory_fd, expected_name, maximum_bytes=1024 * 1024)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return None, None, f"historical terminal closure cannot be read safely: {error}"
    after = _stat_child_nofollow(directory_fd, expected_name)
    if (after is None or _storage_entry_identity(after) != _storage_entry_identity(info)
            or _storage_entry_identity(read_info) != _storage_entry_identity(info)
            or raw != _canonical_pretty_json_bytes(record)):
        return None, None, "historical terminal closure changed or is not canonically encoded"
    if record.get("closure_directory_identity") != list(directory_identity):
        return None, None, "historical terminal closure directory identity is not bound to its certificate"
    return record, raw, None


def _is_supersedable_legacy_terminal_closure(
    record: Mapping[str, Any], raw: bytes, directory_identity: tuple[int, int],
) -> bool:
    """Recognize only the authenticated v2 writer artifact missing its path field."""
    if hashlib.sha256(raw).hexdigest() != HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256:
        return False
    historical_evidence = record.get("historical_evidence")
    evidence_hashes = (historical_evidence.get("sha256")
                       if isinstance(historical_evidence, Mapping) else None)
    if not isinstance(evidence_hashes, Mapping):
        return False
    expected_report = {
        "path": str(_historical_terminal_run_paths()["report"]),
        "bytes": HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES,
        "sha256": HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256,
    }
    return (
        record.get("schema_version") == 2
        and record.get("closure_policy") == "history-preserving-terminal-overrun-v1"
        and record.get("experiment_id") == HISTORICAL_TERMINAL_RUN_ID
        and "closure_path" not in record
        and record.get("storage_manifest_path") == str(_experiment_manifest_path(HISTORICAL_TERMINAL_RUN_ID))
        and record.get("storage_manifest_sha256") == evidence_hashes.get("storage_manifest")
        and record.get("original_reservation_bytes") == HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES
        and record.get("historical_failure_preserved") is True
        and record.get("authorizes_execution") is False
        and record.get("changes_original_gate_result") is False
        and record.get("counts_toward_project_scope") is True
        and record.get("closure_directory_identity") == list(directory_identity)
        and record.get("report_prefix") == expected_report
        and isinstance(record.get("source_sha256"), dict)
        and isinstance(record.get("gate0_control_sha256"), dict)
        and isinstance(record.get("tree_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", record["tree_sha256"]) is not None
        and _verify_storage_evidence(record, "closure_hmac_sha256")
        and bool(raw)
    )


def _read_historical_terminal_closure() -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    path = _historical_terminal_closure_path()
    correction_path = _historical_terminal_closure_correction_path()
    try:
        directory_fd, directory_identity = _open_historical_terminal_closure_directory()
    except FileNotFoundError:
        return None, None, "historical terminal closure is missing"
    except OSError as error:
        return None, None, f"historical terminal closure directory cannot be opened safely: {error}"
    try:
        expected_name = path.name
        correction_name = correction_path.name
        names = sorted(os.listdir(directory_fd))
        allowed_name_sets = ([expected_name], sorted([expected_name, correction_name]))
        if names not in allowed_name_sets:
            return None, None, "historical terminal closure directory has missing or unexpected entries"
        original, original_raw, original_error = _read_historical_terminal_closure_file(
            directory_fd, expected_name, directory_identity)
        if original is None or original_raw is None:
            return None, None, original_error
        if names == [expected_name]:
            if (original.get("schema_version") != 3
                    or original.get("closure_path") != str(path)
                    or original.get("closure_revision") != 0):
                return None, None, "historical terminal closure schema is not a current schema-v3 record"
            record, raw = original, original_raw
        else:
            correction, correction_raw, correction_error = _read_historical_terminal_closure_file(
                directory_fd, correction_name, directory_identity)
            if correction is None or correction_raw is None:
                return None, None, correction_error
            if (not _is_supersedable_legacy_terminal_closure(
                    original, original_raw, directory_identity)
                    or correction.get("schema_version") != 3
                    or correction.get("closure_revision") != 1
                    or correction.get("closure_path") != str(correction_path)
                    or correction.get("supersedes_closure_path") != str(path)
                    or correction.get("supersedes_closure_sha256")
                    != hashlib.sha256(original_raw).hexdigest()):
                return None, None, "historical terminal closure correction does not bind the superseded certificate"
            record, raw = correction, correction_raw
        names_after = sorted(os.listdir(directory_fd))
        directory_after = os.fstat(directory_fd)
        if (names_after != names
                or (directory_after.st_dev, directory_after.st_ino) != directory_identity):
            return None, None, "historical terminal closure directory changed while it was read"
        reopened_fd, reopened_identity = _open_historical_terminal_closure_directory()
        os.close(reopened_fd)
        if reopened_identity != directory_identity:
            return None, None, "historical terminal closure directory path changed while it was read"
        return record, raw, None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return None, None, f"historical terminal closure cannot be read safely: {error}"
    finally:
        os.close(directory_fd)


def _verify_historical_terminal_closure(
    manifest: Mapping[str, Any], manifest_sha256: str, output_dir: Path,
    *, allocated_bytes: int, metadata_tree_sha256: str,
    allowed_active_experiment_id: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    record, raw, read_error = _read_historical_terminal_closure()
    if record is None or raw is None:
        return None, [f"historical terminal closure for {HISTORICAL_TERMINAL_RUN_ID} is invalid: {read_error}"]
    try:
        expected_closure_path = (
            _historical_terminal_closure_correction_path()
            if record.get("closure_revision") == 1 else _historical_terminal_closure_path())
        if (record.get("schema_version") != 3
                or record.get("closure_policy") != "history-preserving-terminal-overrun-v1"
                or record.get("experiment_id") != HISTORICAL_TERMINAL_RUN_ID
                or record.get("closure_path") != str(expected_closure_path)
                or record.get("closure_revision") not in (0, 1)
                or record.get("storage_manifest_path") != str(_experiment_manifest_path(HISTORICAL_TERMINAL_RUN_ID))
                or record.get("storage_manifest_sha256") != manifest_sha256
                or record.get("original_reservation_bytes") != HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES
                or record.get("historical_failure_preserved") is not True
                or record.get("authorizes_execution") is not False
                or record.get("changes_original_gate_result") is not False
                or record.get("counts_toward_project_scope") is not True
                or record.get("closure_directory_identity") is None
                or record.get("report_prefix") != {
                    "path": str(_historical_terminal_run_paths()["report"]),
                    "bytes": HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES,
                    "sha256": HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256,
                }
                or not _verify_storage_evidence(record, "closure_hmac_sha256")):
            raise ValueError("closure schema, exact identity, original manifest binding, immutable reservation, or signature is invalid")
        if (manifest.get("experiment_id") != HISTORICAL_TERMINAL_RUN_ID
                or manifest.get("estimated_persistent_bytes") != HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES
                or hashlib.sha256(_read_file_nofollow(_experiment_manifest_path(
                    HISTORICAL_TERMINAL_RUN_ID))).hexdigest() != manifest_sha256):
            raise ValueError("the original storage manifest changed after terminal closure")
        evidence = _historical_terminal_evidence(report_prefix_binding=record.get("report_prefix"))
        if record.get("historical_evidence") != evidence:
            raise ValueError("one or more immutable run, cleanup, result, controller, or report records changed")
        if (record.get("output_dir") != str(output_dir)
                or record.get("metadata_output_allocated_bytes") != allocated_bytes
                or record.get("metadata_output_tree_sha256") != metadata_tree_sha256):
            raise ValueError("the signed output identity changed after terminal closure")
        liveness = _historical_terminal_liveness_check(
            evidence, allowed_active_experiment_id=allowed_active_experiment_id)
        if record.get("liveness_at_closure") != liveness:
            raise ValueError("historical liveness tools or quiescence evidence changed after closure")
        if record.get("source_sha256") != _historical_terminal_source_hashes():
            raise ValueError("historical closure source-control hashes changed")
        if record.get("gate0_control_sha256") != _gate0_control_hashes():
            raise ValueError("historical closure Gate 0 control hashes changed")
        current_inventory = _historical_terminal_tree_snapshot(output_dir)
        if current_inventory != record.get("tree_inventory"):
            raise ValueError("the closed historical tree changed after terminal closure")
        if (current_inventory.get("allocated_bytes") != record.get("closed_tree_allocated_bytes")
                or current_inventory.get("tree_sha256") != record.get("tree_sha256")
                or current_inventory.get("root_identity") != record.get("tree_root_identity")):
            raise ValueError("the closed tree no longer matches its signed identity and content digest")
        # Re-read all source records and the closure after measurement to fence races.
        evidence_after = _historical_terminal_evidence(report_prefix_binding=record.get("report_prefix"))
        latest_record, latest_raw, latest_error = _read_historical_terminal_closure()
        if (evidence_after != evidence or latest_error is not None or latest_record != record
                or latest_raw != raw):
            raise ValueError("historical evidence or closure changed while Gate 0 verified it")
        return {
            "closure_path": record["closure_path"],
            "closure_sha256": hashlib.sha256(raw).hexdigest(),
            "closure_tree_sha256": current_inventory["tree_sha256"],
            "closure_allocated_bytes": current_inventory["allocated_bytes"],
            "closure_liveness": liveness,
        }, []
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
        return None, [f"historical terminal closure for {HISTORICAL_TERMINAL_RUN_ID} failed verification: {error}"]


def close_historical_terminal_run() -> dict[str, Any]:
    """Append one authenticated, non-authorizing closure for the exact failed Z9 run."""
    original_closure_path = _historical_terminal_closure_path()
    correction_path = _historical_terminal_closure_correction_path()
    certification_lock = acquire_storage_certification_lock(exclusive=True)
    allocation_lock = None
    try:
        allocation_lock = _acquire_storage_allocation_lock()
        _historical_terminal_copy_controller_evidence()
        directory_fd, directory_identity = _open_historical_terminal_closure_directory(create=True)
        legacy_record: dict[str, Any] | None = None
        try:
            existing_names = sorted(os.listdir(directory_fd))
            superseded_raw: bytes | None = None
            if existing_names == []:
                closure_path = original_closure_path
                closure_revision = 0
            elif existing_names == [original_closure_path.name]:
                legacy, legacy_raw, legacy_error = _read_historical_terminal_closure_file(
                    directory_fd, original_closure_path.name, directory_identity)
                if (legacy is None or legacy_raw is None
                        or not _is_supersedable_legacy_terminal_closure(
                            legacy, legacy_raw, directory_identity)):
                    raise FileExistsError(
                        "historical terminal closure is append-only and the existing record cannot be superseded"
                        + (f": {legacy_error}" if legacy_error else ""))
                closure_path = correction_path
                closure_revision = 1
                superseded_raw = legacy_raw
                legacy_record = legacy
            else:
                raise FileExistsError("historical terminal closure is append-only and already exists")
        finally:
            os.close(directory_fd)
        evidence_before = _historical_terminal_evidence()
        liveness_before = _historical_terminal_liveness_check(evidence_before)
        tree_before = _historical_terminal_tree_snapshot(Path(evidence_before["paths"]["output_dir"]))
        tree_confirm = _historical_terminal_tree_snapshot(Path(evidence_before["paths"]["output_dir"]))
        evidence_confirm = _historical_terminal_evidence()
        liveness_confirm = _historical_terminal_liveness_check(evidence_confirm)
        if (tree_before != tree_confirm or evidence_before != evidence_confirm
                or liveness_before != liveness_confirm):
            raise RuntimeError("historical evidence, liveness, or tree changed between closure measurements")
        if superseded_raw is not None and (
                legacy_record is None
                or legacy_record.get("historical_evidence") != evidence_before
                or legacy_record.get("liveness_at_closure") != liveness_before
                or legacy_record.get("tree_inventory") != tree_before
                or legacy_record.get("tree_sha256") != tree_before.get("tree_sha256")
                or legacy_record.get("closed_tree_allocated_bytes") != tree_before.get("allocated_bytes")
                or legacy_record.get("closed_tree_logical_bytes") != tree_before.get("logical_bytes")
                or legacy_record.get("tree_root_identity") != tree_before.get("root_identity")
                or legacy_record.get("output_dir") != evidence_before["paths"]["output_dir"]
                or legacy_record.get("metadata_output_allocated_bytes") != HISTORICAL_TERMINAL_OUTPUT_BYTES
                or legacy_record.get("metadata_output_tree_sha256")
                != _persistent_output_snapshot(Path(evidence_before["paths"]["output_dir"]))[1]):
            raise RuntimeError(
                "legacy terminal closure does not match the independently remeasured evidence, liveness, or tree")

        source_hashes = _historical_terminal_source_hashes()
        report_binding = {
            "path": evidence_before["paths"]["report"],
            "bytes": evidence_before["report_prefix_bytes"],
            "sha256": evidence_before["report_prefix_sha256"],
        }
        record = {
            "schema_version": 3,
            "closure_policy": "history-preserving-terminal-overrun-v1",
            "experiment_id": HISTORICAL_TERMINAL_RUN_ID,
            "closure_path": str(closure_path),
            "closure_revision": closure_revision,
            "created_at_unix": time.time(),
            "storage_manifest_path": evidence_before["paths"]["storage_manifest"],
            "storage_manifest_sha256": evidence_before["sha256"]["storage_manifest"],
            "manifest_hmac_sha256": evidence_before["manifest_hmac_sha256"],
            "original_reservation_bytes": HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            "gate0_post_allocation_bytes": HISTORICAL_TERMINAL_POST_OUTPUT_BYTES,
            "gate0_post_allocation_provenance": evidence_before["gate0_post_allocation_provenance"],
            "gate0_post_status": evidence_before["gate0_post_status"],
            "gate0_post_blockers": evidence_before["gate0_post_blockers"],
            "run_result_status": evidence_before["run_status"],
            "r5_classification": evidence_before["r5_classification"],
            "t8_classification": evidence_before["t8_classification"],
            "finder_treatment": evidence_before["finder_treatment"],
            "historical_failure_preserved": True,
            "authorizes_execution": False,
            "changes_original_gate_result": False,
            "counts_toward_project_scope": True,
            "closure_directory_identity": list(directory_identity),
            "report_prefix": report_binding,
            "historical_evidence": evidence_before,
            "output_dir": evidence_before["paths"]["output_dir"],
            "metadata_output_allocated_bytes": HISTORICAL_TERMINAL_OUTPUT_BYTES,
            "metadata_output_tree_sha256": _persistent_output_snapshot(
                Path(evidence_before["paths"]["output_dir"]))[1],
            "tree_root_identity": tree_before["root_identity"],
            "tree_parent_identity": tree_before["parent_identity"],
            "tree_inventory": tree_before,
            "tree_sha256": tree_before["tree_sha256"],
            "closed_tree_allocated_bytes": tree_before["allocated_bytes"],
            "closed_tree_logical_bytes": tree_before["logical_bytes"],
            "liveness_at_closure": liveness_before,
            "source_sha256": source_hashes,
            "gate0_control_sha256": _gate0_control_hashes(),
            "closure_reason": "Preserve the authenticated terminal FAIL/BLOCKED result while permitting exact historical allocation accounting.",
        }
        if superseded_raw is not None:
            record.update({
                "supersedes_closure_path": str(original_closure_path),
                "supersedes_closure_sha256": hashlib.sha256(superseded_raw).hexdigest(),
                "supersedes_closure_reason": (
                    "The original authenticated schema-v2 append omitted its required closure_path field; "
                    "it is retained unchanged and superseded by this independently bound correction."
                ),
            })
        sealed = _seal_storage_evidence(record, "closure_hmac_sha256")
        raw = _canonical_pretty_json_bytes(sealed)
        directory_fd, write_directory_identity = _open_historical_terminal_closure_directory()
        try:
            if write_directory_identity != directory_identity:
                raise RuntimeError("historical closure directory was substituted before append")
            expected_existing_names = [original_closure_path.name] if closure_revision == 1 else []
            if sorted(os.listdir(directory_fd)) != expected_existing_names:
                raise RuntimeError("historical terminal closure directory changed before append")
            descriptor = os.open(closure_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
            try:
                offset = 0
                while offset < len(raw):
                    count = os.write(descriptor, raw[offset:])
                    if count <= 0:
                        raise OSError("short write while appending historical terminal closure")
                    offset += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        readback, readback_raw, readback_error = _read_historical_terminal_closure()
        evidence_after = _historical_terminal_evidence(report_prefix_binding=report_binding)
        liveness_after = _historical_terminal_liveness_check(evidence_after)
        tree_after = _historical_terminal_tree_snapshot(Path(evidence_after["paths"]["output_dir"]))
        if (readback_error is not None or readback != sealed or readback_raw != raw
                or evidence_after != evidence_before or liveness_after != liveness_before
                or tree_after != tree_before):
            # Keep any partial or invalid append in place; the verifier fails closed and never rebases it.
            raise RuntimeError("historical closure failed its durable post-write revalidation")
        return {
            "status": "CLOSED",
            "experiment_id": HISTORICAL_TERMINAL_RUN_ID,
            "closure_path": str(closure_path),
            "closure_sha256": hashlib.sha256(raw).hexdigest(),
            "tree_sha256": tree_before["tree_sha256"],
            "allocated_bytes": tree_before["allocated_bytes"],
            "original_reservation_bytes": HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            "gate0_post_status": evidence_before["gate0_post_status"],
        }
    finally:
        _release_storage_allocation_lock(allocation_lock)
        release_storage_certification_lock(certification_lock)


def _authorized_runner_records(paths: list[str], *, requires_managed_prefix: bool) -> dict[str, str]:
    """Hash admitted runners and require the managed process path to be reachable from main."""
    records: dict[str, str] = {}
    template_setup = (REPO / "experiments/storage/initialize_prefix_template.py").resolve()
    for raw_path in paths:
        path = Path(raw_path)
        source = _read_file_nofollow(path)
        digest = hashlib.sha256(source).hexdigest()
        if requires_managed_prefix and path.resolve(strict=True) != template_setup:
            try:
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as error:
                raise ValueError(f"authorized prefix runner is not valid Python source: {path}: {error}") from error
            top_level_functions = {node.name: node for node in tree.body
                                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            main = top_level_functions.get("main")
            scoped_withs = []
            if main is not None:
                for node in ast.walk(main):
                    if not isinstance(node, (ast.With, ast.AsyncWith)):
                        continue
                    if any(isinstance(item.context_expr, ast.Call)
                           and isinstance(item.context_expr.func, ast.Name)
                           and item.context_expr.func.id == "scoped_lease"
                           for item in node.items):
                        scoped_withs.append(node)
            managed_entrypoints: set[str] = set()
            for node in scoped_withs:
                for child in ast.walk(node):
                    if (isinstance(child, ast.Return) and isinstance(child.value, ast.Call)
                            and isinstance(child.value.func, ast.Name)):
                        managed_entrypoints.add(child.value.func.id)
            reachable_managed_entrypoint = any(
                name in top_level_functions
                and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "start_wine_process"
                        for call in ast.walk(top_level_functions[name]))
                and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "wait_process"
                        for call in ast.walk(top_level_functions[name]))
                for name in managed_entrypoints
            )
            imported_modules: dict[str, str] = {}
            imported_functions: dict[str, tuple[str, str]] = {}
            process_modules = {"subprocess", "os", "posix", "asyncio", "builtins"}
            # These allowlisted stdlib modules expose their imported `os`
            # module as a public attribute. Resolve those names like direct
            # imports so an admitted helper cannot reach raw process APIs via
            # e.g. `shutil.os.system` or `pathlib.os.spawn*`.
            process_exposing_modules = {"storage_policy", "build_retention", "shutil", "pathlib"}
            process_exposing_module_names = process_modules | {"sys", "ctypes"}
            allowed_runner_imports = {
                "hashlib", "json", "os", "pathlib", "shutil", "subprocess", "sys", "time",
                "storage_policy",
            }
            allowed_storage_policy_imports = {
                "PREFIX_TEMPLATE", "PrefixLease", "begin_experiment", "assert_prefix_lease",
                "open_canonical_artifact", "current_lease", "make_experiment_id", "scoped_lease",
            }
            forbidden_imports = {"multiprocessing", "pty", "ctypes", "cffi", "importlib", "runpy",
                                 "operator", "inspect", "gc", "pickle", "marshal", "posix", "concurrent"}
            forbidden_reflection_names = {
                "__dict__", "__getattribute__", "__getattr__", "__globals__", "__builtins__",
                "__subclasses__", "__mro__", "__bases__", "__base__", "__class__",
            }
            untracked_process_creation = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        imported_modules[alias.asname or root] = alias.name
                        if root in forbidden_imports or alias.name not in allowed_runner_imports:
                            untracked_process_creation = True
                elif isinstance(node, ast.ImportFrom):
                    if node.level != 0 or node.module is None:
                        untracked_process_creation = True
                    if node.module:
                        import_root = node.module.split(".")[0]
                        if node.module != "__future__" and (
                                import_root not in allowed_runner_imports or node.module not in allowed_runner_imports):
                            untracked_process_creation = True
                        for alias in node.names:
                            local_name = alias.asname or alias.name
                            imported_functions[local_name] = (node.module, alias.name)
                            if alias.name == "*":
                                untracked_process_creation = True
                            if (node.module == "storage_policy"
                                    and alias.name not in allowed_storage_policy_imports):
                                untracked_process_creation = True
                            if (node.module != "__future__"
                                    and alias.name.startswith("_")
                                    and alias.name not in {"__future__"}):
                                untracked_process_creation = True
                            if alias.name in process_exposing_module_names:
                                # Standard-library modules and project helpers may
                                # re-export raw process modules as public globals.
                                imported_modules[local_name] = alias.name
                            if alias.name in forbidden_reflection_names:
                                untracked_process_creation = True
                            if (node.module.split(".")[0] in process_exposing_modules
                                    and alias.name.startswith("_")):
                                untracked_process_creation = True
                            # These project helpers import and expose process modules
                            # as globals. Treat `from storage_policy import
                            # subprocess as proc` exactly like `import subprocess`.
                            if (node.module.split(".")[0] in process_exposing_modules
                                    and alias.name in process_exposing_module_names):
                                imported_modules[local_name] = alias.name
                                if alias.name in {"sys", "ctypes"}:
                                    untracked_process_creation = True
                        if node.module.split(".")[0] in forbidden_imports:
                            untracked_process_creation = True

            # Resolve simple aliases conservatively.  Do not let a shadowing
            # assignment in one function erase a module import used by another.
            alias_modules: dict[str, str] = {}
            alias_functions: dict[str, tuple[str, str]] = {}
            dynamic_dispatch_names = {
                "getattr", "setattr", "delattr", "__import__", "eval", "exec",
                "compile", "vars", "globals", "locals",
            }

            def resolve_module(name: str) -> str | None:
                return imported_modules.get(name, alias_modules.get(name))

            def assigned_process_api(value: ast.AST) -> tuple[str, str] | None:
                if isinstance(value, ast.Name):
                    module = resolve_module(value.id)
                    if module is not None:
                        return ("module", module)
                    if value.id in dynamic_dispatch_names:
                        return ("builtins", value.id)
                    return imported_functions.get(value.id, alias_functions.get(value.id))
                if (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                        and resolve_module(value.value.id) is not None):
                    return (resolve_module(value.value.id) or "", value.attr)
                return None

            changed = True
            while changed:
                changed = False
                assignments = [node for node in ast.walk(tree)
                               if isinstance(node, (ast.Assign, ast.AnnAssign))]
                for assignment in assignments:
                    value = assignment.value
                    if value is None:
                        continue
                    api = assigned_process_api(value)
                    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                    names = [target.id for target in targets if isinstance(target, ast.Name)]
                    for name in names:
                        if api is not None and api[0] == "module":
                            if alias_modules.get(name) != api[1]:
                                alias_modules[name] = api[1]
                                changed = True
                        elif api is not None and alias_functions.get(name) != api:
                            alias_functions[name] = api
                            changed = True

            def resolve_process_api(function: ast.AST) -> tuple[str, str] | None:
                if isinstance(function, ast.Name):
                    module = resolve_module(function.id)
                    if module is not None:
                        return ("module", module)
                    imported = imported_functions.get(function.id, alias_functions.get(function.id))
                    if (imported is not None
                            and imported[0].split(".")[0] in process_exposing_modules
                            and imported[1] in process_exposing_module_names):
                        return ("module", imported[1])
                    if function.id in {"subprocess", "os", "posix", "asyncio", "builtins"}:
                        return ("module", function.id)
                    if function.id in dynamic_dispatch_names:
                        return ("builtins", function.id)
                    return imported_functions.get(function.id, alias_functions.get(function.id))
                if isinstance(function, ast.Attribute):
                    parent = resolve_process_api(function.value)
                    if parent is None:
                        return None
                    if parent[0] == "module":
                        if (parent[1] in process_exposing_modules
                                and function.attr in process_exposing_module_names):
                            return ("module", function.attr)
                        return parent[1], function.attr
                    # Calling a method hanging from an imported process API (for
                    # example Popen.__new__) remains equivalent to using that API.
                    if parent[0] in {"subprocess", "os", "posix", "asyncio", "builtins"}:
                        return parent
                return None

            def process_api(call: ast.Call) -> tuple[str, str] | None:
                return resolve_process_api(call.func)

            def unsafe_process_api(api: tuple[str, str] | None) -> bool:
                if api is None:
                    return False
                module, name = api
                return (
                    (module == "subprocess" and name in {
                        "Popen", "run", "call", "check_call", "check_output",
                        "getoutput", "getstatusoutput"})
                    or (module == "os" and (name in {
                        "system", "popen", "fork", "forkpty", "vfork", "startfile",
                        "posix_spawn", "posix_spawnp", "link"}
                        or name.startswith(("spawn", "exec"))))
                    or (module == "posix" and (name in {
                        "system", "popen", "fork", "forkpty", "vfork", "posix_spawn", "posix_spawnp"}
                        or name.startswith(("spawn", "exec"))))
                    or (module == "asyncio" and name.startswith("create_subprocess"))
                    or (module == "builtins" and name in dynamic_dispatch_names)
                    or (module == "sys" and name in {
                        "_getframe", "getframe", "_current_frames", "current_frames",
                        "settrace", "setprofile", "gettrace", "getprofile"})
                )

            def module_root(expression: ast.AST) -> str | None:
                """Follow module and imported process-API expressions to their root."""
                if isinstance(expression, ast.Name):
                    imported = imported_functions.get(expression.id, alias_functions.get(expression.id))
                    if (imported is not None
                            and imported[0].split(".")[0] in process_exposing_modules
                            and imported[1] in process_exposing_module_names):
                        return imported[1]
                    if imported is not None and imported[1] in process_exposing_module_names:
                        return imported[1]
                    if imported is not None and imported[0].split(".")[0] in process_modules | {"sys"}:
                        return imported[0].split(".")[0]
                    module = resolve_module(expression.id)
                    if module is not None:
                        return module.split(".")[0]
                    if expression.id in process_modules | {"sys"}:
                        return expression.id
                    return None
                if isinstance(expression, ast.Attribute):
                    parent = module_root(expression.value)
                    if expression.attr in process_exposing_module_names:
                        return expression.attr
                    return parent
                if isinstance(expression, ast.Subscript):
                    return module_root(expression.value)
                return None

            def is_sys_modules_expression(expression: ast.AST) -> bool:
                if isinstance(expression, ast.Name):
                    imported = imported_functions.get(expression.id, alias_functions.get(expression.id))
                    return imported == ("sys", "modules")
                return (isinstance(expression, ast.Attribute)
                        and expression.attr == "modules"
                        and module_root(expression.value) == "sys")

            timeout_handlers = [node for node in ast.walk(tree)
                                if isinstance(node, ast.ExceptHandler)
                                and isinstance(node.type, ast.Attribute)
                                and node.type.attr == "TimeoutExpired"]
            timeout_reap_calls = {id(child) for handler in timeout_handlers for child in ast.walk(handler)
                                  if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                  and child.func.attr == "wait"}
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.Delete)):
                    targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                    if any(isinstance(child, ast.Attribute)
                           for target in targets for child in ast.walk(target)):
                        # Runner code may call PrefixLease methods, but it may not
                        # redirect cleanup, template, prefix, or output paths.
                        untracked_process_creation = True
                if isinstance(node, ast.Attribute):
                    root = module_root(node.value)
                    if node.attr.startswith("_"):
                        # Runner code may use the public PrefixLease surface only;
                        # private state must not be writable to replace a sealed env.
                        untracked_process_creation = True
                    if (node.attr in forbidden_reflection_names
                            or node.attr.startswith("__") or node.attr.endswith("__")):
                        untracked_process_creation = True
                    if root in allowed_runner_imports and node.attr.startswith("_"):
                        untracked_process_creation = True
                    if ((root in process_exposing_modules and node.attr in {"sys", "ctypes"})
                            or root == "ctypes"):
                        untracked_process_creation = True
                    if ((root in process_modules | {"sys"} and node.attr in {
                            "__dict__", "__getattribute__", "__getattr__", "__globals__", "__builtins__"})
                            or (root in process_exposing_modules and node.attr.startswith("__"))
                            or (root == "sys" and node.attr == "modules")):
                        untracked_process_creation = True
                    if unsafe_process_api(resolve_process_api(node)):
                        # Reject obtaining a constructor or raw process API as a
                        # value, even when it is passed to a lambda/partial and
                        # the eventual call target is no longer statically named.
                        untracked_process_creation = True
                    if (root == "sys" and node.attr in {
                            "_getframe", "getframe", "_current_frames", "settrace", "setprofile"}):
                        untracked_process_creation = True
                if isinstance(node, ast.Name) and unsafe_process_api(resolve_process_api(node)):
                    untracked_process_creation = True
                if isinstance(node, ast.Name) and node.id in forbidden_reflection_names:
                    untracked_process_creation = True
                if isinstance(node, ast.Subscript):
                    if module_root(node.value) in process_modules or is_sys_modules_expression(node.value):
                        untracked_process_creation = True
                if (isinstance(node, ast.Name) and
                        is_sys_modules_expression(node)):
                    untracked_process_creation = True
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in dynamic_dispatch_names:
                    untracked_process_creation = True
                if isinstance(node.func, ast.Subscript):
                    untracked_process_creation = True
                api = process_api(node)
                if api is None:
                    if (isinstance(node.func, ast.Attribute) and node.func.attr == "wait"
                            and id(node) not in timeout_reap_calls):
                        untracked_process_creation = True
                    continue
                module, name = api
                if module == "builtins" and name in dynamic_dispatch_names:
                    untracked_process_creation = True
                if unsafe_process_api((module, name)):
                    untracked_process_creation = True
            if not (main is not None and len(scoped_withs) == 1
                    and reachable_managed_entrypoint and not untracked_process_creation):
                raise ValueError(
                    "authorized prefix runner main() must return a process-owning helper from one "
                    "scoped_lease block, and that helper must use PrefixLease.start_wine_process and "
                    f"PrefixLease.wait_process without unbounded/raw process creation or waits: {path}"
                )
        records[raw_path] = digest
    return records


def _manifest_signing_key(*, create: bool = False) -> bytes:
    """Load the per-user HMAC key used to detect edited or hand-forged manifests."""
    key_path = MANIFEST_SIGNING_KEY_PATH
    if create:
        parent_fd = _open_directory_nofollow(
            key_path.parent, create=True, strict_directory_sync=True)
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(key_path.name, flags, 0o600, dir_fd=parent_fd)
            except FileExistsError:
                descriptor = None
            if descriptor is not None:
                os.fchmod(descriptor, 0o600)
                secret = secrets.token_bytes(32)
                written = 0
                while written < len(secret):
                    count = os.write(descriptor, secret[written:])
                    if count <= 0:
                        raise OSError("short write while creating manifest-signing key")
                    written += count
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = None
                os.fsync(parent_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)

    parent_fd = _open_directory_nofollow(key_path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(key_path.name, flags, dir_fd=parent_fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1):
            raise RuntimeError("manifest-signing key must be a private, singly-linked regular file with mode 0600")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("manifest-signing key is not owned by the current user")
        secret = os.read(descriptor, 33)
    except OSError as error:
        raise RuntimeError(f"manifest-signing key is missing or unsafe: {key_path}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    if len(secret) != 32:
        raise RuntimeError("manifest-signing key must contain exactly 32 bytes")
    return secret


def _acquire_storage_allocation_lock():
    """Serialize build and prefix footprint admission across separate controllers."""
    root_fd = _open_directory_nofollow(CACHE, create=True, strict_directory_sync=True)
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(".storage-allocation.lock", flags, 0o600, dir_fd=root_fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise RuntimeError("shared storage allocation lock is not a private regular file")
        os.fsync(root_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return os.fdopen(descriptor, "a+")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(root_fd)


def _release_storage_allocation_lock(lock_file: Any) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def acquire_storage_certification_lock(*, exclusive: bool = True, blocking: bool = True) -> Any:
    """Acquire the storage certification lock to serialize live certification against synthetic tests."""
    root_fd = _open_directory_nofollow(CACHE, create=True, strict_directory_sync=True)
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(".storage-certification.lock", flags, 0o600, dir_fd=root_fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
            raise RuntimeError("storage certification lock is not a private regular file")
        os.fsync(root_fd)
        lock_mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        if not blocking:
            lock_mode |= fcntl.LOCK_NB
        fcntl.flock(descriptor, lock_mode)
        return os.fdopen(descriptor, "a+")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(root_fd)


def release_storage_certification_lock(lock_file: Any) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


@contextlib.contextmanager
def storage_certification_guard(*, exclusive: bool = True, blocking: bool = True):
    lock = acquire_storage_certification_lock(exclusive=exclusive, blocking=blocking)
    try:
        yield lock
    finally:
        release_storage_certification_lock(lock)


def _manifest_signature(manifest: Mapping[str, Any], *, create_key: bool = False) -> str:
    payload = dict(manifest)
    payload.pop("manifest_hmac_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(_manifest_signing_key(create=create_key), canonical, hashlib.sha256).hexdigest()


def _verify_manifest_signature(manifest: Mapping[str, Any]) -> bool:
    signature = manifest.get("manifest_hmac_sha256")
    if not isinstance(signature, str):
        return False
    try:
        expected = _manifest_signature(manifest)
    except (OSError, RuntimeError):
        return False
    return hmac.compare_digest(signature, expected)

def _storage_evidence_signature(record: Mapping[str, Any], signature_field: str) -> str:
    payload = dict(record)
    payload.pop(signature_field, None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(_manifest_signing_key(), canonical, hashlib.sha256).hexdigest()

def _seal_storage_evidence(record: Mapping[str, Any], signature_field: str) -> dict[str, Any]:
    sealed = dict(record)
    sealed.pop(signature_field, None)
    sealed[signature_field] = _storage_evidence_signature(sealed, signature_field)
    return sealed

def _verify_storage_evidence(record: Mapping[str, Any], signature_field: str) -> bool:
    actual = record.get(signature_field)
    if not isinstance(actual, str):
        return False
    try:
        return hmac.compare_digest(actual, _storage_evidence_signature(record, signature_field))
    except (OSError, RuntimeError):
        return False


def _validated_manifest_estimates(manifest: Mapping[str, Any]) -> dict[str, int]:
    """Reject missing, inconsistent, or non-integer limits before trusting a lease."""
    fields = (
        "expected_duration_seconds", "expected_prefix_bytes", "expected_build_bytes",
        "estimated_persistent_bytes", "estimated_max_new_disk_bytes",
        "estimated_uncertainty_margin_bytes", "estimated_projected_new_disk_bytes",
        "project_budget_bytes",
    )
    values: dict[str, int] = {}
    for name in fields:
        value = manifest.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"storage manifest field {name} must be an integer")
        values[name] = value
    duration = values["expected_duration_seconds"]
    prefix_bytes = values["expected_prefix_bytes"]
    build_bytes = values["expected_build_bytes"]
    persistent_bytes = values["estimated_persistent_bytes"]
    if duration <= 0 or prefix_bytes < 0 or build_bytes < 0 or persistent_bytes < 0:
        raise ValueError("storage manifest contains a non-positive duration or negative estimate")
    if manifest.get("expected_prefix_requirement") is not (prefix_bytes > 0):
        raise ValueError("storage manifest prefix requirement does not match its byte estimate")
    if manifest.get("expected_build_requirement") is not (build_bytes > 0):
        raise ValueError("storage manifest build requirement does not match its byte estimate")
    if values["estimated_max_new_disk_bytes"] != prefix_bytes + build_bytes + persistent_bytes:
        raise ValueError("storage manifest peak estimate does not equal its component estimates")
    expected_margin = admission_uncertainty_margin_bytes(values["estimated_max_new_disk_bytes"])
    if values["estimated_uncertainty_margin_bytes"] != expected_margin:
        raise ValueError("storage manifest uncertainty margin differs from the installed policy")
    if values["estimated_projected_new_disk_bytes"] != values["estimated_max_new_disk_bytes"] + expected_margin:
        raise ValueError("storage manifest projected peak omits or changes its uncertainty margin")
    if values["project_budget_bytes"] != PROJECT_BUDGET:
        raise ValueError("storage manifest project budget differs from the installed policy")
    if prefix_bytes > NORMAL_TEMPORARY_TARGET and not manifest.get("storage_justification"):
        raise ValueError("storage manifest prefix estimate exceeds the temporary target without justification")
    if build_bytes > 5 * GIB and not manifest.get("storage_justification"):
        raise ValueError("storage manifest build estimate exceeds the build target without justification")
    if persistent_bytes >= NORMAL_PERSISTENT_TARGET and not manifest.get("storage_justification"):
        raise ValueError("storage manifest persistent estimate exceeds the output target without justification")
    growth_approval = manifest.get("persistent_growth_over_2g_approved", False)
    growth_reason = manifest.get("persistent_growth_over_2g_approval_reason")
    if not isinstance(growth_approval, bool):
        raise ValueError("storage manifest growth approval must be an explicit boolean")
    if growth_approval and (not isinstance(growth_reason, str) or not growth_reason.strip()):
        raise ValueError("storage manifest growth approval requires a written reason")
    if not growth_approval and growth_reason is not None:
        raise ValueError("storage manifest growth approval reason is present without approval")
    return values


def _gate0_signature(attestation: Mapping[str, Any]) -> str:
    payload = dict(attestation)
    payload.pop("gate0_hmac_sha256", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(_manifest_signing_key(), canonical, hashlib.sha256).hexdigest()


def _verify_gate0_signature(attestation: Mapping[str, Any]) -> bool:
    signature = attestation.get("gate0_hmac_sha256")
    if not isinstance(signature, str):
        return False
    try:
        return hmac.compare_digest(signature, _gate0_signature(attestation))
    except (OSError, RuntimeError):
        return False


def _foreign_active_builds(active_builds: list[Mapping[str, Any]],
                           active_experiment_id: str | None) -> list[Mapping[str, Any]]:
    """Return build leases whose reserved peak is not included in this run's manifest."""
    return [row for row in active_builds
            if active_experiment_id is None or row.get("owner_experiment") != active_experiment_id]


def _gate0_control_hashes() -> dict[str, str]:
    names = (
        "storage_policy.py", "prefix_preflight.py", "storage_inventory.py",
        "artifactize_builds.py", "build_retention.py", "storage_gate0.py",
        "test_build_retention.py", "test_storage_policy.py", "smoke_prefix_lease.py",
        "consolidate_evidence.py", "test_evidence_consolidation.py",
        "test_wine_launcher_policy.py", "test_g0c_checks.py", "test_storage_recovery.py",
    )
    result: dict[str, str] = {}
    for name in names:
        path = Path(__file__).resolve().parent / name
        result[name] = hashlib.sha256(_read_file_nofollow(path)).hexdigest()
    return result


def _attested_smoke_evidence_blockers(attestation: Mapping[str, Any]) -> list[str]:
    """Reopen and revalidate every PrefixLease smoke object after finalization."""
    blockers: list[str] = []
    smoke = attestation.get("prefixlease_smoke")
    if not isinstance(smoke, Mapping):
        return ["final Gate 0 attestation lacks authenticated PrefixLease smoke evidence"]
    smoke_id = smoke.get("experiment_id")
    if not _is_storage_policy_smoke_id(smoke_id):
        return ["final Gate 0 smoke evidence has an invalid run identity"]
    output = REPO / "experiments/storage/smoke-runs" / str(smoke_id)
    report_path = output / "prefix-smoke-report.json"
    cleanup_path = output / "prefix-cleanup.json"
    preflight_path = output / "gate0-preflight-attestation.json"
    try:
        if smoke.get("report_path") != str(report_path):
            raise RuntimeError("attested smoke report path is not canonical")
        report_bytes = _read_file_nofollow(report_path)
        if hashlib.sha256(report_bytes).hexdigest() != smoke.get("report_sha256"):
            raise RuntimeError("attested PrefixLease smoke report is missing or changed")
        report = json.loads(report_bytes)
        if (not _verify_storage_evidence(report, "smoke_report_hmac_sha256")
                or report.get("experiment_id") != smoke_id or report.get("status") != "PASS"
                or report.get("graphics_experiments_run") is not False
                or report.get("wine_app_launched") is not False):
            raise RuntimeError("attested PrefixLease smoke report signature or contract failed")
        manifest_path = _experiment_manifest_path(str(smoke_id))
        manifest_bytes = _read_file_nofollow(manifest_path)
        if (smoke.get("manifest_path") != str(manifest_path)
                or smoke.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
                or report.get("manifest_path") != str(manifest_path)
                or report.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
            raise RuntimeError("attested smoke manifest is missing or changed")
        manifest = json.loads(manifest_bytes)
        if (not _verify_manifest_signature(manifest) or manifest.get("experiment_id") != smoke_id
                or manifest.get("decision") != "ALLOW" or manifest.get("storage_policy_smoke") is not True):
            raise RuntimeError("attested storage-smoke manifest signature or contract failed")
        if smoke.get("preflight_attestation_path") != str(preflight_path):
            raise RuntimeError("smoke evidence does not point to its canonical preflight attestation")
        preflight_bytes = _read_file_nofollow(preflight_path)
        preflight_hash = hashlib.sha256(preflight_bytes).hexdigest()
        if (smoke.get("preflight_attestation_sha256") != preflight_hash
                or report.get("preflight_attestation_path") != str(preflight_path)
                or report.get("preflight_attestation_sha256") != preflight_hash
                or manifest.get("gate0_attestation_sha256") != preflight_hash):
            raise RuntimeError("PrefixLease smoke is not bound to the retained preflight attestation")
        preflight = json.loads(preflight_bytes)
        if (not _verify_gate0_signature(preflight) or preflight.get("status") != "PREFLIGHT_PASS"
                or preflight.get("mode") != "preflight"
                or preflight.get("permitted_smoke_experiment_id") != smoke_id
                or preflight.get("prefixlease_smoke_status") != "PENDING"):
            raise RuntimeError("retained smoke preflight attestation is invalid or authorizes another run")
        claim_path = _storage_smoke_claim_path(str(smoke_id))
        claim, claim_bytes = _read_storage_smoke_claim(str(smoke_id))
        claim_hash = hashlib.sha256(claim_bytes).hexdigest()
        if (claim.get("preflight_attestation_sha256") != preflight_hash
                or manifest.get("smoke_nonce_claim_path") != str(claim_path)
                or manifest.get("smoke_nonce_claim_sha256") != claim_hash
                or report.get("smoke_nonce_claim_path") != str(claim_path)
                or report.get("smoke_nonce_claim_sha256") != claim_hash
                or smoke.get("smoke_nonce_claim_path") != str(claim_path)
                or smoke.get("smoke_nonce_claim_sha256") != claim_hash):
            raise RuntimeError("storage-smoke nonce claim is missing or changed after the one-use claim")
        if (smoke.get("cleanup_receipt_path") != str(cleanup_path)
                or report.get("cleanup_receipt") != str(cleanup_path)
                or smoke.get("cleanup_receipt_sha256") != hashlib.sha256(_read_file_nofollow(cleanup_path)).hexdigest()
                or report.get("cleanup_receipt_sha256") != hashlib.sha256(_read_file_nofollow(cleanup_path)).hexdigest()):
            raise RuntimeError("attested PrefixLease cleanup receipt is missing or changed")
        cleanup = json.loads(_read_file_nofollow(cleanup_path))
        if (not _verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
                or cleanup.get("experiment_id") != smoke_id or cleanup.get("status") != "PASS"
                or cleanup.get("prefix_deleted") is not True
                or cleanup.get("prefix_preserved") is not False
                or cleanup.get("receipt_finalization_complete") is not True):
            raise RuntimeError("attested PrefixLease cleanup signature or deletion proof failed")
        times = [preflight.get("created_at_unix"), claim.get("claimed_at_unix"),
                 manifest.get("created_at_unix"),
                 _cleanup_receipt_timestamp(cleanup), report.get("created_at_unix"),
                 attestation.get("created_at_unix")]
        if (any(not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value)) for value in times)
                or any(float(left) > float(right) for left, right in zip(times, times[1:]))):
            raise RuntimeError("PrefixLease smoke evidence timestamps are invalid or out of order")
    except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as error:
        blockers.append(f"PrefixLease smoke evidence no longer verifies: {error}")
    return blockers


def _prefix_lease_accounting_blockers(
    receipt: Mapping[str, Any], *,
    current_free_bytes: int,
    current_project_bytes: int,
    current_nonlease_project_bytes: int,
    attested_nonlease_project_bytes: int,
    current_prefix_allocated_bytes: int,
    current_output_allocated_bytes: int,
    attested_free_bytes: int,
    other_active_lease_count: int = 0,
    expected_binding: Mapping[str, Any] | None = None,
) -> list[str]:
    """Apply the signed lease rebase without crediting unrelated project growth."""
    blockers: list[str] = []
    if not _verify_storage_evidence(receipt, "prefix_materialization_hmac_sha256"):
        return ["active PrefixLease materialization receipt is unauthenticated"]
    if expected_binding and any(receipt.get(key) != value for key, value in expected_binding.items()):
        blockers.append("active PrefixLease materialization receipt names a stale or different lease")
    if other_active_lease_count:
        blockers.append("another or interrupted PrefixLease is active")
    if current_nonlease_project_bytes != attested_nonlease_project_bytes:
        blockers.append("unrelated non-lease storage changed since Gate 0 certification")
    gate0_nonlease = receipt.get("gate0_attested_nonlease_scope_bytes")
    if (not isinstance(gate0_nonlease, int) or isinstance(gate0_nonlease, bool)
            or gate0_nonlease < 0 or gate0_nonlease != attested_nonlease_project_bytes):
        blockers.append("PrefixLease materialization receipt has a stale Gate 0 storage baseline")
    pre_nonlease = receipt.get("pre_materialization_nonlease_scope_bytes")
    post_nonlease = receipt.get("post_materialization_nonlease_scope_bytes")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in (pre_nonlease, post_nonlease)):
        blockers.append("PrefixLease materialization receipt has invalid non-lease accounting")
    else:
        if pre_nonlease != gate0_nonlease:
            blockers.append("unrelated non-lease storage changed between Gate 0 and PrefixLease materialization")
        if pre_nonlease != post_nonlease:
            blockers.append("unrelated non-lease storage changed during PrefixLease materialization")
        if current_nonlease_project_bytes != post_nonlease:
            blockers.append("unrelated non-lease storage changed after the PrefixLease materialization rebase")
    expected_prefix_bytes = receipt.get("expected_prefix_bytes")
    materialized_prefix_bytes = receipt.get("materialized_prefix_allocated_bytes")
    if (not isinstance(expected_prefix_bytes, int) or isinstance(expected_prefix_bytes, bool)
            or expected_prefix_bytes < 0
            or not isinstance(materialized_prefix_bytes, int)
            or isinstance(materialized_prefix_bytes, bool)
            or materialized_prefix_bytes < 0
            or current_prefix_allocated_bytes != materialized_prefix_bytes
            or current_prefix_allocated_bytes > expected_prefix_bytes):
        blockers.append("materialized PrefixLease allocation no longer matches its signed reservation receipt")
    output_baseline = receipt.get("post_materialization_output_allocated_bytes")
    output_reservation = receipt.get("estimated_persistent_bytes")
    if (not isinstance(output_baseline, int) or isinstance(output_baseline, bool)
            or output_baseline < 0
            or not isinstance(output_reservation, int) or isinstance(output_reservation, bool)
            or output_reservation < 0
            or current_output_allocated_bytes < output_baseline
            or current_output_allocated_bytes > output_reservation):
        blockers.append("current-run output growth exceeds its signed persistent-output reservation")
    pre_free = receipt.get("pre_materialization_free_bytes")
    post_free = receipt.get("post_materialization_free_bytes")
    if (not isinstance(pre_free, int) or isinstance(pre_free, bool)
            or not isinstance(post_free, int) or isinstance(post_free, bool)
            or abs(pre_free - attested_free_bytes) > GATE0_FREE_DRIFT_TOLERANCE_BYTES
            or abs(current_free_bytes - post_free) > GATE0_FREE_DRIFT_TOLERANCE_BYTES):
        blockers.append("current free-space change exceeds the pre/post PrefixLease accounting bounds")
    if current_project_bytes > GATE0_PROJECT_BUDGET:
        blockers.append(f"current FG-Metal storage exceeds the {GATE0_PROJECT_BUDGET // GIB} GiB Gate 0 project budget ({current_project_bytes})")
    estimates = {
        "estimated_projected_new_disk_bytes": receipt.get("estimated_projected_new_disk_bytes"),
    }
    if (not isinstance(estimates["estimated_projected_new_disk_bytes"], int)
            or isinstance(estimates["estimated_projected_new_disk_bytes"], bool)
            or estimates["estimated_projected_new_disk_bytes"] < 0):
        blockers.append("active PrefixLease projected reservation is malformed")
    else:
        projected = _post_materialization_prefix_admission(
            current_free_bytes=current_free_bytes,
            current_scoped_bytes=current_project_bytes,
            manifest_estimates=estimates,
        )
        if not projected["admitted"]:
            blockers.extend(projected["blockers"])
    return blockers


def _pre_materialization_marker_identity(
    payload: Mapping[str, Any], raw_bytes: bytes, file_info: os.stat_result,
    *, experiment_id: str, manifest_path: Path, manifest_sha256: str,
    output_dir: Path,
) -> tuple[str, str]:
    """Validate the bounded PREPARED marker before excluding its exact path."""
    expected_keys = {
        "experiment_id", "pid", "state", "prefix_path", "output_dir",
        "manifest_path", "manifest_sha256", "lease_token", "updated_at_unix",
    }
    allocated_bytes = getattr(file_info, "st_blocks", 0) * 512
    if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid()
            or file_info.st_nlink != 1 or stat.S_IMODE(file_info.st_mode) != 0o600
            or file_info.st_size > ACTIVE_PREFIX_LEASE_MARKER_MAX_BYTES
            or allocated_bytes > ACTIVE_PREFIX_LEASE_MARKER_MAX_ALLOCATED_BYTES):
        raise RuntimeError("prepared PrefixLease marker exceeds its bounded private allocation")
    if set(payload) != expected_keys or raw_bytes != _canonical_pretty_json_bytes(payload):
        raise RuntimeError("prepared PrefixLease marker schema or canonical encoding is invalid")
    prefix_value = payload.get("prefix_path")
    if not isinstance(prefix_value, str):
        raise RuntimeError("prepared PrefixLease marker lacks its exact prefix path")
    prefix = Path(prefix_value)
    allowed_prefix_roots = (
        PREFIX_ROOT.absolute(), (Path.home() / "Library/Caches/FGMetalStep11D-R").absolute())
    if (not prefix.is_absolute() or str(prefix) != os.path.abspath(prefix_value)
            or not any(prefix.parent == root or root in prefix.parents
                       for root in allowed_prefix_roots)
            or _stat_entry_nofollow(prefix) is not None):
        raise RuntimeError("prepared PrefixLease prefix path is unsafe or already materialized")
    token = payload.get("lease_token")
    updated_at = payload.get("updated_at_unix")
    if (payload.get("experiment_id") != experiment_id
            or payload.get("pid") != os.getpid()
            or payload.get("state") != "PREPARED"
            or payload.get("output_dir") != str(output_dir)
            or payload.get("manifest_path") != str(manifest_path)
            or payload.get("manifest_sha256") != manifest_sha256
            or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token)
            or not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool)
            or not math.isfinite(updated_at) or updated_at < 0):
        raise RuntimeError("prepared PrefixLease marker does not bind the signed run manifest")
    return prefix_value, token


def _current_run_scope_binding(
    experiment_id: str | None, attestation_bytes: bytes, *,
    active_prefix_receipt: Mapping[str, Any] | None = None,
    require_active_marker: bool = False,
    allow_storage_smoke: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve only signed, exact current-run paths for nonlease accounting."""
    if experiment_id is None:
        return None, []
    blockers: list[str] = []
    try:
        manifest_path = _experiment_manifest_path(experiment_id)
    except ValueError as error:
        return None, [f"current PrefixLease ID is invalid for storage accounting: {error}"]
    manifest_entry = _stat_entry_nofollow(manifest_path)
    if manifest_entry is None:
        if active_prefix_receipt is not None or require_active_marker:
            return None, ["current PrefixLease signed manifest disappeared before storage accounting"]
        return None, []
    try:
        manifest_parent_fd = _open_directory_nofollow(manifest_path.parent)
        try:
            manifest, manifest_bytes, manifest_info = _read_json_at(
                manifest_parent_fd, manifest_path.name, maximum_bytes=1024 * 1024)
        finally:
            os.close(manifest_parent_fd)
        manifest_allocated = getattr(manifest_info, "st_blocks", 0) * 512
        if not SIGNED_OUTPUT_MANIFEST_REQUIRED_FIELDS.issubset(manifest):
            raise RuntimeError("signed current PrefixLease manifest is missing required fields")
        if (not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_uid != os.getuid()
                or manifest_info.st_nlink != 1 or stat.S_IMODE(manifest_info.st_mode) != 0o600
                or manifest_allocated > 2 * 1024 * 1024
                or not _verify_manifest_signature(manifest)
                or manifest_bytes != _canonical_pretty_json_bytes(manifest)
                or manifest.get("decision") != "ALLOW"
                or manifest.get("experiment_id") != experiment_id
                or manifest.get("manifest_path") != str(manifest_path)
                or manifest.get("gate0_attestation_sha256")
                != hashlib.sha256(attestation_bytes).hexdigest()):
            raise RuntimeError("signed current PrefixLease manifest does not match Gate 0")
        estimates = _validated_manifest_estimates(manifest)
        output_dir = _measured_output_dir(manifest.get("output_dir", ""))
        if str(output_dir) != manifest.get("output_dir"):
            raise RuntimeError("current PrefixLease output path is not canonical")
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        return None, [f"current PrefixLease manifest cannot authorize storage accounting: {error}"]

    exclusions: list[Path] = [manifest_path, output_dir]
    output_info = _stat_entry_nofollow(output_dir)
    if output_info is not None and (
            not stat.S_ISDIR(output_info.st_mode) or output_info.st_uid != os.getuid()
            or output_info.st_dev != REPO.lstat().st_dev):
        blockers.append("current PrefixLease output path is not an owned directory on the measured filesystem")
    try:
        output_allocated, _output_tree_sha256 = _persistent_output_snapshot(output_dir)
    except (OSError, TypeError, ValueError) as error:
        blockers.append(f"current PrefixLease output allocation cannot be measured: {error}")
        output_allocated = 0
    if output_allocated > estimates["estimated_persistent_bytes"]:
        blockers.append("current-run output exceeds its signed persistent-output reservation")

    claim_path: Path | None = None
    claim_sha256: str | None = None
    storage_smoke = manifest.get("storage_policy_smoke") is True
    if storage_smoke:
        if not allow_storage_smoke or not _is_storage_policy_smoke_id(experiment_id):
            blockers.append("storage-smoke paths are not authorized by this Gate 0 preflight")
        else:
            try:
                claim_path = _storage_smoke_claim_path(experiment_id)
                claim, claim_bytes = _read_storage_smoke_claim(experiment_id)
                claim_sha256 = hashlib.sha256(claim_bytes).hexdigest()
                if (manifest.get("smoke_nonce_claim_path") != str(claim_path)
                        or manifest.get("smoke_nonce_claim_sha256") != claim_sha256
                        or claim.get("preflight_attestation_sha256")
                        != hashlib.sha256(attestation_bytes).hexdigest()):
                    raise RuntimeError("storage-smoke nonce claim differs from its signed manifest")
                exclusions.append(claim_path)
            except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
                blockers.append(f"storage-smoke nonce claim cannot authorize storage accounting: {error}")
    elif manifest.get("smoke_nonce_claim_path") is not None or manifest.get("smoke_nonce_claim_sha256") is not None:
        blockers.append("non-smoke PrefixLease manifest contains storage-smoke nonce data")

    if active_prefix_receipt is not None and (
            active_prefix_receipt.get("experiment_id") != experiment_id
            or active_prefix_receipt.get("manifest_path") != str(manifest_path)
            or active_prefix_receipt.get("manifest_sha256")
            != hashlib.sha256(manifest_bytes).hexdigest()
            or active_prefix_receipt.get("output_dir") != str(output_dir)
            or active_prefix_receipt.get("active_marker_path")
            != str(ACTIVE_LEASE_ROOT / f"{experiment_id}.json")):
        blockers.append("materialized PrefixLease receipt differs from its signed current-run manifest")

    marker_path = ACTIVE_LEASE_ROOT / f"{experiment_id}.json"
    marker_identity: tuple[int, int, int, int, int] | None = None
    marker_digest: str | None = None
    marker_info = _stat_entry_nofollow(marker_path)
    if marker_info is None:
        if active_prefix_receipt is not None or require_active_marker:
            blockers.append("current PrefixLease active marker is missing before storage accounting")
    else:
        try:
            active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
            try:
                marker, marker_bytes, marker_info = _read_json_at(
                    active_root_fd, marker_path.name,
                    maximum_bytes=ACTIVE_PREFIX_LEASE_MARKER_MAX_BYTES)
                parent_info = os.fstat(active_root_fd)
            finally:
                os.close(active_root_fd)
            marker_identity = (
                marker_info.st_dev, marker_info.st_ino, marker_info.st_size,
                marker_info.st_mtime_ns, getattr(marker_info, "st_ctime_ns", 0))
            marker_digest = hashlib.sha256(marker_bytes).hexdigest()
            if active_prefix_receipt is None:
                _pre_materialization_marker_identity(
                    marker, marker_bytes, marker_info,
                    experiment_id=experiment_id, manifest_path=manifest_path,
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                    output_dir=output_dir)
            else:
                marker_receipt = _validate_materialized_active_prefix_marker(
                    marker, marker_bytes, marker_info)
                if (marker_receipt != active_prefix_receipt
                        or marker.get("experiment_id") != experiment_id
                        or marker.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()
                        or (marker_receipt.get("active_marker_parent_device"),
                            marker_receipt.get("active_marker_parent_inode"))
                        != (parent_info.st_dev, parent_info.st_ino)):
                    raise RuntimeError("materialized PrefixLease marker differs from its signed receipt")
            exclusions.append(marker_path)
        except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as error:
            blockers.append(f"current PrefixLease active marker cannot authorize storage accounting: {error}")

    prefix_path: Path | None = None
    if active_prefix_receipt is not None:
        prefix_value = active_prefix_receipt.get("prefix_path")
        if not isinstance(prefix_value, str):
            blockers.append("materialized PrefixLease receipt lacks its prefix path")
        else:
            prefix_path = Path(prefix_value)
            prefix_info = _stat_entry_nofollow(prefix_path)
            if (prefix_info is None or not stat.S_ISDIR(prefix_info.st_mode)
                    or (prefix_info.st_dev, prefix_info.st_ino)
                    != (active_prefix_receipt.get("prefix_device"),
                        active_prefix_receipt.get("prefix_inode"))):
                blockers.append("materialized PrefixLease path no longer matches its signed inode")
            exclusions.append(prefix_path)

    binding = {
        "experiment_id": experiment_id,
        "manifest_path": manifest_path,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_identity": (manifest_info.st_dev, manifest_info.st_ino, manifest_info.st_size,
                              manifest_info.st_mtime_ns, getattr(manifest_info, "st_ctime_ns", 0)),
        "output_dir": output_dir,
        "output_identity": ((output_info.st_dev, output_info.st_ino)
                            if output_info is not None else None),
        "output_allocated_bytes": output_allocated,
        "claim_path": claim_path,
        "claim_sha256": claim_sha256,
        "marker_path": marker_path,
        "marker_identity": marker_identity,
        "marker_sha256": marker_digest,
        "prefix_path": prefix_path,
        "exclusion_paths": tuple(exclusions),
    }
    return binding, blockers


def _gate0_blockers(*, active_experiment_id: str | None = None,
                    allow_storage_smoke: bool = False,
                    active_prefix_receipt: Mapping[str, Any] | None = None,
                    require_active_marker: bool = False,
                    measurements: dict[str, Any] | None = None) -> list[str]:
    blockers: list[str] = []
    if not GATE0_ATTESTATION_PATH.is_file() or GATE0_ATTESTATION_PATH.is_symlink():
        return ["Gate 0 attestation is missing; graphics experiments remain disabled"]
    try:
        attestation_bytes = _read_file_nofollow(GATE0_ATTESTATION_PATH)
        inventory_bytes = _read_file_nofollow(INVENTORY_PATH)
        receipt_bytes = _read_file_nofollow(STORAGE_CLEANUP_RECEIPT_PATH)
        attestation = json.loads(attestation_bytes)
        inventory = json.loads(inventory_bytes)
        receipt = json.loads(receipt_bytes)
    except (OSError, json.JSONDecodeError) as error:
        return [f"Gate 0 evidence is unreadable: {error}"]
    permitted_smoke_id = attestation.get("permitted_smoke_experiment_id")
    is_preflight_smoke = (
        allow_storage_smoke
        and _is_storage_policy_smoke_id(permitted_smoke_id)
        and active_experiment_id == permitted_smoke_id
        and attestation.get("status") == "PREFLIGHT_PASS"
        and attestation.get("mode") == "preflight"
    )
    if attestation.get("status") != "PASS" and not is_preflight_smoke:
        blockers.append("Gate 0 requires a final PASS attestation; preflight attestations authorize only the fixed storage smoke")
    if attestation.get("schema_version") != 1:
        blockers.append("Gate 0 attestation schema is missing or unsupported")
    if not _verify_gate0_signature(attestation):
        blockers.append("Gate 0 attestation signature is missing or invalid")
    try:
        attestation_age = time.time() - float(attestation.get("created_at_unix", 0))
    except (TypeError, ValueError):
        attestation_age = float("inf")
    if attestation_age < 0 or attestation_age > GATE0_MAX_AGE_SECONDS:
        blockers.append("Gate 0 attestation is missing or older than one hour")
    if attestation.get("inventory_sha256") != hashlib.sha256(inventory_bytes).hexdigest():
        blockers.append("storage inventory changed after Gate 0 attestation; refresh the gate evidence")
    if attestation.get("cleanup_receipt_sha256") != hashlib.sha256(receipt_bytes).hexdigest():
        blockers.append("storage cleanup receipt changed after Gate 0 attestation; refresh the gate evidence")
    test_report_path = REPO / "experiments/storage/build-retention-tests.log"
    try:
        current_test_hash = hashlib.sha256(_read_file_nofollow(test_report_path)).hexdigest()
    except OSError:
        current_test_hash = None
    if (attestation.get("build_retention_test_report_path") != str(test_report_path)
            or not current_test_hash
            or attestation.get("build_retention_test_report_sha256") != current_test_hash):
        blockers.append("build-retention test evidence changed or is not attested")
    supplemental = attestation.get("supplemental_evidence_sha256", {})
    for name in GATE0_SUPPLEMENTAL_EVIDENCE:
        path = REPO / "experiments/storage" / name
        try:
            current = hashlib.sha256(_read_file_nofollow(path)).hexdigest()
        except OSError:
            current = None
        if not isinstance(supplemental, dict) or not supplemental.get(name) or supplemental.get(name) != current:
            blockers.append(f"supplemental storage evidence changed or is not attested: {name}")
    try:
        inventory_age = time.time() - float(inventory.get("generated_at_unix", 0))
    except (TypeError, ValueError):
        inventory_age = float("inf")
    if inventory_age < 0 or inventory_age > GATE0_MAX_AGE_SECONDS:
        blockers.append("storage inventory is missing a fresh measurement from the last hour")
    if inventory.get("filesystem", {}).get("free_bytes", 0) < FREE_MINIMUM:
        blockers.append(f"attested inventory free space is below the {FREE_MINIMUM // GIB} GiB run minimum")
    try:
        scoped_bytes = int(inventory.get("totals", {}).get("fgmetal_scoped_allocated_inode_bytes", PROJECT_BUDGET + 1))
    except (TypeError, ValueError):
        scoped_bytes = PROJECT_BUDGET + 1
    expected_measurements = attestation.get("measurements", {})
    attested_observed_scope = expected_measurements.get(
        "fgmetal_scoped_observed_allocated_inode_bytes") if isinstance(expected_measurements, Mapping) else None
    attested_nonlease_scope = expected_measurements.get(
        "fgmetal_scoped_observed_nonlease_allocated_inode_bytes") if isinstance(expected_measurements, Mapping) else None
    if (not isinstance(attested_observed_scope, int) or isinstance(attested_observed_scope, bool)
            or attested_observed_scope < 0):
        blockers.append("Gate 0 lacks an exact observed-allocation baseline for mutation detection")
    if (not isinstance(attested_nonlease_scope, int) or isinstance(attested_nonlease_scope, bool)
            or attested_nonlease_scope < 0):
        blockers.append("Gate 0 lacks an exact non-lease allocation baseline for mutation detection")
    attested_persistent_output_bindings = expected_measurements.get(
        "signed_persistent_output_bindings") if isinstance(expected_measurements, Mapping) else None
    if not isinstance(attested_persistent_output_bindings, list):
        blockers.append("Gate 0 lacks its signed historical-output identity baseline")
    baseline_output_bindings: dict[str, Mapping[str, Any]] = {}
    if isinstance(attested_persistent_output_bindings, list):
        for row in attested_persistent_output_bindings:
            experiment_id = row.get("experiment_id") if isinstance(row, Mapping) else None
            if not isinstance(experiment_id, str) or experiment_id in baseline_output_bindings:
                blockers.append("Gate 0 historical-output identity baseline is malformed")
                continue
            baseline_output_bindings[experiment_id] = row
    unlinked_upper_bound = int(inventory.get("totals", {}).get(
        "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes", 0))
    if scoped_bytes > GATE0_PROJECT_BUDGET:
        blockers.append(f"attested FG-Metal inventory exceeds the {GATE0_PROJECT_BUDGET // GIB} GiB Gate 0 project budget")
    missing_image_estimates = inventory.get("totals", {}).get(
        "unaccounted_unlinked_mounted_backing_images", [])
    if missing_image_estimates:
        blockers.append("unlinked mounted image backing files lack a conservative size estimate: "
                        + ", ".join(str(path) for path in missing_image_estimates))
    if attestation.get("automatic_pre_run_disk_checks") is not True:
        blockers.append("automatic pre-run disk checks are not enabled in the attestation")
    if attestation.get("automatic_post_run_disk_checks") is not True:
        blockers.append("automatic post-run disk checks are not enabled in the attestation")
    if attestation.get("automatic_retention_controls_installed") is not True:
        blockers.append("automatic retention controls are not attested")
    if attestation.get("runner_admission_contract") != RUNNER_ADMISSION_CONTRACT:
        blockers.append("Gate 0 does not attest the managed PrefixLease runner admission contract")
    if attestation.get("authorized_runner_source_hashes_required") is not True:
        blockers.append("Gate 0 does not require authorized runner source hashes")
    if attestation.get("certified_template") is not True:
        blockers.append("a certified Wine prefix template is not attested")
    template_marker_path = PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json"
    try:
        template_marker_bytes = _read_file_nofollow(template_marker_path)
        template_marker = json.loads(template_marker_bytes)
        if hashlib.sha256(template_marker_bytes).hexdigest() != attestation.get("template_marker_sha256"):
            blockers.append("certified Wine template marker changed after Gate 0 attestation")
        if template_marker.get("graphics_experiments_run") is not False:
            blockers.append("Wine template no longer certifies a graphics-experiment-free baseline")
        if template_marker.get("tree_identity", {}).get("tree_sha256") != attestation.get("template_tree_sha256"):
            blockers.append("Wine template tree identity differs from Gate 0 attestation")
    except (OSError, json.JSONDecodeError):
        blockers.append("certified Wine template marker is missing or unreadable")
    control_hashes = attestation.get("retention_control_sha256", {})
    for name in ("storage_policy.py", "prefix_preflight.py", "storage_inventory.py",
                 "artifactize_builds.py", "build_retention.py", "storage_gate0.py",
                 "test_build_retention.py", "test_storage_policy.py", "smoke_prefix_lease.py",
                 "consolidate_evidence.py", "test_evidence_consolidation.py",
                 "test_wine_launcher_policy.py", "test_g0c_checks.py", "test_storage_recovery.py"):
        control_path = Path(__file__).resolve().parent / name
        expected_hash = control_hashes.get(name)
        try:
            current_hash = hashlib.sha256(_read_file_nofollow(control_path)).hexdigest()
        except OSError:
            current_hash = None
        if not expected_hash or current_hash != expected_hash:
            blockers.append(f"storage retention control changed or is not attested: {name}")
    try:
        signing_key_hash = hashlib.sha256(_manifest_signing_key()).hexdigest()
        if attestation.get("manifest_signing_key_sha256") != signing_key_hash:
            blockers.append("manifest-signing key changed after Gate 0 attestation")
    except (OSError, RuntimeError) as error:
        blockers.append(f"manifest-signing key is missing or unsafe: {error}")
    worktree_lines = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout
    worktree_count = sum(line.startswith("worktree ") for line in worktree_lines.splitlines())
    if worktree_count > 3:
        blockers.append(f"worktree retention cap exceeded ({worktree_count} > 3)")
    preserved_count = len(preserved_prefixes())
    if preserved_count > MAX_PRESERVED_PREFIXES:
        blockers.append(f"preserved Wine prefix limit exceeded ({preserved_count} > {MAX_PRESERVED_PREFIXES})")
    orphaned_prefixes = uncertified_prefixes()
    if orphaned_prefixes:
        blockers.append("uncertified Wine prefixes require reconciliation: "
                        + ", ".join(str(path) for path in orphaned_prefixes))
    active_leases = _active_lease_markers()
    other_leases = [path for path in active_leases
                    if active_experiment_id is None or path.stem != active_experiment_id]
    if other_leases and active_prefix_receipt is None:
        blockers.append("another or interrupted PrefixLease is active: "
                        + ", ".join(str(path) for path in other_leases))
    pending_prefix_reconciliation = _pending_prefix_reconciliation_receipts()
    if pending_prefix_reconciliation:
        blockers.append("PrefixLease reconciliation evidence is incomplete: "
                        + ", ".join(pending_prefix_reconciliation))
    pending_prefix_recovery = _pending_prefix_recovery_receipts()
    if pending_prefix_recovery:
        blockers.append("PrefixLease recovery evidence is incomplete: "
                        + ", ".join(pending_prefix_recovery))
    try:
        retention_directory = Path(__file__).resolve().parent
        if str(retention_directory) not in sys.path:
            sys.path.insert(0, str(retention_directory))
        import build_retention
        retention = build_retention.status_snapshot()
        if retention.get("gate_status") != "PASS" or retention.get("gate_blockers"):
            blockers.append("build-retention controller reports a blocked or ambiguous state: "
                            + "; ".join(retention.get("gate_blockers", [])))
        artifact_store_audit = build_retention.canonical_artifact_store_audit()
        if attestation.get("canonical_artifact_store_audit") != artifact_store_audit:
            blockers.append("canonical artifact content or link-count inventory changed after Gate 0 audit")
        retention_state = {
            "active_build_count": retention.get("active_build_count"),
            "preserved_build_count": retention.get("preserved_build_count"),
            "retained_build_count": retention.get("retained_build_count"),
            "active_dxvk_count": retention.get("active_dxvk_count"),
            "active_moltenvk_count": retention.get("active_moltenvk_count"),
            "unknown_count": len(retention.get("unknown", [])),
            "completed_pending_retirement_count": len(retention.get("completed_pending_retirement", [])),
            "failed_or_retiring_count": len(retention.get("failed_or_retiring", [])),
            "retention_fingerprint_sha256": retention.get("retention_fingerprint", {}).get("sha256"),
        }
        foreign_active_builds = _foreign_active_builds(retention.get("active", []), active_experiment_id)
        if foreign_active_builds:
            blockers.append("an active large build owned by another experiment overlaps this run: "
                            + ", ".join(str(row.get("build_id")) for row in foreign_active_builds))
        if attestation.get("build_retention_state") != retention_state:
            blockers.append("live build-retention state differs from the Gate 0 attestation")
        retention_fingerprint = retention.get("retention_fingerprint", {}).get("sha256")
        if not retention_fingerprint or attestation.get("build_retention_fingerprint_sha256") != retention_fingerprint:
            blockers.append("live build-root or lease identity differs from the Gate 0 attestation")
        if (inventory.get("storage_gate_metrics", {}).get("build_retention_fingerprint_sha256")
                != retention_fingerprint):
            blockers.append("storage inventory does not bind the current build-root identity fingerprint")
        if inventory.get("storage_gate_metrics", {}).get("build_retention", {}).get("gate_status") != "PASS":
            blockers.append("storage inventory does not contain a passing live build-retention snapshot")
    except Exception as error:
        blockers.append(f"build-retention controller cannot be checked: {error!r}")
    if not receipt.get("phases"):
        blockers.append("storage cleanup receipt has no cleanup phases")
    latest_phase = receipt.get("phases", [])[-1] if receipt.get("phases") else {}
    try:
        prefix_fingerprint = prefix_state_fingerprint().get("sha256")
    except Exception as error:
        prefix_fingerprint = None
        blockers.append(f"preserved-prefix identity cannot be verified: {error!r}")
    if not prefix_fingerprint or attestation.get("prefix_state_fingerprint_sha256") != prefix_fingerprint:
        blockers.append("preserved-prefix identity differs from the Gate 0 attestation")
    if latest_phase.get("prefix_state_fingerprint_sha256") != prefix_fingerprint:
        blockers.append("latest storage cleanup receipt does not bind preserved-prefix identity")
    if inventory.get("storage_gate_metrics", {}).get("prefix_state_fingerprint_sha256") != prefix_fingerprint:
        blockers.append("storage inventory does not bind preserved-prefix identity")
    phase_free = latest_phase.get("final_free_bytes", latest_phase.get("filesystem_free_after_bytes"))
    phase_project = latest_phase.get("fgmetal_scoped_allocated_bytes")
    if phase_project is None:
        phase_project = latest_phase.get("project_usage_after", {}).get("allocated_inode_deduplicated_bytes")
    phase_time = latest_phase.get("completed_at_unix", latest_phase.get("generated_at_unix"))
    if latest_phase.get("status") != "PASS":
        blockers.append("latest storage cleanup phase is not marked PASS")
    if latest_phase.get("storage_hygiene_status") != "PASS":
        blockers.append("latest storage cleanup phase reports a hygiene failure")
    if phase_free is None:
        blockers.append("latest storage cleanup phase lacks a final free-space measurement")
    elif int(phase_free) < FREE_MINIMUM:
        blockers.append(f"latest cleanup phase ended below the {FREE_MINIMUM // GIB} GiB run minimum")
    if phase_project is None:
        blockers.append("latest storage cleanup phase lacks a scoped project-usage measurement")
    elif int(phase_project) > GATE0_PROJECT_BUDGET:
        blockers.append(f"latest cleanup phase exceeded the {GATE0_PROJECT_BUDGET // GIB} GiB Gate 0 FG-Metal scope budget")
    if phase_time is None or time.time() - float(phase_time) < 0 or time.time() - float(phase_time) > GATE0_MAX_AGE_SECONDS:
        blockers.append("latest cleanup phase is missing or older than one hour")
    live_free = disk_free_bytes(REPO)
    if live_free < FREE_MINIMUM:
        blockers.append(f"current free space is below the {FREE_MINIMUM // GIB} GiB run minimum ({live_free})")
    receipt_valid = active_prefix_receipt is not None
    if active_prefix_receipt is not None:
        receipt = active_prefix_receipt
        expected_active_path = (ACTIVE_LEASE_ROOT / f"{active_experiment_id}.json"
                                if active_experiment_id else None)
        receipt_prefix = receipt.get("prefix_path")
        receipt_output = receipt.get("output_dir")
        receipt_marker = receipt.get("active_marker_path")
        try:
            manifest_path = _experiment_manifest_path(str(receipt.get("experiment_id", "")))
        except ValueError:
            manifest_path = None
        if (not _verify_storage_evidence(receipt, "prefix_materialization_hmac_sha256")
                or receipt.get("schema_version") != 1
                or receipt.get("experiment_id") != active_experiment_id
                or receipt.get("pid") != os.getpid()
                or receipt.get("gate0_attestation_sha256") != hashlib.sha256(attestation_bytes).hexdigest()
                or receipt.get("active_marker_path") != str(expected_active_path)
                or receipt.get("manifest_path") != str(manifest_path)
                or receipt.get("gate0_attested_observed_scope_bytes") != attested_observed_scope
                or receipt.get("gate0_attested_nonlease_scope_bytes") != attested_nonlease_scope
                or not isinstance(receipt_prefix, str)
                or not isinstance(receipt_output, str)
                or not isinstance(receipt_marker, str)):
            receipt_valid = False
            blockers.append("active PrefixLease materialization receipt is missing, stale, or unauthenticated")
    persistent_snapshot, persistent_exclusions, persistent_blockers = (
        _signed_persistent_output_snapshot(
            exclude_experiment_id=active_experiment_id,
            expected_gate0_attestation_sha256=hashlib.sha256(attestation_bytes).hexdigest(),
            baseline_experiment_ids=set(baseline_output_bindings)))
    blockers.extend(persistent_blockers)
    current_output_bindings = {
        row.get("experiment_id"): row for row in persistent_snapshot.get("bindings", ())
        if isinstance(row, Mapping) and isinstance(row.get("experiment_id"), str)
    }
    if any(current_output_bindings.get(experiment_id) != row
           for experiment_id, row in baseline_output_bindings.items()):
        blockers.append("signed historical experiment outputs changed since Gate 0 certification")
    scope_binding: dict[str, Any] | None = None
    if active_experiment_id is not None:
        scope_binding, scope_binding_blockers = _current_run_scope_binding(
            active_experiment_id, attestation_bytes,
            active_prefix_receipt=(active_prefix_receipt if receipt_valid else None),
            require_active_marker=require_active_marker,
            allow_storage_smoke=is_preflight_smoke,
        )
        blockers.extend(scope_binding_blockers)
    scope_exclusions = tuple(persistent_exclusions) + (
        tuple(scope_binding["exclusion_paths"]) if scope_binding is not None else ())
    live_project_usage = measure_project_usage(exclude_paths=scope_exclusions)
    live_project = conservative_project_usage_bytes(live_project_usage)
    if live_project > GATE0_PROJECT_BUDGET:
        blockers.append(f"current FG-Metal storage exceeds the {GATE0_PROJECT_BUDGET // GIB} GiB Gate 0 project budget ({live_project})")
    observed_nonlease = (live_project_usage.get("allocated_inode_deduplicated_bytes_excluding_paths")
                         if scope_exclusions else
                         live_project_usage.get("allocated_inode_deduplicated_bytes"))
    live_nonlease_project: int | None = None
    if (not isinstance(observed_nonlease, int) or isinstance(observed_nonlease, bool)
            or observed_nonlease < 0):
        blockers.append("current non-lease FG-Metal storage measurement is invalid")
    else:
        live_nonlease_project = observed_nonlease + unlinked_upper_bound
        if (isinstance(attested_nonlease_scope, int)
                and observed_nonlease != attested_nonlease_scope):
            blockers.append("unrelated non-lease storage changed since Gate 0 certification")
    if receipt_valid and active_prefix_receipt is not None:
        if isinstance(observed_nonlease, int) and not isinstance(observed_nonlease, bool) and observed_nonlease >= 0:
            try:
                prefix_size = _prefix_size(Path(receipt["prefix_path"]))
                current_prefix_bytes = prefix_size["allocated_inode_deduplicated_bytes"]
                current_output_bytes, _output_tree_sha256 = _persistent_output_snapshot(
                    Path(receipt["output_dir"]))
            except (KeyError, OSError, TypeError, ValueError):
                blockers.append("active PrefixLease allocation receipt paths are unsafe or malformed")
            else:
                blockers.extend(_prefix_lease_accounting_blockers(
                    receipt,
                    current_free_bytes=live_free,
                    current_project_bytes=live_project,
                    current_nonlease_project_bytes=observed_nonlease,
                    attested_nonlease_project_bytes=(attested_nonlease_scope
                                                     if isinstance(attested_nonlease_scope, int)
                                                     and not isinstance(attested_nonlease_scope, bool)
                                                     else -1),
                    current_prefix_allocated_bytes=current_prefix_bytes,
                    current_output_allocated_bytes=current_output_bytes,
                    attested_free_bytes=int(expected_measurements.get("free_bytes", live_free)),
                    other_active_lease_count=len(other_leases),
                    expected_binding={
                        "experiment_id": active_experiment_id,
                        "pid": os.getpid(),
                        "prefix_path": str(receipt["prefix_path"]),
                        "active_marker_path": str(receipt["active_marker_path"]),
                        "output_dir": str(receipt["output_dir"]),
                    },
                ))
    if scope_binding is not None:
        rebound, rebound_blockers = _current_run_scope_binding(
            active_experiment_id, attestation_bytes,
            active_prefix_receipt=(active_prefix_receipt if receipt_valid else None),
            require_active_marker=require_active_marker,
            allow_storage_smoke=is_preflight_smoke,
        )
        blockers.extend(rebound_blockers)
        if rebound is None or any(rebound.get(key) != scope_binding.get(key) for key in (
                "manifest_sha256", "manifest_identity", "output_identity", "output_allocated_bytes",
                "claim_sha256", "marker_identity", "marker_sha256", "prefix_path", "exclusion_paths")):
            blockers.append("current-run storage paths changed while Gate 0 accounting was measured")
    rebound_persistent_snapshot, rebound_persistent_exclusions, rebound_persistent_blockers = (
        _signed_persistent_output_snapshot(
            exclude_experiment_id=active_experiment_id,
            expected_gate0_attestation_sha256=hashlib.sha256(attestation_bytes).hexdigest(),
            baseline_experiment_ids=set(baseline_output_bindings)))
    blockers.extend(rebound_persistent_blockers)
    if (rebound_persistent_snapshot != persistent_snapshot
            or rebound_persistent_exclusions != persistent_exclusions):
        blockers.append("signed experiment output paths changed while Gate 0 accounting was measured")
    if measurements is not None:
        measurements.update({
            "free_bytes": live_free,
            "project_usage": live_project_usage,
            "conservative_project_bytes": live_project,
            "nonlease_project_bytes": live_nonlease_project,
        })
    inventory_measurements = attestation.get("inventory_measurements", {})
    if (not isinstance(inventory_measurements, Mapping)
            or inventory_measurements.get("free_bytes") != inventory.get("filesystem", {}).get("free_bytes")
            or inventory_measurements.get("fgmetal_scoped_allocated_inode_bytes") != scoped_bytes
            or inventory_measurements.get("fgmetal_scoped_observed_allocated_inode_bytes")
            != inventory.get("totals", {}).get("fgmetal_scoped_observed_allocated_inode_bytes")):
        blockers.append("Gate 0 inventory snapshot is not bound to its attestation")
    if (not isinstance(attested_observed_scope, int) or isinstance(attested_observed_scope, bool)
            or expected_measurements.get("fgmetal_scoped_allocated_inode_bytes")
            != max(attested_observed_scope + unlinked_upper_bound, scoped_bytes)):
        blockers.append("Gate 0 live scope measurement is inconsistent with its observed baseline and inventory bound")
    if abs(live_free - int(expected_measurements.get("free_bytes", live_free))) > GATE0_FREE_DRIFT_TOLERANCE_BYTES:
        blockers.append("current free space changed by more than 2 GiB since Gate 0 attestation")
    expected_counts = {
        "worktree_count": worktree_count,
        "preserved_prefix_count": preserved_count,
        "uncertified_prefix_count": len(orphaned_prefixes),
        "active_lease_count": len(other_leases),
    }
    for key, actual in expected_counts.items():
        if attestation.get(key) != actual:
            blockers.append(f"Gate 0 {key} no longer matches the live storage state")
        if latest_phase.get(key) != actual:
            blockers.append(f"latest storage receipt {key} does not match the live storage state")
    if not isinstance(latest_phase.get("build_retention_state"), dict):
        blockers.append("latest cleanup receipt lacks a build-retention state snapshot")
    elif 'retention_state' in locals() and latest_phase.get("build_retention_state") != retention_state:
        blockers.append("latest cleanup receipt build-retention state differs from live state")
    if is_preflight_smoke and (not latest_phase.get("storage_policy_reconciliation")
                               or latest_phase.get("storage_hygiene_status") != "PASS"):
        blockers.append("preflight smoke is not authorized by a completed storage reconciliation phase")
    if attestation.get("prefixlease_smoke_status") != ("PENDING" if is_preflight_smoke else "PASS"):
        blockers.append("Gate 0 PrefixLease smoke status is inconsistent with its attestation mode")
    if attestation.get("status") == "PASS":
        blockers.extend(_attested_smoke_evidence_blockers(attestation))
    return blockers


def measure_project_usage(*, exclude_paths: Sequence[Path | str] = ()) -> dict[str, Any]:
    """Estimate project storage, deduplicating hard links but not APFS clones.

    When exact paths are supplied, also return the project allocation that
    remains after excluding those paths. An inode linked both inside and
    outside an excluded tree remains in the included total.
    """
    normalized_exclusions = tuple(Path(os.path.abspath(os.fspath(path))) for path in exclude_paths)
    allocated_by_inode: dict[tuple[int, int], int] = {}
    included_inodes: set[tuple[int, int]] = set()
    logical = 0
    allocated = 0
    file_count = 0
    disappearance_events: list[dict[str, Any]] = []

    for discovered in _classified_project_roots():
        root = discovered.path
        if _is_other_project_path(root):
            continue

        try:
            info = root.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise OSError(errno.EINVAL, f"storage root is an unexpected symlink: {root}")
            if not stat.S_ISDIR(info.st_mode):
                raise OSError(errno.ENOTDIR, f"storage root is not a directory: {root}")

            if (discovered.expected_ino is not None
                    and (info.st_dev, info.st_ino) != (discovered.expected_dev, discovered.expected_ino)):
                raise OSError(errno.ESTALE, f"storage root identity changed between discovery and stat: {root}")

            root_device = info.st_dev
        except FileNotFoundError as error:
            if discovered.classification == ROOT_CLASS_OPTIONAL_EPHEMERAL:
                disappearance_events.append({
                    "path": str(root),
                    "classification": discovered.classification,
                    "category": discovered.category,
                    "event": "OPTIONAL_ROOT_DISAPPEARED_BEFORE_STAT",
                    "errno": errno.ENOENT,
                    "timestamp_unix": time.time(),
                })
                continue
            raise
        except OSError as error:
            if error.errno == errno.ENOENT and discovered.classification == ROOT_CLASS_OPTIONAL_EPHEMERAL:
                disappearance_events.append({
                    "path": str(root),
                    "classification": discovered.classification,
                    "category": discovered.category,
                    "event": "OPTIONAL_ROOT_DISAPPEARED_BEFORE_STAT",
                    "errno": errno.ENOENT,
                    "timestamp_unix": time.time(),
                })
                continue
            raise

        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            descend = []
            for name in dirnames:
                child = current / name
                if child.is_symlink() or _is_other_project_path(child):
                    continue
                try:
                    if child.stat().st_dev != root_device:
                        continue
                except OSError:
                    continue
                descend.append(name)
            dirnames[:] = descend
            for name in filenames:
                path = current / name
                if _is_other_project_path(path):
                    continue
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                logical += info.st_size
                file_count += 1
                key = (info.st_dev, info.st_ino)
                allocated_by_inode.setdefault(key, getattr(info, "st_blocks", 0) * 512)
                if not any(path == excluded or excluded in path.parents
                           for excluded in normalized_exclusions):
                    included_inodes.add(key)
    allocated = sum(allocated_by_inode.values())
    result = {
        "logical_bytes": logical,
        "allocated_inode_deduplicated_bytes": allocated,
        "file_count": file_count,
        "hardlinks_deduplicated": True,
        "apfs_clone_awareness": False,
        "disappearance_events": disappearance_events,
    }
    if normalized_exclusions:
        result["allocated_inode_deduplicated_bytes_excluding_paths"] = sum(
            allocated_by_inode[key] for key in included_inodes)
    return result


def _tree_fingerprint(root: Path, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Hash a prefix tree by relative path, file bytes, link target, and mode."""
    ignored = exclude or set()
    digest = hashlib.sha256()
    file_count = 0
    logical_bytes = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        kept_dirs = []
        for name in sorted(dirnames):
            path = current / name
            if name in ignored and current == root:
                continue
            if path.is_symlink():
                rel = path.relative_to(root).as_posix()
                row = {"path": rel, "kind": "symlink", "mode": oct(stat.S_IMODE(path.lstat().st_mode)),
                       "target": os.readlink(path)}
                digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
                digest.update(b"\n")
                file_count += 1
            else:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            if current == root and name in ignored:
                continue
            path = current / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                row = {"path": rel, "kind": "symlink", "mode": oct(stat.S_IMODE(info.st_mode)),
                       "target": os.readlink(path)}
                digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
                digest.update(b"\n")
                file_count += 1
            elif stat.S_ISREG(info.st_mode):
                file_hash = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        file_hash.update(block)
                row = {"path": rel, "kind": "file", "mode": oct(stat.S_IMODE(info.st_mode)),
                       "size_bytes": info.st_size, "sha256": file_hash.hexdigest()}
                digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
                digest.update(b"\n")
                file_count += 1
                logical_bytes += info.st_size
    return {"tree_sha256": digest.hexdigest(), "file_count": file_count,
            "logical_bytes": logical_bytes}


def certify_prefix_template(
    template: Path = PREFIX_TEMPLATE,
    *,
    runtime_identity: Mapping[str, Any],
    initialization: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal the single pristine Wine prefix after initialization and before any run arm."""
    template = template.expanduser().absolute()
    if template.is_symlink() or not template.is_dir():
        raise ValueError(f"template path must be a real directory: {template}")
    if not (template / "system.reg").is_file():
        raise ValueError("template lacks system.reg; initialize it with the pinned Wine runtime first")
    marker = template / "FGMETAL_PREFIX_TEMPLATE.json"
    if marker.exists() or marker.is_symlink():
        raise FileExistsError(f"refusing to replace certified template marker: {marker}")
    identity = _tree_fingerprint(template)
    payload = {
        "schema_version": 1,
        "certified_at_unix": time.time(),
        "template_path": str(template),
        "tree_identity": identity,
        "runtime_identity": dict(runtime_identity),
        "initialization": dict(initialization),
        "graphics_experiments_run": False,
    }
    _write_json(marker, payload)
    return payload


def preserved_prefixes() -> list[Path]:
    found: list[Path] = []
    roots = [PREFIX_ROOT, Path.home() / "Library/Caches/FGMetalStep11D-R"]
    for root in roots:
        try:
            root_fd = _open_directory_nofollow(root)
        except FileNotFoundError:
            continue
        except OSError:
            found.append(root)
            continue
        try:
            for name in os.listdir(root_fd):
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                path = root / name
                try:
                    preserve_marker = _stat_entry_nofollow(path / "PRESERVE_PREFIX.json")
                    template_marker = _stat_entry_nofollow(path / "FGMETAL_PREFIX_TEMPLATE.json")
                except OSError:
                    found.append(path)
                    continue
                if ((preserve_marker is not None and stat.S_ISREG(preserve_marker.st_mode))
                        or (template_marker is not None and stat.S_ISREG(template_marker.st_mode))):
                    found.append(path)
        finally:
            os.close(root_fd)
    dedup: dict[str, Path] = {str(path): path for path in found}
    return list(dedup.values())


def prefix_state_fingerprint() -> dict[str, Any]:
    """Bind Gate 0 to the identity and retained registry state of preserved prefixes."""
    rows = []
    for path in sorted(preserved_prefixes(), key=str):
        info = _stat_entry_nofollow(path)
        if info is None or not stat.S_ISDIR(info.st_mode):
            rows.append({"path": str(path), "state": "MISSING_OR_NOT_DIRECTORY"})
            continue
        marker_path = path / ("PRESERVE_PREFIX.json" if _stat_entry_nofollow(path / "PRESERVE_PREFIX.json")
                              else "FGMETAL_PREFIX_TEMPLATE.json")
        marker_hash = None
        try:
            marker_hash = hashlib.sha256(_read_file_nofollow(marker_path)).hexdigest()
        except OSError:
            pass
        try:
            payload = _prefix_payload(path)
            size = _prefix_size(path)
        except OSError as error:
            rows.append({"path": str(path), "state": "UNREADABLE", "error": repr(error),
                         "device": info.st_dev, "inode": info.st_ino})
            continue
        rows.append({"path": str(path), "device": info.st_dev, "inode": info.st_ino,
                     "mode": stat.S_IMODE(info.st_mode), "marker_sha256": marker_hash,
                     "registry_identity": payload, "logical_bytes": size.get("logical_bytes"),
                     "allocated_inode_bytes": size.get("allocated_inode_deduplicated_bytes")})
    material = {"preserved_prefixes": rows,
                "uncertified_prefix_paths": sorted(str(path) for path in uncertified_prefixes())}
    return {"sha256": hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(), **material}


def uncertified_prefixes() -> list[Path]:
    """Find Wine prefixes left behind without a template or preservation marker."""
    found: list[Path] = []
    roots = [PREFIX_ROOT, Path.home() / "Library/Caches/FGMetalStep11D-R"]
    active_prefixes: set[str] = set()
    for marker_path in _active_lease_markers():
        try:
            marker = json.loads(_read_file_nofollow(marker_path))
            manifest_path = _experiment_manifest_path(str(marker.get("experiment_id", "")))
            manifest_bytes = _read_file_nofollow(manifest_path)
            manifest = json.loads(manifest_bytes)
            if (marker.get("manifest_sha256") == hashlib.sha256(manifest_bytes).hexdigest()
                    and _verify_manifest_signature(manifest)
                    and manifest.get("experiment_id") == marker.get("experiment_id")
                    and marker_path.stem == marker.get("experiment_id")
                    and marker.get("state") in {"PREPARED", "MATERIALIZED"}
                    and isinstance(marker.get("prefix_path"), str)):
                active_prefixes.add(os.path.abspath(marker["prefix_path"]))
        except (OSError, ValueError, json.JSONDecodeError):
            # Invalid active records remain gate blockers through _active_lease_markers;
            # they never authorize skipping an uncertified prefix.
            continue
    for root in roots:
        try:
            root_fd = _open_directory_nofollow(root)
        except FileNotFoundError:
            continue
        except OSError:
            found.append(root)
            continue
        os.close(root_fd)
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            for name in dirnames:
                if re.fullmatch(r"\..+\.clone-[0-9a-f]{32}", name):
                    found.append(current / name)
            if "system.reg" in filenames and ("dosdevices" in dirnames or "drive_c" in dirnames):
                if os.path.abspath(current) in active_prefixes:
                    dirnames[:] = []
                    continue
                if not ((current / "PRESERVE_PREFIX.json").is_file()
                        or (current / "FGMETAL_PREFIX_TEMPLATE.json").is_file()):
                    found.append(current)
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
    return sorted(set(found))


def _active_lease_markers() -> list[Path]:
    try:
        directory_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
    except FileNotFoundError:
        return []
    except OSError:
        return [ACTIVE_LEASE_ROOT]
    try:
        found = []
        for name in os.listdir(directory_fd):
            if not name.endswith(".json"):
                continue
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            found.append(ACTIVE_LEASE_ROOT / name)
        return sorted(found)
    finally:
        os.close(directory_fd)


def _pid_is_definitely_gone(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def _acquire_prefix_reconciliation_locks() -> tuple[int, Any, Any]:
    active_root_fd = _open_directory_nofollow(
        ACTIVE_LEASE_ROOT, create=True, strict_directory_sync=True)
    descriptor: int | None = None
    gate_file = None
    allocation_lock = None
    try:
        descriptor = os.open(".lease.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=active_root_fd)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise OSError("PrefixLease reconciliation lock is not a private regular file")
        os.fsync(active_root_fd)
        gate_file = os.fdopen(descriptor, "a+")
        descriptor = None
        fcntl.flock(gate_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        allocation_lock = _acquire_storage_allocation_lock()
        return active_root_fd, gate_file, allocation_lock
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if gate_file is not None:
            try:
                fcntl.flock(gate_file.fileno(), fcntl.LOCK_UN)
            finally:
                gate_file.close()
        _release_storage_allocation_lock(allocation_lock)
        os.close(active_root_fd)
        raise


def _release_prefix_reconciliation_locks(active_root_fd: int, gate_file: Any,
                                         allocation_lock: Any) -> None:
    try:
        _release_storage_allocation_lock(allocation_lock)
    finally:
        try:
            fcntl.flock(gate_file.fileno(), fcntl.LOCK_UN)
        finally:
            gate_file.close()
            os.close(active_root_fd)


def _read_json_at(directory_fd: int, name: str, *, maximum_bytes: int = 1024 * 1024
                  ) -> tuple[dict[str, Any], bytes, os.stat_result]:
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise OSError(f"expected bounded regular JSON evidence: {name}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum_bytes:
                raise OSError(f"JSON evidence exceeds read limit: {name}")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or total != before.st_size):
            raise OSError(f"JSON evidence changed while reading: {name}")
        raw = b"".join(chunks)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"expected a JSON object: {name}")
        return value, raw, before
    finally:
        os.close(descriptor)


def _write_reconciliation_record(path: Path, record: Mapping[str, Any], *, create: bool) -> bytes:
    sealed = _seal_storage_evidence(record, "reconciliation_hmac_sha256")
    raw = (json.dumps(sealed, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if create:
        parent_fd = _open_directory_nofollow(path.parent, create=True, strict_directory_sync=True)
        descriptor: int | None = None
        temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
        temporary_identity: tuple[int, int] | None = None
        try:
            descriptor = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
            descriptor_info = os.fstat(descriptor)
            temporary_identity = (descriptor_info.st_dev, descriptor_info.st_ino)
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while persisting PrefixLease reconciliation evidence")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            _rename_child_exclusive(parent_fd, temporary_name, path.name)
            os.fsync(parent_fd)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
                descriptor = None
            try:
                current = _stat_child_nofollow(parent_fd, temporary_name)
                if (current is not None and temporary_identity is not None
                        and (current.st_dev, current.st_ino) == temporary_identity
                        and stat.S_ISREG(current.st_mode)):
                    os.unlink(temporary_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            except OSError:
                pass
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)
    else:
        _write_json(path, sealed, strict_directory_sync=True)
    return raw


def _verify_reconciliation_evidence(record: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    experiment_id = record.get("experiment_id")
    if not _is_storage_policy_smoke_id(experiment_id):
        raise ValueError("reconciliation evidence has an invalid smoke identity")
    output = REPO / "experiments/storage/smoke-runs" / str(experiment_id)
    expected_prefix = PREFIX_ROOT / f"storage-smoke-{experiment_id}"
    manifest_path = _experiment_manifest_path(str(experiment_id))
    manifest_bytes = _read_file_nofollow(manifest_path)
    manifest = json.loads(manifest_bytes)
    cleanup_path = output / "prefix-cleanup.json"
    cleanup_bytes = _read_file_nofollow(cleanup_path)
    cleanup = json.loads(cleanup_bytes)
    preflight_path = output / "gate0-preflight-attestation.json"
    preflight_bytes = _read_file_nofollow(preflight_path)
    preflight = json.loads(preflight_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if (not _verify_manifest_signature(manifest) or manifest.get("experiment_id") != experiment_id
            or manifest.get("storage_policy_smoke") is not True or manifest.get("decision") != "ALLOW"
            or manifest.get("manifest_path") != str(manifest_path)
            or manifest.get("output_dir") != str(output)
            or manifest_sha != record.get("manifest_sha256")):
        raise ValueError("signed smoke manifest is missing or differs from the reconciliation record")
    if (not _verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
            or cleanup.get("experiment_id") != experiment_id
            or cleanup.get("prefix_path") != str(expected_prefix)
            or cleanup.get("receipt_finalization_complete") is not True
            or cleanup.get("cleanup_error") is not None
            or cleanup.get("receipt_write_error") is not None
            or cleanup.get("prefix_preserved") is not False
            or not (cleanup.get("prefix_created") is False or cleanup.get("prefix_deleted") is True)
            or hashlib.sha256(cleanup_bytes).hexdigest() != record.get("cleanup_receipt_sha256")):
        raise ValueError("signed cleanup receipt does not prove that PrefixLease has no remaining prefix")
    if (not _verify_gate0_signature(preflight) or preflight.get("status") != "PREFLIGHT_PASS"
            or preflight.get("mode") != "preflight"
            or preflight.get("prefixlease_smoke_status") != "PENDING"
            or preflight.get("permitted_smoke_experiment_id") != experiment_id
            or hashlib.sha256(preflight_bytes).hexdigest() != record.get("preflight_attestation_sha256")
            or manifest.get("gate0_attestation_sha256") != record.get("preflight_attestation_sha256")):
        raise ValueError("signed pending preflight differs from the reconciliation record")
    return manifest, cleanup, cleanup_bytes, preflight_bytes


def _assert_reconciled_smoke_paths_absent(experiment_id: str) -> list[str]:
    expected_prefix = PREFIX_ROOT / f"storage-smoke-{experiment_id}"
    prefix_root_fd = _open_directory_nofollow(PREFIX_ROOT)
    try:
        if _stat_child_nofollow(prefix_root_fd, expected_prefix.name) is not None:
            raise RuntimeError("PrefixLease prefix path still exists; refusing stale marker reconciliation")
        clone_pattern = re.compile(rf"\.{re.escape(expected_prefix.name)}\.clone-[0-9a-f]{{32}}\Z")
        clones = sorted(name for name in os.listdir(prefix_root_fd) if clone_pattern.fullmatch(name))
        if clones:
            raise RuntimeError("PrefixLease clone temporary path still exists: " + ", ".join(clones))
        return clones
    finally:
        os.close(prefix_root_fd)


def _complete_prepared_prefix_reconciliations(active_root_fd: int) -> list[dict[str, Any]]:
    if not PREFIX_RECONCILIATION_ROOT.exists():
        return []
    reconciliation_fd = _open_directory_nofollow(PREFIX_RECONCILIATION_ROOT)
    completed: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(reconciliation_fd)):
            if not name.endswith(".json"):
                continue
            record, _raw, _info = _read_json_at(reconciliation_fd, name)
            if not _verify_storage_evidence(record, "reconciliation_hmac_sha256"):
                raise RuntimeError(f"PrefixLease reconciliation record signature failed: {name}")
            if record.get("state") != "PREPARED":
                continue
            experiment_id = record.get("experiment_id")
            if not _is_storage_policy_smoke_id(experiment_id) or name != f"{experiment_id}.json":
                raise RuntimeError(f"PrefixLease reconciliation record identity failed: {name}")
            if _stat_child_nofollow(active_root_fd, name) is not None:
                continue
            if not _pid_is_definitely_gone(record.get("pid")):
                raise RuntimeError(f"PrefixLease reconciliation PID is live or ambiguous: {record.get('pid')}")
            _assert_reconciled_smoke_paths_absent(experiment_id)
            _verify_reconciliation_evidence(record)
            record.update({"state": "COMPLETE", "marker_absent_verified": True,
                           "completed_at_unix": time.time()})
            _write_reconciliation_record(PREFIX_RECONCILIATION_ROOT / name, record, create=False)
            completed.append(record)
    finally:
        os.close(reconciliation_fd)
    return completed


def reconcile_abandoned_storage_smoke_lease_markers() -> list[dict[str, Any]]:
    """Clear only stale failed-smoke markers whose signed evidence proves no lease resource remains."""
    active_root_fd, gate_lock, allocation_lock = _acquire_prefix_reconciliation_locks()
    reconciled: list[dict[str, Any]] = []
    try:
        reconciled.extend(_complete_prepared_prefix_reconciliations(active_root_fd))
        for name in sorted(os.listdir(active_root_fd)):
            if not name.endswith(".json"):
                continue
            experiment_id = name[:-5]
            if not _is_storage_policy_smoke_id(experiment_id):
                continue
            marker, marker_bytes, marker_info = _read_json_at(active_root_fd, name)
            if marker.get("experiment_id") != experiment_id:
                continue
            if marker.get("state") not in {"RECONCILIATION_REQUIRED", "MATERIALIZED", "RECOVERY_PENDING"}:
                continue
            expected_prefix = PREFIX_ROOT / f"storage-smoke-{experiment_id}"
            expected_output = REPO / "experiments/storage/smoke-runs" / experiment_id
            output = _measured_output_dir(expected_output)
            cleanup_path = output / "prefix-cleanup.json"
            if not cleanup_path.exists():
                recovered = recover_interrupted_storage_smoke_lease_marker(experiment_id, active_root_fd=active_root_fd)
                reconciled.append(recovered)
                continue
            if marker.get("state") != "RECONCILIATION_REQUIRED":
                continue
            if not _pid_is_definitely_gone(marker.get("pid")):
                raise RuntimeError(f"PrefixLease smoke marker PID is live or ambiguous: {marker.get('pid')}")
            expected_manifest = _experiment_manifest_path(experiment_id)
            if (marker.get("prefix_path") != str(expected_prefix)
                    or marker.get("output_dir") != str(expected_output)
                    or marker.get("manifest_path") != str(expected_manifest)):
                raise RuntimeError("refusing to reconcile a smoke marker with non-canonical paths")
            _assert_reconciled_smoke_paths_absent(experiment_id)
            manifest_bytes = _read_file_nofollow(expected_manifest)
            manifest = json.loads(manifest_bytes)
            if (not _verify_manifest_signature(manifest)
                    or manifest.get("experiment_id") != experiment_id
                    or hashlib.sha256(manifest_bytes).hexdigest() != marker.get("manifest_sha256")):
                raise RuntimeError("stale smoke marker is not bound to its signed manifest")
            cleanup_bytes = _read_file_nofollow(cleanup_path)
            cleanup = json.loads(cleanup_bytes)
            preflight_bytes = _read_file_nofollow(output / "gate0-preflight-attestation.json")
            preflight = json.loads(preflight_bytes)
            preflight_hash = hashlib.sha256(preflight_bytes).hexdigest()
            if (not _verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
                    or cleanup.get("experiment_id") != experiment_id
                    or cleanup.get("prefix_path") != str(expected_prefix)
                    or cleanup.get("receipt_finalization_complete") is not True
                    or cleanup.get("cleanup_error") is not None
                    or cleanup.get("receipt_write_error") is not None
                    or cleanup.get("prefix_preserved") is not False
                    or not (cleanup.get("prefix_created") is False or cleanup.get("prefix_deleted") is True)):
                raise RuntimeError("stale smoke marker lacks a durable cleanup proof for an absent prefix")
            if (not _verify_gate0_signature(preflight)
                    or preflight.get("permitted_smoke_experiment_id") != experiment_id
                    or manifest.get("gate0_attestation_sha256") != preflight_hash):
                raise RuntimeError("stale smoke marker preflight identity or signature is invalid")
            claim_path = _storage_smoke_claim_path(experiment_id)
            try:
                claim, claim_bytes = _read_storage_smoke_claim(experiment_id)
                if claim.get("preflight_attestation_sha256") != preflight_hash:
                    raise RuntimeError("existing smoke nonce claim binds another preflight")
            except FileNotFoundError:
                claim, claim_bytes = _write_storage_smoke_claim(
                    experiment_id, preflight_bytes, reason="failed smoke PrefixLease reconciliation")
            marker_hash = hashlib.sha256(marker_bytes).hexdigest()
            reconciliation_path = PREFIX_RECONCILIATION_ROOT / f"{experiment_id}.json"
            record: dict[str, Any] = {
                "schema_version": 1,
                "state": "PREPARED",
                "experiment_id": experiment_id,
                "active_marker_path": str(ACTIVE_LEASE_ROOT / name),
                "active_marker_sha256": marker_hash,
                "active_marker_device": marker_info.st_dev,
                "active_marker_inode": marker_info.st_ino,
                "prefix_path": str(expected_prefix),
                "prefix_absent_verified": True,
                "clone_temporary_paths_absent": True,
                "clone_temporary_paths": [],
                "pid": marker.get("pid"),
                "pid_definitely_gone": True,
                "manifest_path": str(expected_manifest),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "cleanup_receipt_path": str(cleanup_path),
                "cleanup_receipt_sha256": hashlib.sha256(cleanup_bytes).hexdigest(),
                "preflight_attestation_path": str(output / "gate0-preflight-attestation.json"),
                "preflight_attestation_sha256": preflight_hash,
                "smoke_nonce_claim_path": str(claim_path),
                "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
                "prepared_at_unix": time.time(),
            }
            try:
                _write_reconciliation_record(reconciliation_path, record, create=True)
            except FileExistsError:
                previous = json.loads(_read_file_nofollow(reconciliation_path))
                if (not _verify_storage_evidence(previous, "reconciliation_hmac_sha256")
                        or previous.get("state") != "PREPARED"
                        or previous.get("active_marker_sha256") != marker_hash):
                    raise RuntimeError("existing PrefixLease reconciliation record conflicts with the marker")
                record = previous

            # The durable prepared record precedes removal. Reopen the exact file
            # through the held active-root descriptor and compare its bytes/inode.
            check, check_bytes, check_info = _read_json_at(active_root_fd, name)
            if (check_bytes != marker_bytes or (check_info.st_dev, check_info.st_ino)
                    != (marker_info.st_dev, marker_info.st_ino)
                    or check.get("state") != "RECONCILIATION_REQUIRED"):
                raise RuntimeError("active PrefixLease marker changed after reconciliation was prepared")
            os.unlink(name, dir_fd=active_root_fd)
            os.fsync(active_root_fd)
            record.update({"state": "COMPLETE", "marker_absent_verified": True,
                           "completed_at_unix": time.time()})
            _write_reconciliation_record(reconciliation_path, record, create=False)
            reconciled.append(record)
        pending = _pending_prefix_reconciliation_receipts()
        if pending:
            raise RuntimeError("PrefixLease reconciliation evidence is incomplete: " + ", ".join(pending))
        return reconciled
    finally:
        _release_prefix_reconciliation_locks(active_root_fd, gate_lock, allocation_lock)


def _pending_prefix_reconciliation_receipts() -> list[str]:
    try:
        root_fd = _open_directory_nofollow(PREFIX_RECONCILIATION_ROOT)
    except FileNotFoundError:
        return []
    except OSError:
        return [str(PREFIX_RECONCILIATION_ROOT)]
    pending: list[str] = []
    try:
        for name in os.listdir(root_fd):
            if not name.endswith(".json"):
                continue
            try:
                record, _raw, _info = _read_json_at(root_fd, name)
                if (not _verify_storage_evidence(record, "reconciliation_hmac_sha256")
                        or record.get("state") != "COMPLETE"
                        or record.get("experiment_id") != name[:-5]):
                    pending.append(str(PREFIX_RECONCILIATION_ROOT / name))
            except (OSError, ValueError, json.JSONDecodeError):
                pending.append(str(PREFIX_RECONCILIATION_ROOT / name))
    finally:
        os.close(root_fd)
    return sorted(pending)


def _pending_prefix_recovery_receipts() -> list[str]:
    try:
        root_fd = _open_directory_nofollow(PREFIX_RECOVERY_RECEIPTS_ROOT)
    except FileNotFoundError:
        return []
    except OSError:
        return [str(PREFIX_RECOVERY_RECEIPTS_ROOT)]
    pending: list[str] = []
    try:
        for name in os.listdir(root_fd):
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                record, _raw, _info = _read_json_at(root_fd, name)
                if (not _verify_storage_evidence(record, "recovery_receipt_hmac_sha256")
                        or record.get("current_state_reconciled") is not True
                        or record.get("historical_cleanup_certified") is not False
                        or record.get("receipt_type") != "CURRENT_STATE_STORAGE_RECOVERY"):
                    pending.append(str(PREFIX_RECOVERY_RECEIPTS_ROOT / name))
            except (OSError, ValueError, json.JSONDecodeError):
                pending.append(str(PREFIX_RECOVERY_RECEIPTS_ROOT / name))
    finally:
        os.close(root_fd)
    return sorted(pending)


def recover_interrupted_storage_smoke_lease_marker(
    experiment_id: str,
    *,
    active_root_fd: int | None = None,
) -> dict[str, Any]:
    """Recover an interrupted storage-only PrefixLease smoke marker when no live resources remain."""
    if not _is_storage_policy_smoke_id(experiment_id):
        raise ValueError(f"invalid smoke experiment ID for recovery: {experiment_id}")

    if active_root_fd is None:
        with storage_certification_guard(exclusive=True):
            root_fd, gate_lock, allocation_lock = _acquire_prefix_reconciliation_locks()
            try:
                return _recover_interrupted_storage_smoke_lease_marker_locked(experiment_id, root_fd)
            finally:
                _release_prefix_reconciliation_locks(root_fd, gate_lock, allocation_lock)
    else:
        return _recover_interrupted_storage_smoke_lease_marker_locked(experiment_id, active_root_fd)


def _recover_interrupted_storage_smoke_lease_marker_locked(
    experiment_id: str,
    active_root_fd: int,
) -> dict[str, Any]:
    PREFIX_RECOVERY_RECEIPTS_ROOT.mkdir(parents=True, exist_ok=True)
    PREFIX_RETIRED_MARKERS_ROOT.mkdir(parents=True, exist_ok=True)

    marker_name = f"{experiment_id}.json"
    marker_path = ACTIVE_LEASE_ROOT / marker_name
    retired_marker_path = PREFIX_RETIRED_MARKERS_ROOT / marker_name
    receipt_name = f"storage-recovery-receipt-{experiment_id}.json"
    receipt_path = PREFIX_RECOVERY_RECEIPTS_ROOT / receipt_name
    intent_name = f".intent-{experiment_id}.json"
    intent_path = PREFIX_RECOVERY_RECEIPTS_ROOT / intent_name

    expected_output = REPO / "experiments/storage/smoke-runs" / experiment_id
    expected_prefix = PREFIX_ROOT / f"storage-smoke-{experiment_id}"

    # Idempotence: if marker is already retired and receipt is valid, return existing receipt
    if _stat_child_nofollow(active_root_fd, marker_name) is None and retired_marker_path.exists() and receipt_path.exists():
        receipt_bytes = _read_file_nofollow(receipt_path)
        receipt = json.loads(receipt_bytes)
        if (_verify_storage_evidence(receipt, "recovery_receipt_hmac_sha256")
                and receipt.get("interrupted_smoke_id") == experiment_id
                and receipt.get("current_state_reconciled") is True
                and receipt.get("historical_cleanup_certified") is False):
            _assert_reconciled_smoke_paths_absent(experiment_id)
            return receipt

    # Precondition A: Marker checks
    if _stat_child_nofollow(active_root_fd, marker_name) is None:
        raise FileNotFoundError(f"active PrefixLease marker not found for recovery: {marker_name}")

    marker, marker_bytes, marker_info = _read_json_at(active_root_fd, marker_name)
    marker_hash = hashlib.sha256(marker_bytes).hexdigest()

    for req_key in ("experiment_id", "pid", "state", "prefix_path", "output_dir", "manifest_path", "manifest_sha256"):
        if req_key not in marker:
            raise ValueError(f"interrupted marker schema invalid, missing {req_key}: {marker_name}")

    if marker.get("experiment_id") != experiment_id:
        raise ValueError(f"marker experiment ID mismatch: {marker.get('experiment_id')} != {experiment_id}")

    if marker.get("state") not in {"MATERIALIZED", "RECOVERY_PENDING", "RECONCILIATION_REQUIRED"}:
        raise ValueError(f"marker state not allowed for recovery: {marker.get('state')}")

    expected_manifest = _experiment_manifest_path(experiment_id)
    if marker.get("manifest_path") != str(expected_manifest):
        raise ValueError(f"non-canonical manifest path in marker: {marker.get('manifest_path')}")
    manifest_bytes = _read_file_nofollow(expected_manifest)
    manifest = json.loads(manifest_bytes)
    if not _verify_manifest_signature(manifest):
        raise ValueError("manifest signature invalid")
    if manifest.get("experiment_id") != experiment_id:
        raise ValueError("manifest experiment_id mismatch")
    if manifest.get("storage_policy_smoke") is not True:
        raise ValueError("refusing to recover non-storage-smoke lease")
    if hashlib.sha256(manifest_bytes).hexdigest() != marker.get("manifest_sha256"):
        raise ValueError("marker manifest_sha256 does not match signed manifest")

    if marker.get("output_dir") != str(expected_output):
        raise ValueError(f"non-canonical output dir in marker: {marker.get('output_dir')}")
    if marker.get("prefix_path") != str(expected_prefix):
        raise ValueError(f"non-canonical prefix path in marker: {marker.get('prefix_path')}")

    # Check for existing complete cleanup receipt: if valid and complete, refuse recovery
    cleanup_path = expected_output / "prefix-cleanup.json"
    if cleanup_path.exists():
        try:
            cleanup_bytes = _read_file_nofollow(cleanup_path)
            cleanup = json.loads(cleanup_bytes)
            if (_verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
                    and cleanup.get("receipt_finalization_complete") is True
                    and cleanup.get("cleanup_error") is None):
                raise RuntimeError(
                    "historical cleanup receipt is valid and complete; use standard reconciliation instead of recovery"
                )
        except Exception as e:
            if "standard reconciliation" in str(e):
                raise

    # Precondition B: Process liveness checks
    pid = marker.get("pid")
    if not _pid_is_definitely_gone(pid):
        raise RuntimeError(f"interrupted smoke PID is still live or ambiguous: {pid}")

    ps_check = subprocess.run(["ps", "-p", str(pid), "-o", "pid="],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if ps_check.returncode == 0 and str(pid) in ps_check.stdout:
        raise RuntimeError(f"interrupted smoke PID confirmed alive by ps: {pid}")

    pgrep_wine = subprocess.run(["pgrep", "wineserver"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if pgrep_wine.returncode == 0 and pgrep_wine.stdout.strip():
        raise RuntimeError("a wineserver process is currently running on the system")

    # Precondition C: Prefix path absence
    _assert_reconciled_smoke_paths_absent(experiment_id)

    # Precondition D: Active builds / leases
    retention_directory = Path(__file__).resolve().parent
    if str(retention_directory) not in sys.path:
        sys.path.insert(0, str(retention_directory))
    import build_retention
    retention = build_retention.status_snapshot()
    if retention.get("active_build_count", 0) != 0:
        raise RuntimeError("active build leases exist during recovery")
    if retention.get("gate_status") != "PASS":
        raise RuntimeError("build retention reports gate blocker during recovery")
    active_markers = _active_lease_markers()
    other_active = [m for m in active_markers if m.stem != experiment_id]
    if other_active:
        raise RuntimeError(f"other active leases exist: {[str(m) for m in other_active]}")

    # Precondition E: Storage thresholds
    live_free = disk_free_bytes(REPO)
    if live_free < FREE_MINIMUM:
        raise RuntimeError(f"live free space below threshold: {live_free} < {FREE_MINIMUM}")
    live_project_usage = measure_project_usage()
    live_project = conservative_project_usage_bytes(live_project_usage)
    if live_project > GATE0_PROJECT_BUDGET:
        raise RuntimeError(f"live project usage above budget: {live_project} > {GATE0_PROJECT_BUDGET}")
    if len(preserved_prefixes()) > MAX_PRESERVED_PREFIXES:
        raise RuntimeError("preserved prefix count exceeds limit")
    if uncertified_prefixes():
        raise RuntimeError("uncertified prefixes exist")

    # Step 1: Record Recovery Intent
    intent_record = {
        "experiment_id": experiment_id,
        "marker_sha256": marker_hash,
        "state": "RECOVERY_INTENT_RECORDED",
        "timestamp_unix": time.time(),
    }
    _write_json(intent_path, intent_record, strict_directory_sync=True)

    # Step 2: Update active marker state to RECOVERY_PENDING
    marker_updated = dict(marker)
    marker_updated["state"] = "RECOVERY_PENDING"
    marker_updated["updated_at_unix"] = time.time()
    _write_json(marker_path, marker_updated, strict_directory_sync=True)

    # Step 3: Write signed recovery receipt
    recovery_code_hash = hashlib.sha256(_read_file_nofollow(Path(__file__).resolve())).hexdigest()
    control_hashes = _gate0_control_hashes()
    original_state = marker.get("state")

    receipt_record = {
        "schema_version": 1,
        "receipt_type": "CURRENT_STATE_STORAGE_RECOVERY",
        "recovery_receipt_version": "1.0",
        "interrupted_smoke_id": experiment_id,
        "original_marker_sha256": marker_hash,
        "original_marker_state": original_state,
        "original_prefix_path": str(expected_prefix),
        "original_cleanup_receipt_status": "MISSING",
        "original_process_identity": {
            "pid": pid,
            "manifest_sha256": marker.get("manifest_sha256"),
        },
        "current_process_liveness_result": {
            "pid": pid,
            "process_alive": False,
            "wineserver_alive": False,
            "verification_method": "os.kill_and_ps_process_lookup_error",
        },
        "current_prefix_existence_result": {
            "prefix_path": str(expected_prefix),
            "prefix_exists": False,
            "clone_temporary_paths_exist": False,
            "clone_temporary_paths": [],
        },
        "active_lease_inventory": [str(p) for p in active_markers],
        "build_inventory": {
            "active_build_count": retention.get("active_build_count", 0),
            "unknown_count": len(retention.get("unknown", [])),
            "gate_status": retention.get("gate_status"),
        },
        "prefix_inventory": {
            "preserved_prefix_count": len(preserved_prefixes()),
            "preserved_prefixes": [str(p) for p in preserved_prefixes()],
            "uncertified_prefix_count": len(uncertified_prefixes()),
            "uncertified_prefixes": [str(p) for p in uncertified_prefixes()],
        },
        "free_disk_bytes": live_free,
        "scoped_project_bytes": live_project,
        "verification_timestamp_unix": time.time(),
        "recovery_code_sha256": recovery_code_hash,
        "gate0_control_hashes": control_hashes,
        "historical_cleanup_certified": False,
        "current_state_reconciled": True,
        "recovery_statement": (
            "This receipt independently verifies current live storage safety. "
            "It does NOT certify or establish that historical POST cleanup completed normally."
        ),
        "recovery_state": "RECOVERED_CURRENT_STATE",
    }
    sealed_receipt = _seal_storage_evidence(receipt_record, "recovery_receipt_hmac_sha256")
    _write_json(receipt_path, sealed_receipt, strict_directory_sync=True)
    if expected_output.exists():
        _write_json(expected_output / receipt_name, sealed_receipt, strict_directory_sync=True)

    # Step 4: Verify receipt read-back
    read_back_bytes = _read_file_nofollow(receipt_path)
    read_back = json.loads(read_back_bytes)
    if not _verify_storage_evidence(read_back, "recovery_receipt_hmac_sha256"):
        raise RuntimeError("recovery receipt HMAC signature verification failed")

    # Step 5: Atomically preserve original marker in retired location
    retired_marker_path.write_bytes(marker_bytes)
    retired_dir_fd = _open_directory_nofollow(PREFIX_RETIRED_MARKERS_ROOT)
    try:
        os.fsync(retired_dir_fd)
    finally:
        os.close(retired_dir_fd)

    # Unlink active marker from active directory
    os.unlink(marker_name, dir_fd=active_root_fd)
    os.fsync(active_root_fd)

    # Write companion retirement metadata
    retired_meta = {
        "experiment_id": experiment_id,
        "retired_at_unix": time.time(),
        "original_marker_sha256": marker_hash,
        "recovery_receipt_path": str(receipt_path),
        "recovery_receipt_sha256": hashlib.sha256(read_back_bytes).hexdigest(),
        "state": "RECOVERED_CURRENT_STATE",
    }
    _write_json(PREFIX_RETIRED_MARKERS_ROOT / f"{experiment_id}.meta.json", retired_meta, strict_directory_sync=True)

    # Step 6: Verify active marker is absent from active lease inventory
    remaining_active = _active_lease_markers()
    if any(p.stem == experiment_id for p in remaining_active):
        raise RuntimeError(f"marker still present in active lease set after retirement: {experiment_id}")

    # Step 7: Clean up intent
    if intent_path.exists():
        try:
            os.unlink(intent_path)
        except OSError:
            pass

    return sealed_receipt


def assert_prefix_lease(prefix: Path | str, wine_loader: Path | str | None = None,
                        runner_path: Path | str | None = None,
                        child_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate a live PrefixLease while serializing against managed storage admission."""
    allocation_lock = _acquire_storage_allocation_lock()
    try:
        return _assert_prefix_lease_locked(prefix, wine_loader, runner_path, child_env)
    finally:
        _release_storage_allocation_lock(allocation_lock)


def _assert_prefix_lease_locked(prefix: Path | str, wine_loader: Path | str | None = None,
                                runner_path: Path | str | None = None,
                                child_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Refuse legacy/standalone Wine runners unless PrefixLease materialized this prefix."""
    if runner_path is None:
        raise RuntimeError("Wine runner preflight requires its exact authorized source path")
    selected_runner_path = Path(runner_path).expanduser().absolute()
    try:
        resolved_runner_path = selected_runner_path.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"Wine runner source is unavailable: {runner_path}") from error
    if (selected_runner_path.is_symlink() or selected_runner_path != resolved_runner_path
            or not resolved_runner_path.is_file() or REPO.resolve() not in resolved_runner_path.parents):
        raise RuntimeError("Wine runner source must be a real canonical repository file")
    raw_prefix = os.fspath(prefix)
    if (not isinstance(raw_prefix, str)
            or not Path(raw_prefix).is_absolute()
            or os.path.abspath(raw_prefix) != raw_prefix
            or any(part == ".." for part in raw_prefix.split(os.sep))):
        raise RuntimeError("Wine runner prefix must be an exact canonical absolute path")
    prefix = Path(raw_prefix)
    marker = prefix / "FGMETAL_PREFIX_LEASE.json"
    try:
        prefix_fd = _open_directory_nofollow(prefix)
        try:
            prefix_info = os.fstat(prefix_fd)
            prefix_payload, marker_bytes, prefix_marker_info = _read_json_at(
                prefix_fd, marker.name, maximum_bytes=1024 * 1024)
        finally:
            os.close(prefix_fd)
    except OSError as error:
        raise RuntimeError(f"Wine runner prefix path is unsafe or missing: {prefix}: {error}") from error
    if prefix_info is None or not stat.S_ISDIR(prefix_info.st_mode):
        raise RuntimeError(f"Wine runner requires a live storage-managed PrefixLease: {prefix}")
    prefix_identity = (prefix_info.st_dev, prefix_info.st_ino)
    prefix_marker_identity = (
        prefix_marker_info.st_dev, prefix_marker_info.st_ino, prefix_marker_info.st_size,
        prefix_marker_info.st_mtime_ns, getattr(prefix_marker_info, "st_ctime_ns", 0))
    if (prefix_marker_info.st_uid != os.getuid() or prefix_marker_info.st_nlink != 1
            or stat.S_IMODE(prefix_marker_info.st_mode) != 0o600):
        raise RuntimeError("Wine prefix lease marker is not a private, singly-linked regular file")
    payload = prefix_payload
    lease_token = payload.get("lease_token")
    if (payload.get("prefix_path") != str(prefix)
            or payload.get("pid") != os.getpid()
            or payload.get("experiment_id") is None
            or not isinstance(lease_token, str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease_token)):
        raise RuntimeError("Wine prefix lease marker names a different prefix path")
    manifest_path_value = payload.get("manifest_path")
    if not isinstance(manifest_path_value, str):
        raise RuntimeError("Wine prefix lease manifest is missing or unsafe")
    manifest_path = Path(manifest_path_value)
    try:
        expected_manifest_path = _experiment_manifest_path(str(payload.get("experiment_id", "")))
    except ValueError as error:
        raise RuntimeError(f"Wine prefix lease has an invalid experiment ID: {error}") from error
    if (manifest_path_value != str(expected_manifest_path)
            or not manifest_path.is_absolute()
            or str(manifest_path) != os.path.abspath(manifest_path_value)):
        raise RuntimeError("Wine prefix lease manifest is missing or unsafe")
    try:
        manifest_bytes = _read_file_nofollow(manifest_path)
    except OSError as error:
        raise RuntimeError(f"Wine prefix lease manifest is unsafe or missing: {error}") from error
    if hashlib.sha256(manifest_bytes).hexdigest() != payload.get("manifest_sha256"):
        raise RuntimeError("Wine prefix lease manifest hash no longer matches")
    manifest = json.loads(manifest_bytes)
    if not _verify_manifest_signature(manifest):
        raise RuntimeError("Wine experiment manifest signature is missing or invalid")
    try:
        _validated_manifest_estimates(manifest)
    except ValueError as error:
        raise RuntimeError(f"Wine experiment storage estimates are invalid: {error}") from error
    if (manifest.get("decision") != "ALLOW"
            or manifest.get("experiment_id") != payload.get("experiment_id")
            or manifest.get("output_dir") != payload.get("output_dir")):
        raise RuntimeError("Wine prefix lease does not bind to an allowed experiment manifest")
    if (payload.get("manifest_path") != str(expected_manifest_path)
            or payload.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest()):
        raise RuntimeError("Wine prefix lease marker does not bind to the current manifest")
    if (payload.get("output_dir") != manifest.get("output_dir")
            or payload.get("experiment_id") != manifest.get("experiment_id")):
        raise RuntimeError("Wine prefix lease marker identity differs from its manifest")
    if manifest.get("internal_storage_setup") is True:
        raise RuntimeError("legacy internal-storage manifests cannot bypass Gate 0")
    try:
        output_dir = _measured_output_dir(manifest.get("output_dir", ""))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Wine experiment output is outside measured storage: {error}") from error
    if (str(output_dir) != manifest.get("output_dir")
            or payload.get("output_dir") != str(output_dir)):
        raise RuntimeError("Wine experiment output path is not canonical")
    attestation_hash = hashlib.sha256(_read_file_nofollow(GATE0_ATTESTATION_PATH)).hexdigest() \
        if GATE0_ATTESTATION_PATH.is_file() and not GATE0_ATTESTATION_PATH.is_symlink() else None
    if manifest.get("gate0_attestation_sha256") != attestation_hash:
        raise RuntimeError("Wine prefix manifest does not bind to the current Gate 0 attestation")
    storage_smoke = (manifest.get("storage_policy_smoke") is True
                     and _is_storage_policy_smoke_id(manifest.get("experiment_id")))
    if manifest.get("storage_policy_smoke") is True and not storage_smoke:
        raise RuntimeError("storage smoke marker is not bound to the one permitted experiment ID")
    active_path = ACTIVE_LEASE_ROOT / f"{payload.get('experiment_id')}.json"
    active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
    try:
        active_parent_info = os.fstat(active_root_fd)
        active_parent_identity = (active_parent_info.st_dev, active_parent_info.st_ino)
        active, active_bytes, active_file_info = _read_json_at(
            active_root_fd, active_path.name, maximum_bytes=1024 * 1024)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Wine prefix has no safe active PrefixLease record: {error}") from error
    finally:
        os.close(active_root_fd)
    active_file_identity = (
        active_file_info.st_dev, active_file_info.st_ino, active_file_info.st_size,
        active_file_info.st_mtime_ns, getattr(active_file_info, "st_ctime_ns", 0))
    preliminary_receipt = active.get("materialization_receipt")
    if (isinstance(preliminary_receipt, Mapping)
            and _verify_storage_evidence(
                preliminary_receipt, "prefix_materialization_hmac_sha256")
            and (preliminary_receipt.get("active_marker_parent_device"),
                 preliminary_receipt.get("active_marker_parent_inode")) != active_parent_identity):
        raise OSError("active PrefixLease marker parent changed after admission")
    if (active_file_info.st_uid != os.getuid() or active_file_info.st_nlink != 1
            or stat.S_IMODE(active_file_info.st_mode) != 0o600):
        raise RuntimeError("active PrefixLease marker is not a private, singly-linked regular file")
    if (active.get("experiment_id") != payload.get("experiment_id")
            or active.get("pid") != payload.get("pid")
            or active.get("prefix_path") != str(prefix)
            or active.get("output_dir") != payload.get("output_dir")
            or active.get("manifest_path") != str(expected_manifest_path)
            or active.get("manifest_sha256") != payload.get("manifest_sha256")
            or active.get("lease_token") != lease_token
            or active.get("state") != "MATERIALIZED"):
        raise RuntimeError("active PrefixLease record does not match the materialized prefix")
    receipt = _validate_materialized_active_prefix_marker(
        active, active_bytes, active_file_info)
    template_marker_path = PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json"
    if template_marker_path.is_symlink() or not template_marker_path.is_file():
        raise RuntimeError("certified Wine template marker is unavailable")
    template_marker = json.loads(template_marker_path.read_text(encoding="utf-8"))
    runtime_identity = template_marker.get("runtime_identity", {})
    candidate_hashes = manifest.get("candidate_hashes", {})
    if candidate_hashes.get("wine_runtime_tree") != runtime_identity.get("wine_runtime_tree_sha256"):
        raise RuntimeError("manifest Wine runtime tree differs from the certified template")
    if candidate_hashes.get("wine_binary") != runtime_identity.get("wine_binary_sha256"):
        raise RuntimeError("manifest Wine binary differs from the certified template")
    if candidate_hashes.get("moltenvk") != runtime_identity.get("moltenvk_sha256_for_subsequent_graphics_runs"):
        raise RuntimeError("manifest MoltenVK candidate differs from the certified template")
    selected_env = dict(child_env if child_env is not None else os.environ)
    _validate_wine_child_environment(selected_env)
    selected_loader = Path(wine_loader or selected_env.get("WINELOADER", "")).expanduser().absolute()
    runtime_root = Path(runtime_identity.get("wine_runtime_root", "")).expanduser().absolute()
    if (not selected_loader.is_file() or selected_loader != runtime_root / "bin/wine"
            or hashlib.sha256(selected_loader.read_bytes()).hexdigest() != runtime_identity.get("wine_binary_sha256")):
        raise RuntimeError("Wine runner does not select the certified Wine binary")
    if Path(selected_env.get("WINELOADER", "")).expanduser().absolute() != selected_loader:
        raise RuntimeError("Wine child environment WINELOADER differs from the certified Wine binary")
    if selected_env.get("WINEPREFIX") != str(prefix):
        raise RuntimeError("Wine runner WINEPREFIX does not match the active storage-managed lease")
    if Path(selected_env.get("WINESERVER", "")).expanduser().absolute() != runtime_root / "bin/wineserver":
        raise RuntimeError("Wine runner WINESERVER does not match the certified runtime")
    allowed_runners = {str(Path(path).expanduser().resolve(strict=True))
                       for path in manifest.get("authorized_runner_paths", [])}
    if str(resolved_runner_path) not in allowed_runners:
        raise RuntimeError(f"runner is not authorized by this experiment manifest: {resolved_runner_path}")
    runner_hashes = manifest.get("authorized_runner_sha256")
    try:
        current_runner_hash = hashlib.sha256(_read_file_nofollow(resolved_runner_path)).hexdigest()
    except OSError as error:
        raise RuntimeError(f"authorized runner source cannot be safely read: {error}") from error
    if not isinstance(runner_hashes, Mapping) or runner_hashes.get(str(resolved_runner_path)) != current_runner_hash:
        raise RuntimeError("authorized runner source changed after the storage manifest was written")
    selected_prefix = selected_env.get("WINEPREFIX")
    if selected_prefix != str(prefix):
        raise RuntimeError("Wine child environment WINEPREFIX does not match its leased prefix")
    selected_server = Path(selected_env.get("WINESERVER", "")).expanduser().absolute()
    if selected_server != runtime_root / "bin/wineserver":
        raise RuntimeError("Wine child environment WINESERVER differs from the certified runtime")
    prefix_identity_now = _stat_entry_nofollow(prefix)
    current_inventory_bytes = _read_file_nofollow(INVENTORY_PATH)
    inventory = json.loads(current_inventory_bytes)
    estimates = _validated_manifest_estimates(manifest)
    if (not isinstance(receipt, Mapping)
            or not _verify_storage_evidence(receipt, "prefix_materialization_hmac_sha256")
            or receipt.get("schema_version") != 1
            or receipt.get("experiment_id") != payload.get("experiment_id")
            or receipt.get("pid") != os.getpid()
            or receipt.get("lease_token") != lease_token
            or receipt.get("prefix_path") != str(prefix)
            or receipt.get("output_dir") != str(output_dir)
            or receipt.get("active_marker_path") != str(active_path)
            or (receipt.get("active_marker_parent_device"), receipt.get("active_marker_parent_inode"))
            != active_parent_identity
            or receipt.get("manifest_path") != str(expected_manifest_path)
            or receipt.get("manifest_sha256") != payload.get("manifest_sha256")
            or receipt.get("gate0_attestation_sha256") != attestation_hash
            or receipt.get("inventory_sha256") != hashlib.sha256(current_inventory_bytes).hexdigest()
            or receipt.get("inventory_generated_at_unix") != inventory.get("generated_at_unix")
            or prefix_identity_now is None
            or (prefix_identity_now.st_dev, prefix_identity_now.st_ino)
            != (receipt.get("prefix_device"), receipt.get("prefix_inode"))
            or receipt.get("expected_prefix_bytes") != estimates["expected_prefix_bytes"]
            or receipt.get("estimated_persistent_bytes") != estimates["estimated_persistent_bytes"]
            or receipt.get("estimated_projected_new_disk_bytes")
            != estimates["estimated_projected_new_disk_bytes"]):
        raise RuntimeError("active PrefixLease materialization receipt does not bind this lease, manifest, inventory, and prefix identity")
    numeric_receipt_fields = (
        "pre_materialization_free_bytes", "post_materialization_free_bytes",
        "pre_materialization_nonlease_scope_bytes", "post_materialization_nonlease_scope_bytes",
        "gate0_attested_observed_scope_bytes", "gate0_attested_nonlease_scope_bytes",
        "pre_materialization_output_allocated_bytes", "post_materialization_output_allocated_bytes",
        "materialized_prefix_allocated_bytes", "expected_prefix_bytes",
        "estimated_persistent_bytes", "estimated_projected_new_disk_bytes",
        "post_materialization_scope_bytes", "created_at_unix",
    )
    for name in numeric_receipt_fields:
        value = receipt.get(name)
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or value < 0):
            raise RuntimeError(f"active PrefixLease materialization receipt has an invalid {name}")
    if (receipt["materialized_prefix_allocated_bytes"] > receipt["expected_prefix_bytes"]
            or receipt["pre_materialization_output_allocated_bytes"]
            > receipt["estimated_persistent_bytes"]
            or receipt["post_materialization_output_allocated_bytes"]
            > receipt["estimated_persistent_bytes"]):
        raise RuntimeError("active PrefixLease materialization receipt exceeds a signed allocation reservation")
    receipt_age = time.time() - float(receipt["created_at_unix"])
    if receipt_age < 0 or receipt_age > GATE0_MAX_AGE_SECONDS:
        raise RuntimeError("active PrefixLease materialization receipt is stale")
    storage_smoke = (manifest.get("storage_policy_smoke") is True
                     and _is_storage_policy_smoke_id(manifest.get("experiment_id")))
    observations: dict[str, Any] = {}
    gate_blockers = _gate0_blockers(active_experiment_id=payload.get("experiment_id"),
                                    allow_storage_smoke=storage_smoke,
                                    active_prefix_receipt=receipt,
                                    measurements=observations)
    if gate_blockers:
        raise RuntimeError("Gate 0 is no longer valid: " + "; ".join(gate_blockers))
    remaining_admission = _post_materialization_prefix_admission(
        current_free_bytes=observations["free_bytes"],
        current_scoped_bytes=observations["conservative_project_bytes"],
        manifest_estimates=estimates,
    )
    if not remaining_admission["admitted"]:
        raise RuntimeError("Gate 0 projected storage recheck blocks Wine runner preflight: "
                           + "; ".join(remaining_admission["blockers"]))
    active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
    try:
        current_active_parent_info = os.fstat(active_root_fd)
        current_active_parent_identity = (current_active_parent_info.st_dev,
                                          current_active_parent_info.st_ino)
        current_active, current_active_bytes, current_active_info = _read_json_at(
            active_root_fd, active_path.name, maximum_bytes=1024 * 1024)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"active PrefixLease marker changed during runner preflight: {error}") from error
    finally:
        os.close(active_root_fd)
    current_active_identity = (
        current_active_info.st_dev, current_active_info.st_ino, current_active_info.st_size,
        current_active_info.st_mtime_ns, getattr(current_active_info, "st_ctime_ns", 0))
    if (current_active_parent_identity != active_parent_identity
            or current_active_identity != active_file_identity or current_active != active
            or current_active_bytes != active_bytes):
        raise RuntimeError("active PrefixLease marker identity or receipt changed during runner preflight")
    _validate_materialized_active_prefix_marker(
        current_active, current_active_bytes, current_active_info)
    prefix_fd = _open_directory_nofollow(prefix)
    try:
        current_prefix_info = os.fstat(prefix_fd)
        current_prefix_payload, current_prefix_bytes, current_prefix_marker_info = _read_json_at(
            prefix_fd, marker.name, maximum_bytes=1024 * 1024)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Wine prefix lease marker changed during runner preflight: {error}") from error
    finally:
        os.close(prefix_fd)
    current_prefix_marker_identity = (
        current_prefix_marker_info.st_dev, current_prefix_marker_info.st_ino,
        current_prefix_marker_info.st_size, current_prefix_marker_info.st_mtime_ns,
        getattr(current_prefix_marker_info, "st_ctime_ns", 0))
    if ((current_prefix_info.st_dev, current_prefix_info.st_ino) != prefix_identity
            or current_prefix_marker_identity != prefix_marker_identity
            or current_prefix_bytes != marker_bytes or current_prefix_payload != payload):
        raise RuntimeError("Wine prefix identity or lease marker changed during runner preflight")
    return payload


_ALLOWED_DYLD_ENVIRONMENT_KEYS = {
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_PRINT_LIBRARIES",
}
_FORBIDDEN_PROCESS_ENVIRONMENT_KEYS = {
    "BASH_ENV", "ENV", "GCONV_PATH", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT",
}
_LEGACY_DYLD_MOLTENVK_ALIAS = Path("/private/tmp/fgmetal-vulkan-link")
_MOLTENVK_DYLIB_NAMES = ("libMoltenVK.dylib", "libMoltenVK.1.dylib")
_MOLTENVK_CANDIDATE_DIRECTORY_NAMES = frozenset((*_MOLTENVK_DYLIB_NAMES, "libvulkan.dylib"))
_TRACE_PROXY_BINDING_KEYS = (
    "FG_B_TRACE_PROXY_PATH", "FG_B_TRACE_PROXY_SHA256",
    "FG_B_ORIGINAL_MOLTENVK_LIBRARY", "FG_B_ORIGINAL_MOLTENVK_SHA256",
)


def _verified_regular_file_identity(path_value: str, expected_sha256: str,
                                    description: str) -> Path:
    if (not isinstance(path_value, str) or not path_value
            or not Path(path_value).is_absolute()
            or str(Path(path_value)) != path_value
            or os.path.abspath(path_value) != path_value
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise ValueError(f"Wine child {description} binding is missing or malformed")
    path = Path(path_value)
    info = _stat_entry_nofollow(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Wine child {description} is missing or not a regular file: {path}")
    try:
        digest = hashlib.sha256(_read_file_nofollow(path)).hexdigest()
    except OSError as error:
        raise ValueError(f"Wine child {description} cannot be read safely: {path}") from error
    if digest != expected_sha256:
        raise ValueError(f"Wine child {description} SHA-256 does not match its binding: {path}")
    return path


def _validate_wine_child_environment(env: Mapping[str, str]) -> None:
    """Reject dynamic-loader and interpreter hooks before passing an env to Wine."""
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key or "\x00" in value:
            raise ValueError("Wine child environment must contain NUL-free string keys and values")
        normalized_key = key.upper()
        if (normalized_key.startswith("DYLD_") and key not in _ALLOWED_DYLD_ENVIRONMENT_KEYS):
            raise ValueError(f"Wine child environment contains a forbidden dynamic-loader control: {key}")
        if (normalized_key.startswith(("LD_", "__XPC_DYLD_", "PYTHON"))
                or key in _FORBIDDEN_PROCESS_ENVIRONMENT_KEYS):
            raise ValueError(f"Wine child environment contains a forbidden process startup hook: {key}")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _verified_moltenvk_target(
    path: Path,
    expected_sha256: str,
    *,
    allow_different_hash: bool = False,
) -> tuple[Path, bool, bool] | None:
    """Return a candidate library's real parent and whether the leaf is a symlink."""
    info = _stat_entry_nofollow(path)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        parent_fd = _open_directory_nofollow(path.parent)
        try:
            link_target = os.readlink(path.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        target = Path(link_target)
        if (str(target) != link_target
                or any(part in {".", ".."} for part in link_target.split(os.sep))):
            raise ValueError(f"Wine DYLD candidate symlink target is not canonical: {path}")
        if not target.is_absolute():
            target = path.parent / target
        absolute_target = os.path.abspath(target)
        if str(target) != absolute_target:
            raise ValueError(f"Wine DYLD candidate symlink target is not canonical: {path}")
        target = Path(absolute_target)
        try:
            resolved_target = target.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"Wine DYLD candidate symlink is broken: {path}") from error
        if resolved_target != target:
            raise ValueError(f"Wine DYLD candidate symlink target is not canonical: {path}")
        target_info = _stat_entry_nofollow(target)
        if target_info is None or not stat.S_ISREG(target_info.st_mode):
            raise ValueError(f"Wine DYLD candidate symlink does not select a regular file: {path}")
        try:
            digest = hashlib.sha256(_read_file_nofollow(target)).hexdigest()
        except OSError as error:
            raise ValueError(f"Wine DYLD candidate symlink target cannot be read safely: {path}") from error
        if digest != expected_sha256 and not allow_different_hash:
            raise ValueError(f"Wine DYLD candidate symlink does not select the certified MoltenVK binary: {path}")
        return target.parent, True, digest == expected_sha256
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Wine DYLD candidate is not a regular file: {path}")
    try:
        digest = hashlib.sha256(_read_file_nofollow(path)).hexdigest()
    except OSError as error:
        raise ValueError(f"Wine DYLD candidate cannot be read safely: {path}") from error
    if digest != expected_sha256 and not allow_different_hash:
        raise ValueError(f"Wine DYLD candidate differs from the certified MoltenVK binary: {path}")
    return path.parent, False, digest == expected_sha256


def _verify_candidate_directory(path: Path, expected_sha256: str) -> None:
    """Require a candidate directory to contain only the certified loader names."""
    directory_fd = _open_directory_nofollow(path)
    try:
        names = set(os.listdir(directory_fd))
        if not names or not names <= _MOLTENVK_CANDIDATE_DIRECTORY_NAMES:
            raise ValueError(f"Wine DYLD candidate directory contains unapproved entries: {path}")
        for name in names:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Wine DYLD candidate directory contains a non-regular entry: {path / name}")
            candidate = path / name
            try:
                digest = hashlib.sha256(_read_file_nofollow(candidate)).hexdigest()
            except OSError as error:
                raise ValueError(f"Wine DYLD candidate cannot be read safely: {candidate}") from error
            if digest != expected_sha256:
                raise ValueError(f"Wine DYLD candidate differs from the certified MoltenVK binary: {candidate}")
    finally:
        os.close(directory_fd)


def _normalize_wine_dyld_environment(
    env: Mapping[str, str],
    *,
    expected_moltenvk_sha256: str,
    trusted_roots: tuple[Path, ...],
    candidate_fallback_roots: tuple[Path, ...] = (),
) -> dict[str, str]:
    """Validate DYLD lists, including the exact BR2 proxy-to-MoltenVK binding."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_moltenvk_sha256):
        raise ValueError("certified MoltenVK SHA-256 is missing or malformed")
    result = dict(env)
    components_by_key: dict[str, list[Path]] = {}
    for key in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        if key not in env:
            continue
        value = env[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"Wine child {key} must be a non-empty path list")
        components: list[Path] = []
        for raw_component in value.split(":"):
            if not raw_component or "\x00" in raw_component:
                raise ValueError(f"Wine child {key} contains an empty or malformed path component")
            component = Path(raw_component)
            if (not component.is_absolute()
                    or str(component) != raw_component
                    or os.path.abspath(raw_component) != raw_component):
                raise ValueError(f"Wine child {key} contains a non-canonical absolute path: {raw_component}")
            directory_fd = _open_directory_nofollow(component)
            try:
                if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                    raise ValueError(f"Wine child {key} component is not a directory: {component}")
            finally:
                os.close(directory_fd)
            components.append(component)
        components_by_key[key] = components
    if not components_by_key:
        raise ValueError("Wine child environment has no MoltenVK DYLD search path")

    binding_values = [env.get(key) for key in _TRACE_PROXY_BINDING_KEYS]
    proxy_path: Path | None = None
    original_mvk_path: Path | None = None
    if any(value is not None for value in binding_values):
        if not all(isinstance(value, str) and value for value in binding_values):
            raise ValueError("Wine child trace proxy binding must provide all four exact identity fields")
        proxy_path = _verified_regular_file_identity(
            str(env["FG_B_TRACE_PROXY_PATH"]), str(env["FG_B_TRACE_PROXY_SHA256"]),
            "trace proxy",
        )
        original_mvk_path = _verified_regular_file_identity(
            str(env["FG_B_ORIGINAL_MOLTENVK_LIBRARY"]),
            str(env["FG_B_ORIGINAL_MOLTENVK_SHA256"]), "MoltenVK delegate",
        )
        if (proxy_path.name != "libMoltenVK.dylib"
                or original_mvk_path.name not in _MOLTENVK_DYLIB_NAMES
                or proxy_path == original_mvk_path
                or proxy_path.parent == original_mvk_path.parent):
            raise ValueError("Wine child trace proxy and MoltenVK delegate paths are not distinct approved loader targets")
        library_components = components_by_key.get("DYLD_LIBRARY_PATH", [])
        if not library_components or library_components[0] != proxy_path.parent:
            raise ValueError("BR2 trace proxy directory must be the first DYLD_LIBRARY_PATH component")
        proxy_directory_fd = _open_directory_nofollow(proxy_path.parent)
        try:
            if set(os.listdir(proxy_directory_fd)) != {"libMoltenVK.dylib"}:
                raise ValueError("BR2 trace proxy directory contains unapproved entries")
        finally:
            os.close(proxy_directory_fd)
        delegate_digest = hashlib.sha256(_read_file_nofollow(original_mvk_path)).hexdigest()
        if delegate_digest != expected_moltenvk_sha256:
            raise ValueError("BR2 trace proxy delegate differs from the certified MoltenVK binary")

    direct_candidate_roots: list[Path] = []
    alias_targets: list[Path] = []
    conflicting_components: set[Path] = set()
    all_components = [component for components in components_by_key.values() for component in components]
    for component in all_components:
        is_legacy_alias = component == _LEGACY_DYLD_MOLTENVK_ALIAS
        if is_legacy_alias:
            directory_fd = _open_directory_nofollow(component)
            try:
                if set(os.listdir(directory_fd)) != {"libMoltenVK.dylib"}:
                    raise ValueError("legacy Wine DYLD alias contains unapproved files")
            finally:
                os.close(directory_fd)
        for name in _MOLTENVK_DYLIB_NAMES:
            candidate = component / name
            if proxy_path is not None and candidate == proxy_path:
                continue
            if is_legacy_alias and name != "libMoltenVK.dylib":
                continue
            allow_different_hash = any(_path_is_within(component, root)
                                       for root in candidate_fallback_roots)
            found = _verified_moltenvk_target(
                candidate, expected_moltenvk_sha256,
                allow_different_hash=allow_different_hash and not is_legacy_alias,
            )
            if found is None:
                continue
            candidate_root, is_symlink, matches_candidate = found
            if not matches_candidate:
                conflicting_components.add(component)
                continue
            if is_symlink:
                if not is_legacy_alias:
                    raise ValueError(f"Wine DYLD candidate symlink is outside the exact approved alias: {candidate}")
                alias_targets.append(candidate_root)
            else:
                direct_candidate_roots.append(candidate_root)
    candidate_roots = list(dict.fromkeys((*direct_candidate_roots, *alias_targets)))
    if not candidate_roots:
        raise ValueError("Wine child DYLD paths do not resolve the certified MoltenVK binary")
    if len(candidate_roots) != 1:
        raise ValueError("Wine child DYLD paths expose duplicate certified MoltenVK candidate directories")
    for candidate_root in candidate_roots:
        _verify_candidate_directory(candidate_root, expected_moltenvk_sha256)
    primary_candidate_root = direct_candidate_roots[0] if direct_candidate_roots else candidate_roots[0]

    approved_roots = tuple(dict.fromkeys((*trusted_roots, *candidate_roots)))
    for key, components in components_by_key.items():
        normalized: list[str] = []
        seen: set[str] = set()
        candidate_seen = False
        for component in components:
            selected = primary_candidate_root if component == _LEGACY_DYLD_MOLTENVK_ALIAS else component
            if component != _LEGACY_DYLD_MOLTENVK_ALIAS and not any(
                    _path_is_within(component, root) for root in approved_roots):
                raise ValueError(f"Wine child {key} contains an unapproved search directory: {component}")
            if component in conflicting_components and not candidate_seen:
                raise ValueError(
                    f"certified MoltenVK search directory must precede the Wine runtime fallback: {component}"
                )
            if selected in candidate_roots:
                candidate_seen = True
            selected_text = str(selected)
            if selected_text not in seen:
                normalized.append(selected_text)
                seen.add(selected_text)
        result[key] = ":".join(normalized)
    return result


def _wine_loader_identity(path: Path) -> tuple[str, tuple[int, int, int, int, int]]:
    """Hash a real executable without following its final path component."""
    if not path.is_absolute() or str(path) != str(Path(os.path.abspath(path))):
        raise ValueError("certified Wine executable path is not canonical and absolute")
    before = _stat_entry_nofollow(path)
    if before is None or not stat.S_ISREG(before.st_mode) or not (before.st_mode & 0o111):
        raise ValueError("certified Wine executable is missing, not regular, or not executable")
    try:
        digest = hashlib.sha256(_read_file_nofollow(path)).hexdigest()
    except OSError as error:
        raise ValueError("certified Wine executable cannot be read safely") from error
    after = _stat_entry_nofollow(path)
    if after is None:
        raise ValueError("certified Wine executable disappeared while it was verified")
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                       getattr(before, "st_ctime_ns", 0))
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                      getattr(after, "st_ctime_ns", 0))
    if before_identity != after_identity:
        raise ValueError("certified Wine executable changed while it was verified")
    return digest, after_identity


def _wine_graphics_identity_required(manifest: Mapping[str, Any]) -> bool:
    """Return whether this Wine lease carries the stricter graphics identity contract."""
    candidate_hashes = manifest.get("candidate_hashes", {})
    if not isinstance(candidate_hashes, Mapping):
        candidate_hashes = {}
    return (manifest.get("experiment_step") == "11D.3-B"
            or any(key in manifest for key in (
                "wine_launcher_path", "wine_launcher_sha256",
                "wine_host_image_path", "wine_host_image_sha256",
                "wine_runtime_tree_manifest_sha256"))
            or "wine_host_image" in candidate_hashes)


def _wine_graphics_image_identity(
    path: Path,
    expected_sha256: str,
    *,
    role: str,
) -> tuple[str, tuple[int, int, int, int, int]]:
    """Bind one canonical, executable, thin x86_64 Wine image without following aliases."""
    try:
        digest, identity = _bind_native_file_identity(
            path, expected_sha256, verify_macho=True, expected_architecture="x86_64")
        info = _stat_entry_nofollow(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"certified Wine {role} is missing, unsafe, or not thin x86_64") from error
    if (info is None or not stat.S_ISREG(info.st_mode) or not (info.st_mode & 0o111)
            or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                getattr(info, "st_ctime_ns", 0)) != identity):
        raise ValueError(f"certified Wine {role} is not a stable executable")
    return digest, identity


def _wine_graphics_identity_contract(
    manifest: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Validate the signed launcher/host split and bind both images to the pinned runtime manifest."""
    if not _wine_graphics_identity_required(manifest):
        return None

    candidate_hashes = manifest.get("candidate_hashes")
    if not isinstance(candidate_hashes, Mapping):
        raise ValueError("graphics Wine manifest has no candidate identity mapping")
    runtime_root_value = runtime_identity.get("wine_runtime_root")
    runtime_manifest_value = runtime_identity.get("wine_runtime_tree_manifest")
    if (not isinstance(runtime_root_value, str)
            or not Path(runtime_root_value).is_absolute()
            or str(Path(runtime_root_value)) != runtime_root_value
            or os.path.abspath(runtime_root_value) != runtime_root_value):
        raise ValueError("graphics Wine runtime root must be canonical and absolute")
    runtime_root = Path(runtime_root_value)
    launcher_path_value = manifest.get("wine_launcher_path")
    launcher_sha256 = manifest.get("wine_launcher_sha256")
    host_path_value = manifest.get("wine_host_image_path")
    host_sha256 = manifest.get("wine_host_image_sha256")
    manifest_sha256 = manifest.get("wine_runtime_tree_manifest_sha256")
    expected_launcher = runtime_root / "bin/wine"
    expected_host = runtime_root / "lib/wine/x86_64-unix/wine"
    for label, path_value, expected_path in (
            ("launcher", launcher_path_value, expected_launcher),
            ("host image", host_path_value, expected_host)):
        if (not isinstance(path_value, str) or not Path(path_value).is_absolute()
                or str(Path(path_value)) != path_value
                or os.path.abspath(path_value) != path_value
                or Path(path_value) != expected_path):
            raise ValueError(f"graphics Wine {label} path is not the exact canonical runtime path")
    for label, digest in (("launcher", launcher_sha256), ("host image", host_sha256),
                          ("runtime manifest", manifest_sha256)):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"graphics Wine {label} SHA-256 is missing or malformed")

    runtime_tree_sha256 = runtime_identity.get("wine_runtime_tree_sha256")
    if (not isinstance(runtime_tree_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", runtime_tree_sha256) is None
            or candidate_hashes.get("wine_runtime_tree") != runtime_tree_sha256):
        raise ValueError("graphics Wine runtime tree does not match the certified template")
    launcher_template_sha256 = runtime_identity.get("wine_binary_sha256")
    if (not isinstance(launcher_template_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", launcher_template_sha256) is None
            or launcher_sha256 != launcher_template_sha256
            or candidate_hashes.get("wine_binary") != launcher_template_sha256
            or candidate_hashes.get("wine_launcher") != launcher_template_sha256
            or candidate_hashes.get("wine_launcher_binary", launcher_template_sha256)
            != launcher_template_sha256):
        raise ValueError("graphics Wine binary identity must bind the certified bin/wine launcher")
    if candidate_hashes.get("wine_host_image") != host_sha256:
        raise ValueError("graphics Wine host image identity does not match its separate candidate hash")

    pinned_manifest = WINE_RUNTIME_TREE_MANIFEST
    if (not isinstance(runtime_manifest_value, str)
            or runtime_manifest_value != str(pinned_manifest)
            or str(pinned_manifest) != os.path.abspath(pinned_manifest)
            or Path(runtime_manifest_value).is_symlink()):
        raise ValueError("graphics Wine runtime manifest is not the exact pinned canonical manifest")
    try:
        manifest_bytes = _read_file_nofollow(pinned_manifest)
    except OSError as error:
        raise ValueError("graphics Wine runtime manifest is missing or unsafe") from error
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (actual_manifest_sha256 != manifest_sha256
            or candidate_hashes.get("wine_runtime_manifest") != actual_manifest_sha256):
        raise ValueError("graphics Wine runtime manifest SHA-256 does not match its signed candidate")
    try:
        runtime_manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("graphics Wine runtime manifest is malformed") from error
    if (not isinstance(runtime_manifest, dict)
            or runtime_manifest.get("path") != runtime_root_value
            or runtime_manifest.get("tree_sha256") != runtime_tree_sha256):
        raise ValueError("graphics Wine runtime manifest names a different root or tree")
    entries = runtime_manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("graphics Wine runtime manifest has no exact file inventory")
    entries_by_path: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("relative_path"), str):
            raise ValueError("graphics Wine runtime manifest contains a malformed inventory entry")
        relative_path = entry["relative_path"]
        if relative_path in entries_by_path:
            raise ValueError("graphics Wine runtime manifest contains duplicate inventory paths")
        entries_by_path[relative_path] = entry
    for relative_path, expected_sha256, role in (
            ("bin/wine", launcher_sha256, "launcher"),
            ("lib/wine/x86_64-unix/wine", host_sha256, "host image")):
        entry = entries_by_path.get(relative_path)
        if (entry is None or entry.get("kind") != "file"
                or entry.get("sha256") != expected_sha256):
            raise ValueError(f"graphics Wine runtime manifest does not bind the exact {role} file")

    launcher_path = Path(launcher_path_value)
    host_path = Path(host_path_value)
    launcher_digest, launcher_identity = _wine_graphics_image_identity(
        launcher_path, launcher_sha256, role="launcher")
    host_digest, host_identity = _wine_graphics_image_identity(
        host_path, host_sha256, role="host image")
    return {
        "launcher_path": launcher_path,
        "launcher_sha256": launcher_digest,
        "launcher_identity": launcher_identity,
        "host_image_path": host_path,
        "host_image_sha256": host_digest,
        "host_image_identity": host_identity,
        "runtime_manifest_path": pinned_manifest,
        "runtime_manifest_sha256": actual_manifest_sha256,
        "runtime_root": runtime_root,
        "runtime_tree_sha256": runtime_tree_sha256,
    }


def _open_directory_nofollow(
    path: Path,
    *,
    create: bool = False,
    strict_directory_sync: bool = False,
) -> int:
    """Open (and optionally create) a directory without following symlinks."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o755, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                try:
                    os.fsync(directory_fd)
                except OSError:
                    if strict_directory_sync:
                        raise
                next_fd = os.open(component, flags, dir_fd=directory_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise NotADirectoryError(f"not a real directory: {path}")
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _read_file_nofollow(path: Path | str, *, maximum_bytes: int = 256 * 1024 * 1024) -> bytes:
    """Read one bounded regular file through a no-follow parent/file descriptor pair."""
    selected = Path(path).absolute()
    parent_fd = _open_directory_nofollow(selected.parent)
    try:
        contents, _identity = _read_file_from_directory_fd(
            parent_fd, selected.name, display_path=selected, maximum_bytes=maximum_bytes)
        return contents
    finally:
        os.close(parent_fd)


def _read_file_from_directory_fd(
    directory_fd: int,
    name: str,
    *,
    display_path: Path | str | None = None,
    maximum_bytes: int = 256 * 1024 * 1024,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    """Read a bounded regular child through an already pinned directory descriptor."""
    if (not name or name in {".", ".."} or os.sep in name or "\x00" in name
            or (os.altsep is not None and os.altsep in name)):
        raise ValueError("descriptor-relative file name must be one canonical path component")
    selected = Path(display_path) if display_path is not None else Path(name)
    descriptor: int | None = None
    try:
        descriptor = os.open(selected.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum_bytes:
            raise OSError(f"expected bounded regular file: {selected}")
        chunks = []
        total = 0
        while True:
            block = os.read(descriptor, min(4 * 1024 * 1024, maximum_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum_bytes:
                raise OSError(f"file exceeds the read limit: {selected}")
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             getattr(before, "st_ctime_ns", 0)) !=
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
             getattr(after, "st_ctime_ns", 0)) or total != before.st_size):
            raise OSError(f"file changed while reading: {selected}")
        identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                    getattr(after, "st_ctime_ns", 0))
        return b"".join(chunks), identity
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _spawn_in_directory_fd(
    directory_fd: int,
    command: list[str] | tuple[str, ...],
    *,
    expected_directory_identity: tuple[int, int] | None = None,
    expected_child_identity: tuple[str, int, int, str] | None = None,
    expected_file_identities: tuple[
        tuple[str, str, tuple[int, int, int, int, int], str, bool], ...] | None = None,
    bind_trace_target_pid: bool = False,
    **popen_options: Any,
) -> subprocess.Popen[Any]:
    """Exec a child after fchdir to an already verified directory descriptor."""
    if "cwd" in popen_options:
        raise ValueError("descriptor-bound child launch cannot accept a pathname cwd")
    identity_checks: list[str] = []
    absolute_path_checks: list[str] = []
    if expected_directory_identity is not None:
        directory_dev, directory_ino = expected_directory_identity
        if (not isinstance(directory_dev, int) or isinstance(directory_dev, bool)
                or not isinstance(directory_ino, int) or isinstance(directory_ino, bool)):
            raise ValueError("expected directory identity must contain integer device and inode values")
        identity_checks.extend([
            "directory_info=os.fstat(fd)",
            f"if (directory_info.st_dev,directory_info.st_ino)!=({directory_dev},{directory_ino}):",
            "    raise OSError('descriptor-bound parent identity changed')",
        ])
    if expected_child_identity is not None:
        child_name, child_dev, child_ino, canonical_child_path = expected_child_identity
        if (expected_directory_identity is None
                or not child_name or child_name in {".", ".."} or os.sep in child_name
                or "\x00" in child_name or (os.altsep is not None and os.altsep in child_name)
                or not isinstance(child_dev, int) or isinstance(child_dev, bool)
                or not isinstance(child_ino, int) or isinstance(child_ino, bool)
                or not isinstance(canonical_child_path, str)
                or not Path(canonical_child_path).is_absolute()
                or Path(canonical_child_path).name != child_name
                or os.path.abspath(canonical_child_path) != canonical_child_path):
            raise ValueError("expected child identity must describe one canonical directory entry")
        identity_checks.extend([
            f"child_info=os.stat({child_name!r},dir_fd=fd,follow_symlinks=False)",
            f"if not stat.S_ISDIR(child_info.st_mode) or "
            f"(child_info.st_dev,child_info.st_ino)!=({child_dev},{child_ino}):",
            "    raise OSError('descriptor-bound child identity changed')",
            "prefix_path=os.environ.get('WINEPREFIX')",
            f"if prefix_path!={canonical_child_path!r}:",
            "    raise OSError('Wine WINEPREFIX changed before exec')",
            "if (not os.path.isabs(prefix_path) or os.path.normpath(prefix_path)!=prefix_path "
            "or '..' in prefix_path.split(os.sep)):",
            "    raise OSError('Wine WINEPREFIX is not canonical')",
        ])
        absolute_path_checks.extend([
            "def open_absolute_directory_nofollow(path):",
            "    flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)",
            "    current=os.open(os.sep,flags)",
            "    try:",
            "        for component in path.split(os.sep):",
            "            if not component:",
            "                continue",
            "            next_fd=os.open(component,flags,dir_fd=current)",
            "            os.close(current)",
            "            current=next_fd",
            "        return current",
            "    except BaseException:",
            "        os.close(current)",
            "        raise",
            "prefix_path_parent_fd=open_absolute_directory_nofollow(os.path.dirname(prefix_path))",
            "absolute_parent_info=os.fstat(prefix_path_parent_fd)",
            f"if (absolute_parent_info.st_dev,absolute_parent_info.st_ino)!="
            f"({expected_directory_identity[0]},{expected_directory_identity[1]}):",
            "    raise OSError('absolute Wine prefix parent identity changed')",
            "absolute_child_info=os.stat(os.path.basename(prefix_path),"
            "dir_fd=prefix_path_parent_fd,follow_symlinks=False)",
            f"if not stat.S_ISDIR(absolute_child_info.st_mode) or "
            f"(absolute_child_info.st_dev,absolute_child_info.st_ino)!=({child_dev},{child_ino}):",
            "    raise OSError('absolute Wine prefix inode changed')",
            "os.close(prefix_path_parent_fd)",
        ])
    file_identity_checks: list[str] = []
    execute_path: str | None = None
    if expected_file_identities is not None:
        if not isinstance(expected_file_identities, tuple) or not expected_file_identities:
            raise ValueError("expected Wine file identities must be a non-empty tuple")
        normalized_files: list[tuple[str, str, tuple[int, int, int, int, int], str, bool]] = []
        for record in expected_file_identities:
            if not isinstance(record, tuple) or len(record) != 5:
                raise ValueError("expected Wine file identity record is malformed")
            path_value, digest, file_identity, architecture, execute_file = record
            if (not isinstance(path_value, str) or not Path(path_value).is_absolute()
                    or os.path.abspath(path_value) != path_value or Path(path_value).is_symlink()
                    or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or not isinstance(file_identity, tuple) or len(file_identity) != 5
                    or any(not isinstance(value, int) or isinstance(value, bool)
                           for value in file_identity)
                    or architecture != "x86_64" or not isinstance(execute_file, bool)):
                raise ValueError("expected Wine file identity record is unsafe or non-canonical")
            if execute_file:
                if execute_path is not None:
                    raise ValueError("only one descriptor-bound Wine launcher may be selected")
                execute_path = path_value
            normalized_files.append((path_value, digest, file_identity, architecture, execute_file))
        command_args = [str(item) for item in command]
        if (execute_path is not None and (not command_args or command_args[0] != execute_path)):
            raise ValueError("verified Wine launcher must match the direct child executable")
        file_identity_checks.extend([
            "import hashlib",
            "def open_absolute_file_nofollow(path):",
            "    if (not os.path.isabs(path) or os.path.normpath(path)!=path "
            "or '..' in path.split(os.sep)):",
            "        raise OSError('Wine runtime image path is not canonical')",
            "    flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0)",
            "    current=os.open(os.sep,flags)",
            "    try:",
            "        components=[part for part in path.split(os.sep) if part]",
            "        if not components:",
            "            raise OSError('Wine runtime image path has no filename')",
            "        for component in components[:-1]:",
            "            next_fd=os.open(component,flags,dir_fd=current)",
            "            os.close(current)",
            "            current=next_fd",
            "        file_fd=os.open(components[-1],os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|"
            "getattr(os,'O_NONBLOCK',0),dir_fd=current)",
            "        return file_fd",
            "    finally:",
            "        os.close(current)",
            f"expected_wine_files={tuple(normalized_files)!r}",
            "wine_exec_fd=None",
            "wine_exec_path=None",
            "for wine_path,wine_sha,wine_identity,wine_architecture,wine_exec in expected_wine_files:",
            "    wine_fd=open_absolute_file_nofollow(wine_path)",
            "    keep_fd=False",
            "    try:",
            "        before=os.fstat(wine_fd)",
            "        before_identity=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,"
            "getattr(before,'st_ctime_ns',0))",
            "        if (not stat.S_ISREG(before.st_mode) or not (before.st_mode&0o111) "
            "or before_identity!=wine_identity):",
            "            raise OSError('certified Wine runtime image changed before exec')",
            "        header=os.pread(wine_fd,32,0)",
            "        if header[:4]==bytes.fromhex('cffaedfe'):",
            "            cpu=int.from_bytes(header[4:8],'little')",
            "        elif header[:4]==bytes.fromhex('feedfacf'):",
            "            cpu=int.from_bytes(header[4:8],'big')",
            "        else:",
            "            raise OSError('certified Wine runtime image is not thin x86_64 Mach-O')",
            "        if cpu!=0x01000007:",
            "            raise OSError('certified Wine runtime image is not thin x86_64 Mach-O')",
            "        hasher=hashlib.sha256()",
            "        offset=0",
            "        while True:",
            "            block=os.pread(wine_fd,4*1024*1024,offset)",
            "            if not block:",
            "                break",
            "            hasher.update(block)",
            "            offset+=len(block)",
            "        after=os.fstat(wine_fd)",
            "        after_identity=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,"
            "getattr(after,'st_ctime_ns',0))",
            "        if (after_identity!=wine_identity or before_identity!=after_identity "
            "or hasher.hexdigest()!=wine_sha):",
            "            raise OSError('certified Wine runtime image changed before exec')",
            "        if wine_exec:",
            "            if wine_exec_fd is not None:",
            "                raise OSError('multiple Wine launcher descriptors were supplied')",
            "            wine_exec_fd=wine_fd",
            "            wine_exec_path=wine_path",
            "            keep_fd=True",
            "    finally:",
            "        if not keep_fd:",
            "            os.close(wine_fd)",
            "if wine_exec_fd is not None and args[0]!=wine_exec_path:",
            "    raise OSError('Wine command differs from the verified launcher')",
        ])
    inherited = set(popen_options.pop("pass_fds", ()))
    inherited.add(directory_fd)
    wrapper_lines = ["import os,sys,stat", "fd=int(sys.argv[1])", "os.fchdir(fd)", "args=sys.argv[2:]"]
    wrapper_lines.extend(identity_checks)
    wrapper_lines.extend(absolute_path_checks)
    wrapper_lines.extend(file_identity_checks)
    if bind_trace_target_pid:
        if expected_file_identities is None or execute_path is None:
            raise ValueError("trace target PID requires a verified direct Wine launcher")
        wrapper_lines.extend([
            "if 'FG_B_TARGET_UNIX_PID' in os.environ:",
            "    raise OSError('caller supplied trace target PID')",
            "os.environ['FG_B_TARGET_UNIX_PID']=str(os.getpid())",
        ])
    wrapper_lines.append("os.execvpe(args[0],args,os.environ)")
    wrapper = "\n".join(wrapper_lines)
    child_command = [sys.executable, "-I", "-S", "-c", wrapper, str(directory_fd),
                     *(str(item) for item in command)]
    return subprocess.Popen(child_command, pass_fds=tuple(sorted(inherited)), **popen_options)


def _stat_child_nofollow(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _detach_child_if_identity(directory_fd: int, name: str,
                              expected_identity: tuple[int, int]) -> str:
    """Atomically quarantine an entry, then verify what was moved before deletion."""
    quarantine = f".fgmetal-retire-{uuid.uuid4().hex}"
    _rename_child_exclusive(directory_fd, name, quarantine, sync=False)
    moved = _stat_child_nofollow(directory_fd, quarantine)
    if moved is None or (moved.st_dev, moved.st_ino) != expected_identity:
        try:
            if moved is not None:
                _rename_child_exclusive(directory_fd, quarantine, name, sync=False)
                os.fsync(directory_fd)
        except OSError:
            # Do not delete an unverified replacement. It remains under a
            # randomized quarantine name and the active lease will block Gate 0.
            pass
        raise OSError(f"entry changed during atomic cleanup quarantine: {name}")
    return quarantine


def _remove_child_directory_nofollow(
        directory_fd: int, name: str, *, expected_identity: tuple[int, int] | None = None) -> None:
    """Remove one child by descriptor, refusing identity or filesystem changes."""
    info = _stat_child_nofollow(directory_fd, name)
    if info is None:
        if expected_identity is not None:
            raise OSError(f"bound directory name disappeared before controlled removal: {name}")
        return
    if not stat.S_ISDIR(info.st_mode):
        raise OSError(f"refusing to recursively remove a non-directory or symlink: {name}")
    root_info = os.fstat(directory_fd)
    expected_device = root_info.st_dev
    expected_fsid = getattr(os.fstatvfs(directory_fd), "f_fsid", None)
    if expected_fsid is None:
        raise OSError("filesystem identity is unavailable for verified removal root")
    identity = (info.st_dev, info.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise OSError(f"refusing to remove a replaced directory: {name}")
    if info.st_dev != expected_device:
        raise OSError(f"refusing to traverse a different filesystem: {name}")

    def remove_contents(parent: int, child_name: str,
                        expected: tuple[int, int], parent_device: int) -> None:
        before = _stat_child_nofollow(parent, child_name)
        if before is None or not stat.S_ISDIR(before.st_mode):
            raise OSError(f"directory changed before descriptor-bound removal: {child_name}")
        if (before.st_dev, before.st_ino) != expected or before.st_dev != parent_device:
            raise OSError(f"directory identity or filesystem changed before removal: {child_name}")
        child_fd = os.open(child_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        try:
            opened = os.fstat(child_fd)
            opened_fsid = getattr(os.fstatvfs(child_fd), "f_fsid", None)
            if ((opened.st_dev, opened.st_ino) != expected or opened.st_dev != parent_device
                    or opened_fsid != expected_fsid):
                raise OSError(f"directory was replaced while opening for removal: {child_name}")
            for entry in os.listdir(child_fd):
                entry_before = _stat_child_nofollow(child_fd, entry)
                if entry_before is None:
                    continue
                if entry_before.st_dev != parent_device:
                    raise OSError(f"refusing mount/filesystem transition during removal: {entry}")
                if stat.S_ISDIR(entry_before.st_mode):
                    remove_contents(child_fd, entry, (entry_before.st_dev, entry_before.st_ino), parent_device)
                    continue
                if not (stat.S_ISREG(entry_before.st_mode) or stat.S_ISLNK(entry_before.st_mode)):
                    raise OSError(f"refusing to remove a special file from the leased prefix: {entry}")
                quarantine = _detach_child_if_identity(
                    child_fd, entry, (entry_before.st_dev, entry_before.st_ino))
                moved = _stat_child_nofollow(child_fd, quarantine)
                if (moved is None or (moved.st_dev, moved.st_ino)
                        != (entry_before.st_dev, entry_before.st_ino)):
                    raise OSError(f"quarantined file identity changed before unlink: {entry}")
                os.unlink(quarantine, dir_fd=child_fd)
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
        quarantine = _detach_child_if_identity(parent, child_name, expected)
        current = _stat_child_nofollow(parent, quarantine)
        if current is None or (current.st_dev, current.st_ino) != expected:
            raise OSError(f"quarantined directory identity changed before final removal: {child_name}")
        os.rmdir(quarantine, dir_fd=parent)
        os.fsync(parent)

    parent_now = os.fstat(directory_fd)
    if parent_now.st_dev != expected_device:
        raise OSError("verified prefix parent changed filesystem before removal")
    remove_contents(directory_fd, name, identity, expected_device)
    os.fsync(directory_fd)
    if _stat_child_nofollow(directory_fd, name) is not None:
        raise OSError(f"directory still exists after recursive removal: {name}")


def _rename_child_exclusive(directory_fd: int, source_name: str, target_name: str, *, sync: bool = True) -> None:
    """Atomically publish a completed child directory without replacing any entry."""
    renameatx_np = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
    if renameatx_np is None:
        raise RuntimeError("this platform lacks renameatx_np exclusive directory publication")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(directory_fd, os.fsencode(source_name), directory_fd,
                          os.fsencode(target_name), 0x00000004)  # RENAME_EXCL
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), target_name)
    if sync:
        os.fsync(directory_fd)


def _atomic_write_bytes(path: Path, contents: bytes, *, strict_directory_sync: bool = False) -> None:
    parent_fd = _open_directory_nofollow(
        path.parent, create=True, strict_directory_sync=strict_directory_sync)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary_fd: int | None = None
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OSError(f"refusing to replace non-regular JSON destination: {path}")
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, create_flags, 0o600, dir_fd=parent_fd)
        with os.fdopen(temporary_fd, "wb") as stream:
            temporary_fd = None
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.fsync(parent_fd)
        except OSError:
            # Noncritical snapshots may run on filesystems without directory
            # fsync. Gate evidence and lease markers require durable rename.
            if strict_directory_sync:
                raise
    except BaseException:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_fd)


def _write_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    pad_to_bytes: int | None = None,
    strict_directory_sync: bool = False,
) -> None:
    serialized_bytes = _canonical_pretty_json_bytes(payload)
    serialized_size = len(serialized_bytes)
    if pad_to_bytes is not None:
        if serialized_size > pad_to_bytes:
            raise ValueError(f"JSON receipt grew beyond its reserved size ({serialized_size} > {pad_to_bytes})")
        serialized_bytes += b" " * (pad_to_bytes - serialized_size)
    _atomic_write_bytes(path, serialized_bytes,
                        strict_directory_sync=strict_directory_sync)


def _canonical_pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return the exact on-disk encoding used by _write_json."""
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_materialized_active_prefix_marker(
    payload: Mapping[str, Any], raw_bytes: bytes, file_info: os.stat_result,
) -> Mapping[str, Any]:
    """Reject unbounded or noncanonical data at the Gate 0-excluded lease path."""
    expected_keys = {
        "experiment_id", "pid", "state", "prefix_path", "output_dir",
        "manifest_path", "manifest_sha256", "lease_token", "updated_at_unix",
        "materialization_receipt",
    }
    allocated_bytes = getattr(file_info, "st_blocks", 0) * 512
    if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid()
            or file_info.st_nlink != 1 or stat.S_IMODE(file_info.st_mode) != 0o600
            or file_info.st_size > ACTIVE_PREFIX_LEASE_MARKER_MAX_BYTES
            or allocated_bytes > ACTIVE_PREFIX_LEASE_MARKER_MAX_ALLOCATED_BYTES):
        raise RuntimeError("active PrefixLease marker exceeds its bounded private allocation")
    if set(payload) != expected_keys or raw_bytes != _canonical_pretty_json_bytes(payload):
        raise RuntimeError("active PrefixLease marker schema or canonical encoding is invalid")
    updated_at = payload.get("updated_at_unix")
    if (not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool)
            or not math.isfinite(updated_at) or updated_at < 0):
        raise RuntimeError("active PrefixLease marker timestamp is malformed")
    receipt = payload.get("materialization_receipt")
    if (not isinstance(receipt, Mapping)
            or not _verify_storage_evidence(receipt, "prefix_materialization_hmac_sha256")):
        raise RuntimeError("active PrefixLease marker has no authenticated materialization receipt")
    return receipt


def _stat_entry_nofollow(path: Path) -> os.stat_result | None:
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        try:
            return os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
    finally:
        os.close(parent_fd)


def _unlink_file_nofollow(path: Path, *, expected_json: Mapping[str, Any] | None = None) -> None:
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path.name, flags, dir_fd=parent_fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"refusing to unlink non-regular file: {path}")
            if expected_json is not None:
                try:
                    actual = json.loads(stream.read())
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise OSError(f"refusing to unlink malformed lease marker: {path}") from error
                if any(actual.get(key) != value for key, value in expected_json.items()):
                    raise OSError(f"refusing to unlink a replaced lease marker: {path}")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _remove_directory_nofollow(path: Path) -> None:
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        info = _stat_child_nofollow(parent_fd, path.name)
        if info is None:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise OSError(f"refusing to recursively remove a non-directory or symlink: {path}")
        _remove_child_directory_nofollow(
            parent_fd, path.name, expected_identity=(info.st_dev, info.st_ino))
    finally:
        os.close(parent_fd)


def begin_experiment(
    *,
    experiment_id: str,
    purpose: str,
    candidate_hashes: Mapping[str, str],
    expected_duration_seconds: int,
    expected_prefix_bytes: int = 3 * GIB,
    expected_build_bytes: int = 0,
    estimated_persistent_bytes: int = 100 * MIB,
    storage_justification: str | None = None,
    persistent_growth_over_2g_approval: str | None = None,
    internal_storage_setup: bool = False,
    storage_policy_smoke: bool = False,
    authorized_runner_paths: list[Path | str] | None = None,
    build_source_tree_hash: str | None = None,
    build_configuration_key_sha256: str | None = None,
    build_kind: str | None = None,
    build_type: str | None = None,
    dependency_state: Any = None,
    preserve_debug_reason: str | None = None,
    estimated_debug_bytes: int | None = None,
    failure_capture_paths: list[str] | None = None,
    native_architecture: str | None = None,
    output_dir: Path,
    additional: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the required manifest and refuse an unsafe run before allocation."""
    if not experiment_id.strip() or not purpose.strip():
        raise ValueError("experiment_id and purpose are required before setup")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", experiment_id):
        raise ValueError("experiment_id may contain only letters, numbers, hyphens, and underscores")
    if expected_duration_seconds <= 0 or expected_prefix_bytes < 0 or expected_build_bytes < 0:
        raise ValueError("duration and storage estimates must be positive/non-negative")
    if estimated_persistent_bytes < 0:
        raise ValueError("estimated_persistent_bytes must be non-negative")
    if (persistent_growth_over_2g_approval is not None
            and (not isinstance(persistent_growth_over_2g_approval, str)
                 or not persistent_growth_over_2g_approval.strip())):
        raise ValueError("persistent growth approval requires a non-empty written reason")
    if native_architecture is not None and native_architecture not in SUPPORTED_NATIVE_ARCHITECTURES:
        raise ValueError("native_architecture must be exactly 'arm64' or 'x86_64'")

    if storage_policy_smoke:
        permitted_smoke_id = _current_preflight_smoke_id()
        if not _is_storage_policy_smoke_id(experiment_id) or experiment_id != permitted_smoke_id:
            raise ValueError("the storage preflight bypass is restricted to the nonce-bound ID in the current signed preflight")
        if (expected_duration_seconds != 300 or expected_prefix_bytes != 3 * GIB
                or expected_build_bytes != 0 or estimated_persistent_bytes != 32 * MIB):
            raise ValueError("the fixed storage smoke must use its bounded prefix/build/output estimates")
        expected_output = REPO / "experiments/storage/smoke-runs" / experiment_id
        if Path(output_dir).absolute() != expected_output.absolute():
            raise ValueError("the fixed storage smoke must use its reserved output directory")
    elif _is_storage_policy_smoke_id(experiment_id):
        raise ValueError("a reserved storage-smoke ID cannot be used by a normal experiment")

    normalized_runners: list[str] = []
    if not storage_policy_smoke and (expected_prefix_bytes > 0 or expected_build_bytes > 0 or authorized_runner_paths):
        if not authorized_runner_paths and (expected_prefix_bytes > 0 or expected_build_bytes > 0):
            raise ValueError("every allocated experiment must authorize its exact runner source path")
        for runner in (authorized_runner_paths or []):
            selected = Path(runner).expanduser().absolute()
            resolved = selected.resolve(strict=True)
            if (selected.is_symlink() or selected != resolved or not resolved.is_file()
                    or REPO.resolve() not in resolved.parents):
                raise ValueError(f"authorized runner must be a real canonical source file inside the repository: {runner}")
            if str(resolved) in normalized_runners:
                raise ValueError("authorized runner paths must be unique")
            normalized_runners.append(str(resolved))
    if expected_build_bytes > 0:
        if build_kind not in {"dxvk", "moltenvk", "reference"}:
            raise ValueError("build reservation requires an explicit supported build_kind")
        if (not isinstance(build_source_tree_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", build_source_tree_hash)):
            raise ValueError("build reservation requires its frozen source-tree SHA-256")
        if (not isinstance(build_configuration_key_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", build_configuration_key_sha256)):
            raise ValueError("build reservation requires its complete configuration-key SHA-256")
        if dependency_state is None:
            raise ValueError("build reservation requires frozen dependency state")
        if (not isinstance(build_type, str) or build_type != build_type.strip()
                or build_type != build_type.casefold()
                or build_type.casefold() not in SUPPORTED_BUILD_TYPES):
            raise ValueError("build reservation requires a supported signed build_type")
        full_debug_build = build_type.casefold() in FULL_DEBUG_BUILD_TYPES
        if build_kind == "reference" and (not isinstance(preserve_debug_reason, str)
                                            or not preserve_debug_reason.strip()):
            raise ValueError("reference builds require an explicit preservation reason")
        if full_debug_build:
            if (not isinstance(preserve_debug_reason, str) or not preserve_debug_reason.strip()
                    or not isinstance(estimated_debug_bytes, int) or isinstance(estimated_debug_bytes, bool)
                    or estimated_debug_bytes <= 0 or estimated_debug_bytes > expected_build_bytes):
                raise ValueError("full debug/unstripped builds require a reason and bounded estimated_debug_bytes")
        elif estimated_debug_bytes is not None:
            raise ValueError("estimated_debug_bytes is valid only for full debug/unstripped builds")
    elif any(value is not None for value in (
            build_source_tree_hash, build_configuration_key_sha256, build_kind, build_type, dependency_state,
            preserve_debug_reason, estimated_debug_bytes)):
        raise ValueError("build identity fields cannot be declared when expected_build_bytes is zero")
    if failure_capture_paths is None:
        normalized_failure_capture_paths: list[str] = []
    else:
        if not isinstance(failure_capture_paths, list) or len(failure_capture_paths) > 64:
            raise ValueError("failure_capture_paths must be a list of at most 64 relative paths")
        normalized_failure_capture_paths = []
        for item in failure_capture_paths:
            if (not isinstance(item, str) or not item or "\\" in item or "\x00" in item
                    or item in {".fgmetal-build-lease.json", "build-execution.json"}):
                raise ValueError("failure capture paths must be non-empty POSIX-relative files")
            relative = Path(item)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError(f"unsafe failure capture path: {item}")
            normalized = relative.as_posix()
            if normalized != item or normalized in normalized_failure_capture_paths:
                raise ValueError(f"failure capture path is non-normalized or duplicated: {item}")
            normalized_failure_capture_paths.append(normalized)
    if expected_build_bytes == 0 and normalized_failure_capture_paths:
        raise ValueError("failure capture paths require a build reservation")
    if storage_policy_smoke:
        smoke_runner = Path(__file__).resolve().parent / "smoke_prefix_lease.py"
        smoke_runner_bytes = _read_file_nofollow(smoke_runner)
        normalized_runners = [str(smoke_runner)]
        authorized_runner_hashes = {
            str(smoke_runner): hashlib.sha256(smoke_runner_bytes).hexdigest(),
        }
    else:
        authorized_runner_hashes = _authorized_runner_records(
            normalized_runners,
            requires_managed_prefix=expected_prefix_bytes > 0)

    if internal_storage_setup:
        raise ValueError("internal storage setup is retired; Gate 0 is mandatory for every new manifest")
    output_dir = _measured_output_dir(output_dir, require_missing=True)
    free_before = disk_free_bytes(REPO)
    project = measure_project_usage()
    project_bytes = conservative_project_usage_bytes(project)
    project["conservative_allocated_inode_deduplicated_bytes"] = project_bytes
    estimated_peak = expected_prefix_bytes + expected_build_bytes + estimated_persistent_bytes
    uncertainty_margin = admission_uncertainty_margin_bytes(estimated_peak)
    projected_peak = estimated_peak + uncertainty_margin
    blockers: list[str] = []
    warnings: list[str] = []
    gate0_attestation_sha256: str | None = None
    if free_before < FREE_HARD_STOP:
        blockers.append(f"free space below hard stop ({free_before} < {FREE_HARD_STOP})")
    blockers.extend(_gate0_blockers(
        active_experiment_id=experiment_id if storage_policy_smoke else None,
        allow_storage_smoke=storage_policy_smoke))
    if GATE0_ATTESTATION_PATH.is_file() and not GATE0_ATTESTATION_PATH.is_symlink():
        gate0_attestation_sha256 = hashlib.sha256(_read_file_nofollow(GATE0_ATTESTATION_PATH)).hexdigest()
    admission = projected_storage_admission(
        current_free_bytes=free_before,
        current_scoped_bytes=project_bytes,
        peak_additional_bytes=projected_peak,
    )
    blockers.extend(admission["blockers"])
    warnings.extend(admission["warnings"])
    project_peak = admission["projected_scoped_bytes"]
    if expected_prefix_bytes > NORMAL_TEMPORARY_TARGET and not storage_justification:
        blockers.append("prefix/temporary estimate exceeds 3 GiB without a justification")
    if expected_build_bytes > 5 * GIB and not storage_justification:
        blockers.append("build estimate exceeds 5 GiB without a justification")
    if estimated_persistent_bytes >= NORMAL_PERSISTENT_TARGET and not storage_justification:
        blockers.append("persistent estimate exceeds 250 MiB without a justification")
    orphans = uncertified_prefixes()
    if orphans:
        blockers.append("uncertified Wine prefix cleanup is pending: "
                        + ", ".join(str(path) for path in orphans))
    retained_prefix_count = len(preserved_prefixes())
    if retained_prefix_count > MAX_PRESERVED_PREFIXES:
        blockers.append("preserved Wine prefix count exceeds the configured limit")
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout
    worktree_count = sum(line.startswith("worktree ") for line in worktree_listing.splitlines())
    if worktree_count > 3:
        blockers.append(f"worktree retention cap exceeded ({worktree_count} > 3)")
    active_leases = _active_lease_markers()
    if active_leases:
        blockers.append("active or interrupted PrefixLease records require reconciliation: "
                        + ", ".join(str(path) for path in active_leases))

    manifest: dict[str, Any] = {
        "experiment_id": experiment_id,
        "purpose": purpose,
        "candidate_hashes": dict(candidate_hashes),
        "created_at_unix": time.time(),
        "expected_duration_seconds": expected_duration_seconds,
        "expected_prefix_requirement": expected_prefix_bytes > 0,
        "expected_prefix_bytes": expected_prefix_bytes,
        "expected_build_requirement": expected_build_bytes > 0,
        "expected_build_bytes": expected_build_bytes,
        "estimated_max_new_disk_bytes": estimated_peak,
        "estimated_uncertainty_margin_bytes": uncertainty_margin,
        "estimated_projected_new_disk_bytes": projected_peak,
        "project_estimated_peak_bytes": project_peak,
        "projected_free_bytes": admission["projected_free_bytes"],
        "projected_scoped_project_bytes": admission["projected_scoped_bytes"],
        "storage_admission_warnings": admission["warnings"],
        "estimated_persistent_bytes": estimated_persistent_bytes,
        "project_budget_bytes": PROJECT_BUDGET,
        "project_budget_remaining_before_bytes": max(0, PROJECT_BUDGET - project_bytes),
        "free_before_bytes": free_before,
        "project_usage_before": project,
        "output_dir": str(output_dir),
        "decision": "REFUSE" if blockers else "ALLOW",
        "blockers": blockers,
        "warnings": warnings,
        "cleanup_policy": "NativeRunLease finally cleanup; preserve logs and telemetry" if expected_prefix_bytes == 0 else "PrefixLease finally cleanup; preserve registry/config snapshot and compact logs",
        "retention_policy": "canonical binaries by SHA-256; one clean template plus at most one failure reproduction",
        "storage_justification": storage_justification,
        "persistent_growth_over_2g_approved": persistent_growth_over_2g_approval is not None,
        "persistent_growth_over_2g_approval_reason": persistent_growth_over_2g_approval,
        "internal_storage_setup": False,
        "storage_policy_smoke": storage_policy_smoke,
        "gate0_attestation_sha256": gate0_attestation_sha256,
        "authorized_runner_paths": normalized_runners,
        "authorized_runner_sha256": authorized_runner_hashes,
        "build_source_tree_hash": build_source_tree_hash,
        "build_configuration_key_sha256": build_configuration_key_sha256,
        "build_kind": build_kind,
        "build_type": build_type,
        "dependency_state": dependency_state,
        "preserve_debug_reason": preserve_debug_reason,
        "estimated_debug_bytes": estimated_debug_bytes,
        "failure_capture_paths": normalized_failure_capture_paths,
        "native_architecture": native_architecture,
        **dict(additional or {}),
    }
    reserved = {
        "experiment_id", "purpose", "candidate_hashes", "created_at_unix",
        "expected_duration_seconds", "expected_prefix_requirement", "expected_prefix_bytes",
        "expected_build_requirement", "expected_build_bytes", "estimated_max_new_disk_bytes",
        "estimated_uncertainty_margin_bytes", "estimated_projected_new_disk_bytes",
        "project_estimated_peak_bytes", "estimated_persistent_bytes", "project_budget_bytes", "project_budget_remaining_before_bytes",
        "free_before_bytes", "project_usage_before", "projected_free_bytes",
        "projected_scoped_project_bytes", "storage_admission_warnings",
        "output_dir", "decision", "blockers", "warnings",
        "manifest_path", "internal_storage_setup", "gate0_attestation_sha256",
        "storage_policy_smoke",
        "smoke_nonce_claim_path", "smoke_nonce_claim_sha256",
        "storage_justification", "persistent_growth_over_2g_approved",
        "persistent_growth_over_2g_approval_reason",
        "authorized_runner_paths", "authorized_runner_sha256", "build_source_tree_hash",
        "build_configuration_key_sha256",
        "build_kind", "build_type", "dependency_state", "preserve_debug_reason", "estimated_debug_bytes",
        "failure_capture_paths",
        "native_architecture",
    }
    conflicting = reserved.intersection(additional or {})
    if conflicting:
        raise ValueError("additional manifest fields cannot replace storage-gate fields: "
                         + ", ".join(sorted(conflicting)))
    manifest_path = _experiment_manifest_path(experiment_id)
    if manifest_path.exists():
        raise FileExistsError(f"experiment manifest already exists: {manifest_path}")
    manifest["manifest_path"] = str(manifest_path)
    try:
        _validated_manifest_estimates(manifest)
    except ValueError as error:
        raise ValueError(f"experiment storage estimates are invalid: {error}") from error
    if storage_policy_smoke:
        preflight_bytes = _read_file_nofollow(GATE0_ATTESTATION_PATH)
        if hashlib.sha256(preflight_bytes).hexdigest() != gate0_attestation_sha256:
            raise RuntimeError("Gate 0 preflight changed before consuming the storage-smoke nonce")
        claim, claim_bytes = _claim_storage_smoke_nonce(experiment_id, preflight_bytes)
        manifest["created_at_unix"] = claim["claimed_at_unix"]
        manifest["smoke_nonce_claim_path"] = str(_storage_smoke_claim_path(experiment_id))
        manifest["smoke_nonce_claim_sha256"] = hashlib.sha256(claim_bytes).hexdigest()
    manifest["manifest_hmac_sha256"] = _manifest_signature(manifest, create_key=True)
    _write_json(manifest_path, manifest, strict_directory_sync=True)
    print(json.dumps({
        "experiment_id": experiment_id,
        "free_before_bytes": free_before,
        "estimated_peak_new_bytes": estimated_peak,
        "uncertainty_margin_bytes": uncertainty_margin,
        "projected_peak_new_bytes": projected_peak,
        "project_budget_bytes": PROJECT_BUDGET,
        "project_usage_before_bytes": project_bytes,
        "projected_free_bytes": admission["projected_free_bytes"],
        "projected_scoped_project_bytes": admission["projected_scoped_bytes"],
        "decision": manifest["decision"],
        "blockers": blockers,
        "warnings": warnings,
        "manifest_path": str(manifest_path),
    }, sort_keys=True))
    if blockers:
        raise RuntimeError("storage gate refused experiment: " + "; ".join(blockers))
    return manifest


def _prefix_payload(prefix: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"path": str(prefix), "files": {}, "dosdevices": {}}
    for name in ("system.reg", "user.reg", "userdef.reg"):
        path = prefix / name
        if path.is_file():
            import hashlib
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            payload["files"][name] = {"size_bytes": path.stat().st_size, "sha256": digest}
    links = prefix / "dosdevices"
    if links.is_dir() and not links.is_symlink():
        for path in sorted(links.iterdir()):
            if path.is_symlink():
                payload["dosdevices"][path.name] = os.readlink(path)
    return payload


def _prefix_size(prefix: Path) -> dict[str, int]:
    logical = allocated = files = 0
    seen: set[tuple[int, int]] = set()
    for directory, dirnames, filenames in os.walk(prefix, followlinks=False):
        current = Path(directory)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            logical += info.st_size
            files += 1
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                allocated += getattr(info, "st_blocks", 0) * 512
    return {"logical_bytes": logical, "allocated_inode_deduplicated_bytes": allocated,
            "file_count": files}


def _cleanup_result_status(record: Mapping[str, Any]) -> str:
    """Keep the summary status consistent with every durable cleanup invariant."""
    storage_status = str(record.get("storage_hygiene_status", "FAIL_STORAGE_HYGIENE"))
    free_status = str(record.get("free_space_status", "FAIL_UNKNOWN"))
    threshold_failures = _cleanup_gate0_threshold_failures(record)
    if (storage_status == "FAIL_STORAGE_HYGIENE" or free_status.startswith("FAIL")
            or threshold_failures
            or record.get("receipt_write_error")
            or record.get("receipt_finalization_complete") is False
            or (record.get("prefix_created") and not record.get("prefix_deleted"))):
        return "FAIL"
    if storage_status.startswith("WARN") or free_status.startswith("WARN"):
        return "WARN"
    return "PASS"


def _cleanup_gate0_threshold_failures(record: Mapping[str, Any]) -> list[str]:
    """Return Gate 0 threshold failures from the authoritative post-cleanup sample."""
    failures = list(record.get("gate0_threshold_failures", []))
    free_after = record.get("free_after_bytes")
    if (isinstance(free_after, (int, float)) and not isinstance(free_after, bool)
            and free_after < FREE_MINIMUM and "free_space_below_gate0_minimum" not in failures):
        failures.append("free_space_below_gate0_minimum")
    project_after = record.get("project_usage_after")
    project_bytes = (project_after.get("allocated_inode_deduplicated_bytes")
                     if isinstance(project_after, Mapping) else None)
    if (isinstance(project_bytes, (int, float)) and not isinstance(project_bytes, bool)
            and project_bytes > GATE0_PROJECT_BUDGET
            and "project_usage_above_gate0_budget" not in failures):
        failures.append("project_usage_above_gate0_budget")
    return failures


def _invalidate_gate0_after_cleanup_threshold_failure(
    experiment_id: str, failures: list[str], *, free_after_bytes: Any,
    project_after_bytes: Any,
) -> dict[str, Any]:
    """Durably block later admissions after a cleanup misses Gate 0 thresholds.

    The existing attestation is changed only after its current HMAC verifies. An
    already-blocked or otherwise non-PASS attestation remains untouched because it
    already prevents admission.
    """
    raw = _read_file_nofollow(GATE0_ATTESTATION_PATH)
    attestation = json.loads(raw)
    if not isinstance(attestation, dict) or not _verify_gate0_signature(attestation):
        raise RuntimeError("cannot invalidate an untrusted or malformed Gate 0 attestation")
    current_status = attestation.get("status")
    if current_status not in {"PASS", "PREFLIGHT_PASS"}:
        return {"status": current_status, "already_blocked": True}
    invalidation = {
        "experiment_id": experiment_id,
        "invalidated_at_unix": time.time(),
        "reason": "PrefixLease post-cleanup storage thresholds failed",
        "threshold_failures": list(failures),
        "free_after_bytes": free_after_bytes,
        "project_usage_after_bytes": project_after_bytes,
    }
    attestation["status"] = "BLOCKED"
    attestation["post_cleanup_threshold_failure"] = invalidation
    attestation["gate0_hmac_sha256"] = _gate0_signature(attestation)
    _write_json(GATE0_ATTESTATION_PATH, attestation, strict_directory_sync=True)
    readback = json.loads(_read_file_nofollow(GATE0_ATTESTATION_PATH))
    if (not isinstance(readback, dict)
            or readback.get("status") != "BLOCKED"
            or readback.get("post_cleanup_threshold_failure") != invalidation
            or not _verify_gate0_signature(readback)):
        raise RuntimeError("Gate 0 cleanup-threshold invalidation failed durable read-back")
    return {"status": "BLOCKED", "already_blocked": False,
            "attestation_sha256": hashlib.sha256(
                _read_file_nofollow(GATE0_ATTESTATION_PATH)).hexdigest()}


def _cleanup_receipt_timestamp(record: Mapping[str, Any]) -> float | None:
    """Return a durable cleanup lifecycle timestamp, including legacy receipts."""
    value = record.get("created_at_unix")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        # Older PrefixLease receipts predate created_at_unix. Marker removal is
        # written only after the receipt has been durably finalized.
        value = record.get("active_lease_marker_removed_at_unix")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        return None
    return float(value)


def _cleanup_failure_overrides_exit(status: str, exc: BaseException | None) -> bool:
    """Prevent a successful process exit from masking failed lease cleanup."""
    success_exit = isinstance(exc, SystemExit) and (exc.code is None or exc.code == 0)
    return status != "PASS" and (exc is None or success_exit)


def _write_prefix_state(prefix: Path, template: Path, output_dir: Path,
                        template_marker: Mapping[str, Any] | None) -> dict[str, Any]:
    current = _prefix_payload(prefix)
    baseline = _prefix_payload(template)
    changed: dict[str, Any] = {}
    diffs: list[str] = []
    for name in ("system.reg", "user.reg", "userdef.reg"):
        old_path, new_path = template / name, prefix / name
        old_bytes = old_path.read_bytes() if old_path.is_file() else b""
        new_bytes = new_path.read_bytes() if new_path.is_file() else b""
        if old_bytes == new_bytes:
            continue
        old_lines = old_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        new_lines = new_bytes.decode("utf-8", errors="replace").splitlines(keepends=True)
        diffs.extend(difflib.unified_diff(old_lines, new_lines,
                                         fromfile=f"template/{name}", tofile=f"run/{name}"))
        changed[name] = {"template": baseline["files"].get(name),
                         "run": current["files"].get(name)}
    if diffs:
        diff_bytes = "".join(diffs).encode("utf-8")
        compressed_path = output_dir / "prefix-registry-diff.txt.gz"
        compressed_bytes = gzip.compress(diff_bytes, compresslevel=9, mtime=0)
        _atomic_write_bytes(compressed_path, compressed_bytes, strict_directory_sync=True)
        diff_record: dict[str, Any] = {
            "path": str(compressed_path),
            "uncompressed_bytes": len(diff_bytes),
            "compressed_bytes": len(compressed_bytes),
            "sha256": hashlib.sha256(compressed_bytes).hexdigest(),
        }
    else:
        diff_record = {"path": None, "uncompressed_bytes": 0, "compressed_bytes": 0,
                      "sha256": None}
    return {
        "prefix": current,
        "template": baseline,
        "changed_registry_files": changed,
        "registry_diff": diff_record,
        "dosdevices_changed": current["dosdevices"] != baseline["dosdevices"],
        "dosdevices": current["dosdevices"],
        "template_tree_sha256": ((template_marker or {}).get("tree_identity") or {}).get("tree_sha256"),
    }


@dataclass
class PrefixLease:
    prefix: Path
    output_dir: Path
    experiment_id: str
    wine_server: Path
    env: Mapping[str, str] | None = None
    template: Path = PREFIX_TEMPLATE
    reason_on_failure: str = "automatic cleanup could not verify wineserver shutdown"
    clone_template: bool = True
    manifest_path: Path | None = None

    def __post_init__(self) -> None:
        configured_prefix = os.fspath(self.prefix)
        if (not isinstance(configured_prefix, str)
                or not Path(configured_prefix).is_absolute()
                or any(part == ".." for part in configured_prefix.split(os.sep))
                or os.path.abspath(configured_prefix) != configured_prefix):
            raise ValueError("PrefixLease prefix must be a canonical absolute path without parent components")
        self.prefix = Path(configured_prefix)
        self.output_dir = Path(os.path.abspath(self.output_dir.expanduser()))
        self.wine_server = Path(os.path.abspath(self.wine_server.expanduser()))
        self.template = Path(os.path.abspath(self.template.expanduser()))
        self._free_before = 0
        self._created = False
        self._materialized = False
        self._env: dict[str, str] = dict(self.env or os.environ)
        self._manifest: dict[str, Any] = {}
        self._manifest_estimates: dict[str, int] = {}
        self._project_before: dict[str, Any] = {}
        self._template_marker: dict[str, Any] | None = None
        self._storage_watch_events: list[dict[str, Any]] = []
        self._lease_marker_path: Path | None = None
        self._manifest_sha256: str | None = None
        self._previous_sigterm_handler: Any = None
        self._sigterm_number: int | None = None
        self._cleanup_started = False
        self._managed_processes: list[subprocess.Popen[Any]] = []
        self._unsafe_live_process = False
        self._storage_violation = False
        self._clone_complete = False
        # True only until this lease creates any prefix/clone inode, or after
        # every inode it created has been removed through its pinned parent.
        # A missing pathname alone never changes this proof to true.
        self._leased_tree_absence_proven = True
        # Any unbound namespace entry observed at the requested lease name is
        # a failed reservation, even if it disappears before finalization.
        self._unsafe_prefix_observed = False
        self._runtime_env_verified = False
        self._sealed_wine_env: dict[str, str] | None = None
        self._wine_env_set_attempted = False
        self._wine_started = False
        self._wine_loader_path: Path | None = None
        self._wine_loader_sha256: str | None = None
        self._wine_loader_identity: tuple[int, int, int, int, int] | None = None
        self._wine_graphics_contract: dict[str, Any] | None = None
        self._prefix_parent_fd: int | None = None
        self._clone_temp_name: str | None = None
        self._clone_temp_identity: tuple[int, int] | None = None
        self._clone_temp_fd: int | None = None
        self._prefix_identity: tuple[int, int] | None = None
        self._manifest_parent_identity: tuple[int, int] | None = None
        self._manifest_file_identity: tuple[int, int, int, int, int] | None = None
        self._lease_marker_parent_identity: tuple[int, int] | None = None
        self._wine_launch_prefix: str | None = None
        self._wineserver_prefix: str | None = None
        self._prefix_created_any = False
        self._lease_gate_lock_file: Any = None
        self._storage_allocation_lock_file: Any = None
        self._free_warning_emitted = False
        self._lease_token = secrets.token_hex(32)
        self._materialization_receipt: dict[str, Any] | None = None

    def __enter__(self) -> "PrefixLease":
        allowed = [PREFIX_ROOT.absolute(), (Path.home() / "Library/Caches/FGMetalStep11D-R").absolute()]
        if not any(self.prefix.parent == root or root in self.prefix.parents for root in allowed):
            raise ValueError(f"prefix path is outside FG-Metal cache roots: {self.prefix}")
        prefix_parent_fd = _open_directory_nofollow(self.prefix.parent)
        try:
            try:
                os.stat(self.prefix.name, dir_fd=prefix_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(f"refusing to reuse an existing prefix: {self.prefix}")
        finally:
            os.close(prefix_parent_fd)
        self.manifest_path = (self.manifest_path or
                              REPO / "experiments/storage/manifests" / f"{self.experiment_id}.json")
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"experiment storage manifest is required before prefix setup: {self.manifest_path}")
        manifest_parent_fd = _open_directory_nofollow(self.manifest_path.parent)
        try:
            manifest_parent_info = os.fstat(manifest_parent_fd)
            manifest_bytes, manifest_file_identity = _read_file_from_directory_fd(
                manifest_parent_fd, self.manifest_path.name, display_path=self.manifest_path)
            self._manifest_parent_identity = (manifest_parent_info.st_dev, manifest_parent_info.st_ino)
            self._manifest_file_identity = manifest_file_identity
        finally:
            os.close(manifest_parent_fd)
        self._manifest = json.loads(manifest_bytes)
        self._manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if not _verify_manifest_signature(self._manifest):
            raise RuntimeError("PrefixLease refuses an edited or unsigned storage manifest")
        try:
            self._manifest_estimates = _validated_manifest_estimates(self._manifest)
        except ValueError as error:
            raise RuntimeError(f"PrefixLease refuses invalid storage estimates: {error}") from error
        if self._manifest.get("decision") != "ALLOW":
            raise RuntimeError("PrefixLease refuses a manifest that did not pass the storage gate")
        if self._manifest.get("experiment_id") != self.experiment_id:
            raise ValueError("PrefixLease experiment ID does not match the storage manifest")
        storage_smoke = self._manifest.get("storage_policy_smoke") is True
        if storage_smoke:
            expected_output = REPO / "experiments/storage/smoke-runs" / self.experiment_id
            expected_prefix = PREFIX_ROOT / f"storage-smoke-{self.experiment_id}"
            if (not _is_storage_policy_smoke_id(self.experiment_id)
                    or self.output_dir != expected_output
                    or self.prefix != expected_prefix
                    or self._manifest_estimates["expected_duration_seconds"] != 300
                    or self._manifest_estimates["expected_prefix_bytes"] != 3 * GIB
                    or self._manifest_estimates["expected_build_bytes"] != 0
                    or self._manifest_estimates["estimated_persistent_bytes"] != 32 * MIB):
                raise RuntimeError("PrefixLease rejects a storage smoke outside the fixed path and estimates")
        elif _is_storage_policy_smoke_id(self.experiment_id):
            raise RuntimeError("PrefixLease reserves storage-smoke IDs for the storage smoke contract")
        try:
            manifest_output = _measured_output_dir(self._manifest.get("output_dir", ""))
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"PrefixLease output is outside measured project storage: {error}") from error
        if str(manifest_output) != self._manifest.get("output_dir") or manifest_output != self.output_dir:
            raise ValueError("PrefixLease output directory does not match the storage manifest")
        expected_manifest_path = _experiment_manifest_path(self.experiment_id)
        if self.manifest_path.absolute() != expected_manifest_path.absolute() or self.manifest_path.is_symlink():
            raise ValueError("PrefixLease manifest path must be the reserved project manifest for its experiment ID")
        if not self._manifest.get("expected_prefix_requirement"):
            raise ValueError("PrefixLease was not declared in the pre-run storage manifest")
        if self._manifest.get("internal_storage_setup") is True:
            raise RuntimeError("legacy internal-storage manifests cannot bypass Gate 0")
        attestation_hash = hashlib.sha256(_read_file_nofollow(GATE0_ATTESTATION_PATH)).hexdigest() \
            if GATE0_ATTESTATION_PATH.is_file() and not GATE0_ATTESTATION_PATH.is_symlink() else None
        if self._manifest.get("gate0_attestation_sha256") != attestation_hash:
            raise RuntimeError("PrefixLease manifest does not bind to the current Gate 0 attestation")
        gate_blockers = _gate0_blockers(active_experiment_id=self.experiment_id,
                                        allow_storage_smoke=storage_smoke)
        if gate_blockers:
            raise RuntimeError("Gate 0 blocks PrefixLease startup: " + "; ".join(gate_blockers))
        if not self.clone_template or self.template.absolute() != PREFIX_TEMPLATE.absolute():
            raise ValueError("PrefixLease may clone only the one certified Wine template")
        template_size = _prefix_size(self.template)
        prefix_estimate = self._manifest_estimates["expected_prefix_bytes"]
        if prefix_estimate < template_size["logical_bytes"]:
            raise ValueError("PrefixLease prefix estimate is smaller than the certified template contents")
        self._free_before = disk_free_bytes(self.prefix.parent)
        self._project_before = measure_project_usage()
        project_now = conservative_project_usage_bytes(self._project_before)
        estimated_peak = self._manifest_estimates["estimated_projected_new_disk_bytes"]
        admission = projected_storage_admission(
            current_free_bytes=self._free_before,
            current_scoped_bytes=project_now,
            peak_additional_bytes=estimated_peak,
        )
        if not admission["admitted"]:
            raise RuntimeError("PrefixLease projected-peak admission failed: "
                               + "; ".join(admission["blockers"]))
        for warning in admission["warnings"]:
            self._storage_watch_events.append({
                "sampled_at_unix": time.time(),
                "action": "PROJECTED_STORAGE_WARNING_BEFORE_PREFIX_LEASE",
                "warning": warning,
            })
        marker = self.template / "FGMETAL_PREFIX_TEMPLATE.json"
        if self.clone_template:
            if not marker.is_file():
                raise FileNotFoundError(f"certified Wine template marker is unavailable: {marker}")
            self._template_marker = json.loads(_read_file_nofollow(marker))
            if self._template_marker.get("graphics_experiments_run") is not False:
                raise ValueError("Wine template is not certified graphics-experiment free")
            actual = _tree_fingerprint(self.template, exclude={marker.name})
            expected = self._template_marker.get("tree_identity", {})
            if actual != expected:
                raise ValueError("Wine template contents no longer match the certified fingerprint")
            runtime_identity = self._template_marker.get("runtime_identity", {})
            candidate_hashes = self._manifest.get("candidate_hashes", {})
            if candidate_hashes.get("wine_runtime_tree") != runtime_identity.get("wine_runtime_tree_sha256"):
                raise ValueError("experiment manifest Wine runtime tree does not match the certified template")
            if candidate_hashes.get("wine_binary") != runtime_identity.get("wine_binary_sha256"):
                raise ValueError("experiment manifest Wine binary does not match the certified template")
            expected_moltenvk = runtime_identity.get("moltenvk_sha256_for_subsequent_graphics_runs")
            if expected_moltenvk and candidate_hashes.get("moltenvk") != expected_moltenvk:
                raise ValueError("experiment manifest MoltenVK binary does not match the certified candidate")
            expected_root = Path(runtime_identity.get("wine_runtime_root", "")).expanduser().absolute()
            if not expected_root.is_dir() or self.wine_server != expected_root / "bin/wineserver":
                raise ValueError("PrefixLease was configured with a different wineserver root")
            self._wine_graphics_contract = _wine_graphics_identity_contract(
                self._manifest, runtime_identity)
        active_root_fd = _open_directory_nofollow(
            ACTIVE_LEASE_ROOT, create=True, strict_directory_sync=True)
        lock_path = ACTIVE_LEASE_ROOT / ".lease.lock"
        lock_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_fd: int | None = None
        try:
            lock_fd = os.open(".lease.lock", lock_flags, 0o600, dir_fd=active_root_fd)
            os.fsync(active_root_fd)
            active_root_info = os.fstat(active_root_fd)
            self._lease_marker_parent_identity = (active_root_info.st_dev, active_root_info.st_ino)
        except BaseException:
            if lock_fd is not None:
                os.close(lock_fd)
            raise
        finally:
            os.close(active_root_fd)
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1):
            os.close(lock_fd)
            raise ValueError(f"PrefixLease gate lock is not a private regular file: {lock_path}")
        self._lease_gate_lock_file = os.fdopen(lock_fd, "a+")
        try:
            fcntl.flock(self._lease_gate_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lease_gate_lock_file.close()
            self._lease_gate_lock_file = None
            raise RuntimeError("another PrefixLease is starting or running") from error
        try:
            self._storage_allocation_lock_file = _acquire_storage_allocation_lock()
            locked_blockers = _gate0_blockers(active_experiment_id=self.experiment_id,
                                               allow_storage_smoke=storage_smoke)
            if locked_blockers:
                raise RuntimeError("Gate 0 changed while acquiring the shared allocation lock: "
                                   + "; ".join(locked_blockers))
            locked_free = disk_free_bytes(self.prefix.parent)
            locked_usage = measure_project_usage()
            locked_project = conservative_project_usage_bytes(locked_usage)
            locked_peak = int(self._manifest_estimates["estimated_projected_new_disk_bytes"])
            locked_admission = projected_storage_admission(
                current_free_bytes=locked_free,
                current_scoped_bytes=locked_project,
                peak_additional_bytes=locked_peak,
            )
            if not locked_admission["admitted"]:
                raise RuntimeError("PrefixLease projected-peak admission failed under the shared allocation lock: "
                                   + "; ".join(locked_admission["blockers"]))
            for warning in locked_admission["warnings"]:
                self._storage_watch_events.append({
                    "sampled_at_unix": time.time(),
                    "action": "PROJECTED_STORAGE_WARNING_BEFORE_PREFIX_LEASE",
                    "warning": warning,
                })
            active = _active_lease_markers()
            if active:
                raise RuntimeError("an active or interrupted PrefixLease requires reconciliation: "
                                   + ", ".join(str(path) for path in active))
            self._lease_marker_path = ACTIVE_LEASE_ROOT / f"{self.experiment_id}.json"
            if self._lease_marker_path.exists() or self._lease_marker_path.is_symlink():
                raise FileExistsError(f"active PrefixLease marker already exists: {self._lease_marker_path}")
            self._write_active_lease("PREPARED")
        except BaseException:
            self._release_global_lease_gate()
            raise
        try:
            self._previous_sigterm_handler = signal.signal(signal.SIGTERM, self._handle_sigterm)
        except ValueError:
            # PrefixLease normally runs on the main thread. If embedded in a worker,
            # the caller's outer thread must handle process termination.
            self._previous_sigterm_handler = None
        allocation_lock_file = self._storage_allocation_lock_file
        self._storage_allocation_lock_file = None
        _release_storage_allocation_lock(allocation_lock_file)
        return self

    def _release_global_lease_gate(self) -> None:
        lock_file = self._lease_gate_lock_file
        self._lease_gate_lock_file = None
        allocation_lock_file = self._storage_allocation_lock_file
        self._storage_allocation_lock_file = None
        try:
            if lock_file is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            if lock_file is not None:
                lock_file.close()
            _release_storage_allocation_lock(allocation_lock_file)

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        if self._cleanup_started:
            return
        self._sigterm_number = signum
        raise KeyboardInterrupt(f"PrefixLease interrupted by signal {signum}")

    def _write_active_lease(self, state: str) -> None:
        if self._lease_marker_path is None:
            return
        active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
        try:
            active_root_info = os.fstat(active_root_fd)
            current_parent_identity = (active_root_info.st_dev, active_root_info.st_ino)
            if (self._lease_marker_parent_identity is not None
                    and current_parent_identity != self._lease_marker_parent_identity):
                raise OSError("active PrefixLease marker parent changed after reservation")
            if self._lease_marker_parent_identity is None:
                self._lease_marker_parent_identity = current_parent_identity
        finally:
            os.close(active_root_fd)
        active_record = {
            "experiment_id": self.experiment_id,
            "pid": os.getpid(),
            "state": state,
            "prefix_path": str(self.prefix),
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self._manifest_sha256,
            "lease_token": self._lease_token,
            "updated_at_unix": time.time(),
        }
        if self._materialization_receipt is not None:
            active_record["materialization_receipt"] = self._materialization_receipt
        _write_json(self._lease_marker_path, active_record, strict_directory_sync=True)

    def _assert_exact_wineprefix(self, env: Mapping[str, str]) -> str:
        expected = str(self.prefix)
        value = env.get("WINEPREFIX")
        if (not self.prefix.is_absolute()
                or os.path.abspath(expected) != expected
                or any(part == ".." for part in expected.split(os.sep))
                or value != expected):
            raise ValueError("Wine WINEPREFIX must be the exact canonical absolute PrefixLease path")
        return expected

    def _assert_bound_prefix_lease(self, env: Mapping[str, str]) -> None:
        """Revalidate the pinned prefix, manifest, and both lease markers at launch/cleanup."""
        self._assert_exact_wineprefix(env)
        self._assert_prefix_real()

        if (self.manifest_path is None or self._manifest_sha256 is None
                or self._manifest_parent_identity is None or self._manifest_file_identity is None):
            raise RuntimeError("PrefixLease manifest identity was not bound at admission")
        expected_manifest = _experiment_manifest_path(self.experiment_id)
        if str(self.manifest_path) != str(expected_manifest):
            raise RuntimeError("PrefixLease manifest path changed after admission")
        manifest_parent_fd = _open_directory_nofollow(self.manifest_path.parent)
        try:
            manifest_parent_info = os.fstat(manifest_parent_fd)
            if ((manifest_parent_info.st_dev, manifest_parent_info.st_ino)
                    != self._manifest_parent_identity):
                raise OSError("PrefixLease manifest parent changed after admission")
            manifest_bytes, manifest_identity = _read_file_from_directory_fd(
                manifest_parent_fd, self.manifest_path.name, display_path=self.manifest_path)
        finally:
            os.close(manifest_parent_fd)
        if (manifest_identity != self._manifest_file_identity
                or hashlib.sha256(manifest_bytes).hexdigest() != self._manifest_sha256):
            raise RuntimeError("PrefixLease manifest identity or contents changed after admission")
        manifest = json.loads(manifest_bytes)
        if (not _verify_manifest_signature(manifest)
                or manifest.get("decision") != "ALLOW"
                or manifest.get("experiment_id") != self.experiment_id
                or manifest.get("output_dir") != str(self.output_dir)):
            raise RuntimeError("PrefixLease manifest no longer authenticates this lease")

        expected_active_marker = ACTIVE_LEASE_ROOT / f"{self.experiment_id}.json"
        if (self._lease_marker_path != expected_active_marker
                or self._lease_marker_parent_identity is None):
            raise RuntimeError("PrefixLease active marker identity was not bound at admission")
        active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
        try:
            active_root_info = os.fstat(active_root_fd)
            if ((active_root_info.st_dev, active_root_info.st_ino)
                    != self._lease_marker_parent_identity):
                raise OSError("active PrefixLease marker parent changed after admission")
            active_marker, active_bytes, active_file_info = _read_json_at(
                active_root_fd, expected_active_marker.name,
                maximum_bytes=1024 * 1024)
        finally:
            os.close(active_root_fd)
        _validate_materialized_active_prefix_marker(
            active_marker, active_bytes, active_file_info)
        expected_binding = {
            "experiment_id": self.experiment_id,
            "pid": os.getpid(),
            "prefix_path": str(self.prefix),
            "output_dir": str(self.output_dir),
            "manifest_path": str(expected_manifest),
            "manifest_sha256": self._manifest_sha256,
            "lease_token": self._lease_token,
            "state": "MATERIALIZED",
        }
        if any(active_marker.get(key) != value for key, value in expected_binding.items()):
            raise RuntimeError("active PrefixLease marker is stale or names a different prefix")
        active_receipt = active_marker.get("materialization_receipt")
        if (not isinstance(active_receipt, Mapping)
                or active_receipt != self._materialization_receipt
                or not _verify_storage_evidence(
                    active_receipt, "prefix_materialization_hmac_sha256")):
            raise RuntimeError("active PrefixLease materialization receipt changed after admission")

        prefix_fd = _open_directory_nofollow(self.prefix)
        try:
            prefix_info = os.fstat(prefix_fd)
            if self._prefix_identity != (prefix_info.st_dev, prefix_info.st_ino):
                raise OSError("leased prefix inode changed after materialization")
            prefix_marker_bytes, _prefix_marker_identity = _read_file_from_directory_fd(
                prefix_fd, "FGMETAL_PREFIX_LEASE.json",
                display_path=self.prefix / "FGMETAL_PREFIX_LEASE.json")
        finally:
            os.close(prefix_fd)
        prefix_marker = json.loads(prefix_marker_bytes)
        if any(prefix_marker.get(key) != value for key, value in expected_binding.items()
               if key != "state"):
            raise RuntimeError("materialized prefix marker is stale or names a different lease")

    def set_env(self, env: Mapping[str, str]) -> None:
        if self._wine_env_set_attempted:
            raise RuntimeError("PrefixLease Wine environment may be validated and sealed only once")
        self._wine_env_set_attempted = True
        selected_env = dict(env)
        if "FG_B_TARGET_UNIX_PID" in selected_env:
            raise ValueError("trace target PID is assigned only by the supervised exec wrapper")
        if "FG_B_TRACE_DIRECTORY" in selected_env:
            trace_path = Path(selected_env["FG_B_TRACE_DIRECTORY"])
            if (trace_path != self.output_dir / "host-traces"
                    or not trace_path.is_absolute()
                    or str(trace_path.resolve(strict=True)) != str(trace_path)
                    or selected_env.get("FG_B_EXPERIMENT_TOKEN") != self._manifest_sha256[:32]):
                raise ValueError("host trace directory/token does not bind the leased run")
            trace_fd = _open_directory_nofollow(trace_path)
            try:
                trace_info = os.fstat(trace_fd)
                if (selected_env.get("FG_B_TRACE_DIRECTORY_DEVICE") != str(trace_info.st_dev)
                        or selected_env.get("FG_B_TRACE_DIRECTORY_INODE") != str(trace_info.st_ino)):
                    raise ValueError("host trace directory identity changed")
            finally:
                os.close(trace_fd)
        _validate_wine_child_environment(selected_env)
        self._assert_exact_wineprefix(selected_env)
        self._runtime_env_verified = False
        self._sealed_wine_env = None
        if self.clone_template:
            runtime_identity = (self._template_marker or {}).get("runtime_identity", {})
            runtime_root_value = runtime_identity.get("wine_runtime_root")
            graphics_contract = _wine_graphics_identity_contract(self._manifest, runtime_identity)
            self._wine_graphics_contract = graphics_contract
            loader_value = selected_env.get("WINELOADER")
            server_value = selected_env.get("WINESERVER")
            if (not isinstance(runtime_root_value, str)
                    or not Path(runtime_root_value).is_absolute()
                    or str(Path(runtime_root_value)) != runtime_root_value
                    or os.path.abspath(runtime_root_value) != runtime_root_value):
                raise ValueError("certified Wine runtime root is missing or non-canonical")
            expected_root = Path(runtime_root_value)
            if (not isinstance(loader_value, str)
                    or not isinstance(server_value, str)
                    or loader_value != str(expected_root / "bin/wine")
                    or server_value != str(expected_root / "bin/wineserver")):
                raise ValueError("PrefixLease environment does not select the certified Wine runtime")
            loader = Path(loader_value)
            server = Path(server_value)
            if selected_env.get("WINEPREFIX") != str(self.prefix):
                raise ValueError("PrefixLease environment WINEPREFIX does not match the leased prefix")
            if graphics_contract is not None:
                if (loader != graphics_contract["launcher_path"]
                        or graphics_contract["launcher_sha256"]
                        != runtime_identity.get("wine_binary_sha256")):
                    raise ValueError("PrefixLease environment does not select the certified Wine launcher")
                loader_sha256 = graphics_contract["launcher_sha256"]
                loader_identity = graphics_contract["launcher_identity"]
            else:
                try:
                    loader_sha256, loader_identity = _wine_loader_identity(loader)
                except ValueError as error:
                    raise ValueError(
                        "PrefixLease environment does not select a safe certified Wine executable") from error
            if (loader != expected_root / "bin/wine"
                    or loader_sha256 != runtime_identity.get("wine_binary_sha256")
                    or server != expected_root / "bin/wineserver"):
                raise ValueError("PrefixLease environment does not select the certified Wine runtime")
            expected_mvk = runtime_identity.get("moltenvk_sha256_for_subsequent_graphics_runs")
            if not isinstance(expected_mvk, str):
                raise ValueError("certified Wine template has no MoltenVK SHA-256")
            selected_env = _normalize_wine_dyld_environment(
                selected_env,
                expected_moltenvk_sha256=expected_mvk,
                trusted_roots=(self.output_dir, expected_root / "lib",
                               Path("/usr/local/lib"), Path("/usr/lib")),
                candidate_fallback_roots=(expected_root / "lib",),
            )
            expected_icd = runtime_identity.get("moltenvk_icd_sha256")
            selected_icd = [Path(selected_env[key]).expanduser()
                            for key in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES")
                            if selected_env.get(key)]
            if not any(path.is_file() and not path.is_symlink()
                       and hashlib.sha256(path.read_bytes()).hexdigest() == expected_icd
                       for path in selected_icd):
                raise ValueError("PrefixLease environment does not select the certified MoltenVK ICD")
            manifest_path_value = runtime_identity.get("wine_runtime_tree_manifest")
            manifest_path = (Path(manifest_path_value) if isinstance(manifest_path_value, str)
                             else Path())
            pinned_manifest = WINE_RUNTIME_TREE_MANIFEST
            if (not isinstance(manifest_path_value, str)
                    or manifest_path_value != str(pinned_manifest)
                    or manifest_path.is_symlink()
                    or not manifest_path.is_file()):
                raise ValueError("certified Wine runtime tree manifest is missing or unsafe")
            from initialize_prefix_template import actual_runtime_tree_hash
            actual_tree, _entry_count, _missing = actual_runtime_tree_hash(expected_root)
            if actual_tree != runtime_identity.get("wine_runtime_tree_sha256"):
                raise ValueError("active Wine runtime no longer matches the certified tree")
            _validate_wine_child_environment(selected_env)
            self._env = selected_env
            self._runtime_env_verified = True
            self._wine_loader_path = loader
            self._wine_loader_sha256 = loader_sha256
            self._wine_loader_identity = loader_identity
            self._sealed_wine_env = dict(self._env)
        else:
            self._env = selected_env

    @property
    def storage_watch_events(self) -> list[dict[str, Any]]:
        """Return a copy of the bounded storage watcher log for run results."""
        return [dict(event) for event in self._storage_watch_events]

    def _assert_prefix_real(self) -> None:
        self._assert_prefix_parent_bound()
        info = _stat_entry_nofollow(self.prefix)
        if self._prefix_parent_fd is not None:
            descriptor_info = _stat_child_nofollow(self._prefix_parent_fd, self.prefix.name)
            if (descriptor_info is None or info is None
                    or (descriptor_info.st_dev, descriptor_info.st_ino)
                    != (info.st_dev, info.st_ino)):
                raise OSError(f"leased prefix ancestry changed from its verified directory: {self.prefix}")
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"leased prefix ancestry or directory changed: {self.prefix}")
        if (self._prefix_identity is not None
                and (info.st_dev, info.st_ino) != self._prefix_identity):
            raise OSError(f"leased prefix directory identity changed: {self.prefix}")

    def _assert_prefix_parent_bound(self) -> None:
        if self._prefix_parent_fd is None:
            raise OSError("verified prefix parent directory handle is unavailable")
        current_parent_fd = _open_directory_nofollow(self.prefix.parent)
        try:
            retained = os.fstat(self._prefix_parent_fd)
            current = os.fstat(current_parent_fd)
            if (retained.st_dev, retained.st_ino) != (current.st_dev, current.st_ino):
                raise OSError(f"prefix parent changed after lease binding: {self.prefix.parent}")
        finally:
            os.close(current_parent_fd)

    def _assert_clone_target_real(self) -> None:
        self._assert_prefix_parent_bound()
        if self._clone_temp_name is None or self._clone_temp_fd is None:
            raise OSError("prefix clone temporary name or pinned directory handle is unavailable")
        info = _stat_child_nofollow(self._prefix_parent_fd, self._clone_temp_name)
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise OSError("prefix clone target or its parent changed during materialization")
        pinned = os.fstat(self._clone_temp_fd)
        pinned_fsid = getattr(os.fstatvfs(self._clone_temp_fd), "f_fsid", None)
        parent_fsid = getattr(os.fstatvfs(self._prefix_parent_fd), "f_fsid", None)
        if (self._clone_temp_identity is None
                or (info.st_dev, info.st_ino) != self._clone_temp_identity
                or (pinned.st_dev, pinned.st_ino) != self._clone_temp_identity
                or info.st_dev != os.fstat(self._prefix_parent_fd).st_dev
                or pinned_fsid is None or pinned_fsid != parent_fsid):
            raise OSError("prefix clone target identity or filesystem changed during materialization")

    def wait_process(self, process: subprocess.Popen[Any], *, timeout_seconds: float,
                     poll_interval_seconds: float = 2.0,
                     output_limit_bytes: int | None = None) -> int:
        """Wait while enforcing the emergency free-space stop during a run."""
        if (output_limit_bytes is not None
                and (not isinstance(output_limit_bytes, int)
                     or isinstance(output_limit_bytes, bool)
                     or output_limit_bytes <= 0)):
            raise ValueError("output_limit_bytes must be a positive integer when provided")
        if self._wine_loader_path is not None:
            args = process.args if isinstance(process.args, (list, tuple)) else [process.args]
            if str(self._wine_loader_path) in {str(arg) for arg in args}:
                self._wine_started = True
        if process not in self._managed_processes:
            self._managed_processes.append(process)
        started = time.monotonic()
        deadline = started + timeout_seconds
        while True:
            if process is self._clone_process:
                try:
                    self._assert_clone_target_real()
                except OSError as error:
                    self._storage_violation = True
                    if not self._terminate_and_reap(process):
                        self._unsafe_live_process = True
                    elif process in self._managed_processes:
                        self._managed_processes.remove(process)
                    raise RuntimeError(f"leased prefix or clone target changed during the run: {error}") from error
            elif self._wine_started and self._materialized:
                try:
                    self._assert_prefix_real()
                except OSError as error:
                    self._storage_violation = True
                    if not self._terminate_and_reap(process):
                        self._unsafe_live_process = True
                    elif process in self._managed_processes:
                        self._managed_processes.remove(process)
                    raise RuntimeError(f"Wine prefix path changed during the run: {error}") from error
            if output_limit_bytes is not None:
                output_allocated = _persistent_output_allocated_bytes(self.output_dir)
                stop_margin = min(64 * MIB, max(1 * MIB, output_limit_bytes // 100))
                stop_threshold = output_limit_bytes - stop_margin
                if output_allocated >= stop_threshold:
                    event = {
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "sampled_at_unix": time.time(),
                        "action": "STOP_OUTPUT_BUDGET_GUARD",
                        "output_allocated_bytes": output_allocated,
                        "output_limit_bytes": output_limit_bytes,
                        "output_stop_threshold_bytes": stop_threshold,
                        "output_stop_margin_bytes": stop_margin,
                    }
                    self._storage_watch_events.append(event)
                    if not self._terminate_and_reap(process):
                        self._unsafe_live_process = True
                        event["process_still_running"] = True
                    elif process in self._managed_processes:
                        self._managed_processes.remove(process)
                    raise RuntimeError(
                        "signed output budget guard stopped the target before the reservation boundary: "
                        f"{output_allocated} >= {stop_threshold} of {output_limit_bytes} bytes")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                completed = process.poll()
                if completed is not None:
                    self._managed_processes.remove(process)
                    self._sample_terminal_free_space(started, "PROCESS_COMPLETED_AT_DEADLINE")
                    return completed
                terminal_free = disk_free_bytes(self.prefix.parent)
                event = {"elapsed_seconds": round(time.monotonic() - started, 3),
                         "free_bytes": terminal_free,
                         "sampled_at_unix": time.time(), "action": "PROCESS_TIMEOUT_TERMINATE"}
                if terminal_free < FREE_HARD_STOP:
                    event["action"] = "HARD_STOP_TERMINATE"
                    self._storage_violation = True
                elif terminal_free < FREE_WARNING and not self._free_warning_emitted:
                    event["action"] = "WARNING_NO_NEW_EXPERIMENTS"
                    self._free_warning_emitted = True
                self._storage_watch_events.append(event)
                if not self._terminate_and_reap(process):
                    self._unsafe_live_process = True
                    event["process_still_running"] = True
                else:
                    self._managed_processes.remove(process)
                raise subprocess.TimeoutExpired(process.args, timeout_seconds)
            try:
                returncode = process.wait(timeout=min(poll_interval_seconds, remaining))
                self._managed_processes.remove(process)
                self._sample_terminal_free_space(started, "PROCESS_COMPLETED")
                return returncode
            except subprocess.TimeoutExpired:
                free = disk_free_bytes(self.prefix.parent)
                event = {"elapsed_seconds": round(time.monotonic() - started, 3),
                         "free_bytes": free, "sampled_at_unix": time.time()}
                if free < FREE_HARD_STOP:
                    event["action"] = "HARD_STOP_TERMINATE"
                    self._storage_watch_events.append(event)
                    self._storage_violation = True
                    if not self._terminate_and_reap(process):
                        self._unsafe_live_process = True
                        event["process_still_running"] = True
                    else:
                        self._managed_processes.remove(process)
                    raise RuntimeError(
                        f"free space crossed the 25 GiB hard stop during the run: {free}"
                    )
                if free < FREE_WARNING and not self._free_warning_emitted:
                    event["action"] = "WARNING_NO_NEW_EXPERIMENTS"
                    self._free_warning_emitted = True
                    self._storage_watch_events.append(event)
            except KeyboardInterrupt:
                event = {"elapsed_seconds": round(time.monotonic() - started, 3),
                         "free_bytes": disk_free_bytes(self.prefix.parent),
                         "sampled_at_unix": time.time(),
                         "action": "INTERRUPT_TERMINATE_PROCESS"}
                self._storage_watch_events.append(event)
                if not self._terminate_and_reap(process):
                    self._unsafe_live_process = True
                    event["process_still_running"] = True
                elif process in self._managed_processes:
                    self._managed_processes.remove(process)
                raise

    def _sample_terminal_free_space(self, started: float, completed_action: str) -> int:
        if self._wine_started and self._materialized:
            self._assert_prefix_real()
        free = disk_free_bytes(self.prefix.parent)
        event = {"elapsed_seconds": round(time.monotonic() - started, 3),
                 "free_bytes": free, "sampled_at_unix": time.time(), "action": completed_action}
        if free < FREE_HARD_STOP:
            event["action"] = "HARD_STOP_AFTER_PROCESS_COMPLETION"
            self._storage_violation = True
            self._storage_watch_events.append(event)
            raise RuntimeError(f"free space crossed the 25 GiB hard stop at process completion: {free}")
        if free < FREE_WARNING and not self._free_warning_emitted:
            event["action"] = "WARNING_NO_NEW_EXPERIMENTS"
            self._free_warning_emitted = True
        elif free < FREE_MINIMUM:
            event["action"] = "BELOW_NEXT_RUN_MINIMUM_AT_PROCESS_COMPLETION"
        self._storage_watch_events.append(event)
        return free

    def start_wine_process(self, args: list[str] | tuple[str, ...], *,
                           env: Mapping[str, str] | None = None,
                           runner_path: Path | str | None = None,
                           **popen_options: Any) -> subprocess.Popen[Any]:
        """Launch only the certified Wine loader through a fixed optional arch shim."""
        if not self._materialized or self._wine_loader_path is None:
            raise RuntimeError("Wine can start only after the certified prefix is materialized and bound")
        if self._prefix_parent_fd is None:
            raise RuntimeError("Wine cannot start without the verified prefix parent directory handle")
        child_env = dict(self._env if env is None else env)
        runtime_tree_to_verify: tuple[Path, str] | None = None
        graphics_contract: dict[str, Any] | None = None
        _validate_wine_child_environment(child_env)
        if self._runtime_env_verified and self._sealed_wine_env is not None:
            runtime_identity = (self._template_marker or {}).get("runtime_identity", {})
            try:
                graphics_contract = _wine_graphics_identity_contract(self._manifest, runtime_identity)
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise ValueError(
                    f"Wine launcher or host identity changed after PrefixLease sealing: {error}") from error
            if graphics_contract != self._wine_graphics_contract:
                raise ValueError("Wine launcher or host identity changed after PrefixLease sealing")
            if graphics_contract is not None:
                expected_tree_hash = runtime_identity.get("wine_runtime_tree_sha256")
                manifest_value = runtime_identity.get("wine_runtime_tree_manifest")
                pinned_manifest = WINE_RUNTIME_TREE_MANIFEST
                if (not isinstance(expected_tree_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_tree_hash)
                        or not isinstance(manifest_value, str)
                        or manifest_value != str(pinned_manifest)
                        or pinned_manifest.is_symlink()
                        or not pinned_manifest.is_file()):
                    raise ValueError("Wine graphics launch prerequisites are missing or malformed")
                runtime_tree_to_verify = (graphics_contract["runtime_root"], expected_tree_hash)
            path_keys = {"DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"}
            sealed_env = self._sealed_wine_env
            if child_env != sealed_env:
                if ({key: value for key, value in child_env.items() if key not in path_keys}
                        != {key: value for key, value in sealed_env.items() if key not in path_keys}):
                    raise ValueError("Wine child environment differs from the verified PrefixLease environment")
            # Rehash the selected loader directory on every launch. The output tree
            # remains writable after set_env, so a matching sealed string does not
            # prove that the file selected by DYLD is still the certified candidate.
            if path_keys.intersection(child_env) or path_keys.intersection(sealed_env):
                expected_mvk = runtime_identity.get("moltenvk_sha256_for_subsequent_graphics_runs")
                runtime_root_value = runtime_identity.get("wine_runtime_root")
                if not isinstance(expected_mvk, str) or not isinstance(runtime_root_value, str):
                    raise ValueError("Wine child launch prerequisites are missing or malformed")
                runtime_root = Path(runtime_root_value)
                if (not runtime_root.is_absolute()
                        or str(runtime_root) != runtime_root_value
                        or os.path.abspath(runtime_root_value) != runtime_root_value):
                    raise ValueError("Wine child launch prerequisites are missing or malformed")
                expected_tree_hash = runtime_identity.get("wine_runtime_tree_sha256")
                manifest_value = runtime_identity.get("wine_runtime_tree_manifest")
                pinned_manifest = WINE_RUNTIME_TREE_MANIFEST
                if (not isinstance(expected_tree_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", expected_tree_hash)
                        or not isinstance(manifest_value, str)
                        or manifest_value != str(pinned_manifest)
                        or pinned_manifest.is_symlink()
                        or not pinned_manifest.is_file()):
                    raise ValueError("Wine child launch prerequisites are missing or malformed")
                runtime_tree_to_verify = (runtime_root, expected_tree_hash)
                child_env = _normalize_wine_dyld_environment(
                    child_env,
                    expected_moltenvk_sha256=expected_mvk,
                    trusted_roots=(self.output_dir, runtime_root / "lib",
                                   Path("/usr/local/lib"), Path("/usr/lib")),
                    candidate_fallback_roots=(runtime_root / "lib",),
                )
        if (not self._runtime_env_verified or self._sealed_wine_env is None
                or child_env != self._sealed_wine_env):
            raise ValueError("Wine child environment differs from the verified PrefixLease environment")
        command = [str(item) for item in args]
        wine_loader = str(self._wine_loader_path)
        direct_wine = bool(command and command[0] == wine_loader)
        arch_wine = (len(command) >= 3 and command[:3] == ["/usr/bin/arch", "-x86_64", wine_loader])
        if not (direct_wine or arch_wine):
            raise ValueError(
                "Wine child must execute the certified Wine binary directly or through "
                "the fixed /usr/bin/arch -x86_64 launcher"
            )
        if graphics_contract is not None and not direct_wine:
            raise ValueError("graphics Wine runs must execute the certified thin x86_64 launcher directly")
        if any("\x00" in item for item in command):
            raise ValueError("Wine child arguments cannot contain NUL bytes")
        wineprefix_value = self._assert_exact_wineprefix(child_env)
        self._assert_prefix_real()
        passed_env = popen_options.pop("env", None)
        if passed_env is not None and dict(passed_env) != child_env:
            raise ValueError("Wine child environment must be supplied through the PrefixLease env argument")
        allowed_popen_options = {
            "stdin", "stdout", "stderr", "text", "encoding", "errors", "bufsize",
            "universal_newlines",
        }
        unsupported_options = sorted(set(popen_options) - allowed_popen_options)
        if unsupported_options:
            raise ValueError(
                "Wine child received unsupported process-control options: "
                + ", ".join(unsupported_options)
            )
        if self._wine_loader_sha256 is None or self._wine_loader_identity is None:
            raise RuntimeError("Wine cannot start without the sealed executable identity")
        try:
            current_loader_sha256, current_loader_identity = _wine_loader_identity(self._wine_loader_path)
        except ValueError as error:
            raise ValueError("certified Wine executable changed or became unsafe after PrefixLease sealing") from error
        if (current_loader_sha256 != self._wine_loader_sha256
                or current_loader_identity != self._wine_loader_identity):
            raise ValueError("certified Wine executable changed after PrefixLease sealing")
        if runtime_tree_to_verify is not None:
            from initialize_prefix_template import actual_runtime_tree_hash
            runtime_root, expected_tree_hash = runtime_tree_to_verify
            try:
                current_tree_hash, _entry_count, _missing = actual_runtime_tree_hash(runtime_root)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError("certified Wine runtime changed after PrefixLease sealing") from error
            if current_tree_hash != expected_tree_hash:
                raise ValueError("certified Wine runtime changed after PrefixLease sealing")
        expected_file_identities: tuple[
            tuple[str, str, tuple[int, int, int, int, int], str, bool], ...] | None = None
        if graphics_contract is not None:
            try:
                final_contract = _wine_graphics_identity_contract(
                    self._manifest, (self._template_marker or {}).get("runtime_identity", {}))
            except (OSError, ValueError, TypeError, KeyError) as error:
                raise ValueError("certified Wine launcher or host image changed before exec") from error
            if final_contract != self._wine_graphics_contract:
                raise ValueError("certified Wine launcher or host image changed before exec")
            expected_file_identities = (
                (str(final_contract["launcher_path"]), final_contract["launcher_sha256"],
                 final_contract["launcher_identity"], "x86_64", True),
                (str(final_contract["host_image_path"]), final_contract["host_image_sha256"],
                 final_contract["host_image_identity"], "x86_64", False),
            )
        if self._prefix_identity is None:
            raise RuntimeError("Wine cannot start without the bound leased prefix inode")
        allocation_lock = _acquire_storage_allocation_lock()
        try:
            _assert_prefix_lease_locked(
                self.prefix, str(self._wine_loader_path), runner_path, child_env)
            self._assert_bound_prefix_lease(child_env)
            parent_info = os.fstat(self._prefix_parent_fd)
            process = _spawn_in_directory_fd(
                self._prefix_parent_fd, command,
                expected_directory_identity=(parent_info.st_dev, parent_info.st_ino),
                expected_child_identity=(self.prefix.name, *self._prefix_identity, str(self.prefix)),
                expected_file_identities=expected_file_identities,
                bind_trace_target_pid="FG_B_TRACE_DIRECTORY" in child_env,
                env=child_env, **popen_options)
        finally:
            _release_storage_allocation_lock(allocation_lock)
        self._wine_started = True
        self._wine_launch_prefix = wineprefix_value
        if process not in self._managed_processes:
            self._managed_processes.append(process)
        return process

    @staticmethod
    def _terminate_and_reap(process: subprocess.Popen[Any], *, grace_seconds: float = 5.0) -> bool:
        if process.poll() is not None:
            return True
        try:
            process.terminate()
        except OSError:
            if process.poll() is not None:
                return True
        try:
            process.wait(timeout=grace_seconds)
            return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
        except OSError:
            if process.poll() is not None:
                return True
        try:
            process.wait(timeout=grace_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None
        return True

    def materialize(self) -> None:
        if self._materialized:
            return
        allocation_lock_file = _acquire_storage_allocation_lock()
        try:
            self._materialize_with_allocation_lock()
        finally:
            _release_storage_allocation_lock(allocation_lock_file)

    def _materialize_with_allocation_lock(self) -> None:
        if self._materialized:
            return
        storage_smoke = (self._manifest.get("storage_policy_smoke") is True
                         and _is_storage_policy_smoke_id(self.experiment_id))
        blockers = _gate0_blockers(active_experiment_id=self.experiment_id,
                                   allow_storage_smoke=storage_smoke,
                                   require_active_marker=True)
        if blockers:
            raise RuntimeError("Gate 0 blocks prefix materialization: " + "; ".join(blockers))
        if not self._runtime_env_verified:
            self.set_env(self._env)
        if not self.template.is_dir() or self.template.is_symlink():
            raise FileNotFoundError(f"certified Wine template is unavailable: {self.template}")
        if not (self.template / "FGMETAL_PREFIX_TEMPLATE.json").is_file():
            raise ValueError("Wine template lacks its certification marker")
        if self._lease_marker_path is None:
            raise RuntimeError("PrefixLease active marker path was not reserved before materialization")
        current_free = disk_free_bytes(self.prefix.parent)
        persistent_snapshot, persistent_exclusions, persistent_blockers = (
            _signed_persistent_output_snapshot(exclude_experiment_id=self.experiment_id))
        if persistent_blockers:
            raise RuntimeError("signed experiment output accounting is blocked before PrefixLease materialization: "
                               + "; ".join(persistent_blockers))
        accounting_exclusions = [*persistent_exclusions, self.prefix, self._lease_marker_path,
                                 self.output_dir, self.manifest_path]
        if self._manifest.get("storage_policy_smoke") is True:
            accounting_exclusions.append(_storage_smoke_claim_path(self.experiment_id))
        current_usage = measure_project_usage(exclude_paths=accounting_exclusions)
        current_project = conservative_project_usage_bytes(current_usage)
        pre_materialization_nonlease_scope = current_usage.get(
            "allocated_inode_deduplicated_bytes_excluding_paths")
        if (not isinstance(pre_materialization_nonlease_scope, int)
                or isinstance(pre_materialization_nonlease_scope, bool)
                or pre_materialization_nonlease_scope < 0):
            raise RuntimeError("PrefixLease could not measure the pre-materialization non-lease scope")
        current_attestation_bytes = _read_file_nofollow(GATE0_ATTESTATION_PATH)
        current_attestation = json.loads(current_attestation_bytes)
        if (not _verify_gate0_signature(current_attestation)
                or hashlib.sha256(current_attestation_bytes).hexdigest()
                != self._manifest.get("gate0_attestation_sha256")):
            raise RuntimeError("Gate 0 attestation changed before PrefixLease materialization")
        gate0_nonlease_scope = current_attestation.get("measurements", {}).get(
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes")
        if (not isinstance(gate0_nonlease_scope, int) or isinstance(gate0_nonlease_scope, bool)
                or gate0_nonlease_scope < 0
                or pre_materialization_nonlease_scope != gate0_nonlease_scope):
            raise RuntimeError("unrelated non-lease storage changed between Gate 0 and PrefixLease materialization")
        pre_materialization_output, _output_tree_sha256 = _persistent_output_snapshot(self.output_dir)
        remaining_persistent = int(self._manifest.get("estimated_persistent_bytes", 0))
        remaining_build = int(self._manifest.get("expected_build_bytes", 0))
        estimated_peak = int(self._manifest_estimates["estimated_projected_new_disk_bytes"])
        admission = projected_storage_admission(
            current_free_bytes=current_free,
            current_scoped_bytes=current_project,
            peak_additional_bytes=estimated_peak,
        )
        if not admission["admitted"]:
            raise RuntimeError("PrefixLease projected-peak admission failed before prefix clone: "
                               + "; ".join(admission["blockers"]))
        prefix_parent_fd = _open_directory_nofollow(
            self.prefix.parent, create=True, strict_directory_sync=True)
        self._prefix_parent_fd = prefix_parent_fd
        try:
            if _stat_child_nofollow(prefix_parent_fd, self.prefix.name) is not None:
                raise FileExistsError(f"prefix path appeared before clone: {self.prefix}")
            self._clone_temp_name = f".{self.prefix.name}.clone-{uuid.uuid4().hex}"
            if _stat_child_nofollow(prefix_parent_fd, self._clone_temp_name) is not None:
                self._clone_temp_name = None
                raise FileExistsError("randomized prefix clone path unexpectedly exists")
            os.mkdir(self._clone_temp_name, 0o700, dir_fd=prefix_parent_fd)
            self._leased_tree_absence_proven = False
            os.fsync(prefix_parent_fd)
            created_info = _stat_child_nofollow(prefix_parent_fd, self._clone_temp_name)
            parent_info = os.fstat(prefix_parent_fd)
            if (created_info is None or not stat.S_ISDIR(created_info.st_mode)
                    or created_info.st_dev != parent_info.st_dev):
                raise OSError("new prefix clone directory changed before it could be bound")
            self._clone_temp_identity = (created_info.st_dev, created_info.st_ino)
            self._clone_temp_fd = os.open(self._clone_temp_name, os.O_RDONLY
                                          | getattr(os, "O_DIRECTORY", 0)
                                          | getattr(os, "O_NOFOLLOW", 0), dir_fd=prefix_parent_fd)
            temp_info = os.fstat(self._clone_temp_fd)
            temp_fsid = getattr(os.fstatvfs(self._clone_temp_fd), "f_fsid", None)
            parent_fsid = getattr(os.fstatvfs(prefix_parent_fd), "f_fsid", None)
            if (not stat.S_ISDIR(temp_info.st_mode) or temp_info.st_dev != parent_info.st_dev
                    or (temp_info.st_dev, temp_info.st_ino) != self._clone_temp_identity
                    or temp_fsid is None or temp_fsid != parent_fsid):
                raise OSError("prefix clone directory is not on the verified prefix filesystem")
        except BaseException:
            self._storage_violation = True
            if self._clone_temp_fd is not None:
                os.close(self._clone_temp_fd)
                self._clone_temp_fd = None
            if self._clone_temp_name is not None and self._clone_temp_identity is not None:
                try:
                    _remove_child_directory_nofollow(
                        prefix_parent_fd, self._clone_temp_name,
                        expected_identity=self._clone_temp_identity)
                    self._leased_tree_absence_proven = True
                    self._clone_temp_name = None
                    self._clone_temp_identity = None
                except OSError:
                    # Keep the path visible to inventory; the caller receives
                    # the setup error and no smoke may be attested.
                    pass
            os.close(prefix_parent_fd)
            self._prefix_parent_fd = None
            raise
        self._write_active_lease("MATERIALIZING")
        clone: subprocess.Popen[str] | None = None
        try:
            clone = _spawn_in_directory_fd(
                self._clone_temp_fd,
                ["/bin/cp", "-cR", str(self.template) + "/.", "."],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self._clone_process = clone
            clone_rc = self.wait_process(clone, timeout_seconds=300)
            clone_stdout, clone_stderr = clone.communicate()
            if clone_rc:
                raise subprocess.CalledProcessError(clone_rc, clone.args, clone_stdout, clone_stderr)
        except BaseException:
            reaped = clone is None or self._terminate_and_reap(clone)
            if not reaped:
                self._unsafe_live_process = True
                self._storage_watch_events.append({
                    "sampled_at_unix": time.time(),
                    "action": "CLONE_PROCESS_STILL_RUNNING",
                    "prefix_path": str(self.prefix),
                })
                raise RuntimeError("prefix clone process remains alive; leaving its target and lease record untouched")
            if clone is not None and clone in self._managed_processes:
                self._managed_processes.remove(clone)
            self._clone_process = None
            partial = (_stat_child_nofollow(prefix_parent_fd, self._clone_temp_name)
                       if self._clone_temp_name is not None else None)
            if partial is not None and stat.S_ISDIR(partial.st_mode):
                self._prefix_created_any = True
                try:
                    _remove_child_directory_nofollow(
                        prefix_parent_fd, self._clone_temp_name,
                        expected_identity=self._clone_temp_identity)
                    if self._prefix_identity is None:
                        self._leased_tree_absence_proven = True
                    self._clone_temp_name = None
                    self._clone_temp_identity = None
                    if self._clone_temp_fd is not None:
                        os.close(self._clone_temp_fd)
                        self._clone_temp_fd = None
                except OSError as error:
                    self._storage_violation = True
                    self._storage_watch_events.append({
                        "sampled_at_unix": time.time(),
                        "action": "PARTIAL_CLONE_CLEANUP_FAILED",
                        "prefix_path": str(self.prefix),
                        "error": repr(error),
                    })
                    raise RuntimeError(f"failed to remove partial prefix clone: {error!r}") from error
            elif partial is not None:
                self._storage_violation = True
                raise RuntimeError(f"copy left an unexpected prefix path: {self.prefix}")
            else:
                # The held inode may have been renamed outside the scanned cache.
                # Absence of its old name is not proof that the allocated tree is gone.
                self._storage_violation = True
                self._storage_watch_events.append({
                    "sampled_at_unix": time.time(),
                    "action": "CLONE_TARGET_IDENTITY_LOST",
                    "prefix_path": str(self.prefix),
                })
                raise RuntimeError("bound prefix clone directory disappeared before controlled cleanup")
            raise
        self._clone_process = None
        try:
            self._assert_clone_target_real()
            temporary_prefix = self.prefix.parent / str(self._clone_temp_name)
            clone_identity = _tree_fingerprint(temporary_prefix,
                                               exclude={"FGMETAL_PREFIX_TEMPLATE.json"})
            if clone_identity != self._template_marker.get("tree_identity", {}):
                raise RuntimeError("materialized prefix does not match the certified template fingerprint")
            self._assert_clone_target_real()
            _rename_child_exclusive(prefix_parent_fd, str(self._clone_temp_name), self.prefix.name)
            self._prefix_identity = self._clone_temp_identity
            published = _stat_child_nofollow(prefix_parent_fd, self.prefix.name)
            if (published is None or not stat.S_ISDIR(published.st_mode)
                    or self._prefix_identity is None
                    or (published.st_dev, published.st_ino) != self._prefix_identity):
                raise OSError("published prefix name does not match the inode bound before clone")
            self._assert_prefix_real()
            self._clone_temp_name = None
            self._clone_temp_identity = None
            if self._clone_temp_fd is not None:
                os.close(self._clone_temp_fd)
                self._clone_temp_fd = None
            self._created = True
            self._prefix_created_any = True
        except BaseException:
            self._storage_violation = True
            if self._clone_temp_name is not None:
                try:
                    _remove_child_directory_nofollow(
                        prefix_parent_fd, self._clone_temp_name,
                        expected_identity=self._clone_temp_identity)
                    self._clone_temp_name = None
                    self._clone_temp_identity = None
                except OSError:
                    pass
            raise
        cloned_prefix_info = _stat_child_nofollow(prefix_parent_fd, self.prefix.name)
        if cloned_prefix_info is None or not stat.S_ISDIR(cloned_prefix_info.st_mode):
            self._storage_violation = True
            raise RuntimeError("prefix clone did not produce a real directory")
        clone_path_info = _stat_entry_nofollow(self.prefix)
        if (clone_path_info is None
                or (clone_path_info.st_dev, clone_path_info.st_ino)
                != (cloned_prefix_info.st_dev, cloned_prefix_info.st_ino)):
            self._storage_violation = True
            raise RuntimeError("prefix clone path no longer resolves to its verified directory")
        self._created = self.prefix.is_dir() and not self.prefix.is_symlink()
        self._clone_complete = self._created
        self._materialized = True
        # The template marker must never make a disposable clone look preserved.
        clone_marker = self.prefix / "FGMETAL_PREFIX_TEMPLATE.json"
        if _stat_entry_nofollow(clone_marker) is not None:
            _unlink_file_nofollow(clone_marker)
        lease_payload = {
            "experiment_id": self.experiment_id,
            "prefix_path": str(self.prefix),
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self._manifest_sha256,
            "lease_token": self._lease_token,
            "created_at_unix": time.time(),
            "pid": os.getpid(),
        }
        _write_json(self.prefix / "FGMETAL_PREFIX_LEASE.json", lease_payload,
                    strict_directory_sync=True)
        self._write_active_lease("MATERIALIZED")
        after_clone = disk_free_bytes(self.prefix.parent)
        rebound_persistent_snapshot, rebound_persistent_exclusions, rebound_persistent_blockers = (
            _signed_persistent_output_snapshot(exclude_experiment_id=self.experiment_id))
        if (rebound_persistent_blockers
                or rebound_persistent_snapshot != persistent_snapshot
                or rebound_persistent_exclusions != persistent_exclusions):
            self._storage_violation = True
            raise RuntimeError("signed experiment output paths changed during PrefixLease materialization"
                               + (": " + "; ".join(rebound_persistent_blockers)
                                  if rebound_persistent_blockers else ""))
        project_after_clone = measure_project_usage(exclude_paths=accounting_exclusions)
        project_after_clone_bytes = conservative_project_usage_bytes(project_after_clone)
        if after_clone < FREE_HARD_STOP:
            self._storage_violation = True
            self._storage_watch_events.append({"sampled_at_unix": time.time(),
                "action": "HARD_STOP_AFTER_PREFIX_CLONE", "free_bytes": after_clone})
            raise RuntimeError(f"prefix materialization crossed the 25 GiB hard stop: {after_clone}")
        remaining_admission = _post_materialization_prefix_admission(
            current_free_bytes=after_clone,
            current_scoped_bytes=project_after_clone_bytes,
            manifest_estimates=self._manifest_estimates,
        )
        for warning in remaining_admission["warnings"]:
            self._storage_watch_events.append({
                "sampled_at_unix": time.time(),
                "action": "PROJECTED_STORAGE_WARNING_AFTER_PREFIX_CLONE",
                "warning": warning,
            })
        if not remaining_admission["admitted"]:
            self._storage_violation = True
            self._storage_watch_events.append({"sampled_at_unix": time.time(),
                "action": "PROJECTED_PEAK_AFTER_PREFIX_CLONE",
                "free_bytes": after_clone,
                "project_usage_bytes": project_after_clone_bytes,
                "remaining_prefix_bytes": self._manifest_estimates["expected_prefix_bytes"],
                "remaining_build_bytes": remaining_build,
                "remaining_persistent_bytes": remaining_persistent,
                "uncertainty_margin_bytes": self._manifest_estimates["estimated_uncertainty_margin_bytes"],
                "remaining_peak_additional_bytes": remaining_admission["peak_additional_bytes"],
                "projected_free_bytes": remaining_admission["projected_free_bytes"],
                "projected_scoped_bytes": remaining_admission["projected_scoped_bytes"],
                "blockers": remaining_admission["blockers"],
            })
            raise RuntimeError("prefix materialization leaves an inadmissible projected peak: "
                               + "; ".join(remaining_admission["blockers"]))
        post_materialization_nonlease_scope = project_after_clone.get(
            "allocated_inode_deduplicated_bytes_excluding_paths")
        if (not isinstance(post_materialization_nonlease_scope, int)
                or isinstance(post_materialization_nonlease_scope, bool)
                or post_materialization_nonlease_scope < 0):
            raise RuntimeError("PrefixLease could not measure the post-materialization non-lease scope")
        if post_materialization_nonlease_scope != pre_materialization_nonlease_scope:
            self._storage_violation = True
            raise RuntimeError("unrelated non-lease storage changed during PrefixLease materialization")
        materialized_prefix_bytes = _prefix_size(self.prefix)["allocated_inode_deduplicated_bytes"]
        post_materialization_output, _output_tree_sha256 = _persistent_output_snapshot(self.output_dir)
        if materialized_prefix_bytes > self._manifest_estimates["expected_prefix_bytes"]:
            self._storage_violation = True
            raise RuntimeError("materialized PrefixLease allocation exceeds its signed prefix reservation")
        if (post_materialization_output < pre_materialization_output
                or post_materialization_output > self._manifest_estimates["estimated_persistent_bytes"]):
            self._storage_violation = True
            raise RuntimeError("current-run output exceeds its signed persistent-output reservation")
        prefix_info = _stat_entry_nofollow(self.prefix)
        if (prefix_info is None or not stat.S_ISDIR(prefix_info.st_mode)
                or self._lease_marker_parent_identity is None):
            self._storage_violation = True
            raise RuntimeError("materialized PrefixLease identity disappeared before its accounting receipt")
        inventory_bytes = _read_file_nofollow(INVENTORY_PATH)
        inventory = json.loads(inventory_bytes)
        if _read_file_nofollow(GATE0_ATTESTATION_PATH) != current_attestation_bytes:
            self._storage_violation = True
            raise RuntimeError("Gate 0 attestation changed during PrefixLease materialization")
        current_measurements = current_attestation.get("measurements", {})
        gate0_observed_scope = current_measurements.get(
            "fgmetal_scoped_observed_allocated_inode_bytes")
        if (not isinstance(gate0_observed_scope, int) or isinstance(gate0_observed_scope, bool)
                or gate0_observed_scope < 0):
            self._storage_violation = True
            raise RuntimeError("Gate 0 lacks an exact observed-allocation baseline for PrefixLease receipt")
        if current_measurements.get(
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes") != gate0_nonlease_scope:
            self._storage_violation = True
            raise RuntimeError("Gate 0 non-lease allocation baseline changed before PrefixLease receipt")
        receipt = {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "pid": os.getpid(),
            "lease_token": self._lease_token,
            "prefix_path": str(self.prefix),
            "prefix_device": prefix_info.st_dev,
            "prefix_inode": prefix_info.st_ino,
            "active_marker_path": str(self._lease_marker_path),
            "active_marker_parent_device": self._lease_marker_parent_identity[0],
            "active_marker_parent_inode": self._lease_marker_parent_identity[1],
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self._manifest_sha256,
            "gate0_attestation_sha256": hashlib.sha256(current_attestation_bytes).hexdigest(),
            "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "inventory_generated_at_unix": inventory.get("generated_at_unix"),
            "gate0_attested_observed_scope_bytes": gate0_observed_scope,
            "gate0_attested_nonlease_scope_bytes": gate0_nonlease_scope,
            "pre_materialization_free_bytes": current_free,
            "post_materialization_free_bytes": after_clone,
            "pre_materialization_nonlease_scope_bytes": pre_materialization_nonlease_scope,
            "post_materialization_nonlease_scope_bytes": post_materialization_nonlease_scope,
            "pre_materialization_output_allocated_bytes": pre_materialization_output,
            "post_materialization_output_allocated_bytes": post_materialization_output,
            "materialized_prefix_allocated_bytes": materialized_prefix_bytes,
            "expected_prefix_bytes": self._manifest_estimates["expected_prefix_bytes"],
            "estimated_persistent_bytes": self._manifest_estimates["estimated_persistent_bytes"],
            "estimated_projected_new_disk_bytes": self._manifest_estimates["estimated_projected_new_disk_bytes"],
            "post_materialization_scope_bytes": project_after_clone_bytes,
            "created_at_unix": time.time(),
        }
        self._materialization_receipt = _seal_storage_evidence(
            receipt, "prefix_materialization_hmac_sha256")
        self._write_active_lease("MATERIALIZED")

    def _stop_wineserver(self) -> tuple[bool, list[dict[str, Any]]]:
        if not self._created:
            return True, []
        try:
            self._assert_prefix_real()
        except OSError as error:
            return False, [{"error": repr(error), "status": "PREFIX_PATH_UNSAFE"}]
        if not self._wine_started:
            return True, [{"status": "SKIPPED_NO_WINE_PROCESS_STARTED"}]
        if self._prefix_parent_fd is None:
            return False, [{"status": "PREFIX_PARENT_HANDLE_MISSING"}]
        if (not self._runtime_env_verified or self._sealed_wine_env is None
                or self._env != self._sealed_wine_env):
            return False, [{"status": "SEALED_WINE_ENVIRONMENT_CHANGED"}]
        env = dict(self._sealed_wine_env)
        try:
            self._assert_exact_wineprefix(env)
            if env.get("WINESERVER") != str(self.wine_server):
                raise ValueError("sealed WINESERVER does not match the PrefixLease runtime")
            self._assert_bound_prefix_lease(env)
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
            self._storage_violation = True
            return False, [{"status": "PREFIX_LEASE_BINDING_UNSAFE", "error": repr(error),
                            "WINEPREFIX": env.get("WINEPREFIX")}]
        parent_info = os.fstat(self._prefix_parent_fd)
        if self._prefix_identity is None:
            self._storage_violation = True
            return False, [{"status": "PREFIX_IDENTITY_MISSING",
                            "WINEPREFIX": env.get("WINEPREFIX")}]
        prefix_identity = {
            "path": str(self.prefix),
            "device": self._prefix_identity[0],
            "inode": self._prefix_identity[1],
            "parent_device": parent_info.st_dev,
            "parent_inode": parent_info.st_ino,
            "experiment_id": self.experiment_id,
            "manifest_sha256": self._manifest_sha256,
        }
        records: list[dict[str, Any]] = []
        good = True
        for flag in ("-k", "-w"):
            try:
                # Re-read all durable identities for each shutdown invocation.
                # The exec wrapper then checks the parent and prefix inode again
                # immediately before exec using the inherited directory handle.
                self._assert_bound_prefix_lease(env)
                self._wineserver_prefix = self._assert_exact_wineprefix(env)
                process = _spawn_in_directory_fd(
                    self._prefix_parent_fd,
                    ["/usr/bin/arch", "-x86_64", str(self.wine_server), flag],
                    expected_directory_identity=(parent_info.st_dev, parent_info.st_ino),
                    expected_child_identity=(self.prefix.name, *self._prefix_identity, str(self.prefix)),
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                try:
                    output, _ = process.communicate(timeout=30)
                except subprocess.TimeoutExpired as timeout_error:
                    self._terminate_and_reap(process)
                    records.append({"flag": flag, "error": repr(timeout_error),
                                    "WINEPREFIX": self._wineserver_prefix,
                                    "prefix_identity": prefix_identity,
                                    "output_tail": (process.stdout.read() if process.stdout else "")[-1000:]})
                    good = False
                    break
                records.append({"flag": flag, "returncode": process.returncode,
                                "WINEPREFIX": self._wineserver_prefix,
                                "prefix_identity": prefix_identity,
                                "output_tail": (output or "")[-1000:]})
                good = good and process.returncode == 0
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                records.append({"flag": flag, "error": repr(exc),
                                "WINEPREFIX": self._wineserver_prefix or env.get("WINEPREFIX"),
                                "prefix_identity": prefix_identity})
                good = False
                break
        return good, records

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._cleanup_started = True
        prefix_present = False
        prefix_created = False
        cleanup_error: str | None = None
        size_error: str | None = None
        state_error: str | None = None
        receipt_error: str | None = None
        stop_ok = True
        stop_records: list[dict[str, Any]] = []
        state = None
        deleted = False
        preserved = False
        reason = ""

        for process in list(self._managed_processes):
            if process.poll() is None and not self._terminate_and_reap(process):
                self._unsafe_live_process = True
                self._storage_watch_events.append({"sampled_at_unix": time.time(),
                    "action": "MANAGED_PROCESS_STILL_RUNNING", "pid": process.pid})
            elif process in self._managed_processes:
                self._managed_processes.remove(process)

        if self._clone_temp_name is not None and self._prefix_parent_fd is not None and self._unsafe_live_process:
            cleanup_error = "prefix clone directory retained because a managed process may still be live"
            self._storage_violation = True
        elif self._clone_temp_name is not None and self._prefix_parent_fd is not None:
            try:
                temp_info = _stat_child_nofollow(self._prefix_parent_fd, self._clone_temp_name)
                if temp_info is None:
                    raise OSError("bound clone directory name disappeared without controlled publication or deletion")
                if self._clone_temp_identity is None:
                    raise OSError("refusing to remove a clone directory without its bound identity")
                if not stat.S_ISDIR(temp_info.st_mode):
                    raise OSError("bound clone directory name was replaced by a non-directory or symlink")
                self._prefix_created_any = True
                _remove_child_directory_nofollow(
                    self._prefix_parent_fd, self._clone_temp_name,
                    expected_identity=self._clone_temp_identity)
                if self._prefix_identity is None:
                    self._leased_tree_absence_proven = True
                self._clone_temp_name = None
                self._clone_temp_identity = None
                if self._clone_temp_fd is not None:
                    os.close(self._clone_temp_fd)
                    self._clone_temp_fd = None
            except Exception as error:
                cleanup_error = f"prefix clone temporary cleanup failed: {error!r}"
                self._storage_violation = True

        prefix_is_real_dir = False
        try:
            if self._created:
                self._assert_prefix_real()
            prefix_info = _stat_entry_nofollow(self.prefix)
            if prefix_info is not None:
                if not stat.S_ISDIR(prefix_info.st_mode):
                    self._unsafe_prefix_observed = True
                    raise OSError("reserved prefix name contains an unbound non-directory entry")
                if self._prefix_parent_fd is None or self._prefix_identity is None:
                    self._unsafe_prefix_observed = True
                    raise OSError("refusing to inspect or remove an unbound prefix directory")
                descriptor_info = _stat_child_nofollow(self._prefix_parent_fd, self.prefix.name)
                if (descriptor_info is None
                        or (descriptor_info.st_dev, descriptor_info.st_ino) != self._prefix_identity
                        or (prefix_info.st_dev, prefix_info.st_ino) != self._prefix_identity):
                    self._unsafe_prefix_observed = True
                    raise OSError("prefix path no longer names the directory inode bound to this lease")
                prefix_is_real_dir = True
        except OSError as error:
            prefix_info = None
            self._unsafe_prefix_observed = True
            cleanup_error = f"prefix path ancestry is unsafe during cleanup: {error!r}"
            self._storage_violation = True
        prefix_present = prefix_info is not None or self._created
        prefix_created = self._prefix_created_any or self._created or prefix_present
        try:
            size_before_cleanup = _prefix_size(self.prefix) if prefix_is_real_dir else {
                "logical_bytes": 0, "allocated_inode_deduplicated_bytes": 0, "file_count": 0}
            current_info = _stat_entry_nofollow(self.prefix)
            if (prefix_info is not None and current_info is not None
                    and (prefix_info.st_dev, prefix_info.st_ino) != (current_info.st_dev, current_info.st_ino)):
                raise OSError("prefix identity changed during size measurement")
        except Exception as error:
            size_before_cleanup = {"logical_bytes": 0, "allocated_inode_deduplicated_bytes": 0,
                                   "file_count": 0}
            size_error = repr(error)
            self._storage_violation = True

        if self._materialized and prefix_is_real_dir:
            try:
                if _stat_entry_nofollow(self.prefix) is None:
                    raise OSError("leased prefix disappeared before wineserver shutdown")
                stop_ok, stop_records = self._stop_wineserver()
            except OSError as error:
                stop_ok = False
                cleanup_error = cleanup_error or repr(error)
                self._storage_violation = True
        if prefix_is_real_dir and self._clone_complete:
            try:
                if _stat_entry_nofollow(self.prefix) is None:
                    raise OSError("leased prefix disappeared before state snapshot")
                state = _write_prefix_state(self.prefix, self.template, self.output_dir,
                                            self._template_marker)
                _write_json(self.output_dir / "prefix-state.json", state,
                            strict_directory_sync=True)
            except Exception as error:
                state_error = repr(error)

        lease_marker = self.prefix / "FGMETAL_PREFIX_LEASE.json"
        try:
            lease_marker_info = (_stat_entry_nofollow(lease_marker)
                                 if prefix_is_real_dir else None)
        except OSError as error:
            lease_marker_info = None
            cleanup_error = cleanup_error or f"prefix marker ancestry is unsafe: {error!r}"
            self._storage_violation = True
        if lease_marker_info is not None:
            try:
                _unlink_file_nofollow(lease_marker, expected_json={
                    "experiment_id": self.experiment_id,
                    "prefix_path": str(self.prefix),
                    "manifest_sha256": self._manifest_sha256,
                })
            except OSError as error:
                cleanup_error = cleanup_error or repr(error)
                self._storage_violation = True

        if prefix_present:
            if self._unsafe_live_process:
                reason = "managed Wine or clone process remains alive; prefix retained for reconciliation"
            elif not stop_ok:
                reason = self.reason_on_failure
            elif prefix_is_real_dir:
                try:
                    if self._prefix_parent_fd is None or self._prefix_identity is None:
                        raise OSError("refusing to delete a prefix without its bound parent and inode")
                    self._assert_prefix_real()
                    _remove_child_directory_nofollow(
                        self._prefix_parent_fd, self.prefix.name,
                        expected_identity=self._prefix_identity)
                    deleted = (_stat_child_nofollow(self._prefix_parent_fd, self.prefix.name) is None
                               and _stat_entry_nofollow(self.prefix) is None)
                    if not deleted:
                        raise OSError("bound prefix directory remains after controlled removal")
                    self._leased_tree_absence_proven = True
                    reason = "Wine server stopped; disposable prefix removed in finally"
                except Exception as error:
                    cleanup_error = cleanup_error or repr(error)
                    reason = f"prefix deletion failed after Wine shutdown: {error!r}"
            else:
                reason = "prefix path is not a real directory; left untouched"

        if prefix_present and not deleted and prefix_is_real_dir and not self._unsafe_live_process:
            try:
                retained = preserved_prefixes()
                preserve_marker = self.prefix / "PRESERVE_PREFIX.json"
                is_already_marked = _stat_entry_nofollow(preserve_marker) is not None
                retained_after = len(retained) if is_already_marked else len(retained) + 1
                if retained_after > MAX_PRESERVED_PREFIXES:
                    reason += "; retention limit reached, prefix left for explicit cleanup"
                else:
                    marker = {
                        "PRESERVE_PREFIX": True,
                        "experiment_id": self.experiment_id,
                        "reason": reason,
                        "creation_time_unix": time.time(),
                        "estimated_size_bytes": _prefix_size(self.prefix)["logical_bytes"],
                        "wine_server_shutdown": stop_records,
                    }
                    _write_json(self.prefix / "PRESERVE_PREFIX.json", marker,
                                strict_directory_sync=True)
                    preserved = True
            except Exception as error:
                cleanup_error = cleanup_error or repr(error)
                reason += f"; preservation marker write failed: {error!r}"

        try:
            free_after = disk_free_bytes(self.prefix.parent)
        except OSError as error:
            cleanup_error = cleanup_error or f"prefix volume measurement failed: {error!r}"
            self._storage_violation = True
            free_after = disk_free_bytes(REPO)
        net_free_delta = free_after - self._free_before
        prefix_allocated_removed = (
            int(size_before_cleanup["allocated_inode_deduplicated_bytes"]) if deleted else 0)
        project_before_bytes = int(self._project_before.get("allocated_inode_deduplicated_bytes", 0))
        try:
            project_after = measure_project_usage()
            project_after_bytes = int(project_after["allocated_inode_deduplicated_bytes"])
        except Exception as error:
            cleanup_error = cleanup_error or f"post-run project usage measurement failed: {error!r}"
            self._storage_violation = True
            project_after = dict(self._project_before)
            project_after_bytes = int(project_after.get("allocated_inode_deduplicated_bytes", 0))
        persistent_growth = max(0, project_after_bytes - project_before_bytes)
        expected_persistent = int(self._manifest.get("estimated_persistent_bytes", 0))
        unexpected_persistent = max(0, persistent_growth - expected_persistent)
        prefix_parent_identity_record: dict[str, int] | None = None
        if self._prefix_parent_fd is not None:
            parent_identity_info = os.fstat(self._prefix_parent_fd)
            prefix_parent_identity_record = {
                "device": parent_identity_info.st_dev,
                "inode": parent_identity_info.st_ino,
            }
        prefix_inode_identity_record = (
            {"device": self._prefix_identity[0], "inode": self._prefix_identity[1]}
            if self._prefix_identity is not None else None)

        storage_status = "PASS"
        free_status = "PASS"
        if (unexpected_persistent > 2 * GIB
                and not self._manifest.get("persistent_growth_over_2g_approved", False)):
            storage_status = "FAIL_STORAGE_HYGIENE"
        elif unexpected_persistent > 500 * MIB:
            storage_status = "WARN_PERSISTENT_GROWTH"
        if free_after < FREE_HARD_STOP:
            free_status = "FAIL_HARD_STOP"
            storage_status = "FAIL_STORAGE_HYGIENE"
        elif free_after < FREE_MINIMUM:
            free_status = "FAIL_BELOW_NEXT_RUN_MINIMUM"
            storage_status = "FAIL_STORAGE_HYGIENE"
        if project_after_bytes > GATE0_PROJECT_BUDGET:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_GATE0_PROJECT_BUDGET"
        if prefix_created and not deleted:
            storage_status = "FAIL_STORAGE_HYGIENE"
            if free_status == "PASS":
                free_status = "FAIL_PREFIX_CLEANUP"
        if self._unsafe_live_process:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_PROCESS_REAP"
        if self._storage_violation or size_error or state_error or cleanup_error:
            storage_status = "FAIL_STORAGE_HYGIENE"
        actions = {event.get("action") for event in self._storage_watch_events}
        if actions.intersection({"HARD_STOP_TERMINATE", "HARD_STOP_AFTER_PREFIX_CLONE",
                                 "HARD_STOP_AFTER_PROCESS_COMPLETION"}):
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_HARD_STOP_DURING_RUN"
        elif "WARNING_THRESHOLD_AFTER_PREFIX_CLONE" in actions:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_POST_CLONE_WARNING_THRESHOLD"
        elif "PROJECT_BUDGET_AFTER_PREFIX_CLONE" in actions:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_POST_CLONE_PROJECT_BUDGET"
        elif "WARNING_NO_NEW_EXPERIMENTS" in actions and free_status == "PASS":
            free_status = "WARN_DROPPED_BELOW_WARNING_DURING_RUN"

        record = {
            "schema_version": 3,
            "experiment_id": self.experiment_id,
            "created_at_unix": time.time(),
            "prefix_path": str(self.prefix),
            "wineprefix": str(self.prefix),
            "wineprefix_identity": {
                "canonical_absolute_path": str(self.prefix),
                "experiment_id": self.experiment_id,
                "manifest_sha256": self._manifest_sha256,
                "prefix_inode": prefix_inode_identity_record,
                "parent_inode": prefix_parent_identity_record,
                "launch_WINEPREFIX": self._wine_launch_prefix,
                "cleanup_WINEPREFIX": self._wineserver_prefix,
            },
            "prefix_created": bool(prefix_created),
            "prefix_deleted": deleted,
            "prefix_preserved": preserved,
            "bytes_reclaimed": prefix_allocated_removed,
            "bytes_reclaimed_method": "prefix st_blocks estimate; APFS clone sharing is not observable here",
            "net_free_space_delta_bytes": net_free_delta,
            "prefix_allocated_bytes_removed_estimate": prefix_allocated_removed,
            "temporary_created": bool(prefix_created),
            "temporary_created_logical_bytes": size_before_cleanup["logical_bytes"],
            "temporary_created_allocated_estimate_bytes": size_before_cleanup[
                "allocated_inode_deduplicated_bytes"],
            "temporary_removed": deleted,
            "temporary_removed_logical_bytes": size_before_cleanup["logical_bytes"] if deleted else 0,
            "persistent_retained_allocated_estimate_bytes": persistent_growth,
            "expected_persistent_bytes": expected_persistent,
            "unexpected_persistent_bytes": unexpected_persistent,
            "storage_hygiene_status": storage_status,
            "free_space_status": free_status,
            "free_before_bytes": self._free_before,
            "free_after_bytes": free_after,
            "gate0_threshold_failures": _cleanup_gate0_threshold_failures({
                "free_after_bytes": free_after,
                "project_usage_after": project_after,
            }),
            "free_peak_or_estimate_bytes": max(
                0, self._free_before - int(self._manifest.get(
                    "estimated_projected_new_disk_bytes",
                    self._manifest.get("estimated_max_new_disk_bytes", 0)))),
            "project_usage_before": self._project_before,
            "project_usage_after": project_after,
            "wineserver_shutdown": stop_records,
            "storage_watch_events": self._storage_watch_events,
            "prefix_size_error": size_error,
            "registry_snapshot_error": state_error,
            "cleanup_error": cleanup_error,
            "receipt_write_error": None,
            "receipt_finalization_complete": False,
            "active_lease_marker_removed_after_receipt": False,
            "active_lease_marker_removal_order": "receipt_durable_before_marker_removal",
            "reason": reason,
            "exception": repr(exc) if exc is not None else None,
            "interrupted_by_signal": self._sigterm_number,
        }
        receipt_path = self.output_dir / "prefix-cleanup.json"
        try:
            prefix_missing_for_marker_removal = _stat_entry_nofollow(self.prefix) is None
        except OSError as error:
            prefix_missing_for_marker_removal = False
            self._unsafe_prefix_observed = True
            cleanup_error = cleanup_error or f"prefix ancestry is unsafe at finalization: {error!r}"
            self._storage_violation = True
        # A missing replaceable name does not prove that the inode bound to the
        # lease was removed rather than renamed outside the inventoried roots.
        clone_temp_missing_for_marker_removal = self._clone_temp_name is None
        may_remove_active_marker = (not self._unsafe_live_process and not self._unsafe_prefix_observed
                                    and self._leased_tree_absence_proven
                                    and prefix_missing_for_marker_removal
                                    and clone_temp_missing_for_marker_removal)
        marker_ready = self._lease_marker_path is None
        receipt_reserve_bytes: int | None = None

        def persist_cleanup_receipt() -> None:
            nonlocal receipt_reserve_bytes
            record["cleanup_receipt_hmac_sha256"] = _storage_evidence_signature(
                record, "cleanup_receipt_hmac_sha256")
            serialized = json.dumps(record, indent=2, sort_keys=True) + "\n"
            serialized_bytes = len(serialized.encode("utf-8"))
            if receipt_reserve_bytes is None:
                receipt_reserve_bytes = max(64 * 1024, serialized_bytes + 16 * 1024)
            _write_json(receipt_path, record, pad_to_bytes=receipt_reserve_bytes,
                        strict_directory_sync=True)

        cas_audit_checked = False

        def sample_cleanup_storage() -> None:
            nonlocal cas_audit_checked
            try:
                record["free_after_bytes"] = disk_free_bytes(self.prefix.parent)
            except OSError as error:
                record["free_after_bytes"] = disk_free_bytes(REPO)
                record["free_space_status"] = "FAIL_STORAGE_PATH"
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                record["cleanup_error"] = record.get("cleanup_error") or f"prefix volume measurement failed: {error!r}"
            record["project_usage_after"] = measure_project_usage()
            final_free = int(record["free_after_bytes"])
            final_project_bytes = int(record["project_usage_after"]["allocated_inode_deduplicated_bytes"])
            record["net_free_space_delta_bytes"] = final_free - self._free_before
            record["persistent_retained_allocated_estimate_bytes"] = max(
                0, final_project_bytes - project_before_bytes)
            record["unexpected_persistent_bytes"] = max(
                0, int(record["persistent_retained_allocated_estimate_bytes"]) - expected_persistent)
            record["gate0_threshold_failures"] = _cleanup_gate0_threshold_failures(record)
            if final_free < FREE_HARD_STOP:
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                record["free_space_status"] = "FAIL_HARD_STOP"
            elif final_free < FREE_MINIMUM:
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                record["free_space_status"] = "FAIL_BELOW_NEXT_RUN_MINIMUM"
            if final_project_bytes > GATE0_PROJECT_BUDGET:
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                record["free_space_status"] = "FAIL_GATE0_PROJECT_BUDGET"
            if (int(record["unexpected_persistent_bytes"]) > 2 * GIB
                    and not self._manifest.get("persistent_growth_over_2g_approved", False)):
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
            elif (int(record["unexpected_persistent_bytes"]) > 500 * MIB
                  and record["storage_hygiene_status"] != "FAIL_STORAGE_HYGIENE"):
                record["storage_hygiene_status"] = "WARN_PERSISTENT_GROWTH"
            if "WARNING_NO_NEW_EXPERIMENTS" in actions and record["free_space_status"] == "PASS":
                record["free_space_status"] = "WARN_DROPPED_BELOW_WARNING_DURING_RUN"
            if not cas_audit_checked:
                cas_audit_checked = True
                try:
                    retention_directory = Path(__file__).resolve().parent
                    if str(retention_directory) not in sys.path:
                        sys.path.insert(0, str(retention_directory))
                    import build_retention
                    gate_attestation = json.loads(_read_file_nofollow(GATE0_ATTESTATION_PATH))
                    live_audit = build_retention.canonical_artifact_store_audit()
                    record["canonical_artifact_store_audit"] = live_audit
                    if gate_attestation.get("canonical_artifact_store_audit") != live_audit:
                        record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                        record["free_space_status"] = "FAIL_CAS_AUDIT_CHANGED"
                        record["cleanup_error"] = record.get("cleanup_error") or (
                            "canonical artifact content or link-count inventory changed during the run")
                except Exception as error:
                    record["canonical_artifact_store_audit_error"] = repr(error)
                    record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                    record["free_space_status"] = "FAIL_CAS_AUDIT_UNAVAILABLE"
                    record["cleanup_error"] = record.get("cleanup_error") or (
                        f"canonical artifact post-run audit failed: {error!r}")
            record["status"] = _cleanup_result_status(record)

        try:
            if self._lease_marker_path is not None:
                if self._lease_marker_path.is_symlink():
                    raise OSError(f"active PrefixLease marker became a symlink: {self._lease_marker_path}")
                if not self._lease_marker_path.is_file():
                    self._write_active_lease("RECONCILIATION_REQUIRED")
                marker_ready = self._lease_marker_path.is_file()
                if not marker_ready:
                    raise OSError("active PrefixLease marker could not be durably restored before receipt finalization")
                if not may_remove_active_marker:
                    self._write_active_lease("RECONCILIATION_REQUIRED")
            record["status"] = _cleanup_result_status(record)
            persist_cleanup_receipt()
            receipt_written = True
            # Allocate the final receipt at a fixed size before taking the
            # authoritative post-cleanup free-space and project samples.
            sample_cleanup_storage()
            record["receipt_finalization_complete"] = True
            record["status"] = _cleanup_result_status(record)
            persist_cleanup_receipt()
            # The receipt has the same reserved size on every rewrite, so this
            # terminal sample includes its final on-disk allocation.
            sample_cleanup_storage()
            record["status"] = _cleanup_result_status(record)
            # A failed experiment can release the reservation once the signed
            # cleanup receipt is durable and every leased directory is absent.
            # The receipt remains FAIL; the marker tracks live resources, not run success.
            may_remove_active_marker = may_remove_active_marker and record["receipt_finalization_complete"]
            persist_cleanup_receipt()
            receipt_written = True
        except Exception as error:
            receipt_written = False
            may_remove_active_marker = False
            record["receipt_finalization_complete"] = False
            receipt_error = repr(error)
            record["receipt_write_error"] = receipt_error
            record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
            record["status"] = _cleanup_result_status(record)
            if self._lease_marker_path is not None:
                try:
                    self._write_active_lease("RECONCILIATION_REQUIRED")
                except Exception as marker_error:
                    record["cleanup_error"] = f"receipt finalization failed: {receipt_error}; reconciliation marker failed: {marker_error!r}"
            try:
                persist_cleanup_receipt()
                receipt_written = True
            except Exception as retry_error:
                receipt_error = repr(retry_error)
                record["receipt_write_error"] = receipt_error
                receipt_written = False

        threshold_failures = _cleanup_gate0_threshold_failures(record)
        if threshold_failures and self._lease_marker_path is not None:
            try:
                invalidation = _invalidate_gate0_after_cleanup_threshold_failure(
                    self.experiment_id, threshold_failures,
                    free_after_bytes=record.get("free_after_bytes"),
                    project_after_bytes=(record.get("project_usage_after", {}) or {}).get(
                        "allocated_inode_deduplicated_bytes"),
                )
                record["gate0_admission_invalidated"] = True
                record["gate0_admission_invalidation"] = invalidation
            except Exception as error:
                record["gate0_admission_invalidated"] = False
                record["gate0_admission_invalidation_error"] = repr(error)
                record["cleanup_error"] = record.get("cleanup_error") or (
                    f"Gate 0 could not be durably invalidated after cleanup threshold failure: {error!r}")
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
            record["status"] = _cleanup_result_status(record)
            try:
                persist_cleanup_receipt()
                receipt_written = True
            except Exception as error:
                record["receipt_write_error"] = repr(error)
                record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                record["status"] = _cleanup_result_status(record)
                receipt_written = False

        if self._lease_marker_path is not None:
            if receipt_written and may_remove_active_marker and marker_ready:
                try:
                    _unlink_file_nofollow(self._lease_marker_path, expected_json={
                        "experiment_id": self.experiment_id,
                        "prefix_path": str(self.prefix),
                        "manifest_sha256": self._manifest_sha256,
                    })
                    record["active_lease_marker_removed_after_receipt"] = True
                    record["active_lease_marker_removed_at_unix"] = time.time()
                    persist_cleanup_receipt()
                except Exception as marker_error:
                    cleanup_error = cleanup_error or repr(marker_error)
                    record["cleanup_error"] = cleanup_error
                    record["active_lease_marker_removed_after_receipt"] = False
                    record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                    record["status"] = _cleanup_result_status(record)
                    try:
                        self._write_active_lease("RECONCILIATION_REQUIRED")
                    except Exception as restore_error:
                        record["cleanup_error"] += f"; reconciliation marker restore failed: {restore_error!r}"
                    try:
                        persist_cleanup_receipt()
                    except Exception as receipt_update_error:
                        receipt_error = repr(receipt_update_error)
                        record["receipt_write_error"] = receipt_error
                        record["status"] = _cleanup_result_status(record)
            elif self._lease_marker_path.exists() or self._lease_marker_path.is_symlink():
                try:
                    self._write_active_lease("RECONCILIATION_REQUIRED")
                except Exception as marker_error:
                    cleanup_error = cleanup_error or repr(marker_error)
                    record["cleanup_error"] = cleanup_error
                    record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
                    record["status"] = _cleanup_result_status(record)
                    try:
                        persist_cleanup_receipt()
                    except Exception as receipt_update_error:
                        receipt_error = repr(receipt_update_error)
                        record["receipt_write_error"] = receipt_error
                        record["status"] = _cleanup_result_status(record)

        try:
            self._release_global_lease_gate()
        except OSError as error:
            cleanup_error = cleanup_error or repr(error)
            record["cleanup_error"] = cleanup_error
            record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
            record["status"] = _cleanup_result_status(record)
            try:
                self._write_active_lease("RECONCILIATION_REQUIRED")
            except Exception as marker_error:
                record["cleanup_error"] += f"; reconciliation marker failed: {marker_error!r}"
            try:
                persist_cleanup_receipt()
            except Exception as receipt_update_error:
                receipt_error = repr(receipt_update_error)
                record["receipt_write_error"] = receipt_error
                record["status"] = _cleanup_result_status(record)

        if self._previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, self._previous_sigterm_handler)
        record["status"] = _cleanup_result_status(record)
        try:
            active_marker_removed = (self._lease_marker_path is None
                                     or _stat_entry_nofollow(self._lease_marker_path) is None)
        except OSError:
            active_marker_removed = False
        print(json.dumps({
            "experiment_id": self.experiment_id,
            "prefix_created": bool(prefix_created),
            "prefix_deleted": deleted,
            "prefix_preserved": preserved,
            "bytes_reclaimed": prefix_allocated_removed,
            "net_free_space_delta_bytes": net_free_delta,
            "storage_hygiene_status": record["storage_hygiene_status"],
            "free_space_status": record["free_space_status"],
            "status": record["status"],
            "cleanup_receipt": str(receipt_path),
            "receipt_write_error": receipt_error,
            "active_lease_marker_removed": active_marker_removed,
        }, sort_keys=True))
        if _cleanup_failure_overrides_exit(record["status"], exc):
            details = [f"PrefixLease cleanup completed with status {record['status']}"]
            if record["storage_hygiene_status"] == "FAIL_STORAGE_HYGIENE":
                details.append("storage budget or cleanup invariant failed")
            if receipt_error:
                details.append("durable cleanup receipt could not be written")
            if self._prefix_parent_fd is not None:
                if self._clone_temp_fd is not None:
                    os.close(self._clone_temp_fd)
                    self._clone_temp_fd = None
                os.close(self._prefix_parent_fd)
                self._prefix_parent_fd = None
            raise RuntimeError("; ".join(details))
        if self._prefix_parent_fd is not None:
            if self._clone_temp_fd is not None:
                os.close(self._clone_temp_fd)
                self._clone_temp_fd = None
            os.close(self._prefix_parent_fd)
            self._prefix_parent_fd = None
        return False


_FORBIDDEN_NATIVE_ENVIRONMENT_PATTERNS = (
    "DYLD_",
    "LD_",
    "__XPC_DYLD_",
    "PYTHON",
    "PERL",
    "RUBY",
    "NODE_",
)

_FORBIDDEN_NATIVE_EXACT_KEYS = frozenset({
    "BASH_ENV", "ENV", "SHELLOPTS", "BASH_OPTS",
    "CDPATH", "IFS", "GLOBIGNORE",
    "GCONV_PATH", "HOSTALIASES",
})

_ALLOWED_NATIVE_BASE_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL",
    "LANG", "LC_ALL", "LC_CTYPE", "TERM",
})
SUPPORTED_NATIVE_ARCHITECTURES = frozenset({"arm64", "x86_64"})


def _verify_macho_architecture(path: Path, descriptor: int,
                               expected_architecture: str) -> str:
    """Verify a thin 64-bit Mach-O slice against the signed run architecture."""
    if expected_architecture not in SUPPORTED_NATIVE_ARCHITECTURES:
        raise ValueError(f"unsupported declared native architecture: {expected_architecture!r}")
    header = os.pread(descriptor, 32, 0)
    if len(header) < 4:
        raise ValueError(f"file is smaller than Mach-O header: {path}")
    import struct
    magic = header[:4]
    fat_layouts = {
        b"\xca\xfe\xba\xbe": (">", 20),
        b"\xbe\xba\xfe\xca": ("<", 20),
        b"\xca\xfe\xba\xbf": (">", 32),
        b"\xbf\xba\xfe\xca": ("<", 32),
    }
    if magic in fat_layouts:
        if len(header) < 8:
            raise ValueError(f"fat Mach-O header is truncated: {path}")
        byte_order, entry_size = fat_layouts[magic]
        count = struct.unpack(f"{byte_order}I", header[4:8])[0]
        if count == 0 or count > 64:
            raise ValueError(f"fat Mach-O architecture count is malformed: {path}")
        table_end = 8 + count * entry_size
        if len(header) < min(table_end, 32) or os.fstat(descriptor).st_size < table_end:
            raise ValueError(f"fat Mach-O architecture table is truncated: {path}")
        raise ValueError(f"fat Mach-O binaries are unsupported; use one thin {expected_architecture} slice: {path}")
    if magic not in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        raise ValueError(f"file is not a valid thin 64-bit Mach-O binary (magic={magic!r}): {path}")
    if len(header) < 32:
        raise ValueError(f"thin Mach-O header is truncated: {path}")
    if magic == b"\xcf\xfa\xed\xfe":  # 0xfeedfacf little-endian 64-bit Mach-O
        cputype = struct.unpack("<I", header[4:8])[0]
    elif magic == b"\xfe\xed\xfa\xcf":  # big-endian 64-bit Mach-O
        cputype = struct.unpack(">I", header[4:8])[0]
    else:
        raise ValueError(f"file is not a valid thin 64-bit Mach-O binary (magic={magic!r}): {path}")

    actual_architecture = {
        0x01000007: "x86_64",  # CPU_TYPE_X86_64
        0x0100000C: "arm64",   # CPU_TYPE_ARM64
    }.get(cputype)
    if actual_architecture is None:
        raise ValueError(f"Mach-O CPU type is unsupported (cputype=0x{cputype:x}): {path}")
    if actual_architecture != expected_architecture:
        raise ValueError(
            f"Mach-O architecture mismatch: declared {expected_architecture}, actual {actual_architecture}: {path}")
    return actual_architecture


def _bind_native_file_identity(path: Path, expected_sha256: str | None = None,
                               *, verify_macho: bool = False,
                               expected_architecture: str | None = None) -> tuple[str, tuple[int, int, int, int, int]]:
    """Verify canonical path, descriptor stat stability, optional Mach-O, and content SHA-256."""
    canonical_str = os.path.abspath(path)
    if str(path) != canonical_str or path.is_symlink():
        raise ValueError(f"path must be absolute, canonical, and not a symlink: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError(f"parent directory is invalid or a symlink: {path.parent}")
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            before_stat = os.fstat(file_fd)
            if not stat.S_ISREG(before_stat.st_mode):
                raise ValueError(f"target is not a regular file: {path}")
            if verify_macho:
                if expected_architecture is None:
                    raise ValueError("native Mach-O validation requires a declared architecture")
                _verify_macho_architecture(path, file_fd, expected_architecture)

            hasher = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.pread(file_fd, 4 * 1024 * 1024, offset)
                if not chunk:
                    break
                hasher.update(chunk)
                offset += len(chunk)
            digest = hasher.hexdigest()

            after_stat = os.fstat(file_fd)
            before_id = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size,
                         before_stat.st_mtime_ns, getattr(before_stat, "st_ctime_ns", 0))
            after_id = (after_stat.st_dev, after_stat.st_ino, after_stat.st_size,
                        after_stat.st_mtime_ns, getattr(after_stat, "st_ctime_ns", 0))
            if before_id != after_id:
                raise ValueError(f"file identity changed during reading: {path}")
            if expected_sha256 and digest != expected_sha256:
                raise ValueError(f"content SHA-256 mismatch for {path}: expected {expected_sha256}, got {digest}")
            return digest, after_id
        finally:
            os.close(file_fd)
    finally:
        os.close(parent_fd)


def _construct_native_environment(candidate_roots: tuple[Path, ...],
                                  user_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Scrub inherited environment, reject loader injection hooks, and build synthetic DYLD paths."""
    if user_env is not None:
        for key in list(user_env.keys()):
            upper = key.upper()
            if any(upper.startswith(p) for p in _FORBIDDEN_NATIVE_ENVIRONMENT_PATTERNS):
                raise ValueError(f"native process environment contains forbidden loader variable: {key}")
            if key in _FORBIDDEN_NATIVE_EXACT_KEYS:
                raise ValueError(f"native process environment contains forbidden execution hook: {key}")
        source_env = user_env
    else:
        source_env = os.environ

    clean_env: dict[str, str] = {}
    for key, val in source_env.items():
        upper = key.upper()
        if any(upper.startswith(p) for p in _FORBIDDEN_NATIVE_ENVIRONMENT_PATTERNS):
            continue
        if key in _FORBIDDEN_NATIVE_EXACT_KEYS:
            continue
        if key in _ALLOWED_NATIVE_BASE_KEYS or key.startswith("FGMETAL_"):
            clean_env[key] = val

    clean_env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    clean_env.setdefault("LANG", "en_US.UTF-8")

    verified_paths: list[str] = []
    for root in candidate_roots:
        if not root.is_dir() or root.is_symlink():
            raise ValueError(f"candidate root must be a canonical non-symlinked directory: {root}")
        verified_paths.append(str(root.resolve(strict=True)))
    if "/usr/lib" not in verified_paths:
        verified_paths.append("/usr/lib")

    clean_env["DYLD_LIBRARY_PATH"] = ":".join(dict.fromkeys(verified_paths))
    clean_env["DYLD_PRINT_LIBRARIES"] = "1"
    return clean_env


@dataclass
class NativeRunLease:
    experiment_id: str
    executable_path: Path
    output_dir: Path
    candidate_dylibs: Mapping[str, Path]
    timeout_seconds: float = 300.0
    expected_executable_sha256: str | None = None
    expected_dylib_hashes: Mapping[str, str] | None = None
    expected_architecture: str | None = None
    max_log_bytes: int = 100 * MIB
    env: Mapping[str, str] | None = None
    manifest_path: Path | None = None

    def __post_init__(self) -> None:
        self.executable_path = Path(os.path.abspath(Path(self.executable_path).expanduser()))
        self.output_dir = Path(os.path.abspath(Path(self.output_dir).expanduser()))
        self.candidate_dylibs = {k: Path(os.path.abspath(Path(v).expanduser()))
                                 for k, v in (self.candidate_dylibs or {}).items()}
        self._managed_processes: list[subprocess.Popen[Any]] = []
        self._unsafe_live_process = False
        self._storage_violation = False
        self._lease_marker_path: Path | None = None
        self._manifest_sha256: str | None = None
        self._manifest: dict[str, Any] = {}
        self._manifest_estimates: dict[str, int] = {}
        self._storage_watch_events: list[dict[str, Any]] = []
        self._sealed_env: dict[str, str] | None = None
        self._bound_executable_identity: tuple[str, tuple[int, int, int, int, int]] | None = None
        self._native_architecture: str | None = None
        self._bound_dylibs: dict[str, dict[str, Any]] = {}
        self._temp_run_name: str | None = None
        self._temp_run_dir: Path | None = None
        self._temp_run_dir_identity: tuple[int, int] | None = None
        self._temp_run_dir_fd: int | None = None
        self._output_parent_fd: int | None = None
        self._free_before = 0
        self._project_before: dict[str, Any] = {}
        self._previous_sigterm_handler: Any = None
        self._sigterm_number: int | None = None
        self._cleanup_started = False
        self._lease_gate_lock_file: Any = None
        self._storage_allocation_lock_file: Any = None
        self._free_warning_emitted = False

    def _handle_sigterm(self, signum: int, frame: Any) -> None:
        self._sigterm_number = signum
        for proc in list(self._managed_processes):
            self._terminate_and_reap(proc, grace_seconds=2.0)
        if self._previous_sigterm_handler and callable(self._previous_sigterm_handler):
            self._previous_sigterm_handler(signum, frame)
        else:
            raise KeyboardInterrupt(f"interrupted by signal {signum}")

    def _write_active_marker(self, state: str) -> None:
        if self._lease_marker_path is None:
            return
        _write_json(self._lease_marker_path, {
            "experiment_id": self.experiment_id,
            "lease_type": "NativeRunLease",
            "pid": os.getpid(),
            "state": state,
            "executable_path": str(self.executable_path),
            "output_dir": str(self.output_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self._manifest_sha256,
            "native_architecture": self._native_architecture,
            "updated_at_unix": time.time(),
        }, strict_directory_sync=True)

    def _release_global_lease_gate(self) -> None:
        lock_file = self._lease_gate_lock_file
        self._lease_gate_lock_file = None
        allocation_lock_file = self._storage_allocation_lock_file
        self._storage_allocation_lock_file = None
        try:
            if lock_file is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            if lock_file is not None:
                lock_file.close()
        if allocation_lock_file is not None:
            _release_storage_allocation_lock(allocation_lock_file)

    def bind_executable_identity(self) -> tuple[str, tuple[int, int, int, int, int]]:
        digest, identity = _bind_native_file_identity(
            self.executable_path, self.expected_executable_sha256, verify_macho=True,
            expected_architecture=self._native_architecture)
        self._bound_executable_identity = (digest, identity)
        return digest, identity

    def bind_candidate_dylibs(self) -> dict[str, dict[str, Any]]:
        self._bound_dylibs = {}
        for role, path in self.candidate_dylibs.items():
            expected_hash = (self.expected_dylib_hashes or {}).get(role)
            digest, identity = _bind_native_file_identity(
                path, expected_hash, verify_macho=True,
                expected_architecture=self._native_architecture)
            parent = path.parent
            for child in parent.iterdir():
                if child.is_file() and not child.is_symlink():
                    if child.name not in {path.name, "manifest.json", ".fgmetal-build-lease.json"}:
                        if child.suffix.lower() in {".dylib", ".so", ".a", ".framework"}:
                            raise ValueError(f"candidate directory contains unapproved binary: {child}")
            self._bound_dylibs[role] = {
                "path": str(path),
                "sha256": digest,
                "architecture": self._native_architecture,
                "device": identity[0],
                "inode": identity[1],
            }
        return self._bound_dylibs

    @property
    def scratch_directory(self) -> Path:
        """Return the active lease-owned run directory after identity checks."""
        path = self._temp_run_dir
        if self._cleanup_started or path is None or self._temp_run_dir_identity is None:
            raise RuntimeError("NativeRunLease scratch directory is not active")
        try:
            info = path.lstat()
        except OSError as error:
            raise RuntimeError("NativeRunLease scratch directory is unavailable") from error
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (info.st_dev, info.st_ino) != self._temp_run_dir_identity):
            raise RuntimeError("NativeRunLease scratch directory identity changed")
        return path

    def set_env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        roots = tuple({path.parent for path in self.candidate_dylibs.values()})
        self._sealed_env = _construct_native_environment(roots, env or self.env)
        return self._sealed_env

    def __enter__(self) -> "NativeRunLease":
        if self.manifest_path is None:
            self.manifest_path = _experiment_manifest_path(self.experiment_id)
        if not self.manifest_path.is_file() or self.manifest_path.is_symlink():
            raise ValueError(f"experiment manifest is missing or unsafe: {self.manifest_path}")
        manifest_bytes = _read_file_nofollow(self.manifest_path)
        self._manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self._manifest = json.loads(manifest_bytes)
        if not _verify_manifest_signature(self._manifest):
            raise RuntimeError("NativeRunLease refuses an edited or unsigned storage manifest")
        if self._manifest.get("experiment_id") != self.experiment_id:
            raise ValueError("NativeRunLease experiment ID does not match the storage manifest")
        if self._manifest.get("decision") != "ALLOW":
            raise RuntimeError("NativeRunLease refuses a manifest that did not pass the storage gate")
        try:
            self._manifest_estimates = _validated_manifest_estimates(self._manifest)
        except ValueError as error:
            raise RuntimeError(f"NativeRunLease refuses invalid projected-peak estimates: {error}") from error
        if self._manifest.get("expected_prefix_bytes", 0) != 0:
            raise ValueError("NativeRunLease requires expected_prefix_bytes == 0 (no Wine prefix)")
        if self._manifest.get("expected_prefix_requirement") is True:
            raise ValueError("NativeRunLease rejects manifests requiring a Wine prefix")
        declared_architecture = self._manifest.get("native_architecture")
        if declared_architecture not in SUPPORTED_NATIVE_ARCHITECTURES:
            raise ValueError("NativeRunLease requires a supported native_architecture in the signed manifest")
        if (self.expected_architecture is not None
                and self.expected_architecture != declared_architecture):
            raise ValueError("NativeRunLease architecture differs from the signed manifest")
        self._native_architecture = declared_architecture

        attestation_hash = hashlib.sha256(_read_file_nofollow(GATE0_ATTESTATION_PATH)).hexdigest() \
            if GATE0_ATTESTATION_PATH.is_file() and not GATE0_ATTESTATION_PATH.is_symlink() else None
        if self._manifest.get("gate0_attestation_sha256") != attestation_hash:
            raise RuntimeError("NativeRunLease manifest does not bind to the current Gate 0 attestation")
        storage_smoke = self._manifest.get("storage_policy_smoke") is True
        blockers = _gate0_blockers(active_experiment_id=self.experiment_id, allow_storage_smoke=storage_smoke)
        if blockers:
            raise RuntimeError("Gate 0 blocks NativeRunLease: " + "; ".join(blockers))

        self._free_before = disk_free_bytes(self.output_dir.parent)
        self._project_before = measure_project_usage()
        current_scoped = conservative_project_usage_bytes(self._project_before)
        admission = projected_storage_admission(
            current_free_bytes=self._free_before,
            current_scoped_bytes=current_scoped,
            peak_additional_bytes=self._manifest_estimates["estimated_projected_new_disk_bytes"],
        )
        if not admission["admitted"]:
            raise RuntimeError("NativeRunLease projected-peak admission failed: "
                               + "; ".join(admission["blockers"]))

        ACTIVE_LEASE_ROOT.mkdir(parents=True, exist_ok=True)
        lock_path = ACTIVE_LEASE_ROOT / ".lease.lock"
        active_root_fd = _open_directory_nofollow(ACTIVE_LEASE_ROOT)
        try:
            lock_fd = os.open(lock_path.name, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                              0o600, dir_fd=active_root_fd)
            os.fsync(active_root_fd)
        finally:
            os.close(active_root_fd)
        self._lease_gate_lock_file = os.fdopen(lock_fd, "a+")
        try:
            fcntl.flock(self._lease_gate_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._lease_gate_lock_file.close()
            self._lease_gate_lock_file = None
            raise RuntimeError("another lease is starting or running") from error

        try:
            self._storage_allocation_lock_file = _acquire_storage_allocation_lock()
            locked_blockers = _gate0_blockers(
                active_experiment_id=self.experiment_id, allow_storage_smoke=storage_smoke)
            if locked_blockers:
                raise RuntimeError("Gate 0 changed during NativeRunLease admission: "
                                   + "; ".join(locked_blockers))
            locked_free = disk_free_bytes(self.output_dir.parent)
            locked_project_usage = measure_project_usage()
            locked_project = conservative_project_usage_bytes(locked_project_usage)
            locked_admission = projected_storage_admission(
                current_free_bytes=locked_free,
                current_scoped_bytes=locked_project,
                peak_additional_bytes=self._manifest_estimates["estimated_projected_new_disk_bytes"],
            )
            if not locked_admission["admitted"]:
                raise RuntimeError("NativeRunLease projected-peak admission failed under the shared allocation lock: "
                                   + "; ".join(locked_admission["blockers"]))
            for warning in locked_admission["warnings"]:
                self._storage_watch_events.append({
                    "sampled_at_unix": time.time(),
                    "action": "PROJECTED_STORAGE_WARNING_BEFORE_NATIVE_LEASE",
                    "warning": warning,
                })
            self._free_before = locked_free
            self._project_before = locked_project_usage
            active = _active_lease_markers()
            if active:
                raise RuntimeError("an active or interrupted lease requires reconciliation: "
                                   + ", ".join(str(path) for path in active))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._lease_marker_path = ACTIVE_LEASE_ROOT / f"{self.experiment_id}.json"
            if self._lease_marker_path.exists() or self._lease_marker_path.is_symlink():
                raise FileExistsError(f"active lease marker already exists: {self._lease_marker_path}")
            self._write_active_marker("PREPARED")
        except BaseException:
            self._release_global_lease_gate()
            raise

        try:
            self._previous_sigterm_handler = signal.signal(signal.SIGTERM, self._handle_sigterm)
        except ValueError:
            self._previous_sigterm_handler = None

        _release_storage_allocation_lock(self._storage_allocation_lock_file)
        self._storage_allocation_lock_file = None

        try:
            # Create isolated scratch run directory
            self._output_parent_fd = _open_directory_nofollow(self.output_dir, create=True)
            self._temp_run_name = f".run_scratch_{self.experiment_id}_{uuid.uuid4().hex[:8]}"
            self._temp_run_dir = self.output_dir / self._temp_run_name
            os.mkdir(self._temp_run_name, 0o700, dir_fd=self._output_parent_fd)
            created_stat = _stat_child_nofollow(self._output_parent_fd, self._temp_run_name)
            if created_stat is None:
                raise OSError("scratch directory disappeared immediately after creation")
            self._temp_run_dir_identity = (created_stat.st_dev, created_stat.st_ino)
            self._temp_run_dir_fd = os.open(self._temp_run_name,
                                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                                            dir_fd=self._output_parent_fd)

            # Cryptographic bindings
            self.bind_executable_identity()
            self.bind_candidate_dylibs()
            self.set_env(self.env)
            return self
        except BaseException:
            if self._temp_run_dir_fd is not None:
                try:
                    os.close(self._temp_run_dir_fd)
                except OSError:
                    pass
                self._temp_run_dir_fd = None
            if self._output_parent_fd is not None and self._temp_run_name is not None:
                try:
                    _remove_child_directory_nofollow(
                        self._output_parent_fd, self._temp_run_name,
                        expected_identity=self._temp_run_dir_identity)
                except OSError:
                    pass
            if self._output_parent_fd is not None:
                try:
                    os.close(self._output_parent_fd)
                except OSError:
                    pass
                self._output_parent_fd = None
            if self._lease_marker_path is not None:
                try:
                    _unlink_file_nofollow(self._lease_marker_path)
                except OSError:
                    pass
                self._lease_marker_path = None
            if self._previous_sigterm_handler is not None:
                signal.signal(signal.SIGTERM, self._previous_sigterm_handler)
                self._previous_sigterm_handler = None
            self._release_global_lease_gate()
            raise

    def start_native_process(self, args: list[str] | tuple[str, ...],
                             **popen_options: Any) -> subprocess.Popen[Any]:
        if self._cleanup_started:
            raise RuntimeError("cannot launch process during or after cleanup")
        if self._temp_run_dir is None or not self._temp_run_dir.is_dir():
            raise RuntimeError("scratch directory is not materialized")
        if not args:
            raise ValueError("empty args")
        target_exec = Path(os.path.abspath(Path(args[0]).expanduser()))
        if target_exec != self.executable_path:
            raise ValueError(f"executable path substitution detected: expected {self.executable_path}, got {target_exec}")

        if "env" in popen_options and popen_options["env"] != self._sealed_env:
            raise ValueError("environment mutation after sealing is forbidden")

        # Pre-exec TOCTOU re-check
        self.bind_executable_identity()
        self.bind_candidate_dylibs()
        if self._sealed_env is None:
            raise RuntimeError("environment not sealed")

        self._write_active_marker("RUNNING")
        child = subprocess.Popen(
            args,
            env=self._sealed_env,
            cwd=str(self._temp_run_dir),
            **popen_options,
        )
        self._managed_processes.append(child)
        return child

    def wait_process(self, process: subprocess.Popen[Any], *,
                     timeout_seconds: float | None = None,
                     poll_interval_seconds: float = 1.0) -> int:
        if process not in self._managed_processes:
            self._managed_processes.append(process)
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        started = time.monotonic()
        deadline = started + timeout
        while True:
            ret = process.poll()
            if ret is not None:
                if process in self._managed_processes:
                    self._managed_processes.remove(process)
                return ret
            free = disk_free_bytes(self.output_dir.parent)
            event = {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "free_bytes": free,
                "sampled_at_unix": time.time(),
            }
            if free < FREE_HARD_STOP:
                event["action"] = "HARD_STOP_TERMINATE"
                self._storage_violation = True
                self._storage_watch_events.append(event)
                self._terminate_and_reap(process)
                if process in self._managed_processes:
                    self._managed_processes.remove(process)
                raise RuntimeError(f"free space crossed the 25 GiB hard stop during the run: {free}")
            if free < FREE_WARNING and not self._free_warning_emitted:
                event["action"] = "WARNING_NO_NEW_EXPERIMENTS"
                self._free_warning_emitted = True
                self._storage_watch_events.append(event)
            if time.monotonic() >= deadline:
                event["action"] = "TIMEOUT_EXPIRED"
                self._storage_watch_events.append(event)
                self._terminate_and_reap(process)
                if process in self._managed_processes:
                    self._managed_processes.remove(process)
                raise subprocess.TimeoutExpired(process.args, timeout)
            time.sleep(min(poll_interval_seconds, max(0.05, deadline - time.monotonic())))

    def _terminate_and_reap(self, process: subprocess.Popen[Any], *, grace_seconds: float = 5.0) -> bool:
        if process.poll() is not None:
            return True
        try:
            process.terminate()
            try:
                process.wait(timeout=grace_seconds)
                return True
            except subprocess.TimeoutExpired:
                pass
            process.kill()
            try:
                process.wait(timeout=grace_seconds)
                return True
            except subprocess.TimeoutExpired:
                self._unsafe_live_process = True
                return False
        except OSError:
            return process.poll() is not None

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._cleanup_started = True
        cleanup_error: str | None = None
        receipt_error: str | None = None

        # Phase 1: Reap managed processes
        for process in list(self._managed_processes):
            if not self._terminate_and_reap(process):
                self._unsafe_live_process = True
            elif process in self._managed_processes:
                self._managed_processes.remove(process)

        # Close descriptor to scratch dir
        if self._temp_run_dir_fd is not None:
            try:
                os.close(self._temp_run_dir_fd)
            except OSError:
                pass
            self._temp_run_dir_fd = None

        # Phase 2: Temporary run directory cleanup
        temp_removed = False
        if self._temp_run_dir is not None and self._temp_run_dir.exists():
            if self._unsafe_live_process:
                cleanup_error = "scratch run directory retained because a managed process may still be live"
                self._storage_violation = True
            else:
                try:
                    if self._output_parent_fd is not None and self._temp_run_name is not None:
                        _remove_child_directory_nofollow(
                            self._output_parent_fd, self._temp_run_name,
                            expected_identity=self._temp_run_dir_identity)
                    else:
                        shutil.rmtree(self._temp_run_dir)
                    temp_removed = True
                except Exception as error:
                    cleanup_error = f"temporary run directory cleanup failed: {error!r}"
                    self._storage_violation = True

        if self._output_parent_fd is not None:
            try:
                os.close(self._output_parent_fd)
            except OSError:
                pass
            self._output_parent_fd = None

        # Phase 3: Log file bounds inspection
        if self.output_dir.is_dir():
            try:
                for child in self.output_dir.iterdir():
                    if child.is_file() and child.suffix in {".log", ".jsonl", ".json"}:
                        if child.stat().st_size > self.max_log_bytes:
                            cleanup_error = f"log file exceeds maximum size limit ({child.stat().st_size} > {self.max_log_bytes}): {child.name}"
                            self._storage_violation = True
            except Exception as error:
                cleanup_error = cleanup_error or f"log bounds inspection failed: {error!r}"

        # Phase 4: Storage measurement
        free_after = disk_free_bytes(self.output_dir.parent)
        net_free_delta = free_after - self._free_before
        project_after = measure_project_usage()
        project_before_bytes = int(self._project_before.get("allocated_inode_deduplicated_bytes", 0))
        project_after_bytes = int(project_after.get("allocated_inode_deduplicated_bytes", 0))
        persistent_growth = max(0, project_after_bytes - project_before_bytes)
        expected_persistent = int(self._manifest.get("estimated_persistent_bytes", 0))
        unexpected_persistent = max(0, persistent_growth - expected_persistent)

        storage_status = "PASS"
        free_status = "PASS"
        if free_after < FREE_HARD_STOP:
            free_status = "FAIL_HARD_STOP"
            storage_status = "FAIL_STORAGE_HYGIENE"
        elif free_after < FREE_MINIMUM:
            free_status = "FAIL_BELOW_NEXT_RUN_MINIMUM"
            storage_status = "FAIL_STORAGE_HYGIENE"
        if project_after_bytes > GATE0_PROJECT_BUDGET:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_GATE0_PROJECT_BUDGET"
        if self._unsafe_live_process:
            storage_status = "FAIL_STORAGE_HYGIENE"
            free_status = "FAIL_PROCESS_REAP"
        if self._storage_violation or cleanup_error:
            storage_status = "FAIL_STORAGE_HYGIENE"

        record = {
            "schema_version": 3,
            "lease_type": "NativeRunLease",
            "experiment_id": self.experiment_id,
            "created_at_unix": time.time(),
            "executable_path": str(self.executable_path),
            "executable_sha256": self._bound_executable_identity[0] if self._bound_executable_identity else None,
            "native_architecture": self._native_architecture,
            "bound_dylibs": self._bound_dylibs,
            "temporary_run_dir": str(self._temp_run_dir) if self._temp_run_dir else None,
            "temporary_created": self._temp_run_dir is not None,
            "temporary_removed": temp_removed,
            "free_before_bytes": self._free_before,
            "free_after_bytes": free_after,
            "net_free_space_delta_bytes": net_free_delta,
            "project_usage_before": self._project_before,
            "project_usage_after": project_after,
            "persistent_retained_allocated_estimate_bytes": persistent_growth,
            "unexpected_persistent_bytes": unexpected_persistent,
            "storage_hygiene_status": storage_status,
            "free_space_status": free_status,
            "status": "PASS" if storage_status == "PASS" and free_status == "PASS" and not cleanup_error else "FAIL",
            "cleanup_error": cleanup_error,
            "exception": repr(exc) if exc is not None else None,
            "interrupted_by_signal": self._sigterm_number,
        }

        # Phase 5: Padded receipt persistence
        receipt_path = self.output_dir / "native-run-cleanup.json"
        try:
            record["cleanup_receipt_hmac_sha256"] = _storage_evidence_signature(
                record, "cleanup_receipt_hmac_sha256")
            _write_json(receipt_path, record, pad_to_bytes=64 * 1024, strict_directory_sync=True)
            record["free_after_bytes"] = disk_free_bytes(self.output_dir.parent)
            record["cleanup_receipt_hmac_sha256"] = _storage_evidence_signature(
                record, "cleanup_receipt_hmac_sha256")
            _write_json(receipt_path, record, pad_to_bytes=64 * 1024, strict_directory_sync=True)
            receipt_written = True
        except Exception as error:
            receipt_error = repr(error)
            record["receipt_write_error"] = receipt_error
            record["storage_hygiene_status"] = "FAIL_STORAGE_HYGIENE"
            record["status"] = "FAIL"
            receipt_written = False

        # Phase 6: Invalidation & marker handling
        threshold_failures = _cleanup_gate0_threshold_failures(record)
        if (threshold_failures or storage_status != "PASS" or free_status != "PASS"
                or not receipt_written or cleanup_error):
            if self._lease_marker_path is not None:
                try:
                    self._write_active_marker("RECONCILIATION_REQUIRED")
                except Exception:
                    pass
            try:
                _invalidate_gate0_after_cleanup_threshold_failure(
                    self.experiment_id,
                    threshold_failures or ["NATIVE_RUN_HYGIENE_FAILURE"],
                    free_after_bytes=record.get("free_after_bytes"),
                    project_after_bytes=project_after_bytes,
                )
            except Exception:
                pass
        else:
            if self._lease_marker_path is not None:
                try:
                    _unlink_file_nofollow(self._lease_marker_path, expected_json={
                        "experiment_id": self.experiment_id,
                        "manifest_sha256": self._manifest_sha256,
                    })
                except Exception:
                    try:
                        self._write_active_marker("RECONCILIATION_REQUIRED")
                    except Exception:
                        pass

        try:
            self._release_global_lease_gate()
        except OSError:
            pass

        if self._previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, self._previous_sigterm_handler)

        if _cleanup_failure_overrides_exit(record["status"], exc):
            raise RuntimeError(f"NativeRunLease cleanup failed: {cleanup_error or storage_status}")
        return False


def assert_native_lease(experiment_id: str, executable_path: Path | str,
                        runner_path: Path | str | None = None) -> dict[str, Any]:
    """Require the active lease and the exact runner source authorized by its signed manifest."""
    if runner_path is None:
        raise RuntimeError("Native runner source path is required for authenticated execution")
    selected = Path(runner_path).expanduser().absolute()
    resolved = selected.resolve(strict=True)
    if selected.is_symlink() or selected != resolved or not resolved.is_file():
        raise RuntimeError(f"Native runner source is unavailable: {runner_path}")
    marker_path = ACTIVE_LEASE_ROOT / f"{experiment_id}.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        raise RuntimeError(f"Native runner requires an active NativeRunLease marker: {marker_path}")
    marker = json.loads(_read_file_nofollow(marker_path))
    if marker.get("experiment_id") != experiment_id:
        raise RuntimeError("Native lease marker names a different experiment ID")
    expected_exec = str(Path(os.path.abspath(Path(executable_path).expanduser())))
    if marker.get("executable_path") != expected_exec:
        raise RuntimeError(f"Native lease marker names different executable: {marker.get('executable_path')} != {expected_exec}")
    manifest_path = _experiment_manifest_path(experiment_id)
    if marker.get("manifest_path") != str(manifest_path):
        raise RuntimeError("Native lease marker names a different experiment manifest")
    manifest_bytes = _read_file_nofollow(manifest_path)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    if (marker.get("manifest_sha256") != manifest_sha256
            or not _verify_manifest_signature(manifest)
            or manifest.get("experiment_id") != experiment_id
            or manifest.get("decision") != "ALLOW"):
        raise RuntimeError("Native runner manifest is unsigned, changed, or not admitted")
    if manifest.get("native_architecture") != marker.get("native_architecture"):
        raise RuntimeError("Native lease architecture differs from the signed manifest")
    authorized_paths = manifest.get("authorized_runner_paths")
    authorized_hashes = manifest.get("authorized_runner_sha256")
    runner_hash = hashlib.sha256(_read_file_nofollow(resolved)).hexdigest()
    if (not isinstance(authorized_paths, list) or str(resolved) not in authorized_paths
            or not isinstance(authorized_hashes, Mapping)
            or authorized_hashes.get(str(resolved)) != runner_hash):
        raise RuntimeError("Native runner path or source hash is not authorized by the signed manifest")
    return marker


def current_lease() -> PrefixLease | NativeRunLease:
    if _ACTIVE_LEASE is None:
        raise RuntimeError("runner did not enter the shared Lease context")
    return _ACTIVE_LEASE


def scoped_lease(lease: PrefixLease | NativeRunLease):
    """Context manager that also exposes the lease to a runner body."""
    class Scope:
        def __enter__(self):
            global _ACTIVE_LEASE
            self.old = _ACTIVE_LEASE
            lease.__enter__()
            _ACTIVE_LEASE = lease
            return lease

        def __exit__(self, exc_type, exc, tb):
            global _ACTIVE_LEASE
            try:
                return lease.__exit__(exc_type, exc, tb)
            finally:
                _ACTIVE_LEASE = self.old
    return Scope()


def make_experiment_id(label: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label).strip("-")
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{safe or 'run'}-{uuid.uuid4().hex[:8]}"


@contextmanager
def open_canonical_artifact(digest: str) -> Iterator[BinaryIO]:
    """Yield a verified, descriptor-pinned artifact stream from the canonical CAS."""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("canonical artifact lookup requires a lowercase SHA-256 digest")
    root_fd = _open_directory_nofollow(ARTIFACT_STORE)
    group_fd: int | None = None
    manifest_fd: int | None = None
    payload_fd: int | None = None
    payload_stream: BinaryIO | None = None
    try:
        root_info = os.fstat(root_fd)
        root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        group_fd = os.open(digest, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        group_info = os.fstat(group_fd)
        group_fsid = getattr(os.fstatvfs(group_fd), "f_fsid", None)
        if (group_fsid is None or group_fsid != root_fsid
                or group_info.st_dev != root_info.st_dev):
            raise OSError("canonical artifact hash directory crossed its verified store filesystem")

        manifest_fd = os.open("manifest.json", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                              | getattr(os, "O_NONBLOCK", 0),
                              dir_fd=group_fd)
        manifest_before = os.fstat(manifest_fd)
        if (not stat.S_ISREG(manifest_before.st_mode) or manifest_before.st_nlink != 1
                or manifest_before.st_size > 1024 * 1024):
            raise OSError("canonical artifact manifest is not a bounded regular file")
        manifest_flags = fcntl.fcntl(manifest_fd, fcntl.F_GETFL)
        fcntl.fcntl(manifest_fd, fcntl.F_SETFL, manifest_flags & ~getattr(os, "O_NONBLOCK", 0))
        manifest_chunks: list[bytes] = []
        manifest_total = 0
        while True:
            block = os.read(manifest_fd, min(64 * 1024, 1024 * 1024 + 1 - manifest_total))
            if not block:
                break
            manifest_chunks.append(block)
            manifest_total += len(block)
            if manifest_total > 1024 * 1024:
                raise OSError("canonical artifact manifest exceeds its read limit")
        manifest_after = os.fstat(manifest_fd)
        if ((manifest_before.st_dev, manifest_before.st_ino, manifest_before.st_size,
             manifest_before.st_mtime_ns, getattr(manifest_before, "st_ctime_ns", 0)) !=
            (manifest_after.st_dev, manifest_after.st_ino, manifest_after.st_size,
             manifest_after.st_mtime_ns, getattr(manifest_after, "st_ctime_ns", 0))
                or manifest_total != manifest_before.st_size):
            raise OSError("canonical artifact manifest changed while it was read")
        manifest = json.loads(b"".join(manifest_chunks))
        if not isinstance(manifest, dict) or manifest.get("sha256") != digest:
            raise OSError("canonical artifact manifest does not bind the requested SHA-256")
        stored_path = manifest.get("canonical_path")
        path = Path(stored_path) if isinstance(stored_path, str) else Path()
        name = path.name
        expected_path = ARTIFACT_STORE / digest / name
        if (not name or name in {".", ".."} or "/" in name or "\\" in name
                or path != expected_path or str(path) != stored_path):
            raise OSError("canonical artifact manifest path is outside its exact digest directory")

        payload_fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=group_fd)
        payload_before = os.fstat(payload_fd)
        payload_fsid = getattr(os.fstatvfs(payload_fd), "f_fsid", None)
        if (not stat.S_ISREG(payload_before.st_mode) or payload_before.st_nlink != 1
                or payload_before.st_dev != group_info.st_dev
                or payload_fsid is None or payload_fsid != group_fsid):
            raise OSError("canonical artifact payload is not a regular file on its hash-store filesystem")
        payload_flags = fcntl.fcntl(payload_fd, fcntl.F_GETFL)
        fcntl.fcntl(payload_fd, fcntl.F_SETFL, payload_flags & ~getattr(os, "O_NONBLOCK", 0))
        # Schema-v1 CAS manifests used byte_size; schema-v2 uses size_bytes.
        # Accept either spelling while refusing conflicting dual declarations.
        expected_size = manifest.get("size_bytes", manifest.get("byte_size"))
        legacy_size = manifest.get("byte_size")
        if (isinstance(expected_size, bool) or not isinstance(expected_size, int)
                or expected_size != payload_before.st_size
                or (legacy_size is not None and legacy_size != expected_size)):
            raise OSError("canonical artifact payload size differs from its manifest")
        hasher = hashlib.sha256()
        payload_total = 0
        while True:
            block = os.read(payload_fd, 4 * 1024 * 1024)
            if not block:
                break
            hasher.update(block)
            payload_total += len(block)
        payload_after = os.fstat(payload_fd)
        if ((payload_before.st_dev, payload_before.st_ino, payload_before.st_size,
             payload_before.st_mtime_ns, getattr(payload_before, "st_ctime_ns", 0)) !=
            (payload_after.st_dev, payload_after.st_ino, payload_after.st_size,
             payload_after.st_mtime_ns, getattr(payload_after, "st_ctime_ns", 0))
                or payload_total != payload_before.st_size or hasher.hexdigest() != digest):
            raise OSError("canonical artifact payload failed SHA-256 or stability verification")
        os.lseek(payload_fd, 0, os.SEEK_SET)
        payload_stream = os.fdopen(payload_fd, "rb", closefd=True)
        payload_fd = None
        try:
            yield payload_stream
        finally:
            payload_stream.close()
    except FileNotFoundError as error:
        if payload_stream is not None:
            raise
        raise FileNotFoundError(f"canonical artifact is missing from the verified hash store: {digest}") from error
    finally:
        if payload_fd is not None:
            os.close(payload_fd)
        if manifest_fd is not None:
            os.close(manifest_fd)
        if group_fd is not None:
            os.close(group_fd)
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storage policy management, reconciliation, and recovery.")
    sub = parser.add_subparsers(dest="command", required=True)
    recov = sub.add_parser("recover-interrupted-smoke", help="Recover an interrupted storage-only smoke lease marker")
    recov.add_argument("experiment_id", help="Interrupted smoke experiment ID")
    sub.add_parser("close-historical-terminal-run",
                   help="Append the fixed history-preserving Z9 terminal closure")
    sub.add_parser("reconcile", help="Reconcile abandoned storage smoke lease markers")
    sub.add_parser("blockers", help="Check live Gate 0 storage blockers")
    args = parser.parse_args(argv)

    if args.command == "recover-interrupted-smoke":
        result = recover_interrupted_storage_smoke_lease_marker(args.experiment_id)
        print(json.dumps(result, indent=2))
        return 0
    elif args.command == "close-historical-terminal-run":
        result = close_historical_terminal_run()
        print(json.dumps(result, indent=2))
        return 0
    elif args.command == "reconcile":
        results = reconcile_abandoned_storage_smoke_lease_markers()
        print(json.dumps(results, indent=2))
        return 0
    elif args.command == "blockers":
        blockers = _gate0_blockers()
        print(json.dumps(blockers, indent=2))
        return 0 if not blockers else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
