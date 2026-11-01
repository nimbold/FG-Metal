"""Canonical R6-A execution dependency closure for R71 and later routes.

The manifest binds the frozen candidate, plan, and G0C review core by stable
digests. Detached candidate and G0C envelopes bind the manifest's exact raw
SHA-256; only that envelope field is excluded from each core digest. This module observes dependencies at a boundary. It does not
prove that an uncooperative writer did not mutate and restore a path between
observations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping


MANIFEST_NAME = "R6-A-R71-EXECUTION-DEPENDENCY-MANIFEST.json"
R72_MANIFEST_NAME = "R6-A-R72-EXECUTION-DEPENDENCY-MANIFEST.json"
R73_MANIFEST_NAME = "R6-A-R73-EXECUTION-DEPENDENCY-MANIFEST.json"
R74_MANIFEST_NAME = "R6-A-R74-EXECUTION-DEPENDENCY-MANIFEST.json"
R75_MANIFEST_NAME = "R6-A-R75-EXECUTION-DEPENDENCY-MANIFEST.json"
R76_MANIFEST_NAME = "R6-A-R76-EXECUTION-DEPENDENCY-MANIFEST.json"
R77_MANIFEST_NAME = "R6-A-R77-EXECUTION-DEPENDENCY-MANIFEST.json"
R78_MANIFEST_NAME = "R6-A-R78-EXECUTION-DEPENDENCY-MANIFEST.json"
R79_MANIFEST_NAME = "R6-A-R79-EXECUTION-DEPENDENCY-MANIFEST.json"
R80_MANIFEST_NAME = "R6-A-R80-EXECUTION-DEPENDENCY-MANIFEST.json"
R81_MANIFEST_NAME = "R6-A-R81-EXECUTION-DEPENDENCY-MANIFEST.json"
R86_MANIFEST_NAME = "R6-A-R86-EXECUTION-DEPENDENCY-MANIFEST.json"
R87_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-2.json"
R87_CANDIDATE_3_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-3.json"
R87_CANDIDATE_4_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-4.json"
R87_CANDIDATE_5_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-5.json"
R87_CANDIDATE_6_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-6.json"
R87_CANDIDATE_7_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-7.json"
R87_CANDIDATE_8_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-8.json"
R87_CANDIDATE_9_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-9.json"
R87_CANDIDATE_10_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-10.json"
R87_CANDIDATE_11_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-11.json"
R87_CANDIDATE_12_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-12.json"
R87_CANDIDATE_13_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-13.json"
R87_CANDIDATE_14_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-14.json"
R87_CANDIDATE_15_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-15.json"
R87_CANDIDATE_16_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-16.json"
R87_CANDIDATE_17_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-17.json"
R87_CANDIDATE_18_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-18.json"
R87_CANDIDATE_19_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-19.json"
R87_CANDIDATE_20_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-20.json"
R87_CANDIDATE_21_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-21.json"
R87_CANDIDATE_22_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-22.json"
R87_CANDIDATE_23_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-23.json"
R87_CANDIDATE_24_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-24.json"
R87_CANDIDATE_25_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-25.json"
R87_CANDIDATE_26_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-26.json"
R87_CANDIDATE_27_MANIFEST_NAME = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-27.json"
MANIFEST_SCHEMA_VERSION = 2
R71_BINDING_FIELD = "r71_binding"
R72_BINDING_FIELD = "r72_binding"
R73_BINDING_FIELD = "r73_binding"
R74_BINDING_FIELD = "r74_binding"
R75_BINDING_FIELD = "r75_binding"
R76_BINDING_FIELD = "r76_binding"
R77_BINDING_FIELD = "r77_binding"
R78_BINDING_FIELD = "r78_binding"
R79_BINDING_FIELD = "r79_binding"
R80_BINDING_FIELD = "r80_binding"
R81_BINDING_FIELD = "r81_binding"
R86_BINDING_FIELD = "r86_binding"
R87_BINDING_FIELD = "r87_binding"
R71_MANIFEST_ENVELOPE_FIELD = "execution_dependency_manifest"
GATE_CONTROL_FILES = (
    "storage_policy.py", "prefix_preflight.py", "storage_inventory.py",
    "artifactize_builds.py", "build_retention.py", "storage_gate0.py",
    "test_build_retention.py", "test_storage_policy.py", "smoke_prefix_lease.py",
    "consolidate_evidence.py", "test_evidence_consolidation.py",
    "test_wine_launcher_policy.py", "test_g0c_checks.py", "test_storage_recovery.py",
)
R71_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-[1-3]\Z")
R72_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R72-CAPACITY-RECALIBRATION-CANDIDATE-1\Z")
R73_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-1\Z")
R74_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-1\Z")
R75_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R75-G0C-EVIDENCE-SCHEMA-CANDIDATE-1\Z")
R76_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R76-RUNNER-ROUTE-CANDIDATE-1\Z")
R77_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1\Z")
R78_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1\Z")
R79_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1\Z")
R80_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1\Z")
R81_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-1\Z")
R86_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1\Z")
R87_CANDIDATE_ID_PATTERN = re.compile(
    r"R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-(?:[2-9]|1[0-9]|2[0-7])\Z")


def _candidate_id_pattern(candidate_id: Any) -> re.Pattern[str] | None:
    if not isinstance(candidate_id, str):
        return None
    if R71_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R71_CANDIDATE_ID_PATTERN
    if R72_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R72_CANDIDATE_ID_PATTERN
    if R73_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R73_CANDIDATE_ID_PATTERN
    if R74_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R74_CANDIDATE_ID_PATTERN
    if R75_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R75_CANDIDATE_ID_PATTERN
    if R76_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R76_CANDIDATE_ID_PATTERN
    if R77_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R77_CANDIDATE_ID_PATTERN
    if R78_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R78_CANDIDATE_ID_PATTERN
    if R79_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R79_CANDIDATE_ID_PATTERN
    if R80_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R80_CANDIDATE_ID_PATTERN
    if R81_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R81_CANDIDATE_ID_PATTERN
    if R87_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R87_CANDIDATE_ID_PATTERN
    if R86_CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        return R86_CANDIDATE_ID_PATTERN
    return None


def manifest_name_for_candidate(candidate_id: Any) -> str:
    pattern = _candidate_id_pattern(candidate_id)
    if pattern is R71_CANDIDATE_ID_PATTERN:
        return MANIFEST_NAME
    if pattern is R72_CANDIDATE_ID_PATTERN:
        return R72_MANIFEST_NAME
    if pattern is R73_CANDIDATE_ID_PATTERN:
        return R73_MANIFEST_NAME
    if pattern is R74_CANDIDATE_ID_PATTERN:
        return R74_MANIFEST_NAME
    if pattern is R75_CANDIDATE_ID_PATTERN:
        return R75_MANIFEST_NAME
    if pattern is R76_CANDIDATE_ID_PATTERN:
        return R76_MANIFEST_NAME
    if pattern is R77_CANDIDATE_ID_PATTERN:
        return R77_MANIFEST_NAME
    if pattern is R78_CANDIDATE_ID_PATTERN:
        return R78_MANIFEST_NAME
    if pattern is R79_CANDIDATE_ID_PATTERN:
        return R79_MANIFEST_NAME
    if pattern is R80_CANDIDATE_ID_PATTERN:
        return R80_MANIFEST_NAME
    if pattern is R81_CANDIDATE_ID_PATTERN:
        return R81_MANIFEST_NAME
    if pattern is R86_CANDIDATE_ID_PATTERN:
        return R86_MANIFEST_NAME
    if pattern is R87_CANDIDATE_ID_PATTERN:
        if candidate_id.endswith("CANDIDATE-8"):
            return R87_CANDIDATE_8_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-9"):
            return R87_CANDIDATE_9_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-10"):
            return R87_CANDIDATE_10_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-11"):
            return R87_CANDIDATE_11_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-12"):
            return R87_CANDIDATE_12_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-13"):
            return R87_CANDIDATE_13_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-14"):
            return R87_CANDIDATE_14_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-15"):
            return R87_CANDIDATE_15_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-16"):
            return R87_CANDIDATE_16_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-17"):
            return R87_CANDIDATE_17_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-18"):
            return R87_CANDIDATE_18_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-19"):
            return R87_CANDIDATE_19_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-20"):
            return R87_CANDIDATE_20_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-21"):
            return R87_CANDIDATE_21_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-22"):
            return R87_CANDIDATE_22_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-23"):
            return R87_CANDIDATE_23_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-24"):
            return R87_CANDIDATE_24_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-25"):
            return R87_CANDIDATE_25_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-26"):
            return R87_CANDIDATE_26_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-27"):
            return R87_CANDIDATE_27_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-7"):
            return R87_CANDIDATE_7_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-6"):
            return R87_CANDIDATE_6_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-5"):
            return R87_CANDIDATE_5_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-4"):
            return R87_CANDIDATE_4_MANIFEST_NAME
        if candidate_id.endswith("CANDIDATE-3"):
            return R87_CANDIDATE_3_MANIFEST_NAME
        return R87_MANIFEST_NAME
    raise DependencyManifestError("candidate_id has no canonical dependency manifest")


class DependencyManifestError(RuntimeError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


def _identity_canonical_bytes(value: Any) -> bytes:
    """Match build_retention._canonical_json for candidate/plan/G0C core IDs."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DependencyManifestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _binding_field(value: Mapping[str, Any]) -> str:
    if R87_BINDING_FIELD in value:
        return R87_BINDING_FIELD
    if R86_BINDING_FIELD in value:
        return R86_BINDING_FIELD
    if R81_BINDING_FIELD in value:
        return R81_BINDING_FIELD
    if R80_BINDING_FIELD in value:
        return R80_BINDING_FIELD
    if R79_BINDING_FIELD in value:
        return R79_BINDING_FIELD
    if R77_BINDING_FIELD in value:
        return R77_BINDING_FIELD
    if R78_BINDING_FIELD in value:
        return R78_BINDING_FIELD
    if R76_BINDING_FIELD in value:
        return R76_BINDING_FIELD
    if R75_BINDING_FIELD in value:
        return R75_BINDING_FIELD
    if R74_BINDING_FIELD in value:
        return R74_BINDING_FIELD
    if R73_BINDING_FIELD in value:
        return R73_BINDING_FIELD
    if R72_BINDING_FIELD in value:
        return R72_BINDING_FIELD
    candidate_id = value.get("candidate_id")
    if _candidate_id_pattern(candidate_id) is R74_CANDIDATE_ID_PATTERN:
        return R74_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R75_CANDIDATE_ID_PATTERN:
        return R75_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R76_CANDIDATE_ID_PATTERN:
        return R76_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R77_CANDIDATE_ID_PATTERN:
        return R77_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R78_CANDIDATE_ID_PATTERN:
        return R78_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R79_CANDIDATE_ID_PATTERN:
        return R79_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R80_CANDIDATE_ID_PATTERN:
        return R80_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R81_CANDIDATE_ID_PATTERN:
        return R81_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R86_CANDIDATE_ID_PATTERN:
        return R86_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R87_CANDIDATE_ID_PATTERN:
        return R87_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R73_CANDIDATE_ID_PATTERN:
        return R73_BINDING_FIELD
    if _candidate_id_pattern(candidate_id) is R72_CANDIDATE_ID_PATTERN:
        return R72_BINDING_FIELD
    return R71_BINDING_FIELD


