#!/usr/bin/env python3
"""Run one serialized STEP 11D.3-B Wine Vulkan experiment under PrefixLease."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STORAGE = REPO / "experiments/storage"
sys.path.insert(0, str(STORAGE))
sys.path.insert(0, str(REPO))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402
from trace_validator import BareWineTraceValidator, TraceValidator, ValidationError  # noqa: E402
from experiments.dxvk_macos_step11d3b.loader_evidence import parse_loader_evidence
from experiments.dxvk_macos_step11d3b.trace_processes import build_trace_index, validate_target_association

RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
MOLTENVK_ORIGINAL = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")
MOLTENVK_ICD_LIBRARY = ICD.parent / "libMoltenVK.dylib"
RUNTIME_MANIFEST = STORAGE / "runtime-manifests/step11d_r_active_runtime.json"
APP = HERE / "control/bare_vulkan_app.exe"
APP_SOURCE = HERE / "bare_vulkan_app.cpp"
PROXY = HERE / "control/libvulkan-trace.dylib"
PROXY_SOURCE = HERE / "trace_loader.mm"
PROCESS_RUNNER = HERE / "wine_process_runner.py"
WINEVULKAN_DLL = RUNTIME / "lib/wine/x86_64-windows/winevulkan.dll"
WINEVULKAN_SO = RUNTIME / "lib/wine/x86_64-unix/winevulkan.so"
WINE_HOST_LOADER = RUNTIME / "lib/wine/x86_64-unix/win32u.so"
WINEMAC_SO = RUNTIME / "lib/wine/x86_64-unix/winemac.so"
WINE = RUNTIME / "bin/wine"
WINE_HOST_EXECUTABLE = RUNTIME / "lib/wine/x86_64-unix/wine"
WINESERVER = RUNTIME / "bin/wineserver"
TRACE_VALIDATOR = STORAGE / "trace_validator.py"
CONTROL_VERSION = "step11d3b-br4-process-owned-trace-v7"
EXPECTED_MOLTENVK_VERSION = "MoltenVK v1.4.2"
SMOKE_AUTH_FIELD = "br2_smoke_authorization_hmac_sha256"
WINDOW_TITLE = "FG-Metal Step 11D.3-B Bare Vulkan"
GIB = 1024 ** 3
MIB = 1024 ** 2
FORBIDDEN_MODULE_PARTS = (
    "dxvk", "dxgi", "d3d9", "d3d10", "d3d11", "d3d12", "fgmetalbridge",
    "rife", "framegen", "frame_generation", "fgmetal",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_execution_result(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load the process runner record without following links or assuming it exists."""
    if path.is_symlink():
        return None, "execution result is a symlink"
    if not path.is_file():
        return None, "execution result is missing"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"execution result could not be read: {exc}"
    if not isinstance(result, dict):
        return None, "execution result must contain a JSON object"
    return result, None


