#!/usr/bin/env python3
"""Create disposable R65 dual-graph configure and shader-only identity evidence.

This tool runs only the two exact Meson setup commands, the informational
``ninja -t commands`` query, and the exact pinned glslang generator commands
derived from that query. It never invokes a Ninja build target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/storage"))
import build_retention

CONFIGURE_TIMEOUT_SECONDS = 1200
QUERY_TIMEOUT_SECONDS = 60
GENERATOR_TIMEOUT_SECONDS = 90
MAX_OUTPUT_FILE_BYTES = 1024 * 1024 * 1024
MAX_GENERATED_FILE_BYTES = 64 * 1024 * 1024
RUN_KEY_PATTERN = re.compile(r"202[0-9]{5}T[0-9]{6}Z\Z")
GRAPH_IDS = tuple(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS)


def _canonical(value: Any) -> bytes:
    return build_retention._canonical_json(value)


def _sha256(path: Path) -> str:
    return build_retention._hash_path(path)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to overwrite prebuild evidence: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short evidence write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _limits(cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def _run_bounded(command: list[str], *, cwd: Path, environment: Mapping[str, str],
                 stdout_path: Path, timeout_seconds: int, cpu_seconds: int) -> dict[str, Any]:
    if not command or not all(isinstance(item, str) and item for item in command):
        raise RuntimeError("prebuild action has malformed argv")
    stdout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stdout_path.exists() or stdout_path.is_symlink():
        raise RuntimeError(f"refusing to overwrite action output: {stdout_path}")
    fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    start = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
            stdout=fd, stderr=subprocess.STDOUT, close_fds=True,
            start_new_session=True, preexec_fn=lambda: _limits(cpu_seconds))
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            return_code = process.wait()
    finally:
        os.fsync(fd)
        os.close(fd)
    elapsed = round(time.monotonic() - start, 6)
    if timed_out or return_code != 0:
        raise RuntimeError(
            f"bounded prebuild command failed or timed out ({return_code}, {elapsed}s): {command!r}; "
            f"log={stdout_path}")
    info = stdout_path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("prebuild command log is not a private regular file")
    return {"returncode": return_code, "elapsed_seconds": elapsed,
            "stdout_sha256": _sha256(stdout_path), "stdout_size_bytes": info.st_size}


def _plan_environment(plan: Mapping[str, Any], declaration: Mapping[str, Any],
                      disposable_root: Path) -> dict[str, str]:
    environment = plan.get("environment")
    if not isinstance(environment, Mapping):
        raise RuntimeError("plan has no exact Meson environment")
    selected = {key: str(environment[key]) for key in ("PATH", "LANG", "LC_ALL")
                if isinstance(environment.get(key), str)}
    if set(selected) != {"PATH", "LANG", "LC_ALL"}:
        raise RuntimeError("plan environment lacks its exact PATH and locale")
    graph_id = declaration["graph_id"]
    tools = plan.get("tools")
    if not isinstance(tools, Mapping):
        raise RuntimeError("plan lacks pinned graph tools")
    if declaration.get("graph_role") == "WINDOWS_PRODUCT":
        selected.update({"CC": tools["mingw_cc"]["path"],
                         "CXX": tools["mingw_cxx"]["path"],
                         "AR": tools["mingw_ar"]["path"],
                         "CC_FOR_BUILD": tools["native_cc"]["path"],
                         "CXX_FOR_BUILD": tools["native_cxx"]["path"],
                         "AR_FOR_BUILD": tools["native_ar"]["path"]})
    elif declaration.get("graph_role") == "NATIVE_VERIFICATION":
        profile = plan.get("native_host_profile")
        if not isinstance(profile, Mapping):
            raise RuntimeError("native configure lacks its pinned SDK profile")
        selected.update({
            "DEVELOPER_DIR": str(profile["developer_directory"]),
            "SDKROOT": str(profile["sdk_resolved_path"]),
            "CC": tools["native_cc"]["path"],
            "CXX": tools["native_cxx"]["path"],
            "AR": tools["native_ar"]["path"],
            "RANLIB": tools["native_ranlib"]["path"],
        })
        pkgconfig = next((row for row in plan.get("host_dependency_receipts", [])
                          if isinstance(row, Mapping) and row.get("input_id") == "SDL2_PKGCONFIG"), None)
        if not isinstance(pkgconfig, Mapping):
            raise RuntimeError("native configure lacks its pinned SDL pkg-config receipt")
        selected["PKG_CONFIG_PATH"] = str(Path(str(pkgconfig["path"])).parent)
    else:
        raise RuntimeError(f"unauthorized prebuild graph role: {graph_id}")
    home = disposable_root / "home" / graph_id
    temp = disposable_root / "tmp" / graph_id
    for path in (home, temp):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected.update({"HOME": str(home), "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp),
                     "XDG_CACHE_HOME": str(home / ".cache"),
                     "XDG_CONFIG_HOME": str(home / ".config"),
                     "XDG_DATA_HOME": str(home / ".local/share")})
    return selected


def _safe_build_path(build_dir: Path, value: str, label: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else build_dir / path).resolve(strict=False)
    root = build_dir.resolve(strict=True)
    if resolved == root or root not in resolved.parents:
        raise RuntimeError(f"shader generator {label} escapes its disposable build directory")
    return resolved


def _safe_source_path(source_root: Path, build_dir: Path, value: str) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else build_dir / path).resolve(strict=True)
    root = source_root.resolve(strict=True)
    if resolved == root or root not in resolved.parents or not resolved.is_file():
        raise RuntimeError("shader generator input is outside the frozen source tree")
    return resolved


def _parse_generator_command(line: str, *, tool_path: str, build_dir: Path,
                             source_root: Path) -> dict[str, Any] | None:
    try:
        argv = shlex.split(line, posix=True)
    except ValueError as error:
        raise RuntimeError(f"Ninja command query returned an unparseable command: {error}") from error
    if not argv or argv[0] != tool_path:
        if "glslang" in line.casefold():
            raise RuntimeError("Ninja declared an unpinned or wrapped glslang action")
        return None
    if any(any(char in token for char in ";&|<>`\n\r") for token in argv):
        raise RuntimeError("shader generator argv contains shell control syntax")
    if len(argv) != 11 or argv[1:5] != ["--quiet", "--target-env", "vulkan1.3", "--vn"]:
        raise RuntimeError("glslang command differs from the exact release generator signature")
    if argv[6] != "--depfile" or argv[9] != "-o":
        raise RuntimeError("glslang command has an unexpected depfile or output argument")
    basename, depfile_arg, input_arg, output_arg = argv[5], argv[7], argv[8], argv[10]
    source = _safe_source_path(source_root, build_dir, input_arg)
    output = _safe_build_path(build_dir, output_arg, "output")
    depfile = _safe_build_path(build_dir, depfile_arg, "depfile")
    if not basename or output.name != f"{source.stem}.h" or basename != source.stem:
        raise RuntimeError("glslang output basename differs from the exact input shader")
    if depfile != output.with_name(output.name + ".d"):
        raise RuntimeError("glslang depfile is not the paired generated-header depfile")
    return {"argv": argv, "source": source, "output": output, "depfile": depfile}


def _regular_receipt(path: Path, *, max_bytes: int) -> dict[str, Any]:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or info.st_size > max_bytes):
        raise RuntimeError(f"prebuild output is not a bounded private file: {path}")
    return {"sha256": _sha256(path), "size_bytes": info.st_size}


def _inventory(root: Path) -> list[dict[str, Any]]:
    rows = []
    observed: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(dirnames):
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError("prebuild evidence contains a linked or special directory")
        for name in filenames:
            path = base / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("prebuild evidence contains a linked or special file")
            relative = path.relative_to(root).as_posix()
            if relative in observed:
                raise RuntimeError("prebuild evidence inventory contains duplicate paths")
            observed.add(relative)
            rows.append({"path": relative, "sha256": _sha256(path), "size_bytes": info.st_size})
    return sorted(rows, key=lambda row: row["path"])


def create_prebuild_evidence(plan: Mapping[str, Any], *, run_key: str,
                             repository: Path = REPO) -> dict[str, Any]:
    """Configure both graphs in disposable roots and execute only glslang generators."""
    if not RUN_KEY_PATTERN.fullmatch(run_key):
        raise RuntimeError("prebuild run key must be a fresh UTC YYYYMMDDTHHMMSSZ value")
    if plan.get("build_graphs") is None or plan.get("build_type") not in {"release", "debug"}:
        raise RuntimeError("prebuild input plan lacks its exact dual-graph declaration")
    if [row.get("graph_id") for row in plan["build_graphs"]] != list(GRAPH_IDS):
        raise RuntimeError("prebuild input does not contain the canonical ordered graph pair")
    repository = repository.resolve(strict=True)
    artifact_root = repository / "experiments/storage/build-manifests" / (
        f"{build_retention.BUILD_SESSION_PREBUILD_ARTIFACT_PREFIX}-{run_key}")
    if artifact_root.exists() or artifact_root.is_symlink():
        raise RuntimeError(f"prebuild run root already exists: {artifact_root}")
    artifact_root.mkdir(mode=0o700, parents=True)
    disposable_root = artifact_root / "disposable"
    evidence_root = artifact_root / "evidence"
    disposable_root.mkdir(mode=0o700)
    evidence_root.mkdir(mode=0o700)
    source_root = Path(str(plan["source_identity"]["root"])).resolve(strict=True)
    if not (source_root / "meson.build").is_file():
        raise RuntimeError("frozen source root has no Meson project")
    tools = plan["tools"]
    for tool_id in ("meson", "ninja", "native_glslang"):
        receipt = tools.get(tool_id)
        if (not isinstance(receipt, Mapping) or set(receipt) != {"path", "sha256"}
                or _sha256(Path(str(receipt["path"]))) != receipt["sha256"]):
            raise RuntimeError(f"prebuild tool receipt is stale or malformed: {tool_id}")
    graph_rows: list[dict[str, Any]] = []
    configure_receipts: list[dict[str, Any]] = []
    generator_receipts: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    compiler_probe_receipts: list[dict[str, Any]] = []

    for declaration in plan["build_graphs"]:
        graph_id = str(declaration["graph_id"])
        build_dir = disposable_root / graph_id
        graph_evidence = evidence_root / graph_id
        graph_evidence.mkdir(mode=0o700)
        env = _plan_environment(plan, declaration, disposable_root)
        configure_argv = build_retention._dual_graph_configure_command(plan, declaration, build_dir)
        configure_log_rel = f"{graph_id}/preflight/meson-setup.stdout.log"
        configure_log_path = evidence_root / configure_log_rel
        configure = _run_bounded(
            configure_argv, cwd=source_root, environment=env,
            stdout_path=configure_log_path, timeout_seconds=CONFIGURE_TIMEOUT_SECONDS,
            cpu_seconds=CONFIGURE_TIMEOUT_SECONDS)
        meson_log = build_dir / "meson-logs/meson-log.txt"
        if not meson_log.is_file() or meson_log.is_symlink():
            raise RuntimeError(f"Meson setup produced no regular configure log for {graph_id}")
        meson_log_text = meson_log.read_text(errors="replace")
        probe_lines = [
            {"line_number": index, "text": line}
            for index, line in enumerate(meson_log_text.splitlines(), start=1)
            if re.search(r"sanity testing|checking if .*supports|checking for .*compiler|"
                         r"running (?:compile|link):|command line:|detecting linker via:|"
                         r"compiler for .*|linker for .*|sanity check:|dependency .* found",
                         line, re.IGNORECASE)
        ]
        if not probe_lines:
            raise RuntimeError(f"Meson log has no captured compiler/dependency configure checks: {graph_id}")
        probe_rel = f"{graph_id}/preflight/meson-configure-probes.json"
        probe_bytes = _canonical({"graph_id": graph_id, "meson_log_sha256": _sha256(meson_log),
                                  "lines": probe_lines}) + b"\n"
        _write(evidence_root / probe_rel, probe_bytes)
        configure_receipts.append({
            "graph_id": graph_id, "command": configure_argv, "cwd": str(source_root),
            "returncode": configure["returncode"],
            "timeout_seconds": CONFIGURE_TIMEOUT_SECONDS,
            "cpu_limit_seconds": CONFIGURE_TIMEOUT_SECONDS,
            "stdout_path": configure_log_rel, "stdout_sha256": configure["stdout_sha256"],
            "meson_log_sha256": _sha256(meson_log),
            "probe_summary_path": probe_rel,
            "probe_summary_sha256": hashlib.sha256(probe_bytes).hexdigest(),
            "probe_line_count": len(probe_lines),
        })
        compiler_probe_receipts.append({"graph_id": graph_id, "path": probe_rel,
                                        "sha256": hashlib.sha256(probe_bytes).hexdigest()})

        ninja_query = [str(tools["ninja"]["path"]), "-C", str(build_dir), "-t", "commands"]
        query_rel = f"{graph_id}/preflight/ninja-commands-query.txt"
        query_path = evidence_root / query_rel
        query = _run_bounded(ninja_query, cwd=source_root, environment=env,
                             stdout_path=query_path, timeout_seconds=QUERY_TIMEOUT_SECONDS,
                             cpu_seconds=QUERY_TIMEOUT_SECONDS)
        command_lines = query_path.read_text(errors="strict").splitlines()
        parsed_generators = []
        for line in command_lines:
            candidate = _parse_generator_command(
                line, tool_path=str(tools["native_glslang"]["path"]),
                build_dir=build_dir, source_root=source_root)
            if candidate is not None:
                parsed_generators.append(candidate)
        if not parsed_generators:
            raise RuntimeError(f"Meson graph declares no exact pinned glslang generator commands: {graph_id}")
        query_receipt_rel = f"{graph_id}/preflight/ninja-query-receipt.json"
        query_receipt_bytes = _canonical({
            "graph_id": graph_id, "command": ninja_query, "cwd": str(source_root),
            "returncode": query["returncode"], "timeout_seconds": QUERY_TIMEOUT_SECONDS,
            "cpu_limit_seconds": QUERY_TIMEOUT_SECONDS, "stdout_path": query_rel,
            "stdout_sha256": query["stdout_sha256"],
            "glslang_command_count": len(parsed_generators),
            "executed_build_targets": [],
        }) + b"\n"
        _write(evidence_root / query_receipt_rel, query_receipt_bytes)

        for index, generator in enumerate(parsed_generators):
            argv = generator["argv"]
            source_path = generator["source"]
            output_path = generator["output"]
            depfile_path = generator["depfile"]
            if output_path.exists() or output_path.is_symlink() or depfile_path.exists() or depfile_path.is_symlink():
                raise RuntimeError("glslang preflight would overwrite a preexisting disposable output")
            source_info = _regular_receipt(source_path, max_bytes=MAX_GENERATED_FILE_BYTES)
            output_before = {path.relative_to(build_dir).as_posix()
                             for base, _dirs, files in os.walk(build_dir, followlinks=False)
                             for path in (Path(base) / name for name in files)}
            stdout_rel = f"{graph_id}/preflight/glslang-{index:03d}.log"
            result = _run_bounded(argv, cwd=build_dir, environment=env,
                                  stdout_path=evidence_root / stdout_rel,
                                  timeout_seconds=GENERATOR_TIMEOUT_SECONDS,
                                  cpu_seconds=GENERATOR_TIMEOUT_SECONDS)
            output_info = _regular_receipt(output_path, max_bytes=MAX_GENERATED_FILE_BYTES)
            depfile_info = _regular_receipt(depfile_path, max_bytes=MAX_GENERATED_FILE_BYTES)
            output_after = {path.relative_to(build_dir).as_posix()
                            for base, _dirs, files in os.walk(build_dir, followlinks=False)
                            for path in (Path(base) / name for name in files)}
            additions = output_after - output_before
            expected_additions = {output_path.relative_to(build_dir).as_posix(),
                                  depfile_path.relative_to(build_dir).as_posix()}
            if additions != expected_additions:
                raise RuntimeError(f"glslang wrote unexpected disposable files: {sorted(additions)}")
            source_relative = source_path.relative_to(source_root).as_posix()
            output_relative = output_path.relative_to(build_dir).as_posix()
            depfile_relative = depfile_path.relative_to(build_dir).as_posix()
            generator_receipts.append({
                "graph_id": graph_id, "command": argv, "cwd": str(build_dir),
                "returncode": result["returncode"],
                "timeout_seconds": GENERATOR_TIMEOUT_SECONDS,
                "cpu_limit_seconds": GENERATOR_TIMEOUT_SECONDS,
                "source_path": source_relative, "source_sha256": source_info["sha256"],
                "output_path": output_relative, "output_sha256": output_info["sha256"],
                "output_size_bytes": output_info["size_bytes"],
                "depfile_path": depfile_relative, "depfile_sha256": depfile_info["sha256"],
                "depfile_size_bytes": depfile_info["size_bytes"],
                "stdout_path": stdout_rel, "stdout_sha256": result["stdout_sha256"],
            })
            output_rows.append({"graph_id": graph_id, "build_relative_path": output_relative,
                                "sha256": output_info["sha256"]})

        identity = build_retention._capture_identity(
            build_dir, graph_evidence, require_direct_meson=True,
            expected_cross_file=(source_root / str(declaration["cross_file_input"])
                                 if declaration.get("cross_file_input") else None),
            enforce_cross_file=True, cross_source_root=source_root)
        if identity.get("generated_shader_identity") is None:
            raise RuntimeError(f"Meson graph lacks a generated shader identity list: {graph_id}")
        profile_sha256 = build_retention._validate_dual_graph_configuration_profile(
            plan, declaration, identity, str(plan["build_type"]), graph_evidence)
        dependency = identity.get("dependency_state")
        if not isinstance(dependency, dict):
            raise RuntimeError(f"Meson dependency identity is absent: {graph_id}")
        dependency["plan_profile_sha256"] = profile_sha256
        config_key = build_retention._dual_graph_plan_bound_configuration_key(identity, profile_sha256)
        frozen_identity = {"configuration_profile_sha256": profile_sha256,
                           "config_key_sha256": config_key, "meson_identity": identity}
        graph_rows.append({"graph_id": graph_id,
                           "configuration_identity": build_retention._dual_graph_configuration_identity(identity),
                           "prebuild_identity": frozen_identity,
                           "prebuild_identity_sha256": hashlib.sha256(_canonical(frozen_identity)).hexdigest()})

    # Both configure graphs must expose exactly the outputs produced by the
    # direct, pinned glslang generator commands above.
    for graph_row in graph_rows:
        graph_id = graph_row["graph_id"]
        captured = graph_row["prebuild_identity"]["meson_identity"]["generated_shader_identity"]
        expected = sorted((row["build_relative_path"], row["sha256"])
                          for row in output_rows if row["graph_id"] == graph_id)
        observed = sorted((row.get("build_relative_path"), row.get("sha256"))
                          for row in captured if isinstance(row, Mapping))
        if observed != expected:
            raise RuntimeError(f"captured shader identity differs from exact glslang outputs: {graph_id}")

    graph_map = {
        row["graph_id"]: {"graph_id": row["graph_id"],
                          "config_key_sha256": row["prebuild_identity"]["config_key_sha256"],
                          "meson_identity": row["prebuild_identity"]["meson_identity"]}
        for row in graph_rows
    }
    pair_key = build_retention._dual_graph_configuration_pair_sha256(graph_map)
    evidence_files = _inventory(evidence_root)
    preflight = {
        "schema_version": 1, "status": "PASS", "run_key": run_key,
        "mode": "MESON_CONFIGURE_AND_GLSLANG_GENERATOR_ONLY",
        "configure_graph_ids": list(GRAPH_IDS), "configure_receipts": configure_receipts,
        "compiler_probe_receipts": compiler_probe_receipts,
        "generator_graph_ids": list(GRAPH_IDS), "generator_receipts": generator_receipts,
        "generated_output_rows": sorted(output_rows,
                                         key=lambda row: (row["graph_id"], row["build_relative_path"])),
        "query_receipt_paths": [f"{graph_id}/preflight/ninja-query-receipt.json" for graph_id in GRAPH_IDS],
        "executed_production_targets": [], "executed_ninja_build_targets": [],
        "executed_non_glslang_generator_commands": [],
        "resource_bounds": {"configure_timeout_seconds": CONFIGURE_TIMEOUT_SECONDS,
                            "configure_cpu_seconds": CONFIGURE_TIMEOUT_SECONDS,
                            "ninja_query_timeout_seconds": QUERY_TIMEOUT_SECONDS,
                            "glslang_timeout_seconds": GENERATOR_TIMEOUT_SECONDS,
                            "glslang_cpu_seconds": GENERATOR_TIMEOUT_SECONDS,
                            "max_output_file_bytes": MAX_OUTPUT_FILE_BYTES},
    }
    manifest = {"schema_version": 1, "status": "PASS", "preflight": preflight,
                "graphs": graph_rows, "graph_pair_key_sha256": pair_key,
                "evidence_files": evidence_files}
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    manifest_path = artifact_root / "prebuild-manifest.json"
    _write(manifest_path, raw)
    return {
        "manifest_path": manifest_path.relative_to(repository).as_posix(),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_sha256": hashlib.sha256(_canonical(manifest)).hexdigest(),
        "evidence_root": evidence_root.relative_to(repository).as_posix(),
        "build_configuration_key_sha256": pair_key,
        "build_graph_prebuild_identities": {
            row["graph_id"]: row["prebuild_identity"] for row in graph_rows},
        "configure_receipts": configure_receipts,
        "generator_receipts": generator_receipts,
        "compiler_probe_receipts": compiler_probe_receipts,
        "executed_production_targets": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    plan_path = args.plan.resolve(strict=True)
    if plan_path.parent != REPO:
        raise RuntimeError("prebuild input plan must be in the repository root")
    plan = json.loads(plan_path.read_bytes())
    result = create_prebuild_evidence(plan, run_key=args.run_key)
    output = args.output.resolve(strict=False)
    if output.parent != REPO or output.exists() or output.is_symlink():
        raise RuntimeError("prebuild result path must be a new repository-root file")
    _write(output, (json.dumps(result, sort_keys=True, indent=2) + "\n").encode())
    print(json.dumps({"status": "PASS", "manifest_path": result["manifest_path"],
                      "manifest_sha256": result["manifest_sha256"],
                      "build_configuration_key_sha256": result["build_configuration_key_sha256"],
                      "configure_count": len(result["configure_receipts"]),
                      "glslang_generator_count": len(result["generator_receipts"]),
                      "executed_production_targets": []}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"r65 prebuild identity failed: {error}", file=sys.stderr)
        raise SystemExit(1)
