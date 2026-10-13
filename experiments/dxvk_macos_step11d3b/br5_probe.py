#!/usr/bin/env python3
"""STEP 11D.3-BR5 diagnostic: minimal vkCreateDevice probe under PrefixLease.

Diagnostic only. Uses the same pinned Wine runtime, process runner, and
PrefixLease as the BR4 smoke but runs control/device_probe.exe. It never alters
global runtime files; the MoltenVK/proxy selection is a run-local directory.

Modes:
  proxy142   BR4 routing: trace proxy -> exact MoltenVK 1.4.2
  bypass142  run-local trace-loader/libMoltenVK.dylib = exact 1.4.2 (no proxy)
  bypass143  run-local trace-loader/libMoltenVK.dylib = retained 1.4.3 fallback
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
import experiments.dxvk_macos_step11d3b.runner_b as rb  # noqa: E402

storage_policy = rb.storage_policy
PROBE = HERE / "control/device_probe.exe"
MVK143 = rb.RUNTIME / "lib/wine/x86_64-unix/libMoltenVK.1.dylib"
SHA143 = "8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["proxy142", "proxy142diag", "bypass142", "bypass143"])
    ap.add_argument("--probe-args", default="")
    ap.add_argument("--winedebug", default="+err,+pid,+tid,+loaddll")
    ap.add_argument("--tag", default="")
    ap.add_argument("--exe", default="probe", choices=["probe", "bare"])
    ns = ap.parse_args()

    nonce = uuid.uuid4().hex[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    experiment_id = f"step11d3b-br5probe-{ns.mode}-{stamp}-{nonce}"
    output = HERE / "runs" / experiment_id
    prefix = storage_policy.PREFIX_ROOT / f"{experiment_id}-prefix"

    pre = rb.gate0_snapshot("PRE")
    if pre["gate0_status"] != "PASS":
        raise RuntimeError("Gate 0 PRE blocked: " + "; ".join(pre["gate0_blockers"]))
    def candidate_files_diagnostic():
        files = {
            "wine_host_image": rb.WINE_HOST_EXECUTABLE, "wine_launcher": rb.WINE,
            "wineserver": rb.WINESERVER, "winevulkan_dll": rb.WINEVULKAN_DLL,
            "winevulkan_so": rb.WINEVULKAN_SO,
            "wine_host_loader": rb.WINE_HOST_LOADER, "winemac_so": rb.WINEMAC_SO,
            "moltenvk_original": rb.MOLTENVK_ORIGINAL,
            "moltenvk_icd_library": rb.MOLTENVK_ICD_LIBRARY,
            "moltenvk_icd": rb.ICD, "wine_runtime_manifest": rb.RUNTIME_MANIFEST,
            "app": rb.APP, "app_source": rb.APP_SOURCE, "trace_proxy": rb.PROXY,
            "trace_proxy_source": rb.PROXY_SOURCE, "process_runner": rb.PROCESS_RUNNER,
            "trace_validator": rb.TRACE_VALIDATOR, "runner": rb.HERE / "runner_b.py",
            "storage_policy": rb.STORAGE / "storage_policy.py",
            "trace_processes": rb.HERE / "trace_processes.py",
            "loader_evidence": rb.HERE / "loader_evidence.py",
        }
        identities = {name: rb.file_identity(path) for name, path in files.items()}
        template_dll = storage_policy.PREFIX_TEMPLATE / "drive_c/windows/system32/winevulkan.dll"
        runtime_manifest = json.loads(rb.RUNTIME_MANIFEST.read_text(encoding="utf-8"))
        identities["template_winevulkan_dll"] = rb.file_identity(template_dll)
        identities["wine_runtime_tree"] = {
            "path": str(rb.RUNTIME), "sha256": runtime_manifest["tree_sha256"],
            "manifest_path": str(rb.RUNTIME_MANIFEST.resolve()),
            "manifest_sha256": identities["wine_runtime_manifest"]["sha256"],
        }
        return identities
    inputs = candidate_files_diagnostic()
    hashes = rb.wine_candidate_hashes(inputs)
    runner = rb.PROCESS_RUNNER.resolve(strict=True)
    manifest = storage_policy.begin_experiment(
        experiment_id=experiment_id,
        purpose=f"STEP 11D.3-BR5 minimal vkCreateDevice probe ({ns.mode}); diagnostic only",
        candidate_hashes=hashes, expected_duration_seconds=60,
        expected_prefix_bytes=3 * rb.GIB, expected_build_bytes=0,
        estimated_persistent_bytes=20 * rb.MIB,
        authorized_runner_paths=[runner], output_dir=output,
        additional={"experiment_step": "11D.3-BR5", "run_phase": "br5probe",
                    **rb.wine_execution_binding(inputs)})
    output.mkdir(parents=True, exist_ok=False)
    (output / "trace-loader").mkdir()
    (output / "mvk").mkdir()
    host_traces = output / "host-traces"
    host_traces.mkdir(mode=0o700)
    selected_mvk = rb.MOLTENVK_ORIGINAL
    selected_sha = rb.sha256(rb.MOLTENVK_ORIGINAL)
    if ns.mode == "bypass143":
        selected_mvk, selected_sha = MVK143, rb.sha256(MVK143)
        assert selected_sha == SHA143
    rb.copy_verified(selected_mvk, output / "mvk/libMoltenVK.1.dylib", selected_sha)
    rb.copy_verified(selected_mvk, output / "mvk/libMoltenVK.dylib", selected_sha)
    DIAG = HERE / "control/libvulkan-trace-br5diag.dylib"
    if ns.mode == "proxy142diag":
        rb.copy_verified(DIAG, output / "trace-loader/libMoltenVK.dylib", rb.sha256(DIAG))
    elif ns.mode == "proxy142":
        rb.copy_verified(rb.PROXY, output / "trace-loader/libMoltenVK.dylib",
                         inputs["trace_proxy"]["sha256"])
    else:
        rb.copy_verified(selected_mvk, output / "trace-loader/libMoltenVK.dylib", selected_sha)
    env = rb.clean_environment(prefix, output, output / "app-trace.jsonl", host_traces, 10,
                               experiment_token=rb.sha256(Path(manifest["manifest_path"]))[:32])
    env["WINEDEBUG"] = ns.winedebug
    if ns.mode == "proxy142diag":
        env["FG_B_TRACE_PROXY_SHA256"] = rb.sha256(DIAG)
    if not ns.mode.startswith("proxy"):
        env["FG_B_TRACE_PROXY_PATH"] = str(output / "trace-loader/libMoltenVK.dylib")
        env["FG_B_TRACE_PROXY_SHA256"] = selected_sha
        env["FG_B_ORIGINAL_MOLTENVK_LIBRARY"] = str(output / "mvk/libMoltenVK.1.dylib")
        env["FG_B_ORIGINAL_MOLTENVK_SHA256"] = selected_sha
        for key in [k for k in env if k.startswith("FG_B_TRACE_DIRECTORY") or k == "FG_B_EXPERIMENT_TOKEN"]:
            del env[key]
    exe = PROBE if ns.exe == "probe" else rb.APP
    command = [str(rb.WINE), str(exe)] + ns.probe_args.split()
    runtime_log = output / "runtime.log"
    context = {"experiment_id": experiment_id, "prefix": str(prefix), "output": str(output),
               "wine_server": str(rb.WINESERVER), "environment": env, "command": command,
               "duration_seconds": 10, "runtime_log": str(runtime_log),
               "execution_result": str(output / "execution-result.json")}
    (output / "process-context.json").write_text(json.dumps(context, indent=2, sort_keys=True) + "\n")
    (output / "br5-config.json").write_text(json.dumps({
        "mode": ns.mode, "probe_args": ns.probe_args, "winedebug": ns.winedebug,
        "exe": str(exe), "probe_sha256": rb.sha256(exe), "selected_mvk_sha256": selected_sha,
        "proxy_sha256": (rb.sha256(DIAG) if ns.mode == "proxy142diag" else inputs["trace_proxy"]["sha256"] if ns.mode == "proxy142" else None),
        "tag": ns.tag}, indent=2) + "\n")
    with (output / "process-runner.log").open("w") as log:
        rc = subprocess.call([sys.executable, str(rb.PROCESS_RUNNER), str(output / "process-context.json")],
                             cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
    print(json.dumps({"experiment_id": experiment_id, "runner_rc": rc, "output": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
