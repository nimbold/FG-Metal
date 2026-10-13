"""Process-owned Wine and dyld loader evidence for STEP 11D.3-B.

The parser deliberately binds Wine records to the Windows PID from APP_CONFIG,
dyld records to the supervised Unix PID, and runtime-version evidence to the
host-trace startup block for that same Unix PID.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


class LoaderEvidenceError(RuntimeError):
    """Raised when loader evidence is incomplete, ambiguous, or misbound."""


_STARTUP_ROLES = {
    "wine", "wine_launcher", "wineserver", "winevulkan_dll", "winevulkan_so",
    "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
}
_STARTUP_IDENTITY_KINDS = {
    "wine": "dyld-main-executable",
    "wine_launcher": "prefixlease-spawn-executable",
    "wineserver": "prefixlease-bound-executable",
    "winevulkan_dll": "win32-hmodule-proof-in-paired-app-trace",
    "winevulkan_so": "dyld-loaded-image",
    "wine_host_loader": "dyld-loaded-image",
    "winemac_so": "dyld-loaded-image",
    "trace_proxy": "dyld-loaded-image",
    "moltenvk": "dyld-loaded-image",
}
_EXPECTED_DYLD_ROLES = (
    "winevulkan_so", "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
)
_FORBIDDEN_MODULE_PARTS = (
    "dxvk", "dxgi", "d3d9", "d3d10", "d3d11", "d3d12", "fgmetalbridge",
    "rife", "framegen", "frame_generation", "fgmetal",
)
_BACKUP_SUFFIXES = (".bak", ".backup", ".old", ".orig", ".disabled", ".save", ".tmp", "~")
_WINE_DEBUG_PREFIX = re.compile(
    r"(?P<pid>[0-9a-fA-F]{1,8}):(?P<tid>[0-9a-fA-F]{1,8}):"
    r"trace:loaddll:(?P<category>[A-Za-z0-9_]+)\s+",
    re.IGNORECASE,
)
_WINE_LOADED = re.compile(
    r'^Loaded L"(?P<path>[^"\r\n]+)" at (?P<address>[0-9a-fA-F]+): (?P<kind>[A-Za-z0-9_-]+)$'
)
_DYLD_PREFIX = re.compile(r"^dyld\[(?P<pid>[0-9]+)\]:\s*(?P<body>.*)$")
_DYLD_LOADED = re.compile(
    r"^<(?P<uuid>[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})>\s+(?P<path>.+)$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fail(message: str) -> None:
    raise LoaderEvidenceError(message)


def _is_backup_path(parts: tuple[str, ...] | list[str], basename: str) -> bool:
    if any(part.casefold() in {"backup", "backups"} for part in parts):
        return True
    name = basename.casefold()
    return (name.endswith(_BACKUP_SUFFIXES)
            or ".backup." in name
            or ".bak." in name
            or re.search(r"(?:^|[._-])(?:backup|bak|old|orig|disabled)(?:[._-]|$)", name) is not None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(path_value: Any, expected_sha256: Any, label: str) -> Path:
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        _fail(f"{label} path is absent")
    if not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256):
        _fail(f"{label} SHA-256 is malformed")
    raw_path = str(path_value)
    path = Path(raw_path)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw_path):
        _fail(f"{label} path is not absolute and canonical: {raw_path!r}")
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} is missing, non-regular, or a symlink: {path_value}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise LoaderEvidenceError(f"{label} cannot be resolved: {path_value}") from error
    if resolved != path:
        _fail(f"{label} path traverses an ancestor symlink or is not canonical: {path_value}")
    if _sha256(resolved) != expected_sha256:
        _fail(f"{label} SHA-256 differs from the expected runtime identity")
    return resolved


def _canonical_absolute_path(path_value: Any, label: str) -> Path:
    """Validate an absolute canonical path, even after its target is removed."""
    if not isinstance(path_value, (str, Path)) or not str(path_value):
        _fail(f"{label} path is absent")
    raw_path = str(path_value)
    path = Path(raw_path)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw_path
            or path.resolve(strict=False) != path or path.is_symlink()):
        _fail(f"{label} path is not absolute, canonical, and symlink-free: {raw_path!r}")
    return path


def _decode_wine_path(escaped: str, line_no: int) -> str:
    """Decode raw quoted Wine debug text into a canonical Windows path.

    Wine prints a path separator as two backslashes in the log bytes. The
    expected path argument is already canonical and has one separator.
    """
    decoded: list[str] = []
    index = 0
    while index < len(escaped):
        char = escaped[index]
        if char == "\\":
            if index + 1 >= len(escaped) or escaped[index + 1] != "\\":
                _fail(f"Wine loader line {line_no} contains a malformed path escape")
            decoded.append("\\")
            index += 2
            continue
        if ord(char) < 0x20 or char == "\x7f":
            _fail(f"Wine loader line {line_no} contains a control character in its path")
        decoded.append(char)
        index += 1
    value = "".join(decoded)
    if "/" in value or not re.match(r"^[A-Za-z]:\\", value):
        _fail(f"Wine loader line {line_no} has a malformed Windows absolute path: {value!r}")
    segments = value[3:].split("\\")
    if any(segment in {"", ".", ".."} for segment in segments):
        _fail(f"Wine loader line {line_no} contains path traversal")
    if any(any(char in segment for char in '<>:"|?*') for segment in segments):
        _fail(f"Wine loader line {line_no} contains an invalid Windows path component")
    if any(segment.casefold() == "syswow64" for segment in segments):
        _fail(f"Wine loader line {line_no} loaded from SysWOW64")
    if _is_backup_path(segments, ntpath.basename(value)):
        _fail(f"Wine loader line {line_no} loaded a backup image")
    return value


def _canonical_windows_path(value: Any, label: str) -> str:
    """Normalize an already-decoded Windows path for case-insensitive compare."""
    if not isinstance(value, str):
        _fail(f"{label} is not a drive-qualified Windows path: {value!r}")
    # Expected paths may arrive as a quoted canonical string from config; log
    # paths are extracted from the Wine quotes before reaching this helper.
    if value.startswith('"') or value.endswith('"'):
        if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
            _fail(f"{label} has unmatched path quotes: {value!r}")
        value = value[1:-1]
    if not re.match(r"^[A-Za-z]:\\", value):
        _fail(f"{label} is not a drive-qualified Windows path: {value!r}")
    if "/" in value:
        _fail(f"{label} contains a non-Windows path separator")
    segments = value[3:].split("\\")
    if any(segment in {"", ".", ".."} for segment in segments):
        _fail(f"{label} contains path traversal")
    if any(any(char in segment for char in '<>:"|?*') for segment in segments):
        _fail(f"{label} contains an invalid Windows path component")
    if any(segment.casefold() == "syswow64" for segment in segments):
        _fail(f"{label} points into SysWOW64")
    if _is_backup_path(segments, ntpath.basename(value)):
        _fail(f"{label} points at a backup image")
    return value.casefold()


def _windows_path_for_basename(value: str) -> str:
    """Return the unquoted expected Windows path after its syntax was checked."""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _canonical_dyld_path(value: str, line_no: int) -> str:
    if not value or "\x00" in value or any(ord(char) < 0x20 for char in value):
        _fail(f"dyld line {line_no} contains a malformed image path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (not path.is_absolute() or ".." in raw_parts or "." in raw_parts
            or any(part == "" for part in raw_parts[1:])):
        _fail(f"dyld line {line_no} has a relative or traversing image path: {value!r}")
    if _is_backup_path(raw_parts[1:], path.name):
        _fail(f"dyld line {line_no} loaded a backup image")
    return str(path)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _manifest_prefix_dll_path(run_dir: Path) -> Path:
    manifest_path = run_dir / "run-manifest.json"
    _canonical_absolute_path(manifest_path, "run manifest")
    if manifest_path.is_symlink() or not manifest_path.is_file():
        _fail("run manifest is missing, non-regular, or a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"),
                              object_pairs_hook=_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise LoaderEvidenceError("run manifest is unreadable or malformed") from error
    if not isinstance(manifest, dict):
        _fail("run manifest is not a JSON object")
    prefix_root = _canonical_absolute_path(manifest.get("prefix_path"), "manifest prefix")
    prefix_dll = _canonical_absolute_path(
        manifest.get("prefix_winevulkan_path"), "manifest prefix winevulkan.dll")
    expected_dll = prefix_root / "drive_c/windows/system32/winevulkan.dll"
    if prefix_dll != expected_dll:
        _fail("manifest prefix DLL path is not the exact drive_c/windows/system32 image")
    return prefix_dll


def _load_target_startup(
        host_trace_path: Path,
        target_unix_pid: int,
        expected_module_identities: Mapping[str, Mapping[str, Any]],
        expected_moltenvk_version: str,
        expected_prefix_winevulkan_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    if isinstance(target_unix_pid, bool) or not isinstance(target_unix_pid, int) or target_unix_pid <= 0:
        _fail("target Unix PID must be a positive integer")
    if not isinstance(expected_module_identities, Mapping) or set(expected_module_identities) != _STARTUP_ROLES:
        _fail("expected runtime identity roles are incomplete or contain extras")
    expected: dict[str, dict[str, str]] = {}
    for role in sorted(_STARTUP_ROLES):
        identity = expected_module_identities[role]
        if not isinstance(identity, Mapping):
            _fail(f"expected {role} identity is malformed")
        if role == "winevulkan_dll":
            path = _canonical_absolute_path(identity.get("path"), role)
            digest = identity.get("sha256")
            if (path != expected_prefix_winevulkan_path
                    or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
                _fail("captured prefix winevulkan.dll identity differs from the manifest binding")
            expected[role] = {"path": str(path), "sha256": digest}
        else:
            path = _verified_file(identity.get("path"), identity.get("sha256"), role)
            expected[role] = {"path": str(path), "sha256": str(identity["sha256"])}

    trace_path = Path(host_trace_path)
    if not trace_path.is_absolute() or trace_path.resolve(strict=False) != trace_path:
        _fail(f"host startup trace path is not absolute and canonical: {trace_path}")
    if trace_path.is_symlink() or not trace_path.is_file():
        _fail(f"host startup trace is missing, non-regular, or a symlink: {trace_path}")
    try:
        lines = trace_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise LoaderEvidenceError("host startup trace is unreadable") from error

    events: list[dict[str, Any]] = []
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw, object_pairs_hook=_json_object)
        except (json.JSONDecodeError, ValueError) as error:
            raise LoaderEvidenceError(f"host startup trace line {line_no} is malformed JSON") from error
        if not isinstance(record, dict):
            _fail(f"host startup trace line {line_no} is not a JSON object")
        record["_line_no"] = line_no
        events.append(record)

    target_processes = [row for row in events
                        if row.get("origin") == "INSTRUMENTATION"
                        and row.get("event") == "process_identity"
                        and type(row.get("pid")) is int
                        and row.get("pid") == target_unix_pid]
    if len(target_processes) != 1:
        _fail(f"host trace must contain exactly one process identity for Unix PID {target_unix_pid}")
    process = target_processes[0]
    startup_id = f"pid-{target_unix_pid}"
    if (process.get("startup_id") != startup_id
            or process.get("process_architecture") != "x86_64"
            or type(process.get("process_translated")) is not int
            or process.get("process_translated") != 1
            or process.get("main_executable_path") != expected["wine"]["path"]
            or process.get("main_executable_sha256") != expected["wine"]["sha256"]
            or process.get("wine_launcher_path") != expected["wine_launcher"]["path"]
            or process.get("wine_launcher_sha256") != expected["wine_launcher"]["sha256"]):
        _fail("target process startup identity differs from the pinned Wine executable")

    startups = [row for row in events
                if row.get("origin") == "INSTRUMENTATION"
                and row.get("event") == "host_runtime_startup"
                and row.get("startup_id") == startup_id]
    if len(startups) != 1:
        _fail(f"host trace must contain exactly one runtime startup block for {startup_id}")
    startup = startups[0]
    if (startup.get("startup_valid") is not True
            or startup.get("process_architecture") != "x86_64"
            or type(startup.get("process_translated")) is not int
            or startup.get("process_translated") != 1
            or startup.get("moltenvk_version") != expected_moltenvk_version
            or startup.get("moltenvk_version_source") != "vkGetVersionStringsMVK from loaded delegate"
            or startup.get("moltenvk_sha256") != expected["moltenvk"]["sha256"]
            or startup.get("moltenvk_path") != expected["moltenvk"]["path"]
            or startup.get("trace_proxy_sha256") != expected["trace_proxy"]["sha256"]
            or startup.get("trace_proxy_path") != expected["trace_proxy"]["path"]
            or startup.get("winevulkan_so_sha256") != expected["winevulkan_so"]["sha256"]
            or startup.get("winevulkan_so_path") != expected["winevulkan_so"]["path"]
            or startup.get("wine_host_loader_sha256") != expected["wine_host_loader"]["sha256"]
            or startup.get("wine_host_loader_path") != expected["wine_host_loader"]["path"]
            or startup.get("host_loader_role")
                != "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan"
            or type(startup.get("loaded_moltenvk_count")) is not int
            or startup.get("loaded_moltenvk_count") != 1
            or type(startup.get("loaded_proxy_count")) is not int
            or startup.get("loaded_proxy_count") != 1
            or type(startup.get("loaded_winevulkan_count")) is not int
            or startup.get("loaded_winevulkan_count") != 1
            or type(startup.get("loaded_host_loader_count")) is not int
            or startup.get("loaded_host_loader_count") != 1
            or type(startup.get("loaded_libvulkan_dylib_count")) is not int
            or startup.get("loaded_libvulkan_dylib_count") != 0):
        _fail("target runtime startup trace has an invalid module identity or MoltenVK version")
    if startup.get("vulkan_runtime_version") != "1.4.357":
        _fail("target runtime startup trace has an unexpected Vulkan runtime version")

    startup_rows = [row for row in events
                    if row.get("origin") == "INSTRUMENTATION"
                    and row.get("event") == "startup_module_identity"
                    and row.get("startup_id") == startup_id]
    rows_by_role: dict[str, dict[str, Any]] = {}
    for row in startup_rows:
        role = row.get("role")
        if not isinstance(role, str) or role not in _STARTUP_ROLES or role in rows_by_role:
            _fail("target startup module identity block has an unknown or duplicate role")
        rows_by_role[role] = row
    if set(rows_by_role) != _STARTUP_ROLES:
        _fail("target startup module identity block is missing a required runtime role")
    for role, row in rows_by_role.items():
        identity = expected[role]
        loaded = role not in {"wine_launcher", "wineserver"}
        if (row.get("startup_phase") is not True
                or row.get("path") != identity["path"]
                or row.get("sha256") != identity["sha256"]
                or row.get("observed_path") != identity["path"]
                or row.get("observed_sha256") != identity["sha256"]
                or row.get("identity_match") is not True
                or row.get("loaded_in_process") is not loaded
                or row.get("identity_kind") != _STARTUP_IDENTITY_KINDS[role]):
            _fail(f"target startup identity for {role} differs from its pinned path or SHA-256")
    return startup, expected


def _validate_app_config(
        app_config: Mapping[str, Any],
        app_windows_pid: int,
        target_unix_pid: int,
        prefix_dll_path: Path,
        prefix_dll_sha256: str,
) -> None:
    if not isinstance(app_config, Mapping):
        _fail("target APP_CONFIG is malformed")
    if (app_config.get("origin") != "APP_CONFIG"
            or app_config.get("event") != "app_configuration"
            or type(app_config.get("process_id")) is not int
            or app_config.get("process_id") != app_windows_pid
            or type(app_config.get("target_unix_pid")) is not int
            or app_config.get("target_unix_pid") != target_unix_pid):
        _fail("APP_CONFIG process identity differs from the supervised Wine process")
    if (app_config.get("host_runtime_startup_gate_passed") is not True
            or app_config.get("winevulkan_dll_module_loaded") is not True
            or app_config.get("winevulkan_dll_before_frame_loop") is not True):
        _fail("APP_CONFIG lacks successful paired-app winevulkan.dll startup proof")
    if (_canonical_windows_path(app_config.get("winevulkan_dll_module_path"),
                                "APP_CONFIG winevulkan.dll module path")
            != _canonical_windows_path(r"C:\windows\system32\winevulkan.dll",
                                       "expected system32 winevulkan.dll")):
        _fail("APP_CONFIG does not prove the exact system32 winevulkan.dll HMODULE")
    app_dll_path = _canonical_absolute_path(
        app_config.get("winevulkan_dll_host_path"), "APP_CONFIG prefix winevulkan.dll")
    digest = app_config.get("winevulkan_dll_sha256")
    if (app_dll_path != prefix_dll_path or digest != prefix_dll_sha256
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
        _fail("APP_CONFIG prefix DLL path or SHA-256 differs from the captured manifest identity")


def parse_loader_evidence(
        log_path: Path | str,
        run_dir: Path | str,
        prefix_winevulkan_identity: Mapping[str, Any],
        *,
        target_unix_pid: int,
        app_windows_pid: int,
        host_trace_path: Path | str,
        expected_module_identities: Mapping[str, Mapping[str, Any]],
        expected_app_path: Path | str,
        expected_app_sha256: str,
        app_windows_path: str,
        expected_moltenvk_version: str,
        expected_runtime_winevulkan_path: Path | str,
        app_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse loader rows owned by the target Wine process and host process.

    `app_windows_pid` is the process ID reported in the application's APP_CONFIG
    row. `target_unix_pid` must come from the supervised Wine spawn wrapper.
    The host startup trace is the only source used for the target MoltenVK version.
    The cloned prefix DLL may have been removed by PrefixLease cleanup before
    parsing. Its canonical manifest path and captured hash must still match the
    paired APP_CONFIG and startup trace, while the retained runtime DLL is
    independently verified on disk against that same hash.
    """
    if isinstance(app_windows_pid, bool) or not isinstance(app_windows_pid, int) or app_windows_pid <= 0:
        _fail("app Windows PID must be a positive integer")
    run = _canonical_absolute_path(run_dir, "run directory")
    if run.is_symlink() or not run.is_dir():
        _fail(f"run directory is missing, non-directory, or a symlink: {run}")
    try:
        run = run.resolve(strict=True)
    except OSError as error:
        raise LoaderEvidenceError("run directory cannot be resolved") from error

    manifest_prefix_dll_path = _manifest_prefix_dll_path(run)
    if not isinstance(prefix_winevulkan_identity, Mapping):
        _fail("captured prefix winevulkan.dll identity is malformed")
    captured_prefix_dll_path = _canonical_absolute_path(
        prefix_winevulkan_identity.get("path"), "captured prefix winevulkan.dll")
    captured_prefix_dll_sha256 = prefix_winevulkan_identity.get("sha256")
    if (captured_prefix_dll_path != manifest_prefix_dll_path
            or not isinstance(captured_prefix_dll_sha256, str)
            or not _SHA256.fullmatch(captured_prefix_dll_sha256)):
        _fail("captured prefix winevulkan.dll identity differs from the run manifest")
    if (not isinstance(expected_module_identities, Mapping)
            or not isinstance(expected_module_identities.get("winevulkan_dll"), Mapping)):
        _fail("expected runtime winevulkan.dll identity is missing")
    expected_prefix_identity = expected_module_identities["winevulkan_dll"]
    expected_prefix_path = _canonical_absolute_path(
        expected_prefix_identity.get("path"), "expected prefix winevulkan.dll")
    if (expected_prefix_path != manifest_prefix_dll_path
            or expected_prefix_identity.get("sha256") != captured_prefix_dll_sha256):
        _fail("expected prefix DLL identity differs from captured run-manifest identity")
    prefix_tail = tuple(part.casefold() for part in manifest_prefix_dll_path.parts[-4:])
    if prefix_tail != ("drive_c", "windows", "system32", "winevulkan.dll"):
        _fail("materialized Wine DLL is not under the prefix drive_c/windows/system32 directory")
    _verified_file(expected_runtime_winevulkan_path, captured_prefix_dll_sha256,
                   "retained runtime winevulkan.dll")
    _validate_app_config(app_config, app_windows_pid, target_unix_pid,
                         manifest_prefix_dll_path, captured_prefix_dll_sha256)
    startup, expected = _load_target_startup(
        Path(host_trace_path), target_unix_pid, expected_module_identities,
        expected_moltenvk_version, manifest_prefix_dll_path)

    log = Path(log_path)
    if not log.is_absolute() or log.resolve(strict=False) != log:
        _fail(f"Wine runtime log path is not absolute and canonical: {log}")
    if log.is_symlink() or not log.is_file():
        _fail(f"Wine runtime log is missing, non-regular, or a symlink: {log}")
    try:
        text = log.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise LoaderEvidenceError("Wine runtime log is unreadable") from error

    wine_rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "trace:loaddll:" not in line.casefold():
            continue
        prefix = _WINE_DEBUG_PREFIX.search(line)
        if prefix is None:
            _fail(f"Wine loader line {line_no} lacks a pid:tid debug prefix")
        pid = int(prefix.group("pid"), 16)
        tid = int(prefix.group("tid"), 16)
        if pid <= 0 or tid <= 0:
            _fail(f"Wine loader line {line_no} has an invalid PID or TID")
        if pid != app_windows_pid:
            continue
        category = prefix.group("category").casefold()
        if category in {"free_modref", "free_dll"}:
            continue
        if category not in {"build_module", "build_ntdll_module"}:
            _fail(f"target Wine loader line {line_no} has an unexpected record type")
        message = line[prefix.end():]
        match = _WINE_LOADED.fullmatch(message)
        if match is None:
            _fail(f"target Wine loader line {line_no} is malformed")
        path = _decode_wine_path(match.group("path"), line_no)
        load_kind = match.group("kind").casefold()
        if load_kind not in {"builtin", "native"}:
            _fail(f"target Wine loader line {line_no} has an unsupported load kind")
        wine_rows.append({
            "path": path,
            "line": line.strip(),
            "pid": pid,
            "tid": tid,
            "load_kind": load_kind,
        })
    if not wine_rows:
        _fail(f"Wine runtime log has no loader records for Windows PID {app_windows_pid}")

    seen_wine_paths: set[str] = set()
    seen_wine_basenames: set[str] = set()
    for row in wine_rows:
        path_key = _canonical_windows_path(row["path"], "target Wine module path")
        base_key = ntpath.basename(row["path"]).casefold()
        if path_key in seen_wine_paths:
            _fail(f"duplicate target Wine module path: {row['path']}")
        if base_key in seen_wine_basenames:
            _fail(f"conflicting target Wine module basename: {base_key}")
        seen_wine_paths.add(path_key)
        seen_wine_basenames.add(base_key)

    dll_path_expected = _canonical_windows_path(
        r"C:\windows\system32\winevulkan.dll", "expected builtin winevulkan.dll")
    dll_rows = [row for row in wine_rows
                if ntpath.basename(row["path"]).casefold() == "winevulkan.dll"]
    if (len(dll_rows) != 1 or _canonical_windows_path(dll_rows[0]["path"], "winevulkan.dll")
            != dll_path_expected or dll_rows[0]["load_kind"] != "builtin"):
        _fail("target Wine process did not load exactly the builtin C:\\windows\\system32\\winevulkan.dll")

    expected_app_path_key = _canonical_windows_path(app_windows_path, "expected native PE app path")
    app_rows = [row for row in wine_rows
                if _canonical_windows_path(row["path"], "target Wine module path") == expected_app_path_key]
    if len(app_rows) != 1 or app_rows[0]["load_kind"] != "native":
        _fail("target Wine loader record does not identify the exact native PE app path")
    if any(ntpath.basename(row["path"]).casefold()
           == ntpath.basename(_windows_path_for_basename(app_windows_path)).casefold()
           for row in wine_rows if row not in app_rows):
        _fail("target Wine process loaded a conflicting app executable path")

    expected_run_paths = {
        "trace_proxy": str(run / "trace-loader/libMoltenVK.dylib"),
        "moltenvk": str(run / "mvk/libMoltenVK.1.dylib"),
    }
    for role, path in expected_run_paths.items():
        if expected[role]["path"] != path:
            _fail(f"selected {role} image is outside its exact run artifact path")
    app_path = _verified_file(expected_app_path, expected_app_sha256, "app")

    expected_native_paths: dict[str, str] = {}
    for role in _EXPECTED_DYLD_ROLES:
        expected_native_paths[role] = expected[role]["path"]

    dyld_rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "dyld[" not in line or ">" not in line:
            continue
        prefix = _DYLD_PREFIX.match(line)
        if prefix is None:
            _fail(f"dyld loader line {line_no} has a malformed process prefix")
        pid = int(prefix.group("pid"), 10)
        body = prefix.group("body")
        loaded_match = _DYLD_LOADED.fullmatch(body)
        if pid != target_unix_pid:
            continue
        if loaded_match is None:
            _fail(f"target dyld loader line {line_no} is malformed")
        path = _canonical_dyld_path(loaded_match.group("path"), line_no)
        dyld_rows.append({"path": path, "line": line.strip(), "pid": pid,
                          "uuid": loaded_match.group("uuid")})

    if not dyld_rows:
        _fail(f"dyld runtime log has no loaded images for Unix PID {target_unix_pid}")
    wine_host_expected = expected.get("wine", {}).get("path")
    if wine_host_expected:
        wine_start_idx = next(
            (i for i, r in enumerate(dyld_rows)
             if r["path"].casefold() == wine_host_expected.casefold()),
            None,
        )
        if wine_start_idx is not None:
            dyld_rows = dyld_rows[wine_start_idx:]
    seen_dyld_paths: set[str] = set()
    seen_dyld_uuids: dict[str, str] = {}
    for row in dyld_rows:
        key = row["path"].casefold()
        if key in seen_dyld_paths:
            _fail(f"duplicate target dyld image path: {row['path']}")
        uuid = row["uuid"].casefold()
        prior_path = seen_dyld_uuids.setdefault(uuid, row["path"])
        if prior_path != row["path"]:
            if {prior_path.casefold(), row["path"].casefold()} == {"/usr/lib/liboah.dylib", "/usr/lib/librosetta.dylib"}:
                pass
            else:
                _fail(f"conflicting target dyld image UUID {uuid}: {prior_path} and {row['path']}")
        seen_dyld_paths.add(key)

    forbidden: list[str] = []
    for row in wine_rows:
        name = ntpath.basename(row["path"]).casefold()
        if any(part in name for part in _FORBIDDEN_MODULE_PARTS):
            forbidden.append(row["path"])
    for row in dyld_rows:
        name = PurePosixPath(row["path"]).name.casefold()
        if any(part in name for part in _FORBIDDEN_MODULE_PARTS):
            forbidden.append(row["path"])
    if forbidden:
        _fail(f"forbidden D3D/DXVK/bridge/frame-generation module was loaded: {forbidden}")

    for role, expected_path in expected_native_paths.items():
        loaded = [row["path"] for row in dyld_rows
                  if PurePosixPath(row["path"]).name.casefold()
                  == PurePosixPath(expected_path).name.casefold()]
        if loaded != [expected_path]:
            _fail(f"target dyld log does not show exactly one pinned {role} image: {loaded}")
    if any(PurePosixPath(row["path"]).name.casefold() == "libvulkan.dylib" for row in dyld_rows):
        _fail("target process loaded an alternate libvulkan.dylib")

    version = startup["moltenvk_version"]
    version_match = re.fullmatch(r"MoltenVK v([0-9]+(?:\.[0-9]+)+)", version)
    if version_match is None:
        _fail("target startup trace contains a malformed MoltenVK runtime version")
    runtime_version_rows = [{
        "moltenvk_version": version_match.group(1),
        "vulkan_version": startup["vulkan_runtime_version"],
    }]

    runtime_identities: dict[str, dict[str, Any]] = {
        role: dict(identity) for role, identity in expected.items()
    }
    runtime_identities["winevulkan_dll"]["load_kind"] = "builtin"
    runtime_identities["wine_launcher"]["load_kind"] = "prefixlease-launcher"
    runtime_identities["moltenvk"]["version"] = expected_moltenvk_version
    runtime_identities["app"] = {"path": str(app_path), "sha256": expected_app_sha256}

    expected_mvk_path = expected["moltenvk"]["path"]
    expected_proxy_path = expected["trace_proxy"]["path"]
    return {
        "winevulkan_dll_loaded": True,
        "winevulkan_dll_reported_path": dll_rows[0]["path"],
        "winevulkan_dll_load_kind": "builtin",
        "winevulkan_dll_path": str(manifest_prefix_dll_path),
        "winevulkan_dll_sha256": expected["winevulkan_dll"]["sha256"],
        "winevulkan_so_loaded": True,
        "winevulkan_so_path": expected["winevulkan_so"]["path"],
        "winevulkan_so_sha256": expected["winevulkan_so"]["sha256"],
        "wine_host_loader_path": expected["wine_host_loader"]["path"],
        "wine_host_loader_sha256": expected["wine_host_loader"]["sha256"],
        "wine_host_loader_role": "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib",
        "khronos_loader_loaded": False,
        "moltenvk_loaded_path": expected_mvk_path,
        "moltenvk_sha256": expected["moltenvk"]["sha256"],
        "moltenvk_version": expected_moltenvk_version,
        "moltenvk_version_source": startup["moltenvk_version_source"],
        "moltenvk_runtime_banners": runtime_version_rows,
        "moltenvk_loaded_paths": [expected_proxy_path, expected_mvk_path],
        "trace_proxy_loaded_path": expected_proxy_path,
        "trace_proxy_sha256": expected["trace_proxy"]["sha256"],
        "runtime_identities": runtime_identities,
        "wine_module_records": wine_rows,
        "dyld_loaded_paths": [row["path"] for row in dyld_rows],
        "forbidden_module_names": forbidden,
        "target_unix_pid": target_unix_pid,
        "target_windows_pid": app_windows_pid,
        "moltenvk_startup_id": startup["startup_id"],
        "moltenvk_vulkan_runtime_version": startup["vulkan_runtime_version"],
    }
