#!/usr/bin/env python3
"""Run the bounded P2 stock-macOS base provisioning and cleanup chain."""
from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import datetime as dt
import fcntl
import gzip
import hashlib
import hmac
import json
import math
import os
import platform
import plistlib
import re
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import vm_capacity_domain as vm  # noqa: E402

GIB = 1024 ** 3
MIB = 1024 ** 2
RESTORE_PATH = Path("/Volumes/FGMetal-VM/restore.ipsw")
VM_ROOT = Path("/Volumes/FGMetal-VM")
VM_VOLUME_UUID = "DC80C359-922C-41DE-B106-5E0C00FEDCE1"
SAFETY_VOLUME_UUID = "0FFC5DA2-F198-4CFF-B1C1-112015D3F126"
CONTAINER_UUID = "0C1A50F1-E2F9-4BB0-A6C2-6B2581B7EE83"
PHYSICAL_STORE_UUID = "69F188B6-DF27-4E2E-960B-919C85AA18B3"
RESTORE_SIZE = 26_637_307_067
RESTORE_SHA256 = "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1"
P1_FINAL_PATH = REPO / "R6-VM-P1-RESTORE-VERIFICATION-20261018T063450Z-d697b287.json"
P1_FINAL_SHA256 = "8ae3597f3e59322bfae50996ef1d9da25cc497a4773becf0cadd1fa54cfaec67"
P1_ADMISSION_PATH = REPO / "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-20261017T152047Z-fc8ad609.json"
P1_ADMISSION_SHA256 = "a8c67f4a48382477ef3f3e4a25cff64c54c700017a0e12b12fcd6ed724c9ba4b"
P1_RENEWAL_PATH = REPO / "R6-VM-P1-FRESHNESS-RENEWAL-20261018T063450Z-d697b287.json"
P1_RENEWAL_SHA256 = "81e1c3ed7940050e66041883bdb36012b65e0fcf780b50b7a0cbf2aaf80bad71"
P1_BODY_START_PATH = REPO / "R6-VM-P1-FRESHNESS-START-20261018T063521Z-bcbcde2b.json"
P1_BODY_START_SHA256 = "a7541c0a136c4e5ca993fa4b440edc49303e0f5a7e7c6f7bbbb8c4fe32d6d921"
UPSTREAM_METADATA_PATH = REPO / "R6-VM-RESTORE-UPSTREAM-METADATA-P1-FRESH-20261018T063450Z-d697b287.json"
UPSTREAM_METADATA_FILE_SHA256 = "b445762ee83423f38a651b62ba1533b18c424c98cb6ca11b10dc9b3278333e14"
UPSTREAM_METADATA_SHA256 = "e80edb264eaa89292e22b416c64dcabd716a125b41eeabe21a2bc6e907d997ee"
RESTORE_SUBJECT_IDENTITY = "rsid-v1:sha256:87cd305ad272ed79c483b6f1eb5c5d9ff0414ce63f6740b3e9638b239c8a5ce5"
P1_UPSTREAM_HARDWARE_MODEL_SHA256 = "18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e"
P2_LOCAL_HARDWARE_MODEL_SHA256 = "976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3"
HARDWARE_MODEL_SHA256 = P1_UPSTREAM_HARDWARE_MODEL_SHA256

P2_RECONCILIATION_PATH = REPO / "R6-VM-P2-LOCAL-COMPATIBILITY-RECONCILIATION-20261018T133047Z-p2-local-model-addendum-r1-48e80d1c.json"
P2_RECONCILIATION_SHA256 = "4ab4e0f7ed7613ad756a875cfacbb6d93988a181edc37b03d4bb23718782003b"
P2_RECONCILIATION_REVIEW_PATH = REPO / "R6-VM-P2-LOCAL-COMPATIBILITY-RECONCILIATION-REVIEW-20261018T133358Z-p2-local-model-addendum-review-r1-FRESH.json"
P2_RECONCILIATION_REVIEW_SHA256 = "963e49d0530e4825e6af485905f2f43ab81a114dca2cc197fa2dba6867d422b1"
P2_OBSERVATION_PATH = REPO / "R6-VM-P2-LOCAL-COMPATIBILITY-OBSERVATION-20261018T1241Z-p2-local-compat-observation-r2.json"
P2_OBSERVATION_SHA256 = "4650faffaa38b1f71488e1adeebf2c8f1b503d70fc863f73938883727f809a7c"
P2_ACCEPTED_IDENTITY_PLAN_PATH = REPO / "R6-VM-P2-IDENTITY-PLAN-20261018T184414Z-p2-storage-recheck-1ea851d0.json"
P2_ACCEPTED_IDENTITY_PLAN_SHA256 = "b263aa48608cb2c481d560a7c03cbf15a62442dac5b11f192da5d3c7bd57b620"
P2_R2_ADMISSION_PATH = REPO / "R6-VM-P2-ADMISSION-20261018T190443Z-p2-post-cleanup-a2ba0170.json"
P2_R2_ADMISSION_SHA256 = "e0f3f602a59746ff492b6ce47abc18347c07949812435d4ae9e56a8ee18bd0b5"
P2_R3_ADMISSION_PATH = REPO / "R6-VM-P2-ADMISSION-20261018T195949Z-p2-r3-resumability-51c4cd74.json"
P2_R3_ADMISSION_SHA256 = "b567e166a92890195d33c4ce2d5ab72c5723d1399baa2d1b0afffac21a028045"
P2_R3_CAPABILITY_COMMITMENT_SHA256 = "aade128f2c30e9244db7d785c90aa950c938211670199b8141db9bbf441d38d6"
P2_R3_SOURCE_MANIFEST_SHA256 = "cd9c59c9c3a2e775f2f6c9cea61d4295e4e920c99893aafbb39ef8e6dfdd5394"
P2_R3_PROVISIONER_REVIEW_SHA256 = "51c15f5ccbe3e9addc88c92b0cd4ce2ceabccba43818112f151393950196be88"
P2_R3_IDENTITY_PLAN_SHA256 = "b263aa48608cb2c481d560a7c03cbf15a62442dac5b11f192da5d3c7bd57b620"
P2_R3_ACTIVE_MARKER_SHA256 = "d53ecb28da202e5efaa03acf5cb3e8c64ed1022d250bc63dbfe602e349e74850"
P2_R3_PREFLIGHT_SHA256 = "402d4f33f8d0371e45fac0356b0ef5d0128e44fb7cd233c63d01cd0ed2b72bfc"
P2_R3_RUN_ID = "20261018T195949Z-p2-r3-resumability-51c4cd74"
P2_R3_UNCHANGED_HELPER_RELATIONSHIP_PATH = REPO / (
    "R6-VM-P2-R3-UNCHANGED-HELPER-RELATIONSHIP-20261018T195949Z-"
    "p2-r3-resumability-51c4cd74.json")
P2_EXECUTION_START_LEASE_VERSION = 1
P2_EXECUTION_START_MAX_AGE_SECONDS = 30
P2_EXECUTION_START_AUTHORITY = "START_EXACT_P2_ADMISSION_EXECUTION"
P2_EXECUTION_START_SIGNATURE_FIELD = "vm_p2_execution_start_hmac_sha256"
P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD = (
    "vm_p2_execution_packet_supersession_hmac_sha256")
P2_R2_SESSION_ID = "bc18455a44e143c0a9a0d4e0227b0db9"
P2_R2_EXECUTION_BLOCKER_PATH = REPO / "R6-VM-P2-R2-ADMISSION-EXECUTION-BLOCKER-20261018T194428Z-p2-r3-resumability-repair-blocker-reconciliation.json"
P2_R2_EXECUTION_BLOCKER_SHA256 = "073bb335e6e76cae9b4a325ffb74acfd6ea07c2602857e18db35ee05bceac21e"
P2_R2_EXECUTION_BLOCKER_PREDECESSOR_PATH = REPO / "R6-VM-P2-R2-ADMISSION-EXECUTION-BLOCKER-20261018T193846Z-p2-r3-resumability-repair.json"
P2_R2_EXECUTION_BLOCKER_PREDECESSOR_SHA256 = "360c81f22e7c8827e87b3f88dd5412f420e5dd7f5b8ada6bdc410eeaa10ebb95"

P2_SYSTEM_MAX = 17 * GIB
P2_AUX_MAX = GIB
P2_METADATA_MAX = 32 * MIB
P2_METADATA_FINALIZATION_RESERVE = 2 * MIB
P2_GROWTH_MAX = P2_SYSTEM_MAX + P2_AUX_MAX + P2_METADATA_MAX
P2_HELPER_LAUNCH_ENVIRONMENT = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}
SYSTEM_LOGICAL_CAPACITY = 64 * GIB
AUXILIARY_LOGICAL_CAPACITY = 2 * GIB
P2_CONTROLLER_DEPENDENCIES = (
    HERE / "vm_capacity_domain.py",
    HERE / "storage_policy.py",
)
P2_CLEANUP_LOCK_PATH = Path("/tmp") / f"fgmetal-p2-restore-cleanup-{os.geteuid()}.lock"
P2_CLEANUP_LOCK_SCOPE = (
    "cooperative same-effective-uid serialization for P2 restore cleanup, recovery, "
    "reconciliation, and finalization; arbitrary same-uid hostile code remains in the trusted actor boundary"
)
P2_LAUNCH_CAPABILITY_FORMAT_VERSION = 1
P2_LAUNCH_CAPABILITY_ROOT = vm.policy.VM_CAPACITY_DOMAIN_ROOT / "p2-launch-capabilities"
P2_LAUNCH_CAPABILITY_STORE_ID = "FGMetal/vm-capacity-domain/p2-launch-capabilities/v1"
P2_LAUNCH_CAPABILITY_RENAME_EXCL = 0x00000004


def sha256_file(path: Path, *, maximum: int | None = None) -> tuple[str, int, dict[str, int]]:
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"not a single-link regular file: {path}")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 8 * MIB)
            if not block:
                break
            total += len(block)
            if maximum is not None and total > maximum:
                raise RuntimeError(f"file exceeded its logical size bound: {path}")
            digest.update(block)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        identity = file_identity(after)
        if (identity != file_identity(before) or identity != file_identity(current)
                or total != after.st_size):
            raise RuntimeError(f"file identity changed while hashing: {path}")
        return digest.hexdigest(), total, identity
    finally:
        os.close(descriptor)


def file_identity(info: os.stat_result) -> dict[str, int]:
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "logical_bytes": int(info.st_size),
        "allocated_bytes": int(getattr(info, "st_blocks", 0)) * 512,
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _seal_file_identity(info: os.stat_result) -> dict[str, int]:
    birth_ns = getattr(info, "st_birthtime_ns", None)
    if birth_ns is None:
        birthtime = getattr(info, "st_birthtime", None)
        if birthtime is None:
            raise RuntimeError("filesystem does not expose a file birth time required for base sealing")
        birth_ns = int(float(birthtime) * 1_000_000_000)
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "logical_bytes": int(info.st_size),
        "allocated_bytes": int(getattr(info, "st_blocks", 0)) * 512,
        "link_count": int(info.st_nlink),
        "owner_uid": int(info.st_uid),
        "owner_gid": int(info.st_gid),
        "mode": int(stat.S_IMODE(info.st_mode)),
        "flags": int(getattr(info, "st_flags", 0)),
        "birthtime_ns": int(birth_ns),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
    }


def _compact_file_identity(identity: Mapping[str, int]) -> dict[str, int]:
    return {key: int(identity[key]) for key in (
        "device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns", "ctime_ns")}


def _open_reviewed_binary_for_exec(path: Path, expected_sha256: str,
                                   expected_identity: Mapping[str, int]) -> tuple[int, dict[str, int]]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        path_before = os.stat(path, follow_symlinks=False)
        before_identity = _seal_file_identity(before)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before_identity != dict(expected_identity)
                or before_identity != _seal_file_identity(path_before)):
            raise RuntimeError("reviewed provisioner executable identity changed before descriptor-bound launch")
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            block = os.read(descriptor, 8 * MIB)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        after_identity = _seal_file_identity(after)
        if (digest.hexdigest() != expected_sha256
                or after_identity != before_identity
                or after_identity != _seal_file_identity(path_after)):
            raise RuntimeError("reviewed provisioner bytes or file identity changed before descriptor-bound launch")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, after_identity
    except BaseException:
        os.close(descriptor)
        raise


def _verify_suspended_exec_descriptor(descriptor: int, path: Path, expected_sha256: str,
                                      expected_identity: Mapping[str, int]) -> dict[str, Any]:
    before = os.fstat(descriptor)
    path_before = os.stat(path, follow_symlinks=False)
    before_identity = _seal_file_identity(before)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before_identity != dict(expected_identity)
            or before_identity != _seal_file_identity(path_before)):
        raise RuntimeError("suspended helper executable identity changed between exec and resume")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 8 * MIB)
        if not block:
            break
        digest.update(block)
    after = os.fstat(descriptor)
    path_after = os.stat(path, follow_symlinks=False)
    after_identity = _seal_file_identity(after)
    if (digest.hexdigest() != expected_sha256
            or after_identity != before_identity
            or after_identity != _seal_file_identity(path_after)):
        raise RuntimeError("suspended helper executable bytes changed before code identity approval")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return {
        "sha256": digest.hexdigest(),
        "identity_before": before_identity,
        "identity_after": after_identity,
        "path_identity_after": _seal_file_identity(path_after),
        "matched_reviewed_binary": True,
    }


class SpawnedChild:
    """Popen-compatible view of a posix_spawn child held before user code runs."""

    def __init__(self, pid: int, process_group_id: int, stdin_fd: int,
                 stdout_fd: int, stderr_fd: int, args: list[str]):
        self.pid = pid
        self.process_group_id = process_group_id
        self.args = args
        self.returncode: int | None = None
        self.code_directory_hash: str | None = None
        self.executable_sha256_after_suspended_exec: str | None = None
        self.executable_identity_after_suspended_exec: dict[str, Any] | None = None
        self._wait_lock = threading.Lock()
        self.stdin = os.fdopen(stdin_fd, "w", encoding="utf-8", buffering=1)
        self.stdout = os.fdopen(stdout_fd, "r", encoding="utf-8", errors="replace", buffering=1)
        self.stderr = os.fdopen(stderr_fd, "r", encoding="utf-8", errors="replace", buffering=1)

    def poll(self) -> int | None:
        with self._wait_lock:
            if self.returncode is not None:
                return self.returncode
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                if self.returncode is None:
                    raise RuntimeError("P2 helper child was reaped outside its process owner")
                return self.returncode
            if waited == 0:
                return None
            if os.WIFEXITED(status):
                self.returncode = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                self.returncode = -os.WTERMSIG(status)
            else:
                raise RuntimeError("P2 helper reached an unexpected wait state")
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            selected = self.poll()
            if selected is not None:
                return selected
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.01)

    def terminate(self) -> None:
        try:
            os.killpg(self.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        try:
            os.killpg(self.process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        output: list[str] = []
        errors: list[str] = []
        readers = [
            threading.Thread(target=lambda: output.append(self.stdout.read()), daemon=True),
            threading.Thread(target=lambda: errors.append(self.stderr.read()), daemon=True),
        ]
        try:
            if self.stdin is not None:
                self.stdin.close()
            for reader in readers:
                reader.start()
            self.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            self.wait(timeout=5)
            raise
        finally:
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            raise RuntimeError("P2 helper output reader did not finish after process exit")
        return (output[0] if output else "", errors[0] if errors else "")


def _spawn_reviewed_binary_suspended(command: list[str], executable_fd: int,
                                     expected_sha256: str,
                                     expected_identity: Mapping[str, int],
                                     expected_cdhash: str,
                                     on_spawned_failure: Any | None = None) -> SpawnedChild:
    """Exec the reviewed path paused; verify its image and open-file identity before resume."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    pointer = ctypes.c_void_p
    actions = pointer()
    attributes = pointer()
    pipe_fds: list[int] = []
    child: SpawnedChild | None = None
    pid = ctypes.c_int()

    def checked(result: int, operation: str) -> None:
        if result != 0:
            raise OSError(result, f"{operation} failed")

    def bytes_array(values: list[bytes]) -> Any:
        return (ctypes.c_char_p * (len(values) + 1))(*values, None)

    try:
        libc.posix_spawn_file_actions_init.argtypes = (ctypes.POINTER(pointer),)
        libc.posix_spawn_file_actions_init.restype = ctypes.c_int
        libc.posix_spawn_file_actions_destroy.argtypes = (ctypes.POINTER(pointer),)
        libc.posix_spawn_file_actions_destroy.restype = ctypes.c_int
        libc.posix_spawn_file_actions_adddup2.argtypes = (ctypes.POINTER(pointer), ctypes.c_int, ctypes.c_int)
        libc.posix_spawn_file_actions_adddup2.restype = ctypes.c_int
        libc.posix_spawn_file_actions_addclose.argtypes = (ctypes.POINTER(pointer), ctypes.c_int)
        libc.posix_spawn_file_actions_addclose.restype = ctypes.c_int
        libc.posix_spawnattr_init.argtypes = (ctypes.POINTER(pointer),)
        libc.posix_spawnattr_init.restype = ctypes.c_int
        libc.posix_spawnattr_destroy.argtypes = (ctypes.POINTER(pointer),)
        libc.posix_spawnattr_destroy.restype = ctypes.c_int
        libc.posix_spawnattr_setflags.argtypes = (ctypes.POINTER(pointer), ctypes.c_short)
        libc.posix_spawnattr_setflags.restype = ctypes.c_int
        libc.posix_spawnattr_setpgroup.argtypes = (ctypes.POINTER(pointer), ctypes.c_int)
        libc.posix_spawnattr_setpgroup.restype = ctypes.c_int
        libc.posix_spawn.argtypes = (
            ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.POINTER(pointer),
            ctypes.POINTER(pointer), ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p))
        libc.posix_spawn.restype = ctypes.c_int
        child_stdin_read, parent_stdin_write = os.pipe()
        parent_stdout_read, child_stdout_write = os.pipe()
        parent_stderr_read, child_stderr_write = os.pipe()
        pipe_fds.extend((child_stdin_read, parent_stdin_write, parent_stdout_read,
                         child_stdout_write, parent_stderr_read, child_stderr_write))
        checked(libc.posix_spawn_file_actions_init(ctypes.byref(actions)),
                "posix_spawn_file_actions_init")
        checked(libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), child_stdin_read, 0),
                "posix_spawn_file_actions_adddup2(stdin)")
        checked(libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), child_stdout_write, 1),
                "posix_spawn_file_actions_adddup2(stdout)")
        checked(libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), child_stderr_write, 2),
                "posix_spawn_file_actions_adddup2(stderr)")
        for descriptor in (child_stdin_read, parent_stdin_write, parent_stdout_read,
                           child_stdout_write, parent_stderr_read, child_stderr_write):
            if descriptor not in (0, 1, 2):
                checked(libc.posix_spawn_file_actions_addclose(ctypes.byref(actions), descriptor),
                        "posix_spawn_file_actions_addclose")

        checked(libc.posix_spawnattr_init(ctypes.byref(attributes)), "posix_spawnattr_init")
        checked(libc.posix_spawnattr_setpgroup(ctypes.byref(attributes), 0),
                "posix_spawnattr_setpgroup(helper process group)")
        checked(libc.posix_spawnattr_setflags(ctypes.byref(attributes), ctypes.c_short(0x0082)),
                "posix_spawnattr_setflags(POSIX_SPAWN_START_SUSPENDED|POSIX_SPAWN_SETPGROUP)")
        # Darwin rejects exec of /dev/fd/N even when that descriptor was opened
        # with O_EXEC. Keep the reviewed file descriptor open, spawn the exact
        # reviewed pathname suspended, then bind the loaded image to the
        # reviewed CodeDirectory hash and re-hash/re-stat the open file and
        # pathname before any helper user-space code is resumed.
        executable_path = os.fsencode(os.path.abspath(command[0]))
        argv = bytes_array([os.fsencode(value) for value in command])
        environment = bytes_array([os.fsencode(f"{key}={value}")
                                   for key, value in P2_HELPER_LAUNCH_ENVIRONMENT.items()])
        checked(libc.posix_spawn(ctypes.byref(pid), executable_path, ctypes.byref(actions),
                                 ctypes.byref(attributes), argv, environment),
                "posix_spawn(reviewed helper)")
        for descriptor in (child_stdin_read, child_stdout_write, child_stderr_write):
            os.close(descriptor)
            pipe_fds.remove(descriptor)
        child = SpawnedChild(pid.value, pid.value, parent_stdin_write, parent_stdout_read,
                             parent_stderr_read, command)
        for descriptor in (parent_stdin_write, parent_stdout_read, parent_stderr_read):
            pipe_fds.remove(descriptor)

        suspended_file_identity = _verify_suspended_exec_descriptor(
            executable_fd, Path(command[0]), expected_sha256, expected_identity)
        child.executable_sha256_after_suspended_exec = suspended_file_identity["sha256"]
        child.executable_identity_after_suspended_exec = suspended_file_identity
        if os.getpgid(pid.value) != child.process_group_id:
            raise RuntimeError("suspended helper process group did not match its dedicated PID")
        cdhash_buffer = ctypes.create_string_buffer(20)
        csops = libc.csops
        csops.argtypes = (ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t)
        csops.restype = ctypes.c_int
        if csops(pid.value, 5, cdhash_buffer, ctypes.sizeof(cdhash_buffer)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "csops(CS_OPS_CDHASH) could not identify suspended helper")
        process_cdhash = bytes(cdhash_buffer.raw).hex()
        if process_cdhash != expected_cdhash:
            raise RuntimeError("suspended helper kernel CDHash does not match the reviewed CodeDirectory")
        child.code_directory_hash = process_cdhash
        os.kill(pid.value, signal.SIGCONT)
        return child
    except BaseException as spawn_error:
        failed_pid = child.pid if child is not None else pid.value
        if failed_pid:
            if on_spawned_failure is not None:
                try:
                    on_spawned_failure(failed_pid, failed_pid, str(spawn_error))
                except BaseException as pending_error:
                    spawn_error.add_note(f"signed termination-pending write failed: {pending_error}")
        if child is not None:
            while child.poll() is None:
                try:
                    child.kill()
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    continue
                except BaseException as cleanup_error:
                    spawn_error.add_note(f"suspended helper reap pending: {cleanup_error}")
                    try:
                        child.kill()
                    except BaseException:
                        pass
                    time.sleep(0.2)
            for stream in (child.stdin, child.stdout, child.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        elif pid.value:
            while True:
                try:
                    os.killpg(pid.value, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid.value, 0)
                    break
                except InterruptedError:
                    continue
                except ChildProcessError:
                    break
                except OSError as cleanup_error:
                    spawn_error.add_note(f"suspended helper wait failed: {cleanup_error}")
                    time.sleep(0.2)
        raise spawn_error
    finally:
        if actions:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        if attributes:
            libc.posix_spawnattr_destroy(ctypes.byref(attributes))
        for descriptor in pipe_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _hash_file_with_identity_window(path: Path) -> tuple[str, int, dict[str, Any]]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        path_before = os.stat(path, follow_symlinks=False)
        before_identity = _seal_file_identity(before)
        path_before_identity = _seal_file_identity(path_before)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before_identity != path_before_identity):
            raise RuntimeError(f"base artifact is not a stable single-link regular file: {path}")
        hash_started_at_unix = time.time()
        hash_started_monotonic_ns = time.monotonic_ns()
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(descriptor, 8 * MIB)
            if not block:
                break
            total += len(block)
            digest.update(block)
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        hash_finished_monotonic_ns = time.monotonic_ns()
        hash_finished_at_unix = time.time()
        after_identity = _seal_file_identity(after)
        path_after_identity = _seal_file_identity(path_after)
        if (before_identity != after_identity or after_identity != path_after_identity
                or total != after.st_size or not stat.S_ISREG(path_after.st_mode)
                or path_after.st_nlink != 1):
            raise RuntimeError(f"base artifact identity changed during complete-file hashing: {path}")
        return digest.hexdigest(), total, {
            "path": str(path),
            "hash_started_at_unix": hash_started_at_unix,
            "hash_finished_at_unix": hash_finished_at_unix,
            "hash_elapsed_nanoseconds": hash_finished_monotonic_ns - hash_started_monotonic_ns,
            "identity_before_hash": before_identity,
            "identity_at_path_before_hash": path_before_identity,
            "identity_after_hash": after_identity,
            "identity_at_path_after_hash": path_after_identity,
            "identity_stable_across_hash": True,
        }
    finally:
        os.close(descriptor)


def _p2_metadata_inventory(extra_paths: list[Path] | tuple[Path, ...] = ()) -> dict[str, Any]:
    paths: set[Path] = set()
    for pattern in (
            "R6-VM-P2-*", "R6-VM-MACOS-BASE-IDENTITY-*", "R6-VM-RESTORE-CLEANUP-*",
            "R6-VM-RESTORE-QUARANTINE-SCAN-*", "R6-VM-RESTORE-DELETE-AUTHORIZATION-*"):
        paths.update(REPO.glob(pattern))
    build_root = HERE / "p2-provisioner-builds"
    if build_root.exists():
        for path in build_root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            paths.add(path)
    paths.update(Path(path) for path in extra_paths if os.path.lexists(Path(path)))
    rows: list[dict[str, Any]] = []
    total = 0
    for path in sorted(paths, key=lambda item: str(item)):
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f"P2 metadata inventory contains a non-regular or linked file: {path}")
        allocated = int(getattr(info, "st_blocks", 0)) * 512
        total += allocated
        try:
            label = str(path.relative_to(REPO))
        except ValueError:
            label = str(path)
        rows.append({"path": label, "logical_bytes": int(info.st_size),
                     "physical_allocated_bytes": allocated})
    return {"file_count": len(rows), "physical_allocated_bytes": total, "files": rows}


def _acquire_p2_cleanup_lock() -> tuple[int, dict[str, Any]]:
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(P2_CLEANUP_LOCK_PATH, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise RuntimeError("P2 cleanup lock file is not an owner-only single-link regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another P2 cleanup/recovery/reconciliation/finalization transaction holds the lock") from error
        after = os.fstat(descriptor)
        if (after.st_dev != info.st_dev or after.st_ino != info.st_ino
                or after.st_uid != os.geteuid() or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_nlink != 1):
            raise RuntimeError("P2 cleanup lock identity changed during exclusive acquisition")
        evidence = {
            "path": str(P2_CLEANUP_LOCK_PATH),
            "method": "nonblocking exclusive BSD flock on an owner-only no-follow lock file",
            "scope": P2_CLEANUP_LOCK_SCOPE,
            "effective_uid": os.geteuid(),
            "pid": os.getpid(),
            "device": int(after.st_dev),
            "inode": int(after.st_ino),
            "mode": stat.S_IMODE(after.st_mode),
            "link_count": int(after.st_nlink),
            "exclusive_lock_held": True,
        }
        return descriptor, evidence
    except BaseException:
        os.close(descriptor)
        raise


def _valid_p2_cleanup_lock_evidence(value: Any) -> bool:
    return (isinstance(value, Mapping)
            and value.get("path") == str(P2_CLEANUP_LOCK_PATH)
            and value.get("method") == "nonblocking exclusive BSD flock on an owner-only no-follow lock file"
            and value.get("scope") == P2_CLEANUP_LOCK_SCOPE
            and value.get("effective_uid") == os.geteuid()
            and isinstance(value.get("pid"), int) and not isinstance(value.get("pid"), bool)
            and isinstance(value.get("device"), int) and not isinstance(value.get("device"), bool)
            and isinstance(value.get("inode"), int) and not isinstance(value.get("inode"), bool)
            and value.get("mode") == 0o600 and value.get("link_count") == 1
            and value.get("exclusive_lock_held") is True)


def _assert_no_interrupted_cleanup_namespace(*, run_id: str,
                                             root_directory_identity: Mapping[str, Any],
                                             root_mode: int) -> None:
    pending_pattern = f"R6-VM-RESTORE-CLEANUP-PENDING-*-{run_id}.json"
    for pending_path in REPO.glob(pending_pattern):
        pending, _ = read_signed(pending_path, "vm_p2_restore_cleanup_pending_hmac_sha256")
        if pending.get("run_id") != run_id or pending.get("status") != "PENDING_DELETION":
            continue
        pending_root = pending.get("vm_volume_root_identity")
        if (not isinstance(pending_root, Mapping)
                or pending_root.get("device") != root_directory_identity.get("device")
                or pending_root.get("inode") != root_directory_identity.get("inode")):
            continue
        quarantine_path = Path(pending.get("quarantine_directory_path", ""))
        if (quarantine_path.parent != VM_ROOT
                or not quarantine_path.name.startswith(f".p2-restore-quarantine-{run_id}-")):
            raise RuntimeError("signed cleanup pending state contains a noncanonical quarantine path")
        original_mode = pending.get("vm_volume_root_mode_before_cleanup")
        if os.path.lexists(quarantine_path) or (isinstance(original_mode, int)
                and not isinstance(original_mode, bool) and root_mode != original_mode):
            raise RuntimeError(
                "an interrupted restore cleanup namespace requires recover-cleanup before retrying cleanup")


def _assert_p2_metadata_budget(extra_paths: list[Path] | tuple[Path, ...] = (),
                               additional_allocated_bytes: int = 0) -> dict[str, Any]:
    inventory = _p2_metadata_inventory(extra_paths)
    total = inventory["physical_allocated_bytes"] + additional_allocated_bytes
    inventory["additional_allocated_bytes"] = additional_allocated_bytes
    inventory["total_p2_metadata_allocated_bytes"] = total
    inventory["remaining_bytes"] = max(0, P2_METADATA_MAX - total)
    if total > P2_METADATA_MAX:
        raise RuntimeError(f"P1/P2 provisioning metadata exceeded the 32 MiB envelope: {total} bytes")
    return inventory


def _provisioner_metadata_paths(manifest: Mapping[str, Any], *extra: Path) -> list[Path]:
    sources = manifest["source_manifest"]
    paths = [HERE / "vm_p2_provisioner.swift", HERE / "p2_base_orchestrator.py",
             vm.VM_P0_CREATION_RECEIPT_PATH, vm.VM_P0_ADMISSION_PATH,
             P1_FINAL_PATH, P1_ADMISSION_PATH, P1_RENEWAL_PATH, P1_BODY_START_PATH,
             UPSTREAM_METADATA_PATH, P2_R2_ADMISSION_PATH, P2_R2_EXECUTION_BLOCKER_PATH,
             P2_ACCEPTED_IDENTITY_PLAN_PATH,
             vm.VM_ACTIVE_MARKER_PATH, vm.VM_STOP_SAFETY_PROFILE_PATH]
    for key in ("binary", "entitlements"):
        artifact = Path(sources[key]["path"])
        paths.append(artifact if artifact.is_absolute() else REPO / artifact)
    for path in extra:
        paths.append(Path(path))
    return paths


def json_bytes(record: Mapping[str, Any]) -> bytes:
    return vm.policy._canonical_pretty_json_bytes(record)


def _signed_helper_control(command: str, payload: str, control_key: bytes,
                           *, control_token: bytes | None = None) -> str:
    data = payload.encode("ascii")
    signature = hmac.new(control_key, data, hashlib.sha256).hexdigest()
    fields = [command, base64.b64encode(data).decode("ascii")]
    if control_token is not None:
        fields.append(base64.b64encode(control_token).decode("ascii"))
    fields.append(signature)
    return " ".join(fields)


def unique_path(prefix: str, run_id: str, suffix: str = ".json") -> Path:
    return REPO / f"{prefix}-{run_id}{suffix}"


def _fsync_directory(path: Path) -> None:
    """Durably sync a directory through the storage policy's no-follow opener."""
    descriptor = vm.policy._open_directory_nofollow(path, strict_directory_sync=True)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def utc_timestamp(unix_time: float | None = None) -> str:
    selected = time.time() if unix_time is None else unix_time
    return dt.datetime.fromtimestamp(selected, tz=dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def event_monotonic_seconds(event: Mapping[str, Any]) -> float:
    value = event.get("monotonic_time_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise RuntimeError("provisioner event is missing a finite monotonic timestamp")
    return float(value)


def signed_write(path: Path, record: Mapping[str, Any], signature_field: str,
                 signing_key: bytes | None = None) -> dict[str, Any]:
    if signing_key is None:
        return vm._write_signed_json_unique(path, record, signature_field)
    sealed, raw = _signed_record_bytes(record, signature_field, signing_key)
    parent_fd = vm.policy._open_directory_nofollow(path.parent, create=True, strict_directory_sync=True)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
        view = memoryview(raw)
        while view:
            amount = os.write(descriptor, view)
            if amount <= 0:
                raise OSError("short write while persisting signed P2 failure evidence")
            view = view[amount:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("signed P2 evidence has an unsafe file identity")
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    verified, verified_sha256 = read_signed_with_key(path, signature_field, signing_key)
    if (verified != sealed
            or not hmac.compare_digest(verified_sha256, hashlib.sha256(raw).hexdigest())):
        raise RuntimeError("signed P2 evidence did not survive exact durable readback")
    return sealed


def _signed_record_bytes(record: Mapping[str, Any], signature_field: str,
                         signing_key: bytes) -> tuple[dict[str, Any], bytes]:
    """Return the exact durable encoding for a signed record without writing it."""
    sealed = dict(record)
    sealed.pop(signature_field, None)
    canonical = json.dumps(sealed, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    sealed[signature_field] = hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()
    return sealed, json_bytes(sealed)


def _open_private_capability_directory(parent_fd: int, name: str, *, create: bool) -> int:
    if not name or name in {".", ".."} or os.sep in name or "\x00" in name:
        raise ValueError("launch capability directory component is invalid")
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
            os.fsync(parent_fd)
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        os.close(descriptor)
        raise RuntimeError("launch capability directory has an unsafe owner or type")
    if created:
        os.fchmod(descriptor, 0o700)
        info = os.fstat(descriptor)
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.close(descriptor)
        raise RuntimeError("launch capability directory is not owner-only mode 0700")
    return descriptor


def _open_launch_capability_session_directory(session_id: str, *,
                                               control_root: Path | None = None,
                                               create: bool) -> int:
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise RuntimeError("P2 launch capability session ID is malformed")
    root = Path(control_root or vm.policy.VM_CAPACITY_DOMAIN_ROOT)
    root_fd = vm.policy._open_directory_nofollow(root, create=False)
    try:
        root_info = os.fstat(root_fd)
        if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)):
            raise RuntimeError("P2 launch capability control root has an unsafe owner or permissions")
        capability_fd = _open_private_capability_directory(
            root_fd, "p2-launch-capabilities", create=create)
    finally:
        os.close(root_fd)
    try:
        version_fd = _open_private_capability_directory(capability_fd, "v1", create=create)
    finally:
        os.close(capability_fd)
    try:
        return _open_private_capability_directory(version_fd, session_id, create=create)
    finally:
        os.close(version_fd)


def _acquire_r3_admission_issuance_lock(session_id: str, *,
                                       control_root: Path | None = None):
    """Serialize same-session R3 admission issuance in the private control domain."""
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=True)
    lock_name = "r3-admission-issuance.lock"
    descriptor: int | None = None
    try:
        created = False
        try:
            descriptor = os.open(lock_name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600,
                                 dir_fd=directory_fd)
            created = True
        except FileExistsError:
            descriptor = os.open(lock_name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                                 dir_fd=directory_fd)
        if created:
            os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino)):
            raise RuntimeError("R3 admission issuance lock has an unsafe owner, type, mode, or identity")
        if created:
            os.fsync(directory_fd)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked_info = os.fstat(descriptor)
        locked_entry = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
        if ((locked_info.st_dev, locked_info.st_ino) != (locked_entry.st_dev, locked_entry.st_ino)
                or not stat.S_ISREG(locked_info.st_mode) or locked_info.st_uid != os.geteuid()
                or locked_info.st_nlink != 1 or stat.S_IMODE(locked_info.st_mode) != 0o600):
            raise RuntimeError("R3 admission issuance lock path changed after acquisition")
        lock_file = os.fdopen(descriptor, "a+")
        descriptor = None
        return lock_file
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _read_supersession_receipt(path: Path, *,
                               signing_key: bytes | None = None) -> tuple[dict[str, Any], str]:
    key = signing_key if signing_key is not None else vm.policy._manifest_signing_key()
    return read_signed_with_key(path, P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, key)


def _validate_supersession_receipt_for_admission(
        receipt_path: Path, admission_path: Path, admission: Mapping[str, Any],
        admission_sha256: str, *, signing_key: bytes | None = None,
        control_root: Path | None = None) -> tuple[dict[str, Any], str]:
    key = signing_key if signing_key is not None else vm.policy._manifest_signing_key()
    receipt, receipt_sha = _read_supersession_receipt(receipt_path, signing_key=key)
    session_id = admission.get("session_id")
    commitment = admission.get("helper_control_token_sha256")
    resolved_admission = admission_path.resolve(strict=True)
    if REPO.resolve() not in resolved_admission.parents:
        raise RuntimeError("superseded P2 admission path is outside the repository")
    expected_relative = str(resolved_admission.relative_to(REPO.resolve()))
    if (receipt.get("record_type") != "R6_VM_P2_R3_EXECUTION_PACKET_SUPERSESSION"
            or receipt.get("schema_version") != 1
            or receipt.get("protocol_version") != "P2_R4_EXECUTION_START_LEASE_V1"
            or receipt.get("status") != "PASS"
            or receipt.get("classification")
                != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"
            or receipt.get("authority_scope") != "NONE"
            or receipt.get("execution_authority") != "NONE"
            or receipt.get("r3_admission_path") != expected_relative
            or receipt.get("r3_admission_sha256") != admission_sha256
            or receipt.get("r3_run_id") != admission.get("run_id")
            or receipt.get("r3_session_id") != session_id
            or receipt.get("r3_admission_signature_valid") is not True
            or receipt.get("r3_source_manifest_sha256")
                != admission.get("provisioner_source_manifest_sha256")
            or receipt.get("r3_provisioner_review_sha256")
                != admission.get("provisioner_review_sha256")
            or receipt.get("r3_identity_plan_sha256")
                != admission.get("p2_identity_plan_sha256")
            or receipt.get("r3_preflight_sha256")
                != admission.get("p2_preflight_sha256")
            or not isinstance(commitment, str)
            or receipt.get("r3_capability_commitment_sha256") != commitment
            or receipt.get("capability_state_before") != "READY"
            or receipt.get("claimed_state_absent_before") is not True
            or receipt.get("capability_state_after") != "SUPERSEDED"
            or receipt.get("capability_commitment_verified_after") is not True
            or receipt.get("observed_source_ordering_defect") is not True
            or receipt.get("guest_mutation_occurred") is not False
            or receipt.get("authority") != "NONE"):
        raise RuntimeError("R3 supersession receipt is malformed or does not bind this exact admission")
    if receipt_path.name != f"R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION-{receipt.get('r4_run_id')}.json":
        raise RuntimeError("R3 supersession receipt path does not bind its R4 run ID")
    if not isinstance(receipt.get("r4_run_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,128}", receipt["r4_run_id"]):
        raise RuntimeError("R3 supersession receipt has a malformed R4 run ID")
    private_state = receipt.get("private_capability_state")
    if (not isinstance(private_state, Mapping)
            or private_state.get("path_suffix") != ".superseded"
            or private_state.get("status") != "SUPERSEDED"
            or private_state.get("commitment_sha256") != commitment
            or not isinstance(private_state.get("file_identity"), Mapping)):
        raise RuntimeError("R3 supersession receipt omits the resulting private state binding")
    historical_report = REPO / f"R6-VM-P2-R3-EXECUTION-RESUMABILITY-REPAIR-FINAL-{P2_R3_RUN_ID}.md"
    restore = receipt.get("restore")
    marker_before = receipt.get("active_marker_sha256_before")
    marker_after = receipt.get("active_marker_sha256_after")
    guest_assets_before = receipt.get("guest_assets_present_before")
    guest_assets_after = receipt.get("guest_assets_present_after")
    if (receipt.get("r3_admission_signature_valid") is not True
            or receipt.get("source_ordering_defect")
                != "R3 consumed the private READY capability before the controller rejected the stale static admission at monitor initialization."
            or not isinstance(receipt.get("stale_admission_blocker"), Mapping)
            or receipt["stale_admission_blocker"].get("monitor_maximum_age_seconds")
                != P2_EXECUTION_START_MAX_AGE_SECONDS
            or receipt["stale_admission_blocker"].get("static_admission_remains_signed_pass") is not True
            or receipt.get("guest_mutation_occurred") is not False
            or guest_assets_before is not False or guest_assets_after is not False
            or receipt.get("cross_session_marker_transition") is not False
            or receipt.get("raw_capability_bytes_included") is not False
            or marker_before != P2_R3_ACTIVE_MARKER_SHA256 or marker_after != marker_before
            or receipt.get("blocking_processes_before") != []
            or not isinstance(restore, Mapping)
            or restore.get("path") != str(RESTORE_PATH)
            or restore.get("sha256_before") != admission.get("restore_sha256")
            or restore.get("sha256_after") != restore.get("sha256_before")
            or restore.get("logical_bytes") != RESTORE_SIZE
            or restore.get("file_identity") != admission.get("restore_file_identity")
            or not isinstance(receipt.get("r3_final_report"), Mapping)
            or receipt["r3_final_report"].get("path") != str(historical_report.relative_to(REPO))
            or receipt["r3_final_report"].get("sha256") != vm._file_sha256(historical_report)):
        raise RuntimeError("R3 supersession receipt omits exact no-mutation or preservation evidence")
    implementation_review = receipt.get("r4_implementation_review")
    provenance = receipt.get("r4_provenance")
    if (not isinstance(implementation_review, Mapping)
            or implementation_review.get("accepted_decision") not in {
                "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED",
                "PASS WITH NON-BLOCKING CAVEATS"}
            or not isinstance(provenance, Mapping)
            or provenance.get("unchanged_helper_relationship_status") != "PASS"):
        raise RuntimeError("R3 supersession receipt omits accepted R4 review and provenance bindings")
    implementation_path = _admission_artifact_path(
        implementation_review.get("path"), "R4 implementation review")
    if vm._file_sha256(implementation_path) != implementation_review.get("sha256"):
        raise RuntimeError("R4 implementation review changed after R3 supersession")
    provenance_manifest_path = _admission_artifact_path(
        provenance.get("manifest_path"), "R4 supersession source manifest")
    provenance_review_path = _admission_artifact_path(
        provenance.get("review_path"), "R4 supersession provenance review")
    if (vm._file_sha256(provenance_manifest_path) != provenance.get("manifest_sha256")
            or vm._file_sha256(provenance_review_path) != provenance.get("review_sha256")):
        raise RuntimeError("R4 supersession provenance changed after retirement")
    directory_fd = _open_launch_capability_session_directory(
        str(session_id), control_root=control_root, create=False)
    try:
        state = _capability_file_state(directory_fd, str(session_id), admission_sha256)
        if (state.get("status") != "SUPERSEDED"
                or state.get("file_identity") != private_state.get("file_identity")):
            raise RuntimeError("R3 private capability state differs from its signed supersession receipt")
        descriptor = os.open(state["path_name"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            token_hash = hashlib.sha256(os.read(descriptor, 33)).hexdigest()
            post = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (token_hash != commitment
                or not _same_capability_file(
                    os.stat(state["path_name"], dir_fd=directory_fd, follow_symlinks=False), post)):
            raise RuntimeError("R3 superseded capability commitment or file identity no longer verifies")
    finally:
        os.close(directory_fd)
    # Ensure a valid signature is not being used to relabel a modified R3 PASS.
    historical, historical_sha = read_signed_with_key(
        resolved_admission, "vm_admission_hmac_sha256", key)
    if historical_sha != admission_sha256 or historical != dict(admission):
        raise RuntimeError("R3 admission changed while validating its supersession receipt")
    return receipt, receipt_sha


def _validate_current_r3_supersession(r4_run_id: str, receipt_path: Path, *,
                                      signing_key: bytes,
                                      control_root: Path | None = None,
                                      manifest_path: Path | None = None,
                                      review_path: Path | None = None
                                      ) -> tuple[dict[str, Any], str]:
    """Require the exact R3 terminal receipt that makes this R4 packet unique."""
    admission, admission_sha = read_signed_with_key(
        P2_R3_ADMISSION_PATH, "vm_admission_hmac_sha256", signing_key)
    if (admission_sha != P2_R3_ADMISSION_SHA256
            or vm._file_sha256(P2_R3_ADMISSION_PATH) != P2_R3_ADMISSION_SHA256):
        raise RuntimeError("R4 issuance requires the byte-identical accepted R3 admission")
    receipt, receipt_sha = _validate_supersession_receipt_for_admission(
        Path(receipt_path).resolve(strict=True), P2_R3_ADMISSION_PATH, admission,
        admission_sha, signing_key=signing_key, control_root=control_root)
    if receipt.get("r4_run_id") != r4_run_id:
        raise RuntimeError("R4 supersession receipt belongs to another R4 run")
    r4_provenance = receipt.get("r4_provenance")
    if (manifest_path is not None and (not isinstance(r4_provenance, Mapping)
            or _admission_artifact_path(r4_provenance.get("manifest_path"),
                                       "R4 supersession source manifest")
                != Path(manifest_path).resolve(strict=True)
            or r4_provenance.get("manifest_sha256") != vm._file_sha256(manifest_path))):
        raise RuntimeError("R3 supersession receipt does not bind the exact R4 source manifest")
    if (review_path is not None and (not isinstance(r4_provenance, Mapping)
            or _admission_artifact_path(r4_provenance.get("review_path"),
                                       "R4 supersession provenance review")
                != Path(review_path).resolve(strict=True)
            or r4_provenance.get("review_sha256") != vm._file_sha256(review_path))):
        raise RuntimeError("R3 supersession receipt does not bind the exact R4 provenance review")
    return receipt, receipt_sha


def _supersession_receipt_for_admission(admission_path: Path, admission: Mapping[str, Any],
                                        admission_sha256: str, *,
                                        signing_key: bytes | None = None,
                                        control_root: Path | None = None
                                        ) -> tuple[dict[str, Any], str] | None:
    session_id = admission.get("session_id")
    matches: list[Path] = []
    for candidate in sorted(REPO.glob("R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION-*.json")):
        receipt, _digest = _read_supersession_receipt(candidate, signing_key=signing_key)
        if (receipt.get("r3_admission_sha256") == admission_sha256
                or receipt.get("r3_admission_path") == str(admission_path.resolve().relative_to(REPO.resolve()))
                or (receipt.get("r3_session_id") == session_id
                    and receipt.get("r3_run_id") == admission.get("run_id"))):
            matches.append(candidate)
    if len(matches) > 1:
        raise RuntimeError("duplicate R3 supersession receipts make the same-session packet ambiguous")
    if not matches:
        return None
    return _validate_supersession_receipt_for_admission(
        matches[0], admission_path, admission, admission_sha256,
        signing_key=signing_key, control_root=control_root)


def _assert_admission_not_superseded(admission_path: Path, admission: Mapping[str, Any],
                                    admission_sha256: str, *,
                                    signing_key: bytes | None = None,
                                    control_root: Path | None = None) -> None:
    if _supersession_receipt_for_admission(
            admission_path, admission, admission_sha256,
            signing_key=signing_key, control_root=control_root) is not None:
        raise RuntimeError("this valid historical P2 admission execution packet was superseded before claim")


def _assert_single_r3_pass_admission(session_id: str, *,
                                     current_admission_path: Path | None = None,
                                     current_admission_sha256: str | None = None,
                                     signing_key: bytes | None = None,
                                     control_root: Path | None = None) -> None:
    """Allow one executable PASS P2 admission per authenticated marker lineage."""
    if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise RuntimeError("R3 admission marker-lineage session ID is malformed")
    current_path = (current_admission_path.resolve(strict=True)
                    if current_admission_path is not None else None)
    if current_path is not None and REPO.resolve() not in current_path.parents:
        raise RuntimeError("current R3 admission path is outside the repository")
    if ((current_path is None) != (current_admission_sha256 is None)
            or (current_admission_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", current_admission_sha256))):
        raise RuntimeError("current R3 admission single-run binding is incomplete")
    for candidate in sorted(REPO.glob("R6-VM-P2-ADMISSION-*.json")):
        if candidate == P2_R2_ADMISSION_PATH:
            if vm._file_sha256(candidate) != P2_R2_ADMISSION_SHA256:
                raise RuntimeError("immutable historical R2 admission digest changed")
            continue
        record, digest = read_signed(candidate, "vm_admission_hmac_sha256")
        if (record.get("record_type") != "R6_VM_P2_PROVISIONING_ADMISSION"
                or record.get("session_id") != session_id
                or record.get("decision") != "PASS"
                or record.get("authorized_phase") != "P2"):
            continue
        supersession = _supersession_receipt_for_admission(
            candidate, record, digest, signing_key=signing_key,
            control_root=control_root)
        if supersession is not None:
            continue
        if (current_path is not None and candidate.resolve(strict=True) == current_path
                and digest == current_admission_sha256):
            continue
        raise RuntimeError("a different unsuperseded PASS P2 admission already occupies this marker lineage")


def _launch_capability_names(session_id: str, admission_sha256: str) -> tuple[str, str]:
    if (not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or not isinstance(admission_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", admission_sha256)):
        raise RuntimeError("P2 launch capability admission binding is malformed")
    stem = admission_sha256
    return f"{stem}.ready", f"{stem}.claimed"


def _superseded_capability_name(admission_sha256: str) -> str:
    if not isinstance(admission_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", admission_sha256):
        raise RuntimeError("P2 supersession admission binding is malformed")
    return f"{admission_sha256}.superseded"


def _capability_file_state(directory_fd: int, session_id: str,
                           admission_sha256: str) -> dict[str, Any]:
    ready_name, claimed_name = _launch_capability_names(session_id, admission_sha256)
    superseded_name = _superseded_capability_name(admission_sha256)
    states: dict[str, os.stat_result | None] = {}
    for state, name in (("READY", ready_name), ("CLAIMED", claimed_name),
                        ("SUPERSEDED", superseded_name)):
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        states[state] = info
    present = [state for state, info in states.items() if info is not None]
    if len(present) != 1:
        raise RuntimeError("P2 launch capability state is missing or ambiguous")
    selected = present[0]
    info = states[selected]
    assert info is not None
    _capability_entry_info(directory_fd, {
        "READY": ready_name, "CLAIMED": claimed_name, "SUPERSEDED": superseded_name,
    }[selected], expected_uid=os.geteuid())
    return {
        "status": selected,
        "path_name": {"READY": ready_name, "CLAIMED": claimed_name,
                       "SUPERSEDED": superseded_name}[selected],
        "file_identity": {"device": info.st_dev, "inode": info.st_ino,
                          "owner_uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
                          "link_count": info.st_nlink, "length": info.st_size},
    }


def _capability_entry_info(directory_fd: int, name: str, *, expected_uid: int,
                           expected_size: int = 32) -> os.stat_result:
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size != expected_size):
        raise RuntimeError("P2 launch capability has an unsafe type, owner, mode, link count, or length")
    return info


def _same_capability_file(info_a: os.stat_result, info_b: os.stat_result) -> bool:
    return (info_a.st_dev == info_b.st_dev and info_a.st_ino == info_b.st_ino
            and info_a.st_uid == info_b.st_uid and info_a.st_nlink == info_b.st_nlink
            and stat.S_IMODE(info_a.st_mode) == stat.S_IMODE(info_b.st_mode)
            and info_a.st_size == info_b.st_size)


def _read_launch_capability(directory_fd: int, ready_name: str, claimed_name: str,
                            commitment_sha256: str, *, expected_uid: int | None = None
                            ) -> tuple[bytes, os.stat_result]:
    uid = os.geteuid() if expected_uid is None else expected_uid
    superseded_name = ready_name.removesuffix(".ready") + ".superseded"
    for terminal_name, terminal_state in ((claimed_name, "claimed"),
                                          (superseded_name, "superseded")):
        try:
            os.stat(terminal_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise RuntimeError(f"P2 launch capability is already {terminal_state} or has a conflicting state")
    descriptor = os.open(ready_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        entry_before = _capability_entry_info(directory_fd, ready_name, expected_uid=uid)
        if not _same_capability_file(before, entry_before):
            raise RuntimeError("P2 launch capability path changed while opening")
        token = os.read(descriptor, 33)
        after = os.fstat(descriptor)
        try:
            entry_after = _capability_entry_info(directory_fd, ready_name, expected_uid=uid)
        except FileNotFoundError as error:
            raise RuntimeError("P2 launch capability disappeared while reading") from error
        if (len(token) != 32 or not _same_capability_file(before, after)
                or not _same_capability_file(after, entry_after)
                or not hmac.compare_digest(hashlib.sha256(token).hexdigest(), commitment_sha256)):
            raise RuntimeError("P2 launch capability changed or failed its signed commitment")
        return token, after
    finally:
        os.close(descriptor)


def _write_launch_capability_ready(session_id: str, admission_sha256: str, token: bytes, *,
                                   control_root: Path | None = None,
                                   expected_uid: int | None = None) -> Path:
    if not isinstance(token, bytes) or len(token) != 32:
        raise RuntimeError("new P2 launch capability must contain exactly 32 random bytes")
    uid = os.geteuid() if expected_uid is None else expected_uid
    ready_name, claimed_name = _launch_capability_names(session_id, admission_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=True)
    descriptor: int | None = None
    try:
        for terminal_name in (claimed_name, _superseded_capability_name(admission_sha256)):
            try:
                os.stat(terminal_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("P2 launch capability has a terminal or conflicting state")
        descriptor = os.open(ready_name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 0):
            raise RuntimeError("new P2 launch capability file has unsafe metadata")
        written = 0
        while written < len(token):
            amount = os.write(descriptor, token[written:])
            if amount <= 0:
                raise OSError("short write while persisting P2 launch capability")
            written += amount
        os.fsync(descriptor)
        written_info = os.fstat(descriptor)
        final = _capability_entry_info(directory_fd, ready_name, expected_uid=uid)
        if not _same_capability_file(written_info, final):
            raise RuntimeError("new P2 launch capability file identity changed during creation")
        os.lseek(descriptor, 0, os.SEEK_SET)
        readback = os.read(descriptor, 33)
        if (readback != token
                or not hmac.compare_digest(hashlib.sha256(readback).hexdigest(),
                                           hashlib.sha256(token).hexdigest())):
            raise RuntimeError("new P2 launch capability failed same-descriptor readback")
        os.fsync(directory_fd)
        selected_root = Path(control_root or vm.policy.VM_CAPACITY_DOMAIN_ROOT)
        return selected_root / "p2-launch-capabilities" / "v1" / session_id / ready_name
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _rename_launch_capability_exclusive(directory_fd: int, ready_name: str,
                                        claimed_name: str, *,
                                        before_rename: Callable[[], None] | None = None,
                                        after_rename: Callable[[], None] | None = None) -> None:
    renameatx_np = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
    if renameatx_np is None:
        raise RuntimeError("P2 launch capability claim requires renameatx_np exclusive rename")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                             ctypes.c_char_p, ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    ready_bytes = os.fsencode(ready_name)
    claimed_bytes = os.fsencode(claimed_name)
    if before_rename is not None:
        before_rename()
    result = renameatx_np(directory_fd, ready_bytes, directory_fd,
                          claimed_bytes, P2_LAUNCH_CAPABILITY_RENAME_EXCL)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), claimed_name)
    if after_rename is not None:
        after_rename()


def _inspect_launch_capability(session_id: str, admission_sha256: str,
                               commitment_sha256: str, *,
                               control_root: Path | None = None,
                               expected_uid: int | None = None) -> dict[str, Any]:
    if not isinstance(commitment_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", commitment_sha256):
        raise RuntimeError("P2 launch capability commitment is malformed")
    ready_name, claimed_name = _launch_capability_names(session_id, admission_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    try:
        token, info = _read_launch_capability(
            directory_fd, ready_name, claimed_name, commitment_sha256,
            expected_uid=expected_uid)
        return {
            "status": "READY",
            "session_id": session_id,
            "admission_sha256": admission_sha256,
            "commitment_sha256": hashlib.sha256(token).hexdigest(),
            "ready_path": str(Path(control_root or vm.policy.VM_CAPACITY_DOMAIN_ROOT)
                               / "p2-launch-capabilities" / "v1" / session_id / ready_name),
            "file_identity": {"device": info.st_dev, "inode": info.st_ino,
                              "owner_uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
                              "link_count": info.st_nlink, "length": info.st_size},
        }
    finally:
        os.close(directory_fd)


def _claim_launch_capability(session_id: str, admission_sha256: str,
                             commitment_sha256: str, *,
                             control_root: Path | None = None,
                             expected_uid: int | None = None,
                             before_claim: Callable[[], None] | None = None,
                             after_claim: Callable[[], None] | None = None,
                             claim_state: dict[str, bool] | None = None
                             ) -> tuple[bytes, dict[str, Any]]:
    if not isinstance(commitment_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", commitment_sha256):
        raise RuntimeError("P2 launch capability commitment is malformed")
    ready_name, claimed_name = _launch_capability_names(session_id, admission_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    descriptor: int | None = None
    try:
        token, validated_info = _read_launch_capability(
            directory_fd, ready_name, claimed_name, commitment_sha256,
            expected_uid=expected_uid)
        descriptor = os.open(ready_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        pinned_info = os.fstat(descriptor)
        if not _same_capability_file(validated_info, pinned_info):
            raise RuntimeError("P2 launch capability changed before its atomic claim")
        def mark_atomic_claim_owned() -> None:
            # Ownership belongs only to the process whose exclusive rename
            # returned success. A later read of shared CLAIMED state cannot
            # distinguish this process from a concurrent winner.
            if claim_state is not None:
                claim_state["value"] = True

        _rename_launch_capability_exclusive(
            directory_fd, ready_name, claimed_name,
            before_rename=before_claim, after_rename=mark_atomic_claim_owned)
        os.fsync(directory_fd)
        if after_claim is not None:
            after_claim()
        claimed_fd = os.open(claimed_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            claimed_info = _capability_entry_info(
                directory_fd, claimed_name,
                expected_uid=os.geteuid() if expected_uid is None else expected_uid)
            held_info = os.fstat(descriptor)
            readback = os.read(claimed_fd, 33)
            post_info = os.fstat(claimed_fd)
            if (not _same_capability_file(pinned_info, held_info)
                    or not _same_capability_file(held_info, claimed_info)
                    or not _same_capability_file(claimed_info, post_info)
                    or readback != token or len(readback) != 32
                    or not hmac.compare_digest(hashlib.sha256(readback).hexdigest(), commitment_sha256)):
                raise RuntimeError("claimed P2 launch capability failed identity or commitment revalidation")
        finally:
            os.close(claimed_fd)
        return token, {"status": "CLAIMED", "session_id": session_id,
                       "admission_sha256": admission_sha256,
                       "commitment_sha256": commitment_sha256,
                       "claimed_path": str(Path(control_root or vm.policy.VM_CAPACITY_DOMAIN_ROOT)
                                           / "p2-launch-capabilities" / "v1" / session_id
                                           / claimed_name),
                       "file_identity": {"device": pinned_info.st_dev,
                                         "inode": pinned_info.st_ino,
                                         "owner_uid": pinned_info.st_uid,
                                         "mode": stat.S_IMODE(pinned_info.st_mode),
                                         "link_count": pinned_info.st_nlink,
                                         "length": pinned_info.st_size}}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _supersede_launch_capability(session_id: str, admission_sha256: str,
                                commitment_sha256: str, *,
                                control_root: Path | None = None,
                                expected_uid: int | None = None) -> dict[str, Any]:
    """Atomically retire one exact READY capability without exposing its bytes."""
    if not isinstance(commitment_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", commitment_sha256):
        raise RuntimeError("P2 launch capability commitment is malformed")
    ready_name, claimed_name = _launch_capability_names(session_id, admission_sha256)
    superseded_name = _superseded_capability_name(admission_sha256)
    uid = os.geteuid() if expected_uid is None else expected_uid
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    descriptor: int | None = None
    try:
        try:
            token, ready_info = _read_launch_capability(
                directory_fd, ready_name, claimed_name, commitment_sha256,
                expected_uid=uid)
        except (OSError, RuntimeError) as error:
            # Keep error text independent of the private token and fail before
            # any state transition when the source is no longer READY.
            raise RuntimeError("exact P2 READY capability could not be safely superseded") from error
        descriptor = os.open(ready_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        pinned_info = os.fstat(descriptor)
        if not _same_capability_file(ready_info, pinned_info):
            raise RuntimeError("P2 READY capability identity changed before supersession")
        _rename_launch_capability_exclusive(directory_fd, ready_name, superseded_name)
        os.fsync(directory_fd)
        resulting_info = _capability_entry_info(
            directory_fd, superseded_name, expected_uid=uid)
        held_info = os.fstat(descriptor)
        try:
            os.stat(ready_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 READY capability remained after supersession")
        try:
            os.stat(claimed_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 capability was claimed concurrently with supersession")
        resulting_descriptor = os.open(
            superseded_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            readback = os.read(resulting_descriptor, 33)
            post_info = os.fstat(resulting_descriptor)
        finally:
            os.close(resulting_descriptor)
        if (not _same_capability_file(pinned_info, held_info)
                or not _same_capability_file(held_info, resulting_info)
                or not _same_capability_file(resulting_info, post_info)
                or len(readback) != 32
                or not hmac.compare_digest(hashlib.sha256(readback).hexdigest(), commitment_sha256)
                or not hmac.compare_digest(hashlib.sha256(token).hexdigest(), commitment_sha256)):
            raise RuntimeError("superseded P2 capability failed identity or commitment readback")
        return {
            "status": "SUPERSEDED",
            "session_id": session_id,
            "admission_sha256": admission_sha256,
            "commitment_sha256": commitment_sha256,
            "superseded_path": str(Path(control_root or vm.policy.VM_CAPACITY_DOMAIN_ROOT)
                                   / "p2-launch-capabilities" / "v1" / session_id
                                   / superseded_name),
            "file_identity": {"device": pinned_info.st_dev,
                              "inode": pinned_info.st_ino,
                              "owner_uid": pinned_info.st_uid,
                              "mode": stat.S_IMODE(pinned_info.st_mode),
                              "link_count": pinned_info.st_nlink,
                              "length": pinned_info.st_size},
        }
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _supersede_r3_execution_packet(*, r4_run_id: str,
                                  manifest_path: Path,
                                  review_path: Path,
                                  implementation_review_path: Path,
                                  signing_key: bytes,
                                  control_root: Path | None = None) -> tuple[Path, dict[str, Any], str]:
    """Retire only the exact never-claimed R3 READY packet after R4 review passes."""
    if not isinstance(r4_run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", r4_run_id):
        raise RuntimeError("R4 run ID is malformed for R3 execution-packet supersession")
    implementation_path = Path(implementation_review_path).resolve(strict=True)
    implementation_text = implementation_path.read_text(encoding="utf-8")
    accepted_implementation_decisions = (
        "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED",
        "PASS WITH NON-BLOCKING CAVEATS",
    )
    if not any(decision in implementation_text for decision in accepted_implementation_decisions):
        raise RuntimeError("R3 supersession requires an accepted independent R4 implementation review")
    manifest, provisioner_review = _verify_reviewed_provenance(
        Path(manifest_path).resolve(strict=True), Path(review_path).resolve(strict=True), signing_key)
    if provisioner_review.get("decision") not in {
            "PASS — P2 PROVISIONER APPROVED", "PASS - P2 PROVISIONER APPROVED"}:
        raise RuntimeError("R3 supersession requires a fresh passing independent provisioner provenance review")
    manifest_sha = vm._file_sha256(Path(manifest_path))
    review_sha = vm._file_sha256(Path(review_path))
    manifest_identity = manifest.get("unchanged_helper_relationship", {})
    if (manifest_identity.get("status") != "PASS"
            or manifest_identity.get("rebuild_performed_for_r4") is not False
            or manifest.get("source_manifest", {}).get("orchestration_source", {}).get("sha256")
                != hashlib.sha256((HERE / "p2_base_orchestrator.py").read_bytes()).hexdigest()):
        raise RuntimeError("R4 provenance does not bind the reviewed adapter and unchanged helper")

    admission, admission_sha = read_signed_with_key(
        P2_R3_ADMISSION_PATH, "vm_admission_hmac_sha256", signing_key)
    if (admission_sha != P2_R3_ADMISSION_SHA256
            or vm._file_sha256(P2_R3_ADMISSION_PATH) != P2_R3_ADMISSION_SHA256
            or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("session_id") != P2_R2_SESSION_ID
            or admission.get("run_id") != P2_R3_RUN_ID
            or admission.get("helper_control_token_sha256")
                != P2_R3_CAPABILITY_COMMITMENT_SHA256
            or admission.get("provisioner_source_manifest_sha256") != P2_R3_SOURCE_MANIFEST_SHA256
            or admission.get("provisioner_review_sha256") != P2_R3_PROVISIONER_REVIEW_SHA256
            or admission.get("p2_identity_plan_sha256") != P2_R3_IDENTITY_PLAN_SHA256
            or admission.get("p2_preflight_sha256") != P2_R3_PREFLIGHT_SHA256):
        raise RuntimeError("historical R3 admission does not match its accepted signed PASS identity")
    r3_manifest_path = _admission_artifact_path(
        admission.get("provisioner_source_manifest_path"), "R3 source manifest")
    r3_manifest, r3_manifest_sha = read_signed_with_key(
        r3_manifest_path, "vm_p2_provenance_hmac_sha256", signing_key)
    if (r3_manifest_sha != P2_R3_SOURCE_MANIFEST_SHA256
            or r3_manifest.get("source_manifest", {}).get("source", {}).get("sha256")
                != admission.get("provisioner_source_sha256")):
        raise RuntimeError("historical R3 source/provisioner provenance changed")
    plan_path = _admission_artifact_path(admission.get("p2_identity_plan_path"), "R3 identity plan")
    if (vm._file_sha256(plan_path) != P2_R3_IDENTITY_PLAN_SHA256
            or plan_path != P2_ACCEPTED_IDENTITY_PLAN_PATH.resolve(strict=True)):
        raise RuntimeError("R3 packet does not preserve the accepted P2 identity plan")
    preflight_path = _admission_artifact_path(admission.get("p2_preflight_path"), "R3 preflight")
    if vm._file_sha256(preflight_path) != P2_R3_PREFLIGHT_SHA256:
        raise RuntimeError("R3 preflight digest changed before packet supersession")

    active, active_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    if (active_sha != P2_R3_ACTIVE_MARKER_SHA256
            or active.get("session_id") != admission.get("session_id")
            or active.get("phase_id") != "P2_AUTHORIZED"
            or active.get("state") != "ACTIVE"):
        raise RuntimeError("R3 packet supersession requires the exact unchanged active marker")
    restore_sha, restore_size, restore_identity = sha256_file(RESTORE_PATH)
    if (restore_sha != RESTORE_SHA256 or restore_size != RESTORE_SIZE
            or restore_sha != admission.get("restore_sha256")
            or restore_identity != admission.get("restore_file_identity")):
        raise RuntimeError("R3 packet supersession requires the unchanged complete restore")
    tree_blockers, found_restore = _restore_tree_blockers()
    base_dir = Path(admission["system_raw_path"]).parent
    process_scan = _p2_process_scan_evidence()
    if (tree_blockers or found_restore != [str(RESTORE_PATH)]
            or os.path.lexists(base_dir) or process_scan.get("blocking_processes")):
        raise RuntimeError("R3 packet supersession requires zero guest assets and no VM/provisioner processes")
    admission_age = time.time() - float(admission.get("created_at_unix", 0))
    if admission_age <= P2_EXECUTION_START_MAX_AGE_SECONDS:
        raise RuntimeError("R3 static admission is not the observed expired execution packet")

    capability_fd = _open_launch_capability_session_directory(
        admission["session_id"], control_root=control_root, create=False)
    try:
        state_before = _capability_file_state(
            capability_fd, admission["session_id"], admission_sha)
    finally:
        os.close(capability_fd)
    if state_before.get("status") == "READY":
        ready = _validate_launch_capability_contract(
            admission, admission_sha, control_root=control_root)
        if (ready.get("status") != "READY"
                or ready.get("commitment_sha256") != P2_R3_CAPABILITY_COMMITMENT_SHA256):
            raise RuntimeError("R3 capability is not the exact unclaimed READY commitment")
        capability_state = _supersede_launch_capability(
            admission["session_id"], admission_sha,
            P2_R3_CAPABILITY_COMMITMENT_SHA256, control_root=control_root)
    elif state_before.get("status") == "SUPERSEDED":
        # Permit receipt recovery after a crash between the atomic private-state
        # transition and the signed additive evidence write.
        state_fd = _open_launch_capability_session_directory(
            admission["session_id"], control_root=control_root, create=False)
        try:
            terminal = _capability_file_state(state_fd, admission["session_id"], admission_sha)
            descriptor = os.open(terminal["path_name"], os.O_RDONLY
                                 | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=state_fd)
            try:
                terminal_commitment = hashlib.sha256(os.read(descriptor, 33)).hexdigest()
            finally:
                os.close(descriptor)
        finally:
            os.close(state_fd)
        if terminal_commitment != P2_R3_CAPABILITY_COMMITMENT_SHA256:
            raise RuntimeError("existing R3 SUPERSEDED capability failed its exact commitment check")
        capability_state = {"status": "SUPERSEDED", "session_id": admission["session_id"],
                            "admission_sha256": admission_sha,
                            "commitment_sha256": terminal_commitment,
                            "superseded_path": str(
                                P2_LAUNCH_CAPABILITY_ROOT / "v1" / admission["session_id"]
                                / _superseded_capability_name(admission_sha)),
                            "file_identity": state_before["file_identity"]}
    else:
        raise RuntimeError("R3 execution packet is already claimed or has ambiguous private state")

    active_after, active_after_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    restore_after = os.stat(RESTORE_PATH, follow_symlinks=False)
    if (active_after != active or active_after_sha != active_sha
            or file_identity(restore_after) != restore_identity
            or os.path.lexists(base_dir)):
        raise RuntimeError("R3 packet retirement changed the active marker, restore, or guest inventory")
    report_path = REPO / f"R6-VM-P2-R3-EXECUTION-RESUMABILITY-REPAIR-FINAL-{P2_R3_RUN_ID}.md"
    report_sha = vm._file_sha256(report_path)
    supersession_path = unique_path(
        "R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION", r4_run_id)
    receipt = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_R3_EXECUTION_PACKET_SUPERSESSION",
        "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
        "status": "PASS",
        "classification": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
        "authority": "NONE",
        "authority_scope": "NONE",
        "execution_authority": "NONE",
        "r4_run_id": r4_run_id,
        "created_at_unix": time.time(),
        "r4_implementation_review": {
            "path": str(implementation_path.relative_to(REPO)),
            "sha256": vm._file_sha256(implementation_path),
            "accepted_decision": next(decision for decision in accepted_implementation_decisions
                                      if decision in implementation_text),
        },
        "r4_provenance": {
            "manifest_path": str(Path(manifest_path).resolve(strict=True).relative_to(REPO)),
            "manifest_sha256": manifest_sha,
            "review_path": str(Path(review_path).resolve(strict=True).relative_to(REPO)),
            "review_sha256": review_sha,
            "unchanged_helper_relationship_status": "PASS",
        },
        "r3_admission_path": str(P2_R3_ADMISSION_PATH.relative_to(REPO)),
        "r3_admission_sha256": admission_sha,
        "r3_admission_signature_valid": True,
        "r3_run_id": admission["run_id"],
        "r3_session_id": admission["session_id"],
        "r3_source_manifest_sha256": P2_R3_SOURCE_MANIFEST_SHA256,
        "r3_provisioner_review_sha256": P2_R3_PROVISIONER_REVIEW_SHA256,
        "r3_identity_plan_sha256": P2_R3_IDENTITY_PLAN_SHA256,
        "r3_preflight_sha256": P2_R3_PREFLIGHT_SHA256,
        "r3_capability_commitment_sha256": P2_R3_CAPABILITY_COMMITMENT_SHA256,
        "capability_state_before": "READY",
        "claimed_state_absent_before": True,
        "capability_state_after": "SUPERSEDED",
        "capability_commitment_verified_after": True,
        "private_capability_state": {
            "status": "SUPERSEDED",
            "path_suffix": ".superseded",
            "commitment_sha256": P2_R3_CAPABILITY_COMMITMENT_SHA256,
            "file_identity": capability_state["file_identity"],
        },
        "stale_admission_blocker": {
            "admission_age_seconds_at_retirement": admission_age,
            "monitor_maximum_age_seconds": P2_EXECUTION_START_MAX_AGE_SECONDS,
            "static_admission_remains_signed_pass": True,
        },
        "observed_source_ordering_defect": True,
        "source_ordering_defect": "R3 consumed the private READY capability before the controller rejected the stale static admission at monitor initialization.",
        "r3_final_report": {"path": str(report_path.relative_to(REPO)), "sha256": report_sha},
        "active_marker_sha256_before": active_sha,
        "active_marker_sha256_after": active_after_sha,
        "restore": {"path": str(RESTORE_PATH), "sha256_before": restore_sha,
                    "sha256_after": restore_sha, "logical_bytes": restore_size,
                    "file_identity": restore_identity},
        "guest_assets_present_before": False,
        "guest_assets_present_after": False,
        "blocking_processes_before": process_scan.get("blocking_processes"),
        "guest_mutation_occurred": False,
        "cross_session_marker_transition": False,
        "raw_capability_bytes_included": False,
    }
    signed = signed_write(supersession_path, receipt,
                          P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, signing_key)
    readback, receipt_sha = read_signed_with_key(
        supersession_path, P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, signing_key)
    if readback != signed:
        raise RuntimeError("R3 supersession receipt did not survive exact signed readback")
    _validate_supersession_receipt_for_admission(
        supersession_path, P2_R3_ADMISSION_PATH, admission, admission_sha,
        signing_key=signing_key, control_root=control_root)
    return supersession_path, readback, receipt_sha


def _execution_start_state_names(lease_sha256: str) -> tuple[str, str]:
    if not isinstance(lease_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", lease_sha256):
        raise RuntimeError("P2 execution-start lease digest is malformed")
    stem = f"execution-start-{lease_sha256}"
    return f"{stem}.ready", f"{stem}.claimed"


def _write_execution_start_lease_ready(session_id: str, lease_sha256: str, *,
                                       control_root: Path | None = None) -> dict[str, Any]:
    """Durably register an issued lease for one later atomic startup use."""
    ready_name, claimed_name = _execution_start_state_names(lease_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    descriptor: int | None = None
    try:
        try:
            os.stat(claimed_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 execution-start lease already has a terminal state")
        descriptor = os.open(ready_name, os.O_RDWR | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        payload = lease_sha256.encode("ascii")
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short write while persisting P2 execution-start lease state")
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        entry = _capability_entry_info(directory_fd, ready_name,
                                       expected_uid=os.geteuid(), expected_size=64)
        if not _same_capability_file(info, entry):
            raise RuntimeError("P2 execution-start lease state identity changed during persistence")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, 65) != payload:
            raise RuntimeError("P2 execution-start lease state failed same-descriptor readback")
        os.fsync(directory_fd)
        return {"status": "READY", "lease_sha256": lease_sha256,
                "file_identity": {"device": info.st_dev, "inode": info.st_ino,
                                  "owner_uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
                                  "link_count": info.st_nlink, "length": info.st_size}}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _verify_execution_start_lease_ready(session_id: str, lease_sha256: str, *,
                                        control_root: Path | None = None) -> dict[str, Any]:
    """Verify a lease is still durably available and has not already been consumed."""
    ready_name, claimed_name = _execution_start_state_names(lease_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    descriptor: int | None = None
    try:
        try:
            os.stat(claimed_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 execution-start lease was already consumed")
        descriptor = os.open(ready_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        info = _capability_entry_info(directory_fd, ready_name,
                                      expected_uid=os.geteuid(), expected_size=64)
        held = os.fstat(descriptor)
        content = os.read(descriptor, 65)
        post = os.fstat(descriptor)
        if (not _same_capability_file(info, held)
                or not _same_capability_file(held, post)
                or content != lease_sha256.encode("ascii")):
            raise RuntimeError("P2 execution-start lease READY state is malformed or changed")
        return {"status": "READY", "lease_sha256": lease_sha256,
                "file_identity": {"device": info.st_dev, "inode": info.st_ino,
                                  "owner_uid": info.st_uid, "mode": stat.S_IMODE(info.st_mode),
                                  "link_count": info.st_nlink, "length": info.st_size}}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _claim_execution_start_lease(session_id: str, lease_sha256: str, *,
                                 control_root: Path | None = None) -> dict[str, Any]:
    """Consume a signed start lease once, without involving the guest capability."""
    ready_name, claimed_name = _execution_start_state_names(lease_sha256)
    directory_fd = _open_launch_capability_session_directory(
        session_id, control_root=control_root, create=False)
    descriptor: int | None = None
    try:
        try:
            os.stat(claimed_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 execution-start lease already has a terminal state")
        descriptor = os.open(ready_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        before = _capability_entry_info(directory_fd, ready_name,
                                        expected_uid=os.geteuid(), expected_size=64)
        pinned = os.fstat(descriptor)
        content = os.read(descriptor, 65)
        if (not _same_capability_file(before, pinned)
                or content != lease_sha256.encode("ascii")):
            raise RuntimeError("P2 execution-start lease state is malformed or changed")
        _rename_launch_capability_exclusive(directory_fd, ready_name, claimed_name)
        os.fsync(directory_fd)
        claimed = _capability_entry_info(directory_fd, claimed_name,
                                         expected_uid=os.geteuid(), expected_size=64)
        held = os.fstat(descriptor)
        readback_fd = os.open(claimed_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                              | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            readback = os.read(readback_fd, 65)
            post = os.fstat(readback_fd)
        finally:
            os.close(readback_fd)
        if (not _same_capability_file(pinned, held)
                or not _same_capability_file(held, claimed)
                or not _same_capability_file(claimed, post)
                or readback != lease_sha256.encode("ascii")):
            raise RuntimeError("P2 execution-start lease claim failed identity or digest readback")
        try:
            os.stat(ready_name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("P2 execution-start lease remains READY after its claim")
        return {"status": "CLAIMED", "lease_sha256": lease_sha256,
                "file_identity": {"device": pinned.st_dev, "inode": pinned.st_ino,
                                  "owner_uid": pinned.st_uid, "mode": stat.S_IMODE(pinned.st_mode),
                                  "link_count": pinned.st_nlink, "length": pinned.st_size}}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _execution_start_age_valid(issued_at_unix: Any, *, now_unix: float | None = None,
                               issued_at_monotonic: float | None = None,
                               now_monotonic: float | None = None,
                               maximum_age_seconds: float = P2_EXECUTION_START_MAX_AGE_SECONDS
                               ) -> tuple[bool, float | None]:
    if (not isinstance(issued_at_unix, (int, float)) or isinstance(issued_at_unix, bool)
            or not math.isfinite(float(issued_at_unix))
            or not isinstance(maximum_age_seconds, (int, float))
            or isinstance(maximum_age_seconds, bool)
            or not math.isfinite(float(maximum_age_seconds))
            or maximum_age_seconds != P2_EXECUTION_START_MAX_AGE_SECONDS):
        return False, None
    wall_age = (time.time() if now_unix is None else float(now_unix)) - float(issued_at_unix)
    if wall_age < 0 or wall_age > maximum_age_seconds:
        return False, wall_age
    if issued_at_monotonic is not None:
        if (not isinstance(issued_at_monotonic, (int, float))
                or isinstance(issued_at_monotonic, bool)
                or not math.isfinite(float(issued_at_monotonic))):
            return False, wall_age
        monotonic_age = ((time.monotonic() if now_monotonic is None else float(now_monotonic))
                         - float(issued_at_monotonic))
        if monotonic_age < 0 or monotonic_age > maximum_age_seconds:
            return False, monotonic_age
        return True, monotonic_age
    return True, wall_age


def _prepare_admission_launch_capability(record: Mapping[str, Any], signing_key: bytes,
                                         token: bytes, *,
                                         control_root: Path | None = None) -> tuple[str, Path]:
    session_id = record.get("session_id")
    capability = record.get("launch_capability")
    commitment = record.get("helper_control_token_sha256")
    if (not isinstance(capability, Mapping)
            or capability.get("format_version") != P2_LAUNCH_CAPABILITY_FORMAT_VERSION
            or capability.get("state") != "READY"
            or capability.get("private_store") != P2_LAUNCH_CAPABILITY_STORE_ID
            or capability.get("path_derivation") != "<session_id>/<admission_sha256>.ready"
            or capability.get("session_id") != session_id
            or capability.get("commitment_sha256") != commitment
            or not isinstance(commitment, str)
            or hashlib.sha256(token).hexdigest() != commitment):
        raise RuntimeError("new P2 admission has no exact READY launch capability contract")
    _sealed, raw = _signed_record_bytes(record, "vm_admission_hmac_sha256", signing_key)
    admission_sha256 = hashlib.sha256(raw).hexdigest()
    capability_path = _write_launch_capability_ready(
        str(session_id), admission_sha256, token, control_root=control_root)
    return admission_sha256, capability_path


def _validate_launch_capability_contract(admission: Mapping[str, Any],
                                         admission_sha256: str, *,
                                         control_root: Path | None = None) -> dict[str, Any]:
    capability = admission.get("launch_capability")
    session_id = admission.get("session_id")
    commitment = admission.get("helper_control_token_sha256")
    if (not isinstance(capability, Mapping)
            or capability.get("format_version") != P2_LAUNCH_CAPABILITY_FORMAT_VERSION
            or capability.get("state") != "READY"
            or capability.get("private_store") != P2_LAUNCH_CAPABILITY_STORE_ID
            or capability.get("path_derivation") != "<session_id>/<admission_sha256>.ready"
            or capability.get("session_id") != session_id
            or capability.get("commitment_sha256") != commitment
            or not isinstance(session_id, str)
            or not isinstance(commitment, str)):
        raise RuntimeError("signed P2 admission has no supported private launch capability contract")
    return _inspect_launch_capability(session_id, admission_sha256, commitment,
                                      control_root=control_root)


def _persist_large_signed_record(path: Path, record: Mapping[str, Any], signature_field: str
                                 ) -> tuple[dict[str, Any], str, str, int, int]:
    if not vm.verify_signed_record(record, signature_field):
        raise RuntimeError(f"signed evidence did not verify: {signature_field}")
    raw = json_bytes(record)
    if len(raw) > 256 * MIB:
        raise RuntimeError("P2 monitor receipt exceeded the bounded evidence size")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    if len(compressed) > P2_METADATA_MAX:
        raise RuntimeError("compressed P2 monitor evidence exceeded the 32 MiB metadata envelope")
    parent_fd = vm.policy._open_directory_nofollow(path.parent, create=True, strict_directory_sync=True)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=parent_fd)
        view = memoryview(compressed)
        while view:
            amount = os.write(descriptor, view)
            if amount <= 0:
                raise OSError("short write while persisting compressed P2 monitor receipt")
            view = view[amount:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("compressed P2 monitor receipt has an unsafe file identity")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
    packed = vm.policy._read_file_nofollow(path, maximum_bytes=P2_METADATA_MAX)
    readback = gzip.decompress(packed)
    parsed = json.loads(readback)
    if (readback != raw or parsed != record
            or not vm.verify_signed_record(parsed, signature_field)):
        raise RuntimeError(f"compressed evidence failed exact readback/HMAC verification: {signature_field}")
    return parsed, vm._file_sha256(path), hashlib.sha256(raw).hexdigest(), len(raw), len(compressed)


def _persist_large_signed_monitor(path: Path, record: Mapping[str, Any]) -> tuple[dict[str, Any], str, str, int, int]:
    return _persist_large_signed_record(path, record, "vm_phase_monitor_hmac_sha256")


def read_signed(path: Path, field: str) -> tuple[dict[str, Any], str]:
    record = vm._read_signed_record(path, field)
    return record, vm._file_sha256(path)


def read_signed_with_key(path: Path, field: str, signing_key: bytes) -> tuple[dict[str, Any], str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"signed P2 input is not a single-link regular file: {path}")
        raw = bytearray()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            raw.extend(block)
            if len(raw) > 64 * MIB:
                raise RuntimeError(f"signed P2 input exceeded its bounded read size: {path}")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (file_identity(before) != file_identity(after)
                or file_identity(after) != file_identity(current)
                or len(raw) != after.st_size):
            raise RuntimeError(f"signed P2 input identity changed while reading: {path}")
        record = json.loads(raw)
        actual = record.get(field) if isinstance(record, Mapping) else None
        payload = dict(record) if isinstance(record, Mapping) else None
        if payload is None or not isinstance(actual, str):
            raise RuntimeError(f"signed P2 input omitted its record or {field}: {path}")
        payload.pop(field, None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")
        expected = hmac.new(signing_key, canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError(f"signed P2 input failed {field} verification: {path}")
        return dict(record), hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


class P2ContainmentJournal:
    """Append one HMAC-authenticated gzip member per emergency monitor event."""

    def __init__(self, path: Path, signing_key: bytes):
        self.path = path
        self.signing_key = signing_key
        self.sequence = 0
        self.lock = threading.Lock()
        self.pending_lines: list[bytes] = []
        self.last_flush_monotonic = time.monotonic()
        self.maximum_allocated_bytes = 16 * MIB
        self.parent_fd = vm.policy._open_directory_nofollow(
            path.parent, create=True, strict_directory_sync=True)
        self.fd = -1
        try:
            self.fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                              | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=self.parent_fd)
            info = os.fstat(self.fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 0):
                raise RuntimeError("P2 containment journal has an unsafe initial identity")
            self.initial_identity = file_identity(info)
            os.fsync(self.fd)
            _fsync_directory(path.parent)
        except BaseException:
            if self.fd >= 0:
                os.close(self.fd)
            os.close(self.parent_fd)
            raise
        self.error: str | None = None

    def append(self, event: str, fields: Mapping[str, Any], *, durable: bool = False) -> dict[str, Any]:
        with self.lock:
            if self.error is not None:
                raise RuntimeError(f"P2 containment journal is already unhealthy: {self.error}")
            payload = {
                "schema_version": 1,
                "record_type": "R6_VM_P2_CONTAINMENT_EVENT",
                "sequence": self.sequence + 1,
                "captured_at_unix": time.time(),
                "captured_monotonic_seconds": time.monotonic(),
                "event": event,
                **dict(fields),
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode("utf-8")
            payload["vm_p2_containment_event_hmac_sha256"] = hmac.new(
                self.signing_key, canonical, hashlib.sha256).hexdigest()
            line = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8") + b"\n"
            self.pending_lines.append(line)
            self.sequence += 1
            if not durable and time.monotonic() - self.last_flush_monotonic < 5.0:
                return payload
            self._flush_locked()
            return payload

    def _flush_locked(self) -> None:
        if not self.pending_lines:
            return
        info = os.fstat(self.fd)
        if int(getattr(info, "st_blocks", 0)) * 512 >= self.maximum_allocated_bytes - 64 * 1024:
            self.error = "P2 containment journal reached its bounded emergency evidence allocation"
            self.pending_lines.clear()
            raise RuntimeError(self.error)
        member = gzip.compress(b"".join(self.pending_lines), compresslevel=6, mtime=0)
        try:
            view = memoryview(member)
            while view:
                amount = os.write(self.fd, view)
                if amount <= 0:
                    raise OSError("short append while persisting emergency monitor sample")
                view = view[amount:]
            os.fsync(self.fd)
            info = os.fstat(self.fd)
            path_info = os.stat(self.path, follow_symlinks=False)
            allocated = int(getattr(info, "st_blocks", 0)) * 512
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or file_identity(info) != file_identity(path_info)
                    or allocated > self.maximum_allocated_bytes):
                raise RuntimeError("P2 containment journal identity or allocation changed while appending")
        except BaseException as error:
            self.error = str(error)
            raise
        self.pending_lines.clear()
        self.last_flush_monotonic = time.monotonic()

    def flush(self) -> None:
        with self.lock:
            if self.error is not None:
                raise RuntimeError(f"P2 containment journal is unhealthy: {self.error}")
            self._flush_locked()

    def verify_and_close(self) -> dict[str, Any]:
        with self.lock:
            try:
                if self.error is None:
                    self._flush_locked()
                os.fsync(self.fd)
                written_identity = file_identity(os.fstat(self.fd))
            finally:
                os.close(self.fd)
                os.close(self.parent_fd)
                self.fd = -1
                self.parent_fd = -1
        descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or file_identity(before) != written_identity):
                raise RuntimeError("P2 containment journal changed before final verification")
            digest = hashlib.sha256()
            os.lseek(descriptor, 0, os.SEEK_SET)
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
            os.lseek(descriptor, 0, os.SEEK_SET)
            count = 0
            first_sequence = None
            last_sequence = None
            with os.fdopen(os.dup(descriptor), "rb", closefd=True) as stream:
                with gzip.GzipFile(fileobj=stream, mode="rb") as packed:
                    while True:
                        line = packed.readline(256 * 1024)
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            raise RuntimeError("P2 containment journal contains a truncated record")
                        record = json.loads(line)
                        signature = record.pop("vm_p2_containment_event_hmac_sha256", None)
                        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode("utf-8")
                        expected = hmac.new(self.signing_key, canonical, hashlib.sha256).hexdigest()
                        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                            raise RuntimeError("P2 containment journal event failed HMAC verification")
                        if record.get("sequence") != count + 1:
                            raise RuntimeError("P2 containment journal sequence is not contiguous")
                        count += 1
                        first_sequence = record["sequence"] if first_sequence is None else first_sequence
                        last_sequence = record["sequence"]
            after = os.fstat(descriptor)
            path_after = os.stat(self.path, follow_symlinks=False)
            if (file_identity(after) != written_identity
                    or file_identity(path_after) != written_identity
                    or count > self.sequence
                    or (self.error is None and count != self.sequence)):
                raise RuntimeError("P2 containment journal changed during final verification")
            return {
                "path": str(self.path.relative_to(REPO)),
                "sha256": digest.hexdigest(),
                "file_identity": written_identity,
                "verified_event_count": count,
                "expected_event_count": self.sequence,
                "event_sequence_complete": count == self.sequence,
                "first_sequence": first_sequence,
                "last_sequence": last_sequence,
                "all_event_hmacs_verified": True,
                "journal_error": self.error,
            }
        finally:
            os.close(descriptor)


def _artifact_file(path_text: str, sha: str) -> Path:
    path = Path(path_text)
    if not path.is_absolute():
        path = REPO / path
    path = path.resolve(strict=True)
    if vm._file_sha256(path) != sha:
        raise RuntimeError(f"P1 artifact hash differs from its accepted identity: {path}")
    return path


def _validate_accepted_p1(*, allow_historical_r2_boundary: bool = False) -> dict[str, Any]:
    if vm._file_sha256(P1_FINAL_PATH) != P1_FINAL_SHA256:
        raise RuntimeError("accepted P1 final verification receipt SHA-256 changed")
    if vm._file_sha256(P1_ADMISSION_PATH) != P1_ADMISSION_SHA256:
        raise RuntimeError("accepted P1 admission SHA-256 changed")
    if vm._file_sha256(P1_RENEWAL_PATH) != P1_RENEWAL_SHA256:
        raise RuntimeError("accepted P1 freshness renewal SHA-256 changed")
    if vm._file_sha256(P1_BODY_START_PATH) != P1_BODY_START_SHA256:
        raise RuntimeError("accepted P1 first-body evidence SHA-256 changed")
    if vm._file_sha256(UPSTREAM_METADATA_PATH) != UPSTREAM_METADATA_FILE_SHA256:
        raise RuntimeError("accepted P1 upstream compatibility-metadata file SHA-256 changed")
    creation, creation_sha = read_signed(vm.VM_P0_CREATION_RECEIPT_PATH, "vm_creation_hmac_sha256")
    p0, p0_sha = read_signed(vm.VM_P0_ADMISSION_PATH, "vm_admission_hmac_sha256")
    p1, p1_sha = read_signed(P1_ADMISSION_PATH, "vm_admission_hmac_sha256")
    final, final_sha = read_signed(P1_FINAL_PATH, "restore_verification_hmac_sha256")
    upstream_metadata = vm._read_signed_record(UPSTREAM_METADATA_PATH, "restore_metadata_hmac_sha256")
    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (creation_sha != vm.VM_P0_CREATION_RECEIPT_SHA256
            or p0_sha != vm.VM_P0_ADMISSION_SHA256 or p1_sha != P1_ADMISSION_SHA256
            or final_sha != P1_FINAL_SHA256):
        raise RuntimeError("signed P0/P1 artifact digests do not match accepted ancestry")
    if (creation.get("status") != "PASS" or p0.get("decision") != "PASS"
            or p1.get("decision") != "PASS" or p1.get("authorized_phase") != "P1"
            or final.get("status") != "PASS" or final.get("phase_id") != "P1"
            or final.get("local_sha256_state") != "VERIFIED"
            or final.get("local_sha256") != RESTORE_SHA256
            or final.get("actual_length_bytes") != RESTORE_SIZE
            or final.get("expected_length_bytes") != RESTORE_SIZE):
        raise RuntimeError("accepted P0/P1 signed receipts do not contain PASS and exact restore identity")
    p0_vm_volume = p0.get("production_vm_volume")
    if (not isinstance(p0_vm_volume, Mapping)
            or p0_vm_volume.get("uuid") != VM_VOLUME_UUID
            or p0_vm_volume.get("container_uuid") != CONTAINER_UUID
            or p0_vm_volume.get("physical_store_uuid") != PHYSICAL_STORE_UUID
            or p0_vm_volume.get("quota_bytes") != 53_687_091_200
            or p0_vm_volume.get("reserve_bytes") != 0
            or p0_vm_volume.get("name") != "FGMetal-VM"):
        raise RuntimeError("accepted P0 admission does not bind the expected production VM volume")
    if (final.get("restore_subject_identity") != RESTORE_SUBJECT_IDENTITY
            or final.get("fresh_metadata_path") != UPSTREAM_METADATA_PATH.name
            or final.get("fresh_metadata_receipt_sha256") != UPSTREAM_METADATA_FILE_SHA256
            or final.get("fresh_metadata_observation_sha256") != UPSTREAM_METADATA_SHA256
            or upstream_metadata.get("metadata_sha256") != UPSTREAM_METADATA_SHA256
            or upstream_metadata.get("hardware_model_data_sha256") != HARDWARE_MODEL_SHA256
            or upstream_metadata.get("operating_system_version") != "27.0.1"
            or upstream_metadata.get("build_version") != "26A434"):
        raise RuntimeError("accepted P1 verification and upstream metadata do not bind the sealed restore subject and hardware model")
    session_id = creation.get("session_id")
    if (not isinstance(session_id, str)
            or p1.get("session_id") != session_id or final.get("session_id") != session_id
            or creation.get("volume_uuid") != VM_VOLUME_UUID
            or p1.get("vm_volume_uuid") != VM_VOLUME_UUID
            or final.get("volume_uuid") != VM_VOLUME_UUID):
        raise RuntimeError("accepted P0/P1 session or VM-volume ancestry disagrees")
    ancestry = p1.get("p0_ancestry")
    if (not isinstance(ancestry, Mapping)
            or ancestry.get("creation_receipt_sha256") != creation_sha
            or ancestry.get("p0_admission_sha256") != p0_sha
            or p1.get("volume_creation_receipt_sha256") != creation_sha
            or p1.get("volume_creation_receipt_path") != vm.VM_P0_CREATION_RECEIPT_PATH.name
            or final.get("p1_admission_sha256") != p1_sha
            or final.get("p0_admission_sha256") != p0_sha
            or final.get("p1_freshness_renewal_sha256") != P1_RENEWAL_SHA256
            or final.get("p1_freshness_start_evidence_sha256") != P1_BODY_START_SHA256):
        raise RuntimeError("accepted P1 final receipt does not bind the P0/P1 ancestry and closed freshness evidence")
    p1_marker_matches = (
        active.get("session_id") == session_id and active.get("volume_uuid") == VM_VOLUME_UUID
        and active.get("state") == "ACTIVE" and active.get("phase_id") == "P1_VERIFIED"
        and active.get("p1_admission_sha256") == P1_ADMISSION_SHA256)
    r2_boundary: dict[str, Any] | None = None
    if not p1_marker_matches:
        if not allow_historical_r2_boundary:
            raise RuntimeError("active VM marker is not the accepted P1_VERIFIED boundary")
        historical_admission, historical_sha = read_signed_with_key(
            P2_R2_ADMISSION_PATH, "vm_admission_hmac_sha256", vm.policy._manifest_signing_key())
        blocker, blocker_sha = read_signed_with_key(
            P2_R2_EXECUTION_BLOCKER_PATH, "vm_p2_r2_execution_blocker_hmac_sha256",
            vm.policy._manifest_signing_key())
        blocker_provenance = blocker.get("accepted_r2_provenance")
        predecessor = blocker.get("predecessor_receipt")
        asset_state = blocker.get("guest_asset_state")
        restore_state = blocker.get("restore_unchanged")
        if (blocker_sha != P2_R2_EXECUTION_BLOCKER_SHA256
                or blocker.get("authority_scope") != "NONE"
                or blocker.get("execution_authority") != "NONE"
                or not isinstance(predecessor, Mapping)
                or predecessor.get("path") != str(P2_R2_EXECUTION_BLOCKER_PREDECESSOR_PATH.relative_to(REPO))
                or predecessor.get("sha256") != P2_R2_EXECUTION_BLOCKER_PREDECESSOR_SHA256
                or not isinstance(blocker_provenance, Mapping)
                or blocker_provenance.get("source_manifest_sha256") != historical_admission.get("provisioner_source_manifest_sha256")
                or blocker_provenance.get("provisioner_review_sha256") != historical_admission.get("provisioner_review_sha256")
                or blocker_provenance.get("python_orchestrator_sha256") != historical_admission.get("provisioner_orchestration_source_sha256")
                or blocker_provenance.get("swift_source_sha256") != historical_admission.get("provisioner_source_sha256")
                or blocker_provenance.get("helper_binary_sha256") != historical_admission.get("provisioner_binary_sha256")
                or blocker_provenance.get("helper_code_directory_hash") != historical_admission.get("provisioner_code_directory_hash")
                or not isinstance(asset_state, Mapping)
                or asset_state.get("guest_assets_created") is not False
                or asset_state.get("restore_tree_blockers") != []
                or asset_state.get("related_process_blockers") != []
                or not isinstance(restore_state, Mapping)
                or restore_state.get("matches_accepted_p1_identity") is not True
                or restore_state.get("payload_retained") is not True
                or restore_state.get("sha256") != RESTORE_SHA256
                or restore_state.get("logical_bytes") != RESTORE_SIZE):
            raise RuntimeError("signed R2 blocker reconciliation is not exact, scoped to NONE, and tied to accepted R2 provenance")
        for path_key, digest_key, signature_field, expected_digest in (
                ("source_manifest_path", "source_manifest_sha256", "vm_p2_provenance_hmac_sha256",
                 historical_admission.get("provisioner_source_manifest_sha256")),
                ("provisioner_review_path", "provisioner_review_sha256", "vm_p2_provisioner_review_hmac_sha256",
                 historical_admission.get("provisioner_review_sha256")),
                ("build_receipt_path", "build_receipt_sha256", "vm_p2_provisioner_build_hmac_sha256", None)):
            artifact_path = _admission_artifact_path(blocker_provenance.get(path_key), path_key)
            artifact, artifact_sha = read_signed_with_key(artifact_path, signature_field,
                                                           vm.policy._manifest_signing_key())
            if artifact_sha != blocker_provenance.get(digest_key) or (expected_digest is not None and artifact_sha != expected_digest):
                raise RuntimeError("historical R2 blocker provenance artifact hash or HMAC does not verify")
            if path_key == "source_manifest_path":
                r2_manifest = artifact
            elif path_key == "provisioner_review_path":
                r2_review = artifact
            else:
                r2_build = artifact
        if (r2_review.get("reviewed_manifest_sha256") != historical_admission.get("provisioner_source_manifest_sha256")
                or r2_review.get("reviewed_binary_sha256") != historical_admission.get("provisioner_binary_sha256")
                or r2_build.get("manifest_sha256") != historical_admission.get("provisioner_source_manifest_sha256")
                or r2_manifest.get("source_manifest", {}).get("source", {}).get("sha256") != historical_admission.get("provisioner_source_sha256")
                or r2_manifest.get("source_manifest", {}).get("orchestration_source", {}).get("sha256") != historical_admission.get("provisioner_orchestration_source_sha256")
                or r2_manifest.get("source_manifest", {}).get("binary", {}).get("sha256") != historical_admission.get("provisioner_binary_sha256")):
            raise RuntimeError("historical R2 provenance relationship differs from the PASS admission")
        active_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        if (historical_sha != P2_R2_ADMISSION_SHA256
                or historical_admission.get("decision") != "PASS"
                or historical_admission.get("session_id") != session_id
                or historical_admission.get("authorized_phase") != "P2"
                or historical_admission.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
                or historical_admission.get("p1_admission_sha256") != P1_ADMISSION_SHA256
                or historical_admission.get("restore_sha256") != RESTORE_SHA256
                or historical_admission.get("p2_identity_plan_path") != P2_ACCEPTED_IDENTITY_PLAN_PATH.name
                or historical_admission.get("p2_identity_plan_sha256") != P2_ACCEPTED_IDENTITY_PLAN_SHA256
                or blocker.get("classification") != "ADMISSION_VALID_BUT_EXECUTION_PACKET_NON_CONSUMABLE"
                or blocker.get("historical_admission_sha256") != P2_R2_ADMISSION_SHA256
                or blocker.get("historical_session_id") != session_id
                or blocker.get("historical_admission_cryptographically_failed") is not False
                or active.get("session_id") != session_id
                or active.get("volume_uuid") != VM_VOLUME_UUID
                or active.get("state") != "ACTIVE"
                or active.get("phase_id") != "P2_AUTHORIZED"):
            raise RuntimeError("current marker is not the exact accepted R2 P2 boundary with its signed non-consumable blocker receipt")
        r2_boundary = {
            "historical_admission": historical_admission,
            "historical_admission_sha256": historical_sha,
            "execution_blocker_receipt": blocker,
            "execution_blocker_receipt_path": str(P2_R2_EXECUTION_BLOCKER_PATH.relative_to(REPO)),
            "execution_blocker_receipt_sha256": blocker_sha,
            "active_marker_sha256": active_sha,
        }
    current_g0c = vm.verify_fresh_g0c_review()
    if final.get("g0c_identity") != current_g0c:
        raise RuntimeError("accepted P1 final receipt does not bind the current G0C identity")
    return {
        "session_id": session_id,
        "creation": creation,
        "creation_sha256": creation_sha,
        "p0_admission": p0,
        "p0_admission_sha256": p0_sha,
        "p1_admission": p1,
        "p1_admission_sha256": p1_sha,
        "p1_final": final,
        "p1_final_sha256": final_sha,
        "upstream_metadata": upstream_metadata,
        "upstream_metadata_path": UPSTREAM_METADATA_PATH,
        "upstream_metadata_file_sha256": UPSTREAM_METADATA_FILE_SHA256,
        "upstream_metadata_sha256": UPSTREAM_METADATA_SHA256,
        "active_marker": active,
        "historical_r2_boundary": r2_boundary,
        "g0c_identity": current_g0c,
    }


def _validate_preserved_identity_plan(plan_path: Path,
                                      manifest: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    selected = Path(plan_path).resolve(strict=True)
    if (selected != P2_ACCEPTED_IDENTITY_PLAN_PATH.resolve(strict=True)
            or vm._file_sha256(selected) != P2_ACCEPTED_IDENTITY_PLAN_SHA256):
        raise RuntimeError("P2 R3 must reuse the exact accepted identity-plan receipt")
    plan, plan_sha = read_signed(selected, "vm_p2_identity_plan_hmac_sha256")
    if (plan.get("status") != "PASS" or plan.get("plan_only") is not True
            or plan.get("guest_assets_created") is not False
            or plan.get("restore_path") != str(RESTORE_PATH)
            or plan.get("restore_sha256") != RESTORE_SHA256
            or plan.get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or plan.get("minimum_supported_cpu_count") != 2
            or plan.get("minimum_supported_memory_size_bytes") != 4 * GIB
            or plan.get("provisioner_binary_sha256") != manifest["source_manifest"]["binary"]["sha256"]
            or plan.get("provisioner_source_sha256") != manifest["source_manifest"]["source"]["sha256"]):
        raise RuntimeError("preserved P2 identity plan is stale or differs from the exact current helper identity")
    machine_data = plan.get("machine_identifier_data_base64")
    hardware_data = plan.get("hardware_model_data_base64")
    if (not isinstance(machine_data, str) or not isinstance(hardware_data, str)
            or hashlib.sha256(base64.b64decode(machine_data, validate=True)).hexdigest()
                != plan.get("machine_identifier_sha256")
            or hashlib.sha256(base64.b64decode(hardware_data, validate=True)).hexdigest()
                != P2_LOCAL_HARDWARE_MODEL_SHA256):
        raise RuntimeError("preserved identity plan opaque platform identities do not match their signed hashes")
    historical_review_path = Path(plan.get("review_path", ""))
    if not historical_review_path.is_absolute():
        historical_review_path = REPO / historical_review_path
    historical_review_path = historical_review_path.resolve(strict=True)
    historical_review, historical_review_sha = read_signed(
        historical_review_path, "vm_p2_provisioner_review_hmac_sha256")
    if (historical_review_sha != plan.get("review_sha256")
            or historical_review.get("decision") not in {
                "PASS — P2 PROVISIONER APPROVED", "PASS - P2 PROVISIONER APPROVED"}
            or historical_review.get("reviewed_source_sha256") != plan.get("provisioner_source_sha256")
            or historical_review.get("reviewed_binary_sha256") != plan.get("provisioner_binary_sha256")):
        raise RuntimeError("preserved identity plan no longer binds its accepted historical review")
    historical_manifest_path = Path(historical_review.get("manifest_path", ""))
    if not historical_manifest_path.is_absolute():
        historical_manifest_path = REPO / historical_manifest_path
    historical_manifest_path = historical_manifest_path.resolve(strict=True)
    historical_manifest, historical_manifest_sha = read_signed(
        historical_manifest_path, "vm_p2_provenance_hmac_sha256")
    _check_review(historical_review_path, historical_manifest, historical_manifest_path)
    if (historical_manifest_sha != historical_review.get("reviewed_manifest_sha256")
            or historical_manifest["source_manifest"]["source"]["sha256"]
                != plan.get("provisioner_source_sha256")
            or historical_manifest["source_manifest"]["binary"]["sha256"]
                != plan.get("provisioner_binary_sha256")):
        raise RuntimeError("preserved identity plan historical source and helper provenance changed")
    return plan, plan_sha


def _admission_artifact_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"signed P2 admission omitted its {label} path")
    selected = Path(value)
    if not selected.is_absolute():
        selected = REPO / selected
    resolved = selected.resolve(strict=True)
    if REPO.resolve() not in resolved.parents:
        raise RuntimeError(f"signed P2 admission {label} path is outside the repository")
    return resolved


def _validate_r3_active_marker_transition(admission: Mapping[str, Any],
                                          admission_path: Path,
                                          admission_sha256: str,
                                          preflight: Mapping[str, Any],
                                          active_marker: Mapping[str, Any],
                                          active_marker_sha256: str,
                                          *, signing_key: bytes) -> tuple[dict[str, Any], str]:
    """Validate the additive same-session marker update receipt for an exact R3 admission."""
    run_id = admission.get("run_id")
    session_id = admission.get("session_id")
    ancestry = admission.get("historical_r2_ancestry")
    lineage = _validate_accepted_p1(allow_historical_r2_boundary=True)
    boundary = lineage.get("historical_r2_boundary")
    expected_ancestry = {
        "admission_path": str(P2_R2_ADMISSION_PATH.relative_to(REPO)),
        "admission_sha256": P2_R2_ADMISSION_SHA256,
        "run_id": P2_R2_ADMISSION_PATH.stem.removeprefix("R6-VM-P2-ADMISSION-"),
        "session_id": P2_R2_SESSION_ID,
        "execution_blocker_receipt_path": str(P2_R2_EXECUTION_BLOCKER_PATH.relative_to(REPO)),
        "execution_blocker_receipt_sha256": P2_R2_EXECUTION_BLOCKER_SHA256,
    }
    if (not isinstance(run_id, str) or run_id == expected_ancestry["run_id"]
            or session_id != P2_R2_SESSION_ID or ancestry != expected_ancestry
            or boundary is None
            or admission.get("active_marker_before_sha256") != preflight.get("active_marker_sha256")):
        raise RuntimeError("R3 admission does not distinguish its new run/admission from the accepted R2 lineage")
    transition_path = _admission_artifact_path(
        admission.get("active_marker_transition_receipt_path"), "active-marker transition receipt")
    expected_transition_path = unique_path("R6-VM-P2-ACTIVE-MARKER-TRANSITION", run_id).resolve()
    if transition_path != expected_transition_path:
        raise RuntimeError("R3 admission does not bind its canonical run-specific marker-transition receipt")
    transition, transition_sha = read_signed_with_key(
        transition_path, "vm_p2_active_marker_transition_hmac_sha256", signing_key)
    prior_marker = transition.get("prior_active_marker")
    resulting_marker = transition.get("resulting_active_marker")
    r2_boundary = transition.get("historical_r2_ancestry")
    if (transition.get("record_type") != "R6_VM_P2_ACTIVE_MARKER_TRANSITION_RECONCILIATION"
            or transition.get("status") != "PASS"
            or transition.get("authority_scope") != "NONE"
            or transition.get("execution_authority") != "NONE"
            or transition.get("cross_session_transition_protocol_added") is not False
            or transition.get("transition_protocol") != "no-op: retain the exact authenticated active marker; the new admission is separately bound to its digest"
            or transition.get("run_id") != run_id
            or transition.get("new_admission_path") != str(admission_path.relative_to(REPO))
            or transition.get("new_admission_sha256") != admission_sha256
            or transition.get("new_admission_session_id") != session_id
            or transition.get("session_id_role") != "AUTHENTICATED_ACTIVE_MARKER_LINEAGE_ONLY"
            or r2_boundary != expected_ancestry
            or transition.get("active_marker_before_sha256") != admission.get("active_marker_before_sha256")
            or transition.get("active_marker_before_sha256") != preflight.get("active_marker_sha256")
            or transition.get("active_marker_before_sha256") != transition.get("active_marker_after_sha256")
            or transition.get("active_marker_after_sha256") != active_marker_sha256
            or not isinstance(prior_marker, Mapping)
            or not isinstance(resulting_marker, Mapping)
            or prior_marker != resulting_marker
            or prior_marker.get("session_id") != session_id
            or prior_marker.get("volume_uuid") != VM_VOLUME_UUID
            or prior_marker.get("state") != "ACTIVE"
            or prior_marker.get("phase_id") != "P2_AUTHORIZED"
            or prior_marker.get("p1_admission_sha256") != P1_ADMISSION_SHA256
            or resulting_marker.get("session_id") != session_id
            or resulting_marker.get("volume_uuid") != VM_VOLUME_UUID
            or resulting_marker.get("state") != "ACTIVE"
            or resulting_marker.get("phase_id") != "P2_AUTHORIZED"
            or resulting_marker.get("p1_admission_sha256") != P1_ADMISSION_SHA256
            or active_marker.get("session_id") != session_id):
        raise RuntimeError("signed R3 same-session marker-transition receipt does not bind the exact admission and active marker")
    return transition, transition_sha


def _validate_execution_entry(admission_path: Path, *, signing_key: bytes | None = None,
                              control_root: Path | None = None) -> dict[str, Any]:
    """Validate an existing admission without claiming or exposing its launch token."""
    key = signing_key if signing_key is not None else vm.policy._manifest_signing_key()
    selected_admission = Path(admission_path).resolve(strict=True)
    if REPO.resolve() not in selected_admission.parents:
        raise RuntimeError("P2 execution admission path is outside the repository")
    admission, admission_sha = read_signed_with_key(
        selected_admission, "vm_admission_hmac_sha256", key)
    run_id = admission.get("run_id")
    session_id = admission.get("session_id")
    if (admission.get("record_type") != "R6_VM_P2_PROVISIONING_ADMISSION"
            or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
            or admission.get("p2_authorized") is not True
            or admission.get("vm_start_authorized_for_installation_only") is not True
            or admission.get("runtime_feasibility_authorized") is not False
            or admission.get("serial_runtime_testing_authorized") is not False
            or admission.get("r6_execution_authorized") is not False
            or not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id)
            or not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or session_id != P2_R2_SESSION_ID
            or run_id == P2_R2_ADMISSION_PATH.stem.removeprefix("R6-VM-P2-ADMISSION-")):
        raise RuntimeError("signed P2 admission does not grant the exact bounded P2 provisioning authority")
    _assert_admission_not_superseded(
        selected_admission, admission, admission_sha,
        signing_key=key, control_root=control_root)
    freshness_protocol = admission.get("execution_freshness_protocol")
    if (not isinstance(freshness_protocol, Mapping)
            or freshness_protocol.get("version") != "P2_R4_EXECUTION_START_LEASE_V1"
            or freshness_protocol.get("static_admission_age_limited") is not False
            or freshness_protocol.get("age_subject") != "execution_start_lease"
            or freshness_protocol.get("maximum_start_age_seconds")
                != P2_EXECUTION_START_MAX_AGE_SECONDS
            or freshness_protocol.get("claim_boundary")
                != "after_live_monitor_initial_gate_before_helper_authorization"):
        raise RuntimeError("signed P2 admission does not bind the reviewed R4 execution-start freshness protocol")
    _assert_single_r3_pass_admission(
        session_id, current_admission_path=selected_admission,
        current_admission_sha256=admission_sha,
        signing_key=key, control_root=control_root)

    manifest_path = _admission_artifact_path(admission.get("provisioner_source_manifest_path"),
                                              "source manifest")
    review_path = _admission_artifact_path(admission.get("provisioner_review_path"), "review")
    manifest, review = _verify_reviewed_provenance(manifest_path, review_path, key)
    manifest_sha = vm._file_sha256(manifest_path)
    review_sha = vm._file_sha256(review_path)
    source = manifest["source_manifest"]
    if (manifest_sha != admission.get("provisioner_source_manifest_sha256")
            or review_sha != admission.get("provisioner_review_sha256")
            or source["source"]["sha256"] != admission.get("provisioner_source_sha256")
            or source["orchestration_source"]["sha256"]
                != admission.get("provisioner_orchestration_source_sha256")
            or source["binary"]["sha256"] != admission.get("provisioner_binary_sha256")
            or source["binary"]["code_directory_hash"] != admission.get("provisioner_code_directory_hash")
            or review.get("decision") != admission.get("provisioner_review_decision")):
        raise RuntimeError("P2 execution admission does not bind the exact current source, helper, and review")

    plan_path = _admission_artifact_path(admission.get("p2_identity_plan_path"), "identity plan")
    plan, plan_sha = _validate_preserved_identity_plan(plan_path, manifest)
    if (plan_path != P2_ACCEPTED_IDENTITY_PLAN_PATH.resolve(strict=True)
            or plan_sha != P2_ACCEPTED_IDENTITY_PLAN_SHA256
            or admission.get("p2_identity_plan_sha256") != plan_sha
            or plan.get("machine_identifier_sha256") != admission.get("machine_identifier_sha256")
            or plan.get("hardware_model_sha256") != admission.get("hardware_model_sha256")):
        raise RuntimeError("P2 execution admission does not bind the exact preserved identity plan")

    preflight_path = _admission_artifact_path(admission.get("p2_preflight_path"), "preflight")
    if vm._file_sha256(preflight_path) != admission.get("p2_preflight_sha256"):
        raise RuntimeError("P2 execution admission preflight digest changed")
    preflight, preflight_sha = read_signed_with_key(preflight_path, "vm_p2_preflight_hmac_sha256", key)
    supersession_binding = admission.get("r3_execution_packet_supersession")
    preflight_supersession_binding = preflight.get("r3_execution_packet_supersession")
    if (not isinstance(supersession_binding, Mapping)
            or not isinstance(preflight_supersession_binding, Mapping)
            or dict(supersession_binding) != dict(preflight_supersession_binding)):
        raise RuntimeError("P2 admission does not bind its fresh preflight R3 supersession receipt")
    supersession_path = _admission_artifact_path(
        supersession_binding.get("path"), "R3 execution-packet supersession receipt")
    _supersession, supersession_sha = _validate_current_r3_supersession(
        run_id, supersession_path, signing_key=key, control_root=control_root,
        manifest_path=manifest_path, review_path=review_path)
    receipt_provenance = _supersession.get("r4_provenance", {})
    if (supersession_sha != supersession_binding.get("sha256")
            or receipt_provenance.get("manifest_sha256")
                != admission.get("provisioner_source_manifest_sha256")
            or receipt_provenance.get("review_sha256")
                != admission.get("provisioner_review_sha256")):
        raise RuntimeError("P2 admission R3 supersession receipt digest changed")
    if (preflight_sha != admission.get("p2_preflight_sha256")
            or preflight.get("status") != "PASS"
            or preflight.get("authority") != "READ_ONLY_P2_PREFLIGHT_ONLY"
            or preflight.get("run_id") != run_id
            or preflight.get("identity_plan_sha256") != plan_sha
            or preflight.get("provisioner_manifest_sha256") != manifest_sha
            or preflight.get("provisioner_review_sha256") != review_sha
            or preflight.get("historical_r2_admission_sha256") != P2_R2_ADMISSION_SHA256
            or preflight.get("historical_r2_execution_blocker_receipt_sha256")
                != P2_R2_EXECUTION_BLOCKER_SHA256
            or preflight.get("r3_execution_packet_supersession")
                != dict(supersession_binding)):
        raise RuntimeError("P2 execution admission does not bind its fresh read-only R3 preflight")

    active, active_sha = read_signed_with_key(vm.VM_ACTIVE_MARKER_PATH,
                                               "vm_session_hmac_sha256", key)
    if (active.get("session_id") != session_id or active.get("volume_uuid") != VM_VOLUME_UUID
            or active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_AUTHORIZED"
            or active.get("p1_admission_sha256") != P1_ADMISSION_SHA256):
        raise RuntimeError("P2 execution requires the exact active P2-authorized marker")
    transition, transition_sha = _validate_r3_active_marker_transition(
        admission, selected_admission, admission_sha, preflight, active, active_sha,
        signing_key=key)
    capability_info = _validate_launch_capability_contract(admission, admission_sha,
                                                              control_root=control_root)
    return {
        "admission": admission,
        "admission_path": selected_admission,
        "admission_sha256": admission_sha,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha,
        "review_path": review_path,
        "review_sha256": review_sha,
        "active_marker_sha256": active_sha,
        "active_marker_transition_receipt_sha256": transition_sha,
        "active_marker_transition_receipt": transition,
        "launch_capability": capability_info,
    }


def _fresh_p2_execution_start_snapshot(evidence: Mapping[str, Any], *,
                                       signing_key: bytes,
                                       control_root: Path | None = None
                                       ) -> dict[str, Any]:
    admission = evidence["admission"]
    admission_path = Path(evidence["admission_path"])
    admission_sha = evidence["admission_sha256"]
    if not isinstance(admission, Mapping):
        raise RuntimeError("execution-start snapshot lacks a validated static admission")
    manifest_path = Path(evidence["manifest_path"])
    review_path = Path(evidence["review_path"])
    manifest, review = _verify_reviewed_provenance(manifest_path, review_path, signing_key)
    manifest_sha = vm._file_sha256(manifest_path)
    review_sha = vm._file_sha256(review_path)
    if (manifest_sha != admission.get("provisioner_source_manifest_sha256")
            or review_sha != admission.get("provisioner_review_sha256")
            or review.get("decision") != admission.get("provisioner_review_decision")):
        raise RuntimeError("execution-start snapshot source or independent provenance review changed")
    plan_path = _admission_artifact_path(admission.get("p2_identity_plan_path"), "identity plan")
    plan, plan_sha = _validate_preserved_identity_plan(plan_path, manifest)
    if (plan_sha != P2_ACCEPTED_IDENTITY_PLAN_SHA256
            or plan_sha != admission.get("p2_identity_plan_sha256")):
        raise RuntimeError("execution-start snapshot does not bind the preserved accepted identity plan")
    supersession_binding = admission.get("r3_execution_packet_supersession")
    if not isinstance(supersession_binding, Mapping):
        raise RuntimeError("execution-start snapshot lacks the signed R3 packet supersession binding")
    supersession_path = _admission_artifact_path(
        supersession_binding.get("path"), "R3 execution-packet supersession receipt")
    _supersession, supersession_sha = _validate_current_r3_supersession(
        admission["run_id"], supersession_path, signing_key=signing_key,
        control_root=control_root, manifest_path=manifest_path, review_path=review_path)
    if supersession_sha != supersession_binding.get("sha256"):
        raise RuntimeError("R3 supersession receipt changed after static admission validation")

    active, active_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    g0c_identity = vm.verify_fresh_g0c_review()
    if (active_sha != evidence.get("active_marker_sha256")
            or active.get("session_id") != admission.get("session_id")
            or active.get("volume_uuid") != VM_VOLUME_UUID
            or active.get("state") != "ACTIVE"
            or active.get("phase_id") != "P2_AUTHORIZED"
            or g0c_identity != admission.get("g0c_identity")):
        raise RuntimeError("execution-start snapshot active marker differs from the exact admitted session")
    capability = _validate_launch_capability_contract(
        admission, admission_sha, control_root=control_root)
    if (capability.get("status") != "READY"
            or capability.get("commitment_sha256") != admission.get("helper_control_token_sha256")):
        raise RuntimeError("execution-start snapshot requires the exact unclaimed READY capability")

    restore_sha, restore_size, restore_identity = sha256_file(RESTORE_PATH)
    if (restore_sha != admission.get("restore_sha256")
            or restore_sha != RESTORE_SHA256
            or restore_size != RESTORE_SIZE
            or restore_identity != admission.get("restore_file_identity")):
        raise RuntimeError("execution-start snapshot restore bytes or file identity differ from the reviewed admission")
    tree_blockers, found_restore = _restore_tree_blockers()
    if tree_blockers or found_restore != [str(RESTORE_PATH)]:
        raise RuntimeError("execution-start snapshot VM restore inventory is not exact: "
                           + "; ".join(tree_blockers))
    base_dir = Path(admission["system_raw_path"]).parent
    if os.path.lexists(base_dir):
        raise RuntimeError("execution-start snapshot found an existing guest base directory")
    process_scan = _p2_process_scan_evidence()
    if process_scan.get("blocking_processes"):
        raise RuntimeError("execution-start snapshot found provisioner, installer, or VM processes")

    metadata_paths = _provisioner_metadata_paths(
        manifest, manifest_path, review_path, admission_path, plan_path,
        _admission_artifact_path(admission.get("p2_preflight_path"), "preflight"),
        supersession_path,
        Path(capability.get("ready_path", P2_LAUNCH_CAPABILITY_ROOT / "v1"
                            / str(admission["session_id"])
                            / f"{admission_sha}.ready")))
    metadata_inventory = _assert_p2_metadata_budget(metadata_paths)
    metadata_remaining = metadata_inventory.get("remaining_bytes")
    if (not isinstance(metadata_remaining, int) or isinstance(metadata_remaining, bool)
            or metadata_remaining <= P2_METADATA_FINALIZATION_RESERVE):
        raise RuntimeError("execution-start P1/P2 metadata inventory cannot preserve finalization reserve")

    with vm.policy._acquire_storage_allocation_lock():
        observation = vm.capture_host_observation()
        storage_blockers = list(vm._session_container_blockers(
            observation, volume_uuid=VM_VOLUME_UUID))
        contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
        volume = vm._volume_row(observation, VM_VOLUME_UUID)
        safety = vm._volume_row(observation, SAFETY_VOLUME_UUID)
        mount = vm._volume_mount_info(observation, VM_VOLUME_UUID)
        current_used = volume.get("CapacityInUse")
        quota = contract.get("quota_bytes")
        shared_free = vm.conservative_session_unreserved_free_bytes(
            observation, volume_uuid=VM_VOLUME_UUID, capacity_contract=contract)
        reserve_state = observation.get("safety_volume_info", {})
        if (contract.get("safety_volume_uuid") != SAFETY_VOLUME_UUID
                or contract.get("safety_reserve_bytes") != 15 * GIB
                or safety.get("CapacityInUse") != 16_106_127_360
                or reserve_state.get("mount_state") != "UNMOUNTED_VERIFIED"
                or quota != admission.get("vm_volume_quota_bytes")
                or observation.get("apfs_container", {}).get("APFSContainerUUID") != CONTAINER_UUID
                or observation.get("physical_store_info", {}).get("DiskUUID") != PHYSICAL_STORE_UUID
                or mount.get("volume_uuid") != VM_VOLUME_UUID):
            storage_blockers.append("execution-start safety, VM volume, quota, container, or store identity changed")
        if not isinstance(current_used, int) or isinstance(current_used, bool):
            storage_blockers.append("execution-start VM volume usage is unknown")
        if not isinstance(quota, int) or isinstance(quota, bool):
            storage_blockers.append("execution-start VM volume quota is unknown")
        full_remaining_growth = P2_SYSTEM_MAX + P2_AUX_MAX + metadata_remaining
        p2_growth = full_remaining_growth - P2_METADATA_FINALIZATION_RESERVE
        uncertainty = max(
            vm.VM_PHASE_MIN_UNCERTAINTY_BYTES,
            (full_remaining_growth * vm.VM_PHASE_UNCERTAINTY_BASIS_POINTS + 9999) // 10000)
        total_demand = full_remaining_growth + uncertainty
        projected_shared_free = shared_free - total_demand
        projected_quota_headroom = (quota - current_used - total_demand
                                    if isinstance(quota, int) and isinstance(current_used, int)
                                    else None)
        if p2_growth > admission.get("p2_growth_bytes", -1):
            storage_blockers.append("current P2 growth demand exceeds the statically reviewed admission bound")
        if projected_shared_free <= vm.VM_OPERATIONAL_WARNING_FREE:
            storage_blockers.append("projected shared free reaches the unchanged R4 8 GiB early-stop threshold")
        if projected_shared_free <= vm.VM_OPERATIONAL_UNRESERVED_FREE_MIN:
            storage_blockers.append("projected shared free reaches the unchanged R4 5 GiB hard floor")
        if projected_quota_headroom is None or projected_quota_headroom < 0:
            storage_blockers.append("current P2 demand exceeds fresh VM quota headroom")
        phase_evaluation = vm.calculate_vm_phase_admission(
            mode=vm.VM_MODE, container_uuid=CONTAINER_UUID, volume_uuid=VM_VOLUME_UUID,
            free_bytes=shared_free, quota_bytes=quota,
            quota_used_bytes=current_used, growth_bytes=p2_growth,
            uncertainty_bytes=uncertainty, concurrent_growth_bytes=None,
            expected_quota_bytes=quota, logical_capacity_bytes=SYSTEM_LOGICAL_CAPACITY,
            maximum_logical_capacity_bytes=SYSTEM_LOGICAL_CAPACITY)
        storage_blockers.extend(phase_evaluation.get("blockers", []))
        if storage_blockers:
            raise RuntimeError("execution-start R4 storage gate failed: "
                               + "; ".join(sorted(set(storage_blockers))))
        storage_measurement = {
            "captured_at_unix": observation.get("captured_at_unix"),
            "container_free_bytes": observation.get("crosschecks", {}).get("apfs_container_free_bytes"),
            "data_container_free_bytes": observation.get("crosschecks", {}).get("data_info_container_free_bytes"),
            "shared_container_free_bytes": vm.conservative_container_free_bytes(observation),
            "unreserved_shared_free_bytes": shared_free,
            "vm_volume_usage_bytes": current_used,
            "vm_volume_quota_bytes": quota,
            "vm_quota_headroom_bytes": quota - current_used,
            "p1_p2_metadata_allocated_bytes": metadata_inventory["total_p2_metadata_allocated_bytes"],
            "p1_p2_metadata_remaining_bytes": metadata_remaining,
            "safety_volume_uuid": contract.get("safety_volume_uuid"),
            "safety_volume_capacity_in_use_bytes": safety.get("CapacityInUse"),
            "safety_volume_reserve_bytes": contract.get("safety_reserve_bytes"),
            "safety_mount_state": reserve_state.get("mount_state"),
            "vm_volume_uuid": mount.get("volume_uuid"),
            "vm_mount_identity": mount.get("mount_identity"),
            "vm_device_identifier": mount.get("device_identifier"),
            "vm_mount": mount,
            "apfs_container_uuid": observation.get("apfs_container", {}).get("APFSContainerUUID"),
            "physical_store_uuid": observation.get("physical_store_info", {}).get("DiskUUID"),
        }
        demand = {
            "system_ceiling_bytes": P2_SYSTEM_MAX,
            "auxiliary_ceiling_bytes": P2_AUX_MAX,
            "metadata_remaining_bytes": metadata_remaining,
            "metadata_finalization_reserve_bytes": P2_METADATA_FINALIZATION_RESERVE,
            "p2_growth_bytes": p2_growth,
            "uncertainty_bytes": uncertainty,
            "total_demand_bytes": total_demand,
        }
        gate = {
            "decision": "PASS",
            "early_stop_threshold_bytes": vm.VM_OPERATIONAL_WARNING_FREE,
            "hard_operational_floor_bytes": vm.VM_OPERATIONAL_UNRESERVED_FREE_MIN,
            "projected_shared_free_after_demand_bytes": projected_shared_free,
            "projected_quota_headroom_after_demand_bytes": projected_quota_headroom,
            "phase_evaluation": phase_evaluation,
        }

    active_after, active_after_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    restore_after = os.stat(RESTORE_PATH, follow_symlinks=False)
    if (active_after != active or active_after_sha != active_sha
            or file_identity(restore_after) != restore_identity):
        raise RuntimeError("execution-start marker or restore identity changed during fresh storage capture")
    return {
        "active_marker": active,
        "active_marker_sha256": active_sha,
        "g0c_identity": g0c_identity,
        "capability": capability,
        "restore": {"path": str(RESTORE_PATH), "sha256": restore_sha,
                    "logical_bytes": restore_size, "file_identity": restore_identity},
        "storage_observation": observation,
        "storage_measurement": storage_measurement,
        "storage_identities": {
            "safety_volume_uuid": SAFETY_VOLUME_UUID,
            "safety_volume_state": reserve_state.get("mount_state"),
            "safety_volume_capacity_in_use_bytes": safety.get("CapacityInUse"),
            "safety_reserve_bytes": contract.get("safety_reserve_bytes"),
            "vm_volume_uuid": VM_VOLUME_UUID,
            "vm_volume_quota_bytes": quota,
            "vm_volume_usage_bytes": current_used,
            "vm_mount_identity": mount.get("mount_identity"),
            "vm_device_identifier": mount.get("device_identifier"),
            "apfs_container_uuid": observation.get("apfs_container", {}).get("APFSContainerUUID"),
            "physical_store_uuid": observation.get("physical_store_info", {}).get("DiskUUID"),
        },
        "metadata_inventory": metadata_inventory,
        "p2_demand": demand,
        "r4_gate": gate,
        "guest_assets": {"base_directory": str(base_dir), "present": False,
                          "assets": []},
        "process_scan": process_scan,
        "identity_plan_sha256": plan_sha,
        "provenance": {"manifest_path": str(manifest_path.relative_to(REPO)),
                       "manifest_sha256": manifest_sha,
                       "review_path": str(review_path.relative_to(REPO)),
                       "review_sha256": review_sha,
                       "source_sha256": manifest["source_manifest"]["source"]["sha256"],
                       "adapter_sha256": manifest["source_manifest"]["orchestration_source"]["sha256"]},
    }


def _create_execution_start_lease(evidence: Mapping[str, Any], *,
                                   signing_key: bytes,
                                   control_root: Path | None = None
                                   ) -> tuple[Path, dict[str, Any], str, float]:
    admission = evidence["admission"]
    admission_path = Path(evidence["admission_path"]).resolve(strict=True)
    admission_sha = evidence["admission_sha256"]
    snapshot = _fresh_p2_execution_start_snapshot(
        evidence, signing_key=signing_key, control_root=control_root)
    lease_run_id = f"{admission['run_id']}-execstart-{uuid.uuid4().hex[:12]}"
    lease_path = unique_path("R6-VM-P2-EXECUTION-START", lease_run_id)
    if lease_path.exists():
        raise FileExistsError("execution-start lease path already exists")
    issued_at_unix = time.time()
    issued_at_monotonic = time.monotonic()
    record = {
        "schema_version": P2_EXECUTION_START_LEASE_VERSION,
        "record_type": "R6_VM_P2_EXECUTION_START_LEASE",
        "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
        "lease_id": lease_run_id,
        "authority": P2_EXECUTION_START_AUTHORITY,
        "authority_scope": "EXACT_ADMISSION_STARTUP_ONLY",
        "status": "ISSUED",
        "single_use": True,
        "created_at_unix": issued_at_unix,
        "issued_at_unix": issued_at_unix,
        "maximum_age_seconds": P2_EXECUTION_START_MAX_AGE_SECONDS,
        "freshness_semantics": "issuance_through_initial_live_monitor_gate_and_atomic_capability_claim; no active-installation expiry",
        "claim_boundary": "after_live_monitor_initial_gate_before_helper_authorization",
        "static_admission_age_limited": False,
        "admission_path": str(admission_path.relative_to(REPO)),
        "admission_sha256": admission_sha,
        "admission_decision": admission.get("decision"),
        "admission_authority_scope": admission.get("authority_scope"),
        "session_id": admission.get("session_id"),
        "run_id": admission.get("run_id"),
        "provenance": snapshot["provenance"],
        "identity_plan_path": admission.get("p2_identity_plan_path"),
        "identity_plan_sha256": snapshot["identity_plan_sha256"],
        "active_marker_sha256": snapshot["active_marker_sha256"],
        "g0c_identity": snapshot["g0c_identity"],
        "capability": {"commitment_sha256": admission.get("helper_control_token_sha256"),
                       "state": "READY", "file_identity": snapshot["capability"]["file_identity"],
                       "admission_sha256": admission_sha, "session_id": admission.get("session_id")},
        "restore": snapshot["restore"],
        "storage_identities": snapshot["storage_identities"],
        "storage_observation": snapshot["storage_observation"],
        "storage_measurement": snapshot["storage_measurement"],
        "metadata_inventory": snapshot["metadata_inventory"],
        "p2_demand": snapshot["p2_demand"],
        "r4_gate": snapshot["r4_gate"],
        "guest_assets": snapshot["guest_assets"],
        "process_scan": snapshot["process_scan"],
        "guest_mutation_authorized_by_lease": False,
        "vm_start_authorized_by_lease": False,
    }
    signed = signed_write(lease_path, record, P2_EXECUTION_START_SIGNATURE_FIELD, signing_key)
    readback, lease_sha = read_signed_with_key(
        lease_path, P2_EXECUTION_START_SIGNATURE_FIELD, signing_key)
    if readback != signed or readback != {**record, P2_EXECUTION_START_SIGNATURE_FIELD:
                                         signed[P2_EXECUTION_START_SIGNATURE_FIELD]}:
        raise RuntimeError("execution-start lease failed exact signed readback")
    private_state = _write_execution_start_lease_ready(
        str(admission["session_id"]), lease_sha, control_root=control_root)
    valid, age = _execution_start_age_valid(
        issued_at_unix, issued_at_monotonic=issued_at_monotonic)
    if not valid:
        raise RuntimeError(f"execution-start lease expired during durable issuance: {age}")
    return lease_path, readback, lease_sha, issued_at_monotonic


def _validate_execution_start_lease(lease_path: Path, evidence: Mapping[str, Any], *,
                                    signing_key: bytes,
                                    lease_sha256: str | None = None,
                                    issued_at_monotonic: float | None = None,
                                    control_root: Path | None = None) -> tuple[dict[str, Any], str]:
    lease, actual_sha = read_signed_with_key(
        lease_path, P2_EXECUTION_START_SIGNATURE_FIELD, signing_key)
    admission = evidence["admission"]
    expected_path = str(Path(evidence["admission_path"]).resolve(strict=True).relative_to(REPO.resolve()))
    provenance = {
        "manifest_path": str(Path(evidence["manifest_path"]).resolve(strict=True).relative_to(REPO.resolve())),
        "manifest_sha256": evidence["manifest_sha256"],
        "review_path": str(Path(evidence["review_path"]).resolve(strict=True).relative_to(REPO.resolve())),
        "review_sha256": evidence["review_sha256"],
    }
    valid_age, age = _execution_start_age_valid(
        lease.get("issued_at_unix"), issued_at_monotonic=issued_at_monotonic)
    capability = lease.get("capability")
    restore = lease.get("restore")
    expected_restore_identity = admission.get("restore_file_identity")
    try:
        restore_info = os.stat(RESTORE_PATH, follow_symlinks=False)
        restore_identity = file_identity(restore_info)
    except OSError as error:
        raise RuntimeError("P2 execution-start restore identity is unavailable") from error
    if restore_identity != expected_restore_identity:
        raise RuntimeError("P2 execution-start restore identity changed after lease issuance")
    active, active_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    current_capability = _validate_launch_capability_contract(
        admission, evidence["admission_sha256"], control_root=control_root)
    if (lease.get("record_type") != "R6_VM_P2_EXECUTION_START_LEASE"
            or lease.get("schema_version") != P2_EXECUTION_START_LEASE_VERSION
            or lease.get("protocol_version") != "P2_R4_EXECUTION_START_LEASE_V1"
            or lease.get("authority") != P2_EXECUTION_START_AUTHORITY
            or lease.get("authority_scope") != "EXACT_ADMISSION_STARTUP_ONLY"
            or lease.get("status") != "ISSUED"
            or lease.get("single_use") is not True
            or lease.get("static_admission_age_limited") is not False
            or lease.get("maximum_age_seconds") != P2_EXECUTION_START_MAX_AGE_SECONDS
            or lease.get("claim_boundary")
                != "after_live_monitor_initial_gate_before_helper_authorization"
            or lease.get("admission_path") != expected_path
            or lease.get("admission_sha256") != evidence["admission_sha256"]
            or lease.get("session_id") != admission.get("session_id")
            or lease.get("run_id") != admission.get("run_id")
            or lease.get("provenance", {}).get("manifest_path") != provenance["manifest_path"]
            or lease.get("provenance", {}).get("manifest_sha256") != provenance["manifest_sha256"]
            or lease.get("provenance", {}).get("review_path") != provenance["review_path"]
            or lease.get("provenance", {}).get("review_sha256") != provenance["review_sha256"]
            or lease.get("provenance", {}).get("source_sha256")
                != admission.get("provisioner_source_sha256")
            or lease.get("provenance", {}).get("adapter_sha256")
                != admission.get("provisioner_orchestration_source_sha256")
            or lease.get("identity_plan_sha256") != admission.get("p2_identity_plan_sha256")
            or lease.get("active_marker_sha256") != evidence.get("active_marker_sha256")
            or active_sha != lease.get("active_marker_sha256")
            or active.get("session_id") != admission.get("session_id")
            or active.get("state") != "ACTIVE"
            or active.get("phase_id") != "P2_AUTHORIZED"
            or lease.get("g0c_identity") != admission.get("g0c_identity")
            or vm.verify_fresh_g0c_review() != lease.get("g0c_identity")
            or not isinstance(capability, Mapping)
            or capability.get("state") != "READY"
            or capability.get("file_identity") != evidence.get("launch_capability", {}).get("file_identity")
            or current_capability.get("status") != "READY"
            or current_capability.get("file_identity") != capability.get("file_identity")
            or capability.get("commitment_sha256")
                != admission.get("helper_control_token_sha256")
            or lease.get("restore", {}).get("sha256") != admission.get("restore_sha256")
            or lease.get("restore", {}).get("path") != str(RESTORE_PATH)
            or lease.get("restore", {}).get("logical_bytes") != RESTORE_SIZE
            or restore.get("file_identity") != expected_restore_identity
            or lease.get("r4_gate", {}).get("decision") != "PASS"
            or lease.get("guest_assets", {}).get("present") is not False
            or lease.get("guest_assets", {}).get("assets") != []
            or lease.get("process_scan", {}).get("blocking_processes") != []
            or not valid_age):
        raise RuntimeError(f"P2 execution-start lease is stale or does not bind the exact admission: {age}")
    base_dir = Path(admission["system_raw_path"]).parent
    if os.path.lexists(base_dir):
        raise RuntimeError("P2 execution-start guest base directory appeared before capability claim")
    process_scan = _p2_process_scan_evidence()
    if process_scan.get("blocking_processes"):
        raise RuntimeError("P2 execution-start process scan found an installer, helper, or VM process")
    storage_ids = lease.get("storage_identities")
    storage_measurement = lease.get("storage_measurement")
    vm_mount = storage_measurement.get("vm_mount") if isinstance(storage_measurement, Mapping) else None
    if (not isinstance(storage_ids, Mapping) or not isinstance(storage_measurement, Mapping)
            or storage_ids.get("safety_volume_uuid") != SAFETY_VOLUME_UUID
            or storage_ids.get("safety_volume_state") != "UNMOUNTED_VERIFIED"
            or storage_ids.get("safety_reserve_bytes") != 15 * GIB
            or storage_ids.get("vm_volume_uuid") != VM_VOLUME_UUID
            or storage_ids.get("vm_volume_quota_bytes") != storage_measurement.get("vm_volume_quota_bytes")
            or storage_ids.get("vm_volume_usage_bytes") != storage_measurement.get("vm_volume_usage_bytes")
            or storage_ids.get("vm_mount_identity") != storage_measurement.get("vm_mount_identity")
            or storage_ids.get("vm_device_identifier") != storage_measurement.get("vm_device_identifier")
            or not isinstance(vm_mount, Mapping)
            or vm_mount.get("mount_state") != "MOUNTED_VERIFIED"
            or vm_mount.get("mounted") is not True
            or vm_mount.get("volume_uuid") != VM_VOLUME_UUID
            or vm_mount.get("device_identifier") != storage_measurement.get("vm_device_identifier")
            or vm_mount.get("mount_identity") != storage_measurement.get("vm_mount_identity")
            or storage_ids.get("apfs_container_uuid") != CONTAINER_UUID
            or storage_ids.get("physical_store_uuid") != PHYSICAL_STORE_UUID
            or lease.get("storage_observation", {}).get("apfs_container", {}).get("APFSContainerUUID")
                != CONTAINER_UUID):
        raise RuntimeError("P2 execution-start lease storage identity binding is malformed")
    if lease_sha256 is not None and not hmac.compare_digest(actual_sha, lease_sha256):
        raise RuntimeError("P2 execution-start lease digest changed after issuance")
    return lease, actual_sha


def _claim_p2_after_initial_monitor_gate(*, admission_path: Path, lease_path: Path,
                                         lease_sha256: str, issued_at_monotonic: float,
                                         signing_key: bytes, control_root: Path | None,
                                         host_monitor_ready: threading.Event,
                                         monitor_thread: threading.Thread,
                                         monitor_state: Mapping[str, Any],
                                         monitor_ready_evidence: dict[str, Any],
                                         boundary_lock: Any,
                                         boundary_sample: Callable[[], Mapping[str, Any]],
                                         execution_claimed_state: dict[str, bool]
                                         ) -> dict[str, Any]:
    """Revalidate a fresh start lease and consume the exact capability after a live gate."""
    if (not host_monitor_ready.is_set()
            or monitor_state.get("error") is not None
            or not monitor_thread.is_alive()
            or monitor_ready_evidence.get("validated_sample_count", 0) < 3):
        raise RuntimeError("P2 host monitor has not passed and retained its initial gate")
    evidence = _validate_execution_entry(
        Path(admission_path), signing_key=signing_key, control_root=control_root)
    admission = evidence["admission"]
    lease, actual_lease_sha = _validate_execution_start_lease(
        Path(lease_path), evidence, signing_key=signing_key,
        lease_sha256=lease_sha256, issued_at_monotonic=issued_at_monotonic,
        control_root=control_root)
    _verify_execution_start_lease_ready(
        admission["session_id"], actual_lease_sha, control_root=control_root)
    with boundary_lock:
        last_validated = monitor_ready_evidence.get("last_validated_at_monotonic")
        if (evidence["admission_sha256"] != vm._file_sha256(Path(admission_path))
                or not isinstance(last_validated, (int, float))
                or time.monotonic() - float(last_validated)
                    > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS
                or monitor_state.get("error") is not None
                or not monitor_thread.is_alive()):
            raise RuntimeError("P2 live monitor initial PASS is not current at the claim boundary")
        lease_mount = lease.get("storage_measurement", {}).get("vm_mount")
        boundary_observation = boundary_sample()
        boundary_mount = _validate_execution_vm_mount(boundary_observation, lease_mount)
        boundary_check = vm._check_runtime_phase_sample(
            boundary_observation, volume_uuid=VM_VOLUME_UUID,
            initial_quota_used_bytes=lease["storage_measurement"]["vm_volume_usage_bytes"],
            maximum_phase_growth_bytes=lease["p2_demand"]["p2_growth_bytes"],
            concurrent_growth_bytes=None, session_id=admission["session_id"])
        boundary_sampled_at = time.monotonic()
        boundary_gap = boundary_sampled_at - float(last_validated)
        if (boundary_check.get("blockers") or boundary_check.get("admitted") is not True
                or boundary_gap > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
            raise RuntimeError("P2 current storage or VM mount failed the monitor gate at capability claim")
        monitor_ready_evidence.update({
            "validated_sample_count": int(monitor_ready_evidence.get("validated_sample_count", 0)) + 1,
            "last_validated_at_unix": time.time(),
            "last_validated_at_monotonic": boundary_sampled_at,
            "last_vm_mount": dict(boundary_mount),
        })
        last_validated = boundary_sampled_at
        if (monitor_state.get("error") is not None or not monitor_thread.is_alive()
                or not host_monitor_ready.is_set()):
            raise RuntimeError("P2 monitor did not remain active through the claim-boundary storage sample")
        valid_age, age = _execution_start_age_valid(
            lease.get("issued_at_unix"), issued_at_monotonic=issued_at_monotonic)
        if not valid_age:
            raise RuntimeError(f"P2 execution-start lease expired before capability claim: {age}")
        active, active_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        if (active_sha != evidence["active_marker_sha256"]
                or active.get("session_id") != admission.get("session_id")
                or active.get("state") != "ACTIVE"
                or active.get("phase_id") != "P2_AUTHORIZED"):
            raise RuntimeError("P2 active marker changed before capability claim")
        current_capability = _validate_launch_capability_contract(
            admission, evidence["admission_sha256"], control_root=control_root)
        expected_capability = lease.get("capability")
        if (current_capability.get("status") != "READY"
                or current_capability.get("file_identity")
                    != (expected_capability.get("file_identity")
                        if isinstance(expected_capability, Mapping) else None)):
            raise RuntimeError("P2 launch capability changed before its atomic claim")
        valid_age, age = _execution_start_age_valid(
            lease.get("issued_at_unix"), issued_at_monotonic=issued_at_monotonic)
        if not valid_age:
            raise RuntimeError(f"P2 execution-start lease expired at the atomic claim boundary: {age}")
        lease_claim = _claim_execution_start_lease(
            admission["session_id"], actual_lease_sha, control_root=control_root)
        if lease_claim.get("status") != "CLAIMED":
            raise RuntimeError("P2 execution-start lease did not complete its single-use claim")
        valid_age, age = _execution_start_age_valid(
            lease.get("issued_at_unix"), issued_at_monotonic=issued_at_monotonic)
        if not valid_age:
            raise RuntimeError(f"P2 execution-start lease expired while its single-use state was claimed: {age}")

        def validate_atomic_claim_boundary() -> None:
            # Run after capability descriptors and rename arguments are ready,
            # at the last Python boundary before the exclusive rename syscall.
            if vm._file_sha256(Path(admission_path)) != evidence["admission_sha256"]:
                raise RuntimeError("P2 static admission changed at the atomic capability claim boundary")
            if vm._file_sha256(Path(lease_path)) != actual_lease_sha:
                raise RuntimeError("P2 execution-start lease changed at the atomic capability claim boundary")
            active_at_claim, active_at_claim_sha = read_signed_with_key(
                vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
            if (active_at_claim != active or active_at_claim_sha != active_sha
                    or active_at_claim_sha != evidence["active_marker_sha256"]):
                raise RuntimeError("P2 active marker changed at the atomic capability claim boundary")
            if (monitor_state.get("error") is not None
                    or not monitor_thread.is_alive()
                    or not host_monitor_ready.is_set()):
                raise RuntimeError("P2 host monitor failed before the atomic capability claim")
            last_sample = monitor_ready_evidence.get("last_validated_at_monotonic")
            now_unix = time.time()
            now_monotonic = time.monotonic()
            if (not isinstance(last_sample, (int, float))
                    or now_monotonic - float(last_sample)
                        > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
                raise RuntimeError("P2 live monitor sample exceeded its cadence before the atomic capability claim")
            age_valid, boundary_age = _execution_start_age_valid(
                lease.get("issued_at_unix"), now_unix=now_unix,
                issued_at_monotonic=issued_at_monotonic,
                now_monotonic=now_monotonic)
            if not age_valid:
                raise RuntimeError(
                    f"P2 execution-start lease expired at the atomic capability claim boundary: {boundary_age}")
            monitor_ready_evidence["execution_start_claim_boundary"] = {
                "validated_at_unix": now_unix,
                "lease_age_seconds": boundary_age,
                "monitor_gap_seconds": now_monotonic - float(last_sample),
                "validated_sample_count": int(
                    monitor_ready_evidence.get("validated_sample_count", 0)),
            }

        def validate_atomic_claim_returned() -> None:
            last_sample = monitor_ready_evidence.get("last_validated_at_monotonic")
            now_unix = time.time()
            now_monotonic = time.monotonic()
            monitor_gap = (now_monotonic - float(last_sample)
                           if isinstance(last_sample, (int, float)) else None)
            age_valid, boundary_age = _execution_start_age_valid(
                lease.get("issued_at_unix"), now_unix=now_unix,
                issued_at_monotonic=issued_at_monotonic,
                now_monotonic=now_monotonic)
            boundary_evidence = dict(
                monitor_ready_evidence.get("execution_start_claim_boundary", {}))
            boundary_evidence.update({
                "atomic_rename_returned_at_unix": now_unix,
                "lease_age_seconds_after_atomic_rename": boundary_age,
                "monitor_gap_seconds_after_atomic_rename": monitor_gap,
            })
            monitor_ready_evidence["execution_start_claim_boundary"] = boundary_evidence
            if (monitor_state.get("error") is not None
                    or not monitor_thread.is_alive()
                    or not host_monitor_ready.is_set()):
                raise RuntimeError("P2 host monitor failed during the atomic capability claim")
            if not isinstance(monitor_gap, (int, float)) or monitor_gap > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS:
                raise RuntimeError("P2 monitor sample exceeded cadence during the atomic capability claim")
            if not age_valid:
                raise RuntimeError(
                    f"P2 execution-start lease expired during the atomic capability claim: {boundary_age}")

        control_token, launch_claim = _claim_launch_capability(
            admission["session_id"], evidence["admission_sha256"],
            admission["helper_control_token_sha256"], control_root=control_root,
            before_claim=validate_atomic_claim_boundary,
            after_claim=validate_atomic_claim_returned,
            claim_state=execution_claimed_state)
        if (launch_claim.get("status") != "CLAIMED"
                or launch_claim.get("admission_sha256") != evidence["admission_sha256"]
                or launch_claim.get("session_id") != admission["session_id"]
                or launch_claim.get("commitment_sha256")
                    != admission["helper_control_token_sha256"]):
            raise RuntimeError("P2 launch capability claim did not bind the exact admission")
    return {"evidence": evidence, "lease": lease, "lease_claim": lease_claim,
            "control_token": control_token, "launch_claim": launch_claim}


def _resume_existing_admission(args: argparse.Namespace, *,
                               signing_key: bytes | None = None,
                               control_root: Path | None = None,
                               provisioner: Any = None) -> dict[str, Any]:
    """Validate a static P2 admission and dispatch only after a fresh start lease is issued."""
    evidence = _validate_execution_entry(Path(args.admission), signing_key=signing_key,
                                         control_root=control_root)
    admission = evidence["admission"]
    if getattr(args, "validate_only", False):
        result = {"status": "PASS", "execution_entry": "READY_BEFORE_ATOMIC_CLAIM",
                  "admission_path": str(evidence["admission_path"]),
                  "admission_sha256": evidence["admission_sha256"],
                  "session_id": admission["session_id"],
                  "launch_capability_commitment_sha256": evidence["launch_capability"]["commitment_sha256"],
                  "launch_capability_state": evidence["launch_capability"]["status"],
                  "execution_start_lease": "REQUIRED_NOT_ISSUED",
                  "guest_assets_created": False}
        print(json.dumps(result, sort_keys=True))
        return result

    key = signing_key if signing_key is not None else vm.policy._manifest_signing_key()
    lease_path, lease, lease_sha, issued_at_monotonic = _create_execution_start_lease(
        evidence, signing_key=key, control_root=control_root)
    execution_args = argparse.Namespace(
        run_id=admission["run_id"], manifest=str(evidence["manifest_path"]),
        review=str(evidence["review_path"]), admission=str(evidence["admission_path"]),
        execution_start_lease=str(lease_path),
        execution_start_lease_sha256=lease_sha,
        execution_start_issued_at_unix=lease["issued_at_unix"],
        execution_start_issued_at_monotonic=issued_at_monotonic,
        active_marker_expected_sha256=evidence["active_marker_sha256"])
    dispatch = provision if provisioner is None else provisioner
    return dispatch(execution_args, execution_start_lease=lease,
                    execution_start_lease_sha256=lease_sha,
                    issued_at_monotonic=issued_at_monotonic,
                    control_root=control_root)


def _validate_p2_reconciliation() -> dict[str, Any]:
    """Validate the exact accepted P2 local-compatibility reconciliation receipts."""
    signing_key = vm.policy._manifest_signing_key()
    if not P2_RECONCILIATION_PATH.is_file():
        raise RuntimeError("P2 reconciliation addendum is missing: " + str(P2_RECONCILIATION_PATH))
    if vm._file_sha256(P2_RECONCILIATION_PATH) != P2_RECONCILIATION_SHA256:
        raise RuntimeError("P2 reconciliation addendum SHA-256 differs from canonical accepted value")
    reconciliation, reconcile_sha = read_signed_with_key(
        P2_RECONCILIATION_PATH, "vm_p2_local_compatibility_reconciliation_hmac_sha256", signing_key)

    if not P2_RECONCILIATION_REVIEW_PATH.is_file():
        raise RuntimeError("P2 reconciliation review is missing: " + str(P2_RECONCILIATION_REVIEW_PATH))
    if vm._file_sha256(P2_RECONCILIATION_REVIEW_PATH) != P2_RECONCILIATION_REVIEW_SHA256:
        raise RuntimeError("P2 reconciliation review SHA-256 differs from canonical accepted value")
    review, review_sha = read_signed_with_key(
        P2_RECONCILIATION_REVIEW_PATH, "vm_p2_local_compatibility_reconciliation_review_hmac_sha256", signing_key)

    if not P2_OBSERVATION_PATH.is_file():
        raise RuntimeError("P2 local observation receipt is missing: " + str(P2_OBSERVATION_PATH))
    if vm._file_sha256(P2_OBSERVATION_PATH) != P2_OBSERVATION_SHA256:
        raise RuntimeError("P2 local observation receipt SHA-256 differs from canonical accepted value")
    observation, observation_sha = read_signed_with_key(
        P2_OBSERVATION_PATH, "vm_p2_local_compatibility_observation_hmac_sha256", signing_key)

    if (review.get("decision") != "PASS — ADDITIVE P2 LOCAL COMPATIBILITY RECONCILIATION REVIEWED"
            or review.get("reconciliation_receipt_sha256") != P2_RECONCILIATION_SHA256
            or review.get("reconciliation_receipt_path") != P2_RECONCILIATION_PATH.name):
        raise RuntimeError("P2 reconciliation review does not approve the exact reconciliation addendum")

    p1_accepted = reconciliation.get("p1_accepted_restore", {})
    if (p1_accepted.get("final_verification_receipt_sha256") != P1_FINAL_SHA256
            or p1_accepted.get("latest_supported_hardware_model_data_sha256") != P1_UPSTREAM_HARDWARE_MODEL_SHA256
            or p1_accepted.get("restore_sha256") != RESTORE_SHA256):
        raise RuntimeError("P2 reconciliation addendum does not bind the accepted P1 restore identity")

    local_obs = reconciliation.get("local_restore_observation", {})
    if (local_obs.get("receipt_sha256") != P2_OBSERVATION_SHA256
            or local_obs.get("p2_local_hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or local_obs.get("restore_sha256") != RESTORE_SHA256):
        raise RuntimeError("P2 reconciliation addendum does not bind the exact local observation and model hash")

    if (observation.get("observed_local_hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or observation.get("expected_p1_hardware_model_sha256") != P1_UPSTREAM_HARDWARE_MODEL_SHA256
            or observation.get("restore_sha256") != RESTORE_SHA256):
        raise RuntimeError("P2 local observation does not record the reconciled hardware model identities")

    return {
        "reconciliation": reconciliation,
        "reconciliation_sha256": reconcile_sha,
        "reconciliation_path": P2_RECONCILIATION_PATH,
        "review": review,
        "review_sha256": review_sha,
        "review_path": P2_RECONCILIATION_REVIEW_PATH,
        "observation": observation,
        "observation_sha256": observation_sha,
        "observation_path": P2_OBSERVATION_PATH,
    }


def _validate_binary(binary: Path, source_path: Path, adapter_path: Path,
                     entitlements_path: Path) -> dict[str, Any]:
    def artifact(path: Path) -> dict[str, Any]:
        path = path.resolve(strict=True)
        digest, length, window = _hash_file_with_identity_window(path)
        return {"path": str(path.relative_to(REPO)),
                "canonical_path": str(path), "file_type": "regular_file",
                "sha256": digest, "bytes": length,
                "file_identity": window["identity_after_hash"],
                "mode": window["identity_after_hash"]["mode"],
                "hash_window_identity": window}

    source = artifact(source_path)
    adapter = artifact(adapter_path)
    controller_dependencies = [artifact(path) for path in P2_CONTROLLER_DEPENDENCIES]
    binary_identity = artifact(binary)
    entitlements = artifact(entitlements_path)
    verify = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(binary)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if verify.returncode != 0:
        raise RuntimeError(f"provisioner codesign verification failed: {verify.stderr.strip()}")
    details = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(binary)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    if "runtime" not in details.stderr:
        raise RuntimeError("provisioner signature is missing the hardened runtime flag")
    entitlement = subprocess.run(["/usr/bin/codesign", "-d", "--entitlements", ":-", str(binary)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    readback = plistlib.loads(entitlement.stdout)
    if readback != {"com.apple.security.virtualization": True}:
        raise RuntimeError("signed provisioner does not have exactly the reviewed virtualization entitlement")
    identity_lines = [line.strip() for line in details.stderr.splitlines()
                      if line.startswith(("Identifier=", "Format=", "CodeDirectory=", "CDHash=",
                                          "Signature=", "TeamIdentifier=", "Authority="))]
    if "Signature=adhoc" not in identity_lines:
        raise RuntimeError("provisioner signature identity is not the expected local ad-hoc signature")
    cdhash_values = [line.partition("=")[2] for line in identity_lines if line.startswith("CDHash=")]
    if (len(cdhash_values) != 1
            or re.fullmatch(r"[0-9a-fA-F]{40}", cdhash_values[0]) is None):
        raise RuntimeError("provisioner CodeDirectory hash could not be bound to the executable identity")
    requirements = subprocess.run(["/usr/bin/codesign", "-dr", "-", str(binary)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, check=False)
    designated_requirement = "\n".join(part for part in (
        requirements.stderr.strip(), requirements.stdout.strip()) if part)
    if requirements.returncode != 0 or "designated =>" not in designated_requirement:
        raise RuntimeError("provisioner designated code-signing requirement could not be read")
    entitlement_bytes = vm.policy._read_file_nofollow(entitlements_path, maximum_bytes=MIB)
    if hashlib.sha256(entitlement_bytes).hexdigest() != entitlements["sha256"]:
        raise RuntimeError("virtualization entitlement plist changed after identity hashing")
    return {
        "source": source,
        "orchestration_source": adapter,
        "controller_dependencies": controller_dependencies,
        "binary": {**binary_identity, "codesign_identity": identity_lines,
                   "code_directory_hash": cdhash_values[0].lower(),
                   "designated_requirement": designated_requirement,
                   "signature_verified": True},
        "entitlements": {**entitlements, "bytes_base64": base64.b64encode(entitlement_bytes).decode("ascii"),
                         "readback": readback},
    }


def _unchanged_helper_relationship_r4(identities: Mapping[str, Any],
                                      signing_key: bytes) -> dict[str, Any]:
    """Bind the unchanged R2 Swift helper through R3 ancestry to the R4 adapter."""
    relationship_path = P2_R3_UNCHANGED_HELPER_RELATIONSHIP_PATH
    relationship, relationship_sha = read_signed_with_key(
        relationship_path, "vm_p2_r3_unchanged_helper_relationship_hmac_sha256", signing_key)
    if (relationship.get("record_type") != "R6_VM_P2_R3_UNCHANGED_HELPER_RELATIONSHIP"
            or relationship.get("status") != "PASS"
            or relationship.get("authority_scope") != "NONE"
            or relationship.get("execution_authority") != "NONE"
            or relationship.get("rebuild_performed_for_r3") is not False
            or relationship.get("r3_source_manifest", {}).get("sha256") != P2_R3_SOURCE_MANIFEST_SHA256):
        raise RuntimeError("accepted R3 unchanged-helper relationship is missing or malformed")
    r2_manifest_row = relationship.get("historical_r2_source_manifest", {})
    r2_build_row = relationship.get("historical_r2_build_receipt", {})
    r2_review_row = relationship.get("historical_r2_provisioner_review", {})
    r2_manifest_path = _admission_artifact_path(r2_manifest_row.get("path"), "R2 source manifest")
    r2_build_path = _admission_artifact_path(r2_build_row.get("path"), "R2 build receipt")
    r2_review_path = _admission_artifact_path(r2_review_row.get("path"), "R2 provisioner review")
    r2_manifest, r2_manifest_sha = read_signed_with_key(
        r2_manifest_path, "vm_p2_provenance_hmac_sha256", signing_key)
    if (r2_manifest_sha != r2_manifest_row.get("sha256")
            or vm._file_sha256(r2_build_path) != r2_build_row.get("sha256")
            or vm._file_sha256(r2_review_path) != r2_review_row.get("sha256")):
        raise RuntimeError("historical R2 helper provenance digest changed")
    r2_build, _ = read_signed_with_key(
        r2_build_path, "vm_p2_provisioner_build_hmac_sha256", signing_key)
    r2_review = _check_review(r2_review_path, r2_manifest, r2_manifest_path)
    r3_manifest_path = _admission_artifact_path(
        relationship.get("r3_source_manifest", {}).get("path"), "R3 source manifest")
    r3_manifest, r3_manifest_sha = read_signed_with_key(
        r3_manifest_path, "vm_p2_provenance_hmac_sha256", signing_key)
    if r3_manifest_sha != P2_R3_SOURCE_MANIFEST_SHA256:
        raise RuntimeError("historical R3 source manifest digest changed")
    r2_source = r2_manifest.get("source_manifest", {})
    r3_source = r3_manifest.get("source_manifest", {})
    current_source = identities
    for predecessor in (r2_source, r3_source):
        for name in ("source", "binary", "entitlements"):
            prior = predecessor.get(name, {})
            current = current_source.get(name, {})
            fields = ("path", "sha256") if name != "binary" else (
                "path", "sha256", "code_directory_hash")
            if any(prior.get(field) != current.get(field) for field in fields):
                raise RuntimeError("R4 helper identity differs from accepted R2/R3 helper provenance")
    r2_build_identity = r2_build.get("executable", {})
    current_binary = current_source.get("binary", {})
    if (r2_build_identity.get("sha256") != current_binary.get("sha256")
            or r2_build_identity.get("code_directory_hash")
                != current_binary.get("code_directory_hash")
            or r2_review.get("decision") not in {
                "PASS — P2 PROVISIONER APPROVED", "PASS - P2 PROVISIONER APPROVED"}):
        raise RuntimeError("R2 build or provisioner review does not bind the unchanged R4 helper")
    adapter = current_source.get("orchestration_source", {})
    return {
        "schema_version": 1,
        "record_type": "R6_VM_P2_R4_UNCHANGED_HELPER_RELATIONSHIP",
        "status": "PASS",
        "authority_scope": "NONE",
        "execution_authority": "NONE",
        "rebuild_performed_for_r4": False,
        "historical_r2_source_manifest": {
            "path": str(r2_manifest_path.relative_to(REPO)),
            "sha256": r2_manifest_sha,
        },
        "historical_r2_build_receipt": {
            "path": str(r2_build_path.relative_to(REPO)),
            "sha256": r2_build_row.get("sha256"),
        },
        "historical_r2_provisioner_review": {
            "path": str(r2_review_path.relative_to(REPO)),
            "sha256": r2_review_row.get("sha256"),
            "decision": r2_review.get("decision"),
        },
        "historical_r3_source_manifest": {
            "path": str(r3_manifest_path.relative_to(REPO)),
            "sha256": r3_manifest_sha,
        },
        "historical_r3_unchanged_helper_relationship": {
            "path": str(relationship_path.relative_to(REPO)),
            "sha256": relationship_sha,
        },
        "r2_helper_identity": {
            "swift_source_sha256": r2_source["source"]["sha256"],
            "binary_path": r2_source["binary"]["path"],
            "binary_sha256": r2_source["binary"]["sha256"],
            "code_directory_hash": r2_source["binary"]["code_directory_hash"],
            "entitlements_sha256": r2_source["entitlements"]["sha256"],
        },
        "r3_helper_identity": {
            "swift_source_sha256": r3_source["source"]["sha256"],
            "binary_path": r3_source["binary"]["path"],
            "binary_sha256": r3_source["binary"]["sha256"],
            "code_directory_hash": r3_source["binary"]["code_directory_hash"],
            "entitlements_sha256": r3_source["entitlements"]["sha256"],
        },
        "r4_helper_identity": {
            "swift_source_sha256": current_source["source"]["sha256"],
            "binary_path": current_binary["path"],
            "binary_sha256": current_binary["sha256"],
            "code_directory_hash": current_binary["code_directory_hash"],
            "entitlements_sha256": current_source["entitlements"]["sha256"],
        },
        "r4_python_adapter": {"path": adapter["path"], "sha256": adapter["sha256"]},
        "comparisons": {
            "swift_source_unchanged": True,
            "helper_path_unchanged": True,
            "helper_binary_unchanged": True,
            "code_directory_hash_unchanged": True,
            "entitlements_unchanged": True,
            "r3_ancestry_verified": True,
            "r4_adapter_bound_separately": True,
        },
    }


def record_provenance(args: argparse.Namespace) -> None:
    build_dir = Path(args.build_directory).resolve(strict=True)
    binary = build_dir / "vm-p2-provisioner"
    entitlements = build_dir / "virtualization-entitlements.plist"
    if not binary.is_file() or not entitlements.is_file():
        raise RuntimeError("reviewed helper binary or entitlement file is absent")
    identities = _validate_binary(binary, HERE / "vm_p2_provisioner.swift",
                                  HERE / "p2_base_orchestrator.py", entitlements)
    xcode_version = subprocess.run(["/usr/bin/xcodebuild", "-version"], check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    swift_version = subprocess.run(["/usr/bin/xcrun", "swiftc", "--version"], check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    sdk_path = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    sdk_version = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    sdk_build = subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-build-version"], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()
    compiler_path = Path(subprocess.run(["/usr/bin/xcrun", "--find", "swiftc"], check=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True).stdout.strip()).resolve(strict=True)
    compiler_sha256, compiler_bytes, compiler_hash_window = _hash_file_with_identity_window(compiler_path)
    compiler_identity = {
        "path": str(compiler_path),
        "canonical_path": str(compiler_path),
        "file_type": "regular_file",
        "sha256": compiler_sha256,
        "bytes": compiler_bytes,
        "file_identity": compiler_hash_window["identity_after_hash"],
        "mode": compiler_hash_window["identity_after_hash"]["mode"],
        "hash_window_identity": compiler_hash_window,
    }
    compiler_codesign = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=4", str(compiler_path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, check=False)
    compiler_identity["codesign_details"] = {
        "returncode": compiler_codesign.returncode,
        "identity_lines": [line.strip() for line in compiler_codesign.stderr.splitlines()
                            if line.startswith(("Identifier=", "Format=", "CodeDirectory=", "Signature=",
                                                "TeamIdentifier=", "Authority="))],
    }
    compiler_target_info = json.loads(subprocess.run(
        ["/usr/bin/xcrun", "swiftc", "-print-target-info"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout)
    repository_revision = subprocess.run(["/usr/bin/git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True).stdout.strip()
    repository_tree_revision = subprocess.run(["/usr/bin/git", "rev-parse", "HEAD^{tree}"], cwd=REPO,
                                              check=True, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True).stdout.strip()
    signing_key = vm.policy._manifest_signing_key()
    unchanged_helper_relationship = _unchanged_helper_relationship_r4(identities, signing_key)
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_PROVISIONER_SOURCE_MANIFEST",
        "run_id": args.run_id,
        "created_at_unix": time.time(),
        "source_manifest": identities,
        "compiler": {"xcode_version": xcode_version, "swiftc_version": swift_version,
                     "swiftc_executable": compiler_identity,
                     "sdk_path": sdk_path, "sdk_version": sdk_version, "sdk_build": sdk_build,
                     "target_triple": compiler_target_info.get("target", {}).get("triple"),
                     "source_revision": repository_revision,
                     "source_tree_revision": repository_tree_revision,
                     "source_input_manifest_sha256": hashlib.sha256(json_bytes(identities).rstrip(b"\n")).hexdigest(),
                     "worktree_dirty": bool(subprocess.run(
                         ["/usr/bin/git", "status", "--porcelain", "--", str(HERE.relative_to(REPO))],
                         cwd=REPO, check=True, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True).stdout.strip()),
                     "host_architecture": platform.machine(),
                     "host_build": subprocess.run(["/usr/bin/sw_vers", "-buildVersion"], check=True,
                                                  stdout=subprocess.PIPE, text=True).stdout.strip()},
        "build_command": {
            "compile_argv": ["xcrun", "swiftc", "-target",
                             compiler_target_info.get("target", {}).get("triple"),
                             "-parse-as-library", "-O", "-whole-module-optimization",
                             "-framework", "Virtualization", "-framework", "Foundation", "-framework", "CryptoKit",
                             str(HERE / "vm_p2_provisioner.swift"), "-o", str(binary)],
            "codesign_argv": ["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements",
                              str(entitlements), str(binary)],
            "launch_policy": {
                "executable_resolution": "open reviewed binary with O_NOFOLLOW, rehash and compare full manifest file identity, posix_spawn the reviewed pathname suspended, recheck the same open-file and pathname identity and SHA-256, match the kernel CS_OPS_CDHASH to the reviewed CodeDirectory hash, then resume",
                "environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
                "hardened_runtime_required": True,
            },
        },
        "configuration_schema": {
            "platform": "VZMacPlatformConfiguration",
            "hardware_model": "exact accepted local IPSW mostFeaturefulSupportedConfiguration model",
            "machine_identifier": "single persisted VZMacMachineIdentifier dataRepresentation",
            "auxiliary_storage": "VZMacAuxiliaryStorage tied to exact hardware model",
            "boot_loader": "VZMacOSBootLoader",
            "cpu_count_minimum": 2,
            "memory_bytes_minimum": 4294967296,
            "system_raw_logical_capacity_bytes": SYSTEM_LOGICAL_CAPACITY,
            "storage_devices": ["one VZVirtioBlockDeviceConfiguration backed by local RAW"],
            "network_devices": [], "directory_sharing_devices": [], "serial_ports": [],
            "console_devices": [], "socket_devices": [], "usb_controllers": [], "graphics_devices": [],
            "runtime_start_gate": {
                "native_admission_verification": "before Virtualization.framework access, the helper independently verifies the fixed-path HMAC-signed PASS P2 admission, P2_AUTHORIZED marker, signed source manifest, review, binary, and imported controller dependency hashes",
                "authorization_payload": "P2AUTH1 binds run, P2 session, exact signed admission, active-marker, provisioner hashes, PROVISION_MACOS_GUEST_BASE_ONLY, and P2; a fresh random 32-byte launch capability is stored only in an owner-only private file derived from the exact signed admission digest, its SHA-256 commitment is signed in the admission, and one atomic no-replace claim loads it for the private helper control pipe",
                "execution_start_freshness": "the static reviewed admission is durable and age-unlimited; a signed, exact-admission, 30-second single-use execution-start lease binds the fresh storage observation and remains fresh through the three-sample host monitor initial gate and the atomic capability claim",
                "monitor_gate_order": "P2 adapter host monitor initializes and its initial live capacity gate passes while the exact capability remains READY; then the lease is revalidated and the capability is atomically claimed; helper authorization, helper monitor acknowledgment, and START follow; the same monitor remains live throughout installation",
                "monitor_heartbeat": "HMAC-authenticated with the admission-committed in-memory token after each subsequent adapter-approved sample; helper expires after 250 ms",
                "start_acknowledgment_required": True,
                "maximum_monitor_heartbeat_age_seconds": vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS,
            },
            "controller_dependencies": [
                {"path": item["path"], "sha256": item["sha256"]}
                for item in identities["controller_dependencies"]
            ],
        },
        "unchanged_helper_relationship": unchanged_helper_relationship,
        "authority": "PROVISION_MACOS_GUEST_BASE_ONLY",
    }
    path = unique_path("R6-VM-P2-PROVISIONER-SOURCE-MANIFEST", args.run_id)
    signed = signed_write(path, record, "vm_p2_provenance_hmac_sha256")
    print(json.dumps({"manifest_path": str(path), "manifest_sha256": vm._file_sha256(path),
                      "binary_sha256": identities["binary"]["sha256"],
                      "source_sha256": identities["source"]["sha256"],
                      "adapter_sha256": identities["orchestration_source"]["sha256"],
                      "status": "PASS" if signed else "FAIL"}, sort_keys=True))


def _check_review(review_path: Path, manifest: Mapping[str, Any],
                  expected_manifest_path: Path) -> dict[str, Any]:
    try:
        review, _review_sha = read_signed(
            review_path, "vm_p2_provisioner_review_hmac_sha256")
    except (OSError, RuntimeError) as error:
        raise RuntimeError("independent P2 provisioner review is not a valid signed review receipt") from error
    sources = manifest.get("source_manifest", {})
    allowed = {"PASS — P2 PROVISIONER APPROVED", "PASS - P2 PROVISIONER APPROVED"}
    if review.get("decision") not in allowed:
        raise RuntimeError("independent P2 provisioner reviewer did not return the exact PASS disposition")
    manifest_path_text = review.get("manifest_path")
    manifest_path = Path(manifest_path_text) if isinstance(manifest_path_text, str) else Path("/")
    if not manifest_path.is_absolute():
        manifest_path = REPO / manifest_path
    try:
        same_manifest_path = manifest_path.resolve(strict=True) == expected_manifest_path.resolve(strict=True)
    except OSError:
        same_manifest_path = False
    if (review.get("reviewed_source_sha256") != sources.get("source", {}).get("sha256")
            or review.get("reviewed_adapter_sha256") != sources.get("orchestration_source", {}).get("sha256")
            or review.get("reviewed_binary_sha256") != sources.get("binary", {}).get("sha256")
            or review.get("reviewed_code_directory_hash") != sources.get("binary", {}).get("code_directory_hash")
            or review.get("reviewed_controller_dependencies") != [
                {"path": item.get("path"), "sha256": item.get("sha256")}
                for item in sources.get("controller_dependencies", []) if isinstance(item, Mapping)]
            or not same_manifest_path
            or review.get("reviewed_manifest_sha256") != vm._file_sha256(expected_manifest_path)):
        raise RuntimeError("independent P2 provisioner review does not bind exact current source, adapter, binary, and manifest")
    return review


def _stable_provenance_binding(value: Any) -> Any:
    """Project out per-hash timing evidence while retaining every stable binding."""
    if isinstance(value, Mapping):
        return {key: _stable_provenance_binding(item)
                for key, item in value.items() if key != "hash_window_identity"}
    if isinstance(value, list):
        return [_stable_provenance_binding(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stable_provenance_binding(item) for item in value)
    return value


def _verify_reviewed_provenance(manifest_path: Path, review_path: Path,
                                signing_key: bytes | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, _ = (read_signed(manifest_path, "vm_p2_provenance_hmac_sha256")
                   if signing_key is None else
                   read_signed_with_key(manifest_path, "vm_p2_provenance_hmac_sha256", signing_key))
    _check_review(review_path, manifest, manifest_path)
    current = _validate_binary(REPO / manifest["source_manifest"]["binary"]["path"],
                               REPO / manifest["source_manifest"]["source"]["path"],
                               REPO / manifest["source_manifest"]["orchestration_source"]["path"],
                               REPO / manifest["source_manifest"]["entitlements"]["path"])
    if (_stable_provenance_binding(current)
            != _stable_provenance_binding(manifest["source_manifest"])):
        raise RuntimeError("reviewed source or provisioner binary identity changed after review")
    return manifest, json.loads(review_path.read_text(encoding="utf-8"))


def _persist_local_model_mismatch_observation(
        run_id: str, manifest_path: Path, review_path: Path,
        manifest: Mapping[str, Any], process: SpawnedChild,
        execution_identity: Mapping[str, Any], events: list[Mapping[str, Any]]) -> Path | None:
    """Persist only a fully bound, non-mutating local-restore compatibility mismatch."""
    match = re.search(
        r'Error Domain=FGMetalP2 Code=7 "local restore hardware model differs from (?:accepted P1 compatibility metadata|reconciled P2 local compatibility identity) '
        r'\(expected SHA-256 ([0-9a-f]{64}), actual SHA-256 ([0-9a-f]{64})\)"',
        process.stderr_text)
    hashes = [row for row in events if row.get("event") == "restore_hash_verified"]
    observations = [row for row in events if row.get("event") == "local_restore_compatibility_observed"]
    plans = [row for row in events if row.get("event") == "plan"]
    if (match is None or process.returncode == 0 or len(hashes) != 1
            or len(observations) != 1 or plans):
        return None
    expected_model_sha256, actual_model_sha256 = match.groups()
    restore_event = hashes[0]
    observed = observations[0]
    if (expected_model_sha256 not in (P1_UPSTREAM_HARDWARE_MODEL_SHA256, P2_LOCAL_HARDWARE_MODEL_SHA256)
            or actual_model_sha256 == expected_model_sha256
            or observed.get("hardware_model_sha256") != actual_model_sha256
            or observed.get("restore_sha256") != RESTORE_SHA256
            or restore_event.get("sha256") != RESTORE_SHA256
            or observed.get("restore_parse_source")
                != "VZMacOSRestoreImage.image(from: verified local IPSW URL)"
            or observed.get("operating_system_version") != "27.0.1"
            or observed.get("build_version") != "26A434"
            or observed.get("image_supported") is not True
            or observed.get("hardware_model_supported") is not True
            or observed.get("restore_file_identity_after_hash") != restore_event.get("file_identity")
            or observed.get("restore_file_identity_after_parse") != restore_event.get("file_identity")):
        return None
    lineage = _validate_accepted_p1()
    current_sha256, current_size, current_identity = sha256_file(RESTORE_PATH)
    p1_identity = lineage["p1_final"].get("file_identity")
    observed_identity = restore_event.get("file_identity")
    p1_object_identity_fields = (
        "device", "inode", "logical_bytes", "mtime_ns", "ctime_ns")
    if (current_sha256 != RESTORE_SHA256 or current_size != RESTORE_SIZE
            or not isinstance(p1_identity, Mapping) or not isinstance(observed_identity, Mapping)
            or observed_identity.get("link_count") != 1
            or not stat.S_ISREG(observed_identity.get("mode", 0))
            or any(p1_identity.get(key) != current_identity.get(key)
                   for key in p1_object_identity_fields)
            or not isinstance(p1_identity.get("allocated_bytes"), int)
            or isinstance(p1_identity.get("allocated_bytes"), bool)
            or any(observed_identity.get(key) != current_identity.get(key)
                   for key in current_identity)):
        return None
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_LOCAL_COMPATIBILITY_OBSERVATION",
        "run_id": run_id,
        "created_at_unix": time.time(),
        "status": "OBSERVED_LOCAL_MODEL_MISMATCH",
        "plan_only": True,
        "guest_assets_created": False,
        "preflight_executed": False,
        "admission_created": False,
        "virtualization_vm_actions_performed": False,
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": current_sha256,
        "restore_size_bytes": current_size,
        "restore_file_identity": current_identity,
        "p1_restore_file_identity": dict(p1_identity),
        "p1_restore_object_identity_match_fields": list(p1_object_identity_fields),
        "p1_restore_object_identity_match": True,
        "p1_restore_allocated_bytes": p1_identity["allocated_bytes"],
        "current_restore_allocated_bytes": current_identity["allocated_bytes"],
        "restore_allocated_bytes_delta_since_p1": (
            current_identity["allocated_bytes"] - p1_identity["allocated_bytes"]),
        "restore_allocation_reconciliation": (
            "P1 and current file device, inode, logical size, mtime, and ctime match; "
            "accepted full SHA-256 and logical size reverify; allocated_bytes is preserved "
            "as a separate P1-versus-current capacity observation, and fresh P2 capacity "
            "admission uses current measurements"),
        "p1_final_receipt_path": str(P1_FINAL_PATH.relative_to(REPO)),
        "p1_final_receipt_sha256": lineage["p1_final_sha256"],
        "p1_metadata_receipt_path": str(UPSTREAM_METADATA_PATH.relative_to(REPO)),
        "p1_metadata_receipt_sha256": lineage["upstream_metadata_file_sha256"],
        "p1_metadata_observation_sha256": lineage["upstream_metadata_sha256"],
        "p1_metadata_probe": lineage["upstream_metadata"].get("probe"),
        "expected_p1_hardware_model_sha256": expected_model_sha256,
        "observed_local_hardware_model_sha256": actual_model_sha256,
        "local_restore_compatibility_event": dict(observed),
        "restore_hash_event": dict(restore_event),
        "provisioner_source_sha256": manifest["source_manifest"]["source"]["sha256"],
        "orchestration_source_sha256": manifest["source_manifest"]["orchestration_source"]["sha256"],
        "provisioner_binary_sha256": manifest["source_manifest"]["binary"]["sha256"],
        "provisioner_binary_execution_identity": dict(execution_identity),
        "helper_process_code_directory_hash": process.code_directory_hash,
        "helper_executable_sha256_after_suspended_exec": process.executable_sha256_after_suspended_exec,
        "helper_executable_identity_after_suspended_exec": process.executable_identity_after_suspended_exec,
        "helper_return_code": process.returncode,
        "helper_launch_environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
        "helper_launch_method": "reviewed-path suspended exec with open-file and pathname identity plus SHA-256 revalidated before resume, matching kernel CodeDirectory hash, and sanitized environment",
        "manifest_path": str(manifest_path.relative_to(REPO)),
        "manifest_sha256": vm._file_sha256(manifest_path),
        "review_path": str(review_path.relative_to(REPO)),
        "review_sha256": vm._file_sha256(review_path),
        "helper_event_sha256": hashlib.sha256(process.stdout_text.encode()).hexdigest(),
        "helper_stderr_sha256": hashlib.sha256(process.stderr_text.encode()).hexdigest(),
        "helper_stdout_event_count": len(events),
    }
    path = unique_path("R6-VM-P2-LOCAL-COMPATIBILITY-OBSERVATION", run_id)
    signed_write(path, record, "vm_p2_local_compatibility_observation_hmac_sha256")
    return path


def _validate_local_restore_plan_events(events: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Validate the production helper's local-restore event contract before sealing its plan."""
    if not events or any(not isinstance(row, Mapping) for row in events):
        raise RuntimeError("reviewed helper plan emitted malformed event data")
    plans = [row for row in events if row.get("event") == "plan"]
    hashes = [row for row in events if row.get("event") == "restore_hash_verified"]
    observations = [row for row in events if row.get("event") == "local_restore_compatibility_observed"]
    if len(plans) != 1 or len(hashes) != 1 or len(observations) != 1:
        raise RuntimeError("helper plan omitted or duplicated the local restore identity events")

    plan, restore_hash, observation = plans[0], hashes[0], observations[0]
    metadata = plan.get("metadata")
    file_identity = restore_hash.get("file_identity")
    if not isinstance(metadata, Mapping) or not isinstance(file_identity, Mapping):
        raise RuntimeError("helper plan omitted local restore metadata or file identity")
    p1_identity_fields = ("device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns", "ctime_ns")
    p1_identity = {field: file_identity.get(field) for field in p1_identity_fields}
    if (restore_hash.get("sha256") != RESTORE_SHA256
            or file_identity.get("logical_bytes") != RESTORE_SIZE
            or plan.get("is_supported") is not True
            or plan.get("restore_file_identity") != p1_identity
            or metadata.get("restore_file_identity") != file_identity
            or metadata.get("restore_sha256") != RESTORE_SHA256
            or metadata.get("operating_system_version") != "27.0.1"
            or metadata.get("build_version") != "26A434"
            or metadata.get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or metadata.get("minimum_supported_cpu_count") != 2
            or metadata.get("minimum_supported_memory_size_bytes") != 4 * GIB
            or observation.get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or observation.get("restore_sha256") != RESTORE_SHA256
            or observation.get("restore_file_identity_after_hash") != file_identity
            or observation.get("restore_file_identity_after_parse") != file_identity
            or observation.get("restore_parse_source")
                != "VZMacOSRestoreImage.image(from: verified local IPSW URL)"
            or observation.get("operating_system_version") != "27.0.1"
            or observation.get("build_version") != "26A434"
            or observation.get("image_supported") is not True
            or observation.get("hardware_model_supported") is not True
            or observation.get("minimum_supported_cpu_count") != 2
            or observation.get("minimum_supported_memory_size_bytes") != 4 * GIB
            or plan.get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256):
        raise RuntimeError("helper plan does not revalidate the exact accepted local IPSW and compatibility requirements")
    return plan


def _run_plan(run_id: str, binary: Path, provenance_path: Path, review_path: Path) -> Path:
    manifest, review = _verify_reviewed_provenance(provenance_path, review_path)
    binary_record = manifest["source_manifest"]["binary"]
    descriptor, execution_identity = _open_reviewed_binary_for_exec(
        binary, binary_record["sha256"], binary_record["file_identity"])
    try:
        process = _spawn_reviewed_binary_suspended(
            [str(binary), "plan", str(RESTORE_PATH)], descriptor,
            binary_record["sha256"], binary_record["file_identity"],
            binary_record["code_directory_hash"])
        process.stdout_text, process.stderr_text = process.communicate(timeout=30)
    finally:
        os.close(descriptor)
    try:
        events = [json.loads(line) for line in process.stdout_text.splitlines() if line.strip()]
    except (ValueError, json.JSONDecodeError):
        events = []
    if process.returncode != 0:
        observation = _persist_local_model_mismatch_observation(
            run_id, provenance_path, review_path, manifest, process, execution_identity, events)
        suffix = f"; signed observation: {observation}" if observation is not None else ""
        raise RuntimeError(f"reviewed helper plan failed: {process.stderr_text.strip()}{suffix}")
    data = _validate_local_restore_plan_events(events)
    machine = data.get("machine_identifier_data_base64")
    hardware = data.get("hardware_model_data_base64")
    if not isinstance(machine, str) or not isinstance(hardware, str):
        raise RuntimeError("helper plan omitted opaque persisted platform identities")
    machine_creation = data.get("machine_identifier_creation")
    if (not isinstance(machine_creation, Mapping)
            or machine_creation.get("method") != "VZMacMachineIdentifier() exactly once during the signed P2 identity-plan command"
            or not isinstance(machine_creation.get("created_at_unix"), (int, float))
            or not math.isfinite(machine_creation.get("created_at_unix"))
            or machine_creation.get("created_at_unix") <= 0
            or not isinstance(data.get("monotonic_time_seconds"), (int, float))):
        raise RuntimeError("helper plan omitted a bounded machine-identifier creation method and timestamp")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_IDENTITY_PLAN",
        "run_id": run_id,
        "created_at_unix": time.time(),
        "status": "PASS",
        "plan_only": True,
        "guest_assets_created": False,
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": RESTORE_SHA256,
        "restore_file_identity": data.get("restore_file_identity"),
        "hardware_model_data_base64": hardware,
        "hardware_model_sha256": data["hardware_model_sha256"],
        "machine_identifier_data_base64": machine,
        "machine_identifier_sha256": data["machine_identifier_sha256"],
        "machine_identifier_creation": machine_creation,
        "helper_plan_event_monotonic_seconds": data["monotonic_time_seconds"],
        "restore_subject": data.get("metadata"),
        "minimum_supported_cpu_count": data["metadata"]["minimum_supported_cpu_count"],
        "minimum_supported_memory_size_bytes": data["metadata"]["minimum_supported_memory_size_bytes"],
        "provisioner_source_sha256": manifest["source_manifest"]["source"]["sha256"],
        "provisioner_binary_sha256": manifest["source_manifest"]["binary"]["sha256"],
        "provisioner_binary_execution_identity": execution_identity,
        "helper_launch_environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
        "helper_launch_method": "reviewed-path suspended exec with open-file and pathname identity plus SHA-256 revalidated before resume, matching kernel CodeDirectory hash, and sanitized environment",
        "helper_process_code_directory_hash": process.code_directory_hash,
        "helper_executable_sha256_after_suspended_exec": process.executable_sha256_after_suspended_exec,
        "helper_executable_identity_after_suspended_exec": process.executable_identity_after_suspended_exec,
        "review_path": str(review_path),
        "review_sha256": vm._file_sha256(review_path),
        "helper_event_sha256": hashlib.sha256(process.stdout_text.encode()).hexdigest(),
        "helper_stderr_sha256": hashlib.sha256(process.stderr_text.encode()).hexdigest(),
    }
    path = unique_path("R6-VM-P2-IDENTITY-PLAN", run_id)
    signed_write(path, record, "vm_p2_identity_plan_hmac_sha256")
    return path


def _process_group_presence_for_pid(process_group_id: int) -> bool | None:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def _p2_process_scan_evidence(*, required_absent_pids: tuple[int, ...] = (),
                              required_absent_process_group_ids: tuple[int, ...] = ()) -> dict[str, Any]:
    command = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="]
    result = subprocess.run(command, check=False, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"P2 process scan failed with exit code {result.returncode}")
    if result.stderr.strip():
        raise RuntimeError("P2 process scan emitted diagnostics; process-table completeness is unknown")
    own_pids = {os.getpid()}
    try:
        parent = os.getppid()
        own_pids.add(parent)
    except OSError:
        pass
    blockers: list[dict[str, Any]] = []
    scanned_pids: list[int] = []
    scanned_process_groups: dict[int, list[int]] = {}
    scanned_rows = 0
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)", line)
        if not match:
            raise RuntimeError("P2 process scan returned a non-parseable process row")
        scanned_rows += 1
        pid = int(match.group(1))
        parent_pid = int(match.group(2))
        process_group_id = int(match.group(3))
        scanned_pids.append(pid)
        scanned_process_groups.setdefault(process_group_id, []).append(pid)
        if pid in own_pids:
            continue
        process_command = match.group(4)
        if ("vm-p2-provisioner" in process_command or "vm_p2_provisioner.swift" in process_command
                or "VZMacOSInstaller" in process_command or "R6-VM-P2-INSTALL" in process_command):
            blockers.append({"pid": pid, "ppid": parent_pid, "process_group_id": process_group_id,
                             "command": process_command[:300]})
    if scanned_rows == 0:
        raise RuntimeError("P2 process scan returned no parseable process rows")
    required_pid_presence = {str(pid): pid in scanned_pids for pid in required_absent_pids}
    for pid, is_present in required_pid_presence.items():
        if is_present:
            blockers.append({"pid": int(pid), "command": "required-exited-P2-helper-still-present"})
    required_process_group_members = {
        str(process_group_id): sorted(scanned_process_groups.get(process_group_id, []))
        for process_group_id in required_absent_process_group_ids
    }
    for process_group_id, members in required_process_group_members.items():
        for pid in members:
            blockers.append({"pid": pid, "process_group_id": int(process_group_id),
                             "command": "required-empty-P2-process-group-still-has-member"})
    return {
        "captured_at_unix": time.time(),
        "method": "ps PID/PPID/PGID process-table scan; required helper process group must be empty",
        "command": command,
        "exit_code": result.returncode,
        "scanned_process_rows": scanned_rows,
        "scanned_pids": sorted(scanned_pids),
        "scanned_process_groups": {str(group): sorted(members)
                                    for group, members in sorted(scanned_process_groups.items())},
        "excluded_supervisor_pids": sorted(own_pids),
        "required_absent_pid_presence": required_pid_presence,
        "required_absent_process_group_members": required_process_group_members,
        "blocking_processes": blockers,
        "process_table_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "process_table_bytes": len(result.stdout.encode()),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "stderr_bytes": len(result.stderr.encode()),
    }


def _stable_descriptor_target_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a scanner row onto the path/file identity that must remain stable."""
    path = row.get("path")
    identity = row.get("file_identity")
    if not isinstance(path, str) or not isinstance(identity, Mapping):
        raise RuntimeError("P2 descriptor scan row lacks a canonical path/file identity")
    return {"path": path, "file_identity": dict(identity)}


def _descriptor_scan_evidence(paths: list[Path]) -> dict[str, Any]:
    expected: dict[str, dict[str, Any]] = {}
    identity_before = []
    expected_lsof_device_numbers: dict[str, str] = {}
    for path in paths:
        identity = _seal_file_identity(os.stat(path, follow_symlinks=False))
        row = {"path": str(path), "file_identity": identity}
        device_number = f"0x{identity['device']:x}"
        expected_lsof_device_numbers[str(path)] = device_number
        expected[str(path)] = {**row, "lsof_device_number_hex": device_number}
        identity_before.append(row)
    command = ["/usr/sbin/lsof", "-nP", "-FpcfaDtikn", "--", *(str(path) for path in paths)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=False, timeout=10)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"P2 descriptor scan failed with exit code {result.returncode}")
    if result.stderr.strip():
        raise RuntimeError("P2 descriptor scan emitted diagnostic output; scan completeness is unknown")
    if result.returncode == 1 and result.stdout:
        raise RuntimeError("P2 descriptor scan returned an unexpected nonempty no-match result")
    descriptor_rows: list[dict[str, Any]] = []
    current_process: dict[str, Any] | None = None
    current_file: dict[str, Any] | None = None
    field_names = {"p": "pid", "c": "command", "f": "fd", "a": "access_mode",
                   "D": "device_number_hex", "t": "file_type", "i": "inode",
                   "k": "link_count", "n": "name"}
    for line in result.stdout.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            if current_process is not None:
                if current_file is not None:
                    current_process.setdefault("files", []).append(current_file)
                    current_file = None
                descriptor_rows.append(current_process)
            current_process = {"pid": int(value), "files": []}
            current_file = None
        elif field == "f":
            if current_process is None:
                raise RuntimeError("P2 lsof output contains a file row without a process row")
            if current_file is not None:
                current_process.setdefault("files", []).append(current_file)
            current_file = {"fd": value}
        elif field == "c" and current_process is not None:
            current_process["command"] = value
        elif current_file is not None and field in field_names:
            current_file[field_names[field]] = value
    if current_process is not None:
        if current_file is not None:
            current_process.setdefault("files", []).append(current_file)
        descriptor_rows.append(current_process)
    if result.returncode == 0 and not descriptor_rows:
        raise RuntimeError("P2 descriptor scan returned success without parseable descriptor records")
    identity_after = [{"path": str(path),
                       "file_identity": _seal_file_identity(os.stat(path, follow_symlinks=False))}
                      for path in paths]
    stable_before = [_stable_descriptor_target_identity(row) for row in identity_before]
    stable_after = [_stable_descriptor_target_identity(row) for row in identity_after]
    if stable_before != stable_after:
        raise RuntimeError("P2 descriptor-scan target identity changed while scanning")
    exact_matches: list[dict[str, Any]] = []
    identity_mismatches: list[dict[str, Any]] = []
    for process in descriptor_rows:
        for descriptor in process.get("files", []):
            match = expected.get(descriptor.get("name"))
            exact = False
            if match is not None:
                identity = match["file_identity"]
                expected_inode = str(identity["inode"])
                expected_device = match["lsof_device_number_hex"]
                exact = (descriptor.get("inode") == expected_inode
                         and descriptor.get("device_number_hex") == expected_device)
            descriptor_result = {
                "pid": process.get("pid"), "command": process.get("command"),
                "fd": descriptor.get("fd"), "name": descriptor.get("name"),
                "inode": descriptor.get("inode"),
                "device_number_hex": descriptor.get("device_number_hex"),
                "matched_expected_file_identity": match["file_identity"] if match else None,
                "exact_identity_match": exact,
            }
            if exact:
                exact_matches.append(descriptor_result)
            else:
                identity_mismatches.append(descriptor_result)
    return {
        "captured_at_unix": time.time(),
        "method": "lsof field mode; every returned descriptor must match the exact scanned path, device, and inode",
        "command": command,
        "exit_code": result.returncode,
        "scanned_paths": [str(path) for path in paths],
        "expected_file_identities_before_scan": identity_before,
        "expected_file_identities_after_scan": identity_after,
        "expected_lsof_device_numbers_by_path": expected_lsof_device_numbers,
        "open_file_descriptors": descriptor_rows,
        "exact_target_descriptor_matches": exact_matches,
        "descriptor_identity_mismatches": identity_mismatches,
        "scan_clear": not descriptor_rows and not identity_mismatches,
        "scan_status": ("NO_MATCHING_OPEN_DESCRIPTOR"
                        if result.returncode == 1 and not result.stdout and not result.stderr
                        else "SCANNED"),
        "open_process_ids": sorted({str(row["pid"]) for row in descriptor_rows}),
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stdout_bytes": len(result.stdout.encode()),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "stderr_bytes": len(result.stderr.encode()),
    }


def _process_blockers() -> list[str]:
    return [f"{row['pid']} {row['command']}"
            for row in _p2_process_scan_evidence()["blocking_processes"]]


def _restore_tree_blockers() -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    found_restore: list[str] = []
    ignored = {".DocumentRevisions-V100", ".Spotlight-V100", ".fseventsd",
               ".Trashes", ".TemporaryItems", ".vol", ".apdisk"}
    if not VM_ROOT.is_dir() or VM_ROOT.is_symlink():
        return ["VM volume mount root is absent or not a directory"], []
    templates_root = VM_ROOT / "templates"
    if templates_root.exists() and (templates_root.is_symlink() or not templates_root.is_dir()
                                    or any(templates_root.iterdir())):
        blockers.append("pre-existing template directory or P2 base assets are present and not excluded as historical")
    for base, directories, files in os.walk(VM_ROOT, topdown=True, followlinks=False):
        rel = Path(base).relative_to(VM_ROOT)
        if rel == Path("."):
            directories[:] = [name for name in directories if name not in ignored]
            unexpected_dirs = [name for name in directories if name != "templates"]
            for name in unexpected_dirs:
                blockers.append(f"unexpected pre-P2 VM-volume directory: {name}")
        for name in list(directories):
            entry = Path(base) / name
            if entry.is_symlink():
                blockers.append(f"symlink in VM-volume inventory: {entry}")
        for name in files:
            path = Path(base) / name
            lowered = name.lower()
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as error:
                blockers.append(f"VM-volume inventory failed for {path}: {error}")
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                blockers.append(f"non-regular or linked VM-volume file: {path}")
            if lowered.endswith(".ipsw"):
                found_restore.append(str(path))
                if path != RESTORE_PATH:
                    blockers.append(f"second IPSW payload exists in the controlled VM volume: {path}")
            elif ("restore" in lowered and any(mark in lowered for mark in ("partial", ".part", ".tmp", ".download"))
                  or lowered.endswith((".partial", ".part", ".download"))):
                blockers.append(f"partial restore-like file exists: {path}")
            elif info.st_size >= RESTORE_SIZE - MIB:
                blockers.append(f"unexpected restore-sized payload exists in the controlled VM volume: {path}")
            if rel == Path(".") and name != "restore.ipsw" and not name.startswith("."):
                blockers.append(f"unexpected user data at VM-volume root: {name}")
    if found_restore != [str(RESTORE_PATH)]:
        blockers.append(f"controlled VM volume does not contain exactly the accepted IPSW: {found_restore}")
    return blockers, found_restore


def _preflight(run_id: str, plan_path: Path, provenance_path: Path,
               review_path: Path, r3_supersession_path: Path
               ) -> tuple[dict[str, Any], Path]:
    lineage = _validate_accepted_p1(allow_historical_r2_boundary=True)
    if lineage.get("historical_r2_boundary") is None:
        raise RuntimeError("fresh R3 preflight requires the exact historical R2 admission and blocker receipt boundary")
    reconciliation_info = _validate_p2_reconciliation()
    manifest, review = _verify_reviewed_provenance(provenance_path, review_path)
    supersession_receipt, supersession_sha = _validate_current_r3_supersession(
        run_id, r3_supersession_path,
        signing_key=vm.policy._manifest_signing_key(),
        manifest_path=provenance_path, review_path=review_path)
    supersession_binding = {
        "path": str(Path(r3_supersession_path).resolve(strict=True).relative_to(REPO.resolve())),
        "sha256": supersession_sha,
        "classification": supersession_receipt["classification"],
        "r3_admission_sha256": supersession_receipt["r3_admission_sha256"],
        "r3_capability_commitment_sha256": supersession_receipt[
            "r3_capability_commitment_sha256"],
    }
    plan, plan_sha = _validate_preserved_identity_plan(plan_path, manifest)
    blockers: list[str] = []
    current_identity = None
    current_hash = None
    try:
        current_hash, length, current_identity = sha256_file(RESTORE_PATH)
        if length != RESTORE_SIZE or current_hash != RESTORE_SHA256:
            blockers.append("accepted IPSW size or complete local SHA-256 differs from P1")
        if current_identity != plan.get("restore_file_identity"):
            blockers.append("local IPSW identity differs from the planned hardware identity read")
        p1_id = lineage["p1_final"].get("file_identity", {})
        p1_object_identity_fields = ("device", "inode", "logical_bytes", "mtime_ns", "ctime_ns")
        if any(current_identity.get(k) != p1_id.get(k) for k in p1_object_identity_fields):
            blockers.append("local IPSW file identity differs from the accepted P1 final verification receipt")
    except (OSError, RuntimeError) as error:
        blockers.append(f"accepted local IPSW preflight failed: {error}")
        length = None
    tree_blockers, restore_files = _restore_tree_blockers()
    blockers.extend(tree_blockers)
    blockers.extend(f"stale provisioner or installer process: {row}" for row in _process_blockers())
    try:
        metadata_inventory = _assert_p2_metadata_budget(
            _provisioner_metadata_paths(manifest, provenance_path, review_path, plan_path,
                                        r3_supersession_path))
    except (OSError, RuntimeError) as error:
        metadata_inventory = {"physical_allocated_bytes": None, "remaining_bytes": 0,
                              "file_count": 0, "files": [], "error": str(error)}
        blockers.append(f"P1/P2 metadata inventory failed: {error}")
    target_dir = VM_ROOT / "templates" / f"macos-base-{run_id}"
    if target_dir.exists() or target_dir.is_symlink():
        blockers.append("run-specific base-template destination already exists")
    if vm.VM_STOP_SAFETY_PROFILE_PATH.exists():
        profile, profile_blockers = vm._verify_stop_safety_profile(vm.VM_STOP_SAFETY_PROFILE_PATH)
        if profile_blockers or profile is None:
            blockers.append("present stop-safety profile is invalid: " + "; ".join(profile_blockers))
        elif (profile.get("volume_uuid") != VM_VOLUME_UUID
              or profile.get("g0c_identity") != vm.verify_fresh_g0c_review()):
            blockers.append("present stop-safety profile binds a different VM volume or G0C identity")
    with vm.policy._acquire_storage_allocation_lock():
        observation = vm.capture_host_observation()
        blockers.extend(vm._session_container_blockers(observation, volume_uuid=VM_VOLUME_UUID))
        contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
        volume = vm._volume_row(observation, VM_VOLUME_UUID)
        safety = vm._volume_row(observation, SAFETY_VOLUME_UUID)
        mount = vm._volume_mount_info(observation, VM_VOLUME_UUID)
        current_used = volume.get("CapacityInUse")
        quota_bytes = contract.get("quota_bytes")
        shared_free = vm.conservative_session_unreserved_free_bytes(
            observation, volume_uuid=VM_VOLUME_UUID, capacity_contract=contract)
        reserve_state = observation.get("safety_volume_info", {})
        if (contract.get("safety_volume_uuid") != SAFETY_VOLUME_UUID
                or contract.get("safety_reserve_bytes") != 15 * GIB
                or safety.get("CapacityInUse") != 16_106_127_360
                or reserve_state.get("mount_state") != "UNMOUNTED_VERIFIED"
                or contract.get("quota_bytes") != 53_687_091_200
                or observation.get("apfs_container", {}).get("APFSContainerUUID") != CONTAINER_UUID
                or observation.get("physical_store_info", {}).get("DiskUUID") != PHYSICAL_STORE_UUID):
            blockers.append("fresh safety, production-volume quota, container, or physical-store identity differs from accepted P0")
        if not isinstance(current_used, int) or isinstance(current_used, bool):
            blockers.append("fresh VM volume CapacityInUse is unknown")
            current_used = None
        if not isinstance(quota_bytes, int) or isinstance(quota_bytes, bool):
            blockers.append("fresh VM quota is unknown")
            quota_bytes = None
        if current_identity is not None:
            try:
                restore_now = os.stat(RESTORE_PATH, follow_symlinks=False)
                if file_identity(restore_now) != current_identity or restore_now.st_nlink != 1:
                    blockers.append("restore file identity changed between hash and storage snapshot")
            except OSError as error:
                blockers.append(f"restore path changed during storage observation: {error}")
        measurement = {
            "captured_at_unix": observation.get("captured_at_unix"),
            "container_free_bytes": observation.get("crosschecks", {}).get("apfs_container_free_bytes"),
            "data_container_free_bytes": observation.get("crosschecks", {}).get("data_info_container_free_bytes"),
            "shared_container_free_bytes": vm.conservative_container_free_bytes(observation),
            "unreserved_shared_free_bytes": shared_free,
            "vm_volume_usage_bytes": current_used,
            "vm_volume_quota_bytes": quota_bytes,
            "vm_quota_headroom_bytes": quota_bytes - current_used
            if isinstance(quota_bytes, int) and isinstance(current_used, int) else None,
            "p1_p2_metadata_allocated_bytes": metadata_inventory.get("physical_allocated_bytes"),
            "p1_p2_metadata_remaining_bytes": metadata_inventory.get("remaining_bytes"),
            "safety_volume_uuid": contract.get("safety_volume_uuid"),
            "safety_volume_capacity_in_use_bytes": safety.get("CapacityInUse"),
            "safety_volume_reserve_bytes": contract.get("safety_reserve_bytes"),
            "safety_mount_state": reserve_state.get("mount_state"),
            "vm_volume_uuid": mount.get("volume_uuid"),
            "vm_mount_identity": mount.get("mount_identity"),
            "apfs_container_uuid": observation.get("apfs_container", {}).get("APFSContainerUUID"),
            "physical_store_uuid": observation.get("physical_store_info", {}).get("DiskUUID"),
        }
        preflight_record = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_FRESH_READ_ONLY_PREFLIGHT",
            "run_id": run_id,
            "captured_at_unix": time.time(),
            "status": "PASS" if not blockers else "FAIL",
            "authority": "READ_ONLY_P2_PREFLIGHT_ONLY",
            "p1_final_receipt_path": str(P1_FINAL_PATH.relative_to(REPO)),
            "p1_final_receipt_sha256": P1_FINAL_SHA256,
            "p1_admission_path": str(P1_ADMISSION_PATH.relative_to(REPO)),
            "p1_admission_sha256": P1_ADMISSION_SHA256,
            "p1_freshness_renewal_sha256": P1_RENEWAL_SHA256,
            "p1_first_body_evidence_sha256": P1_BODY_START_SHA256,
            "restore_path": str(RESTORE_PATH),
            "restore_sha256": current_hash,
            "restore_logical_bytes": length,
            "restore_file_identity": current_identity,
            "restore_files_found_in_controlled_vm_volume": restore_files,
            "storage_observation": observation,
            "storage_measurement": measurement,
            "p1_p2_metadata_inventory": metadata_inventory,
            "active_marker_sha256": vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH),
            "controller_identity": vm._controller_identity(),
            "g0c_identity": vm.verify_fresh_g0c_review(),
            "provisioner_manifest_path": str(provenance_path),
            "provisioner_manifest_sha256": vm._file_sha256(provenance_path),
            "provisioner_review_path": str(review_path),
            "provisioner_review_sha256": vm._file_sha256(review_path),
            "p2_reconciliation_path": str(P2_RECONCILIATION_PATH.relative_to(REPO)),
            "p2_reconciliation_sha256": P2_RECONCILIATION_SHA256,
            "p2_reconciliation_review_path": str(P2_RECONCILIATION_REVIEW_PATH.relative_to(REPO)),
            "p2_reconciliation_review_sha256": P2_RECONCILIATION_REVIEW_SHA256,
            "p2_local_hardware_model_sha256": P2_LOCAL_HARDWARE_MODEL_SHA256,
            "p1_upstream_hardware_model_sha256": P1_UPSTREAM_HARDWARE_MODEL_SHA256,
            "identity_plan_path": str(plan_path),
            "identity_plan_sha256": plan_sha,
            "historical_r2_admission_path": str(P2_R2_ADMISSION_PATH.relative_to(REPO)),
            "historical_r2_admission_sha256": lineage["historical_r2_boundary"]["historical_admission_sha256"],
            "historical_r2_execution_blocker_receipt_path": lineage["historical_r2_boundary"]["execution_blocker_receipt_path"],
            "historical_r2_execution_blocker_receipt_sha256": lineage["historical_r2_boundary"]["execution_blocker_receipt_sha256"],
            "r3_execution_packet_supersession": supersession_binding,
            "launch_capability_format_version": P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
            "blockers": blockers,
        }
        preflight_path = unique_path("R6-VM-P2-FRESH-PREFLIGHT", run_id)
        signed_write(preflight_path, preflight_record, "vm_p2_preflight_hmac_sha256")
    if blockers:
        raise RuntimeError("fresh signed P2 preflight failed: " + "; ".join(blockers))
    return preflight_record, preflight_path


def create_admission(args: argparse.Namespace) -> None:
    run_id = args.run_id
    manifest_path = Path(args.manifest).resolve(strict=True)
    review_path = Path(args.review).resolve(strict=True)
    plan_path = Path(getattr(args, "identity_plan", P2_ACCEPTED_IDENTITY_PLAN_PATH)).resolve(strict=True)
    lineage = _validate_accepted_p1(allow_historical_r2_boundary=True)
    if lineage.get("historical_r2_boundary") is None:
        raise RuntimeError("R3 admission requires the exact historical R2 admission and blocker receipt boundary")
    reconciliation_info = _validate_p2_reconciliation()
    r3_supersession_path = Path(args.r3_supersession_receipt).resolve(strict=True)
    preflight, preflight_path = _preflight(
        run_id, plan_path, manifest_path, review_path, r3_supersession_path)
    supersession_receipt, supersession_sha = _validate_current_r3_supersession(
        run_id, r3_supersession_path,
        signing_key=vm.policy._manifest_signing_key(),
        manifest_path=manifest_path, review_path=review_path)
    supersession_binding = dict(preflight["r3_execution_packet_supersession"])
    if supersession_sha != supersession_binding.get("sha256"):
        raise RuntimeError("R3 supersession receipt changed after fresh P2 preflight")
    manifest, review = _verify_reviewed_provenance(manifest_path, review_path)
    plan, plan_sha = _validate_preserved_identity_plan(plan_path, manifest)
    observation = preflight["storage_observation"]
    measurement = preflight["storage_measurement"]
    contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
    used = measurement["vm_volume_usage_bytes"]
    quota = measurement["vm_volume_quota_bytes"]
    free = measurement["unreserved_shared_free_bytes"]
    metadata_remaining = measurement.get("p1_p2_metadata_remaining_bytes")
    if not isinstance(metadata_remaining, int) or isinstance(metadata_remaining, bool):
        raise RuntimeError("fresh P1/P2 metadata inventory has no bounded remaining capacity")
    if metadata_remaining <= P2_METADATA_FINALIZATION_RESERVE:
        raise RuntimeError("P2 metadata envelope cannot preserve the signed receipt finalization reserve")
    full_remaining_growth = P2_SYSTEM_MAX + P2_AUX_MAX + metadata_remaining
    p2_growth = full_remaining_growth - P2_METADATA_FINALIZATION_RESERVE
    uncertainty = max(vm.VM_PHASE_MIN_UNCERTAINTY_BYTES,
                      (full_remaining_growth * vm.VM_PHASE_UNCERTAINTY_BASIS_POINTS + 9999) // 10000)
    total_demand = full_remaining_growth + uncertainty
    projected_shared_free = free - total_demand
    projected_quota_headroom = quota - used - total_demand
    phase_evaluation = vm.calculate_vm_phase_admission(
        mode=vm.VM_MODE, container_uuid=CONTAINER_UUID, volume_uuid=VM_VOLUME_UUID,
        free_bytes=free, quota_bytes=quota, quota_used_bytes=used,
        growth_bytes=p2_growth, uncertainty_bytes=uncertainty,
        concurrent_growth_bytes=None, expected_quota_bytes=quota,
        logical_capacity_bytes=SYSTEM_LOGICAL_CAPACITY,
        maximum_logical_capacity_bytes=SYSTEM_LOGICAL_CAPACITY)
    blockers = list(phase_evaluation.get("blockers", []))
    if projected_shared_free <= vm.VM_OPERATIONAL_WARNING_FREE:
        blockers.append("projected shared free after P2 growth and uncertainty is at or below the R4 8 GiB early-stop threshold")
    if projected_shared_free <= vm.VM_OPERATIONAL_UNRESERVED_FREE_MIN:
        blockers.append("projected shared free after P2 growth and uncertainty is at or below the R4 5 GiB hard floor")
    if projected_quota_headroom < 0:
        blockers.append("P2 growth and uncertainty exceed fresh VM-volume quota headroom")
    profile_mode = vm.VM_STOP_SAFETY_PROFILE_MODE_LIVE
    profile = None
    profile_sha = None
    if vm.VM_STOP_SAFETY_PROFILE_PATH.exists():
        profile, profile_blockers = vm._verify_stop_safety_profile(vm.VM_STOP_SAFETY_PROFILE_PATH)
        blockers.extend(profile_blockers)
        profile_mode = vm.VM_STOP_SAFETY_PROFILE_MODE_MEASURED
        profile_sha = vm._file_sha256(vm.VM_STOP_SAFETY_PROFILE_PATH)
        if profile is not None and (profile.get("volume_uuid") != VM_VOLUME_UUID
                                    or profile.get("g0c_identity") != vm.verify_fresh_g0c_review()):
            blockers.append("measured stop-safety profile binds a different VM volume or G0C identity")
    if preflight.get("g0c_identity") != vm.verify_fresh_g0c_review():
        blockers.append("G0C identity changed after the fresh signed P2 preflight")
    if preflight.get("active_marker_sha256") != vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH):
        blockers.append("active R2 P2 marker changed after the fresh signed R3 preflight")
    marker_snapshot, marker_snapshot_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", vm.policy._manifest_signing_key())
    if (marker_snapshot_sha != preflight.get("active_marker_sha256")
            or marker_snapshot.get("session_id") != lineage["session_id"]
            or marker_snapshot.get("state") != "ACTIVE"
            or marker_snapshot.get("phase_id") != "P2_AUTHORIZED"):
        blockers.append("same-session active-marker lineage changed after the fresh R3 preflight")
    p2_path = unique_path("R6-VM-P2-ADMISSION", run_id)
    marker_transition_path = unique_path("R6-VM-P2-ACTIVE-MARKER-TRANSITION", run_id)
    historical_r2_ancestry = {
        "admission_path": str(P2_R2_ADMISSION_PATH.relative_to(REPO)),
        "admission_sha256": P2_R2_ADMISSION_SHA256,
        "run_id": P2_R2_ADMISSION_PATH.stem.removeprefix("R6-VM-P2-ADMISSION-"),
        "session_id": P2_R2_SESSION_ID,
        "execution_blocker_receipt_path": str(P2_R2_EXECUTION_BLOCKER_PATH.relative_to(REPO)),
        "execution_blocker_receipt_sha256": P2_R2_EXECUTION_BLOCKER_SHA256,
    }
    control_token: bytes | None = os.urandom(32) if not blockers else None
    control_commitment = hashlib.sha256(control_token).hexdigest() if control_token is not None else None
    base_dir = VM_ROOT / "templates" / f"macos-base-{run_id}"
    system_path = base_dir / "system.raw"
    auxiliary_path = base_dir / "auxiliary.bin"
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
        "mode": vm.VM_MODE,
        "decision": "PASS" if not blockers else "FAIL",
        "authorized_phase": "P2" if not blockers else None,
        "authority_scope": "PROVISION_MACOS_GUEST_BASE_ONLY",
        "p2_authorized": True if not blockers else False,
        "vm_start_authorized_for_installation_only": True if not blockers else False,
        "runtime_feasibility_authorized": False,
        "r6_execution_authorized": False,
        "serial_runtime_testing_authorized": False,
        "execution_freshness_protocol": {
            "version": "P2_R4_EXECUTION_START_LEASE_V1",
            "static_admission_age_limited": False,
            "age_subject": "execution_start_lease",
            "maximum_start_age_seconds": P2_EXECUTION_START_MAX_AGE_SECONDS,
            "claim_boundary": "after_live_monitor_initial_gate_before_helper_authorization",
            "authority": P2_EXECUTION_START_AUTHORITY,
            "single_use": True,
            "active_installation_expires_with_lease": False,
        },
        "helper_control_token_sha256": control_commitment,
        "launch_capability": ({
            "format_version": P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
            "commitment_sha256": control_commitment,
            "session_id": lineage["session_id"],
            "private_store": P2_LAUNCH_CAPABILITY_STORE_ID,
            "path_derivation": "<session_id>/<admission_sha256>.ready",
            "state": "READY",
            "single_use": True,
            "claim_transition": "same-directory renameatx_np(RENAME_EXCL): .ready -> .claimed",
            "crash_after_claim": "CONSUMED_FAIL_CLOSED_REVIEWED_RECOVERY_REQUIRED",
        } if control_token is not None else None),
        "run_id": run_id,
        "session_id": lineage["session_id"],
        "created_at_unix": time.time(),
        "controller_identity": vm._controller_identity(),
        "g0c_identity": vm.verify_fresh_g0c_review(),
        "host": observation["host"],
        "p0_ancestry": {
            "creation_receipt_path": str(vm.VM_P0_CREATION_RECEIPT_PATH.relative_to(REPO)),
            "creation_receipt_sha256": lineage["creation_sha256"],
            "p0_admission_path": str(vm.VM_P0_ADMISSION_PATH.relative_to(REPO)),
            "p0_admission_sha256": lineage["p0_admission_sha256"],
            "p0_review_path": str(vm.VM_P0_REVIEW_PATH.relative_to(REPO)),
            "p0_review_sha256": vm.VM_P0_REVIEW_SHA256,
            "p1_admission_path": str(P1_ADMISSION_PATH.relative_to(REPO)),
            "p1_admission_sha256": lineage["p1_admission_sha256"],
        },
        "p1_final_verification_path": str(P1_FINAL_PATH.relative_to(REPO)),
        "restore_verification_sha256": P1_FINAL_SHA256,
        "p1_admission_sha256": P1_ADMISSION_SHA256,
        "p1_freshness_renewal_path": str(P1_RENEWAL_PATH.relative_to(REPO)),
        "p1_freshness_renewal_sha256": P1_RENEWAL_SHA256,
        "p1_first_body_evidence_path": str(P1_BODY_START_PATH.relative_to(REPO)),
        "p1_first_body_evidence_sha256": P1_BODY_START_SHA256,
        "p1_upstream_metadata_path": str(UPSTREAM_METADATA_PATH.relative_to(REPO)),
        "p1_upstream_metadata_file_sha256": UPSTREAM_METADATA_FILE_SHA256,
        "p1_upstream_metadata_sha256": UPSTREAM_METADATA_SHA256,
        "restore_subject_identity": RESTORE_SUBJECT_IDENTITY,
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": RESTORE_SHA256,
        "restore_logical_bytes": RESTORE_SIZE,
        "restore_file_identity": preflight["restore_file_identity"],
        "minimum_supported_cpu_count": plan["minimum_supported_cpu_count"],
        "minimum_supported_memory_size_bytes": plan["minimum_supported_memory_size_bytes"],
        "p2_preflight_path": str(preflight_path.relative_to(REPO)),
        "p2_preflight_sha256": vm._file_sha256(preflight_path),
        "r3_execution_packet_supersession": supersession_binding,
        "active_marker_before_sha256": preflight["active_marker_sha256"],
        "active_marker_transition_receipt_path": str(marker_transition_path.relative_to(REPO)),
        "historical_r2_ancestry": historical_r2_ancestry,
        "p2_identity_plan_path": str(plan_path.relative_to(REPO)),
        "p2_identity_plan_sha256": plan_sha,
        "hardware_model_data_base64": plan["hardware_model_data_base64"],
        "hardware_model_sha256": plan["hardware_model_sha256"],
        "p1_upstream_hardware_model_sha256": P1_UPSTREAM_HARDWARE_MODEL_SHA256,
        "p2_reconciliation_path": str(P2_RECONCILIATION_PATH.relative_to(REPO)),
        "p2_reconciliation_sha256": P2_RECONCILIATION_SHA256,
        "p2_reconciliation_review_path": str(P2_RECONCILIATION_REVIEW_PATH.relative_to(REPO)),
        "p2_reconciliation_review_sha256": P2_RECONCILIATION_REVIEW_SHA256,
        "p2_reconciliation_review_decision": reconciliation_info["review"]["decision"],
        "machine_identifier_data_base64": plan["machine_identifier_data_base64"],
        "machine_identifier_sha256": plan["machine_identifier_sha256"],
        "machine_identifier_creation": {
            **plan["machine_identifier_creation"],
            "identity_plan_path": str(plan_path.relative_to(REPO)),
            "identity_plan_sha256": plan_sha,
        },
        "provisioner_source_manifest_path": str(manifest_path),
        "provisioner_source_manifest_sha256": vm._file_sha256(manifest_path),
        "provisioner_review_path": str(review_path),
        "provisioner_review_sha256": vm._file_sha256(review_path),
        "provisioner_review_decision": review["decision"],
        "provisioner_source_sha256": manifest["source_manifest"]["source"]["sha256"],
        "provisioner_orchestration_source_sha256": manifest["source_manifest"]["orchestration_source"]["sha256"],
        "provisioner_binary_path": manifest["source_manifest"]["binary"]["path"],
        "provisioner_binary_sha256": manifest["source_manifest"]["binary"]["sha256"],
        "provisioner_code_directory_hash": manifest["source_manifest"]["binary"]["code_directory_hash"],
        "provisioner_entitlements": manifest["source_manifest"]["entitlements"]["readback"],
        "provisioner_entitlements_sha256": manifest["source_manifest"]["entitlements"]["sha256"],
        "provisioner_binary_file_identity": manifest["source_manifest"]["binary"]["file_identity"],
        "vm_volume_uuid": VM_VOLUME_UUID,
        "vm_volume_quota_bytes": quota,
        "vm_volume_reserve_bytes": 0,
        "safety_volume_uuid": SAFETY_VOLUME_UUID,
        "safety_volume_reserve_bytes": 15 * GIB,
        "safety_container_uuid": CONTAINER_UUID,
        "safety_physical_store_uuid": PHYSICAL_STORE_UUID,
        "vm_volume_usage_bytes": used,
        "vm_quota_headroom_bytes": quota - used,
        "shared_container_free_bytes": measurement["shared_container_free_bytes"],
        "unreserved_free_ceiling_bytes": contract["unreserved_free_ceiling_bytes"],
        "p2_component_maxima_bytes": {
            "guest_system_user_file_data": P2_SYSTEM_MAX,
            "auxiliary_storage_user_file_data": P2_AUX_MAX,
            "p1_p2_provisioning_metadata": P2_METADATA_MAX,
        },
        "p1_p2_metadata_allocated_at_admission_bytes": measurement["p1_p2_metadata_allocated_bytes"],
        "p1_p2_metadata_remaining_at_admission_bytes": metadata_remaining,
        "p2_growth_bytes": p2_growth,
        "p2_uncertainty_bytes": uncertainty,
        "p2_uncertainty_basis": {
            "formula": "max(256 MiB, ceil(10 percent * total remaining P2 envelope at fresh preflight))",
            "growth_bytes": full_remaining_growth,
            "rate_basis_points": vm.VM_PHASE_UNCERTAINTY_BASIS_POINTS,
        },
        "p2_metadata_finalization_reserve_bytes": P2_METADATA_FINALIZATION_RESERVE,
        "p2_projected_total_demand_bytes": total_demand,
        "p2_projected_shared_free_after_demand_bytes": projected_shared_free,
        "p2_projected_quota_headroom_after_demand_bytes": projected_quota_headroom,
        "r4_early_stop_threshold_bytes": vm.VM_OPERATIONAL_WARNING_FREE,
        "r4_hard_operational_floor_bytes": vm.VM_OPERATIONAL_UNRESERVED_FREE_MIN,
        "capacity_phase_evaluation": {**phase_evaluation, "projected_shared_free_after_demand_bytes": projected_shared_free,
                                       "projected_quota_headroom_after_demand_bytes": projected_quota_headroom},
        "storage_observation": observation,
        "stop_safety_profile_mode": profile_mode,
        "stop_safety_profile": profile,
        "stop_safety_profile_sha256": profile_sha,
        "system_raw_path": str(system_path),
        "auxiliary_storage_path": str(auxiliary_path),
        "system_raw_logical_capacity_bytes": SYSTEM_LOGICAL_CAPACITY,
        "system_raw_physical_allocation_ceiling_bytes": P2_SYSTEM_MAX,
        "auxiliary_physical_allocation_ceiling_bytes": P2_AUX_MAX,
        "live_storage_monitor": {
            "controller_function": "p2_base_orchestrator._run_p2_live_monitor",
            "sample_interval_seconds": 0.1,
            "maximum_allowed_gap_seconds": vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS,
            "sample_source": "fresh diskutil APFS plist plus kernel mount table, crosschecked against signed P2 baseline Data readback",
            "file_cap_checks": ["system.raw physical allocation <= 17 GiB",
                                "auxiliary.bin physical allocation <= 1 GiB",
                                "combined P1/P2 provisioning metadata <= 32 MiB"],
        },
        "stop_callback": {
            "cancel_operation": "VZMacOSInstaller.progress.cancel() only after installer invocation; before start, abort asset creation",
            "stop_vm": "record stop request; VZVirtualMachine.stop() only after installer operation quiescence",
            "quiescence_timeout_seconds": vm.VM_PHASE_STOP_TIMEOUT_SECONDS,
        },
        "handle_release_procedure": ["await installer completion or cancellation quiescence",
                                     "verify VZVirtualMachine.state == stopped",
                                     "release VZMacOSInstaller, VZVirtualMachine, configuration, and disk attachment",
                                     "wait for helper process exit before sealing"],
        "configuration_schema": manifest["configuration_schema"],
        "prohibited_authority": ["runtime-feasibility acceptance", "R6 execution", "BuildLease",
                                 "serial testing", "P3/runtime work"],
        "blockers": blockers,
        "warnings": ["P2 authorizes stock macOS base installation only; guest version is not independently read back",
                     "no network, directory sharing, socket bridge, USB controller, serial, or R6 source is configured"],
    }
    if blockers:
        signed_write(p2_path, record, "vm_admission_hmac_sha256")
        raise RuntimeError("fresh signed P2 admission failed: " + "; ".join(blockers))
    admission_signing_key = vm.policy._manifest_signing_key()
    admission_sha: str | None = None
    admission_observed_sha: str | None = None
    admission_hash_failure: str | None = None
    admission_readback_status = "NOT_ATTEMPTED"
    admission_write_attempted = False
    admission_write_returned = False
    failure_stage = "signed_pass_admission_write"
    expected_admission_payload = dict(record)
    expected_admission_payload.pop("vm_admission_hmac_sha256", None)
    marker_before: dict[str, Any] | None = None
    marker_before_sha: str | None = None
    marker_after: dict[str, Any] | None = None
    marker_after_sha: str | None = None
    marker_failure: str | None = None
    admission_slot_lock = None
    try:
        if control_token is None:
            raise RuntimeError("passing P2 admission has no newly generated launch capability")
        admission_slot_lock = _acquire_r3_admission_issuance_lock(lineage["session_id"])
        _assert_single_r3_pass_admission(lineage["session_id"])
        failure_stage = "private_launch_capability_persist"
        prepared_admission_sha, capability_path = _prepare_admission_launch_capability(
            record, admission_signing_key, control_token)
        admission_write_attempted = True
        failure_stage = "signed_pass_admission_write"
        signed_write(p2_path, record, "vm_admission_hmac_sha256", admission_signing_key)
        admission_write_returned = True
        failure_stage = "post_pass_admission_readback"
        admitted, readback_sha = read_signed_with_key(
            p2_path, "vm_admission_hmac_sha256", admission_signing_key)
        admission_observed_sha = readback_sha
        admitted_payload = dict(admitted)
        admitted_payload.pop("vm_admission_hmac_sha256", None)
        if (admitted.get("decision") != "PASS" or admitted.get("authorized_phase") != "P2"
                or admitted.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
                or admitted.get("helper_control_token_sha256") != control_commitment
                or admitted.get("session_id") != lineage["session_id"]
                or readback_sha != prepared_admission_sha
                or admitted_payload != expected_admission_payload):
            raise RuntimeError("fresh signed P2 admission readback failed before in-memory launch authorization")
        admission_sha = readback_sha
        admission_readback_status = "EXACT_PASS_READBACK"
        failure_stage = "active_marker_update"
        marker_before, marker_before_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
        if (marker_before_sha != preflight["active_marker_sha256"]
                or marker_before.get("session_id") != lineage["session_id"]
                or marker_before.get("state") != "ACTIVE"
                or marker_before.get("phase_id") != "P2_AUTHORIZED"):
            raise RuntimeError("same-session active marker no longer matches the signed R3 preflight")
        failure_stage = "active_marker_readback"
        marker_after, marker_after_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
        if (marker_after_sha != marker_before_sha
                or marker_after != marker_before
                or marker_after.get("session_id") != lineage["session_id"]
                or marker_after.get("phase_id") != "P2_AUTHORIZED"
                or marker_after.get("state") != "ACTIVE"):
            raise RuntimeError("active marker changed while the R3 admission was being published")
        failure_stage = "active_marker_transition_receipt"
        transition_record = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_ACTIVE_MARKER_TRANSITION_RECONCILIATION",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "PASS",
            "authority_scope": "NONE",
            "execution_authority": "NONE",
            "cross_session_transition_protocol_added": False,
            "transition_protocol": "no-op: retain the exact authenticated active marker; the new admission is separately bound to its digest",
            "session_id_role": "AUTHENTICATED_ACTIVE_MARKER_LINEAGE_ONLY",
            "new_admission_path": str(p2_path.relative_to(REPO)),
            "new_admission_sha256": admission_sha,
            "new_admission_session_id": lineage["session_id"],
            "historical_r2_ancestry": historical_r2_ancestry,
            "p1_admission_path": str(P1_ADMISSION_PATH.relative_to(REPO)),
            "p1_admission_sha256": P1_ADMISSION_SHA256,
            "prior_active_marker": {key: marker_before.get(key) for key in (
                "session_id", "volume_uuid", "state", "phase_id", "p1_admission_sha256",
                "committed_growth_bytes")},
            "active_marker_before_sha256": marker_before_sha,
            "resulting_active_marker": {key: marker_after.get(key) for key in (
                "session_id", "volume_uuid", "state", "phase_id", "p1_admission_sha256",
                "committed_growth_bytes")},
            "active_marker_after_sha256": marker_after_sha,
            "receipt_note": "Additive lineage evidence only; the R3 PASS admission and its exact private launch capability carry the P2 execution authority.",
        }
        marker_receipt_check, marker_receipt_check_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
        if (marker_receipt_check_sha != marker_before_sha
                or marker_receipt_check != marker_before):
            raise RuntimeError("active marker changed before its R3 no-op lineage receipt was signed")
        signed_write(marker_transition_path, transition_record,
                     "vm_p2_active_marker_transition_hmac_sha256", admission_signing_key)
        transition_readback, transition_sha = read_signed_with_key(
            marker_transition_path, "vm_p2_active_marker_transition_hmac_sha256", admission_signing_key)
        transition_payload = dict(transition_readback)
        transition_payload.pop("vm_p2_active_marker_transition_hmac_sha256", None)
        if (transition_payload != transition_record
                or transition_readback.get("new_admission_sha256") != admission_sha
                or transition_readback.get("active_marker_after_sha256") != marker_after_sha):
            raise RuntimeError("signed R3 active-marker transition receipt failed exact readback")
        marker_final, marker_final_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
        if marker_final_sha != marker_before_sha or marker_final != marker_before:
            raise RuntimeError("active marker changed during R3 no-op lineage receipt publication")
        admission_slot_lock.close()
        admission_slot_lock = None
        failure_stage = "preflight_output"
        print(json.dumps({"admission_path": str(p2_path), "admission_sha256": admission_sha,
                          "preflight_path": str(preflight_path), "preflight_sha256": vm._file_sha256(preflight_path),
                          "active_marker_transition_receipt_path": str(marker_transition_path),
                          "active_marker_transition_receipt_sha256": transition_sha,
                          "launch_capability_path": str(capability_path),
                          "launch_capability_commitment_sha256": control_commitment,
                          "status": "PASS", "projected_shared_free": projected_shared_free,
                          "projected_quota_headroom": projected_quota_headroom}, sort_keys=True))
        if getattr(args, "admit_only", False):
            return
        failure_stage = "provision"
        _resume_existing_admission(argparse.Namespace(admission=str(p2_path), validate_only=False))
    except BaseException as error:
        if admission_slot_lock is not None:
            admission_slot_lock.close()
            admission_slot_lock = None
        if admission_sha is None:
            try:
                observed_admission, readback_sha = read_signed_with_key(
                    p2_path, "vm_admission_hmac_sha256", admission_signing_key)
                admission_observed_sha = readback_sha
                observed_payload = dict(observed_admission)
                observed_payload.pop("vm_admission_hmac_sha256", None)
                if observed_payload == expected_admission_payload:
                    admission_sha = readback_sha
                    admission_readback_status = "EXACT_PASS_READBACK_AFTER_EXCEPTION"
                else:
                    admission_readback_status = "SIGNED_RECORD_MISMATCH"
            except BaseException as readback_error:
                admission_readback_status = "UNVERIFIED_READBACK"
                admission_hash_failure = str(readback_error)
                try:
                    admission_observed_sha = vm._file_sha256(p2_path)
                    admission_readback_status = "HASH_ONLY_UNVERIFIED"
                except BaseException as digest_error:
                    admission_hash_failure += "; best-effort hash failed: " + str(digest_error)
                    admission_readback_status = "DIGEST_UNAVAILABLE"
        try:
            marker_before, marker_before_sha = read_signed_with_key(
                vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
            marker_after, marker_after_sha = read_signed_with_key(
                vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", admission_signing_key)
        except BaseException as marker_error:
            marker_failure = str(marker_error)
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_ADMISSION_START_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": failure_stage,
            "error": str(error),
            "p2_admission_path": str(p2_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "p2_admission_observed_sha256": admission_observed_sha,
            "p2_admission_hash_read_error": admission_hash_failure,
            "p2_admission_readback_status": admission_readback_status,
            "p2_admission_write_attempted": admission_write_attempted,
            "p2_admission_write_returned": admission_write_returned,
            "p2_admission_decision": record["decision"],
            "session_id": lineage["session_id"],
            "helper_control_token_sha256": record["helper_control_token_sha256"],
            "active_marker_before_failure": marker_before,
            "active_marker_before_failure_sha256": marker_before_sha,
            "active_marker_after_failure": marker_after,
            "active_marker_after_failure_sha256": marker_after_sha,
            "active_marker_fail_closed_error": marker_failure,
            "guest_assets_created": os.path.lexists(base_dir),
            "guest_asset_directory": str(base_dir),
        }
        failure_path = unique_path("R6-VM-P2-ADMISSION-START-FAILURE", run_id)
        signed_write(failure_path, failure, "vm_p2_admission_start_failure_hmac_sha256",
                     admission_signing_key)
        raise RuntimeError("P2 admission/readback/provision failed; signed failure evidence recorded") from error


class HelperProcess:
    def __init__(self, process: SpawnedChild, log_path: Path, stderr_path: Path):
        self.process = process
        self.log_path = log_path
        self.stderr_path = stderr_path
        self.events: list[dict[str, Any]] = []
        self.reader_errors: list[str] = []
        self.condition = threading.Condition()
        self.writer_lock = threading.Lock()
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        try:
            with self.log_path.open("x", encoding="utf-8") as stream:
                for line in self.process.stdout or []:
                    stream.write(line)
                    stream.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"event": "non_json_stdout", "text": line[:2000]}
                    with self.condition:
                        self.events.append(event)
                        self.condition.notify_all()
        except BaseException as error:
            self.reader_errors.append(f"stdout reader: {error}")
        finally:
            with self.condition:
                self.condition.notify_all()

    def _read_stderr(self) -> None:
        try:
            with self.stderr_path.open("x", encoding="utf-8") as stream:
                for line in self.process.stderr or []:
                    stream.write(line)
                    stream.flush()
        except BaseException as error:
            self.reader_errors.append(f"stderr reader: {error}")

    def events_named(self, name: str) -> list[dict[str, Any]]:
        with self.condition:
            return [row for row in self.events if row.get("event") == name]

    def wait_for_event(self, name: str, timeout: float,
                       predicate: Any | None = None) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                matches = [row for row in self.events if row.get("event") == name
                           and (predicate is None or predicate(row))]
                if matches:
                    return matches[-1]
                if self.process.poll() is not None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(min(remaining, 0.1))

    def wait_for_any_event(self, names: set[str], timeout: float,
                           predicate: Any | None = None) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                matches = [row for row in self.events if row.get("event") in names
                           and (predicate is None or predicate(row))]
                if matches:
                    return matches[-1]
                if self.process.poll() is not None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(min(remaining, 0.05))

    def terminal(self) -> bool:
        # Keep the live capacity monitor sampling through the VM's actual stop,
        # handle release, and helper exit. Installer completion alone is not
        # the end of the writable phase.
        return self.process.poll() is not None

    def send(self, command: str) -> bool:
        if self.process.poll() is not None:
            return False
        if self.process.stdin is None:
            raise BrokenPipeError("P2 helper control pipe is unavailable while the process is running")
        with self.writer_lock:
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                if self.process.poll() is None:
                    raise
                return False
        return True

    def wait_terminal(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.terminal():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.1))
        return True

    def wait_released_and_exit(self, timeout: float, *, allow_no_vm_created: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            handles = self.events_named("handles_released")
            if handles and self.process.poll() is not None:
                row = handles[-1]
                allowed_states = {"STOPPED"}
                if allow_no_vm_created:
                    allowed_states.add("NO_VM_CREATED")
                if (row.get("vm_state") in allowed_states
                        and row.get("all_writable_handles_released") is True
                        and row.get("installer_object_released") is True
                        and row.get("disk_attachment_released") is True):
                    return True
            time.sleep(0.05)
        return False

    def join(self, *, reader_timeout: float | None = None,
             process_timeout: float | None = None) -> int:
        returncode = self.process.wait(timeout=process_timeout)
        self.stdout_thread.join(timeout=reader_timeout)
        self.stderr_thread.join(timeout=reader_timeout)
        if self.stdout_thread.is_alive() or self.stderr_thread.is_alive():
            raise RuntimeError("P2 helper output reader thread remained alive after process exit and join")
        return returncode


def _live_observation_from_baseline(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh quota/free and kernel mount identity with one APFS plist sample."""
    apfs = vm._run_plist([vm._diskutil(), "apfs", "list", "-plist"])
    containers = apfs.get("Containers")
    if not isinstance(containers, list):
        raise RuntimeError("live monitor APFS plist omitted Containers")
    matches = [row for row in containers if isinstance(row, Mapping)
               and row.get("APFSContainerUUID") == CONTAINER_UUID]
    if len(matches) != 1:
        raise RuntimeError("live monitor APFS container identity is absent or ambiguous")
    container = matches[0]
    mount_records = vm._mounted_filesystem_table()
    volumes = container.get("Volumes")
    if not isinstance(volumes, list):
        raise RuntimeError("live monitor APFS volume list is missing")
    data_rows = [row for row in volumes if isinstance(row, Mapping)
                 and row.get("APFSVolumeUUID") == vm.EXPECTED_DATA_VOLUME_UUID]
    safety_rows = [row for row in volumes if isinstance(row, Mapping)
                   and row.get("APFSVolumeUUID") == SAFETY_VOLUME_UUID]
    if len(data_rows) != 1 or len(safety_rows) != 1:
        raise RuntimeError("live monitor Data or safety volume identity is absent or ambiguous")
    data_mount = vm._capture_volume_info(container, data_rows[0], mount_records,
                                         expected_mount_location=vm.EXPECTED_DATA_MOUNT)
    safety_info = vm._capture_volume_info(container, safety_rows[0], mount_records)
    result = copy.deepcopy(dict(baseline))
    result["captured_at_unix"] = time.time()
    result["apfs_container"] = dict(container)
    data_info = dict(result.get("data_volume_info", {}))
    data_info["canonical_mount_state"] = data_mount.get("mount_state")
    data_info["canonical_mount_identity"] = data_mount.get("mount_identity")
    data_info["canonical_device_identifier"] = data_mount.get("DeviceIdentifier")
    result["data_volume_info"] = data_info
    result["safety_volume_info"] = safety_info
    result["mounted_filesystem_table_sha256"] = vm._sha256(vm.policy._canonical_pretty_json_bytes(mount_records))
    crosschecks = dict(result.get("crosschecks", {}))
    crosschecks["apfs_container_free_bytes"] = container.get("CapacityFree")
    # Keep the signed-baseline Data readback as a conservative independent crosscheck.
    # APFS current free is always the lower side on a decline; a >1 GiB disagreement fails closed.
    crosschecks["data_info_container_free_bytes"] = data_info.get("APFSContainerFree")
    result["crosschecks"] = crosschecks
    result["vm_volume_mount"] = vm._volume_mount_info(result, VM_VOLUME_UUID)
    return result


def _validate_execution_vm_mount(observation: Mapping[str, Any],
                                 expected_mount: Mapping[str, Any]) -> dict[str, Any]:
    """Require every live P2 sample to retain the lease-bound VM mount mapping."""
    if (not isinstance(expected_mount, Mapping)
            or expected_mount.get("mount_state") != "MOUNTED_VERIFIED"
            or expected_mount.get("mounted") is not True):
        raise RuntimeError("P2 execution-start lease lacks a verified mounted VM-volume identity")
    current = observation.get("vm_volume_mount")
    if not isinstance(current, Mapping):
        raise RuntimeError("live P2 sample lacks a kernel-verified VM-volume mount identity")
    if vm._mount_identity_key(current) != vm._mount_identity_key(expected_mount):
        raise RuntimeError("P2 VM volume device, UUID, container, or kernel mount identity changed after lease issuance")
    return current


def _run_p2_live_monitor(*, admission_path: Path, admission_sha256: str,
                         lease_path: Path, lease: Mapping[str, Any],
                         lease_sha256: str, issued_at_monotonic: float,
                         signing_key: bytes, session_id: str,
                         initial_quota_used_bytes: int,
                         maximum_phase_growth_bytes: int,
                         sample_observation: Callable[[], Mapping[str, Any]],
                         operation_done: Callable[[], bool],
                         cancel_operation: Callable[[], None],
                         wait_operation_quiescent: Callable[[float], bool],
                         stop_vm: Callable[[], None],
                         wait_vm_stopped_and_handles_released: Callable[[float], bool],
                         host_monitor_ready: threading.Event,
                         monitor_ready_evidence: dict[str, Any],
                         monitor_state: dict[str, Any],
                         execution_claimed_state: Mapping[str, bool],
                         boundary_lock: Any,
                         poll_interval_seconds: float = 0.001) -> dict[str, Any]:
    """Run the adapter-owned P2 monitor from pre-claim gate through stopped proof."""
    if (not isinstance(initial_quota_used_bytes, int)
            or isinstance(initial_quota_used_bytes, bool)
            or initial_quota_used_bytes < 0
            or not isinstance(maximum_phase_growth_bytes, int)
            or isinstance(maximum_phase_growth_bytes, bool)
            or maximum_phase_growth_bytes < 0
            or not isinstance(poll_interval_seconds, (int, float))
            or isinstance(poll_interval_seconds, bool)
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
            or poll_interval_seconds > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
        raise RuntimeError("P2 execution monitor requires finite lease-bound budgets and cadence")

    if not isinstance(lease, Mapping):
        raise RuntimeError("P2 execution monitor requires a signed execution-start lease")

    canonical_admission_path = Path(admission_path).resolve(strict=True)
    admission, admission_sha = read_signed_with_key(
        canonical_admission_path, "vm_admission_hmac_sha256", signing_key)
    if (admission_sha != admission_sha256
            or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("session_id") != session_id
            or admission.get("vm_volume_uuid") != VM_VOLUME_UUID
            or not isinstance(admission.get("p2_growth_bytes"), int)
            or isinstance(admission.get("p2_growth_bytes"), bool)
            or maximum_phase_growth_bytes > admission["p2_growth_bytes"]
            or admission.get("controller_identity") != vm._controller_identity()):
        raise RuntimeError("P2 monitor static admission does not authorize this exact lease-bound phase")
    # The signed admission is durable reviewed authority. Its creation time is
    # intentionally not the mutable execution-start freshness subject.
    if (lease.get("record_type") != "R6_VM_P2_EXECUTION_START_LEASE"
            or lease.get("protocol_version") != "P2_R4_EXECUTION_START_LEASE_V1"
            or lease.get("authority") != P2_EXECUTION_START_AUTHORITY
            or lease.get("authority_scope") != "EXACT_ADMISSION_STARTUP_ONLY"
            or lease.get("status") != "ISSUED"
            or lease.get("single_use") is not True
            or lease.get("maximum_age_seconds") != P2_EXECUTION_START_MAX_AGE_SECONDS
            or lease.get("static_admission_age_limited") is not False
            or lease.get("admission_path") != str(canonical_admission_path.relative_to(REPO.resolve()))
            or lease.get("admission_sha256") != admission_sha
            or lease.get("session_id") != session_id
            or lease.get("active_marker_sha256") is None
            or lease.get("storage_measurement", {}).get("vm_volume_usage_bytes")
                != initial_quota_used_bytes
            or lease.get("p2_demand", {}).get("p2_growth_bytes")
                != maximum_phase_growth_bytes):
        raise RuntimeError("P2 monitor requires the exact signed execution-start lease")
    lease_record, current_lease_sha = read_signed_with_key(
        Path(lease_path).resolve(strict=True), P2_EXECUTION_START_SIGNATURE_FIELD, signing_key)
    if lease_record != dict(lease) or current_lease_sha != lease_sha256:
        raise RuntimeError("P2 execution-start lease changed before monitor initialization")
    lease_age_ok, lease_age = _execution_start_age_valid(
        lease.get("issued_at_unix"), issued_at_monotonic=issued_at_monotonic)
    if not lease_age_ok:
        raise RuntimeError(f"P2 execution-start lease expired before monitor initialization: {lease_age}")
    current_g0c = vm.verify_fresh_g0c_review()
    if admission.get("g0c_identity") != current_g0c or lease.get("g0c_identity") != current_g0c:
        raise RuntimeError("P2 monitor static admission or execution lease does not bind current G0C evidence")
    active, active_sha = read_signed_with_key(
        vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
    if (active_sha != lease.get("active_marker_sha256")
            or active.get("session_id") != session_id
            or active.get("volume_uuid") != VM_VOLUME_UUID
            or active.get("state") != "ACTIVE"
            or active.get("phase_id") != "P2_AUTHORIZED"):
        raise RuntimeError("P2 monitor requires the exact active marker bound by its execution lease")

    profile_mode = admission.get("stop_safety_profile_mode")
    profile_path = vm.VM_STOP_SAFETY_PROFILE_PATH
    profile_absent = vm._path_entry_is_absent(profile_path)
    safety_profile: dict[str, Any] | None = None
    if not profile_absent:
        safety_profile, safety_blockers = vm._verify_stop_safety_profile(profile_path)
        if safety_blockers or safety_profile is None:
            raise RuntimeError("P2 writes cannot start with an invalid stop-safety profile: "
                               + "; ".join(safety_blockers))
        if safety_profile.get("volume_uuid") != VM_VOLUME_UUID:
            raise RuntimeError("P2 stop-safety profile is measured against a different VM volume")
    if profile_mode == vm.VM_STOP_SAFETY_PROFILE_MODE_MEASURED:
        if (safety_profile is None
                or admission.get("stop_safety_profile_sha256") != vm._file_sha256(profile_path)
                or admission.get("stop_safety_profile") != safety_profile):
            raise RuntimeError("P2 stop-safety profile differs from the signed static admission")
    elif profile_mode == vm.VM_STOP_SAFETY_PROFILE_MODE_LIVE:
        if safety_profile is not None or admission.get("stop_safety_profile_sha256") is not None:
            raise RuntimeError("P2 live-capacity admission unexpectedly binds a measured stop-safety profile")
    else:
        raise RuntimeError("P2 admission has an unknown stop-safety profile mode")

    expected_mount = lease.get("storage_measurement", {}).get("vm_mount")
    if (not isinstance(expected_mount, Mapping)
            or expected_mount.get("mount_state") != "MOUNTED_VERIFIED"
            or expected_mount.get("mounted") is not True):
        raise RuntimeError("P2 execution lease lacks a verified mounted VM-volume identity")
    maximum_gap_seconds = (
        float(safety_profile["maximum_monitor_gap_microseconds"]) / 1_000_000
        if safety_profile is not None else vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS)
    samples: list[dict[str, Any]] = []
    last_sampled_at_monotonic: float | None = None
    last_quota_used = initial_quota_used_bytes
    last_shared_free: int | None = None
    validated_count = int(monitor_ready_evidence.get("validated_sample_count", 0))
    failed_sample: dict[str, Any] | None = None

    def take_sample() -> tuple[dict[str, Any], Mapping[str, Any], float]:
        nonlocal last_quota_used, last_shared_free, last_sampled_at_monotonic
        observation = sample_observation()
        current_mount = _validate_execution_vm_mount(observation, expected_mount)
        checked = vm._check_runtime_phase_sample(
            observation, volume_uuid=VM_VOLUME_UUID,
            initial_quota_used_bytes=initial_quota_used_bytes,
            maximum_phase_growth_bytes=maximum_phase_growth_bytes,
            concurrent_growth_bytes=None, session_id=session_id)
        sampled_at_monotonic = time.monotonic()
        gap = (sampled_at_monotonic - last_sampled_at_monotonic
               if last_sampled_at_monotonic is not None else 0.0)
        blockers = list(checked.get("blockers", []))
        if last_sampled_at_monotonic is not None and gap > maximum_gap_seconds:
            blockers.append("actual P2 execution monitor gap exceeded its authorized 250 ms/profile bound")
        if not checked.get("admitted") and not blockers:
            blockers.append("P2 execution monitor capacity policy did not admit the live sample")
        previous_quota_used = last_quota_used
        if checked.get("vm_volume_used_bytes") is not None:
            last_quota_used = checked["vm_volume_used_bytes"]
        current_free = checked.get("shared_container_free_bytes")
        if last_shared_free is not None and isinstance(current_free, int):
            total_growth = max(0, last_shared_free - current_free)
            vm_growth = max(0, (checked.get("vm_volume_used_bytes") or previous_quota_used)
                            - previous_quota_used)
            checked["observed_concurrent_growth_bytes"] = max(0, total_growth - vm_growth)
            checked["concurrent_growth_authority"] = "DIAGNOSTIC_ONLY"
        last_shared_free = current_free if isinstance(current_free, int) else None
        checked["monitor_sampled_at_unix"] = time.time()
        checked["monitor_gap_seconds"] = gap
        checked["vm_mount_identity"] = dict(current_mount)
        checked["execution_start_lease_sha256"] = lease_sha256
        checked["blockers"] = sorted(set(blockers))
        checked["admitted"] = not blockers and bool(checked.get("admitted"))
        last_sampled_at_monotonic = sampled_at_monotonic
        return checked, observation, sampled_at_monotonic

    while True:
        operation_complete = False
        with boundary_lock:
            try:
                if (not execution_claimed_state.get("value", False)
                        and not _execution_start_age_valid(
                            lease.get("issued_at_unix"),
                            issued_at_monotonic=issued_at_monotonic)[0]):
                    raise RuntimeError("P2 execution-start lease expired before atomic capability claim")
                checked, observation, sampled_at = take_sample()
                samples.append(checked)
                if checked["blockers"]:
                    failed_sample = checked
                else:
                    try:
                        operation_complete = operation_done()
                    except BaseException as error:
                        failed_sample = {
                            "monitor_sampled_at_unix": time.time(),
                            "monitor_gap_seconds": 0.0,
                            "blockers": [f"P2 monitor operation state is unknown: {error}"],
                            "admitted": False,
                        }
                        samples.append(failed_sample)
                    if failed_sample is None:
                        validated_count = max(
                            validated_count + 1,
                            int(monitor_ready_evidence.get("validated_sample_count", 0)))
                        monitor_ready_evidence.update({
                            "validated_sample_count": validated_count,
                            "last_validated_at_unix": checked["monitor_sampled_at_unix"],
                            "last_validated_at_monotonic": sampled_at,
                            "last_vm_mount": dict(observation["vm_volume_mount"]),
                        })
                        if validated_count >= 3 and not execution_claimed_state.get("value", False):
                            host_monitor_ready.set()
            except BaseException as error:
                failed_sample = {
                    "monitor_sampled_at_unix": time.time(),
                    "monitor_gap_seconds": 0.0,
                    "blockers": [f"P2 monitor measurement failed: {error}"],
                    "admitted": False,
                }
                samples.append(failed_sample)
            if failed_sample is not None:
                monitor_state["error"] = RuntimeError(
                    "; ".join(failed_sample.get("blockers", ["P2 monitor failed"])))
        if failed_sample is not None or operation_complete:
            break
        time.sleep(float(poll_interval_seconds))

    if failed_sample is None:
        try:
            final_sample, _observation, _sampled_at = take_sample()
            samples.append(final_sample)
            if final_sample["blockers"]:
                failed_sample = final_sample
        except BaseException as error:
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "monitor_gap_seconds": 0.0,
                "blockers": [f"P2 final monitor measurement failed: {error}"],
                "admitted": False,
            }
            samples.append(failed_sample)

    stop_measurement: dict[str, Any] = {}
    if failed_sample is None:
        stop_started = time.monotonic()
        try:
            phase_quiescent = bool(wait_operation_quiescent(vm.VM_PHASE_STOP_TIMEOUT_SECONDS))
        except BaseException as error:
            phase_quiescent = False
            stop_measurement["phase_quiescence_failure"] = str(error)
        try:
            stop_vm()
            stop_measurement["vm_stop_requested"] = True
        except BaseException as error:
            stop_measurement["vm_stop_requested"] = False
            stop_measurement["vm_stop_failure"] = str(error)
        try:
            vm_stopped = bool(wait_vm_stopped_and_handles_released(
                vm.VM_PHASE_STOP_TIMEOUT_SECONDS))
        except BaseException as error:
            vm_stopped = False
            stop_measurement["vm_stop_proof_failure"] = str(error)
        stop_elapsed = time.monotonic() - stop_started
        stop_latency_us = math.ceil(stop_elapsed * 1_000_000)
        stop_measurement.update({
            "phase_quiescent": phase_quiescent,
            "vm_stopped_and_handles_released": vm_stopped,
            "stop_latency_microseconds": stop_latency_us,
        })
        if (safety_profile is not None
                and stop_latency_us > safety_profile["maximum_stop_latency_microseconds"]):
            vm_stopped = False
            stop_measurement["vm_stopped_and_handles_released"] = False
            stop_measurement["failure"] = "measured stop latency exceeded the validated profile"
        if not phase_quiescent or not vm_stopped:
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "monitor_gap_seconds": 0.0,
                "blockers": ["P2 completion did not prove writer quiescence, stopped state, and handle release"],
                "admitted": False,
            }
        else:
            try:
                stopped_sample, _observation, _sampled_at = take_sample()
                previous = samples[-1] if samples else {}
                previous_free = previous.get("shared_container_free_bytes")
                previous_used = previous.get("vm_volume_used_bytes")
                if (isinstance(previous_free, int) and isinstance(previous_used, int)
                        and isinstance(stopped_sample.get("shared_container_free_bytes"), int)
                        and isinstance(stopped_sample.get("vm_volume_used_bytes"), int)):
                    total_growth = max(0, previous_free - stopped_sample["shared_container_free_bytes"])
                    vm_growth = max(0, stopped_sample["vm_volume_used_bytes"] - previous_used)
                    stop_measurement["concurrent_growth_during_stop_bytes"] = max(0, total_growth - vm_growth)
                else:
                    stop_measurement["concurrent_growth_during_stop_bytes"] = None
                samples.append(stopped_sample)
                if stopped_sample["blockers"]:
                    failed_sample = stopped_sample
            except BaseException as error:
                failed_sample = {
                    "monitor_sampled_at_unix": time.time(),
                    "monitor_gap_seconds": 0.0,
                    "blockers": [f"P2 post-stop storage measurement failed: {error}"],
                    "admitted": False,
                }
                samples.append(failed_sample)

    if failed_sample is None:
        current_marker, current_marker_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        if (current_marker_sha != active_sha or current_marker != active):
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "monitor_gap_seconds": 0.0,
                "blockers": ["P2 active marker changed before monitor completion"],
                "admitted": False,
            }

    if failed_sample is None:
        result = {
            "schema_version": 1,
            "mode": vm.VM_MODE,
            "session_id": session_id,
            "phase_id": "P2",
            "status": "PASS_PHASE_COMPLETE",
            "host_build_version": (safety_profile["host_build_version"]
                                    if safety_profile is not None else vm._host_build_version()),
            "volume_uuid": VM_VOLUME_UUID,
            "safety_volume_uuid": active.get("safety_volume_uuid"),
            "safety_reserve_bytes": active.get("safety_reserve_bytes"),
            "controller_identity": vm._controller_identity(),
            "g0c_identity": current_g0c,
            "phase_quiescent": True,
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
            "stop_measurement": stop_measurement,
            "stop_safety_profile_mode": profile_mode,
            "p2_admission_sha256": admission_sha,
            "execution_start_lease_sha256": lease_sha256,
            "execution_start_claim_boundary": copy.deepcopy(
                monitor_ready_evidence.get("execution_start_claim_boundary")),
            "samples": samples,
            "sample_count": len(samples),
            "maximum_observed_monitor_gap_seconds": max(
                (float(row.get("monitor_gap_seconds", 0.0)) for row in samples), default=0.0),
        }
        vm.update_active_session_marker(
            session_id, phase_id="P2_COMPLETE", committed_growth_bytes=0,
            expected_marker_sha256=active_sha)
        return vm._seal(result, "vm_phase_monitor_hmac_sha256")

    callbacks: list[str] = []
    stop_started = time.monotonic()
    try:
        cancel_operation()
        callbacks.append("cancel_requested")
    except BaseException as error:
        callbacks.append(f"cancel_failed:{error}")
    try:
        stop_vm()
        callbacks.append("vm_stop_requested")
    except BaseException as error:
        callbacks.append(f"vm_stop_failed:{error}")
    try:
        phase_quiescent = bool(wait_operation_quiescent(vm.VM_PHASE_STOP_TIMEOUT_SECONDS))
    except BaseException as error:
        phase_quiescent = False
        callbacks.append(f"phase_quiescence_check_failed:{error}")
    try:
        vm_stopped = bool(wait_vm_stopped_and_handles_released(vm.VM_PHASE_STOP_TIMEOUT_SECONDS))
    except BaseException as error:
        vm_stopped = False
        callbacks.append(f"vm_stop_proof_failed:{error}")
    stop_elapsed = time.monotonic() - stop_started
    if (safety_profile is not None
            and stop_elapsed * 1_000_000 > safety_profile["maximum_stop_latency_microseconds"]):
        vm_stopped = False
        callbacks.append("observed_stop_elapsed_exceeded_validated_profile")
    stopped_proven = phase_quiescent and vm_stopped
    preclaim_failure = not execution_claimed_state.get("value", False)
    marker_state = "ACTIVE" if preclaim_failure and stopped_proven else (
        "FAILED" if stopped_proven else "UNKNOWN")
    marker_unchanged = False
    try:
        current_marker, current_marker_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        marker_unchanged = current_marker_sha == active_sha and current_marker == active
    except BaseException as error:
        callbacks.append(f"active_marker_readback_failed:{error}")
    if preclaim_failure and not marker_unchanged:
        marker_state = "UNKNOWN"
        stopped_proven = False
        callbacks.append("active_marker_changed_before_capability_claim")
    elif not preclaim_failure:
        try:
            vm.update_active_session_marker(
                session_id, state=marker_state, phase_id="P2",
                expected_marker_sha256=active_sha)
        except BaseException as error:
            callbacks.append(f"marker_update_failed:{error}")
            stopped_proven = False
            marker_state = "UNKNOWN"
    record = {
        "schema_version": 1,
        "mode": vm.VM_MODE,
        "session_id": session_id,
        "phase_id": "P2",
        "status": ("FAILED_STOPPED_BEFORE_CLAIM" if preclaim_failure and stopped_proven
                   else "FAILED_STOPPED" if stopped_proven else "UNKNOWN_BLOCKING"),
        "marker_state": marker_state,
        "capability_claimed": not preclaim_failure,
        "failure": failed_sample,
        "samples": samples,
        "sample_count": len(samples),
        "callbacks": callbacks,
        "phase_quiescent": phase_quiescent,
        "vm_stopped_and_handles_released": vm_stopped,
        "maximum_observed_monitor_gap_seconds": max(
            (float(row.get("monitor_gap_seconds", 0.0)) for row in samples), default=0.0),
        "measured_stop_elapsed_seconds": stop_elapsed,
        "validated_maximum_stop_latency_microseconds": (
            safety_profile.get("maximum_stop_latency_microseconds") if safety_profile else None),
        "host_build_version": (safety_profile["host_build_version"]
                                if safety_profile is not None else vm._host_build_version()),
        "volume_uuid": VM_VOLUME_UUID,
        "safety_volume_uuid": active.get("safety_volume_uuid"),
        "safety_reserve_bytes": active.get("safety_reserve_bytes"),
        "controller_identity": vm._controller_identity(),
        "g0c_identity": current_g0c,
        "all_writable_handles_released": vm_stopped,
        "serial_and_attachment_handles_released": vm_stopped,
        "stop_measurement": stop_measurement,
        "stop_safety_profile_mode": profile_mode,
        "p2_admission_sha256": admission_sha,
        "execution_start_lease_sha256": lease_sha256,
        "execution_start_claim_boundary": copy.deepcopy(
            monitor_ready_evidence.get("execution_start_claim_boundary")),
    }
    signed_failure = vm._seal(record, "vm_phase_monitor_hmac_sha256")
    raise vm.VMPhaseViolation(
        "; ".join(failed_sample.get("blockers", ["P2 monitor failed closed"])), signed_failure)


def _template_allocations(base_dir: Path) -> dict[str, Any]:
    expected = {"system.raw", "auxiliary.bin", "machine-identifier.bin", "hardware-model.bin"}
    if not base_dir.exists():
        return {"system_bytes": 0, "auxiliary_bytes": 0, "metadata_bytes": 0,
                "total_bytes": 0, "files": {}}
    if base_dir.is_symlink() or not base_dir.is_dir():
        raise RuntimeError("P2 destination is not a regular directory")
    names = set(os.listdir(base_dir))
    unknown = names - expected
    if unknown:
        raise RuntimeError(f"unexpected files appeared in P2 template directory: {sorted(unknown)}")
    records: dict[str, Any] = {}
    allocated: dict[str, int] = {}
    for name in expected:
        path = base_dir / name
        try:
            info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != os.stat(VM_ROOT).st_dev:
            raise RuntimeError(f"P2 artifact has an unsafe type, link count, or APFS volume: {path}")
        amount = int(getattr(info, "st_blocks", 0)) * 512
        allocated[name] = amount
        records[name] = {**file_identity(info), "mode": stat.S_IMODE(info.st_mode), "flags": getattr(info, "st_flags", 0)}
    system = allocated.get("system.raw", 0)
    auxiliary = allocated.get("auxiliary.bin", 0)
    metadata = allocated.get("machine-identifier.bin", 0) + allocated.get("hardware-model.bin", 0)
    return {"system_bytes": system, "auxiliary_bytes": auxiliary, "metadata_bytes": metadata,
            "total_bytes": system + auxiliary + metadata, "files": records}


def provision(args: argparse.Namespace, *,
              execution_start_lease: Mapping[str, Any] | None = None,
              execution_start_lease_sha256: str | None = None,
              issued_at_monotonic: float | None = None,
              control_root: Path | None = None) -> None:
    run_id = args.run_id
    safe_run_id = run_id if isinstance(run_id, str) and re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", run_id) else f"invalid-{uuid.uuid4().hex}"
    signing_key: bytes | None = None

    def record_prelaunch_failure(stage: str, error: BaseException) -> None:
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_HELPER_LAUNCH_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": stage,
            "error": str(error),
            "manifest_path": str(getattr(args, "manifest", "")),
            "review_path": str(getattr(args, "review", "")),
            "admission_path": str(getattr(args, "admission", "")),
            "helper_process_created": False,
            "guest_assets_created": False,
        }
        path = unique_path("R6-VM-P2-HELPER-LAUNCH-FAILURE", safe_run_id)
        signed_write(path, failure, "vm_p2_helper_launch_failure_hmac_sha256", signing_key)

    try:
        if safe_run_id != run_id:
            raise RuntimeError("P2 run ID is malformed")
        signing_key = vm.policy._manifest_signing_key()
        admission_path = Path(args.admission).resolve(strict=True)
        evidence = _validate_execution_entry(
            admission_path, signing_key=signing_key, control_root=control_root)
        manifest_path = Path(evidence["manifest_path"]).resolve(strict=True)
        review_path = Path(evidence["review_path"]).resolve(strict=True)
        manifest, review = _verify_reviewed_provenance(manifest_path, review_path, signing_key)
        admission = evidence["admission"]
        admission_sha = evidence["admission_sha256"]
        if (Path(args.manifest).resolve(strict=True) != manifest_path
                or Path(args.review).resolve(strict=True) != review_path
                or not isinstance(execution_start_lease, Mapping)
                or not isinstance(execution_start_lease_sha256, str)):
            raise RuntimeError("P2 execution requires the exact static admission and fresh start lease")
        lease_path = Path(args.execution_start_lease).resolve(strict=True)
        verified_lease, actual_lease_sha = _validate_execution_start_lease(
            lease_path, evidence, signing_key=signing_key,
            lease_sha256=execution_start_lease_sha256,
            issued_at_monotonic=issued_at_monotonic, control_root=control_root)
        if dict(execution_start_lease) != verified_lease:
            raise RuntimeError("P2 execution-start lease changed after its durable readback")
        if _verify_execution_start_lease_ready(
                admission["session_id"], actual_lease_sha,
                control_root=control_root).get("status") != "READY":
            raise RuntimeError("P2 execution-start lease is not available for its first startup use")
        baseline = copy.deepcopy(verified_lease["storage_observation"])
        fresh_vm_volume_usage = verified_lease["storage_measurement"]["vm_volume_usage_bytes"]
        fresh_p2_growth = verified_lease["p2_demand"]["p2_growth_bytes"]
        control_key: bytes | None = None
        active, active_marker_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        expected_marker_sha = getattr(args, "active_marker_expected_sha256", None)
        if (admission.get("decision") != "PASS" or admission.get("authorized_phase") != "P2"
                or admission.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
                or admission.get("run_id") != run_id
                or admission.get("provisioner_review_sha256") != vm._file_sha256(review_path)
                or admission.get("provisioner_source_manifest_sha256") != vm._file_sha256(manifest_path)
                or admission.get("provisioner_source_sha256") != manifest["source_manifest"]["source"]["sha256"]
                or admission.get("provisioner_orchestration_source_sha256")
                != manifest["source_manifest"]["orchestration_source"]["sha256"]
                or admission.get("provisioner_binary_sha256") != manifest["source_manifest"]["binary"]["sha256"]
                or admission.get("provisioner_code_directory_hash")
                != manifest["source_manifest"]["binary"]["code_directory_hash"]
                or admission.get("minimum_supported_cpu_count") != 2
                or admission.get("minimum_supported_memory_size_bytes") != 4 * GIB
                or active.get("phase_id") != "P2_AUTHORIZED"
                or active.get("session_id") != admission.get("session_id")
                or active.get("volume_uuid") != VM_VOLUME_UUID
                or active.get("state") != "ACTIVE"
                or not isinstance(expected_marker_sha, str)
                or not hmac.compare_digest(active_marker_sha, expected_marker_sha)):
            raise RuntimeError("P2 execution requires the exact signed PASS admission, reviewed helper, and authorized active marker")
        active_readback, active_readback_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        if active_readback != active or active_readback_sha != active_marker_sha:
            raise RuntimeError("P2 active marker changed while helper launch authorization was being prepared")
        binary = REPO / admission["provisioner_binary_path"]
        base_dir = Path(admission["system_raw_path"]).parent
        if (not base_dir.is_absolute()
                or base_dir != VM_ROOT / "templates" / f"macos-base-{run_id}"
                or admission.get("auxiliary_storage_path") != str(base_dir / "auxiliary.bin")):
            raise RuntimeError("P2 base-template paths are not the exact canonical admitted locations beneath the VM volume")
        if base_dir.exists() or base_dir.is_symlink():
            raise RuntimeError("admitted P2 target already exists")
        log_path = unique_path("R6-VM-P2-HELPER-LOG", run_id, ".jsonl")
        stderr_path = unique_path("R6-VM-P2-HELPER-STDERR", run_id, ".log")
        monitor_path = unique_path("R6-VM-P2-MONITOR-RESULT", run_id, ".json.gz")
        allocation_path = unique_path("R6-VM-P2-FILE-ALLOCATION-MONITOR", run_id, ".json.gz")
        result_path = unique_path("R6-VM-P2-INSTALL-RESULT", run_id)
        monitor_summary_path = unique_path("R6-VM-P2-MONITOR-SUMMARY", run_id)
        pending_path = unique_path("R6-VM-P2-TERMINATION-PENDING", run_id)
        containment_journal_path = unique_path(
            "R6-VM-P2-CONTAINMENT-MONITOR", run_id, ".jsonl.gz")
        if any(path.exists() for path in (log_path, stderr_path, monitor_path, allocation_path,
                                          result_path, monitor_summary_path, pending_path,
                                          containment_journal_path)):
            raise FileExistsError("a run-specific P2 execution receipt already exists")
        _ready_name, claimed_name = _launch_capability_names(
            str(admission["session_id"]), admission_sha)
        claimed_capability_path = (P2_LAUNCH_CAPABILITY_ROOT / "v1"
                                   / str(admission["session_id"]) / claimed_name)
        execution_lease_state_path = (P2_LAUNCH_CAPABILITY_ROOT / "v1"
                                      / str(admission["session_id"])
                                      / _execution_start_state_names(actual_lease_sha)[0])
        admission_slot_lock_path = (P2_LAUNCH_CAPABILITY_ROOT / "v1"
                                    / str(admission["session_id"])
                                    / "r3-admission-issuance.lock")
        metadata_paths = _provisioner_metadata_paths(
            manifest, manifest_path, review_path, admission_path,
            REPO / admission["p2_identity_plan_path"], REPO / admission["p2_preflight_path"],
            _admission_artifact_path(
                admission["r3_execution_packet_supersession"]["path"],
                "R3 execution-packet supersession receipt"),
            lease_path, execution_lease_state_path,
            claimed_capability_path, admission_slot_lock_path)
    except BaseException as error:
        try:
            record_prelaunch_failure("signed_provenance_admission_marker_key_target_or_baseline_validation", error)
        except BaseException as signing_error:
            raise RuntimeError("P2 prelaunch validation failed and its failure receipt could not be signed: "
                               + str(signing_error)) from error
        raise RuntimeError("P2 prelaunch validation failed; signed failure evidence recorded") from error
    monitor_ready = threading.Event()
    host_monitor_ready = threading.Event()
    monitor_state: dict[str, Any] = {"result": None, "error": None,
                                     "external_failure_reason": None}
    process_state: dict[str, HelperProcess] = {}
    execution_claimed_state = {"value": False}
    monitor_sample_lock = threading.Lock()
    monitor_thread: threading.Thread | None = None
    child: HelperProcess | None = None
    start_command_accepted = False
    start_command_attempted = False
    file_samples: list[dict[str, Any]] = []
    sample_lock = threading.Lock()
    monitor_ready_evidence: dict[str, Any] = {
        "validated_sample_count": 0,
        "monitor_gate_accepted": False,
        "monitor_heartbeats_accepted": 0,
        "last_validated_at_monotonic": None,
    }
    normal_release_scan_stop = threading.Event()
    normal_release_scan_thread: threading.Thread | None = None
    normal_release_scan_state: dict[str, Any] = {
        "lock": threading.Lock(),
        "sequence": 0,
        "consecutive_clear_scans": 0,
        "latest": None,
        "error": None,
    }
    containment_state: dict[str, Any] = {
        "journal": None,
        "pending_path": unique_path("R6-VM-P2-TERMINATION-PENDING", run_id),
        "pending_record": None,
        "pending_sha256": None,
        "termination_signals": [],
        "shutdown_errors": [],
        "forced_process_termination": False,
        "emergency_monitor_failed": False,
        "emergency_termination_deadline": None,
        "drain_started_at_monotonic": None,
        "drain_complete": threading.Event(),
        "drain_lock": threading.Lock(),
        "scan_state": {"sequence": 0, "clear_sequence": 0, "clear": False,
                       "latest": None, "error": None},
    }
    def sample() -> Mapping[str, Any]:
        observation = _live_observation_from_baseline(baseline)
        _validate_execution_vm_mount(
            observation, verified_lease["storage_measurement"]["vm_mount"])
        allocation = _template_allocations(base_dir)
        allocation_blockers = []
        if allocation["system_bytes"] > P2_SYSTEM_MAX:
            allocation_blockers.append("system.raw exceeded the 17 GiB physical allocation cap")
        if allocation["auxiliary_bytes"] > P2_AUX_MAX:
            allocation_blockers.append("auxiliary.bin exceeded the 1 GiB physical allocation cap")
        if allocation["metadata_bytes"] > P2_METADATA_MAX:
            allocation_blockers.append("P2 metadata exceeded the 32 MiB physical allocation cap")
        if allocation["total_bytes"] > P2_GROWTH_MAX:
            allocation_blockers.append("P2 template assets exceeded the complete growth cap")
        metadata_inventory = _assert_p2_metadata_budget(
            metadata_paths, additional_allocated_bytes=allocation["metadata_bytes"])
        metadata_total = metadata_inventory["total_p2_metadata_allocated_bytes"]
        if metadata_total > P2_METADATA_MAX:
            allocation_blockers.append("combined P1/P2 provisioning metadata exceeded the 32 MiB cap")
        observation["p2_file_allocation_sample"] = {
            **allocation,
            "combined_p1_p2_metadata_allocated_bytes": metadata_total,
            "combined_p1_p2_metadata_remaining_bytes": metadata_inventory["remaining_bytes"],
        }
        with sample_lock:
            file_samples.append({
                "captured_at_unix": observation.get("captured_at_unix"),
                "system_raw_allocated_bytes": allocation["system_bytes"],
                "auxiliary_allocated_bytes": allocation["auxiliary_bytes"],
                "metadata_allocated_bytes": allocation["metadata_bytes"],
                "combined_p1_p2_metadata_allocated_bytes": metadata_total,
                "combined_p1_p2_metadata_remaining_bytes": metadata_inventory["remaining_bytes"],
                "total_allocated_bytes": allocation["total_bytes"],
                "blockers": allocation_blockers,
            })
        if allocation_blockers:
            raise RuntimeError("live P2 file-allocation validation failed: " + "; ".join(allocation_blockers))
        return observation

    def operation_done() -> bool:
        nonlocal normal_release_scan_thread
        # The adapter calls this only after the sample's policy and actual
        # inter-sample gap checks have passed. Release startup after three such
        # complete monitor-approved samples, never from inside sample().
        with sample_lock:
            validated_sample_count = len(file_samples)
            monitor_ready_evidence["validated_sample_count"] = validated_sample_count
            monitor_ready_evidence["last_validated_at_unix"] = time.time()
            monitor_ready_evidence["last_validated_at_monotonic"] = time.monotonic()
        selected = process_state.get("process")
        external_failure = monitor_state.get("external_failure_reason")
        if external_failure:
            raise RuntimeError(f"P2 orchestration requested fail-closed shutdown: {external_failure}")
        if selected is None:
            if validated_sample_count >= 3:
                host_monitor_ready.set()
            return False
        installer_results = selected.events_named("installer_result")
        if installer_results:
            if (len(installer_results) != 1
                    or installer_results[0].get("installer_completed") is not True):
                raise RuntimeError("P2 helper reported a failed or ambiguous installer result")
            if not monitor_ready_evidence.get("successful_install_stop_requested"):
                if not selected.send("STOP"):
                    raise RuntimeError("P2 helper exited before installer-completion STOP was delivered")
                monitor_ready_evidence["successful_install_stop_requested"] = True
                monitor_ready_evidence["successful_install_result_at_monotonic"] = time.monotonic()
            if normal_release_scan_thread is None:
                normal_release_scan_thread = threading.Thread(
                    target=normal_completion_scan_worker,
                    name="p2-normal-release-scan", daemon=True)
                normal_release_scan_thread.start()
            handles = selected.events_named("handles_released")
            helper_results = selected.events_named("helper_result")
            if handles:
                valid_release = (len(handles) == 1
                                 and handles[0].get("vm_state") == "STOPPED"
                                 and handles[0].get("all_writable_handles_released") is True
                                 and handles[0].get("installer_object_released") is True
                                 and handles[0].get("disk_attachment_released") is True)
                if not valid_release:
                    raise RuntimeError("P2 helper emitted invalid or ambiguous writable-handle release evidence")
            if selected.terminal():
                if (not handles or len(helper_results) != 1) and selected.stdout_thread.is_alive():
                    return False
                if not handles or len(helper_results) != 1:
                    raise RuntimeError("P2 helper exited without verified stopped-state and handle-release evidence")
                release = helper_results[0].get("release")
                if (helper_results[0].get("installer_completed") is not True
                        or not isinstance(release, Mapping)
                        or release.get("vm_state") != "STOPPED"
                        or release.get("all_writable_handles_released") is not True
                        or release.get("installer_object_released") is not True
                        or release.get("disk_attachment_released") is not True):
                    raise RuntimeError("P2 helper result did not bind successful installation to stopped and released ownership")
                with normal_release_scan_state["lock"]:
                    scan_snapshot = normal_release_scan_state["latest"]
                    clear_scan_count = normal_release_scan_state["consecutive_clear_scans"]
                    scan_error = normal_release_scan_state["error"]
                monitor_ready_evidence["normal_release_writer_scan"] = scan_snapshot
                if clear_scan_count >= 2 and isinstance(scan_snapshot, Mapping):
                    normal_release_scan_stop.set()
                    monitor_ready_evidence["normal_release_writer_scan_clear_count"] = clear_scan_count
                    return True
                started = monitor_ready_evidence["successful_install_result_at_monotonic"]
                if time.monotonic() - started > vm.VM_PHASE_STOP_TIMEOUT_SECONDS:
                    raise RuntimeError("P2 helper group or exact base descriptors did not clear inside the stop bound: "
                                       + str(scan_error or "two consecutive clear scans are required"))
                return False
            started = monitor_ready_evidence["successful_install_result_at_monotonic"]
            if time.monotonic() - started > vm.VM_PHASE_STOP_TIMEOUT_SECONDS:
                raise RuntimeError("P2 successful installer did not reach stopped state and release handles within the stop bound")
            return False
        if selected.terminal():
            raise RuntimeError("P2 helper exited before reporting successful installer completion")
        if validated_sample_count < 3:
            return False
        if not selected.events_named("p2_authorization_accepted"):
            if selected.events_named("p2_authorization_rejected"):
                raise RuntimeError("helper rejected the signed P2 launch authorization")
            return False

        command = "MONITOR_GATE" if not monitor_ready_evidence["monitor_gate_accepted"] else "HEARTBEAT"
        payload = "|".join((
            "P2MON1", run_id, str(admission["session_id"]), admission_sha,
            active_marker_sha, manifest["source_manifest"]["binary"]["sha256"],
            str(validated_sample_count), str(validated_sample_count),
        ))
        if not selected.send(_signed_helper_control(command, payload, control_key)):
            raise RuntimeError("P2 helper exited before accepting the live monitor control")
        accepted_event_name = "monitor_gate_accepted" if command == "MONITOR_GATE" else "monitor_heartbeat_accepted"
        rejected_event_name = "monitor_gate_rejected" if command == "MONITOR_GATE" else "monitor_heartbeat_rejected"
        acknowledgement = selected.wait_for_any_event(
            {accepted_event_name, rejected_event_name}, 0.1,
            predicate=lambda row: row.get("monitor_sequence") == validated_sample_count)
        if acknowledgement is None:
            if selected.terminal():
                raise RuntimeError("P2 helper exited before acknowledging the live monitor control")
            raise RuntimeError(f"helper did not acknowledge signed {command} for validated sample "
                               f"{validated_sample_count} within the monitor bound")
        if acknowledgement.get("event") != accepted_event_name or acknowledgement.get("accepted") is not True:
            raise RuntimeError(f"helper rejected signed {command} for validated sample {validated_sample_count}")
        monitor_ready_evidence["last_control_acknowledgement"] = acknowledgement
        if command == "MONITOR_GATE":
            monitor_ready_evidence["monitor_gate_accepted"] = True
            monitor_ready_evidence["monitor_gate_sample_count"] = validated_sample_count
            monitor_ready.set()
        else:
            monitor_ready_evidence["monitor_heartbeats_accepted"] += 1
        return False

    def _current_base_asset_paths() -> list[Path]:
        if not os.path.lexists(base_dir):
            return []
        info = os.stat(base_dir, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("P2 base path changed type during shutdown containment")
        result: list[Path] = []
        for entry in os.scandir(base_dir):
            entry_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_nlink != 1:
                raise RuntimeError("P2 base contains a non-regular or linked asset during shutdown containment")
            result.append(Path(entry.path))
        return result

    def normal_completion_scan_worker() -> None:
        while not normal_release_scan_stop.is_set():
            try:
                process_scan = _p2_process_scan_evidence(
                    required_absent_pids=(child.pid,),
                    required_absent_process_group_ids=(child.process_group_id,))
                assets = _current_base_asset_paths()
                descriptor_scan = _descriptor_scan_evidence(assets) if assets else {
                    "captured_at_unix": time.time(), "scan_clear": True,
                    "scanned_paths": [], "open_file_descriptors": [],
                    "exact_target_descriptor_matches": [],
                    "descriptor_identity_mismatches": [],
                }
                group_presence = _process_group_presence_for_pid(child.process_group_id)
                clear = (not process_scan["blocking_processes"]
                         and group_presence is False
                         and descriptor_scan.get("scan_clear") is True
                         and not descriptor_scan.get("open_file_descriptors")
                         and not descriptor_scan.get("descriptor_identity_mismatches"))
                snapshot = {
                    "captured_at_unix": time.time(),
                    "sequence": normal_release_scan_state["sequence"] + 1,
                    "clear": clear,
                    "process_scan": process_scan,
                    "descriptor_scan": descriptor_scan,
                    "process_group_presence": group_presence,
                    "error": None,
                }
            except BaseException as error:
                snapshot = {
                    "captured_at_unix": time.time(),
                    "sequence": normal_release_scan_state["sequence"] + 1,
                    "clear": False,
                    "error": str(error)[:4000],
                }
            with normal_release_scan_state["lock"]:
                normal_release_scan_state["sequence"] = snapshot["sequence"]
                if snapshot["clear"]:
                    normal_release_scan_state["consecutive_clear_scans"] += 1
                else:
                    normal_release_scan_state["consecutive_clear_scans"] = 0
                normal_release_scan_state["latest"] = snapshot
                normal_release_scan_state["error"] = snapshot.get("error")
            if normal_release_scan_stop.wait(0.5):
                return

    def _journal() -> P2ContainmentJournal:
        selected = containment_state.get("journal")
        if selected is None:
            path = unique_path("R6-VM-P2-CONTAINMENT-MONITOR", run_id, ".jsonl.gz")
            selected = P2ContainmentJournal(path, signing_key)
            containment_state["journal"] = selected
        return selected

    def begin_emergency_shutdown(reason: str, *, stage: str) -> None:
        if containment_state.get("pending_record") is not None:
            return
        selected = process_state.get("process")
        journal = _journal()
        journal.append("termination_pending_created", {
            "failure_stage": stage,
            "reason": reason[:2000],
            "helper_pid": child.pid if child is not None else None,
            "helper_process_group_id": child.process_group_id if child is not None else None,
            "helper_exit_code_before_termination": child.poll() if child is not None else None,
            "start_command_attempted": start_command_attempted,
            "start_command_accepted": start_command_accepted,
            "guest_asset_paths": [str(base_dir / name) for name in (
                "system.raw", "auxiliary.bin", "machine-identifier.bin", "hardware-model.bin")],
            "monitor_sample_count_before_termination": len(file_samples),
        }, durable=True)
        journal_info = os.stat(journal.path, follow_symlinks=False)
        if file_identity(journal_info) != file_identity(os.fstat(journal.fd)):
            raise RuntimeError("P2 containment journal changed before the signed termination-pending boundary")
        pending_path = containment_state["pending_path"]
        pending = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_TERMINATION_PENDING",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "UNKNOWN_BLOCKING_TERMINATION_PENDING",
            "decision": "UNKNOWN_BLOCKING",
            "failure_stage": stage,
            "failure_reason": reason[:4000],
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "helper_pid": child.pid if child is not None else None,
            "helper_process_group_id": child.process_group_id if child is not None else None,
            "helper_exit_code_before_termination": child.poll() if child is not None else None,
            "start_command_attempted": start_command_attempted,
            "start_command_accepted": start_command_accepted,
            "vm_state": "UNKNOWN",
            "all_writable_handles_released": False,
            "installer_object_released": False,
            "disk_attachment_released": False,
            "termination_signals_sent_at_pending_write": [],
            "guest_asset_paths": [str(base_dir / name) for name in (
                "system.raw", "auxiliary.bin", "machine-identifier.bin", "hardware-model.bin")],
            "guest_assets_created": os.path.lexists(base_dir),
            "monitor_sample_count_before_termination": len(file_samples),
            "containment_journal_path": str(journal.path.relative_to(REPO)),
            "containment_journal_initial_file_identity": journal.initial_identity,
            "containment_journal_sha256_before_os_termination": vm._file_sha256(journal.path),
            "containment_journal_file_identity_before_os_termination": file_identity(journal_info),
            "containment_journal_event_sequence_before_os_termination": journal.sequence,
        }
        sealed = signed_write(pending_path, pending, "vm_p2_termination_pending_hmac_sha256",
                              signing_key)
        containment_state["pending_record"] = sealed
        containment_state["pending_sha256"] = vm._file_sha256(pending_path)
        containment_state["drain_started_at_monotonic"] = time.monotonic()

    def _emergency_capacity_sample(previous_sampled_at: float | None) -> tuple[float | None, bool]:
        journal = _journal()
        try:
            observation = sample()
            checked_sample = vm._check_runtime_phase_sample(
                observation, volume_uuid=VM_VOLUME_UUID,
                initial_quota_used_bytes=fresh_vm_volume_usage,
                maximum_phase_growth_bytes=fresh_p2_growth,
                concurrent_growth_bytes=None, session_id=admission["session_id"])
            sampled_at = time.monotonic()
            gap = sampled_at - previous_sampled_at if previous_sampled_at is not None else None
            blockers = list(checked_sample.get("blockers", []))
            if previous_sampled_at is None:
                blockers.append("emergency P2 monitor lacks the last adapter-approved sample timestamp")
            elif gap is not None and gap > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS:
                blockers.append("emergency P2 monitor gap exceeded the 250 ms policy bound")
            journal.append("emergency_capacity_sample", {
                "sample": checked_sample,
                "sample_gap_seconds": gap,
                "sample_gap_within_250ms": gap is not None and gap <= 0.25,
                "provisioning_file_allocation": observation.get("p2_file_allocation_sample"),
                "sample_blockers": sorted(set(blockers)),
            })
            if blockers:
                containment_state["shutdown_errors"].append(
                    "emergency capacity sample reported: " + "; ".join(sorted(set(blockers)))[:2000])
            return sampled_at, not blockers and bool(checked_sample.get("admitted"))
        except BaseException as error:
            containment_state["shutdown_errors"].append(f"emergency capacity sample unknown: {error}")
            try:
                journal.append("emergency_capacity_sample_unknown", {
                    "error": str(error)[:4000],
                    "sample_gap_seconds": (time.monotonic() - previous_sampled_at
                                           if previous_sampled_at is not None else None),
                })
            except BaseException as journal_error:
                containment_state["shutdown_errors"].append(f"journal append failed: {journal_error}")
            return time.monotonic(), False

    def _process_group_presence() -> bool | None:
        if child is None:
            return False
        return _process_group_presence_for_pid(child.process_group_id)

    def _emergency_sample_and_escalate(previous_sampled_at: float | None) -> tuple[float | None, bool]:
        sampled_at, admitted = _emergency_capacity_sample(previous_sampled_at)
        if not admitted:
            containment_state["emergency_monitor_failed"] = True
        if not containment_state["emergency_monitor_failed"]:
            return sampled_at, True
        now = time.monotonic()
        process_alive = child.poll() is None
        group_presence = _process_group_presence()
        signals = containment_state["termination_signals"]
        if (process_alive or group_presence is not False) and not signals:
            containment_state["forced_process_termination"] = True
            try:
                child.terminate()
                signals.append("SIGTERM_PROCESS_GROUP_EMERGENCY_MONITOR_FAILURE")
                containment_state["emergency_termination_deadline"] = now + 5.0
            except BaseException as error:
                containment_state["shutdown_errors"].append(
                    f"immediate SIGTERM after emergency monitor failure: {error}")
                containment_state["emergency_termination_deadline"] = now
        elif (containment_state.get("emergency_termination_deadline") is not None
              and now >= containment_state["emergency_termination_deadline"]
              and (process_alive or group_presence is not False)
              and (not signals or "SIGKILL_PROCESS_GROUP_EMERGENCY_MONITOR_FAILURE" not in signals)):
            containment_state["forced_process_termination"] = True
            try:
                child.kill()
                signals.append("SIGKILL_PROCESS_GROUP_EMERGENCY_MONITOR_FAILURE")
                containment_state["emergency_termination_deadline"] = now + 5.0
            except BaseException as error:
                containment_state["shutdown_errors"].append(
                    f"SIGKILL after emergency monitor failure: {error}")
                containment_state["emergency_termination_deadline"] = now + 5.0
        elif (containment_state.get("emergency_termination_deadline") is not None
              and now >= containment_state["emergency_termination_deadline"]
              and (process_alive or group_presence is not False)
              and signals and signals[-1] == "SIGKILL_PROCESS_GROUP_EMERGENCY_MONITOR_FAILURE"):
            try:
                child.kill()
                containment_state["emergency_termination_deadline"] = now + 5.0
            except BaseException as error:
                containment_state["shutdown_errors"].append(
                    f"repeated SIGKILL after emergency monitor failure: {error}")
                containment_state["emergency_termination_deadline"] = now + 5.0
        return sampled_at, admitted

    def _scan_for_writers(stop_event: threading.Event) -> None:
        scan_state = containment_state["scan_state"]
        while not stop_event.is_set():
            try:
                process_scan = _p2_process_scan_evidence(
                    required_absent_pids=(child.pid,),
                    required_absent_process_group_ids=(child.process_group_id,))
                assets = _current_base_asset_paths()
                descriptor_scan = _descriptor_scan_evidence(assets) if assets else {
                    "captured_at_unix": time.time(), "scan_clear": True,
                    "scanned_paths": [], "open_file_descriptors": [],
                    "exact_target_descriptor_matches": [],
                    "descriptor_identity_mismatches": [],
                }
                group_presence = _process_group_presence()
                clear = (not process_scan["blocking_processes"]
                         and group_presence is False
                         and descriptor_scan.get("scan_clear") is True
                         and not descriptor_scan.get("open_file_descriptors")
                         and not descriptor_scan.get("descriptor_identity_mismatches"))
                error = None if group_presence is not None else "process group presence is permission-denied"
                snapshot = {
                    "process_scan": process_scan,
                    "descriptor_scan": descriptor_scan,
                    "process_group_presence": group_presence,
                    "clear": clear,
                    "error": error,
                }
            except BaseException as error:
                snapshot = {"clear": False, "error": str(error)[:4000]}
            with scan_state["lock"]:
                scan_state["sequence"] += 1
                sequence = scan_state["sequence"]
                if snapshot.get("clear"):
                    scan_state["clear_sequence"] = sequence
                scan_state["clear"] = bool(snapshot.get("clear"))
                scan_state["latest"] = snapshot
                scan_state["error"] = snapshot.get("error")
            try:
                process_summary = None
                if isinstance(snapshot.get("process_scan"), Mapping):
                    process_row = snapshot["process_scan"]
                    process_summary = {
                        "captured_at_unix": process_row.get("captured_at_unix"),
                        "scanned_process_rows": process_row.get("scanned_process_rows"),
                        "process_table_sha256": process_row.get("process_table_sha256"),
                        "blocking_processes": process_row.get("blocking_processes"),
                        "required_absent_pid_presence": process_row.get("required_absent_pid_presence"),
                        "required_absent_process_group_members": process_row.get(
                            "required_absent_process_group_members"),
                    }
                descriptor_summary = None
                if isinstance(snapshot.get("descriptor_scan"), Mapping):
                    descriptor_row = snapshot["descriptor_scan"]
                    descriptor_summary = {
                        "captured_at_unix": descriptor_row.get("captured_at_unix"),
                        "scan_clear": descriptor_row.get("scan_clear"),
                        "scanned_paths": descriptor_row.get("scanned_paths"),
                        "open_process_ids": descriptor_row.get("open_process_ids"),
                        "exact_target_descriptor_matches": descriptor_row.get(
                            "exact_target_descriptor_matches"),
                        "descriptor_identity_mismatches": descriptor_row.get(
                            "descriptor_identity_mismatches"),
                        "stdout_sha256": descriptor_row.get("stdout_sha256"),
                    }
                _journal().append("writer_and_handle_scan", {
                    "scan_sequence": sequence,
                    "clear": bool(snapshot.get("clear")),
                    "process_scan": process_summary,
                    "descriptor_scan": descriptor_summary,
                    "process_group_presence": snapshot.get("process_group_presence"),
                    "scan_error": snapshot.get("error"),
                })
            except BaseException as error:
                containment_state["shutdown_errors"].append(f"scan journal append failed: {error}")
            stop_event.wait(1.0)

    def drain_emergency_writers(graceful_exit_timeout_seconds: float) -> None:
        completed = containment_state["drain_complete"]
        if completed.is_set():
            return
        lock = containment_state["drain_lock"]
        if not lock.acquire(blocking=False):
            previous_sampled_at = monitor_ready_evidence.get("last_validated_at_monotonic")
            if (not isinstance(previous_sampled_at, (int, float))
                    or isinstance(previous_sampled_at, bool)
                    or not math.isfinite(previous_sampled_at)):
                previous_sampled_at = None
            while not completed.wait(0.2):
                if start_command_attempted:
                    previous_sampled_at, _ = _emergency_sample_and_escalate(previous_sampled_at)
            return
        try:
            if completed.is_set():
                return
            begin_emergency_shutdown("P2 failure shutdown is waiting for all writers and handles to disappear",
                                     stage="emergency_writer_containment")
            selected = process_state.get("process")
            if selected is not None:
                try:
                    selected.send("CANCEL")
                    selected.send("STOP")
                except BaseException as error:
                    containment_state["shutdown_errors"].append(f"graceful control send: {error}")
            scan_state = containment_state["scan_state"]
            scan_state.setdefault("lock", threading.Lock())
            scanner_stop = threading.Event()
            scanner = threading.Thread(target=_scan_for_writers, args=(scanner_stop,),
                                       name="p2-writer-and-handle-scan", daemon=True)
            scanner.start()
            started = time.monotonic()
            graceful_deadline = started + max(0.0, graceful_exit_timeout_seconds)
            term_deadline: float | None = None
            previous_sampled_at = monitor_ready_evidence.get("last_validated_at_monotonic")
            if (not isinstance(previous_sampled_at, (int, float))
                    or isinstance(previous_sampled_at, bool)
                    or not math.isfinite(previous_sampled_at)):
                previous_sampled_at = None
            previous_clear_sequence = 0
            clear_scans = 0
            last_progress = started
            while True:
                if start_command_attempted:
                    previous_sampled_at, _ = _emergency_sample_and_escalate(previous_sampled_at)
                else:
                    try:
                        _journal().append("emergency_capacity_monitor_not_required", {
                            "reason": "guest START was never attempted; no admitted guest writer was created",
                            "sample_count": len(file_samples),
                        })
                    except BaseException as error:
                        containment_state["shutdown_errors"].append(f"journal append failed: {error}")
                now = time.monotonic()
                process_alive = child.poll() is None
                group_presence = _process_group_presence()
                if now >= graceful_deadline and not containment_state["termination_signals"]:
                    containment_state["forced_process_termination"] = process_alive or group_presence is True
                    if process_alive or group_presence is True:
                        try:
                            child.terminate()
                            containment_state["termination_signals"].append("SIGTERM_PROCESS_GROUP")
                            term_deadline = now + 5.0
                        except BaseException as error:
                            containment_state["shutdown_errors"].append(f"SIGTERM process group: {error}")
                elif (term_deadline is not None and now >= term_deadline
                      and (process_alive or group_presence is True or group_presence is None)):
                    containment_state["forced_process_termination"] = True
                    try:
                        child.kill()
                        if not containment_state["termination_signals"] or containment_state["termination_signals"][-1] != "SIGKILL_PROCESS_GROUP":
                            containment_state["termination_signals"].append("SIGKILL_PROCESS_GROUP")
                        term_deadline = now + 5.0
                    except BaseException as error:
                        containment_state["shutdown_errors"].append(f"SIGKILL process group: {error}")
                with scan_state["lock"]:
                    scan_sequence = scan_state["sequence"]
                    scan_clear = scan_state["clear"]
                    scan_error = scan_state["error"]
                    latest_scan = scan_state["latest"]
                if scan_clear and scan_sequence != previous_clear_sequence:
                    clear_scans += 1
                    previous_clear_sequence = scan_sequence
                elif not scan_clear:
                    clear_scans = 0
                    previous_clear_sequence = scan_sequence
                if child.poll() is not None and scan_clear and clear_scans >= 2:
                    break
                if now - last_progress >= 10.0:
                    print(json.dumps({
                        "status": "P2_WRITER_CONTAINMENT_PENDING",
                        "helper_pid": child.pid,
                        "helper_process_group_id": child.process_group_id,
                        "helper_reaped": child.poll() is not None,
                        "process_group_presence": group_presence,
                        "clear_scan_count": clear_scans,
                        "latest_scan_error": scan_error,
                        "failure_stage": "emergency_writer_containment",
                    }, sort_keys=True), file=sys.stderr, flush=True)
                    last_progress = now
                elapsed = time.monotonic() - now
                time.sleep(max(0.001, 0.10 - elapsed))
            scanner_stop.set()
            scanner.join(timeout=12)
            if scanner.is_alive():
                containment_state["shutdown_errors"].append("post-termination writer scanner did not stop")
                while scanner.is_alive():
                    if start_command_attempted:
                        previous_sampled_at, _ = _emergency_sample_and_escalate(previous_sampled_at)
                    scanner.join(timeout=0.2)
            containment_state["final_writer_scan"] = latest_scan
            containment_state["writer_scans_clear"] = True
            containment_state["drain_complete"].set()
        finally:
            lock.release()

    def _successful_helper_release_verified(selected: HelperProcess | None) -> bool:
        if selected is None or not selected.terminal():
            return False
        installer_results = selected.events_named("installer_result")
        releases = selected.events_named("handles_released")
        helper_results = selected.events_named("helper_result")
        if len(installer_results) != 1 or len(releases) != 1 or len(helper_results) != 1:
            return False
        release = helper_results[0].get("release")
        return bool(
            installer_results[0].get("installer_completed") is True
            and releases[0].get("vm_state") == "STOPPED"
            and releases[0].get("all_writable_handles_released") is True
            and releases[0].get("installer_object_released") is True
            and releases[0].get("disk_attachment_released") is True
            and helper_results[0].get("installer_completed") is True
            and isinstance(release, Mapping)
            and release.get("vm_state") == "STOPPED"
            and release.get("all_writable_handles_released") is True
            and release.get("installer_object_released") is True
            and release.get("disk_attachment_released") is True)

    def cancel_operation() -> None:
        if process_state.get("process") is None and not start_command_attempted:
            return
        begin_emergency_shutdown("capacity monitor requested cancellation", stage="monitor_violation")
        selected = process_state.get("process")
        if selected:
            selected.send("CANCEL")

    def stop_vm() -> None:
        selected = process_state.get("process")
        if selected is None and not start_command_attempted:
            return
        if _successful_helper_release_verified(selected):
            return
        begin_emergency_shutdown("capacity monitor requested VM stop", stage="monitor_violation")
        if selected:
            selected.send("STOP")

    def wait_operation_quiescent(timeout: float) -> bool:
        selected = process_state.get("process")
        if _successful_helper_release_verified(selected):
            with normal_release_scan_state["lock"]:
                scan_snapshot = normal_release_scan_state["latest"]
                clear_scan_count = normal_release_scan_state["consecutive_clear_scans"]
            if (clear_scan_count >= 2 and isinstance(scan_snapshot, Mapping)
                    and scan_snapshot.get("clear") is True):
                return True
            raise RuntimeError("verified normal helper release lost its two-clear-scan quiescence evidence")
        if not start_command_attempted:
            return True
        drain_emergency_writers(timeout)
        terminal_events = selected.events_named("installer_terminal") if selected else []
        return bool(terminal_events and len(terminal_events) == 1
                    and terminal_events[0].get("installer_completed") is True)

    def wait_vm_stopped_and_handles_released(timeout: float) -> bool:
        selected = process_state.get("process")
        if selected is None:
            return True
        if not containment_state["drain_complete"].is_set() and monitor_state.get("error") is not None:
            drain_emergency_writers(timeout)
        return selected.wait_released_and_exit(
            min(timeout, 0.25), allow_no_vm_created=not start_command_attempted)

    def run_monitor() -> None:
        try:
            monitor_state["result"] = _run_p2_live_monitor(
                admission_path=admission_path, admission_sha256=admission_sha,
                lease_path=lease_path, lease=verified_lease,
                lease_sha256=actual_lease_sha, issued_at_monotonic=issued_at_monotonic,
                signing_key=signing_key, session_id=admission["session_id"],
                initial_quota_used_bytes=fresh_vm_volume_usage,
                maximum_phase_growth_bytes=fresh_p2_growth,
                operation_done=operation_done, cancel_operation=cancel_operation,
                wait_operation_quiescent=wait_operation_quiescent,
                sample_observation=sample, stop_vm=stop_vm,
                wait_vm_stopped_and_handles_released=wait_vm_stopped_and_handles_released,
                host_monitor_ready=host_monitor_ready,
                monitor_ready_evidence=monitor_ready_evidence,
                monitor_state=monitor_state,
                execution_claimed_state=execution_claimed_state,
                boundary_lock=monitor_sample_lock,
                poll_interval_seconds=0.1)
        except BaseException as error:
            monitor_state["error"] = error

    # Bind the last pre-launch readback before starting the helper. The helper
    # remains idle in awaitProvisionStart and cannot create guest assets until
    # the live monitor has completed three policy-approved samples.
    try:
        launch_manifest, launch_review = _verify_reviewed_provenance(
            manifest_path, review_path, signing_key)
        launch_manifest_sha = vm._file_sha256(manifest_path)
        launch_review_sha = vm._file_sha256(review_path)
        active_prelaunch, active_prelaunch_sha = read_signed_with_key(
            vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
        if (launch_manifest_sha != admission["provisioner_source_manifest_sha256"]
                or launch_review_sha != admission["provisioner_review_sha256"]
                or launch_manifest["source_manifest"]["binary"]["sha256"] != admission["provisioner_binary_sha256"]
                or launch_manifest["source_manifest"]["binary"]["code_directory_hash"]
                != admission["provisioner_code_directory_hash"]
                or launch_manifest["source_manifest"]["source"]["sha256"] != admission["provisioner_source_sha256"]
                or launch_manifest["source_manifest"]["orchestration_source"]["sha256"]
                != admission["provisioner_orchestration_source_sha256"]
                or launch_review.get("decision") != admission["provisioner_review_decision"]
                or active_prelaunch != active or active_prelaunch_sha != active_marker_sha):
            raise RuntimeError("P2 helper source, executable, review, or active marker changed after admission and before launch")
    except BaseException as error:
        record_prelaunch_failure("immediately_prelaunch_provenance_active_marker_or_admission_revalidation", error)
        raise RuntimeError("P2 immediate prelaunch validation failed; signed failure recorded") from error

    def fail_before_capability_claim(reason: str, *, stage: str) -> None:
        if monitor_state.get("error") is None:
            monitor_state["external_failure_reason"] = reason
        if monitor_thread is not None and monitor_thread.ident is not None:
            monitor_thread.join()
        if process_state.get("process") is not None:
            raise RuntimeError("P2 failure crossed the pre-claim boundary; production containment is required")
        capability_status: str | None = None
        try:
            state_fd = _open_launch_capability_session_directory(
                admission["session_id"], control_root=control_root, create=False)
            try:
                private_state = _capability_file_state(
                    state_fd, admission["session_id"], admission_sha)
                capability_status = private_state.get("status")
                if capability_status == "CLAIMED":
                    descriptor = os.open(
                        private_state["path_name"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0), dir_fd=state_fd)
                    try:
                        commitment = hashlib.sha256(os.read(descriptor, 33)).hexdigest()
                    finally:
                        os.close(descriptor)
                    if commitment != admission.get("helper_control_token_sha256"):
                        raise RuntimeError("claimed P2 capability commitment no longer verifies")
            finally:
                os.close(state_fd)
            if capability_status not in {"READY", "CLAIMED"}:
                raise RuntimeError("P2 capability entered an unexpected state during startup failure")
            if capability_status == "READY":
                _validate_launch_capability_contract(
                    admission, admission_sha, control_root=control_root)
                current_marker, current_marker_sha = read_signed_with_key(
                    vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", signing_key)
                if (current_marker_sha != active_marker_sha or current_marker != active):
                    raise RuntimeError("P2 active marker changed during a pre-claim failure")
            if os.path.lexists(base_dir):
                raise RuntimeError("guest base assets appeared before capability claim")
        except BaseException as verification_error:
            reason = f"{reason}; pre-claim invariant verification failed: {verification_error}"
        try:
            record_prelaunch_failure(stage, RuntimeError(reason))
        except BaseException as record_error:
            raise RuntimeError("P2 pre-claim gate failed and its signed failure evidence could not be persisted: "
                               + str(record_error))
        if execution_claimed_state["value"]:
            raise RuntimeError("P2 startup failed after this process's atomic capability claim and before guest asset creation: "
                               + reason)
        if capability_status == "CLAIMED":
            raise RuntimeError("P2 startup failed before this process's capability claim; the shared capability is claimed by another attempt: "
                               + reason)
        raise RuntimeError("P2 pre-claim monitor gate failed; this process did not claim the capability: " + reason)

    try:
        monitor_thread = threading.Thread(target=run_monitor, name="p2-capacity-monitor")
        monitor_thread.start()
    except BaseException as error:
        fail_before_capability_claim(str(error), stage="preclaim_monitor_thread_startup")
    if not host_monitor_ready.wait(10):
        fail_before_capability_claim(
            str(monitor_state.get("error") or "three complete host monitor samples timed out"),
            stage="preclaim_monitor_initial_gate")
    if (monitor_state.get("error") is not None
            or monitor_thread is None or not monitor_thread.is_alive()
            or monitor_ready_evidence.get("validated_sample_count", 0) < 3):
        fail_before_capability_claim(
            str(monitor_state.get("error") or "host monitor did not remain active after its initial gate"),
            stage="preclaim_monitor_initial_gate")

    try:
        claim_result = _claim_p2_after_initial_monitor_gate(
            admission_path=admission_path, lease_path=lease_path,
            lease_sha256=execution_start_lease_sha256,
            issued_at_monotonic=issued_at_monotonic,
            signing_key=signing_key, control_root=control_root,
            host_monitor_ready=host_monitor_ready,
            monitor_thread=monitor_thread,
            monitor_state=monitor_state,
            monitor_ready_evidence=monitor_ready_evidence,
            boundary_lock=monitor_sample_lock,
            boundary_sample=sample,
            execution_claimed_state=execution_claimed_state)
        if claim_result["evidence"]["admission_sha256"] != admission_sha:
            raise RuntimeError("P2 static admission digest changed before capability claim")
        control_token = claim_result["control_token"]
        launch_claim = claim_result["launch_claim"]
        control_key = control_token
    except BaseException as error:
        fail_before_capability_claim(str(error), stage="preclaim_lease_revalidation_or_capability_claim")

    def stop_monitor_after_prestart_failure(reason: str) -> dict[str, Any] | None:
        """Stop the claimed-session monitor if helper startup fails before guest mutation."""
        if monitor_thread is None or monitor_thread.ident is None:
            raise RuntimeError("P2 live monitor was not initialized before helper startup failure")
        if monitor_state.get("error") is None:
            monitor_state["external_failure_reason"] = reason
        monitor_thread.join()
        if monitor_thread.is_alive():
            raise RuntimeError("P2 live monitor did not stop after pre-guest helper startup failure")
        result = monitor_state.get("result")
        if isinstance(result, Mapping):
            raise RuntimeError("P2 live monitor reported completion before P2 helper startup")
        error = monitor_state.get("error")
        record = getattr(error, "record", None)
        return dict(record) if isinstance(record, Mapping) else None

    launch_provenance_checked_at_unix = time.time()
    command = [str(binary), "provision", str(admission_path), str(manifest_path), str(review_path)]
    binary_record = launch_manifest["source_manifest"]["binary"]
    executable_descriptor: int | None = None
    executable_identity: dict[str, int] | None = None
    spawn_pending: dict[str, Any] = {}

    def persist_spawn_termination_pending(pid: int, process_group_id: int, reason: str) -> None:
        pending_path = unique_path("R6-VM-P2-TERMINATION-PENDING", run_id)
        pending_record = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_TERMINATION_PENDING",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "UNKNOWN_BLOCKING_TERMINATION_PENDING",
            "decision": "UNKNOWN_BLOCKING",
            "failure_stage": "suspended_helper_launch_validation",
            "failure_reason": reason[:4000],
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "helper_pid": pid,
            "helper_process_group_id": process_group_id,
            "helper_suspended_before_user_code": True,
            "start_command_attempted": False,
            "start_command_accepted": False,
            "vm_state": "UNKNOWN",
            "all_writable_handles_released": False,
            "installer_object_released": False,
            "disk_attachment_released": False,
            "termination_signals_sent_at_pending_write": [],
            "guest_asset_paths": [str(VM_ROOT / "templates" / f"macos-base-{run_id}" / name)
                                  for name in ("system.raw", "auxiliary.bin",
                                               "machine-identifier.bin", "hardware-model.bin")],
            "guest_assets_created": False,
        }
        # Publish the containment identity to the outer failure path before
        # touching the journal. If a write or readback fails, the suspended
        # child is still killed by _spawn_reviewed_binary_suspended and the
        # parent can retry the signed record and prove the group is empty.
        spawn_pending.update({"path": pending_path, "record": pending_record,
                              "pid": pid, "process_group_id": process_group_id,
                              "readback_verified": False})
        write_errors: list[str] = []
        candidate_paths = (
            pending_path,
            unique_path("R6-VM-P2-TERMINATION-PENDING", run_id + "-write-retry"),
        )
        for candidate_path in candidate_paths:
            try:
                signed = signed_write(candidate_path, pending_record,
                                      "vm_p2_termination_pending_hmac_sha256", signing_key)
                pending_sha = vm._file_sha256(candidate_path)
                readback, readback_sha = read_signed_with_key(
                    candidate_path, "vm_p2_termination_pending_hmac_sha256", signing_key)
                if (readback_sha != pending_sha or readback != signed
                        or readback.get("helper_pid") != pid
                        or readback.get("helper_process_group_id") != process_group_id):
                    raise RuntimeError("suspended helper pending receipt failed exact readback")
                spawn_pending.update({"path": candidate_path, "record": signed,
                                      "sha256": pending_sha, "readback_verified": True,
                                      "pending_write_errors": write_errors})
                return
            except BaseException as error:
                write_errors.append(str(error)[:2000])
        spawn_pending["pending_write_errors"] = write_errors
        raise RuntimeError("could not persist/read back signed suspended-helper termination-pending evidence: "
                           + "; ".join(write_errors))

    try:
        executable_descriptor, executable_identity = _open_reviewed_binary_for_exec(
            binary, binary_record["sha256"], binary_record["file_identity"])
        child = _spawn_reviewed_binary_suspended(
            command, executable_descriptor, binary_record["sha256"],
            binary_record["file_identity"], binary_record["code_directory_hash"],
            on_spawned_failure=persist_spawn_termination_pending)
    except BaseException as error:
        monitor_failure_record = stop_monitor_after_prestart_failure(
            "reviewed helper process could not be created before guest mutation")
        spawn_scan = None
        if spawn_pending:
            clear_scans = 0
            while clear_scans < 2:
                try:
                    spawn_scan = _p2_process_scan_evidence(
                        required_absent_pids=(spawn_pending["pid"],),
                        required_absent_process_group_ids=(spawn_pending["process_group_id"],))
                    if (not spawn_scan["blocking_processes"]
                            and _process_group_presence_for_pid(
                                spawn_pending["process_group_id"]) is False):
                        clear_scans += 1
                    else:
                        clear_scans = 0
                except BaseException as scan_error:
                    error.add_note(f"suspended process-group scan unresolved: {scan_error}")
                    clear_scans = 0
                if clear_scans >= 2:
                    break
                try:
                    os.killpg(spawn_pending["process_group_id"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                print(json.dumps({"status": "P2_SUSPENDED_HELPER_CONTAINMENT_PENDING",
                                  "helper_pid": spawn_pending["pid"],
                                  "process_group_id": spawn_pending["process_group_id"],
                                  "clear_scan_count": clear_scans}, sort_keys=True),
                      file=sys.stderr, flush=True)
                time.sleep(1.0)
            if not spawn_pending.get("readback_verified"):
                # _spawn_reviewed_binary_suspended has already fail-closed by
                # killing/reaping the still-suspended child. Make one durable
                # retry from the parent before emitting the final launch
                # failure; the receipt explicitly records that OS termination
                # may have preceded this retry.
                retry_path = unique_path("R6-VM-P2-TERMINATION-PENDING", run_id + "-parent-retry")
                retry_record = dict(spawn_pending["record"])
                retry_record["termination_signals_sent_at_pending_write"] = [
                    "SIGKILL_PROCESS_GROUP_MAY_PRECEDE_PARENT_RETRY"]
                retry_record["failure_reason"] = (
                    str(retry_record.get("failure_reason", ""))[:2500]
                    + " | pre-termination pending write/readback retry: "
                    + "; ".join(spawn_pending.get("pending_write_errors", []))[:1200])
                try:
                    signed = signed_write(retry_path, retry_record,
                                          "vm_p2_termination_pending_hmac_sha256", signing_key)
                    retry_sha = vm._file_sha256(retry_path)
                    retry_readback, retry_readback_sha = read_signed_with_key(
                        retry_path, "vm_p2_termination_pending_hmac_sha256", signing_key)
                    if (retry_sha != retry_readback_sha or retry_readback != signed
                            or retry_readback.get("helper_pid") != spawn_pending["pid"]
                            or retry_readback.get("helper_process_group_id")
                            != spawn_pending["process_group_id"]):
                        raise RuntimeError("parent retry receipt failed exact signed readback")
                    spawn_pending.update({"path": retry_path, "record": signed,
                                          "sha256": retry_sha, "readback_verified": True,
                                          "parent_retry_after_os_termination": True})
                except BaseException as retry_error:
                    error.add_note(f"parent termination-pending retry failed: {retry_error}")
            if spawn_pending.get("readback_verified"):
                pending_readback, pending_sha = read_signed_with_key(
                    spawn_pending["path"], "vm_p2_termination_pending_hmac_sha256", signing_key)
                if (pending_sha != spawn_pending["sha256"]
                        or pending_readback.get("helper_pid") != spawn_pending["pid"]
                        or pending_readback.get("helper_process_group_id") != spawn_pending["process_group_id"]):
                    raise RuntimeError("suspended helper termination-pending receipt failed exact readback") from error
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_HELPER_LAUNCH_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": "process_creation",
            "error": str(error),
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "provisioner_binary_execution_identity": executable_identity,
            "helper_launch_environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
            "provisioner_pid": spawn_pending.get("pid"),
            "provisioner_process_group_id": spawn_pending.get("process_group_id"),
            "termination_pending_path": (str(spawn_pending["path"].relative_to(REPO))
                                         if spawn_pending.get("readback_verified") else None),
            "termination_pending_sha256": spawn_pending.get("sha256"),
            "termination_pending_readback_verified": bool(spawn_pending.get("readback_verified")),
            "termination_pending_write_errors": spawn_pending.get("pending_write_errors", []),
            "parent_retry_after_os_termination": spawn_pending.get(
                "parent_retry_after_os_termination", False),
            "post_termination_process_group_scan": spawn_scan,
            "post_termination_process_group_clear": (
                bool(spawn_pending) and isinstance(spawn_scan, Mapping)
                and not spawn_scan["blocking_processes"]
                and _process_group_presence_for_pid(spawn_pending["process_group_id"]) is False),
            "capability_claimed": execution_claimed_state["value"],
            "monitor_failure_record": monitor_failure_record,
            "guest_assets_created": base_dir.exists(),
        }
        signed_write(unique_path("R6-VM-P2-HELPER-LAUNCH-FAILURE", run_id), failure,
                     "vm_p2_helper_launch_failure_hmac_sha256", signing_key)
        raise RuntimeError("P2 reviewed helper process could not be created; signed launch failure recorded") from error
    finally:
        if executable_descriptor is not None:
            os.close(executable_descriptor)
    helper_candidate: HelperProcess | None = None
    try:
        helper_candidate = HelperProcess.__new__(HelperProcess)
        HelperProcess.__init__(helper_candidate, child, log_path, stderr_path)
        helper = helper_candidate
    except BaseException as error:
        monitor_failure_record = stop_monitor_after_prestart_failure(
            "helper event reader could not initialize before guest mutation")
        # This helper has not received START. Persist UNKNOWN_BLOCKING before
        # sending control or OS termination, then prove its dedicated group is
        # empty before returning from this pre-guest failure path.
        pending_path = unique_path("R6-VM-P2-TERMINATION-PENDING", run_id)
        pending = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_TERMINATION_PENDING",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "UNKNOWN_BLOCKING_TERMINATION_PENDING",
            "decision": "UNKNOWN_BLOCKING",
            "failure_stage": "event_reader_initialization",
            "failure_reason": str(error)[:4000],
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "helper_pid": child.pid,
            "helper_process_group_id": child.process_group_id,
            "helper_suspended_before_user_code": False,
            "start_command_attempted": False,
            "start_command_accepted": False,
            "vm_state": "UNKNOWN",
            "all_writable_handles_released": False,
            "installer_object_released": False,
            "disk_attachment_released": False,
            "termination_signals_sent_at_pending_write": [],
            "guest_asset_paths": [str(base_dir / name) for name in (
                "system.raw", "auxiliary.bin", "machine-identifier.bin", "hardware-model.bin")],
            "guest_assets_created": os.path.lexists(base_dir),
        }
        pending_write_errors: list[str] = []
        pending_sha256: str | None = None
        pending_readback_sha: str | None = None
        pending_readback_verified = False
        for candidate_path in (
                pending_path,
                unique_path("R6-VM-P2-TERMINATION-PENDING", run_id + "-reader-retry")):
            try:
                signed_pending = signed_write(candidate_path, pending,
                                              "vm_p2_termination_pending_hmac_sha256", signing_key)
                candidate_sha = vm._file_sha256(candidate_path)
                candidate_readback, candidate_readback_sha = read_signed_with_key(
                    candidate_path, "vm_p2_termination_pending_hmac_sha256", signing_key)
                if (candidate_sha != candidate_readback_sha
                        or candidate_readback != signed_pending
                        or candidate_readback.get("helper_pid") != child.pid
                        or candidate_readback.get("helper_process_group_id") != child.process_group_id):
                    raise RuntimeError("idle helper termination-pending receipt failed exact readback")
                pending_path = candidate_path
                pending_sha256 = candidate_sha
                pending_readback_sha = candidate_readback_sha
                pending_readback_verified = True
                break
            except BaseException as write_error:
                pending_write_errors.append(str(write_error)[:2000])
        if not pending_readback_verified:
            print(json.dumps({
                "status": "P2_IDLE_HELPER_PENDING_RECEIPT_WRITE_FAILED",
                "helper_pid": child.pid,
                "process_group_id": child.process_group_id,
                "errors": pending_write_errors,
            }, sort_keys=True), file=sys.stderr, flush=True)
        try:
            if child.stdin is not None and child.poll() is None:
                child.stdin.write("CANCEL\nSTOP\n")
                child.stdin.flush()
            child.wait(timeout=5)
        except BaseException:
            pass
        termination_signals: list[str] = []
        escalation_at = time.monotonic() + 5.0
        clear_scans = 0
        while clear_scans < 2:
            now = time.monotonic()
            if now >= escalation_at:
                try:
                    child.terminate()
                    termination_signals.append("SIGTERM_PROCESS_GROUP")
                except BaseException:
                    pass
                try:
                    child.wait(timeout=5)
                except BaseException:
                    pass
                try:
                    child.kill()
                    termination_signals.append("SIGKILL_PROCESS_GROUP")
                except BaseException:
                    pass
                escalation_at = time.monotonic() + 5.0
            try:
                scan = _p2_process_scan_evidence(
                    required_absent_pids=(child.pid,),
                    required_absent_process_group_ids=(child.process_group_id,))
                if (child.poll() is not None and not scan["blocking_processes"]
                        and _process_group_presence_for_pid(child.process_group_id) is False):
                    clear_scans += 1
                else:
                    clear_scans = 0
            except BaseException:
                clear_scans = 0
            print(json.dumps({"status": "P2_IDLE_HELPER_CONTAINMENT_PENDING",
                              "helper_pid": child.pid,
                              "process_group_id": child.process_group_id,
                              "clear_scan_count": clear_scans}, sort_keys=True),
                  file=sys.stderr, flush=True)
            if clear_scans < 2:
                time.sleep(1.0)
        if child.stdin is not None:
            try:
                child.stdin.close()
            except OSError:
                pass
        if helper_candidate is not None:
            for reader in (getattr(helper_candidate, "stdout_thread", None),
                           getattr(helper_candidate, "stderr_thread", None)):
                if isinstance(reader, threading.Thread) and reader.ident is not None:
                    while reader.is_alive():
                        reader.join(timeout=1)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        final_scan = _p2_process_scan_evidence(
            required_absent_pids=(child.pid,),
            required_absent_process_group_ids=(child.process_group_id,))
        if pending_readback_verified:
            pending_readback, pending_readback_sha = read_signed_with_key(
                pending_path, "vm_p2_termination_pending_hmac_sha256", signing_key)
        if ((pending_readback_verified and (
                pending_readback_sha != pending_sha256
                or pending_readback is None
                or pending_readback.get("helper_process_group_id") != child.process_group_id))
                or final_scan["blocking_processes"]
                or _process_group_presence_for_pid(child.process_group_id) is not False):
            raise RuntimeError("idle helper containment did not pass signed readback and empty process-group proof") from error
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_HELPER_LAUNCH_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": "event_reader_initialization",
            "error": str(error),
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "provisioner_binary_execution_identity": executable_identity,
            "helper_launch_environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
            "provisioner_pid": child.pid,
            "provisioner_process_group_id": child.process_group_id,
            "provisioner_process_code_directory_hash": child.code_directory_hash,
            "provisioner_executable_sha256_after_suspended_exec": child.executable_sha256_after_suspended_exec,
            "provisioner_executable_identity_after_suspended_exec": child.executable_identity_after_suspended_exec,
            "provisioner_exit_code": child.returncode,
            "termination_signals_sent": termination_signals,
            "termination_pending_path": (str(pending_path.relative_to(REPO))
                                         if pending_readback_verified else None),
            "termination_pending_sha256": pending_readback_sha,
            "termination_pending_readback_verified": pending_readback_verified,
            "termination_pending_write_errors": pending_write_errors,
            "post_termination_process_group_scan": final_scan,
            "post_termination_process_group_clear": True,
            "capability_claimed": execution_claimed_state["value"],
            "monitor_failure_record": monitor_failure_record,
            "stdout_reader_joined": (helper_candidate is None
                                      or not getattr(helper_candidate, "stdout_thread", None)
                                      or not helper_candidate.stdout_thread.is_alive()),
            "stderr_reader_joined": (helper_candidate is None
                                      or not getattr(helper_candidate, "stderr_thread", None)
                                      or not helper_candidate.stderr_thread.is_alive()),
            "guest_assets_created": base_dir.exists(),
        }
        signed_write(unique_path("R6-VM-P2-HELPER-LAUNCH-FAILURE", run_id), failure,
                     "vm_p2_helper_launch_failure_hmac_sha256")
        raise RuntimeError("P2 helper event reader failed to initialize; idle helper stopped and signed launch failure recorded") from error
    process_state["process"] = helper

    def fail_p2_transition(reason: str, *, stage: str,
                           start_command_attempted: bool = False,
                           graceful_exit_timeout_seconds: float = vm.VM_PHASE_STOP_TIMEOUT_SECONDS) -> None:
        monitor_state["external_failure_reason"] = reason
        normal_release_scan_stop.set()
        if normal_release_scan_thread is not None and normal_release_scan_thread.ident is not None:
            normal_release_scan_thread.join()
        if (monitor_thread is not None and monitor_thread.ident is not None
                and monitor_thread is not threading.current_thread()):
            # operation_done observes this request after a policy-approved
            # sample and routes post-START failures through the adapter
            # monitor's containment callbacks.
            monitor_thread.join()
        if not containment_state["drain_complete"].is_set():
            begin_emergency_shutdown(reason, stage=stage)
            for control in ("CANCEL", "STOP"):
                try:
                    helper.send(control)
                except BaseException as error:
                    containment_state["shutdown_errors"].append(f"{control}: {error}")
            drain_emergency_writers(graceful_exit_timeout_seconds)
        if monitor_thread is not None and monitor_thread.ident is not None:
            monitor_thread.join()
        shutdown_errors = containment_state["shutdown_errors"]
        forced_process_termination = containment_state["forced_process_termination"]
        termination_signals = containment_state["termination_signals"]
        post_scan = containment_state.get("final_writer_scan")
        post_termination_process_scan = (post_scan.get("process_scan")
                                         if isinstance(post_scan, Mapping) else None)
        post_termination_descriptor_scan = (post_scan.get("descriptor_scan")
                                            if isinstance(post_scan, Mapping) else None)
        post_termination_writer_scan_clear = (
            containment_state.get("writer_scans_clear") is True
            and isinstance(post_scan, Mapping) and post_scan.get("clear") is True
            and child.poll() is not None)
        if not post_termination_writer_scan_clear:
            raise RuntimeError("P2 failure shutdown lacks clear process-group and exact-descriptor evidence; signed pending receipt retained")
        try:
            helper.join(reader_timeout=5, process_timeout=5)
        except BaseException as error:
            shutdown_errors.append(f"reader_join: {error}")
            while helper.stdout_thread.is_alive() or helper.stderr_thread.is_alive():
                helper.stdout_thread.join(timeout=1)
                helper.stderr_thread.join(timeout=1)
        journal_receipt: dict[str, Any] | None = None
        journal = containment_state.get("journal")
        if journal is not None and getattr(journal, "fd", -1) >= 0:
            try:
                journal_receipt = journal.verify_and_close()
            except BaseException as error:
                shutdown_errors.append(f"containment journal final verification: {error}")
        pending_path = containment_state["pending_path"]
        pending_sha256 = containment_state.get("pending_sha256")
        if not pending_sha256 or not pending_path.is_file():
            raise RuntimeError("P2 termination pending receipt is missing after failure containment")
        pending_readback, pending_readback_sha = read_signed_with_key(
            pending_path, "vm_p2_termination_pending_hmac_sha256", signing_key)
        if (pending_readback_sha != pending_sha256
                or pending_readback.get("run_id") != run_id
                or pending_readback.get("helper_process_group_id") != child.process_group_id):
            raise RuntimeError("P2 termination pending receipt failed exact signed readback")
        empty_template_directory_removed = False
        guest_assets_created = base_dir.exists()
        try:
            if (child.poll() is not None
                    and (monitor_thread is None or not monitor_thread.is_alive())
                    and post_termination_writer_scan_clear
                    and base_dir.exists() and not base_dir.is_symlink()
                    and base_dir.is_dir() and not os.listdir(base_dir)):
                os.rmdir(base_dir)
                empty_template_directory_removed = True
                _fsync_directory(base_dir.parent)
                guest_assets_created = False
            elif base_dir.exists() and not base_dir.is_symlink() and base_dir.is_dir():
                guest_assets_created = bool(os.listdir(base_dir))
        except OSError as error:
            shutdown_errors.append(f"empty_template_directory_cleanup: {error}")
        monitor_receipt: dict[str, Any] = {}
        result = monitor_state.get("result")
        monitor_record_source = "p2_adapter_monitor_result"
        if not isinstance(result, Mapping):
            monitor_error = monitor_state.get("error")
            exception_record = getattr(monitor_error, "record", None)
            if isinstance(exception_record, Mapping):
                result = exception_record
                monitor_record_source = "VMPhaseViolation.record"
        persistence_error: str | None = None
        if isinstance(result, Mapping):
            try:
                (_parsed, compressed_sha, raw_sha, raw_bytes, compressed_bytes) = (
                    _persist_large_signed_monitor(monitor_path, result))
                monitor_receipt = {
                    "path": str(monitor_path.relative_to(REPO)),
                    "compressed_sha256": compressed_sha,
                    "uncompressed_sha256": raw_sha,
                    "uncompressed_bytes": raw_bytes,
                    "compressed_bytes": compressed_bytes,
                    "status": result.get("status"),
                    "sample_count": result.get("sample_count"),
                    "record_source": monitor_record_source,
                }
            except BaseException as error:
                persistence_error = str(error)
        release_events = helper.events_named("handles_released")
        helper_release_event = release_events[-1] if len(release_events) == 1 else None
        helper_release_proven = bool(
            child.poll() is not None
            and not forced_process_termination
            and isinstance(helper_release_event, Mapping)
            and helper_release_event.get("vm_state") in {"STOPPED", "NO_VM_CREATED"}
            and helper_release_event.get("all_writable_handles_released") is True
            and helper_release_event.get("installer_object_released") is True
            and helper_release_event.get("disk_attachment_released") is True)
        vm_state_after_failure = (
            helper_release_event.get("vm_state") if helper_release_proven
            else "UNKNOWN")
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_STARTUP_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": stage,
            "failure_reason": reason,
            "start_command_attempted": start_command_attempted,
            "start_command_accepted": start_command_accepted,
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_pid": child.pid,
            "provisioner_process_group_id": child.process_group_id,
            "provisioner_exit_code": child.returncode,
            "provisioner_log_path": str(log_path.relative_to(REPO)),
            "termination_signals_sent": termination_signals,
            "termination_pending_path": str(pending_path.relative_to(REPO)),
            "termination_pending_sha256": pending_readback_sha,
            "termination_pending_readback_verified": True,
            "containment_monitor_journal": journal_receipt,
            "containment_monitor_journal_error": getattr(journal, "error", None),
            "provisioner_process_reaped": child.poll() is not None,
            "vm_state": vm_state_after_failure,
            "vm_stop_proven": vm_state_after_failure == "STOPPED",
            "helper_release_event": helper_release_event,
            "helper_release_proven_without_forced_termination": helper_release_proven,
            "all_writable_handles_released": helper_release_proven,
            "post_termination_process_scan": post_termination_process_scan,
            "post_termination_descriptor_scan": post_termination_descriptor_scan,
            "post_termination_writer_scan_clear": post_termination_writer_scan_clear,
            "stdout_reader_joined": not helper.stdout_thread.is_alive(),
            "stderr_reader_joined": not helper.stderr_thread.is_alive(),
            "reader_errors": list(helper.reader_errors),
            "shutdown_errors": shutdown_errors,
            "forced_process_termination": forced_process_termination,
            "helper_process_still_running": child.poll() is None,
            "helper_process_group_still_present": _process_group_presence() is not False,
            "validated_monitor_samples_before_start": monitor_ready_evidence["validated_sample_count"],
            "monitor_gate_accepted": monitor_ready_evidence["monitor_gate_accepted"],
            "monitor_gate_sample_count": monitor_ready_evidence.get("monitor_gate_sample_count"),
            "monitor_heartbeats_accepted": monitor_ready_evidence["monitor_heartbeats_accepted"],
            "last_monitor_control_acknowledgement": monitor_ready_evidence.get("last_control_acknowledgement"),
            "monitor_thread_still_running": bool(
                monitor_thread is not None and monitor_thread.is_alive()),
            "monitor_result": monitor_receipt,
            "monitor_result_persistence_error": persistence_error,
            "monitor_error": str(monitor_state.get("error")) if monitor_state.get("error") else None,
            "monitor_exception_type": type(monitor_state["error"]).__name__
            if monitor_state.get("error") is not None else None,
            "empty_template_directory_removed": empty_template_directory_removed,
            "guest_assets_created": guest_assets_created,
        }
        failure_path = unique_path("R6-VM-P2-STARTUP-FAILURE", run_id)
        signed_write(failure_path, failure, "vm_p2_startup_failure_hmac_sha256")
        raise RuntimeError("P2 transition failed; helper termination/reaping and signed failure evidence recorded: "
                           + reason)

    assert monitor_thread is not None

    authorization_payload = "|".join((
        "P2AUTH1", run_id, str(admission["session_id"]), admission_sha,
        active_marker_sha, launch_manifest["source_manifest"]["binary"]["sha256"],
        "PROVISION_MACOS_GUEST_BASE_ONLY", "P2",
    ))
    try:
        authorization_sent = helper.send(
            _signed_helper_control("AUTH", authorization_payload, control_key,
                                   control_token=control_token))
        authorization_event = helper.wait_for_any_event(
            {"p2_authorization_accepted", "p2_authorization_rejected"}, 5) if authorization_sent else None
    except BaseException as error:
        fail_p2_transition(str(error), stage="p2_authorization")
    if not authorization_sent:
        fail_p2_transition("reviewed helper exited before P2 launch authorization was delivered",
                           stage="p2_authorization")
    if not authorization_event or authorization_event.get("event") != "p2_authorization_accepted":
        fail_p2_transition("reviewed helper did not accept the admission-bound P2 launch authorization",
                           stage="p2_authorization")
    monitor_ready_evidence["authorization_event"] = authorization_event

    if not monitor_ready.wait(10):
        fail_p2_transition(str(monitor_state.get("error") or "three complete monitor samples timed out"),
                           stage="monitor_gate")
    if (monitor_ready_evidence.get("validated_sample_count", 0) < 3
            or monitor_ready_evidence.get("monitor_gate_accepted") is not True):
        fail_p2_transition("startup gate opened without three complete samples and helper-accepted monitor gate",
                           stage="monitor_gate")

    if not monitor_thread.is_alive():
        fail_p2_transition(str(monitor_state.get("error") or "monitor stopped before helper authorization"),
                           stage="monitor_gate")
    try:
        if not base_dir.parent.exists():
            os.mkdir(base_dir.parent, 0o700)
            _fsync_directory(VM_ROOT)
        if base_dir.parent.is_symlink() or not base_dir.parent.is_dir():
            raise RuntimeError("templates directory changed type before P2 asset creation")
        os.mkdir(base_dir, 0o700)
        _fsync_directory(base_dir.parent)
        _fsync_directory(VM_ROOT)
        start_command_attempted = True
        if not helper.send("START"):
            raise RuntimeError("reviewed helper exited before the P2 START command was delivered")
        start_event = helper.wait_for_event("start_command_received", 5)
        if (not start_event or start_event.get("accepted") is not True
                or start_event.get("p2_authorization_accepted") is not True
                or start_event.get("monitor_gate_accepted") is not True
                or start_event.get("monitor_sequence", 0) < 3):
            raise RuntimeError("reviewed helper did not acknowledge an accepted P2 START command")
        start_command_accepted = True
        monitor_ready_evidence["start_event"] = start_event
    except BaseException as error:
        fail_p2_transition(str(error), stage="template_directory_or_start_command",
                           start_command_attempted=start_command_attempted)
    # Keep the accepted monitor active for the entire installer lifetime. A
    # bounded shutdown timeout is applied only after installer_result, inside
    # operation_done, while the capacity sampler continues to run.
    monitor_thread.join()
    normal_release_scan_stop.set()
    if normal_release_scan_thread is not None and normal_release_scan_thread.ident is not None:
        normal_release_scan_thread.join()
    with normal_release_scan_state["lock"]:
        monitor_ready_evidence["normal_release_writer_scan"] = normal_release_scan_state["latest"]
        monitor_ready_evidence["normal_release_writer_scan_clear_count"] = (
            normal_release_scan_state["consecutive_clear_scans"])
    if monitor_state.get("error") is not None:
        fail_p2_transition(
            str(monitor_state["error"]), stage="monitor_failure_after_start",
            start_command_attempted=start_command_attempted,
            graceful_exit_timeout_seconds=0.0)
    try:
        helper_returncode = helper.join(reader_timeout=5, process_timeout=5)
    except BaseException as error:
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_HELPER_OUTPUT_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": "helper_process_or_reader_join",
            "error": str(error),
            "helper_pid": child.pid,
            "helper_exit_code": child.poll(),
            "helper_wait_completed": child.poll() is not None,
            "stdout_reader_joined": not helper.stdout_thread.is_alive(),
            "stderr_reader_joined": not helper.stderr_thread.is_alive(),
            "reader_errors": list(helper.reader_errors),
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "provisioner_source_manifest_sha256": launch_manifest_sha,
            "provisioner_review_sha256": launch_review_sha,
            "guest_assets_created": base_dir.exists(),
        }
        signed_write(unique_path("R6-VM-P2-HELPER-OUTPUT-FAILURE", run_id), failure,
                     "vm_p2_helper_output_failure_hmac_sha256")
        raise RuntimeError("P2 helper exited without bounded output-reader joins; signed failure recorded") from error
    if helper.reader_errors:
        failure = {
            "schema_version": 1,
            "record_type": "R6_VM_P2_HELPER_OUTPUT_FAILURE",
            "run_id": run_id,
            "created_at_unix": time.time(),
            "status": "FAIL",
            "failure_stage": "helper_output_reader_error",
            "helper_pid": child.pid,
            "helper_exit_code": helper_returncode,
            "helper_wait_completed": True,
            "stdout_reader_joined": not helper.stdout_thread.is_alive(),
            "stderr_reader_joined": not helper.stderr_thread.is_alive(),
            "reader_errors": list(helper.reader_errors),
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "guest_assets_created": base_dir.exists(),
        }
        signed_write(unique_path("R6-VM-P2-HELPER-OUTPUT-FAILURE", run_id), failure,
                     "vm_p2_helper_output_failure_hmac_sha256")
        raise RuntimeError("P2 helper output reader failed; signed failure recorded")
    monitor_result = monitor_state.get("result")
    monitor_error = monitor_state.get("error")
    allocation_record = vm._seal({
        "schema_version": 1,
        "record_type": "R6_VM_P2_FILE_ALLOCATION_MONITOR",
        "run_id": run_id,
        "created_at_unix": time.time(),
        "status": "PASS" if monitor_error is None and isinstance(monitor_result, Mapping)
        and monitor_result.get("status") == "PASS_PHASE_COMPLETE" else "FAIL",
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "provisioner_source_sha256": admission["provisioner_source_sha256"],
        "provisioner_binary_sha256": admission["provisioner_binary_sha256"],
        "sample_count": len(file_samples),
        "sample_interval_target_seconds": 0.001,
        "maximum_allowed_monitor_gap_seconds": vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS,
        "system_raw_physical_cap_bytes": P2_SYSTEM_MAX,
        "auxiliary_physical_cap_bytes": P2_AUX_MAX,
        "metadata_physical_cap_bytes": P2_METADATA_MAX,
        "samples": file_samples,
        "monitor_error": str(monitor_error) if monitor_error is not None else None,
    }, "vm_p2_file_allocation_monitor_hmac_sha256")
    _, allocation_compressed_sha, allocation_raw_sha, allocation_uncompressed_bytes, allocation_compressed_bytes = (
        _persist_large_signed_record(allocation_path, allocation_record,
                                     "vm_p2_file_allocation_monitor_hmac_sha256"))
    metadata_after_allocation_monitor = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    if monitor_error is not None:
        monitor_receipt = getattr(monitor_error, "record", None)
        if monitor_receipt is not None and not monitor_path.exists():
            _persist_large_signed_monitor(monitor_path, monitor_receipt)
        raise RuntimeError(f"signed P2 live monitor failed: {monitor_error}")
    if not isinstance(monitor_result, Mapping) or monitor_result.get("status") != "PASS_PHASE_COMPLETE":
        raise RuntimeError("P2 live monitor did not produce a signed stopped-state PASS")
    monitor_result, monitor_compressed_sha, monitor_uncompressed_sha, monitor_uncompressed_bytes, monitor_compressed_bytes = (
        _persist_large_signed_monitor(monitor_path, monitor_result))
    metadata_after_monitor = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    installer_events = helper.events_named("installer_result")
    result_events = helper.events_named("helper_result")
    configuration_events = helper.events_named("configuration_validated")
    restore_hash_events = helper.events_named("restore_hash_verified")
    authorization_events = helper.events_named("p2_authorization_accepted")
    authorization_rejected_events = helper.events_named("p2_authorization_rejected")
    monitor_gate_events = helper.events_named("monitor_gate_accepted")
    monitor_gate_rejected_events = helper.events_named("monitor_gate_rejected")
    monitor_heartbeat_events = helper.events_named("monitor_heartbeat_accepted")
    monitor_heartbeat_rejected_events = helper.events_named("monitor_heartbeat_rejected")
    start_events = helper.events_named("start_command_received")
    release_events = helper.events_named("handles_released")
    terminal_events = helper.events_named("installer_terminal")
    invocation_events = helper.events_named("installer_invocation_started")
    progress_events = helper.events_named("installer_progress")
    restore_identity_events = helper.events_named("installer_restore_identity_verified")
    restore_identity_after_constructor_events = helper.events_named("installer_restore_identity_after_construction")
    restore_identity_at_start_events = helper.events_named("installer_restore_identity_verified_at_start")
    runtime_configuration_events = helper.events_named("configuration_runtime_readback")
    system_created_events = helper.events_named("system_raw_created")
    auxiliary_created_events = helper.events_named("auxiliary_storage_created")
    platform_identity_events = helper.events_named("platform_identity_persisted")
    provision_start_events = helper.events_named("provision_start_authorized")
    installer_start_events = helper.events_named("installer_started")
    accepted_monitor_sequences = [row.get("monitor_sequence") for row in
                                  [*monitor_gate_events, *monitor_heartbeat_events]]
    if (len(authorization_events) != 1 or authorization_rejected_events
            or len(monitor_gate_events) != 1 or monitor_gate_rejected_events
            or monitor_heartbeat_rejected_events
            or start_command_accepted is not True
            or len(start_events) != 1 or start_events[0].get("accepted") is not True
            or start_events[0].get("p2_authorization_accepted") is not True
            or start_events[0].get("monitor_gate_accepted") is not True
            or not isinstance(start_events[0].get("monitor_sequence"), int)
            or isinstance(start_events[0].get("monitor_sequence"), bool)
            or start_events[0]["monitor_sequence"] < 3
            or any(not isinstance(value, int) or isinstance(value, bool) for value in accepted_monitor_sequences)
            or accepted_monitor_sequences != sorted(accepted_monitor_sequences)
            or len(set(accepted_monitor_sequences)) != len(accepted_monitor_sequences)
            or len(monitor_heartbeat_events) != monitor_ready_evidence["monitor_heartbeats_accepted"]):
        raise RuntimeError("P2 helper authorization, live monitor gate/heartbeat, or START acknowledgements are incomplete")
    configuration = configuration_events[-1] if configuration_events else {}
    readback = configuration.get("runtime_readback_counts", {})
    released = result_events[-1].get("release", {}) if result_events else {}
    if (len(release_events) != 1
            or release_events[0].get("vm_state") != "STOPPED"
            or released.get("vm_state") != "STOPPED"
            or not isinstance(release_events[0].get("stopped_observed_at_unix"), (int, float))
            or not isinstance(release_events[0].get("stopped_observed_monotonic_seconds"), (int, float))
            or release_events[0].get("observer_source")
            != "VZVirtualMachine.state polled by ProvisioningSession.stopAndRelease"):
        raise RuntimeError("P2 helper did not record an actual stopped-state observation before owner release")
    config_record = configuration.get("configuration_record", {})
    expected_empty_arrays = ("networkDevices", "directorySharingDevices", "serialDevices",
                             "serialPorts", "usbDevices", "usbControllers", "socketDevices",
                             "consoleDevices", "graphicsDevices")
    if (len(configuration_events) != 1 or len(runtime_configuration_events) != 1
            or configuration.get("validation_result") != "success"
            or configuration.get("event") != "configuration_validated"
            or not isinstance(config_record, Mapping)
            or any(config_record.get(key) != [] for key in expected_empty_arrays)
            or config_record.get("storageDevices") != [admission["system_raw_path"]]
            or config_record.get("restorePath") != str(RESTORE_PATH)
            or config_record.get("hardwareModelSHA256") != admission["hardware_model_sha256"]
            or config_record.get("machineIdentifierSHA256") != admission["machine_identifier_sha256"]
            or config_record.get("auxiliaryStoragePath") != admission["auxiliary_storage_path"]
            or config_record.get("auxiliaryStorageHardwareModelSHA256") != admission["hardware_model_sha256"]
            or config_record.get("bootLoaderType") != "VZMacOSBootLoader"
            or config_record.get("platformType") != "VZMacPlatformConfiguration"
            or config_record.get("validationOccurredBeforeVMCreation") is not True
            or runtime_configuration_events[0].get("vm_created") is not True
            or runtime_configuration_events[0].get("vm_started") is not False
            or runtime_configuration_events[0].get("storage_path") != admission["system_raw_path"]
            or runtime_configuration_events[0].get("network_devices") != []
            or runtime_configuration_events[0].get("directory_sharing_devices") != []
            or runtime_configuration_events[0].get("serial_devices") != []
            or runtime_configuration_events[0].get("usb_devices") != []
            or runtime_configuration_events[0].get("socket_devices") != []):
        raise RuntimeError("P2 helper configuration did not record the exact validated, isolated stock-base configuration")
    if (len(system_created_events) != 1
            or system_created_events[0].get("api") != "openat(O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW) plus ftruncate and fsync"
            or system_created_events[0].get("file_format") != "sparse RAW regular file"
            or system_created_events[0].get("exclusive_creation") is not True
            or len(auxiliary_created_events) != 1
            or auxiliary_created_events[0].get("api") != "VZMacAuxiliaryStorage(creatingStorageAt:hardwareModel:options:)"
            or auxiliary_created_events[0].get("hardware_model_sha256") != admission["hardware_model_sha256"]):
        raise RuntimeError("P2 guest storage creation events do not bind the admitted RAW and matching auxiliary API")
    if (len(platform_identity_events) != 1
            or platform_identity_events[0].get("machine_identifier_sha256") != admission["machine_identifier_sha256"]
            or platform_identity_events[0].get("machine_identifier_data_base64") != admission["machine_identifier_data_base64"]
            or platform_identity_events[0].get("machine_identifier_creation_method") != admission["machine_identifier_creation"]["method"]
            or platform_identity_events[0].get("machine_identifier_created_at_unix") != admission["machine_identifier_creation"]["created_at_unix"]
            or platform_identity_events[0].get("machine_identifier_reused_unchanged_through_install_and_shutdown") is not True
            or platform_identity_events[0].get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or platform_identity_events[0].get("hardware_model_data_base64") != admission["hardware_model_data_base64"]
            or platform_identity_events[0].get("hardware_model_source") != "VZMacOSRestoreImage.mostFeaturefulSupportedConfiguration for the P1-accepted local IPSW"
            or platform_identity_events[0].get("restore_image_supported") is not True):
        raise RuntimeError("P2 helper did not bind the planned machine identifier and exact P1-compatible hardware model bytes")
    if (len(restore_identity_events) != 1
            or restore_identity_events[0].get("path") != str(RESTORE_PATH)
            or restore_identity_events[0].get("sha256") != RESTORE_SHA256
            or restore_identity_events[0].get("hardware_model_sha256") != P2_LOCAL_HARDWARE_MODEL_SHA256
            or restore_identity_events[0].get("restore_subject") != {"macos_version": "27.0.1", "macos_build": "26A434"}
            or not isinstance(restore_identity_events[0].get("file_identity"), Mapping)
            or not isinstance(restore_identity_events[0].get("verification_method"), str)):
        raise RuntimeError("P2 helper did not verify exact restore identity immediately before installer construction")
    if (len(restore_identity_after_constructor_events) != 1
            or restore_identity_after_constructor_events[0].get("identity_matches_preconstruction") is not True
            or restore_identity_after_constructor_events[0].get("sha256") != RESTORE_SHA256
            or restore_identity_after_constructor_events[0].get("path") != str(RESTORE_PATH)
            or len(restore_identity_at_start_events) != 1
            or restore_identity_at_start_events[0].get("sha256") != RESTORE_SHA256
            or restore_identity_at_start_events[0].get("path") != str(RESTORE_PATH)
            or restore_identity_at_start_events[0].get("verification_method")
            != "O_NOFOLLOW descriptor fstat plus path lstat matched signed P2 admission immediately before VZMacOSInstaller.install"):
        raise RuntimeError("P2 restore identity changed around installer construction or was not revalidated at install start")
    for identity_key in ("device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns", "ctime_ns"):
        if restore_identity_events[0]["file_identity"].get(identity_key) != admission["restore_file_identity"].get(identity_key):
            raise RuntimeError("restore identity at installer construction differs from signed P2 admission")
        if restore_identity_at_start_events[0].get("file_identity", {}).get(identity_key) != admission["restore_file_identity"].get(identity_key):
            raise RuntimeError("restore identity at installer invocation differs from signed P2 admission")
    if (len(invocation_events) != 1 or not progress_events
            or not installer_start_events or not terminal_events or not installer_events):
        raise RuntimeError("P2 installer lifecycle lacks start, invocation, progress, or terminal event evidence")
    if (installer_start_events[0].get("api") != "VZMacOSInstaller.install(completionHandler:)"
            or installer_start_events[0].get("restore_sha256") != RESTORE_SHA256
            or installer_events[0].get("installer_completed") is not True
            or installer_events[0].get("failure") is not None
            or terminal_events[0].get("installer_completed") is not True):
        raise RuntimeError("P2 installer source or successful terminal result differs from the accepted local restore")
    installer_started_at = event_monotonic_seconds(installer_start_events[0])
    invocation_at = event_monotonic_seconds(invocation_events[0])
    progress_times = [event_monotonic_seconds(event) for event in progress_events]
    terminal_at = event_monotonic_seconds(terminal_events[0])
    installer_result_at = event_monotonic_seconds(installer_events[0])
    if (installer_started_at >= invocation_at or progress_times != sorted(progress_times)
            or any(value < invocation_at or value > installer_result_at for value in progress_times)
            or not invocation_at <= terminal_at <= installer_result_at):
        raise RuntimeError("P2 installer start, progress, and terminal event timestamps are not monotonically ordered")
    lifecycle_times = [event_monotonic_seconds(event) for event in (
        authorization_events[0], monitor_gate_events[0], start_events[0], provision_start_events[0],
        restore_hash_events[0], platform_identity_events[0],
        system_created_events[0], auxiliary_created_events[0], configuration_events[0],
        runtime_configuration_events[0], restore_identity_events[0],
        restore_identity_at_start_events[0], installer_start_events[0])]
    heartbeat_times = [event_monotonic_seconds(event) for event in monitor_heartbeat_events]
    if (lifecycle_times != sorted(lifecycle_times)
            or heartbeat_times != sorted(heartbeat_times)
            or any(value < event_monotonic_seconds(monitor_gate_events[0]) for value in heartbeat_times)
            or event_monotonic_seconds(configuration_events[0])
            >= event_monotonic_seconds(runtime_configuration_events[0])
            or event_monotonic_seconds(restore_identity_at_start_events[0])
            >= event_monotonic_seconds(installer_start_events[0])):
        raise RuntimeError("P2 configuration or restore identity was not verified before installer start")
    if (helper_returncode != 0 or not installer_events or not result_events
            or len(configuration_events) != 1 or len(restore_hash_events) != 1
            or len(start_events) != 1 or start_events[-1].get("accepted") is not True
            or len(release_events) != 1
            or len(terminal_events) != 1 or terminal_events[-1].get("installer_completed") is not True
            or len(system_created_events) != 1
            or system_created_events[-1].get("path") != admission["system_raw_path"]
            or system_created_events[-1].get("logical_capacity_bytes") != SYSTEM_LOGICAL_CAPACITY
            or len(auxiliary_created_events) != 1
            or auxiliary_created_events[-1].get("path") != admission["auxiliary_storage_path"]
            or len(platform_identity_events) != 1
            or len(provision_start_events) != 1
            or len(installer_start_events) != 1
            or installer_events[-1].get("installer_completed") is not True
            or installer_events[-1].get("restore_sha256") != RESTORE_SHA256
            or result_events[-1].get("installer_completed") is not True
            or result_events[-1].get("restore_sha256") != RESTORE_SHA256
            or result_events[-1].get("hardware_model_sha256") != admission["hardware_model_sha256"]
            or result_events[-1].get("machine_identifier_sha256") != admission["machine_identifier_sha256"]
            or released.get("vm_state") != "STOPPED"
            or released.get("all_writable_handles_released") is not True
            or released.get("installer_object_released") is not True
            or released.get("disk_attachment_released") is not True
            or release_events[-1].get("all_writable_handles_released") is not True
            or release_events[-1].get("installer_object_released") is not True
            or release_events[-1].get("disk_attachment_released") is not True
            or release_events[-1].get("vm_state") != "STOPPED"
            or restore_hash_events[-1].get("sha256") != RESTORE_SHA256
            or configuration.get("cpu_count") != manifest_cpu(admission)
            or configuration.get("memory_bytes") != 4 * GIB
            or configuration.get("storage_device_count") != 1
            or configuration.get("network_devices") != 0
            or configuration.get("directory_sharing_devices") != 0
            or configuration.get("serial_ports") != 0
            or configuration.get("console_devices") != 0
            or configuration.get("socket_devices") != 0
            or configuration.get("usb_controllers") != 0
            or configuration.get("graphics_devices") != 0
            or not isinstance(readback, Mapping)
            or any(readback.get(key) != 0 for key in (
                "network_devices", "directory_sharing_devices", "console_devices",
                "socket_devices", "usb_controllers", "graphics_devices"))
            or configuration.get("hardware_model_sha256") != admission["hardware_model_sha256"]
            or configuration.get("machine_identifier_sha256") != admission["machine_identifier_sha256"]
            or configuration.get("restore_subject") != {"macos_version": "27.0.1", "macos_build": "26A434"}):
        failure = installer_events[-1].get("failure") if installer_events else None
        raise RuntimeError(f"P2 helper did not prove exact configuration, IPSW identity, install completion, and release; exit={helper_returncode}; failure={failure}")
    allocation = _template_allocations(base_dir)
    if (allocation["system_bytes"] > P2_SYSTEM_MAX or allocation["auxiliary_bytes"] > P2_AUX_MAX
            or allocation["metadata_bytes"] > P2_METADATA_MAX):
        raise RuntimeError("final P2 file allocation exceeds a signed component ceiling")
    system_hash, system_size, system_identity = sha256_file(Path(admission["system_raw_path"]))
    auxiliary_hash, auxiliary_size, auxiliary_identity = sha256_file(Path(admission["auxiliary_storage_path"]))
    if system_size != SYSTEM_LOGICAL_CAPACITY or auxiliary_size > AUXILIARY_LOGICAL_CAPACITY:
        raise RuntimeError("final system or auxiliary logical size differs from the admitted schema")
    hardware_hash, _, hardware_identity = sha256_file(base_dir / "hardware-model.bin")
    machine_hash, _, machine_identity = sha256_file(base_dir / "machine-identifier.bin")
    if (hardware_hash != admission["hardware_model_sha256"]
            or machine_hash != admission["machine_identifier_sha256"]):
        raise RuntimeError("persisted hardware or machine identifier differs from the P2 admission")
    installer_record = {
        "installer_completed": True,
        "installer_api": installer_start_events[0].get("api"),
        "installer_source": "VZMacOSInstaller constructed for the exact accepted local IPSW and admitted VM",
        "installer_started_at_utc": utc_timestamp(installer_start_events[0].get("captured_at_unix")),
        "installer_terminal_at_utc": utc_timestamp(terminal_events[0].get("captured_at_unix")),
        "installer_result_at_utc": utc_timestamp(installer_events[0].get("captured_at_unix")),
        "installer_start_event": installer_start_events[0],
        "installer_invocation_event": invocation_events[0],
        "installer_progress_events": progress_events,
        "installer_terminal_event": terminal_events[0],
        "installer_result_event": installer_events[0],
        "installer_restore_identity_event": restore_identity_events[0],
        "installer_restore_identity_after_constructor_event": restore_identity_after_constructor_events[0],
        "installer_restore_identity_at_start_event": restore_identity_at_start_events[0],
        "installer_restore_file_identity": restore_identity_events[0]["file_identity"],
        "installer_restore_sha256": RESTORE_SHA256,
        "installer_restore_path": str(RESTORE_PATH),
        "installer_restore_subject": {"product_version": "27.0.1", "build_version": "26A434",
                                       "architecture": "arm64"},
        "hardware_model_sha256": P2_LOCAL_HARDWARE_MODEL_SHA256,
        "machine_identifier_sha256": admission["machine_identifier_sha256"],
        "machine_identifier_creation": admission["machine_identifier_creation"],
        "vm_state": "STOPPED",
        "all_writable_handles_released": True,
        "serial_and_attachment_handles_released": True,
        "guest_bootstrap_receipt_verified": False,
        "system_image_sha256": system_hash,
        "auxiliary_storage_sha256": auxiliary_hash,
        "hardware_model_sha256": hardware_hash,
        "macos_version": "27.0.1",
        "macos_build": "26A434",
        "architecture": "arm64",
        "system_image_logical_capacity_bytes": system_size,
        "auxiliary_logical_capacity_bytes": auxiliary_size,
        "guest_version_independently_verified": False,
    }
    if not vm.verify_signed_record(monitor_result, "vm_phase_monitor_hmac_sha256"):
        raise RuntimeError("persisted P2 monitor result HMAC did not verify")
    monitor_summary_record = {
        key: monitor_result[key]
        for key in ("schema_version", "mode", "session_id", "phase_id", "status",
                    "host_build_version", "volume_uuid", "safety_volume_uuid",
                    "safety_reserve_bytes", "controller_identity", "g0c_identity",
                    "phase_quiescent", "vm_state", "all_writable_handles_released",
                    "serial_and_attachment_handles_released", "stop_measurement",
                    "stop_safety_profile_mode", "p2_admission_sha256", "sample_count",
                    "maximum_observed_monitor_gap_seconds")
    }
    monitor_summary_record.update({
        "run_id": run_id,
        "created_at_unix": time.time(),
        "full_signed_monitor_result_path": str(monitor_path.relative_to(REPO)),
        "full_signed_monitor_result_compressed_sha256": monitor_compressed_sha,
        "full_signed_monitor_result_uncompressed_sha256": monitor_uncompressed_sha,
        "full_signed_monitor_result_uncompressed_bytes": monitor_uncompressed_bytes,
        "full_signed_monitor_sample_count": monitor_result.get("sample_count"),
    })
    signed_write(monitor_summary_path, monitor_summary_record, "vm_phase_monitor_hmac_sha256")
    monitor_summary, _summary_sha = read_signed(monitor_summary_path, "vm_phase_monitor_hmac_sha256")
    vm.record_p2_install_result(installer_result=installer_record,
                                phase_monitor_result=monitor_summary,
                                p2_admission_path=admission_path,
                                receipt_path=result_path)
    metadata_after_install_result = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    execution_record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_EXECUTION_RESULT",
        "run_id": run_id,
        "status": "PASS_INSTALL_COMPLETE",
        "created_at_unix": time.time(),
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "provisioner_launch_verification": {
            "checked_at_unix": launch_provenance_checked_at_unix,
            "checked_at_utc": utc_timestamp(launch_provenance_checked_at_unix),
            "manifest_path": admission["provisioner_source_manifest_path"],
            "manifest_sha256": launch_manifest_sha,
            "review_path": admission["provisioner_review_path"],
            "review_sha256": launch_review_sha,
            "source_sha256": launch_manifest["source_manifest"]["source"]["sha256"],
            "orchestration_source_sha256": launch_manifest["source_manifest"]["orchestration_source"]["sha256"],
            "binary_path": admission["provisioner_binary_path"],
            "binary_sha256": launch_manifest["source_manifest"]["binary"]["sha256"],
            "binary_code_directory_hash": launch_manifest["source_manifest"]["binary"]["code_directory_hash"],
            "controller_dependencies": [
                {"path": item["path"], "sha256": item["sha256"]}
                for item in launch_manifest["source_manifest"]["controller_dependencies"]
            ],
            "binary_execution_identity": executable_identity,
            "process_code_directory_hash": child.code_directory_hash,
            "executable_sha256_after_suspended_exec": child.executable_sha256_after_suspended_exec,
            "executable_identity_after_suspended_exec": child.executable_identity_after_suspended_exec,
            "helper_launch_environment": dict(P2_HELPER_LAUNCH_ENVIRONMENT),
            "helper_launch_method": "reviewed-path suspended exec with open-file and pathname identity plus SHA-256 revalidated before resume, matching kernel CodeDirectory hash, and sanitized environment",
            "review_decision": launch_review["decision"],
            "verification_immediately_preceded_helper_launch": True,
            "p2_authorization_event": authorization_events[0],
            "monitor_gate_event": monitor_gate_events[0],
            "monitor_gate_validated_sample_count": monitor_ready_evidence["monitor_gate_sample_count"],
            "monitor_heartbeat_events": monitor_heartbeat_events,
            "start_command_event": start_events[0],
        },
        "claim": "VZMacOSInstaller completed successfully using the P1-verified local macOS 27.0.1 build 26A434 restore; no guest-side version readback was performed.",
        "installer_result_path": str(result_path.relative_to(REPO)),
        "installer_result_sha256": vm._file_sha256(result_path),
        "monitor_result_path": str(monitor_path.relative_to(REPO)),
        "monitor_result_sha256": monitor_compressed_sha,
        "monitor_result_uncompressed_sha256": monitor_uncompressed_sha,
        "monitor_result_uncompressed_bytes": monitor_uncompressed_bytes,
        "monitor_result_compressed_bytes": monitor_compressed_bytes,
        "monitor_summary_path": str(monitor_summary_path.relative_to(REPO)),
        "monitor_summary_sha256": vm._file_sha256(monitor_summary_path),
        "file_allocation_monitor_path": str(allocation_path.relative_to(REPO)),
        "file_allocation_monitor_compressed_sha256": allocation_compressed_sha,
        "file_allocation_monitor_uncompressed_sha256": allocation_raw_sha,
        "file_allocation_monitor_uncompressed_bytes": allocation_uncompressed_bytes,
        "file_allocation_monitor_compressed_bytes": allocation_compressed_bytes,
        "file_allocation_monitor_sample_count": len(file_samples),
        "p1_p2_metadata_allocated_before_execution_receipt_bytes":
            metadata_after_install_result["total_p2_metadata_allocated_bytes"],
        "p1_p2_metadata_remaining_before_execution_receipt_bytes":
            metadata_after_install_result["remaining_bytes"],
        "helper_log_path": str(log_path.relative_to(REPO)),
        "helper_log_sha256": vm._file_sha256(log_path),
        "helper_stderr_path": str(stderr_path.relative_to(REPO)),
        "helper_stderr_sha256": vm._file_sha256(stderr_path),
        "helper_pid": child.pid,
        "helper_process_group_id": child.process_group_id,
        "helper_exit_code": helper_returncode,
        "helper_process_completion": {
            "pid": child.pid,
            "process_group_id": child.process_group_id,
            "exit_status": helper_returncode,
            "wait_completed": True,
            "stdout_reader_joined": not helper.stdout_thread.is_alive(),
            "stderr_reader_joined": not helper.stderr_thread.is_alive(),
            "reader_errors": list(helper.reader_errors),
            "handle_release_event_observed": True,
        },
        "restore_sha256": RESTORE_SHA256,
        "system_raw": {"path": str(Path(admission["system_raw_path"])), "sha256": system_hash,
                       "file_identity": system_identity, "logical_bytes": system_size,
                       "physical_allocated_bytes": allocation["system_bytes"]},
        "auxiliary_storage": {"path": str(Path(admission["auxiliary_storage_path"])),
                              "sha256": auxiliary_hash, "file_identity": auxiliary_identity,
                              "logical_bytes": auxiliary_size, "physical_allocated_bytes": allocation["auxiliary_bytes"]},
        "hardware_model": {"sha256": hardware_hash, "file_identity": hardware_identity},
        "machine_identifier": {"sha256": machine_hash, "file_identity": machine_identity},
        "machine_identifier_creation": admission["machine_identifier_creation"],
        "platform_identity_event": platform_identity_events[0],
        "system_raw_creation_event": system_created_events[0],
        "auxiliary_storage_creation_event": auxiliary_created_events[0],
        "configuration_validated_event": configuration_events[0],
        "configuration_runtime_readback_event": runtime_configuration_events[0],
        "installer_restore_identity_event": restore_identity_events[0],
        "installer_restore_identity_after_constructor_event": restore_identity_after_constructor_events[0],
        "installer_restore_identity_at_start_event": restore_identity_at_start_events[0],
        "installer_timeline": {
            "started": installer_start_events[0],
            "invocation_started": invocation_events[0],
            "progress_samples": progress_events,
            "terminal": terminal_events[0],
            "result": installer_events[0],
            "monotonic_order_validated": True,
        },
        "helper_quiescence_evidence": {
            "helper_exit_code": helper_returncode,
            "helper_pid": child.pid,
            "helper_process_group_id": child.process_group_id,
            "installer_terminal_event": terminal_events[-1],
            "helper_result_release": released,
            "handles_released_event": release_events[-1],
            "normal_release_writer_scan": monitor_ready_evidence.get("normal_release_writer_scan"),
            "normal_release_writer_scan_clear_count": monitor_ready_evidence.get(
                "normal_release_writer_scan_clear_count"),
        },
        "vm_state": "STOPPED",
        "all_writable_handles_released": True,
        "serial_and_attachment_handles_released": True,
        "guest_version_independently_verified": False,
        "network_devices": [],
        "directory_sharing_devices": [],
        "serial_ports": [],
        "blockers": [],
    }
    execution_path = unique_path("R6-VM-P2-EXECUTION-RESULT", run_id)
    signed_write(execution_path, execution_record, "vm_p2_execution_result_hmac_sha256")
    metadata_after_execution = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    print(json.dumps({"status": "PASS_INSTALL_COMPLETE", "admission_sha256": admission_sha,
                      "installer_result_path": str(result_path), "execution_result_path": str(execution_path),
                      "monitor_result_path": str(monitor_path),
                      "file_allocation_monitor_path": str(allocation_path),
                      "system_raw_sha256": system_hash, "auxiliary_sha256": auxiliary_hash}, sort_keys=True))


def seal_base(args: argparse.Namespace) -> None:
    run_id = args.run_id
    termination_pending_path = unique_path("R6-VM-P2-TERMINATION-PENDING", run_id)
    if os.path.lexists(termination_pending_path):
        raise RuntimeError("P2 base sealing is blocked while a signed termination-pending record exists for this run")
    admission_path = Path(args.admission).resolve(strict=True)
    execution_path = Path(args.execution_result).resolve(strict=True)
    install_path = Path(args.install_result).resolve(strict=True)
    monitor_path = Path(args.monitor_result).resolve(strict=True)
    admission, admission_sha = read_signed(admission_path, "vm_admission_hmac_sha256")
    execution, execution_sha = read_signed(execution_path, "vm_p2_execution_result_hmac_sha256")
    install, install_sha = read_signed(install_path, "vm_install_result_hmac_sha256")
    p1_final, p1_final_sha = read_signed(P1_FINAL_PATH, "restore_verification_hmac_sha256")
    p1_renewal, p1_renewal_sha = read_signed(
        P1_RENEWAL_PATH, "vm_p1_freshness_renewal_hmac_sha256")
    p1_first_body, p1_first_body_sha = read_signed(
        P1_BODY_START_PATH, "vm_p1_freshness_start_hmac_sha256")
    p0_admission, p0_admission_sha = read_signed(vm.VM_P0_ADMISSION_PATH, "vm_admission_hmac_sha256")
    p0_review = json.loads(vm.VM_P0_REVIEW_PATH.read_text(encoding="utf-8"))
    p0_review_sha = vm._file_sha256(vm.VM_P0_REVIEW_PATH)
    p1_admission, p1_admission_sha = read_signed(P1_ADMISSION_PATH, "vm_admission_hmac_sha256")
    upstream_metadata = vm._read_signed_record(UPSTREAM_METADATA_PATH, "restore_metadata_hmac_sha256")
    if (p0_admission_sha != vm.VM_P0_ADMISSION_SHA256
            or p0_review_sha != vm.VM_P0_REVIEW_SHA256
            or p0_review.get("status") != "PASS WITH NON-BLOCKING CAVEATS — P0 STORAGE ESTABLISHED AND PROVISIONING ADMISSION VALID"
            or p0_admission.get("decision") != "PASS"
            or p1_admission_sha != P1_ADMISSION_SHA256
            or vm._file_sha256(UPSTREAM_METADATA_PATH) != UPSTREAM_METADATA_FILE_SHA256
            or upstream_metadata.get("metadata_sha256") != UPSTREAM_METADATA_SHA256
            or upstream_metadata.get("hardware_model_data_sha256") != P1_UPSTREAM_HARDWARE_MODEL_SHA256):
        raise RuntimeError("P2 base seal does not bind the accepted P0 review and P1/compatibility ancestry")
    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    monitor_compressed = vm.policy._read_file_nofollow(monitor_path, maximum_bytes=P2_METADATA_MAX)
    monitor_raw = gzip.decompress(monitor_compressed)
    monitor = json.loads(monitor_raw)
    monitor_sha = vm._file_sha256(monitor_path)
    monitor_uncompressed_sha = hashlib.sha256(monitor_raw).hexdigest()
    allocation_path = REPO / execution["file_allocation_monitor_path"]
    allocation_packed = vm.policy._read_file_nofollow(allocation_path, maximum_bytes=P2_METADATA_MAX)
    allocation_raw = gzip.decompress(allocation_packed)
    allocation_monitor = json.loads(allocation_raw)
    allocation_sha = vm._file_sha256(allocation_path)
    allocation_uncompressed_sha = hashlib.sha256(allocation_raw).hexdigest()
    if (admission.get("decision") != "PASS" or admission.get("authorized_phase") != "P2"
            or admission.get("run_id") != run_id
            or p1_final_sha != P1_FINAL_SHA256
            or p1_renewal_sha != P1_RENEWAL_SHA256
            or p1_first_body_sha != P1_BODY_START_SHA256
            or p1_renewal.get("decision") != "PASS_P1_FRESHNESS_REVALIDATED"
            or p1_renewal.get("renewal_sequence") != 2
            or p1_first_body.get("status") != "PASS_FIRST_BODY_ACCEPTED"
            or p1_first_body.get("renewal_sha256") != p1_renewal_sha
            or p1_final.get("p1_freshness_renewal_sha256") != p1_renewal_sha
            or p1_final.get("p1_freshness_start_evidence_sha256") != p1_first_body_sha
            or active.get("state") != "ACTIVE"
            or active.get("phase_id") != "P2_COMPLETE"
            or active.get("session_id") != admission.get("session_id")
            or execution.get("run_id") != run_id
            or execution.get("p2_admission_path") != str(admission_path.relative_to(REPO))
            or execution.get("p2_admission_sha256") != admission_sha
            or execution.get("installer_result_path") != str(install_path.relative_to(REPO))
            or execution.get("installer_result_sha256") != install_sha
            or execution.get("monitor_result_path") != str(monitor_path.relative_to(REPO))
            or install.get("status") != "PASS_INSTALL_COMPLETE"
            or install.get("phase_id") != "P2"
            or install.get("session_id") != admission.get("session_id")
            or install.get("p2_admission_sha256") != admission_sha
            or execution.get("status") != "PASS_INSTALL_COMPLETE"
            or install.get("status") != "PASS_INSTALL_COMPLETE"
            or monitor.get("status") != "PASS_PHASE_COMPLETE"
            or monitor.get("phase_id") != "P2"
            or monitor.get("session_id") != admission.get("session_id")
            or monitor.get("p2_admission_sha256") != admission_sha
            or not vm.verify_signed_record(monitor, "vm_phase_monitor_hmac_sha256")
            or monitor_sha != execution.get("monitor_result_sha256")
            or monitor_uncompressed_sha != execution.get("monitor_result_uncompressed_sha256")
            or allocation_monitor.get("status") != "PASS"
            or not vm.verify_signed_record(allocation_monitor, "vm_p2_file_allocation_monitor_hmac_sha256")
            or allocation_sha != execution.get("file_allocation_monitor_compressed_sha256")
            or allocation_uncompressed_sha != execution.get("file_allocation_monitor_uncompressed_sha256")
            or allocation_monitor.get("p2_admission_sha256") != admission_sha
            or allocation_monitor.get("run_id") != run_id
            or not isinstance(execution.get("helper_process_completion"), Mapping)
            or execution["helper_process_completion"].get("pid") != execution.get("helper_pid")
            or execution["helper_process_completion"].get("exit_status") != execution.get("helper_exit_code")
            or execution["helper_process_completion"].get("wait_completed") is not True
            or execution["helper_process_completion"].get("stdout_reader_joined") is not True
            or execution["helper_process_completion"].get("stderr_reader_joined") is not True
            or execution["helper_process_completion"].get("handle_release_event_observed") is not True
            or monitor.get("maximum_observed_monitor_gap_seconds", float("inf"))
            > vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS
            or monitor.get("vm_state") != "STOPPED"
            or execution.get("guest_version_independently_verified") is not False):
        raise RuntimeError("P2 base sealing requires successful installer, monitor, and stopped-state results")
    base_dir = Path(admission["system_raw_path"]).parent
    if (not base_dir.is_absolute()
            or base_dir != VM_ROOT / "templates" / f"macos-base-{run_id}"
            or base_dir.resolve(strict=True) != base_dir):
        raise RuntimeError("P2 base-template directory is not the exact canonical admitted path beneath the VM volume")
    current = _template_allocations(base_dir)
    if (current["system_bytes"] > P2_SYSTEM_MAX or current["auxiliary_bytes"] > P2_AUX_MAX
            or current["metadata_bytes"] > P2_METADATA_MAX):
        raise RuntimeError("base assets exceed P2 physical allocation ceilings before sealing")
    manifest_path = REPO / admission["provisioner_source_manifest_path"]
    review_path = REPO / admission["provisioner_review_path"]
    manifest, _review = _verify_reviewed_provenance(manifest_path, review_path)
    if (vm._file_sha256(manifest_path) != admission["provisioner_source_manifest_sha256"]
            or vm._file_sha256(review_path) != admission["provisioner_review_sha256"]):
        raise RuntimeError("base sealing provenance no longer matches the signed P2 admission")
    metadata_paths = _provisioner_metadata_paths(
        manifest, manifest_path, review_path, admission_path, execution_path,
        install_path, monitor_path, allocation_path)
    _assert_p2_metadata_budget(
        metadata_paths,
        additional_allocated_bytes=current["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE)
    helper_pid = execution.get("helper_pid")
    if not isinstance(helper_pid, int) or isinstance(helper_pid, bool) or helper_pid <= 0:
        raise RuntimeError("base sealing requires the exact P2 helper PID and wait status")
    helper_quiescence = execution.get("helper_quiescence_evidence")
    if not isinstance(helper_quiescence, Mapping):
        raise RuntimeError("base sealing lacks the signed helper stop and release event evidence")
    helper_process_group_id = execution.get("helper_process_group_id")
    if (not isinstance(helper_process_group_id, int)
            or isinstance(helper_process_group_id, bool) or helper_process_group_id <= 0
            or helper_quiescence.get("helper_process_group_id") != helper_process_group_id):
        raise RuntimeError("base sealing lacks the exact dedicated helper process-group identity")
    stopped_event = helper_quiescence.get("handles_released_event")
    normal_release_scan = helper_quiescence.get("normal_release_writer_scan")
    release_process_scan = (normal_release_scan.get("process_scan")
                            if isinstance(normal_release_scan, Mapping) else None)
    release_descriptor_scan = (normal_release_scan.get("descriptor_scan")
                               if isinstance(normal_release_scan, Mapping) else None)
    monitor_samples = monitor.get("samples")
    if (not isinstance(stopped_event, Mapping)
            or stopped_event.get("vm_state") != "STOPPED"
            or not isinstance(stopped_event.get("captured_at_unix"), (int, float))
            or not isinstance(helper_quiescence.get("normal_release_writer_scan_clear_count"), int)
            or isinstance(helper_quiescence.get("normal_release_writer_scan_clear_count"), bool)
            or helper_quiescence.get("normal_release_writer_scan_clear_count") < 2
            or not isinstance(normal_release_scan, Mapping)
            or normal_release_scan.get("clear") is not True
            or normal_release_scan.get("process_group_presence") is not False
            or not isinstance(release_process_scan, Mapping)
            or release_process_scan.get("blocking_processes")
            or not isinstance(release_descriptor_scan, Mapping)
            or release_descriptor_scan.get("scan_clear") is not True
            or release_descriptor_scan.get("open_file_descriptors")
            or release_descriptor_scan.get("descriptor_identity_mismatches")
            or not isinstance(monitor_samples, list) or not monitor_samples
            or monitor_samples[-1].get("admitted") is not True
            or monitor_samples[-1].get("blockers")):
        raise RuntimeError("base sealing lacks an observed stopped event and admitted post-stop capacity sample")
    files = [base_dir / "system.raw", base_dir / "auxiliary.bin",
             base_dir / "machine-identifier.bin", base_dir / "hardware-model.bin"]
    for path in files:
        if path.resolve(strict=True) != path or path.is_symlink():
            raise RuntimeError(f"P2 base artifact path is linked or non-canonical: {path}")
    preseal_process_scan = _p2_process_scan_evidence(
        required_absent_pids=(helper_pid,),
        required_absent_process_group_ids=(helper_process_group_id,))
    scan_targets = [*files, RESTORE_PATH, base_dir]
    preseal_descriptor_scan = _descriptor_scan_evidence(scan_targets)
    if (preseal_descriptor_scan["open_process_ids"]
            or preseal_descriptor_scan.get("scan_clear") is not True
            or preseal_process_scan["blocking_processes"]):
        raise RuntimeError("base sealing requires no helper, installer, or open artifact descriptor")
    for path in files:
        os.chmod(path, 0o444, follow_symlinks=False)
    os.chmod(base_dir, 0o555, follow_symlinks=False)
    immutable = getattr(stat, "UF_IMMUTABLE", None)
    if immutable is None:
        raise RuntimeError("Darwin UF_IMMUTABLE is unavailable; template-only protection cannot be proven")
    for path in files:
        os.chflags(path, immutable, follow_symlinks=False)
    os.chflags(base_dir, immutable, follow_symlinks=False)
    flags = {str(path): int(os.stat(path, follow_symlinks=False).st_flags) for path in [*files, base_dir]}
    if any((value & immutable) == 0 for value in flags.values()):
        raise RuntimeError("template-only immutable flags did not persist on every base artifact")

    system_hash, system_size, system_hash_window = _hash_file_with_identity_window(base_dir / "system.raw")
    auxiliary_hash, auxiliary_size, auxiliary_hash_window = _hash_file_with_identity_window(base_dir / "auxiliary.bin")
    machine_hash, machine_size, machine_hash_window = _hash_file_with_identity_window(base_dir / "machine-identifier.bin")
    hardware_hash, hardware_size, hardware_hash_window = _hash_file_with_identity_window(base_dir / "hardware-model.bin")
    system_identity = dict(system_hash_window["identity_after_hash"])
    auxiliary_identity = dict(auxiliary_hash_window["identity_after_hash"])
    machine_identity = dict(machine_hash_window["identity_after_hash"])
    hardware_identity = dict(hardware_hash_window["identity_after_hash"])
    if (system_hash != execution["system_raw"]["sha256"]
            or auxiliary_hash != execution["auxiliary_storage"]["sha256"]
            or machine_hash != admission["machine_identifier_sha256"]
            or hardware_hash != admission["hardware_model_sha256"]
            or system_size != SYSTEM_LOGICAL_CAPACITY):
        raise RuntimeError("sealed base artifact hash or logical capacity differs from the P2 execution evidence")
    postseal_descriptor_scan = _descriptor_scan_evidence(scan_targets)
    postseal_process_scan = _p2_process_scan_evidence(
        required_absent_pids=(helper_pid,),
        required_absent_process_group_ids=(helper_process_group_id,))
    if (postseal_descriptor_scan["open_process_ids"]
            or postseal_descriptor_scan.get("scan_clear") is not True
            or postseal_process_scan["blocking_processes"]):
        raise RuntimeError("post-hash quiescence scan found a process or open descriptor retaining a base artifact")
    observation = vm.capture_host_observation()
    blockers = vm._session_container_blockers(observation, volume_uuid=VM_VOLUME_UUID)
    contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
    volume = vm._volume_row(observation, VM_VOLUME_UUID)
    safety = vm._volume_row(observation, SAFETY_VOLUME_UUID)
    current_used = volume.get("CapacityInUse")
    quota_bytes = contract.get("quota_bytes")
    initial_used = admission.get("vm_volume_usage_bytes")
    shared_free = vm.conservative_session_unreserved_free_bytes(
        observation, volume_uuid=VM_VOLUME_UUID, capacity_contract=contract)
    if (observation.get("apfs_container", {}).get("APFSContainerUUID") != CONTAINER_UUID
            or observation.get("physical_store_info", {}).get("DiskUUID") != PHYSICAL_STORE_UUID
            or vm._volume_mount_info(observation, VM_VOLUME_UUID).get("volume_uuid") != VM_VOLUME_UUID
            or contract.get("quota_bytes") != 53_687_091_200):
        blockers.append("final P2 storage observation no longer matches the admitted APFS volume, container, store, and quota")
    if (not isinstance(current_used, int) or isinstance(current_used, bool)
            or not isinstance(initial_used, int) or isinstance(initial_used, bool)
            or not isinstance(quota_bytes, int) or isinstance(quota_bytes, bool)
            or current_used > quota_bytes):
        blockers.append("final P2 VM-volume quota usage or headroom is unknown or exceeded")
    else:
        actual_growth = current_used - initial_used
        if actual_growth > admission.get("p2_growth_bytes", -1):
            blockers.append("actual VM-volume growth exceeded the signed P2 admission")
    if shared_free <= vm.VM_OPERATIONAL_WARNING_FREE:
        blockers.append("final unreserved shared free capacity is at or below the R4 8 GiB early-stop line")
    if shared_free <= vm.VM_OPERATIONAL_UNRESERVED_FREE_MIN:
        blockers.append("final unreserved shared free capacity is at or below the R4 5 GiB hard floor")
    if (safety.get("CapacityInUse") != 16_106_127_360
            or observation.get("safety_volume_info", {}).get("mount_state") != "UNMOUNTED_VERIFIED"):
        blockers.append("final P2 safety reserve is changed or mounted")
    if blockers:
        raise RuntimeError("final base-seal storage identity check failed: " + "; ".join(blockers))
    allocation = _template_allocations(base_dir)
    postseal_observation_path = unique_path("R6-VM-P2-POST-SEAL-OBSERVATION", run_id)
    postseal_observation_record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_POST_QUIESCENCE_STORAGE_OBSERVATION",
        "run_id": run_id,
        "created_at_unix": time.time(),
        "created_at_utc": utc_timestamp(),
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "base_template_path": str(base_dir),
        "system_raw_allocated_bytes": allocation["system_bytes"],
        "auxiliary_storage_allocated_bytes": allocation["auxiliary_bytes"],
        "storage_observation": observation,
        "vm_volume_uuid": VM_VOLUME_UUID,
        "vm_volume_quota_bytes": quota_bytes,
        "vm_volume_capacity_in_use_bytes": current_used,
        "vm_volume_growth_since_admission_bytes": current_used - initial_used,
        "unreserved_shared_free_bytes": shared_free,
        "safety_volume_uuid": SAFETY_VOLUME_UUID,
        "safety_volume_capacity_in_use_bytes": safety.get("CapacityInUse"),
        "safety_volume_mount_state": observation.get("safety_volume_info", {}).get("mount_state"),
        "blockers": [],
    }
    postseal_observation_signed = signed_write(
        postseal_observation_path, postseal_observation_record,
        "vm_p2_post_seal_observation_hmac_sha256")
    postseal_observation_record_readback, postseal_observation_sha = read_signed(
        postseal_observation_path, "vm_p2_post_seal_observation_hmac_sha256")
    if postseal_observation_record_readback != postseal_observation_signed:
        raise RuntimeError("post-quiescence storage observation did not read back exactly")
    metadata_paths = metadata_paths + [postseal_observation_path]
    metadata_inventory = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=allocation["metadata_bytes"])
    helper_log_events = jsonl_events(execution["helper_log_path"])
    configuration_event = next((row for row in helper_log_events
                                if row.get("event") == "configuration_validated"), None)
    if not isinstance(configuration_event, Mapping):
        raise RuntimeError("base sealing lacks the complete validated VM configuration event")
    configuration_record = configuration_event.get("configuration_record")
    runtime_configuration_event = execution.get("configuration_runtime_readback_event")
    if (not isinstance(configuration_record, Mapping)
            or not isinstance(runtime_configuration_event, Mapping)
            or configuration_record.get("storageDevices") != [admission["system_raw_path"]]
            or configuration_record.get("restorePath") != str(RESTORE_PATH)
            or configuration_record.get("storageDeviceReadOnly") is not False
            or configuration_record.get("storageDeviceFormat") != "sparse RAW regular file"
            or configuration_record.get("writableAttachmentOwner")
            != "only the P2 provisioning VZVirtualMachine during installer execution"
            or runtime_configuration_event.get("vm_started") is not False):
        raise RuntimeError("P2 seal configuration record does not match the admitted raw disk and no-start boundary")
    canonical_configuration_bytes = vm.policy._canonical_pretty_json_bytes(configuration_record)
    created_at_unix = time.time()
    provisioner_source_manifest_path = admission["provisioner_source_manifest_path"]
    provisioner_review_path = admission["provisioner_review_path"]
    provisioner_source_manifest_sha = vm._file_sha256(manifest_path)
    provisioner_review_sha = vm._file_sha256(review_path)
    sealed_system_path = str(base_dir / "system.raw")
    sealed_auxiliary_path = str(base_dir / "auxiliary.bin")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_MACOS_BASE_IDENTITY",
        "run_id": run_id,
        "created_at_unix": created_at_unix,
        "created_at_utc": utc_timestamp(created_at_unix),
        "status": "PASS_MACOS_BASE_SEALED",
        "authority": "STOCK_MACOS_BASE_TEMPLATE_ONLY",
        "disposition": "SEALED_STOCK_MACOS_BASE",
        "authority_scope": "P2_BASE_TEMPLATE_IDENTITY_ONLY",
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "p2_admission": {"path": str(admission_path.relative_to(REPO)),
                          "sha256": admission_sha,
                          "signature_field": "vm_admission_hmac_sha256",
                          "decision": admission.get("decision")},
        "p2_session_id": admission["session_id"],
        "p0_ancestry": {
            "admission_path": str(vm.VM_P0_ADMISSION_PATH.relative_to(REPO)),
            "admission_sha256": p0_admission_sha,
            "review_path": str(vm.VM_P0_REVIEW_PATH.relative_to(REPO)),
            "review_sha256": p0_review_sha,
            "storage_mode": vm.VM_MODE,
            "creation_receipt_path": str(vm.VM_P0_CREATION_RECEIPT_PATH.relative_to(REPO)),
            "creation_receipt_sha256": vm.VM_P0_CREATION_RECEIPT_SHA256,
            "p1_admission_path": str(P1_ADMISSION_PATH.relative_to(REPO)),
            "p1_admission_sha256": p1_admission_sha,
        },
        "p1_ancestry": {
            "verification_path": str(P1_FINAL_PATH.relative_to(REPO)),
            "verification_sha256": p1_final_sha,
            "renewal_path": str(P1_RENEWAL_PATH.relative_to(REPO)),
            "renewal_sha256": p1_renewal_sha,
            "first_body_path": str(P1_BODY_START_PATH.relative_to(REPO)),
            "first_body_sha256": p1_first_body_sha,
            "restore_path": str(RESTORE_PATH),
            "restore_sha256": RESTORE_SHA256,
            "restore_logical_bytes": RESTORE_SIZE,
            "restore_subject_identity": RESTORE_SUBJECT_IDENTITY,
            "product_version": "27.0.1",
            "build_version": "26A434",
            "architecture": "arm64",
            "upstream_metadata_path": str(UPSTREAM_METADATA_PATH.relative_to(REPO)),
            "upstream_metadata_file_sha256": UPSTREAM_METADATA_FILE_SHA256,
            "upstream_metadata_sha256": UPSTREAM_METADATA_SHA256,
            "p1_admission_path": str(P1_ADMISSION_PATH.relative_to(REPO)),
            "p1_admission_sha256": p1_admission_sha,
        },
        "storage_domain": {
            "safety_volume_uuid": SAFETY_VOLUME_UUID,
            "safety_state": "UNMOUNTED_VERIFIED",
            "safety_reserve_bytes": 15 * GIB,
            "vm_volume_uuid": VM_VOLUME_UUID,
            "vm_quota_bytes": quota_bytes,
            "container_uuid": CONTAINER_UUID,
            "physical_store_uuid": PHYSICAL_STORE_UUID,
            "post_seal_observation_path": str(postseal_observation_path.relative_to(REPO)),
            "post_seal_observation_sha256": postseal_observation_sha,
        },
        "provisioner": {
            "source_manifest_path": provisioner_source_manifest_path,
            "source_manifest_sha256": provisioner_source_manifest_sha,
            "review_path": provisioner_review_path,
            "review_sha256": provisioner_review_sha,
            "review_decision": admission["provisioner_review_decision"],
            "manifest_record": manifest,
            "source_identity": manifest["source_manifest"]["source"],
            "orchestration_source_identity": manifest["source_manifest"]["orchestration_source"],
            "executable_identity": manifest["source_manifest"]["binary"],
            "entitlement_identity": manifest["source_manifest"]["entitlements"],
            "compiler_identity": manifest["compiler"],
            "build_command": manifest["build_command"],
            "launch_verification": execution["provisioner_launch_verification"],
        },
        "guest_platform": {
            "restore_path": str(RESTORE_PATH),
            "restore_sha256": RESTORE_SHA256,
            "restore_subject_identity": RESTORE_SUBJECT_IDENTITY,
            "restore_subject": {"product_version": "27.0.1", "build_version": "26A434",
                                "architecture": "arm64"},
            "restore_metadata_path": str(UPSTREAM_METADATA_PATH.relative_to(REPO)),
            "restore_metadata_file_sha256": UPSTREAM_METADATA_FILE_SHA256,
            "restore_metadata_sha256": UPSTREAM_METADATA_SHA256,
            "restore_image_supported": execution["platform_identity_event"]["restore_image_supported"],
            "hardware_model_source": execution["platform_identity_event"]["hardware_model_source"],
            "hardware_model_data_base64": admission["hardware_model_data_base64"],
            "hardware_model_sha256": hardware_hash,
            "hardware_model_logical_bytes": hardware_size,
            "machine_identifier_data_base64": admission["machine_identifier_data_base64"],
            "machine_identifier_sha256": machine_hash,
            "machine_identifier_creation": admission["machine_identifier_creation"],
            "machine_identifier_reused_unchanged_through_install_and_shutdown": True,
            "platform_type": "VZMacPlatformConfiguration",
            "boot_loader_type": "VZMacOSBootLoader",
            "cpu_count": configuration_record.get("cpuCount"),
            "memory_bytes": configuration_record.get("memoryBytes"),
        },
        "system_raw_template": {"path": sealed_system_path,
                                 "canonical_path": sealed_system_path,
                                 "logical_capacity_bytes": system_size,
                                 "physical_allocated_bytes": allocation["system_bytes"],
                                 "sha256": system_hash,
                                 "file_identity": system_identity,
                                 "hash_window": system_hash_window,
                                 "apfs_volume_uuid": VM_VOLUME_UUID,
                                 "container_uuid": CONTAINER_UUID,
                                 "physical_store_uuid": PHYSICAL_STORE_UUID,
                                 "file_format": "sparse RAW regular file",
                                 "link_free_path_resolution": True},
        "auxiliary_storage_template": {"path": sealed_auxiliary_path,
                                        "canonical_path": sealed_auxiliary_path,
                                        "logical_bytes": auxiliary_size,
                                        "physical_allocated_bytes": allocation["auxiliary_bytes"],
                                        "sha256": auxiliary_hash,
                                        "file_identity": auxiliary_identity,
                                        "hash_window": auxiliary_hash_window,
                                        "api_creation_event": execution["auxiliary_storage_creation_event"],
                                        "hardware_model_sha256": hardware_hash,
                                        "apfs_volume_uuid": VM_VOLUME_UUID,
                                        "container_uuid": CONTAINER_UUID,
                                        "physical_store_uuid": PHYSICAL_STORE_UUID,
                                        "link_free_path_resolution": True},
        "installation": {
            "installer_result_path": str(install_path.relative_to(REPO)),
            "installer_result_sha256": install_sha,
            "execution_result_path": str(execution_path.relative_to(REPO)),
            "execution_result_sha256": execution_sha,
            "restore_path": str(RESTORE_PATH),
            "restore_sha256": RESTORE_SHA256,
            "restore_file_identity_verified_immediately_before_use": execution["installer_restore_identity_event"],
            "installer_source_identity": {"api": "VZMacOSInstaller", "helper_binary_sha256": admission["provisioner_binary_sha256"]},
            "installer_timeline": execution["installer_timeline"],
            "installation_claim": execution["claim"],
            "measured_claim_classification": "INSTALLATION_FROM_MACOS_27.0.1_26A434_RESTORE_COMPLETED",
            "guest_version_independently_verified": False,
            "monitor_result_path": str(monitor_path.relative_to(REPO)),
            "monitor_result_sha256": monitor_sha,
            "monitor_result_uncompressed_sha256": monitor_uncompressed_sha,
            "monitor_sample_count": monitor.get("sample_count"),
            "monitor_interval_data": {"sample_interval_target_seconds": 0.001,
                                      "maximum_observed_gap_seconds": monitor.get("maximum_observed_monitor_gap_seconds"),
                                      "first_sample": monitor.get("samples", [None])[0],
                                      "last_sample": monitor.get("samples", [None])[-1]},
        },
        "p1_restore_verification_path": str(P1_FINAL_PATH.relative_to(REPO)),
        "p1_restore_verification_sha256": p1_final_sha,
        "p1_freshness_renewal_path": str(P1_RENEWAL_PATH.relative_to(REPO)),
        "p1_freshness_renewal_sha256": p1_renewal_sha,
        "p1_first_body_evidence_path": str(P1_BODY_START_PATH.relative_to(REPO)),
        "p1_first_body_evidence_sha256": p1_first_body_sha,
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": RESTORE_SHA256,
        "restore_subject": {"operating_system_version": "27.0.1", "build_version": "26A434",
                            "host_architecture": "arm64"},
        "restore_file_identity_at_install": admission["restore_file_identity"],
        "provisioner_source_manifest_path": admission["provisioner_source_manifest_path"],
        "provisioner_source_manifest_sha256": admission["provisioner_source_manifest_sha256"],
        "provisioner_review_path": admission["provisioner_review_path"],
        "provisioner_review_sha256": admission["provisioner_review_sha256"],
        "provisioner_source_sha256": admission["provisioner_source_sha256"],
        "provisioner_orchestration_source_sha256": admission["provisioner_orchestration_source_sha256"],
        "provisioner_binary_path": admission["provisioner_binary_path"],
        "provisioner_binary_sha256": admission["provisioner_binary_sha256"],
        "installation_result_path": str(install_path.relative_to(REPO)),
        "installation_result_sha256": install_sha,
        "execution_result_path": str(execution_path.relative_to(REPO)),
        "execution_result_sha256": execution_sha,
        "installation_claim": execution["claim"],
        "guest_reported_version_build": "NOT_INDEPENDENTLY_MEASURED",
        "hardware_model": {"path": str(base_dir / "hardware-model.bin"),
                            "sha256": hardware_hash, "bytes": hardware_size,
                            "file_identity": hardware_identity,
                            "hash_window_identity": hardware_hash_window,
                            "data_base64": admission["hardware_model_data_base64"]},
        "machine_identifier": {"path": str(base_dir / "machine-identifier.bin"),
                                "sha256": machine_hash, "bytes": machine_size,
                                "file_identity": machine_identity,
                                "hash_window_identity": machine_hash_window,
                                "data_base64": admission["machine_identifier_data_base64"]},
        "auxiliary_storage": {"path": str(base_dir / "auxiliary.bin"),
                              "sha256": auxiliary_hash, "bytes": auxiliary_size,
                              "physical_allocated_bytes": allocation["auxiliary_bytes"],
                              "logical_capacity_ceiling_bytes": AUXILIARY_LOGICAL_CAPACITY,
                              "file_identity": auxiliary_identity,
                              "hash_window_identity": auxiliary_hash_window,
                              "hardware_model_sha256": hardware_hash},
        "system_raw": {"path": str(base_dir / "system.raw"), "sha256": system_hash,
                       "logical_capacity_bytes": system_size,
                       "physical_allocated_bytes": allocation["system_bytes"],
                       "physical_allocation_ceiling_bytes": P2_SYSTEM_MAX,
                       "file_identity": system_identity,
                       "hash_window_identity": system_hash_window},
        "seal_quiescence_evidence": {
            "provisioner_helper": execution.get("helper_quiescence_evidence"),
            "helper_process_exit_evidence": execution.get("helper_process_completion"),
            "pre_hash_process_scan": preseal_process_scan,
            "pre_hash_descriptor_scan": preseal_descriptor_scan,
            "post_hash_process_scan": postseal_process_scan,
            "post_hash_descriptor_scan": postseal_descriptor_scan,
            "helper_exit_code": execution.get("helper_exit_code"),
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "installer_object_released": True,
            "disk_attachment_released": True,
        },
        "quiescence": {
            "vm_state_observed": "stopped",
            "vm_state_observation_event": stopped_event,
            "installer_object_released": True,
            "vm_object_released": True,
            "configuration_and_platform_released": True,
            "storage_device_and_disk_attachment_released": True,
            "writable_descriptor_closure": {
                "restore_descriptor_closed_before_helper_exit": True,
                "system_and_auxiliary_hash_descriptors_closed_by_hash_helper_return": True,
                "helper_pid": helper_pid,
                "helper_exit_status": execution["helper_exit_code"],
                "wait_completed": execution["helper_process_completion"]["wait_completed"],
            },
            "process_scan_before_hash": preseal_process_scan,
            "descriptor_scan_before_hash": preseal_descriptor_scan,
            "process_scan_after_hash": postseal_process_scan,
            "descriptor_scan_after_hash": postseal_descriptor_scan,
            "hash_windows": {"system_raw": system_hash_window,
                             "auxiliary_storage": auxiliary_hash_window},
            "post_stop_capacity_observation_path": str(postseal_observation_path.relative_to(REPO)),
            "post_stop_capacity_observation_sha256": postseal_observation_sha,
        },
        "vm_configuration": {
            "cpu_count": configuration_record.get("cpuCount"),
            "memory_bytes": configuration_record.get("memoryBytes"),
            "disk_count": 1, "disk_format": "RAW sparse regular file",
            "network_devices": [], "directory_sharing_devices": [], "serial_ports": [],
            "console_devices": [], "socket_devices": [], "usb_controllers": [], "graphics_devices": [],
            "storageDevices": configuration_record.get("storageDevices"),
            "restorePath": configuration_record.get("restorePath"),
            "configuration_schema": admission["configuration_schema"],
            "validation_method": "VZVirtualMachineConfiguration.validate() returned successfully before VM creation/start",
            "canonical_record_sha256": hashlib.sha256(canonical_configuration_bytes).hexdigest(),
            "full_configuration_record": configuration_record,
            "validation_event": configuration_event,
            "runtime_readback_event": runtime_configuration_event,
            "runtime_readback_counts": configuration_event.get("runtime_readback_counts"),
        },
        "monitor_result_path": str(monitor_path.relative_to(REPO)),
        "monitor_result_compressed_sha256": monitor_sha,
        "monitor_result_uncompressed_sha256": monitor_uncompressed_sha,
        "monitor_sample_count": monitor.get("sample_count"),
        "maximum_monitor_gap_seconds": monitor.get("maximum_observed_monitor_gap_seconds"),
        "file_allocation_monitor_path": str(allocation_path.relative_to(REPO)),
        "file_allocation_monitor_compressed_sha256": allocation_sha,
        "file_allocation_monitor_uncompressed_sha256": allocation_uncompressed_sha,
        "file_allocation_monitor_sample_count": allocation_monitor.get("sample_count"),
        "vm_state_observed": "stopped",
        "vm_state_observation": {
            "observer_source": "VZVirtualMachine.state observed by ProvisioningSession.stopAndRelease and emitted in handles_released",
            "observer_event": stopped_event,
            "monitor_stop_measurement": monitor["stop_measurement"],
            "post_stop_capacity_sample": monitor_samples[-1],
        },
        "vm_state": "STOPPED",
        "all_writable_handles_released": True,
        "installer_object_released": True,
        "disk_attachment_released": True,
        "writable_descriptor_closure": {
            "restore_descriptor_closed_before_helper_exit": True,
            "system_and_auxiliary_hash_descriptors_closed_by_hash_helper_return": True,
            "helper_pid": helper_pid,
            "helper_exit_status": execution["helper_exit_code"],
            "helper_wait_completed": execution["helper_process_completion"]["wait_completed"],
            "pre_hash_descriptor_scan_found_open_files": bool(preseal_descriptor_scan["open_file_descriptors"]),
            "post_hash_descriptor_scan_found_open_files": bool(postseal_descriptor_scan["open_file_descriptors"]),
        },
        "helper_process_exit_evidence": execution["helper_process_completion"],
        "process_descriptor_scan_result": {
            "pre_hash_process_scan": preseal_process_scan,
            "pre_hash_descriptor_scan": preseal_descriptor_scan,
            "post_hash_process_scan": postseal_process_scan,
            "post_hash_descriptor_scan": postseal_descriptor_scan,
            "quiescence_proven": not any((
                preseal_process_scan["blocking_processes"],
                preseal_descriptor_scan["open_file_descriptors"],
                postseal_process_scan["blocking_processes"],
                postseal_descriptor_scan["open_file_descriptors"])),
        },
        "no_p2_or_r6_activity": {"serial_runtime_test": False, "cold_boots": 0,
                                  "r6_execution": False, "buildlease": False,
                                  "source_workspace_or_export_disks": False},
        "current_storage_measurement": {
            "captured_at_unix": observation.get("captured_at_unix"),
            "container_uuid": observation.get("apfs_container", {}).get("APFSContainerUUID"),
            "physical_store_uuid": observation.get("physical_store_info", {}).get("DiskUUID"),
            "vm_volume_uuid": VM_VOLUME_UUID,
            "vm_volume_quota_bytes": vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)["quota_bytes"],
            "vm_volume_capacity_in_use_bytes": vm._volume_row(observation, VM_VOLUME_UUID).get("CapacityInUse"),
            "vm_volume_growth_since_admission_bytes": current_used - initial_used,
            "vm_volume_quota_headroom_bytes": quota_bytes - current_used,
            "shared_container_free_bytes": vm.conservative_container_free_bytes(observation),
            "unreserved_shared_free_bytes": shared_free,
            "safety_volume_uuid": SAFETY_VOLUME_UUID,
            "safety_reserve_bytes": 15 * GIB,
            "safety_mount_state": observation.get("safety_volume_info", {}).get("mount_state"),
        },
        "template_only_protection": {
            "template_only": True,
            "bootable_in_place": False,
            "writable_attachment_forbidden": True,
            "source_for_copy_or_clone_only": True,
            "runtime_validator_policy": "reject sealed system.raw as any writable boot attachment; derive a separate session copy first",
            "file_modes": {str(path): stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode) for path in files},
            "directory_mode": stat.S_IMODE(os.stat(base_dir, follow_symlinks=False).st_mode),
            "immutable_flag_constant": immutable,
            "verified_flags": flags,
            "sealed_base_must_not_be_booted_or_written_again": True,
            "future_runtime_must_derive_a_session_writable_copy": True,
        },
        "p1_p2_metadata_allocated_bytes_before_base_identity_bytes":
            metadata_inventory["total_p2_metadata_allocated_bytes"],
        "p1_p2_metadata_remaining_before_base_identity_bytes": metadata_inventory["remaining_bytes"],
        "readiness_state": {
            "P1_RESTORE_ACQUISITION": "ACCEPTED",
            "VM_GUEST_BASE_PROVISIONED": "YES",
            "VM_GUEST_BASE_SEALED": "YES",
            "RESTORE_LOCAL_PAYLOAD_RETAINED": "YES_PENDING_SIGNED_CLEANUP",
            "VM_ARCHITECTURE_READY": "NO — SERIAL RUNTIME PROOF REQUIRED",
            "VM_IMPLEMENTATION_READY": "NO",
            "VM_RUNTIME_ACCEPTED": "NO",
            "R6-A": "NOT ACCEPTED",
        },
        "blockers": [],
    }
    identity_path = unique_path("R6-VM-MACOS-BASE-IDENTITY", run_id)
    identity_signed = signed_write(identity_path, record, "vm_macos_base_identity_hmac_sha256")
    identity_readback, identity_sha = read_signed(identity_path, "vm_macos_base_identity_hmac_sha256")
    if identity_readback != identity_signed or not vm.verify_signed_record(
            identity_readback, "vm_macos_base_identity_hmac_sha256"):
        raise RuntimeError("sealed base identity failed durable signed readback")
    metadata_after_identity = _assert_p2_metadata_budget(
        metadata_paths + [identity_path], additional_allocated_bytes=allocation["metadata_bytes"])
    print(json.dumps({"status": "PASS_MACOS_BASE_SEALED", "base_identity_path": str(identity_path),
                      "base_identity_sha256": identity_sha,
                      "system_raw_sha256": system_hash, "auxiliary_sha256": auxiliary_hash,
                      "system_raw_allocated_bytes": allocation["system_bytes"],
                      "auxiliary_allocated_bytes": allocation["auxiliary_bytes"],
                      "p1_p2_metadata_allocated_bytes":
                          metadata_after_identity["total_p2_metadata_allocated_bytes"]}, sort_keys=True))


def manifest_cpu(admission: Mapping[str, Any]) -> int:
    return max(2, int(admission.get("configuration_schema", {}).get("cpu_count_minimum", 2)))


def jsonl_events(relative_path: str) -> list[dict[str, Any]]:
    path = REPO / relative_path
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_stable_json_artifact(path: Path, *, maximum_bytes: int = 16 * MIB
                              ) -> tuple[dict[str, Any], str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum_bytes:
            raise RuntimeError(f"review artifact is not a bounded single-link regular file: {path}")
        raw = bytearray()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            raw.extend(block)
            if len(raw) > maximum_bytes:
                raise RuntimeError(f"review artifact exceeded its read bound: {path}")
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (file_identity(before) != file_identity(after)
                or file_identity(after) != file_identity(path_after)
                or len(raw) != after.st_size):
            raise RuntimeError(f"review artifact identity changed while reading: {path}")
    finally:
        os.close(descriptor)
    parsed = json.loads(raw)
    if not isinstance(parsed, Mapping):
        raise RuntimeError(f"review artifact is not a JSON object: {path}")
    return dict(parsed), hashlib.sha256(raw).hexdigest()


def _validate_base_review_binding(*, reviewer: Mapping[str, Any], reviewer_sha: str,
                                 run_id: str, base_identity: Mapping[str, Any],
                                 base_sha: str, admission_sha: str) -> None:
    allowed_decisions = {
        "PASS — MACOS BASE SEALED",
        "PASS WITH NON-BLOCKING CAVEATS — MACOS BASE SEALED",
    }
    if (reviewer.get("decision") not in allowed_decisions
            or reviewer.get("run_id") != run_id
            or reviewer.get("base_identity_sha256") != base_sha
            or reviewer.get("independently_computed_system_raw_sha256")
            != base_identity.get("system_raw", {}).get("sha256")
            or reviewer.get("independently_computed_auxiliary_storage_sha256")
            != base_identity.get("auxiliary_storage", {}).get("sha256")
            or reviewer.get("p2_admission_sha256") != admission_sha
            or not re.fullmatch(r"[0-9a-f]{64}", reviewer_sha)):
        raise RuntimeError("restore cleanup requires the exact PASS base review and its complete P2/base hash bindings")


def _renameatx_exclusive(source_dir_fd: int, source_name: str,
                         destination_dir_fd: int, destination_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise RuntimeError("atomic exclusive quarantine requires macOS renameatx_np")
    renameatx_np.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                             ctypes.c_uint)
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(source_dir_fd, os.fsencode(source_name), destination_dir_fd,
                          os.fsencode(destination_name), 0x00000004)  # RENAME_EXCL
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _write_pending_cleanup_receipt_atomically(
        pending_path: Path, pending_record: Mapping[str, Any], *,
        attempt_id: str, run_id: str) -> tuple[dict[str, Any], str]:
    signature_field = "vm_p2_restore_cleanup_pending_hmac_sha256"
    staging_path = unique_path(
        f"R6-VM-P2-CLEANUP-PENDING-STAGE-{attempt_id}", run_id, ".tmp")
    if pending_path.parent != REPO or staging_path.parent != REPO:
        raise RuntimeError("pending cleanup receipt and staging file must share the repository directory")

    staged_record = signed_write(staging_path, pending_record, signature_field)
    verified_stage, stage_bytes, stage_sha, stage_identity = vm._read_signed_record_snapshot(
        staging_path, signature_field)
    if verified_stage != staged_record:
        raise RuntimeError("staged cleanup pending receipt changed after signed readback")

    repository_fd = vm.policy._open_directory_nofollow(
        REPO, create=False, strict_directory_sync=True)
    try:
        stage_info = os.stat(staging_path.name, dir_fd=repository_fd,
                             follow_symlinks=False)
        if (not stat.S_ISREG(stage_info.st_mode)
                or stage_info.st_uid != os.getuid()
                or stage_info.st_nlink != 1
                or stat.S_IMODE(stage_info.st_mode) != 0o600
                or (stage_info.st_dev, stage_info.st_ino, stage_info.st_size,
                    stage_info.st_mtime_ns, getattr(stage_info, "st_ctime_ns", 0))
                != stage_identity):
            raise RuntimeError("staged cleanup pending receipt identity changed before atomic commit")
        try:
            os.stat(pending_path.name, dir_fd=repository_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("cleanup pending receipt destination already exists")

        _renameatx_exclusive(repository_fd, staging_path.name,
                              repository_fd, pending_path.name)
        os.fsync(repository_fd)

        committed_record, committed_bytes, committed_sha, committed_identity = (
            vm._read_signed_record_snapshot(pending_path, signature_field))
        committed_info = os.stat(pending_path.name, dir_fd=repository_fd,
                                 follow_symlinks=False)
        if (committed_record != staged_record or committed_bytes != stage_bytes
                or committed_sha != stage_sha
                or committed_identity[:3] != stage_identity[:3]
                or not stat.S_ISREG(committed_info.st_mode)
                or committed_info.st_uid != os.getuid()
                or committed_info.st_nlink != 1
                or stat.S_IMODE(committed_info.st_mode) != 0o600
                or (committed_info.st_dev, committed_info.st_ino, committed_info.st_size)
                != committed_identity[:3]):
            raise RuntimeError("atomically committed cleanup pending receipt failed exact identity/readback")
        try:
            os.stat(staging_path.name, dir_fd=repository_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("cleanup pending receipt staging name remained after atomic commit")
        return committed_record, committed_sha
    finally:
        os.close(repository_fd)


def _discard_orphan_pending_cleanup_stages(*, run_id: str) -> list[dict[str, Any]]:
    stage_pattern = re.compile(
        rf"R6-VM-P2-CLEANUP-PENDING-STAGE-([0-9a-f]{{12}})-{re.escape(run_id)}\.tmp")
    staging_paths = sorted(REPO.glob(
        f"R6-VM-P2-CLEANUP-PENDING-STAGE-*-{run_id}.tmp"))
    if not staging_paths:
        return []

    repository_fd = vm.policy._open_directory_nofollow(
        REPO, create=False, strict_directory_sync=True)
    evidence: list[dict[str, Any]] = []
    try:
        repository_info = os.fstat(repository_fd)
        for staging_path in staging_paths:
            match = stage_pattern.fullmatch(staging_path.name)
            if match is None:
                raise RuntimeError("orphan cleanup staging filename is not canonical")
            attempt_id = match.group(1)
            pending_name = f"R6-VM-RESTORE-CLEANUP-PENDING-{attempt_id}-{run_id}.json"
            try:
                os.stat(pending_name, dir_fd=repository_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("cleanup staging file coexists with its committed pending receipt")
            quarantine_path = VM_ROOT / f".p2-restore-quarantine-{run_id}-{attempt_id}"
            if os.path.lexists(quarantine_path):
                raise RuntimeError("cleanup staging file has a quarantine namespace and cannot be discarded")

            try:
                before = os.stat(staging_path.name, dir_fd=repository_fd,
                                 follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(before.st_mode)
                    or before.st_dev != repository_info.st_dev
                    or before.st_uid != os.getuid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != 0o600):
                raise RuntimeError("orphan cleanup staging file has an unsafe filesystem identity")

            descriptor = os.open(
                staging_path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0), dir_fd=repository_fd)
            try:
                opened = os.fstat(descriptor)
                if ((opened.st_dev, opened.st_ino, opened.st_size,
                     opened.st_mtime_ns, getattr(opened, "st_ctime_ns", 0))
                        != (before.st_dev, before.st_ino, before.st_size,
                            before.st_mtime_ns, getattr(before, "st_ctime_ns", 0))
                        or not stat.S_ISREG(opened.st_mode)
                        or opened.st_uid != os.getuid()
                        or opened.st_nlink != 1
                        or stat.S_IMODE(opened.st_mode) != 0o600):
                    raise RuntimeError("orphan cleanup staging file changed during identity validation")
            finally:
                os.close(descriptor)

            # A staging pathname is published before any VM-volume namespace mutation.
            # The matching final receipt must be absent before discarding this uncommitted file.
            os.unlink(staging_path.name, dir_fd=repository_fd)
            os.fsync(repository_fd)
            try:
                os.stat(staging_path.name, dir_fd=repository_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("orphan cleanup staging file remained after unlink")
            evidence.append({
                "path": str(staging_path.relative_to(REPO)),
                "attempt_id": attempt_id,
                "device": int(before.st_dev),
                "inode": int(before.st_ino),
                "logical_bytes": int(before.st_size),
                "owner_uid": int(before.st_uid),
                "mode": stat.S_IMODE(before.st_mode),
                "link_count": int(before.st_nlink),
                "matching_pending_receipt_absent": True,
                "action": "DISCARDED_UNCOMMITTED_RECEIPT_STAGING_FILE",
            })
    finally:
        os.close(repository_fd)
    return evidence


def _directory_access_evidence(path: Path, descriptor: int, *, expected_uid: int,
                               expected_mode: int, expected_device: int) -> dict[str, Any]:
    before = os.fstat(descriptor)
    path_before = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISDIR(before.st_mode) or not stat.S_ISDIR(path_before.st_mode)
            or before.st_dev != expected_device or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_dev != path_before.st_dev or before.st_ino != path_before.st_ino
            or before.st_uid != path_before.st_uid
            or stat.S_IMODE(path_before.st_mode) != expected_mode):
        raise RuntimeError(f"cleanup namespace directory is not the expected private identity: {path}")
    result = subprocess.run(["/bin/ls", "-lde", "--", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=False, timeout=5)
    lines = result.stdout.splitlines()
    mode_field = lines[0].split(None, 1)[0] if lines else ""
    if (result.returncode != 0 or result.stderr.strip() or len(lines) != 1
            or "+" in mode_field):
        raise RuntimeError(f"cleanup namespace directory has ACLs or incomplete access evidence: {path}")
    after = os.fstat(descriptor)
    path_after = os.stat(path, follow_symlinks=False)
    if (before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or after.st_dev != path_after.st_dev or after.st_ino != path_after.st_ino
            or after.st_uid != path_after.st_uid
            or stat.S_IMODE(after.st_mode) != expected_mode
            or stat.S_IMODE(path_after.st_mode) != expected_mode):
        raise RuntimeError(f"cleanup namespace directory changed while reading access evidence: {path}")
    return {
        "path": str(path),
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "owner_uid": int(after.st_uid),
        "group_gid": int(after.st_gid),
        "mode": stat.S_IMODE(after.st_mode),
        "link_count": int(after.st_nlink),
        "acl_absent": True,
        "acl_evidence": {
            "command": ["/bin/ls", "-lde", "--", str(path)],
            "exit_code": result.returncode,
            "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "stdout_bytes": len(result.stdout.encode()),
            "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            "stderr_bytes": len(result.stderr.encode()),
            "line_count": len(lines),
        },
    }


def _cleanup_post_delete_assessment(*, base_identity: Mapping[str, Any], base_dir: Path,
                                    before: Mapping[str, Any] | None,
                                    before_free_override: int | None = None,
                                    quarantine_path: Path | None = None,
                                    quarantine_directory_path: Path | None = None,
                                    expected_root_identity: Mapping[str, Any] | None = None,
                                    expected_root_mode: int | None = None
                                    ) -> dict[str, Any]:
    after = vm.capture_host_observation()
    after_free = vm.conservative_container_free_bytes(after)
    if before_free_override is not None:
        before_free = before_free_override
    elif before is not None:
        before_free = vm.conservative_container_free_bytes(before)
    else:
        raise RuntimeError("cleanup reconciliation has no signed pre-deletion free-space baseline")
    reclaimed = after_free - before_free
    absent = not os.path.lexists(RESTORE_PATH)
    tree_blockers, restore_files = _restore_tree_blockers_after_cleanup(
        expected_base_dir=base_dir, allowed_large_files=(base_dir / "system.raw",))
    if quarantine_path is not None and os.path.lexists(quarantine_path):
        restore_files.append(str(quarantine_path))
        tree_blockers.append(f"authorized restore remains in cleanup quarantine: {quarantine_path}")
    quarantine_directory_absent = (quarantine_directory_path is None
                                   or not os.path.lexists(quarantine_directory_path))
    if not quarantine_directory_absent:
        tree_blockers.append(
            f"cleanup quarantine namespace remains after restore deletion: {quarantine_directory_path}")
    root_mode_restored = False
    root_access_after: dict[str, Any] = {}
    root_after_device: int | None = None
    root_after_inode: int | None = None
    root_after_mode: int | None = None
    try:
        if (not isinstance(expected_root_identity, Mapping)
                or not isinstance(expected_root_mode, int)
                or isinstance(expected_root_mode, bool)):
            raise RuntimeError("signed cleanup has no original VM-volume root identity and mode")
        root_fd = os.open(VM_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_NOFOLLOW", 0))
        try:
            root_info = os.fstat(root_fd)
            root_after_device = int(root_info.st_dev)
            root_after_inode = int(root_info.st_ino)
            root_after_mode = stat.S_IMODE(root_info.st_mode)
            if (root_info.st_dev != expected_root_identity.get("device")
                    or root_info.st_ino != expected_root_identity.get("inode")
                    or root_info.st_uid != os.geteuid()
                    or stat.S_IMODE(root_info.st_mode) != expected_root_mode):
                raise RuntimeError("VM-volume root identity or original permissions were not restored")
            current_mount = vm._volume_mount_info(after, VM_VOLUME_UUID)
            vm._verify_open_mount_fd(root_fd, current_mount)
            root_access_after = _directory_access_evidence(
                VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=expected_root_mode,
                expected_device=int(expected_root_identity["device"]))
            root_mode_restored = True
        finally:
            os.close(root_fd)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        tree_blockers.append(f"VM-volume root cleanup mode or identity is not restored: {error}")
    for key, filename in (("system_raw", "system.raw"), ("auxiliary_storage", "auxiliary.bin"),
                          ("machine_identifier", "machine-identifier.bin"),
                          ("hardware_model", "hardware-model.bin")):
        artifact_path = base_dir / filename
        try:
            actual_hash, actual_size, actual_identity = sha256_file(artifact_path)
            expected_identity = _compact_file_identity(base_identity[key]["file_identity"])
            if (actual_identity != expected_identity
                    or actual_hash != base_identity[key]["sha256"]
                    or actual_size != base_identity[key].get("logical_capacity_bytes",
                                                              base_identity[key].get("bytes"))):
                tree_blockers.append(f"sealed base file identity or SHA-256 changed after restore deletion: {filename}")
        except (OSError, RuntimeError) as error:
            tree_blockers.append(f"sealed base file could not be reverified after restore deletion: {filename}: {error}")
    blockers = list(tree_blockers)
    blockers.extend(vm._session_container_blockers(after, volume_uuid=VM_VOLUME_UUID))
    blockers.extend(_process_blockers())
    safety = vm._volume_row(after, SAFETY_VOLUME_UUID)
    if (safety.get("CapacityInUse") != 16_106_127_360
            or after.get("safety_volume_info", {}).get("mount_state") != "UNMOUNTED_VERIFIED"):
        blockers.append("safety reserve changed or is mounted after restore cleanup")
    if not absent:
        blockers.append("accepted restore IPSW remains after cleanup")
    if after_free <= vm.VM_OPERATIONAL_WARNING_FREE:
        blockers.append("post-cleanup unreserved free capacity is at or below the 8 GiB R4 early-stop line")
    if reclaimed <= 0:
        blockers.append("post-cleanup APFS free-space delta did not show positive restore reclamation")
    after_volume = vm._volume_row(after, VM_VOLUME_UUID)
    after_quota = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID).get("quota_bytes")
    if (not isinstance(after_volume.get("CapacityInUse"), int)
            or isinstance(after_volume.get("CapacityInUse"), bool)
            or not isinstance(after_quota, int)
            or isinstance(after_quota, bool)
            or after_volume.get("CapacityInUse") > after_quota):
        blockers.append("post-cleanup VM-volume quota measurement is unknown or exceeds the accepted quota")
    return {
        "after": after,
        "after_free": after_free,
        "before_free": before_free,
        "reclaimed": reclaimed,
        "restore_absent": absent,
        "restore_files_remaining": restore_files,
        "quarantine_directory_absent": quarantine_directory_absent,
        "vm_volume_root_mode_restored": root_mode_restored,
        "vm_volume_root_access_after_cleanup": root_access_after,
        "vm_volume_root_after_cleanup": {
            "device": root_after_device, "inode": root_after_inode, "mode": root_after_mode,
        },
        "blockers": blockers,
        "base_artifacts_intact": not any("sealed base file" in item for item in blockers),
        "storage_after": {
            "captured_at_unix": after.get("captured_at_unix"),
            "container_free_bytes": after.get("crosschecks", {}).get("apfs_container_free_bytes"),
            "data_container_free_bytes": after.get("crosschecks", {}).get("data_info_container_free_bytes"),
            "conservative_shared_free_bytes": after_free,
            "vm_volume_capacity_in_use_bytes": after_volume.get("CapacityInUse"),
            "vm_volume_quota_bytes": after_quota,
            "safety_volume_uuid": SAFETY_VOLUME_UUID,
            "safety_reserve_bytes": 15 * GIB,
            "safety_volume_capacity_in_use_bytes": safety.get("CapacityInUse"),
            "safety_mount_state": after.get("safety_volume_info", {}).get("mount_state"),
            "vm_volume_root_device": root_after_device,
            "vm_volume_root_inode": root_after_inode,
            "vm_volume_root_mode": root_after_mode,
        },
    }


def cleanup(args: argparse.Namespace) -> None:
    run_id = args.run_id
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id) is None:
        raise RuntimeError("P2 cleanup run id contains unsupported path characters")
    base_identity_path = Path(args.base_identity).resolve(strict=True)
    base_reviewer_path = Path(args.base_reviewer).resolve(strict=True)
    base_identity, base_sha = read_signed(base_identity_path, "vm_macos_base_identity_hmac_sha256")
    reviewer, reviewer_sha = _read_stable_json_artifact(base_reviewer_path)
    if (base_identity.get("status") != "PASS_MACOS_BASE_SEALED"
            or base_identity.get("run_id") != run_id):
        raise RuntimeError("restore cleanup requires the sealed base identity for this exact run")
    base_dir = Path(base_identity["system_raw"]["path"]).parent
    if (base_dir != VM_ROOT / "templates" / f"macos-base-{run_id}"
            or base_dir.resolve(strict=True) != base_dir):
        raise RuntimeError("restore cleanup base path is not the exact canonical admitted template directory")
    admission_path = (REPO / base_identity["p2_admission_path"]).resolve(strict=True)
    admission = vm._read_signed_record(admission_path, "vm_admission_hmac_sha256")
    admission_sha = vm._file_sha256(admission_path)
    p1_final, p1_final_sha = read_signed(P1_FINAL_PATH, "restore_verification_hmac_sha256")
    if (admission.get("run_id") != run_id or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("session_id") != base_identity.get("p2_session_id")
            or base_identity.get("p2_admission_sha256") != admission_sha
            or p1_final_sha != P1_FINAL_SHA256
            or p1_final.get("status") != "PASS"
            or p1_final.get("local_sha256") != RESTORE_SHA256
            or p1_final.get("actual_length_bytes") != RESTORE_SIZE):
        raise RuntimeError("base identity and signed P2 admission do not share the requested run, session, and digest")
    _validate_base_review_binding(reviewer=reviewer, reviewer_sha=reviewer_sha,
                                 run_id=run_id, base_identity=base_identity,
                                 base_sha=base_sha, admission_sha=admission_sha)
    manifest_path = (REPO / base_identity["provisioner_source_manifest_path"]).resolve(strict=True)
    review_path = (REPO / base_identity["provisioner_review_path"]).resolve(strict=True)
    manifest, _helper_review = _verify_reviewed_provenance(manifest_path, review_path)
    if (vm._file_sha256(manifest_path) != base_identity.get("provisioner_source_manifest_sha256")
            or vm._file_sha256(review_path) != base_identity.get("provisioner_review_sha256")):
        raise RuntimeError("cleanup provenance no longer matches the sealed base identity")

    before = vm.capture_host_observation()
    current_hash, current_length, current_identity = sha256_file(RESTORE_PATH)
    if (current_hash != RESTORE_SHA256 or current_length != RESTORE_SIZE
            or current_identity != base_identity["restore_file_identity_at_install"]):
        raise RuntimeError("restore cleanup target is not the exact P1-accepted file bound to the sealed base")
    storage_blockers = vm._session_container_blockers(before, volume_uuid=VM_VOLUME_UUID)
    before_volume = vm._volume_row(before, VM_VOLUME_UUID)
    before_contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
    before_mount = vm._volume_mount_info(before, VM_VOLUME_UUID)
    before_safety = vm._volume_row(before, SAFETY_VOLUME_UUID)
    before_free = vm.conservative_container_free_bytes(before)
    if (storage_blockers
            or before_safety.get("CapacityInUse") != 16_106_127_360
            or before.get("safety_volume_info", {}).get("mount_state") != "UNMOUNTED_VERIFIED"
            or before.get("apfs_container", {}).get("APFSContainerUUID") != CONTAINER_UUID
            or before.get("physical_store_info", {}).get("DiskUUID") != PHYSICAL_STORE_UUID
            or before_mount.get("volume_uuid") != VM_VOLUME_UUID
            or before_contract.get("quota_bytes") != 53_687_091_200
            or not isinstance(before_volume.get("CapacityInUse"), int)
            or isinstance(before_volume.get("CapacityInUse"), bool)
            or before_volume.get("CapacityInUse") > before_contract.get("quota_bytes", -1)
            or before_free <= vm.VM_OPERATIONAL_WARNING_FREE):
        raise RuntimeError("restore cleanup authorization blocked by fresh storage drift or the 8 GiB early-stop threshold")
    descriptor_scan = _descriptor_scan_evidence([RESTORE_PATH])
    if (descriptor_scan.get("scan_clear") is not True
            or descriptor_scan.get("open_file_descriptors")
            or descriptor_scan.get("descriptor_identity_mismatches")
            or descriptor_scan.get("stderr_bytes") != 0
            or descriptor_scan.get("expected_file_identities_before_scan", [{}])[0].get("file_identity", {}).get("inode")
            != current_identity["inode"]):
        raise RuntimeError("restore deletion blocked because its exact descriptor scan is open or incomplete")
    process_blockers = _process_blockers()
    if process_blockers:
        raise RuntimeError("a VM provisioning helper or installer process is still present: "
                           + "; ".join(process_blockers[:5]))
    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_COMPLETE"
            or active.get("session_id") != admission.get("session_id")):
        raise RuntimeError("active VM marker is not the same-session completed stopped P2 boundary")

    root_check_fd = os.open(VM_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
    try:
        vm._verify_open_mount_fd(root_check_fd, before_mount)
        root_check_info = os.fstat(root_check_fd)
        root_mode_before = stat.S_IMODE(root_check_info.st_mode)
        if (not stat.S_ISDIR(root_check_info.st_mode)
                or root_check_info.st_dev != current_identity["device"]
                or root_check_info.st_uid != os.geteuid()
                or not root_mode_before & stat.S_IWUSR
                or not root_mode_before & stat.S_IXUSR):
            raise RuntimeError("VM-volume root cannot provide an owner-controlled cleanup namespace")
        root_namespace_before = _directory_access_evidence(
            VM_ROOT, root_check_fd, expected_uid=os.geteuid(),
            expected_mode=root_mode_before, expected_device=current_identity["device"])
        root_directory_identity = file_identity(root_check_info)
    finally:
        os.close(root_check_fd)
    cleanup_lock_evidence = args.p2_cleanup_lock_evidence
    _assert_no_interrupted_cleanup_namespace(
        run_id=run_id, root_directory_identity=root_directory_identity,
        root_mode=root_mode_before)
    recovered_pending_stages = _discard_orphan_pending_cleanup_stages(run_id=run_id)

    attempt_id = uuid.uuid4().hex[:12]
    authorization_path = unique_path(f"R6-VM-RESTORE-CLEANUP-AUTHORIZATION-{attempt_id}", run_id)
    pending_path = unique_path(f"R6-VM-RESTORE-CLEANUP-PENDING-{attempt_id}", run_id)
    cleanup_path = unique_path(f"R6-VM-RESTORE-CLEANUP-{attempt_id}", run_id)
    quarantine_directory_name = f".p2-restore-quarantine-{run_id}-{attempt_id}"
    quarantine_directory_path = VM_ROOT / quarantine_directory_name
    quarantine_path = quarantine_directory_path / "restore.ipsw"
    quarantine_scan_path = unique_path(f"R6-VM-RESTORE-QUARANTINE-SCAN-{attempt_id}", run_id)
    delete_authorization_path = unique_path(
        f"R6-VM-RESTORE-DELETE-AUTHORIZATION-{attempt_id}", run_id)
    storage_before = {
        "captured_at_unix": before.get("captured_at_unix"),
        "container_free_bytes": before.get("crosschecks", {}).get("apfs_container_free_bytes"),
        "data_container_free_bytes": before.get("crosschecks", {}).get("data_info_container_free_bytes"),
        "conservative_shared_free_bytes": before_free,
        "vm_volume_capacity_in_use_bytes": before_volume.get("CapacityInUse"),
        "vm_volume_quota_bytes": before_contract["quota_bytes"],
        "vm_volume_mount_identity": before_mount.get("mount_identity"),
        "vm_volume_mount_location": before_mount.get("mount_point"),
        "vm_volume_quota_headroom_bytes": before_contract["quota_bytes"] - before_volume["CapacityInUse"],
        "container_uuid": before.get("apfs_container", {}).get("APFSContainerUUID"),
        "physical_store_uuid": before.get("physical_store_info", {}).get("DiskUUID"),
        "safety_volume_uuid": SAFETY_VOLUME_UUID,
        "safety_reserve_bytes": 15 * GIB,
        "safety_volume_capacity_in_use_bytes": before_safety.get("CapacityInUse"),
        "safety_mount_state": before.get("safety_volume_info", {}).get("mount_state"),
    }
    metadata_paths = _provisioner_metadata_paths(
        manifest, manifest_path, review_path, admission_path, base_identity_path,
        base_reviewer_path, authorization_path, pending_path, quarantine_scan_path,
        delete_authorization_path)
    _assert_p2_metadata_budget(
        metadata_paths[:-2], additional_allocated_bytes=(
            _template_allocations(base_dir)["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE))
    authorization = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_CLEANUP_AUTHORIZATION",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "session_id": admission.get("session_id"),
        "created_at_unix": time.time(),
        "cleanup_transaction_lock": cleanup_lock_evidence,
        "authority": "DELETE_ONLY_THE_EXACT_P1_RESTORE_IPSW",
        "decision": "PASS",
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": RESTORE_SHA256,
        "restore_size_bytes": RESTORE_SIZE,
        "restore_file_identity": current_identity,
        "vm_volume_root_identity": root_directory_identity,
        "vm_volume_root_mode_before_cleanup": root_mode_before,
        "vm_volume_root_access_before_cleanup": root_namespace_before,
        "recovered_uncommitted_pending_stages": recovered_pending_stages,
        "p1_final_verification_path": str(P1_FINAL_PATH.relative_to(REPO)),
        "p1_final_verification_sha256": P1_FINAL_SHA256,
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "base_identity_path": str(base_identity_path.relative_to(REPO)),
        "base_identity_sha256": base_sha,
        "base_reviewer_path": str(base_reviewer_path.relative_to(REPO)),
        "base_reviewer_sha256": reviewer_sha,
        "base_reviewer_decision": reviewer["decision"],
        "independently_computed_system_raw_sha256": reviewer["independently_computed_system_raw_sha256"],
        "independently_computed_auxiliary_storage_sha256": reviewer["independently_computed_auxiliary_storage_sha256"],
        "cleanup_pending_path": str(pending_path.relative_to(REPO)),
        "quarantine_directory_path": str(quarantine_directory_path),
        "quarantine_path": str(quarantine_path),
        "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
        "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
        "delete_authority_state": "PENDING_POST_QUARANTINE_DESCRIPTOR_SCAN_AND_NAMESPACE_PROOF",
        "storage_before_deletion": storage_before,
        "open_ipsw_descriptor_check": descriptor_scan,
        "vm_state": "STOPPED",
        "installer_and_helper_processes_absent": True,
    }
    signed_write(authorization_path, authorization,
                 "vm_p2_restore_cleanup_authorization_hmac_sha256")
    authorization_sha = vm._file_sha256(authorization_path)
    pending_record = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_CLEANUP_PENDING",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "created_at_unix": time.time(),
        "cleanup_transaction_lock": cleanup_lock_evidence,
        "status": "PENDING_DELETION",
        "restore_path": str(RESTORE_PATH),
        "restore_sha256": RESTORE_SHA256,
        "restore_file_identity": current_identity,
        "vm_volume_root_identity": root_directory_identity,
        "vm_volume_root_mode_before_cleanup": root_mode_before,
        "quarantine_directory_path": str(quarantine_directory_path),
        "quarantine_path": str(quarantine_path),
        "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
        "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
        "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
        "cleanup_authorization_sha256": authorization_sha,
        "base_identity_path": str(base_identity_path.relative_to(REPO)),
        "base_identity_sha256": base_sha,
        "base_reviewer_path": str(base_reviewer_path.relative_to(REPO)),
        "base_reviewer_sha256": reviewer_sha,
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "storage_before_deletion": storage_before,
        "open_ipsw_descriptor_check": descriptor_scan,
        "vm_state": "STOPPED",
        "installer_and_helper_processes_absent": True,
    }
    _pending_record_readback, pending_sha = _write_pending_cleanup_receipt_atomically(
        pending_path, pending_record, attempt_id=attempt_id, run_id=run_id)
    metadata_inventory = _assert_p2_metadata_budget(
        metadata_paths, additional_allocated_bytes=(
            _template_allocations(base_dir)["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE))

    root_fd: int | None = None
    quarantine_dir_fd: int | None = None
    root_mode_protected: int | None = None
    moved_to_quarantine = False
    unlinked = False
    moved_identity: dict[str, int] | None = None
    root_namespace_during: dict[str, Any] | None = None
    quarantine_namespace_evidence: dict[str, Any] | None = None
    quarantine_scan: dict[str, Any] | None = None
    quarantine_scan_sha: str | None = None
    delete_authorization_sha: str | None = None
    try:
        _reviewer_readback, reviewer_sha_after = _read_stable_json_artifact(base_reviewer_path)
        if reviewer_sha_after != reviewer_sha:
            raise RuntimeError("independent base review changed after signed cleanup authorization")
        root_fd = os.open(VM_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                          | getattr(os, "O_NOFOLLOW", 0))
        root_info = os.fstat(root_fd)
        vm._verify_open_mount_fd(root_fd, before_mount)
        if (not stat.S_ISDIR(root_info.st_mode)
                or file_identity(root_info) != root_directory_identity
                or stat.S_IMODE(root_info.st_mode) != root_mode_before):
            raise RuntimeError("VM-volume root identity or mode changed after signed cleanup authorization")
        _directory_access_evidence(
            VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=root_mode_before,
            expected_device=current_identity["device"])

        root_mode_protected = root_mode_before | stat.S_ISVTX
        if root_mode_protected != root_mode_before:
            os.fchmod(root_fd, root_mode_protected)
            os.fsync(root_fd)
        root_namespace_during = _directory_access_evidence(
            VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=root_mode_protected,
            expected_device=current_identity["device"])

        os.mkdir(quarantine_directory_name, 0o700, dir_fd=root_fd)
        quarantine_dir_fd = os.open(
            quarantine_directory_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        os.fchmod(quarantine_dir_fd, 0o700)
        quarantine_namespace_evidence = _directory_access_evidence(
            quarantine_directory_path, quarantine_dir_fd, expected_uid=os.geteuid(),
            expected_mode=0o700, expected_device=current_identity["device"])
        before_move = os.stat("restore.ipsw", dir_fd=root_fd, follow_symlinks=False)
        if (file_identity(before_move) != current_identity or before_move.st_nlink != 1
                or not stat.S_ISREG(before_move.st_mode)):
            raise RuntimeError("restore target identity changed after signed cleanup authorization")
        _renameatx_exclusive(root_fd, "restore.ipsw", quarantine_dir_fd, "restore.ipsw")
        moved_to_quarantine = True
        os.fsync(root_fd)
        os.fsync(quarantine_dir_fd)
        moved_info = os.stat("restore.ipsw", dir_fd=quarantine_dir_fd, follow_symlinks=False)
        moved_identity = file_identity(moved_info)
        if (not stat.S_ISREG(moved_info.st_mode) or moved_info.st_nlink != 1
                or any(moved_identity[key] != current_identity[key] for key in (
                    "device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns"))):
            raise RuntimeError("atomically quarantined restore does not match the authorized P1 file identity")
        quarantine_namespace_evidence = _directory_access_evidence(
            quarantine_directory_path, quarantine_dir_fd, expected_uid=os.geteuid(),
            expected_mode=0o700, expected_device=current_identity["device"])
        quarantine_scan = _descriptor_scan_evidence([quarantine_path])
        if (quarantine_scan.get("scan_clear") is not True
                or quarantine_scan.get("open_file_descriptors")
                or quarantine_scan.get("descriptor_identity_mismatches")
                or quarantine_scan.get("stderr_bytes") != 0
                or quarantine_scan.get("expected_file_identities_before_scan", [{}])[0]
                .get("file_identity", {}).get("inode") != moved_identity["inode"]):
            raise RuntimeError("quarantined restore acquired a descriptor or its identity scan was incomplete")
        scan_record = {
            "schema_version": 1,
            "record_type": "R6_VM_RESTORE_QUARANTINE_SCAN",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "created_at_unix": time.time(),
            "cleanup_transaction_lock": cleanup_lock_evidence,
            "status": "PASS",
            "cleanup_pending_path": str(pending_path.relative_to(REPO)),
            "cleanup_pending_sha256": pending_sha,
            "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
            "cleanup_authorization_sha256": authorization_sha,
            "restore_path": str(RESTORE_PATH),
            "quarantine_directory_path": str(quarantine_directory_path),
            "quarantine_path": str(quarantine_path),
            "restore_sha256": RESTORE_SHA256,
            "restore_file_identity": current_identity,
            "quarantined_file_identity": moved_identity,
            "post_quarantine_descriptor_scan": quarantine_scan,
            "vm_volume_root_identity": root_directory_identity,
            "vm_volume_root_mode_before_cleanup": root_mode_before,
            "vm_volume_root_mode_during_unlink": root_mode_protected,
            "vm_volume_root_mode_restored": (
                os.path.lexists(VM_ROOT)
                and stat.S_IMODE(os.stat(VM_ROOT, follow_symlinks=False).st_mode) == root_mode_before),
            "vm_volume_root_access_during_unlink": root_namespace_during,
            "quarantine_directory_access_during_unlink": quarantine_namespace_evidence,
            "namespace_exclusion": {
                "parent_sticky_bit_prevents_other_owners_from_replacing_quarantine_directory":
                    bool(root_mode_protected & stat.S_ISVTX),
                "quarantine_directory_owner_only_access":
                    stat.S_IMODE(os.fstat(quarantine_dir_fd).st_mode) == 0o700,
                "quarantine_directory_acl_absent": True,
                "restore_has_one_link": moved_info.st_nlink == 1,
            },
        }
        signed_write(quarantine_scan_path, scan_record,
                     "vm_p2_restore_quarantine_scan_hmac_sha256")
        quarantine_scan_sha = vm._file_sha256(quarantine_scan_path)
        verified_scan, verified_scan_sha = read_signed(
            quarantine_scan_path, "vm_p2_restore_quarantine_scan_hmac_sha256")
        if (verified_scan_sha != quarantine_scan_sha
                or verified_scan.get("status") != "PASS"
                or verified_scan.get("cleanup_pending_sha256") != pending_sha
                or verified_scan.get("post_quarantine_descriptor_scan") != quarantine_scan
                or verified_scan.get("quarantined_file_identity") != moved_identity):
            raise RuntimeError("signed post-quarantine scan receipt failed binding readback")
        delete_authorization = {
            "schema_version": 1,
            "record_type": "R6_VM_RESTORE_DELETE_AUTHORIZATION",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "session_id": admission.get("session_id"),
            "created_at_unix": time.time(),
            "cleanup_transaction_lock": cleanup_lock_evidence,
            "authority": "DELETE_ONLY_THE_EXACT_P1_RESTORE_IPSW_FROM_THE_REVIEWED_PRIVATE_QUARANTINE",
            "decision": "PASS",
            "restore_path": str(RESTORE_PATH),
            "restore_sha256": RESTORE_SHA256,
            "restore_file_identity": current_identity,
            "quarantine_directory_path": str(quarantine_directory_path),
            "quarantine_path": str(quarantine_path),
            "quarantined_file_identity": moved_identity,
            "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
            "cleanup_authorization_sha256": authorization_sha,
            "cleanup_pending_path": str(pending_path.relative_to(REPO)),
            "cleanup_pending_sha256": pending_sha,
            "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
            "quarantine_scan_sha256": quarantine_scan_sha,
            "base_identity_sha256": base_sha,
            "base_reviewer_sha256": reviewer_sha,
            "p2_admission_sha256": admission_sha,
            "vm_volume_root_identity": root_directory_identity,
            "vm_volume_root_mode_before_cleanup": root_mode_before,
            "vm_volume_root_mode_during_unlink": root_mode_protected,
            "vm_volume_root_access_during_unlink": root_namespace_during,
            "quarantine_directory_access_during_unlink": quarantine_namespace_evidence,
            "post_quarantine_descriptor_scan_clear": True,
            "delete_authority": "EXACT_QUARANTINED_INODE_ONLY",
        }
        signed_write(delete_authorization_path, delete_authorization,
                     "vm_p2_restore_delete_authorization_hmac_sha256")
        delete_authorization_sha = vm._file_sha256(delete_authorization_path)
        verified_delete_authorization = vm._read_signed_record(
            delete_authorization_path, "vm_p2_restore_delete_authorization_hmac_sha256")
        if (vm._file_sha256(delete_authorization_path) != delete_authorization_sha
                or verified_delete_authorization.get("decision") != "PASS"
                or verified_delete_authorization.get("cleanup_pending_sha256") != pending_sha
                or verified_delete_authorization.get("quarantine_scan_sha256") != quarantine_scan_sha
                or verified_delete_authorization.get("quarantined_file_identity") != moved_identity):
            raise RuntimeError("signed exact-inode deletion authorization failed durable readback")
        metadata_inventory = _assert_p2_metadata_budget(
            metadata_paths, additional_allocated_bytes=(
                _template_allocations(base_dir)["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE))

        root_identity_now = file_identity(os.fstat(root_fd))
        if (root_identity_now["device"] != root_directory_identity["device"]
                or root_identity_now["inode"] != root_directory_identity["inode"]
                or stat.S_IMODE(os.fstat(root_fd).st_mode) != root_mode_protected):
            raise RuntimeError("sticky VM-volume namespace exclusion changed before restore unlink")
        _directory_access_evidence(
            VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=root_mode_protected,
            expected_device=current_identity["device"])
        quarantine_namespace_evidence = _directory_access_evidence(
            quarantine_directory_path, quarantine_dir_fd, expected_uid=os.geteuid(),
            expected_mode=0o700, expected_device=current_identity["device"])
        quarantine_directory_at_root = os.stat(
            quarantine_directory_name, dir_fd=root_fd, follow_symlinks=False)
        quarantine_directory_by_fd = os.fstat(quarantine_dir_fd)
        if (quarantine_directory_at_root.st_dev != quarantine_directory_by_fd.st_dev
                or quarantine_directory_at_root.st_ino != quarantine_directory_by_fd.st_ino):
            raise RuntimeError("private quarantine directory name no longer resolves to its pinned descriptor")
        immediately_before_unlink = os.stat(
            "restore.ipsw", dir_fd=quarantine_dir_fd, follow_symlinks=False)
        immediately_before_identity = file_identity(immediately_before_unlink)
        if (immediately_before_identity != moved_identity
                or immediately_before_unlink.st_nlink != 1
                or not stat.S_ISREG(immediately_before_unlink.st_mode)):
            raise RuntimeError("quarantine entry changed before identity-bound deletion")
        os.unlink("restore.ipsw", dir_fd=quarantine_dir_fd)
        unlinked = True
        moved_to_quarantine = False
        os.fsync(quarantine_dir_fd)
        if os.listdir(quarantine_dir_fd):
            raise RuntimeError("private quarantine is not empty after deleting the authorized IPSW inode")
        os.close(quarantine_dir_fd)
        quarantine_dir_fd = None
        os.rmdir(quarantine_directory_name, dir_fd=root_fd)
        os.fsync(root_fd)
        os.fchmod(root_fd, root_mode_before)
        os.fsync(root_fd)
        if (stat.S_IMODE(os.fstat(root_fd).st_mode) != root_mode_before
                or os.path.lexists(quarantine_directory_path)):
            raise RuntimeError("cleanup namespace could not be removed and restored to its original mode")
        os.close(root_fd)
        root_fd = None
        assessment = _cleanup_post_delete_assessment(
            base_identity=base_identity, base_dir=base_dir, before=before,
            quarantine_path=quarantine_path,
            quarantine_directory_path=quarantine_directory_path,
            expected_root_identity=root_directory_identity,
            expected_root_mode=root_mode_before)
        metadata_inventory = _assert_p2_metadata_budget(
            metadata_paths, additional_allocated_bytes=(
                _template_allocations(base_dir)["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE))
    except BaseException as error:
        if not unlinked:
            if moved_to_quarantine and root_fd is not None and quarantine_dir_fd is not None:
                try:
                    restore_info = os.stat("restore.ipsw", dir_fd=quarantine_dir_fd,
                                           follow_symlinks=False)
                    restore_identity = file_identity(restore_info)
                    if all(restore_identity[key] == current_identity[key] for key in (
                            "device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns")):
                        _renameatx_exclusive(quarantine_dir_fd, "restore.ipsw",
                                             root_fd, "restore.ipsw")
                        os.fsync(quarantine_dir_fd)
                        os.fsync(root_fd)
                        moved_to_quarantine = False
                except BaseException as recovery_error:
                    error = RuntimeError(f"{error}; restore quarantine recovery failed: {recovery_error}")
        if quarantine_dir_fd is not None:
            try:
                os.close(quarantine_dir_fd)
            except OSError as close_error:
                error = RuntimeError(f"{error}; quarantine directory descriptor close failed: {close_error}")
            quarantine_dir_fd = None
        if root_fd is not None:
            if not moved_to_quarantine and os.path.lexists(quarantine_directory_path):
                try:
                    os.rmdir(quarantine_directory_name, dir_fd=root_fd)
                    os.fsync(root_fd)
                except OSError as rmdir_error:
                    error = RuntimeError(f"{error}; quarantine directory cleanup failed: {rmdir_error}")
            if root_mode_protected is not None:
                try:
                    os.fchmod(root_fd, root_mode_before)
                    os.fsync(root_fd)
                except OSError as mode_error:
                    error = RuntimeError(f"{error}; VM-volume root mode restoration failed: {mode_error}")
            try:
                os.close(root_fd)
            except OSError as close_error:
                error = RuntimeError(f"{error}; cleanup directory descriptor close failed: {close_error}")
            root_fd = None
        if not unlinked:
            raise RuntimeError(f"restore cleanup stopped before deleting the authorized IPSW: {error}") from error
        fallback = {
            "schema_version": 1,
            "record_type": "R6_VM_RESTORE_CLEANUP",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "created_at_unix": time.time(),
            "cleanup_transaction_lock": cleanup_lock_evidence,
            "status": "UNKNOWN",
            "cleanup_pending_path": str(pending_path.relative_to(REPO)),
            "cleanup_pending_sha256": pending_sha,
            "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
            "cleanup_authorization_sha256": authorization_sha,
            "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
            "quarantine_scan_sha256": quarantine_scan_sha,
            "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
            "delete_authorization_sha256": delete_authorization_sha,
            "post_quarantine_descriptor_scan": quarantine_scan,
            "vm_volume_root_mode_before_cleanup": root_mode_before,
            "vm_volume_root_mode_during_unlink": root_mode_protected,
            "vm_volume_root_access_during_unlink": root_namespace_during,
            "quarantine_directory_path": str(quarantine_directory_path),
            "quarantine_directory_access_during_unlink": quarantine_namespace_evidence,
            "deleted_path": str(RESTORE_PATH),
            "deleted_file_sha256": RESTORE_SHA256,
            "deleted_file_identity": current_identity,
            "restore_absent": not os.path.lexists(RESTORE_PATH),
            "restore_files_remaining": ([str(quarantine_path)] if os.path.lexists(quarantine_path) else []),
            "quarantine_directory_absent": not os.path.lexists(quarantine_directory_path),
            "RESTORE_CLEANUP_RECLAIMED_BYTES": 0,
            "storage_before": storage_before,
            "storage_after": {},
            "base_identity_path": str(base_identity_path.relative_to(REPO)),
            "base_identity_sha256": base_sha,
            "p2_admission_path": str(admission_path.relative_to(REPO)),
            "p2_admission_sha256": admission_sha,
            "session_id": admission.get("session_id"),
            "base_reviewer_path": str(base_reviewer_path.relative_to(REPO)),
            "base_reviewer_sha256": reviewer_sha,
            "base_artifacts_intact_by_file_identity_and_hash": False,
            "blockers": [f"post-delete storage or inventory measurement failed: {error}"],
            "reconciliation_required": True,
        }
        signed_write(cleanup_path, fallback, "vm_p2_restore_cleanup_hmac_sha256")
        raise RuntimeError("restore was deleted but signed cleanup disposition is UNKNOWN; run read-only cleanup "
                           f"reconciliation with pending receipt {pending_path.relative_to(REPO)} and result "
                           f"{cleanup_path.relative_to(REPO)}") from error

    cleanup_record = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_CLEANUP",
        "run_id": run_id,
        "attempt_id": attempt_id,
        "created_at_unix": time.time(),
        "cleanup_transaction_lock": cleanup_lock_evidence,
        "status": "PASS" if not assessment["blockers"] else "FAIL",
        "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
        "cleanup_authorization_sha256": authorization_sha,
        "cleanup_pending_path": str(pending_path.relative_to(REPO)),
        "cleanup_pending_sha256": pending_sha,
        "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
        "quarantine_scan_sha256": quarantine_scan_sha,
        "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
        "delete_authorization_sha256": delete_authorization_sha,
        "post_quarantine_descriptor_scan": quarantine_scan,
        "vm_volume_root_identity": root_directory_identity,
        "vm_volume_root_mode_before_cleanup": root_mode_before,
        "vm_volume_root_mode_during_unlink": root_mode_protected,
        "vm_volume_root_mode_restored": assessment["vm_volume_root_mode_restored"],
        "vm_volume_root_access_after_cleanup": assessment["vm_volume_root_access_after_cleanup"],
        "vm_volume_root_access_during_unlink": root_namespace_during,
        "quarantine_directory_path": str(quarantine_directory_path),
        "quarantine_directory_access_during_unlink": quarantine_namespace_evidence,
        "quarantine_path": str(quarantine_path),
        "quarantined_file_identity_after_atomic_move": moved_identity,
        "deleted_path": str(RESTORE_PATH),
        "deleted_file_sha256": RESTORE_SHA256,
        "deleted_file_identity": current_identity,
        "restore_absent": assessment["restore_absent"],
        "restore_files_remaining": assessment["restore_files_remaining"],
        "quarantine_directory_absent": assessment["quarantine_directory_absent"],
        "vm_volume_root_after_cleanup": assessment["vm_volume_root_after_cleanup"],
        "RESTORE_CLEANUP_RECLAIMED_BYTES": assessment["reclaimed"],
        "measurement_method": "difference between conservative min(APFS container free, Data APFSContainerFree) immediately before and after unlink; APFS reclaim is measured, not inferred from logical size",
        "storage_before": storage_before,
        "storage_after": assessment["storage_after"],
        "base_identity_path": str(base_identity_path.relative_to(REPO)),
        "base_identity_sha256": base_sha,
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "session_id": admission.get("session_id"),
        "base_reviewer_path": str(base_reviewer_path.relative_to(REPO)),
        "base_reviewer_sha256": reviewer_sha,
        "base_artifacts_intact_by_file_identity_and_hash": assessment["base_artifacts_intact"],
        "open_ipsw_descriptor_check": descriptor_scan,
        "post_delete_metadata_inventory": metadata_inventory,
        "blockers": assessment["blockers"],
    }
    signed_write(cleanup_path, cleanup_record, "vm_p2_restore_cleanup_hmac_sha256")
    print(json.dumps({"status": cleanup_record["status"], "authorization_path": str(authorization_path),
                      "quarantine_scan_path": str(quarantine_scan_path),
                      "delete_authorization_path": str(delete_authorization_path),
                      "pending_path": str(pending_path), "cleanup_path": str(cleanup_path),
                      "RESTORE_CLEANUP_RECLAIMED_BYTES": assessment["reclaimed"],
                      "restore_absent": assessment["restore_absent"],
                      "blockers": assessment["blockers"]}, sort_keys=True))
    if assessment["blockers"]:
        raise RuntimeError("restore was deleted but its signed cleanup result is FAIL: "
                           + "; ".join(assessment["blockers"][:8]))


def recover_cleanup(args: argparse.Namespace) -> None:
    run_id = args.run_id
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id) is None:
        raise RuntimeError("cleanup recovery run id contains unsupported path characters")
    base_identity_path = Path(args.base_identity).resolve(strict=True)
    base_reviewer_path = Path(args.base_reviewer).resolve(strict=True)
    pending_path = Path(args.pending).resolve(strict=True)
    if any(path.parent != REPO for path in (base_identity_path, base_reviewer_path, pending_path)):
        raise RuntimeError("cleanup recovery receipts must be canonical files in the repository root")
    base_identity, base_sha = read_signed(base_identity_path, "vm_macos_base_identity_hmac_sha256")
    reviewer, reviewer_sha = _read_stable_json_artifact(base_reviewer_path)
    pending, pending_sha = read_signed(pending_path, "vm_p2_restore_cleanup_pending_hmac_sha256")
    admission_path = (REPO / base_identity["p2_admission_path"]).resolve(strict=True)
    admission = vm._read_signed_record(admission_path, "vm_admission_hmac_sha256")
    admission_sha = vm._file_sha256(admission_path)
    p1_final, p1_final_sha = read_signed(P1_FINAL_PATH, "restore_verification_hmac_sha256")
    _validate_base_review_binding(reviewer=reviewer, reviewer_sha=reviewer_sha,
                                  run_id=run_id, base_identity=base_identity,
                                  base_sha=base_sha, admission_sha=admission_sha)
    cleanup_lock_evidence = args.p2_cleanup_lock_evidence
    if (base_identity.get("status") != "PASS_MACOS_BASE_SEALED"
            or base_identity.get("run_id") != run_id
            or base_identity.get("p2_admission_sha256") != admission_sha
            or admission.get("run_id") != run_id
            or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("session_id") != base_identity.get("p2_session_id")
            or p1_final_sha != P1_FINAL_SHA256
            or p1_final.get("status") != "PASS"
            or p1_final.get("local_sha256") != RESTORE_SHA256
            or p1_final.get("actual_length_bytes") != RESTORE_SIZE
            or pending.get("status") != "PENDING_DELETION"
            or pending.get("run_id") != run_id
            or pending.get("base_identity_sha256") != base_sha
            or pending.get("base_reviewer_sha256") != reviewer_sha
            or pending.get("p2_admission_sha256") != admission_sha
            or pending.get("restore_path") != str(RESTORE_PATH)
            or pending.get("restore_sha256") != RESTORE_SHA256
            or pending.get("restore_file_identity") != base_identity.get("restore_file_identity_at_install")
            or not _valid_p2_cleanup_lock_evidence(pending.get("cleanup_transaction_lock"))):
        raise RuntimeError("cleanup recovery is not bound to the accepted P1 restore, sealed base, and signed P2 pending state")
    expected_directory = VM_ROOT / f".p2-restore-quarantine-{run_id}-{pending.get('attempt_id')}"
    quarantine_directory_path = Path(pending.get("quarantine_directory_path", ""))
    quarantine_path = Path(pending.get("quarantine_path", ""))
    if (quarantine_directory_path != expected_directory
            or quarantine_path != expected_directory / "restore.ipsw"
            or pending.get("restore_path") != str(RESTORE_PATH)):
        raise RuntimeError("signed pending cleanup state contains a noncanonical recovery namespace")
    authorization_path = (REPO / pending["cleanup_authorization_path"]).resolve(strict=True)
    authorization, authorization_sha = read_signed(
        authorization_path, "vm_p2_restore_cleanup_authorization_hmac_sha256")
    if (authorization.get("decision") != "PASS"
            or authorization.get("run_id") != run_id
            or authorization.get("cleanup_pending_path") != str(pending_path.relative_to(REPO))
            or authorization.get("cleanup_pending_sha256") not in (None, pending_sha)
            or authorization.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
            or authorization.get("restore_path") != str(RESTORE_PATH)
            or authorization.get("restore_sha256") != RESTORE_SHA256
            or authorization.get("restore_file_identity") != pending.get("restore_file_identity")
            or authorization.get("base_identity_sha256") != base_sha
            or authorization.get("base_reviewer_sha256") != reviewer_sha
            or authorization.get("p2_admission_sha256") != admission_sha
            or authorization.get("quarantine_directory_path") != str(quarantine_directory_path)
            or authorization.get("quarantine_path") != str(quarantine_path)
            or authorization.get("quarantine_scan_path") != pending.get("quarantine_scan_path")
            or authorization.get("delete_authorization_path") != pending.get("delete_authorization_path")
            or authorization.get("vm_volume_root_identity") != pending.get("vm_volume_root_identity")
            or authorization.get("vm_volume_root_mode_before_cleanup")
            != pending.get("vm_volume_root_mode_before_cleanup")
            or pending.get("cleanup_authorization_sha256") != authorization_sha):
        raise RuntimeError("cleanup recovery lacks its exact signed pending authorization binding")

    scan_relative = Path(pending["quarantine_scan_path"])
    delete_authorization_relative = Path(pending["delete_authorization_path"])
    if (scan_relative.is_absolute() or scan_relative.parent != Path(".")
            or delete_authorization_relative.is_absolute()
            or delete_authorization_relative.parent != Path(".")):
        raise RuntimeError("signed cleanup receipt paths are not canonical repository-relative names")
    scan_path = REPO / scan_relative
    delete_authorization_path = REPO / delete_authorization_relative
    scan_file_exists = os.path.lexists(scan_path)
    delete_authorization_file_exists = os.path.lexists(delete_authorization_path)
    scan_exists = scan_file_exists
    delete_authorization_exists = False
    scan: dict[str, Any] | None = None
    scan_sha: str | None = None
    delete_authorization: dict[str, Any] | None = None
    delete_authorization_sha: str | None = None
    scan_receipt_error: str | None = None
    delete_authorization_receipt_error: str | None = None
    if scan_exists:
        try:
            scan, scan_sha = read_signed(scan_path, "vm_p2_restore_quarantine_scan_hmac_sha256")
            if (scan.get("status") != "PASS"
                    or scan.get("run_id") != run_id
                    or scan.get("cleanup_pending_sha256") != pending_sha
                    or scan.get("cleanup_authorization_sha256") != authorization_sha
                    or scan.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
                    or scan.get("restore_file_identity") != pending.get("restore_file_identity")
                    or scan.get("quarantine_directory_path") != str(quarantine_directory_path)
                    or scan.get("quarantine_path") != str(quarantine_path)
                    or scan.get("vm_volume_root_identity") != pending.get("vm_volume_root_identity")
                    or scan.get("vm_volume_root_mode_before_cleanup")
                    != pending.get("vm_volume_root_mode_before_cleanup")
                    or not isinstance(scan.get("namespace_exclusion"), Mapping)
                    or scan["namespace_exclusion"].get(
                        "parent_sticky_bit_prevents_other_owners_from_replacing_quarantine_directory") is not True
                    or scan["namespace_exclusion"].get("quarantine_directory_owner_only_access") is not True
                    or scan["namespace_exclusion"].get("quarantine_directory_acl_absent") is not True):
                raise RuntimeError("signed quarantine scan does not bind this exact cleanup attempt")
        except (OSError, RuntimeError, ValueError) as error:
            scan_receipt_error = str(error)
            scan = None
            scan_sha = None
            scan_exists = False
    if delete_authorization_file_exists:
        try:
            if not scan_exists or scan is None or scan_sha is None:
                raise RuntimeError("delete authorization exists without its signed post-quarantine scan")
            delete_authorization, delete_authorization_sha = read_signed(
                delete_authorization_path, "vm_p2_restore_delete_authorization_hmac_sha256")
            if (delete_authorization.get("decision") != "PASS"
                    or delete_authorization.get("run_id") != run_id
                    or delete_authorization.get("session_id") != admission.get("session_id")
                    or delete_authorization.get("authority")
                    != "DELETE_ONLY_THE_EXACT_P1_RESTORE_IPSW_FROM_THE_REVIEWED_PRIVATE_QUARANTINE"
                    or delete_authorization.get("cleanup_transaction_lock")
                    != pending.get("cleanup_transaction_lock")
                    or delete_authorization.get("cleanup_authorization_sha256") != authorization_sha
                    or delete_authorization.get("cleanup_pending_sha256") != pending_sha
                    or delete_authorization.get("restore_path") != str(RESTORE_PATH)
                    or delete_authorization.get("restore_sha256") != RESTORE_SHA256
                    or delete_authorization.get("restore_file_identity") != pending.get("restore_file_identity")
                    or delete_authorization.get("quarantine_scan_path")
                    != str(scan_path.relative_to(REPO))
                    or delete_authorization.get("quarantine_scan_sha256") != scan_sha
                    or delete_authorization.get("quarantined_file_identity")
                    != scan.get("quarantined_file_identity")
                    or delete_authorization.get("quarantine_directory_path") != str(quarantine_directory_path)
                    or delete_authorization.get("quarantine_path") != str(quarantine_path)
                    or delete_authorization.get("vm_volume_root_identity")
                    != pending.get("vm_volume_root_identity")
                    or delete_authorization.get("vm_volume_root_mode_before_cleanup")
                    != pending.get("vm_volume_root_mode_before_cleanup")
                    or delete_authorization.get("base_identity_sha256") != base_sha
                    or delete_authorization.get("base_reviewer_sha256") != reviewer_sha
                    or delete_authorization.get("p2_admission_sha256") != admission_sha):
                raise RuntimeError("delete authorization does not bind the signed quarantine scan and P2 authority")
            delete_authorization_exists = True
        except (OSError, RuntimeError, ValueError) as error:
            delete_authorization_receipt_error = str(error)
            delete_authorization = None
            delete_authorization_sha = None

    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_COMPLETE"
            or active.get("session_id") != admission.get("session_id") or _process_blockers()):
        raise RuntimeError("cleanup recovery is not at the same-session stopped P2 boundary")
    observation = vm.capture_host_observation()
    recovery_volume = vm._volume_row(observation, VM_VOLUME_UUID)
    recovery_capacity_contract = vm._active_capacity_contract(volume_uuid=VM_VOLUME_UUID)
    if (vm._session_container_blockers(observation, volume_uuid=VM_VOLUME_UUID)
            or vm._volume_row(observation, SAFETY_VOLUME_UUID).get("CapacityInUse") != 16_106_127_360
            or observation.get("safety_volume_info", {}).get("mount_state") != "UNMOUNTED_VERIFIED"
            or observation.get("apfs_container", {}).get("APFSContainerUUID") != CONTAINER_UUID
            or observation.get("physical_store_info", {}).get("DiskUUID") != PHYSICAL_STORE_UUID
            or recovery_capacity_contract.get("quota_bytes") != 53_687_091_200
            or not isinstance(recovery_volume.get("CapacityInUse"), int)
            or isinstance(recovery_volume.get("CapacityInUse"), bool)
            or recovery_volume.get("CapacityInUse") > recovery_capacity_contract.get("quota_bytes", -1)):
        raise RuntimeError("cleanup recovery storage identities or safety reserve differ from the accepted P2 domain")
    mount = vm._volume_mount_info(observation, VM_VOLUME_UUID)
    if mount.get("volume_uuid") != VM_VOLUME_UUID:
        raise RuntimeError("cleanup recovery VM-volume mount is not the exact admitted storage volume")
    root_fd = os.open(VM_ROOT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_NOFOLLOW", 0))
    quarantine_dir_fd: int | None = None
    attempt_id: str | None = None
    intent_path: Path | None = None
    intent_sha: str | None = None
    ready_path: Path | None = None
    ready_sha: str | None = None
    result_path: Path | None = None
    metadata_paths: list[Path] | None = None
    base_dir: Path | None = None
    try:
        root_info = os.fstat(root_fd)
        expected_root = pending.get("vm_volume_root_identity")
        original_mode = pending.get("vm_volume_root_mode_before_cleanup")
        if (not isinstance(expected_root, Mapping)
                or not isinstance(original_mode, int) or isinstance(original_mode, bool)
                or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_dev != expected_root.get("device")
                or root_info.st_ino != expected_root.get("inode")
                or root_info.st_uid != os.geteuid()
                or stat.S_IMODE(root_info.st_mode) not in {original_mode, original_mode | stat.S_ISVTX}):
            raise RuntimeError("cleanup recovery VM-volume root identity or mode is outside the signed transaction state")
        vm._verify_open_mount_fd(root_fd, mount)
        current_root_mode = stat.S_IMODE(root_info.st_mode)
        _directory_access_evidence(
            VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=current_root_mode,
            expected_device=int(expected_root["device"]))

        try:
            original_info = os.stat("restore.ipsw", dir_fd=root_fd, follow_symlinks=False)
            original_identity = file_identity(original_info)
        except FileNotFoundError:
            original_info = None
            original_identity = None
        try:
            quarantine_directory_info = os.stat(
                quarantine_directory_path.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            quarantine_directory_info = None
        if quarantine_directory_info is not None:
            if (not stat.S_ISDIR(quarantine_directory_info.st_mode)
                    or quarantine_directory_info.st_dev != expected_root.get("device")
                    or quarantine_directory_info.st_uid != os.geteuid()):
                raise RuntimeError("interrupted quarantine name is not an owned directory on the admitted VM volume")
            quarantine_dir_fd = os.open(
                quarantine_directory_path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
            quarantine_access = _directory_access_evidence(
                quarantine_directory_path, quarantine_dir_fd, expected_uid=os.geteuid(),
                expected_mode=0o700, expected_device=int(expected_root["device"]))
            entries = os.listdir(quarantine_dir_fd)
            if any(name != "restore.ipsw" for name in entries) or len(entries) > 1:
                raise RuntimeError("interrupted quarantine contains an unexpected entry; recovery will not remove it")
            try:
                quarantined_info = os.stat("restore.ipsw", dir_fd=quarantine_dir_fd,
                                           follow_symlinks=False)
                quarantined_identity = file_identity(quarantined_info)
            except FileNotFoundError:
                quarantined_info = None
                quarantined_identity = None
        else:
            quarantine_access = None
            quarantined_info = None
            quarantined_identity = None

        stable_keys = ("device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns")
        authorized_identity = pending["restore_file_identity"]
        for label, info, identity in (("original", original_info, original_identity),
                                      ("quarantined", quarantined_info, quarantined_identity)):
            if info is not None and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or any(identity.get(key) != authorized_identity.get(key) for key in stable_keys)):
                raise RuntimeError(f"{label} recovery entry is not the exact authorized P1 restore inode")
        if original_info is not None and quarantined_info is not None:
            raise RuntimeError("the P1 restore appears at both original and quarantine paths")
        if not delete_authorization_exists and original_info is None and quarantined_info is None:
            raise RuntimeError("restore is absent without durable delete authorization; recovery cannot infer deletion")

        action = "COMPLETE_AUTHORIZED_DELETION" if delete_authorization_exists else "RESTORE_AUTHORIZED_FILE"
        attempt_id = uuid.uuid4().hex[:12]
        intent_path = unique_path(f"R6-VM-RESTORE-CLEANUP-RECOVERY-INTENT-{attempt_id}", run_id)
        ready_path = unique_path(f"R6-VM-RESTORE-CLEANUP-RECOVERY-DELETE-READY-{attempt_id}", run_id)
        result_path = unique_path(f"R6-VM-RESTORE-CLEANUP-RECOVERY-{attempt_id}", run_id)
        base_dir = Path(base_identity["system_raw"]["path"]).parent
        manifest_path = (REPO / base_identity["provisioner_source_manifest_path"]).resolve(strict=True)
        review_path = (REPO / base_identity["provisioner_review_path"]).resolve(strict=True)
        manifest, _helper_review = _verify_reviewed_provenance(manifest_path, review_path)
        if (vm._file_sha256(manifest_path) != base_identity.get("provisioner_source_manifest_sha256")
                or vm._file_sha256(review_path) != base_identity.get("provisioner_review_sha256")):
            raise RuntimeError("cleanup recovery helper provenance changed after base sealing")
        metadata_paths = _provisioner_metadata_paths(
            manifest, manifest_path, review_path, admission_path, base_identity_path,
            base_reviewer_path, authorization_path, pending_path,
            *((scan_path,) if scan_file_exists else ()),
            *((delete_authorization_path,) if delete_authorization_file_exists else ()),
            intent_path, ready_path, result_path)
        _assert_p2_metadata_budget(
            metadata_paths[:-3], additional_allocated_bytes=(
                _template_allocations(base_dir)["metadata_bytes"]
                + P2_METADATA_FINALIZATION_RESERVE + 128 * 1024))
        existing_file_path = (quarantine_path if quarantined_info is not None else
                              RESTORE_PATH if original_info is not None else None)
        existing_file_identity = (quarantined_identity if quarantined_info is not None else
                                  original_identity if original_info is not None else None)
        if existing_file_path is not None:
            digest, length, hashed_identity = sha256_file(existing_file_path, maximum=RESTORE_SIZE)
            if (digest != RESTORE_SHA256 or length != RESTORE_SIZE
                    or any(hashed_identity.get(key) != authorized_identity.get(key) for key in stable_keys)):
                raise RuntimeError("recovery hash verification failed for the exact accepted restore")
        initial_descriptor_scan = None
        if delete_authorization_exists and existing_file_path == RESTORE_PATH:
            initial_descriptor_scan = _descriptor_scan_evidence([RESTORE_PATH])
            if (initial_descriptor_scan.get("scan_clear") is not True
                    or initial_descriptor_scan.get("open_file_descriptors")
                    or initial_descriptor_scan.get("descriptor_identity_mismatches")
                    or initial_descriptor_scan.get("stderr_bytes") != 0):
                raise RuntimeError("cannot resume exact restore deletion while a descriptor remains open or scan is incomplete")
        intent = {
            "schema_version": 1,
            "record_type": "R6_VM_RESTORE_CLEANUP_RECOVERY_INTENT",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "created_at_unix": time.time(),
            "decision": "PASS",
            "action": action,
            "cleanup_pending_path": str(pending_path.relative_to(REPO)),
            "cleanup_pending_sha256": pending_sha,
            "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
            "cleanup_authorization_sha256": authorization_sha,
            "quarantine_scan_path": str(scan_path.relative_to(REPO)),
            "quarantine_scan_sha256": scan_sha,
            "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
            "delete_authorization_sha256": delete_authorization_sha,
            "base_identity_sha256": base_sha,
            "base_reviewer_sha256": reviewer_sha,
            "p2_admission_sha256": admission_sha,
            "restore_file_identity": authorized_identity,
            "observed_original_file_identity": original_identity,
            "observed_quarantined_file_identity": quarantined_identity,
            "quarantine_scan_receipt_read_error": scan_receipt_error,
            "delete_authorization_receipt_read_error": delete_authorization_receipt_error,
            "initial_descriptor_scan": initial_descriptor_scan,
            "vm_volume_root_identity": expected_root,
            "vm_volume_root_mode_before_cleanup": original_mode,
            "vm_volume_root_mode_at_recovery_start": current_root_mode,
            "quarantine_directory_path": str(quarantine_directory_path),
            "quarantine_directory_access_at_recovery_start": quarantine_access,
            "cleanup_transaction_lock": pending.get("cleanup_transaction_lock"),
            "recovery_transaction_lock": cleanup_lock_evidence,
            "same_uid_trust_boundary": P2_CLEANUP_LOCK_SCOPE,
            "storage_state": {
                "vm_volume_uuid": VM_VOLUME_UUID,
                "vm_volume_capacity_in_use_bytes": recovery_volume.get("CapacityInUse"),
                "vm_volume_quota_bytes": recovery_capacity_contract.get("quota_bytes"),
                "container_uuid": observation.get("apfs_container", {}).get("APFSContainerUUID"),
                "physical_store_uuid": observation.get("physical_store_info", {}).get("DiskUUID"),
                "safety_volume_uuid": SAFETY_VOLUME_UUID,
                "safety_reserve_bytes": 15 * GIB,
                "safety_mount_state": observation.get("safety_volume_info", {}).get("mount_state"),
            },
        }
        signed_write(intent_path, intent, "vm_p2_restore_cleanup_recovery_intent_hmac_sha256")
        intent_sha = vm._file_sha256(intent_path)

        root_mode_protected = original_mode | stat.S_ISVTX
        recovery_deleted_identity: dict[str, int] | None = None
        if delete_authorization_exists:
            ready_sha = None
            if existing_file_path is not None:
                if current_root_mode != root_mode_protected:
                    os.fchmod(root_fd, root_mode_protected)
                    os.fsync(root_fd)
                if quarantine_dir_fd is None:
                    os.mkdir(quarantine_directory_path.name, 0o700, dir_fd=root_fd)
                    quarantine_dir_fd = os.open(
                        quarantine_directory_path.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
                    os.fchmod(quarantine_dir_fd, 0o700)
                if original_info is not None:
                    _renameatx_exclusive(root_fd, "restore.ipsw", quarantine_dir_fd, "restore.ipsw")
                    os.fsync(root_fd)
                    os.fsync(quarantine_dir_fd)
                entries = os.listdir(quarantine_dir_fd)
                if len(entries) != 1 or entries[0] != "restore.ipsw":
                    raise RuntimeError("authorized recovery quarantine does not contain exactly the accepted IPSW")
                recovery_identity = file_identity(os.stat(
                    "restore.ipsw", dir_fd=quarantine_dir_fd, follow_symlinks=False))
                if any(recovery_identity.get(key) != authorized_identity.get(key) for key in stable_keys):
                    raise RuntimeError("recovery quarantine inode changed before authorized deletion")
                digest, length, hashed_identity = sha256_file(quarantine_path, maximum=RESTORE_SIZE)
                if (digest != RESTORE_SHA256 or length != RESTORE_SIZE
                        or any(hashed_identity.get(key) != authorized_identity.get(key) for key in stable_keys)):
                    raise RuntimeError("recovery could not reverify the exact accepted IPSW before deletion")
                recovery_scan = _descriptor_scan_evidence([quarantine_path])
                if (recovery_scan.get("scan_clear") is not True
                        or recovery_scan.get("open_file_descriptors")
                        or recovery_scan.get("descriptor_identity_mismatches")
                        or recovery_scan.get("stderr_bytes") != 0
                        or recovery_scan.get("expected_file_identities_before_scan", [{}])[0]
                        .get("file_identity", {}).get("inode") != recovery_identity.get("inode")):
                    raise RuntimeError("recovery deletion blocked by an open descriptor or incomplete post-quarantine scan")
                readiness = {
                "schema_version": 1,
                "record_type": "R6_VM_RESTORE_CLEANUP_RECOVERY_DELETE_READY",
                "run_id": run_id,
                "attempt_id": attempt_id,
                "created_at_unix": time.time(),
                "decision": "PASS",
                "cleanup_recovery_intent_path": str(intent_path.relative_to(REPO)),
                "cleanup_recovery_intent_sha256": intent_sha,
                "cleanup_pending_sha256": pending_sha,
                "delete_authorization_sha256": delete_authorization_sha,
                "restore_sha256": RESTORE_SHA256,
                "restore_file_identity": recovery_identity,
                "post_recovery_descriptor_scan": recovery_scan,
                "quarantine_directory_path": str(quarantine_directory_path),
                "vm_volume_root_identity": expected_root,
                "vm_volume_root_mode_during_unlink": root_mode_protected,
                "cleanup_transaction_lock": pending.get("cleanup_transaction_lock"),
                "recovery_transaction_lock": cleanup_lock_evidence,
                }
                _assert_p2_metadata_budget(
                    metadata_paths[:-2], additional_allocated_bytes=(
                        _template_allocations(base_dir)["metadata_bytes"]
                        + P2_METADATA_FINALIZATION_RESERVE + 128 * 1024))
                signed_write(ready_path, readiness, "vm_p2_restore_cleanup_recovery_ready_hmac_sha256")
                ready_sha = vm._file_sha256(ready_path)
                recovery_deleted_identity = recovery_identity
                root_now = os.fstat(root_fd)
                directory_now = os.fstat(quarantine_dir_fd)
                leaf_now = os.stat("restore.ipsw", dir_fd=quarantine_dir_fd, follow_symlinks=False)
                leaf_identity_now = file_identity(leaf_now)
                if (root_now.st_dev != expected_root.get("device")
                        or root_now.st_ino != expected_root.get("inode")
                        or stat.S_IMODE(root_now.st_mode) != root_mode_protected
                        or not stat.S_ISDIR(directory_now.st_mode)
                        or directory_now.st_dev != recovery_identity["device"]
                        or leaf_identity_now != recovery_identity
                        or leaf_now.st_nlink != 1):
                    raise RuntimeError("recovery delete namespace changed after durable readiness evidence")
                _directory_access_evidence(
                    VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=root_mode_protected,
                    expected_device=int(expected_root["device"]))
                _directory_access_evidence(
                    quarantine_directory_path, quarantine_dir_fd, expected_uid=os.geteuid(),
                    expected_mode=0o700, expected_device=recovery_identity["device"])
                os.unlink("restore.ipsw", dir_fd=quarantine_dir_fd)
                os.fsync(quarantine_dir_fd)
                if os.listdir(quarantine_dir_fd):
                    raise RuntimeError("recovery quarantine contains an unexpected file after authorized unlink")
                os.close(quarantine_dir_fd)
                quarantine_dir_fd = None
                os.rmdir(quarantine_directory_path.name, dir_fd=root_fd)
                os.fsync(root_fd)
            elif quarantine_dir_fd is not None:
                if os.listdir(quarantine_dir_fd):
                    raise RuntimeError("recovery will not remove a quarantine directory that is not empty")
                os.close(quarantine_dir_fd)
                quarantine_dir_fd = None
                os.rmdir(quarantine_directory_path.name, dir_fd=root_fd)
                os.fsync(root_fd)
            if recovery_deleted_identity is None and delete_authorization is not None:
                recovery_deleted_identity = delete_authorization.get("quarantined_file_identity")
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != original_mode:
                os.fchmod(root_fd, original_mode)
                os.fsync(root_fd)
            outcome = "RESTORE_DELETION_COMPLETED"
        else:
            ready_sha = None
            if quarantined_info is not None:
                scan_to_restore = _descriptor_scan_evidence([quarantine_path])
                if (scan_to_restore.get("scan_clear") is not True
                        or scan_to_restore.get("open_file_descriptors")
                        or scan_to_restore.get("descriptor_identity_mismatches")
                        or scan_to_restore.get("stderr_bytes") != 0):
                    raise RuntimeError("cannot safely return the authorized IPSW while descriptors remain open")
                if current_root_mode != root_mode_protected:
                    os.fchmod(root_fd, root_mode_protected)
                    os.fsync(root_fd)
                _renameatx_exclusive(quarantine_dir_fd, "restore.ipsw", root_fd, "restore.ipsw")
                os.fsync(quarantine_dir_fd)
                os.fsync(root_fd)
            if quarantine_dir_fd is not None:
                if os.listdir(quarantine_dir_fd):
                    raise RuntimeError("recovery will not remove a quarantine directory that is not empty")
                os.close(quarantine_dir_fd)
                quarantine_dir_fd = None
                os.rmdir(quarantine_directory_path.name, dir_fd=root_fd)
                os.fsync(root_fd)
            restored_hash, restored_size, restored_identity = sha256_file(RESTORE_PATH, maximum=RESTORE_SIZE)
            if (restored_hash != RESTORE_SHA256 or restored_size != RESTORE_SIZE
                    or any(restored_identity.get(key) != authorized_identity.get(key) for key in stable_keys)):
                raise RuntimeError("recovery did not restore the exact accepted P1 IPSW")
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != original_mode:
                os.fchmod(root_fd, original_mode)
                os.fsync(root_fd)
            outcome = "RESTORE_RETAINED_FOR_RETRY"

        if (stat.S_IMODE(os.fstat(root_fd).st_mode) != original_mode
                or os.path.lexists(quarantine_directory_path)):
            raise RuntimeError("recovery did not restore the original VM-volume mode and remove its empty quarantine")
        final_root = os.fstat(root_fd)
        final_root_identity = {"device": int(final_root.st_dev), "inode": int(final_root.st_ino),
                               "mode": stat.S_IMODE(final_root.st_mode)}
        _directory_access_evidence(
            VM_ROOT, root_fd, expected_uid=os.geteuid(), expected_mode=original_mode,
            expected_device=int(expected_root["device"]))
        result = {
            "schema_version": 1,
            "record_type": "R6_VM_RESTORE_CLEANUP_RECOVERY",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "created_at_unix": time.time(),
            "status": "PASS",
            "outcome": outcome,
            "cleanup_pending_path": str(pending_path.relative_to(REPO)),
            "cleanup_pending_sha256": pending_sha,
            "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
            "cleanup_authorization_sha256": authorization_sha,
            "quarantine_scan_path": str(scan_path.relative_to(REPO)),
            "quarantine_scan_sha256": scan_sha,
            "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
            "delete_authorization_sha256": delete_authorization_sha,
            "cleanup_recovery_intent_path": str(intent_path.relative_to(REPO)),
            "cleanup_recovery_intent_sha256": intent_sha,
            "cleanup_recovery_delete_ready_path": str(ready_path.relative_to(REPO)) if ready_sha else None,
            "cleanup_recovery_delete_ready_sha256": ready_sha,
            "base_identity_sha256": base_sha,
            "base_reviewer_sha256": reviewer_sha,
            "p2_admission_sha256": admission_sha,
            "restore_sha256": RESTORE_SHA256,
            "restore_file_identity": authorized_identity,
            "recovery_deleted_file_identity": recovery_deleted_identity,
            "restore_absent": not os.path.lexists(RESTORE_PATH),
            "quarantine_directory_absent": not os.path.lexists(quarantine_directory_path),
            "vm_volume_root_identity": expected_root,
            "vm_volume_root_mode_before_cleanup": original_mode,
            "vm_volume_root_after_recovery": final_root_identity,
            "cleanup_transaction_lock": pending.get("cleanup_transaction_lock"),
            "recovery_transaction_lock": cleanup_lock_evidence,
            "same_uid_trust_boundary": P2_CLEANUP_LOCK_SCOPE,
            "storage_state": intent["storage_state"],
        }
        _assert_p2_metadata_budget(
            metadata_paths[:-1], additional_allocated_bytes=(
                _template_allocations(base_dir)["metadata_bytes"]
                + P2_METADATA_FINALIZATION_RESERVE + 128 * 1024))
        signed_write(result_path, result, "vm_p2_restore_cleanup_recovery_hmac_sha256")
        print(json.dumps({"status": result["status"], "outcome": outcome,
                          "recovery_result_path": str(result_path),
                          "restore_absent": result["restore_absent"],
                          "quarantine_directory_absent": result["quarantine_directory_absent"]}, sort_keys=True))
    except BaseException as error:
        if intent_sha is not None and intent_path is not None and attempt_id is not None:
            try:
                failure_path = unique_path(
                    f"R6-VM-RESTORE-CLEANUP-RECOVERY-UNKNOWN-{attempt_id}", run_id)
                failure_record = {
                    "schema_version": 1,
                    "record_type": "R6_VM_RESTORE_CLEANUP_RECOVERY",
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "created_at_unix": time.time(),
                    "status": "UNKNOWN",
                    "outcome": "RECOVERY_INCOMPLETE",
                    "error_type": type(error).__name__,
                    "error": str(error)[:2000],
                    "cleanup_pending_path": str(pending_path.relative_to(REPO)),
                    "cleanup_pending_sha256": pending_sha,
                    "cleanup_authorization_sha256": authorization_sha,
                    "quarantine_scan_sha256": scan_sha,
                    "delete_authorization_sha256": delete_authorization_sha,
                    "cleanup_recovery_intent_path": str(intent_path.relative_to(REPO)),
                    "cleanup_recovery_intent_sha256": intent_sha,
                    "cleanup_recovery_delete_ready_path": (
                        str(ready_path.relative_to(REPO)) if ready_path is not None
                        and os.path.lexists(ready_path) else None),
                    "cleanup_recovery_delete_ready_sha256": ready_sha,
                    "observed_restore_at_original_path": os.path.lexists(RESTORE_PATH),
                    "observed_restore_in_quarantine": os.path.lexists(quarantine_path),
                    "observed_quarantine_directory": os.path.lexists(quarantine_directory_path),
                    "observed_vm_volume_root_mode": (
                        stat.S_IMODE(os.fstat(root_fd).st_mode) if root_fd >= 0 else None),
                    "cleanup_transaction_lock": pending.get("cleanup_transaction_lock"),
                    "recovery_transaction_lock": cleanup_lock_evidence,
                }
                failure_metadata_paths = list(metadata_paths or []) + [failure_path]
                _assert_p2_metadata_budget(
                    failure_metadata_paths,
                    additional_allocated_bytes=(
                        _template_allocations(base_dir)["metadata_bytes"]
                        + P2_METADATA_FINALIZATION_RESERVE + 128 * 1024
                        if base_dir is not None else P2_METADATA_FINALIZATION_RESERVE + 128 * 1024))
                signed_write(failure_path, failure_record,
                             "vm_p2_restore_cleanup_recovery_hmac_sha256")
            except BaseException as evidence_error:
                error = RuntimeError(f"recovery failed and its signed UNKNOWN disposition could not be written: {error}; {evidence_error}")
        raise
    finally:
        if quarantine_dir_fd is not None:
            os.close(quarantine_dir_fd)
        os.close(root_fd)


def reconcile_cleanup(args: argparse.Namespace) -> None:
    run_id = args.run_id
    if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", run_id) is None:
        raise RuntimeError("cleanup reconciliation run id contains unsupported path characters")
    base_identity_path = Path(args.base_identity).resolve(strict=True)
    base_reviewer_path = Path(args.base_reviewer).resolve(strict=True)
    pending_path = Path(args.pending).resolve(strict=True)
    base_identity, base_sha = read_signed(base_identity_path, "vm_macos_base_identity_hmac_sha256")
    reviewer, reviewer_sha = _read_stable_json_artifact(base_reviewer_path)
    pending, pending_sha = read_signed(pending_path, "vm_p2_restore_cleanup_pending_hmac_sha256")
    admission_path = (REPO / base_identity["p2_admission_path"]).resolve(strict=True)
    admission = vm._read_signed_record(admission_path, "vm_admission_hmac_sha256")
    admission_sha = vm._file_sha256(admission_path)
    p1_final, p1_final_sha = read_signed(P1_FINAL_PATH, "restore_verification_hmac_sha256")
    if (base_identity.get("status") != "PASS_MACOS_BASE_SEALED"
            or base_identity.get("run_id") != run_id):
        raise RuntimeError("cleanup reconciliation requires the sealed base identity for this run")
    if (base_identity.get("p2_admission_sha256") != admission_sha
            or admission.get("run_id") != run_id
            or admission.get("decision") != "PASS"
            or admission.get("authorized_phase") != "P2"
            or admission.get("session_id") != base_identity.get("p2_session_id")
            or p1_final_sha != P1_FINAL_SHA256
            or p1_final.get("status") != "PASS"
            or p1_final.get("local_sha256") != RESTORE_SHA256
            or p1_final.get("actual_length_bytes") != RESTORE_SIZE):
        raise RuntimeError("cleanup reconciliation admission is not the signed P2 session bound by the sealed base")
    _validate_base_review_binding(reviewer=reviewer, reviewer_sha=reviewer_sha,
                                 run_id=run_id, base_identity=base_identity,
                                 base_sha=base_sha, admission_sha=admission_sha)
    if (pending.get("status") != "PENDING_DELETION"
            or pending.get("run_id") != run_id
            or pending.get("base_identity_sha256") != base_sha
            or pending.get("base_reviewer_sha256") != reviewer_sha
            or pending.get("p2_admission_sha256") != admission_sha
            or not _valid_p2_cleanup_lock_evidence(pending.get("cleanup_transaction_lock"))
            or pending.get("restore_sha256") != RESTORE_SHA256
            or pending.get("restore_file_identity") != base_identity.get("restore_file_identity_at_install")):
        raise RuntimeError("read-only cleanup reconciliation is not bound to this run, pending authorization, and base")
    authorization_path = (REPO / pending["cleanup_authorization_path"]).resolve(strict=True)
    authorization, authorization_sha = read_signed(
        authorization_path, "vm_p2_restore_cleanup_authorization_hmac_sha256")
    if (authorization.get("decision") != "PASS"
            or authorization.get("run_id") != run_id
            or authorization.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
            or authorization.get("cleanup_pending_path") != str(pending_path.relative_to(REPO))
            or authorization.get("restore_path") != str(RESTORE_PATH)
            or authorization.get("restore_sha256") != RESTORE_SHA256
            or authorization.get("restore_file_identity") != pending.get("restore_file_identity")
            or authorization.get("p1_final_verification_sha256") != P1_FINAL_SHA256
            or authorization.get("base_identity_sha256") != base_sha
            or authorization.get("base_reviewer_sha256") != reviewer_sha
            or authorization.get("p2_admission_sha256") != admission_sha
            or authorization.get("session_id") != admission.get("session_id")
            or authorization.get("quarantine_directory_path") != pending.get("quarantine_directory_path")
            or authorization.get("quarantine_path") != pending.get("quarantine_path")
            or authorization.get("quarantine_scan_path") != pending.get("quarantine_scan_path")
            or authorization.get("delete_authorization_path") != pending.get("delete_authorization_path")
            or authorization.get("vm_volume_root_identity") != pending.get("vm_volume_root_identity")
            or pending.get("cleanup_authorization_sha256") != authorization_sha):
        raise RuntimeError("cleanup reconciliation lacks the exact signed authorization and reviewer binding")
    if os.path.lexists(RESTORE_PATH):
        raise RuntimeError("read-only cleanup reconciliation requires restore.ipsw to be absent")
    quarantine_path = Path(pending["quarantine_path"])
    quarantine_directory_path = Path(pending["quarantine_directory_path"])
    quarantine_scan_path = (REPO / pending["quarantine_scan_path"]).resolve(strict=True)
    quarantine_scan, quarantine_scan_sha = read_signed(
        quarantine_scan_path, "vm_p2_restore_quarantine_scan_hmac_sha256")
    delete_authorization_path = (REPO / pending["delete_authorization_path"]).resolve(strict=True)
    delete_authorization, delete_authorization_sha = read_signed(
        delete_authorization_path, "vm_p2_restore_delete_authorization_hmac_sha256")
    descriptor_evidence = quarantine_scan.get("post_quarantine_descriptor_scan")
    namespace_exclusion = quarantine_scan.get("namespace_exclusion")
    quarantine_directory_access = quarantine_scan.get("quarantine_directory_access_during_unlink")
    expected_protected_root_mode = (
        pending.get("vm_volume_root_mode_before_cleanup", 0) | stat.S_ISVTX)
    if (quarantine_scan.get("status") != "PASS"
            or quarantine_scan.get("run_id") != run_id
            or quarantine_scan.get("cleanup_pending_sha256") != pending_sha
            or quarantine_scan.get("cleanup_authorization_sha256") != authorization_sha
            or quarantine_scan.get("restore_file_identity") != pending.get("restore_file_identity")
            or quarantine_scan.get("quarantine_directory_path") != str(quarantine_directory_path)
            or quarantine_scan.get("quarantine_path") != str(quarantine_path)
            or quarantine_scan.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
            or not isinstance(descriptor_evidence, Mapping)
            or descriptor_evidence.get("scan_clear") is not True
            or descriptor_evidence.get("open_file_descriptors")
            or descriptor_evidence.get("descriptor_identity_mismatches")
            or descriptor_evidence.get("stderr_bytes") != 0
            or not isinstance(namespace_exclusion, Mapping)
            or namespace_exclusion.get(
                "parent_sticky_bit_prevents_other_owners_from_replacing_quarantine_directory") is not True
            or namespace_exclusion.get("quarantine_directory_owner_only_access") is not True
            or namespace_exclusion.get("quarantine_directory_acl_absent") is not True
            or not isinstance(quarantine_directory_access, Mapping)
            or quarantine_directory_access.get("path") != str(quarantine_directory_path)
            or quarantine_directory_access.get("mode") != 0o700
            or quarantine_directory_access.get("owner_uid") != os.geteuid()
            or quarantine_directory_access.get("acl_absent") is not True
            or quarantine_scan.get("vm_volume_root_identity") != pending.get("vm_volume_root_identity")
            or quarantine_scan.get("vm_volume_root_mode_before_cleanup")
            != pending.get("vm_volume_root_mode_before_cleanup")
            or quarantine_scan.get("vm_volume_root_mode_during_unlink") != expected_protected_root_mode
            or delete_authorization.get("decision") != "PASS"
            or delete_authorization.get("run_id") != run_id
            or delete_authorization.get("session_id") != admission.get("session_id")
            or delete_authorization.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
            or delete_authorization.get("authority")
            != "DELETE_ONLY_THE_EXACT_P1_RESTORE_IPSW_FROM_THE_REVIEWED_PRIVATE_QUARANTINE"
            or delete_authorization.get("cleanup_authorization_sha256") != authorization_sha
            or delete_authorization.get("cleanup_pending_sha256") != pending_sha
            or delete_authorization.get("restore_path") != str(RESTORE_PATH)
            or delete_authorization.get("restore_sha256") != RESTORE_SHA256
            or delete_authorization.get("quarantine_directory_path") != str(quarantine_directory_path)
            or delete_authorization.get("quarantine_path") != str(quarantine_path)
            or delete_authorization.get("vm_volume_root_identity") != pending.get("vm_volume_root_identity")
            or delete_authorization.get("vm_volume_root_mode_before_cleanup")
            != pending.get("vm_volume_root_mode_before_cleanup")
            or delete_authorization.get("quarantine_scan_path")
            != str(quarantine_scan_path.relative_to(REPO))
            or delete_authorization.get("quarantine_scan_sha256") != quarantine_scan_sha
            or delete_authorization.get("restore_file_identity") != pending.get("restore_file_identity")
            or delete_authorization.get("quarantined_file_identity")
            != quarantine_scan.get("quarantined_file_identity")
            or delete_authorization.get("base_identity_sha256") != base_sha
            or delete_authorization.get("base_reviewer_sha256") != reviewer_sha
            or delete_authorization.get("p2_admission_sha256") != admission_sha):
        raise RuntimeError("read-only cleanup reconciliation lacks the signed post-move scan and exact-inode delete authorization")
    if args.prior_cleanup_result:
        prior_path = Path(args.prior_cleanup_result).resolve(strict=True)
        prior, prior_sha = read_signed(prior_path, "vm_p2_restore_cleanup_hmac_sha256")
        if (prior.get("status") not in {"UNKNOWN", "FAIL"}
                or prior.get("run_id") != run_id
                or prior.get("cleanup_pending_sha256") != pending_sha
                or prior.get("cleanup_authorization_sha256") != authorization_sha
                or prior.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
                or prior.get("quarantine_scan_sha256") != quarantine_scan_sha
                or prior.get("delete_authorization_sha256") != delete_authorization_sha):
            raise RuntimeError("prior cleanup result is not an UNKNOWN/FAIL disposition for this run")
    else:
        prior_path = None
        prior_sha = None
    manifest_path = (REPO / base_identity["provisioner_source_manifest_path"]).resolve(strict=True)
    review_path = (REPO / base_identity["provisioner_review_path"]).resolve(strict=True)
    manifest, _helper_review = _verify_reviewed_provenance(manifest_path, review_path)
    if (vm._file_sha256(manifest_path) != base_identity.get("provisioner_source_manifest_sha256")
            or vm._file_sha256(review_path) != base_identity.get("provisioner_review_sha256")):
        raise RuntimeError("cleanup reconciliation helper provenance changed after base sealing")
    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_COMPLETE"
            or active.get("session_id") != admission.get("session_id")):
        raise RuntimeError("cleanup reconciliation is not at the same-session completed P2 boundary")
    base_dir = Path(base_identity["system_raw"]["path"]).parent
    storage_before = pending.get("storage_before_deletion")
    before_free = storage_before.get("conservative_shared_free_bytes") if isinstance(storage_before, Mapping) else None
    if (not isinstance(before_free, int) or isinstance(before_free, bool)
            or admission.get("run_id") != run_id or admission.get("session_id") != base_identity.get("p2_session_id")):
        raise RuntimeError("cleanup pending receipt has no valid signed P2 admission or free-space baseline")
    try:
        assessment = _cleanup_post_delete_assessment(
            base_identity=base_identity, base_dir=base_dir, before=None,
            before_free_override=before_free, quarantine_path=quarantine_path,
            quarantine_directory_path=quarantine_directory_path,
            expected_root_identity=pending.get("vm_volume_root_identity"),
            expected_root_mode=pending.get("vm_volume_root_mode_before_cleanup"))
        assessment_error = None
    except BaseException as error:
        assessment_error = str(error)
        assessment = {
            "after": None,
            "after_free": None,
            "before_free": before_free,
            "reclaimed": 0,
            "restore_absent": not os.path.lexists(RESTORE_PATH),
            "restore_files_remaining": ([str(quarantine_path)]
                                        if os.path.lexists(quarantine_path) else []),
            "quarantine_directory_absent": not os.path.lexists(quarantine_directory_path),
            "vm_volume_root_mode_restored": False,
            "vm_volume_root_access_after_cleanup": {},
            "vm_volume_root_after_cleanup": {},
            "blockers": [f"read-only post-delete reconciliation could not complete: {error}"],
            "base_artifacts_intact": False,
            "storage_after": {},
        }
    attempt_id = uuid.uuid4().hex[:12]
    result_path = unique_path(f"R6-VM-RESTORE-CLEANUP-RECONCILIATION-{attempt_id}", run_id)
    recovery_path = Path(args.recovery_result).resolve(strict=True) if args.recovery_result else None
    recovery_record: dict[str, Any] | None = None
    recovery_sha: str | None = None
    if recovery_path is not None:
        recovery_record, recovery_sha = read_signed(
            recovery_path, "vm_p2_restore_cleanup_recovery_hmac_sha256")
        recovery_root_after = recovery_record.get("vm_volume_root_after_recovery")
        if (recovery_record.get("run_id") != run_id
                or recovery_record.get("cleanup_pending_sha256") != pending_sha
                or recovery_record.get("cleanup_authorization_sha256") != authorization_sha
                or recovery_record.get("delete_authorization_sha256") != delete_authorization_sha
                or recovery_record.get("cleanup_transaction_lock") != pending.get("cleanup_transaction_lock")
                or not _valid_p2_cleanup_lock_evidence(recovery_record.get("recovery_transaction_lock"))
                or recovery_record.get("restore_absent") is not True
                or recovery_record.get("quarantine_directory_absent") is not True
                or not isinstance(recovery_root_after, Mapping)
                or recovery_root_after.get("mode") != pending.get("vm_volume_root_mode_before_cleanup")
                or recovery_record.get("outcome") != "RESTORE_DELETION_COMPLETED"):
            raise RuntimeError("cleanup recovery result does not bind a completed exact-run deletion")
    metadata_paths = _provisioner_metadata_paths(
        manifest, manifest_path, review_path, admission_path, base_identity_path,
        base_reviewer_path, authorization_path, pending_path, quarantine_scan_path,
        delete_authorization_path, *((recovery_path,) if recovery_path is not None else ()),
        *((prior_path,) if prior_path is not None else ()), result_path)
    try:
        metadata_inventory = _assert_p2_metadata_budget(
            metadata_paths[:-1], additional_allocated_bytes=(
                _template_allocations(base_dir)["metadata_bytes"] + P2_METADATA_FINALIZATION_RESERVE))
        metadata_error = None
    except BaseException as error:
        metadata_error = str(error)
        metadata_inventory = {"budget_check_failed": metadata_error}
        assessment["blockers"].append(f"read-only reconciliation metadata budget is unknown: {metadata_error}")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_CLEANUP",
        "reconciliation_only": True,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "created_at_unix": time.time(),
        "status": "PASS" if not assessment["blockers"] else "FAIL",
        "cleanup_pending_path": str(pending_path.relative_to(REPO)),
        "cleanup_pending_sha256": pending_sha,
        "cleanup_authorization_path": str(authorization_path.relative_to(REPO)),
        "cleanup_authorization_sha256": authorization_sha,
        "cleanup_transaction_lock": pending.get("cleanup_transaction_lock"),
        "reconciliation_lock": args.p2_cleanup_lock_evidence,
        "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
        "quarantine_scan_sha256": quarantine_scan_sha,
        "post_quarantine_descriptor_scan": descriptor_evidence,
        "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
        "delete_authorization_sha256": delete_authorization_sha,
        "quarantined_file_identity_after_atomic_move": delete_authorization.get(
            "quarantined_file_identity"),
        "vm_volume_root_identity": pending.get("vm_volume_root_identity"),
        "vm_volume_root_mode_before_cleanup": pending.get("vm_volume_root_mode_before_cleanup"),
        "quarantine_directory_path": str(quarantine_directory_path),
        "quarantine_path": str(quarantine_path),
        "quarantine_directory_absent": assessment["quarantine_directory_absent"],
        "vm_volume_root_mode_restored": assessment["vm_volume_root_mode_restored"],
        "vm_volume_root_access_after_cleanup": assessment["vm_volume_root_access_after_cleanup"],
        "vm_volume_root_after_cleanup": assessment["vm_volume_root_after_cleanup"],
        "prior_cleanup_result_path": str(prior_path.relative_to(REPO)) if prior_path else None,
        "prior_cleanup_result_sha256": prior_sha,
        "recovery_result_path": str(recovery_path.relative_to(REPO)) if recovery_path else None,
        "recovery_result_sha256": recovery_sha,
        "deleted_path": str(RESTORE_PATH),
        "deleted_file_sha256": RESTORE_SHA256,
        "deleted_file_identity": pending["restore_file_identity"],
        "restore_absent": assessment["restore_absent"],
        "restore_files_remaining": assessment["restore_files_remaining"],
        "RESTORE_CLEANUP_RECLAIMED_BYTES": assessment["reclaimed"],
        "measurement_method": "read-only reconciliation using signed pre-deletion APFS baseline and a fresh conservative APFS observation",
        "storage_before": storage_before,
        "storage_after": assessment["storage_after"],
        "base_identity_path": str(base_identity_path.relative_to(REPO)),
        "base_identity_sha256": base_sha,
        "p2_admission_path": str(admission_path.relative_to(REPO)),
        "p2_admission_sha256": admission_sha,
        "session_id": admission.get("session_id"),
        "base_reviewer_path": str(base_reviewer_path.relative_to(REPO)),
        "base_reviewer_sha256": reviewer_sha,
        "base_artifacts_intact_by_file_identity_and_hash": assessment["base_artifacts_intact"],
        "post_delete_metadata_inventory": metadata_inventory,
        "reconciliation_error": assessment_error,
        "metadata_budget_error": metadata_error,
        "blockers": assessment["blockers"],
    }
    signed_write(result_path, record, "vm_p2_restore_cleanup_hmac_sha256")
    print(json.dumps({"status": record["status"], "cleanup_path": str(result_path),
                      "RESTORE_CLEANUP_RECLAIMED_BYTES": assessment["reclaimed"],
                      "restore_absent": assessment["restore_absent"],
                      "blockers": assessment["blockers"]}, sort_keys=True))
    if assessment["blockers"]:
        raise RuntimeError("read-only cleanup reconciliation produced a signed non-PASS result: "
                           + "; ".join(assessment["blockers"][:8]))


def _restore_tree_blockers_after_cleanup(*, expected_base_dir: Path,
        allowed_large_files: tuple[Path, ...] = ()) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    restore_files: list[str] = []
    allowed = {path.resolve(strict=False) for path in allowed_large_files}
    ignored = {".DocumentRevisions-V100", ".Spotlight-V100", ".fseventsd",
               ".Trashes", ".TemporaryItems", ".vol", ".apdisk"}
    for base, directories, files in os.walk(VM_ROOT, topdown=True, followlinks=False):
        if Path(base) == VM_ROOT:
            directories[:] = [name for name in directories if name not in ignored]
        current_dir = Path(base)
        if current_dir == VM_ROOT:
            expected_directories = {"templates"}
        elif current_dir == VM_ROOT / "templates":
            expected_directories = {expected_base_dir.name}
        elif current_dir == expected_base_dir:
            expected_directories = set()
        else:
            expected_directories = set()
        if set(directories) != expected_directories:
            blockers.append(f"unexpected post-cleanup VM-volume directories under {current_dir}: {directories}")
        for name in list(directories):
            directory_path = current_dir / name
            try:
                directory_info = os.stat(directory_path, follow_symlinks=False)
            except OSError as error:
                blockers.append(f"post-cleanup directory inventory failed for {directory_path}: {error}")
                directories.remove(name)
                continue
            if (not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_dev != os.stat(VM_ROOT).st_dev):
                blockers.append(f"unsafe post-cleanup directory entry in VM volume: {directory_path}")
                directories.remove(name)
        for name in files:
            path = Path(base) / name
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as error:
                blockers.append(f"post-cleanup VM-volume inventory failed for {path}: {error}")
                continue
            expected_files = ({"system.raw", "auxiliary.bin", "machine-identifier.bin", "hardware-model.bin"}
                              if Path(base) == expected_base_dir else set())
            if (Path(base) == VM_ROOT and name not in expected_files) or (
                    Path(base) == expected_base_dir and name not in expected_files):
                blockers.append(f"unexpected post-cleanup VM-volume file: {path}")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                blockers.append(f"non-regular or linked file remains in the VM volume: {path}")
            if name.lower().endswith(".ipsw"):
                restore_files.append(str(path))
            if "restore" in name.lower() and any(x in name.lower() for x in ("partial", ".part", ".tmp", ".download")):
                blockers.append(f"partial restore remains: {path}")
            if (stat.S_ISREG(info.st_mode) and info.st_size >= RESTORE_SIZE - MIB
                    and path.resolve(strict=False) not in allowed):
                blockers.append(f"unexpected restore-sized payload remains in the controlled VM volume: {path}")
    return blockers, restore_files


def finalize(args: argparse.Namespace) -> None:
    run_id = args.run_id
    cleanup_path = Path(args.cleanup_result).resolve(strict=True)
    cleanup, cleanup_sha = read_signed(cleanup_path, "vm_p2_restore_cleanup_hmac_sha256")
    base_review_path = Path(args.base_reviewer).resolve(strict=True)
    final_review_path = Path(args.final_reviewer).resolve(strict=True)
    base_review, base_review_sha = _read_stable_json_artifact(base_review_path)
    final_review, final_review_sha = _read_stable_json_artifact(final_review_path)
    if final_review.get("decision") not in {
            "PASS — MACOS VM BASE PROVISIONING COMPLETE",
            "PASS WITH NON-BLOCKING CAVEATS — MACOS VM BASE PROVISIONING COMPLETE"}:
        raise RuntimeError("final independent P2 reviewer did not return PASS")
    if (final_review.get("run_id") != run_id
            or final_review.get("cleanup_result_sha256") != cleanup_sha
            or final_review.get("restore_absent") is not True
            or final_review.get("quarantine_directory_absent") is not True
            or final_review.get("vm_volume_root_mode_restored") is not True
            or final_review.get("base_artifacts_intact") is not True
            or final_review.get("runtime_or_r6_activity") is not False):
        raise RuntimeError("final independent review does not bind this cleanup result and run")
    if (cleanup.get("status") != "PASS" or cleanup.get("restore_absent") is not True
            or cleanup.get("run_id") != run_id
            or cleanup.get("RESTORE_CLEANUP_RECLAIMED_BYTES", 0) <= 0
            or cleanup.get("restore_files_remaining")
            or cleanup.get("quarantine_directory_absent") is not True
            or cleanup.get("vm_volume_root_mode_restored") is not True
            or cleanup.get("base_reviewer_sha256") != base_review_sha
            or base_review.get("decision") not in {
                "PASS — MACOS BASE SEALED", "PASS WITH NON-BLOCKING CAVEATS — MACOS BASE SEALED"}):
        raise RuntimeError("final P2 chain has a failed cleanup or base review")
    base_identity_path = REPO / cleanup["base_identity_path"]
    base_identity = vm._read_signed_record(
        base_identity_path, "vm_macos_base_identity_hmac_sha256")
    if (base_identity.get("run_id") != run_id
            or base_identity.get("p2_admission_sha256") != cleanup.get("p2_admission_sha256")
            or cleanup.get("base_identity_sha256") != vm._file_sha256(base_identity_path)):
        raise RuntimeError("final P2 cleanup does not bind the exact signed base identity and admission")
    cleanup_storage = cleanup.get("storage_after", {})
    if (cleanup.get("base_artifacts_intact_by_file_identity_and_hash") is not True
            or cleanup_storage.get("safety_volume_uuid") != SAFETY_VOLUME_UUID
            or cleanup_storage.get("safety_reserve_bytes") != 15 * GIB
            or cleanup_storage.get("safety_volume_capacity_in_use_bytes") != 16_106_127_360
            or cleanup_storage.get("safety_mount_state") != "UNMOUNTED_VERIFIED"
            or cleanup_storage.get("vm_volume_quota_bytes") != 53_687_091_200
            or cleanup_storage.get("conservative_shared_free_bytes", 0) <= vm.VM_OPERATIONAL_WARNING_FREE
            or cleanup_storage.get("vm_volume_capacity_in_use_bytes", 53_687_091_201)
            > cleanup_storage.get("vm_volume_quota_bytes", 0)):
        raise RuntimeError("post-cleanup capacity, safety reserve, or sealed base identity did not pass")
    if (base_review.get("run_id") != run_id
            or base_review.get("base_identity_sha256") != cleanup.get("base_identity_sha256")
            or base_review.get("independently_computed_system_raw_sha256")
            != base_identity.get("system_raw", {}).get("sha256")
            or base_review.get("independently_computed_auxiliary_storage_sha256")
            != base_identity.get("auxiliary_storage", {}).get("sha256")
            or base_review.get("p2_admission_sha256") != base_identity.get("p2_admission_sha256")):
        raise RuntimeError("independent base review does not bind its own complete system/auxiliary hashes and P2 admission")
    cleanup_pending_path = REPO / cleanup["cleanup_pending_path"]
    cleanup_pending, cleanup_pending_sha = read_signed(
        cleanup_pending_path, "vm_p2_restore_cleanup_pending_hmac_sha256")
    if (cleanup_pending_sha != cleanup.get("cleanup_pending_sha256")
            or cleanup_pending.get("status") != "PENDING_DELETION"
            or cleanup_pending.get("run_id") != run_id
            or cleanup_pending.get("base_identity_sha256") != cleanup.get("base_identity_sha256")
            or cleanup_pending.get("base_reviewer_sha256") != base_review_sha
            or cleanup_pending.get("p2_admission_sha256") != cleanup.get("p2_admission_sha256")
            or cleanup_pending.get("cleanup_authorization_path") != cleanup.get("cleanup_authorization_path")
            or cleanup_pending.get("cleanup_authorization_sha256")
            != cleanup.get("cleanup_authorization_sha256")
            or cleanup_pending.get("cleanup_transaction_lock") != cleanup.get("cleanup_transaction_lock")
            or cleanup_pending.get("quarantine_scan_path") != cleanup.get("quarantine_scan_path")
            or cleanup_pending.get("delete_authorization_path") != cleanup.get("delete_authorization_path")
            or cleanup_pending.get("quarantine_directory_path") != cleanup.get("quarantine_directory_path")
            or cleanup_pending.get("quarantine_path") != cleanup.get("quarantine_path")
            or cleanup_pending.get("vm_volume_root_identity") != cleanup.get("vm_volume_root_identity")
            or cleanup_pending.get("vm_volume_root_mode_before_cleanup")
            != cleanup.get("vm_volume_root_mode_before_cleanup")
            or cleanup_pending.get("restore_file_identity") != cleanup.get("deleted_file_identity")
            or cleanup_pending.get("restore_sha256") != RESTORE_SHA256):
        raise RuntimeError("final P2 cleanup does not bind its signed pre-deletion pending state")
    if (cleanup.get("vm_volume_root_identity") != cleanup_pending.get("vm_volume_root_identity")
            or cleanup.get("vm_volume_root_mode_before_cleanup")
            != cleanup_pending.get("vm_volume_root_mode_before_cleanup")):
        raise RuntimeError("final cleanup result omits or changes the signed VM-volume root identity and original mode")
    if (not _valid_p2_cleanup_lock_evidence(cleanup_pending.get("cleanup_transaction_lock"))
            or not _valid_p2_cleanup_lock_evidence(cleanup.get("cleanup_transaction_lock"))
            or cleanup.get("cleanup_transaction_lock") != cleanup_pending.get("cleanup_transaction_lock")
            or (cleanup.get("reconciliation_only") is True
                and not _valid_p2_cleanup_lock_evidence(cleanup.get("reconciliation_lock")))):
        raise RuntimeError("final P2 cleanup result lacks the enforced cooperative transaction-lock evidence")
    authorization_path = REPO / cleanup["cleanup_authorization_path"]
    authorization = vm._read_signed_record(
        authorization_path, "vm_p2_restore_cleanup_authorization_hmac_sha256")
    if (vm._file_sha256(authorization_path) != cleanup.get("cleanup_authorization_sha256")
            or authorization.get("decision") != "PASS"
            or authorization.get("session_id") != cleanup.get("session_id")
            or authorization.get("run_id") != run_id
            or authorization.get("restore_path") != str(RESTORE_PATH)
            or authorization.get("restore_sha256") != RESTORE_SHA256
            or authorization.get("restore_file_identity") != cleanup.get("deleted_file_identity")
            or authorization.get("p1_final_verification_sha256") != P1_FINAL_SHA256
            or authorization.get("base_identity_sha256") != cleanup.get("base_identity_sha256")
            or authorization.get("base_identity_path") != cleanup.get("base_identity_path")
            or authorization.get("p2_admission_sha256") != cleanup.get("p2_admission_sha256")
            or authorization.get("cleanup_pending_path") != cleanup.get("cleanup_pending_path")
            or authorization.get("quarantine_scan_path") != cleanup.get("quarantine_scan_path")
            or authorization.get("delete_authorization_path") != cleanup.get("delete_authorization_path")
            or authorization.get("quarantine_directory_path") != cleanup.get("quarantine_directory_path")
            or authorization.get("quarantine_path") != cleanup.get("quarantine_path")
            or authorization.get("vm_volume_root_identity") != cleanup.get("vm_volume_root_identity")
            or authorization.get("vm_volume_root_mode_before_cleanup")
            != cleanup.get("vm_volume_root_mode_before_cleanup")
            or authorization.get("cleanup_transaction_lock") != cleanup_pending.get("cleanup_transaction_lock")
            or authorization.get("base_reviewer_sha256") != base_review_sha):
        raise RuntimeError("final P2 cleanup does not bind its exact signed authorization and independent base review")
    quarantine_scan_path = REPO / cleanup["quarantine_scan_path"]
    quarantine_scan, quarantine_scan_sha = read_signed(
        quarantine_scan_path, "vm_p2_restore_quarantine_scan_hmac_sha256")
    delete_authorization_path = REPO / cleanup["delete_authorization_path"]
    delete_authorization, delete_authorization_sha = read_signed(
        delete_authorization_path, "vm_p2_restore_delete_authorization_hmac_sha256")
    final_quarantine_scan_evidence = quarantine_scan.get("post_quarantine_descriptor_scan")
    final_namespace_exclusion = quarantine_scan.get("namespace_exclusion")
    if (quarantine_scan_sha != cleanup.get("quarantine_scan_sha256")
            or quarantine_scan.get("status") != "PASS"
            or quarantine_scan.get("run_id") != run_id
            or quarantine_scan.get("cleanup_pending_sha256") != cleanup_pending_sha
            or quarantine_scan.get("cleanup_authorization_sha256")
            != cleanup.get("cleanup_authorization_sha256")
            or quarantine_scan.get("post_quarantine_descriptor_scan")
            != cleanup.get("post_quarantine_descriptor_scan")
            or not isinstance(final_quarantine_scan_evidence, Mapping)
            or final_quarantine_scan_evidence.get("scan_clear") is not True
            or final_quarantine_scan_evidence.get("open_file_descriptors")
            or final_quarantine_scan_evidence.get("descriptor_identity_mismatches")
            or final_quarantine_scan_evidence.get("stderr_bytes") != 0
            or not isinstance(final_namespace_exclusion, Mapping)
            or final_namespace_exclusion.get(
                "parent_sticky_bit_prevents_other_owners_from_replacing_quarantine_directory") is not True
            or final_namespace_exclusion.get("quarantine_directory_owner_only_access") is not True
            or final_namespace_exclusion.get("quarantine_directory_acl_absent") is not True
            or quarantine_scan.get("restore_file_identity") != cleanup.get("deleted_file_identity")
            or quarantine_scan.get("vm_volume_root_identity") != cleanup.get("vm_volume_root_identity")
            or quarantine_scan.get("vm_volume_root_mode_before_cleanup")
            != cleanup.get("vm_volume_root_mode_before_cleanup")
            or quarantine_scan.get("quarantine_directory_path") != cleanup.get("quarantine_directory_path")
            or quarantine_scan.get("quarantine_path") != cleanup.get("quarantine_path")
            or quarantine_scan.get("cleanup_transaction_lock") != cleanup_pending.get("cleanup_transaction_lock")
            or delete_authorization_sha != cleanup.get("delete_authorization_sha256")
            or delete_authorization.get("decision") != "PASS"
            or delete_authorization.get("run_id") != run_id
            or delete_authorization.get("authority")
            != "DELETE_ONLY_THE_EXACT_P1_RESTORE_IPSW_FROM_THE_REVIEWED_PRIVATE_QUARANTINE"
            or delete_authorization.get("session_id") != cleanup.get("session_id")
            or delete_authorization.get("cleanup_transaction_lock") != cleanup_pending.get("cleanup_transaction_lock")
            or delete_authorization.get("cleanup_pending_sha256") != cleanup_pending_sha
            or delete_authorization.get("cleanup_authorization_sha256")
            != cleanup.get("cleanup_authorization_sha256")
            or delete_authorization.get("quarantine_scan_sha256") != quarantine_scan_sha
            or delete_authorization.get("quarantined_file_identity")
            != cleanup.get("quarantined_file_identity_after_atomic_move")
            or cleanup.get("quarantined_file_identity_after_atomic_move")
            != quarantine_scan.get("quarantined_file_identity")
            or delete_authorization.get("quarantine_directory_path")
            != cleanup.get("quarantine_directory_path")
            or delete_authorization.get("quarantine_path") != cleanup.get("quarantine_path")
            or delete_authorization.get("vm_volume_root_identity")
            != cleanup.get("vm_volume_root_identity")
            or delete_authorization.get("vm_volume_root_mode_before_cleanup")
            != cleanup.get("vm_volume_root_mode_before_cleanup")
            or delete_authorization.get("base_identity_sha256") != cleanup.get("base_identity_sha256")
            or delete_authorization.get("base_reviewer_sha256") != base_review_sha
            or delete_authorization.get("p2_admission_sha256") != cleanup.get("p2_admission_sha256")):
        raise RuntimeError("final P2 cleanup lacks the exact signed post-move descriptor scan and deletion authorization")
    recovery_path = None
    recovery_sha = None
    if cleanup.get("recovery_result_path") is not None:
        recovery_path = REPO / cleanup["recovery_result_path"]
        recovery_record, recovery_sha = read_signed(
            recovery_path, "vm_p2_restore_cleanup_recovery_hmac_sha256")
        if (recovery_sha != cleanup.get("recovery_result_sha256")
                or recovery_record.get("status") != "PASS"
                or recovery_record.get("outcome") != "RESTORE_DELETION_COMPLETED"
                or recovery_record.get("run_id") != run_id
                or recovery_record.get("cleanup_pending_sha256") != cleanup_pending_sha
                or recovery_record.get("cleanup_authorization_sha256")
                != cleanup.get("cleanup_authorization_sha256")
                or recovery_record.get("delete_authorization_sha256") != delete_authorization_sha
                or recovery_record.get("cleanup_transaction_lock") != cleanup_pending.get("cleanup_transaction_lock")
                or not _valid_p2_cleanup_lock_evidence(recovery_record.get("recovery_transaction_lock"))):
            raise RuntimeError("final cleanup recovery result does not bind the completed signed deletion transaction")
        recovery_root_after = recovery_record.get("vm_volume_root_after_recovery")
        recovery_deleted_identity = recovery_record.get("recovery_deleted_file_identity")
        delete_identity = delete_authorization.get("quarantined_file_identity")
        recovery_stable_keys = ("device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns")
        if (recovery_record.get("restore_absent") is not True
                or recovery_record.get("quarantine_directory_absent") is not True
                or not isinstance(recovery_root_after, Mapping)
                or recovery_root_after.get("device") != cleanup_pending.get("vm_volume_root_identity", {}).get("device")
                or recovery_root_after.get("inode") != cleanup_pending.get("vm_volume_root_identity", {}).get("inode")
                or recovery_root_after.get("mode") != cleanup_pending.get("vm_volume_root_mode_before_cleanup")
                or not isinstance(recovery_deleted_identity, Mapping)
                or not isinstance(delete_identity, Mapping)
                or any(recovery_deleted_identity.get(key) != delete_identity.get(key)
                       for key in recovery_stable_keys)):
            raise RuntimeError("final cleanup recovery result does not prove exact restore deletion and root restoration")
        recovery_intent_path = REPO / recovery_record["cleanup_recovery_intent_path"]
        recovery_intent, recovery_intent_sha = read_signed(
            recovery_intent_path, "vm_p2_restore_cleanup_recovery_intent_hmac_sha256")
        if (recovery_intent_sha != recovery_record.get("cleanup_recovery_intent_sha256")
                or recovery_intent.get("record_type") != "R6_VM_RESTORE_CLEANUP_RECOVERY_INTENT"
                or recovery_intent.get("decision") != "PASS"
                or recovery_intent.get("action") != "COMPLETE_AUTHORIZED_DELETION"
                or recovery_intent.get("run_id") != run_id
                or recovery_intent.get("cleanup_pending_sha256") != cleanup_pending_sha
                or recovery_intent.get("delete_authorization_sha256") != delete_authorization_sha
                or recovery_intent.get("recovery_transaction_lock")
                != recovery_record.get("recovery_transaction_lock")):
            raise RuntimeError("final cleanup recovery intent does not bind the authorized deletion transition")
        recovery_ready_path_value = recovery_record.get("cleanup_recovery_delete_ready_path")
        if recovery_ready_path_value is not None:
            recovery_ready_path = REPO / recovery_ready_path_value
            recovery_ready, recovery_ready_sha = read_signed(
                recovery_ready_path, "vm_p2_restore_cleanup_recovery_ready_hmac_sha256")
            ready_scan = recovery_ready.get("post_recovery_descriptor_scan")
            if (recovery_ready_sha != recovery_record.get("cleanup_recovery_delete_ready_sha256")
                    or recovery_ready.get("decision") != "PASS"
                    or recovery_ready.get("cleanup_recovery_intent_sha256") != recovery_intent_sha
                    or recovery_ready.get("cleanup_pending_sha256") != cleanup_pending_sha
                    or recovery_ready.get("delete_authorization_sha256") != delete_authorization_sha
                    or recovery_ready.get("restore_sha256") != RESTORE_SHA256
                    or recovery_ready.get("restore_file_identity") != recovery_deleted_identity
                    or recovery_ready.get("quarantine_directory_path")
                    != cleanup_pending.get("quarantine_directory_path")
                    or recovery_ready.get("vm_volume_root_identity")
                    != cleanup_pending.get("vm_volume_root_identity")
                    or recovery_ready.get("vm_volume_root_mode_during_unlink")
                    != (cleanup_pending.get("vm_volume_root_mode_before_cleanup") | stat.S_ISVTX)
                    or not isinstance(ready_scan, Mapping)
                    or ready_scan.get("scan_clear") is not True
                    or ready_scan.get("open_file_descriptors")
                    or ready_scan.get("descriptor_identity_mismatches")
                    or ready_scan.get("stderr_bytes") != 0
                    or recovery_ready.get("recovery_transaction_lock")
                    != recovery_record.get("recovery_transaction_lock")):
                raise RuntimeError("final cleanup recovery lacks the exact signed post-recovery deletion readiness proof")
        elif recovery_record.get("cleanup_recovery_delete_ready_sha256") is not None:
            raise RuntimeError("recovery readiness hash is present without its exact receipt path")
    elif cleanup.get("recovery_result_sha256") is not None:
        raise RuntimeError("final cleanup has a recovery-result hash without its exact recovery-result path")
    if (final_review.get("base_reviewer_sha256") != base_review_sha
            or final_review.get("base_identity_sha256") != cleanup.get("base_identity_sha256")
            or final_review.get("p2_admission_sha256") != cleanup.get("p2_admission_sha256")
            or final_review.get("provisioner_manifest_sha256")
            != base_identity.get("provisioner_source_manifest_sha256")
            or final_review.get("provisioner_binary_sha256")
            != base_identity.get("provisioner_binary_sha256")
            or final_review.get("cleanup_authorization_sha256")
            != cleanup.get("cleanup_authorization_sha256")
            or final_review.get("cleanup_pending_sha256") != cleanup_pending_sha
            or final_review.get("quarantine_scan_sha256") != quarantine_scan_sha
            or final_review.get("delete_authorization_sha256") != delete_authorization_sha
            or (recovery_sha is not None
                and final_review.get("recovery_result_sha256") != recovery_sha)):
        raise RuntimeError("final independent review does not bind the sealed base, helper, and signed cleanup chain")
    active = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_COMPLETE"
            or active.get("session_id") != authorization.get("session_id")):
        raise RuntimeError("final P2 state is not bound to the completed same-session active marker")
    requested_state = {
        "P1_RESTORE_ACQUISITION": "ACCEPTED",
        "VM_GUEST_BASE_PROVISIONED": "YES",
        "VM_GUEST_BASE_SEALED": "YES",
        "RESTORE_LOCAL_PAYLOAD_RETAINED": "NO",
        "VM_ARCHITECTURE_READY": "NO — SERIAL RUNTIME PROOF REQUIRED",
        "VM_IMPLEMENTATION_READY": "NO",
        "VM_RUNTIME_ACCEPTED": "NO",
        "R6-A": "NOT ACCEPTED",
        "next_milestone": "R6 VM Runtime Feasibility Gate",
    }
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P2_FINAL_STATE",
        "run_id": run_id,
        "created_at_unix": time.time(),
        "status": final_review["decision"],
        "state": requested_state,
        "cleanup_result_path": str(cleanup_path.relative_to(REPO)),
        "cleanup_result_sha256": cleanup_sha,
        "cleanup_transaction_lock": cleanup.get("cleanup_transaction_lock"),
        "reconciliation_lock": cleanup.get("reconciliation_lock"),
        "cleanup_pending_path": str(cleanup_pending_path.relative_to(REPO)),
        "cleanup_pending_sha256": cleanup_pending_sha,
        "quarantine_scan_path": str(quarantine_scan_path.relative_to(REPO)),
        "quarantine_scan_sha256": quarantine_scan_sha,
        "delete_authorization_path": str(delete_authorization_path.relative_to(REPO)),
        "delete_authorization_sha256": delete_authorization_sha,
        "recovery_result_path": str(recovery_path.relative_to(REPO)) if recovery_path else None,
        "recovery_result_sha256": recovery_sha,
        "base_reviewer_path": str(base_review_path.relative_to(REPO)),
        "base_reviewer_sha256": base_review_sha,
        "final_reviewer_path": str(final_review_path.relative_to(REPO)),
        "final_reviewer_sha256": final_review_sha,
        "RESTORE_CLEANUP_RECLAIMED_BYTES": cleanup["RESTORE_CLEANUP_RECLAIMED_BYTES"],
        "stop_boundary": "STOP AFTER P2; no runtime feasibility, serial proof, R6 execution, or BuildLease",
    }
    path = unique_path("R6-VM-P2-FINAL-STATE", run_id)
    base_dir = Path(base_identity["system_raw"]["path"]).parent
    manifest_path = REPO / base_identity["provisioner_source_manifest_path"]
    review_path = REPO / base_identity["provisioner_review_path"]
    manifest, _helper_review = _verify_reviewed_provenance(manifest_path, review_path)
    metadata_paths = _provisioner_metadata_paths(
        manifest, manifest_path, review_path, cleanup_path, cleanup_pending_path,
        authorization_path, quarantine_scan_path, delete_authorization_path,
        base_review_path, final_review_path,
        *((recovery_path,) if recovery_path is not None else ()))
    metadata = _assert_p2_metadata_budget(
        metadata_paths,
        additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    if metadata["remaining_bytes"] < 64 * 1024:
        raise RuntimeError("P2 metadata envelope lacks reserved room for the signed final state")
    signed_write(path, record, "vm_p2_final_state_hmac_sha256")
    metadata = _assert_p2_metadata_budget(
        metadata_paths + [path], additional_allocated_bytes=_template_allocations(base_dir)["metadata_bytes"])
    print(json.dumps({"status": final_review["decision"], "final_state_path": str(path),
                      "final_state_sha256": vm._file_sha256(path),
                      "RESTORE_CLEANUP_RECLAIMED_BYTES": cleanup["RESTORE_CLEANUP_RECLAIMED_BYTES"],
                      "p1_p2_metadata_allocated_bytes": metadata["total_p2_metadata_allocated_bytes"]}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prov = subparsers.add_parser("record-provenance")
    prov.add_argument("--run-id", required=True)
    prov.add_argument("--build-directory", required=True)
    prov.set_defaults(fn=record_provenance)
    admit = subparsers.add_parser("admit")
    admit.add_argument("--run-id", required=True)
    admit.add_argument("--manifest", required=True)
    admit.add_argument("--review", required=True)
    admit.add_argument("--binary", required=False,
                       help="Legacy plan helper; retained for caller compatibility and not invoked by R3 admission")
    admit.add_argument("--identity-plan", default=str(P2_ACCEPTED_IDENTITY_PLAN_PATH),
                       help="Exact accepted P2 identity-plan receipt to reuse")
    admit.add_argument("--r3-supersession-receipt", required=True,
                       help="Signed receipt for the exact R3 packet retired before this R4 preflight")
    admit.add_argument("--admit-only", action="store_true",
                       help="Create, sign, and verify P2 admission without starting provisioning execution")
    admit.set_defaults(fn=create_admission)
    execute = subparsers.add_parser("provision")
    execute.add_argument("--admission", required=True,
                         help="Exact signed R3 P2 admission to validate and atomically claim")
    execute.add_argument("--validate-only", action="store_true",
                         help="Validate the non-consuming execution entry without claiming the launch capability")
    execute.set_defaults(fn=_resume_existing_admission)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--run-id", required=True)
    seal.add_argument("--admission", required=True)
    seal.add_argument("--execution-result", required=True)
    seal.add_argument("--install-result", required=True)
    seal.add_argument("--monitor-result", required=True)
    seal.set_defaults(fn=seal_base)
    delete = subparsers.add_parser("cleanup")
    delete.add_argument("--run-id", required=True)
    delete.add_argument("--base-identity", required=True)
    delete.add_argument("--base-reviewer", required=True)
    delete.set_defaults(fn=cleanup)
    recover = subparsers.add_parser("recover-cleanup")
    recover.add_argument("--run-id", required=True)
    recover.add_argument("--base-identity", required=True)
    recover.add_argument("--base-reviewer", required=True)
    recover.add_argument("--pending", required=True)
    recover.set_defaults(fn=recover_cleanup)
    reconcile = subparsers.add_parser("reconcile-cleanup")
    reconcile.add_argument("--run-id", required=True)
    reconcile.add_argument("--base-identity", required=True)
    reconcile.add_argument("--base-reviewer", required=True)
    reconcile.add_argument("--pending", required=True)
    reconcile.add_argument("--prior-cleanup-result")
    reconcile.add_argument("--recovery-result")
    reconcile.set_defaults(fn=reconcile_cleanup)
    final = subparsers.add_parser("finalize")
    final.add_argument("--run-id", required=True)
    final.add_argument("--cleanup-result", required=True)
    final.add_argument("--base-reviewer", required=True)
    final.add_argument("--final-reviewer", required=True)
    final.set_defaults(fn=finalize)
    args = parser.parse_args()
    if args.command in {"cleanup", "recover-cleanup", "reconcile-cleanup", "finalize"}:
        lock_descriptor, args.p2_cleanup_lock_evidence = _acquire_p2_cleanup_lock()
        try:
            args.fn(args)
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
