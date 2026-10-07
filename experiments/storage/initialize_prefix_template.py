#!/usr/bin/env python3
"""Create the single Wine template without running a presentation or rendering experiment."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/storage"))
from storage_policy import (  # noqa: E402
    FREE_HARD_STOP,
    FREE_WARNING,
    GIB,
    PREFIX_ROOT,
    PREFIX_TEMPLATE,
    begin_experiment,
    certify_prefix_template,
    disk_free_bytes,
    make_experiment_id,
    measure_project_usage,
    preserved_prefixes,
    _prefix_size,
)

RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
RUNTIME_MANIFEST = REPO / "experiments/storage/runtime-manifests/step11d_r_active_runtime.json"
MOLTENVK = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
OUT = REPO / "experiments/storage/template-initialization-flushed-20261007"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def actual_runtime_tree_hash(root: Path) -> tuple[str, int, list[str]]:
    manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    if Path(manifest["path"]) != root:
        raise ValueError("active Wine runtime manifest points to a different path")
    entries = manifest["entries"]
    missing: list[str] = []
    declared_paths = {row["relative_path"] for row in entries}
    actual_paths: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirnames):
            path = current / name
            if path.is_symlink():
                actual_paths.add(path.relative_to(root).as_posix())
                dirnames.remove(name)
        for name in filenames:
            actual_paths.add((current / name).relative_to(root).as_posix())
    extra = sorted(actual_paths - declared_paths)
    missing_from_tree = sorted(declared_paths - actual_paths)
    if extra or missing_from_tree:
        raise ValueError(f"active Wine runtime path inventory changed; extra={extra[:10]}, missing={missing_from_tree[:10]}")
    aggregate = hashlib.sha256()
    for row in entries:
        path = root / row["relative_path"]
        try:
            info = path.lstat()
        except OSError:
            missing.append(row["relative_path"])
            continue
        kind = ("symlink" if stat.S_ISLNK(info.st_mode)
                else "file" if stat.S_ISREG(info.st_mode) else "other")
        if kind != row["kind"]:
            missing.append(row["relative_path"])
            continue
        if info.st_mtime_ns != row.get("mtime_ns"):
            missing.append(row["relative_path"])
            continue
        if kind == "symlink":
            actual = os.readlink(path)
            if actual != row.get("target") or stat.S_IMODE(info.st_mode) != int(row["mode"], 8):
                missing.append(row["relative_path"])
        elif kind == "file":
            if info.st_size != row.get("size_bytes") or stat.S_IMODE(info.st_mode) != int(row["mode"], 8):
                missing.append(row["relative_path"])
            elif sha(path) != row.get("sha256"):
                missing.append(row["relative_path"])
        aggregate.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        aggregate.update(b"\n")
    if missing:
        raise ValueError(f"active Wine runtime no longer matches its manifest: {missing[:10]}")
    digest = aggregate.hexdigest()
    if digest != manifest["tree_sha256"]:
        raise ValueError("active Wine runtime manifest aggregate hash is invalid")
    return digest, len(entries), missing


def stop_server(env: dict[str, str], log_path: Path) -> tuple[bool, list[dict[str, Any]]]:
    records = []
    good = True
    with log_path.open("a", encoding="utf-8") as log:
        for flag in ("-k", "-w"):
            try:
                result = subprocess.run(
                    ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wineserver"), flag],
                    env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
                    timeout=30, check=False,
                )
                records.append({"flag": flag, "returncode": result.returncode})
                good = good and result.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as error:
                records.append({"flag": flag, "error": repr(error)})
                good = False
                break
    return good, records


def main() -> int:
    if OUT.exists() or OUT.is_symlink():
        raise SystemExit(f"refusing to reuse template initialization output: {OUT}")
    if PREFIX_TEMPLATE.exists() or PREFIX_TEMPLATE.is_symlink():
        raise SystemExit(f"refusing to replace existing template path: {PREFIX_TEMPLATE}")
    for path in (RUNTIME / "bin/wine", RUNTIME / "bin/wineserver", RUNTIME / "bin/wineboot",
                 RUNTIME / "share", MOLTENVK, ICD, RUNTIME_MANIFEST):
        if not path.exists():
            raise SystemExit(f"required frozen runtime input is missing: {path}")

    tree_hash, tree_entries, _ = actual_runtime_tree_hash(RUNTIME)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("WINE", "DXVK_", "FG_", "MVK_", "MTL_", "VK_", "DYLD_")):
            env.pop(key, None)
    env.update({
        "WINEPREFIX": str(PREFIX_TEMPLATE),
        "WINEARCH": "win64",
        "WINELOADER": str(RUNTIME / "bin/wine"),
        "WINESERVER": str(RUNTIME / "bin/wineserver"),
        "WINEDATADIR": str(RUNTIME / "share"),
        "WINEDLLPATH": ":".join(str(RUNTIME / path) for path in (
            "lib/wine/x86_64-unix", "lib/wine/x86_64-windows", "lib/wine/i386-windows")),
        "WINEDLLOVERRIDES": "winemenubuilder.exe=d;winevulkan=d",
        "VK_DRIVER_FILES": "/private/tmp/FGMetal-step11d3-no-vulkan-icd.json",
        "VK_ICD_FILENAMES": "/private/tmp/FGMetal-step11d3-no-vulkan-icd.json",
        "WINEDEBUG": "-all",
    })
    if Path(env["VK_DRIVER_FILES"]).exists():
        raise SystemExit(f"refusing to use an existing Vulkan ICD override path: {env['VK_DRIVER_FILES']}")
    version = subprocess.run(
        ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wine"), "--version"],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=15, check=False,
    )
    if version.returncode != 0 or "wine-11.17" not in version.stdout:
        raise SystemExit(f"unexpected Wine runtime version: {version.stdout.strip()}")

    experiment_id = make_experiment_id("step11d3-template-initialize")
    manifest = begin_experiment(
        experiment_id=experiment_id,
        purpose="Initialize one pristine Wine 11.17 prefix with wineboot -u only; no graphics experiment",
        candidate_hashes={
            "wine_runtime_tree": tree_hash,
            "wine_binary": sha(RUNTIME / "bin/wine"),
            "moltenvk_runtime_candidate": sha(MOLTENVK),
            "moltenvk_icd": sha(ICD),
        },
        expected_duration_seconds=300,
        expected_prefix_bytes=3 * GIB,
        expected_build_bytes=0,
        estimated_persistent_bytes=0,
        storage_justification="One required retained template; exact tree is fingerprinted and reused by APFS clone-on-write for sequential runs.",
        internal_storage_setup=False,
        authorized_runner_paths=[Path(__file__).resolve()],
        output_dir=OUT,
        additional={
            "retention_policy": "Retain this single certified template; later runs use PrefixLease clone and finally cleanup.",
            "graphics_experiments_run": False,
            "wine_runtime_tree_manifest": str(RUNTIME_MANIFEST),
            "wine_runtime_tree_entries": tree_entries,
        },
    )
    OUT.mkdir(parents=True)
    (OUT / "runtime-identity.json").write_text(json.dumps({
        "runtime_root": str(RUNTIME),
        "runtime_tree_manifest": str(RUNTIME_MANIFEST),
        "runtime_tree_sha256": tree_hash,
        "runtime_tree_entry_count": tree_entries,
        "wine_version": version.stdout.strip(),
        "wine_binary_sha256": sha(RUNTIME / "bin/wine"),
        "moltenvk_path": str(MOLTENVK),
        "moltenvk_sha256": sha(MOLTENVK),
        "icd_path": str(ICD),
        "icd_sha256": sha(ICD),
    }, indent=2) + "\n", encoding="utf-8")

    PREFIX_ROOT.mkdir(parents=True, exist_ok=True)
    before = disk_free_bytes(PREFIX_ROOT)
    if before < 45 * GIB:
        raise RuntimeError(f"free space dropped below 45 GiB before template initialization: {before}")
    start = time.monotonic()
    watcher: list[dict[str, Any]] = []
    process: subprocess.Popen[str] | None = None
    server_ok = False
    server_records: list[dict[str, Any]] = []
    success = False
    process_reaped = True
    prefix_removed_after_failure = False
    cleanup_error = None
    try:
        with (OUT / "wineboot.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wine"), "wineboot", "-u"],
                env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
            )
            deadline = start + 300
            while process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    free = disk_free_bytes(PREFIX_ROOT)
                    if free < FREE_HARD_STOP:
                        watcher.append({"free_bytes": free, "action": "HARD_STOP_TERMINATE"})
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise RuntimeError(f"template init crossed the 25 GiB hard stop: {free}")
                    if free < FREE_WARNING and not watcher:
                        watcher.append({"free_bytes": free, "action": "WARNING_NO_NEW_EXPERIMENTS"})
                    if time.monotonic() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                        raise subprocess.TimeoutExpired(process.args, 300)
            returncode = process.returncode
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, process.args)
        server_ok, server_records = stop_server(env, OUT / "wineboot.log")
        if not server_ok:
            raise RuntimeError(f"could not verify wineserver shutdown: {server_records}")
        log_text = (OUT / "wineboot.log").read_text(encoding="utf-8", errors="replace")
        graphics_observation_lines = [line for line in log_text.splitlines()
                                      if "[mvk-" in line.lower()
                                      or "moltenvk version" in line.lower()
                                      or "created vkinstance" in line.lower()]
        presentation_tokens = (
            "nextdrawable", "vkcreateswapchain", "vkqueuepresent", "presentdrawable",
            "cametallayer", "mtldrawable", "vkcmddraw", "vkcmddrawindexed",
        )
        presentation_or_render_lines = [line for line in log_text.splitlines()
                                        if any(token in line.lower() for token in presentation_tokens)]
        if presentation_or_render_lines:
            raise RuntimeError("Wine bootstrap emitted drawable, swapchain, present, or render evidence; refusing to certify")
        missing = [name for name in ("system.reg", "user.reg", "drive_c", "dosdevices")
                   if not (PREFIX_TEMPLATE / name).exists()]
        if missing:
            root_entries = sorted(path.name for path in PREFIX_TEMPLATE.iterdir()) if PREFIX_TEMPLATE.is_dir() else []
            raise RuntimeError(f"wineboot did not produce required prefix items {missing}; root_entries={root_entries[:40]}")
        if preserved_prefixes() and len(preserved_prefixes()) >= 2:
            raise RuntimeError("adding the template would exceed the two-preserved-prefix limit")
        initialized = {
            "command": [str(RUNTIME / "bin/wine"), "wineboot", "-u"],
            "wineboot_returncode": returncode,
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "wineserver_shutdown": server_records,
            "graphics_experiments_run": False,
            "presentation_or_rendering_experiment_run": False,
            "winevulkan_disable_override_requested": True,
            "vulkan_icd_override": env["VK_ICD_FILENAMES"],
            "vulkan_icd_override_path_existed": Path(env["VK_ICD_FILENAMES"]).exists(),
            "winevulkan_disabled_by_override": not bool(graphics_observation_lines),
            "graphics_api_initialization_observed": bool(graphics_observation_lines),
            "moltenvk_process_load_observed": any("moltenvk" in line.lower() or "[mvk-" in line.lower()
                                                   for line in graphics_observation_lines),
            "vulkan_device_enumeration_observed": any("gpu device" in line.lower()
                                                       for line in graphics_observation_lines),
            "graphics_initialization_log_sha256": hashlib.sha256(log_text.encode()).hexdigest(),
            "graphics_initialization_observation_lines": graphics_observation_lines,
            "screen_capture": False,
        }
        certification = certify_prefix_template(
            PREFIX_TEMPLATE,
            runtime_identity={
                "wine_runtime_root": str(RUNTIME),
                "wine_runtime_tree_manifest": str(RUNTIME_MANIFEST),
                "wine_runtime_tree_sha256": tree_hash,
                "wine_version": version.stdout.strip(),
                "wine_binary_sha256": sha(RUNTIME / "bin/wine"),
                "moltenvk_path_for_subsequent_graphics_runs": str(MOLTENVK),
                "moltenvk_sha256_for_subsequent_graphics_runs": sha(MOLTENVK),
                "moltenvk_icd_path": str(ICD),
                "moltenvk_icd_sha256": sha(ICD),
            },
            initialization=initialized,
        )
        success = True
        (OUT / "template-certification.json").write_text(json.dumps(certification, indent=2) + "\n",
                                                             encoding="utf-8")
    finally:
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            process_reaped = process.poll() is not None
        if PREFIX_TEMPLATE.exists() and not success:
            stopped, records = ((True, server_records) if server_ok else
                                stop_server(env, OUT / "wineboot.log")) if process_reaped else (
                                    False, [{"error": "wineboot process could not be reaped"}])
            server_records = records
            server_ok = stopped
            if stopped and PREFIX_TEMPLATE.is_dir() and not PREFIX_TEMPLATE.is_symlink():
                try:
                    shutil.rmtree(PREFIX_TEMPLATE)
                    prefix_removed_after_failure = not PREFIX_TEMPLATE.exists()
                except OSError as error:
                    cleanup_error = repr(error)
            elif process_reaped and PREFIX_TEMPLATE.is_dir() and not PREFIX_TEMPLATE.is_symlink():
                try:
                    (PREFIX_TEMPLATE / "PRESERVE_PREFIX.json").write_text(json.dumps({
                        "PRESERVE_PREFIX": True,
                        "experiment_id": manifest["experiment_id"],
                        "reason": "template initialization failed and Wine server shutdown was not verified",
                        "creation_time_unix": time.time(),
                        "estimated_size_bytes": _prefix_size(PREFIX_TEMPLATE)["logical_bytes"],
                        "wine_server_shutdown": records,
                    }, indent=2) + "\n", encoding="utf-8")
                except OSError as error:
                    cleanup_error = repr(error)

        after = disk_free_bytes(PREFIX_ROOT)
        output_log = (OUT / "wineboot.log").read_text(encoding="utf-8", errors="replace") \
            if (OUT / "wineboot.log").exists() else ""
        moltenvk_observed = any("[mvk-" in line.lower()
                                or "moltenvk version" in line.lower()
                                or "created vkinstance" in line.lower()
                                for line in output_log.splitlines())
        result = {
            "experiment_id": manifest["experiment_id"],
            "decision": manifest["decision"],
            "template_path": str(PREFIX_TEMPLATE),
            "template_retained": bool(success and PREFIX_TEMPLATE.is_dir()),
            "template_certified": (PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json").is_file(),
            "prefix_size": _prefix_size(PREFIX_TEMPLATE) if PREFIX_TEMPLATE.is_dir() else None,
            "wineserver_shutdown": server_records,
            "wineboot_process_reaped": process_reaped,
            "prefix_removed_after_failure": prefix_removed_after_failure,
            "cleanup_error": cleanup_error,
            "storage_watch_events": watcher,
            "free_before_bytes": before,
            "free_after_bytes": after,
            "project_usage_after": measure_project_usage(),
            "preserved_prefixes_after": [str(path) for path in preserved_prefixes()],
            "graphics_experiments_run": False,
            "presentation_or_rendering_experiment_run": False,
            "winevulkan_disable_override_requested": True,
            "vulkan_icd_override": env["VK_ICD_FILENAMES"],
            "vulkan_icd_override_path_existed": Path(env["VK_ICD_FILENAMES"]).exists(),
            "winevulkan_disabled_by_override": not moltenvk_observed,
            "graphics_api_initialization_observed": moltenvk_observed,
            "moltenvk_process_load_observed": moltenvk_observed,
            "vulkan_device_enumeration_observed": any("gpu device" in line.lower()
                                                       for line in output_log.splitlines()),
            "storage_hygiene_status": (
                "FAIL_STORAGE_HYGIENE" if cleanup_error or not process_reaped
                or (not success and PREFIX_TEMPLATE.exists() and not prefix_removed_after_failure)
                else "PASS_CLEANUP"),
        }
        result_path = OUT / "template-initialization-result.json"
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        result["free_after_bytes"] = disk_free_bytes(PREFIX_ROOT)
        result["project_usage_after"] = measure_project_usage()
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
