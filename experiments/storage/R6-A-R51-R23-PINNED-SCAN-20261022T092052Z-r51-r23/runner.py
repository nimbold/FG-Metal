#!/usr/bin/env python3
"""One R51 R23 pinned scan using the accepted R50 parser and status policy."""
from __future__ import annotations
import base64, fcntl, hashlib, json, os, stat, sys, time
from datetime import datetime, timezone
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent
REPO = RUN_DIR.parents[2]
STORAGE = REPO / "experiments/storage"
sys.path.insert(0, str(STORAGE))
import r6a_r33_live_open_capture as scanner  # noqa: E402

ROOT_PATH = scanner.EXPECTED_ROOT
PARENT_PATH = scanner.EXPECTED_PARENT
ROOT_NAME = os.path.basename(ROOT_PATH)
EXPECTED_BINARY_SHA256 = "deff85c7b4750a948a69fcd48eae3b394a37575e5048e5eb4a35b7a0482c3dc8"
EXPECTED_SCANNER_SHA256 = "27b9c03079db40d218831b576de0201927d4b704ff4d787673f7f27f76ae7c95"
EXPECTED_PRECHECK_SHA256 = "2a648916c9042309b7feb10714e545eb6cefea10a01345917031de9e56dddeed"
EXPECTED_MANIFEST_SHA256 = "681d0a379bcb2dde2d40802f9a5d079cf72a71b2774ad2801eff2dbae2b83673"
EXPECTED_STATUS_CONTRACT_SHA256 = "0eca8d93b699daa9947c8a67a809aed6e34e4f590c57fbbfbb16ce25047b362a"
EXPECTED_STATUS_REVIEW_SHA256 = "145b4920aac84425fc0362b734aa9fdca4ad40422181dc2b4a005ad6e0a2cb1b"
EXPECTED_PROBE_SHA256 = "279896760fcc15c20fa677f122724c2ed802cff9f71f6b922121e6942aa80bfc"
EXPECTED_PRIOR_INVENTORY = {
    "": (16777229, 102865819, stat.S_IFDIR, 0o700, 501, 20),
    "2e66676d6574616c2d6275696c642d6c656173652e6a736f6e": (16777229, 102865821, stat.S_IFREG, 0o600, 501, 20),
}
EXPECTED_ROOT_DEVICE = 16777229
EXPECTED_ROOT_INODE = 102865819
EXPECTED_ROOT_TYPE = stat.S_IFDIR
EXPECTED_POLICY_REVIEW = "PASS — INSTALLED LSOF +D SEARCH-STATUS CONTRACT ACCEPTED"
LSOF_TYPES = {
    "DIR": stat.S_IFDIR, "REG": stat.S_IFREG, "CHR": stat.S_IFCHR,
    "BLK": stat.S_IFBLK, "FIFO": stat.S_IFIFO, "SOCK": stat.S_IFSOCK,
}
OPENED: dict[str, int] = {}
REPORT: dict = {
    "schema": "r6a-r51-r23-pinned-scan-v1",
    "run_id": RUN_DIR.name.removeprefix("R6-A-R51-R23-PINNED-SCAN-"),
    "scanner_path": str(Path(scanner.__file__).resolve()),
    "scanner_sha256": None,
    "runner_path": str(Path(__file__).resolve()),
    "runner_sha256": None,
    "canonical_root": ROOT_PATH,
    "lsof_scan_invocation_count": 0,
    "closeout_invoked": False,
    "mutations": [],
    "status_classification": None,
    "accepted": False,
    "blockers": [],
}


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write_exclusive(path: Path, payload: bytes) -> dict:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short evidence write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return {"path": str(path), "bytes": len(payload), "sha256": sha256(payload)}


def identity(st: os.stat_result) -> dict:
    return {
        "device": st.st_dev, "inode": st.st_ino, "type": stat.S_IFMT(st.st_mode),
        "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
        "nlink": st.st_nlink, "size": st.st_size,
    }


