#!/usr/bin/env python3
"""One-shot paired -Q matrix against disposable roots; never touches R23 state."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LSOF = "/usr/sbin/lsof"
FIELDS = "-F0pcRftDrin"
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
BUNDLE = HERE / f"R6-A-R37-LSOF-Q-PAIRED-STATUS-EXPERIMENT-{RUN_ID}-EVIDENCE"
JSON_PATH = HERE / f"R6-A-R37-LSOF-Q-PAIRED-STATUS-EXPERIMENT-{RUN_ID}.json"
MD_PATH = HERE / f"R6-A-R37-LSOF-Q-PAIRED-STATUS-EXPERIMENT-{RUN_ID}.md"
COMPLETE_PATH = HERE / f"R6-A-R37-LSOF-Q-PAIRED-STATUS-EXPERIMENT-{RUN_ID}-COMPLETE.json"
SCANNER_PATH = HERE / "r6a_r33_live_open_capture.py"

spec = importlib.util.spec_from_file_location("r6a_r33_live_open_capture", SCANNER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load the preserved structured lsof parser")
scanner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scanner
spec.loader.exec_module(scanner)

HELPER_CODE = r'''import os,stat,sys
mode,root,child=sys.argv[1:]
fd=None
if mode=='root': fd=os.open(root,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
elif mode=='child': fd=os.open(child,os.O_RDONLY)
elif mode=='cwd': os.chdir(root)
else: raise SystemExit(91)
def snap():
 st=os.fstat(fd) if fd is not None else os.stat('.',follow_symlinks=False)
 return (os.getpid(),os.getppid(),fd if fd is not None else 'cwd',st.st_dev,st.st_ino,stat.S_IFMT(st.st_mode),stat.S_IMODE(st.st_mode))
print('READY '+' '.join(map(str,snap())),flush=True)
for line in sys.stdin:
 cmd=line.strip()
 if cmd=='POST': print('POST '+' '.join(map(str,snap())),flush=True)
 elif cmd=='EXIT': break
if fd is not None: os.close(fd)
'''


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_bytes(path: Path, data: bytes) -> dict[str, Any]:
    path.write_bytes(data)
    return {"path": str(path.relative_to(HERE)), "size_bytes": len(data), "sha256": sha256(data)}


def file_record(path: Path, label: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {"label": label, "path": str(path.relative_to(HERE)), "size_bytes": len(data), "sha256": sha256(data)}


def object_id_from_stat(st: os.stat_result) -> dict[str, Any]:
    return {
        "device": st.st_dev,
        "inode": st.st_ino,
        "file_type": stat.S_IFMT(st.st_mode),
        "mode": stat.S_IMODE(st.st_mode),
    }


def fd_identity(fd: int) -> dict[str, Any]:
    return object_id_from_stat(os.fstat(fd))


def path_snapshot(path: Path) -> dict[str, Any]:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        return {"exists": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        canonical = os.path.realpath(os.fsencode(path), strict=True)
        canonical_b64 = base64.b64encode(canonical).decode("ascii")
        canonical_text = os.fsdecode(canonical)
    except OSError as exc:
        canonical_b64 = None
        canonical_text = None
        canonical_error = f"{type(exc).__name__}: {exc}"
    else:
        canonical_error = None
    return {
        "exists": True,
        "lstat_identity": object_id_from_stat(st),
        "canonical_path": canonical_text,
        "canonical_path_base64": canonical_b64,
        "canonicalization_error": canonical_error,
    }


def create_root(parent: Path, name: str, child: bool = False) -> Path:
    root = parent / name
    root.mkdir(mode=0o700)
    if child:
        (root / "foreign-child.bin").write_bytes(b"R37 disposable lsof status fixture\n")
        os.chmod(root / "foreign-child.bin", 0o600)
    return root


def row_record(row: dict[str, Any]) -> dict[str, Any]:
    fd_raw = row["fd_raw"]
    type_raw = row["fd_type_raw"]
    device_raw = row["device_raw"]
    inode_raw = row["inode_raw"]
    name_raw = row["name_raw"]
    try:
        canonical = os.path.realpath(name_raw, strict=True)
        canonical_error = None
    except OSError as exc:
        canonical = None
        canonical_error = f"{type(exc).__name__}: {exc}"
    return {
        "pid": row["pid"],
        "ppid": row["ppid"],
        "command_raw_base64": base64.b64encode(row["command_raw"]).decode("ascii"),
        "command_raw_repr": repr(row["command_raw"]),
        "fd_raw_base64": base64.b64encode(fd_raw).decode("ascii"),
        "fd_raw_repr": repr(fd_raw),
        "fd_number": row["fd_number"],
        "fd_suffix_base64": base64.b64encode(row["fd_suffix"] or b"").decode("ascii"),
        "type_raw_base64": base64.b64encode(type_raw).decode("ascii"),
        "type_raw_repr": repr(type_raw),
        "device_raw_base64": base64.b64encode(device_raw).decode("ascii"),
        "device_raw_repr": repr(device_raw),
        "device": row["device"],
        "inode_raw_base64": base64.b64encode(inode_raw).decode("ascii"),
        "inode_raw_repr": repr(inode_raw),
        "inode": row["inode"],
        "name_raw_base64": base64.b64encode(name_raw).decode("ascii"),
        "name_raw_repr": repr(name_raw),
        "name_canonical": os.fsdecode(canonical) if canonical is not None else None,
        "name_canonical_base64": base64.b64encode(canonical).decode("ascii") if canonical is not None else None,
        "name_canonical_error": canonical_error,
        "parser_status": row["parser_status"],
    }


def parse_capture(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    try:
        rows, stream = scanner.parse_lsof(raw)
        return [row_record(row) for row in rows], stream, None
    except scanner.LsofParseError as exc:
        return [row_record(row) for row in exc.rows], {
            "complete": False,
            "empty": len(raw) == 0,
            "row_count": len(exc.rows),
            "bytes_consumed": None,
        }, str(exc)


def run_capture(case_id: str, variant: str, target: Path, malformed: bool = False) -> dict[str, Any]:
    argv = [LSOF]
    if variant == "with_q":
        argv.append("-Q")
    argv.append(FIELDS)
    if malformed:
        argv.append("-#")
    argv.extend(["+D", str(target)])
    started = utc_now()
    capture = scanner.run_lsof_bounded(argv, timeout=30.0)
    ended = utc_now()
    stdout = capture["stdout_raw"]
    stderr = capture["stderr_raw"]
    parsed_rows, stream, parser_error = parse_capture(stdout)
    prefix = f"{case_id}-{variant}"
    stdout_sidecar = write_bytes(BUNDLE / f"{prefix}.stdout.bin", stdout)
    stderr_sidecar = write_bytes(BUNDLE / f"{prefix}.stderr.bin", stderr)
    capture_complete = (
        type(capture.get("returncode")) is int
        and not capture.get("timed_out")
        and not capture.get("stdout_capped")
        and not capture.get("stderr_capped")
        and capture.get("stdout_bytes_captured") == capture.get("stdout_total_bytes")
        and capture.get("stderr_bytes_captured") == capture.get("stderr_total_bytes")
    )
    # run_lsof_bounded drains each pipe through EOF unless timeout/cap termination occurs.
    return {
        "case_id": case_id,
        "variant": variant,
        "argv": argv,
        "cwd": os.getcwd(),
        "started_at_utc": started,
        "ended_at_utc": ended,
        "timeout_seconds": 30.0,
        "returncode": capture.get("returncode"),
        "timed_out": capture.get("timed_out"),
        "stdout_capped": capture.get("stdout_capped"),
        "stderr_capped": capture.get("stderr_capped"),
        "stdout_total_bytes": capture.get("stdout_total_bytes"),
        "stderr_total_bytes": capture.get("stderr_total_bytes"),
        "stdout_captured_bytes": capture.get("stdout_bytes_captured"),
        "stderr_captured_bytes": capture.get("stderr_bytes_captured"),
        "stdout_sha256": capture.get("stdout_sha256"),
        "stderr_sha256": capture.get("stderr_sha256"),
        "stdout_captured_sha256": capture.get("stdout_captured_sha256"),
        "stderr_captured_sha256": capture.get("stderr_captured_sha256"),
        "stdout_base64": capture.get("stdout_base64"),
        "stderr_base64": capture.get("stderr_base64"),
        "stdout_repr": repr(stdout),
        "stderr_repr": repr(stderr),
        "stdout_sidecar": stdout_sidecar,
        "stderr_sidecar": stderr_sidecar,
        "lsof_child_pid": capture.get("child_pid"),
        "lsof_child_ppid": capture.get("child_ppid"),
        "lsof_child_identity": capture.get("child_identity"),
        "lsof_child_identity_error": capture.get("child_identity_error"),
        "capture_complete": capture_complete,
        "stdout_pipe_eof": capture_complete,
        "stderr_pipe_eof": capture_complete,
        "parsed_rows": parsed_rows,
        "parser_valid": parser_error is None,
        "parser_error": parser_error,
        "stream": stream,
        "stream_complete": stream.get("complete") is True,
        "stderr_empty": capture.get("stderr_total_bytes") == 0,
        "legacy_scan_complete_predicate": (
            parser_error is None
            and stream.get("complete") is True
            and capture.get("returncode") == 0
            and capture.get("stderr_total_bytes") == 0
            and not capture.get("stdout_capped")
            and not capture.get("stderr_capped")
            and not capture.get("timed_out")
            and "child_identity_error" not in capture
        ),
    }


def spawn_helper(mode: str, root: Path, child: Path | None = None) -> dict[str, Any]:
    cwd = str(root) if mode == "cwd" else str(HERE.parent.parent)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", HELPER_CODE, mode, str(root), str(child or "")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        close_fds=True,
    )
    assert proc.stdout is not None and proc.stdin is not None
    import select
    ready, _, _ = select.select([proc.stdout], [], [], 5.0)
    if not ready:
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError(f"helper readiness timeout: {mode}")
    line = proc.stdout.readline().strip().split()
    if len(line) != 8 or line[0] != "READY":
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError(f"malformed helper readiness: {line!r}")
    snap = {
        "pid": int(line[1]),
        "ppid": int(line[2]),
        "fd_token": line[3],
        "device": int(line[4]),
        "inode": int(line[5]),
        "file_type": int(line[6]),
        "mode": int(line[7]),
    }
    try:
        process = scanner.process_identity(proc.pid)
    except BaseException:
        proc.kill()
        proc.wait(timeout=5)
        raise
    return {
        "mode": mode,
        "argv": proc.args,
        "cwd": cwd,
        "pid": proc.pid,
        "ppid": os.getpid(),
        "process_identity_before": process,
        "ready_identity": snap,
        "process": proc,
        "post_identity": None,
        "shutdown_returncode": None,
    }


def helper_post(helper: dict[str, Any]) -> dict[str, Any] | None:
    proc = helper["process"]
    assert proc.stdin is not None and proc.stdout is not None
    import select
    proc.stdin.write("POST\n")
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], 5.0)
    if not ready:
        return {"error": "post-scan identity timeout"}
    line = proc.stdout.readline().strip().split()
    if len(line) != 8 or line[0] != "POST":
        return {"error": f"malformed post-scan identity: {line!r}"}
    result = {
        "pid": int(line[1]),
        "ppid": int(line[2]),
        "fd_token": line[3],
        "device": int(line[4]),
        "inode": int(line[5]),
        "file_type": int(line[6]),
        "mode": int(line[7]),
    }
    helper["post_identity"] = result
    helper["process_identity_after"] = scanner.process_identity(proc.pid)
    return result


def stop_helper(helper: dict[str, Any]) -> dict[str, Any]:
    proc = helper["process"]
    assert proc.stdin is not None
    if proc.poll() is None:
        try:
            proc.stdin.write("EXIT\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    helper["shutdown_returncode"] = proc.returncode
    return {
        "pid": proc.pid,
        "returncode": proc.returncode,
        "alive_after_cleanup": proc.poll() is None,
    }


def row_identity_set(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    normalized = []
    for row in rows:
        normalized.append((
            row["pid"], row["fd_raw_base64"], row["type_raw_base64"], row["device"],
            row["inode"], row["name_canonical_base64"] or row["name_raw_base64"],
        ))
    return sorted(normalized)


def expected_row(pid: int, fd_token: str, kind: bytes, device: int, inode: int, path: Path) -> dict[str, Any]:
    canonical = os.path.realpath(os.fsencode(path), strict=True)
    return {
        "pid": pid,
        "fd_raw_base64": base64.b64encode(fd_token.encode()).decode("ascii"),
        "type_raw_base64": base64.b64encode(kind).decode("ascii"),
        "device": device,
        "inode": inode,
        "name_canonical_base64": base64.b64encode(canonical).decode("ascii"),
    }


def matching_expected(rows: list[dict[str, Any]], expected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for item in expected:
        matched = any(all(row.get(key) == value for key, value in item.items()) for row in rows)
        found.append({"expected": item, "matched": matched})
    return found


def snapshot_handles(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "checker_fds": {str(fd): fd_identity(fd) for fd in state.get("fds", [])},
        "helpers_ready": [h["ready_identity"] for h in state.get("helpers", [])],
        "target": path_snapshot(state["target"]),
    }


def close_fds(fds: list[int]) -> list[dict[str, Any]]:
    outcomes = []
    while fds:
        fd = fds.pop()
        try:
            os.close(fd)
            outcomes.append({"fd": fd, "closed": True})
        except OSError as exc:
            outcomes.append({"fd": fd, "closed": False, "error": f"{type(exc).__name__}: {exc}"})
    return outcomes


def run_pair(parent: Path, case_id: str, target: Path, mode: str,
             child: Path | None = None, malformed: bool = False,
             inaccessible: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {"target": target, "fds": [], "helpers": [], "expected": [], "setup": {}}
    root = parent / case_id
    if mode != "nonexistent" and mode != "invalid_file":
        root.mkdir(mode=0o700)
    if mode in {"checker_child", "foreign_child", "normal_j"}:
        child = root / "foreign-child.bin"
        child.write_bytes(b"R37 disposable lsof status fixture\n")
        os.chmod(child, 0o600)
    if mode == "invalid_file":
        root.mkdir(mode=0o700)
        target.write_bytes(b"not a directory; disposable target\n")
        os.chmod(target, 0o600)
    if mode == "nonexistent":
        if os.path.lexists(target):
            raise RuntimeError("nonexistent target unexpectedly exists")
    if inaccessible:
        os.chmod(root, 0)
        state["setup"]["requested_mode"] = 0
        state["setup"]["caller_uid"] = os.getuid()
        state["setup"]["caller_euid"] = os.geteuid()
        state["setup"]["access_read_execute"] = os.access(root, os.R_OK | os.X_OK, effective_ids=True)
        state["setup"]["exercised"] = os.geteuid() != 0 and not state["setup"]["access_read_execute"]
    try:
        if mode == "checker_root":
            fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            state["fds"].append(fd)
            ident = fd_identity(fd)
            state["expected"].append(expected_row(os.getpid(), str(fd), b"DIR", ident["device"], ident["inode"], root))
        elif mode == "checker_child":
            fd_root = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            fd_child = os.open(child, os.O_RDONLY)
            state["fds"].extend([fd_root, fd_child])
            root_id = fd_identity(fd_root)
            child_id = fd_identity(fd_child)
            state["expected"].extend([
                expected_row(os.getpid(), str(fd_root), b"DIR", root_id["device"], root_id["inode"], root),
                expected_row(os.getpid(), str(fd_child), b"REG", child_id["device"], child_id["inode"], child),
            ])
        elif mode in {"foreign_root", "foreign_child", "foreign_cwd"}:
            helper_mode = {"foreign_root": "root", "foreign_child": "child", "foreign_cwd": "cwd"}[mode]
            helper = spawn_helper(helper_mode, root, child)
            state["helpers"].append(helper)
            ident = helper["ready_identity"]
            holder_name = child if mode == "foreign_child" else root
            holder_type = b"REG" if mode == "foreign_child" else b"DIR"
            state["expected"].append(expected_row(ident["pid"], ident["fd_token"], holder_type, ident["device"], ident["inode"], holder_name))
        elif mode == "normal_j":
            root_helper = spawn_helper("root", root)
            child_helper = spawn_helper("child", root, child)
            state["helpers"].extend([root_helper, child_helper])
            for helper, holder_name, kind in ((root_helper, root, b"DIR"), (child_helper, child, b"REG")):
                ident = helper["ready_identity"]
                state["expected"].append(expected_row(ident["pid"], ident["fd_token"], kind, ident["device"], ident["inode"], holder_name))
        elif mode in {"empty", "nonexistent", "invalid_file", "inaccessible", "malformed"}:
            pass
        else:
            raise RuntimeError(f"unknown fixture mode: {mode}")

        before = snapshot_handles(state)
        if inaccessible and not state["setup"].get("exercised"):
            state["setup"]["status"] = "NOT_EXERCISED_CREDENTIALS_DID_NOT_PROVE_DENIAL"
        no_q = run_capture(case_id, "without_q", target, malformed=malformed)
        with_q = run_capture(case_id, "with_q", target, malformed=malformed)
        after_scan = snapshot_handles(state)
        helper_posts = [helper_post(helper) for helper in state["helpers"]]
        after_post = snapshot_handles(state)
        for idx, helper in enumerate(state["helpers"]):
            helper["post_identity_result"] = helper_posts[idx]
            helper["identity_stable"] = helper["post_identity_result"] == helper["ready_identity"]
        no_q["expected_holder_matches"] = matching_expected(no_q["parsed_rows"], state["expected"])
        with_q["expected_holder_matches"] = matching_expected(with_q["parsed_rows"], state["expected"])
        no_sig = row_identity_set(no_q["parsed_rows"])
        q_sig = row_identity_set(with_q["parsed_rows"])
        pair_result = {
            "same_expected_target": str(target) == no_q["argv"][-1] == with_q["argv"][-1],
            "only_q_argument_differs": (
                [arg for arg in no_q["argv"] if arg != "-Q"]
                == [arg for arg in with_q["argv"] if arg != "-Q"]
                and with_q["argv"].count("-Q") == 1
            ),
            "rows_equal": no_sig == q_sig,
            "without_q_row_count": len(no_q["parsed_rows"]),
            "with_q_row_count": len(with_q["parsed_rows"]),
            "without_q_expected_matches": no_q["expected_holder_matches"],
            "with_q_expected_matches": with_q["expected_holder_matches"],
            "fixture_identity_stable": before["target"].get("lstat_identity") == after_scan["target"].get("lstat_identity"),
            "checker_fd_identity_stable": before["checker_fds"] == after_scan["checker_fds"],
            "helper_identity_stable": all(h.get("identity_stable") is True for h in state["helpers"]),
            "helper_process_identities": [
                {key: value for key, value in helper.items() if key != "process"}
                for helper in state["helpers"]
            ],
        }
        cleanup_fds = close_fds(state["fds"])
        cleanup_helpers = [stop_helper(helper) for helper in state["helpers"]]
        if inaccessible:
            os.chmod(root, 0o700)
        return {
            "case_id": case_id,
            "fixture_mode": mode,
            "target": str(target),
            "expected_holders": state["expected"],
            "setup": state["setup"],
            "identity_before_pair": before,
            "identity_after_pair": after_scan,
            "identity_after_helper_check": after_post,
            "without_q": no_q,
            "with_q": with_q,
            "pair_comparison": pair_result,
            "cleanup": {"checker_fds": cleanup_fds, "helpers": cleanup_helpers},
        }
    except BaseException:
        close_fds(state["fds"])
        for helper in state["helpers"]:
            stop_helper(helper)
        if inaccessible and root.exists():
            os.chmod(root, 0o700)
        raise


def capture_local_artifacts() -> dict[str, Any]:
    records: dict[str, Any] = {"binary": {}, "commands": {}, "man_page": {}}
    binary = Path(LSOF)
    st = os.stat(binary)
    binary_bytes = binary.read_bytes()
    records["binary"] = {
        "path": LSOF,
        "device": st.st_dev,
        "inode": st.st_ino,
        "mode": stat.S_IMODE(st.st_mode),
        "size_bytes": st.st_size,
        "sha256": sha256(binary_bytes),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "uid": os.getuid(),
        "euid": os.geteuid(),
    }
    for name, argv in {
        "version": [LSOF, "-v"],
        "help_h": [LSOF, "-h"],
        "help_question": [LSOF, "-?"],
    }.items():
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=10,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        out = bytes(result.stdout)
        err = bytes(result.stderr)
        records["commands"][name] = {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": write_bytes(BUNDLE / f"local-{name}.stdout.bin", out),
            "stderr": write_bytes(BUNDLE / f"local-{name}.stderr.bin", err),
            "stdout_base64": base64.b64encode(out).decode("ascii"),
            "stderr_base64": base64.b64encode(err).decode("ascii"),
        }
    man = Path("/usr/share/man/man8/lsof.8")
    if man.is_file():
        raw = man.read_bytes()
        records["man_page"] = {
            "path": str(man),
            "sha256": sha256(raw),
            "size_bytes": len(raw),
            "sidecar": write_bytes(BUNDLE / "local-lsof.8", raw),
        }
    else:
        records["man_page"] = {"path": str(man), "available": False}
    return records


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# R6-A R37 paired disposable lsof `-Q` status experiment",
        "",
        f"Run ID: `{report['run_id']}`",
        f"Observation status: **{report['observation_status']}**",
        f"`-Q` capability from case J: **{report.get('q_option_acceptance', 'UNRESOLVED')}**",
        f"Reconciliation at experiment start: **{report['reconciliation_case_at_start']}**",
        "",
        "| Case | Without `-Q` rc / rows / parser | With `-Q` rc / rows / parser | Same rows | Stable fixture |",
        "|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        a, b = case.get("without_q", {}), case.get("with_q", {})
        lines.append(
            f"| {case['case_id']} | {a.get('returncode')} / {len(a.get('parsed_rows', []))} / {a.get('parser_valid')} | "
            f"{b.get('returncode')} / {len(b.get('parsed_rows', []))} / {b.get('parser_valid')} | "
            f"{case.get('pair_comparison', {}).get('rows_equal')} | "
            f"{case.get('pair_comparison', {}).get('fixture_identity_stable')} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        report.get("interpretation", "The paired matrix did not complete; see the JSON fatal_error field."),
        "",
        "Raw stdout/stderr bytes, local help/version/man captures, exact argv, parsed rows, framing results, helper identities, and cleanup records are preserved in the evidence directory named in the JSON report.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    if BUNDLE.exists() or JSON_PATH.exists() or MD_PATH.exists() or COMPLETE_PATH.exists():
        raise SystemExit("refusing to overwrite a prior R37 experiment artifact")
    BUNDLE.mkdir(mode=0o700)
    sidecars: list[dict[str, Any]] = []
    script_copy = BUNDLE / "experiment-source.py"
    script_copy.write_bytes(Path(__file__).read_bytes())
    sidecars.append(file_record(script_copy, "exact experiment source"))
    local = capture_local_artifacts()
    for group in ("commands",):
        for name, item in local[group].items():
            sidecars.extend([item["stdout"], item["stderr"]])
    if local["man_page"].get("available") is not False:
        sidecars.append(local["man_page"]["sidecar"])

    report: dict[str, Any] = {
        "schema": "r6a-r37-lsof-q-paired-status-v1",
        "run_id": RUN_ID,
        "reconciliation_case_at_start": "CASE Q5 — UNRESOLVED",
        "experiment_scope": "disposable temporary roots only; no R23 or production paths",
        "started_at_utc": utc_now(),
        "executable": local,
        "scanner_source": {
            "path": str(SCANNER_PATH.relative_to(HERE)),
            "sha256": sha256(SCANNER_PATH.read_bytes()),
        },
        "experiment_script": {
            "path": str(Path(__file__).relative_to(HERE)),
            "sha256": sha256(Path(__file__).read_bytes()),
        },
        "cases": [],
    }
    tmp_root_name = None
    try:
        with tempfile.TemporaryDirectory(prefix="r6a-r37-lsof-q-") as tmp:
            tmp_parent = Path(tmp)
            tmp_root_name = str(tmp_parent)
            report["temporary_parent_identity_before"] = object_id_from_stat(os.stat(tmp_parent))
            # The harness remains in the repository, outside the temporary target tree.
            plan = [
                ("A_root_pin_only", "checker_root", False, False),
                ("B_root_pin_plus_child_fd", "checker_child", False, False),
                ("C_foreign_root_fd", "foreign_root", False, False),
                ("D_foreign_child_fd", "foreign_child", False, False),
                ("E_foreign_cwd", "foreign_cwd", False, False),
                ("F_no_holders", "empty", False, False),
                ("G_nonexistent_root", "nonexistent", False, False),
                ("H1_inaccessible_target", "inaccessible", False, True),
                ("H2_invalid_target_type", "invalid_file", False, False),
                ("I_malformed_option", "malformed", True, False),
                ("J_normal_parser_valid_scan", "normal_j", False, False),
            ]
            for case_id, mode, malformed, inaccessible in plan:
                if mode == "nonexistent":
                    target = tmp_parent / "G_nonexistent_root" / "missing-root"
                elif mode == "invalid_file":
                    target = tmp_parent / "H2_invalid_target_type" / "not-a-directory"
                else:
                    target = tmp_parent / case_id
                result = run_pair(tmp_parent, case_id, target, mode, malformed=malformed, inaccessible=inaccessible)
                report["cases"].append(result)
            report["temporary_root_removed_after_context"] = False
            report["temporary_parent_identity_after_pairs"] = object_id_from_stat(os.stat(tmp_parent))
        report["temporary_root_removed_after_context"] = not Path(tmp_root_name).exists()
    except BaseException as exc:
        report["fatal_error"] = f"{type(exc).__name__}: {exc}"
        report["observation_status"] = "INCOMPLETE_MATRIX_EXECUTION"
        report["temporary_root_removed_after_context"] = not Path(tmp_root_name).exists() if tmp_root_name else None
        report["ended_at_utc"] = utc_now()
        known_paths = {item.get("path") for item in sidecars}
        for path in sorted(BUNDLE.iterdir()):
            if path.is_file() and str(path.relative_to(HERE)) not in known_paths:
                sidecars.append(file_record(path, f"captured sidecar {path.name}"))
        report["sidecars"] = sidecars
        JSON_PATH.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
        MD_PATH.write_text(markdown(report) + "\n")
        COMPLETE_PATH.write_text(json.dumps({
            "schema": "r6a-r37-lsof-q-paired-status-completion-v1",
            "run_id": RUN_ID,
            "observation_status": report["observation_status"],
            "fatal_error": report["fatal_error"],
            "json": file_record(JSON_PATH, "incomplete JSON report"),
            "markdown": file_record(MD_PATH, "incomplete Markdown report"),
            "evidence_sidecars": sidecars,
            "all_sidecars_verified": all(
                sha256((HERE / item["path"]).read_bytes()) == item["sha256"]
                and (HERE / item["path"]).stat().st_size == item["size_bytes"]
                for item in sidecars
            ),
        }, indent=2, sort_keys=True) + "\n")
        return 1

    for case in report["cases"]:
        for variant in ("without_q", "with_q"):
            item = case[variant]
            sidecars.extend([item["stdout_sidecar"], item["stderr_sidecar"]])

    j_q = next(case for case in report["cases"] if case["case_id"] == "J_normal_parser_valid_scan")["with_q"]
    j_expected = j_q.get("expected_holder_matches", [])
    j_positive_complete = (
        j_q.get("capture_complete") is True
        and j_q.get("stream_complete") is True
        and j_q.get("parser_valid") is True
        and len(j_expected) == 2
        and all(item.get("matched") is True for item in j_expected)
    )
    q_diagnostic = (j_q.get("stdout_repr", "") + " " + j_q.get("stderr_repr", "")).lower()
    q_rejected_hint = any(term in q_diagnostic for term in ("illegal option", "unknown option", "unrecognized option", "usage:"))
    if j_positive_complete and not q_rejected_hint:
        q_acceptance = "ACCEPTED_ON_CASE_J_POSITIVE_STREAM"
    elif q_rejected_hint:
        q_acceptance = "REJECTED_OR_OPTION_PARSE_FAILED_ON_CASE_J"
    else:
        q_acceptance = "AMBIGUOUS_CASE_J_DID_NOT_PROVE_OPTION_ACCEPTANCE"

    case_by_id = {case["case_id"]: case for case in report["cases"]}
    ordinary_miss_ids = ("F_no_holders", "G_nonexistent_root")
    ordinary_misses_q0 = all(case_by_id[cid]["with_q"].get("returncode") == 0 for cid in ordinary_miss_ids)
    malformed_nonzero = case_by_id["I_malformed_option"]["with_q"].get("returncode") not in (None, 0)
    missing_root_detectable = (
        case_by_id["G_nonexistent_root"]["with_q"].get("returncode") not in (None, 0)
        or not case_by_id["G_nonexistent_root"]["with_q"].get("stderr_empty", False)
    )
    permission_detectable = (
        case_by_id["H1_inaccessible_target"]["setup"].get("exercised") is not True
        or case_by_id["H1_inaccessible_target"]["with_q"].get("returncode") not in (None, 0)
        or not case_by_id["H1_inaccessible_target"]["with_q"].get("stderr_empty", False)
    )
    all_captures_complete = all(
        case[variant].get("capture_complete") is True
        for case in report["cases"] for variant in ("without_q", "with_q")
    )
    q1_supported = (
        q_acceptance == "ACCEPTED_ON_CASE_J_POSITIVE_STREAM"
        and ordinary_misses_q0
        and malformed_nonzero
        and missing_root_detectable
        and permission_detectable
        and all_captures_complete
    )
    report["q_option_acceptance"] = q_acceptance
    report["contract_checks"] = {
        "case_j_positive_parser_valid_stream": j_positive_complete,
        "ordinary_empty_or_missing_cases_with_q_return_zero": ordinary_misses_q0,
        "malformed_option_with_q_remains_nonzero": malformed_nonzero,
        "nonexistent_root_remains_mechanically_detectable": missing_root_detectable,
        "inaccessible_target_remains_mechanically_detectable_or_not_exercised": permission_detectable,
        "all_captures_complete": all_captures_complete,
        "Q1_contract_supported_by_this_matrix": q1_supported,
    }
    report["observation_status"] = "COMPLETE_MATRIX_Q1_SUPPORTED" if q1_supported else "COMPLETE_MATRIX_Q1_NOT_PROVEN"
    report["interpretation"] = (
        "The complete paired matrix supports the local -Q contract checks; an unchanged status-0 gate and all independent capture, parser, stderr, identity, and holder-classification gates remain mandatory."
        if q1_supported else
        "The complete paired matrix did not prove the required local -Q fail-closed contract. Do not change the scanner invocation, accept raw status 1, or proceed to the 16-case suite, R23, production, Gate/admission, or R6-A execution."
    )
    report["ended_at_utc"] = utc_now()
    report["temporary_root"] = tmp_root_name
    report["temporary_root_removed_after_context"] = not Path(tmp_root_name).exists()
    report["sidecars"] = sidecars

    json_bytes = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    md_bytes = (markdown(report) + "\n").encode("utf-8")
    JSON_PATH.write_bytes(json_bytes)
    MD_PATH.write_bytes(md_bytes)
    complete = {
        "schema": "r6a-r37-lsof-q-paired-status-completion-v1",
        "run_id": RUN_ID,
        "observation_status": report["observation_status"],
        "json": file_record(JSON_PATH, "complete JSON report"),
        "markdown": file_record(MD_PATH, "complete Markdown report"),
        "evidence_sidecars": sidecars,
        "sidecar_count": len(sidecars),
        "all_sidecars_verified": all(
            sha256((HERE / item["path"]).read_bytes()) == item["sha256"]
            and (HERE / item["path"]).stat().st_size == item["size_bytes"]
            for item in sidecars
        ),
    }
    COMPLETE_PATH.write_text(json.dumps(complete, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "run_id": RUN_ID,
        "observation_status": report["observation_status"],
        "q_option_acceptance": q_acceptance,
        "cases": len(report["cases"]),
        "capture_pairs": sum(1 for case in report["cases"] if "without_q" in case and "with_q" in case),
        "sidecars": len(sidecars),
        "manifest": str(COMPLETE_PATH.relative_to(HERE)),
        "json": str(JSON_PATH.relative_to(HERE)),
        "markdown": str(MD_PATH.relative_to(HERE)),
        "temporary_root_removed": report["temporary_root_removed_after_context"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
