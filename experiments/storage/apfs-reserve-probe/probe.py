#!/usr/bin/env python3
"""Bounded disposable APFS reserve/quota behavior probe for R4.

The only disk image created here is exactly 3000 MiB (3,145,728,000 bytes),
including partition layout overhead, and is below the 3,200,000,000-byte
logical-image cap. The script verifies the image's declared virtual size before
attaching it, refuses to touch a non-image APFS container, limits all test-file
writes to that image, records readbacks before cleanup, and removes it in
``finally``.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import Any


GIB = 1024 ** 3
MIB = 1024 ** 2
IMAGE_MAX_BYTES = 3_200_000_000
IMAGE_REQUESTED_SIZE_BYTES = 3000 * MIB
SAFETY_RESERVE_BYTES = 512 * MIB
VM_RESERVE_BYTES = 64 * MIB
VM_QUOTA_BYTES = 512 * MIB
QUOTA_WRITE_ATTEMPT_BYTES = 600 * MIB
WRITER_WRITE_ATTEMPT_BYTES = 2_800_000_000
CHUNK_BYTES = 16 * MIB

ROOT = Path(__file__).resolve().parent
IMAGE = Path("/private/tmp/fgmetal-apfs-reserve-r4-20261017.sparsebundle")
EVIDENCE = ROOT / "probe-20261017.json"
LOG: list[dict[str, Any]] = []
SNAPSHOTS: list[dict[str, Any]] = []
ATTACHED_DISK: str | None = None
CREATED_VOLUME_UUIDS: set[str] = set()
RESULTS: dict[str, Any] = {}


def command(args: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    started = time.time()
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    LOG.append({"argv": args, "returncode": result.returncode,
                "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:],
                "duration_seconds": round(time.time() - started, 3)})
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args}: {result.stderr[-1000:]}")
    return result


def plist_command(args: list[str]) -> dict[str, Any]:
    return plistlib.loads(command(args).stdout.encode())


def _integer_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def image_virtual_size_bytes() -> int:
    info = plist_command(["/usr/bin/hdiutil", "imageinfo", "-plist", str(IMAGE)])
    size_info = info.get("Size Information")
    value = size_info.get("Total Bytes") if isinstance(size_info, dict) else None
    if not _integer_value(value):
        raise RuntimeError("hdiutil imageinfo did not provide Size Information / Total Bytes")
    return value


def image_container() -> tuple[dict[str, Any], str]:
    if ATTACHED_DISK is None:
        raise RuntimeError("probe image is not attached")
    root = plist_command(["/usr/sbin/diskutil", "apfs", "list", "-plist"])
    containers = root.get("Containers")
    if not isinstance(containers, list):
        raise RuntimeError("diskutil APFS plist has no Containers array")
    matches = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        stores = container.get("PhysicalStores")
        if isinstance(stores, list) and any(
                isinstance(row, dict)
                and (row.get("DeviceIdentifier") == ATTACHED_DISK
                     or row.get("DeviceIdentifier", "").startswith(ATTACHED_DISK + "s"))
                for row in stores):
            matches.append(container)
    if len(matches) != 1:
        raise RuntimeError(f"expected one APFS container for image {ATTACHED_DISK}; got {len(matches)}")
    container = matches[0]
    reference = container.get("ContainerReference")
    if not isinstance(reference, str):
        raise RuntimeError("test image container has no reference identifier")
    return container, reference


def volume(container: dict[str, Any], name: str) -> dict[str, Any]:
    rows = [row for row in container.get("Volumes", [])
            if isinstance(row, dict) and row.get("Name") == name]
    if len(rows) != 1:
        raise RuntimeError(f"expected one {name} volume; got {len(rows)}")
    row = rows[0]
    volume_uuid = row.get("APFSVolumeUUID")
    if isinstance(volume_uuid, str):
        CREATED_VOLUME_UUIDS.add(volume_uuid)
    return row


def mount_info(row: dict[str, Any]) -> tuple[str, str]:
    device = row.get("DeviceIdentifier")
    if not isinstance(device, str):
        raise RuntimeError("volume has no device identifier")
    command(["/usr/sbin/diskutil", "mount", device])
    info = plist_command(["/usr/sbin/diskutil", "info", "-plist", "/dev/" + device])
    mount_point = info.get("MountPoint")
    if not isinstance(mount_point, str) or not mount_point.startswith("/"):
        raise RuntimeError(f"volume did not mount at an absolute path: {device}")
    return device, mount_point


def capture(label: str) -> dict[str, Any]:
    container, _ = image_container()
    rows = []
    for row in container.get("Volumes", []):
        if not isinstance(row, dict):
            continue
        device = row.get("DeviceIdentifier")
        info: dict[str, Any] = {}
        if isinstance(device, str):
            completed = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", "/dev/" + device],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            if completed.returncode == 0:
                full = plistlib.loads(completed.stdout)
                for key in ("VolumeUUID", "MountPoint", "APFSContainerReference", "APFSContainerFree",
                            "FilesystemType", "FreeSpace", "CapacityInUse"):
                    if key in full:
                        info[key] = full[key]
        mount_point = info.get("MountPoint")
        if isinstance(mount_point, str) and mount_point.startswith("/"):
            try:
                statvfs = os.statvfs(mount_point)
                info["statfs_available_bytes"] = statvfs.f_bavail * statvfs.f_frsize
                info["statfs_free_bytes"] = statvfs.f_bfree * statvfs.f_frsize
            except OSError as error:
                info["statfs_error"] = repr(error)
        rows.append({"volume": {key: row.get(key) for key in (
            "APFSVolumeUUID", "DeviceIdentifier", "Name", "CapacityInUse",
            "CapacityQuota", "CapacityReserve", "Roles")}, "info": info})
    snapshot = {
        "label": label,
        "captured_at_unix": time.time(),
        "container": {key: container.get(key) for key in (
            "APFSContainerUUID", "ContainerReference", "CapacityCeiling", "CapacityFree",
            "DesignatedPhysicalStore", "PhysicalStores")},
        "volumes": rows,
    }
    SNAPSHOTS.append(snapshot)
    return snapshot


def write_random(path: Path, target_bytes: int) -> tuple[int, str | None]:
    written = 0
    error_text: str | None = None
    with path.open("wb", buffering=0) as stream:
        while written < target_bytes:
            amount = min(CHUNK_BYTES, target_bytes - written)
            data = memoryview(os.urandom(amount))
            offset = 0
            try:
                while offset < amount:
                    count = stream.write(data[offset:])
                    if not isinstance(count, int) or count <= 0:
                        raise OSError("write returned no forward progress")
                    offset += count
                    written += count
            except OSError as error:
                error_text = f"{type(error).__name__}: {error}"
                break
        try:
            stream.flush()
            os.fsync(stream.fileno())
        except OSError as error:
            error_text = error_text or f"{type(error).__name__}: {error}"
    return written, error_text


def remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def save(status: str, error: str | None = None, cleanup_errors: list[str] | None = None) -> None:
    record = {
        "schema_version": 1,
        "status": status,
        "scope": "DISPOSABLE_BOUNDED_APFS_DISK_IMAGE_ONLY",
        "image_path": str(IMAGE),
        "image_virtual_size_max_bytes": IMAGE_MAX_BYTES,
        "image_virtual_size_bytes": RESULTS.get("image_virtual_size_bytes"),
        "limits": {
            "safety_reserve_bytes": SAFETY_RESERVE_BYTES,
            "quota_volume_reserve_bytes": VM_RESERVE_BYTES,
            "quota_volume_quota_bytes": VM_QUOTA_BYTES,
            "quota_write_attempt_max_bytes": QUOTA_WRITE_ATTEMPT_BYTES,
            "unlimited_sibling_write_attempt_max_bytes": WRITER_WRITE_ATTEMPT_BYTES,
            "safety_volume_data_write_max_bytes": SAFETY_RESERVE_BYTES,
            "write_chunk_max_bytes": CHUNK_BYTES,
        },
        "snapshots": SNAPSHOTS,
        "commands": LOG,
        "results": RESULTS,
        "error": error,
        "cleanup_errors": cleanup_errors or [],
        "reboot_tested": False,
        "production_startup_container_touched": False,
        "created_volume_uuids": sorted(CREATED_VOLUME_UUIDS),
    }
    temp = EVIDENCE.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(temp, EVIDENCE)


def cleanup() -> list[str]:
    errors: list[str] = []
    try:
        container, _ = image_container()
    except Exception:
        container = {}
    rows = container.get("Volumes", []) if isinstance(container, dict) else []
    devices = [row for row in rows
               if isinstance(row, dict) and isinstance(row.get("DeviceIdentifier"), str)]
    for device in devices:
        device_id = device["DeviceIdentifier"]
        info_result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", "/dev/" + device_id],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        mounted = False
        if info_result.returncode == 0:
            mount_point = plistlib.loads(info_result.stdout).get("MountPoint")
            mounted = isinstance(mount_point, str) and mount_point.startswith("/")
        if mounted:
            result = command(["/usr/sbin/diskutil", "unmount", device_id], check=False)
            if result.returncode:
                errors.append(f"unmount {device_id}: {result.stderr[-500:]}")
    for device in devices:
        device_id = device["DeviceIdentifier"]
        result = command(["/usr/sbin/diskutil", "apfs", "deleteVolume", device_id], check=False)
        if result.returncode:
            errors.append(f"delete volume {device_id}: {result.stderr[-500:]}")
    if ATTACHED_DISK:
        result = command(["/usr/bin/hdiutil", "detach", ATTACHED_DISK], check=False)
        if result.returncode:
            errors.append(f"detach {ATTACHED_DISK}: {result.stderr[-500:]}")
    if IMAGE.exists():
        result = command(["/bin/rm", "-rf", str(IMAGE)], check=False)
        if result.returncode:
            errors.append(f"remove image {IMAGE}: {result.stderr[-500:]}")
    return errors


def main() -> int:
    global ATTACHED_DISK
    if IMAGE.exists():
        raise RuntimeError(f"refusing to overwrite existing disposable image path: {IMAGE}")
    if IMAGE_MAX_BYTES > 3_200_000_000:
        raise RuntimeError("image bound exceeds the reviewed 3.2 GB maximum")
    status = "FAIL"
    error: str | None = None
    cleanup_errors: list[str] = []
    try:
        command(["/usr/bin/hdiutil", "create", "-megabytes", "3000", "-type", "SPARSEBUNDLE",
                 "-fs", "APFS", "-volname", "FGMetalProbeBootstrap", "-nospotlight", str(IMAGE)])
        image_size = image_virtual_size_bytes()
        RESULTS["image_virtual_size_bytes"] = image_size
        if image_size != IMAGE_REQUESTED_SIZE_BYTES or image_size > IMAGE_MAX_BYTES:
            raise RuntimeError(
                f"image virtual size {image_size} differs from requested/bounded size "
                f"{IMAGE_REQUESTED_SIZE_BYTES}/{IMAGE_MAX_BYTES}")
        attached = command(["/usr/bin/hdiutil", "attach", "-nomount", str(IMAGE)]).stdout
        candidates = [line.strip().split()[0] for line in attached.splitlines()
                      if line.strip().startswith("/dev/disk")]
        disks = [candidate.removeprefix("/dev/") for candidate in candidates]
        if not disks:
            raise RuntimeError(f"could not resolve attached disk image identifier from: {attached!r}")
        ATTACHED_DISK = disks[0]
        initial, container_ref = image_container()
        capture("initial")
        if initial.get("CapacityCeiling", 0) > IMAGE_MAX_BYTES:
            raise RuntimeError("attached APFS container exceeds the 3.2 GB logical bound")

        command(["/usr/sbin/diskutil", "apfs", "addVolume", container_ref, "APFS",
                 "FGMetalProbeSafety", "-reserve", "512MiB", "-nomount"])
        command(["/usr/sbin/diskutil", "apfs", "addVolume", container_ref, "APFS",
                 "FGMetalProbeQuota", "-reserve", "64MiB", "-quota", "512MiB", "-nomount"])
        command(["/usr/sbin/diskutil", "apfs", "addVolume", container_ref, "APFS",
                 "FGMetalProbeWriter", "-nomount"])
        current, _ = image_container()
        safety = volume(current, "FGMetalProbeSafety")
        quota = volume(current, "FGMetalProbeQuota")
        writer = volume(current, "FGMetalProbeWriter")
        if (safety.get("CapacityReserve") != SAFETY_RESERVE_BYTES
                or safety.get("CapacityQuota") != 0
                or quota.get("CapacityReserve") != VM_RESERVE_BYTES
                or quota.get("CapacityQuota") != VM_QUOTA_BYTES):
            raise RuntimeError("APFS reserve/quota readback does not match requested test values")
        RESULTS["configured_policy_readback"] = {
            "safety_volume_uuid": safety.get("APFSVolumeUUID"),
            "safety_reserve_bytes": safety.get("CapacityReserve"),
            "safety_quota_bytes": safety.get("CapacityQuota"),
            "quota_volume_uuid": quota.get("APFSVolumeUUID"),
            "quota_volume_reserve_bytes": quota.get("CapacityReserve"),
            "quota_volume_quota_bytes": quota.get("CapacityQuota"),
        }
        for row in (safety, quota, writer):
            mount_info(row)
        capture("after_volume_creation_and_mount")

        quota = volume(image_container()[0], "FGMetalProbeQuota")
        _, quota_mount = mount_info(quota)
        quota_path = Path(quota_mount) / "quota-exhaustion.bin"
        quota_written, quota_error = write_random(quota_path, QUOTA_WRITE_ATTEMPT_BYTES)
        quota_after = capture("after_quota_write_attempt")
        quota_now = volume(image_container()[0], "FGMetalProbeQuota")
        quota_enforced = quota_error is not None and quota_written <= VM_QUOTA_BYTES
        RESULTS["quota_exhaustion"] = {
            "attempted_bytes": QUOTA_WRITE_ATTEMPT_BYTES,
            "written_bytes_before_denial": quota_written,
            "file_size_bytes": quota_path.stat().st_size if quota_path.exists() else 0,
            "write_denied": quota_error is not None,
            "denial": quota_error,
            "readback_quota_bytes": quota_now.get("CapacityQuota"),
            "readback_in_use_bytes": quota_now.get("CapacityInUse"),
            "passed": quota_enforced and quota_written >= VM_QUOTA_BYTES - 64 * MIB,
        }
        remove_if_present(quota_path)
        command(["/bin/sync"])
        capture("after_quota_probe_cleanup")
        if not RESULTS["quota_exhaustion"]["passed"]:
            raise RuntimeError(f"quota exhaustion was not observed: wrote {quota_written}, error={quota_error}")

        writer = volume(image_container()[0], "FGMetalProbeWriter")
        _, writer_mount = mount_info(writer)
        writer_path = Path(writer_mount) / "sibling-growth.bin"
        writer_written, writer_error = write_random(writer_path, WRITER_WRITE_ATTEMPT_BYTES)
        writer_after = capture("after_unlimited_sibling_fill_attempt")
        writer_denied = writer_error is not None and writer_written < WRITER_WRITE_ATTEMPT_BYTES
        RESULTS["unlimited_sibling"] = {
            "attempted_bytes_cap": WRITER_WRITE_ATTEMPT_BYTES,
            "written_bytes_before_denial": writer_written,
            "file_size_bytes": writer_path.stat().st_size if writer_path.exists() else 0,
            "write_denied": writer_error is not None,
            "denial": writer_error,
            "container_capacity_free_bytes": writer_after["container"].get("CapacityFree"),
            "passed": writer_denied,
        }
        if not writer_denied:
            raise RuntimeError(f"unlimited sibling was not stopped within the bound: wrote {writer_written}")

        safety = volume(image_container()[0], "FGMetalProbeSafety")
        _, safety_mount = mount_info(safety)
        safety_path = Path(safety_mount) / "reserve-capacity-readback.bin"
        safety_written, safety_error = write_random(safety_path, SAFETY_RESERVE_BYTES)
        safety_after = capture("after_safety_volume_reserved_capacity_write")
        safety_after_volume = next(
            (entry["volume"] for entry in safety_after["volumes"]
             if entry["volume"].get("Name") == "FGMetalProbeSafety"), {})
        RESULTS["safety_reserved_file_data"] = {
            "requested_bytes": SAFETY_RESERVE_BYTES,
            "written_bytes": safety_written,
            "file_size_bytes": safety_path.stat().st_size if safety_path.exists() else 0,
            "safety_capacity_in_use_bytes": safety_after_volume.get("CapacityInUse"),
            "write_error": safety_error,
            "capacity_free_after_write_bytes": safety_after["container"].get("CapacityFree"),
            "passed": safety_error is None and safety_written == SAFETY_RESERVE_BYTES,
        }
        if safety_error is not None or safety_written != SAFETY_RESERVE_BYTES:
            raise RuntimeError(f"safety volume could not use its reserved data capacity: {safety_written}, {safety_error}")

        writer_path_2 = Path(writer_mount) / "sibling-growth-after-reserve.bin"
        append_bytes = 32 * MIB
        sibling_after_written, append_error = write_random(writer_path_2, append_bytes)
        sibling_after_reserve_denied = append_error is not None
        sibling_after_snapshot = capture("after_second_sibling_attempt_with_safety_data_present")
        RESULTS["sibling_after_safety_allocation"] = {
            "attempted_bytes": append_bytes,
            "written_bytes_before_denial": sibling_after_written,
            "file_size_bytes": writer_path_2.stat().st_size if writer_path_2.exists() else 0,
            "write_denied": append_error is not None,
            "denial": append_error,
            "capacity_free_bytes": sibling_after_snapshot["container"].get("CapacityFree"),
            "passed": sibling_after_reserve_denied,
        }
        if not sibling_after_reserve_denied:
            raise RuntimeError("sibling volume accepted extra data after safety reserve allocation")

        remove_if_present(safety_path)
        remove_if_present(writer_path)
        remove_if_present(writer_path_2)
        command(["/bin/sync"])
        capture("after_large_probe_file_cleanup")

        safety = volume(image_container()[0], "FGMetalProbeSafety")
        safety_uuid = safety.get("APFSVolumeUUID")
        safety_device = safety.get("DeviceIdentifier")
        if not isinstance(safety_uuid, str) or not isinstance(safety_device, str):
            raise RuntimeError("safety volume identity disappeared before remount test")
        command(["/usr/sbin/diskutil", "unmount", safety_device])
        unmounted = capture("safety_volume_unmounted")
        command(["/usr/sbin/diskutil", "mount", safety_device])
        remounted = capture("safety_volume_remounted")
        remounted_row = volume(image_container()[0], "FGMetalProbeSafety")
        persistence_ok = (remounted_row.get("APFSVolumeUUID") == safety_uuid
                          and remounted_row.get("CapacityReserve") == SAFETY_RESERVE_BYTES)
        RESULTS["unmount_remount"] = {
            "original_uuid": safety_uuid,
            "remounted_uuid": remounted_row.get("APFSVolumeUUID"),
            "unmounted_snapshot_reserve_bytes": next(
                (entry["volume"].get("CapacityReserve") for entry in unmounted["volumes"]
                 if entry["volume"].get("Name") == "FGMetalProbeSafety"), None),
            "remounted_reserve_bytes": remounted_row.get("CapacityReserve"),
            "passed": persistence_ok,
        }
        if not persistence_ok:
            raise RuntimeError("reserve or volume identity changed across unmount/remount")

        command(["/usr/sbin/diskutil", "unmount", safety_device])
        command(["/usr/sbin/diskutil", "apfs", "deleteVolume", safety_device])
        command(["/usr/sbin/diskutil", "apfs", "addVolume", container_ref, "APFS",
                 "FGMetalProbeSafety", "-reserve", "512MiB", "-nomount"])
        recreated = volume(image_container()[0], "FGMetalProbeSafety")
        recreated_uuid = recreated.get("APFSVolumeUUID")
        recreation_changed_identity = isinstance(recreated_uuid, str) and recreated_uuid != safety_uuid
        RESULTS["delete_recreate_identity"] = {
            "old_volume_uuid": safety_uuid,
            "new_volume_uuid": recreated_uuid,
            "uuid_changed": recreation_changed_identity,
            "passed": recreation_changed_identity,
        }
        capture("safety_volume_deleted_and_recreated")
        if not recreation_changed_identity:
            raise RuntimeError("deleted/recreated safety volume did not receive a new UUID")

        status = "PASS"
        return 0
    except Exception as caught:
        error = f"{type(caught).__name__}: {caught}"
        return 1
    finally:
        cleanup_errors = cleanup()
        save(status if not cleanup_errors else "FAIL_CLEANUP", error, cleanup_errors)


if __name__ == "__main__":
    raise SystemExit(main())