def namespace_inventory(root_fd: int) -> dict:
    root_st = os.fstat(root_fd)
    root_dev = root_st.st_dev
    entries: list[dict] = []
    errors: list[dict] = []

    def visit(fd: int, relative: bytes) -> None:
        try:
            names = os.listdir(fd)
        except OSError as e:
            errors.append({"operation": "listdir", "relative_path_hex": relative.hex(), "error": f"{type(e).__name__}: {e}"})
            return
        names_b = sorted(os.fsencode(name) for name in names)
        for name_b in names_b:
            name = os.fsdecode(name_b)
            rel = name_b if not relative else relative + b"/" + name_b
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError as e:
                errors.append({"operation": "lstat", "relative_path_hex": rel.hex(), "error": f"{type(e).__name__}: {e}"})
                continue
            is_link = stat.S_ISLNK(st.st_mode)
            rec = {
                "relative_path_hex": rel.hex(), "device": st.st_dev, "inode": st.st_ino,
                "type": stat.S_IFMT(st.st_mode), "mode": stat.S_IMODE(st.st_mode),
                "uid": st.st_uid, "gid": st.st_gid, "nlink": st.st_nlink,
                "size": st.st_size, "symlink": is_link,
                "device_transition": st.st_dev != root_dev,
            }
            entries.append(rec)
            if stat.S_ISDIR(st.st_mode) and not is_link:
                child_fd = None
                try:
                    child_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)):
                        errors.append({"operation": "directory_identity", "relative_path_hex": rel.hex(), "error": "lstat/open identity mismatch"})
                    else:
                        visit(child_fd, rel)
                except OSError as e:
                    errors.append({"operation": "open_directory", "relative_path_hex": rel.hex(), "error": f"{type(e).__name__}: {e}"})
                finally:
                    if child_fd is not None:
                        os.close(child_fd)

    entries.append({
        "relative_path_hex": "", "device": root_st.st_dev, "inode": root_st.st_ino,
        "type": stat.S_IFMT(root_st.st_mode), "mode": stat.S_IMODE(root_st.st_mode),
        "uid": root_st.st_uid, "gid": root_st.st_gid, "nlink": root_st.st_nlink,
        "size": root_st.st_size, "symlink": False, "device_transition": False,
    })
    visit(root_fd, b"")
    entries.sort(key=lambda x: bytes.fromhex(x["relative_path_hex"]))
    errors.sort(key=lambda x: (x["relative_path_hex"], x["operation"], x["error"]))
    canonical = json.dumps({"entries": entries, "errors": errors}, sort_keys=True, separators=(",", ":")).encode()
    counts = {
        "search_item_count": len(entries),
        "root_count": sum(not e["relative_path_hex"] for e in entries),
        "regular_file_count": sum(e["type"] == stat.S_IFREG for e in entries),
        "directory_count_excluding_root": sum(e["type"] == stat.S_IFDIR for e in entries if e["relative_path_hex"]),
        "symlink_count": sum(e["symlink"] for e in entries),
        "other_count": sum(e["type"] not in {stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK} for e in entries),
        "error_count": len(errors),
        "device_transition_count": sum(e["device_transition"] for e in entries),
    }
    return {"entries": entries, "errors": errors, "counts": counts, "digest": sha256(canonical)}


def inventory_key(path_bytes: bytes, device: int, inode: int, type_mode: int) -> tuple[bytes, int, int, int]:
    return (path_bytes, device, inode, type_mode)


def row_key(row: dict) -> tuple[bytes, int, int, int] | None:
    type_name = row["fd_type_raw"].decode("ascii", "strict")
    mode_type = LSOF_TYPES.get(type_name)
    if mode_type is None:
        return None
    return inventory_key(row["name_decoded"], row["device"], row["inode"], mode_type)