def file_identity(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required pinned file is missing, non-regular, or a symlink: {path}")
    return {"path": str(path.resolve(strict=True)), "sha256": sha256(path),
            "size_bytes": path.stat().st_size}


def candidate_files() -> dict[str, dict[str, Any]]:
    files = {
        "wine_host_image": WINE_HOST_EXECUTABLE, "wine_launcher": WINE,
        "wineserver": WINESERVER,
        "winevulkan_dll": WINEVULKAN_DLL, "winevulkan_so": WINEVULKAN_SO,
        "wine_host_loader": WINE_HOST_LOADER, "winemac_so": WINEMAC_SO,
        "moltenvk_original": MOLTENVK_ORIGINAL,
        "moltenvk_icd_library": MOLTENVK_ICD_LIBRARY,
        "moltenvk_icd": ICD, "wine_runtime_manifest": RUNTIME_MANIFEST,
        "app": APP, "app_source": APP_SOURCE, "trace_proxy": PROXY,
        "trace_proxy_source": PROXY_SOURCE, "process_runner": PROCESS_RUNNER,
        "trace_validator": TRACE_VALIDATOR, "runner": HERE / "runner_b.py",
        "storage_policy": STORAGE / "storage_policy.py",
        "trace_processes": HERE / "trace_processes.py",
        "loader_evidence": HERE / "loader_evidence.py",
    }
    identities = {name: file_identity(path) for name, path in files.items()}
    expected = {
        "wine_host_image": "38525a48b11217238977eff6fa1f573ffed88a8534975c9477827e886ec0ec94",
        "wine_launcher": "946a7032484d02981182d59f82edda03f40f38c510e0208d48482c0830ef20e9",
        "wineserver": "2af98e4aedd73515e5980e21585ebb6e6ad6c705242d760df24b7fd83fe2d51a",
        "winevulkan_dll": "a18992aca34493aae325a5c1c6ddc78b905876fb65b2ca91fe7b1882b33fd2dc",
        "winevulkan_so": "6362dd64890738166dda79bdd9fcc94df55857c1f9756a3268ae91b8d3c38d88",
        "wine_host_loader": "b43893430e929e17086653d9c2266a0d6dd4863b8739c005c3c9a9bdb2a86f1a",
        "winemac_so": "e39ec04fbe3db971e272e13209bffc5ccb059232015b565e5082e161069df199",
        "moltenvk_original": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
        "moltenvk_icd_library": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
        "moltenvk_icd": "578ff08cd0d8734619357541771a5abc9c3470ca300030219a971a9e9dbbe466",
        "wine_runtime_manifest": "662a47726872910cf7e0c9f16d3d416a0183cff0c974ba47150ea916b4186a95",
        "app": "ca636ff8573fa7c0b8752f4f5097eed73d9c69f417455809214b6c051a4e6dc6",
        "app_source": "bd0ce6017ce048edf95150dc193312e5ab69ed9a417ed80de94392b2ad001214",
        "trace_proxy": "1b3dda059905bbacb6dfb63718c78eb1505682fb6d493d5be2ef4dec51a38688",
        "trace_proxy_source": "c6bbc3c34c6b5708ae231946ccfe4c8ab9ed270519454d6a9f1819209a4bea26",
    }
    for name, digest in expected.items():
        if identities[name]["sha256"] != digest:
            raise RuntimeError(f"pinned B input changed: {name} {identities[name]['sha256']} != {digest}")
    template_dll = storage_policy.PREFIX_TEMPLATE / "drive_c/windows/system32/winevulkan.dll"
    template_hash = sha256(template_dll)
    if template_hash != identities["winevulkan_dll"]["sha256"]:
        raise RuntimeError("certified prefix template's builtin winevulkan.dll differs from the pinned Wine module")
    runtime_manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    if runtime_manifest.get("tree_sha256") != "2fea825b205073bf011ce948ec4cbe6560d9d90e26982233ad05822a17a92bfe":
        raise RuntimeError("pinned Wine runtime tree manifest changed")
    identities["template_winevulkan_dll"] = file_identity(template_dll)
    identities["wine_runtime_tree"] = {
        "path": str(RUNTIME), "sha256": runtime_manifest["tree_sha256"],
        "manifest_path": str(RUNTIME_MANIFEST.resolve()),
        "manifest_sha256": identities["wine_runtime_manifest"]["sha256"],
    }
    return identities


def wine_execution_binding(inputs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Keep the certified launcher and the loaded host image independent."""
    return {
        "wine_launcher_path": inputs["wine_launcher"]["path"],
        "wine_launcher_sha256": inputs["wine_launcher"]["sha256"],
        "wine_host_image_path": inputs["wine_host_image"]["path"],
        "wine_host_image_sha256": inputs["wine_host_image"]["sha256"],
        "wine_runtime_tree_manifest_sha256": inputs["wine_runtime_manifest"]["sha256"],
    }


def wine_candidate_hashes(inputs: dict[str, dict[str, Any]]) -> dict[str, str]:
    candidate_hashes = {name: row["sha256"] for name, row in inputs.items()
                        if name not in {"wine_runtime_tree", "template_winevulkan_dll"}}
    candidate_hashes["wine_runtime_tree"] = inputs["wine_runtime_tree"]["sha256"]
    candidate_hashes["wine_binary"] = inputs["wine_launcher"]["sha256"]
    candidate_hashes["wine_host_image"] = inputs["wine_host_image"]["sha256"]
    candidate_hashes["wine_launcher_binary"] = inputs["wine_launcher"]["sha256"]
    candidate_hashes["moltenvk"] = inputs["moltenvk_original"]["sha256"]
    return candidate_hashes


def smoke_authorization_bindings() -> dict[str, Any]:
    identities = candidate_files()
    return {
        "control_version": CONTROL_VERSION,
        "architecture": "x86_64-under-Rosetta",
        "candidate_identities": {name: row["sha256"] for name, row in identities.items()},
        "gate0_control_hashes": storage_policy._gate0_control_hashes(),
        "gate0_policy_sha256": sha256(STORAGE / "storage_policy.py"),
        "runner_sha256": sha256(HERE / "runner_b.py"),
        "process_runner_sha256": sha256(PROCESS_RUNNER),
        "trace_validator_sha256": sha256(TRACE_VALIDATOR),
        "validator_contract": "bare-wine-trace-validator-preframe-dll-binding-startup-and-clock-v6",
    }


def automatic_post_matches(receipt: dict[str, Any] | None, experiment_id: str,
                           prefix: Path, manifest_sha256: str) -> bool:
    if not isinstance(receipt, dict) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        return False
    identity = receipt.get("wineprefix_identity", {})
    shutdown = receipt.get("wineserver_shutdown", [])
    return bool(
        storage_policy._verify_storage_evidence(receipt, "cleanup_receipt_hmac_sha256")
        and receipt.get("experiment_id") == experiment_id
        and receipt.get("prefix_path") == str(prefix)
        and receipt.get("status") == "PASS"
        and receipt.get("storage_hygiene_status") == "PASS"
        and receipt.get("free_space_status") == "PASS"
        and receipt.get("prefix_deleted") is True
        and receipt.get("active_lease_marker_removed_after_receipt") is True
        and identity.get("experiment_id") == experiment_id
        and identity.get("manifest_sha256") == manifest_sha256
        and all(identity.get(key) == str(prefix) for key in (
            "canonical_absolute_path", "launch_WINEPREFIX", "cleanup_WINEPREFIX"))
        and len(shutdown) == 2
        and {row.get("flag") for row in shutdown} == {"-k", "-w"}
        and all(row.get("returncode") == 0 and row.get("WINEPREFIX") == str(prefix)
                and row.get("prefix_identity", {}).get("experiment_id") == experiment_id
                and row.get("prefix_identity", {}).get("manifest_sha256") == manifest_sha256
                for row in shutdown))


def verify_smoke_authorization(smoke_id: str, *, runs_root: Path | None = None,
                               current_bindings: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
    if not re.fullmatch(r"step11d3b-smoke-\d{8}-\d{6}-[0-9a-f]{8}", smoke_id):
        raise RuntimeError("baseline authorization requires the exact ID of a new BR2 smoke")
    root = (HERE / "runs") if runs_root is None else runs_root
    run_dir = root / smoke_id
    auth_path = run_dir / "smoke-authorization.json"
    result_path = run_dir / "run-result.json"
    if (not auth_path.is_file() or auth_path.is_symlink()
            or not result_path.is_file() or result_path.is_symlink()):
        raise RuntimeError("baseline requires an authenticated prior smoke artifact and run result")
    try:
        authorization = json.loads(auth_path.read_text(encoding="utf-8"))
        run_result_bytes = result_path.read_bytes()
        run_result = json.loads(run_result_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("prior smoke authorization artifacts are unreadable") from error
    if not storage_policy._verify_storage_evidence(authorization, SMOKE_AUTH_FIELD):
        raise RuntimeError("prior smoke authorization HMAC is missing or invalid")
    if (authorization.get("experiment_id") != smoke_id
            or authorization.get("phase") != "smoke"
            or authorization.get("status") != "PASS"
            or authorization.get("is_valid") is not True
            or authorization.get("quantitative_ready") is not True
            or authorization.get("prefixlease_automatic_post") != "PASS"
            or authorization.get("gate0_post") != "PASS"
            or authorization.get("run_result_path") != str(result_path.resolve())
            or authorization.get("run_result_sha256") != hashlib.sha256(run_result_bytes).hexdigest()):
        raise RuntimeError("prior smoke authorization does not bind a complete passing smoke and automatic POST")
    if (run_result.get("experiment_id") != smoke_id
            or run_result.get("phase") != "smoke"
            or run_result.get("status") != "PASS"
            or run_result.get("is_valid") is not True
            or run_result.get("process_returncode") != 0
            or run_result.get("timed_out") is not False
            or run_result.get("duration_seconds_requested") != 30
            or run_result.get("prefix_cleanup_pass") is not True
            or run_result.get("screenshot_check_pass") is not True
            or run_result.get("gate0_post", {}).get("gate0_status") != "PASS"
            or run_result.get("validation", {}).get("is_valid") is not True
            or run_result.get("validation", {}).get("quantitative_ready") is not True):
        raise RuntimeError("prior smoke run result no longer satisfies the baseline admission contract")
    if not automatic_post_matches(
            run_result.get("prefix_cleanup"), smoke_id,
            storage_policy.PREFIX_ROOT / f"{smoke_id}-prefix",
            str(run_result.get("storage_manifest_sha256", ""))):
        raise RuntimeError("prior smoke automatic POST does not bind this smoke prefix and manifest")
    current = current_bindings if current_bindings is not None else smoke_authorization_bindings()
    if (authorization.get("bindings") != current
            or run_result.get("control_binding_snapshot") != current):
        raise RuntimeError("prior smoke authorization is stale because a runtime or control hash changed")
    if authorization.get("trace_artifacts") != run_result.get("trace_artifacts"):
        raise RuntimeError("prior smoke trace artifact bindings differ")
    verify_trace_artifact_bindings(run_dir, authorization.get("trace_artifacts"))
    return authorization, auth_path


def verify_trace_artifact_bindings(run_dir: Path, artifacts: dict | None) -> None:
    if not isinstance(artifacts, dict) or set(artifacts) != {"target_host_trace", "trace_index", "app_trace"}:
        raise RuntimeError("prior smoke requires complete target trace, index, and app bindings")
    for role, row in artifacts.items():
        path = Path(row.get("path", ""))
        expected_parent = run_dir / "host-traces" if role == "target_host_trace" else run_dir
        expected_name = {"trace_index": "trace-index.json", "app_trace": "app-trace.jsonl"}.get(role)
        if (path.parent != expected_parent or not path.is_absolute()
                or str(path.resolve(strict=True)) != str(path) or path.is_symlink()
                or not path.is_file() or expected_name and path.name != expected_name
                or role == "target_host_trace" and not re.fullmatch(r"host-trace\.[1-9][0-9]*\.jsonl", path.name)
                or sha256(path) != row.get("sha256")):
            raise RuntimeError(f"prior smoke {role} artifact is missing, unsafe, or changed")


def gate0_snapshot(stage: str) -> dict[str, Any]:
    blockers = storage_policy._gate0_blockers()
    free_bytes = storage_policy.disk_free_bytes(storage_policy.REPO)
    project = storage_policy.measure_project_usage()
    scoped_bytes = int(project["allocated_inode_deduplicated_bytes"])
    attestation = json.loads(storage_policy.GATE0_ATTESTATION_PATH.read_text(encoding="utf-8"))
    retention = attestation.get("build_retention_state", {})
    active_prefixes = [str(path) for path in storage_policy._active_lease_markers()]
    uncertified = [str(path) for path in storage_policy.uncertified_prefixes()]
    return {
        "stage": stage, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate0_status": "PASS" if not blockers else "BLOCKED",
        "gate0_blockers": blockers,
        "free_bytes": free_bytes, "free_gib": free_bytes / GIB,
        "scoped_fgmetal_bytes": scoped_bytes, "scoped_fgmetal_gib": scoped_bytes / GIB,
        "formal_free_minimum_bytes": storage_policy.FREE_MINIMUM,
        "formal_scope_maximum_bytes": storage_policy.GATE0_PROJECT_BUDGET,
        "emergency_hard_stop_bytes": storage_policy.FREE_HARD_STOP,
        "active_prefix_leases": active_prefixes,
        "active_build_count": retention.get("active_build_count"),
        "active_builds": [] if retention.get("active_build_count") == 0 else None,
        "preserved_prefixes": [str(path) for path in storage_policy.preserved_prefixes()],
        "uncertified_prefixes": uncertified,
    }


def windows_path(path: Path) -> str:
    absolute = str(path.resolve())
    if not absolute.startswith("/"):
        raise ValueError("Wine Z: trace output path must be absolute")
    return "Z:" + absolute.replace("/", "\\")


def clean_environment(prefix: Path, output: Path, app_trace_path: Path,
                      host_trace_path: Path, duration_seconds: int, *, experiment_token: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in
           ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM")
           if key in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    runtime_paths = (
        RUNTIME / "lib/wine/x86_64-unix",
        RUNTIME / "lib/wine/x86_64-windows",
        RUNTIME / "lib/wine/i386-windows",
    )
    proxy_path = output / "trace-loader/libMoltenVK.dylib"
    moltenvk_path = output / "mvk/libMoltenVK.1.dylib"
    dyld_paths = (proxy_path.parent, moltenvk_path.parent, runtime_paths[0])
    fallback_paths = (*dyld_paths, RUNTIME / "lib")
    env.update({
        "WINEPREFIX": str(prefix), "WINEARCH": "win64",
        "WINELOADER": str(WINE), "WINESERVER": str(WINESERVER),
        "WINEDATADIR": str(RUNTIME / "share"),
        "WINEDLLPATH": ":".join(str(path) for path in runtime_paths),
        "WINEDEBUG": "+err,+pid,+tid,+loaddll",
        # PrefixLease checks these manifest bytes against the certified
        # runtime identity. Wine's retained win32u path directly dlopens
        # libMoltenVK.dylib; this manifest is not used to steer that route.
        "VK_DRIVER_FILES": str(ICD),
        "VK_ICD_FILENAMES": str(ICD),
        "DYLD_LIBRARY_PATH": ":".join(str(path) for path in dyld_paths),
        "DYLD_FALLBACK_LIBRARY_PATH": ":".join(str(path) for path in fallback_paths),
        "DYLD_PRINT_LIBRARIES": "1",
        "FG_B_ORIGINAL_MOLTENVK_LIBRARY": str(moltenvk_path),
        "FG_B_ORIGINAL_MOLTENVK_SHA256": sha256(MOLTENVK_ORIGINAL),
        "FG_B_TRACE_PROXY_PATH": str(proxy_path),
        "FG_B_TRACE_PROXY_SHA256": sha256(PROXY),
        "FG_B_EXPECTED_MOLTENVK_VERSION": EXPECTED_MOLTENVK_VERSION,
        "FG_B_EXPECTED_WINE_PATH": str(WINE_HOST_EXECUTABLE),
        "FG_B_EXPECTED_WINE_SHA256": sha256(WINE_HOST_EXECUTABLE),
        "FG_B_WINE_LAUNCHER_PATH": str(WINE),
        "FG_B_WINE_LAUNCHER_SHA256": sha256(WINE),
        "FG_B_WINESERVER_PATH": str(WINESERVER),
        "FG_B_WINESERVER_SHA256": sha256(WINESERVER),
        "FG_B_WINEVULKAN_DLL_PATH": str(prefix / "drive_c/windows/system32/winevulkan.dll"),
        "FG_B_WINEVULKAN_DLL_SHA256": sha256(WINEVULKAN_DLL),
        "FG_B_WINEVULKAN_SO_PATH": str(WINEVULKAN_SO),
        "FG_B_WINEVULKAN_SO_SHA256": sha256(WINEVULKAN_SO),
        "FG_B_HOST_LOADER_PATH": str(WINE_HOST_LOADER),
        "FG_B_HOST_LOADER_SHA256": sha256(WINE_HOST_LOADER),
        "FG_B_WINEMAC_PATH": str(WINEMAC_SO),
        "FG_B_WINEMAC_SHA256": sha256(WINEMAC_SO),
        "FG_B_TRACE_DIRECTORY": str(host_trace_path),
        "FG_B_TRACE_DIRECTORY_DEVICE": str(host_trace_path.stat().st_dev),
        "FG_B_TRACE_DIRECTORY_INODE": str(host_trace_path.stat().st_ino),
        "FG_B_EXPERIMENT_TOKEN": experiment_token,
        "FG_APP_TRACE_PATH": windows_path(app_trace_path),
        "FG_RUN_SECONDS": str(duration_seconds),
    })
    storage_policy._validate_wine_child_environment(env)
    return env


def copy_verified(source: Path, target: Path, expected_sha: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to replace an existing run input: {target}")
    shutil.copyfile(source, target, follow_symlinks=False)
    if sha256(target) != expected_sha:
        raise RuntimeError(f"copied input failed exact SHA-256 verification: {target}")


def capture_window(output_path: Path) -> dict[str, Any]:
    swift = f'''import AppKit
let wanted = {json.dumps(WINDOW_TITLE)}
if let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {{
  let matches = rows.filter {{ row in
    (row[kCGWindowName as String] as? String) == wanted
      && ((row[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1) == 0
      && ((row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 0) > 0.95
  }}
  if let match = matches.first,
     let number = match[kCGWindowNumber as String] as? NSNumber {{
    if let ownerPID = match[kCGWindowOwnerPID as String] as? NSNumber {{
      if let app = NSRunningApplication(processIdentifier: pid_t(ownerPID.intValue)) {{
        app.activate(options: [.activateIgnoringOtherApps])
      }}
    }}
    print(number.intValue)
  }}
}}
'''
    listing = subprocess.run(["/usr/bin/swift", "-e", swift], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=30, check=False)
    raw_ids = [line.strip() for line in listing.stdout.splitlines() if line.strip().isdigit()]
    if listing.returncode != 0 or not raw_ids:
        return {"status": "FAIL", "reason": "visible Wine app window was not discoverable",
                "swift_returncode": listing.returncode,
                "swift_stderr_tail": listing.stderr[-1000:]}
    window_id = raw_ids[0]
    captured = subprocess.run(["/usr/sbin/screencapture", "-x", "-l", window_id,
                                str(output_path)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=20, check=False)
    if captured.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        return {"status": "FAIL", "reason": "target-window screenshot failed",
                "window_id": window_id, "returncode": captured.returncode,
                "stderr_tail": captured.stderr[-1000:]}
    return {"status": "PASS", "window_id": int(window_id),
            "path": str(output_path), "size_bytes": output_path.stat().st_size,
            "sha256": sha256(output_path)}


def inspect_screenshot_content(screenshots: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    if len(screenshots) != 2:
        return {"status": "FAIL", "reason": "two separated smoke screenshots are required"}
    paths = [str(Path(row["path"]).resolve(strict=True)) for row in screenshots]
    swift = f'''import AppKit
import Foundation
let paths = {json.dumps(paths)}
var results: [[String: Any]] = []
for path in paths {{
  guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
        let image = NSBitmapImageRep(data: data) else {{ fatalError("image decode failed") }}
  let width = image.pixelsWide
  let height = image.pixelsHigh
  let stepX = max(1, width / 128)
  let stepY = max(1, height / 128)
  var sum = [Double](repeating: 0, count: 3)
  var sampled = 0
  var nonblack = 0
  var colors = Set<String>()
  for y in stride(from: 0, to: height, by: stepY) {{
    for x in stride(from: 0, to: width, by: stepX) {{
      guard let color = image.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else {{ continue }}
      let rgb = [color.redComponent, color.greenComponent, color.blueComponent]
      for i in 0..<3 {{ sum[i] += rgb[i] }}
      if rgb.max()! > 0.04 {{ nonblack += 1 }}
      colors.insert(rgb.map {{ String(Int(($0 * 31).rounded())) }}.joined(separator: ","))
      sampled += 1
    }}
  }}
  results.append(["path": path, "width": width, "height": height,
                  "sampled_pixels": sampled,
                  "nonblack_ratio": sampled == 0 ? 0 : Double(nonblack) / Double(sampled),
                  "mean_rgb": sampled == 0 ? [0, 0, 0] : sum.map {{ $0 / Double(sampled) }},
                  "distinct_quantized_colors": colors.count])
}}
let encoded = try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
print(String(data: encoded, encoding: .utf8)!)
'''
    completed = subprocess.run(["/usr/bin/swift", "-e", swift], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=90, check=False)
    if completed.returncode != 0:
        return {"status": "FAIL", "reason": "native screenshot content analysis failed",
                "returncode": completed.returncode, "stderr_tail": completed.stderr[-1000:]}
    try:
        samples = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {"status": "FAIL", "reason": f"screenshot content analysis returned invalid data: {error}"}
    if (not isinstance(samples, list) or len(samples) != 2
            or any(row.get("sampled_pixels", 0) <= 0
                   or row.get("nonblack_ratio", 0) < 0.10
                   or row.get("distinct_quantized_colors", 0) < 2 for row in samples)):
        return {"status": "FAIL", "reason": "smoke window was black or lacked spatial color content",
                "samples": samples}
    means = [row["mean_rgb"] for row in samples]
    frame_change = sum(abs(float(means[0][i]) - float(means[1][i])) for i in range(3))
    result = {"status": "PASS" if frame_change >= 0.03 else "FAIL",
              "reason": None if frame_change >= 0.03 else "separated smoke screenshots did not change visibly",
              "mean_rgb_l1_delta": frame_change, "samples": samples}
    (output / "screenshot-content-check.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def wait_short(process: subprocess.Popen[Any], seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return process.poll() is None


def phase_command(mode: str, baseline_index: int | None = None) -> tuple[int, str]:
    if mode == "smoke":
        return 30, "smoke"
    if mode == "baseline" and baseline_index in (1, 2, 3):
        return 300, f"baseline-{baseline_index:02d}"
    raise ValueError("usage: runner_b.py smoke | baseline 1|2|3")


def run_one(mode: str, baseline_index: int | None = None,
            smoke_authorization_id: str | None = None) -> dict[str, Any]:
    duration_seconds, phase = phase_command(mode, baseline_index)
    prior_smoke: dict[str, Any] | None = None
    prior_smoke_path: Path | None = None
    if mode == "baseline":
        if smoke_authorization_id is None:
            raise RuntimeError("baseline mode requires a matching authenticated prior smoke ID")
        prior_smoke, prior_smoke_path = verify_smoke_authorization(smoke_authorization_id)
    elif smoke_authorization_id is not None:
        raise RuntimeError("smoke mode cannot consume a prior smoke authorization")
    nonce = uuid.uuid4().hex[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    experiment_id = f"step11d3b-{phase}-{stamp}-{nonce}"
    output = HERE / "runs" / experiment_id
    prefix = storage_policy.PREFIX_ROOT / f"{experiment_id}-prefix"
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to reuse B run output: {output}")

    pre = gate0_snapshot("PRE")
    if pre["gate0_status"] != "PASS":
        raise RuntimeError("Gate 0 PRE is blocked: " + "; ".join(pre["gate0_blockers"]))
    if pre["free_bytes"] < 45 * GIB or pre["scoped_fgmetal_bytes"] > 45 * GIB:
        raise RuntimeError("Gate 0 PRE violates the formal 45 GiB/45 GiB thresholds")
    inputs = candidate_files()
    binding_snapshot = smoke_authorization_bindings()
    input_hashes = {name: row["sha256"] for name, row in inputs.items()}
    if binding_snapshot.get("candidate_identities") != input_hashes:
        raise RuntimeError("candidate runtime identity changed while binding the run inputs")
    if prior_smoke is not None and prior_smoke.get("bindings") != binding_snapshot:
        raise RuntimeError("baseline runtime or controls changed after prior-smoke verification")
    candidate_hashes = wine_candidate_hashes(inputs)
    if prior_smoke_path is not None:
        candidate_hashes["prior_smoke_authorization"] = sha256(prior_smoke_path)
    runner = PROCESS_RUNNER.resolve(strict=True)
    manifest = storage_policy.begin_experiment(
        experiment_id=experiment_id,
        purpose=f"STEP 11D.3-B bare Wine Vulkan {phase}: pinned Wine 11.17 + winevulkan + MoltenVK 1.4.2 + WineMetalLayer; no DXVK/D3D/interop/scheduler",
        candidate_hashes=candidate_hashes,
        expected_duration_seconds=duration_seconds,
        expected_prefix_bytes=3 * GIB,
        expected_build_bytes=0,
        estimated_persistent_bytes=(100 if mode == "smoke" else 300) * MIB,
        storage_justification=(None if mode == "smoke" else
            "Retain bounded 300-second app and asynchronous host traces, loader log, run manifest, and PrefixLease cleanup receipt for one independent baseline."),
        authorized_runner_paths=[runner],
        output_dir=output,
        additional={"experiment_step": "11D.3-B", "run_phase": phase,
                    "trace_contract_version": CONTROL_VERSION,
                    "requested_window": [640, 360], "requested_format": "VK_FORMAT_B8G8R8A8_UNORM",
                    "requested_present_mode": "VK_PRESENT_MODE_FIFO_KHR",
                    "requested_swapchain_images": 3, "architecture": "x86_64-under-Rosetta",
                    **wine_execution_binding(inputs)},
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "trace-loader").mkdir()
    (output / "mvk").mkdir()
    copied_proxy = output / "trace-loader/libMoltenVK.dylib"
    copy_verified(PROXY, copied_proxy, inputs["trace_proxy"]["sha256"])
    copied_mvk = output / "mvk/libMoltenVK.1.dylib"
    copy_verified(MOLTENVK_ORIGINAL, copied_mvk, inputs["moltenvk_original"]["sha256"])
    copied_mvk_alias = output / "mvk/libMoltenVK.dylib"
    copy_verified(MOLTENVK_ORIGINAL, copied_mvk_alias, inputs["moltenvk_original"]["sha256"])
    app_trace = output / "app-trace.jsonl"
    host_trace_directory = output / "host-traces"
    host_trace_directory.mkdir(mode=0o700)
    host_trace = None
    trace_artifacts = None
    trace_index = None
    runtime_log = output / "runtime.log"
    env = clean_environment(prefix, output, app_trace, host_trace_directory, duration_seconds,
                            experiment_token=sha256(Path(manifest["manifest_path"]))[:32])
    manifest_record = {
        "schema_version": 1, "experiment_id": experiment_id, "phase": phase,
        "duration_seconds": duration_seconds, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate0_pre": pre, "storage_manifest_path": manifest["manifest_path"],
        "candidate_identities": inputs,
        **wine_execution_binding(inputs),
        "wine_version": "wine-11.17 (WineForge 0.6.0.5)",
        "moltenvk_version": "MoltenVK v1.4.2, commit db66022459ffb663aa2b50f6b018bc2e124f5edf",
        "runtime_root": str(RUNTIME), "moltenvk_original_path": str(MOLTENVK_ORIGINAL),
        "moltenvk_icd_path": str(ICD), "winevulkan_dll_path": str(WINEVULKAN_DLL),
        "winevulkan_so_path": str(WINEVULKAN_SO),
        "wine_host_loader_path": str(WINE_HOST_LOADER),
        "wine_host_loader_role": "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos loader",
        "winemac_so_path": str(WINEMAC_SO),
        "prefix_template": str(storage_policy.PREFIX_TEMPLATE),
        "prefix_path": str(prefix), "prefix_template_runtime_identity":
            json.loads((storage_policy.PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json").read_text())["runtime_identity"],
        "app_path": str(APP), "app_trace_path_host": str(app_trace),
        "app_trace_path_wine": env["FG_APP_TRACE_PATH"], "host_trace_directory": str(host_trace_directory),
        "trace_proxy_selected_path": str(copied_proxy),
        "moltenvk_selected_path_candidates": [str(copied_mvk), str(copied_mvk_alias)],
        "command": [str(WINE), env["FG_APP_TRACE_PATH"]],
        "environment": env,
        "control_binding_snapshot": binding_snapshot,
        "requested": {"architecture": "x86_64 under Rosetta", "windowed": True,
                      "window_client_px": [640, 360], "format": "BGRA8 UNORM",
                      "present_mode": "FIFO", "swapchain_images": 3},
        "presentation_behavior_modified": False,
        "app_is_renderer_owned_clear_color_only": True,
        "excluded_components": ["DXVK", "D3D11", "D3D9", "DXGI", "frame generation",
                                "Vulkan-to-Metal interop provider", "FGMetalBridge",
                                "internal generated-frame scheduler", "RIFE", "neural model"],
    }
    (output / "run-manifest.json").write_text(json.dumps(manifest_record, indent=2, sort_keys=True) + "\n")

    process_returncode: int | None = None
    timed_out = False
    exception_text: str | None = None
    screenshots: list[dict[str, Any]] = []
    runtime_elapsed = 0.0
    cleanup_receipt: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    loader_evidence: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    execution_result_error: str | None = None
    prefix_winevulkan_path = prefix / "drive_c/windows/system32/winevulkan.dll"
    app_windows_path = env["FG_APP_TRACE_PATH"].casefold()
    process_context_path = output / "process-context.json"
    execution_result_path = output / "execution-result.json"
    command = [str(WINE), str(APP)]
    (output / "run-manifest.json").write_text(
        json.dumps({**manifest_record, "command": command,
                    "prefix_winevulkan_path": str(prefix_winevulkan_path),
                    "prior_smoke_authorization": (
                        {"path": str(prior_smoke_path.resolve()),
                         "sha256": sha256(prior_smoke_path),
                         "experiment_id": prior_smoke.get("experiment_id")}
                        if prior_smoke is not None and prior_smoke_path is not None else None)},
                   indent=2, sort_keys=True) + "\n")
    process_context = {
        "experiment_id": experiment_id, "prefix": str(prefix), "output": str(output),
        "wine_server": str(WINESERVER), "environment": env, "command": command,
        "duration_seconds": duration_seconds, "runtime_log": str(runtime_log),
        "execution_result": str(execution_result_path),
    }
    process_context_path.write_text(json.dumps(process_context, indent=2, sort_keys=True) + "\n")
    process = None
    runner_returncode = None
    controller_log = output / "process-runner.log"
    try:
        with controller_log.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(PROCESS_RUNNER), str(process_context_path)],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT, text=True)
            if mode == "smoke":
                smoke_deadline = time.monotonic() + duration_seconds + 300
                last_capture_at = 0.0
                next_capture_at = time.monotonic()
                while process.poll() is None and len(screenshots) < 2 and time.monotonic() < smoke_deadline:
                    now = time.monotonic()
                    host_trace_bytes = max(
                        [p.stat().st_size for p in host_trace_directory.glob("host-trace.*.jsonl")],
                        default=0,
                    )
                    rendering_active = (
                        len(screenshots) > 0
                        or (app_trace.is_file() and app_trace.stat().st_size > 100_000)
                        or host_trace_bytes > 20_000
                    )
                    if runtime_log.is_file() and rendering_active and now >= next_capture_at:
                        screenshot_path = output / f"smoke-window-{len(screenshots) + 1}.png"
                        observation = capture_window(screenshot_path)
                        if observation.get("status") == "PASS":
                            observation["captured_after_first_success_seconds"] = (
                                0.0 if not screenshots else now - last_capture_at
                            )
                            if not screenshots or now - last_capture_at >= 10.0:
                                screenshots.append(observation)
                                last_capture_at = now
                                next_capture_at = now + 10.0
                            else:
                                next_capture_at = last_capture_at + 10.0
                        else:
                            next_capture_at = now + 4.0
                    try:
                        process.wait(timeout=min(0.5, max(0.05, next_capture_at - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
            try:
                runner_returncode = process.wait(timeout=duration_seconds + 300)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    runner_returncode = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    runner_returncode = process.wait(timeout=10)
    except BaseException as exc:
        exception_text = f"{type(exc).__name__}: {exc}"

    execution_result, execution_result_error = read_execution_result(execution_result_path)
    if execution_result is not None:
        process_returncode = execution_result.get("process_returncode")
        timed_out = timed_out or bool(execution_result.get("timed_out"))
        try:
            runtime_elapsed = float(execution_result.get("runtime_process_elapsed_seconds", 0.0))
        except (TypeError, ValueError) as exc:
            exception_text = exception_text or f"execution result elapsed time is invalid: {exc!r}"
    elif runner_returncode not in (None, 0):
        detail = f": {execution_result_error}" if execution_result_error else ""
        exception_text = (exception_text or
                          f"PrefixLease process runner exited {runner_returncode} without execution result{detail}")
    elif execution_result_error:
        exception_text = exception_text or execution_result_error

    cleanup_path = output / "prefix-cleanup.json"
    if cleanup_path.is_file() and not cleanup_path.is_symlink():
        try:
            cleanup_receipt = json.loads(cleanup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            exception_text = exception_text or f"cleanup receipt parse failed: {exc!r}"
    if not runtime_log.is_file():
        validation_error = "Wine runtime log was not created"
        exception_text = exception_text or validation_error
        validation = {"is_valid": False, "error": validation_error}
    elif execution_result is None:
        validation_error = execution_result_error or "execution result is unavailable"
        exception_text = exception_text or validation_error
        validation = {"is_valid": False, "error": validation_error}
    else:
        try:
            if (execution_result.get("prefix_winevulkan_path") != str(prefix_winevulkan_path)
                    or execution_result.get("prefix_winevulkan_sha256") != inputs["winevulkan_dll"]["sha256"]):
                raise ValidationError("captured materialized DLL identity differs from the exact leased prefix/runtime")
            prefix_winevulkan_identity = {
                "path": execution_result["prefix_winevulkan_path"],
                "sha256": execution_result["prefix_winevulkan_sha256"],
            }
            trace_index = build_trace_index(output, execution_result["target_unix_pid"],
                                            env["FG_B_EXPERIMENT_TOKEN"])
            # The index selects by PrefixLease's exec PID, never by arrival order.
            host_trace = Path(trace_index["target_trace_path"])
            host_events = TraceValidator().load_file(host_trace)
            app_events = TraceValidator().load_file(app_trace)
            startup_identities = {
                "wine": {"path": inputs["wine_host_image"]["path"], "sha256": inputs["wine_host_image"]["sha256"]},
                "wine_launcher": {"path": inputs["wine_launcher"]["path"],
                                  "sha256": inputs["wine_launcher"]["sha256"]},
                "wineserver": {"path": inputs["wineserver"]["path"], "sha256": inputs["wineserver"]["sha256"]},
                "winevulkan_dll": {"path": prefix_winevulkan_identity["path"],
                                   "sha256": prefix_winevulkan_identity["sha256"]},
                "winevulkan_so": {"path": inputs["winevulkan_so"]["path"],
                                   "sha256": inputs["winevulkan_so"]["sha256"]},
                "wine_host_loader": {"path": inputs["wine_host_loader"]["path"],
                                     "sha256": inputs["wine_host_loader"]["sha256"]},
                "winemac_so": {"path": inputs["winemac_so"]["path"],
                               "sha256": inputs["winemac_so"]["sha256"]},
                "trace_proxy": {"path": str(copied_proxy.resolve()),
                                "sha256": inputs["trace_proxy"]["sha256"]},
                "moltenvk": {"path": str(copied_mvk.resolve()),
                             "sha256": inputs["moltenvk_original"]["sha256"]},
            }
            validate_target_association(app_events, trace_index)
            app_configs = [row for row in app_events if row.get("origin") == "APP_CONFIG"]
            if len(app_configs) != 1:
                raise ValidationError("expected exactly one target APP_CONFIG")
            loader_evidence = parse_loader_evidence(
                runtime_log, output, prefix_winevulkan_identity,
                target_unix_pid=execution_result["target_unix_pid"],
                app_windows_pid=app_configs[0]["process_id"], host_trace_path=host_trace,
                expected_module_identities=startup_identities, expected_app_path=APP,
                expected_app_sha256=inputs["app"]["sha256"],
                app_windows_path=windows_path(APP),
                expected_moltenvk_version=EXPECTED_MOLTENVK_VERSION,
                expected_runtime_winevulkan_path=WINEVULKAN_DLL,
                app_config=app_configs[0])
            trace_artifacts = {role: {"path": str(path.resolve()), "sha256": sha256(path)}
                               for role, path in (("target_host_trace", host_trace),
                                                  ("trace_index", output / "trace-index.json"),
                                                  ("app_trace", app_trace))}
            verify_trace_artifact_bindings(output, trace_artifacts)
            validator = BareWineTraceValidator(
                expected_original_vulkan_path=str(copied_mvk),
                expected_moltenvk_paths=[str(copied_mvk)],
                expected_startup_identities=startup_identities,
                expected_app_path=str(APP), expected_app_sha256=inputs["app"]["sha256"],
                expected_moltenvk_version=EXPECTED_MOLTENVK_VERSION,
                expected_duration_seconds=duration_seconds,
                expected_target_unix_pid=execution_result["target_unix_pid"],
                expected_experiment_token=env["FG_B_EXPERIMENT_TOKEN"])
            validation = validator.validate(app_events, host_events, loader_evidence=loader_evidence)
            app_config = validator._one(validator._rows(app_events, "APP_CONFIG", "app_configuration"),
                                        "app configuration")
            if str(app_config.get("trace_path", "")).casefold() != app_windows_path:
                raise ValidationError("app trace path in the PE does not match the exact per-run output")
            validation["app_config_trace_path_matches"] = True
        except BaseException as exc:
            exception_text = exception_text or f"trace validation failed: {type(exc).__name__}: {exc}"
            validation = {"is_valid": False, "error": str(exc)}
            index_path = output / "trace-index.json"
            if index_path.is_file() and not index_path.is_symlink():
                trace_index = json.loads(index_path.read_text())
            pid = execution_result.get("target_unix_pid") if execution_result is not None else None
            if isinstance(pid, int) and pid > 0:
                candidate_target = host_trace_directory / f"host-trace.{pid}.jsonl"
                if candidate_target.is_file() and not candidate_target.is_symlink():
                    host_trace = candidate_target

    try:
        binding_stable = smoke_authorization_bindings() == binding_snapshot
    except BaseException as exc:
        binding_stable = False
        exception_text = exception_text or f"runtime/control identity recheck failed: {type(exc).__name__}: {exc}"
    post = gate0_snapshot("POST")
    storage_manifest_sha256 = sha256(Path(manifest["manifest_path"]))
    cleanup_good = automatic_post_matches(cleanup_receipt, experiment_id, prefix,
                                          storage_manifest_sha256)
    screenshot_content = (inspect_screenshot_content(screenshots, output)
                          if mode == "smoke" else None)
    screenshots_good = (mode != "smoke" or
                        len(screenshots) == 2
                        and all(row.get("status") == "PASS" for row in screenshots)
                        and screenshot_content is not None
                        and screenshot_content.get("status") == "PASS")
    app_good = process_returncode == 0 and not timed_out
    post_good = (post["gate0_status"] == "PASS" and post["free_bytes"] >= 45 * GIB
                 and post["scoped_fgmetal_bytes"] <= 45 * GIB)
    trace_good = bool(validation and validation.get("is_valid") is True
                      and validation.get("quantitative_ready") is True)
    valid = (app_good and trace_good and cleanup_good and post_good
             and screenshots_good and binding_stable and exception_text is None)
    result = {
        "schema_version": 1, "experiment_id": experiment_id, "phase": phase,
        "status": "PASS" if valid else "FAIL", "is_valid": valid,
        "duration_seconds_requested": duration_seconds,
        "process_returncode": process_returncode, "timed_out": timed_out,
        "runtime_process_elapsed_seconds": runtime_elapsed,
        "exception": exception_text, "execution_result_error": execution_result_error,
        "gate0_pre": pre, "gate0_post": post,
        "storage_manifest_sha256": storage_manifest_sha256,
        "prefix_cleanup": cleanup_receipt, "prefix_cleanup_pass": cleanup_good,
        "screenshot_checks": screenshots, "screenshot_content_check": screenshot_content,
        "screenshot_check_pass": screenshots_good,
        "loader_evidence": loader_evidence, "validation": validation,
        "control_binding_snapshot": binding_snapshot,
        "control_binding_stable_through_run": binding_stable,
        "runtime_log_path": str(runtime_log), "process_runner_log_path": str(controller_log),
        "process_runner_returncode": runner_returncode,
        "app_trace_path": str(app_trace) if app_trace.is_file() else None,
        "host_trace_path": str(host_trace) if host_trace is not None and host_trace.is_file() else None,
        "trace_index": trace_index, "trace_artifacts": trace_artifacts,
        "host_trace_directory": str(host_trace_directory),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    result_path = output / "run-result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    authorization_path = None
    if mode == "smoke" and valid and trace_good and validation is not None:
        authorization = {
            "schema_version": 1, "experiment_id": experiment_id, "phase": "smoke",
            "status": "PASS", "is_valid": True,
            "quantitative_ready": validation.get("quantitative_ready") is True,
            "prefixlease_automatic_post": "PASS" if cleanup_good else "FAIL",
            "gate0_post": "PASS" if post_good else "FAIL",
            "run_result_path": str(result_path.resolve()),
            "run_result_sha256": sha256(result_path),
            "bindings": binding_snapshot, "trace_artifacts": trace_artifacts,
            "authenticated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if authorization["quantitative_ready"] and cleanup_good and post_good:
            sealed_authorization = storage_policy._seal_storage_evidence(
                authorization, SMOKE_AUTH_FIELD)
            authorization_path = output / "smoke-authorization.json"
            authorization_path.write_text(
                json.dumps(sealed_authorization, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
    print(json.dumps({"status": result["status"], "experiment_id": experiment_id,
                      "report": str(result_path), "gate0_pre": pre["gate0_status"],
                      "gate0_post": post["gate0_status"],
                      "trace_validation": trace_good,
                      "presented_fps": validation.get("presented_fps") if validation else None,
                      "next_drawable_max_ns": (validation.get("next_drawable", {}).get("maximum_ns")
                                                if validation else None),
                      "smoke_authorization": (str(authorization_path)
                                              if authorization_path else None),
                      "exception": exception_text}, indent=2, sort_keys=True))
    return result


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "smoke":
        result = run_one("smoke")
    elif (len(sys.argv) == 4 and sys.argv[1] == "baseline"
          and sys.argv[2] in {"1", "2", "3"}):
        result = run_one("baseline", int(sys.argv[2]), sys.argv[3])
    else:
        print("usage: runner_b.py smoke | baseline 1|2|3 <passing-smoke-id>", file=sys.stderr)
        return 2
    return 0 if result.get("is_valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
