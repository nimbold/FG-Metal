#!/usr/bin/env python3
"""Run the matched 30 Hz R5 cadence sequence and quantify RSS monitor cost."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
R5_EVIDENCE = Path("/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/renderer-integration")
TEMPLATE = R5_EVIDENCE / "run_metal_invert_rate_current_r5_controlled.py"
APP = R5_EVIDENCE / "slow-visual-metal.exe"
BRIDGE = Path("/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof/build/status-v3-current-20261006")
RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
PREFIX_ROOT = Path("/Users/nima/Library/Caches/FGMetalStep11D-R")
DURATION = 300
SOURCE_HZ = 30
SEQUENCE = [("A1", False), ("B1", True), ("A2", False), ("B2", True), ("A3", False), ("B3", True)]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def ps_rows() -> dict[int, tuple[int, int, str]]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,command="],
        check=True, text=True, stdout=subprocess.PIPE,
    ).stdout
    rows: dict[int, tuple[int, int, str]] = {}
    for line in output.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) != 4:
            continue
        try:
            pid, ppid, rss = (int(fields[i]) for i in range(3))
        except ValueError:
            continue
        rows[pid] = (ppid, rss, fields[3])
    return rows


def monitored_processes(root_pid: int, rows: dict[int, tuple[int, int, str]]) -> set[int]:
    pids = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _rss, _command) in rows.items():
            if ppid in pids and pid not in pids:
                pids.add(pid)
                changed = True
    server_path = str(RUNTIME / "bin/wineserver")
    pids.update(pid for pid, (_ppid, _rss, command) in rows.items()
                if command.startswith(server_path))
    return pids


def prepare_runner(name: str, output: Path, prefix: Path) -> Path:
    source = TEMPLATE.read_text(encoding="utf-8")
    substitutions = {
        'OUT = HERE / f"metal-invert-rate-{SOURCE_HZ}hz-{DURATION}s-current-r5-controlled-nocapture"':
        f'OUT = HERE / "{output.relative_to(ROOT).as_posix()}"',
        'PREFIX = Path(f"/Users/nima/Library/Caches/FGMetalStep11D-R/current-r5-controlled-nocapture-{SOURCE_HZ}hz-{DURATION}s-prefix")':
        f'PREFIX = Path("{prefix}")',
        'APP_SOURCE = HERE / "slow-visual-metal.exe"':
        f'APP_SOURCE = Path("{APP}")',
        'BRIDGE_BUILD = HERE.parent.parent / "bridge-gpu-proof" / "build" / "status-v3-current-20261006"':
        f'BRIDGE_BUILD = Path("{BRIDGE}")',
    }
    for old, new in substitutions.items():
        if source.count(old) != 1:
            raise RuntimeError(f"expected one runner marker for {name}: {old}")
        source = source.replace(old, new)
    runner = ROOT / f"runner-{name}.py"
    if runner.exists():
        raise FileExistsError(runner)
    runner.write_text(source, encoding="utf-8")
    runner.chmod(0o755)
    return runner


def run_trial(name: str, monitored: bool) -> dict[str, object]:
    output = ROOT / name
    prefix = PREFIX_ROOT / f"step11d1-cadence-{name}-prefix"
    if output.exists() or prefix.exists():
        raise FileExistsError(f"refusing to overwrite {output} or {prefix}")
    runner = prepare_runner(name, output, prefix)
    wrapper_log = ROOT / f".{name}-runner-output.tmp.log"
    monitor_csv = output / "memory-samples.csv"
    monitor_result: dict[str, object] | None = None
    if wrapper_log.exists():
        raise FileExistsError(wrapper_log)

    with wrapper_log.open("w", encoding="utf-8") as log:
        start = time.monotonic()
        self_cpu_start = sum((resource.getrusage(resource.RUSAGE_SELF).ru_utime,
                              resource.getrusage(resource.RUSAGE_SELF).ru_stime))
        proc = subprocess.Popen(
            [sys.executable, str(runner), str(SOURCE_HZ), str(DURATION)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
        if monitored:
            samples: list[dict[str, object]] = []
            ps_cpu_seconds = 0.0
            full_process_rows = 0
            sleep_wakeups = 0
            next_sample = start
            while proc.poll() is None:
                now = time.monotonic()
                children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
                rows = ps_rows()
                children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
                ps_cpu_seconds += ((children_after.ru_utime - children_before.ru_utime)
                                   + (children_after.ru_stime - children_before.ru_stime))
                full_process_rows += len(rows)
                pids = monitored_processes(proc.pid, rows)
                rss_kib = sum(rows[pid][1] for pid in pids if pid in rows)
                samples.append({
                    "elapsed_seconds": round(now - start, 3),
                    "sampled_at_unix": round(time.time(), 3),
                    "process_count": len(pids),
                    "pid_list": ";".join(str(pid) for pid in sorted(pids)),
                    "rss_kib_sum": rss_kib,
                    "rss_mib_sum": round(rss_kib / 1024, 2),
                })
                next_sample += 1.0
                delay = next_sample - time.monotonic()
                if delay > 0:
                    sleep_wakeups += 1
                    time.sleep(delay)
            returncode = proc.wait()
            self_cpu_end = sum((resource.getrusage(resource.RUSAGE_SELF).ru_utime,
                                resource.getrusage(resource.RUSAGE_SELF).ru_stime))
            monitor_elapsed = max(0.001, time.monotonic() - start)
            monitor_cpu_seconds = (self_cpu_end - self_cpu_start) + ps_cpu_seconds
            rss = [float(sample["rss_mib_sum"]) for sample in samples]
            monitor_result = {
                "sample_count": len(samples),
                "sample_interval_seconds_nominal": 1,
                "sample_loop_timer_wakeups": sleep_wakeups,
                "sampling_command": "ps -axo pid=,ppid=,rss=,command=",
                "sampling_process": "run_matrix.py; one full-host ps child per sample",
                "ps_commands": len(samples),
                "full_process_table_rows_scanned": full_process_rows,
                "process_tree_enumeration": "yes; all process rows read each sample, descendants selected by PPID; matching frozen-runtime wineserver added",
                "wine_child_querying": "one full process-table query per second; no repeated per-Wine-child query",
                "monitor_cpu_seconds_self_plus_ps_children": round(monitor_cpu_seconds, 3),
                "monitor_cpu_percent_of_wall": round(100 * monitor_cpu_seconds / monitor_elapsed, 3),
                "sampled_elapsed_seconds": round(samples[-1]["elapsed_seconds"], 3) if samples else 0,
                "rss_mib_median": round(statistics.median(rss), 2) if rss else None,
                "rss_mib_max": max(rss) if rss else None,
                "rss_mib_last": rss[-1] if rss else None,
                "wakeups_scope": "counts deliberate one-second sample-loop wakes; no kernel per-process wakeup counter was collected",
                "monitor_csv": str(monitor_csv),
            }
        else:
            try:
                returncode = proc.wait(timeout=DURATION + 120)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                returncode = 124

    output.mkdir(exist_ok=True)
    wrapper_log.replace(output / "runner-output.log")
    if monitored:
        with monitor_csv.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(samples[0]) if samples else [
                "elapsed_seconds", "sampled_at_unix", "process_count", "pid_list",
                "rss_kib_sum", "rss_mib_sum",
            ])
            writer.writeheader()
            writer.writerows(samples)
        (output / "monitoring-overhead.json").write_text(
            json.dumps(monitor_result, indent=2) + "\n", encoding="utf-8")

    child_result = json.loads((output / "result.json").read_text(encoding="utf-8")) \
        if (output / "result.json").exists() else {}
    if returncode == 0 and child_result.get("provider_ready"):
        wine_env = os.environ.copy()
        for key in list(wine_env):
            if key.startswith(("WINE", "DXVK_", "FG_", "MVK_", "MTL_", "VK_", "DYLD_")):
                wine_env.pop(key, None)
        runtime_unix = RUNTIME / "lib/wine/x86_64-unix"
        dyld = ":".join((str(BRIDGE / "mvk142-loader-override"), str(runtime_unix),
                         "/private/tmp/fgmetal-vulkan-link"))
        wine_env.update({"WINEPREFIX": str(prefix), "WINEARCH": "win64",
                         "WINELOADER": str(RUNTIME / "bin/wine"),
                         "WINESERVER": str(RUNTIME / "bin/wineserver"),
                         "WINEDEBUG": "-all", "DYLD_LIBRARY_PATH": dyld,
                         "DYLD_FALLBACK_LIBRARY_PATH": dyld + ":" + str(RUNTIME / "lib")})
        shutdown_path = output / "wineserver-shutdown.log"
        shutdown_ok = True
        with shutdown_path.open("w", encoding="utf-8") as stream:
            for flag in ("-k", "-w"):
                try:
                    shutdown = subprocess.run(
                        ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wineserver"), flag],
                        env=wine_env, stdout=stream, stderr=subprocess.STDOUT,
                        timeout=30, check=False,
                    )
                    if shutdown.returncode != 0:
                        shutdown_ok = False
                except subprocess.TimeoutExpired:
                    shutdown_ok = False
                    break
        if shutdown_ok and prefix.is_dir():
            total_bytes = 0
            file_count = 0
            for item in prefix.rglob("*"):
                if item.is_file():
                    total_bytes += item.stat().st_size
                    file_count += 1
            shutil.rmtree(prefix)
            (output / "prefix-cleanup.json").write_text(json.dumps({
                "created_by_this_trial": True,
                "prefix_path": str(prefix),
                "wineserver_kill_and_wait_returned_zero": True,
                "removed_files": file_count,
                "removed_bytes": total_bytes,
                "reason": "release isolated temporary Wine prefix after successful normal test exit; logs and manifests are retained",
            }, indent=2) + "\n", encoding="utf-8")
        else:
            (output / "prefix-cleanup.json").write_text(json.dumps({
                "created_by_this_trial": True,
                "prefix_path": str(prefix),
                "wineserver_kill_and_wait_returned_zero": False,
                "removed": False,
                "reason": "preserved prefix because Wine server teardown did not verify cleanly",
            }, indent=2) + "\n", encoding="utf-8")
    status = {
        "trial": name,
        "monitoring_enabled": monitored,
        "wrapper_returncode": returncode,
        "result": child_result,
        "monitoring_overhead": monitor_result,
    }
    (output / "trial-summary.json").write_text(json.dumps(status, indent=2) + "\n",
                                               encoding="utf-8")
    return status


def main() -> int:
    raise SystemExit(
        "legacy six-trial matrix is disabled because it creates and removes Wine prefixes itself; "
        "run arms through the Step 11D.3 storage-managed PrefixLease driver"
    )
    if not TEMPLATE.is_file() or not APP.is_file() or not BRIDGE.is_dir():
        raise SystemExit("verified R5 renderer integration inputs are unavailable")
    expected = {
        "d3d11.dll": (Path("/private/tmp/fgmetal-step11d-r-current-r5-build-A-20261006/src/d3d11/d3d11.dll"),
                      "e747ca5a5c2087d1f788762a5c4bae13939593a2cb89dedf2f7c04999ab94de5"),
        "dxgi.dll": (Path("/private/tmp/fgmetal-step11d-r-current-r5-build-A-20261006/src/dxgi/dxgi.dll"),
                     "58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132"),
        "FGMetalBridge.dll": (BRIDGE / "overlay/x86_64-windows/FGMetalBridge.dll",
                              "44cdaa3ff503a1beb3afd735620c3b33eac4bca17377fa78e5c3be5f852c18cf"),
        "fgmetalbridge.so": (BRIDGE / "overlay/x86_64-unix/fgmetalbridge.so",
                             "033e3a8cbd957b1e16131fc690256aba8684d51b17688514a5ff7eaa351c2fe2"),
    }
    identities = {name: {"path": str(path), "sha256": sha256(path)} for name, (path, _) in expected.items()}
    mismatches = [name for name, (path, wanted) in expected.items() if sha256(path) != wanted]
    (ROOT / "input-identities.json").write_text(json.dumps(identities, indent=2) + "\n",
                                                encoding="utf-8")
    if mismatches:
        raise SystemExit(f"R5 frozen hash mismatch: {mismatches}")
    results: list[dict[str, object]] = []
    for name, monitored in SEQUENCE:
        print(f"START {name} monitor={monitored}", flush=True)
        result = run_trial(name, monitored)
        results.append(result)
        print(json.dumps({"finished": name, "returncode": result["wrapper_returncode"],
                          "source_rows": result["result"].get("source_present_rows"),
                          "commits": result["result"].get("metal_job_commits"),
                          "consumers": result["result"].get("metal_vulkan_consumers"),
                          "internal_presents": result["result"].get("internal_metal_presents")},
                         sort_keys=True), flush=True)
        if result["wrapper_returncode"] != 0:
            (ROOT / "sequence-results.json").write_text(json.dumps(results, indent=2) + "\n",
                                                         encoding="utf-8")
            return int(result["wrapper_returncode"])
    (ROOT / "sequence-results.json").write_text(json.dumps(results, indent=2) + "\n",
                                                 encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
