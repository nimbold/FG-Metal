#!/usr/bin/env python3
"""Deterministic, offline shared-trace O_EXCL collision proof for BR4."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import select
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEP = HERE.parents[2]
PROXY = STEP / "control/libvulkan-trace.dylib"
DELEGATE = STEP / "runs/step11d3b-smoke-20261009-122135-b7e17c8f/mvk/libMoltenVK.1.dylib"
HARNESS_SOURCE = HERE / "collision_harness.cpp"
INTERPOSE_SOURCE = HERE / "open_interpose.c"
HARNESS = HERE / "collision_harness"
INTERPOSE = HERE / "open_interpose.dylib"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_ready(process: subprocess.Popen[bytes], timeout: float = 20.0) -> tuple[bytes, str]:
    output = bytearray()
    deadline = time.monotonic() + timeout
    assert process.stdout is not None
    fd = process.stdout.fileno()
    while time.monotonic() < deadline:
        if process.poll() is not None:
            readable, _, _ = select.select([fd], [], [], 0)
            if not readable:
                break
        remaining = deadline - time.monotonic()
        readable, _, _ = select.select([fd], [], [], max(0.0, min(0.25, remaining)))
        if readable:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            output.extend(chunk)
            if b"READY pid=" in output:
                return bytes(output), "ready"
    return bytes(output), "exited_or_timed_out"


def drain_stdout(process: subprocess.Popen[bytes]) -> bytes:
    output = bytearray()
    assert process.stdout is not None
    fd = process.stdout.fileno()
    while True:
        readable, _, _ = select.select([fd], [], [], 0.25)
        if not readable:
            if process.poll() is not None:
                readable, _, _ = select.select([fd], [], [], 0)
                if not readable:
                    break
            else:
                continue
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        output.extend(chunk)
    return bytes(output)


def wait_for_stable_trace(path: Path, timeout: float = 12.0) -> tuple[bytes, list[dict]]:
    deadline = time.monotonic() + timeout
    previous: bytes | None = None
    stable_samples = 0
    last_bytes = b""
    last_rows: list[dict] = []
    while time.monotonic() < deadline:
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = b""
        last_bytes = current
        try:
            last_rows = [json.loads(line) for line in current.decode("utf-8").splitlines() if line]
        except (UnicodeDecodeError, json.JSONDecodeError):
            last_rows = []
        has_clock = any(row.get("event") == "clock_metadata" for row in last_rows)
        if has_clock and current == previous:
            stable_samples += 1
            if stable_samples >= 4:
                return current, last_rows
        else:
            stable_samples = 0
        previous = current
        time.sleep(0.15)
    raise RuntimeError(f"trace did not stabilize with a clock record; last_bytes={len(last_bytes)} rows={len(last_rows)}")


def parse_lines(stderr: str) -> tuple[list[dict], list[dict]]:
    processes: list[dict] = []
    opens: list[dict] = []
    for line in stderr.splitlines():
        process_match = re.fullmatch(
            r"DYLD_INTERPOSE_PROCESS pid=(\d+) architecture=([^ ]+) translated=(-?\d+)(?: .*)?", line
        )
        if process_match:
            processes.append({
                "pid": int(process_match.group(1)),
                "architecture": process_match.group(2),
                "translated": int(process_match.group(3)),
                "raw": line,
            })
        open_match = re.fullmatch(
            r"DYLD_INTERPOSE pid=(\d+) symbol=([^ ]+) path=(.*) flags_dec=(-?\d+) "
            r"flags_hex=(0x[0-9a-fA-F]+) mode_dec=(\d+) mode_octal=(0[0-7]+) "
            r"result=(-?\d+) errno=(\d+) errno_text=(.*)", line
        )
        if open_match:
            opens.append({
                "pid": int(open_match.group(1)),
                "symbol": open_match.group(2),
                "path": open_match.group(3),
                "flags_dec": int(open_match.group(4)),
                "flags_hex": open_match.group(5),
                "mode_dec": int(open_match.group(6)),
                "mode_octal": open_match.group(7),
                "result": int(open_match.group(8)),
                "errno": int(open_match.group(9)),
                "errno_text": open_match.group(10),
                "raw": line,
            })
    return processes, opens


def main() -> int:
    if not PROXY.is_file() or not DELEGATE.is_file():
        raise SystemExit(f"missing pinned proxy/delegate: {PROXY} / {DELEGATE}")

    compile_commands = [
        ["xcrun", "--sdk", "macosx", "clang++", "-std=c++17", "-O2", "-arch", "x86_64",
         str(HARNESS_SOURCE), "-o", str(HARNESS)],
        ["xcrun", "--sdk", "macosx", "clang", "-std=c11", "-O2", "-arch", "x86_64",
         "-dynamiclib", str(INTERPOSE_SOURCE), "-o", str(INTERPOSE), "-ldl"],
    ]
    compile_results = []
    for command in compile_commands:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   check=False)
        compile_results.append({"command": command, "returncode": completed.returncode,
                                "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode != 0:
            raise RuntimeError(f"compile failed: {command}\n{completed.stderr}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = HERE / f"run-{stamp}-{uuid.uuid4().hex[:8]}"
    run.mkdir(mode=0o700)
    trace = run / "host-trace.jsonl"
    trace_path = str(trace.resolve())
    proxy_path = str(PROXY.resolve())
    delegate_path = str(DELEGATE.resolve())
    proxy_sha = sha256(PROXY)
    delegate_sha = sha256(DELEGATE)
    if proxy_sha != "7b60124e8a211dbb8155101030311bd12015ba10b6da51c86a3557c682494b61":
        raise RuntimeError(f"pinned proxy hash changed: {proxy_sha}")

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "DYLD_INSERT_LIBRARIES": str(INTERPOSE.resolve()),
        "FG_B_MVK_TRACE_PATH": trace_path,
        "FG_B_TRACE_PROXY_PATH": proxy_path,
        "FG_B_TRACE_PROXY_SHA256": proxy_sha,
        "FG_B_ORIGINAL_MOLTENVK_LIBRARY": delegate_path,
        "FG_B_ORIGINAL_MOLTENVK_SHA256": delegate_sha,
        "FG_B_EXPECTED_MOLTENVK_VERSION": "MoltenVK v1.4.2",
    }
    first_command = [str(HARNESS.resolve()), proxy_path]
    second_command = [str(HARNESS.resolve()), proxy_path]
    first_stderr_path = run / "first.stderr.txt"
    first_stderr_file = first_stderr_path.open("wb")
    first = subprocess.Popen(first_command, cwd=str(run), env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=first_stderr_file,
                             text=False, bufsize=0)
    first_prefix, first_ready = wait_for_ready(first)
    if first_ready != "ready":
        if first.poll() is None:
            first.terminate()
        first.wait(timeout=10)
        first_rest = drain_stdout(first)
        first_stderr_file.flush()
        first_stderr_file.close()
        first_stdout = first_prefix + first_rest
        first_stderr = first_stderr_path.read_text(encoding="utf-8")
        (run / "first.stdout.txt").write_bytes(first_stdout)
        failure = {
            "status": "FAIL",
            "reason": "first proxy process did not reach READY",
            "returncode": first.returncode,
            "command": first_command,
            "environment": env,
            "proxy_sha256": proxy_sha,
            "delegate_sha256": delegate_sha,
            "stdout": first_stdout.decode("utf-8", errors="replace"),
            "stderr": first_stderr,
            "trace_exists": trace.exists(),
            "trace_sha256": sha256(trace) if trace.exists() else None,
        }
        (run / "collision-proof.failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise RuntimeError(f"first proxy process did not reach READY; captured {run / 'collision-proof.failure.json'}")
    if first.poll() is not None:
        raise RuntimeError("first process exited before the second process was started")
    first_trace_snapshot, first_trace_rows = wait_for_stable_trace(trace)

    second_started_while_first_alive = first.poll() is None
    second = subprocess.run(second_command, cwd=str(run), env=env, input="", text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
    first_still_alive_after_second = first.poll() is None
    trace_bytes_after_second = trace.read_bytes()
    trace_unchanged_during_collision = trace_bytes_after_second == first_trace_snapshot

    assert first.stdin is not None
    first.stdin.write(b"release\n")
    first.stdin.close()
    first.wait(timeout=20)
    first_rest_stdout = drain_stdout(first)
    first_stderr_file.flush()
    first_stderr_file.close()
    first_stdout = (first_prefix + first_rest_stdout).decode("utf-8", errors="replace")
    first_stderr = first_stderr_path.read_text(encoding="utf-8")
    first_trace_final = trace.read_bytes()
    first_trace_rows_final = [json.loads(line) for line in first_trace_final.decode("utf-8").splitlines() if line]

    first_process_records, first_open_records = parse_lines(first_stderr)
    second_process_records, second_open_records = parse_lines(second.stderr)
    all_process_records = first_process_records + second_process_records
    all_open_records = first_open_records + second_open_records
    first_pid_match = re.search(r"^harness_pid=(\d+)$", first_stdout, re.MULTILINE)
    ready_pid_match = re.search(r"^READY pid=(\d+)$", first_stdout, re.MULTILINE)
    second_pid_records = second_process_records

    first_trace_opens = [entry for entry in first_open_records if entry["path"] == trace_path]
    second_trace_opens = [entry for entry in second_open_records if entry["path"] == trace_path]
    first_process = first_process_records[0] if first_process_records else None
    second_process = second_process_records[0] if second_process_records else None
    clock_rows = [row for row in first_trace_rows_final if row.get("event") == "clock_metadata"]
    summary_rows = [row for row in first_trace_rows_final if row.get("event") == "trace_summary"]

    proof = bool(
        first.returncode == 0
        and first_pid_match
        and ready_pid_match
        and first_process
        and first_process["pid"] == int(first_pid_match.group(1))
        and first_process["architecture"] == "x86_64"
        and first_process["translated"] == 1
        and first_trace_opens
        and first_trace_opens[0]["result"] >= 0
        and first_trace_opens[0]["errno"] == 0
        and second.returncode == 86
        and second_process
        and second_process["architecture"] == "x86_64"
        and second_process["translated"] == 1
        and len(second_trace_opens) == 1
        and second_trace_opens[0]["result"] == -1
        and second_trace_opens[0]["errno"] == 17
        and second_trace_opens[0]["errno_text"] == "File exists"
        and second_trace_opens[0]["flags_dec"] == first_trace_opens[0]["flags_dec"]
        and second_trace_opens[0]["mode_octal"] == first_trace_opens[0]["mode_octal"]
        and second_started_while_first_alive
        and first_still_alive_after_second
        and trace_unchanged_during_collision
        and len(clock_rows) == 1
        and len(summary_rows) == 1
        and proxy_sha == "7b60124e8a211dbb8155101030311bd12015ba10b6da51c86a3557c682494b61"
        and sha256(PROXY) == proxy_sha
    )

    (run / "first.stdout.txt").write_text(first_stdout, encoding="utf-8")
    (run / "first.stderr.txt").write_text(first_stderr, encoding="utf-8")
    (run / "second.stdout.txt").write_text(second.stdout, encoding="utf-8")
    (run / "second.stderr.txt").write_text(second.stderr, encoding="utf-8")
    (run / "host-trace.jsonl").write_bytes(first_trace_final)

    report = {
        "schema_version": 1,
        "status": "PASS" if proof else "FAIL",
        "proof_claim": "The unchanged BR3 proxy opens its shared trace with O_EXCL in the first x86_64 process; a second x86_64 process loading the same proxy with the same path receives EEXIST and exits 86.",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "macos_version": subprocess.run(["sw_vers", "-productVersion"], text=True,
                    stdout=subprocess.PIPE, check=False).stdout.strip()},
        "commands": {
            "compile": compile_results,
            "first": first_command,
            "second": second_command,
            "working_directory": str(run.resolve()),
            "environment": env,
        },
        "artifacts": {
            "run_directory": str(run.resolve()),
            "runner_source": {"path": str(Path(__file__).resolve()), "sha256": sha256(Path(__file__))},
            "proxy": {"path": proxy_path, "sha256_before": proxy_sha,
                      "sha256_after": sha256(PROXY)},
            "delegate": {"path": delegate_path, "sha256": delegate_sha},
            "harness_source": {"path": str(HARNESS_SOURCE.resolve()), "sha256": sha256(HARNESS_SOURCE)},
            "harness_binary": {"path": str(HARNESS.resolve()), "sha256": sha256(HARNESS)},
            "interpose_source": {"path": str(INTERPOSE_SOURCE.resolve()), "sha256": sha256(INTERPOSE_SOURCE)},
            "interpose_binary": {"path": str(INTERPOSE.resolve()), "sha256": sha256(INTERPOSE)},
            "trace": {"path": str((run / 'host-trace.jsonl').resolve()), "sha256": sha256(run / 'host-trace.jsonl'),
                      "bytes_before_second": len(first_trace_snapshot),
                      "sha256_before_second": hashlib.sha256(first_trace_snapshot).hexdigest(),
                      "unchanged_during_collision": trace_unchanged_during_collision,
                      "records": first_trace_rows_final},
            "first_stdout": str((run / "first.stdout.txt").resolve()),
            "first_stderr": str((run / "first.stderr.txt").resolve()),
            "second_stdout": str((run / "second.stdout.txt").resolve()),
            "second_stderr": str((run / "second.stderr.txt").resolve()),
            "report_md": str((run / "collision-proof.md").resolve()),
        },
        "processes": {
            "first": {"pid": int(first_pid_match.group(1)) if first_pid_match else None,
                      "returncode": first.returncode, "ready": bool(ready_pid_match),
                      "still_alive_when_second_started": second_started_while_first_alive,
                      "still_alive_after_second_exit": first_still_alive_after_second,
                      "architecture_record": first_process,
                      "trace_open_records": first_trace_opens},
            "second": {"pid": second_pid_records[0]["pid"] if second_pid_records else None,
                       "returncode": second.returncode,
                       "architecture_record": second_process,
                       "trace_open_records": second_trace_opens,
                       "stdout": second.stdout},
        },
        "assertions": {
            "first_proxy_constructor_succeeded": first.returncode == 0 and bool(ready_pid_match),
            "both_processes_x86_64_rosetta": bool(first_process and second_process
                and first_process["architecture"] == second_process["architecture"] == "x86_64"
                and first_process["translated"] == second_process["translated"] == 1),
            "same_shared_trace_path": len(first_trace_opens) >= 1 and len(second_trace_opens) == 1
                and first_trace_opens[0]["path"] == second_trace_opens[0]["path"] == trace_path,
            "same_open_flags_and_mode": bool(first_trace_opens and second_trace_opens
                and first_trace_opens[0]["flags_dec"] == second_trace_opens[0]["flags_dec"]
                and first_trace_opens[0]["mode_octal"] == second_trace_opens[0]["mode_octal"]),
            "first_open_succeeded": bool(first_trace_opens and first_trace_opens[0]["result"] >= 0),
            "second_open_failed_with_eexist": bool(second_trace_opens and second_trace_opens[0]["result"] == -1
                and second_trace_opens[0]["errno"] == 17 and second_trace_opens[0]["errno_text"] == "File exists"),
            "second_process_exit_86": second.returncode == 86,
            "first_remained_alive_through_collision": second_started_while_first_alive and first_still_alive_after_second,
            "trace_contents_unchanged_by_second": trace_unchanged_during_collision,
            "trace_contains_first_process_clock_and_summary": len(clock_rows) == 1 and len(summary_rows) == 1,
            "proxy_unchanged": proxy_sha == sha256(PROXY),
        },
    }
    json_path = run / "collision-proof.json"
    markdown_path = run / "collision-proof.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    first_open = first_trace_opens[0] if first_trace_opens else {}
    second_open = second_trace_opens[0] if second_trace_opens else {}
    trace_text = (run / "host-trace.jsonl").read_text(encoding="utf-8").rstrip()
    md_lines = [
        "# BR4 shared-trace collision proof", "",
        f"**Result: {report['status']}**", "",
        f"Run ID: `{run.name}`  ", f"Timestamp: `{report['timestamp_utc']}`  ",
        f"Host: `{report['host']['platform']}` ({report['host']['machine']}; macOS {report['host']['macos_version']})", "",
        "This offline two-process run loaded the unchanged proxy. The first x86_64/Rosetta process initialized the proxy and remained alive while the second x86_64/Rosetta process loaded the same proxy with the same shared trace path. `DYLD_INTERPOSE` captured the real `open` arguments and result. No Wine process, PrefixLease, graphics surface, or live graphics lease was used.", "",
        "## Pinned artifacts", "",
        f"- Proxy: `{proxy_path}`; SHA-256 `{proxy_sha}` before and `{sha256(PROXY)}` after.",
        f"- Delegate: `{delegate_path}`; SHA-256 `{delegate_sha}`.",
        f"- Harness source/binary SHA-256: `{sha256(HARNESS_SOURCE)}` / `{sha256(HARNESS)}`.",
        f"- Interposer source/binary SHA-256: `{sha256(INTERPOSE_SOURCE)}` / `{sha256(INTERPOSE)}`.",
        f"- Runner source SHA-256: `{sha256(Path(__file__))}`.",
        f"- Shared trace: `{trace}`; final SHA-256 `{sha256(run / 'host-trace.jsonl')}`.", "",
        "## Commands", "", "Compile commands:", "", "```sh",
    ]
    md_lines.extend("$ " + " ".join(item["command"]) for item in compile_results)
    md_lines.extend(["```", "", "Both processes ran these same arguments sequentially:", "", "```sh"])
    md_lines.extend("$ " + " ".join(command) for command in (first_command, second_command))
    md_lines.extend(["```", "", "Shared child environment:", "", "```text"])
    md_lines.extend(f"{key}={value}" for key, value in sorted(env.items()))
    md_lines.extend([
        "```", "", "The interposer logs PID, symbol, path, flags, mode, return value, and post-call `errno`; it forwards through Darwin `_open$NOCANCEL`.",
        "", "## Results", "",
        "| Process | PID | Architecture | Translated | Open flags | Mode | Return | errno | Exit |",
        "| --- | ---: | --- | ---: | --- | --- | --- | --- | ---: |",
        f"| First | {report['processes']['first']['pid']} | {first_process['architecture'] if first_process else 'unknown'} | {first_process['translated'] if first_process else 'unknown'} | `{first_open.get('flags_hex', 'unknown')}` (`{first_open.get('flags_dec', 'unknown')}`) | `{first_open.get('mode_octal', 'unknown')}` | fd `{first_open.get('result', 'unknown')}` | {first_open.get('errno', 'unknown')} | {first.returncode} |",
        f"| Second | {report['processes']['second']['pid']} | {second_process['architecture'] if second_process else 'unknown'} | {second_process['translated'] if second_process else 'unknown'} | `{second_open.get('flags_hex', 'unknown')}` (`{second_open.get('flags_dec', 'unknown')}`) | `{second_open.get('mode_octal', 'unknown')}` | `{second_open.get('result', 'unknown')}` | {second_open.get('errno', 'unknown')} `{second_open.get('errno_text', 'unknown')}` | {second.returncode} |", "",
        "`0x1000a01` is `O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC`; both calls used mode `0600` and the identical absolute trace path. The first process remained alive through the second attempt. The second open returned `-1`, `errno=17 (EEXIST / File exists)`, then the second process exited `86`.", "",
        f"Trace bytes before the second open: {len(first_trace_snapshot)}. SHA-256 before collision: `{hashlib.sha256(first_trace_snapshot).hexdigest()}`. It was unchanged during the collision: `{trace_unchanged_during_collision}`. After first-process shutdown it contains one clock record and one trace summary:", "", "```jsonl", trace_text, "```", "",
        "Raw logs: [first.stderr.txt](first.stderr.txt), [second.stderr.txt](second.stderr.txt), [first.stdout.txt](first.stdout.txt), [second.stdout.txt](second.stdout.txt).", "",
        f"Complete machine-readable commands, environment, hashes, outputs, trace, and assertions: [collision-proof.json](collision-proof.json) (SHA-256 `{sha256(json_path)}`).", "",
    ])
    markdown_path.write_text("\n".join(md_lines), encoding="utf-8")
    (HERE / "latest-run.json").write_text(json.dumps({"run_directory": str(run.resolve()),
        "status": report["status"], "collision_proof_json": str(json_path.resolve()),
        "collision_proof_markdown": str(markdown_path.resolve())},
        indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "run_directory": str(run.resolve()),
                      "first_pid": report["processes"]["first"]["pid"],
                      "second_pid": report["processes"]["second"]["pid"],
                      "first_open": first_trace_opens, "second_open": second_trace_opens,
                      "second_returncode": second.returncode,
                      "collision_proof_json": str((run / "collision-proof.json").resolve())},
                     indent=2, sort_keys=True))
    return 0 if proof else 1


if __name__ == "__main__":
    raise SystemExit(main())