def summarize_row(row: dict, root_fd: int, checker_pid: int, pin: dict) -> dict:
    return {
        "pid": row["pid"], "ppid": row["ppid"],
        "fd_raw_b64": base64.b64encode(row["fd_raw"]).decode("ascii"),
        "fd_number": row["fd_number"], "fd_suffix": row["fd_suffix"],
        "type": row["fd_type_raw"].decode("ascii", "backslashreplace"),
        "device": row["device"], "inode": row["inode"],
        "name_raw_b64": base64.b64encode(row["name_raw"]).decode("ascii"),
        "name_decoded_b64": base64.b64encode(row["name_decoded"]).decode("ascii"),
        "name_decoded_display": row["name_decoded"].decode("utf-8", "backslashreplace"),
        "parser_status": row["parser_status"],
        "identity_key_bound": row_key(row) is not None,
        "exact_root_pin": (
            row["pid"] == checker_pid and row["fd_number"] == root_fd
            and row["fd_raw"] == str(root_fd).encode("ascii")
            and row["fd_type_raw"] == b"DIR"
            and row["device"] == pin["device"] and row["inode"] == pin["inode"]
            and row["name_decoded"] == os.fsencode(ROOT_PATH)
        ),
    }


def jsonable(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"), "bytes": len(value), "sha256": sha256(value)}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


def emit() -> None:
    raw = json.dumps(jsonable(REPORT), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    write_exclusive(RUN_DIR / "scan.json", raw)
    lines = [
        "# R6-A R51 Fresh R23 Pinned Scan", "",
        f"- Run ID: `{REPORT['run_id']}`",
        f"- Status class: `{REPORT.get('status_classification')}`",
        f"- Result: **{'PASS' if REPORT.get('accepted') else 'FAIL'}**",
        f"- lsof +D invocation count: `{REPORT.get('lsof_scan_invocation_count')}`",
        f"- lsof return code: `{REPORT.get('lsof', {}).get('returncode')}`",
        f"- Normal stderr bytes: `{REPORT.get('lsof', {}).get('stderr_total_bytes')}`",
        f"- Structured stream complete: `{REPORT.get('stream', {}).get('complete')}`",
        f"- Exact self-pin row count: `{REPORT.get('exact_pin_match_count')}`; total rows: `{len(REPORT.get('rows', []))}`",
        f"- Pre/post namespace digest equal: `{REPORT.get('namespace', {}).get('stable')}`",
        f"- Closeout invoked: `{REPORT.get('closeout_invoked')}`",
        f"- Mutations: `{REPORT.get('mutations')}`", "",
        "## Parsed rows", "",
        "| PID | FD | Type | Device | Inode | Path | Classification |",
        "|---:|---:|---|---:|---:|---|---|",
    ]
    for row in REPORT.get("rows", []):
        lines.append("| " + " | ".join(str(row.get(k, "")).replace("|", "\\|") for k in ("pid", "fd_number", "type", "device", "inode", "name_decoded_display", "classification")) + " |")
    lines += ["", "## Acceptance predicates", "", "```json", json.dumps(REPORT.get("acceptance", {}), indent=2, sort_keys=True), "```", "", "## Blockers", ""]
    lines += [f"- {x}" for x in REPORT.get("blockers", [])] or ["- None"]
    lines += ["", "The operation performed no R23 closeout or namespace mutation.", ""]
    write_exclusive(RUN_DIR / "scan.md", "\n".join(lines).encode("utf-8"))
    for name, data in REPORT.get("raw_sidecars", {}).items():
        write_exclusive(RUN_DIR / name, data)


def main() -> int:
    result = 1
    stdout_raw = b""
    stderr_raw = b""
    try:
        REPORT["scanner_sha256"] = sha256(Path(scanner.__file__).read_bytes())
        REPORT["runner_sha256"] = sha256(Path(__file__).read_bytes())
        if REPORT["scanner_sha256"] != EXPECTED_SCANNER_SHA256:
            raise RuntimeError("accepted scanner source hash changed")
        REPORT["checker_before"] = scanner.process_snapshot()
        checker_pid = REPORT["checker_before"]["pid"]

        precheck_path = STORAGE / "R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021.json"
        precheck_raw = scanner.read_nofollow(str(precheck_path))
        if sha256(precheck_raw) != EXPECTED_PRECHECK_SHA256:
            raise RuntimeError("R23 parent/root precheck hash changed")
        precheck = json.loads(precheck_raw)
        if precheck.get("classification") != "PARENT_ROOT_IDENTITY_PASS":
            raise RuntimeError("R23 parent/root precheck is not accepted")
        check_rows = [x for x in precheck.get("checks", []) if x.get("phase") == "parent_root_identity"]
        if len(check_rows) != 40 or any(x.get("result") != "PASS" for x in check_rows):
            raise RuntimeError("R23 parent/root precheck does not contain all 40 passing rows")
        REPORT["precheck"] = {"path": str(precheck_path), "sha256": sha256(precheck_raw), "rows": len(check_rows), "all_pass": True}

        manifest_path = STORAGE / "manifests/R6-A-R23-DUAL-LINEAGE-20261020T214331Z.json"
        manifest_raw = scanner.read_nofollow(str(manifest_path))
        if sha256(manifest_raw) != EXPECTED_MANIFEST_SHA256:
            raise RuntimeError("R23 admission manifest hash changed")
        REPORT["admission_manifest"] = {"path": str(manifest_path), "sha256": sha256(manifest_raw)}

        prior_scan_path = STORAGE / "R6-A-R50-R23-PINNED-SCAN-20261022T085801Z-r50-r23.json"
        prior_scan_raw = scanner.read_nofollow(str(prior_scan_path))
        if sha256(prior_scan_raw) != "3d8909f6150be0d4d523a24731df760f31000d69095403cc985839c783cee8dc":
            raise RuntimeError("preserved R50 R23 scan evidence changed")
        prior_scan = json.loads(prior_scan_raw)
        if prior_scan.get("lsof_scan_invocation_count") != 1 or prior_scan.get("mutations") != [] or prior_scan.get("closeout_invoked") is not False:
            raise RuntimeError("preserved R50 R23 scan does not prove one read-only scan")
        REPORT["prior_R50_scan_sha256"] = sha256(prior_scan_raw)

        review_path = STORAGE / "R6-A-R51-STATUS-POLICY-INDEPENDENT-REVIEW.md"
        review_raw = scanner.read_nofollow(str(review_path))
        if sha256(review_raw) != EXPECTED_STATUS_REVIEW_SHA256 or EXPECTED_POLICY_REVIEW.encode("utf-8") not in review_raw:
            raise RuntimeError("fresh independent status-policy review is missing or not PASS")
        regressions_path = STORAGE / "R6-A-R51-STATUS-POLICY-REGRESSIONS-20261022T091851Z-r51-status-policy.json"
        regressions_raw = scanner.read_nofollow(str(regressions_path))
        regressions = json.loads(regressions_raw)
        if regressions.get("all_pass") is not True or regressions.get("passed") != 10 or regressions.get("total") != 10:
            raise RuntimeError("offline status-policy regression matrix is not 10/10 PASS")
        contract_path = STORAGE / "R6-A-R51-LSOF-SEARCH-STATUS-CONTRACT.md"
        contract_raw = scanner.read_nofollow(str(contract_path))
        if sha256(contract_raw) != EXPECTED_STATUS_CONTRACT_SHA256:
            raise RuntimeError("accepted R51 status contract hash changed")
        probe_path = STORAGE / "R6-A-R51-CLOSED-CHILD-PROBE-20261022T091606Z-r51-closed-child/probe.json"
        probe_raw = scanner.read_nofollow(str(probe_path))
        probe = json.loads(probe_raw)
        probe_acceptance = probe.get("acceptance", {})
        if (sha256(probe_raw) != EXPECTED_PROBE_SHA256
                or probe.get("lsof_sha256") != EXPECTED_BINARY_SHA256
                or probe.get("probe_count") != 1
                or not all(probe_acceptance.get(k) is True for k in (
                    "child_closed_by_fixture_design", "root_pin_valid", "root_and_child_identity_stable",
                    "normal_returncode_one", "normal_stderr_empty", "normal_stdout_stream_complete",
                    "normal_rows_parser_valid", "normal_exactly_one_root_pin_row", "normal_no_child_row",
                    "normal_no_timeout", "diagnostic_no_timeout"))
                or b"no file use located" not in base64.b64decode(probe["diagnostic_scan"]["stdout_b64"])):
            raise RuntimeError("authorized closed-child status probe is incomplete or contradictory")
        REPORT["status_policy_acceptance"] = {
            "review": {"path": str(review_path), "sha256": sha256(review_raw), "disposition": EXPECTED_POLICY_REVIEW},
            "contract": {"path": str(contract_path), "sha256": sha256(contract_raw)},
            "regressions": {"path": str(regressions_path), "sha256": sha256(regressions_raw), "passed": 10, "total": 10},
            "closed_child_probe": {"path": str(probe_path), "sha256": sha256(probe_raw), "run_id": probe.get("run_id")},
        }

        expected_parent = precheck["parent_capture"]["parent_fd_identity"]
        parent_fd = scanner.open_dir_nofollow(PARENT_PATH)
        OPENED["parent_fd"] = parent_fd
        parent_before = scanner.parent_identity(parent_fd)
        if parent_before != expected_parent:
            raise RuntimeError(f"approved R23 parent identity differs: {parent_before!r}")
        REPORT["parent_before"] = parent_before

        cache_fd = scanner.open_dir_nofollow("/Users/nima/Library/Caches/FGMetal")
        OPENED["cache_fd"] = cache_fd
        allocation_prior = {
            "allocation_device": precheck["checks"]["ALLOC_LOCK_NAMED_FD_DEVICE"]["observed"],
            "allocation_inode": precheck["checks"]["ALLOC_LOCK_NAMED_FD_INODE"]["observed"],
        }
        allocation_fd, allocation = scanner.open_lock(cache_fd, ".storage-allocation.lock", allocation_prior, "allocation")
        OPENED["allocation_fd"] = allocation_fd
        lease_dir = STORAGE / "build-leases"
        lease_fd = scanner.open_dir_nofollow(str(lease_dir))
        OPENED["lease_fd"] = lease_fd
        retention_prior = {
            "retention_device": precheck["checks"]["RETENTION_LOCK_NAMED_FD_DEVICE"]["observed"],
            "retention_inode": precheck["checks"]["RETENTION_LOCK_NAMED_FD_INODE"]["observed"],
        }
        retention_fd, retention = scanner.open_lock(lease_fd, ".retention.lock", retention_prior, "retention")
        OPENED["retention_fd"] = retention_fd
        if not all(v is True for lock in (allocation, retention) for v in lock["checks"].values() if isinstance(v, bool)):
            raise RuntimeError("R23 coordinator lock identity or exclusive lock check failed")
        REPORT["coordinator_locks"] = {"allocation": allocation, "retention": retention}

        named_before = scanner.stat_at(parent_fd, ROOT_NAME)
        root_fd = os.open(ROOT_NAME, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        OPENED["root_pin_fd"] = root_fd
        pin_before = scanner.fstat_identity(root_fd, with_fsid=True)
        root_identity_valid = (
            named_before["device"] == EXPECTED_ROOT_DEVICE and named_before["inode"] == EXPECTED_ROOT_INODE
            and named_before["type"] == EXPECTED_ROOT_TYPE
            and pin_before["device"] == EXPECTED_ROOT_DEVICE and pin_before["inode"] == EXPECTED_ROOT_INODE
            and pin_before["type"] == EXPECTED_ROOT_TYPE
            and (named_before["device"], named_before["inode"], named_before["type"])
                == (pin_before["device"], pin_before["inode"], pin_before["type"])
        )
        if not root_identity_valid:
            raise RuntimeError("fresh R23 named root does not match the exact expected root pin")
        REPORT["root_pin"] = {"fd": root_fd, "canonical_root": ROOT_PATH, "named_before": named_before, "pinned_before": pin_before}

        pre_inventory = namespace_inventory(root_fd)
        REPORT["namespace_pre"] = pre_inventory
        if (pre_inventory["counts"]["error_count"] or pre_inventory["counts"]["symlink_count"]
                or pre_inventory["counts"]["device_transition_count"] or pre_inventory["counts"]["root_count"] != 1):
            raise RuntimeError("pre-scan R23 namespace inventory is incomplete or has an unmodeled boundary")
        if not any(e["relative_path_hex"] == "2e66676d6574616c2d6275696c642d6c656173652e6a736f6e" and e["type"] == stat.S_IFREG for e in pre_inventory["entries"]):
            raise RuntimeError("pre-scan R23 namespace lacks the expected regular marker item")
        observed_inventory = {
            e["relative_path_hex"]: (e["device"], e["inode"], e["type"], e["mode"], e["uid"], e["gid"])
            for e in pre_inventory["entries"]
        }
        if observed_inventory != EXPECTED_PRIOR_INVENTORY:
            raise RuntimeError("fresh R23 namespace identity set differs from the independently audited inventory")

        executable_before = scanner.executable_identity(scanner.LSOF)
        version = scanner.lsof_version()
        if (not executable_before["stable"] or executable_before["sha256"] != EXPECTED_BINARY_SHA256
                or executable_before["owner_uid"] != 0 or executable_before["mode"] & 0o022
                or not version["version_match"] or version["stderr_base64"] != ""):
            raise RuntimeError("installed lsof no longer matches reviewed 4.91/libproc binary")
        REPORT["lsof_executable_before"] = executable_before
        REPORT["lsof_version"] = version

        REPORT["lsof_scan_invocation_count"] = 1
        capture = scanner.scan_lsof_path(ROOT_PATH)
        stdout_raw = capture.pop("stdout_raw")
        stderr_raw = capture.pop("stderr_raw")
        rows = capture.pop("rows")
        stream = capture.pop("stream")
        REPORT["lsof"] = capture
        REPORT["stream"] = stream
        REPORT["raw_sidecars"] = {"lsof.stdout.bin": stdout_raw, "lsof.stderr.bin": stderr_raw}

        post_inventory = namespace_inventory(root_fd)
        REPORT["namespace_post"] = post_inventory
        namespace_stable = (
            pre_inventory["digest"] == post_inventory["digest"]
            and pre_inventory["entries"] == post_inventory["entries"]
            and pre_inventory["errors"] == post_inventory["errors"]
            and post_inventory["counts"]["error_count"] == 0
        )
        REPORT["namespace"] = {"stable": namespace_stable, "pre_digest": pre_inventory["digest"], "post_digest": post_inventory["digest"]}

        named_after = scanner.stat_at(parent_fd, ROOT_NAME)
        pin_after = scanner.fstat_identity(root_fd, with_fsid=True)
        parent_after_fd = scanner.parent_identity(parent_fd)
        parent_path_fd = scanner.open_dir_nofollow(PARENT_PATH)
        try:
            parent_after_path = scanner.parent_identity(parent_path_fd)
        finally:
            os.close(parent_path_fd)
        checker_after = scanner.process_identity(checker_pid)
        executable_after = scanner.executable_identity(scanner.LSOF)
        REPORT["root_pin"]["named_after"] = named_after
        REPORT["root_pin"]["pinned_after"] = pin_after
        REPORT["parent_after_fd"] = parent_after_fd
        REPORT["parent_after_path"] = parent_after_path
        REPORT["checker_after"] = checker_after
        REPORT["lsof_executable_after"] = executable_after
        identities_stable = (
            (named_before["device"], named_before["inode"], named_before["type"])
                == (named_after["device"], named_after["inode"], named_after["type"])
            == (pin_before["device"], pin_before["inode"], pin_before["type"])
            == (pin_after["device"], pin_after["inode"], pin_after["type"])
            and pin_before["filesystem_id"] == pin_after["filesystem_id"]
            and parent_before == parent_after_fd == parent_after_path
            and checker_after["pid"] == checker_pid
            and checker_after["ppid"] == REPORT["checker_before"]["ppid"]
            and checker_after["start_identity"] == REPORT["checker_before"]["start_identity"]
            and executable_before == executable_after
        )
        REPORT["identities_stable"] = identities_stable

        inventory_objects = set()
        inventory_by_key = {}
        for item in pre_inventory["entries"]:
            rel = bytes.fromhex(item["relative_path_hex"])
            absolute = os.fsencode(ROOT_PATH) if not rel else os.fsencode(ROOT_PATH.rstrip("/")) + b"/" + rel
            key = inventory_key(absolute, item["device"], item["inode"], item["type"])
            inventory_objects.add(key)
            inventory_by_key[key] = item
        rows_out = []
        represented = set()
        all_rows_bound = True
        for row in rows:
            key = row_key(row)
            bound = key is not None and key in inventory_objects
            all_rows_bound = all_rows_bound and bound and row["parser_status"] == "PASS"
            if bound:
                represented.add(key)
            rec = summarize_row(row, root_fd, checker_pid, pin_before)
            rec["inventory_object_binding"] = bool(bound)
            rec["classification"] = "EXPECTED_IDENTITY_PIN" if rec["exact_root_pin"] else "ADDITIONAL_OR_FOREIGN_HOLDER"
            rows_out.append(rec)
        missing = sorted(inventory_objects - represented, key=lambda x: x[0])
        exact_pins = sum(bool(r["exact_root_pin"]) for r in rows_out)
        REPORT["rows"] = rows_out
        REPORT["exact_pin_match_count"] = exact_pins
        REPORT["inventory_binding"] = {"all_returned_rows_bound": all_rows_bound, "inventory_object_count": len(inventory_objects), "represented_object_count": len(represented), "unrepresented_search_items": [{"path_b64": base64.b64encode(k[0]).decode(), "path_display": k[0].decode("utf-8", "backslashreplace"), "device": k[1], "inode": k[2], "type": k[3]} for k in missing]}

        cap_ok = (
            capture["stdout_bytes_captured"] == capture["stdout_total_bytes"]
            and capture["stderr_bytes_captured"] == capture["stderr_total_bytes"]
            and not capture["stdout_capped"] and not capture["stderr_capped"]
        )
        base_gates = {
            "installed_binary_proven": executable_before["sha256"] == EXPECTED_BINARY_SHA256 and version["version_match"] and executable_before == executable_after,
            "root_pin_valid": root_identity_valid and named_after["device"] == pin_after["device"] and named_after["inode"] == pin_after["inode"] and named_after["type"] == pin_after["type"],
            "namespace_inventory_complete": pre_inventory["counts"]["error_count"] == 0 and post_inventory["counts"]["error_count"] == 0 and pre_inventory["counts"]["symlink_count"] == 0 and pre_inventory["counts"]["device_transition_count"] == 0,
            "namespace_stable": namespace_stable,
            "stdout_stderr_fully_captured": cap_ok,
            "stream_complete": stream.get("complete") is True and stream.get("bytes_consumed") == capture["stdout_total_bytes"],
            "parser_rows_valid": all(r["parser_status"] == "PASS" for r in rows),
            "lsof_child_identity_valid": (
                "child_identity_error" not in capture
                and capture.get("child_identity", {}).get("pid") == capture.get("child_pid")
                and capture.get("child_identity", {}).get("ppid") == checker_pid
            ),
            "normal_stderr_empty": capture["stderr_total_bytes"] == 0 and stderr_raw == b"",
            "no_timeout": not capture["timed_out"],
            "no_cap": not capture["stdout_capped"] and not capture["stderr_capped"],
            "all_rows_bind_to_inventory_identity": all_rows_bound,
            "named_pinned_parent_checker_identities_stable": identities_stable,
            "independent_closed_child_probe_signature_proven": True,
        }
        REPORT["status_predicates_A_M"] = base_gates
        policy_gates = all(base_gates.values())
        if not policy_gates:
            status_class = "STATUS_ONE_UNEXPLAINED_ERROR" if capture.get("returncode") == 1 else "OTHER_NONZERO_ERROR"
            REPORT["blockers"].append("one or more expected-search-miss predicates A-M failed")
        elif capture.get("returncode") == 0 and not missing:
            status_class = "STATUS_ZERO_ALL_SEARCH_ITEMS_MATCHED"
        elif capture.get("returncode") == 1 and missing:
            status_class = "STATUS_ONE_EXPECTED_SEARCH_MISS"
        elif capture.get("returncode") == 1:
            status_class = "STATUS_ONE_UNEXPLAINED_ERROR"
            REPORT["blockers"].append("status 1 has no predicted unrepresented search item")
        elif capture.get("returncode") == 0:
            status_class = "STATUS_ONE_UNEXPLAINED_ERROR"
            REPORT["blockers"].append("status zero contradicts one or more unrepresented search items")
        else:
            status_class = "OTHER_NONZERO_ERROR"
            REPORT["blockers"].append(f"lsof returned unsupported status {capture.get('returncode')}")
        REPORT["status_classification"] = status_class
        self_pin_only = len(rows_out) == 1 and exact_pins == 1 and rows_out[0]["exact_root_pin"] and all_rows_bound
        REPORT["self_pin_only"] = self_pin_only
        if status_class not in {"STATUS_ZERO_ALL_SEARCH_ITEMS_MATCHED", "STATUS_ONE_EXPECTED_SEARCH_MISS"}:
            REPORT["blockers"].append("lsof search-status classifier failed closed")
        if not self_pin_only:
            REPORT["blockers"].append("returned holder rows were not exact self-pin only")
        if not identities_stable:
            REPORT["blockers"].append("named/pinned/parent/checker/executable identity changed")
        if not stream.get("complete"):
            REPORT["blockers"].append("accepted parser did not establish complete stream framing")
        if capture["stderr_total_bytes"]:
            REPORT["blockers"].append("normal lsof field-mode stderr was nonempty")
        if capture["timed_out"] or capture["stdout_capped"] or capture["stderr_capped"]:
            REPORT["blockers"].append("lsof capture timed out or hit an output cap")
        REPORT["acceptance"] = {
            **base_gates,
            "status_class_accepted": status_class in {"STATUS_ZERO_ALL_SEARCH_ITEMS_MATCHED", "STATUS_ONE_EXPECTED_SEARCH_MISS"},
            "exactly_one_returned_row": len(rows_out) == 1,
            "exactly_one_expected_pin_row": exact_pins == 1,
            "no_additional_holder": self_pin_only,
            "closeout_not_invoked": REPORT["closeout_invoked"] is False,
            "no_mutations": REPORT["mutations"] == [],
        }
        REPORT["accepted"] = all(REPORT["acceptance"].values()) and not REPORT["blockers"]
        if not REPORT["accepted"]:
            result = 1
        else:
            result = 0
    except BaseException as e:
        REPORT["fatal_error"] = f"{type(e).__name__}: {e}"
        REPORT["blockers"].append(REPORT["fatal_error"])
        result = 1
    finally:
        for name in ("root_pin_fd", "retention_fd", "lease_fd", "allocation_fd", "cache_fd", "parent_fd"):
            fd = OPENED.get(name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        REPORT["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        REPORT["raw_sidecars"] = REPORT.get("raw_sidecars", {})
        try:
            emit()
        except BaseException as e:
            REPORT["evidence_write_error"] = f"{type(e).__name__}: {e}"
            print(f"evidence write failed: {REPORT['evidence_write_error']}", file=sys.stderr)
    print(json.dumps({k: REPORT.get(k) for k in ("run_id", "lsof_scan_invocation_count", "status_classification", "accepted", "exact_pin_match_count", "self_pin_only", "blockers", "acceptance")}, indent=2, default=str))
    return result

if __name__ == "__main__":
    raise SystemExit(main())