def _manifest_envelope(value: Mapping[str, Any], *, role: str,
                       binding_field: str | None = None) -> Mapping[str, Any]:
    binding = value.get(binding_field or _binding_field(value))
    descriptor = binding.get(R71_MANIFEST_ENVELOPE_FIELD) if isinstance(binding, Mapping) else None
    if (not isinstance(descriptor, Mapping)
            or set(descriptor) != {"path", "sha256"}
            or descriptor.get("path") not in {
                MANIFEST_NAME, R72_MANIFEST_NAME, R73_MANIFEST_NAME, R74_MANIFEST_NAME,
                R75_MANIFEST_NAME, R76_MANIFEST_NAME, R77_MANIFEST_NAME, R78_MANIFEST_NAME,
                R79_MANIFEST_NAME,
                R80_MANIFEST_NAME,
                R81_MANIFEST_NAME,
                R86_MANIFEST_NAME,
                R87_MANIFEST_NAME,
                R87_CANDIDATE_3_MANIFEST_NAME,
                R87_CANDIDATE_4_MANIFEST_NAME,
                R87_CANDIDATE_5_MANIFEST_NAME,
                R87_CANDIDATE_6_MANIFEST_NAME,
                R87_CANDIDATE_7_MANIFEST_NAME,
                R87_CANDIDATE_8_MANIFEST_NAME,
                R87_CANDIDATE_9_MANIFEST_NAME,
                R87_CANDIDATE_10_MANIFEST_NAME,
                R87_CANDIDATE_11_MANIFEST_NAME,
                R87_CANDIDATE_12_MANIFEST_NAME,
                R87_CANDIDATE_13_MANIFEST_NAME,
                R87_CANDIDATE_14_MANIFEST_NAME,
                R87_CANDIDATE_15_MANIFEST_NAME,
                R87_CANDIDATE_16_MANIFEST_NAME,
                R87_CANDIDATE_17_MANIFEST_NAME,
                R87_CANDIDATE_18_MANIFEST_NAME,
                R87_CANDIDATE_19_MANIFEST_NAME,
                R87_CANDIDATE_20_MANIFEST_NAME,
                R87_CANDIDATE_21_MANIFEST_NAME,
                R87_CANDIDATE_22_MANIFEST_NAME,
                R87_CANDIDATE_23_MANIFEST_NAME,
                R87_CANDIDATE_24_MANIFEST_NAME,
                R87_CANDIDATE_25_MANIFEST_NAME,
                R87_CANDIDATE_26_MANIFEST_NAME,
                R87_CANDIDATE_27_MANIFEST_NAME,
            }
            or not isinstance(descriptor.get("sha256"), str)
            or len(descriptor["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in descriptor["sha256"])):
        raise DependencyManifestError(f"{role} lacks the exact detached manifest envelope")
    return descriptor


def _core_projection(value: Mapping[str, Any], *, role: str) -> dict[str, Any]:
    _manifest_envelope(value, role=role)
    projection = dict(value)
    binding_field = _binding_field(value)
    binding = dict(value[binding_field])
    binding.pop(R71_MANIFEST_ENVELOPE_FIELD)
    projection[binding_field] = binding
    return projection


def candidate_core_projection(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Candidate core removes only r71_binding.execution_dependency_manifest."""
    return _core_projection(candidate, role="candidate")


def g0c_core_projection(review: Mapping[str, Any]) -> dict[str, Any]:
    """G0C core removes only r71_binding.execution_dependency_manifest."""
    return _core_projection(review, role="G0C review")


def _candidate_core_sha256(candidate: Mapping[str, Any]) -> str:
    return _sha256(_identity_canonical_bytes(candidate_core_projection(candidate)))


def _g0c_core_sha256(review: Mapping[str, Any]) -> str:
    return _sha256(_identity_canonical_bytes(g0c_core_projection(review)))


def validate_candidate_g0c_binding(*, candidate: Mapping[str, Any],
                                   g0c_review: Mapping[str, Any],
                                   candidate_path: str,
                                   manifest_sha256: str) -> dict[str, str]:
    """Check both detached envelopes and the G0C-to-candidate core edge."""
    if (not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64
            or any(c not in "0123456789abcdef" for c in manifest_sha256)):
        raise DependencyManifestError("expected manifest digest is malformed")
    candidate_envelope = _manifest_envelope(candidate, role="candidate")
    g0c_binding_field = _binding_field(candidate)
    g0c_envelope = _manifest_envelope(
        g0c_review, role="G0C review", binding_field=g0c_binding_field)
    expected_envelope = {"path": manifest_name_for_candidate(candidate.get("candidate_id")),
                         "sha256": manifest_sha256}
    if dict(candidate_envelope) != expected_envelope or dict(g0c_envelope) != expected_envelope:
        raise DependencyManifestError("candidate and G0C envelopes do not bind the exact manifest")
    candidate_id = candidate.get("candidate_id")
    candidate_core_sha256 = _candidate_core_sha256(candidate)
    if (_candidate_id_pattern(candidate_id) is None
            or candidate_id != Path(candidate_path).stem
            or Path(candidate_path).name != f"{candidate_id}.json"):
        raise DependencyManifestError("candidate_id does not match an exact R71-R87 candidate path")
    g0c_binding = g0c_review.get(g0c_binding_field)
    candidate_descriptor = (g0c_binding.get("candidate")
                            if isinstance(g0c_binding, Mapping) else None)
    expected_candidate = {"path": candidate_path, "candidate_id": candidate_id,
                          "candidate_core_sha256": candidate_core_sha256}
    if candidate_descriptor != expected_candidate:
        raise DependencyManifestError("G0C core does not bind the exact candidate core")
    return {"candidate_id": candidate_id,
            "candidate_core_sha256": candidate_core_sha256,
            "g0c_sha256": _g0c_core_sha256(g0c_review)}


def _read_regular(path: Path) -> dict[str, Any]:
    resolved_before = str(path.resolve(strict=True))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DependencyManifestError(f"dependency is not a regular file: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        current = path.lstat()
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                           before.st_ctime_ns, before.st_mode, before.st_nlink)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                          after.st_ctime_ns, after.st_mode, after.st_nlink)
        identity_path = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                         current.st_ctime_ns, current.st_mode, current.st_nlink)
        if identity_before != identity_after or identity_after != identity_path:
            raise DependencyManifestError(f"dependency changed while being read: {path}")
        resolved_after = str(path.resolve(strict=True))
        if resolved_before != resolved_after:
            raise DependencyManifestError(f"dependency path resolution changed while being read: {path}")
        return {"kind": "file", "resolved_path": resolved_after,
                "device": before.st_dev, "inode": before.st_ino,
                "mode": stat.S_IMODE(before.st_mode), "links": before.st_nlink,
                "size_bytes": before.st_size, "allocated_bytes": before.st_blocks * 512,
                "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns,
                "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _read_regular_bytes(path: Path) -> bytes:
    """Read a regular file without following its final component or accepting races."""
    resolved_before = str(path.resolve(strict=True))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DependencyManifestError(f"dependency is not a regular file: {path}")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                                 info.st_ctime_ns, info.st_mode, info.st_nlink)
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise DependencyManifestError(f"dependency changed while being read: {path}")
        resolved_after = str(path.resolve(strict=True))
        if resolved_before != resolved_after:
            raise DependencyManifestError(f"dependency path resolution changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _reject_symlink_components(path: Path, *, role: str,
                               include_final: bool = True) -> None:
    """Reject lexical symlinks in a canonical path, optionally allowing its leaf."""
    system_prefix_aliases = {
        Path("/tmp"): Path("/private/tmp"),
        Path("/var"): Path("/private/var"),
    }
    absolute = Path(path)
    if (not absolute.is_absolute()
            or str(absolute) != os.path.normpath(str(absolute))
            or ".." in absolute.parts):
        raise DependencyManifestError(f"{role} root/path is not canonical absolute")
    components = absolute.parts[1:]
    cursor = Path(absolute.anchor)
    for index, component in enumerate(components):
        if not include_final and index == len(components) - 1:
            break
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except OSError as error:
            raise DependencyManifestError(
                f"{role} path has a missing or unreadable component: {cursor}") from error
        if stat.S_ISLNK(info.st_mode):
            expected_target = system_prefix_aliases.get(cursor)
            try:
                resolved_prefix = cursor.resolve(strict=True)
            except OSError:
                resolved_prefix = None
            if expected_target is not None and resolved_prefix == expected_target:
                continue
            raise DependencyManifestError(f"{role} path contains a symlink component: {cursor}")


def _repo_relative_file(repo: Path, relative: Any, *, role: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise DependencyManifestError(f"{role} path is malformed")
    rel = Path(relative)
    if (rel.is_absolute() or rel.as_posix() != relative
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise DependencyManifestError(f"{role} path is not canonical repository-relative")
    selected_root = Path(repo)
    _reject_symlink_components(selected_root, role=f"{role} root")
    path = selected_root / rel
    try:
        path.resolve(strict=True).relative_to(selected_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise DependencyManifestError(f"{role} path escapes the repository or is missing") from error
    _reject_symlink_components(path, role=role, include_final=False)
    return path


def _root_relative_file(root: Path, relative: Any, *, role: str) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise DependencyManifestError(f"{role} path is malformed")
    rel = Path(relative)
    if (rel.is_absolute() or rel.as_posix() != relative
            or any(part in {"", ".", ".."} for part in rel.parts)):
        raise DependencyManifestError(f"{role} path is not canonical root-relative")
    selected_root = Path(root)
    _reject_symlink_components(selected_root, role=f"{role} root")
    path = selected_root / rel
    try:
        path.resolve(strict=True).relative_to(selected_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise DependencyManifestError(f"{role} path escapes its root or is missing") from error
    _reject_symlink_components(path, role=role, include_final=False)
    return path


def resolve_manifest_path(repo: Path, relative: Any) -> Path:
    """Return one canonical in-repository R71-R87 manifest path."""
    if relative not in {MANIFEST_NAME, R72_MANIFEST_NAME, R73_MANIFEST_NAME,
                        R74_MANIFEST_NAME, R75_MANIFEST_NAME, R76_MANIFEST_NAME,
                        R77_MANIFEST_NAME, R78_MANIFEST_NAME, R79_MANIFEST_NAME,
                        R80_MANIFEST_NAME, R81_MANIFEST_NAME, R86_MANIFEST_NAME,
                        R87_MANIFEST_NAME, R87_CANDIDATE_3_MANIFEST_NAME,
                        R87_CANDIDATE_4_MANIFEST_NAME,
                        R87_CANDIDATE_5_MANIFEST_NAME,
                        R87_CANDIDATE_6_MANIFEST_NAME,
                        R87_CANDIDATE_7_MANIFEST_NAME,
                        R87_CANDIDATE_8_MANIFEST_NAME,
                        R87_CANDIDATE_9_MANIFEST_NAME,
                        R87_CANDIDATE_10_MANIFEST_NAME,
                        R87_CANDIDATE_11_MANIFEST_NAME,
                        R87_CANDIDATE_12_MANIFEST_NAME,
                        R87_CANDIDATE_13_MANIFEST_NAME,
                        R87_CANDIDATE_14_MANIFEST_NAME,
                        R87_CANDIDATE_15_MANIFEST_NAME,
                        R87_CANDIDATE_16_MANIFEST_NAME,
                        R87_CANDIDATE_17_MANIFEST_NAME,
                        R87_CANDIDATE_18_MANIFEST_NAME,
                        R87_CANDIDATE_19_MANIFEST_NAME,
                        R87_CANDIDATE_20_MANIFEST_NAME,
                        R87_CANDIDATE_21_MANIFEST_NAME,
                        R87_CANDIDATE_22_MANIFEST_NAME,
                        R87_CANDIDATE_23_MANIFEST_NAME,
                        R87_CANDIDATE_24_MANIFEST_NAME,
                        R87_CANDIDATE_25_MANIFEST_NAME,
                        R87_CANDIDATE_26_MANIFEST_NAME,
                        R87_CANDIDATE_27_MANIFEST_NAME}:
        raise DependencyManifestError(
            "dependency manifest path must be the canonical repository-relative filename")
    try:
        repo_root = Path(repo).resolve(strict=True)
    except OSError as error:
        raise DependencyManifestError("dependency manifest repository root is missing") from error
    path = _repo_relative_file(repo_root, relative, role="dependency manifest")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise DependencyManifestError("dependency manifest path is missing or unreadable") from error
    if resolved != path or not resolved.is_relative_to(repo_root):
        raise DependencyManifestError("dependency manifest path escapes or aliases its repository root")
    return path


def _tree_snapshot(root: Path, *, internal_symlinks: bool) -> dict[str, Any]:
    """Capture a sorted no-follow tree; unsupported objects and scan races fail closed."""
    root = Path(os.path.abspath(root))
    resolved_before = str(root.resolve(strict=True))
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise DependencyManifestError(f"dependency tree root is not a directory: {root}")
    rows: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        before = directory.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise DependencyManifestError(f"dependency directory changed type: {directory}")
        try:
            with os.scandir(directory) as scan:
                children = sorted(list(scan), key=lambda item: item.name)
        except OSError as error:
            raise DependencyManifestError(f"cannot enumerate dependency directory: {directory}") from error
        after = directory.lstat()
        if ((before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_mtime_ns,
             before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                                     after.st_mtime_ns, after.st_ctime_ns)):
            raise DependencyManifestError(f"dependency directory changed during enumeration: {directory}")
        names = [entry.name for entry in children]
        rows.append({"path": directory.relative_to(root).as_posix() or ".", "kind": "directory",
                     "device": before.st_dev, "inode": before.st_ino,
                     "mode": stat.S_IMODE(before.st_mode), "links": before.st_nlink,
                     "size_bytes": before.st_size, "allocated_bytes": before.st_blocks * 512,
                     "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns,
                     "children": names})
        for entry in reversed(children):
            path = Path(entry.path)
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            base = {"path": relative, "device": info.st_dev, "inode": info.st_ino,
                    "mode": stat.S_IMODE(info.st_mode), "links": info.st_nlink,
                    "size_bytes": info.st_size, "allocated_bytes": info.st_blocks * 512,
                    "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}
            if stat.S_ISDIR(info.st_mode):
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                base.update(_read_regular(path))
                base["path"] = relative
                rows.append(base)
            elif stat.S_ISLNK(info.st_mode) and internal_symlinks:
                base.update({"kind": "symlink", "target": os.readlink(path)})
                rows.append(base)
            else:
                raise DependencyManifestError(f"unsupported or forbidden dependency object: {path}")
    rows.sort(key=lambda row: row["path"])
    resolved_after = str(root.resolve(strict=True))
    if resolved_before != resolved_after:
        raise DependencyManifestError(f"dependency tree path resolution changed during scan: {root}")
    return {"kind": "tree_internal_symlinks" if internal_symlinks else "tree",
            "path": str(root), "resolved_path": resolved_after,
            "entry_count": len(rows), "entries": rows,
            "sha256": _sha256(_canonical_bytes(rows))}


def _snapshot_path(path_value: str, *, kind: str, role: str) -> dict[str, Any]:
    path = Path(path_value)
    if not path.is_absolute() or str(path) != str(Path(os.path.abspath(path))):
        raise DependencyManifestError(f"{role} path is not canonical absolute: {path_value}")
    if kind == "symlink":
        # A declared alias may occupy the leaf only. Reject arbitrary parent
        # aliases, while _reject_symlink_components permits exact macOS /tmp
        # and /var system-prefix aliases whose resolved targets are fixed.
        _reject_symlink_components(path, role=role, include_final=False)
    try:
        info = path.lstat()
    except OSError as error:
        raise DependencyManifestError(f"{role} path is missing or unreadable: {path_value}") from error
    if kind in {"tree", "tree_internal_symlinks"}:
        if stat.S_ISLNK(info.st_mode):
            raise DependencyManifestError(f"{role} tree root may not be a symlink: {path_value}")
        return {"role": role, **_tree_snapshot(path, internal_symlinks=kind == "tree_internal_symlinks")}
    if kind == "symlink":
        if not stat.S_ISLNK(info.st_mode):
            raise DependencyManifestError(f"{role} is not the declared symlink: {path_value}")
        target = os.readlink(path)
        resolved = str(path.resolve(strict=True))
        resolved_info = _read_regular(Path(resolved))
        return {"role": role, "kind": "symlink", "path": str(path), "target": target,
                "resolved_path": resolved, "resolved_identity": resolved_info}
    if kind == "file":
        if stat.S_ISLNK(info.st_mode):
            raise DependencyManifestError(f"{role} file may not be a symlink: {path_value}")
        return {"role": role, "path": str(path), **_read_regular(path)}
    raise DependencyManifestError(f"{role} has unsupported dependency kind: {kind}")


def current_gate_control_roster(repo: Path) -> dict[str, str]:
    """Return the exact fixed control roster from current regular-file bytes.

    This is for pre-Gate manifest construction only. Final validation must use
    the current Gate attestation map (after the caller validates Gate status,
    signature, and freshness), so a stale historical Gate cannot authorize a
    control set that differs from the one reviewed at admission.
    """
    result: dict[str, str] = {}
    for control_name in GATE_CONTROL_FILES:
        control_path = _repo_relative_file(
            repo, f"experiments/storage/{control_name}", role=f"Gate control:{control_name}")
        try:
            result[control_name] = _read_regular(control_path)["sha256"]
        except OSError as error:
            raise DependencyManifestError(
                f"current Gate control is unavailable: {control_name}") from error
    return result


def _validated_gate_control_roster(roster: Mapping[str, Any]) -> dict[str, str]:
    if set(roster) != set(GATE_CONTROL_FILES):
        raise DependencyManifestError("Gate control roster does not match the exact fixed control set")
    result: dict[str, str] = {}
    for name in GATE_CONTROL_FILES:
        digest = roster.get(name)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise DependencyManifestError(f"Gate control roster has an invalid digest: {name}")
        result[name] = digest
    return result


def build_manifest(*, candidate: Mapping[str, Any], plan: Mapping[str, Any],
                   plan_raw_sha256: str, plan_canonical_sha256: str, g0c_sha256: str,
                   candidate_path: str, plan_path: str, repo: Path,
                   gate_control_roster: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Construct the canonical closure object; callers publish its canonical bytes."""
    for label, digest in (("plan raw", plan_raw_sha256),
                          ("plan canonical", plan_canonical_sha256),
                          ("G0C core", g0c_sha256)):
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise DependencyManifestError(f"{label} identity is not a SHA-256 digest")
    build_graphs = plan.get("build_graphs")
    if (not isinstance(build_graphs, list) or len(build_graphs) != 2
            or any(not isinstance(row, Mapping) for row in build_graphs)):
        raise DependencyManifestError("plan does not contain exactly two graph descriptors")
    graph_ids = [row.get("graph_id") for row in build_graphs]
    if graph_ids != ["windows-product-r6a", "native-verification-r6a"]:
        raise DependencyManifestError("dependency manifest requires the ordered R6-A graph pair")
    graph_key = plan.get("build_configuration_key_sha256")
    if (not isinstance(graph_key, str) or len(graph_key) != 64
            or any(c not in "0123456789abcdef" for c in graph_key)):
        raise DependencyManifestError("plan lacks a canonical graph-pair configuration digest")
    entries: list[dict[str, Any]] = []
    if gate_control_roster is None:
        gate_path = _repo_relative_file(
            repo, "experiments/storage/storage-gate0-attestation.json", role="Gate attestation")
        try:
            gate_raw = _read_regular_bytes(gate_path)
            gate_payload = json.loads(gate_raw, object_pairs_hook=_json_object_without_duplicate_keys)
        except (OSError, TypeError, ValueError) as error:
            raise DependencyManifestError("current Gate attestation is unavailable or malformed") from error
        gate_controls = gate_payload.get("retention_control_sha256")
        if not isinstance(gate_controls, Mapping):
            raise DependencyManifestError("Gate attestation lacks the exact storage-control inventory")
        gate_controls = _validated_gate_control_roster(gate_controls)
    else:
        gate_controls = _validated_gate_control_roster(gate_control_roster)
    # Gate attestation bytes are mutable output: final Gate publication can
    # follow manifest/G0C-envelope construction. Bind its control inventory
    # here, and let the admission capability separately pin the exact final
    # Gate digest at ALLOW and each handoff boundary.
    for control_name in GATE_CONTROL_FILES:
        control_path = _repo_relative_file(
            repo, f"experiments/storage/{control_name}", role=f"Gate control:{control_name}")
        observed_control = {"role": f"gate_control:{control_name}",
                            "path": str(control_path), **_read_regular(control_path)}
        if observed_control.get("sha256") != gate_controls[control_name]:
            source = "explicit Gate control roster" if gate_control_roster is not None else "Gate-attested control"
            raise DependencyManifestError(f"{source} differs from current bytes: {control_name}")
        entries.append(observed_control)
    candidate_file_path = _repo_relative_file(repo, candidate_path, role="candidate")
    candidate_file_raw = _read_regular_bytes(candidate_file_path)
    try:
        disk_candidate = json.loads(candidate_file_raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError) as error:
        raise DependencyManifestError(f"candidate file is malformed: {error}") from error
    if disk_candidate != candidate:
        raise DependencyManifestError("candidate file differs from the parsed candidate core")
    candidate_id = candidate.get("candidate_id")
    if (_candidate_id_pattern(candidate_id) is None
            or candidate_id != Path(candidate_path).stem
            or Path(candidate_path).name != f"{candidate_id}.json"):
        raise DependencyManifestError("candidate_id does not match an exact R71-R87 candidate path")
    candidate_core_sha256 = _candidate_core_sha256(candidate)
    plan_file = _snapshot_path(
        str(_repo_relative_file(repo, plan_path, role="execution plan")),
        kind="file", role="execution_plan")
    if (plan_file.get("sha256") != plan_raw_sha256
            or _sha256(_identity_canonical_bytes(plan)) != plan_canonical_sha256):
        raise DependencyManifestError("plan file or parsed plan differs from its frozen identity")
    entries.append(plan_file)
    source = plan.get("source_identity")
    if not isinstance(source, Mapping) or not isinstance(source.get("root"), str):
        raise DependencyManifestError("plan lacks a source-root identity")
    from build_retention import _tree_hash
    try:
        source_hash, _source_count = _tree_hash(
            Path(source["root"]), reject_symlinks=True, reject_external_hardlinks=True)
    except Exception as error:
        raise DependencyManifestError(f"source tree cannot be safely fingerprinted: {error}") from error
    if source_hash != source.get("sha256"):
        raise DependencyManifestError("source tree differs from the plan's exact source identity")
    entries.append(_snapshot_path(source["root"], kind="tree_internal_symlinks", role="source_tree"))
    for name, row in sorted(plan.get("tools", {}).items()):
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
            raise DependencyManifestError(f"tool receipt is malformed: {name}")
        observed = _snapshot_path(row["path"], kind="file", role=f"tool:{name}")
        if observed.get("sha256") != row["sha256"]:
            raise DependencyManifestError(f"tool differs from its plan receipt: {name}")
        entries.append(observed)
    source_root = Path(source["root"])
    for index, row in enumerate(plan.get("input_receipts", [])):
        if not isinstance(row, Mapping):
            raise DependencyManifestError("plan input receipt is malformed")
        path = _root_relative_file(source_root, row.get("path"), role="plan input")
        observed = _snapshot_path(str(path), kind="file", role=f"plan_input:{index}:{row.get('path')}")
        if observed.get("sha256") != row.get("sha256"):
            raise DependencyManifestError(f"plan input differs from its receipt: {row.get('path')}")
        entries.append(observed)
    for row in plan.get("host_dependency_receipts", []):
        if not isinstance(row, Mapping):
            raise DependencyManifestError("host dependency receipt is malformed")
        observed = _snapshot_path(str(row.get("path", "")), kind=str(row.get("kind", "")),
                                  role=f"host_dependency:{row.get('input_id', '')}")
        if row.get("kind") in {"file", "symlink"} and observed.get("sha256", observed.get("resolved_identity", {}).get("sha256")) != row.get("sha256"):
            raise DependencyManifestError(f"host dependency differs from its receipt: {row.get('input_id')}")
        if row.get("kind") in {"tree", "tree_internal_symlinks"}:
            try:
                tree_hash, file_count = _tree_hash(
                    Path(str(row.get("path", ""))),
                    reject_symlinks=row.get("kind") == "tree",
                    require_internal_symlinks=row.get("kind") == "tree_internal_symlinks")
            except Exception as error:
                raise DependencyManifestError(
                    f"host dependency tree cannot be safely fingerprinted: {row.get('input_id')}") from error
            if (tree_hash != row.get("sha256")
                    or (row.get("file_count") is not None and row.get("file_count") != file_count)):
                raise DependencyManifestError(f"host dependency tree differs from its receipt: {row.get('input_id')}")
        if row.get("kind") == "symlink" and (observed.get("target") != row.get("target")
                                                  or observed.get("resolved_path") != row.get("resolved_path")):
            raise DependencyManifestError(f"host dependency alias differs from its receipt: {row.get('input_id')}")
        entries.append(observed)
    for row in plan.get("runtime_receipts", []):
        if not isinstance(row, Mapping):
            raise DependencyManifestError("runtime receipt is malformed")
        observed = _snapshot_path(str(row.get("path", "")), kind="file",
                                  role=f"runtime:{row.get('runtime_id', '')}")
        if observed.get("sha256") != row.get("sha256"):
            raise DependencyManifestError(f"runtime input differs from its receipt: {row.get('runtime_id')}")
        entries.append(observed)
    for group in ("changed_source_hashes", "changed_test_hashes"):
        rows = candidate.get(group)
        if not isinstance(rows, Mapping):
            raise DependencyManifestError(f"candidate lacks {group}")
        for relative in sorted(rows):
            candidate_file = _repo_relative_file(repo, relative, role=f"candidate {group}")
            observed = _snapshot_path(str(candidate_file), kind="file", role=f"candidate:{group}:{relative}")
            if observed.get("sha256") != rows[relative]:
                raise DependencyManifestError(f"candidate file differs from its frozen hash: {relative}")
            entries.append(observed)
    exact_diff = candidate.get("exact_diff")
    if not isinstance(exact_diff, Mapping) or not isinstance(exact_diff.get("path"), str):
        raise DependencyManifestError("candidate lacks exact diff identity")
    diff_path = _repo_relative_file(repo, exact_diff["path"], role="candidate diff")
    observed_diff = _snapshot_path(str(diff_path), kind="file", role="candidate:exact_diff")
    if observed_diff.get("sha256") != exact_diff.get("sha256"):
        raise DependencyManifestError("candidate diff differs from its frozen hash")
    entries.append(observed_diff)
    transaction_contracts = candidate.get("transaction_contracts")
    if not isinstance(transaction_contracts, Mapping):
        raise DependencyManifestError("candidate transaction-contract inventory is malformed")
    for relative in sorted(transaction_contracts):
        contract_path = _repo_relative_file(repo, relative, role="candidate contract")
        observed_contract = _snapshot_path(str(contract_path), kind="file", role=f"candidate:contract:{relative}")
        if observed_contract.get("sha256") != transaction_contracts[relative]:
            raise DependencyManifestError(f"candidate contract differs from its frozen hash: {relative}")
        entries.append(observed_contract)
    entries.sort(key=lambda row: (row["role"], row["path"]))
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "status": "FROZEN_DEPENDENCY_CLOSURE",
        "identity_rule": "candidate_and_g0c_envelopes_bind_manifest_raw_sha256; candidate_and_g0c_core_bind_each_other_without_envelopes",
        "candidate_path": candidate_path,
        "candidate_id": candidate_id,
        "candidate_core_sha256": candidate_core_sha256,
        "candidate_core_sha256_projection": f"canonical candidate JSON with only {_binding_field(candidate)}.execution_dependency_manifest removed",
        "plan_path": plan_path,
        "plan_raw_sha256": plan_raw_sha256,
        "plan_canonical_sha256": plan_canonical_sha256,
        "g0c_sha256": g0c_sha256,
        "g0c_sha256_projection": f"canonical G0C review JSON with only {_binding_field(candidate)}.execution_dependency_manifest removed",
        "graph_pair": graph_ids,
        "graph_pair_key": graph_key,
        "source_identity": dict(source),
        "dependency_rows": entries,
    }


def validate_manifest(*, manifest_path: Path, expected_sha256: str,
                      candidate: Mapping[str, Any], plan: Mapping[str, Any],
                      plan_raw_sha256: str, plan_canonical_sha256: str,
                      g0c_sha256: str,
                      candidate_path: str, plan_path: str, repo: Path,
                      gate_control_roster: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Verify canonical bytes, cross-bindings, and live closure contents fail-closed."""
    repo_absolute = Path(os.path.abspath(repo))
    input_absolute = Path(os.path.abspath(manifest_path))
    try:
        relative = input_absolute.relative_to(repo_absolute).as_posix()
        canonical_manifest_path = resolve_manifest_path(repo, relative)
    except (ValueError, DependencyManifestError) as error:
        raise DependencyManifestError("dependency manifest path is not canonical within the repository") from error
    try:
        input_resolved = input_absolute.resolve(strict=True)
    except OSError as error:
        raise DependencyManifestError("dependency manifest path is missing or unreadable") from error
    if input_resolved != canonical_manifest_path:
        raise DependencyManifestError("dependency manifest path is not canonical within the repository")
    try:
        raw = _read_regular_bytes(canonical_manifest_path)
    except OSError as error:
        raise DependencyManifestError(f"dependency manifest is unavailable: {manifest_path}") from error
    if _sha256(raw) != expected_sha256:
        raise DependencyManifestError("dependency manifest digest differs from candidate/plan binding")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise DependencyManifestError("dependency manifest is malformed") from error
    if _canonical_bytes(value) != raw:
        raise DependencyManifestError("dependency manifest is not canonical JSON")
    candidate_envelope = _manifest_envelope(candidate, role="candidate")
    if candidate_envelope.get("sha256") != expected_sha256:
        raise DependencyManifestError("candidate detached envelope does not bind these manifest bytes")
    expected = build_manifest(candidate=candidate, plan=plan,
                              plan_raw_sha256=plan_raw_sha256,
                              plan_canonical_sha256=plan_canonical_sha256,
                              g0c_sha256=g0c_sha256,
                              candidate_path=candidate_path, plan_path=plan_path, repo=repo,
                              gate_control_roster=gate_control_roster)
    if value != expected:
        raise DependencyManifestError("live dependency closure differs from the frozen manifest")
    return {"path": str(canonical_manifest_path), "sha256": expected_sha256,
            "candidate_id": expected["candidate_id"],
            "candidate_core_sha256": expected["candidate_core_sha256"],
            "plan_raw_sha256": expected["plan_raw_sha256"],
            "g0c_sha256": expected["g0c_sha256"],
            "graph_pair_key": expected["graph_pair_key"]}
