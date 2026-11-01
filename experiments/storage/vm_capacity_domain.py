#!/usr/bin/env python3
"""Narrow internal APFS capacity mode for VM provisioning and runtime assets.

This controller deliberately leaves normal FG-Metal storage limits in
``storage_policy.py``. It binds a separate quota-limited VM APFS volume to one
physical APFS container. Its authenticated active-session marker reports any
outstanding shared physical-capacity commitment for ``storage_policy`` to
account for once during ordinary storage admission; VM/P2 execution authority
remains owned by this controller. Malformed or unknown capacity claims fail
closed.
"""
from __future__ import annotations

import hashlib
import errno
import ctypes
from dataclasses import dataclass
import json
import math
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import storage_policy as policy

GIB = 1024 ** 3
MIB = 1024 ** 2
VM_MODE = "INTERNAL_VM_CAPACITY_DOMAIN"
VM_VOLUME_NAME = "FGMetal-VM"
VM_SAFETY_VOLUME_NAME = "FGMetal-VM-Safety"
VM_FILESYSTEM = "APFS"
VM_REVIEWED_QUOTA_MAX_BYTES = 50 * GIB
VM_PROTECTED_CAPACITY_RESERVE_BYTES = 15 * GIB
VM_OPERATIONAL_UNRESERVED_FREE_MIN = 5 * GIB
VM_OPERATIONAL_WARNING_FREE = 8 * GIB
VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES = 256 * MIB
VM_SHARED_FREE_CROSSCHECK_TOLERANCE = GIB
VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS = 0.25
VM_PHASE_STOP_TIMEOUT_SECONDS = 30.0
VM_RESTORE_DOWNLOAD_CHUNK_BYTES = 16 * MIB
VM_PHASE_UNCERTAINTY_BASIS_POINTS = 1000
VM_PHASE_MIN_UNCERTAINTY_BYTES = 256 * MIB
VM_CONCURRENT_GROWTH_SAMPLE_COUNT = 4
VM_CONCURRENT_GROWTH_SAMPLE_INTERVAL_SECONDS = 0.25
VM_P1_METADATA_MAX_BYTES = 32 * MIB
VM_P2_SYSTEM_FILE_DATA_MAX_BYTES = 17 * GIB
VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES = GIB
VM_SYSTEM_LOGICAL_CAPACITY_BYTES = 64 * GIB
VM_AUXILIARY_LOGICAL_CAPACITY_BYTES = 2 * GIB
VM_MAX_OBSERVATION_AGE_SECONDS = 30
VM_MAX_RESTORE_METADATA_AGE_SECONDS = 300
VM_STOP_SAFETY_PROFILE_MAX_AGE_SECONDS = 24 * 60 * 60
VM_STOP_SAFETY_PROFILE_MODE_MEASURED = "MEASURED_STOP_SAFETY_PROFILE"
VM_STOP_SAFETY_PROFILE_MODE_LIVE = "LIVE_APFS_CAPACITY_MONITOR"
VM_ACTIVE_MARKER_PATH = policy.VM_CAPACITY_DOMAIN_ACTIVE_MARKER
VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD = (
    "vm_deferred_branch_retirement_hmac_sha256")
VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD = (
    "vm_deferred_restore_retirement_authorization_hmac_sha256")
VM_DEFERRED_RESTORE_PENDING_SIGNATURE_FIELD = (
    "vm_deferred_restore_retirement_pending_hmac_sha256")
VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD = (
    "vm_deferred_restore_retirement_hmac_sha256")
VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD = (
    "vm_deferred_restore_recovery_validation_hmac_sha256")
VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION_SIGNATURE_FIELD = (
    "vm_deferred_restore_recovery_authorization_hmac_sha256")
VM_DEFERRED_RESTORE_RECOVERY_USE_SIGNATURE_FIELD = (
    "vm_deferred_restore_recovery_use_hmac_sha256")
VM_DEFERRED_CAPACITY_INVENTORY_SIGNATURE_FIELD = (
    "vm_deferred_capacity_inventory_hmac_sha256")
VM_DEFERRED_CAPACITY_INACTIVATION_SIGNATURE_FIELD = (
    "vm_deferred_capacity_inactivation_hmac_sha256")
VM_R14_RECOVERY_MEASUREMENT_FAILURE_PATH = policy.REPO / (
    "experiments/storage/R6-VM-DEFERRED-RESTORE-RECOVERY-"
    "POST-UNLINK-MEASUREMENT-FAILURE-R13-20261020T070049Z.json")
VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256 = (
    "9b88a4bd9b82a2337a0ae0db4d10b186e2a606f77ca0085b228dd662481e4478")
VM_R14_RESOLUTION_REVIEW_DECISION = (
    "PASS — POST-UNLINK RETIREMENT RESOLUTION ACCEPTED")
VM_R14_FUTURE_COMMITMENT_REVIEW_DECISION = (
    "PASS — FUTURE P2 CAPACITY COMMITMENT MAY BE INACTIVATED")
VM_R14_PARKING_REVIEW_DECISION = (
    "PASS — DEFERRED VM BRANCH PARKED SAFELY")
VM_R14_CURRENT_INVENTORY_MAX_AGE_SECONDS = 30 * 60
VM_R14_R6A_PROJECTED_DEMAND_BYTES = 4_423_832_374
VM_R14_R6A_DEMAND_PLAN_PATH = policy.REPO / "R6-A-C-TEST-PROVENANCE.md"
VM_R14_R6A_DEMAND_PLAN_SHA256 = (
    "a583d7842163170ec1aac70b206ef66f8e003e2f0d47f4cac0628fc7343e48b4")
VM_R13_BOUNDARY_HASH_STATUS = (
    "PASSED_BEFORE_UNLINK_BY_CONTROLLER_CONTROL_FLOW; result was not persisted because "
    "post-unlink storage reconciliation failed")
VM_R13_PENDING_RECEIPT_PATH = policy.REPO / (
    "R6-VM-DEFERRED-RESTORE-RETIREMENT-PENDING-"
    "2e6cd78d402945e08d5a755b86e55bac.json")
VM_R13_OLD_AUTHORIZATION_PATH = policy.REPO / (
    "R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-"
    "20261019T201910Z-3e1a9e053bb5.json")
VM_R13_OLD_PREDELETE_REVIEW_PATH = policy.REPO / (
    "R6-VM-DEFERRED-RESTORE-PREDELETE-REVIEW-PASS-"
    "20261019T201910Z-3e1a9e053bb5.json")
VM_R13_R11_FAILURE_PATH = Path(
    "/tmp/fgmetal-vm-deferred-parking-20261019/restore-retirement-r11.json")
VM_R13_R12_IDENTITY_REVIEW_PATH = policy.REPO / (
    "R6-VM-R12-RESTORE-IDENTITY-RECONCILIATION.md")
VM_R13_R12_QUARANTINE_REVIEW_PATH = policy.REPO / (
    "R6-VM-R12-CURRENT-QUARANTINE-IDENTITY-REVIEW.md")
VM_R13_PENDING_RECEIPT_SHA256 = (
    "26fcb6bae64a8a1c5e9cbc7f4e781676c4d40657766c67d10422fbf4fef16169")
VM_R13_OLD_AUTHORIZATION_SHA256 = (
    "7314df88fc991c617143d38983c9b3fb708451fc33cbc84ccbb7b73831119ef6")
VM_R13_OLD_PREDELETE_REVIEW_SHA256 = (
    "4540c6750a48732fecedb46b1005b673cec2c7578c9597f51eb8687e5dcd8129")
VM_R13_R11_FAILURE_SHA256 = (
    "17f74d17241cb0b16f777b07c564d6a4fcd710ce9c1301f8ad35b916ef9b3ae7")
VM_R13_R12_CONTROLLER_SHA256 = (
    "a20e3aa2769c8b3d539a9376b7080dbfc978f56d7eeb8c21410948739b1a497e")
VM_R13_R12_TEST_SHA256 = (
    "129f227c4ed42257c17563b5b1b9b909bb3c0889d2a7ef83d22e8b778f517b46")
VM_R13_R12_IDENTITY_REVIEW_SHA256 = (
    "366ee80008597ef5d817f8f758b2c31be51f4c51480d1aa85cb0827bcb5f05c9")
VM_R13_R12_QUARANTINE_REVIEW_SHA256 = (
    "51972adbe62aa49f719ec0f6961bf400544628d2b3585e63e75f188df4bb545e")
VM_R13_EXPECTED_QUARANTINE_NAME = (
    ".restore.ipsw.deferred-retirement-2e6cd78d402945e08d5a755b86e55bac")
VM_R13_EXPECTED_QUARANTINE_IDENTITY = {
    "device": 16_777_241,
    "inode": 380,
    "logical_bytes": 26_637_307_067,
    "allocated_bytes": 26_637_307_904,
    "mtime_ns": 1_792_307_579_121_190_387,
    "birthtime_ns": 1_792_305_321_463_162_880,
    "owner_uid": 501,
    "owner_gid": 20,
    "mode": 0o600,
    "link_count": 1,
    "file_type": "regular",
}
VM_R13_EXPECTED_ROOT_IDENTITY = {
    "device": 16_777_241, "inode": 2, "owner_uid": 501, "mode": 0o775,
}
VM_R13_BIRTHTIME_TOLERANCE_NS = 1_000
VM_R13_RECOVERY_REVIEW_DECISION = (
    "PASS — EXACT QUARANTINED RESTORE VALIDATED FOR RECOVERY DELETION")
VM_R13_PATH_REVIEW_DECISION = "PASS — DEFERRED RESTORE RECOVERY PATH ACCEPTED"
VM_R13_VALIDATION_MAX_AGE_SECONDS = 30 * 60
VM_DEFERRED_R4_ADMISSION_PATH = policy.REPO / (
    "R6-VM-P2-ADMISSION-p2-r4-execution-freshness-20261018T214134Z.json")
VM_DEFERRED_R4_FAILURE_PATH = policy.REPO / (
    "R6-VM-P2-HELPER-LAUNCH-FAILURE-p2-r4-execution-freshness-20261018T214134Z.json")
VM_DEFERRED_R4_EXECUTION_START_PATH = policy.REPO / (
    "R6-VM-P2-EXECUTION-START-p2-r4-execution-freshness-20261018T214134Z-"
    "execstart-2de2f1cb5cf0.json")
VM_DEFERRED_P1_ADMISSION_PATH = policy.REPO / (
    "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-20261017T152047Z-fc8ad609.json")
VM_DEFERRED_P1_VERIFICATION_PATH = policy.REPO / (
    "R6-VM-P1-RESTORE-VERIFICATION-20261018T063450Z-d697b287.json")
VM_DEFERRED_RESTORE_PATH = Path("/Volumes/FGMetal-VM/restore.ipsw")
VM_DEFERRED_RESTORE_SHA256 = (
    "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1")
VM_DEFERRED_RESTORE_LOGICAL_BYTES = 26_637_307_067
VM_DEFERRED_R4_ADMISSION_SHA256 = (
    "c8e1763f7f53dfb3586f997a5817ec66d9a251b7551b06775ee1120f16c44eeb")
VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256 = (
    "f28d3ef9e45db1f41b20ac70b586d0e022f33482d1ef6b269e60f2318c37f164")
VM_DEFERRED_ACTIVE_MARKER_SHA256 = (
    "d53ecb28da202e5efaa03acf5cb3e8c64ed1022d250bc63dbfe602e349e74850")
VM_DEFERRED_SESSION_ID = "bc18455a44e143c0a9a0d4e0227b0db9"
VM_DEFERRED_CONTAINER_UUID = "0C1A50F1-E2F9-4BB0-A6C2-6B2581B7EE83"
VM_DEFERRED_PHYSICAL_STORE_UUID = "69F188B6-DF27-4E2E-960B-919C85AA18B3"
VM_DEFERRED_VM_VOLUME_UUID = "DC80C359-922C-41DE-B106-5E0C00FEDCE1"
VM_DEFERRED_SAFETY_VOLUME_UUID = "0FFC5DA2-F198-4CFF-B1C1-112015D3F126"
VM_DEFERRED_QUOTA_BYTES = 50 * GIB
VM_DEFERRED_SAFETY_RESERVE_BYTES = 15 * GIB
VM_DEFERRED_REVIEW_MAX_AGE_SECONDS = 30 * 60
VM_ADMISSION_PATH = policy.REPO / "R6-VM-INTERNAL-PROVISIONING-ADMISSION.json"
VM_PRECREATION_ADMISSION_PATH = policy.REPO / "R6-VM-INTERNAL-VOLUME-PRECREATION-ADMISSION.json"
VM_VOLUME_CREATION_RECEIPT_PATH = policy.REPO / "R6-VM-INTERNAL-APFS-VOLUME-CREATION.json"
VM_P2_ADMISSION_PATH = policy.REPO / "R6-VM-P2-INSTALL-ADMISSION.json"
VM_P2_MONITOR_RECEIPT_PATH = policy.REPO / "R6-VM-P2-MONITOR-RESULT.json"
VM_RESTORE_VERIFICATION_PATH = policy.REPO / "R6-VM-P1-RESTORE-VERIFICATION.json"
VM_QUOTA_PROBE_VOLUME_NAME = "FGMetal-VM-QuotaProbe"
VM_QUOTA_PROBE_QUOTA_BYTES = 512 * MIB
VM_QUOTA_PROBE_OVERWRITE_BYTES = 64 * MIB
VM_QUOTA_PROBE_MAX_WRITE_BYTES = VM_QUOTA_PROBE_QUOTA_BYTES + VM_QUOTA_PROBE_OVERWRITE_BYTES
VM_QUOTA_PROBE_RECEIPT_PATH = policy.REPO / "R6-VM-APFS-QUOTA-PROBE.json"
VM_RESTORE_METADATA_PATH = policy.REPO / "R6-VM-RESTORE-UPSTREAM-METADATA.json"
VM_RESTORE_METADATA_PREFIX = "R6-VM-RESTORE-UPSTREAM-METADATA"
VM_RESTORE_METADATA_PROBE_SOURCE = policy.REPO / "experiments/storage/vm_restore_metadata_probe.swift"
VM_RESTORE_LINEAGE_PREFIX = "R6-VM-RESTORE-METADATA-REFRESH-LINEAGE"
VM_RESTORE_COMPATIBILITY_BASIS_PREFIX = "R6-VM-RESTORE-METADATA-COMPATIBILITY-BASIS"
VM_P0_REVALIDATION_PREFIX = "R6-VM-P0-REVALIDATED-FOR-P1"
VM_P1_ADMISSION_PREFIX = "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1"
VM_P1_REVIEW_PREFIX = "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-REVIEW"
VM_P1_FRESHNESS_RENEWAL_PREFIX = "R6-VM-P1-FRESHNESS-RENEWAL"
VM_P1_FRESHNESS_START_PREFIX = "R6-VM-P1-FRESHNESS-START"
VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD = "vm_p1_freshness_renewal_hmac_sha256"
VM_P1_FRESHNESS_START_SIGNATURE_FIELD = "vm_p1_freshness_start_hmac_sha256"
VM_P0_RUN_ID = "20261017T135010Z-14baf9fe"
VM_P0_ADMISSION_PATH = policy.REPO / f"R6-VM-INTERNAL-PROVISIONING-ADMISSION-{VM_P0_RUN_ID}.json"
VM_P0_REVIEW_PATH = policy.REPO / f"R6-VM-INTERNAL-PROVISIONING-ADMISSION-REVIEW-{VM_P0_RUN_ID}.json"
VM_P0_CREATION_RECEIPT_PATH = policy.REPO / f"R6-VM-INTERNAL-APFS-VOLUME-CREATION-{VM_P0_RUN_ID}.json"
VM_P0_METADATA_PATH = policy.REPO / f"R6-VM-RESTORE-UPSTREAM-METADATA-{VM_P0_RUN_ID}.json"
VM_P0_G0C_SNAPSHOT_PATH = policy.REPO / "experiments/storage/G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-R6.json"
VM_P0_G0C_REVIEW_PATH = policy.REPO / "experiments/storage/g0c-internal-vm-controller-independent-review-r6.json"
VM_P0_ADMISSION_SHA256 = "2fcdb0537259092f8dbaf1b99ab67a11b472e7d91f5668f25ddb7bb45c140a78"
VM_P0_REVIEW_SHA256 = "8058e34ca1dbc5ed898f9a372529a486350ca3e3e2c3e553847882392f139a9c"
VM_P0_CREATION_RECEIPT_SHA256 = "37abb870f91faf1c1f3170ae8c42cf025d1108c8bcf52e217e6138387e25fe65"
VM_P0_METADATA_SHA256 = "0aaec91a3f19b964a3b735ef1b1c1cda48f8e1fe2a79202787e09e808432962d"
VM_P0_G0C_SNAPSHOT_SHA256 = "b6f154df0b8350a6a81f8493f29d86bc34442bb5b00a36dfb39b45ec0a776dbc"
VM_P0_G0C_REVIEW_SHA256 = "c258cac10f45f7ea63c7c9e63eef31eb78ede181eebbd6845f179417839e562c"
VM_P2_INSTALL_RECEIPT_PATH = policy.REPO / "R6-VM-P2-INSTALL-RESULT.json"
VM_STOP_SAFETY_PROFILE_PATH = policy.REPO / "R6-VM-STOP-SAFETY-PROFILE.json"
VM_RECOVERY_RESERVE_RELEASE_PATH = policy.REPO / "R6-VM-RECOVERY-RESERVE-RELEASE.json"
VM_P3_SEAL_RECEIPT_PATH = policy.REPO / "R6-VM-P3-SEALED-BASE.json"
VM_P4_CLEANUP_ACTION_PATH = policy.REPO / "R6-VM-P4-RESTORE-CLEANUP-ACTION.json"
VM_P4_CLEANUP_RECEIPT_PATH = policy.REPO / "R6-VM-P4-RESTORE-CLEANUP-RESULT.json"
VM_P5_ADMISSION_PATH = policy.REPO / "R6-VM-P5-RUNTIME-ADMISSION.json"
VM_STOP_CUSHION_BYTES = 3 * GIB
VM_P5_LOGICAL_LIMITS = {
    "read_only_input_tool_raw": 8 * GIB,
    "workspace_raw": 2 * GIB,
    "export_capsule_raw": 8 * GIB,
}
G0C_SNAPSHOT_PATH = policy.REPO / "experiments/storage/G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-P1-BODY-START-REPAIR-R6-20261017.json"
G0C_REVIEW_PATH = policy.REPO / "experiments/storage/g0c-internal-vm-controller-independent-review-p1-body-start-repair-r6-20261017.json"

# The accepted R2 P1 review predates the redundant source_identity field. This
# is a single evidence tuple, never a general fallback for omitted fields.
VM_P1_ACCEPTED_ADMISSION_PATH = policy.REPO / "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-20261017T152047Z-fc8ad609.json"
VM_P1_ACCEPTED_ADMISSION_SHA256 = "a8c67f4a48382477ef3f3e4a25cff64c54c700017a0e12b12fcd6ed724c9ba4b"
VM_P1_ACCEPTED_REVIEW_PATH = policy.REPO / "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-REVIEW-20261017T152047Z-fc8ad609.json"
VM_P1_ACCEPTED_REVIEW_SHA256 = "b08cb4cf6c8c51c866205007111d2e799c2aefb34b51ec1c3bf0f28ff558e143"
VM_P1_R2_G0C_SNAPSHOT_PATH = policy.REPO / "experiments/storage/G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-METADATA-P1-TRANSITION-R2-20261017.json"
VM_P1_R2_G0C_SNAPSHOT_SHA256 = "1ea833e2831d0ec26bac0d51c992ba802d320d64d2a1a9cc5b62e6acbac370c1"
VM_P1_R2_G0C_REVIEW_PATH = policy.REPO / "experiments/storage/g0c-internal-vm-controller-independent-review-metadata-p1-transition-r2-20261017.json"
VM_P1_R2_G0C_REVIEW_SHA256 = "f9fd8cb2a7b1d160733fbdccc85296a97f76e5efa54e75b00f79009b574470e3"
VM_P1_R2_G0C_REVIEWER_IDENTITY = "GPT-6 Luna Max independent G0C reviewer for metadata/P1 transition R2, 2026-10-17"
VM_P1_R2_CONTROLLER_IDENTITY = {
    "vm_capacity_controller_sha256": "7d480e540c49030ad75100693ab40e9fbc7b911d7fe4f14dd4a0b35c5a867af1",
    "storage_policy_sha256": "0b622ab3443a1b7c1086673ad3db1b966ecd12b9145222a294f025eeb511def8",
    "restore_metadata_probe_sha256": "779cbba581e6cfdec12d7a42b4dd3e9d225c3db2a49be1f390cb1827d8781e95",
}
VM_P1_R2_TEST_BINDING = {
    "path": "experiments/storage/test_vm_capacity_domain.py",
    "sha256": "673118b593ad0795b0d6af4cb60bdcc12b5203caa89e72244abb6a0b96e58fac",
    "size_bytes": 119333,
}
VM_P1_R2_REVIEWED_FILE_KEYS = frozenset({
    "build_retention_control", "build_retention_tests", "gate0_checks_tests",
    "gate0_control", "prefix_preflight_control", "prefixlease_contract_tests",
    "source", "storage_policy_tests", "tests", "vm_capacity_controller",
    "vm_capacity_tests", "vm_restore_metadata_probe",
})
VM_P1_G0C_ANCESTRY_RULE = "R6_VM_P1_G0C_R2_SOURCE_IDENTITY_CONTROLLER_TEST_ABSENT_V1"
VM_P1_G0C_ANCESTRY_RECEIPT_PATH = policy.REPO / "R6-VM-P1-HISTORICAL-G0C-ANCESTRY-COMPLETION.json"
VM_P1_G0C_ANCESTRY_REVIEW_PATH = policy.REPO / "R6-VM-P1-HISTORICAL-G0C-ANCESTRY-COMPLETION-REVIEW.json"
VM_P1_G0C_ANCESTRY_RECEIPT_SHA256 = "e2ed2cf21573d0f918a5442029c19ff7761015257a6ef394f1fdd90ccc422791"
VM_P1_G0C_ANCESTRY_REVIEW_SHA256 = "efa945e78113969dc6e8352ee155ccccc647e646027d5a0793b0c3fffa1e6df5"
VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD = "vm_p1_historical_g0c_ancestry_completion_hmac_sha256"
VM_P1_G0C_ANCESTRY_REVIEWER_IDENTITY = "GPT-6 Luna Max Historical G0C Ancestry Compatibility Reviewer"
VM_P1_G0C_ANCESTRY_RECEIPT_TYPE = "R6_VM_P1_HISTORICAL_G0C_ANCESTRY_COMPLETION"
VM_P1_G0C_ANCESTRY_RECEIPT_SCOPE = "HISTORICAL_EVIDENCE_COMPLETENESS_ONLY"
VM_P1_G0C_ANCESTRY_REVIEW_SCOPE = "EXACT_HISTORICAL_G0C_ANCESTRY_COMPLETION_RECEIPT"
VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY = {
    "vm_capacity_controller_sha256": "821654429b89939240ed18de4a26665ead98d2221869dbc47e48bdd58ccd3650",
    "storage_policy_sha256": "0b622ab3443a1b7c1086673ad3db1b966ecd12b9145222a294f025eeb511def8",
    "restore_metadata_probe_sha256": "779cbba581e6cfdec12d7a42b4dd3e9d225c3db2a49be1f390cb1827d8781e95",
}
VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY = {
    "model_identity": "GPT-6 Luna Max",
    "review_path": "experiments/storage/g0c-internal-vm-controller-independent-review-p1-historical-g0c-ancestry-r4-20261017.json",
    "review_sha256": "37bd3be9d1885a078678b8ed416f0c945c5b5e4520fedfaca64ab92476172501",
    "reviewed_file_count": 12,
    "reviewer_identity": "GPT-6 Luna Max G0C Storage/Gate Controller Reviewer",
    "snapshot_path": "experiments/storage/G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-P1-HISTORICAL-G0C-ANCESTRY-R4-20261017.json",
    "snapshot_sha256": "7293571ec94c2bdfbbecc99ede2b75750c7153472c6b0d1600d161480763aace",
}
VM_P1_G0C_ANCESTRY_ISSUANCE_TEST_BINDING = {
    "path": "experiments/storage/test_vm_capacity_domain.py",
    "sha256": "abc5e1bad7cff9daa54049ce156de79aff346bf90c7f2b92ecda9057f63ec78a",
    "size_bytes": 185316,
}
VM_P1_G0C_ANCESTRY_TRANSITION_PATH = policy.REPO / (
    "R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-6c5df87aac7446619941480f99b69827.json")
VM_P1_G0C_ANCESTRY_TRANSITION_PREDECESSOR_PATH: Path | None = None
VM_P1_G0C_ANCESTRY_TRANSITION_TYPE = "R6_VM_HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION"
VM_P1_G0C_ANCESTRY_TRANSITION_SCOPE = "HISTORICAL_G0C_ANCESTRY_COMPATIBILITY_ONLY"
VM_P1_G0C_ANCESTRY_TRANSITION_RULE = "R6_VM_P1_G0C_HISTORICAL_COMPLETION_COMPATIBILITY_CHAIN_V1"
VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD = (
    "vm_p1_historical_ancestry_compatibility_transition_hmac_sha256")
VM_P1_G0C_ANCESTRY_TRANSITION_MAX_CHAIN_LENGTH = 64

# These UUIDs identify the intended internal Data-backed APFS container captured
# by the prior read-only audit. Any changed identity requires an explicit review.
EXPECTED_PHYSICAL_STORE_UUID = "69F188B6-DF27-4E2E-960B-919C85AA18B3"
EXPECTED_CONTAINER_UUID = "0C1A50F1-E2F9-4BB0-A6C2-6B2581B7EE83"
EXPECTED_DATA_VOLUME_UUID = "270E840B-FC2D-4B73-BE0F-D0ED097C4ECA"
EXPECTED_INTERNAL_MEDIA_NAME = "APPLE SSD AP0512Z"
EXPECTED_DATA_MOUNT = "/System/Volumes/Data"
EXPECTED_PRODUCTION_VM_VOLUME_UUID = "DC80C359-922C-41DE-B106-5E0C00FEDCE1"
EXPECTED_PRODUCTION_SAFETY_VOLUME_UUID = "0FFC5DA2-F198-4CFF-B1C1-112015D3F126"
EXPECTED_PRODUCTION_VM_QUOTA_BYTES = 53_687_091_200


class _DarwinStatFS(ctypes.Structure):
    """64-bit Darwin ``struct statfs`` fields used by getmntinfo/fstatfs.

    Keep this in lockstep with the public ``sys/mount.h`` layout for
    ``__DARWIN_64_BIT_INO_T``. The runtime size check below fails closed if a
    future ABI changes the structure.
    """

    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


_DARWIN_STATFS_SIZE = 2168
_MNT_WAIT = 1
_EXPECTED_VM_MOUNT_STATES = {"MOUNTED_VERIFIED", "UNMOUNTED_VERIFIED"}


def _darwin_libsystem() -> Any:
    if platform.system() != "Darwin":
        raise RuntimeError("APFS mount identity requires Darwin mounted-filesystem APIs")
    if ctypes.sizeof(_DarwinStatFS) != _DARWIN_STATFS_SIZE:
        raise RuntimeError("Darwin statfs ABI size differs from the reviewed getmntinfo layout")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    library.getmntinfo.argtypes = [
        ctypes.POINTER(ctypes.POINTER(_DarwinStatFS)), ctypes.c_int,
    ]
    library.getmntinfo.restype = ctypes.c_int
    library.fstatfs.argtypes = [ctypes.c_int, ctypes.POINTER(_DarwinStatFS)]
    library.fstatfs.restype = ctypes.c_int
    return library


def _statfs_text(value: bytes) -> str:
    return value.split(b"\0", 1)[0].decode("utf-8", errors="surrogateescape")


def _statfs_mount_record(row: _DarwinStatFS) -> dict[str, Any]:
    return {
        "source": _statfs_text(row.f_mntfromname),
        "mount_point": _statfs_text(row.f_mntonname),
        "filesystem_type": _statfs_text(row.f_fstypename),
        "filesystem_id": [int(row.f_fsid[0]), int(row.f_fsid[1])],
    }


def _mounted_filesystem_table() -> list[dict[str, Any]]:
    """Read one kernel-backed mount table snapshot with getmntinfo(3)."""
    library = _darwin_libsystem()
    rows = ctypes.POINTER(_DarwinStatFS)()
    count = library.getmntinfo(ctypes.byref(rows), _MNT_WAIT)
    if count <= 0 or not rows:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "getmntinfo did not return a mounted-filesystem table")
    result = [_statfs_mount_record(rows[index]) for index in range(count)]
    if any(not row["source"] or not row["mount_point"].startswith("/")
           or not row["filesystem_type"] for row in result):
        raise RuntimeError("getmntinfo returned a malformed mounted-filesystem entry")
    return result


def _fd_mount_record(file_descriptor: int) -> dict[str, Any]:
    """Read the mounted filesystem attached to an already-open file descriptor."""
    library = _darwin_libsystem()
    row = _DarwinStatFS()
    if library.fstatfs(file_descriptor, ctypes.byref(row)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "fstatfs failed for the open APFS probe target")
    record = _statfs_mount_record(row)
    if not record["source"] or not record["mount_point"].startswith("/"):
        raise RuntimeError("fstatfs returned a malformed APFS mount identity")
    return record


def _resolve_apfs_mount_state(
        *, volume_uuid: str, device_identifier: str, container_uuid: str,
        container_reference: str, volume_name: str, expected_mount_location: str,
        diskutil_info: Mapping[str, Any], mount_records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Resolve one APFS volume against exact diskutil identity and getmntinfo state.

    A missing ``Mounted`` or ``MountPoint`` plist key is allowed. The kernel
    mount table determines state; diskutil identity and any supplied mount
    fields must corroborate it. Paths alone never establish mounted state.
    """
    if (not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid)
            or not re.fullmatch(r"[0-9A-Fa-f-]{36}", container_uuid)
            or not re.fullmatch(r"disk[0-9]+s[0-9]+", device_identifier)
            or not re.fullmatch(r"disk[0-9]+", container_reference)
            or not isinstance(volume_name, str) or not volume_name
            or Path(volume_name).name != volume_name or "\0" in volume_name
            or not isinstance(expected_mount_location, str)
            or not expected_mount_location.startswith("/")):
        raise ValueError("expected APFS UUID/device/container/mount identity is malformed")
    if os.path.normpath(expected_mount_location) != expected_mount_location:
        raise ValueError("expected APFS mount location is not normalized")
    if not isinstance(diskutil_info, Mapping):
        raise ValueError("diskutil APFS volume identity is missing")
    expected_uuid = volume_uuid.upper()
    actual_uuid = diskutil_info.get("VolumeUUID")
    actual_container_uuid = diskutil_info.get("APFSContainerUUID")
    if (diskutil_info.get("DeviceIdentifier") != device_identifier
            or not isinstance(actual_uuid, str) or actual_uuid.upper() != expected_uuid
            or diskutil_info.get("APFSContainerReference") != container_reference
            or (actual_container_uuid is not None
                and (not isinstance(actual_container_uuid, str)
                     or actual_container_uuid.upper() != container_uuid.upper()))
            or diskutil_info.get("VolumeName") != volume_name
            or str(diskutil_info.get("FilesystemType", "")).lower() != "apfs"):
        raise RuntimeError("diskutil UUID/device/container/name/filesystem identity conflicts with APFS enumeration")

    mounted_key_present = "Mounted" in diskutil_info
    mounted_value = diskutil_info.get("Mounted")
    if mounted_key_present and not isinstance(mounted_value, bool):
        raise RuntimeError("diskutil Mounted field is present but is not Boolean")
    mount_point_key_present = "MountPoint" in diskutil_info
    diskutil_mount_point = diskutil_info.get("MountPoint")
    if mount_point_key_present and not isinstance(diskutil_mount_point, str):
        raise RuntimeError("diskutil MountPoint field is present but is not text")

    if not isinstance(mount_records, Sequence) or isinstance(mount_records, (str, bytes)):
        raise ValueError("mounted-filesystem table is malformed")
    expected_source = f"/dev/{device_identifier}"
    normalized_expected_mount = os.path.normpath(expected_mount_location)
    normalized_rows: list[dict[str, Any]] = []
    for record in mount_records:
        if (not isinstance(record, Mapping)
                or not all(isinstance(record.get(key), str)
                           for key in ("source", "mount_point", "filesystem_type"))
                or not isinstance(record.get("filesystem_id"), Sequence)
                or isinstance(record.get("filesystem_id"), (str, bytes))
                or len(record["filesystem_id"]) != 2
                or any(not isinstance(part, int) or isinstance(part, bool)
                       or part < -(2**31) or part > 2**31 - 1
                       for part in record["filesystem_id"])):
            raise ValueError("mounted-filesystem table contains a malformed record")
        source = record["source"]
        raw_mount_point = record["mount_point"]
        mount_point = os.path.normpath(raw_mount_point)
        filesystem_type = record["filesystem_type"]
        if (not source or not mount_point.startswith("/")
                or raw_mount_point != mount_point or not filesystem_type):
            raise ValueError("mounted-filesystem table contains an empty identity field")
        normalized_rows.append({
            "source": source, "mount_point": mount_point,
            "filesystem_type": filesystem_type,
            "filesystem_id": [int(record["filesystem_id"][0]),
                               int(record["filesystem_id"][1])],
        })

    device_rows = [row for row in normalized_rows if row["source"] == expected_source]
    location_rows = [row for row in normalized_rows
                     if row["mount_point"] == normalized_expected_mount]
    if len(device_rows) > 1:
        raise RuntimeError("exact APFS BSD device has duplicate mounted-filesystem records")
    if len(location_rows) > 1:
        raise RuntimeError("expected APFS mount point has duplicate mounted-filesystem records")
    if location_rows and location_rows[0]["source"] != expected_source:
        raise RuntimeError("expected APFS mount point is occupied by another mounted filesystem")

    if device_rows:
        row = device_rows[0]
        if row["mount_point"] != normalized_expected_mount:
            raise RuntimeError("exact APFS device is mounted at an unexpected mount point")
        if row["filesystem_type"].lower() != "apfs":
            raise RuntimeError("exact APFS device is mounted with a non-APFS filesystem type")
        if mounted_key_present and mounted_value is not True:
            raise RuntimeError("diskutil Mounted=false contradicts the mounted-filesystem table")
        if (mount_point_key_present and diskutil_mount_point
                and os.path.normpath(diskutil_mount_point) != normalized_expected_mount):
            raise RuntimeError("diskutil MountPoint contradicts the mounted-filesystem table")
        if mount_point_key_present and not diskutil_mount_point:
            raise RuntimeError("diskutil reports an empty MountPoint for a mounted APFS volume")
        try:
            mount_stat = os.lstat(normalized_expected_mount)
        except OSError as error:
            raise RuntimeError(f"verified APFS mount point cannot be read: {error}") from error
        if stat.S_ISLNK(mount_stat.st_mode) or not stat.S_ISDIR(mount_stat.st_mode):
            raise RuntimeError("verified APFS mount point is not a direct directory")
        resolved_mount_point = os.path.realpath(normalized_expected_mount)
        if (resolved_mount_point != normalized_expected_mount
                or resolved_mount_point != expected_mount_location):
            raise RuntimeError("verified APFS mount point resolves through a substituted path")
        state = "MOUNTED_VERIFIED"
        selected_row = row
    else:
        if mounted_key_present and mounted_value is not False:
            raise RuntimeError("diskutil Mounted=true contradicts the unmounted-filesystem table")
        if (mount_point_key_present and diskutil_mount_point not in (None, "")):
            raise RuntimeError("diskutil MountPoint reports an active path absent from the mount table")
        state = "UNMOUNTED_VERIFIED"
        selected_row = None
        resolved_mount_point = None

    relevant_rows = [row for row in normalized_rows
                     if row["source"] == expected_source
                     or row["mount_point"] == normalized_expected_mount]
    table_sha256 = _sha256(policy._canonical_pretty_json_bytes(normalized_rows))
    return {
        "mount_state": state,
        "volume_uuid": expected_uuid,
        "device_identifier": device_identifier,
        "container_uuid": container_uuid.upper(),
        "container_reference": container_reference,
        "mount_table_device": selected_row["source"] if selected_row else None,
        "mount_table_mount_point": selected_row["mount_point"] if selected_row else None,
        "mount_table_filesystem_id": selected_row["filesystem_id"] if selected_row else None,
        "resolved_mount_point": resolved_mount_point,
        "mount_table_device_match_count": len(device_rows),
        "mount_table_expected_path_records": relevant_rows,
        "mount_table_sha256": table_sha256,
        "diskutil_mounted": mounted_value if mounted_key_present else None,
        "diskutil_mount_point": diskutil_mount_point if mount_point_key_present else None,
        "diskutil_mount_point_present": mount_point_key_present,
    }


def _read_apfs_mount_state(*, volume_uuid: str, device_identifier: str,
                           container_uuid: str, container_reference: str,
                           volume_name: str,
                           expected_mount_location: str) -> dict[str, Any]:
    info = _run_plist([_diskutil(), "info", "-plist", f"/dev/{device_identifier}"])
    return _resolve_apfs_mount_state(
        volume_uuid=volume_uuid, device_identifier=device_identifier,
        container_uuid=container_uuid, container_reference=container_reference,
        volume_name=volume_name, expected_mount_location=expected_mount_location,
        diskutil_info=info, mount_records=_mounted_filesystem_table())


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    raw = policy._read_file_nofollow(path, maximum_bytes=16 * 1024 * 1024)
    return _sha256(raw)


def _controller_identity() -> dict[str, str]:
    return {
        "vm_capacity_controller_sha256": _file_sha256(Path(__file__).resolve()),
        "storage_policy_sha256": _file_sha256(Path(policy.__file__).resolve()),
        "restore_metadata_probe_sha256": _file_sha256(VM_RESTORE_METADATA_PROBE_SOURCE),
    }


def verify_fresh_g0c_review() -> dict[str, Any]:
    """Require current controller/test hashes and the fresh independent G0C review."""
    import storage_gate0

    try:
        snapshot_raw = policy._read_file_nofollow(G0C_SNAPSHOT_PATH, maximum_bytes=4 * 1024 * 1024)
        review_raw = policy._read_file_nofollow(G0C_REVIEW_PATH, maximum_bytes=4 * 1024 * 1024)
        snapshot = json.loads(snapshot_raw)
        review = json.loads(review_raw)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"fresh G0C snapshot or independent review is unavailable: {error}") from error
    if not isinstance(snapshot, dict) or not isinstance(review, dict):
        raise RuntimeError("fresh G0C snapshot and review must both be JSON objects")
    snapshot_sha = _sha256(snapshot_raw)
    expected_required = dict(storage_gate0.G0C_REVIEW_REQUIRED_FILES)
    required = snapshot.get("review_contract", {}).get("required_fields")
    reviewed = snapshot.get("reviewed_controller_and_test_files")
    review_files = review.get("reviewed_files")
    blockers: list[str] = []
    if (snapshot.get("schema_version") != 1
            or snapshot.get("snapshot_id") != "G0C-INTERNAL-VM-CONTROLLER"):
        blockers.append("G0C snapshot identity or schema is invalid")
    if required != expected_required:
        blockers.append("G0C snapshot does not bind the current Gate 0 review contract")
    if not isinstance(reviewed, Mapping) or set(reviewed) != set(expected_required):
        blockers.append("G0C snapshot omits or adds controller/test bindings")
        reviewed = {}
    for field, relative_path in expected_required.items():
        path = policy.REPO / "experiments/storage" / relative_path
        record = reviewed.get(field) if isinstance(reviewed, Mapping) else None
        try:
            data = policy._read_file_nofollow(path)
        except OSError as error:
            blockers.append(f"G0C reviewed file is unreadable: {relative_path}: {error}")
            continue
        if (not isinstance(record, Mapping)
                or record.get("path") != path.relative_to(policy.REPO).as_posix()
                or record.get("sha256") != _sha256(data)
                or record.get("size_bytes") != len(data)):
            blockers.append(f"G0C snapshot does not bind current bytes: {relative_path}")
    runner = snapshot.get("separately_bound_files", {}).get("storage_smoke_runner")
    runner_path = policy.REPO / "experiments/storage/smoke_prefix_lease.py"
    if (not isinstance(runner, Mapping)
            or runner.get("path") != runner_path.relative_to(policy.REPO).as_posix()
            or runner.get("sha256") != _file_sha256(runner_path)):
        blockers.append("G0C snapshot does not bind the separate storage-smoke runner")
    suite = snapshot.get("full_suite_evidence")
    if (not isinstance(suite, Mapping) or not isinstance(suite.get("path"), str)
            or suite.get("result") != "PASS"):
        blockers.append("G0C snapshot lacks the requested full storage/Gate suite evidence")
    else:
        suite_path = Path(suite["path"])
        if not suite_path.is_absolute():
            suite_path = policy.REPO / suite_path
        try:
            if _file_sha256(suite_path) != suite.get("sha256"):
                blockers.append("G0C full-suite evidence hash does not match its current log")
        except OSError as error:
            blockers.append(f"G0C full-suite evidence is unreadable: {error}")
    focused = snapshot.get("focused_vm_policy_suite_evidence")
    if (not isinstance(focused, Mapping) or not isinstance(focused.get("path"), str)
            or focused.get("result") != "PASS"):
        blockers.append("G0C snapshot lacks passing focused VM metadata/transition test evidence")
    else:
        focused_path = Path(focused["path"])
        if not focused_path.is_absolute():
            focused_path = policy.REPO / focused_path
        try:
            focused_raw = policy._read_file_nofollow(focused_path, maximum_bytes=16 * 1024 * 1024)
            if (_sha256(focused_raw) != focused.get("sha256")
                    or len(focused_raw) != focused.get("size_bytes")):
                blockers.append("G0C focused VM test evidence hash or size differs from its log")
        except OSError as error:
            blockers.append(f"G0C focused VM test evidence is unreadable: {error}")
    if (review.get("status") != "PASS" or review.get("findings") != []
            or review.get("snapshot_sha256") != snapshot_sha
            or review.get("snapshot_path") != G0C_SNAPSHOT_PATH.relative_to(policy.REPO).as_posix()
            or review.get("model_identity") != "GPT-6 Luna Max"
            or review.get("reviewer_status") != "PASS"):
        blockers.append("fresh independent GPT-6 Luna Max G0C review is missing or does not PASS")
    if not isinstance(review_files, Mapping):
        blockers.append("fresh G0C reviewer file bindings are malformed")
        review_files = {}
    for field, relative_path in expected_required.items():
        record = review_files.get(field) if isinstance(review_files, Mapping) else None
        snap_record = reviewed.get(field) if isinstance(reviewed, Mapping) else None
        if (not isinstance(record, Mapping) or not isinstance(snap_record, Mapping)
                or record.get("path") != snap_record.get("path")
                or record.get("sha256") != snap_record.get("sha256")):
            blockers.append(f"fresh G0C review does not bind {relative_path}")
    if blockers:
        raise RuntimeError("current fresh G0C review verification failed: " + "; ".join(blockers))
    return {
        "snapshot_path": G0C_SNAPSHOT_PATH.relative_to(policy.REPO).as_posix(),
        "snapshot_sha256": snapshot_sha,
        "review_path": G0C_REVIEW_PATH.relative_to(policy.REPO).as_posix(),
        "review_sha256": _sha256(review_raw),
        "reviewer_identity": review.get("reviewer_identity"),
        "model_identity": review.get("model_identity"),
        "reviewed_file_count": len(expected_required),
    }


def _historical_g0c_pair(identity: Mapping[str, Any],
                         controller_identity: Mapping[str, Any]
                         ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], bool]:
    """Read and validate one P1-bound G0C pair; return its test and omission state."""
    required_identity_fields = {
        "snapshot_path", "snapshot_sha256", "review_path", "review_sha256",
        "reviewer_identity", "model_identity", "reviewed_file_count",
    }
    if (set(identity) != required_identity_fields
            or not isinstance(identity.get("snapshot_path"), str)
            or not isinstance(identity.get("review_path"), str)
            or identity.get("model_identity") != "GPT-6 Luna Max"
            or not isinstance(identity.get("reviewer_identity"), str)
            or not identity["reviewer_identity"].strip()
            or not _integer(identity.get("reviewed_file_count"), minimum=1)):
        raise RuntimeError("historical P1 G0C identity is malformed")
    snapshot_path, snapshot_path_text = _repo_artifact_path(identity.get("snapshot_path", ""))
    review_path, review_path_text = _repo_artifact_path(identity.get("review_path", ""))
    if (snapshot_path_text != identity.get("snapshot_path")
            or review_path_text != identity.get("review_path")):
        raise RuntimeError("historical P1 G0C path is not canonical")
    snapshot, _snapshot_raw, snapshot_sha, _ = _read_json_file_snapshot(snapshot_path)
    review, _review_raw, review_sha, _ = _read_json_file_snapshot(review_path)
    if (snapshot_sha != identity.get("snapshot_sha256")
            or review_sha != identity.get("review_sha256")
            or snapshot.get("schema_version") != 1
            or snapshot.get("snapshot_id") != "G0C-INTERNAL-VM-CONTROLLER"
            or review.get("status") != "PASS"
            or review.get("reviewer_status") != "PASS"
            or review.get("reviewer_identity") != identity.get("reviewer_identity")
            or review.get("model_identity") != "GPT-6 Luna Max"
            or review.get("snapshot_path") != snapshot_path_text
            or review.get("snapshot_sha256") != snapshot_sha):
        raise RuntimeError("historical P1 G0C snapshot/review is not the exact accepted PASS pair")
    reviewed = snapshot.get("reviewed_controller_and_test_files")
    reviewed_by_reviewer = review.get("reviewed_files")
    if (not isinstance(reviewed, Mapping)
            or not isinstance(reviewed_by_reviewer, Mapping)
            or reviewed_by_reviewer != reviewed
            or identity.get("reviewed_file_count") != len(reviewed)):
        raise RuntimeError("historical P1 G0C review does not bind the snapshot inventory")
    tests_file = reviewed.get("vm_capacity_tests")
    legacy_pair = (
        snapshot_path == VM_P1_R2_G0C_SNAPSHOT_PATH
        and snapshot_path_text == VM_P1_R2_G0C_SNAPSHOT_PATH.relative_to(policy.REPO).as_posix()
        and snapshot_sha == VM_P1_R2_G0C_SNAPSHOT_SHA256
        and review_path == VM_P1_R2_G0C_REVIEW_PATH
        and review_path_text == VM_P1_R2_G0C_REVIEW_PATH.relative_to(policy.REPO).as_posix()
        and review_sha == VM_P1_R2_G0C_REVIEW_SHA256
        and identity.get("reviewer_identity") == VM_P1_R2_G0C_REVIEWER_IDENTITY
        and identity.get("reviewed_file_count") == 12
        and dict(controller_identity) == VM_P1_R2_CONTROLLER_IDENTITY
    )
    source_identity = review.get("source_identity")
    if legacy_pair and "source_identity" not in review:
        expected_snapshot_keys = {
            "created_at_utc", "focused_vm_policy_suite_evidence", "full_suite_evidence",
            "metadata_p1_transition_source_review", "review_contract",
            "reviewed_controller_and_test_files", "schema_version", "separately_bound_files",
            "snapshot_id",
        }
        expected_review_keys = {
            "findings", "model_identity", "reviewed_files", "reviewer_identity",
            "reviewer_status", "snapshot_path", "snapshot_sha256", "status",
        }
        if (set(snapshot) != expected_snapshot_keys
                or set(review) != expected_review_keys
                or review.get("findings") != []
                or set(reviewed) != VM_P1_R2_REVIEWED_FILE_KEYS
                or not isinstance(tests_file, Mapping)
                or dict(tests_file) != VM_P1_R2_TEST_BINDING):
            raise RuntimeError("pinned historical R2 pair omits or changes a required identity field")
        expected_historical_controller_files = {
            "vm_capacity_controller": (
                "experiments/storage/vm_capacity_domain.py", "vm_capacity_controller_sha256"),
            "source": ("experiments/storage/storage_policy.py", "storage_policy_sha256"),
            "vm_restore_metadata_probe": (
                "experiments/storage/vm_restore_metadata_probe.swift",
                "restore_metadata_probe_sha256"),
        }
        for member, (expected_path, identity_field) in expected_historical_controller_files.items():
            source_file = reviewed.get(member)
            if (not isinstance(source_file, Mapping)
                    or source_file.get("path") != expected_path
                    or source_file.get("sha256") != controller_identity.get(identity_field)):
                raise RuntimeError(
                    "historical P1 G0C does not bind its accepted controller, policy, and probe")
        return snapshot, review, dict(tests_file), True

    p1_source = (source_identity.get("controller_test")
                 if isinstance(source_identity, Mapping) else None)
    if (not isinstance(p1_source, Mapping)
            or not isinstance(tests_file, Mapping)
            or p1_source.get("path") != tests_file.get("path")
            or p1_source.get("sha256") != tests_file.get("sha256")
            or not isinstance(reviewed.get("vm_capacity_controller"), Mapping)
            or reviewed["vm_capacity_controller"].get("sha256")
            != controller_identity.get("vm_capacity_controller_sha256")
            or not isinstance(reviewed.get("source"), Mapping)
            or reviewed["source"].get("sha256") != controller_identity.get("storage_policy_sha256")
            or not isinstance(reviewed.get("vm_restore_metadata_probe"), Mapping)
            or reviewed["vm_restore_metadata_probe"].get("sha256")
            != controller_identity.get("restore_metadata_probe_sha256")):
        raise RuntimeError("historical P1 G0C does not bind its accepted controller tests")
    return snapshot, review, dict(tests_file), False


def _legacy_ancestry_receipt_base(*, p1_path_text: str, p1_sha256: str,
                                  p1_review_sha256: str,
                                  identity: Mapping[str, Any],
                                  historical_controller_identity: Mapping[str, Any],
                                  historical_test_binding: Mapping[str, Any],
                                  issuance_controller_identity: Mapping[str, Any],
                                  issuance_g0c_identity: Mapping[str, Any],
                                  created_at_unix: float) -> dict[str, Any]:
    """Reconstruct the immutable receipt using issuance-time, never present, identities."""
    return {
        "schema_version": 1,
        "record_type": VM_P1_G0C_ANCESTRY_RECEIPT_TYPE,
        "receipt_path": VM_P1_G0C_ANCESTRY_RECEIPT_PATH.relative_to(policy.REPO).as_posix(),
        "review_path": VM_P1_G0C_ANCESTRY_REVIEW_PATH.relative_to(policy.REPO).as_posix(),
        "status": "COMPLETE",
        "scope": VM_P1_G0C_ANCESTRY_RECEIPT_SCOPE,
        "compatibility_rule": VM_P1_G0C_ANCESTRY_RULE,
        "created_at_unix": created_at_unix,
        "p1_binding": {
            "admission_path": p1_path_text,
            "admission_sha256": p1_sha256,
            "review_path": VM_P1_ACCEPTED_REVIEW_PATH.relative_to(policy.REPO).as_posix(),
            "review_sha256": p1_review_sha256,
        },
        "g0c_binding": {
            "snapshot_path": identity["snapshot_path"],
            "snapshot_sha256": identity["snapshot_sha256"],
            "review_path": identity["review_path"],
            "review_sha256": identity["review_sha256"],
            "snapshot_authentication": "EXACT_HASH_BOUND_BY_SIGNED_P1_ADMISSION",
            "review_authentication": "EXACT_HASH_BOUND_BY_SIGNED_P1_ADMISSION",
            "reviewer_identity": identity["reviewer_identity"],
            "model_identity": identity["model_identity"],
            "reviewed_file_count": identity["reviewed_file_count"],
        },
        "omission": {
            "review_member": "/source_identity/controller_test",
            "source_identity_member_present": False,
            "controller_test_member_present": False,
            "state": "ABSENT_FROM_PINNED_REVIEW_BYTES",
            "scope": "EXACT_R2_P1_REVIEW_ONLY",
        },
        "legacy_test_binding": {
            "snapshot_member": "reviewed_controller_and_test_files.vm_capacity_tests",
            "review_member": "reviewed_files.vm_capacity_tests",
            "path": historical_test_binding["path"],
            "sha256": historical_test_binding["sha256"],
            "size_bytes": historical_test_binding["size_bytes"],
            "review_match": "EXACT_PATH_SHA256_SIZE_MATCH",
        },
        "historical_controller_identity": dict(historical_controller_identity),
        # In schema v1 these keys mean the controller/G0C at receipt issuance.
        "current_controller_identity": dict(issuance_controller_identity),
        "current_g0c_identity": dict(issuance_g0c_identity),
        "authority": {
            "scope": "NONE",
            "p1_authorized": False,
            "restore_download_authorized": False,
            "p2_authorized": False,
            "vm_start_authorized": False,
        },
    }


def _verify_ancestry_issuance_g0c(receipt: Mapping[str, Any]) -> None:
    """Verify schema-v1 current_* fields against the exact R4 issuance evidence."""
    if (receipt.get("current_controller_identity")
            != VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY
            or receipt.get("current_g0c_identity")
            != VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY):
        raise RuntimeError("historical completion issuance-time controller/G0C identity changed")
    identity = VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY
    snapshot_path, snapshot_path_text = _repo_artifact_path(identity["snapshot_path"])
    review_path, review_path_text = _repo_artifact_path(identity["review_path"])
    snapshot, _snapshot_raw, snapshot_sha, _ = _read_json_file_snapshot(snapshot_path)
    review, _review_raw, review_sha, _ = _read_json_file_snapshot(review_path)
    expected_snapshot_fields = {
        "created_at_utc", "direct_full_suite_diagnostic", "focused_vm_policy_suite_evidence",
        "full_suite_evidence", "historical_ancestry_focused_suite_evidence", "isolation_note",
        "p1_freshness_renewal_source_review", "p1_renewal_suite_evidence",
        "prior_p1_renewal_review", "review_contract", "reviewed_controller_and_test_files",
        "schema_version", "separately_bound_files", "snapshot_id",
    }
    expected_review_fields = {
        "findings", "model_identity", "record_type", "reviewed_files", "reviewer_identity",
        "reviewer_status", "schema_version", "snapshot_path", "snapshot_sha256", "status",
    }
    reviewed = snapshot.get("reviewed_controller_and_test_files")
    if (snapshot_sha != identity["snapshot_sha256"]
            or review_sha != identity["review_sha256"]
            or snapshot_path_text != identity["snapshot_path"]
            or review_path_text != identity["review_path"]
            or set(snapshot) != expected_snapshot_fields
            or snapshot.get("schema_version") != 1
            or snapshot.get("snapshot_id") != "G0C-INTERNAL-VM-CONTROLLER"
            or not isinstance(reviewed, Mapping)
            or len(reviewed) != identity["reviewed_file_count"]
            or set(review) != expected_review_fields
            or review.get("schema_version") != 1
            or review.get("record_type") != "R6_VM_P1_HISTORICAL_ANCESTRY_FRESH_G0C_REVIEW"
            or review.get("status") != "PASS"
            or review.get("reviewer_status") != "PASS"
            or review.get("findings") != []
            or review.get("reviewer_identity") != identity["reviewer_identity"]
            or review.get("model_identity") != identity["model_identity"]
            or review.get("snapshot_path") != identity["snapshot_path"]
            or review.get("snapshot_sha256") != identity["snapshot_sha256"]
            or review.get("reviewed_files") != reviewed):
        raise RuntimeError("historical completion issuance-time R4 G0C pair is not the exact PASS pair")
    expected_controller_files = {
        "vm_capacity_controller": (
            "experiments/storage/vm_capacity_domain.py",
            VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY["vm_capacity_controller_sha256"]),
        "source": (
            "experiments/storage/storage_policy.py",
            VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY["storage_policy_sha256"]),
        "vm_restore_metadata_probe": (
            "experiments/storage/vm_restore_metadata_probe.swift",
            VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY["restore_metadata_probe_sha256"]),
    }
    for member, (expected_path, expected_sha256) in expected_controller_files.items():
        entry = reviewed.get(member)
        if (not isinstance(entry, Mapping)
                or entry.get("path") != expected_path
                or entry.get("sha256") != expected_sha256
                or not _integer(entry.get("size_bytes"), minimum=1)):
            raise RuntimeError("historical completion R4 inventory does not bind its issuer controller")
    if reviewed.get("vm_capacity_tests") != VM_P1_G0C_ANCESTRY_ISSUANCE_TEST_BINDING:
        raise RuntimeError("historical completion R4 inventory does not bind its issuer test identity")
    snapshot_created = snapshot.get("created_at_utc")
    receipt_created = receipt.get("created_at_unix")
    if (not isinstance(snapshot_created, str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", snapshot_created)
            or not isinstance(receipt_created, (int, float))
            or snapshot_created > time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(receipt_created))):
        raise RuntimeError("historical completion issuance G0C postdates its receipt")


def _verify_legacy_ancestry_review(receipt: Mapping[str, Any], receipt_sha256: str) -> str:
    try:
        review, _raw, review_sha256, _ = _read_json_file_snapshot(
            VM_P1_G0C_ANCESTRY_REVIEW_PATH)
    except OSError as error:
        raise RuntimeError("historical G0C ancestry completion independent review is unavailable") from error
    required_fields = {
        "schema_version", "record_type", "scope", "status", "reviewer_identity",
        "model_identity", "reviewed_at_unix", "reviewed_artifact_path",
        "reviewed_artifact_sha256", "findings",
    }
    receipt_path = VM_P1_G0C_ANCESTRY_RECEIPT_PATH.relative_to(policy.REPO).as_posix()
    if (review_sha256 != VM_P1_G0C_ANCESTRY_REVIEW_SHA256
            or set(review) != required_fields
            or review.get("schema_version") != 1
            or review.get("record_type") != "R6_VM_P1_HISTORICAL_G0C_ANCESTRY_COMPLETION_REVIEW"
            or review.get("scope") != VM_P1_G0C_ANCESTRY_REVIEW_SCOPE
            or review.get("status") != "PASS"
            or review.get("reviewer_identity") != VM_P1_G0C_ANCESTRY_REVIEWER_IDENTITY
            or review.get("model_identity") != "GPT-6 Luna Max"
            or review.get("reviewed_artifact_path") != receipt_path
            or review.get("reviewed_artifact_sha256") != receipt_sha256
            or review.get("findings") != []):
        raise RuntimeError("historical G0C ancestry completion lacks its exact independent PASS review")
    reviewed_at = _require_nonfuture_timestamp(
        review.get("reviewed_at_unix"), label="historical ancestry completion review")
    receipt_at = _require_nonfuture_timestamp(
        receipt.get("created_at_unix"), label="historical ancestry completion receipt")
    if reviewed_at < receipt_at:
        raise RuntimeError("historical ancestry completion review predates its exact receipt")
    return review_sha256


def _verify_legacy_ancestry_receipt(*, p1_path_text: str, p1_sha256: str,
                                    p1_review_sha256: str,
                                    identity: Mapping[str, Any],
                                    historical_controller_identity: Mapping[str, Any],
                                    historical_test_binding: Mapping[str, Any]
                                    ) -> tuple[dict[str, Any], str, str]:
    """Verify the immutable completion only against historical/issuance-time authority."""
    try:
        receipt, _raw, receipt_sha256, _ = _read_signed_record_snapshot(
            VM_P1_G0C_ANCESTRY_RECEIPT_PATH, VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
    except OSError as error:
        raise RuntimeError("historical G0C ancestry completion receipt is unavailable") from error
    if receipt_sha256 != VM_P1_G0C_ANCESTRY_RECEIPT_SHA256:
        raise RuntimeError("historical G0C ancestry completion receipt bytes differ from the pinned anchor")
    created_at = _require_nonfuture_timestamp(
        receipt.get("created_at_unix"), label="historical ancestry completion receipt")
    expected = _legacy_ancestry_receipt_base(
        p1_path_text=p1_path_text, p1_sha256=p1_sha256,
        p1_review_sha256=p1_review_sha256, identity=identity,
        historical_controller_identity=historical_controller_identity,
        historical_test_binding=historical_test_binding,
        issuance_controller_identity=VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY,
        issuance_g0c_identity=VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY,
        created_at_unix=created_at)
    expected[VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD] = receipt.get(
        VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
    if receipt != expected:
        raise RuntimeError("historical G0C ancestry completion receipt has an unexpected historical binding or schema")
    _verify_ancestry_issuance_g0c(receipt)
    review_sha256 = _verify_legacy_ancestry_review(receipt, receipt_sha256)
    return receipt, receipt_sha256, review_sha256


def _historical_ancestry_anchor(receipt: Mapping[str, Any], receipt_sha256: str,
                                review_sha256: str) -> dict[str, Any]:
    """Return the exact historical tuple carried forward by compatibility transitions."""
    return {
        "completion_receipt": {
            "path": VM_P1_G0C_ANCESTRY_RECEIPT_PATH.relative_to(policy.REPO).as_posix(),
            "sha256": receipt_sha256,
        },
        "completion_review": {
            "path": VM_P1_G0C_ANCESTRY_REVIEW_PATH.relative_to(policy.REPO).as_posix(),
            "sha256": review_sha256,
        },
        "p1_binding": dict(receipt["p1_binding"]),
        "r2_g0c_binding": dict(receipt["g0c_binding"]),
        "omission": dict(receipt["omission"]),
        "legacy_test_binding": dict(receipt["legacy_test_binding"]),
        "historical_controller_identity": dict(receipt["historical_controller_identity"]),
        "issuance_controller_identity": dict(receipt["current_controller_identity"]),
        "issuance_g0c_identity": dict(receipt["current_g0c_identity"]),
    }


def _accepted_historical_ancestry_anchor() -> dict[str, Any]:
    """Load the exact P1/R2 completion anchor without consulting present identity."""
    p1_path_text = VM_P1_ACCEPTED_ADMISSION_PATH.relative_to(policy.REPO).as_posix()
    admission, _admission_raw, admission_sha256, _ = _read_signed_record_snapshot(
        VM_P1_ACCEPTED_ADMISSION_PATH, "vm_admission_hmac_sha256")
    if (admission_sha256 != VM_P1_ACCEPTED_ADMISSION_SHA256
            or admission.get("receipt_path") != p1_path_text
            or admission.get("record_type") != "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION"
            or admission.get("decision") != "PASS"
            or admission.get("authorization") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or admission.get("authorized_phase") != "P1"
            or admission.get("p1_authorized") is not True
            or admission.get("restore_download_authorized") is not True
            or admission.get("p2_authorized") is not False
            or admission.get("vm_start_authorized") is not False):
        raise RuntimeError("compatibility transition requires the exact signed accepted P1 admission")
    review_path_text = admission.get("required_independent_review_path")
    if review_path_text != VM_P1_ACCEPTED_REVIEW_PATH.relative_to(policy.REPO).as_posix():
        raise RuntimeError("compatibility transition requires the exact accepted P1 review path")
    p1_review, p1_review_sha256 = _verify_review_artifact(
        VM_P1_ACCEPTED_REVIEW_PATH, artifact_path=p1_path_text,
        artifact_sha256=admission_sha256, not_before_unix=admission.get("created_at_unix"))
    if (p1_review_sha256 != VM_P1_ACCEPTED_REVIEW_SHA256
            or p1_review.get("findings") != []):
        raise RuntimeError("compatibility transition requires the exact pinned P1 review bytes")
    identity = admission.get("g0c_identity")
    historical_controller = admission.get("controller_identity")
    if not isinstance(identity, Mapping) or not isinstance(historical_controller, Mapping):
        raise RuntimeError("accepted P1 admission omits historical controller/G0C identity")
    _snapshot, _review, historical_test, omission = _historical_g0c_pair(
        identity, historical_controller)
    if (not omission or dict(historical_controller) != VM_P1_R2_CONTROLLER_IDENTITY):
        raise RuntimeError("compatibility transition is outside the exact eligible historical R2 omission")
    receipt, receipt_sha256, review_sha256 = _verify_legacy_ancestry_receipt(
        p1_path_text=p1_path_text, p1_sha256=admission_sha256,
        p1_review_sha256=p1_review_sha256, identity=identity,
        historical_controller_identity=historical_controller,
        historical_test_binding=historical_test)
    return _historical_ancestry_anchor(receipt, receipt_sha256, review_sha256)


def _current_g0c_transition_files(
        controller_identity: Mapping[str, Any], g0c_identity: Mapping[str, Any]
        ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read the current G0C inventory and return its exact controller/test records."""
    snapshot_path, snapshot_path_text = _repo_artifact_path(g0c_identity.get("snapshot_path", ""))
    review_path, review_path_text = _repo_artifact_path(g0c_identity.get("review_path", ""))
    if (snapshot_path != G0C_SNAPSHOT_PATH or review_path != G0C_REVIEW_PATH
            or snapshot_path_text != G0C_SNAPSHOT_PATH.relative_to(policy.REPO).as_posix()
            or review_path_text != G0C_REVIEW_PATH.relative_to(policy.REPO).as_posix()):
        raise RuntimeError("current transition G0C paths differ from the reviewed current G0C paths")
    snapshot, _snapshot_raw, snapshot_sha256, _ = _read_json_file_snapshot(snapshot_path)
    review, _review_raw, review_sha256, _ = _read_json_file_snapshot(review_path)
    if (snapshot_sha256 != g0c_identity.get("snapshot_sha256")
            or review_sha256 != g0c_identity.get("review_sha256")
            or review.get("snapshot_path") != snapshot_path_text
            or review.get("snapshot_sha256") != snapshot_sha256
            or review.get("status") != "PASS"
            or review.get("reviewer_status") != "PASS"
            or review.get("findings") != []):
        raise RuntimeError("current compatibility transition G0C bytes or independent review changed")
    reviewed = snapshot.get("reviewed_controller_and_test_files")
    if not isinstance(reviewed, Mapping):
        raise RuntimeError("current transition G0C has no reviewed file inventory")
    controller_fields = {
        "vm_capacity_controller": (
            "experiments/storage/vm_capacity_domain.py", "vm_capacity_controller_sha256"),
        "source": ("experiments/storage/storage_policy.py", "storage_policy_sha256"),
        "vm_restore_metadata_probe": (
            "experiments/storage/vm_restore_metadata_probe.swift", "restore_metadata_probe_sha256"),
    }
    current_controller_files: dict[str, dict[str, Any]] = {}
    for member, (expected_path, identity_field) in controller_fields.items():
        entry = reviewed.get(member)
        if (not isinstance(entry, Mapping)
                or entry.get("path") != expected_path
                or entry.get("sha256") != controller_identity.get(identity_field)
                or not _integer(entry.get("size_bytes"), minimum=1)):
            raise RuntimeError("current transition G0C does not bind the exact controller source bytes")
        current_controller_files[member] = dict(entry)
    current_test = reviewed.get("vm_capacity_tests")
    expected_test_path = "experiments/storage/test_vm_capacity_domain.py"
    if (not isinstance(current_test, Mapping)
            or current_test.get("path") != expected_test_path
            or not re.fullmatch(r"[0-9a-f]{64}", str(current_test.get("sha256", "")))
            or not _integer(current_test.get("size_bytes"), minimum=1)):
        raise RuntimeError("current transition G0C does not bind the exact controller test file")
    test_raw = policy._read_file_nofollow(policy.REPO / expected_test_path)
    if (_sha256(test_raw) != current_test.get("sha256")
            or len(test_raw) != current_test.get("size_bytes")):
        raise RuntimeError("current transition test identity differs from its present bytes")
    review_files = review.get("reviewed_files")
    if (not isinstance(review_files, Mapping)
            or review_files.get("vm_capacity_tests") != dict(current_test)):
        raise RuntimeError("current transition G0C review does not bind the current test identity")
    return current_controller_files, dict(current_test)


def _transition_identity_shape(value: Any) -> bool:
    required = {
        "vm_capacity_controller_sha256", "storage_policy_sha256", "restore_metadata_probe_sha256",
    }
    return (isinstance(value, Mapping) and set(value) == required
            and all(isinstance(value.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in required))


def _transition_g0c_shape(value: Any) -> bool:
    required = {
        "snapshot_path", "snapshot_sha256", "review_path", "review_sha256",
        "reviewer_identity", "model_identity", "reviewed_file_count",
    }
    if (not isinstance(value, Mapping) or set(value) != required
            or not isinstance(value.get("snapshot_path"), str)
            or not isinstance(value.get("review_path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("snapshot_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("review_sha256", "")))
            or not isinstance(value.get("reviewer_identity"), str)
            or not value.get("reviewer_identity", "").strip()
            or value.get("model_identity") != "GPT-6 Luna Max"
            or not _integer(value.get("reviewed_file_count"), minimum=1)):
        return False
    try:
        snapshot_path, snapshot_path_text = _repo_artifact_path(value["snapshot_path"])
        review_path, review_path_text = _repo_artifact_path(value["review_path"])
    except (RuntimeError, TypeError, ValueError):
        return False
    return (snapshot_path_text == value["snapshot_path"]
            and review_path_text == value["review_path"]
            and snapshot_path != review_path)


def _validate_ancestry_transition_record(record: Mapping[str, Any], *,
                                         path_text: str, digest: str,
                                         anchor: Mapping[str, Any]) -> int:
    required_fields = {
        "schema_version", "record_type", "transition_path", "status", "transition_scope",
        "compatibility_rule_version", "sequence", "created_at_unix", "historical_anchor",
        "predecessor", "previous_controller_identity", "previous_g0c_identity",
        "current_controller_identity", "current_controller_files", "current_test_identity",
        "current_g0c_identity", "semantics", "authority",
        VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD,
    }
    expected_semantics = {
        "historical_artifacts_unchanged": True,
        "historical_fact_reinterpreted": False,
        "explicit_current_controller_acceptance": True,
    }
    expected_authority = {
        "scope": "NONE", "p1_authorized": False,
        "restore_download_authorized": False, "p2_authorized": False,
        "vm_start_authorized": False,
    }
    if (set(record) != required_fields
            or record.get("schema_version") != 1
            or record.get("record_type") != VM_P1_G0C_ANCESTRY_TRANSITION_TYPE
            or record.get("transition_path") != path_text
            or record.get("status") != "ACCEPTED"
            or record.get("transition_scope") != VM_P1_G0C_ANCESTRY_TRANSITION_SCOPE
            or record.get("compatibility_rule_version") != VM_P1_G0C_ANCESTRY_TRANSITION_RULE
            or record.get("historical_anchor") != anchor
            or record.get("semantics") != expected_semantics
            or record.get("authority") != expected_authority
            or not _transition_identity_shape(record.get("previous_controller_identity"))
            or not _transition_g0c_shape(record.get("previous_g0c_identity"))
            or not _transition_identity_shape(record.get("current_controller_identity"))
            or not _transition_g0c_shape(record.get("current_g0c_identity"))):
        raise RuntimeError("compatibility transition has an unsupported schema, anchor, or authority scope")
    sequence = record.get("sequence")
    if not _integer(sequence, minimum=1):
        raise RuntimeError("compatibility transition sequence is invalid")
    _require_nonfuture_timestamp(record.get("created_at_unix"), label="compatibility transition")
    predecessor = record.get("predecessor")
    if (not isinstance(predecessor, Mapping)
            or set(predecessor) != {"kind", "path", "sha256"}
            or not isinstance(predecessor.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(predecessor.get("sha256", "")))):
        raise RuntimeError("compatibility transition predecessor is malformed")
    if not isinstance(record.get("current_controller_files"), Mapping):
        raise RuntimeError("compatibility transition current controller inventory is malformed")
    files = record["current_controller_files"]
    file_fields = {
        "vm_capacity_controller": ("experiments/storage/vm_capacity_domain.py",
                                   "vm_capacity_controller_sha256"),
        "source": ("experiments/storage/storage_policy.py", "storage_policy_sha256"),
        "vm_restore_metadata_probe": ("experiments/storage/vm_restore_metadata_probe.swift",
                                      "restore_metadata_probe_sha256"),
    }
    if set(files) != set(file_fields):
        raise RuntimeError("compatibility transition current controller inventory is not closed")
    for member, (expected_path, identity_field) in file_fields.items():
        entry = files.get(member)
        if (not isinstance(entry, Mapping)
                or set(entry) != {"path", "sha256", "size_bytes"}
                or entry.get("path") != expected_path
                or entry.get("sha256") != record["current_controller_identity"].get(identity_field)
                or not _integer(entry.get("size_bytes"), minimum=1)):
            raise RuntimeError("compatibility transition controller file binding is inconsistent")
    test = record.get("current_test_identity")
    if (not isinstance(test, Mapping)
            or set(test) != {"path", "sha256", "size_bytes"}
            or test.get("path") != "experiments/storage/test_vm_capacity_domain.py"
            or not re.fullmatch(r"[0-9a-f]{64}", str(test.get("sha256", "")))
            or not _integer(test.get("size_bytes"), minimum=1)):
        raise RuntimeError("compatibility transition current test binding is malformed")
    return sequence


def _read_ancestry_transition_chain(anchor: Mapping[str, Any], *, tip_path: Path,
                                    require_current_tip: bool
                                    ) -> list[tuple[dict[str, Any], str, str]]:
    """Read a bounded append-only chain and validate all predecessor links."""
    records: list[tuple[dict[str, Any], str, str]] = []
    seen: set[str] = set()
    selected = Path(tip_path)
    while True:
        selected, path_text = _repo_artifact_path(selected)
        if (selected.parent != policy.REPO
                or not selected.name.startswith("R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-")
                or not selected.name.endswith(".json")):
            raise RuntimeError("compatibility transition path is not a canonical chain record")
        if path_text in seen:
            raise RuntimeError("compatibility transition chain contains a path cycle")
        if len(records) >= VM_P1_G0C_ANCESTRY_TRANSITION_MAX_CHAIN_LENGTH:
            raise RuntimeError("compatibility transition chain exceeds its reviewed bound")
        seen.add(path_text)
        record, _raw, digest, _ = _read_signed_record_snapshot(
            selected, VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        sequence = _validate_ancestry_transition_record(
            record, path_text=path_text, digest=digest, anchor=anchor)
        records.append((record, digest, path_text))
        predecessor = record["predecessor"]
        if sequence == 1:
            if (predecessor.get("kind") != "HISTORICAL_COMPLETION_RECEIPT"
                    or predecessor.get("path") != anchor["completion_receipt"]["path"]
                    or predecessor.get("sha256") != anchor["completion_receipt"]["sha256"]
                    or record["previous_controller_identity"]
                    != anchor["issuance_controller_identity"]
                    or record["previous_g0c_identity"] != anchor["issuance_g0c_identity"]):
                raise RuntimeError("first compatibility transition does not start at the exact historical issuance")
            break
        if predecessor.get("kind") != "HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION":
            raise RuntimeError("compatibility transition chain has a missing or downgraded predecessor")
        parent_path, parent_path_text = _repo_artifact_path(predecessor.get("path", ""))
        if (parent_path_text != predecessor.get("path")
                or parent_path == selected or parent_path_text in seen):
            raise RuntimeError("compatibility transition predecessor path is noncanonical or cyclic")
        selected = parent_path

    ordered = list(reversed(records))
    if len(ordered) != ordered[-1][0]["sequence"]:
        raise RuntimeError("compatibility transition chain has a sequence gap or rollback")
    for index, (record, digest, path_text) in enumerate(ordered):
        if record["sequence"] != index + 1:
            raise RuntimeError("compatibility transition chain sequence is not monotonic")
        if index == 0:
            if (record["current_controller_identity"] == record["previous_controller_identity"]
                    and record["current_g0c_identity"] == record["previous_g0c_identity"]):
                raise RuntimeError("initial compatibility transition does not bind a new reviewed identity")
            continue
        previous, previous_digest, previous_path_text = ordered[index - 1]
        if (record["predecessor"] != {
                "kind": "HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION",
                "path": previous_path_text, "sha256": previous_digest}
                or record["previous_controller_identity"] != previous["current_controller_identity"]
                or record["previous_g0c_identity"] != previous["current_g0c_identity"]
                or record["created_at_unix"] <= previous["created_at_unix"]
                or (record["current_controller_identity"] == record["previous_controller_identity"]
                    and record["current_g0c_identity"] == record["previous_g0c_identity"])):
            raise RuntimeError("compatibility transition does not monotonically extend its exact predecessor")
    if require_current_tip:
        tip, _tip_digest, tip_path_text = records[0]
        expected_tip_text = VM_P1_G0C_ANCESTRY_TRANSITION_PATH.relative_to(policy.REPO).as_posix()
        expected_predecessor = VM_P1_G0C_ANCESTRY_TRANSITION_PREDECESSOR_PATH
        expected_predecessor_text = (
            expected_predecessor.relative_to(policy.REPO).as_posix()
            if expected_predecessor is not None
            else anchor["completion_receipt"]["path"])
        actual_predecessor_text = tip["predecessor"].get("path")
        if (tip_path_text != expected_tip_text
                or actual_predecessor_text != expected_predecessor_text):
            raise RuntimeError("compatibility transition tip or predecessor differs from reviewed source")
    return records


def _ancestry_transition_base(*, anchor: Mapping[str, Any], sequence: int,
                              predecessor: Mapping[str, Any],
                              previous_controller_identity: Mapping[str, Any],
                              previous_g0c_identity: Mapping[str, Any],
                              current_controller_identity: Mapping[str, Any],
                              current_controller_files: Mapping[str, Mapping[str, Any]],
                              current_test_identity: Mapping[str, Any],
                              current_g0c_identity: Mapping[str, Any],
                              created_at_unix: float) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": VM_P1_G0C_ANCESTRY_TRANSITION_TYPE,
        "transition_path": VM_P1_G0C_ANCESTRY_TRANSITION_PATH.relative_to(policy.REPO).as_posix(),
        "status": "ACCEPTED",
        "transition_scope": VM_P1_G0C_ANCESTRY_TRANSITION_SCOPE,
        "compatibility_rule_version": VM_P1_G0C_ANCESTRY_TRANSITION_RULE,
        "sequence": sequence,
        "created_at_unix": created_at_unix,
        "historical_anchor": dict(anchor),
        "predecessor": dict(predecessor),
        "previous_controller_identity": dict(previous_controller_identity),
        "previous_g0c_identity": dict(previous_g0c_identity),
        "current_controller_identity": dict(current_controller_identity),
        "current_controller_files": {
            key: dict(value) for key, value in current_controller_files.items()},
        "current_test_identity": dict(current_test_identity),
        "current_g0c_identity": dict(current_g0c_identity),
        "semantics": {
            "historical_artifacts_unchanged": True,
            "historical_fact_reinterpreted": False,
            "explicit_current_controller_acceptance": True,
        },
        "authority": {
            "scope": "NONE",
            "p1_authorized": False,
            "restore_download_authorized": False,
            "p2_authorized": False,
            "vm_start_authorized": False,
        },
    }


def create_historical_g0c_ancestry_compatibility_transition() -> dict[str, Any]:
    """Append one signed current-controller bridge without rewriting historical evidence."""
    anchor = _accepted_historical_ancestry_anchor()
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    current_controller_files, current_test = _current_g0c_transition_files(
        current_controller, current_g0c)
    predecessor_path = VM_P1_G0C_ANCESTRY_TRANSITION_PREDECESSOR_PATH
    if predecessor_path is None:
        receipt, _receipt_raw, receipt_sha256, _ = _read_signed_record_snapshot(
            VM_P1_G0C_ANCESTRY_RECEIPT_PATH, VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        sequence = 1
        predecessor = {
            "kind": "HISTORICAL_COMPLETION_RECEIPT",
            "path": anchor["completion_receipt"]["path"],
            "sha256": receipt_sha256,
        }
        previous_controller = dict(receipt["current_controller_identity"])
        previous_g0c = dict(receipt["current_g0c_identity"])
    else:
        prior_chain = _read_ancestry_transition_chain(
            anchor, tip_path=predecessor_path, require_current_tip=False)
        prior, prior_sha, prior_path_text = prior_chain[0]
        sequence = prior["sequence"] + 1
        predecessor = {
            "kind": "HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION",
            "path": prior_path_text,
            "sha256": prior_sha,
        }
        previous_controller = dict(prior["current_controller_identity"])
        previous_g0c = dict(prior["current_g0c_identity"])
    base = _ancestry_transition_base(
        anchor=anchor, sequence=sequence, predecessor=predecessor,
        previous_controller_identity=previous_controller,
        previous_g0c_identity=previous_g0c,
        current_controller_identity=current_controller,
        current_controller_files=current_controller_files,
        current_test_identity=current_test, current_g0c_identity=current_g0c,
        created_at_unix=time.time())
    sealed = _write_signed_json_unique(
        VM_P1_G0C_ANCESTRY_TRANSITION_PATH, base,
        VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
    _verify_ancestry_transition_chain(
        anchor, current_controller_identity=current_controller,
        current_g0c_identity=current_g0c)
    return sealed


def _verify_ancestry_transition_chain(anchor: Mapping[str, Any], *,
                                      current_controller_identity: Mapping[str, Any],
                                      current_g0c_identity: Mapping[str, Any]) -> None:
    if (dict(current_controller_identity) != _controller_identity()
            or dict(current_g0c_identity) != verify_fresh_g0c_review()):
        raise RuntimeError("compatibility transition current identities are not the present reviewed identities")
    current_controller_files, current_test = _current_g0c_transition_files(
        current_controller_identity, current_g0c_identity)
    chain = _read_ancestry_transition_chain(
        anchor, tip_path=VM_P1_G0C_ANCESTRY_TRANSITION_PATH, require_current_tip=True)
    tip, _digest, _path_text = chain[0]
    if (tip.get("current_controller_identity") != dict(current_controller_identity)
            or tip.get("current_controller_files") != current_controller_files
            or tip.get("current_test_identity") != current_test
            or tip.get("current_g0c_identity") != dict(current_g0c_identity)):
        raise RuntimeError("compatibility transition does not bind the exact present controller, tests, and G0C")


def _verify_historical_g0c_identity(
        identity: Mapping[str, Any], controller_identity: Mapping[str, Any], *,
        p1_path_text: str | None = None, p1_sha256: str | None = None,
        p1_review_sha256: str | None = None,
        current_controller_identity: Mapping[str, Any] | None = None,
        current_g0c_identity: Mapping[str, Any] | None = None) -> None:
    """Verify accepted historical G0C, bridging only the pinned R2 field omission."""
    snapshot, review, historical_test, legacy_omission = _historical_g0c_pair(
        identity, controller_identity)
    if not legacy_omission:
        return
    expected_p1_path = VM_P1_ACCEPTED_ADMISSION_PATH.relative_to(policy.REPO).as_posix()
    if (p1_path_text != expected_p1_path
            or p1_sha256 != VM_P1_ACCEPTED_ADMISSION_SHA256
            or p1_review_sha256 != VM_P1_ACCEPTED_REVIEW_SHA256
            or dict(controller_identity) != VM_P1_R2_CONTROLLER_IDENTITY
            or current_controller_identity is None
            or current_g0c_identity is None):
        raise RuntimeError("legacy historical G0C omission is not bound to the exact accepted P1")
    receipt, receipt_sha256, review_sha256 = _verify_legacy_ancestry_receipt(
        p1_path_text=p1_path_text, p1_sha256=p1_sha256,
        p1_review_sha256=p1_review_sha256, identity=identity,
        historical_controller_identity=controller_identity,
        historical_test_binding=historical_test)
    anchor = _historical_ancestry_anchor(receipt, receipt_sha256, review_sha256)
    _verify_ancestry_transition_chain(
        anchor, current_controller_identity=current_controller_identity,
        current_g0c_identity=current_g0c_identity)


def _run_plist(command: Sequence[str], *, timeout: float = 20.0) -> dict[str, Any]:
    completed = subprocess.run(
        list(command), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout,
    )
    value = plistlib.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"property-list command returned a non-dictionary: {command[0]}")
    return value


def _diskutil() -> str:
    path = shutil.which("diskutil")
    if path is None:
        raise RuntimeError("diskutil is unavailable")
    return path


def _host_build_version() -> str:
    completed = subprocess.run(
        ["/usr/bin/sw_vers", "-buildVersion"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5.0)
    build = completed.stdout.strip()
    if not build:
        raise RuntimeError("macOS host build version is unavailable")
    return build


def _path_entry_is_absent(path: Path) -> bool:
    """Distinguish a missing profile from a present but invalid path without following links."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _capture_volume_info(container: Mapping[str, Any], row: Mapping[str, Any],
                         mount_records: Sequence[Mapping[str, Any]], *,
                         expected_mount_location: str | None = None) -> dict[str, Any]:
    """Capture diskutil identity and canonical kernel-backed mount state."""
    device = row.get("DeviceIdentifier")
    if not isinstance(device, str) or not re.fullmatch(r"disk[0-9]+s[0-9]+", device):
        raise RuntimeError("APFS volume device identifier is malformed")
    volume_uuid = row.get("APFSVolumeUUID")
    container_uuid = container.get("APFSContainerUUID")
    container_reference = container.get("ContainerReference")
    volume_name = row.get("Name")
    if (not isinstance(volume_uuid, str) or not isinstance(container_uuid, str)
            or not isinstance(container_reference, str) or not isinstance(volume_name, str)):
        raise RuntimeError("APFS volume UUID/container/name identity is incomplete")
    mount_location = (expected_mount_location
                      if expected_mount_location is not None
                      else f"/Volumes/{volume_name}")
    info = _run_plist([_diskutil(), "info", "-plist", f"/dev/{device}"])
    mount_state = _resolve_apfs_mount_state(
        volume_uuid=volume_uuid, device_identifier=device,
        container_uuid=container_uuid, container_reference=container_reference,
        volume_name=volume_name, expected_mount_location=mount_location,
        diskutil_info=info, mount_records=mount_records)
    return {
        "DeviceIdentifier": device,
        "VolumeUUID": volume_uuid,
        "VolumeName": volume_name,
        "APFSContainerReference": container_reference,
        "FilesystemType": "apfs",
        "Mounted": info.get("Mounted"),
        "MountPoint": info.get("MountPoint"),
        "mount_state": mount_state["mount_state"],
        "mount_identity": mount_state,
        "diskutil_info_sha256": _sha256(plistlib.dumps(info, fmt=plistlib.FMT_BINARY)),
    }


def capture_host_observation() -> dict[str, Any]:
    """Capture one APFS plist and cross-check it against the Data mount."""
    if platform.system() != "Darwin":
        raise RuntimeError("internal VM APFS capacity mode is supported only on macOS")
    diskutil = _diskutil()
    started = time.time()
    apfs = _run_plist([diskutil, "apfs", "list", "-plist"])
    containers = apfs.get("Containers")
    if not isinstance(containers, list):
        raise RuntimeError("diskutil APFS plist has no complete Containers array")
    container_matches = [row for row in containers
                         if isinstance(row, dict)
                         and row.get("APFSContainerUUID") == EXPECTED_CONTAINER_UUID]
    if len(container_matches) != 1:
        raise RuntimeError("intended APFS container is absent or ambiguous")
    container = container_matches[0]
    stores = container.get("PhysicalStores")
    if not isinstance(stores, list) or len(stores) != 1 or not isinstance(stores[0], dict):
        raise RuntimeError("target container physical-store enumeration is incomplete or ambiguous")
    store = stores[0]
    store_device = store.get("DeviceIdentifier")
    if not isinstance(store_device, str) or not re.fullmatch(r"disk[0-9]+s[0-9]+", store_device):
        raise RuntimeError("APFS physical-store device identifier is malformed")
    physical_store_info = _run_plist([diskutil, "info", "-plist", f"/dev/{store_device}"])
    physical_device = physical_store_info.get("ParentWholeDisk")
    if not isinstance(physical_device, str) or not re.fullmatch(r"disk[0-9]+", physical_device):
        raise RuntimeError("APFS physical device identity is unavailable")
    physical_device_info = _run_plist([diskutil, "info", "-plist", f"/dev/{physical_device}"])
    physical_device_info["selected_parent_whole_disk"] = physical_device
    data_info = _run_plist([diskutil, "info", "-plist", EXPECTED_DATA_MOUNT])
    mount_records = _mounted_filesystem_table()
    volume_rows = container.get("Volumes")
    data_rows = ([row for row in volume_rows if isinstance(row, Mapping)
                  and row.get("APFSVolumeUUID") == EXPECTED_DATA_VOLUME_UUID]
                 if isinstance(volume_rows, list) else [])
    data_mount_info = None
    if len(data_rows) == 1:
        data_mount_info = _capture_volume_info(
            container, data_rows[0], mount_records,
            expected_mount_location=EXPECTED_DATA_MOUNT)
    elif len(data_rows) > 1:
        data_mount_info = {"mount_state": "AMBIGUOUS"}
    if data_mount_info is not None:
        data_info["canonical_mount_state"] = data_mount_info.get("mount_state")
        data_info["canonical_mount_identity"] = data_mount_info.get("mount_identity")
        data_info["canonical_device_identifier"] = data_mount_info.get("DeviceIdentifier")
    safety_rows = ([row for row in volume_rows if isinstance(row, Mapping)
                    and row.get("Name") == VM_SAFETY_VOLUME_NAME]
                   if isinstance(volume_rows, list) else [])
    safety_volume_info = None
    if len(safety_rows) == 1:
        safety_volume_info = _capture_volume_info(container, safety_rows[0], mount_records)
    elif len(safety_rows) > 1:
        safety_volume_info = {"status": "AMBIGUOUS"}
    statfs_free: int | None = None
    statfs_failure: str | None = None
    try:
        statvfs = os.statvfs(EXPECTED_DATA_MOUNT)
        statfs_free = int(statvfs.f_bavail) * int(statvfs.f_frsize)
    except OSError as error:
        # Data statfs is retained only as diagnostic evidence. APFS shared
        # capacity authority comes from the container and Data info readbacks.
        statfs_failure = str(error)
    captured = time.time()
    return {
        "schema_version": 1,
        "captured_at_unix": captured,
        "capture_started_at_unix": started,
        "host": {
            "product_version": subprocess.run(
                ["/usr/bin/sw_vers", "-productVersion"], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=5.0).stdout.strip(),
            "build_version": subprocess.run(
                ["/usr/bin/sw_vers", "-buildVersion"], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=5.0).stdout.strip(),
            "architecture": platform.machine(),
        },
        "apfs_container": container,
        "mounted_filesystem_table_sha256": _sha256(
            policy._canonical_pretty_json_bytes(mount_records)),
        "physical_store_info": physical_store_info,
        "physical_device_info": physical_device_info,
        "data_volume_info": data_info,
        "safety_volume_info": safety_volume_info,
        "crosschecks": {
            "apfs_container_free_bytes": container.get("CapacityFree"),
            "data_info_container_free_bytes": data_info.get("APFSContainerFree"),
            "data_statfs_available_bytes": statfs_free,
            "data_statfs_failure": statfs_failure,
            "data_mount_path": EXPECTED_DATA_MOUNT,
            "tolerance_bytes": VM_SHARED_FREE_CROSSCHECK_TOLERANCE,
        },
    }


def _container_blockers(observation: Mapping[str, Any], *,
                        expected_vm_volume_uuid: str | None = None,
                        expected_vm_quota_bytes: int | None = None,
                        expected_vm_volume_name: str = VM_VOLUME_NAME,
                        expected_safety_volume_uuid: str | None = None,
                        expected_safety_volume_in_use_bytes: int | None = None) -> list[str]:
    """Validate one exact APFS domain and its optional signed volume contracts."""
    blockers: list[str] = []
    container = observation.get("apfs_container")
    physical_store = observation.get("physical_store_info")
    physical_device = observation.get("physical_device_info")
    data = observation.get("data_volume_info")
    cross = observation.get("crosschecks")
    if not all(isinstance(item, Mapping) for item in (container, physical_store, physical_device, data, cross)):
        return ["APFS observation is incomplete"]
    if container.get("APFSContainerUUID") != EXPECTED_CONTAINER_UUID:
        blockers.append("APFS container UUID does not match the authorized internal target")
    if not _integer(container.get("CapacityCeiling"), minimum=1):
        blockers.append("APFS container capacity ceiling is unknown")
    if not _integer(container.get("CapacityFree")):
        blockers.append("APFS container free capacity is unknown")
    for key in ("CapacityQuota", "CapacityReserve", "ContainerQuota", "ContainerReserve"):
        if key in container:
            value = container.get(key)
            if not _integer(value) or value != 0:
                blockers.append(f"unexpected or unknown container quota/reserve field: {key}")
    if container.get("DesignatedPhysicalStore") != physical_store.get("DeviceIdentifier"):
        blockers.append("APFS designated physical store differs from diskutil store identity")
    stores = container.get("PhysicalStores")
    if not isinstance(stores, list) or len(stores) != 1 or not isinstance(stores[0], Mapping):
        blockers.append("APFS physical-store enumeration is not a single complete internal store")
        store: Mapping[str, Any] = {}
    else:
        store = stores[0]
    if store.get("DiskUUID") != EXPECTED_PHYSICAL_STORE_UUID:
        blockers.append("physical-store UUID does not match the authorized internal device")
    if physical_store.get("DiskUUID") != EXPECTED_PHYSICAL_STORE_UUID:
        blockers.append("diskutil physical-store UUID does not match the authorized internal device")
    if physical_store.get("Internal") is not True or physical_store.get("Removable") is not False:
        blockers.append("physical store is not verified as internal and non-removable")
    if (physical_store.get("DeviceIdentifier") != store.get("DeviceIdentifier")
            or physical_store.get("ParentWholeDisk") != physical_device.get("selected_parent_whole_disk")):
        blockers.append("physical-store and whole-device diskutil identities do not agree")
    if physical_device.get("Internal") is not True or physical_device.get("Removable") is not False:
        blockers.append("physical device is not verified as internal and non-removable")
    if physical_device.get("MediaName") != EXPECTED_INTERNAL_MEDIA_NAME:
        blockers.append("physical device media identity does not match the reviewed internal SSD")
    if physical_device.get("SolidState") is not True:
        blockers.append("physical device is not verified as solid state")
    if store.get("Size") != container.get("CapacityCeiling"):
        blockers.append("physical-store byte size and APFS container capacity ceiling disagree")
    if data.get("VolumeUUID") != EXPECTED_DATA_VOLUME_UUID:
        blockers.append("current Data volume UUID does not match the measured target")
    if data.get("MountPoint") != EXPECTED_DATA_MOUNT:
        blockers.append("current Data mount point identity changed")
    if data.get("canonical_mount_state") != "MOUNTED_VERIFIED":
        blockers.append("Data volume is not mounted at its exact kernel-verified mount identity")
    if data.get("FilesystemType") != "apfs":
        blockers.append("Data volume filesystem is not APFS")
    if data.get("APFSContainerReference") != container.get("ContainerReference"):
        blockers.append("Data volume and target APFS container references disagree")
    volumes = container.get("Volumes")
    if not isinstance(volumes, list) or not volumes:
        blockers.append("APFS volume enumeration is missing or empty")
        volumes = []
    seen: set[str] = set()
    data_matches = vm_matches = safety_matches = 0
    for row in volumes:
        if not isinstance(row, Mapping):
            blockers.append("APFS volume enumeration contains a malformed entry")
            continue
        volume_uuid = row.get("APFSVolumeUUID")
        if not isinstance(volume_uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid):
            blockers.append("APFS volume UUID is missing or malformed")
            continue
        if volume_uuid in seen:
            blockers.append("APFS volume enumeration contains duplicate volume UUIDs")
        seen.add(volume_uuid)
        if (not isinstance(row.get("DeviceIdentifier"), str)
                or not isinstance(row.get("Name"), str)
                or not _integer(row.get("CapacityInUse"))
                or not _integer(row.get("CapacityQuota"))
                or not _integer(row.get("CapacityReserve"))):
            blockers.append(f"APFS volume {volume_uuid} has incomplete capacity or identity fields")
        data_matches += volume_uuid == EXPECTED_DATA_VOLUME_UUID
        vm_matches += row.get("Name") == expected_vm_volume_name
        safety_matches += row.get("Name") == VM_SAFETY_VOLUME_NAME
        if (expected_vm_volume_uuid is None and row.get("Name") in {
                VM_VOLUME_NAME, VM_QUOTA_PROBE_VOLUME_NAME}):
            blockers.append("a same-name VM APFS volume already exists before creation")
        if expected_safety_volume_uuid is None and row.get("Name") == VM_SAFETY_VOLUME_NAME:
            blockers.append("a same-name safety APFS volume already exists before creation")
        is_expected_vm = expected_vm_volume_uuid is not None and volume_uuid == expected_vm_volume_uuid
        is_expected_safety = expected_safety_volume_uuid is not None and volume_uuid == expected_safety_volume_uuid
        if not (is_expected_vm or is_expected_safety) and (
                row.get("CapacityQuota") != 0 or row.get("CapacityReserve") != 0):
            blockers.append("an existing non-VM volume has unexpected quota/reserve state")
    if data_matches != 1:
        blockers.append("Data volume is absent or ambiguous in the complete container enumeration")
    else:
        data_rows = [row for row in volumes if isinstance(row, Mapping)
                     and EXPECTED_DATA_VOLUME_UUID == row.get("APFSVolumeUUID")]
        if (len(data_rows) != 1
                or data.get("DeviceIdentifier") != data_rows[0].get("DeviceIdentifier")
                or data.get("canonical_device_identifier") != data_rows[0].get("DeviceIdentifier")):
            blockers.append("Data volume diskutil and APFS device identities do not agree")
    if expected_vm_volume_uuid is None and vm_matches != 0:
        blockers.append("VM volume name is not available for a fresh creation")
    if expected_vm_volume_uuid is not None:
        matches = [row for row in volumes if isinstance(row, Mapping)
                   and row.get("APFSVolumeUUID") == expected_vm_volume_uuid]
        if len(matches) != 1 or vm_matches != 1:
            blockers.append("bound VM volume UUID is absent or ambiguous")
        elif (matches[0].get("Name") != expected_vm_volume_name
              or not _integer(expected_vm_quota_bytes, minimum=1)
              or matches[0].get("CapacityQuota") != expected_vm_quota_bytes
              or matches[0].get("CapacityReserve") != 0):
            blockers.append("VM APFS volume name, quota, or reserve differs from policy")
    if expected_safety_volume_uuid is None:
        if safety_matches != 0:
            blockers.append("an unbound safety-volume APFS identity is present")
    else:
        safety_rows = [row for row in volumes if isinstance(row, Mapping)
                       and row.get("APFSVolumeUUID") == expected_safety_volume_uuid]
        if len(safety_rows) != 1 or safety_matches != 1:
            blockers.append("bound safety-volume UUID is absent or ambiguous")
        elif (safety_rows[0].get("Name") != VM_SAFETY_VOLUME_NAME
              or safety_rows[0].get("CapacityQuota") != 0
              or safety_rows[0].get("CapacityReserve") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
              or not _integer(safety_rows[0].get("CapacityInUse"))
              or (expected_safety_volume_in_use_bytes is not None
                  and safety_rows[0].get("CapacityInUse") != expected_safety_volume_in_use_bytes)):
            blockers.append("safety-volume name, empty accounting, quota, or exact reserve differs from policy")
        else:
            safety_info = observation.get("safety_volume_info")
            if not isinstance(safety_info, Mapping):
                blockers.append("safety-volume mount-state read-back is missing")
            elif (safety_info.get("VolumeUUID") != expected_safety_volume_uuid
                  or safety_info.get("DeviceIdentifier") != safety_rows[0].get("DeviceIdentifier")
                  or safety_info.get("APFSContainerReference") != container.get("ContainerReference")
                  or safety_info.get("FilesystemType") != "apfs"
                  or safety_info.get("mount_state") != "UNMOUNTED_VERIFIED"):
                blockers.append("safety-volume UUID, container, filesystem, or unmounted state differs from policy")
    container_free = cross.get("apfs_container_free_bytes")
    data_free = cross.get("data_info_container_free_bytes")
    if not all(_integer(item) for item in (container_free, data_free)):
        blockers.append("shared APFS container free-capacity cross-checks are incomplete")
    elif abs(container_free - data_free) > VM_SHARED_FREE_CROSSCHECK_TOLERANCE:
        blockers.append("APFS plist and Data APFSContainerFree readings disagree beyond tolerance")
    captured = observation.get("captured_at_unix")
    if (not isinstance(captured, (int, float)) or isinstance(captured, bool)
            or not math.isfinite(float(captured)) or time.time() - float(captured) < 0
            or time.time() - float(captured) > VM_MAX_OBSERVATION_AGE_SECONDS):
        blockers.append("APFS observation is stale or has an invalid timestamp")
    return blockers


def conservative_container_free_bytes(observation: Mapping[str, Any]) -> int:
    """Return the conservative shared free value, counting one container once."""
    cross = observation.get("crosschecks")
    if not isinstance(cross, Mapping):
        raise ValueError("APFS free-space cross-checks are missing")
    values = [cross.get(key) for key in (
        "apfs_container_free_bytes", "data_info_container_free_bytes")]
    if not all(_integer(value) for value in values):
        raise ValueError("APFS shared free-space cross-checks contain an unknown value")
    if max(values) - min(values) > VM_SHARED_FREE_CROSSCHECK_TOLERANCE:
        raise ValueError("APFS shared free-space cross-checks disagree beyond tolerance")
    return min(values)


def calculate_post_reserve_free_capacity(*, pre_reserve_free_bytes: int | None,
                                         post_reserve_free_bytes: int | None) -> dict[str, Any]:
    """Conservatively bound ordinary free capacity without APFS readback proof.

    Either pre- or post-reserve readback may still include the reserve. Subtract
    the reserve from the lower of those observations. This can under-credit
    capacity when a readback already nets out the reserve, but cannot overstate
    ordinary free capacity based on an undocumented accounting assumption.
    """
    blockers: list[str] = []
    if not _integer(pre_reserve_free_bytes, minimum=VM_PROTECTED_CAPACITY_RESERVE_BYTES + 1):
        blockers.append("pre-reserve shared free capacity is unknown or below the protected reserve")
    if not _integer(post_reserve_free_bytes):
        blockers.append("post-reserve shared free capacity is unknown")
    if blockers:
        return {
            "pre_reserve_shared_free_bytes": pre_reserve_free_bytes,
            "post_reserve_observed_shared_free_bytes": post_reserve_free_bytes,
            "safety_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "unreserved_free_ceiling_bytes": None,
            "effective_unreserved_free_bytes": None,
            "admitted": False,
            "blockers": blockers,
        }
    assert pre_reserve_free_bytes is not None and post_reserve_free_bytes is not None
    ceiling = pre_reserve_free_bytes - VM_PROTECTED_CAPACITY_RESERVE_BYTES
    effective = max(0, min(pre_reserve_free_bytes, post_reserve_free_bytes)
                    - VM_PROTECTED_CAPACITY_RESERVE_BYTES)
    return {
        "pre_reserve_shared_free_bytes": pre_reserve_free_bytes,
        "post_reserve_observed_shared_free_bytes": post_reserve_free_bytes,
        "safety_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
        "unreserved_free_ceiling_bytes": ceiling,
        "effective_unreserved_free_bytes": effective,
        "admitted": True,
        "blockers": [],
    }


def conservative_session_unreserved_free_bytes(
        observation: Mapping[str, Any], *, volume_uuid: str | None = None,
        capacity_contract: Mapping[str, Any] | None = None) -> int:
    """Subtract reserve from live free, capped by the immutable baseline.

    APFS readback semantics are not established as a cross-version contract.
    Subtracting from every live reading is deliberately conservative if a host
    already nets the reserve out; it prevents an ambiguous gross reading from
    being treated as wholly available ordinary capacity.
    """
    contract = capacity_contract or _active_capacity_contract(volume_uuid=volume_uuid)
    pre_reserve = contract.get("pre_reserve_shared_free_bytes")
    ceiling = contract.get("unreserved_free_ceiling_bytes")
    if (not _integer(pre_reserve, minimum=VM_PROTECTED_CAPACITY_RESERVE_BYTES + 1)
            or not _integer(ceiling)
            or ceiling != pre_reserve - VM_PROTECTED_CAPACITY_RESERVE_BYTES):
        raise ValueError("active VM session lacks a valid pre-reserve capacity contract")
    live_free = conservative_container_free_bytes(observation)
    return max(0, min(live_free, pre_reserve) - VM_PROTECTED_CAPACITY_RESERVE_BYTES)


def calculate_dynamic_vm_quota(*, free_bytes: int | None,
                               deterministic_uncertainty_bytes: int | None,
                               admitted_coexistence_bytes: int | None,
                               minimum_required_quota_bytes: int | None,
                               maximum_quota_bytes: int = VM_REVIEWED_QUOTA_MAX_BYTES,
                               reserve_already_reflected: bool = False) -> dict[str, Any]:
    """Calculate an exact VM quota from live free capacity and explicit terms.

    ``free_bytes`` is measured before reserve creation or is already a
    conservatively bounded post-reserve value. The caller selects the matching
    15 GiB or zero GiB reserve subtraction. This helper never includes sampled
    future growth, unknown metadata, or a quota-as-physical-allocation assumption.
    """
    blockers: list[str] = []
    values = {
        "free_bytes": free_bytes,
        "deterministic_uncertainty_bytes": deterministic_uncertainty_bytes,
        "admitted_coexistence_bytes": admitted_coexistence_bytes,
        "minimum_required_quota_bytes": minimum_required_quota_bytes,
        "maximum_quota_bytes": maximum_quota_bytes,
    }
    for name, value in values.items():
        if not _integer(value, minimum=1 if name in {
                "free_bytes", "minimum_required_quota_bytes", "maximum_quota_bytes"} else 0):
            blockers.append(f"dynamic VM quota input is unknown or invalid: {name}")
    if blockers:
        return {**values, "quota_bytes": None, "admitted": False, "blockers": blockers}
    assert free_bytes is not None and deterministic_uncertainty_bytes is not None
    assert admitted_coexistence_bytes is not None and minimum_required_quota_bytes is not None
    assert maximum_quota_bytes is not None
    if maximum_quota_bytes > VM_REVIEWED_QUOTA_MAX_BYTES:
        blockers.append("dynamic VM quota maximum exceeds the reviewed quota cap")
    if not isinstance(reserve_already_reflected, bool):
        blockers.append("dynamic VM quota reserve-accounting mode is invalid")
    if free_bytes <= VM_OPERATIONAL_UNRESERVED_FREE_MIN:
        blockers.append("shared APFS free is at or below the 5 GiB operational hard floor")
    elif free_bytes <= VM_OPERATIONAL_WARNING_FREE:
        blockers.append("shared APFS free reached the 8 GiB early-stop threshold")
    floor_bytes = VM_OPERATIONAL_UNRESERVED_FREE_MIN
    reserve_bytes = 0 if reserve_already_reflected else VM_PROTECTED_CAPACITY_RESERVE_BYTES
    available = (free_bytes - reserve_bytes - floor_bytes
                 - deterministic_uncertainty_bytes - admitted_coexistence_bytes)
    quota = min(maximum_quota_bytes, available)
    # diskutil accepts MiB size arguments; rounding down preserves the exact
    # selected byte ceiling in the plist read-back.
    quota = (quota // MIB) * MIB
    if quota < minimum_required_quota_bytes:
        blockers.append("live capacity leaves less than the reviewed minimum VM quota")
    return {
        **values,
        "operational_free_floor_bytes": floor_bytes,
        "operational_warning_free_bytes": VM_OPERATIONAL_WARNING_FREE,
        "safety_reserve_subtracted_bytes": reserve_bytes,
        "available_quota_before_reviewed_cap_bytes": available,
        "quota_bytes": quota if quota >= minimum_required_quota_bytes and not blockers else None,
        "admitted": not blockers,
        "blockers": blockers,
    }


def _active_capacity_contract(*, volume_uuid: str | None = None) -> dict[str, Any]:
    """Read the signed session marker as the authority for live volume identity."""
    marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    marker_volume_uuid = marker.get("volume_uuid")
    quota = marker.get("quota_bytes")
    safety_uuid = marker.get("safety_volume_uuid")
    safety_used = marker.get("safety_volume_in_use_bytes")
    pre_reserve_free = marker.get("pre_reserve_shared_free_bytes")
    unreserved_free_ceiling = marker.get("unreserved_free_ceiling_bytes")
    if volume_uuid is not None and marker_volume_uuid != volume_uuid:
        raise RuntimeError("active VM marker is bound to a different VM volume UUID")
    if (not isinstance(safety_uuid, str)
            or not re.fullmatch(r"[0-9A-Fa-f-]{36}", safety_uuid)
            or not _integer(safety_used)
            or marker.get("safety_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or marker.get("safety_container_uuid") != EXPECTED_CONTAINER_UUID
            or marker.get("safety_physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID
            or not _integer(pre_reserve_free, minimum=VM_PROTECTED_CAPACITY_RESERVE_BYTES + 1)
            or not _integer(unreserved_free_ceiling)
            or unreserved_free_ceiling != pre_reserve_free - VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or not _integer(quota, minimum=1)):
        raise RuntimeError("active VM marker lacks a valid safety reserve and dynamic quota contract")
    return {
        "session_id": marker.get("session_id"),
        "volume_uuid": marker_volume_uuid,
        "safety_volume_uuid": safety_uuid,
        "safety_volume_in_use_bytes": safety_used,
        "safety_reserve_bytes": marker["safety_reserve_bytes"],
        "safety_container_uuid": marker["safety_container_uuid"],
        "safety_physical_store_uuid": marker["safety_physical_store_uuid"],
        "pre_reserve_shared_free_bytes": pre_reserve_free,
        "unreserved_free_ceiling_bytes": unreserved_free_ceiling,
        "quota_bytes": quota,
    }


def _session_container_blockers(observation: Mapping[str, Any], *, volume_uuid: str,
                                volume_name: str = VM_VOLUME_NAME) -> list[str]:
    try:
        contract = _active_capacity_contract(volume_uuid=volume_uuid)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
        return [f"active VM capacity contract is unavailable: {error}"]
    return _container_blockers(
        observation, expected_vm_volume_uuid=volume_uuid,
        expected_vm_quota_bytes=contract["quota_bytes"],
        expected_vm_volume_name=volume_name,
        expected_safety_volume_uuid=contract["safety_volume_uuid"],
        expected_safety_volume_in_use_bytes=contract["safety_volume_in_use_bytes"])


def measure_concurrent_growth_bound(*, volume_uuid: str | None = None,
                                    sample_observation: Callable[[], Mapping[str, Any]] = capture_host_observation,
                                    sample_count: int = VM_CONCURRENT_GROWTH_SAMPLE_COUNT,
                                    sample_interval_seconds: float = VM_CONCURRENT_GROWTH_SAMPLE_INTERVAL_SECONDS
                                    ) -> dict[str, Any]:
    """Collect diagnostic shared-container growth samples without admission authority."""
    if (not _integer(sample_count, minimum=2)
            or not isinstance(sample_interval_seconds, (int, float))
            or isinstance(sample_interval_seconds, bool)
            or not math.isfinite(float(sample_interval_seconds))
            or sample_interval_seconds <= 0
            or sample_interval_seconds > VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
        raise ValueError("concurrent-growth measurement requires at least two samples at <=250 ms intervals")
    samples: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for index in range(sample_count):
        observation = sample_observation()
        if volume_uuid is None:
            blockers = _container_blockers(observation, expected_vm_volume_uuid=None)
        else:
            marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
            quota_bytes = marker.get("quota_bytes")
            safety_uuid = marker.get("safety_volume_uuid")
            safety_used = marker.get("safety_volume_in_use_bytes")
            blockers = _container_blockers(
                observation, expected_vm_volume_uuid=volume_uuid,
                expected_vm_quota_bytes=quota_bytes if _integer(quota_bytes, minimum=1) else None,
                expected_vm_volume_name=marker.get("volume_name", VM_VOLUME_NAME),
                expected_safety_volume_uuid=safety_uuid,
                expected_safety_volume_in_use_bytes=safety_used)
        if blockers:
            raise RuntimeError("concurrent-growth sample identity is invalid: " + "; ".join(blockers))
        free = conservative_container_free_bytes(observation)
        used = 0
        if volume_uuid is not None:
            used_value = _volume_row(observation, volume_uuid).get("CapacityInUse")
            if not _integer(used_value):
                raise RuntimeError("concurrent-growth sample has unknown VM-volume use")
            used = used_value
        sampled_at = observation.get("captured_at_unix")
        if (not isinstance(sampled_at, (int, float)) or isinstance(sampled_at, bool)
                or not math.isfinite(float(sampled_at))):
            raise RuntimeError("concurrent-growth sample timestamp is invalid")
        row = {
            "captured_at_unix": float(sampled_at),
            "shared_container_free_bytes": free,
            "vm_volume_used_bytes": used,
        }
        if previous is not None:
            gap = row["captured_at_unix"] - previous["captured_at_unix"]
            if gap <= 0 or gap > 5.0:
                raise RuntimeError("concurrent-growth sampling gap is invalid or too long")
            total_growth = max(0, previous["shared_container_free_bytes"] - free)
            vm_growth = max(0, used - previous["vm_volume_used_bytes"])
            row["concurrent_growth_bytes"] = max(0, total_growth - vm_growth)
            row["sample_gap_seconds"] = gap
        samples.append(row)
        previous = row
        if index + 1 < sample_count:
            time.sleep(float(sample_interval_seconds))
    observed = max((row.get("concurrent_growth_bytes", 0) for row in samples), default=0)
    return {
        "schema_version": 1,
        "mode": VM_MODE,
        "volume_uuid": volume_uuid,
        "sample_count": len(samples),
        "observed_max_interval_growth_bytes": observed,
        "concurrent_growth_bound_bytes": observed,
        "authority": "DIAGNOSTIC_ONLY",
        "samples": samples,
    }


def deduplicate_container_free_measurements(
        observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return one conservative free value per APFS container UUID, never per mount."""
    by_container: dict[str, list[int]] = {}
    for observation in observations:
        container = observation.get("apfs_container")
        if not isinstance(container, Mapping):
            raise ValueError("capacity measurement has no APFS container identity")
        container_uuid = container.get("APFSContainerUUID")
        if not isinstance(container_uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{36}", container_uuid):
            raise ValueError("capacity measurement has a malformed APFS container UUID")
        by_container.setdefault(container_uuid, []).append(
            conservative_container_free_bytes(observation))
    return {
        "container_count": len(by_container),
        "capacity_domains": {
            container_uuid: {
                "shared_free_bytes": min(values),
                "observation_count": len(values),
            }
            for container_uuid, values in sorted(by_container.items())
        },
        "total_unique_shared_free_bytes": sum(min(values) for values in by_container.values()),
    }


def _phase_uncertainty(growth_bytes: int) -> int:
    proportional = (growth_bytes * VM_PHASE_UNCERTAINTY_BASIS_POINTS + 9999) // 10000
    return max(VM_PHASE_MIN_UNCERTAINTY_BYTES, proportional)


def _canonical_apple_restore_url(value: Any) -> str:
    """Canonicalize only scheme/host/default port; preserve path and query exactly."""
    if not isinstance(value, str) or not value or any(
            ord(char) <= 0x20 or ord(char) >= 0x7f for char in value) or "#" in value or "\\" in value:
        raise ValueError("restore URL is absent, non-ASCII, contains whitespace, or has a fragment")
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("restore URL contains a malformed percent escape")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("restore URL authority is malformed") from error
    host = (parsed.hostname or "").lower()
    apple_host = (host == "apple.com" or host.endswith(".apple.com")
                  or host.endswith(".cdn-apple.com"))
    if (parsed.scheme.lower() != "https" or not apple_host or parsed.username is not None
            or parsed.password is not None or port not in (None, 443) or not parsed.path):
        raise ValueError("restore URL must be an absolute HTTPS Apple URL without userinfo or non-default port")
    authority_start = value.find("://") + 3
    authority_end_candidates = [index for index in (value.find("/", authority_start),
                                                     value.find("?", authority_start))
                                if index >= 0]
    authority_end = min(authority_end_candidates) if authority_end_candidates else len(value)
    raw_authority = value[authority_start:authority_end]
    raw_host = raw_authority.rsplit("@", 1)[-1]
    if raw_host.lower().endswith(":443"):
        raw_host = raw_host[:-4]
    if raw_host.lower() != host:
        raise ValueError("restore URL authority is ambiguous")
    has_query_delimiter = "?" in value[authority_end:]
    canonical = f"https://{host}{parsed.path}"
    if has_query_delimiter:
        canonical += "?" + parsed.query
    return canonical


def _subject_ascii_string(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(char) < 0x20 or ord(char) > 0x7e for char in value)):
        raise ValueError(f"restore subject {label} is missing or not canonical ASCII")
    return value


def _validate_subject_shape(subject: Mapping[str, Any]) -> None:
    """Enforce the exact v1 legacy or complete subject schema before JCS encoding."""
    if not isinstance(subject, Mapping):
        raise ValueError("restore subject must be a JSON object")
    schema = subject.get("schema")
    if schema == "fg-metal.restore-subject-legacy.v1":
        if set(subject) != {"schema", "restore", "host"}:
            raise ValueError("legacy restore subject has missing or unknown top-level fields")
        restore = subject.get("restore")
        host = subject.get("host")
        if (not isinstance(restore, Mapping)
                or set(restore) != {"source_url", "final_url", "content_length_bytes",
                                   "product_version", "build_version", "is_supported"}
                or not isinstance(host, Mapping)
                or set(host) != {"architecture", "product_version", "build_version"}):
            raise ValueError("legacy restore subject has a missing or unknown field")
        if (not _integer(restore.get("content_length_bytes"), minimum=1)
                or restore["content_length_bytes"] > 9_007_199_254_740_991
                or restore.get("is_supported") is not True):
            raise ValueError("legacy restore subject length or support state is invalid")
        for key in ("source_url", "final_url"):
            url = _subject_ascii_string(restore.get(key), f"{key}")
            if _canonical_apple_restore_url(url) != url:
                raise ValueError(f"legacy restore subject {key} is not canonical")
        for obj, version_key, build_key in ((restore, "product_version", "build_version"),
                                            (host, "product_version", "build_version")):
            version = _subject_ascii_string(obj.get(version_key), version_key)
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
                raise ValueError(f"restore subject {version_key} is malformed")
            _subject_ascii_string(obj.get(build_key), build_key)
        if (host.get("architecture") not in {"arm64", "x86_64"}
                or restore.get("product_version") != host.get("product_version")
                or restore.get("build_version") != host.get("build_version")):
            raise ValueError("legacy restore subject host/platform compatibility is inconsistent")
        return
    if schema != "fg-metal.restore-subject.v1":
        raise ValueError("restore subject schema is unknown")
    if set(subject) != {"schema", "restore", "compatibility"}:
        raise ValueError("restore subject has missing or unknown top-level fields")
    restore = subject.get("restore")
    compatibility = subject.get("compatibility")
    if (not isinstance(restore, Mapping)
            or set(restore) != {"source_url", "final_url", "content_length_bytes",
                               "product_version", "build_version"}
            or not isinstance(compatibility, Mapping)
            or set(compatibility) != {"restore_api", "guest_platform", "host_architecture",
                                      "host_product_version", "host_build_version",
                                      "restore_supported_for_host", "hardware"}):
        raise ValueError("restore subject has a missing or unknown field")
    if (not _integer(restore.get("content_length_bytes"), minimum=1)
            or restore["content_length_bytes"] > 9_007_199_254_740_991):
        raise ValueError("restore subject length is missing or outside the exact JCS range")
    for key in ("source_url", "final_url"):
        url = _subject_ascii_string(restore.get(key), key)
        if _canonical_apple_restore_url(url) != url:
            raise ValueError(f"restore subject {key} is not canonical")
    product_version = _subject_ascii_string(restore.get("product_version"), "product version")
    host_version = _subject_ascii_string(compatibility.get("host_product_version"), "host product version")
    if (not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
                         product_version)
            or host_version != product_version):
        raise ValueError("restore subject product version or host compatibility is invalid")
    _subject_ascii_string(restore.get("build_version"), "build version")
    _subject_ascii_string(compatibility.get("host_build_version"), "host build version")
    if (restore.get("build_version") != compatibility.get("host_build_version")
            or compatibility.get("restore_api") != "VZMacOSRestoreImage.latestSupported"
            or compatibility.get("guest_platform") != "macOS"
            or compatibility.get("host_architecture") not in {"arm64", "x86_64"}
            or compatibility.get("restore_supported_for_host") is not True):
        raise ValueError("restore subject support/API/platform binding is invalid")
    hardware = compatibility.get("hardware")
    if (not isinstance(hardware, Mapping)
            or set(hardware) != {"model_data_sha256", "model_supported_for_host",
                                "minimum_supported_cpu_count", "minimum_supported_memory_size_bytes"}
            or not isinstance(hardware.get("model_data_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", hardware.get("model_data_sha256", ""))
            or hardware.get("model_supported_for_host") is not True
            or not _integer(hardware.get("minimum_supported_cpu_count"), minimum=1)
            or not _integer(hardware.get("minimum_supported_memory_size_bytes"), minimum=1)
            or hardware["minimum_supported_cpu_count"] > 9_007_199_254_740_991
            or hardware["minimum_supported_memory_size_bytes"] > 9_007_199_254_740_991):
        raise ValueError("restore subject hardware compatibility has missing or unknown fields, or is incomplete or unsupported")


def _canonical_subject_bytes(subject: Mapping[str, Any]) -> bytes:
    """Encode the closed ASCII/integer/bool subject subset exactly as JCS."""
    _validate_subject_shape(subject)
    def validate(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                _subject_ascii_string(key, "key")
                validate(child)
        elif isinstance(value, list):
            raise ValueError("restore subject v1 does not permit arrays")
        elif isinstance(value, str):
            _subject_ascii_string(value, "string")
        elif isinstance(value, bool):
            return
        elif isinstance(value, int):
            if value < 0 or value > 9_007_199_254_740_991:
                raise ValueError("restore subject integer is outside the exact JCS range")
        else:
            raise ValueError("restore subject contains a null, float, or unsupported JSON value")
    validate(subject)
    return json.dumps(subject, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("ascii")


def _restore_subject_digest(subject: Mapping[str, Any], *, legacy: bool = False) -> str:
    domain = (b"FG-Metal/restore-subject-legacy/v1\0" if legacy
              else b"FG-Metal/restore-subject-identity/v1\0")
    return "rsid-v1:sha256:" + _sha256(domain + _canonical_subject_bytes(subject))


def _host_tuple(host: Any) -> dict[str, str]:
    if not isinstance(host, Mapping):
        raise ValueError("contemporaneous host tuple is missing")
    architecture = host.get("architecture")
    if not isinstance(architecture, str) or architecture not in {"arm64", "x86_64"}:
        raise ValueError("contemporaneous host architecture is unknown")
    product_version = _subject_ascii_string(host.get("product_version"), "host product version")
    build_version = _subject_ascii_string(host.get("build_version"), "host build version")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", product_version):
        raise ValueError("contemporaneous host product version is malformed")
    return {"architecture": architecture, "product_version": product_version,
            "build_version": build_version}


def _legacy_restore_subject(metadata: Mapping[str, Any], host: Any) -> dict[str, Any]:
    if metadata.get("is_supported") is not True:
        raise ValueError("restore image is not explicitly supported by the host")
    length = metadata.get("expected_length_bytes")
    if not _integer(length, minimum=1) or length > 9_007_199_254_740_991:
        raise ValueError("restore subject Content-Length is missing or inexact")
    product_version = _subject_ascii_string(metadata.get("operating_system_version"),
                                            "restore product version")
    build_version = _subject_ascii_string(metadata.get("build_version"), "restore build version")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", product_version):
        raise ValueError("restore product version is malformed")
    host_values = _host_tuple(host)
    if (product_version != host_values["product_version"]
            or build_version != host_values["build_version"]):
        raise ValueError("latest supported restore image does not match the contemporaneous host OS/build")
    return {
        "schema": "fg-metal.restore-subject-legacy.v1",
        "restore": {
            "source_url": _canonical_apple_restore_url(metadata.get("source_url")),
            "final_url": _canonical_apple_restore_url(metadata.get("final_url")),
            "content_length_bytes": length,
            "product_version": product_version,
            "build_version": build_version,
            "is_supported": True,
        },
        "host": host_values,
    }


def _hardware_compatibility_tuple(metadata: Mapping[str, Any]) -> dict[str, Any]:
    model_digest = metadata.get("hardware_model_data_sha256")
    model_supported = metadata.get("hardware_model_supported")
    minimum_cpus = metadata.get("minimum_supported_cpu_count")
    minimum_memory = metadata.get("minimum_supported_memory_size_bytes")
    if (not isinstance(model_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", model_digest)
            or model_supported is not True
            or not _integer(minimum_cpus, minimum=1)
            or not _integer(minimum_memory, minimum=1)
            or minimum_cpus > 9_007_199_254_740_991
            or minimum_memory > 9_007_199_254_740_991):
        raise ValueError("restore hardware compatibility tuple is missing, malformed, or unsupported")
    return {
        "model_data_sha256": model_digest,
        "model_supported_for_host": True,
        "minimum_supported_cpu_count": minimum_cpus,
        "minimum_supported_memory_size_bytes": minimum_memory,
    }


def _require_fresh_timestamp(value: Any, *, label: str,
                             maximum_age_seconds: int,
                             now: float | None = None) -> tuple[float, float]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeError(f"{label} timestamp is missing or non-finite")
    current = time.time() if now is None else now
    if not isinstance(current, (int, float)) or isinstance(current, bool):
        raise RuntimeError("current clock value is not finite")
    try:
        timestamp_value = float(value)
        current_value = float(current)
    except (OverflowError, TypeError, ValueError) as error:
        raise RuntimeError(f"{label} timestamp is missing or non-finite") from error
    if not math.isfinite(timestamp_value) or not math.isfinite(current_value):
        raise RuntimeError(f"{label} timestamp is missing or non-finite")
    age = current_value - timestamp_value
    if not math.isfinite(age) or age < 0 or age > maximum_age_seconds:
        raise RuntimeError(f"{label} is future-dated or stale")
    return timestamp_value, age


def _require_nonfuture_timestamp(value: Any, *, label: str) -> float:
    timestamp, _age = _require_fresh_timestamp(
        value, label=label, maximum_age_seconds=9_007_199_254_740_991)
    return timestamp


def _require_recorded_age(value: Any, expected_age: float, *, label: str) -> None:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise RuntimeError(f"{label} recorded age is missing or malformed")
    try:
        recorded_age = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise RuntimeError(f"{label} recorded age is missing or malformed") from error
    if not math.isfinite(recorded_age) or abs(recorded_age - expected_age) > 0.001:
        raise RuntimeError(f"{label} recorded age does not match its signed timestamps")


def _complete_restore_subject(legacy_subject: Mapping[str, Any],
                               metadata: Mapping[str, Any]) -> dict[str, Any]:
    hardware = _hardware_compatibility_tuple(metadata)
    legacy_restore = legacy_subject["restore"]
    host = legacy_subject["host"]
    return {
        "schema": "fg-metal.restore-subject.v1",
        "restore": {
            "source_url": legacy_restore["source_url"],
            "final_url": legacy_restore["final_url"],
            "content_length_bytes": legacy_restore["content_length_bytes"],
            "product_version": legacy_restore["product_version"],
            "build_version": legacy_restore["build_version"],
        },
        "compatibility": {
            "restore_api": "VZMacOSRestoreImage.latestSupported",
            "guest_platform": "macOS",
            "host_architecture": host["architecture"],
            "host_product_version": host["product_version"],
            "host_build_version": host["build_version"],
            "restore_supported_for_host": legacy_restore["is_supported"],
            "hardware": hardware,
        },
    }


def validate_restore_metadata(source_url: str, expected_length_bytes: int,
                              metadata_sha256: str,
                              expected_final_url: str | None = None) -> dict[str, Any]:
    canonical_source = _canonical_apple_restore_url(source_url)
    canonical_final = (_canonical_apple_restore_url(expected_final_url)
                       if expected_final_url is not None else None)
    if not _integer(expected_length_bytes, minimum=1):
        raise ValueError("restore expected byte length is unknown or invalid")
    if not isinstance(metadata_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata_sha256):
        raise ValueError("restore upstream metadata digest is missing or malformed")
    result = {
        "source_url": canonical_source,
        "expected_length_bytes": expected_length_bytes,
        "metadata_sha256": metadata_sha256,
        "upstream_digest_is_not_local_verification": True,
        "local_sha256_state": "PENDING",
    }
    if canonical_final is not None:
        result["final_url"] = canonical_final
    return result


class _AppleOnlyRedirectHandler(HTTPRedirectHandler):
    def __init__(self, expected_length_bytes: int, metadata_sha256: str,
                 expected_source_url: str, expected_final_url: str,
                 before_redirect: Callable[[], None] | None = None):
        super().__init__()
        self.expected_length_bytes = expected_length_bytes
        self.metadata_sha256 = metadata_sha256
        self.expected_source_url = _canonical_apple_restore_url(expected_source_url)
        self.expected_final_url = _canonical_apple_restore_url(expected_final_url)
        self.redirect_count = 0
        self.redirect_target: str | None = None
        self.redirect_attempts: list[dict[str, Any]] = []
        self.before_redirect = before_redirect

    def validate_final_url(self, final_url: str) -> str:
        canonical = _canonical_apple_restore_url(final_url)
        if canonical != self.expected_final_url:
            raise RuntimeError("restore GET final URL differs from the signed metadata final URL")
        return canonical

    def redirect_request(self, request: Request, fp: Any, code: int,
                         msg: str, headers: Any, new_url: str) -> Request | None:
        self.redirect_attempts.append({
            "source_url": request.full_url,
            "target_url": new_url,
            "http_status": code,
        })
        validate_restore_metadata(new_url, self.expected_length_bytes, self.metadata_sha256,
                                  self.expected_final_url)
        target = _canonical_apple_restore_url(new_url)
        if (target != self.expected_final_url or self.redirect_count != 0
                or _canonical_apple_restore_url(request.full_url) != self.expected_source_url):
            raise RuntimeError("restore GET redirect is not the one exact metadata-bound final URL")
        if self.before_redirect is not None:
            self.before_redirect()
        self.redirect_count += 1
        self.redirect_target = target
        return super().redirect_request(request, fp, code, msg, headers, new_url)


def fetch_latest_restore_metadata(*, receipt_path: Path | None = None) -> dict[str, Any]:
    """Compile/sign the G0C-bound metadata probe and fetch only VZ metadata plus HTTP HEAD."""
    # Gate before compilation or any subprocess. The compiled probe performs an
    # Apple metadata lookup and HTTP HEAD request, so this must be an enforced
    # precondition rather than a later lineage check.
    current_g0c = verify_fresh_g0c_review()
    current_controller = _controller_identity()
    if receipt_path is None:
        receipt_path = _fresh_artifact_path(VM_RESTORE_METADATA_PREFIX, _new_run_id())
    receipt_path, receipt_path_text = _repo_artifact_path(receipt_path)
    source_sha = current_controller["restore_metadata_probe_sha256"]
    entitlements = (b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                   b"<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
                   b"\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
                   b"<plist version=\"1.0\"><dict><key>com.apple.security.virtualization</key>"
                   b"<true/></dict></plist>\n")
    with tempfile.TemporaryDirectory(prefix="fgmetal-vm-restore-probe-", dir="/private/tmp") as temporary_root:
        temporary = Path(temporary_root)
        binary = temporary / "restore-metadata-probe"
        entitlement_path = temporary / "entitlements.plist"
        entitlement_path.write_bytes(entitlements)
        entitlement_path.chmod(0o600)
        compile_result = subprocess.run(
            ["swiftc", "-swift-version", "5", "-parse-as-library", "-framework", "Virtualization",
             "-framework", "Foundation", "-framework", "CryptoKit",
             str(VM_RESTORE_METADATA_PROBE_SOURCE), "-o", str(binary)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180.0, text=True,
        )
        if compile_result.returncode != 0:
            raise RuntimeError("restore metadata probe compilation failed: " + compile_result.stderr[-3000:])
        sign_result = subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements",
             str(entitlement_path), str(binary)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30.0, text=True,
        )
        if sign_result.returncode != 0:
            raise RuntimeError("restore metadata probe entitlement signing failed: "
                               + sign_result.stderr[-2000:])
        verify_result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", str(binary)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20.0, text=True,
        )
        if verify_result.returncode != 0:
            raise RuntimeError("restore metadata probe code signature verification failed: "
                               + verify_result.stderr[-2000:])
        entitlement_result = subprocess.run(
            ["/usr/bin/codesign", "-d", "--entitlements", ":-", str(binary)],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20.0,
        )
        raw_entitlement_output = entitlement_result.stdout + entitlement_result.stderr
        xml_start = raw_entitlement_output.find(b"<?xml")
        xml_end = raw_entitlement_output.find(b"</plist>", xml_start) if xml_start >= 0 else -1
        if entitlement_result.returncode != 0 or xml_start < 0 or xml_end < 0:
            raise RuntimeError("restore metadata probe entitlement read-back is unavailable")
        xml_end += len(b"</plist>")
        entitlement_data = plistlib.loads(raw_entitlement_output[xml_start:xml_end])
        if entitlement_data.get("com.apple.security.virtualization") is not True:
            raise RuntimeError("restore metadata probe lacks the Virtualization entitlement")
        executable_sha = _file_sha256(binary)
        if (_controller_identity() != current_controller
                or verify_fresh_g0c_review() != current_g0c):
            raise RuntimeError("controller or fresh G0C changed before the metadata probe")
        probe_result = subprocess.run(
            [str(binary)], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=90.0, text=True,
        )
        if probe_result.returncode != 0:
            raise RuntimeError("VZMacOSRestoreImage latestSupported metadata probe failed: "
                               + probe_result.stderr[-2000:])
        try:
            payload = json.loads(probe_result.stdout, parse_constant=_reject_json_constant,
                                 object_pairs_hook=_unique_json_object)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("restore metadata probe returned malformed JSON") from error
        if not isinstance(payload, dict):
            raise RuntimeError("restore metadata probe returned a non-object result")
        if (payload.get("schema_version") != 1
                or payload.get("probe") != "VZMacOSRestoreImage.latestSupported+HTTPS_HEAD"
                or payload.get("probe_executable_sha256") != executable_sha
                or Path(payload.get("probe_executable_path", "")).resolve() != binary.resolve()
                or payload.get("is_supported") is not True
                or payload.get("http_status") != 200
                or not _integer(payload.get("content_length_bytes"), minimum=1)):
            raise RuntimeError("restore metadata probe response failed its executable/HTTP/schema binding")
        host = _host_tuple(capture_host_observation()["host"])
        if (payload.get("operating_system_version") != host.get("product_version")
                or payload.get("build_version") != host.get("build_version")):
            raise RuntimeError("latest supported restore OS/build does not match the current host target")
        hardware = _hardware_compatibility_tuple(payload)
        source_url = payload.get("source_url")
        final_url = payload.get("final_url")
        if not isinstance(source_url, str) or not isinstance(final_url, str):
            raise RuntimeError("latest restore URL identity is missing")
        validate_restore_metadata(source_url, payload["content_length_bytes"], "0" * 64)
        validate_restore_metadata(final_url, payload["content_length_bytes"], "0" * 64)
        if (_controller_identity() != current_controller
                or verify_fresh_g0c_review() != current_g0c):
            raise RuntimeError("controller or fresh G0C changed during the metadata probe")
        base = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS",
            "probe": payload["probe"],
            "source_url": source_url,
            "final_url": final_url,
            "expected_length_bytes": payload["content_length_bytes"],
            "operating_system_version": payload["operating_system_version"],
            "build_version": payload["build_version"],
            "is_supported": payload["is_supported"],
            "host": host,
            "hardware_model_data_sha256": hardware["model_data_sha256"],
            "hardware_model_supported": hardware["model_supported_for_host"],
            "minimum_supported_cpu_count": hardware["minimum_supported_cpu_count"],
            "minimum_supported_memory_size_bytes": hardware["minimum_supported_memory_size_bytes"],
            "http_status": payload["http_status"],
            "headers": payload.get("headers"),
            "probe_source_sha256": source_sha,
            "probe_executable_sha256": executable_sha,
            "probe_entitlements_sha256": _sha256(entitlements),
            "probe_entitlements": {"com.apple.security.virtualization": True},
            "probe_entitlement_readback_sha256": _sha256(raw_entitlement_output),
            "probe_compile_stdout_sha256": _sha256(compile_result.stdout.encode()),
            "probe_compile_stderr_sha256": _sha256(compile_result.stderr.encode()),
            "captured_at_unix": time.time(),
            "receipt_path": receipt_path_text,
            "g0c_identity": current_g0c,
        }
        metadata_sha = _sha256(policy._canonical_pretty_json_bytes(base))
        base["metadata_sha256"] = metadata_sha
        if not math.isfinite(base["captured_at_unix"]):
            raise RuntimeError("restore metadata probe capture clock is not finite")
        return _write_signed_json_unique(receipt_path, base, "restore_metadata_hmac_sha256")


def _read_latest_restore_metadata_snapshot(
        path: Path = VM_RESTORE_METADATA_PATH, *, maximum_age_seconds: int | None = 300,
        expected_controller_identity: Mapping[str, Any] | None = None,
        expected_g0c_identity: Mapping[str, Any] | None = None,
        ) -> tuple[dict[str, Any], str, tuple[int, int, int, int, int]]:
    record, _raw, receipt_sha256, identity = _read_signed_record_snapshot(
        path, "restore_metadata_hmac_sha256", maximum_bytes=16 * 1024 * 1024)
    expected_fields = {
        "schema_version", "mode", "status", "probe", "source_url", "final_url",
        "expected_length_bytes", "operating_system_version", "build_version", "is_supported",
        "host", "hardware_model_data_sha256", "hardware_model_supported",
        "minimum_supported_cpu_count", "minimum_supported_memory_size_bytes", "http_status",
        "headers", "probe_source_sha256", "probe_executable_sha256",
        "probe_entitlements_sha256", "probe_entitlements", "probe_entitlement_readback_sha256",
        "probe_compile_stdout_sha256", "probe_compile_stderr_sha256", "captured_at_unix",
        "receipt_path", "g0c_identity", "metadata_sha256", "restore_metadata_hmac_sha256",
    }
    if set(record) != expected_fields:
        raise RuntimeError("latest restore metadata has missing or unknown schema fields")
    digest_input = {key: value for key, value in record.items()
                    if key not in {"metadata_sha256", "restore_metadata_hmac_sha256"}}
    if (record.get("status") != "PASS" or record.get("mode") != VM_MODE
            or record.get("probe") != "VZMacOSRestoreImage.latestSupported+HTTPS_HEAD"
            or record.get("metadata_sha256") != _sha256(policy._canonical_pretty_json_bytes(digest_input))):
        raise RuntimeError("latest Apple restore metadata record is not authentic or self-consistent")
    if record.get("receipt_path") is not None:
        _actual_path, actual_path_text = _repo_artifact_path(path)
        if record.get("receipt_path") != actual_path_text:
            raise RuntimeError("fresh metadata receipt path differs from its signed locator")
    if maximum_age_seconds is None:
        _require_nonfuture_timestamp(record.get("captured_at_unix"),
                                     label="historical Apple restore metadata")
    else:
        _require_fresh_timestamp(
            record.get("captured_at_unix"), label="latest Apple restore metadata",
            maximum_age_seconds=maximum_age_seconds)
    controller_identity = (expected_controller_identity if expected_controller_identity is not None
                           else _controller_identity())
    g0c_identity = (expected_g0c_identity if expected_g0c_identity is not None
                    else verify_fresh_g0c_review())
    if record.get("probe_source_sha256") != controller_identity.get("restore_metadata_probe_sha256"):
        raise RuntimeError("latest restore metadata was produced by different probe source bytes")
    if record.get("g0c_identity") != g0c_identity:
        raise RuntimeError("latest restore metadata is not bound to the current fresh G0C review")
    if (record.get("probe_entitlements") != {"com.apple.security.virtualization": True}
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_entitlements_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_executable_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_source_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_entitlement_readback_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_compile_stdout_sha256", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", record.get("probe_compile_stderr_sha256", ""))):
        raise RuntimeError("restore metadata probe executable or required entitlement is unbound")
    headers = record.get("headers")
    if (not isinstance(headers, Mapping)
            or set(headers) != {"etag", "last_modified", "digest", "content_md5",
                                "x_apple_content_sha256", "x_apple_signature"}
            or any(value is not None and not isinstance(value, str) for value in headers.values())):
        raise RuntimeError("restore metadata response headers have missing or unknown schema fields")
    if (not _integer(record.get("expected_length_bytes"), minimum=1)
            or record.get("http_status") != 200 or record.get("is_supported") is not True):
        raise RuntimeError("latest restore metadata lacks exact supported HTTP size/build evidence")
    try:
        host = _host_tuple(record.get("host"))
        _legacy_restore_subject(record, host)
        _hardware_compatibility_tuple(record)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"latest restore metadata subject or compatibility tuple is invalid: {error}") from error
    for field in ("source_url", "final_url"):
        url = record.get(field)
        if not isinstance(url, str):
            raise RuntimeError(f"latest restore metadata is missing {field}")
        validate_restore_metadata(url, record["expected_length_bytes"], record["metadata_sha256"])
    current_os = subprocess.run(
        ["/usr/bin/sw_vers", "-productVersion"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5.0).stdout.strip()
    current_build = subprocess.run(
        ["/usr/bin/sw_vers", "-buildVersion"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5.0).stdout.strip()
    if (record.get("operating_system_version") != current_os
            or record.get("build_version") != current_build
            or host.get("product_version") != current_os
            or host.get("build_version") != current_build
            or host.get("architecture") != platform.machine().lower()):
        raise RuntimeError("latest supported restore metadata no longer matches the current host OS/build")
    return record, receipt_sha256, identity


def _read_latest_restore_metadata(path: Path = VM_RESTORE_METADATA_PATH,
                                  *, maximum_age_seconds: int = 300) -> dict[str, Any]:
    record, _receipt_sha256, _identity = _read_latest_restore_metadata_snapshot(
        path, maximum_age_seconds=maximum_age_seconds)
    return record


def _repo_artifact_path(path: Path | str) -> tuple[Path, str]:
    selected = Path(path)
    if not selected.is_absolute():
        selected = policy.REPO / selected
    raw_parts = selected.parts
    if ".." in raw_parts:
        raise RuntimeError("evidence locator may not contain parent traversal")
    absolute = Path(os.path.abspath(selected))
    try:
        relative = absolute.relative_to(policy.REPO)
    except ValueError as error:
        raise RuntimeError("evidence locator is outside the approved repository root") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise RuntimeError("evidence locator is not canonical")
    return absolute, relative.as_posix()


def _metadata_observation_is_valid(record: Mapping[str, Any], *, probe_sha256: str) -> None:
    digest_input = {key: value for key, value in record.items()
                    if key not in {"metadata_sha256", "restore_metadata_hmac_sha256"}}
    if (record.get("schema_version") != 1 or record.get("mode") != VM_MODE
            or record.get("status") != "PASS"
            or record.get("probe") != "VZMacOSRestoreImage.latestSupported+HTTPS_HEAD"
            or record.get("probe_source_sha256") != probe_sha256
            or record.get("metadata_sha256") != _sha256(policy._canonical_pretty_json_bytes(digest_input))):
        raise RuntimeError("restore metadata observation is not self-consistent or bound to its probe")


def _accepted_p0_chain() -> dict[str, Any]:
    """Load only the fixed accepted P0 chain; never select a predecessor by freshness or subject."""
    admission, _admission_raw, admission_sha, _ = _read_signed_record_snapshot(
        VM_P0_ADMISSION_PATH, "vm_admission_hmac_sha256")
    review, _review_raw, review_sha, _ = _read_json_file_snapshot(VM_P0_REVIEW_PATH)
    creation, _creation_raw, creation_sha, _ = _read_signed_record_snapshot(
        VM_P0_CREATION_RECEIPT_PATH, "vm_creation_hmac_sha256")
    metadata, _metadata_raw, metadata_sha, _ = _read_signed_record_snapshot(
        VM_P0_METADATA_PATH, "restore_metadata_hmac_sha256", maximum_bytes=16 * 1024 * 1024)
    old_snapshot, _old_snapshot_raw, old_snapshot_sha, _ = _read_json_file_snapshot(
        VM_P0_G0C_SNAPSHOT_PATH)
    old_review, _old_review_raw, old_review_sha, _ = _read_json_file_snapshot(VM_P0_G0C_REVIEW_PATH)
    if (admission_sha != VM_P0_ADMISSION_SHA256 or review_sha != VM_P0_REVIEW_SHA256
            or creation_sha != VM_P0_CREATION_RECEIPT_SHA256
            or metadata_sha != VM_P0_METADATA_SHA256
            or old_snapshot_sha != VM_P0_G0C_SNAPSHOT_SHA256
            or old_review_sha != VM_P0_G0C_REVIEW_SHA256):
        raise RuntimeError("accepted P0 ancestry or historical G0C bytes differ from the pinned run")
    if (admission.get("schema_version") != 1 or admission.get("mode") != VM_MODE
            or admission.get("decision") != "PASS"
            or admission.get("authorization") != "P0_INTERNAL_PROVISIONING_ONLY"
            or any(admission.get(field) is not False for field in (
                "p1_authorized", "p1_started", "p2_authorized", "p2_started",
                "restore_download_authorized", "vm_start_authorized"))):
        raise RuntimeError("accepted P0 admission is not a completed P0-only decision")
    if (not isinstance(review.get("status"), str)
            or not review["status"].startswith("PASS")
            or review.get("run_id") != VM_P0_RUN_ID):
        raise RuntimeError("accepted P0 independent review is not a PASS for the anchored run")
    fresh_paths = admission.get("fresh_evidence_paths")
    restore_ref = fresh_paths.get("restore_metadata") if isinstance(fresh_paths, Mapping) else None
    creation_ref = fresh_paths.get("creation_receipt") if isinstance(fresh_paths, Mapping) else None
    if (not isinstance(restore_ref, Mapping) or not isinstance(creation_ref, Mapping)
            or restore_ref.get("path") != VM_P0_METADATA_PATH.relative_to(policy.REPO).as_posix()
            or restore_ref.get("sha256") != VM_P0_METADATA_SHA256
            or creation_ref.get("path") != VM_P0_CREATION_RECEIPT_PATH.relative_to(policy.REPO).as_posix()
            or creation_ref.get("sha256") != VM_P0_CREATION_RECEIPT_SHA256):
        raise RuntimeError("P0 admission does not bind the exact fixed creation and metadata ancestors")
    review_evidence = review.get("signed_evidence")
    if not isinstance(review_evidence, list):
        raise RuntimeError("P0 independent review omits its signed evidence index")
    expected_review_refs = {
        "final_admission": (VM_P0_ADMISSION_PATH.relative_to(policy.REPO).as_posix(),
                            VM_P0_ADMISSION_SHA256),
        "creation_receipt": (VM_P0_CREATION_RECEIPT_PATH.relative_to(policy.REPO).as_posix(),
                              VM_P0_CREATION_RECEIPT_SHA256),
        "fresh_metadata": (VM_P0_METADATA_PATH.relative_to(policy.REPO).as_posix(),
                           VM_P0_METADATA_SHA256),
    }
    for role, (expected_path, expected_hash) in expected_review_refs.items():
        matches = [item for item in review_evidence if isinstance(item, Mapping)
                   and item.get("role") == role]
        if (len(matches) != 1 or matches[0].get("path") != expected_path
                or matches[0].get("sha256") != expected_hash
                or matches[0].get("signature_verified_by_controller_api") is not True):
            raise RuntimeError(f"P0 independent review does not bind the exact {role} ancestor")
    expected_old_g0c = admission.get("g0c_identity")
    controller = admission.get("controller_identity")
    if (not isinstance(expected_old_g0c, Mapping) or not isinstance(controller, Mapping)
            or expected_old_g0c.get("snapshot_sha256") != VM_P0_G0C_SNAPSHOT_SHA256
            or expected_old_g0c.get("review_sha256") != VM_P0_G0C_REVIEW_SHA256
            or expected_old_g0c.get("snapshot_path") != VM_P0_G0C_SNAPSHOT_PATH.relative_to(policy.REPO).as_posix()
            or expected_old_g0c.get("review_path") != VM_P0_G0C_REVIEW_PATH.relative_to(policy.REPO).as_posix()):
        raise RuntimeError("P0 does not bind the accepted historical R6 G0C snapshot/review")
    if (old_snapshot.get("snapshot_id") != "G0C-INTERNAL-VM-CONTROLLER"
            or old_review.get("status") != "PASS"
            or old_review.get("snapshot_sha256") != VM_P0_G0C_SNAPSHOT_SHA256):
        raise RuntimeError("historical R6 G0C snapshot/review is not a valid accepted pair")
    old_files = old_snapshot.get("reviewed_controller_and_test_files")
    old_controller_entry = old_files.get("vm_capacity_controller") if isinstance(old_files, Mapping) else None
    old_test_entry = old_files.get("vm_capacity_tests") if isinstance(old_files, Mapping) else None
    source_identity = review.get("source_identity")
    old_test_identity = source_identity.get("controller_test") if isinstance(source_identity, Mapping) else None
    if (not isinstance(old_controller_entry, Mapping)
            or old_controller_entry.get("sha256") != controller.get("vm_capacity_controller_sha256")
            or not isinstance(old_test_entry, Mapping)
            or not isinstance(old_test_identity, Mapping)
            or old_test_entry.get("sha256") != old_test_identity.get("sha256")):
        raise RuntimeError("historical R6 G0C does not bind the original P0 controller/tests")
    production_vm = admission.get("production_vm_volume")
    if (creation.get("status") != "PASS" or creation.get("mode") != VM_MODE
            or not isinstance(production_vm, Mapping)
            or creation.get("volume_uuid") != production_vm.get("uuid")
            or creation.get("quota_bytes") != production_vm.get("quota_bytes")
            or creation.get("restore_metadata_receipt_sha256") != VM_P0_METADATA_SHA256
            or creation.get("g0c_identity") != expected_old_g0c
            or creation.get("restore_upstream_metadata", {}).get("metadata_sha256")
            != metadata.get("metadata_sha256")):
        raise RuntimeError("P0 creation receipt does not authenticate the direct metadata predecessor")
    _metadata_observation_is_valid(metadata, probe_sha256=controller.get("restore_metadata_probe_sha256"))
    before = creation.get("before_observation", {}).get("host")
    after = creation.get("after_observation", {}).get("host")
    before_tuple = _host_tuple(before)
    after_tuple = _host_tuple(after)
    if before_tuple != after_tuple:
        raise RuntimeError("P0 before/after host compatibility tuples disagree")
    legacy_subject = _legacy_restore_subject(metadata, after_tuple)
    return {
        "p0_admission": admission,
        "p0_review": review,
        "creation_receipt": creation,
        "metadata": metadata,
        "legacy_subject": legacy_subject,
        "legacy_restore_subject_identity": _restore_subject_digest(legacy_subject, legacy=True),
        "historical_controller_identity": dict(controller),
        "historical_g0c_identity": dict(expected_old_g0c),
        "p0_ancestry": {
            "run_id": VM_P0_RUN_ID,
            "p0_admission_path": VM_P0_ADMISSION_PATH.relative_to(policy.REPO).as_posix(),
            "p0_admission_sha256": admission_sha,
            "p0_review_path": VM_P0_REVIEW_PATH.relative_to(policy.REPO).as_posix(),
            "p0_review_sha256": review_sha,
            "creation_receipt_path": VM_P0_CREATION_RECEIPT_PATH.relative_to(policy.REPO).as_posix(),
            "creation_receipt_sha256": creation_sha,
            "metadata_receipt_path": VM_P0_METADATA_PATH.relative_to(policy.REPO).as_posix(),
            "metadata_receipt_sha256": metadata_sha,
            "metadata_observation_sha256": metadata["metadata_sha256"],
            "legacy_restore_subject_identity": _restore_subject_digest(legacy_subject, legacy=True),
            "historical_g0c_snapshot_sha256": old_snapshot_sha,
            "historical_g0c_review_sha256": old_review_sha,
        },
    }


def _require_unchanged_pending_p0_marker(marker: Mapping[str, Any],
                                         creation: Mapping[str, Any]) -> None:
    """Validate the post-P0 pending boundary by same-session signed marker fields.

    The creation receipt's active_marker_sha256 identifies the initial
    VOLUME_CREATE marker. P0 legitimately advanced that marker through the
    reserve and volume read-backs to P1_ADMISSION, so it is not a hash of the
    current marker. The transition receipt binds the current marker's exact
    bytes and rechecks them before sealing.
    """
    marker_created_at = marker.get("created_at_unix")
    marker_updated_at = marker.get("updated_at_unix")
    _require_nonfuture_timestamp(marker_created_at, label="P0 active marker creation")
    _require_nonfuture_timestamp(marker_updated_at, label="P0 active marker update")
    initial_marker_sha = creation.get("active_marker_sha256")
    initial_plan = creation.get("initial_phase_plan")
    initial_p1_phase = (initial_plan.get("phases", {}).get("P1")
                        if isinstance(initial_plan, Mapping)
                        and isinstance(initial_plan.get("phases"), Mapping) else None)
    if (not isinstance(initial_marker_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", initial_marker_sha)
            or marker.get("state") != "ACTIVE"
            or marker.get("phase_id") != "P1_ADMISSION"
            or marker.get("session_id") != creation.get("session_id")
            or marker.get("container_uuid") != EXPECTED_CONTAINER_UUID
            or marker.get("volume_uuid") != creation.get("volume_uuid")
            or marker.get("volume_name") != creation.get("volume_name")
            or marker.get("quota_bytes") != creation.get("quota_bytes")
            or marker.get("safety_volume_uuid") != creation.get("safety_volume_uuid")
            or marker.get("safety_volume_in_use_bytes") != creation.get("safety_volume_in_use_bytes")
            or marker.get("safety_reserve_bytes") != creation.get("safety_volume_reserve_bytes")
            or marker.get("safety_container_uuid") != creation.get("safety_container_uuid")
            or marker.get("safety_physical_store_uuid") != creation.get("safety_physical_store_uuid")
            or marker.get("pre_reserve_shared_free_bytes") != creation.get("pre_reserve_shared_free_bytes")
            or marker.get("unreserved_free_ceiling_bytes") != creation.get("unreserved_free_ceiling_bytes")
            or marker.get("plan_sha256") != _sha256(policy._canonical_pretty_json_bytes(initial_plan))
            or not isinstance(initial_p1_phase, Mapping)
            or marker.get("committed_growth_bytes") != initial_p1_phase.get("growth_bytes")
            or marker.get("p1_admission_path") is not None
            or marker.get("p1_admission_sha256") is not None):
        raise RuntimeError("active marker is not the unchanged post-P0 pending P1_ADMISSION boundary")


def _require_direct_p0_metadata_lineage(lineage: Mapping[str, Any], *,
                                        path_text: str,
                                        p0_ancestry: Mapping[str, Any]) -> None:
    """Reject refresh chains that do not point directly to the pinned P0 leaf."""
    if (lineage.get("record_type") != "R6_VM_RESTORE_METADATA_REFRESH_LINEAGE"
            or lineage.get("status") != "PASS"
            or lineage.get("receipt_path") != path_text
            or lineage.get("p0_ancestry") != p0_ancestry
            or lineage.get("predecessor") != {
                "type": "IMMUTABLE_P0_METADATA_RECEIPT",
                "sha256": p0_ancestry.get("metadata_receipt_sha256")}
            or lineage.get("comparison") != {"legacy_subject_equal": True, "changed_fields": []}):
        raise RuntimeError("metadata lineage is not the exact direct-P0 PASS refresh receipt")


def _restore_payload_entries(directory_fd: int) -> list[str]:
    """Return every canonical restore or partial payload entry, including symlinks."""
    return sorted(name for name in os.listdir(directory_fd)
                  if name == "restore.ipsw" or name.startswith(".restore.ipsw.partial-"))


def _active_marker_locator() -> str:
    """Record the configured cache marker path without assuming it is in the repo."""
    return Path(os.path.abspath(VM_ACTIVE_MARKER_PATH)).as_posix()


def _verify_review_artifact(path: Path, *, artifact_path: str,
                            artifact_sha256: str,
                            not_before_unix: float | None = None) -> tuple[dict[str, Any], str]:
    review, _raw, review_sha, _ = _read_json_file_snapshot(path)
    status = review.get("status")
    if (status != "PASS" or review.get("reviewed_artifact_path") != artifact_path
            or review.get("reviewed_artifact_sha256") != artifact_sha256
            or not isinstance(review.get("reviewer_identity"), str)
            or not review["reviewer_identity"].strip()):
        raise RuntimeError("independent review does not PASS the exact bound artifact")
    reviewed_at = review.get("reviewed_at_unix", review.get("created_at_unix"))
    reviewed_at_value = _require_nonfuture_timestamp(reviewed_at, label="independent review")
    if not_before_unix is not None:
        artifact_at = _require_nonfuture_timestamp(not_before_unix, label="reviewed artifact")
        if reviewed_at_value < artifact_at:
            raise RuntimeError("independent review predates the exact artifact it claims to review")
    return review, review_sha


def _fresh_artifact_path(prefix: str, run_id: str) -> Path:
    return policy.REPO / f"{prefix}-{run_id}.json"


def _new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def create_restore_metadata_refresh_lineage(
        fresh_metadata_path: Path, *, receipt_path: Path | None = None) -> dict[str, Any]:
    """Bind one fresh metadata leaf to the exact accepted P0 metadata ancestor."""
    p0 = _accepted_p0_chain()
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_metadata_path)
    fresh, fresh_sha, _fresh_identity = _read_latest_restore_metadata_snapshot(fresh_path)
    if fresh_sha == p0["p0_ancestry"]["metadata_receipt_sha256"]:
        raise RuntimeError("fresh metadata leaf must be distinct from the immutable P0 receipt")
    fresh_host = _host_tuple(fresh.get("host"))
    p0_subject = p0["legacy_subject"]
    fresh_subject = _legacy_restore_subject(fresh, fresh_host)
    fresh_captured_at, _age_during_lineage_validation = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at lineage creation",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    p0_subject_id = _restore_subject_digest(p0_subject, legacy=True)
    fresh_subject_id = _restore_subject_digest(fresh_subject, legacy=True)
    if p0_subject != fresh_subject or p0_subject_id != fresh_subject_id:
        raise RuntimeError("RESTORE SUBJECT CHANGED between immutable P0 and fresh metadata")
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    if (current_controller.get("storage_policy_sha256")
            != p0["historical_controller_identity"].get("storage_policy_sha256")):
        raise RuntimeError("storage policy changed across the P0 controller transition")
    run_id = _new_run_id()
    output_path = receipt_path or _fresh_artifact_path(VM_RESTORE_LINEAGE_PREFIX, run_id)
    output_path, output_path_text = _repo_artifact_path(output_path)
    if output_path.exists():
        raise FileExistsError("refresh-lineage receipt path already exists")
    created_at = time.time()
    if not math.isfinite(created_at):
        raise RuntimeError("current clock is not finite")
    final_p0 = _accepted_p0_chain()
    fresh_again, fresh_sha_again, fresh_identity_again = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    if (final_p0["p0_ancestry"] != p0["p0_ancestry"] or fresh_sha_again != fresh_sha
            or fresh_identity_again != _fresh_identity
            or fresh_again.get("metadata_sha256") != fresh.get("metadata_sha256")):
        raise RuntimeError("P0 or fresh metadata bytes changed while the lineage was being created")
    fresh_captured_at, age_at_lineage_creation = _require_fresh_timestamp(
        fresh_again.get("captured_at_unix"), label="fresh metadata at lineage sealing",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=created_at)
    if (_controller_identity() != current_controller
            or verify_fresh_g0c_review() != current_g0c):
        raise RuntimeError("controller or current G0C changed while the lineage was being created")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_METADATA_REFRESH_LINEAGE",
        "mode": VM_MODE,
        "status": "PASS",
        "run_id": run_id,
        "created_at_unix": created_at,
        "purpose": "P0_ANCESTOR_TO_FRESH_CURRENT_OBSERVATION",
        "p0_ancestry": p0["p0_ancestry"],
        "fresh_observation": {
            "receipt_path": fresh_path_text,
            "receipt_sha256": fresh_sha,
            "metadata_observation_sha256": fresh["metadata_sha256"],
            "captured_at_unix": fresh_captured_at,
            "age_at_lineage_creation_seconds": age_at_lineage_creation,
            "host": fresh_host,
            "legacy_restore_subject_identity": fresh_subject_id,
        },
        "predecessor": {"type": "IMMUTABLE_P0_METADATA_RECEIPT",
                        "sha256": p0["p0_ancestry"]["metadata_receipt_sha256"]},
        "historical_controller_identity": p0["historical_controller_identity"],
        "historical_g0c_identity": p0["historical_g0c_identity"],
        "current_controller_identity": current_controller,
        "current_g0c_identity": current_g0c,
        "probe_source_transition": {
            "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
            "current_probe_sha256": current_controller["restore_metadata_probe_sha256"],
            "current_g0c_reviewed_current_probe": True,
        },
        "comparison": {"legacy_subject_equal": True, "changed_fields": []},
        "receipt_path": output_path_text,
    }
    return _write_signed_json_unique(output_path, record,
                                     "vm_metadata_refresh_lineage_hmac_sha256")


def create_restore_metadata_compatibility_basis(
        lineage_path: Path, *, receipt_path: Path | None = None) -> dict[str, Any]:
    """Adopt one fresh VZ hardware tuple without claiming historical P0 equivalence."""
    p0 = _accepted_p0_chain()
    lineage_absolute, lineage_path_text = _repo_artifact_path(lineage_path)
    lineage, _lineage_raw, lineage_sha, lineage_identity = _read_signed_record_snapshot(
        lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256")
    _require_direct_p0_metadata_lineage(
        lineage, path_text=lineage_path_text, p0_ancestry=p0["p0_ancestry"])
    _require_nonfuture_timestamp(lineage.get("created_at_unix"), label="metadata refresh lineage")
    fresh_ref = lineage.get("fresh_observation")
    if not isinstance(fresh_ref, Mapping):
        raise RuntimeError("refresh lineage omits its fresh metadata leaf")
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_ref.get("receipt_path", ""))
    fresh, fresh_sha, fresh_identity = _read_latest_restore_metadata_snapshot(fresh_path)
    if (fresh_path_text != fresh_ref.get("receipt_path") or fresh_sha != fresh_ref.get("receipt_sha256")
            or fresh.get("metadata_sha256") != fresh_ref.get("metadata_observation_sha256")):
        raise RuntimeError("fresh metadata receipt changed after lineage creation")
    fresh_host = _host_tuple(fresh.get("host"))
    if fresh_ref.get("host") != fresh_host:
        raise RuntimeError("lineage host tuple differs from its signed fresh metadata observation")
    legacy_subject = _legacy_restore_subject(fresh, fresh_host)
    legacy_identity = _restore_subject_digest(legacy_subject, legacy=True)
    complete_subject = _complete_restore_subject(legacy_subject, fresh)
    if (legacy_identity != p0["p0_ancestry"]["legacy_restore_subject_identity"]
            or legacy_identity != fresh_ref.get("legacy_restore_subject_identity")):
        raise RuntimeError("fresh legacy restore subject differs from immutable P0 lineage")
    _require_fresh_timestamp(fresh.get("captured_at_unix"),
                             label="fresh metadata at compatibility-basis creation",
                             maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    if (lineage.get("current_controller_identity") != current_controller
            or lineage.get("current_g0c_identity") != current_g0c
            or lineage.get("historical_controller_identity") != p0["historical_controller_identity"]
            or lineage.get("historical_g0c_identity") != p0["historical_g0c_identity"]
            or lineage.get("probe_source_transition") != {
                "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
                "current_probe_sha256": current_controller["restore_metadata_probe_sha256"],
                "current_g0c_reviewed_current_probe": True}):
        raise RuntimeError("refresh lineage was produced under different current controller/G0C bytes")
    run_id = _new_run_id()
    output_path = receipt_path or _fresh_artifact_path(VM_RESTORE_COMPATIBILITY_BASIS_PREFIX, run_id)
    output_path, output_path_text = _repo_artifact_path(output_path)
    if output_path.exists():
        raise FileExistsError("compatibility-basis receipt path already exists")
    now = time.time()
    if not math.isfinite(now):
        raise RuntimeError("current clock is not finite")
    p0_again = _accepted_p0_chain()
    lineage_again, _lineage_raw_again, lineage_sha_again, lineage_identity_again = (
        _read_signed_record_snapshot(lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256"))
    fresh_again, fresh_sha_again, fresh_identity_again = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    _fresh_captured_at, fresh_age_at_basis = _require_fresh_timestamp(
        fresh_again.get("captured_at_unix"), label="fresh metadata at compatibility-basis sealing",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=now)
    if (p0_again["p0_ancestry"] != p0["p0_ancestry"]
            or lineage_sha_again != lineage_sha or lineage_identity_again != lineage_identity
            or fresh_sha_again != fresh_sha or fresh_identity_again != fresh_identity
            or fresh_again.get("metadata_sha256") != fresh.get("metadata_sha256")
            or lineage_again.get("receipt_path") != lineage_path_text
            or lineage_again.get("p0_ancestry") != p0["p0_ancestry"]):
        raise RuntimeError("P0, lineage, or fresh metadata bytes changed while sealing compatibility basis")
    if (_controller_identity() != current_controller
            or verify_fresh_g0c_review() != current_g0c):
        raise RuntimeError("controller or current G0C changed while sealing compatibility basis")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_RESTORE_METADATA_COMPATIBILITY_BASIS",
        "mode": VM_MODE,
        "status": "PASS",
        "run_id": run_id,
        "created_at_unix": now,
        "decision": "ADOPT_CURRENT_HARDWARE_COMPATIBILITY_FOR_P1",
        "p0_hardware_fields_were_not_captured": True,
        "retroactive_p0_hardware_equivalence_claim": False,
        "restore_download_authorized": False,
        "p1_authorized": False,
        "p2_authorized": False,
        "vm_start_authorized": False,
        "p0_ancestry": p0["p0_ancestry"],
        "lineage_path": lineage_path_text,
        "lineage_sha256": lineage_sha,
        "fresh_metadata_path": fresh_path_text,
        "fresh_metadata_sha256": fresh_sha,
        "fresh_metadata_observation_sha256": fresh["metadata_sha256"],
        "fresh_metadata_age_at_basis_seconds": fresh_age_at_basis,
        "legacy_restore_subject_identity": p0["p0_ancestry"]["legacy_restore_subject_identity"],
        "adopted_hardware_compatibility": _hardware_compatibility_tuple(fresh),
        "restore_subject_identity": _restore_subject_digest(complete_subject),
        "restore_subject": complete_subject,
        "controller_identity": current_controller,
        "g0c_identity": current_g0c,
        "receipt_path": output_path_text,
    }
    return _write_signed_json_unique(output_path, record,
                                     "vm_restore_compatibility_basis_hmac_sha256")


def create_p0_revalidation_transition(
        lineage_path: Path, compatibility_basis_path: Path, basis_review_path: Path,
        *, receipt_path: Path | None = None,
        observation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Create a read-only, no-authority transition receipt for immutable P0."""
    p0 = _accepted_p0_chain()
    lineage_absolute, lineage_path_text = _repo_artifact_path(lineage_path)
    lineage, _lineage_raw, lineage_sha, lineage_identity = _read_signed_record_snapshot(
        lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256")
    basis_absolute, basis_path_text = _repo_artifact_path(compatibility_basis_path)
    basis, _basis_raw, basis_sha, basis_identity = _read_signed_record_snapshot(
        basis_absolute, "vm_restore_compatibility_basis_hmac_sha256")
    basis_review_absolute, basis_review_path_text = _repo_artifact_path(basis_review_path)
    _basis_review, basis_review_sha = _verify_review_artifact(
        basis_review_absolute, artifact_path=basis_path_text, artifact_sha256=basis_sha,
        not_before_unix=basis.get("created_at_unix"))
    _require_direct_p0_metadata_lineage(
        lineage, path_text=lineage_path_text, p0_ancestry=p0["p0_ancestry"])
    if (basis.get("record_type") != "R6_VM_RESTORE_METADATA_COMPATIBILITY_BASIS"
            or basis.get("receipt_path") != basis_path_text
            or basis.get("status") != "PASS" or basis.get("p0_ancestry") != p0["p0_ancestry"]
            or basis.get("lineage_path") != lineage_path_text
            or basis.get("lineage_sha256") != lineage_sha
            or basis.get("p0_hardware_fields_were_not_captured") is not True
            or basis.get("retroactive_p0_hardware_equivalence_claim") is not False
            or basis.get("decision") != "ADOPT_CURRENT_HARDWARE_COMPATIBILITY_FOR_P1"
            or basis.get("restore_download_authorized") is not False
            or basis.get("p1_authorized") is not False
            or basis.get("p2_authorized") is not False
            or basis.get("vm_start_authorized") is not False):
        raise RuntimeError("P0 transition requires the exact no-retroactive-claim compatibility basis")
    _require_nonfuture_timestamp(lineage.get("created_at_unix"), label="metadata refresh lineage")
    _require_nonfuture_timestamp(basis.get("created_at_unix"), label="hardware compatibility basis")
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    if (basis.get("controller_identity") != current_controller
            or basis.get("g0c_identity") != current_g0c
            or lineage.get("current_controller_identity") != current_controller
            or lineage.get("current_g0c_identity") != current_g0c
            or lineage.get("historical_controller_identity") != p0["historical_controller_identity"]
            or lineage.get("historical_g0c_identity") != p0["historical_g0c_identity"]
            or lineage.get("probe_source_transition") != {
                "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
                "current_probe_sha256": current_controller["restore_metadata_probe_sha256"],
                "current_g0c_reviewed_current_probe": True}):
        raise RuntimeError("lineage/basis do not bind the current controller and G0C")
    fresh_ref = lineage.get("fresh_observation")
    if not isinstance(fresh_ref, Mapping):
        raise RuntimeError("P0 transition lineage omits the fresh metadata receipt")
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_ref.get("receipt_path", ""))
    fresh, fresh_sha, fresh_identity = _read_latest_restore_metadata_snapshot(fresh_path)
    if (fresh_path_text != fresh_ref.get("receipt_path") or fresh_sha != fresh_ref.get("receipt_sha256")
            or fresh_sha != basis.get("fresh_metadata_sha256")
            or fresh.get("metadata_sha256") != fresh_ref.get("metadata_observation_sha256")
            or fresh.get("metadata_sha256") != basis.get("fresh_metadata_observation_sha256")):
        raise RuntimeError("fresh metadata leaf or adopted hardware basis changed")
    fresh_host = _host_tuple(fresh.get("host"))
    if fresh_ref.get("host") != fresh_host:
        raise RuntimeError("lineage host tuple differs from its signed fresh metadata observation")
    legacy_subject = _legacy_restore_subject(fresh, fresh_host)
    complete_subject = _complete_restore_subject(legacy_subject, fresh)
    legacy_identity = _restore_subject_digest(legacy_subject, legacy=True)
    complete_identity = _restore_subject_digest(complete_subject)
    if (legacy_identity != basis.get("legacy_restore_subject_identity")
            or complete_identity != basis.get("restore_subject_identity")
            or complete_subject != basis.get("restore_subject")
            or _hardware_compatibility_tuple(fresh) != basis.get("adopted_hardware_compatibility")):
        raise RuntimeError("fresh metadata subject differs from the signed compatibility basis")
    _require_fresh_timestamp(fresh.get("captured_at_unix"),
                             label="fresh metadata at P0 transition creation",
                             maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)

    marker, _marker_raw, marker_sha, marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    creation = p0["creation_receipt"]
    _require_unchanged_pending_p0_marker(marker, creation)
    if (creation.get("volume_uuid") != EXPECTED_PRODUCTION_VM_VOLUME_UUID
            or creation.get("safety_volume_uuid") != EXPECTED_PRODUCTION_SAFETY_VOLUME_UUID
            or creation.get("quota_bytes") != EXPECTED_PRODUCTION_VM_QUOTA_BYTES
            or creation.get("safety_volume_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES):
        raise RuntimeError("active marker is not the unchanged pending P0 P1_ADMISSION boundary")
    live = dict(observation) if observation is not None else capture_host_observation()
    _live_captured, _live_age = _require_fresh_timestamp(
        live.get("captured_at_unix"), label="P0 transition live APFS observation",
        maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
    blockers = _container_blockers(
        live, expected_vm_volume_uuid=creation["volume_uuid"],
        expected_vm_quota_bytes=creation["quota_bytes"],
        expected_safety_volume_uuid=creation["safety_volume_uuid"],
        expected_safety_volume_in_use_bytes=creation.get("safety_volume_in_use_bytes"))
    if blockers:
        raise RuntimeError("P0 transition live APFS state is blocked: " + "; ".join(blockers))
    mount = _volume_mount_info(live, creation["volume_uuid"])
    if (mount.get("mount_state") != "MOUNTED_VERIFIED"
            or mount.get("mount_point") != "/Volumes/FGMetal-VM"
            or mount.get("volume_uuid") != creation["volume_uuid"]):
        raise RuntimeError("P0 transition VM volume is not at its exact verified mount")
    contract = {
        "pre_reserve_shared_free_bytes": marker.get("pre_reserve_shared_free_bytes"),
        "unreserved_free_ceiling_bytes": marker.get("unreserved_free_ceiling_bytes"),
    }
    free_bytes = conservative_session_unreserved_free_bytes(
        live, volume_uuid=creation["volume_uuid"], capacity_contract=contract)
    if free_bytes <= VM_OPERATIONAL_UNRESERVED_FREE_MIN:
        raise RuntimeError("P0 transition shared free fell below the 5 GiB hard floor")
    if free_bytes <= VM_OPERATIONAL_WARNING_FREE:
        raise RuntimeError("P0 transition shared free reached the 8 GiB early-stop threshold")
    mount_root = Path(mount["mount_point"])
    root_fd = policy._open_directory_nofollow(mount_root)
    try:
        payload_names = _restore_payload_entries(root_fd)
        if payload_names:
            raise RuntimeError(
                "P0 transition refuses existing restore payload state: "
                + ", ".join(sorted(payload_names)))
    finally:
        os.close(root_fd)
    for forbidden in (VM_P2_ADMISSION_PATH, VM_P2_INSTALL_RECEIPT_PATH,
                      VM_P2_MONITOR_RECEIPT_PATH, VM_RESTORE_VERIFICATION_PATH):
        directory_fd = policy._open_directory_nofollow(forbidden.parent)
        try:
            os.stat(forbidden.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError(f"P0 transition refuses existing P1/P2 execution evidence: {forbidden.name}")
        finally:
            os.close(directory_fd)
    p1_directory_fd = policy._open_directory_nofollow(policy.REPO)
    try:
        for name in os.listdir(p1_directory_fd):
            if name.startswith(VM_P1_ADMISSION_PREFIX + "-") and name.endswith(".json"):
                raise RuntimeError("P0 transition refuses existing run-specific P1 admission evidence")
    finally:
        os.close(p1_directory_fd)
    run_id = _new_run_id()
    output_path = receipt_path or _fresh_artifact_path(VM_P0_REVALIDATION_PREFIX, run_id)
    output_path, output_path_text = _repo_artifact_path(output_path)
    if output_path.exists():
        raise FileExistsError("P0 transition receipt path already exists")
    created_at = time.time()
    if not math.isfinite(created_at):
        raise RuntimeError("current clock is not finite")
    _fresh_captured_at, fresh_age_at_transition = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at P0 transition sealing",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=created_at)
    _require_fresh_timestamp(
        live.get("captured_at_unix"), label="P0 transition live APFS observation at sealing",
        maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS, now=created_at)
    p0_again = _accepted_p0_chain()
    lineage_again, _lineage_raw_again, lineage_sha_again, lineage_identity_again = (
        _read_signed_record_snapshot(lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256"))
    basis_again, _basis_raw_again, basis_sha_again, basis_identity_again = (
        _read_signed_record_snapshot(basis_absolute, "vm_restore_compatibility_basis_hmac_sha256"))
    fresh_again, fresh_sha_again, fresh_identity_again = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    if (p0_again["p0_ancestry"] != p0["p0_ancestry"]
            or lineage_sha_again != lineage_sha or lineage_identity_again != lineage_identity
            or basis_sha_again != basis_sha or basis_identity_again != basis_identity
            or fresh_sha_again != fresh_sha or fresh_identity_again != fresh_identity
            or fresh_again.get("metadata_sha256") != fresh.get("metadata_sha256")
            or lineage_again.get("receipt_path") != lineage_path_text
            or basis_again.get("receipt_path") != basis_path_text):
        raise RuntimeError("P0, lineage, basis, or fresh metadata bytes changed while sealing transition")
    _basis_review_again, basis_review_sha_again = _verify_review_artifact(
        basis_review_absolute, artifact_path=basis_path_text, artifact_sha256=basis_sha,
        not_before_unix=basis.get("created_at_unix"))
    if basis_review_sha_again != basis_review_sha:
        raise RuntimeError("compatibility-basis review bytes changed while sealing transition")
    if (_controller_identity() != current_controller
            or verify_fresh_g0c_review() != current_g0c):
        raise RuntimeError("controller or current G0C changed while sealing transition")
    _marker_after, _marker_raw_after, marker_sha_after, marker_identity_after = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if marker_sha_after != marker_sha or marker_identity_after != marker_identity:
        raise RuntimeError("active marker changed during the read-only transition")
    live_sha = _sha256(policy._canonical_pretty_json_bytes(live))
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_P0_REVALIDATED_FOR_P1",
        "mode": VM_MODE,
        "run_id": run_id,
        "created_at_unix": created_at,
        "decision": "PASS",
        "transition_scope": "P0_COMPATIBILITY_REVALIDATION_ONLY",
        "p1_authorized": False,
        "p2_authorized": False,
        "vm_start_authorized": False,
        "restore_download_authorized": False,
        "p0_ancestry": p0["p0_ancestry"],
        "historical_creation_receipt": {
            "path": p0["p0_ancestry"]["creation_receipt_path"],
            "sha256": p0["p0_ancestry"]["creation_receipt_sha256"],
            "session_id": creation["session_id"],
            "volume_uuid": creation["volume_uuid"],
        },
        "restore_lineage": {"path": lineage_path_text, "sha256": lineage_sha},
        "compatibility_basis": {
            "path": basis_path_text, "sha256": basis_sha,
            "review_path": basis_review_path_text, "review_sha256": basis_review_sha,
        },
        "fresh_metadata": {
            "path": fresh_path_text,
            "receipt_sha256": fresh_sha,
            "observation_sha256": fresh["metadata_sha256"],
            "age_at_transition_seconds": fresh_age_at_transition,
            "legacy_restore_subject_identity": legacy_identity,
            "restore_subject_identity": complete_identity,
        },
        "historical_p0_controller_identity": p0["historical_controller_identity"],
        "historical_p0_g0c_identity": p0["historical_g0c_identity"],
        "probe_source_transition": {
            "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
            "current_probe_sha256": current_controller["restore_metadata_probe_sha256"],
            "current_g0c_reviewed_current_probe": True,
        },
        "current_controller_identity": current_controller,
        "current_g0c_identity": current_g0c,
        "live_storage": {
            "captured_at_unix": live["captured_at_unix"],
            "observation_sha256": live_sha,
            "observation": live,
            "shared_unreserved_free_bytes": free_bytes,
            "vm_mount": mount,
        },
        "active_marker": {"path": _active_marker_locator(),
                          "sha256": marker_sha, "state": marker["state"],
                          "phase_id": marker["phase_id"],
                          "session_id": marker["session_id"],
                          "volume_uuid": marker["volume_uuid"]},
        "receipt_path": output_path_text,
        "blockers": [],
    }
    return _write_signed_json_unique(output_path, record, "vm_p0_revalidation_hmac_sha256")


def calculate_vm_phase_admission(*, mode: str, container_uuid: str,
                                 free_bytes: int | None, quota_bytes: int | None,
                                 quota_used_bytes: int | None, growth_bytes: int | None,
                                 uncertainty_bytes: int | None,
                                 concurrent_growth_bytes: int | None,
                                 expected_quota_bytes: int = VM_REVIEWED_QUOTA_MAX_BYTES,
                                 volume_uuid: str | None = None,
                                 logical_capacity_bytes: int | None = None,
                                 maximum_logical_capacity_bytes: int | None = None) -> dict[str, Any]:
    """Admit against live shared free and exact current VM quota headroom."""
    inputs = {
        "free_bytes": free_bytes,
        "quota_bytes": quota_bytes,
        "quota_used_bytes": quota_used_bytes,
        "growth_bytes": growth_bytes,
        "uncertainty_bytes": uncertainty_bytes,
    }
    blockers: list[str] = []
    if mode != VM_MODE:
        blockers.append("VM capacity exception requires explicit INTERNAL_VM_CAPACITY_DOMAIN authorization")
    if container_uuid != EXPECTED_CONTAINER_UUID:
        blockers.append("VM phase is not bound to the authorized shared APFS container")
    for name, value in inputs.items():
        if not _integer(value):
            blockers.append(f"VM phase input is unknown or invalid: {name}")
    if (not _integer(expected_quota_bytes, minimum=1)
            or quota_bytes != expected_quota_bytes):
        blockers.append("VM volume quota differs from its exact dynamic signed policy")
    if volume_uuid is not None and not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid):
        blockers.append("VM phase volume UUID is malformed")
    if (logical_capacity_bytes is not None and maximum_logical_capacity_bytes is not None
            and (not _integer(logical_capacity_bytes)
                 or not _integer(maximum_logical_capacity_bytes)
                 or logical_capacity_bytes > maximum_logical_capacity_bytes)):
        blockers.append("sparse RAW logical capacity exceeds its explicit per-image limit")
    elif (logical_capacity_bytes is None) != (maximum_logical_capacity_bytes is None):
        blockers.append("sparse RAW logical capacity or its explicit limit is unknown")
    if any(not _integer(value) for value in inputs.values()):
        return {
            "mode": mode, "container_uuid": container_uuid, **inputs,
            "admitted": False, "blockers": blockers,
            "warnings": [],
        }
    assert free_bytes is not None and quota_bytes is not None
    assert quota_used_bytes is not None and growth_bytes is not None
    assert uncertainty_bytes is not None
    if quota_used_bytes > quota_bytes:
        blockers.append("VM volume CapacityInUse exceeds its exact APFS quota")
        quota_headroom = 0
    else:
        quota_headroom = quota_bytes - quota_used_bytes
    quota_demand = growth_bytes + uncertainty_bytes
    projected_quota_headroom = quota_headroom - quota_demand
    if free_bytes <= VM_OPERATIONAL_UNRESERVED_FREE_MIN:
        blockers.append("shared APFS free space is at or below the 5 GiB operational hard floor")
    elif free_bytes <= VM_OPERATIONAL_WARNING_FREE:
        blockers.append("shared APFS free space reached the 8 GiB early-stop threshold")
    if quota_demand > quota_headroom:
        blockers.append("identified VM phase file data exceeds remaining APFS quota headroom")
    warnings = []
    if free_bytes <= VM_OPERATIONAL_WARNING_FREE:
        warnings.append("shared APFS free space is at or below the 8 GiB early-stop threshold")
    return {
        "mode": mode,
        "container_uuid": container_uuid,
        "volume_uuid": volume_uuid,
        **inputs,
        "quota_headroom_bytes": quota_headroom,
        "expected_quota_bytes": expected_quota_bytes,
        "quota_demand_bytes": quota_demand,
        "projected_quota_headroom_bytes": projected_quota_headroom,
        "shared_free_bytes": free_bytes,
        "operational_free_floor_bytes": VM_OPERATIONAL_UNRESERVED_FREE_MIN,
        "early_stop_threshold_bytes": VM_OPERATIONAL_WARNING_FREE,
        "concurrent_growth_observation_bytes": concurrent_growth_bytes,
        "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
        "admitted": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


def initial_p1_p2_plan(restore_expected_bytes: int | None,
                       concurrent_growth_bound_bytes: int | None) -> dict[str, Any]:
    """Describe known P1/P2 data without granting sampled-growth authority."""
    if not _integer(restore_expected_bytes, minimum=1):
        return {"status": "FAIL", "blockers": ["latest restore expected length is unknown"],
                "phases": {}}
    p1_growth = restore_expected_bytes + VM_P1_METADATA_MAX_BYTES
    p2_growth = (restore_expected_bytes + VM_P2_SYSTEM_FILE_DATA_MAX_BYTES
                 + VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES)
    return {
        "policy_id": VM_MODE,
        "restore_expected_bytes": restore_expected_bytes,
        "restore_local_sha256": None,
        "restore_local_hash_state": "PENDING",
        "per_image_logical_limits": {
            "guest_system_raw_bytes": VM_SYSTEM_LOGICAL_CAPACITY_BYTES,
            "auxiliary_raw_bytes": VM_AUXILIARY_LOGICAL_CAPACITY_BYTES,
        },
        "phases": {
            "P1": {
                "growth_bytes": p1_growth,
                "uncertainty_bytes": _phase_uncertainty(p1_growth),
                "concurrent_growth_observation_bytes": concurrent_growth_bound_bytes,
                "coexisting": ["restore_ipsw", "p1_metadata"],
                "authorized_by_initial_pass": True,
            },
            "P2": {
                "growth_bytes": p2_growth,
                "uncertainty_bytes": 0,
                "concurrent_growth_observation_bytes": concurrent_growth_bound_bytes,
                "component_maxima_bytes": {
                    "restore_ipsw_user_file_data": restore_expected_bytes,
                    "guest_system_user_file_data": VM_P2_SYSTEM_FILE_DATA_MAX_BYTES,
                    "auxiliary_storage_user_file_data": VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES,
                },
                "coexisting": ["restore_ipsw", "guest_system_raw", "auxiliary_storage"],
                "authorized_by_initial_pass": False,
                "requires_fresh_revalidation_before_install": True,
            },
        },
        "status": "PENDING_LIVE_MEASUREMENT",
        "blockers": [],
    }


def evaluate_initial_plan(observation: Mapping[str, Any], restore_expected_bytes: int | None,
                          *, vm_volume_uuid: str | None = None,
                          vm_quota_used_bytes: int = 0,
                          vm_quota_bytes: int | None = None,
                          safety_volume_uuid: str | None = None,
                          safety_volume_in_use_bytes: int | None = None,
                          concurrent_growth_bound_bytes: int | None = None,
                          shared_free_bytes_override: int | None = None) -> dict[str, Any]:
    plan = initial_p1_p2_plan(restore_expected_bytes, concurrent_growth_bound_bytes)
    blockers = _container_blockers(
        observation, expected_vm_volume_uuid=vm_volume_uuid,
        expected_vm_quota_bytes=vm_quota_bytes,
        expected_safety_volume_uuid=safety_volume_uuid,
        expected_safety_volume_in_use_bytes=safety_volume_in_use_bytes)
    phases: dict[str, Any] = {}
    if plan.get("status") == "FAIL":
        blockers.extend(plan.get("blockers", []))
    if shared_free_bytes_override is not None:
        if not _integer(shared_free_bytes_override):
            free = None
            blockers.append("session-bounded shared free capacity is invalid")
        else:
            free = shared_free_bytes_override
    else:
        try:
            free = conservative_container_free_bytes(observation)
        except (TypeError, ValueError) as error:
            free = None
            blockers.append(str(error))
    phase_blockers: dict[str, list[str]] = {}
    for phase_id, phase in plan.get("phases", {}).items():
        if phase_id != "P1":
            phases[phase_id] = {
                "authorized_by_initial_pass": False,
                "requires_fresh_revalidation_before_install": True,
                "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
                "admitted": False,
                "blockers": ["P2 requires fresh direct admission at its phase boundary"],
            }
            phase_blockers[phase_id] = list(phases[phase_id]["blockers"])
            continue
        phase_growth = max(0, phase.get("growth_bytes", 0) - vm_quota_used_bytes)
        phase_quota = vm_quota_bytes
        if phase_quota is None:
            dynamic = calculate_dynamic_vm_quota(
                free_bytes=free,
                deterministic_uncertainty_bytes=VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES,
                admitted_coexistence_bytes=0,
                minimum_required_quota_bytes=phase.get("growth_bytes"),
                maximum_quota_bytes=VM_REVIEWED_QUOTA_MAX_BYTES)
            blockers.extend(dynamic["blockers"])
            phase_quota = dynamic.get("quota_bytes")
        result = calculate_vm_phase_admission(
            mode=VM_MODE,
            container_uuid=str(observation.get("apfs_container", {}).get("APFSContainerUUID", "")),
            volume_uuid=vm_volume_uuid,
            free_bytes=free,
            quota_bytes=phase_quota,
            quota_used_bytes=vm_quota_used_bytes,
            growth_bytes=phase_growth,
            uncertainty_bytes=_phase_uncertainty(phase_growth),
            concurrent_growth_bytes=phase.get("concurrent_growth_bytes"),
            expected_quota_bytes=phase_quota if _integer(phase_quota, minimum=1)
            else VM_REVIEWED_QUOTA_MAX_BYTES,
        )
        result["identified_p1_data_bytes"] = phase.get("growth_bytes")
        result["additional_growth_from_current_use_bytes"] = phase_growth
        phases[phase_id] = result
        phase_blockers[phase_id] = list(result["blockers"])
        if phase_id == "P1":
            blockers.extend(f"P1: {item}" for item in result["blockers"])
    return {
        "plan": plan,
        "phases": phases,
        "admitted_for_p1": plan.get("status") != "FAIL" and not blockers,
        "authorized_phase": "P1" if plan.get("status") != "FAIL" and not blockers else None,
        "planned_future_phase_blockers": {key: value for key, value in phase_blockers.items()
                                          if key != "P1" and value},
        "blockers": blockers,
    }


class VMPhaseViolation(RuntimeError):
    """A VM phase crossed a physical or quota boundary and was stopped."""

    def __init__(self, message: str, record: Mapping[str, Any]):
        super().__init__(message)
        self.record = dict(record)


def _sample_tracked_raw_images(*, observation: Mapping[str, Any], session_id: str, volume_uuid: str,
                               receipt_paths: Sequence[Path]) -> dict[str, Any]:
    blockers: list[str] = []
    measurements: list[dict[str, Any]] = []
    allowed_root = (policy.REPO / "R6-VM-P5-COMPONENTS").resolve()
    try:
        mount = _volume_mount_info(observation, volume_uuid)
        if not mount.get("mounted"):
            raise RuntimeError("bound VM volume is not mounted")
        mount_path = Path(mount["mount_point"])
        root_fd = policy._open_directory_nofollow(mount_path)
    except (OSError, RuntimeError, ValueError) as error:
        return {"measurements": [], "blockers": [f"tracked RAW volume identity is unknown: {error}"]}
    try:
        for path in receipt_paths:
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(allowed_root):
                    raise RuntimeError("tracked RAW receipt is outside the P5 receipt root")
                record = _read_signed_record(resolved, "vm_component_hmac_sha256")
                role = record.get("role")
                expected_names = {
                    "guest_system_raw": "guest-system-p5.raw",
                    "read_only_input_tool_raw": "read-only-input.raw",
                    "workspace_raw": "workspace.raw",
                    "export_capsule_raw": "export-capsule.raw",
                }
                if (record.get("status") != "PASS" or record.get("phase_id") != "P5"
                        or record.get("session_id") != session_id
                        or record.get("volume_uuid") != volume_uuid
                        or record.get("container_uuid") != EXPECTED_CONTAINER_UUID
                        or role not in expected_names
                        or record.get("basename") != expected_names.get(role)):
                    raise RuntimeError("tracked RAW receipt identity or role is invalid")
                file_name = expected_names[role]
                fd = os.open(file_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
                try:
                    info = os.fstat(fd)
                    expected = record.get("file_identity")
                    allocated = getattr(info, "st_blocks", 0) * 512
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_dev != os.fstat(root_fd).st_dev
                            or info.st_size != record.get("logical_capacity_bytes")
                            or not isinstance(expected, Mapping)
                            or (info.st_dev, info.st_ino) != (expected.get("device"), expected.get("inode"))):
                        raise RuntimeError("tracked RAW inode, volume, or logical capacity changed")
                    maximum = record.get("maximum_physical_allocation_bytes")
                    if not _integer(maximum) or allocated > maximum:
                        raise RuntimeError("tracked RAW physical allocation exceeded its signed per-image cap")
                    measurements.append({
                        "role": role,
                        "logical_capacity_bytes": info.st_size,
                        "physical_allocated_bytes": allocated,
                        "maximum_physical_allocation_bytes": maximum,
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    })
                finally:
                    os.close(fd)
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
                blockers.append(f"tracked RAW image {path} failed live cap verification: {error}")
    finally:
        os.close(root_fd)
    if len({row["role"] for row in measurements}) != len(measurements):
        blockers.append("tracked RAW image roles are duplicated")
    return {"measurements": measurements, "blockers": blockers}


def _check_runtime_phase_sample(observation: Mapping[str, Any], *, volume_uuid: str,
                                initial_quota_used_bytes: int,
                                maximum_phase_growth_bytes: int,
                                concurrent_growth_bytes: int | None,
                                session_id: str | None = None,
                                tracked_raw_receipt_paths: Sequence[Path] = ()) -> dict[str, Any]:
    blockers = _session_container_blockers(observation, volume_uuid=volume_uuid)
    try:
        free = conservative_session_unreserved_free_bytes(observation, volume_uuid=volume_uuid)
        volume = _volume_row(observation, volume_uuid)
        current_used = volume.get("CapacityInUse")
        if not _integer(current_used):
            raise ValueError("VM volume CapacityInUse is unknown")
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        free = None
        current_used = None
        blockers.append(str(error))
    if (current_used is not None
            and current_used - initial_quota_used_bytes > maximum_phase_growth_bytes):
        blockers.append("VM phase user file-data exceeded its signed phase allocation cap")
    if (free is not None and current_used is not None):
        observed_growth = max(0, current_used - initial_quota_used_bytes)
        remaining_growth = max(0, maximum_phase_growth_bytes - observed_growth)
        contract = _active_capacity_contract(volume_uuid=volume_uuid)
        result = calculate_vm_phase_admission(
            mode=VM_MODE,
            container_uuid=str(observation.get("apfs_container", {}).get("APFSContainerUUID", "")),
            volume_uuid=volume_uuid,
            free_bytes=free,
            quota_bytes=contract["quota_bytes"],
            quota_used_bytes=current_used,
            growth_bytes=remaining_growth,
            uncertainty_bytes=0,
            concurrent_growth_bytes=concurrent_growth_bytes,
            expected_quota_bytes=contract["quota_bytes"],
        )
        blockers.extend(result["blockers"])
    else:
        result = {"admitted": False, "blockers": []}
    tracked = {"measurements": [], "blockers": []}
    if tracked_raw_receipt_paths:
        if not isinstance(session_id, str):
            tracked["blockers"].append("tracked RAW checks lack a bound VM session ID")
        else:
            tracked = _sample_tracked_raw_images(
                observation=observation, session_id=session_id, volume_uuid=volume_uuid,
                receipt_paths=tracked_raw_receipt_paths)
        blockers.extend(tracked["blockers"])
    return {
        "captured_at_unix": observation.get("captured_at_unix"),
        "shared_container_free_bytes": free,
        "vm_volume_used_bytes": current_used,
        "vm_observed_phase_growth_bytes": (
            max(0, current_used - initial_quota_used_bytes) if current_used is not None else None),
        "vm_phase_growth_cap_bytes": maximum_phase_growth_bytes,
        "concurrent_growth_bound_bytes": concurrent_growth_bytes,
        "live_capacity_and_quota_check": result,
        "tracked_raw_images": tracked["measurements"],
        "blockers": sorted(set(blockers)),
        "admitted": not blockers and bool(result.get("admitted")),
    }


def monitor_vm_phase(*, session_id: str, volume_uuid: str,
                     phase_id: str, initial_quota_used_bytes: int,
                     maximum_phase_growth_bytes: int,
                     operation_done: Callable[[], bool],
                     cancel_operation: Callable[[], None],
                     wait_operation_quiescent: Callable[[float], bool],
                     sample_observation: Callable[[], Mapping[str, Any]] = capture_host_observation,
                     stop_vm: Callable[[], None] | None = None,
                     wait_vm_stopped_and_handles_released: Callable[[float], bool] | None = None,
                     stop_safety_profile_path: Path | None = None,
                     p2_admission_path: Path = VM_P2_ADMISSION_PATH,
                     tracked_raw_receipt_paths: Sequence[Path] = (),
                     evidence_receipt_path: Path | None = None,
                     p5_admission_path: Path = VM_P5_ADMISSION_PATH,
                     poll_interval_seconds: float = VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS,
                     concurrent_growth_bytes: int | None = None) -> dict[str, Any]:
    """Poll a write/install operation and stop it at the preemptive shared-free line.

    The caller supplies the Virtualization.framework cancellation/stop callbacks.
    A sample or callback failure is a blocking unknown state and leaves the signed
    session marker in place. This function does not treat a successful cancel or
    stop request as proof that writers have exited.
    """
    import time as _time

    if (not isinstance(poll_interval_seconds, (int, float))
            or isinstance(poll_interval_seconds, bool)
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
            or poll_interval_seconds > VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
        raise ValueError("VM phase monitor interval must be positive and at most 250 ms")
    if (not _integer(initial_quota_used_bytes)
            or not _integer(maximum_phase_growth_bytes)
            or (concurrent_growth_bytes is not None and not _integer(concurrent_growth_bytes))):
        raise ValueError("VM phase monitor budgets must be known; telemetry may be unavailable")
    if stop_vm is None or wait_vm_stopped_and_handles_released is None:
        raise RuntimeError("every VM write phase requires explicit VM-stop and writable-handle release proof callbacks")
    selected_profile_path = stop_safety_profile_path or VM_STOP_SAFETY_PROFILE_PATH
    profile_absent = _path_entry_is_absent(selected_profile_path)
    safety_profile: dict[str, Any] | None = None
    if not profile_absent:
        safety_profile, safety_blockers = _verify_stop_safety_profile(selected_profile_path)
        if safety_blockers or safety_profile is None:
            raise RuntimeError("VM writes cannot start with a present invalid stop-safety profile: "
                               + "; ".join(safety_blockers))
        if safety_profile.get("volume_uuid") != volume_uuid:
            raise RuntimeError("stop-safety evidence was measured against a different VM APFS volume")
    current_g0c = verify_fresh_g0c_review()
    if safety_profile is not None and safety_profile.get("g0c_identity") != current_g0c:
        raise RuntimeError("stop-safety profile does not bind the current G0C review")
    active = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("session_id") != session_id or active.get("volume_uuid") != volume_uuid
            or active.get("state") != "ACTIVE"):
        raise RuntimeError("VM writer monitor requires the same active session and volume marker")
    if phase_id == "P5_RUNTIME" and not tracked_raw_receipt_paths:
        raise RuntimeError("P5 runtime writes require signed per-image RAW allocation receipts")
    if phase_id not in {"P2", "P5_RUNTIME"}:
        raise RuntimeError("VM write monitor phase identifier is not an authorized live-writer phase")
    profile_mode: str | None = None
    effective_evidence_receipt_path = evidence_receipt_path
    p2_admission_sha256: str | None = None
    if phase_id == "P2":
        p2_admission = _read_signed_record(p2_admission_path, "vm_admission_hmac_sha256")
        p2_admission_sha256 = _file_sha256(p2_admission_path)
        admission_age = time.time() - float(p2_admission.get("created_at_unix", 0))
        if (p2_admission.get("decision") != "PASS"
                or p2_admission.get("authorized_phase") != "P2"
                or p2_admission.get("session_id") != session_id
                or p2_admission.get("vm_volume_uuid") != volume_uuid
                or p2_admission.get("p2_growth_bytes") != maximum_phase_growth_bytes
                or not _integer(initial_quota_used_bytes)
                or p2_admission.get("vm_volume_usage_bytes") != initial_quota_used_bytes
                or admission_age < 0 or admission_age > VM_MAX_OBSERVATION_AGE_SECONDS
                or p2_admission.get("controller_identity") != _controller_identity()):
            raise RuntimeError("P2 monitor requires a fresh same-session signed installer admission")
        if p2_admission.get("g0c_identity") != verify_fresh_g0c_review():
            raise RuntimeError("P2 monitor admission does not bind the current G0C review")
        active = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (active.get("session_id") != session_id or active.get("volume_uuid") != volume_uuid
                or active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_AUTHORIZED"):
            raise RuntimeError("P2 monitor requires its active P2_AUTHORIZED session marker")
        profile_mode = p2_admission.get("stop_safety_profile_mode")
        if profile_mode == VM_STOP_SAFETY_PROFILE_MODE_MEASURED:
            if (safety_profile is None
                    or p2_admission.get("stop_safety_profile_sha256")
                    != _file_sha256(selected_profile_path)
                    or p2_admission.get("stop_safety_profile") != safety_profile):
                raise RuntimeError("P2 monitor stop-safety profile differs from its signed admission")
        elif profile_mode == VM_STOP_SAFETY_PROFILE_MODE_LIVE:
            if safety_profile is not None or p2_admission.get("stop_safety_profile_sha256") is not None:
                raise RuntimeError("P2 live-capacity admission unexpectedly binds a stop-safety profile")
        else:
            raise RuntimeError("P2 admission has an unknown stop-safety profile mode")
    if phase_id == "P5_RUNTIME":
        if safety_profile is None:
            raise RuntimeError("P5 runtime writes require a validated measured stop-safety profile")
        if (active.get("phase_id") != "P5_AUTHORIZED"
                and not str(active.get("phase_id", "")).startswith("P5_")):
            raise RuntimeError("P5 monitor requires the active same-session P5-authorized marker")
        p5_admission = _read_signed_record(p5_admission_path, "vm_admission_hmac_sha256")
        admission_age = time.time() - float(p5_admission.get("created_at_unix", 0))
        if (p5_admission.get("decision") != "PASS"
                or p5_admission.get("authorized_phase") != "P5"
                or p5_admission.get("session_id") != session_id
                or p5_admission.get("vm_volume_uuid") != volume_uuid
                or p5_admission.get("controller_identity") != _controller_identity()
                or p5_admission.get("g0c_identity") != current_g0c
                or admission_age < 0 or admission_age > VM_MAX_OBSERVATION_AGE_SECONDS):
            raise RuntimeError("P5 runtime monitor requires a fresh same-session per-image-bound admission")
        tracked_roles = set()
        for receipt_path in tracked_raw_receipt_paths:
            item = _read_signed_record(receipt_path, "vm_component_hmac_sha256")
            tracked_roles.add(item.get("role"))
        required_roles = {"guest_system_raw", "read_only_input_tool_raw",
                          "workspace_raw", "export_capsule_raw"}
        if not required_roles.issubset(tracked_roles):
            raise RuntimeError("P5 runtime monitor does not bind all authorized RAW image roles")
    maximum_gap_seconds = (
        float(safety_profile["maximum_monitor_gap_microseconds"]) / 1_000_000
        if safety_profile is not None else VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS)
    monitor_host_build = (
        safety_profile["host_build_version"] if safety_profile is not None else _host_build_version())
    samples: list[dict[str, Any]] = []
    last_sample_at: float | None = None
    last_quota_used = initial_quota_used_bytes
    last_shared_free: int | None = None
    failed_sample: dict[str, Any] | None = None
    while True:
        try:
            observation = sample_observation()
            sample = _check_runtime_phase_sample(
                observation, volume_uuid=volume_uuid,
                initial_quota_used_bytes=initial_quota_used_bytes,
                maximum_phase_growth_bytes=maximum_phase_growth_bytes,
                concurrent_growth_bytes=concurrent_growth_bytes,
                session_id=session_id,
                tracked_raw_receipt_paths=tracked_raw_receipt_paths)
            now = time.time()
            sample["monitor_sampled_at_unix"] = now
            sample["monitor_gap_seconds"] = now - last_sample_at if last_sample_at is not None else 0.0
            previous_quota_used = last_quota_used
            if (maximum_gap_seconds is not None and last_sample_at is not None
                    and sample["monitor_gap_seconds"] > maximum_gap_seconds):
                sample["blockers"].append("actual VM monitor gap exceeded its authorized 250 ms/profile bound")
                sample["admitted"] = False
            if sample.get("vm_volume_used_bytes") is not None:
                last_quota_used = sample["vm_volume_used_bytes"]
            if last_shared_free is not None and sample.get("shared_container_free_bytes") is not None:
                total_growth = max(0, last_shared_free - sample["shared_container_free_bytes"])
                vm_growth = max(0, sample.get("vm_volume_used_bytes", previous_quota_used)
                                - previous_quota_used)
                observed_concurrent = max(0, total_growth - vm_growth)
                sample["observed_concurrent_growth_bytes"] = observed_concurrent
                sample["concurrent_growth_authority"] = "DIAGNOSTIC_ONLY"
            last_shared_free = sample.get("shared_container_free_bytes")
            samples.append(sample)
            last_sample_at = now
            if sample["blockers"]:
                failed_sample = sample
                break
        except BaseException as error:
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "blockers": [f"VM phase monitor measurement failed: {error}"],
                "admitted": False,
            }
            samples.append(failed_sample)
            break
        try:
            if operation_done():
                break
        except BaseException as error:
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "blockers": [f"VM phase operation state is unknown: {error}"],
                "admitted": False,
            }
            samples.append(failed_sample)
            break
        _time.sleep(float(poll_interval_seconds))
    if failed_sample is None:
        # One final read closes the race between operation completion and the
        # preceding poll, and is also the phase transition measurement.
        try:
            observation = sample_observation()
            final_sample = _check_runtime_phase_sample(
                observation, volume_uuid=volume_uuid,
                initial_quota_used_bytes=initial_quota_used_bytes,
                maximum_phase_growth_bytes=maximum_phase_growth_bytes,
                concurrent_growth_bytes=concurrent_growth_bytes,
                session_id=session_id,
                tracked_raw_receipt_paths=tracked_raw_receipt_paths)
            final_now = time.time()
            final_sample["monitor_sampled_at_unix"] = final_now
            final_sample["monitor_gap_seconds"] = (
                final_now - last_sample_at if last_sample_at is not None else 0.0)
            if (maximum_gap_seconds is not None and last_sample_at is not None
                    and final_sample["monitor_gap_seconds"] > maximum_gap_seconds):
                final_sample["blockers"].append("final VM monitor gap exceeded its authorized 250 ms/profile bound")
                final_sample["admitted"] = False
            samples.append(final_sample)
            if final_sample["blockers"]:
                failed_sample = final_sample
        except BaseException as error:
            failed_sample = {
                "monitor_sampled_at_unix": time.time(),
                "blockers": [f"VM phase final measurement failed: {error}"],
                "admitted": False,
            }
            samples.append(failed_sample)
    stop_measurement: dict[str, Any] = {}
    if failed_sample is None:
        stop_started = time.monotonic()
        try:
            phase_quiescent = bool(wait_operation_quiescent(VM_PHASE_STOP_TIMEOUT_SECONDS))
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
            vm_stopped = bool(wait_vm_stopped_and_handles_released(VM_PHASE_STOP_TIMEOUT_SECONDS))
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
                "blockers": ["VM phase completion did not prove writer quiescence, stopped state, and handle release"],
                "admitted": False,
            }
        else:
            try:
                stopped_observation = sample_observation()
                stopped_sample = _check_runtime_phase_sample(
                    stopped_observation, volume_uuid=volume_uuid,
                    initial_quota_used_bytes=initial_quota_used_bytes,
                    maximum_phase_growth_bytes=maximum_phase_growth_bytes,
                    concurrent_growth_bytes=concurrent_growth_bytes,
                    session_id=session_id,
                    tracked_raw_receipt_paths=tracked_raw_receipt_paths)
                stopped_sample["monitor_sampled_at_unix"] = time.time()
                stopped_sample["monitor_gap_seconds"] = (
                    stopped_sample["monitor_sampled_at_unix"] - last_sample_at
                    if last_sample_at is not None else 0.0)
                if stopped_sample["monitor_gap_seconds"] > maximum_gap_seconds:
                    stopped_sample["blockers"].append(
                        "post-stop VM monitor gap exceeded its authorized 250 ms/profile bound")
                    stopped_sample["admitted"] = False
                previous = samples[-1] if samples else {}
                previous_free = previous.get("shared_container_free_bytes")
                previous_used = previous.get("vm_volume_used_bytes")
                if (_integer(previous_free) and _integer(previous_used)
                        and _integer(stopped_sample.get("shared_container_free_bytes"))
                        and _integer(stopped_sample.get("vm_volume_used_bytes"))):
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
                    "blockers": [f"post-stop shared-container measurement failed: {error}"],
                    "admitted": False,
                }
        if failed_sample is not None:
            stop_measurement["phase_quiescent"] = phase_quiescent
            stop_measurement["vm_stopped_and_handles_released"] = vm_stopped
            stop_measurement["stop_latency_microseconds"] = stop_latency_us
    if failed_sample is None:
        result = {
            "schema_version": 1,
            "mode": VM_MODE,
            "session_id": session_id,
            "phase_id": phase_id,
            "status": "PASS_PHASE_COMPLETE",
            "host_build_version": monitor_host_build,
            "volume_uuid": volume_uuid,
            "safety_volume_uuid": active.get("safety_volume_uuid"),
            "safety_reserve_bytes": active.get("safety_reserve_bytes"),
            "controller_identity": _controller_identity(),
            "g0c_identity": current_g0c,
            "phase_quiescent": True,
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
            "stop_measurement": stop_measurement,
            "stop_safety_profile_mode": profile_mode,
            "p2_admission_sha256": p2_admission_sha256,
            "samples": samples,
            "sample_count": len(samples),
            "maximum_observed_monitor_gap_seconds": max(
                (float(row.get("monitor_gap_seconds", 0.0)) for row in samples), default=0.0),
        }
        update_active_session_marker(session_id, phase_id=f"{phase_id}_COMPLETE",
                                     committed_growth_bytes=0)
        signed = _seal(result, "vm_phase_monitor_hmac_sha256")
        if effective_evidence_receipt_path is not None:
            signed = _write_signed_json(
                effective_evidence_receipt_path, result, "vm_phase_monitor_hmac_sha256")
        return signed
    callbacks: list[str] = []
    stop_started = time.monotonic()
    try:
        cancel_operation()
        callbacks.append("cancel_requested")
    except BaseException as error:
        callbacks.append(f"cancel_failed:{error}")
    if stop_vm is not None:
        try:
            stop_vm()
            callbacks.append("vm_stop_requested")
        except BaseException as error:
            callbacks.append(f"vm_stop_failed:{error}")
    try:
        phase_quiescent = bool(wait_operation_quiescent(VM_PHASE_STOP_TIMEOUT_SECONDS))
    except BaseException as error:
        phase_quiescent = False
        callbacks.append(f"phase_quiescence_check_failed:{error}")
    vm_stopped = False
    try:
        vm_stopped = bool(wait_vm_stopped_and_handles_released(VM_PHASE_STOP_TIMEOUT_SECONDS))
    except BaseException as error:
        callbacks.append(f"vm_stop_proof_failed:{error}")
    stop_elapsed = time.monotonic() - stop_started
    if (safety_profile is not None
            and stop_elapsed * 1_000_000 > safety_profile["maximum_stop_latency_microseconds"]):
        vm_stopped = False
        callbacks.append("observed_stop_elapsed_exceeded_validated_profile")
    stopped_proven = phase_quiescent and vm_stopped
    marker_state = "FAILED" if stopped_proven else "UNKNOWN"
    try:
        update_active_session_marker(session_id, state=marker_state, phase_id=phase_id)
    except BaseException as error:
        callbacks.append(f"marker_update_failed:{error}")
        stopped_proven = False
        marker_state = "UNKNOWN"
    record = {
        "schema_version": 1,
        "mode": VM_MODE,
        "session_id": session_id,
        "phase_id": phase_id,
        "status": "FAILED_STOPPED" if stopped_proven else "UNKNOWN_BLOCKING",
        "marker_state": marker_state,
        "failure": failed_sample,
        "samples": samples,
        "sample_count": len(samples),
        "callbacks": callbacks,
        "phase_quiescent": phase_quiescent,
        "vm_stopped_and_handles_released": vm_stopped,
        "maximum_observed_monitor_gap_seconds": max(
            (float(row.get("monitor_gap_seconds", 0.0)) for row in samples), default=0.0),
        "measured_stop_elapsed_seconds": stop_elapsed,
        "validated_maximum_stop_latency_microseconds": safety_profile.get(
            "maximum_stop_latency_microseconds") if safety_profile else None,
    }
    record.update({
        "host_build_version": monitor_host_build,
        "volume_uuid": volume_uuid,
        "safety_volume_uuid": active.get("safety_volume_uuid"),
        "safety_reserve_bytes": active.get("safety_reserve_bytes"),
        "controller_identity": _controller_identity(),
        "g0c_identity": current_g0c,
        "phase_quiescent": phase_quiescent,
        "vm_state": "STOPPED" if vm_stopped else "UNKNOWN",
        "all_writable_handles_released": vm_stopped,
        "serial_and_attachment_handles_released": vm_stopped,
        "stop_measurement": stop_measurement,
        "p2_admission_sha256": p2_admission_sha256,
    })
    signed = _seal(record, "vm_phase_monitor_hmac_sha256")
    if effective_evidence_receipt_path is not None:
        signed = _write_signed_json(
            effective_evidence_receipt_path, record, "vm_phase_monitor_hmac_sha256")
    raise VMPhaseViolation(
        "; ".join(failed_sample.get("blockers", ["VM phase failed closed"])), signed)


def _volume_row(observation: Mapping[str, Any], volume_uuid: str) -> Mapping[str, Any]:
    container = observation.get("apfs_container")
    volumes = container.get("Volumes") if isinstance(container, Mapping) else None
    if not isinstance(volumes, list):
        raise ValueError("APFS volume enumeration is missing")
    matches = [row for row in volumes if isinstance(row, Mapping)
               and row.get("APFSVolumeUUID") == volume_uuid]
    if len(matches) != 1:
        raise ValueError("VM volume UUID is missing or ambiguous")
    return matches[0]


def _volume_mount_info(observation: Mapping[str, Any], volume_uuid: str) -> dict[str, Any]:
    row = _volume_row(observation, volume_uuid)
    device = row.get("DeviceIdentifier")
    if not isinstance(device, str) or not re.fullmatch(r"disk[0-9]+s[0-9]+", device):
        raise ValueError("VM APFS volume device identity is malformed")
    container = observation.get("apfs_container")
    if not isinstance(container, Mapping):
        raise ValueError("VM APFS container identity is unavailable")
    container_uuid = container.get("APFSContainerUUID")
    container_reference = container.get("ContainerReference")
    volume_name = row.get("Name")
    if (not isinstance(container_uuid, str)
            or not isinstance(container_reference, str)
            or not isinstance(volume_name, str)):
        raise ValueError("VM APFS volume/container identity is incomplete")
    expected_mount = f"/Volumes/{volume_name}"
    try:
        mount_state = _read_apfs_mount_state(
            volume_uuid=volume_uuid, device_identifier=device,
            container_uuid=container_uuid, container_reference=container_reference,
            volume_name=volume_name, expected_mount_location=expected_mount)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"VM volume UUID, container, or mount identity failed read-back: {error}") from error
    if mount_state["mount_state"] != "MOUNTED_VERIFIED":
        raise ValueError("VM volume is not mounted in the exact kernel-verified location")
    return {
        "device_identifier": device,
        "volume_uuid": volume_uuid.upper(),
        "container_uuid": container_uuid.upper(),
        "container_reference": container_reference,
        "filesystem_type": "apfs",
        "mount_point": mount_state["resolved_mount_point"],
        "mount_state": mount_state["mount_state"],
        "mounted": True,
        "mount_identity": mount_state,
    }


def _mount_identity_key(mount: Mapping[str, Any]) -> tuple[Any, ...]:
    identity = mount.get("mount_identity")
    if not isinstance(identity, Mapping):
        raise RuntimeError("canonical APFS mount identity is missing")
    return (
        mount.get("volume_uuid"), mount.get("device_identifier"),
        mount.get("container_uuid"), mount.get("container_reference"),
        identity.get("mount_table_device"), identity.get("mount_table_filesystem_id"),
        mount.get("mount_point"),
    )


def _revalidate_volume_mount(observation: Mapping[str, Any], volume_uuid: str,
                             expected_mount: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read diskutil and getmntinfo, then require an unchanged volume tuple."""
    current_mount = _volume_mount_info(observation, volume_uuid)
    if _mount_identity_key(current_mount) != _mount_identity_key(expected_mount):
        raise RuntimeError("APFS device/UUID/container/mount mapping changed during quota-probe setup")
    return current_mount


def _verify_open_mount_fd(file_descriptor: int, expected_mount: Mapping[str, Any]) -> None:
    """Bind an open root/payload descriptor to the same kernel mount identity."""
    actual = _fd_mount_record(file_descriptor)
    identity = expected_mount.get("mount_identity")
    if not isinstance(identity, Mapping):
        raise RuntimeError("expected APFS descriptor mount identity is missing")
    if (actual.get("source") != identity.get("mount_table_device")
            or actual.get("filesystem_id") != identity.get("mount_table_filesystem_id")
            or actual.get("mount_point") != expected_mount.get("mount_point")
            or actual.get("filesystem_type", "").lower() != "apfs"):
        raise RuntimeError("open probe descriptor no longer resolves to the admitted APFS volume instance")


def _open_verified_probe_payload(root_fd: int, volume_uuid: str,
                                 expected_mount: Mapping[str, Any]) -> int:
    """Revalidate identity at the payload-open boundary and open relative to its root FD."""
    latest = capture_host_observation()
    blockers = _container_blockers(
        latest, expected_vm_volume_uuid=volume_uuid,
        expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
        expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
    if blockers:
        raise RuntimeError("quota-probe identity changed immediately before payload open: "
                           + "; ".join(blockers))
    _revalidate_volume_mount(latest, volume_uuid, expected_mount)
    _verify_open_mount_fd(root_fd, expected_mount)
    file_descriptor = os.open(
        "quota-enforcement-probe.bin",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600, dir_fd=root_fd)
    try:
        _verify_open_mount_fd(file_descriptor, expected_mount)
    except BaseException:
        os.close(file_descriptor)
        raise
    return file_descriptor


def verify_quota_cli_support() -> dict[str, Any]:
    completed = subprocess.run(
        [_diskutil(), "apfs", "addVolume", "-help"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=10.0, text=True,
    )
    output = completed.stdout
    if not isinstance(output, str) or "-quota" not in output or "-reserve" not in output:
        raise RuntimeError("diskutil APFS addVolume does not expose both quota and reserve controls")
    if "maximum file data usage limit" not in output:
        raise RuntimeError("diskutil APFS quota semantics are unknown on this OS build")
    return {
        "command": [_diskutil(), "apfs", "addVolume", "-help"],
        "exit_code": completed.returncode,
        "help_text_sha256": _sha256(output.encode()),
        "quota_semantics": "maximum_user_file_data_capacity",
        "reserve_semantics": "minimum_guaranteed_user_file_data_capacity",
        "reviewed_quota_max_bytes": VM_REVIEWED_QUOTA_MAX_BYTES,
        "vm_volume_reserve_bytes": 0,
        "safety_volume_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
    }


def _seal(record: Mapping[str, Any], signature_field: str) -> dict[str, Any]:
    return policy._seal_storage_evidence(record, signature_field)


def verify_signed_record(record: Mapping[str, Any], signature_field: str) -> bool:
    return policy._verify_storage_evidence(record, signature_field)


def _write_signed_json(path: Path, record: Mapping[str, Any], signature_field: str) -> dict[str, Any]:
    sealed = _seal(record, signature_field)
    policy._write_json(path, sealed, strict_directory_sync=True)
    readback = json.loads(policy._read_file_nofollow(path, maximum_bytes=2 * 1024 * 1024))
    if (readback != sealed or not verify_signed_record(readback, signature_field)):
        raise RuntimeError(f"signed VM evidence did not pass durable read-back: {path}")
    return sealed


def create_precreation_admission(observation: Mapping[str, Any], restore_expected_bytes: int,
                                 restore_source_url: str,
                                 restore_metadata_sha256: str,
                                 *, authorization_scope: str = "RUN_QUOTA_PROBE_ONLY",
                                 restore_metadata_path: Path = VM_RESTORE_METADATA_PATH,
                                 quota_probe_receipt_path: Path = VM_QUOTA_PROBE_RECEIPT_PATH,
                                 receipt_path: Path = VM_PRECREATION_ADMISSION_PATH) -> dict[str, Any]:
    """Create a read-only PASS for a bounded quota probe or R4 volume creation."""
    blockers = _container_blockers(observation, expected_vm_volume_uuid=None)
    metadata_receipt_sha256: str | None = None
    live_metadata: Mapping[str, Any] | None = None
    try:
        live_metadata = _read_latest_restore_metadata(restore_metadata_path)
        metadata_receipt_sha256 = _file_sha256(restore_metadata_path)
        if (live_metadata.get("source_url") != restore_source_url
                or live_metadata.get("expected_length_bytes") != restore_expected_bytes
                or live_metadata.get("metadata_sha256") != restore_metadata_sha256):
            raise RuntimeError("requested restore fields differ from the latest signed VZ/Apple metadata")
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
        blockers.append(f"fresh signed VZMacOSRestoreImage/Apple metadata is unavailable: {error}")
    if authorization_scope not in {"RUN_QUOTA_PROBE_ONLY", "CREATE_VM_VOLUME_ONLY"}:
        blockers.append("pre-creation authorization scope is invalid")
    try:
        restore_metadata = validate_restore_metadata(
            restore_source_url, restore_expected_bytes, restore_metadata_sha256)
    except (TypeError, ValueError) as error:
        restore_metadata = {
            "source_url": restore_source_url,
            "expected_length_bytes": restore_expected_bytes,
            "metadata_sha256": restore_metadata_sha256,
            "status": "UNKNOWN",
        }
        blockers.append(f"upstream restore metadata is invalid: {error}")
    try:
        quota_support = verify_quota_cli_support()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        quota_support = {"status": "UNKNOWN"}
        blockers.append(f"APFS quota CLI semantics are unknown: {error}")
    try:
        concurrent_measurement = measure_concurrent_growth_bound()
        concurrent_growth_bound_bytes = concurrent_measurement.get("observed_max_interval_growth_bytes")
        if not _integer(concurrent_growth_bound_bytes):
            concurrent_growth_bound_bytes = None
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        concurrent_measurement = {"status": "UNKNOWN", "failure": str(error)}
        concurrent_growth_bound_bytes = None
    try:
        live_free = conservative_container_free_bytes(observation)
    except (TypeError, ValueError) as error:
        live_free = None
        blockers.append(f"dynamic quota shared-container free measurement is unknown: {error}")
    minimum_vm_quota = None
    if _integer(restore_expected_bytes, minimum=1):
        minimum_vm_quota = (restore_expected_bytes + VM_P1_METADATA_MAX_BYTES
                            + VM_P2_SYSTEM_FILE_DATA_MAX_BYTES
                            + VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES)
    dynamic_quota = calculate_dynamic_vm_quota(
        free_bytes=live_free,
        deterministic_uncertainty_bytes=VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES,
        admitted_coexistence_bytes=0,
        minimum_required_quota_bytes=minimum_vm_quota,
        maximum_quota_bytes=VM_REVIEWED_QUOTA_MAX_BYTES,
        reserve_already_reflected=False)
    blockers.extend(dynamic_quota["blockers"])
    plan_result = evaluate_initial_plan(
        observation, restore_expected_bytes,
        vm_quota_bytes=dynamic_quota.get("quota_bytes"),
        concurrent_growth_bound_bytes=concurrent_growth_bound_bytes)
    blockers.extend(plan_result["blockers"])
    quota_probe_receipt: Mapping[str, Any] | None = None
    quota_probe_sha256: str | None = None
    if authorization_scope == "RUN_QUOTA_PROBE_ONLY":
        free = None
        try:
            free = conservative_container_free_bytes(observation)
        except (TypeError, ValueError) as error:
            blockers.append(f"quota-probe free-space measurement is unknown: {error}")
        probe = calculate_vm_phase_admission(
            mode=VM_MODE, container_uuid=EXPECTED_CONTAINER_UUID,
            free_bytes=free, quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            quota_used_bytes=0, growth_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            uncertainty_bytes=0, concurrent_growth_bytes=concurrent_growth_bound_bytes,
            expected_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
        )
        blockers.extend(f"QUOTA_PROBE: {item}" for item in probe["blockers"])
        probe_evaluation: Mapping[str, Any] = probe
    else:
        try:
            quota_probe_receipt = _read_signed_record(
                quota_probe_receipt_path, "vm_quota_probe_hmac_sha256")
            quota_probe_sha256 = _file_sha256(quota_probe_receipt_path)
            if (quota_probe_receipt.get("status") != "PASS"
                    or quota_probe_receipt.get("probe_volume_deleted") is not True
                    or quota_probe_receipt.get("mode") != VM_MODE
                    or quota_probe_receipt.get("controller_identity") != _controller_identity()):
                raise RuntimeError("quota probe did not pass, delete its exact volume, or match current controller")
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            blockers.append(f"a current signed empirical APFS quota-probe PASS is required: {error}")
        probe_evaluation = {"status": "VERIFIED_FROM_SIGNED_RECEIPT" if quota_probe_receipt else "UNKNOWN"}
    project_usage = policy.measure_project_usage()
    project_bytes = policy.conservative_project_usage_bytes(project_usage)
    project_usage["conservative_allocated_inode_deduplicated_bytes"] = project_bytes
    try:
        g0c_identity = verify_fresh_g0c_review()
    except RuntimeError as error:
        g0c_identity = {"status": "FAIL"}
        blockers.append(str(error))
    record = {
        "schema_version": 1,
        "mode": VM_MODE,
        "decision": "PASS" if not blockers else "FAIL",
        "authorization": authorization_scope if not blockers else "NONE",
        "created_at_unix": time.time(),
        "controller_identity": _controller_identity(),
        "policy": {
            "reviewed_quota_max_bytes": VM_REVIEWED_QUOTA_MAX_BYTES,
            "safety_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "operational_free_floor_bytes": VM_OPERATIONAL_UNRESERVED_FREE_MIN,
            "operational_warning_free_bytes": VM_OPERATIONAL_WARNING_FREE,
        },
        "dynamic_vm_quota_pre_reserve": dynamic_quota,
        "live_observation": dict(observation),
        "live_shared_container_free_bytes": conservative_container_free_bytes(observation)
        if not _container_blockers(observation, expected_vm_volume_uuid=None) else None,
        "normal_project_usage": project_usage,
        "normal_project_scope_bytes": project_bytes,
        "normal_policy": {
            "project_ceiling_bytes": policy.PROJECT_BUDGET,
            "normal_projected_free_min_bytes": policy.FREE_MINIMUM,
            "preferred_free_bytes": policy.FREE_PREFERRED,
            "emergency_free_stop_bytes": policy.FREE_HARD_STOP,
            "status": "MEASURED_SEPARATELY_NO_VM_CREDIT",
        },
        "restore_expected_bytes": restore_expected_bytes,
        "restore_upstream_metadata": {
            **restore_metadata,
            "final_url": live_metadata.get("final_url") if live_metadata else None,
            "operating_system_version": live_metadata.get("operating_system_version") if live_metadata else None,
            "build_version": live_metadata.get("build_version") if live_metadata else None,
            "server_headers": live_metadata.get("headers") if live_metadata else None,
        },
        "restore_metadata_receipt_sha256": metadata_receipt_sha256,
        "phase_evaluation": plan_result,
        "concurrent_growth_measurement": concurrent_measurement,
        "concurrent_growth_bound_bytes": concurrent_growth_bound_bytes,
        "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
        "quota_probe_evaluation": dict(probe_evaluation),
        "quota_probe_receipt_sha256": quota_probe_sha256,
        "g0c_identity": g0c_identity,
        "quota_semantics_observation": quota_support,
        "blockers": blockers,
        "warnings": ["receipt is read-only and creates no APFS reservation",
                     "PASS authorizes only its named quota-probe or volume-creation operation",
                     "P1 requires a separate post-creation receipt",
                     "no Gate, execution admission, BuildLease, R6 test/build, or R6 acceptance authority"],
    }
    return _write_signed_json(receipt_path, record, "vm_admission_hmac_sha256")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_json_file_snapshot(path: Path, *, maximum_bytes: int = 8 * 1024 * 1024,
                             require_private: bool = False
                             ) -> tuple[dict[str, Any], bytes, str, tuple[int, int, int, int, int]]:
    """Read/hash a JSON evidence file and its stat identity from one descriptor."""
    selected = Path(os.path.abspath(path))
    parent_fd = policy._open_directory_nofollow(selected.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(selected.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_size > maximum_bytes
                or (require_private and stat.S_IMODE(before.st_mode) != 0o600)):
            raise RuntimeError(f"JSON evidence file has unsafe identity or size: {selected}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum_bytes:
                raise RuntimeError(f"JSON evidence file exceeds its read limit: {selected}")
        after = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                           getattr(before, "st_ctime_ns", 0))
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                          getattr(after, "st_ctime_ns", 0))
        raw = b"".join(chunks)
        if before_identity != after_identity or total != before.st_size:
            raise RuntimeError(f"JSON evidence file changed while being read: {selected}")
        try:
            record = json.loads(raw, parse_constant=_reject_json_constant,
                                object_pairs_hook=_unique_json_object)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"JSON evidence file is malformed: {selected}") from error
        if not isinstance(record, dict):
            raise RuntimeError(f"JSON evidence file is not an object: {selected}")
        return record, raw, _sha256(raw), after_identity
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _read_signed_record_snapshot(path: Path, signature_field: str,
                                 *, maximum_bytes: int = 2 * 1024 * 1024
                                 ) -> tuple[dict[str, Any], bytes, str, tuple[int, int, int, int, int]]:
    """Verify and hash one receipt from the same no-follow descriptor read."""
    selected = Path(os.path.abspath(path))
    parent_fd = policy._open_directory_nofollow(selected.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(selected.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0), dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size > maximum_bytes):
            raise RuntimeError(f"VM receipt is unsafe or exceeds its read limit: {selected}")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum_bytes:
                raise RuntimeError(f"VM receipt exceeds its read limit: {selected}")
        after = os.fstat(descriptor)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                           getattr(before, "st_ctime_ns", 0))
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                          getattr(after, "st_ctime_ns", 0))
        if before_identity != after_identity or total != before.st_size:
            raise RuntimeError(f"VM receipt changed while it was being read: {selected}")
        raw = b"".join(chunks)
        try:
            record = json.loads(raw, parse_constant=_reject_json_constant,
                                object_pairs_hook=_unique_json_object)
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"VM receipt JSON is malformed: {selected}") from error
        if (not isinstance(record, dict) or raw != policy._canonical_pretty_json_bytes(record)
                or not verify_signed_record(record, signature_field)):
            raise RuntimeError(f"VM receipt is noncanonical or unauthenticated: {selected}")
        return record, raw, _sha256(raw), after_identity
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _read_signed_record(path: Path, signature_field: str) -> dict[str, Any]:
    record, _raw, _digest, _identity = _read_signed_record_snapshot(path, signature_field)
    return record


def _write_signed_json_unique(path: Path, record: Mapping[str, Any], signature_field: str) -> dict[str, Any]:
    """Write one new signed receipt using O_EXCL; existing evidence is never replaced."""
    selected = Path(os.path.abspath(path))
    sealed = _seal(record, signature_field)
    raw = policy._canonical_pretty_json_bytes(sealed)
    parent_fd = policy._open_directory_nofollow(
        selected.parent, create=True, strict_directory_sync=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            selected.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=parent_fd)
        initial = os.fstat(descriptor)
        if (not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
                or initial.st_nlink != 1 or stat.S_IMODE(initial.st_mode) != 0o600):
            raise RuntimeError(f"new VM receipt has unsafe identity: {selected}")
        view = memoryview(raw)
        offset = 0
        while offset < len(view):
            count = os.write(descriptor, view[offset:])
            if count <= 0:
                raise OSError("short write while sealing new VM receipt")
            offset += count
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, min(1024 * 1024, len(raw) + 1))
            if not block:
                break
            chunks.append(block)
        readback = b"".join(chunks)
        final = os.fstat(descriptor)
        if (readback != raw or final.st_dev != initial.st_dev or final.st_ino != initial.st_ino
                or final.st_size != len(raw) or final.st_nlink != 1
                or final.st_uid != os.getuid() or stat.S_IMODE(final.st_mode) != 0o600):
            raise RuntimeError(f"new VM receipt failed same-descriptor read-back: {selected}")
        if not verify_signed_record(sealed, signature_field):
            raise RuntimeError(f"new VM receipt signature failed read-back: {selected}")
        os.fsync(parent_fd)
        return sealed
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)


def _deferred_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]


def _deferred_repo_reference(path: Path) -> dict[str, str]:
    selected = Path(os.path.abspath(path))
    try:
        relative = selected.relative_to(policy.REPO.resolve()).as_posix()
    except ValueError as error:
        raise RuntimeError("deferred VM evidence must be stored inside the repository") from error
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RuntimeError("deferred VM evidence path is not a safe repository-relative path")
    return {"path": relative, "sha256": _file_sha256(selected)}


def _deferred_repo_output_path(path: Path) -> str:
    """Validate a not-yet-created additive evidence path inside the repository."""
    selected = Path(os.path.abspath(path))
    try:
        relative = selected.relative_to(policy.REPO.resolve()).as_posix()
    except ValueError as error:
        raise RuntimeError("deferred VM output evidence must be stored inside the repository") from error
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise RuntimeError("deferred VM output evidence path is not a safe repository-relative path")
    return relative


def _deferred_read_repo_signed(path_text: str, digest: str,
                              signature_field: str) -> tuple[dict[str, Any], Path, str]:
    relative = Path(path_text)
    if (not path_text or relative.is_absolute() or ".." in relative.parts
            or relative.as_posix() != path_text):
        raise RuntimeError("deferred VM evidence reference path is malformed")
    path = policy.REPO / relative
    record, _raw, actual_digest, _identity = _read_signed_record_snapshot(path, signature_field)
    if actual_digest != digest:
        raise RuntimeError("deferred VM evidence reference digest changed")
    return record, path, actual_digest


def _deferred_read_branch_retirement(path: Path) -> tuple[dict[str, Any], Path, str]:
    """Read the signed branch receipt while retaining its canonical path and digest."""
    selected = Path(os.path.abspath(path))
    record, _raw, digest, _identity = _read_signed_record_snapshot(
        selected, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
    return record, selected, digest


def _deferred_storage_summary(observation: Mapping[str, Any]) -> dict[str, Any]:
    blockers = _session_container_blockers(
        observation, volume_uuid=VM_DEFERRED_VM_VOLUME_UUID)
    if blockers:
        raise RuntimeError("deferred VM storage identity is not safe: " + "; ".join(blockers))
    contract = _active_capacity_contract(volume_uuid=VM_DEFERRED_VM_VOLUME_UUID)
    vm_volume = _volume_row(observation, VM_DEFERRED_VM_VOLUME_UUID)
    safety_volume = _volume_row(observation, VM_DEFERRED_SAFETY_VOLUME_UUID)
    mount = _volume_mount_info(observation, VM_DEFERRED_VM_VOLUME_UUID)
    if (observation.get("apfs_container", {}).get("APFSContainerUUID")
            != VM_DEFERRED_CONTAINER_UUID
            or observation.get("physical_store_info", {}).get("DiskUUID")
            != VM_DEFERRED_PHYSICAL_STORE_UUID
            or vm_volume.get("CapacityQuota") != VM_DEFERRED_QUOTA_BYTES
            or safety_volume.get("CapacityInUse") != VM_DEFERRED_SAFETY_RESERVE_BYTES
            or safety_volume.get("CapacityReserve") != VM_DEFERRED_SAFETY_RESERVE_BYTES
            or contract.get("safety_reserve_bytes") != VM_DEFERRED_SAFETY_RESERVE_BYTES):
        raise RuntimeError("deferred VM quota, reserve, container, or physical-store identity changed")
    return {
        "captured_at_unix": observation.get("captured_at_unix"),
        "container_uuid": VM_DEFERRED_CONTAINER_UUID,
        "physical_store_uuid": VM_DEFERRED_PHYSICAL_STORE_UUID,
        "container_capacity_free_bytes": observation.get("crosschecks", {}).get(
            "apfs_container_free_bytes"),
        "data_capacity_free_bytes": observation.get("crosschecks", {}).get(
            "data_info_container_free_bytes"),
        "conservative_shared_free_bytes": conservative_container_free_bytes(observation),
        "vm_volume_uuid": VM_DEFERRED_VM_VOLUME_UUID,
        "vm_volume_capacity_in_use_bytes": vm_volume.get("CapacityInUse"),
        "vm_volume_quota_bytes": contract.get("quota_bytes"),
        "vm_volume_quota_headroom_bytes": (
            contract["quota_bytes"] - vm_volume["CapacityInUse"]
            if _integer(vm_volume.get("CapacityInUse")) else None),
        "vm_volume_mount_identity": mount.get("mount_identity"),
        "safety_volume_uuid": VM_DEFERRED_SAFETY_VOLUME_UUID,
        "safety_volume_capacity_in_use_bytes": safety_volume.get("CapacityInUse"),
        "safety_volume_reserve_bytes": VM_DEFERRED_SAFETY_RESERVE_BYTES,
        "safety_volume_mount_state": observation.get("safety_volume_info", {}).get(
            "mount_state"),
    }


def _deferred_process_scan() -> dict[str, Any]:
    command = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,command="]
    result = subprocess.run(command, check=False, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=5)
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError("deferred VM process scan was incomplete")
    own_pids = {os.getpid(), os.getppid()}
    pattern = re.compile(
        r"vm-p2-provisioner|vm_p2_provisioner|p2_base_orchestrator|"
        r"VZMacOSInstaller|virtualizationd|macOS.*installer|restore.*installer|"
        r"R6-VM-P[12].*(download|install|provision)|download-restore|renew-P1-and-download",
        re.IGNORECASE)
    rows: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(.*)", line)
        if not match:
            raise RuntimeError("deferred VM process scan returned an unparseable process row")
        pid, ppid, pgid = (int(match.group(index)) for index in (1, 2, 3))
        command_text = match.group(4)
        row = {"pid": pid, "ppid": ppid, "process_group_id": pgid,
               "command": command_text[:300]}
        rows.append(row)
        if pid not in own_pids and pattern.search(command_text):
            blockers.append(row)
    if not rows:
        raise RuntimeError("deferred VM process scan returned no process rows")
    return {
        "captured_at_unix": time.time(),
        "command": command,
        "scan_status": "PASS" if not blockers else "BLOCKED",
        "scanned_process_rows": len(rows),
        "blocking_processes": blockers,
        "process_table_sha256": _sha256(result.stdout.encode()),
        "process_table_bytes": len(result.stdout.encode()),
    }


def _deferred_restore_tree_evidence(
        *, expect_restore: bool,
        expected_quarantine_name: str | None = None) -> dict[str, Any]:
    blockers: list[str] = []
    restore_paths: list[str] = []
    quarantine_paths: list[str] = []
    partial_paths: list[str] = []
    large_paths: list[str] = []
    walk_errors: list[str] = []
    ignored = {".DocumentRevisions-V100", ".Spotlight-V100", ".fseventsd",
               ".Trashes", ".TemporaryItems", ".vol", ".apdisk"}

    def onerror(error: OSError) -> None:
        walk_errors.append(f"{error.filename}: {error}")

    for base, directories, files in os.walk(
            VM_DEFERRED_RESTORE_PATH.parent, topdown=True, followlinks=False,
            onerror=onerror):
        if Path(base) == VM_DEFERRED_RESTORE_PATH.parent:
            directories[:] = [name for name in directories if name not in ignored]
            if "templates" in directories:
                blockers.append("unexpected guest/base template directory remains")
        for name in list(directories):
            path = Path(base) / name
            if name.startswith(_DEFERRED_RESTORE_QUARANTINE_PREFIX):
                quarantine_paths.append(str(path))
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as error:
                blockers.append(f"VM-volume directory identity is unavailable: {path}: {error}")
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                blockers.append(f"unsafe VM-volume directory entry: {path}")
        for name in files:
            path = Path(base) / name
            if name.startswith(_DEFERRED_RESTORE_QUARANTINE_PREFIX):
                quarantine_paths.append(str(path))
            try:
                info = os.stat(path, follow_symlinks=False)
            except OSError as error:
                blockers.append(f"VM-volume file identity is unavailable: {path}: {error}")
                continue
            lower = name.lower()
            if lower.endswith(".ipsw"):
                restore_paths.append(str(path))
            if (("restore" in lower and any(mark in lower for mark in (
                    "partial", ".part", ".tmp", ".download")))
                    or lower.endswith((".partial", ".part", ".download"))):
                partial_paths.append(str(path))
            if (stat.S_ISREG(info.st_mode)
                    and info.st_size >= VM_DEFERRED_RESTORE_LOGICAL_BYTES - MIB
                    and path != VM_DEFERRED_RESTORE_PATH
                    and name != expected_quarantine_name):
                large_paths.append(str(path))
    if walk_errors:
        blockers.extend(f"VM-volume inventory is incomplete: {item}" for item in walk_errors)
    expected = [str(VM_DEFERRED_RESTORE_PATH)] if expect_restore else []
    expected_quarantine = ([str(VM_DEFERRED_RESTORE_PATH.parent / expected_quarantine_name)]
                           if expected_quarantine_name is not None else [])
    if sorted(restore_paths) != expected:
        blockers.append("controlled VM volume does not contain the exact expected IPSW set")
    if sorted(quarantine_paths) != expected_quarantine:
        blockers.append("controlled VM volume does not contain the exact expected quarantine set")
    if partial_paths:
        blockers.append("a partial restore payload remains in the controlled VM volume")
    if large_paths:
        blockers.append("a second restore-sized payload remains in the controlled VM volume")
    return {
        "restore_paths": sorted(restore_paths),
        "quarantine_paths": sorted(quarantine_paths),
        "partial_paths": sorted(partial_paths),
        "unexpected_restore_sized_paths": sorted(large_paths),
        "walk_errors": walk_errors,
        "blockers": blockers,
    }


_DEFERRED_RESTORE_OBJECT_FIELDS = ("device", "inode")
_DEFERRED_RESTORE_STABILITY_FIELDS = (
    "logical_bytes", "allocated_bytes", "mtime_ns", "owner_uid", "owner_gid", "mode",
    "link_count",
)
_DEFERRED_RESTORE_CTIME_FIELD = "ctime_ns"
_DEFERRED_RESTORE_QUARANTINE_PREFIX = ".restore.ipsw.deferred-retirement-"


def _deferred_restore_stable_object_identity(info: os.stat_result) -> tuple[int, int, int]:
    """Return device, inode, and regular-file type for the restore object."""
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("deferred restore descriptor is not a regular file")
    return int(info.st_dev), int(info.st_ino), int(stat.S_IFMT(info.st_mode))


def _deferred_restore_file_identity(info: os.stat_result) -> dict[str, int]:
    """Capture object identity and phase-specific stability evidence.

    ctime is retained in every snapshot. It is not part of stable object
    identity, because the accepted same-volume quarantine rename changes it.
    """
    device, inode, _file_type = _deferred_restore_stable_object_identity(info)
    return {
        "device": device,
        "inode": inode,
        "logical_bytes": int(info.st_size),
        "allocated_bytes": int(getattr(info, "st_blocks", 0) * 512),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(getattr(info, "st_ctime_ns", 0)),
        "owner_uid": int(info.st_uid),
        "owner_gid": int(info.st_gid),
        "mode": int(stat.S_IMODE(info.st_mode)),
        "link_count": int(info.st_nlink),
    }


def _assert_deferred_restore_same_object(
        expected: Mapping[str, Any], current: Mapping[str, Any], *,
        allow_ctime_change: bool = False) -> None:
    """Compare snapshots while allowing ctime only at an accepted rename edge."""
    if not isinstance(expected, Mapping) or not isinstance(current, Mapping):
        raise RuntimeError("deferred restore identity snapshot is malformed")
    required_fields = (
        *_DEFERRED_RESTORE_OBJECT_FIELDS,
        *_DEFERRED_RESTORE_STABILITY_FIELDS,
        _DEFERRED_RESTORE_CTIME_FIELD,
    )
    for snapshot in (expected, current):
        if any(not isinstance(snapshot.get(field), int)
               or isinstance(snapshot.get(field), bool) for field in required_fields):
            raise RuntimeError("deferred restore identity snapshot is malformed")
    if any(expected.get(field) != current.get(field)
           for field in _DEFERRED_RESTORE_OBJECT_FIELDS):
        raise RuntimeError("deferred restore object identity changed")
    if any(expected.get(field) != current.get(field)
           for field in _DEFERRED_RESTORE_STABILITY_FIELDS):
        raise RuntimeError("deferred restore stability metadata changed")
    if (not allow_ctime_change
            and expected.get(_DEFERRED_RESTORE_CTIME_FIELD)
            != current.get(_DEFERRED_RESTORE_CTIME_FIELD)):
        raise RuntimeError("deferred restore ctime changed within a stable phase")


def _deferred_restore_namespace_candidates(root_fd: int) -> list[str]:
    """List restore/quarantine names participating in this one-file transaction."""
    try:
        names = os.listdir(root_fd)
    except OSError as error:
        raise RuntimeError("deferred restore namespace could not be verified") from error
    return sorted(name for name in names
                  if name == "restore.ipsw"
                  or name.startswith(_DEFERRED_RESTORE_QUARANTINE_PREFIX))


def _assert_deferred_restore_namespace(root_fd: int, expected_names: list[str]) -> None:
    if _deferred_restore_namespace_candidates(root_fd) != sorted(expected_names):
        raise RuntimeError("deferred restore namespace is ambiguous")


def _deferred_restore_file_identity_at(root_fd: int, name: str) -> dict[str, int]:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("deferred restore no-follow path identity is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("deferred restore target is not a single-link regular file")
    return _deferred_restore_file_identity(info)


def _assert_deferred_restore_matches_p1(
        identity: Mapping[str, Any], p1_verification: Mapping[str, Any]) -> None:
    if not isinstance(identity, Mapping) or not isinstance(p1_verification, Mapping):
        raise RuntimeError("accepted P1 verification or current restore identity is malformed")
    if (p1_verification.get("status") != "PASS"
            or p1_verification.get("phase_id") != "P1"
            or p1_verification.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or p1_verification.get("local_sha256_state") != "VERIFIED"
            or p1_verification.get("local_sha256") != VM_DEFERRED_RESTORE_SHA256
            or p1_verification.get("actual_length_bytes") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or p1_verification.get("expected_length_bytes") != VM_DEFERRED_RESTORE_LOGICAL_BYTES):
        raise RuntimeError("accepted P1 verification receipt does not bind the exact restore payload")
    historical = p1_verification.get("file_identity")
    # st_blocks is a filesystem-dependent allocation measurement, not an object
    # identifier. Preserve the P1 value as historical evidence; the current value
    # is separately bound by authorization and must remain stable through deletion.
    fields = ("device", "inode", "logical_bytes", "mtime_ns", "ctime_ns")
    if (not isinstance(historical, Mapping)
            or any(historical.get(field) != identity.get(field) for field in fields)):
        raise RuntimeError("current restore file identity differs from accepted P1 provenance")
    for allocated in (historical.get("allocated_bytes"), identity.get("allocated_bytes")):
        if (not isinstance(allocated, int) or isinstance(allocated, bool) or allocated < 0):
            raise RuntimeError("P1/current restore allocation measurement is invalid")


def _deferred_restore_allocation_measurement(
        p1_verification: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    historical = p1_verification.get("file_identity")
    if not isinstance(historical, Mapping):
        raise RuntimeError("accepted P1 receipt omits its historical allocation measurement")
    _assert_deferred_restore_matches_p1(identity, p1_verification)
    p1_allocated = historical["allocated_bytes"]
    current_allocated = identity["allocated_bytes"]
    return {
        "p1_historical_allocated_bytes": p1_allocated,
        "current_allocated_bytes": current_allocated,
        "current_minus_p1_bytes": current_allocated - p1_allocated,
        "method": "st_blocks * 512; filesystem-dependent time-specific allocation measurement",
    }


def _assert_deferred_restore_authorization_supersedable(
        previous: Mapping[str, Any], *, branch_sha: str, marker: Mapping[str, Any],
        p1_admission_sha: str, p1_verification_sha: str,
        p1_verification: Mapping[str, Any], identity: Mapping[str, Any],
        root_identity: Mapping[str, Any]) -> None:
    """Accept only the exact earlier authorization with the premature retired claim."""
    expected = {
        "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION",
        "status": "AUTHORIZED",
        "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
        "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
        "vm_containment": "DEFERRED / OPTIONAL",
        "session_id": marker.get("session_id"),
        "active_marker_path": str(VM_ACTIVE_MARKER_PATH),
        "active_marker_sha256": VM_DEFERRED_ACTIVE_MARKER_SHA256,
        "committed_growth_bytes": marker.get("committed_growth_bytes"),
        "branch_retirement_receipt_sha256": branch_sha,
        "p1_admission_sha256": p1_admission_sha,
        "p1_verification_sha256": p1_verification_sha,
        "p1_restore_acquisition_historically_accepted": True,
        "p1_upstream_content_digest_validated": False,
        "restore_path": str(VM_DEFERRED_RESTORE_PATH),
        "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
        "restore_logical_bytes": VM_DEFERRED_RESTORE_LOGICAL_BYTES,
        "vm_volume_uuid": VM_DEFERRED_VM_VOLUME_UUID,
        "container_uuid": VM_DEFERRED_CONTAINER_UUID,
        "physical_store_uuid": VM_DEFERRED_PHYSICAL_STORE_UUID,
        "vm_volume_quota_bytes": VM_DEFERRED_QUOTA_BYTES,
        "safety_volume_uuid": VM_DEFERRED_SAFETY_VOLUME_UUID,
        "safety_reserve_bytes": VM_DEFERRED_SAFETY_RESERVE_BYTES,
        "local_restore_payload_retired": True,
        "p2_base_provisioning_accepted": False,
        "vm_guest_base_provisioned": False,
        "vm_guest_base_sealed": False,
        "p2_cleanup_success_claimed": False,
        "future_vm_work_requires_fresh_p1": True,
    }
    if (not isinstance(previous, Mapping)
            or any(previous.get(key) != value for key, value in expected.items())
            or previous.get("superseded_authorization_path") is not None
            or previous.get("superseded_authorization_sha256") is not None):
        raise RuntimeError("existing deferred restore authorization is not the exact replaceable record")
    previous_identity = previous.get("restore_file_identity")
    if not isinstance(previous_identity, Mapping):
        raise RuntimeError("existing deferred restore authorization omits its exact file identity")
    identity_fields = (
        "device", "inode", "logical_bytes", "mtime_ns", "ctime_ns",
        "owner_uid", "owner_gid", "mode", "link_count")
    if any(previous_identity.get(field) != identity.get(field) for field in identity_fields):
        raise RuntimeError("restore identity changed since the authorization being replaced")
    if (previous.get("vm_volume_root_identity") != dict(root_identity)
            or previous.get("restore_allocation_measurement")
                != _deferred_restore_allocation_measurement(p1_verification, previous_identity)):
        raise RuntimeError("existing deferred restore authorization has inconsistent root/allocation evidence")


def _assert_deferred_restore_authorization_executable(action: Mapping[str, Any]) -> None:
    if (not isinstance(action, Mapping)
            or action.get("record_type") != "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION"
            or action.get("status") != "AUTHORIZED"
            or action.get("action") != "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY"
            or action.get("deferred_decision") != "VM_CONTAINMENT_DEFERRED_OPTIONAL"
            or action.get("authorization_scope_state") != "LOCAL_RESTORE_PRESENT_AWAITING_DELETE"
            or action.get("local_restore_payload_retired") is not False
            or action.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or action.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256
            or action.get("restore_logical_bytes") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or action.get("p2_base_provisioning_accepted") is not False
            or action.get("vm_guest_base_provisioned") is not False
            or action.get("vm_guest_base_sealed") is not False
            or action.get("p2_cleanup_success_claimed") is not False):
        raise RuntimeError("deferred restore authorization is not the exact executable single-file action")


def _deferred_restore_retirement_receipts() -> list[Path]:
    """Return pending or terminal restore-retirement records, excluding authorities."""
    prefix = "R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-"
    return sorted(
        path for path in policy.REPO.glob("R6-VM-DEFERRED-RESTORE-RETIREMENT-*.json")
        if not path.name.startswith(prefix))


def _assert_deferred_restore_authorization_history(
        action: Mapping[str, Any], authorization_path: Path, *, branch_sha: str,
        marker: Mapping[str, Any], p1_admission_sha: str, p1_verification_sha: str,
        p1_verification: Mapping[str, Any]) -> None:
    current_path = Path(os.path.abspath(authorization_path))
    authorities = sorted(policy.REPO.glob(
        "R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-*.json"))
    retirements = _deferred_restore_retirement_receipts()
    if retirements or current_path not in authorities:
        raise RuntimeError("deferred restore authorization history is not the exact unused action")
    previous_path_text = action.get("superseded_authorization_path")
    previous_sha = action.get("superseded_authorization_sha256")
    if previous_path_text is None and previous_sha is None:
        if len(authorities) != 1:
            raise RuntimeError("deferred restore authorization is not the sole active action")
        return
    if (not isinstance(previous_path_text, str) or not isinstance(previous_sha, str)
            or len(authorities) != 2):
        raise RuntimeError("deferred restore authorization replacement chain is malformed")
    previous, previous_path, actual_previous_sha = _deferred_read_repo_signed(
        previous_path_text, previous_sha, VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
    if previous_path.resolve() == current_path.resolve() or set(authorities) != {
            previous_path.resolve(), current_path.resolve()}:
        raise RuntimeError("deferred restore authorization replacement chain is ambiguous")
    identity = action.get("restore_file_identity")
    root_identity = action.get("vm_volume_root_identity")
    if not isinstance(identity, Mapping) or not isinstance(root_identity, Mapping):
        raise RuntimeError("replacement authorization omits current restore/root identity")
    _assert_deferred_restore_authorization_supersedable(
        previous, branch_sha=branch_sha, marker=marker,
        p1_admission_sha=p1_admission_sha,
        p1_verification_sha=p1_verification_sha,
        p1_verification=p1_verification, identity=identity,
        root_identity=root_identity)
    if actual_previous_sha != previous_sha:
        raise RuntimeError("superseded authorization digest changed")


def _hash_deferred_restore_at(root_fd: int, name: str) -> tuple[str, dict[str, int]]:
    before_path = _deferred_restore_file_identity_at(root_fd, name)
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
    try:
        before_fd = os.fstat(descriptor)
        fd_identity = _deferred_restore_file_identity(before_fd)
        if fd_identity != before_path:
            raise RuntimeError("deferred restore path identity changed while opening")
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            block = os.read(descriptor, 8 * MIB)
            if not block:
                break
            digest.update(block)
            bytes_read += len(block)
        after_fd = os.fstat(descriptor)
        after_path = _deferred_restore_file_identity_at(root_fd, name)
        after_fd_identity = _deferred_restore_file_identity(after_fd)
        if (bytes_read != before_path["logical_bytes"]
                or after_fd_identity != before_path or after_path != before_path):
            raise RuntimeError("deferred restore changed during its full SHA-256 read")
        return digest.hexdigest(), after_fd_identity
    finally:
        os.close(descriptor)


def _deferred_read_exact_active_marker() -> tuple[dict[str, Any], str]:
    marker, _raw, marker_sha, _identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (marker_sha != VM_DEFERRED_ACTIVE_MARKER_SHA256
            or marker.get("session_id") != VM_DEFERRED_SESSION_ID
            or marker.get("state") != "ACTIVE"
            or marker.get("phase_id") != "P2_AUTHORIZED"
            or marker.get("container_uuid") != VM_DEFERRED_CONTAINER_UUID
            or marker.get("volume_uuid") != VM_DEFERRED_VM_VOLUME_UUID
            or marker.get("quota_bytes") != VM_DEFERRED_QUOTA_BYTES
            or marker.get("committed_growth_bytes") != 19_350_155_264
            or marker.get("safety_volume_uuid") != VM_DEFERRED_SAFETY_VOLUME_UUID
            or marker.get("safety_reserve_bytes") != VM_DEFERRED_SAFETY_RESERVE_BYTES):
        raise RuntimeError("active VM marker differs from the audited exact R4 capacity state")
    return marker, marker_sha


def _deferred_p1_evidence() -> tuple[dict[str, Any], str, dict[str, Any], str]:
    p1_admission, _raw, p1_admission_sha, _identity = _read_signed_record_snapshot(
        VM_DEFERRED_P1_ADMISSION_PATH, "vm_admission_hmac_sha256")
    p1_verification, _raw, p1_verification_sha, _identity = _read_signed_record_snapshot(
        VM_DEFERRED_P1_VERIFICATION_PATH, "restore_verification_hmac_sha256")
    if (p1_admission_sha != "a8c67f4a48382477ef3f3e4a25cff64c54c700017a0e12b12fcd6ed724c9ba4b"
            or p1_admission.get("decision") != "PASS"
            or p1_admission.get("p1_authorized") is not True
            or p1_admission.get("p2_authorized") is not False
            or p1_verification.get("p1_admission_sha256") != p1_admission_sha
            or p1_verification.get("payload_upstream_content_digest_validated") is not False):
        raise RuntimeError("immutable accepted P1 admission ancestry is unavailable")
    _assert_deferred_restore_matches_p1(
        p1_verification.get("file_identity", {}), p1_verification)
    return p1_admission, p1_admission_sha, p1_verification, p1_verification_sha


def _deferred_review_record_json(
        path: Path, *, decision: str, expected_fields: Mapping[str, Any],
        expected_record_type: str | None = None) -> tuple[dict[str, Any], str]:
    """Read and validate review structure and exact semantic bindings, without age policy."""
    review, _raw, digest, _identity = _read_json_file_snapshot(path, maximum_bytes=256 * 1024)
    reviewed_at = review.get("reviewed_at_unix")
    now = time.time()
    schema_version = review.get("schema_version")
    if (not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
            or review.get("status") != "PASS"
            or review.get("decision") != decision
            or not isinstance(review.get("reviewer_identity"), str)
            or not review["reviewer_identity"].strip()
            or review.get("model_identity") != "GPT-6 Luna Max"
            or (expected_record_type is not None
                and review.get("record_type") != expected_record_type)
            or not isinstance(reviewed_at, (int, float))
            or isinstance(reviewed_at, bool)
            or not math.isfinite(float(reviewed_at))
            or reviewed_at <= 0
            or reviewed_at > now + 1
            or review.get("findings") != []
            or any(review.get(field) != value for field, value in expected_fields.items())):
        raise RuntimeError("independent deferred VM review does not PASS the exact bound evidence")
    return review, digest


def _deferred_fresh_review_json(
        path: Path, *, decision: str,
        expected_fields: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Load a current review that may support an operation now."""
    review, digest = _deferred_review_record_json(
        path, decision=decision, expected_fields=expected_fields)
    if time.time() - float(review["reviewed_at_unix"]) > VM_DEFERRED_REVIEW_MAX_AGE_SECONDS:
        raise RuntimeError("independent deferred VM review does not PASS the exact bound evidence: stale")
    return review, digest


@dataclass(frozen=True)
class _DeferredHistoricalReviewBinding:
    """Exact transaction links verified by the signed R13 readers before historical loading."""

    action_id: str
    review_sha256: str
    validation_sha256: str
    pending_receipt_sha256: str
    subject_sha256: str
    authorization: Mapping[str, Any]
    authorization_sha256: str
    use_receipt: Mapping[str, Any]
    use_receipt_sha256: str
    failure: Mapping[str, Any]
    failure_sha256: str


def _deferred_bound_historical_review_json(
        path: Path, *, decision: str, expected_record_type: str,
        expected_fields: Mapping[str, Any],
        binding: _DeferredHistoricalReviewBinding) -> tuple[dict[str, Any], str]:
    """Load exact R13 historical review evidence through its already verified signed chain.

    All signed records in ``binding`` must have been read with
    ``_read_signed_record_snapshot`` and cross-checked by the transaction
    verifier before calling this loader. The review itself follows its original
    unsigned JSON contract; its exact digest is authenticated by those signed
    records. This function never grants current authority.
    """
    review, digest = _deferred_review_record_json(
        path, decision=decision, expected_fields=expected_fields,
        expected_record_type=expected_record_type)
    expected_digest = binding.review_sha256
    if (not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            or digest != expected_digest):
        raise RuntimeError("historical deferred VM review SHA-256 differs from its signed transaction binding")
    hash_bindings = (
        binding.validation_sha256,
        binding.pending_receipt_sha256,
        binding.subject_sha256,
        binding.authorization_sha256,
        binding.use_receipt_sha256,
        binding.failure_sha256,
    )
    if (not isinstance(binding.action_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", binding.action_id) is None
            or any(not isinstance(value, str)
                   or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in hash_bindings)
            or binding.failure_sha256 != VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256
            or binding.failure.get("recovery_review_sha256") != digest
            or binding.failure.get("recovery_validation_sha256")
                != binding.validation_sha256
            or binding.failure.get("pending_receipt_sha256")
                != binding.pending_receipt_sha256
            or binding.failure.get("recovery_authorization_sha256")
                != binding.authorization_sha256
            or binding.failure.get("recovery_use_receipt_sha256")
                != binding.use_receipt_sha256
            or binding.authorization.get("action_id") != binding.action_id
            or binding.authorization.get("record_type")
                != "R6_VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION"
            or binding.authorization.get("status") != "AUTHORIZED"
            or binding.authorization.get("action")
                != "DELETE_EXACT_PENDING_QUARANTINED_RESTORE"
            or binding.authorization.get("single_use") is not True
            or binding.authorization.get("recovery_review_sha256") != digest
            or binding.authorization.get("recovery_validation_sha256")
                != binding.validation_sha256
            or binding.authorization.get("pending_receipt_sha256")
                != binding.pending_receipt_sha256
            or binding.use_receipt.get("record_type")
                != "R6_VM_DEFERRED_RESTORE_RECOVERY_USE"
            or binding.use_receipt.get("status")
                != "CONSUMED_FOR_SINGLE_RECOVERY_ATTEMPT"
            or binding.use_receipt.get("authorization_sha256")
                != binding.authorization_sha256
            or binding.use_receipt.get("recovery_review_sha256") != digest
            or binding.use_receipt.get("recovery_validation_sha256")
                != binding.validation_sha256
            or binding.use_receipt.get("pending_receipt_sha256")
                != binding.pending_receipt_sha256
            or expected_fields.get("reviewed_validation_sha256")
                != binding.validation_sha256
            or expected_fields.get("reviewed_pending_receipt_sha256")
                != binding.pending_receipt_sha256
            or expected_fields.get("reviewed_full_sha256")
                != binding.subject_sha256
            or review.get("reviewed_full_sha256") != binding.subject_sha256
            or review.get("reviewed_exact_byte_count")
                != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or _deferred_repo_reference(path).get("path")
                != binding.failure.get("recovery_review_path")
            or not str(binding.failure.get("recovery_use_receipt_path", "")).endswith(
                f"{binding.action_id}.json")):
        raise RuntimeError("historical deferred VM review is not bound to the exact R13 action and subject")
    _assert_no_conflicting_r13_recovery_reviews(
        path, expected_digest=digest,
        validation_sha256=binding.validation_sha256,
        pending_receipt_sha256=binding.pending_receipt_sha256)
    return review, digest


def _assert_no_conflicting_r13_recovery_reviews(
        selected_path: Path, *, expected_digest: str,
        validation_sha256: str, pending_receipt_sha256: str) -> None:
    """Reject a second review for this R13 transaction if its bytes/disposition differ."""
    selected = selected_path.resolve()
    review_roots = (policy.REPO, policy.REPO / "experiments/storage")
    for root in review_roots:
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("R6-VM-DEFERRED-RESTORE-RECOVERY-REVIEW-*.json")):
            if candidate.resolve() == selected:
                continue
            alias, _raw, alias_digest, _identity = _read_json_file_snapshot(
                candidate, maximum_bytes=256 * 1024)
            same_transaction = (
                alias.get("reviewed_validation_sha256") == validation_sha256
                or alias.get("reviewed_pending_receipt_sha256") == pending_receipt_sha256)
            if same_transaction and alias_digest != expected_digest:
                raise RuntimeError(
                    "conflicting duplicate R13 recovery review exists for the same transaction")


def _require_deferred_review_postdates(
        review: Mapping[str, Any], *evidence_created_at_unix: Any) -> None:
    """Require the fresh independent review to follow every exact evidence object."""
    reviewed_at = review.get("reviewed_at_unix")
    if (not isinstance(reviewed_at, (int, float)) or isinstance(reviewed_at, bool)
            or not math.isfinite(float(reviewed_at))
            or any(not isinstance(created_at, (int, float))
                   or isinstance(created_at, bool)
                   or not math.isfinite(float(created_at))
                   or reviewed_at <= float(created_at)
                   for created_at in evidence_created_at_unix)):
        raise RuntimeError(
            "independent deferred VM review must postdate each exact authorization and retirement receipt")


def retire_deferred_vm_p2_capability(
        *, admission_path: Path = VM_DEFERRED_R4_ADMISSION_PATH,
        failed_attempt_path: Path = VM_DEFERRED_R4_FAILURE_PATH,
        receipt_path: Path | None = None) -> dict[str, Any]:
    """Supersede the exact unclaimed R4 capability and append signed parking evidence."""
    import p2_base_orchestrator as p2

    selected_admission = Path(admission_path)
    selected_failure = Path(failed_attempt_path)
    run_id = _deferred_run_id()
    final_path = receipt_path or policy.REPO / f"R6-VM-DEFERRED-BRANCH-RETIREMENT-{run_id}.json"
    pending_path = policy.REPO / f"R6-VM-DEFERRED-BRANCH-RETIREMENT-PENDING-{run_id}.json"
    with policy._acquire_storage_allocation_lock():
        p2_lock, p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            admission, _raw, admission_sha, _identity = _read_signed_record_snapshot(
                selected_admission, "vm_admission_hmac_sha256")
            failure, _raw, failure_sha, _identity = _read_signed_record_snapshot(
                selected_failure, "vm_p2_helper_launch_failure_hmac_sha256")
            marker, marker_sha = _deferred_read_exact_active_marker()
            p1_admission, p1_admission_sha, p1_verification, p1_verification_sha = _deferred_p1_evidence()
            if (selected_admission.resolve() != VM_DEFERRED_R4_ADMISSION_PATH.resolve()
                    or admission_sha != VM_DEFERRED_R4_ADMISSION_SHA256
                    or admission.get("decision") != "PASS"
                    or admission.get("authority_scope") != "PROVISION_MACOS_GUEST_BASE_ONLY"
                    or admission.get("authorized_phase") != "P2"
                    or admission.get("session_id") != VM_DEFERRED_SESSION_ID
                    or admission.get("active_marker_before_sha256") != marker_sha
                    or admission.get("helper_control_token_sha256")
                        != VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256
                    or admission.get("r6_execution_authorized") is not False
                    or admission.get("runtime_feasibility_authorized") is not False
                    or admission.get("serial_runtime_testing_authorized") is not False
                    or admission.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
                    or admission.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256
                    or admission.get("restore_logical_bytes") != VM_DEFERRED_RESTORE_LOGICAL_BYTES):
                raise RuntimeError("R4 admission is not the exact bounded P2-only artifact under review")
            if (failure.get("status") != "FAIL"
                    or failure.get("failure_stage") != "preclaim_monitor_initial_gate"
                    or failure.get("helper_process_created") is not False
                    or failure.get("guest_assets_created") is not False
                    or failure.get("admission_path") != str(selected_admission.resolve())):
                raise RuntimeError("R4 failed attempt does not establish a pre-claim/no-mutation stop")
            if (p1_admission_sha != admission.get("p1_admission_sha256")
                    or p1_verification.get("local_sha256") != admission.get("restore_sha256")
                    or p1_verification.get("session_id") != marker.get("session_id")):
                raise RuntimeError("R4 and accepted P1 restore provenance do not match")
            execution_start, _raw, execution_start_sha, _identity = _read_signed_record_snapshot(
                VM_DEFERRED_R4_EXECUTION_START_PATH, "vm_p2_execution_start_hmac_sha256")
            start_age = time.time() - float(execution_start.get("created_at_unix", 0))
            if (execution_start.get("status") != "ISSUED"
                    or execution_start.get("admission_sha256") != admission_sha
                    or start_age <= p2.P2_EXECUTION_START_MAX_AGE_SECONDS):
                raise RuntimeError("R4 execution-start lease is not the exact expired pre-claim lease")
            capability = p2._inspect_launch_capability(
                VM_DEFERRED_SESSION_ID, admission_sha,
                VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256)
            if capability.get("status") != "READY":
                raise RuntimeError("exact R4 capability is no longer READY and unclaimed")
            observation = capture_host_observation()
            storage_summary = _deferred_storage_summary(observation)
            tree_evidence = _deferred_restore_tree_evidence(expect_restore=True)
            if tree_evidence["blockers"]:
                raise RuntimeError("pre-parking VM-volume inventory is unsafe: "
                                   + "; ".join(tree_evidence["blockers"]))
            expected_base = VM_DEFERRED_RESTORE_PATH.parent / (
                "templates/macos-base-p2-r4-execution-freshness-20261018T214134Z")
            if expected_base.exists() or expected_base.is_symlink():
                raise RuntimeError("R4 guest/base target exists despite the pre-claim stop evidence")
            process_scan = _deferred_process_scan()
            if process_scan["blocking_processes"]:
                raise RuntimeError("VM/P2/installer process exists during R4 supersession")
            descriptor_scan = p2._descriptor_scan_evidence([VM_DEFERRED_RESTORE_PATH])
            if descriptor_scan.get("scan_clear") is not True:
                raise RuntimeError("a process has the restore open during R4 supersession")
            r4_source_match = admission.get("controller_identity") == _controller_identity()
            pending = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_BRANCH_RETIREMENT_PENDING",
                "status": "PENDING",
                "created_at_unix": time.time(),
                "session_id": VM_DEFERRED_SESSION_ID,
                "r4_admission_path": selected_admission.resolve().relative_to(policy.REPO.resolve()).as_posix(),
                "r4_admission_sha256": admission_sha,
                "capability_commitment_sha256": VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256,
                "capability_state_before": "READY",
                "active_marker_sha256": marker_sha,
                "decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            }
            pending_signed = _write_signed_json_unique(
                pending_path, pending, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
            superseded = p2._supersede_launch_capability(
                VM_DEFERRED_SESSION_ID, admission_sha,
                VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256)
            marker_after, marker_after_sha = _deferred_read_exact_active_marker()
            if (marker_after_sha != marker_sha or marker_after != marker
                    or superseded.get("status") != "SUPERSEDED"):
                raise RuntimeError("capability supersession changed the active marker or failed read-back")
            current_identity = _controller_identity()
            record = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_BRANCH_RETIREMENT",
                "status": "PASS",
                "created_at_unix": time.time(),
                "decision": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
                "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
                "session_id": VM_DEFERRED_SESSION_ID,
                "r4_admission_path": selected_admission.resolve().relative_to(policy.REPO.resolve()).as_posix(),
                "r4_admission_sha256": admission_sha,
                "r4_admission_decision": admission.get("decision"),
                "r4_execution_start_path": VM_DEFERRED_R4_EXECUTION_START_PATH.relative_to(
                    policy.REPO).as_posix(),
                "r4_execution_start_sha256": execution_start_sha,
                "r4_execution_start_age_seconds": start_age,
                "expired_execution_start_lease_status": "EXPIRED",
                "failed_attempt_path": selected_failure.resolve().relative_to(
                    policy.REPO.resolve()).as_posix(),
                "failed_attempt_sha256": failure_sha,
                "capability_commitment_sha256": VM_DEFERRED_CAPABILITY_COMMITMENT_SHA256,
                "capability_state_before": "READY",
                "capability_state_after": "SUPERSEDED",
                "capability_identity_before": capability.get("file_identity"),
                "capability_identity_after": superseded.get("file_identity"),
                "capability_superseded_path": superseded.get("superseded_path"),
                "capability_claimed": False,
                "active_marker_path": str(VM_ACTIVE_MARKER_PATH),
                "active_marker_sha256": marker_sha,
                "active_marker_state": marker.get("state"),
                "active_marker_phase": marker.get("phase_id"),
                "committed_growth_bytes": marker.get("committed_growth_bytes"),
                "active_marker_rewritten": False,
                "p1_admission_path": VM_DEFERRED_P1_ADMISSION_PATH.relative_to(policy.REPO).as_posix(),
                "p1_admission_sha256": p1_admission_sha,
                "p1_verification_path": VM_DEFERRED_P1_VERIFICATION_PATH.relative_to(policy.REPO).as_posix(),
                "p1_verification_sha256": p1_verification_sha,
                "restore_path": str(VM_DEFERRED_RESTORE_PATH),
                "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
                "restore_logical_bytes": VM_DEFERRED_RESTORE_LOGICAL_BYTES,
                "guest_assets_created": False,
                "helper_process_created": False,
                "guest_base_provisioned": False,
                "guest_base_sealed": False,
                "r4_guest_base_directory_absent": True,
                "current_controller_identity": current_identity,
                "r4_controller_identity_matches_current": r4_source_match,
                "p2_cleanup_lock": p2_lock_evidence,
                "vm_process_scan": process_scan,
                "restore_descriptor_scan": descriptor_scan,
                "vm_volume_inventory": tree_evidence,
                "storage_observation": storage_summary,
                "pending_receipt_path": pending_path.relative_to(policy.REPO).as_posix(),
                "pending_receipt_sha256": _sha256(policy._canonical_pretty_json_bytes(pending_signed)),
            }
            return _write_signed_json_unique(
                final_path, record, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
        finally:
            os.close(p2_lock)


def authorize_deferred_vm_restore_retirement(
        branch_retirement_path: Path,
        *, authorization_path: Path | None = None) -> dict[str, Any]:
    """Sign the distinct deferred-branch authority for the exact local P1 payload."""
    branch_record, branch_path, branch_sha = _deferred_read_branch_retirement(
        branch_retirement_path)
    if (branch_record.get("record_type") != "R6_VM_DEFERRED_BRANCH_RETIREMENT"
            or branch_record.get("status") != "PASS"
            or branch_record.get("decision")
                != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"
            or branch_record.get("capability_state_after") != "SUPERSEDED"
            or branch_record.get("capability_claimed") is not False):
        raise RuntimeError("restore retirement requires a signed pre-claim R4 supersession receipt")
    run_id = _deferred_run_id()
    final_path = authorization_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-{run_id}.json")
    with policy._acquire_storage_allocation_lock():
        import p2_base_orchestrator as p2
        p2_lock, p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            marker, marker_sha = _deferred_read_exact_active_marker()
            p1_admission, p1_admission_sha, p1_verification, p1_verification_sha = (
                _deferred_p1_evidence())
            if (branch_record.get("active_marker_sha256") != marker_sha
                    or branch_record.get("session_id") != marker.get("session_id")
                    or branch_record.get("p1_admission_sha256") != p1_admission_sha
                    or branch_record.get("p1_verification_sha256") != p1_verification_sha):
                raise RuntimeError("branch retirement receipt no longer matches exact active/P1 evidence")
            observation = capture_host_observation()
            storage_summary = _deferred_storage_summary(observation)
            tree_evidence = _deferred_restore_tree_evidence(expect_restore=True)
            if tree_evidence["blockers"]:
                raise RuntimeError("restore retirement inventory is blocked: "
                                   + "; ".join(tree_evidence["blockers"]))
            process_scan = _deferred_process_scan()
            if process_scan["blocking_processes"]:
                raise RuntimeError("VM/P2/installer process exists during restore authorization")
            root_fd = policy._open_directory_nofollow(VM_DEFERRED_RESTORE_PATH.parent)
            try:
                mount = _volume_mount_info(observation, VM_DEFERRED_VM_VOLUME_UUID)
                _verify_open_mount_fd(root_fd, mount)
                identity = _deferred_restore_file_identity_at(root_fd, "restore.ipsw")
                _assert_deferred_restore_matches_p1(identity, p1_verification)
                descriptor_scan = p2._descriptor_scan_evidence([VM_DEFERRED_RESTORE_PATH])
                if descriptor_scan.get("scan_clear") is not True:
                    raise RuntimeError("a process has the restore open during authorization")
                root_info = os.fstat(root_fd)
                root_identity = {"device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                                 "owner_uid": int(root_info.st_uid),
                                 "mode": int(stat.S_IMODE(root_info.st_mode))}
            finally:
                os.close(root_fd)
            prior_authorities = sorted(policy.REPO.glob(
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-*.json"))
            prior_retirements = _deferred_restore_retirement_receipts()
            if prior_retirements or len(prior_authorities) > 1:
                raise RuntimeError("a deferred restore terminal receipt or multiple authorities exist; action is one-use")
            superseded_authorization_ref: dict[str, str] | None = None
            if prior_authorities:
                previous, _raw, previous_sha, _previous_fs_identity = _read_signed_record_snapshot(
                    prior_authorities[0], VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
                _assert_deferred_restore_authorization_supersedable(
                    previous, branch_sha=branch_sha, marker=marker,
                    p1_admission_sha=p1_admission_sha,
                    p1_verification_sha=p1_verification_sha,
                    p1_verification=p1_verification, identity=identity,
                    root_identity=root_identity)
                superseded_authorization_ref = _deferred_repo_reference(prior_authorities[0])
                if superseded_authorization_ref["sha256"] != previous_sha:
                    raise RuntimeError("existing authorization changed while its replacement was prepared")
            branch_ref = _deferred_repo_reference(branch_path)
            p1_admission_ref = _deferred_repo_reference(VM_DEFERRED_P1_ADMISSION_PATH)
            p1_verification_ref = _deferred_repo_reference(VM_DEFERRED_P1_VERIFICATION_PATH)
            record = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION",
                "status": "AUTHORIZED",
                "created_at_unix": time.time(),
                "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
                "action_id": uuid.uuid4().hex,
                "authorization_scope_state": "LOCAL_RESTORE_PRESENT_AWAITING_DELETE",
                "superseded_authorization_path": (
                    superseded_authorization_ref["path"]
                    if superseded_authorization_ref else None),
                "superseded_authorization_sha256": (
                    superseded_authorization_ref["sha256"]
                    if superseded_authorization_ref else None),
                "authorization_supersession_reason": (
                    "Replaces the immutable earlier authorization that prematurely claimed the local restore was retired."
                    if superseded_authorization_ref else None),
                "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
                "vm_containment": "DEFERRED / OPTIONAL",
                "session_id": marker.get("session_id"),
                "active_marker_path": str(VM_ACTIVE_MARKER_PATH),
                "active_marker_sha256": marker_sha,
                "committed_growth_bytes": marker.get("committed_growth_bytes"),
                "branch_retirement_receipt_path": branch_ref["path"],
                "branch_retirement_receipt_sha256": branch_sha,
                "p1_admission_path": p1_admission_ref["path"],
                "p1_admission_sha256": p1_admission_sha,
                "p1_verification_path": p1_verification_ref["path"],
                "p1_verification_sha256": p1_verification_sha,
                "restore_allocation_measurement": _deferred_restore_allocation_measurement(
                    p1_verification, identity),
                "p1_restore_acquisition_historically_accepted": True,
                "p1_upstream_content_digest_validated": False,
                "restore_path": str(VM_DEFERRED_RESTORE_PATH),
                "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
                "restore_logical_bytes": VM_DEFERRED_RESTORE_LOGICAL_BYTES,
                "restore_file_identity": identity,
                "vm_volume_root_identity": root_identity,
                "vm_volume_uuid": VM_DEFERRED_VM_VOLUME_UUID,
                "container_uuid": VM_DEFERRED_CONTAINER_UUID,
                "physical_store_uuid": VM_DEFERRED_PHYSICAL_STORE_UUID,
                "vm_volume_quota_bytes": VM_DEFERRED_QUOTA_BYTES,
                "safety_volume_uuid": VM_DEFERRED_SAFETY_VOLUME_UUID,
                "safety_reserve_bytes": VM_DEFERRED_SAFETY_RESERVE_BYTES,
                "storage_observation": storage_summary,
                "restore_tree_inventory": tree_evidence,
                "restore_descriptor_scan": descriptor_scan,
                "vm_process_scan": process_scan,
                "p2_cleanup_lock": p2_lock_evidence,
                "local_restore_payload_retired": False,
                "p2_base_provisioning_accepted": False,
                "vm_guest_base_provisioned": False,
                "vm_guest_base_sealed": False,
                "p2_cleanup_success_claimed": False,
                "future_vm_work_requires_fresh_p1": True,
            }
            return _write_signed_json_unique(
                final_path, record, VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
        finally:
            os.close(p2_lock)


def perform_deferred_vm_restore_retirement(
        authorization_path: Path,
        predelete_review_path: Path,
        *, retirement_receipt_path: Path | None = None,
        pending_receipt_path: Path | None = None) -> dict[str, Any]:
    """Delete only the authorized restore inode after an independent PASS review."""
    import fcntl
    import p2_base_orchestrator as p2

    action, _raw, action_sha, _identity = _read_signed_record_snapshot(
        authorization_path, VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
    _assert_deferred_restore_authorization_executable(action)
    branch_relative = Path(action.get("branch_retirement_receipt_path", ""))
    branch_record, branch_path, branch_sha = _deferred_read_repo_signed(
        branch_relative.as_posix(), action.get("branch_retirement_receipt_sha256", ""),
        VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
    if (branch_record.get("status") != "PASS"
            or branch_record.get("decision")
                != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"):
        raise RuntimeError("restore authority lacks the exact R4 capability retirement receipt")
    review, review_sha = _deferred_fresh_review_json(
        predelete_review_path,
        decision="PASS — DEFERRED VM RESTORE PREDELETE REVIEW",
        expected_fields={
            "reviewed_authorization_path": Path(authorization_path).resolve().relative_to(
                policy.REPO.resolve()).as_posix(),
            "reviewed_authorization_sha256": action_sha,
            "reviewed_branch_retirement_receipt_sha256": branch_sha,
            "restore_path": str(VM_DEFERRED_RESTORE_PATH),
            "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
            "restore_file_identity": action.get("restore_file_identity"),
        })
    _require_deferred_review_postdates(
        review, action.get("created_at_unix"))
    run_id = str(action.get("action_id"))
    final_path = retirement_receipt_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RETIREMENT-{run_id}.json")
    pending_path = pending_receipt_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RETIREMENT-PENDING-{run_id}.json")
    if (final_path.exists() or final_path.is_symlink()
            or pending_path.exists() or pending_path.is_symlink()):
        raise FileExistsError("deferred restore action already has a pending or terminal result")

    with policy._acquire_storage_allocation_lock():
        p2_lock, p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            # Reload all authority after both controller locks are held.
            action_now, _raw, action_now_sha, _identity = _read_signed_record_snapshot(
                authorization_path, VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
            branch_now, _raw, branch_now_sha, _identity = _read_signed_record_snapshot(
                branch_path, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
            if (action_now_sha != action_sha or branch_now_sha != branch_sha
                    or action_now != action or branch_now != branch_record):
                raise RuntimeError("signed retirement authority changed before deletion")
            marker, marker_sha = _deferred_read_exact_active_marker()
            p1_admission, p1_admission_sha, p1_verification, p1_verification_sha = (
                _deferred_p1_evidence())
            if (marker_sha != action.get("active_marker_sha256")
                    or marker.get("session_id") != action.get("session_id")
                    or p1_admission_sha != action.get("p1_admission_sha256")
                    or p1_verification_sha != action.get("p1_verification_sha256")
                    or branch_sha != action.get("branch_retirement_receipt_sha256")):
                raise RuntimeError("active marker, P1 ancestry, or branch-retirement binding changed")
            _assert_deferred_restore_authorization_history(
                action, authorization_path, branch_sha=branch_sha, marker=marker,
                p1_admission_sha=p1_admission_sha,
                p1_verification_sha=p1_verification_sha,
                p1_verification=p1_verification)
            if action.get("restore_allocation_measurement") != (
                    _deferred_restore_allocation_measurement(
                        p1_verification, action.get("restore_file_identity", {}))):
                raise RuntimeError("authorized restore allocation measurement is inconsistent")
            before = capture_host_observation()
            before_storage = _deferred_storage_summary(before)
            tree_before = _deferred_restore_tree_evidence(expect_restore=True)
            if tree_before["blockers"]:
                raise RuntimeError("predelete VM-volume inventory is unsafe: "
                                   + "; ".join(tree_before["blockers"]))
            process_before = _deferred_process_scan()
            if process_before["blocking_processes"]:
                raise RuntimeError("VM/P2/installer/download process exists at deletion boundary")
            mount = _volume_mount_info(before, VM_DEFERRED_VM_VOLUME_UUID)
            root_fd = policy._open_directory_nofollow(VM_DEFERRED_RESTORE_PATH.parent)
            restore_fd: int | None = None
            pending_written = False
            renamed = False
            unlinked = False
            quarantine_name = f".restore.ipsw.deferred-retirement-{run_id}"
            quarantine_path = VM_DEFERRED_RESTORE_PATH.parent / quarantine_name
            before_used = _volume_row(before, VM_DEFERRED_VM_VOLUME_UUID).get("CapacityInUse")
            before_free = before_storage["conservative_shared_free_bytes"]
            hash_digest: str | None = None
            identity_before: dict[str, int] | None = None
            identity_after_rename: dict[str, int] | None = None
            post_rename_descriptor_scan: dict[str, Any] | None = None
            try:
                _verify_open_mount_fd(root_fd, mount)
                root_info = os.fstat(root_fd)
                root_identity = {"device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                                 "owner_uid": int(root_info.st_uid),
                                 "mode": int(stat.S_IMODE(root_info.st_mode))}
                if root_identity != action.get("vm_volume_root_identity"):
                    raise RuntimeError("VM-volume root identity differs from signed authorization")
                identity_before = _deferred_restore_file_identity_at(root_fd, "restore.ipsw")
                if identity_before != action.get("restore_file_identity"):
                    raise RuntimeError("restore path/type/device/inode/size/link identity changed")
                _assert_deferred_restore_namespace(root_fd, ["restore.ipsw"])
                _assert_deferred_restore_matches_p1(identity_before, p1_verification)
                restore_fd = os.open(
                    "restore.ipsw", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
                pinned = os.fstat(restore_fd)
                if _deferred_restore_file_identity(pinned) != identity_before:
                    raise RuntimeError("restore inode changed while opening at deletion boundary")
                hash_state = hashlib.sha256()
                bytes_hashed = 0
                while True:
                    block = os.read(restore_fd, 8 * MIB)
                    if not block:
                        break
                    hash_state.update(block)
                    bytes_hashed += len(block)
                hash_digest = hash_state.hexdigest()
                after_hash_fd = os.fstat(restore_fd)
                after_hash_path = _deferred_restore_file_identity_at(root_fd, "restore.ipsw")
                if (bytes_hashed != VM_DEFERRED_RESTORE_LOGICAL_BYTES
                        or _deferred_restore_file_identity(after_hash_fd) != identity_before
                        or after_hash_path != identity_before
                        or hash_digest != VM_DEFERRED_RESTORE_SHA256
                        or hash_digest != p1_verification.get("local_sha256")):
                    raise RuntimeError("fresh full restore hash or path identity failed at unlink boundary")

                process_at_hash = _deferred_process_scan()
                if process_at_hash["blocking_processes"]:
                    raise RuntimeError("VM/P2/installer/download process appeared during restore hash")
                descriptor_before = p2._descriptor_scan_evidence([VM_DEFERRED_RESTORE_PATH])
                own_matches = descriptor_before.get("exact_target_descriptor_matches", [])
                all_rows = [file_row for process in descriptor_before.get("open_file_descriptors", [])
                            for file_row in process.get("files", [])]
                if (descriptor_before.get("descriptor_identity_mismatches") != []
                        or len(all_rows) != 1 or len(own_matches) != 1
                        or own_matches[0].get("pid") != os.getpid()
                        or descriptor_before.get("open_process_ids") != [str(os.getpid())]):
                    raise RuntimeError("a process other than this authorized controller has the restore open")
                try:
                    fcntl.flock(restore_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError("restore inode is held by another cooperative reader") from error
                last_identity = _deferred_restore_file_identity_at(root_fd, "restore.ipsw")
                if last_identity != identity_before:
                    raise RuntimeError("restore path identity changed immediately before quarantine rename")
                _assert_deferred_restore_namespace(root_fd, ["restore.ipsw"])
                pending_record = {
                    "schema_version": 1,
                    "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_PENDING",
                    "status": "PENDING",
                    "created_at_unix": time.time(),
                    "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
                    "authorization_path": Path(authorization_path).resolve().relative_to(
                        policy.REPO.resolve()).as_posix(),
                    "authorization_sha256": action_sha,
                    "predelete_review_path": Path(predelete_review_path).resolve().relative_to(
                        policy.REPO.resolve()).as_posix(),
                    "predelete_review_sha256": review_sha,
                    "branch_retirement_receipt_path": branch_relative.as_posix(),
                    "branch_retirement_receipt_sha256": branch_sha,
                    "p1_verification_sha256": p1_verification_sha,
                    "restore_path": str(VM_DEFERRED_RESTORE_PATH),
                    "restore_sha256": hash_digest,
                    "restore_file_identity": identity_before,
                    "vm_volume_root_identity": root_identity,
                    "storage_before": before_storage,
                    "pending_recovery": "Any interruption requires fresh independent inspection; do not restore or unlink a different inode.",
                }
                pending = _write_signed_json_unique(
                    pending_path, pending_record, VM_DEFERRED_RESTORE_PENDING_SIGNATURE_FIELD)
                pending_sha = _sha256(policy._canonical_pretty_json_bytes(pending))
                pending_written = True
                p2._renameatx_exclusive(root_fd, "restore.ipsw", root_fd, quarantine_name)
                renamed = True
                os.fsync(root_fd)
                post_rename_fd = _deferred_restore_file_identity(os.fstat(restore_fd))
                _assert_deferred_restore_same_object(
                    identity_before, post_rename_fd, allow_ctime_change=True)
                identity_after_rename = post_rename_fd
                _assert_deferred_restore_namespace(root_fd, [quarantine_name])
                quarantined_identity = _deferred_restore_file_identity_at(root_fd, quarantine_name)
                _assert_deferred_restore_same_object(post_rename_fd, quarantined_identity)
                post_rename_descriptor_scan = p2._descriptor_scan_evidence([quarantine_path])
                post_matches = post_rename_descriptor_scan.get("exact_target_descriptor_matches", [])
                post_rows = [file_row for process in post_rename_descriptor_scan.get(
                    "open_file_descriptors", []) for file_row in process.get("files", [])]
                if (post_rename_descriptor_scan.get("descriptor_identity_mismatches") != []
                        or len(post_rows) != 1 or len(post_matches) != 1
                        or post_matches[0].get("pid") != os.getpid()
                        or post_rename_descriptor_scan.get("open_process_ids") != [str(os.getpid())]):
                    raise RuntimeError("unexpected process has the quarantined restore open; deletion stopped")
                process_after_move = _deferred_process_scan()
                if process_after_move["blocking_processes"]:
                    raise RuntimeError("VM/P2/installer/download process appeared before restore unlink")
                _assert_deferred_restore_namespace(root_fd, [quarantine_name])
                before_unlink_fd = _deferred_restore_file_identity(os.fstat(restore_fd))
                before_unlink_path = _deferred_restore_file_identity_at(root_fd, quarantine_name)
                _assert_deferred_restore_same_object(identity_after_rename, before_unlink_fd)
                _assert_deferred_restore_same_object(identity_after_rename, before_unlink_path)
                _assert_deferred_restore_same_object(before_unlink_fd, before_unlink_path)
                os.unlink(quarantine_name, dir_fd=root_fd)
                os.fsync(root_fd)
                unlinked = True
            except BaseException:
                if renamed and not unlinked and restore_fd is not None and identity_after_rename is not None:
                    try:
                        _assert_deferred_restore_namespace(root_fd, [quarantine_name])
                        rollback_fd_identity = _deferred_restore_file_identity(os.fstat(restore_fd))
                        quarantined = _deferred_restore_file_identity_at(root_fd, quarantine_name)
                        _assert_deferred_restore_same_object(
                            identity_after_rename, rollback_fd_identity)
                        _assert_deferred_restore_same_object(
                            identity_after_rename, quarantined)
                        _assert_deferred_restore_same_object(rollback_fd_identity, quarantined)
                        _assert_deferred_restore_namespace(root_fd, [quarantine_name])
                        p2._renameatx_exclusive(
                            root_fd, quarantine_name, root_fd, "restore.ipsw")
                        os.fsync(root_fd)
                        rollback_after_fd = _deferred_restore_file_identity(os.fstat(restore_fd))
                        rollback_after_path = _deferred_restore_file_identity_at(
                            root_fd, "restore.ipsw")
                        _assert_deferred_restore_same_object(
                            identity_after_rename, rollback_after_fd, allow_ctime_change=True)
                        _assert_deferred_restore_same_object(
                            rollback_after_fd, rollback_after_path)
                        _assert_deferred_restore_namespace(root_fd, ["restore.ipsw"])
                    except BaseException:
                        pass
                raise
            finally:
                if restore_fd is not None:
                    os.close(restore_fd)
                os.close(root_fd)

            if not unlinked or not pending_written:
                raise RuntimeError("restore retirement did not complete its exact signed pending transition")
            tree_after = _deferred_restore_tree_evidence(expect_restore=False)
            if tree_after["blockers"]:
                raise RuntimeError("restore file was removed but postdelete VM inventory is incomplete: "
                                   + "; ".join(tree_after["blockers"]))
            observation_after = capture_host_observation()
            after_storage = _deferred_storage_summary(observation_after)
            after_vm_used = _volume_row(observation_after, VM_DEFERRED_VM_VOLUME_UUID).get(
                "CapacityInUse")
            after_free = after_storage["conservative_shared_free_bytes"]
            if (after_storage.get("vm_volume_uuid") != VM_DEFERRED_VM_VOLUME_UUID
                    or after_storage.get("safety_volume_uuid") != VM_DEFERRED_SAFETY_VOLUME_UUID
                    or after_storage.get("safety_volume_reserve_bytes")
                        != VM_DEFERRED_SAFETY_RESERVE_BYTES):
                raise RuntimeError("postdelete APFS identity or safety reserve changed")
            quota_reduction = (max(0, int(before_used) - int(after_vm_used))
                               if _integer(before_used) and _integer(after_vm_used) else 0)
            shared_free_increase = max(0, int(after_free) - int(before_free))
            reclaimed = min(quota_reduction, shared_free_increase)
            marker_after, marker_after_sha = _deferred_read_exact_active_marker()
            if marker_after_sha != marker_sha or marker_after != marker:
                raise RuntimeError("active VM marker changed during restore retirement")
            final = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT",
                "status": "PASS",
                "created_at_unix": time.time(),
                "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
                "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
                "session_id": marker.get("session_id"),
                "authorization_path": Path(authorization_path).resolve().relative_to(
                    policy.REPO.resolve()).as_posix(),
                "authorization_sha256": action_sha,
                "superseded_authorization_path": action.get("superseded_authorization_path"),
                "superseded_authorization_sha256": action.get("superseded_authorization_sha256"),
                "predelete_review_path": Path(predelete_review_path).resolve().relative_to(
                    policy.REPO.resolve()).as_posix(),
                "predelete_review_sha256": review_sha,
                "branch_retirement_receipt_path": branch_relative.as_posix(),
                "branch_retirement_receipt_sha256": branch_sha,
                "p1_admission_path": VM_DEFERRED_P1_ADMISSION_PATH.relative_to(policy.REPO).as_posix(),
                "p1_admission_sha256": p1_admission_sha,
                "p1_verification_path": VM_DEFERRED_P1_VERIFICATION_PATH.relative_to(policy.REPO).as_posix(),
                "p1_verification_sha256": p1_verification_sha,
                "p1_provenance_preserved": True,
                "p1_upstream_content_digest_validated": False,
                "restore_path": str(VM_DEFERRED_RESTORE_PATH),
                "deleted_path": str(VM_DEFERRED_RESTORE_PATH),
                "deleted_file_sha256": hash_digest,
                "deleted_file_identity": identity_before,
                "restore_file_identity_before_quarantine_rename": identity_before,
                "restore_file_identity_after_quarantine_rename": identity_after_rename,
                "logical_bytes": VM_DEFERRED_RESTORE_LOGICAL_BYTES,
                "allocated_bytes_before_delete": identity_before["allocated_bytes"],
                "restore_allocation_measurement": action["restore_allocation_measurement"],
                "authorization_scope_state": "LOCAL_RESTORE_RETIRED",
                "local_restore_payload_retired": True,
                "restore_absent": not VM_DEFERRED_RESTORE_PATH.exists(),
                "quarantine_absent": not quarantine_path.exists(),
                "partial_restore_paths_after_delete": tree_after["partial_paths"],
                "other_restore_paths_after_delete": tree_after["restore_paths"],
                "vm_volume_uuid": VM_DEFERRED_VM_VOLUME_UUID,
                "container_uuid": VM_DEFERRED_CONTAINER_UUID,
                "physical_store_uuid": VM_DEFERRED_PHYSICAL_STORE_UUID,
                "vm_volume_quota_bytes": VM_DEFERRED_QUOTA_BYTES,
                "safety_volume_uuid": VM_DEFERRED_SAFETY_VOLUME_UUID,
                "safety_reserve_bytes": VM_DEFERRED_SAFETY_RESERVE_BYTES,
                "storage_before": before_storage,
                "storage_after": after_storage,
                "vm_volume_used_before_bytes": before_used,
                "vm_volume_used_after_bytes": after_vm_used,
                "shared_free_before_bytes": before_free,
                "shared_free_after_bytes": after_free,
                "measured_vm_quota_reduction_bytes": quota_reduction,
                "measured_shared_free_increase_bytes": shared_free_increase,
                "RESTORE_RETIREMENT_RECLAIMED_BYTES": reclaimed,
                "measurement_method": "minimum of the same-container conservative free increase and VM APFS capacity-in-use reduction measured across fresh observations",
                "pending_receipt_path": pending_path.relative_to(policy.REPO).as_posix(),
                "pending_receipt_sha256": pending_sha,
                "post_rename_descriptor_scan": post_rename_descriptor_scan,
                "p2_cleanup_success_claimed": False,
                "p2_base_provisioning_accepted": False,
                "vm_guest_base_provisioned": False,
                "vm_guest_base_sealed": False,
                "future_vm_work_requires_fresh_p1": True,
                "active_marker_sha256": marker_sha,
                "active_marker_rewritten": False,
            }
            return _write_signed_json_unique(
                final_path, final, VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
        finally:
            os.close(p2_lock)


def _deferred_restore_recovery_file_identity(info: os.stat_result) -> dict[str, Any]:
    """Capture the crash-recovery identity without treating ctime as rename-stable."""
    identity: dict[str, Any] = _deferred_restore_file_identity(info)
    birthtime_ns = getattr(info, "st_birthtime_ns", None)
    if not isinstance(birthtime_ns, int) or isinstance(birthtime_ns, bool):
        birthtime = getattr(info, "st_birthtime", None)
        if (isinstance(birthtime, (int, float)) and not isinstance(birthtime, bool)
                and math.isfinite(float(birthtime)) and birthtime >= 0):
            birthtime_ns = int(round(float(birthtime) * 1_000_000_000))
        else:
            birthtime_ns = None
    identity["birthtime_ns"] = birthtime_ns
    identity["file_type"] = "regular"
    return identity


def _deferred_restore_recovery_file_identity_at(
        root_fd: int, name: str) -> dict[str, Any]:
    try:
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as error:
        raise RuntimeError("deferred recovery quarantine no-follow stat failed") from error
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("deferred recovery quarantine is not a regular file")
    if info.st_nlink != 1:
        raise RuntimeError("deferred recovery quarantine link count is not one")
    return _deferred_restore_recovery_file_identity(info)


def _assert_deferred_restore_recovery_identity(
        expected: Mapping[str, Any], current: Mapping[str, Any], *,
        compare_ctime: bool = True) -> None:
    """Require the reviewed stable identity and phase metadata for recovery."""
    if not isinstance(expected, Mapping) or not isinstance(current, Mapping):
        raise RuntimeError("deferred recovery file identity is malformed")
    required = (
        "device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns",
        "owner_uid", "owner_gid", "mode", "link_count",
    )
    for field in required:
        left, right = expected.get(field), current.get(field)
        if (not isinstance(left, int) or isinstance(left, bool)
                or not isinstance(right, int) or isinstance(right, bool)):
            raise RuntimeError("deferred recovery file identity is incomplete")
        if left != right:
            raise RuntimeError(f"deferred recovery {field} mismatch")
    if expected.get("file_type", "regular") != "regular" or current.get("file_type") != "regular":
        raise RuntimeError("deferred recovery target type is not regular")
    expected_birth = expected.get("birthtime_ns")
    current_birth = current.get("birthtime_ns")
    if expected_birth is not None:
        if (not isinstance(expected_birth, int) or isinstance(expected_birth, bool)
                or not isinstance(current_birth, int) or isinstance(current_birth, bool)
                or abs(expected_birth - current_birth) > VM_R13_BIRTHTIME_TOLERANCE_NS):
            raise RuntimeError("deferred recovery birth time mismatch or unavailable")
    if compare_ctime:
        expected_ctime, current_ctime = expected.get("ctime_ns"), current.get("ctime_ns")
        if (not isinstance(expected_ctime, int) or isinstance(expected_ctime, bool)
                or not isinstance(current_ctime, int) or isinstance(current_ctime, bool)
                or expected_ctime != current_ctime):
            raise RuntimeError("deferred recovery ctime changed during a stable phase")


def _deferred_restore_recovery_expected_identity(
        pending: Mapping[str, Any]) -> dict[str, Any]:
    historical = pending.get("restore_file_identity")
    if not isinstance(historical, Mapping):
        raise RuntimeError("old PENDING receipt omits its restore identity")
    expected = dict(VM_R13_EXPECTED_QUARANTINE_IDENTITY)
    for field in ("device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns",
                  "owner_uid", "owner_gid", "mode", "link_count"):
        if historical.get(field) != expected[field]:
            raise RuntimeError(f"old PENDING restore {field} differs from the recovery anchor")
    expected["ctime_ns"] = historical.get("ctime_ns")
    return expected


def _assert_deferred_restore_recovery_namespace(
        root_fd: int, quarantine_name: str) -> list[str]:
    try:
        os.stat("restore.ipsw", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RuntimeError("canonical restore namespace state is unavailable") from error
    else:
        raise RuntimeError("canonical restore path unexpectedly reappeared")
    _assert_deferred_restore_namespace(root_fd, [quarantine_name])
    return _deferred_restore_namespace_candidates(root_fd)


def _hash_deferred_restore_recovery_descriptor(
        root_fd: int, quarantine_name: str, descriptor: int, *,
        expected_identity: Mapping[str, Any], expected_sha256: str,
        expected_bytes: int, compare_expected_ctime: bool = True
        ) -> tuple[str, int, dict[str, Any]]:
    """Hash the entire held no-follow descriptor and recheck its exact namespace binding."""
    before_fd = _deferred_restore_recovery_file_identity(os.fstat(descriptor))
    _assert_deferred_restore_recovery_identity(
        expected_identity, before_fd, compare_ctime=compare_expected_ctime)
    before_path = _deferred_restore_recovery_file_identity_at(root_fd, quarantine_name)
    _assert_deferred_restore_recovery_identity(before_fd, before_path)
    if before_path != before_fd:
        raise RuntimeError("deferred recovery quarantine changed while opening")
    os.lseek(descriptor, 0, os.SEEK_SET)
    hasher = hashlib.sha256()
    byte_count = 0
    while True:
        block = os.read(descriptor, 8 * MIB)
        if not block:
            break
        byte_count += len(block)
        if byte_count > expected_bytes:
            raise RuntimeError("deferred recovery payload exceeds the exact byte count")
        hasher.update(block)
    digest = hasher.hexdigest()
    after_fd = _deferred_restore_recovery_file_identity(os.fstat(descriptor))
    after_path = _deferred_restore_recovery_file_identity_at(root_fd, quarantine_name)
    _assert_deferred_restore_recovery_identity(before_fd, after_fd)
    _assert_deferred_restore_recovery_identity(before_fd, after_path)
    if (after_fd != after_path or byte_count != expected_bytes
            or byte_count != expected_identity.get("logical_bytes")
            or digest != expected_sha256):
        raise RuntimeError("deferred recovery full hash, byte count, or path stability check failed")
    return digest, byte_count, after_fd


def _deferred_recovery_require_process_scan() -> dict[str, Any]:
    evidence = _deferred_process_scan()
    if (evidence.get("scan_status") != "PASS"
            or evidence.get("blocking_processes") != []):
        raise RuntimeError("VM/process scan blocks deferred restore recovery")
    return evidence


def _deferred_recovery_require_descriptor_scan(
        paths: list[Path], *, allow_current_process_only: bool = False
        ) -> dict[str, Any]:
    import p2_base_orchestrator as p2

    evidence = p2._descriptor_scan_evidence(paths)
    files = [row for process in evidence.get("open_file_descriptors", [])
             for row in process.get("files", [])]
    exact = evidence.get("exact_target_descriptor_matches", [])
    if evidence.get("descriptor_identity_mismatches") != []:
        raise RuntimeError("descriptor scan found an identity mismatch during recovery")
    if not allow_current_process_only:
        if (evidence.get("scan_clear") is not True or files or exact
                or evidence.get("open_process_ids") != []):
            raise RuntimeError("another process or descriptor holds the recovery quarantine")
    elif (len(files) != 1 or len(exact) != 1
          or exact[0].get("pid") != os.getpid()
          or evidence.get("open_process_ids") != [str(os.getpid())]):
        raise RuntimeError("recovery quarantine descriptor scan is not limited to this controller")
    return evidence


def _deferred_recovery_lock_evidence(lock_file: Any) -> dict[str, Any]:
    info = os.fstat(lock_file.fileno())
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError("storage allocation lock identity is unsafe during recovery")
    return {"held": True, "device": int(info.st_dev), "inode": int(info.st_ino),
            "owner_uid": int(info.st_uid), "mode": int(stat.S_IMODE(info.st_mode)),
            "link_count": int(info.st_nlink)}


def _deferred_recovery_path_review(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    review, _raw, digest, _identity = _read_json_file_snapshot(
        path, maximum_bytes=256 * 1024)
    current_controller = _controller_identity()
    tests_path = policy.REPO / "experiments/storage/test_vm_capacity_domain.py"
    current_tests_sha = _file_sha256(tests_path)
    reviewed_at = review.get("reviewed_at_unix")
    if (review.get("schema_version") != 1
            or review.get("record_type") != "R6_VM_DEFERRED_RESTORE_RECOVERY_PATH_REVIEW"
            or review.get("status") != "PASS"
            or review.get("decision") != VM_R13_PATH_REVIEW_DECISION
            or review.get("reviewer_identity")
                != "GPT-6 Luna Max — Deferred Restore Recovery Path Reviewer"
            or review.get("model_identity") != "GPT-6 Luna Max"
            or review.get("reviewed_controller_identity") != current_controller
            or review.get("reviewed_tests_sha256") != current_tests_sha
            or review.get("reviewed_real_payload") is not False
            or review.get("findings") != []
            or not isinstance(reviewed_at, (int, float))
            or isinstance(reviewed_at, bool)
            or not math.isfinite(float(reviewed_at))
            or reviewed_at <= 0 or reviewed_at > time.time() + 1
            or time.time() - reviewed_at > VM_R13_VALIDATION_MAX_AGE_SECONDS):
        raise RuntimeError("fresh independent recovery-path review is absent, stale, or not bound to current source")
    reference = _deferred_repo_reference(path)
    if reference.get("sha256") != digest:
        raise RuntimeError("recovery-path review changed while its identity was checked")
    return review, {"path": reference["path"], "sha256": digest}


def _deferred_restore_recovery_r11_anchor(review_raw: bytes) -> tuple[str, dict[str, Any]]:
    """Extract the R11 result from the hash-pinned R12 review, not its vanished /tmp file."""
    try:
        review_text = review_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("R12 quarantine identity provenance evidence is invalid") from error

    matching_rows: list[list[str]] = []
    for line in review_text.splitlines():
        cells = line.split("|")
        if (len(cells) == 5
                and cells[1].strip().strip("`") == str(VM_R13_R11_FAILURE_PATH)):
            matching_rows.append(cells)
    if len(matching_rows) != 1:
        raise RuntimeError("R11 failure evidence is missing or ambiguous in the preserved R12 review")

    cells = matching_rows[0]
    r11_sha = cells[2].strip().strip("`")
    description = cells[3].strip()
    result_match = re.fullmatch(
        r"Failure result is `(.+)`\. It supplies no observed identity tuple or comparison detail\.",
        description)
    if r11_sha != VM_R13_R11_FAILURE_SHA256 or result_match is None:
        raise RuntimeError("R11 failure evidence differs from the preserved provenance anchor")
    try:
        r11 = json.loads(result_match.group(1))
    except json.JSONDecodeError as error:
        raise RuntimeError("R11 failure evidence in the preserved R12 review is invalid") from error
    if r11 != {
            "error": "quarantined inode differs from the exact authorized restore",
            "status": "FAIL",
    }:
        raise RuntimeError("R11 failure evidence differs from the preserved provenance anchor")
    return r11_sha, r11


def _deferred_restore_recovery_ancestry() -> dict[str, Any]:
    """Verify the immutable R11/R12/PENDING evidence chain without touching the payload."""
    pending, _raw, pending_sha, _identity = _read_signed_record_snapshot(
        VM_R13_PENDING_RECEIPT_PATH, VM_DEFERRED_RESTORE_PENDING_SIGNATURE_FIELD)
    old_authorization, _raw, authorization_sha, _identity = _read_signed_record_snapshot(
        VM_R13_OLD_AUTHORIZATION_PATH,
        VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
    if (pending_sha != VM_R13_PENDING_RECEIPT_SHA256
            or authorization_sha != VM_R13_OLD_AUTHORIZATION_SHA256
            or pending.get("record_type") != "R6_VM_DEFERRED_RESTORE_RETIREMENT_PENDING"
            or pending.get("status") != "PENDING"
            or pending.get("action") != "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY"
            or pending.get("authorization_sha256") != authorization_sha
            or pending.get("authorization_path") != VM_R13_OLD_AUTHORIZATION_PATH.name
            or pending.get("predelete_review_path") != VM_R13_OLD_PREDELETE_REVIEW_PATH.name
            or pending.get("predelete_review_sha256") != VM_R13_OLD_PREDELETE_REVIEW_SHA256
            or pending.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or pending.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256
            or pending.get("restore_file_identity", {}).get("logical_bytes")
                != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or old_authorization.get("record_type")
                != "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION"
            or old_authorization.get("status") != "AUTHORIZED"
            or old_authorization.get("action") != "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY"
            or old_authorization.get("action_id") != "2e6cd78d402945e08d5a755b86e55bac"
            or old_authorization.get("authorization_scope_state")
                != "LOCAL_RESTORE_PRESENT_AWAITING_DELETE"
            or old_authorization.get("local_restore_payload_retired") is not False
            or old_authorization.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or old_authorization.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256
            or old_authorization.get("restore_logical_bytes")
                != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or old_authorization.get("active_marker_sha256")
                != VM_DEFERRED_ACTIVE_MARKER_SHA256
            or old_authorization.get("committed_growth_bytes") != 19_350_155_264
            or old_authorization.get("session_id") != VM_DEFERRED_SESSION_ID
            or old_authorization.get("p2_base_provisioning_accepted") is not False
            or old_authorization.get("vm_guest_base_provisioned") is not False
            or old_authorization.get("vm_guest_base_sealed") is not False
            or old_authorization.get("p2_cleanup_success_claimed") is not False):
        raise RuntimeError("immutable old PENDING/authentication ancestry does not match R13 anchors")
    expected_identity = _deferred_restore_recovery_expected_identity(pending)
    if (old_authorization.get("restore_file_identity", {}).get("device")
            != expected_identity["device"]
            or old_authorization.get("restore_file_identity", {}).get("inode")
            != expected_identity["inode"]
            or pending.get("restore_file_identity")
                != old_authorization.get("restore_file_identity")
            or pending.get("vm_volume_root_identity")
                != old_authorization.get("vm_volume_root_identity")
            or pending.get("vm_volume_root_identity") != VM_R13_EXPECTED_ROOT_IDENTITY):
        raise RuntimeError("old pending and consumed authorization identities disagree")
    branch_record, branch_path, branch_sha = _deferred_read_repo_signed(
        pending.get("branch_retirement_receipt_path", ""),
        pending.get("branch_retirement_receipt_sha256", ""),
        VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
    if (branch_record.get("status") != "PASS"
            or branch_record.get("decision")
                != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"
            or old_authorization.get("branch_retirement_receipt_sha256") != branch_sha):
        raise RuntimeError("old PENDING transaction does not bind its signed branch retirement")
    old_review, _raw, old_review_sha, _identity = _read_json_file_snapshot(
        VM_R13_OLD_PREDELETE_REVIEW_PATH, maximum_bytes=256 * 1024)
    if (old_review_sha != VM_R13_OLD_PREDELETE_REVIEW_SHA256
            or old_review.get("status") != "PASS"
            or old_review.get("decision") != "PASS — DEFERRED VM RESTORE PREDELETE REVIEW"
            or old_review.get("reviewed_authorization_sha256") != authorization_sha
            or old_review.get("reviewed_branch_retirement_receipt_sha256") != branch_sha
            or old_review.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or old_review.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256):
        raise RuntimeError("old predelete review is not the exact immutable signed-action review")
    try:
        r12_quarantine_raw = policy._read_file_nofollow(
            VM_R13_R12_QUARANTINE_REVIEW_PATH, maximum_bytes=256 * 1024)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError("R12 quarantine identity provenance evidence is unavailable") from error
    r12_quarantine_sha = _sha256(r12_quarantine_raw)
    if r12_quarantine_sha != VM_R13_R12_QUARANTINE_REVIEW_SHA256:
        raise RuntimeError("R12 quarantine identity review evidence changed")
    r11_sha, _r11 = _deferred_restore_recovery_r11_anchor(r12_quarantine_raw)
    r12_quarantine_ref = _deferred_repo_reference(VM_R13_R12_QUARANTINE_REVIEW_PATH)
    if r12_quarantine_ref.get("sha256") != r12_quarantine_sha:
        raise RuntimeError("R12 quarantine identity review evidence changed while it was checked")
    r12_identity_sha = _file_sha256(VM_R13_R12_IDENTITY_REVIEW_PATH)
    if r12_identity_sha != VM_R13_R12_IDENTITY_REVIEW_SHA256:
        raise RuntimeError("R12 identity-contract review evidence changed")
    return {
        "pending_receipt_path": _deferred_repo_reference(VM_R13_PENDING_RECEIPT_PATH)["path"],
        "pending_receipt_sha256": pending_sha,
        "old_authorization_path": _deferred_repo_reference(VM_R13_OLD_AUTHORIZATION_PATH)["path"],
        "old_authorization_sha256": authorization_sha,
        "old_predelete_review_path": _deferred_repo_reference(
            VM_R13_OLD_PREDELETE_REVIEW_PATH)["path"],
        "old_predelete_review_sha256": old_review_sha,
        "r11_failure_path": str(VM_R13_R11_FAILURE_PATH),
        "r11_failure_sha256": r11_sha,
        "r11_failure_anchor_source_path": r12_quarantine_ref["path"],
        "r12_identity_contract_review_path": _deferred_repo_reference(
            VM_R13_R12_IDENTITY_REVIEW_PATH)["path"],
        "r12_identity_contract_review_sha256": r12_identity_sha,
        "r12_quarantine_identity_review_path": r12_quarantine_ref["path"],
        "r12_quarantine_identity_review_sha256": r12_quarantine_sha,
        "r12_controller_source_sha256": VM_R13_R12_CONTROLLER_SHA256,
        "r12_test_source_sha256": VM_R13_R12_TEST_SHA256,
        "branch_retirement_receipt_path": branch_path.resolve().relative_to(
            policy.REPO.resolve()).as_posix(),
        "branch_retirement_receipt_sha256": branch_sha,
        "expected_quarantine_path": str(
            VM_DEFERRED_RESTORE_PATH.parent / VM_R13_EXPECTED_QUARANTINE_NAME),
        "expected_quarantine_name": VM_R13_EXPECTED_QUARANTINE_NAME,
        "expected_quarantine_identity": expected_identity,
        "pending_root_identity": pending["vm_volume_root_identity"],
        "active_marker_sha256": old_authorization["active_marker_sha256"],
        "committed_growth_bytes": old_authorization["committed_growth_bytes"],
        "session_id": old_authorization["session_id"],
        "p1_provenance_historically_accepted": True,
        "p2_base_provisioning_accepted": False,
        "vm_guest_base_provisioned": False,
        "vm_guest_base_sealed": False,
    }


def _deferred_recovery_review_fields(
        validation: Mapping[str, Any], validation_path: Path, validation_sha: str
        ) -> dict[str, Any]:
    return {
        "reviewer_identity": "GPT-6 Luna Max — Quarantined Restore Recovery Reviewer",
        "review_scope": "Read-only independent review of the exact signed full-hash recovery validation and linked R11/R12/PENDING evidence.",
        "reviewed_validation_path": _deferred_repo_reference(validation_path)["path"],
        "reviewed_validation_sha256": validation_sha,
        "reviewed_pending_receipt_sha256": validation.get("pending_receipt_sha256"),
        "reviewed_old_authorization_sha256": validation.get("old_authorization_sha256"),
        "reviewed_old_predelete_review_sha256": validation.get("old_predelete_review_sha256"),
        "reviewed_r11_failure_sha256": validation.get("r11_failure_sha256"),
        "reviewed_r12_identity_contract_review_sha256": validation.get(
            "r12_identity_contract_review_sha256"),
        "reviewed_r12_quarantine_identity_review_sha256": validation.get(
            "r12_quarantine_identity_review_sha256"),
        "reviewed_branch_retirement_receipt_sha256": validation.get(
            "branch_retirement_receipt_sha256"),
        "reviewed_p1_admission_sha256": validation.get("p1_admission_sha256"),
        "reviewed_p1_verification_sha256": validation.get("p1_verification_sha256"),
        "reviewed_full_sha256": validation.get("current_sha256"),
        "reviewed_exact_byte_count": validation.get("current_byte_count"),
        "reviewed_quarantine_path": validation.get("quarantine_path"),
        "reviewed_quarantine_file_identity": validation.get("quarantine_file_identity"),
        "reviewed_namespace_state": validation.get("namespace_state"),
        "reviewed_process_scan_before": validation.get("vm_process_scan_before"),
        "reviewed_process_scan_after": validation.get("vm_process_scan_after"),
        "reviewed_descriptor_scan_before": validation.get("restore_descriptor_scan_before"),
        "reviewed_descriptor_scan_after": validation.get("restore_descriptor_scan_after"),
        "reviewed_current_controller_identity": validation.get("current_controller_identity"),
        "reviewed_current_tests_sha256": validation.get("current_tests_sha256"),
        "reviewed_p1_restore_acquisition_historically_accepted": validation.get(
            "p1_restore_acquisition_historically_accepted"),
        "reviewed_p1_upstream_content_digest_validated": validation.get(
            "p1_upstream_content_digest_validated"),
        "reviewed_p2_base_provisioning_accepted": validation.get(
            "p2_base_provisioning_accepted"),
        "reviewed_capacity_commitment_inactivation_attempted": validation.get(
            "capacity_commitment_inactivation_attempted"),
    }


def _assert_deferred_restore_recovery_authorization(
        authorization: Mapping[str, Any]) -> None:
    if (authorization.get("record_type")
            != "R6_VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION"
            or authorization.get("status") != "AUTHORIZED"
            or authorization.get("action") != "DELETE_EXACT_PENDING_QUARANTINED_RESTORE"
            or authorization.get("single_use") is not True
            or authorization.get("local_restore_payload_retired") is not False
            or authorization.get("p2_base_provisioning_accepted") is not False
            or authorization.get("vm_guest_base_provisioned") is not False
            or authorization.get("vm_guest_base_sealed") is not False
            or authorization.get("p2_cleanup_success_claimed") is not False
            or authorization.get("restore_path") != str(VM_DEFERRED_RESTORE_PATH)
            or authorization.get("restore_sha256") != VM_DEFERRED_RESTORE_SHA256
            or authorization.get("restore_logical_bytes")
                != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or authorization.get("quarantine_path") != str(
                VM_DEFERRED_RESTORE_PATH.parent / VM_R13_EXPECTED_QUARANTINE_NAME)):
        raise RuntimeError("recovery authorization is absent, replayed, or outside the exact one-use action")


def _assert_deferred_restore_recovery_validation(
        validation: Mapping[str, Any], ancestry: Mapping[str, Any], *,
        current_controller_identity: Mapping[str, Any], current_tests_sha256: str) -> None:
    identity = validation.get("quarantine_file_identity")
    expected_identity = ancestry.get("expected_quarantine_identity")
    namespace = validation.get("namespace_state")
    if (validation.get("record_type") != "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION"
            or validation.get("status") != "PASS_VALIDATED"
            or validation.get("current_sha256") != VM_DEFERRED_RESTORE_SHA256
            or validation.get("current_byte_count") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or validation.get("current_controller_identity") != current_controller_identity
            or validation.get("current_tests_sha256") != current_tests_sha256
            or validation.get("quarantine_path") != ancestry.get("expected_quarantine_path")
            or not isinstance(identity, Mapping)
            or not isinstance(expected_identity, Mapping)
            or validation.get("vm_volume_root_identity")
                != ancestry.get("pending_root_identity")
            or not isinstance(namespace, Mapping)
            or namespace.get("canonical_restore_absent") is not True
            or namespace.get("expected_quarantine_present") is not True
            or namespace.get("candidate_count") != 1
            or namespace.get("namespace_unambiguous") is not True
            or namespace.get("exact_candidate_names")
                != [ancestry.get("expected_quarantine_name")]
            or validation.get("local_restore_payload_retired") is not False
            or validation.get("p2_base_provisioning_accepted") is not False
            or validation.get("vm_guest_base_provisioned") is not False
            or validation.get("vm_guest_base_sealed") is not False
            or validation.get("p2_cleanup_success_claimed") is not False
            or validation.get("capacity_commitment_inactivation_attempted") is not False):
        raise RuntimeError("signed recovery validation is incomplete or outside the exact pending object")
    _assert_deferred_restore_recovery_identity(
        expected_identity, identity, compare_ctime=False)


def inspect_deferred_restore_recovery(
        path_review_path: Path,
        *, validation_path: Path | None = None) -> dict[str, Any]:
    """Hash the exact PENDING quarantine under the reviewed recovery controller locks."""
    import fcntl
    import p2_base_orchestrator as p2

    # This review gate is checked before any access to the live quarantine.
    path_review, path_review_ref = _deferred_recovery_path_review(path_review_path)
    ancestry = _deferred_restore_recovery_ancestry()
    expected_identity = ancestry["expected_quarantine_identity"]
    quarantine_name = ancestry["expected_quarantine_name"]
    quarantine_path = Path(ancestry["expected_quarantine_path"])
    run_id = _deferred_run_id()
    selected_validation_path = validation_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-{run_id}.json")
    _deferred_repo_output_path(selected_validation_path)
    if selected_validation_path.exists() or selected_validation_path.is_symlink():
        raise FileExistsError("recovery validation receipt already exists")

    with policy._acquire_storage_allocation_lock() as allocation_lock:
        allocation_lock_evidence = _deferred_recovery_lock_evidence(allocation_lock)
        p2_lock, p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            # Reload all immutable inputs after the controller locks are held.
            path_review_now, path_review_ref_now = _deferred_recovery_path_review(path_review_path)
            if (path_review_now != path_review or path_review_ref_now != path_review_ref
                    or _deferred_restore_recovery_ancestry() != ancestry):
                raise RuntimeError("recovery review or immutable R11/R12/PENDING evidence changed")
            marker, marker_sha = _deferred_read_exact_active_marker()
            if (marker_sha != ancestry["active_marker_sha256"]
                    or marker.get("committed_growth_bytes")
                        != ancestry["committed_growth_bytes"]):
                raise RuntimeError("active VM marker or future growth commitment changed")
            p1_admission, p1_admission_sha, p1_verification, p1_verification_sha = (
                _deferred_p1_evidence())
            if (p1_verification.get("local_sha256") != VM_DEFERRED_RESTORE_SHA256
                    or p1_verification.get("expected_length_bytes")
                        != VM_DEFERRED_RESTORE_LOGICAL_BYTES
                    or p1_verification.get("actual_length_bytes")
                        != VM_DEFERRED_RESTORE_LOGICAL_BYTES):
                raise RuntimeError("historically accepted P1 restore provenance changed")
            before = capture_host_observation()
            before_storage = _deferred_storage_summary(before)
            mount = _volume_mount_info(before, VM_DEFERRED_VM_VOLUME_UUID)
            if (mount.get("mount_state") != "MOUNTED_VERIFIED"
                    or mount.get("mount_point") != str(VM_DEFERRED_RESTORE_PATH.parent)):
                raise RuntimeError("expected VM APFS mount is not mounted at the reviewed path")
            root_fd = policy._open_directory_nofollow(VM_DEFERRED_RESTORE_PATH.parent)
            restore_fd: int | None = None
            try:
                _verify_open_mount_fd(root_fd, mount)
                root_info = os.fstat(root_fd)
                root_identity = {
                    "device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                    "owner_uid": int(root_info.st_uid),
                    "mode": int(stat.S_IMODE(root_info.st_mode)),
                }
                if (root_identity != VM_R13_EXPECTED_ROOT_IDENTITY
                        or root_identity != ancestry.get("pending_root_identity")):
                    raise RuntimeError("expected VM mount root identity changed")
                namespace_before = _assert_deferred_restore_recovery_namespace(
                    root_fd, quarantine_name)
                tree_before = _deferred_restore_tree_evidence(
                    expect_restore=False, expected_quarantine_name=quarantine_name)
                if tree_before["blockers"]:
                    raise RuntimeError("recovery quarantine inventory is unsafe: "
                                       + "; ".join(tree_before["blockers"]))
                process_before = _deferred_recovery_require_process_scan()
                descriptor_before = _deferred_recovery_require_descriptor_scan(
                    [quarantine_path])
                path_identity = _deferred_restore_recovery_file_identity_at(
                    root_fd, quarantine_name)
                _assert_deferred_restore_recovery_identity(
                    expected_identity, path_identity, compare_ctime=False)
                restore_fd = os.open(
                    quarantine_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
                _verify_open_mount_fd(restore_fd, mount)
                descriptor_identity = _deferred_restore_recovery_file_identity(
                    os.fstat(restore_fd))
                _assert_deferred_restore_recovery_identity(
                    expected_identity, descriptor_identity, compare_ctime=False)
                _assert_deferred_restore_recovery_identity(
                    descriptor_identity, path_identity)
                try:
                    fcntl.flock(restore_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError("recovery quarantine is held by another cooperative reader") from error
                digest, byte_count, identity_after_hash = (
                    _hash_deferred_restore_recovery_descriptor(
                        root_fd, quarantine_name, restore_fd,
                        expected_identity=descriptor_identity,
                        expected_sha256=VM_DEFERRED_RESTORE_SHA256,
                        expected_bytes=VM_DEFERRED_RESTORE_LOGICAL_BYTES))
                process_after = _deferred_recovery_require_process_scan()
                descriptor_after = _deferred_recovery_require_descriptor_scan(
                    [quarantine_path], allow_current_process_only=True)
                namespace_after = _assert_deferred_restore_recovery_namespace(
                    root_fd, quarantine_name)
                tree_after = _deferred_restore_tree_evidence(
                    expect_restore=False, expected_quarantine_name=quarantine_name)
                if tree_after["blockers"]:
                    raise RuntimeError("recovery quarantine inventory changed during hashing")
                current_marker, current_marker_sha = _deferred_read_exact_active_marker()
                if current_marker_sha != marker_sha or current_marker != marker:
                    raise RuntimeError("active VM marker changed during recovery inspection")
                record = {
                    "schema_version": 1,
                    "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION",
                    "status": "PASS_VALIDATED",
                    "created_at_unix": time.time(),
                    "operation": "recover-deferred-restore-retirement",
                    **ancestry,
                    "path_review_path": path_review_ref["path"],
                    "path_review_sha256": path_review_ref["sha256"],
                    "path_review_decision": path_review.get("decision"),
                    "current_controller_identity": _controller_identity(),
                    "current_tests_sha256": _file_sha256(
                        policy.REPO / "experiments/storage/test_vm_capacity_domain.py"),
                    "active_marker_sha256": marker_sha,
                    "active_marker_state": marker.get("state"),
                    "active_marker_phase": marker.get("phase_id"),
                    "committed_growth_bytes": marker.get("committed_growth_bytes"),
                    "capacity_commitment_inactivation_attempted": False,
                    "p1_admission_path": VM_DEFERRED_P1_ADMISSION_PATH.relative_to(
                        policy.REPO).as_posix(),
                    "p1_admission_sha256": p1_admission_sha,
                    "p1_verification_path": VM_DEFERRED_P1_VERIFICATION_PATH.relative_to(
                        policy.REPO).as_posix(),
                    "p1_verification_sha256": p1_verification_sha,
                    "p1_restore_acquisition_historically_accepted": True,
                    "p1_upstream_content_digest_validated": False,
                    "quarantine_path": str(quarantine_path),
                    "quarantine_file_identity": identity_after_hash,
                    "current_sha256": digest,
                    "current_byte_count": byte_count,
                    "full_payload_hash_count": 1,
                    "namespace_state": {
                        "canonical_restore_absent": True,
                        "expected_quarantine_present": True,
                        "exact_candidate_names": namespace_after,
                        "candidate_count": 1,
                        "namespace_unambiguous": True,
                    },
                    "vm_process_scan_before": process_before,
                    "vm_process_scan_after": process_after,
                    "restore_descriptor_scan_before": descriptor_before,
                    "restore_descriptor_scan_after": descriptor_after,
                    "restore_tree_inventory_before": tree_before,
                    "restore_tree_inventory_after": tree_after,
                    "vm_mount_identity": mount,
                    "vm_volume_root_identity": root_identity,
                    "storage_observation_before": before_storage,
                    "storage_allocation_lock": allocation_lock_evidence,
                    "p2_cleanup_lock": p2_lock_evidence,
                    "local_restore_payload_retired": False,
                    "p2_base_provisioning_accepted": False,
                    "vm_guest_base_provisioned": False,
                    "vm_guest_base_sealed": False,
                    "p2_cleanup_success_claimed": False,
                }
                return _write_signed_json_unique(
                    selected_validation_path, record,
                    VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD)
            finally:
                if restore_fd is not None:
                    os.close(restore_fd)
                os.close(root_fd)
        finally:
            os.close(p2_lock)


def authorize_deferred_restore_recovery(
        validation_path: Path,
        recovery_review_path: Path,
        *, authorization_path: Path | None = None) -> dict[str, Any]:
    """Issue a new one-use authority only after the exact current hash is reviewed."""
    validation, _raw, validation_sha, _identity = _read_signed_record_snapshot(
        validation_path, VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD)
    ancestry = _deferred_restore_recovery_ancestry()
    current_controller_identity = _controller_identity()
    current_tests_sha256 = _file_sha256(
        policy.REPO / "experiments/storage/test_vm_capacity_domain.py")
    _assert_deferred_restore_recovery_validation(
        validation, ancestry, current_controller_identity=current_controller_identity,
        current_tests_sha256=current_tests_sha256)
    if any(validation.get(key) != ancestry.get(key) for key in (
            "pending_receipt_sha256", "old_authorization_sha256",
            "old_predelete_review_sha256", "r11_failure_sha256",
            "r12_identity_contract_review_sha256", "r12_quarantine_identity_review_sha256",
            "expected_quarantine_path", "expected_quarantine_name")):
        raise RuntimeError("signed recovery validation is stale or does not bind the exact pending object")
    validation_created = validation.get("created_at_unix")
    if (not isinstance(validation_created, (int, float)) or isinstance(validation_created, bool)
            or time.time() - float(validation_created) < 0
            or time.time() - float(validation_created) > VM_R13_VALIDATION_MAX_AGE_SECONDS):
        raise RuntimeError("signed recovery validation is stale")
    path_review_path = policy.REPO / str(validation.get("path_review_path", ""))
    path_review, path_review_ref = _deferred_recovery_path_review(path_review_path)
    if path_review_ref.get("sha256") != validation.get("path_review_sha256"):
        raise RuntimeError("recovery-path review changed after source acceptance")
    review, review_sha = _deferred_fresh_review_json(
        recovery_review_path,
        decision=VM_R13_RECOVERY_REVIEW_DECISION,
        expected_fields=_deferred_recovery_review_fields(
            validation, validation_path, validation_sha))
    _require_deferred_review_postdates(review, validation_created)
    prior_authorities = sorted(policy.REPO.glob(
        "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-*.json"))
    prior_uses = sorted(policy.REPO.glob("R6-VM-DEFERRED-RESTORE-RECOVERY-USE-*.json"))
    prior_resolutions = sorted(policy.REPO.glob(
        "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-*.json"))
    if prior_authorities or prior_uses or prior_resolutions:
        raise RuntimeError("a recovery authority/use/resolution already exists for the immutable PENDING action")
    run_id = _deferred_run_id()
    selected_authorization_path = authorization_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-{run_id}.json")
    _deferred_repo_output_path(selected_authorization_path)
    if selected_authorization_path.exists() or selected_authorization_path.is_symlink():
        raise FileExistsError("fresh recovery authorization path already exists")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION",
        "status": "AUTHORIZED",
        "created_at_unix": time.time(),
        "action_id": uuid.uuid4().hex,
        "action": "DELETE_EXACT_PENDING_QUARANTINED_RESTORE",
        "single_use": True,
        "recovery_validation_path": _deferred_repo_reference(validation_path)["path"],
        "recovery_validation_sha256": validation_sha,
        "recovery_review_path": _deferred_repo_reference(recovery_review_path)["path"],
        "recovery_review_sha256": review_sha,
        "path_review_path": path_review_ref["path"],
        "path_review_sha256": path_review_ref["sha256"],
        "pending_receipt_path": ancestry["pending_receipt_path"],
        "pending_receipt_sha256": ancestry["pending_receipt_sha256"],
        "old_authorization_path": ancestry["old_authorization_path"],
        "old_authorization_sha256": ancestry["old_authorization_sha256"],
        "old_predelete_review_path": ancestry["old_predelete_review_path"],
        "old_predelete_review_sha256": ancestry["old_predelete_review_sha256"],
        "branch_retirement_receipt_path": ancestry["branch_retirement_receipt_path"],
        "branch_retirement_receipt_sha256": ancestry["branch_retirement_receipt_sha256"],
        "r11_failure_path": ancestry["r11_failure_path"],
        "r11_failure_sha256": ancestry["r11_failure_sha256"],
        "r12_identity_contract_review_path": ancestry[
            "r12_identity_contract_review_path"],
        "r12_identity_contract_review_sha256": ancestry[
            "r12_identity_contract_review_sha256"],
        "r12_quarantine_identity_review_path": ancestry[
            "r12_quarantine_identity_review_path"],
        "r12_quarantine_identity_review_sha256": ancestry[
            "r12_quarantine_identity_review_sha256"],
        "r12_controller_source_sha256": ancestry["r12_controller_source_sha256"],
        "recovery_path_review_decision": path_review.get("decision"),
        "recovery_review_decision": review.get("decision"),
        "restore_path": str(VM_DEFERRED_RESTORE_PATH),
        "quarantine_path": validation["quarantine_path"],
        "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
        "restore_logical_bytes": VM_DEFERRED_RESTORE_LOGICAL_BYTES,
        "fresh_full_sha256": validation["current_sha256"],
        "fresh_byte_count": validation["current_byte_count"],
        "restore_file_identity": validation["quarantine_file_identity"],
        "vm_volume_root_identity": validation["vm_volume_root_identity"],
        "active_marker_sha256": validation["active_marker_sha256"],
        "committed_growth_bytes": validation["committed_growth_bytes"],
        "current_controller_identity": validation["current_controller_identity"],
        "local_restore_payload_retired": False,
        "p1_restore_acquisition_historically_accepted": True,
        "p1_upstream_content_digest_validated": False,
        "p2_base_provisioning_accepted": False,
        "vm_guest_base_provisioned": False,
        "vm_guest_base_sealed": False,
        "p2_cleanup_success_claimed": False,
        "capacity_commitment_inactivation_attempted": False,
    }
    _assert_deferred_restore_recovery_authorization(record)
    return _write_signed_json_unique(
        selected_authorization_path, record,
        VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION_SIGNATURE_FIELD)


def perform_deferred_restore_recovery(
        authorization_path: Path,
        recovery_review_path: Path,
        *, resolution_receipt_path: Path | None = None) -> dict[str, Any]:
    """Rehash and unlink only the newly authorized quarantine, then resolve PENDING additively."""
    import fcntl
    import p2_base_orchestrator as p2

    authorization, _raw, authorization_sha, _identity = _read_signed_record_snapshot(
        authorization_path, VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION_SIGNATURE_FIELD)
    _assert_deferred_restore_recovery_authorization(authorization)
    action_id = authorization.get("action_id")
    if not isinstance(action_id, str) or not re.fullmatch(r"[0-9a-f]{32}", action_id):
        raise RuntimeError("recovery authorization action identifier is malformed")
    validation_path_text = authorization.get("recovery_validation_path")
    if (not isinstance(validation_path_text, str) or not validation_path_text
            or Path(validation_path_text).is_absolute()
            or ".." in Path(validation_path_text).parts):
        raise RuntimeError("recovery authorization validation path is malformed")
    validation_path = policy.REPO / validation_path_text
    validation, _raw, validation_sha, _identity = _read_signed_record_snapshot(
        validation_path, VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD)
    ancestry = _deferred_restore_recovery_ancestry()
    current_controller_identity = _controller_identity()
    current_tests_sha256 = _file_sha256(
        policy.REPO / "experiments/storage/test_vm_capacity_domain.py")
    _assert_deferred_restore_recovery_validation(
        validation, ancestry, current_controller_identity=current_controller_identity,
        current_tests_sha256=current_tests_sha256)
    if (validation_sha != authorization.get("recovery_validation_sha256")
            or validation.get("current_controller_identity") != current_controller_identity):
        raise RuntimeError("recovery authorization does not bind a fresh passing content validation")
    review, review_sha = _deferred_fresh_review_json(
        recovery_review_path,
        decision=VM_R13_RECOVERY_REVIEW_DECISION,
        expected_fields=_deferred_recovery_review_fields(
            validation, validation_path, validation_sha))
    if review_sha != authorization.get("recovery_review_sha256"):
        raise RuntimeError("independent recovery review changed after authorization")
    review_at, auth_at = review.get("reviewed_at_unix"), authorization.get("created_at_unix")
    if (not isinstance(auth_at, (int, float)) or isinstance(auth_at, bool)
            or not isinstance(review_at, (int, float)) or isinstance(review_at, bool)
            or review_at >= auth_at):
        raise RuntimeError("fresh independent content review must precede its recovery authorization")
    validation_created = validation.get("created_at_unix")
    if (not isinstance(validation_created, (int, float)) or isinstance(validation_created, bool)
            or not (validation_created < review_at < auth_at)
            or auth_at > time.time() + 1
            or time.time() - float(validation_created) > VM_R13_VALIDATION_MAX_AGE_SECONDS):
        raise RuntimeError("recovery validation/review/authorization chronology is invalid or stale")
    path_review_path = policy.REPO / str(validation.get("path_review_path", ""))
    path_review, path_review_ref = _deferred_recovery_path_review(path_review_path)
    if (path_review_ref.get("sha256") != authorization.get("path_review_sha256")
            or path_review_ref.get("sha256") != validation.get("path_review_sha256")):
        raise RuntimeError("recovery-path source review is stale or changed")
    for key in ("pending_receipt_sha256", "old_authorization_sha256",
                "old_predelete_review_sha256", "r11_failure_sha256",
                "r12_identity_contract_review_sha256", "r12_quarantine_identity_review_sha256",
                "expected_quarantine_path", "expected_quarantine_name"):
        if validation.get(key) != ancestry.get(key):
            raise RuntimeError("immutable R11/R12/PENDING ancestry changed after authorization")
    if (authorization.get("pending_receipt_sha256") != ancestry["pending_receipt_sha256"]
            or authorization.get("old_authorization_sha256") != ancestry["old_authorization_sha256"]
            or authorization.get("old_predelete_review_sha256")
                != ancestry["old_predelete_review_sha256"]
            or authorization.get("r11_failure_sha256") != ancestry["r11_failure_sha256"]
            or authorization.get("r12_identity_contract_review_sha256")
                != ancestry["r12_identity_contract_review_sha256"]
            or authorization.get("r12_quarantine_identity_review_sha256")
                != ancestry["r12_quarantine_identity_review_sha256"]):
        raise RuntimeError("fresh authorization does not link every immutable recovery predecessor")
    if (authorization.get("fresh_full_sha256") != validation.get("current_sha256")
            or authorization.get("fresh_byte_count") != validation.get("current_byte_count")
            or authorization.get("restore_file_identity")
                != validation.get("quarantine_file_identity")):
        raise RuntimeError("fresh authorization does not bind the recovery inspection object")
    selected_auth = Path(os.path.abspath(authorization_path))
    auth_paths = sorted(policy.REPO.glob(
        "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-*.json"))
    if len(auth_paths) != 1 or auth_paths[0].resolve() != selected_auth.resolve():
        raise RuntimeError("recovery authorization history is not the exact single-use authority")
    # The single-use claim always has one deterministic location. Accepting a
    # caller-selected path would let a failed attempt be replayed by pointing
    # the next attempt at a different, still-empty receipt path.
    selected_use_path = policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RECOVERY-USE-{action_id}.json")
    selected_resolution_path = resolution_receipt_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-{action_id}.json")
    _deferred_repo_output_path(selected_use_path)
    _deferred_repo_output_path(selected_resolution_path)
    if (selected_use_path.exists() or selected_use_path.is_symlink()
            or selected_resolution_path.exists() or selected_resolution_path.is_symlink()):
        raise RuntimeError("fresh recovery authorization has already been consumed or resolved")
    with policy._acquire_storage_allocation_lock() as allocation_lock:
        allocation_lock_evidence = _deferred_recovery_lock_evidence(allocation_lock)
        p2_lock, p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            authorization_now, _raw, authorization_now_sha, _identity = (
                _read_signed_record_snapshot(
                    authorization_path,
                    VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION_SIGNATURE_FIELD))
            validation_now, _raw, validation_now_sha, _identity = (
                _read_signed_record_snapshot(
                    validation_path,
                    VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD))
            if (authorization_now_sha != authorization_sha
                    or authorization_now != authorization
                    or validation_now_sha != validation_sha
                    or validation_now != validation):
                raise RuntimeError("recovery authority or validation changed at the deletion boundary")
            if (selected_use_path.exists() or selected_use_path.is_symlink()
                    or selected_resolution_path.exists()
                    or selected_resolution_path.is_symlink()):
                raise RuntimeError("recovery authorization replay was detected under controller locks")
            marker, marker_sha = _deferred_read_exact_active_marker()
            if (marker_sha != validation.get("active_marker_sha256")
                    or marker_sha != authorization.get("active_marker_sha256")
                    or marker.get("committed_growth_bytes")
                        != validation.get("committed_growth_bytes")):
                raise RuntimeError("active VM capacity marker changed since recovery validation")
            p1_admission, p1_admission_sha, p1_verification, p1_verification_sha = (
                _deferred_p1_evidence())
            if (p1_admission_sha != validation.get("p1_admission_sha256")
                    or p1_verification_sha != validation.get("p1_verification_sha256")
                    or p1_verification.get("local_sha256") != VM_DEFERRED_RESTORE_SHA256):
                raise RuntimeError("historical P1 acquisition provenance changed before recovery")
            before = capture_host_observation()
            before_storage = _deferred_storage_summary(before)
            mount = _volume_mount_info(before, VM_DEFERRED_VM_VOLUME_UUID)
            if (mount.get("mount_state") != "MOUNTED_VERIFIED"
                    or mount.get("mount_point") != str(VM_DEFERRED_RESTORE_PATH.parent)
                    or mount != validation.get("vm_mount_identity")):
                raise RuntimeError("VM volume/container/root mount identity changed before recovery")
            root_fd = policy._open_directory_nofollow(VM_DEFERRED_RESTORE_PATH.parent)
            restore_fd: int | None = None
            try:
                _verify_open_mount_fd(root_fd, mount)
                root_info = os.fstat(root_fd)
                root_identity = {
                    "device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                    "owner_uid": int(root_info.st_uid),
                    "mode": int(stat.S_IMODE(root_info.st_mode)),
                }
                if (root_identity != VM_R13_EXPECTED_ROOT_IDENTITY
                        or root_identity != validation.get("vm_volume_root_identity")
                        or root_identity != authorization.get("vm_volume_root_identity")):
                    raise RuntimeError("VM volume root identity changed before recovery unlink")
                quarantine_name = ancestry["expected_quarantine_name"]
                quarantine_path = Path(ancestry["expected_quarantine_path"])
                namespace_before = _assert_deferred_restore_recovery_namespace(
                    root_fd, quarantine_name)
                tree_before = _deferred_restore_tree_evidence(
                    expect_restore=False, expected_quarantine_name=quarantine_name)
                if tree_before["blockers"]:
                    raise RuntimeError("pre-unlink restore inventory is unsafe: "
                                       + "; ".join(tree_before["blockers"]))
                process_before = _deferred_recovery_require_process_scan()
                descriptor_before = _deferred_recovery_require_descriptor_scan(
                    [quarantine_path])
                path_identity = _deferred_restore_recovery_file_identity_at(
                    root_fd, quarantine_name)
                _assert_deferred_restore_recovery_identity(
                    validation["quarantine_file_identity"], path_identity)
                restore_fd = os.open(
                    quarantine_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0), dir_fd=root_fd)
                _verify_open_mount_fd(restore_fd, mount)
                descriptor_identity = _deferred_restore_recovery_file_identity(
                    os.fstat(restore_fd))
                _assert_deferred_restore_recovery_identity(
                    validation["quarantine_file_identity"], descriptor_identity)
                _assert_deferred_restore_recovery_identity(
                    descriptor_identity, path_identity)
                try:
                    fcntl.flock(restore_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError("recovery quarantine is held by another cooperative reader") from error
                use_record = {
                    "schema_version": 1,
                    "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_USE",
                    "status": "CONSUMED_FOR_SINGLE_RECOVERY_ATTEMPT",
                    "created_at_unix": time.time(),
                    "authorization_path": _deferred_repo_reference(
                        selected_auth)["path"],
                    "authorization_sha256": authorization_sha,
                    "recovery_validation_sha256": validation_sha,
                    "recovery_review_sha256": review_sha,
                    "pending_receipt_sha256": ancestry["pending_receipt_sha256"],
                    "quarantine_path": str(quarantine_path),
                    "quarantine_file_identity": descriptor_identity,
                    "boundary_full_hash_pending": True,
                    "unlink_completed": False,
                    "reclaimed_bytes_claimed": False,
                    "local_restore_payload_retired": False,
                    "p2_base_provisioning_accepted": False,
                    "vm_guest_base_provisioned": False,
                    "vm_guest_base_sealed": False,
                    "p2_cleanup_success_claimed": False,
                }
                _write_signed_json_unique(
                    selected_use_path, use_record,
                    VM_DEFERRED_RESTORE_RECOVERY_USE_SIGNATURE_FIELD)
                # This second complete hash is the destructive-boundary content check.
                boundary_digest, boundary_bytes, boundary_identity = (
                    _hash_deferred_restore_recovery_descriptor(
                        root_fd, quarantine_name, restore_fd,
                        expected_identity=validation["quarantine_file_identity"],
                        expected_sha256=VM_DEFERRED_RESTORE_SHA256,
                        expected_bytes=VM_DEFERRED_RESTORE_LOGICAL_BYTES))
                process_after_hash = _deferred_recovery_require_process_scan()
                descriptor_after_hash = _deferred_recovery_require_descriptor_scan(
                    [quarantine_path], allow_current_process_only=True)
                namespace_at_unlink = _assert_deferred_restore_recovery_namespace(
                    root_fd, quarantine_name)
                last_path_identity = _deferred_restore_recovery_file_identity_at(
                    root_fd, quarantine_name)
                _assert_deferred_restore_recovery_identity(boundary_identity, last_path_identity)
                _assert_deferred_restore_recovery_identity(
                    boundary_identity,
                    _deferred_restore_recovery_file_identity(os.fstat(restore_fd)))
                if (boundary_digest != VM_DEFERRED_RESTORE_SHA256
                        or boundary_bytes != VM_DEFERRED_RESTORE_LOGICAL_BYTES
                        or namespace_at_unlink != [quarantine_name]):
                    raise RuntimeError("destructive-boundary hash, length, or namespace check failed")
                current_marker, current_marker_sha = _deferred_read_exact_active_marker()
                if current_marker_sha != marker_sha or current_marker != marker:
                    raise RuntimeError("active capacity commitment changed at destructive boundary")
                # Name-based unlink is constrained to the pinned, no-follow entry and
                # immediately follows the final descriptor/path/namespace comparison.
                os.unlink(quarantine_name, dir_fd=root_fd)
                os.fsync(root_fd)
                namespace_after_unlink = _deferred_restore_namespace_candidates(root_fd)
                if namespace_after_unlink:
                    raise RuntimeError("restore candidate remains after exact quarantine unlink")
                try:
                    os.stat("restore.ipsw", dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise RuntimeError("canonical restore path reappeared after quarantine unlink")
                unlinked_info = os.fstat(restore_fd)
                if (not stat.S_ISREG(unlinked_info.st_mode)
                        or unlinked_info.st_dev != boundary_identity["device"]
                        or unlinked_info.st_ino != boundary_identity["inode"]
                        or unlinked_info.st_nlink != 0):
                    raise RuntimeError("held descriptor does not prove the exact unlinked quarantine object")
                unlinked_identity = _deferred_restore_recovery_file_identity(unlinked_info)
                os.close(restore_fd)
                restore_fd = None
            finally:
                if restore_fd is not None:
                    os.close(restore_fd)
                os.close(root_fd)

            tree_after = _deferred_restore_tree_evidence(expect_restore=False)
            if (tree_after["blockers"] or tree_after["restore_paths"]
                    or tree_after.get("quarantine_paths") != []):
                raise RuntimeError("post-unlink VM inventory is incomplete or contains another restore")
            after = capture_host_observation()
            after_storage = _deferred_storage_summary(after)
            before_used = _volume_row(before, VM_DEFERRED_VM_VOLUME_UUID).get("CapacityInUse")
            after_used = _volume_row(after, VM_DEFERRED_VM_VOLUME_UUID).get("CapacityInUse")
            before_free = before_storage.get("conservative_shared_free_bytes")
            after_free = after_storage.get("conservative_shared_free_bytes")
            if (not _integer(before_used) or not _integer(after_used)
                    or not _integer(before_free) or not _integer(after_free)
                    or after_storage.get("vm_volume_uuid") != VM_DEFERRED_VM_VOLUME_UUID
                    or after_storage.get("safety_volume_uuid") != VM_DEFERRED_SAFETY_VOLUME_UUID
                    or after_storage.get("safety_volume_reserve_bytes")
                        != VM_DEFERRED_SAFETY_RESERVE_BYTES):
                raise RuntimeError("fresh post-retirement storage measurement is unavailable")
            quota_reduction = max(0, int(before_used) - int(after_used))
            shared_free_increase = max(0, int(after_free) - int(before_free))
            reclaimed = min(quota_reduction, shared_free_increase)
            final_marker, final_marker_sha = _deferred_read_exact_active_marker()
            if final_marker_sha != marker_sha or final_marker != marker:
                raise RuntimeError("active marker changed during recovery retirement")
            final = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_RESOLUTION",
                "status": "PASS",
                "created_at_unix": time.time(),
                "classification": "PENDING_TRANSACTION_RESOLVED_BY_FRESHLY_AUTHORIZED_RECOVERY",
                "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
                "recovery_authorization_path": _deferred_repo_reference(
                    selected_auth)["path"],
                "recovery_authorization_sha256": authorization_sha,
                "recovery_authorization_consumed_exactly_once": True,
                "recovery_use_receipt_path": _deferred_repo_reference(
                    selected_use_path)["path"],
                "recovery_use_receipt_sha256": _file_sha256(selected_use_path),
                "recovery_validation_path": validation_path_text,
                "recovery_validation_sha256": validation_sha,
                "recovery_review_path": _deferred_repo_reference(
                    recovery_review_path)["path"],
                "recovery_review_sha256": review_sha,
                "recovery_path_review_path": path_review_ref["path"],
                "recovery_path_review_sha256": path_review_ref["sha256"],
                "pending_receipt_path": ancestry["pending_receipt_path"],
                "pending_receipt_sha256": ancestry["pending_receipt_sha256"],
                "branch_retirement_receipt_path": ancestry[
                    "branch_retirement_receipt_path"],
                "branch_retirement_receipt_sha256": ancestry[
                    "branch_retirement_receipt_sha256"],
                "old_authorization_path": ancestry["old_authorization_path"],
                "old_authorization_sha256": ancestry["old_authorization_sha256"],
                "old_predelete_review_path": ancestry["old_predelete_review_path"],
                "old_predelete_review_sha256": ancestry["old_predelete_review_sha256"],
                "r11_failure_path": ancestry["r11_failure_path"],
                "r11_failure_sha256": ancestry["r11_failure_sha256"],
                "r12_identity_contract_review_path": ancestry[
                    "r12_identity_contract_review_path"],
                "r12_identity_contract_review_sha256": ancestry[
                    "r12_identity_contract_review_sha256"],
                "r12_quarantine_identity_review_path": ancestry[
                    "r12_quarantine_identity_review_path"],
                "r12_quarantine_identity_review_sha256": ancestry[
                    "r12_quarantine_identity_review_sha256"],
                "r12_controller_source_sha256": ancestry["r12_controller_source_sha256"],
                "p1_admission_path": VM_DEFERRED_P1_ADMISSION_PATH.relative_to(
                    policy.REPO).as_posix(),
                "p1_admission_sha256": p1_admission_sha,
                "p1_verification_path": VM_DEFERRED_P1_VERIFICATION_PATH.relative_to(
                    policy.REPO).as_posix(),
                "p1_verification_sha256": p1_verification_sha,
                "p1_restore_acquisition_historically_accepted": True,
                "p1_provenance_preserved": True,
                "p1_upstream_content_digest_validated": False,
                "restore_path": str(VM_DEFERRED_RESTORE_PATH),
                "deleted_path": str(ancestry["expected_quarantine_path"]),
                "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
                "deleted_file_sha256": boundary_digest,
                "first_current_full_sha256": validation["current_sha256"],
                "destructive_boundary_full_sha256": boundary_digest,
                "exact_byte_count": boundary_bytes,
                "restore_file_identity_first_inspection": validation[
                    "quarantine_file_identity"],
                "restore_file_identity_destructive_boundary": boundary_identity,
                "restore_file_identity_after_unlink": unlinked_identity,
                "namespace_before_unlink": namespace_at_unlink,
                "canonical_restore_absent": True,
                "restore_absent": True,
                "quarantine_absent": True,
                "no_second_restore_candidate": True,
                "parent_directory_fsynced": True,
                "held_descriptor_confirmed_unlinked_object": True,
                "post_unlink_vm_inventory": tree_after,
                "vm_volume_uuid": VM_DEFERRED_VM_VOLUME_UUID,
                "container_uuid": VM_DEFERRED_CONTAINER_UUID,
                "physical_store_uuid": VM_DEFERRED_PHYSICAL_STORE_UUID,
                "vm_volume_quota_bytes": VM_DEFERRED_QUOTA_BYTES,
                "safety_volume_uuid": VM_DEFERRED_SAFETY_VOLUME_UUID,
                "safety_reserve_bytes": VM_DEFERRED_SAFETY_RESERVE_BYTES,
                "storage_before": before_storage,
                "storage_after": after_storage,
                "vm_volume_used_before_bytes": before_used,
                "vm_volume_used_after_bytes": after_used,
                "shared_free_before_bytes": before_free,
                "shared_free_after_bytes": after_free,
                "measured_vm_quota_reduction_bytes": quota_reduction,
                "measured_shared_free_increase_bytes": shared_free_increase,
                "RESTORE_RETIREMENT_RECLAIMED_BYTES": reclaimed,
                "measurement_method": (
                    "minimum of fresh same-container conservative free increase and "
                    "VM APFS capacity-in-use reduction"),
                "local_restore_payload_retired": True,
                "authorization_scope_state": "LOCAL_RESTORE_RETIRED",
                "p2_base_provisioning_accepted": False,
                "vm_guest_base_provisioned": False,
                "vm_guest_base_sealed": False,
                "p2_cleanup_success_claimed": False,
                "active_marker_sha256": marker_sha,
                "active_marker_rewritten": False,
                "capacity_commitment_inactivation_attempted": False,
            }
            if (not final["canonical_restore_absent"] or not final["quarantine_absent"]
                    or final["RESTORE_RETIREMENT_RECLAIMED_BYTES"] < 0
                    or final["p2_base_provisioning_accepted"] is not False):
                raise RuntimeError("recovery resolution contains an invalid terminal state")
            return _write_signed_json_unique(
                selected_resolution_path, final,
                VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
        finally:
            os.close(p2_lock)


def _r14_resolution_review_fields(
        failure_path: Path, failure_sha256: str,
        failure: Mapping[str, Any], current_controller_identity: Mapping[str, Any],
        current_tests_sha256: str) -> dict[str, Any]:
    ancestry = _deferred_restore_recovery_ancestry()
    return {
        "record_type": "R6_VM_DEFERRED_RESTORE_RESOLUTION_REVIEW",
        "reviewer_identity": "GPT-6 Luna Max — R14 Post-Unlink Resolution Reviewer",
        "review_scope": "Read-only independent review of exact R13 deletion proof and rejected immediate reclaim measurement.",
        "reviewed_measurement_failure_path": _deferred_repo_reference(failure_path)["path"],
        "reviewed_measurement_failure_sha256": failure_sha256,
        "reviewed_pending_receipt_sha256": failure.get("pending_receipt_sha256"),
        "reviewed_recovery_authorization_sha256": failure.get(
            "recovery_authorization_sha256"),
        "reviewed_recovery_validation_sha256": failure.get("recovery_validation_sha256"),
        "reviewed_recovery_path_review_sha256": failure.get("recovery_path_review_sha256"),
        "reviewed_recovery_content_review_sha256": failure.get("recovery_review_sha256"),
        "reviewed_recovery_use_receipt_sha256": failure.get("recovery_use_receipt_sha256"),
        "reviewed_branch_retirement_receipt_sha256": ancestry.get(
            "branch_retirement_receipt_sha256"),
        "reviewed_old_authorization_sha256": failure.get("old_authorization_sha256"),
        "reviewed_old_predelete_review_sha256": failure.get("old_predelete_review_sha256"),
        "reviewed_r11_failure_sha256": failure.get("r11_failure_sha256"),
        "reviewed_r12_identity_contract_review_sha256": failure.get(
            "r12_identity_contract_review_sha256"),
        "reviewed_r12_quarantine_identity_review_sha256": failure.get(
            "r12_quarantine_identity_review_sha256"),
        "reviewed_boundary_full_sha256": failure.get("boundary_full_sha256"),
        "reviewed_boundary_exact_byte_count": failure.get("boundary_exact_byte_count"),
        "reviewed_unlink_completed": failure.get("unlink_completed"),
        "reviewed_parent_directory_fsync_completed": failure.get(
            "parent_directory_fsync_completed"),
        "reviewed_post_attempt_namespace_entries": failure.get(
            "post_attempt_namespace_entries"),
        "reviewed_post_attempt_namespace_candidates": failure.get(
            "post_attempt_namespace_candidates"),
        "reviewed_unlink_completion_evidence": failure.get("unlink_completion_evidence"),
        "reviewed_reclaimed_bytes_exact": None,
        "reviewed_measurement_status": "REJECTED_APFS_CROSSCHECK_DISAGREEMENT",
        "reviewed_later_delta_is_accepted_as_actual_reclaim": failure.get(
            "later_delta_is_accepted_as_actual_reclaim"),
        "reviewed_p1_restore_acquisition_historically_accepted": failure.get(
            "p1_restore_acquisition_historically_accepted"),
        "reviewed_p2_base_provisioning_accepted": failure.get(
            "p2_base_provisioning_accepted"),
        "reviewed_active_marker_sha256": failure.get("active_marker_sha256"),
        "reviewed_current_controller_identity": current_controller_identity,
        "reviewed_current_tests_sha256": current_tests_sha256,
    }


def _assert_r13_post_unlink_measurement_failure(
        failure: Mapping[str, Any], *, ancestry: Mapping[str, Any],
        authorization: Mapping[str, Any], authorization_sha256: str,
        validation: Mapping[str, Any], validation_sha256: str,
        use: Mapping[str, Any], use_sha256: str) -> None:
    """Validate the signed R13 post-action failure without treating use sentinels as outcomes."""
    if (failure.get("record_type")
            != "R6_VM_DEFERRED_RESTORE_RECOVERY_POST_UNLINK_MEASUREMENT_FAILURE"
            or failure.get("status") != "FAIL_MEASUREMENT_RECONCILIATION"
            or failure.get("classification")
                != "LOCAL_RESTORE_ABSENT_BUT_OLD_PENDING_NOT_RESOLVED"
            or failure.get("execution_phase_at_failure") != "POST_UNLINK_STORAGE_MEASUREMENT"
            or "_deferred_storage_summary(after)" not in str(failure.get("error_location"))
            or "APFS plist and Data APFSContainerFree readings disagree beyond tolerance"
                not in str(failure.get("execution_error"))
            or failure.get("unlink_completed") is not True
            or failure.get("parent_directory_fsync_completed") is not True
            or "held descriptor inode/nlink check" not in str(
                failure.get("unlink_completion_evidence"))
            or "zero restore candidates" not in str(
                failure.get("unlink_completion_evidence"))
            or failure.get("canonical_restore_absent") is not True
            or failure.get("quarantine_absent") is not True
            or failure.get("post_attempt_namespace_candidates") != []
            or failure.get("local_restore_payload_retired") is not True
            or failure.get("old_pending_resolved") is not False
            or failure.get("pending_receipt_remains_status") != "PENDING"
            or failure.get("retirement_resolution_issued") is not False
            or failure.get("reclaimed_bytes_claimed") is not False
            or failure.get("RESTORE_RETIREMENT_RECLAIMED_BYTES") is not None
            or failure.get("later_delta_is_accepted_as_actual_reclaim") is not False
            or failure.get("capacity_commitment_inactivation_attempted") is not False
            or failure.get("p2_base_provisioning_accepted") is not False
            or failure.get("p1_restore_acquisition_historically_accepted") is not True
            or failure.get("p1_upstream_content_digest_validated") is not False
            or failure.get("boundary_full_sha256") != VM_DEFERRED_RESTORE_SHA256
            or failure.get("boundary_exact_byte_count") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or not _r13_boundary_hash_evidence_passed(failure)
            or failure.get("first_fresh_full_sha256") != VM_DEFERRED_RESTORE_SHA256
            or failure.get("first_fresh_exact_byte_count") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or failure.get("pending_receipt_sha256") != ancestry.get("pending_receipt_sha256")
            or failure.get("pending_receipt_path") != Path(
                ancestry.get("pending_receipt_path", "")).name
            or failure.get("old_authorization_sha256") != ancestry.get("old_authorization_sha256")
            or failure.get("old_predelete_review_sha256")
                != ancestry.get("old_predelete_review_sha256")
            or failure.get("r11_failure_sha256") != ancestry.get("r11_failure_sha256")
            or failure.get("r12_identity_contract_review_sha256")
                != ancestry.get("r12_identity_contract_review_sha256")
            or failure.get("r12_quarantine_identity_review_sha256")
                != ancestry.get("r12_quarantine_identity_review_sha256")
            or failure.get("recovery_authorization_sha256") != authorization_sha256
            or failure.get("recovery_authorization_path")
                != "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-R13-20261020T065538Z.json"
            or failure.get("recovery_validation_sha256") != validation_sha256
            or failure.get("recovery_validation_path")
                != "experiments/storage/R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-R13-20261020T0651Z.json"
            or failure.get("recovery_use_receipt_sha256") != use_sha256
            or failure.get("recovery_use_receipt_path")
                != "R6-VM-DEFERRED-RESTORE-RECOVERY-USE-16911d60bb634fb1a60ff6c5d36639be.json"
            or failure.get("recovery_use_receipt_original_boundary_hash_pending") is not True
            or failure.get("recovery_use_receipt_original_unlink_completed") is not False
            or failure.get("quarantine_file_identity")
                != validation.get("quarantine_file_identity")
            or authorization.get("recovery_validation_sha256") != validation_sha256
            or authorization.get("recovery_validation_path")
                != "experiments/storage/R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-R13-20261020T0651Z.json"
            or use.get("record_type") != "R6_VM_DEFERRED_RESTORE_RECOVERY_USE"
            or use.get("status") != "CONSUMED_FOR_SINGLE_RECOVERY_ATTEMPT"
            or use.get("authorization_sha256") != authorization_sha256
            or use.get("recovery_review_sha256")
                != failure.get("recovery_review_sha256")
            or use.get("recovery_validation_sha256") != validation_sha256
            or use.get("pending_receipt_sha256") != ancestry.get("pending_receipt_sha256")
            or use.get("boundary_full_hash_pending") is not True
            or use.get("unlink_completed") is not False
            or use.get("reclaimed_bytes_claimed") is not False
            or authorization.get("status") != "AUTHORIZED"
            or authorization.get("single_use") is not True
            or authorization.get("action") != "DELETE_EXACT_PENDING_QUARANTINED_RESTORE"
            or authorization.get("action_id") != "16911d60bb634fb1a60ff6c5d36639be"
            or authorization.get("recovery_review_sha256")
                != failure.get("recovery_review_sha256")
            or authorization.get("recovery_validation_sha256") != validation_sha256
            or authorization.get("pending_receipt_sha256")
                != ancestry.get("pending_receipt_sha256")
            or authorization.get("fresh_full_sha256") != VM_DEFERRED_RESTORE_SHA256
            or authorization.get("fresh_byte_count") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or validation.get("record_type")
                != "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION"
            or validation.get("status") != "PASS_VALIDATED"
            or validation.get("current_sha256") != VM_DEFERRED_RESTORE_SHA256
            or validation.get("current_byte_count") != VM_DEFERRED_RESTORE_LOGICAL_BYTES
            or validation.get("local_restore_payload_retired") is not False
            or failure.get("active_marker_sha256") != ancestry.get("active_marker_sha256")
            or failure.get("active_marker_committed_growth_bytes")
                != ancestry.get("committed_growth_bytes")):
        raise RuntimeError("signed R13 evidence does not establish the exact unlink with rejected immediate measurement")


def _r13_boundary_hash_evidence_passed(failure: Mapping[str, Any]) -> bool:
    """Accept only the exact signed R13 outcome, digest, and logical length."""
    return (
        failure.get("boundary_full_hash_status") == VM_R13_BOUNDARY_HASH_STATUS
        and failure.get("boundary_full_sha256") == VM_DEFERRED_RESTORE_SHA256
        and failure.get("boundary_exact_byte_count") == VM_DEFERRED_RESTORE_LOGICAL_BYTES
    )


def _assert_r14_resolution_namespace_absent(root_fd: int) -> None:
    candidates = _deferred_restore_namespace_candidates(root_fd)
    if candidates:
        raise RuntimeError("a restore payload or quarantine is present during R14 resolution")
    try:
        os.stat("restore.ipsw", dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise RuntimeError("canonical restore path is present during R14 resolution")


def _assert_r14_resolution_not_duplicate(selected: Path) -> None:
    if (selected.exists() or selected.is_symlink()
            or list(policy.REPO.glob(
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-*.json"))
            or list((policy.REPO / "experiments/storage").glob(
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-*.json"))):
        raise RuntimeError("the old PENDING transaction already has a terminal resolution")


def _assert_r14_resolution_measurement_unknown(record: Mapping[str, Any]) -> None:
    if (record.get("payload_deletion_status") != "CONFIRMED"
            or record.get("deleted_path") != str(VM_DEFERRED_RESTORE_PATH)
            or record.get("actual_unlinked_path") != str(
                VM_DEFERRED_RESTORE_PATH.parent / VM_R13_EXPECTED_QUARANTINE_NAME)
            or record.get("reclaimed_bytes_exact") is not None
            or record.get("RESTORE_RETIREMENT_RECLAIMED_BYTES") is not None
            or record.get("immediate_reclaim_measurement_status")
                != "REJECTED_APFS_CROSSCHECK_DISAGREEMENT"
            or record.get("reclaim_measurement_status")
                != "IMMEDIATE_POST_UNLINK_MEASUREMENT_REJECTED"
            or record.get("historical_reclaim_backfilled") is not False
            or record.get("later_delta_is_accepted_as_actual_reclaim") is not False
            or record.get("current_state_measurement_required") is not True
            or record.get("current_state_capacity_validated") is not False
            or record.get("future_vm_work_requires_fresh_p1") is not True
            or record.get("p2_base_provisioning_accepted") is not False
            or record.get("capacity_commitment_inactivation_attempted") is not False):
        raise RuntimeError("R14 resolution conflates unlink proof with unknown immediate reclaim")


def resolve_deferred_restore_measurement_failure(
        resolution_review_path: Path, *, receipt_path: Path | None = None) -> dict[str, Any]:
    """Close R13 PENDING additively from signed unlink evidence while keeping reclaim unknown."""
    import p2_base_orchestrator as p2

    failure_path = VM_R14_RECOVERY_MEASUREMENT_FAILURE_PATH
    failure, _raw, failure_sha, _identity = _read_signed_record_snapshot(
        failure_path, VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
    ancestry = _deferred_restore_recovery_ancestry()
    if (failure_path.resolve() != VM_R14_RECOVERY_MEASUREMENT_FAILURE_PATH.resolve()
            or failure_sha != VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256):
        raise RuntimeError("R13 post-unlink failure evidence is not the fixed reviewed artifact")
    authorization, _raw, authorization_sha, _identity = _read_signed_record_snapshot(
        policy.REPO / "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-R13-20261020T065538Z.json",
        VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION_SIGNATURE_FIELD)
    validation_path = policy.REPO / "experiments/storage/R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-R13-20261020T0651Z.json"
    validation, _raw, validation_sha, _identity = _read_signed_record_snapshot(
        validation_path, VM_DEFERRED_RESTORE_RECOVERY_VALIDATION_SIGNATURE_FIELD)
    use_path = policy.REPO / "R6-VM-DEFERRED-RESTORE-RECOVERY-USE-16911d60bb634fb1a60ff6c5d36639be.json"
    use, _raw, use_sha, _identity = _read_signed_record_snapshot(
        use_path, VM_DEFERRED_RESTORE_RECOVERY_USE_SIGNATURE_FIELD)
    _assert_deferred_restore_recovery_authorization(authorization)
    _assert_r13_post_unlink_measurement_failure(
        failure, ancestry=ancestry, authorization=authorization,
        authorization_sha256=authorization_sha, validation=validation,
        validation_sha256=validation_sha, use=use, use_sha256=use_sha)

    path_review_path = policy.REPO / str(failure.get("recovery_path_review_path", ""))
    path_review, _raw, path_review_sha, _identity = _read_json_file_snapshot(
        path_review_path, maximum_bytes=256 * 1024)
    path_reviewed_at = path_review.get("reviewed_at_unix")
    validation_created_at = validation.get("created_at_unix")
    if (path_review_sha != failure.get("recovery_path_review_sha256")
            or path_review.get("schema_version") != 1
            or path_review.get("record_type")
                != "R6_VM_DEFERRED_RESTORE_RECOVERY_PATH_REVIEW"
            or path_review.get("status") != "PASS"
            or path_review.get("decision") != VM_R13_PATH_REVIEW_DECISION
            or path_review.get("reviewer_identity")
                != "GPT-6 Luna Max — Deferred Restore Recovery Path Reviewer"
            or path_review.get("model_identity") != "GPT-6 Luna Max"
            or path_review.get("reviewed_controller_identity")
                != validation.get("current_controller_identity")
            or path_review.get("reviewed_tests_sha256") != validation.get("current_tests_sha256")
            or path_review.get("reviewed_real_payload") is not False
            or path_review.get("findings") != []):
        raise RuntimeError("historical R13 recovery-path review linkage is invalid")
    if (not isinstance(path_reviewed_at, (int, float))
            or isinstance(path_reviewed_at, bool)
            or not math.isfinite(float(path_reviewed_at))
            or path_reviewed_at <= 0 or path_reviewed_at > time.time() + 1
            or not isinstance(validation_created_at, (int, float))
            or isinstance(validation_created_at, bool)
            or path_reviewed_at >= validation_created_at):
        raise RuntimeError("historical R13 recovery-path review chronology is invalid")
    content_review_path = policy.REPO / str(failure.get("recovery_review_path", ""))
    historical_binding = _DeferredHistoricalReviewBinding(
        action_id=authorization.get("action_id"),
        review_sha256=failure.get("recovery_review_sha256"),
        validation_sha256=validation_sha,
        pending_receipt_sha256=ancestry["pending_receipt_sha256"],
        subject_sha256=VM_DEFERRED_RESTORE_SHA256,
        authorization=authorization,
        authorization_sha256=authorization_sha,
        use_receipt=use,
        use_receipt_sha256=use_sha,
        failure=failure,
        failure_sha256=failure_sha,
    )
    historical_review_fields = {
        **_deferred_recovery_review_fields(validation, validation_path, validation_sha),
        "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
    }
    content_review, content_review_sha = _deferred_bound_historical_review_json(
        content_review_path, decision=VM_R13_RECOVERY_REVIEW_DECISION,
        expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
        expected_fields=historical_review_fields, binding=historical_binding)
    current_controller_identity = _controller_identity()
    current_tests_sha256 = _file_sha256(
        policy.REPO / "experiments/storage/test_vm_capacity_domain.py")
    review, review_sha = _deferred_fresh_review_json(
        resolution_review_path, decision=VM_R14_RESOLUTION_REVIEW_DECISION,
        expected_fields=_r14_resolution_review_fields(
            failure_path, failure_sha, failure, current_controller_identity,
            current_tests_sha256))
    _require_deferred_review_postdates(review, failure.get("created_at_unix"))

    selected = Path(os.path.abspath(receipt_path or policy.REPO / (
        f"R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-R14-{_deferred_run_id()}.json")))
    relative = _deferred_repo_output_path(selected)
    if (selected.parent.resolve() != policy.REPO.resolve()
            or not selected.name.startswith(
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-R14-")
            or not selected.name.endswith(".json")):
        raise RuntimeError("R14 terminal resolution must use the exact additive repository receipt namespace")
    _assert_r14_resolution_not_duplicate(selected)

    with policy._acquire_storage_allocation_lock() as allocation_lock:
        p2_lock, _p2_lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            # The existing PENDING transaction and all R13 source records are immutable.
            pending, _raw, pending_sha, _identity = _read_signed_record_snapshot(
                VM_R13_PENDING_RECEIPT_PATH, VM_DEFERRED_RESTORE_PENDING_SIGNATURE_FIELD)
            failure_now, _raw, failure_now_sha, _identity = _read_signed_record_snapshot(
                failure_path, VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
            if (pending_sha != ancestry["pending_receipt_sha256"]
                    or pending.get("status") != "PENDING"
                    or failure_now_sha != failure_sha or failure_now != failure
                    ):
                raise RuntimeError("R13 PENDING or failure evidence changed, or resolution replay was detected")
            _assert_r14_resolution_not_duplicate(policy.REPO / relative)
            marker, marker_sha = _deferred_read_exact_active_marker()
            if (marker_sha != failure.get("active_marker_sha256")
                    or marker.get("committed_growth_bytes")
                        != failure.get("active_marker_committed_growth_bytes")):
                raise RuntimeError("active future commitment changed before additive R14 resolution")
            observation = capture_host_observation()
            mount = _volume_mount_info(observation, VM_DEFERRED_VM_VOLUME_UUID)
            if (mount.get("mount_state") != "MOUNTED_VERIFIED"
                    or mount.get("mount_point") != str(VM_DEFERRED_RESTORE_PATH.parent)
                    or mount != validation.get("vm_mount_identity")):
                raise RuntimeError("VM volume identity changed before R14 namespace reconciliation")
            root_fd = policy._open_directory_nofollow(VM_DEFERRED_RESTORE_PATH.parent)
            try:
                _verify_open_mount_fd(root_fd, mount)
                root_info = os.fstat(root_fd)
                root_identity = {
                    "device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                    "owner_uid": int(root_info.st_uid),
                    "mode": int(stat.S_IMODE(root_info.st_mode)),
                }
                if root_identity != ancestry["pending_root_identity"]:
                    raise RuntimeError("VM volume root identity changed before R14 resolution")
                _assert_r14_resolution_namespace_absent(root_fd)
            finally:
                os.close(root_fd)
            tree = _deferred_restore_tree_evidence(expect_restore=False)
            if tree.get("blockers") or tree.get("restore_paths") or tree.get("quarantine_paths"):
                raise RuntimeError("current VM tree conflicts with signed post-unlink evidence")
            marker_now, marker_now_sha = _deferred_read_exact_active_marker()
            if marker_now_sha != marker_sha or marker_now != marker:
                raise RuntimeError("active VM marker changed during R14 namespace reconciliation")
            record = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_RESOLUTION",
                "status": "RESOLVED_WITH_MEASUREMENT_CAVEAT",
                "created_at_unix": time.time(),
                "classification": "PENDING_TRANSACTION_RESOLVED_BY_VERIFIED_UNLINK_WITH_MEASUREMENT_CAVEAT",
                "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
                "payload_deletion_status": "CONFIRMED",
                "local_restore_payload_retired": True,
                "restore_payload_present": False,
                "restore_absent": True,
                "canonical_restore_absent": True,
                "quarantine_absent": True,
                "no_second_restore_candidate": True,
                "deleted_path": str(VM_DEFERRED_RESTORE_PATH),
                "actual_unlinked_path": ancestry["expected_quarantine_path"],
                "restore_sha256": VM_DEFERRED_RESTORE_SHA256,
                "deleted_file_sha256": failure["boundary_full_sha256"],
                "boundary_full_sha256": failure["boundary_full_sha256"],
                "boundary_exact_byte_count": failure["boundary_exact_byte_count"],
                "recovery_authorization_path": _deferred_repo_reference(
                    policy.REPO / "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-R13-20261020T065538Z.json")["path"],
                "recovery_authorization_sha256": authorization_sha,
                "recovery_authorization_consumed_exactly_once": True,
                "recovery_use_receipt_path": _deferred_repo_reference(use_path)["path"],
                "recovery_use_receipt_sha256": use_sha,
                "recovery_use_receipt_unlink_flag_is_pre_action": True,
                "recovery_validation_path": _deferred_repo_reference(validation_path)["path"],
                "recovery_validation_sha256": validation_sha,
                "recovery_path_review_path": _deferred_repo_reference(path_review_path)["path"],
                "recovery_path_review_sha256": path_review_sha,
                "recovery_review_path": _deferred_repo_reference(content_review_path)["path"],
                "recovery_review_sha256": content_review_sha,
                "r14_resolution_review_path": _deferred_repo_reference(
                    resolution_review_path)["path"],
                "r14_resolution_review_sha256": review_sha,
                "post_unlink_measurement_failure_path": _deferred_repo_reference(
                    failure_path)["path"],
                "post_unlink_measurement_failure_sha256": failure_sha,
                "pending_receipt_path": ancestry["pending_receipt_path"],
                "pending_receipt_sha256": pending_sha,
                "pending_receipt_remains_status": "PENDING",
                "old_pending_receipt_rewritten": False,
                "branch_retirement_receipt_path": ancestry["branch_retirement_receipt_path"],
                "branch_retirement_receipt_sha256": ancestry["branch_retirement_receipt_sha256"],
                "old_authorization_path": ancestry["old_authorization_path"],
                "old_authorization_sha256": ancestry["old_authorization_sha256"],
                "old_predelete_review_path": ancestry["old_predelete_review_path"],
                "old_predelete_review_sha256": ancestry["old_predelete_review_sha256"],
                "r11_failure_path": ancestry["r11_failure_path"],
                "r11_failure_sha256": ancestry["r11_failure_sha256"],
                "r12_identity_contract_review_path": ancestry[
                    "r12_identity_contract_review_path"],
                "r12_identity_contract_review_sha256": ancestry[
                    "r12_identity_contract_review_sha256"],
                "r12_quarantine_identity_review_path": ancestry[
                    "r12_quarantine_identity_review_path"],
                "r12_quarantine_identity_review_sha256": ancestry[
                    "r12_quarantine_identity_review_sha256"],
                "r12_controller_source_sha256": ancestry["r12_controller_source_sha256"],
                "p1_admission_path": VM_DEFERRED_P1_ADMISSION_PATH.relative_to(
                    policy.REPO).as_posix(),
                "p1_admission_sha256": failure["p1_admission_sha256"],
                "p1_verification_path": VM_DEFERRED_P1_VERIFICATION_PATH.relative_to(
                    policy.REPO).as_posix(),
                "p1_verification_sha256": failure["p1_verification_sha256"],
                "p1_restore_acquisition_historically_accepted": True,
                "p1_provenance_preserved": True,
                "p1_upstream_content_digest_validated": False,
                "restore_file_identity_destructive_boundary": failure[
                    "quarantine_file_identity"],
                "parent_directory_fsynced": True,
                "held_descriptor_confirmed_unlinked_object": True,
                "namespace_after_unlink": failure["post_attempt_namespace_entries"],
                "post_unlink_vm_inventory": tree,
                "immediate_reclaim_measurement_status": "REJECTED_APFS_CROSSCHECK_DISAGREEMENT",
                "reclaim_measurement_status": "IMMEDIATE_POST_UNLINK_MEASUREMENT_REJECTED",
                "reclaimed_bytes_exact": None,
                "RESTORE_RETIREMENT_RECLAIMED_BYTES": None,
                "historical_reclaim_backfilled": False,
                "later_delta_is_accepted_as_actual_reclaim": False,
                "current_state_measurement_required": True,
                "current_state_capacity_validated": False,
                "active_marker_sha256": marker_sha,
                "active_marker_committed_growth_bytes": marker["committed_growth_bytes"],
                "active_marker_rewritten": False,
                "capacity_commitment_inactivation_attempted": False,
                "p2_base_provisioning_accepted": False,
                "vm_guest_base_provisioned": False,
                "vm_guest_base_sealed": False,
                "p2_cleanup_success_claimed": False,
                "future_vm_work_requires_fresh_p1": True,
                "resolution_namespace_revalidated_at_unix": time.time(),
            }
            _assert_r14_resolution_measurement_unknown(record)
            return _write_signed_json_unique(
                policy.REPO / relative, record,
                VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
        finally:
            os.close(p2_lock)


def create_deferred_capacity_current_inventory(
        storage_inventory_path: Path = policy.INVENTORY_PATH,
        *, branch_retirement_path: Path,
        restore_retirement_path: Path,
        receipt_path: Path | None = None) -> dict[str, Any]:
    """Capture current shared and VM capacity without deriving it from R13 deltas."""
    selected_inventory = Path(os.path.abspath(storage_inventory_path))
    if selected_inventory != Path(os.path.abspath(policy.INVENTORY_PATH)):
        raise RuntimeError("R14 capacity inventory must bind the canonical fresh storage inventory")
    branch, _branch_raw, branch_sha, _branch_identity = _read_signed_record_snapshot(
        branch_retirement_path, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
    resolution, _resolution_raw, resolution_sha, _resolution_identity = (
        _read_signed_record_snapshot(
            restore_retirement_path, VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD))
    if (branch.get("status") != "PASS"
            or branch.get("decision") != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"
            or resolution.get("status") != "RESOLVED_WITH_MEASUREMENT_CAVEAT"
            or resolution.get("classification")
                != "PENDING_TRANSACTION_RESOLVED_BY_VERIFIED_UNLINK_WITH_MEASUREMENT_CAVEAT"
            or resolution.get("branch_retirement_receipt_sha256") != branch_sha
            or resolution.get("deleted_path") != str(VM_DEFERRED_RESTORE_PATH)
            or resolution.get("actual_unlinked_path") != str(
                VM_DEFERRED_RESTORE_PATH.parent / VM_R13_EXPECTED_QUARANTINE_NAME)
            or resolution.get("reclaimed_bytes_exact") is not None
            or resolution.get("RESTORE_RETIREMENT_RECLAIMED_BYTES") is not None
            or resolution.get("future_vm_work_requires_fresh_p1") is not True
            or resolution.get("p2_base_provisioning_accepted") is not False):
        raise RuntimeError("fresh capacity inventory requires the reviewed R14 retirement resolution")
    source_inventory, source_raw, source_sha, _identity = _read_json_file_snapshot(
        selected_inventory, maximum_bytes=32 * 1024 * 1024)
    source_created = source_inventory.get("generated_at_unix")
    now = time.time()
    if (not isinstance(source_created, (int, float)) or isinstance(source_created, bool)
            or not math.isfinite(float(source_created))
            or source_created <= 0 or source_created > now + 1
            or now - source_created > VM_R14_CURRENT_INVENTORY_MAX_AGE_SECONDS
            or source_inventory.get("schema_version") != 2):
        raise RuntimeError("canonical storage inventory is missing or stale for R14 capacity review")
    filesystem = source_inventory.get("filesystem")
    totals = source_inventory.get("totals")
    if not isinstance(filesystem, Mapping) or not isinstance(totals, Mapping):
        raise RuntimeError("canonical storage inventory omits current filesystem or project totals")
    inventory_free = filesystem.get("free_bytes")
    scoped_bytes = totals.get("fgmetal_scoped_allocated_inode_bytes")
    if (not _integer(inventory_free) or not _integer(scoped_bytes)
            or totals.get("unaccounted_unlinked_mounted_backing_images") != []):
        raise RuntimeError("canonical storage inventory has unknown free, scope, or mounted-image allocation")

    plan_raw = policy._read_file_nofollow(VM_R14_R6A_DEMAND_PLAN_PATH, maximum_bytes=2 * 1024 * 1024)
    plan_sha = _sha256(plan_raw)
    if (plan_sha != VM_R14_R6A_DEMAND_PLAN_SHA256
            or b"4,423,832,374" not in plan_raw
            or b"planning-only peak estimate" not in plan_raw):
        raise RuntimeError("frozen R6-A planning estimate changed or is no longer source-bound")

    observation = capture_host_observation()
    storage = _deferred_storage_summary(observation)
    marker, marker_sha = _deferred_read_exact_active_marker()
    capacity = policy._vm_capacity_commitment_snapshot()
    if (capacity.get("marker_sha256") != marker_sha
            or capacity.get("historical_committed_growth_bytes")
                != marker.get("committed_growth_bytes")
            or not _integer(capacity.get("committed_growth_bytes"))
            or capacity.get("historical_committed_growth_bytes") != 19_350_155_264):
        raise RuntimeError("authenticated active VM future-capacity commitment changed during inventory")
    current_free = min(storage["conservative_shared_free_bytes"], inventory_free)
    projected_peak = VM_R14_R6A_PROJECTED_DEMAND_BYTES
    active_projection = policy.projected_storage_admission(
        current_free_bytes=current_free,
        current_scoped_bytes=scoped_bytes,
        peak_additional_bytes=projected_peak)
    inactive_projected_free = current_free - projected_peak
    inactive_projected_scope = scoped_bytes + projected_peak
    inactive_projection_pass = (
        current_free >= policy.FREE_HARD_STOP
        and inactive_projected_free >= policy.FREE_MINIMUM
        and inactive_projected_scope <= policy.GATE0_PROJECT_BUDGET)
    hfs_path = Path(
        "/private/tmp/fgmetal-host-v3-adversarial-xhvisnca/build-root/workspace")
    hfs_mount_present = os.path.ismount(hfs_path)
    run_id = _deferred_run_id()
    selected_receipt = Path(os.path.abspath(receipt_path or policy.REPO / (
        f"experiments/storage/R6-VM-DEFERRED-CAPACITY-CURRENT-INVENTORY-R14-{run_id}.json")))
    _deferred_repo_output_path(selected_receipt)
    if selected_receipt.exists() or selected_receipt.is_symlink():
        raise FileExistsError("R14 current-capacity inventory receipt path already exists")
    record = {
        "schema_version": 1,
        "record_type": "R6_VM_DEFERRED_CAPACITY_CURRENT_INVENTORY",
        "status": "PASS",
        "created_at_unix": time.time(),
        "run_id": run_id,
        "branch_retirement_receipt_path": _deferred_repo_reference(
            branch_retirement_path)["path"],
        "branch_retirement_receipt_sha256": branch_sha,
        "restore_resolution_path": _deferred_repo_reference(restore_retirement_path)["path"],
        "restore_resolution_sha256": resolution_sha,
        "source_storage_inventory_path": _deferred_repo_reference(selected_inventory)["path"],
        "source_storage_inventory_sha256": source_sha,
        "source_storage_inventory_generated_at_unix": source_created,
        "source_storage_inventory_bytes": len(source_raw),
        "current_state_capacity_validated": True,
        "shared_physical_free_bytes": storage["conservative_shared_free_bytes"],
        "shared_apfs_container_free_bytes": storage["container_capacity_free_bytes"],
        "shared_data_container_free_bytes": storage["data_capacity_free_bytes"],
        "canonical_filesystem_free_bytes": inventory_free,
        "conservative_admission_free_bytes": current_free,
        "fgmetal_scoped_allocated_bytes": scoped_bytes,
        "vm_container_uuid": storage["container_uuid"],
        "vm_physical_store_uuid": storage["physical_store_uuid"],
        "vm_volume_uuid": storage["vm_volume_uuid"],
        "vm_volume_current_use_bytes": storage["vm_volume_capacity_in_use_bytes"],
        "vm_volume_quota_bytes": storage["vm_volume_quota_bytes"],
        "vm_volume_quota_headroom_bytes": storage["vm_volume_quota_headroom_bytes"],
        "safety_volume_uuid": storage["safety_volume_uuid"],
        "safety_volume_current_use_bytes": storage["safety_volume_capacity_in_use_bytes"],
        "safety_volume_reserve_bytes": storage["safety_volume_reserve_bytes"],
        "safety_volume_mount_state": storage["safety_volume_mount_state"],
        "active_marker_sha256": marker_sha,
        "historical_committed_growth_bytes": capacity["historical_committed_growth_bytes"],
        "effective_committed_growth_bytes": capacity["committed_growth_bytes"],
        "capacity_inactivation_sha256": capacity["capacity_inactivation_sha256"],
        "r6a_projected_peak_bytes": projected_peak,
        "r6a_projected_peak_basis": "FROZEN_PLAN_ESTIMATE_WITH_10_PERCENT_UNCERTAINTY_NOT_YET_OWNER_MANIFEST_ADMISSION",
        "r6a_projected_peak_source_path": _deferred_repo_reference(
            VM_R14_R6A_DEMAND_PLAN_PATH)["path"],
        "r6a_projected_peak_source_sha256": plan_sha,
        "r6a_uncertainty_included_in_projected_peak": True,
        "projected_free_with_current_commitment_bytes": active_projection["projected_free_bytes"],
        "projected_scope_after_r6a_bytes": inactive_projected_scope,
        "projected_free_if_future_commitment_inactivated_bytes": inactive_projected_free,
        "projected_admission_with_current_commitment": active_projection["admitted"],
        "projected_admission_if_future_commitment_inactivated": inactive_projection_pass,
        "gate_minimum_free_bytes": policy.FREE_MINIMUM,
        "gate_hard_stop_free_bytes": policy.FREE_HARD_STOP,
        "gate_project_scope_ceiling_bytes": policy.GATE0_PROJECT_BUDGET,
        "hfs_test_mount_path": str(hfs_path),
        "hfs_test_mount_present": hfs_mount_present,
        "p1_restore_acquisition_historically_accepted": True,
        "p2_base_provisioning_accepted": False,
        "restore_reclaimed_bytes_exact": None,
        "immediate_reclaim_measurement_status": "REJECTED_APFS_CROSSCHECK_DISAGREEMENT",
    }
    if (not inactive_projection_pass
            or record["vm_volume_quota_bytes"] != VM_DEFERRED_QUOTA_BYTES
            or record["safety_volume_reserve_bytes"] != VM_DEFERRED_SAFETY_RESERVE_BYTES
            or record["restore_reclaimed_bytes_exact"] is not None):
        raise RuntimeError("fresh current-state inventory does not support the reviewed R6-A capacity envelope")
    if (_file_sha256(selected_inventory) != source_sha
            or _file_sha256(VM_R14_R6A_DEMAND_PLAN_PATH) != plan_sha):
        raise RuntimeError("source inventory or frozen R6-A planning estimate changed during capacity capture")
    marker_now, marker_now_sha = _deferred_read_exact_active_marker()
    if marker_now_sha != marker_sha or marker_now != marker:
        raise RuntimeError("active VM capacity commitment changed during current-state inventory")
    return _write_signed_json_unique(
        selected_receipt, record, VM_DEFERRED_CAPACITY_INVENTORY_SIGNATURE_FIELD)


def _r14_future_commitment_review_fields(
        resolution_path: Path, resolution_sha256: str,
        inventory_path: Path, inventory_sha256: str,
        resolution: Mapping[str, Any], inventory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reviewed_branch_retirement_receipt_path": inventory.get(
            "branch_retirement_receipt_path"),
        "reviewed_branch_retirement_receipt_sha256": inventory.get(
            "branch_retirement_receipt_sha256"),
        "reviewed_restore_resolution_path": _deferred_repo_reference(resolution_path)["path"],
        "reviewed_restore_resolution_sha256": resolution_sha256,
        "reviewed_capacity_inventory_path": _deferred_repo_reference(inventory_path)["path"],
        "reviewed_capacity_inventory_sha256": inventory_sha256,
        "reviewed_current_state_capacity_validated": inventory.get(
            "current_state_capacity_validated"),
        "reviewed_shared_physical_free_bytes": inventory.get("shared_physical_free_bytes"),
        "reviewed_canonical_filesystem_free_bytes": inventory.get(
            "canonical_filesystem_free_bytes"),
        "reviewed_fgmetal_scoped_allocated_bytes": inventory.get(
            "fgmetal_scoped_allocated_bytes"),
        "reviewed_vm_volume_current_use_bytes": inventory.get("vm_volume_current_use_bytes"),
        "reviewed_vm_volume_quota_bytes": inventory.get("vm_volume_quota_bytes"),
        "reviewed_safety_volume_current_use_bytes": inventory.get(
            "safety_volume_current_use_bytes"),
        "reviewed_safety_volume_reserve_bytes": inventory.get("safety_volume_reserve_bytes"),
        "reviewed_active_future_commitment_bytes": inventory.get(
            "effective_committed_growth_bytes"),
        "reviewed_active_marker_sha256": inventory.get("active_marker_sha256"),
        "reviewed_projected_free_with_current_commitment_bytes": inventory.get(
            "projected_free_with_current_commitment_bytes"),
        "reviewed_r6a_projected_peak_bytes": inventory.get("r6a_projected_peak_bytes"),
        "reviewed_projected_scope_after_r6a_bytes": inventory.get(
            "projected_scope_after_r6a_bytes"),
        "reviewed_projected_free_if_inactivated_bytes": inventory.get(
            "projected_free_if_future_commitment_inactivated_bytes"),
        "reviewed_projected_admission_with_current_commitment": inventory.get(
            "projected_admission_with_current_commitment"),
        "reviewed_hfs_test_mount_path": inventory.get("hfs_test_mount_path"),
        "reviewed_hfs_test_mount_present": inventory.get("hfs_test_mount_present"),
        "reviewed_reclaimed_bytes_exact": resolution.get("reclaimed_bytes_exact"),
        "reviewed_p2_base_provisioning_accepted": resolution.get(
            "p2_base_provisioning_accepted"),
    }


def _create_deferred_capacity_inactivation_locked(
        branch_retirement_path: Path,
        restore_retirement_path: Path,
        commitment_review_path: Path,
        current_inventory_path: Path,
        *, inactivation_path: Path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
        ) -> dict[str, Any]:
    """Write the reviewed additive state that releases only future VM growth."""
    branch, _branch_raw, branch_sha, _branch_identity = _read_signed_record_snapshot(
        branch_retirement_path, VM_DEFERRED_BRANCH_RETIREMENT_SIGNATURE_FIELD)
    restore, _restore_raw, restore_sha, _restore_identity = _read_signed_record_snapshot(
        restore_retirement_path, VM_DEFERRED_RESTORE_RETIREMENT_SIGNATURE_FIELD)
    inventory, _inventory_raw, inventory_sha, _inventory_identity = _read_signed_record_snapshot(
        current_inventory_path, VM_DEFERRED_CAPACITY_INVENTORY_SIGNATURE_FIELD)
    if (branch.get("status") != "PASS"
            or branch.get("decision") != "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"
            or restore.get("status") != "RESOLVED_WITH_MEASUREMENT_CAVEAT"
            or restore.get("classification")
                != "PENDING_TRANSACTION_RESOLVED_BY_VERIFIED_UNLINK_WITH_MEASUREMENT_CAVEAT"
            or restore.get("action") != "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY"
            or restore.get("branch_retirement_receipt_sha256") != branch_sha
            or restore.get("payload_deletion_status") != "CONFIRMED"
            or restore.get("restore_absent") is not True
            or restore.get("restore_payload_present") is not False
            or restore.get("deleted_path") != str(VM_DEFERRED_RESTORE_PATH)
            or restore.get("actual_unlinked_path") != str(
                VM_DEFERRED_RESTORE_PATH.parent / VM_R13_EXPECTED_QUARANTINE_NAME)
            or restore.get("reclaimed_bytes_exact") is not None
            or restore.get("RESTORE_RETIREMENT_RECLAIMED_BYTES") is not None
            or restore.get("immediate_reclaim_measurement_status")
                != "REJECTED_APFS_CROSSCHECK_DISAGREEMENT"
            or restore.get("pending_receipt_remains_status") != "PENDING"
            or restore.get("p1_provenance_preserved") is not True
            or restore.get("future_vm_work_requires_fresh_p1") is not True
            or restore.get("p2_base_provisioning_accepted") is not False):
        raise RuntimeError("capacity inactivation requires exact branch and deletion-confirmed R14 receipts")
    inventory_created = inventory.get("created_at_unix")
    if (inventory.get("record_type") != "R6_VM_DEFERRED_CAPACITY_CURRENT_INVENTORY"
            or inventory.get("status") != "PASS"
            or inventory.get("current_state_capacity_validated") is not True
            or inventory.get("branch_retirement_receipt_path")
                != _deferred_repo_reference(branch_retirement_path)["path"]
            or inventory.get("branch_retirement_receipt_sha256") != branch_sha
            or inventory.get("restore_resolution_path")
                != _deferred_repo_reference(restore_retirement_path)["path"]
            or inventory.get("restore_resolution_sha256") != restore_sha
            or inventory.get("r6a_projected_peak_bytes") != VM_R14_R6A_PROJECTED_DEMAND_BYTES
            or inventory.get("r6a_projected_peak_source_sha256")
                != VM_R14_R6A_DEMAND_PLAN_SHA256
            or inventory.get("r6a_projected_peak_basis")
                != "FROZEN_PLAN_ESTIMATE_WITH_10_PERCENT_UNCERTAINTY_NOT_YET_OWNER_MANIFEST_ADMISSION"
            or inventory.get("effective_committed_growth_bytes")
                != restore.get("active_marker_committed_growth_bytes")
            or inventory.get("projected_admission_if_future_commitment_inactivated") is not True
            or inventory.get("restore_reclaimed_bytes_exact") is not None
            or inventory.get("p2_base_provisioning_accepted") is not False
            or inventory.get("source_storage_inventory_sha256") is None
            or inventory.get("shared_physical_free_bytes") is None
            or inventory.get("vm_volume_current_use_bytes") is None
            or inventory.get("vm_volume_quota_bytes") != VM_DEFERRED_QUOTA_BYTES
            or inventory.get("safety_volume_reserve_bytes")
                != VM_DEFERRED_SAFETY_RESERVE_BYTES):
        raise RuntimeError("R14 current-state inventory is incomplete or fails the post-inactivation capacity envelope")
    if (restore.get("post_unlink_measurement_failure_sha256")
            != VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256
            or inventory.get("active_marker_sha256")
                != restore.get("active_marker_sha256")
            or inventory.get("historical_committed_growth_bytes")
                != restore.get("active_marker_committed_growth_bytes")):
        raise RuntimeError("R14 resolution or inventory does not bind the active future commitment")
    review, review_sha = _deferred_fresh_review_json(
        commitment_review_path,
        decision=VM_R14_FUTURE_COMMITMENT_REVIEW_DECISION,
        expected_fields=_r14_future_commitment_review_fields(
            restore_retirement_path, restore_sha, current_inventory_path, inventory_sha,
            restore, inventory))
    _require_deferred_review_postdates(
        review, branch.get("created_at_unix"), restore.get("created_at_unix"),
        inventory_created)
    marker, marker_sha = _deferred_read_exact_active_marker()
    if (branch.get("active_marker_sha256") != marker_sha
            or restore.get("active_marker_sha256") != marker_sha
            or inventory.get("active_marker_sha256") != marker_sha
            or branch.get("committed_growth_bytes") != marker.get("committed_growth_bytes")
            or inventory.get("historical_committed_growth_bytes")
                != marker.get("committed_growth_bytes")
            or inventory.get("effective_committed_growth_bytes")
                != marker.get("committed_growth_bytes")):
        raise RuntimeError("current marker changed after R14 commitment review")
    branch_ref = _deferred_repo_reference(branch_retirement_path)
    restore_ref = _deferred_repo_reference(restore_retirement_path)
    inventory_ref = _deferred_repo_reference(current_inventory_path)
    review_ref = _deferred_repo_reference(commitment_review_path)
    record = {
        "schema_version": 2,
        "record_type": "R6_VM_DEFERRED_CAPACITY_INACTIVATION",
        "status": "PASS",
        "created_at_unix": time.time(),
        "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
        "session_id": marker.get("session_id"),
        "active_marker_path": str(VM_ACTIVE_MARKER_PATH),
        "active_marker_sha256": marker_sha,
        "container_uuid": marker.get("container_uuid"),
        "vm_volume_uuid": marker.get("volume_uuid"),
        "quota_bytes": marker.get("quota_bytes"),
        "safety_volume_uuid": marker.get("safety_volume_uuid"),
        "safety_reserve_bytes": marker.get("safety_reserve_bytes"),
        "historical_committed_growth_bytes": marker.get("committed_growth_bytes"),
        "effective_committed_growth_bytes": 0,
        "branch_retirement_receipt_path": branch_ref["path"],
        "branch_retirement_receipt_sha256": branch_sha,
        "restore_retirement_receipt_path": restore_ref["path"],
        "restore_retirement_receipt_sha256": restore_sha,
        "restore_reclaimed_bytes_exact": None,
        "capacity_inventory_path": inventory_ref["path"],
        "capacity_inventory_sha256": inventory_sha,
        "capacity_inventory_decision": "PASS_FRESH_CURRENT_STATE_CAPACITY",
        "future_commitment_review_path": review_ref["path"],
        "future_commitment_review_sha256": review_sha,
        "future_commitment_review_decision": review.get("decision"),
        "reviewer_identity": review.get("reviewer_identity"),
        "future_vm_reacquisition_required": True,
        "p2_base_provisioning_accepted": False,
        "vm_guest_base_provisioned": False,
        "active_marker_rewritten": False,
        "quota_changed": False,
        "safety_reserve_released": False,
    }
    return _write_signed_json_unique(
        inactivation_path, record, VM_DEFERRED_CAPACITY_INACTIVATION_SIGNATURE_FIELD)


def create_deferred_capacity_inactivation(
        branch_retirement_path: Path,
        restore_retirement_path: Path,
        commitment_review_path: Path,
        current_inventory_path: Path,
        *, inactivation_path: Path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
        ) -> dict[str, Any]:
    """Commit the reviewed release before a separate post-release parking review."""
    selected = Path(os.path.abspath(inactivation_path))
    if selected != Path(os.path.abspath(policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION)):
        raise RuntimeError("deferred capacity inactivation must use the fixed authenticated state path")
    import p2_base_orchestrator as p2
    with policy._acquire_storage_allocation_lock():
        p2_lock, _lock_evidence = p2._acquire_p2_cleanup_lock()
        try:
            record = _create_deferred_capacity_inactivation_locked(
                branch_retirement_path, restore_retirement_path,
                commitment_review_path, current_inventory_path,
                inactivation_path=selected)
            snapshot = policy._vm_capacity_commitment_snapshot()
            if (snapshot.get("committed_growth_bytes") != 0
                    or snapshot.get("historical_committed_growth_bytes")
                        != record.get("historical_committed_growth_bytes")
                    or snapshot.get("capacity_inactivation_sha256")
                        != _sha256(policy._canonical_pretty_json_bytes(record))):
                raise RuntimeError("signed deferred capacity inactivation failed policy read-back")
            return {**record,
                    "effective_capacity_snapshot": snapshot}
        finally:
            os.close(p2_lock)


def _named_volume(observation: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    container = observation.get("apfs_container")
    volumes = container.get("Volumes") if isinstance(container, Mapping) else None
    if not isinstance(volumes, list):
        raise ValueError("complete APFS volume enumeration is unavailable")
    matches = [row for row in volumes if isinstance(row, Mapping) and row.get("Name") == name]
    if len(matches) > 1:
        raise ValueError(f"APFS volume name {name!r} is ambiguous")
    return matches[0] if matches else None


def _delete_exact_probe_volume(
        volume_uuid: str,
        expected_mount_identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    before = capture_host_observation()
    row = _volume_row(before, volume_uuid)
    if (row.get("Name") != VM_QUOTA_PROBE_VOLUME_NAME
            or row.get("CapacityQuota") != VM_QUOTA_PROBE_QUOTA_BYTES
            or row.get("CapacityReserve") != 0):
        raise RuntimeError("refusing to delete a volume that is not the exact bounded APFS quota probe")
    blockers = _container_blockers(
        before, expected_vm_volume_uuid=volume_uuid,
        expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
        expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
    if blockers:
        raise RuntimeError("refusing to delete an APFS volume in an unexpected container state: "
                           + "; ".join(blockers))
    if expected_mount_identity is not None:
        _revalidate_volume_mount(before, volume_uuid, expected_mount_identity)
    device = row.get("DeviceIdentifier")
    if not isinstance(device, str) or not re.fullmatch(r"disk[0-9]+s[0-9]+", device):
        raise RuntimeError("quota-probe delete target has an invalid device identifier")
    completed = subprocess.run(
        [_diskutil(), "apfs", "deleteVolume", f"/dev/{device}"],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120.0, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("exact quota-probe volume deletion failed: " + completed.stderr[-2000:])
    after = capture_host_observation()
    if _named_volume(after, VM_QUOTA_PROBE_VOLUME_NAME) is not None:
        raise RuntimeError("quota-probe volume remains after deleteVolume returned success")
    if any(isinstance(item, Mapping) and item.get("APFSVolumeUUID") == volume_uuid
           for item in after["apfs_container"].get("Volumes", [])):
        raise RuntimeError("quota-probe volume UUID remains after deleteVolume returned success")
    mount_records = _mounted_filesystem_table()
    expected_mount_point = f"/Volumes/{VM_QUOTA_PROBE_VOLUME_NAME}"
    if any(record.get("source") == f"/dev/{device}"
           or record.get("mount_point") == expected_mount_point
           for record in mount_records):
        raise RuntimeError("quota-probe device or mount point remains active after volume deletion")
    blockers = _container_blockers(after, expected_vm_volume_uuid=None)
    if blockers:
        raise RuntimeError("APFS state is not clean after quota-probe deletion: " + "; ".join(blockers))
    return {
        "before_observation": before,
        "after_observation": after,
        "delete_stdout_sha256": _sha256(completed.stdout.encode()),
        "delete_stderr_sha256": _sha256(completed.stderr.encode()),
    }


def _run_apfs_quota_enforcement_probe_impl(
        precreation_receipt_path: Path = VM_PRECREATION_ADMISSION_PATH,
        *, receipt_path: Path = VM_QUOTA_PROBE_RECEIPT_PATH) -> dict[str, Any]:
    """Create, fill, empirically exhaust, and delete a 512 MiB disposable APFS volume."""
    admission = _read_signed_record(precreation_receipt_path, "vm_admission_hmac_sha256")
    if (admission.get("decision") != "PASS"
            or admission.get("authorization") != "RUN_QUOTA_PROBE_ONLY"
            or admission.get("mode") != VM_MODE):
        raise RuntimeError("quota probe requires a fresh signed probe-only PASS")
    age = time.time() - float(admission.get("created_at_unix", 0))
    if age < 0 or age > VM_MAX_OBSERVATION_AGE_SECONDS:
        raise RuntimeError("quota-probe admission is stale")
    if admission.get("controller_identity") != _controller_identity():
        raise RuntimeError("quota-probe admission does not bind current controller bytes")
    g0c_identity = verify_fresh_g0c_review()
    if admission.get("g0c_identity") != g0c_identity:
        raise RuntimeError("quota-probe admission does not bind the current G0C review")
    if admission.get("quota_probe_evaluation", {}).get("admitted") is not True:
        raise RuntimeError("quota-probe admission has no passing bounded-write projection")

    session_id = uuid.uuid4().hex
    plan = {
        "mode": VM_MODE,
        "operation": "BOUNDED_APFS_QUOTA_ENFORCEMENT_PROBE",
        "quota_bytes": VM_QUOTA_PROBE_QUOTA_BYTES,
        "maximum_file_write_bytes": VM_QUOTA_PROBE_MAX_WRITE_BYTES,
        "reserve_bytes": 0,
    }
    plan_hash = _sha256(policy._canonical_pretty_json_bytes(plan))
    volume_uuid: str | None = None
    marker_created = False
    file_fd: int | None = None
    root_fd: int | None = None
    write_bytes = 0
    quota_denied = False
    denial_errno: int | None = None
    peak_usage: int | None = None
    peak_allocated: int | None = None
    start_observation: Mapping[str, Any] | None = None
    create_stdout_hash: str | None = None
    failure: str | None = None
    delete_evidence: Mapping[str, Any] | None = None
    final_observation: Mapping[str, Any] | None = None
    with policy._acquire_storage_allocation_lock():
        start_observation = capture_host_observation()
        blockers = _container_blockers(start_observation, expected_vm_volume_uuid=None)
        if blockers:
            raise RuntimeError("quota-probe target preflight failed: " + "; ".join(blockers))
        try:
            live_growth_measurement = measure_concurrent_growth_bound()
            concurrent_growth_bound = live_growth_measurement.get("observed_max_interval_growth_bytes")
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            live_growth_measurement = {"status": "UNKNOWN", "failure": str(error),
                                       "authority": "DIAGNOSTIC_ONLY"}
            concurrent_growth_bound = None
        fresh_probe_admission = calculate_vm_phase_admission(
            mode=VM_MODE, container_uuid=EXPECTED_CONTAINER_UUID,
            free_bytes=conservative_container_free_bytes(start_observation),
            quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES, quota_used_bytes=0,
            growth_bytes=VM_QUOTA_PROBE_QUOTA_BYTES, uncertainty_bytes=0,
            concurrent_growth_bytes=concurrent_growth_bound,
            expected_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES)
        if not fresh_probe_admission["admitted"]:
            raise RuntimeError("fresh bounded quota-probe admission failed: "
                               + "; ".join(fresh_probe_admission["blockers"]))
        if (time.time() - float(admission.get("created_at_unix", 0)) > VM_MAX_OBSERVATION_AGE_SECONDS
                or verify_fresh_g0c_review() != g0c_identity):
            raise RuntimeError("quota-probe authorization or G0C review changed before volume creation")
        marker = write_active_session_marker(
            session_id=session_id, phase_id="QUOTA_PROBE",
            container_uuid=EXPECTED_CONTAINER_UUID, volume_uuid=None,
            committed_growth_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            plan_sha256=plan_hash, quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
        marker_created = True
        container_reference = start_observation["apfs_container"].get("ContainerReference")
        completed = subprocess.run(
            [_diskutil(), "apfs", "addVolume", str(container_reference), VM_FILESYSTEM,
             VM_QUOTA_PROBE_VOLUME_NAME, "-quota", "512MiB"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120.0, text=True,
        )
        create_stdout_hash = _sha256(completed.stdout.encode())
        if completed.returncode != 0:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="QUOTA_PROBE_CREATE_UNKNOWN")
            raise RuntimeError("quota-probe APFS volume creation failed; marker retained: "
                               + completed.stderr[-2000:])
        created = capture_host_observation()
        probe_row = _named_volume(created, VM_QUOTA_PROBE_VOLUME_NAME)
        if not isinstance(probe_row, Mapping):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="QUOTA_PROBE_IDENTITY_UNKNOWN")
            raise RuntimeError("new quota-probe volume is absent or ambiguous; marker retained")
        volume_uuid = probe_row.get("APFSVolumeUUID")
        if not isinstance(volume_uuid, str):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="QUOTA_PROBE_UUID_UNKNOWN")
            raise RuntimeError("quota-probe volume UUID is unknown; marker retained")
        blockers = _container_blockers(
            created, expected_vm_volume_uuid=volume_uuid,
            expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
        if blockers:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="QUOTA_PROBE_READBACK_FAILED",
                                         volume_uuid=volume_uuid)
            raise RuntimeError("quota-probe volume failed identity/quota read-back; marker retained: "
                               + "; ".join(blockers))
        mount = _volume_mount_info(created, volume_uuid)
        update_active_session_marker(session_id, phase_id="QUOTA_PROBE_WRITE",
                                     volume_uuid=volume_uuid,
                                     committed_growth_bytes=VM_QUOTA_PROBE_QUOTA_BYTES)
        before_open = capture_host_observation()
        blockers = _container_blockers(
            before_open, expected_vm_volume_uuid=volume_uuid,
            expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
        if blockers:
            raise RuntimeError("quota-probe identity changed before payload open: " + "; ".join(blockers))
        _revalidate_volume_mount(before_open, volume_uuid, mount)
        root_fd = policy._open_directory_nofollow(Path(mount["mount_point"]))
        _verify_open_mount_fd(root_fd, mount)
        try:
            file_fd = _open_verified_probe_payload(root_fd, volume_uuid, mount)
            file_info = os.fstat(file_fd)
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
                raise RuntimeError("quota-probe target is not a new regular file")
            write_started = time.monotonic()
            while write_bytes < VM_QUOTA_PROBE_MAX_WRITE_BYTES:
                current = capture_host_observation()
                blockers = _container_blockers(
                    current, expected_vm_volume_uuid=volume_uuid,
                    expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
                    expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
                if blockers:
                    raise RuntimeError("quota-probe live identity/accounting changed: " + "; ".join(blockers))
                _revalidate_volume_mount(current, volume_uuid, mount)
                _verify_open_mount_fd(root_fd, mount)
                _verify_open_mount_fd(file_fd, mount)
                free = conservative_container_free_bytes(current)
                if free <= VM_OPERATIONAL_WARNING_FREE:
                    raise RuntimeError("shared APFS free reached the 8 GiB early-stop threshold during quota probe")
                row = _volume_row(current, volume_uuid)
                used = row.get("CapacityInUse")
                if not _integer(used) or used > VM_QUOTA_PROBE_QUOTA_BYTES:
                    raise RuntimeError("APFS quota probe exceeded or lost quota accounting")
                peak_usage = max(peak_usage or 0, used)
                piece = min(16 * MIB, VM_QUOTA_PROBE_MAX_WRITE_BYTES - write_bytes)
                data = os.urandom(piece)
                offset = 0
                try:
                    while offset < len(data):
                        count = os.write(file_fd, data[offset:])
                        if count <= 0:
                            raise OSError(errno.EIO, "quota-probe short write")
                        offset += count
                        write_bytes += count
                except OSError as error:
                    if error.errno not in {errno.ENOSPC, errno.EFBIG}:
                        raise
                    quota_denied = True
                    denial_errno = error.errno
                    break
                os.fsync(file_fd)
                file_info = os.fstat(file_fd)
                peak_allocated = max(peak_allocated or 0, getattr(file_info, "st_blocks", 0) * 512)
                after_write = capture_host_observation()
                blockers = _container_blockers(
                    after_write, expected_vm_volume_uuid=volume_uuid,
                    expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
                    expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
                if blockers:
                    raise RuntimeError("APFS quota-probe read-back failed after write: " + "; ".join(blockers))
                _revalidate_volume_mount(after_write, volume_uuid, mount)
                _verify_open_mount_fd(root_fd, mount)
                _verify_open_mount_fd(file_fd, mount)
                row = _volume_row(after_write, volume_uuid)
                used = row.get("CapacityInUse")
                if not _integer(used) or used > VM_QUOTA_PROBE_QUOTA_BYTES:
                    raise RuntimeError("APFS reported usage above the probe volume quota")
                peak_usage = max(peak_usage or 0, used)
                if conservative_container_free_bytes(after_write) <= VM_OPERATIONAL_WARNING_FREE:
                    raise RuntimeError("shared APFS free crossed the 8 GiB early-stop threshold after probe write")
            os.fsync(file_fd)
            elapsed = time.monotonic() - write_started
        finally:
            if file_fd is not None:
                os.close(file_fd)
                file_fd = None
            if root_fd is not None:
                os.close(root_fd)
                root_fd = None
        if (not quota_denied or write_bytes < VM_QUOTA_PROBE_QUOTA_BYTES - 64 * MIB
                or (peak_usage is not None and peak_usage > VM_QUOTA_PROBE_QUOTA_BYTES)):
            failure = "APFS did not empirically deny bounded writes at the configured quota"
        else:
            failure = None
        update_active_session_marker(session_id,
                                     state="FAILED" if failure else "ACTIVE",
                                     phase_id="QUOTA_PROBE_DELETE")
        before_cleanup = capture_host_observation()
        blockers = _container_blockers(
            before_cleanup, expected_vm_volume_uuid=volume_uuid,
            expected_vm_quota_bytes=VM_QUOTA_PROBE_QUOTA_BYTES,
            expected_vm_volume_name=VM_QUOTA_PROBE_VOLUME_NAME)
        if blockers:
            raise RuntimeError("quota-probe identity changed after the empirical write test: "
                               + "; ".join(blockers))
        _revalidate_volume_mount(before_cleanup, volume_uuid, mount)
        delete_evidence = _delete_exact_probe_volume(volume_uuid, mount)
        final_observation = delete_evidence["after_observation"]
        before_delete_free = conservative_container_free_bytes(delete_evidence["before_observation"])
        after_delete_free = conservative_container_free_bytes(final_observation)
        reclaimed = after_delete_free - before_delete_free
        minimum_reclaim = max(64 * MIB, (peak_allocated or 0) - 128 * MIB)
        if reclaimed < minimum_reclaim:
            failure = (failure + "; " if failure else "") + (
                f"quota-probe deletion did not verify reclaimed container allocation: "
                f"{reclaimed} < {minimum_reclaim} bytes")
        proof_record = _seal({
            "schema_version": 1,
            "session_id": session_id,
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
            "probe_operation": "VOLUME_DELETED_AND_WRITER_CLOSED",
            "marker_sha256": _file_sha256(VM_ACTIVE_MARKER_PATH),
        }, "vm_stop_proof_hmac_sha256")
        remove_active_session_marker(session_id, proof_record)
    record = {
        "schema_version": 1,
        "mode": VM_MODE,
        "status": "PASS" if failure is None else "FAIL",
        "session_id": session_id,
        "probe_volume_name": VM_QUOTA_PROBE_VOLUME_NAME,
        "probe_volume_uuid": volume_uuid,
        "probe_mount_state": mount["mount_state"],
        "probe_mount_identity": mount["mount_identity"],
        "probe_quota_bytes": VM_QUOTA_PROBE_QUOTA_BYTES,
        "probe_reserve_bytes": 0,
        "maximum_write_bytes": VM_QUOTA_PROBE_MAX_WRITE_BYTES,
        "write_bytes_before_denial": write_bytes,
        "denial_observed": quota_denied,
        "denial_errno": denial_errno,
        "peak_apfs_capacity_in_use_bytes": peak_usage,
        "peak_file_allocated_bytes": peak_allocated,
        "write_duration_seconds": elapsed,
        "probe_volume_deleted": True,
        "pre_delete_free_bytes": before_delete_free,
        "post_delete_free_bytes": after_delete_free,
        "measured_reclaimed_container_bytes": reclaimed,
        "minimum_reclaim_bytes": minimum_reclaim,
        "controller_identity": _controller_identity(),
        "g0c_identity": g0c_identity,
        "precreation_admission_sha256": _file_sha256(precreation_receipt_path),
        "create_stdout_sha256": create_stdout_hash,
        "delete_stdout_sha256": delete_evidence["delete_stdout_sha256"],
        "initial_observation": start_observation,
            "concurrent_growth_measurement": live_growth_measurement,
            "concurrent_growth_bound_bytes": concurrent_growth_bound,
            "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
        "final_observation": final_observation,
        "failure": failure,
    }
    return _write_signed_json(receipt_path, record, "vm_quota_probe_hmac_sha256")


def run_apfs_quota_enforcement_probe(
        precreation_receipt_path: Path = VM_PRECREATION_ADMISSION_PATH,
        *, receipt_path: Path = VM_QUOTA_PROBE_RECEIPT_PATH) -> dict[str, Any]:
    """Persist a blocking signed failure record if the bounded probe aborts."""
    try:
        return _run_apfs_quota_enforcement_probe_impl(
            precreation_receipt_path, receipt_path=receipt_path)
    except BaseException as error:
        marker: Mapping[str, Any] | None = None
        marker_digest: str | None = None
        try:
            marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
            marker_digest = _file_sha256(VM_ACTIVE_MARKER_PATH)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            pass
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "FAIL",
            "session_id": marker.get("session_id") if isinstance(marker, Mapping) else None,
            "probe_volume_name": VM_QUOTA_PROBE_VOLUME_NAME,
            "probe_volume_uuid": marker.get("volume_uuid") if isinstance(marker, Mapping) else None,
            "probe_quota_bytes": VM_QUOTA_PROBE_QUOTA_BYTES,
            "probe_reserve_bytes": 0,
            "probe_volume_deleted": False,
            "active_marker_sha256": marker_digest,
            "active_marker_state": marker.get("state") if isinstance(marker, Mapping) else "UNKNOWN",
            "active_marker_phase": marker.get("phase_id") if isinstance(marker, Mapping) else "UNKNOWN",
            "controller_identity": _controller_identity(),
            "failure": str(error),
            "blockers": ["quota probe aborted; no VM-volume creation is authorized"],
        }
        return _write_signed_json(receipt_path, record, "vm_quota_probe_hmac_sha256")


def write_active_session_marker(*, session_id: str, phase_id: str,
                                container_uuid: str, volume_uuid: str | None,
                                committed_growth_bytes: int, plan_sha256: str,
                                quota_bytes: int = VM_REVIEWED_QUOTA_MAX_BYTES,
                                volume_name: str | None = None,
                                safety_volume_uuid: str | None = None,
                                safety_volume_in_use_bytes: int | None = None,
                                safety_container_uuid: str | None = None,
                                safety_physical_store_uuid: str | None = None,
                                pre_reserve_shared_free_bytes: int | None = None,
                                unreserved_free_ceiling_bytes: int | None = None,
                                state: str = "ACTIVE") -> dict[str, Any]:
    """Install a sticky marker while holding the normal shared allocation lock."""
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise ValueError("VM session ID must be 32 lowercase hexadecimal characters")
    if state not in {"ACTIVE", "FAILED", "UNKNOWN"}:
        raise ValueError("VM active marker state is not a blocking state")
    if container_uuid != EXPECTED_CONTAINER_UUID:
        raise ValueError("VM marker is not bound to the authorized APFS container")
    if volume_uuid is not None and not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid):
        raise ValueError("VM marker volume UUID is malformed")
    if not _integer(quota_bytes, minimum=1) or quota_bytes > VM_REVIEWED_QUOTA_MAX_BYTES:
        raise ValueError("VM marker quota is outside the reviewed maximum")
    if safety_volume_uuid is not None and not re.fullmatch(r"[0-9A-Fa-f-]{36}", safety_volume_uuid):
        raise ValueError("VM marker safety-volume UUID is malformed")
    if safety_volume_in_use_bytes is not None and not _integer(safety_volume_in_use_bytes):
        raise ValueError("VM marker safety-volume empty-accounting baseline is invalid")
    if (safety_volume_uuid is None) != (safety_volume_in_use_bytes is None):
        raise ValueError("VM marker safety-volume UUID and accounting baseline must be bound together")
    if safety_volume_uuid is not None and (
            safety_container_uuid != EXPECTED_CONTAINER_UUID
            or safety_physical_store_uuid != EXPECTED_PHYSICAL_STORE_UUID):
        raise ValueError("VM marker safety-volume container/store identities are not the authorized target")
    if ((pre_reserve_shared_free_bytes is None) != (unreserved_free_ceiling_bytes is None)
            or (pre_reserve_shared_free_bytes is not None
                and (not _integer(pre_reserve_shared_free_bytes,
                                  minimum=VM_PROTECTED_CAPACITY_RESERVE_BYTES + 1)
                     or not _integer(unreserved_free_ceiling_bytes)
                     or unreserved_free_ceiling_bytes
                     != pre_reserve_shared_free_bytes - VM_PROTECTED_CAPACITY_RESERVE_BYTES))):
        raise ValueError("VM marker pre-reserve free capacity and unreserved ceiling are inconsistent")
    if not _integer(committed_growth_bytes) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValueError("VM marker growth or plan identity is malformed")
    root_fd = policy._open_directory_nofollow(VM_ACTIVE_MARKER_PATH.parent,
                                               create=True, strict_directory_sync=True)
    try:
        try:
            os.stat(VM_ACTIVE_MARKER_PATH.name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("an active or unresolved VM capacity-domain marker already exists")
        now = time.time()
        record = _seal({
            "schema_version": 1,
            "session_id": session_id,
            "state": state,
            "phase_id": phase_id,
            "container_uuid": container_uuid,
            "volume_uuid": volume_uuid,
            "quota_bytes": quota_bytes,
            "volume_name": volume_name or VM_VOLUME_NAME,
            "safety_volume_uuid": safety_volume_uuid,
            "safety_volume_in_use_bytes": safety_volume_in_use_bytes,
            "safety_container_uuid": safety_container_uuid,
            "safety_physical_store_uuid": safety_physical_store_uuid,
            "safety_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES if safety_volume_uuid is not None else 0,
            "pre_reserve_shared_free_bytes": pre_reserve_shared_free_bytes,
            "unreserved_free_ceiling_bytes": unreserved_free_ceiling_bytes,
            "committed_growth_bytes": committed_growth_bytes,
            "plan_sha256": plan_sha256,
            "created_at_unix": now,
            "updated_at_unix": now,
        }, "vm_session_hmac_sha256")
        policy._write_json(VM_ACTIVE_MARKER_PATH, record, strict_directory_sync=True)
        return record
    finally:
        os.close(root_fd)


def _validate_p1_freshness_renewal_marker_update(
        current: Mapping[str, Any], proposed: Mapping[str, Any], *,
        session_id: str) -> dict[str, Any]:
    """Validate one immutable READY ticket, terminal claim, or start-evidence bind."""
    if (current.get("state") != "ACTIVE" or current.get("phase_id") != "P1"
            or current.get("session_id") != session_id
            or not isinstance(current.get("p1_admission_path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("p1_admission_sha256")))):
        raise RuntimeError("P1 freshness renewal requires the exact ACTIVE/P1 marker")
    required = {
        "path", "sha256", "sequence", "predecessor_path", "predecessor_sha256", "use_state",
        "attempt_id", "claimed_at_unix", "body_start_evidence",
    }
    if (set(proposed) != required
            or not isinstance(proposed.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(proposed.get("sha256")))
            or not _integer(proposed.get("sequence"), minimum=1)
            or ((proposed.get("predecessor_path") is None)
                != (proposed.get("predecessor_sha256") is None))
            or (proposed.get("predecessor_path") is not None
                and not isinstance(proposed.get("predecessor_path"), str))
            or (proposed.get("predecessor_sha256") is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(proposed.get("predecessor_sha256"))))):
        raise RuntimeError("P1 freshness renewal marker binding is malformed")
    if proposed.get("predecessor_path") is not None:
        predecessor_path, predecessor_path_text = _repo_artifact_path(proposed["predecessor_path"])
        if predecessor_path_text != proposed["predecessor_path"]:
            raise RuntimeError("P1 freshness renewal predecessor path is not canonical")
    elif proposed.get("sequence") != 1:
        raise RuntimeError("subsequent P1 freshness renewal must bind its predecessor path and hash")
    proposed_path, proposed_path_text = _repo_artifact_path(proposed["path"])
    if proposed_path_text != proposed["path"]:
        raise RuntimeError("P1 freshness renewal marker path is not canonical")
    receipt, _raw, receipt_sha, _identity = _read_signed_record_snapshot(
        proposed_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD)
    if (receipt_sha != proposed["sha256"]
            or receipt.get("mode") != VM_MODE
            or receipt.get("record_type") != "R6_VM_P1_FRESHNESS_RENEWAL"
            or receipt.get("receipt_path") != proposed_path_text
            or receipt.get("sequence") != proposed["sequence"]
            or receipt.get("predecessor_path") != proposed["predecessor_path"]
            or receipt.get("predecessor_sha256") != proposed["predecessor_sha256"]
            or receipt.get("session_id") != session_id
            or receipt.get("original_p1") != {
                "path": current.get("p1_admission_path"),
                "sha256": current.get("p1_admission_sha256")}
            or receipt.get("authority_effect") != "REVALIDATES_EXECUTION_TIME_FRESHNESS_ONLY"
            or receipt.get("retained_authority") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or any(receipt.get(field) is not False for field in (
                "p1_authorized", "restore_download_authorized", "p2_authorized",
                "vm_start_authorized"))):
        raise RuntimeError("P1 freshness renewal marker does not bind a valid inert receipt")
    current_binding = current.get("p1_freshness_renewal")
    if current_binding is not None and not isinstance(current_binding, Mapping):
        raise RuntimeError("current P1 freshness renewal marker binding is malformed")
    if proposed.get("use_state") == "READY":
        if (proposed.get("attempt_id") is not None or proposed.get("claimed_at_unix") is not None
                or proposed.get("body_start_evidence") is not None):
            raise RuntimeError("new P1 freshness renewal must start in unused READY state")
        if current_binding is None:
            if (proposed.get("sequence") != 1
                    or proposed.get("predecessor_path") is not None
                    or proposed.get("predecessor_sha256") is not None):
                raise RuntimeError("first P1 freshness renewal must have sequence one and no predecessor")
        else:
            prior_path = current_binding.get("path")
            prior_sha = current_binding.get("sha256")
            prior_sequence = current_binding.get("sequence")
            if (not isinstance(prior_sha, str) or not _integer(prior_sequence, minimum=1)
                    or proposed.get("sequence") != prior_sequence + 1
                    or proposed.get("predecessor_path") != prior_path
                    or proposed.get("predecessor_sha256") != prior_sha):
                raise RuntimeError("P1 freshness renewal sequence or predecessor does not advance the marker")
            if current_binding.get("use_state") == "READY":
                prior_path, prior_path_text = _repo_artifact_path(current_binding.get("path", ""))
                if prior_path_text != current_binding.get("path"):
                    raise RuntimeError("prior P1 freshness renewal path is not canonical")
                prior, _prior_raw, prior_sha_read, _prior_identity = _read_signed_record_snapshot(
                    prior_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD)
                if (prior_sha_read != prior_sha
                        or prior.get("sequence") != current_binding.get("sequence")
                        or prior.get("predecessor_path") != current_binding.get("predecessor_path")
                        or prior.get("predecessor_sha256") != current_binding.get("predecessor_sha256")
                        or prior.get("original_p1") != {
                            "path": current.get("p1_admission_path"),
                            "sha256": current.get("p1_admission_sha256")}):
                    raise RuntimeError("prior P1 freshness renewal changed before supersession")
                prior_fresh = prior.get("fresh_metadata")
                prior_captured_at = (prior_fresh.get("captured_at_unix")
                                     if isinstance(prior_fresh, Mapping) else None)
                _require_nonfuture_timestamp(prior_captured_at,
                                             label="prior P1 freshness renewal")
                prior_age = time.time() - float(prior_captured_at)
                if not math.isfinite(prior_age) or prior_age < 0:
                    raise RuntimeError("prior P1 freshness renewal timestamp is invalid")
                if prior_age <= VM_MAX_RESTORE_METADATA_AGE_SECONDS:
                    raise RuntimeError("an unconsumed, unexpired P1 freshness renewal cannot be replaced")
    elif (proposed.get("use_state") == "CONSUMED"
          and (current_binding is None or current_binding.get("use_state") == "READY")):
        attempt_id = proposed.get("attempt_id")
        claimed_at = proposed.get("claimed_at_unix")
        if (current_binding is None
                or current_binding.get("path") != proposed.get("path")
                or current_binding.get("sha256") != proposed.get("sha256")
                or current_binding.get("sequence") != proposed.get("sequence")
                or current_binding.get("predecessor_path") != proposed.get("predecessor_path")
                or current_binding.get("predecessor_sha256") != proposed.get("predecessor_sha256")
                or current_binding.get("use_state") != "READY"
                or current_binding.get("attempt_id") is not None
                or not isinstance(attempt_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
                or not isinstance(claimed_at, (int, float)) or isinstance(claimed_at, bool)
                or not math.isfinite(float(claimed_at))
                or proposed.get("body_start_evidence") is not None):
            raise RuntimeError("P1 freshness renewal is not the current unconsumed marker-bound ticket")
        _require_nonfuture_timestamp(claimed_at, label="P1 freshness renewal claim")
    elif proposed.get("use_state") == "CONSUMED":
        if (current_binding is None
                or current_binding.get("use_state") != "CONSUMED"
                or current_binding.get("path") != proposed.get("path")
                or current_binding.get("sha256") != proposed.get("sha256")
                or current_binding.get("sequence") != proposed.get("sequence")
                or current_binding.get("predecessor_path") != proposed.get("predecessor_path")
                or current_binding.get("attempt_id") != proposed.get("attempt_id")
                or current_binding.get("claimed_at_unix") != proposed.get("claimed_at_unix")
                or current_binding.get("body_start_evidence") is not None):
            raise RuntimeError("P1 start evidence must bind the same terminally consumed attempt")
        evidence = proposed.get("body_start_evidence")
        if (not isinstance(evidence, Mapping)
                or set(evidence) != {"path", "sha256"}
                or not isinstance(evidence.get("path"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256")))):
            raise RuntimeError("P1 body-start evidence binding is malformed")
        evidence_path, evidence_path_text = _repo_artifact_path(evidence["path"])
        if evidence_path_text != evidence["path"]:
            raise RuntimeError("P1 body-start evidence path is not canonical")
        start_record, _start_raw, start_sha, _start_identity = _read_signed_record_snapshot(
            evidence_path, VM_P1_FRESHNESS_START_SIGNATURE_FIELD)
        if (start_sha != evidence["sha256"]
                or start_record.get("record_type") != "R6_VM_P1_FRESHNESS_START"
                or start_record.get("receipt_path") != evidence_path_text
                or start_record.get("renewal_sha256") != proposed.get("sha256")
                or start_record.get("attempt_id") != proposed.get("attempt_id")):
            raise RuntimeError("P1 body-start evidence does not authenticate the exact renewal attempt")
    else:
        raise RuntimeError("P1 freshness renewal marker use state is invalid")
    return dict(proposed)


def update_active_session_marker(session_id: str, *, state: str | None = None,
                                 phase_id: str | None = None,
                                 volume_uuid: str | None = None,
                                 quota_bytes: int | None = None,
                                 volume_name: str | None = None,
                                 safety_volume_uuid: str | None = None,
                                 safety_volume_in_use_bytes: int | None = None,
                                 safety_container_uuid: str | None = None,
                                 safety_physical_store_uuid: str | None = None,
                                 safety_reserve_released: bool | None = None,
                                 committed_growth_bytes: int | None = None,
                                 p1_admission_path: Path | None = None,
                                 p1_admission_sha256: str | None = None,
                                 p1_freshness_renewal: Mapping[str, Any] | None = None,
                                 expected_marker_sha256: str | None = None) -> dict[str, Any]:
    current, _marker_raw, current_marker_sha256, _marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (expected_marker_sha256 is not None
            and current_marker_sha256 != expected_marker_sha256):
        raise RuntimeError("VM active marker changed since the P1 admission boundary was checked")
    if current.get("session_id") != session_id:
        raise RuntimeError("VM active marker belongs to a different session")
    if current.get("state") in {"FAILED", "UNKNOWN"} and state == "ACTIVE":
        raise RuntimeError("FAILED/UNKNOWN VM marker state is sticky and cannot return to ACTIVE")
    if current.get("state") in {"FAILED", "UNKNOWN"} and state not in (None, current.get("state")):
        raise RuntimeError("FAILED/UNKNOWN VM marker state requires the explicit recovery protocol")
    updated = {key: value for key, value in current.items() if key != "vm_session_hmac_sha256"}
    if state is not None:
        if state not in {"ACTIVE", "FAILED", "UNKNOWN"}:
            raise ValueError("VM active marker state is not a blocking state")
        updated["state"] = state
    if phase_id is not None:
        current_phase = current.get("phase_id")
        current_phase_match = re.match(r"^P([1-5])(?:_|$)", current_phase) if isinstance(current_phase, str) else None
        next_phase_match = re.match(r"^P([1-5])(?:_|$)", phase_id)
        if (current_phase_match is not None and next_phase_match is not None
                and int(next_phase_match.group(1)) < int(current_phase_match.group(1))):
            raise RuntimeError("VM marker phase cannot move backwards")
        if (next_phase_match is not None and int(next_phase_match.group(1)) >= 2
                and (not isinstance(current.get("p1_admission_path"), str)
                     or not isinstance(current.get("p1_admission_sha256"), str)
                     or not re.fullmatch(r"[0-9a-f]{64}", current.get("p1_admission_sha256", "")))):
            raise RuntimeError("VM marker cannot pass P1 without its exact bound P1 admission hash")
        if (phase_id == "P1_VERIFIED"
                and (current.get("phase_id") != "P1"
                     or not isinstance(current.get("p1_admission_sha256"), str))):
            raise RuntimeError("P1 verification requires the exact marker-bound P1 admission")
        if current.get("phase_id") in {"P1", "P1_VERIFIED"} and phase_id in {"P1_ADMISSION", "P1"}:
            if phase_id != current.get("phase_id"):
                raise RuntimeError("VM marker P1 phase cannot move backwards")
        updated["phase_id"] = phase_id
    if p1_admission_path is not None or p1_admission_sha256 is not None:
        if (p1_admission_path is None or not isinstance(p1_admission_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", p1_admission_sha256)
                or phase_id != "P1" or current.get("phase_id") != "P1_ADMISSION"
                or current.get("state") != "ACTIVE"
                or current.get("p1_admission_sha256") is not None):
            raise RuntimeError("P1 marker advancement requires one signed P1 receipt from the pending boundary")
        p1_path, p1_path_text = _repo_artifact_path(p1_admission_path)
        p1_record, _p1_raw, p1_digest, _p1_identity = _read_signed_record_snapshot(
            p1_path, "vm_admission_hmac_sha256")
        if (p1_digest != p1_admission_sha256
                or p1_record.get("record_type") != "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION"
                or p1_record.get("mode") != VM_MODE
                or p1_record.get("decision") != "PASS"
                or p1_record.get("authorization") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
                or p1_record.get("authorized_phase") != "P1"
                or p1_record.get("p1_authorized") is not True
                or p1_record.get("restore_download_authorized") is not True
                or p1_record.get("p2_authorized") is not False
                or p1_record.get("vm_start_authorized") is not False
                or p1_record.get("receipt_path") != p1_path_text
                or p1_record.get("session_id") != session_id
                or p1_record.get("vm_volume_uuid") != current.get("volume_uuid")):
            raise RuntimeError("marker P1 hash does not identify a valid same-session download-only admission")
        updated["p1_admission_path"] = p1_path_text
        updated["p1_admission_sha256"] = p1_admission_sha256
    elif phase_id == "P1":
        raise RuntimeError("marker cannot advance to P1 without the exact P1 admission path and hash")
    if current.get("p1_admission_sha256") is not None:
        if (updated.get("p1_admission_sha256") != current.get("p1_admission_sha256")
                or updated.get("p1_admission_path") != current.get("p1_admission_path")):
            raise RuntimeError("VM marker P1 admission path/hash is immutable once bound")
    if p1_freshness_renewal is not None:
        if (expected_marker_sha256 is None or state is not None or phase_id is not None
                or volume_uuid is not None or quota_bytes is not None or volume_name is not None
                or safety_volume_uuid is not None or safety_volume_in_use_bytes is not None
                or safety_container_uuid is not None or safety_physical_store_uuid is not None
                or safety_reserve_released is not None or committed_growth_bytes is not None
                or p1_admission_path is not None or p1_admission_sha256 is not None):
            raise RuntimeError("P1 freshness renewal marker changes require an isolated ACTIVE/P1 CAS")
        updated["p1_freshness_renewal"] = _validate_p1_freshness_renewal_marker_update(
            current, p1_freshness_renewal, session_id=session_id)
    if volume_uuid is not None:
        if not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid):
            raise ValueError("VM marker volume UUID is malformed")
        if current.get("volume_uuid") not in (None, volume_uuid):
            raise RuntimeError("VM active marker cannot be rebound to a different volume UUID")
        updated["volume_uuid"] = volume_uuid
    if quota_bytes is not None:
        if not _integer(quota_bytes, minimum=1) or quota_bytes > VM_REVIEWED_QUOTA_MAX_BYTES:
            raise ValueError("VM marker quota is outside the reviewed maximum")
        if current.get("safety_volume_uuid") is not None and quota_bytes != current.get("quota_bytes"):
            raise RuntimeError("VM active marker dynamic quota is immutable after reserve binding")
        updated["quota_bytes"] = quota_bytes
    if volume_name is not None:
        if volume_name not in {VM_VOLUME_NAME, VM_QUOTA_PROBE_VOLUME_NAME}:
            raise ValueError("VM marker volume name is not recognized")
        if current.get("volume_name") not in (None, volume_name):
            raise RuntimeError("VM active marker volume name cannot be rebound")
        updated["volume_name"] = volume_name
    if safety_volume_uuid is not None:
        if not re.fullmatch(r"[0-9A-Fa-f-]{36}", safety_volume_uuid):
            raise ValueError("VM marker safety-volume UUID is malformed")
        if current.get("safety_volume_uuid") not in (None, safety_volume_uuid):
            raise RuntimeError("VM active marker cannot be rebound to a different safety-volume UUID")
        updated["safety_volume_uuid"] = safety_volume_uuid
        updated["safety_reserve_bytes"] = VM_PROTECTED_CAPACITY_RESERVE_BYTES
        safety_container_uuid = safety_container_uuid or EXPECTED_CONTAINER_UUID
        safety_physical_store_uuid = safety_physical_store_uuid or EXPECTED_PHYSICAL_STORE_UUID
        if (safety_container_uuid != EXPECTED_CONTAINER_UUID
                or safety_physical_store_uuid != EXPECTED_PHYSICAL_STORE_UUID):
            raise ValueError("VM marker safety-volume container/store identities are not authorized")
        for key, value in (("safety_container_uuid", safety_container_uuid),
                           ("safety_physical_store_uuid", safety_physical_store_uuid)):
            if current.get(key) not in (None, value):
                raise RuntimeError(f"VM marker {key} cannot be rebound")
            updated[key] = value
    if safety_volume_in_use_bytes is not None:
        if not _integer(safety_volume_in_use_bytes):
            raise ValueError("VM marker safety-volume accounting baseline is invalid")
        if current.get("safety_volume_in_use_bytes") not in (None, safety_volume_in_use_bytes):
            raise RuntimeError("VM active marker safety-volume accounting baseline is immutable")
        updated["safety_volume_in_use_bytes"] = safety_volume_in_use_bytes
    if (updated.get("safety_volume_uuid") is None) != (updated.get("safety_volume_in_use_bytes") is None):
        raise ValueError("VM marker safety-volume UUID and accounting baseline must be bound together")
    if updated.get("safety_volume_uuid") is not None and (
            updated.get("safety_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or updated.get("safety_container_uuid") != EXPECTED_CONTAINER_UUID
            or updated.get("safety_physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID):
        raise ValueError("VM marker safety-volume reserve/container/store contract is incomplete")
    if safety_reserve_released is not None:
        if safety_reserve_released is not True or updated.get("state") != "FAILED":
            raise ValueError("safety reserve release can be recorded only for a failed VM session")
        if phase_id != "RECOVERY":
            raise ValueError("safety reserve release can be recorded only at the RECOVERY phase")
        updated["safety_reserve_released"] = True
    if committed_growth_bytes is not None:
        if not _integer(committed_growth_bytes):
            raise ValueError("VM marker growth bound is invalid")
        updated["committed_growth_bytes"] = committed_growth_bytes
    updated["updated_at_unix"] = time.time()
    return _write_signed_json(VM_ACTIVE_MARKER_PATH, updated, "vm_session_hmac_sha256")


def remove_active_session_marker(session_id: str, stopped_proof: Mapping[str, Any]) -> None:
    """Remove a sticky reservation only after authenticated stopped/owner-release proof."""
    current = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (current.get("session_id") != session_id
            or not verify_signed_record(stopped_proof, "vm_stop_proof_hmac_sha256")
            or stopped_proof.get("session_id") != session_id
            or stopped_proof.get("vm_state") != "STOPPED"
            or stopped_proof.get("all_writable_handles_released") is not True
            or stopped_proof.get("serial_and_attachment_handles_released") is not True):
        raise RuntimeError("VM marker removal requires an authenticated stopped-state and owner-release proof")
    if current.get("safety_volume_uuid") is not None and (
            current.get("state") != "FAILED"
            or current.get("phase_id") != "RECOVERY"
            or current.get("safety_reserve_released") is not True
            or stopped_proof.get("recovery_only") is not True
            or stopped_proof.get("status") != "FAILED / RECOVERY"
            or stopped_proof.get("safety_volume_uuid") != current.get("safety_volume_uuid")):
        raise RuntimeError("safety-reserve sessions can clear their marker only after failed recovery release")
    root_fd = policy._open_directory_nofollow(VM_ACTIVE_MARKER_PATH.parent)
    try:
        current_info = os.stat(VM_ACTIVE_MARKER_PATH.name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISREG(current_info.st_mode) or current_info.st_nlink != 1:
            raise RuntimeError("VM active marker identity is unsafe during recovery")
        raw, identity = policy._read_file_from_directory_fd(
            root_fd, VM_ACTIVE_MARKER_PATH.name, display_path=VM_ACTIVE_MARKER_PATH,
            maximum_bytes=policy.VM_CAPACITY_DOMAIN_ACTIVE_MARKER_MAX_BYTES)
        if (identity[:3] != (current_info.st_dev, current_info.st_ino, current_info.st_size)
                or _sha256(raw) != stopped_proof.get("marker_sha256")):
            raise RuntimeError("VM stop proof does not bind the current active marker")
        os.unlink(VM_ACTIVE_MARKER_PATH.name, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _safety_reserve_recovery_release_blockers(marker: Mapping[str, Any],
                                               stopped_proof: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if marker.get("state") != "FAILED":
        blockers.append("safety reserve release requires a failed VM session")
    if marker.get("phase_id") in {"RECOVERY", "RECOVERY_RESERVE_RELEASE_PENDING"}:
        blockers.append("safety reserve recovery release is already pending or complete")
    if (not isinstance(marker.get("safety_volume_uuid"), str)
            or not _integer(marker.get("safety_reserve_bytes"))
            or marker.get("safety_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or marker.get("safety_container_uuid") != EXPECTED_CONTAINER_UUID
            or marker.get("safety_physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID):
        blockers.append("active marker lacks the exact safety-volume identity contract")
    if (not verify_signed_record(stopped_proof, "vm_phase_monitor_hmac_sha256")
            or stopped_proof.get("status") != "FAILED_STOPPED"
            or stopped_proof.get("session_id") != marker.get("session_id")
            or stopped_proof.get("volume_uuid") != marker.get("volume_uuid")
            or stopped_proof.get("safety_volume_uuid") != marker.get("safety_volume_uuid")
            or stopped_proof.get("phase_quiescent") is not True
            or stopped_proof.get("vm_state") != "STOPPED"
            or stopped_proof.get("all_writable_handles_released") is not True
            or stopped_proof.get("serial_and_attachment_handles_released") is not True):
        blockers.append("reserve release lacks signed failed-phase quiescence and stopped handle-release proof")
    if marker.get("volume_uuid") is None:
        if (marker.get("phase_id") != "P0_RECOVERY_PREPARED"
                or stopped_proof.get("pre_vm_creation_recovery") is not True
                or stopped_proof.get("prepared_marker_sha256") != _file_sha256(VM_ACTIVE_MARKER_PATH)):
            blockers.append("pre-VM reserve release lacks a current signed no-VM P0 recovery proof")
    return blockers


def release_safety_reserve_for_recovery(
        stopped_proof: Mapping[str, Any], *,
        receipt_path: Path = VM_RECOVERY_RESERVE_RELEASE_PATH) -> dict[str, Any]:
    """Delete the exact safety volume only after signed failed-session quiescence.

    The result permanently records ``FAILED / RECOVERY``. Reserve release never
    converts an unsuccessful VM session to a passing result.
    """
    with policy._acquire_storage_allocation_lock():
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        blockers = _safety_reserve_recovery_release_blockers(marker, stopped_proof)
        if blockers:
            raise RuntimeError("safety reserve recovery release is blocked: " + "; ".join(blockers))
        volume_uuid = marker.get("volume_uuid")
        safety_uuid = marker.get("safety_volume_uuid")
        pre_vm_recovery = (volume_uuid is None
                           and stopped_proof.get("pre_vm_creation_recovery") is True)
        if (not isinstance(safety_uuid, str)
                or (not isinstance(volume_uuid, str) and not pre_vm_recovery)):
            raise RuntimeError("safety reserve recovery identity is incomplete")
        before = capture_host_observation()
        if pre_vm_recovery:
            blockers = _container_blockers(
                before, expected_vm_volume_uuid=None,
                expected_safety_volume_uuid=safety_uuid,
                expected_safety_volume_in_use_bytes=marker.get("safety_volume_in_use_bytes"))
        else:
            blockers = _session_container_blockers(before, volume_uuid=volume_uuid)
        if blockers:
            raise RuntimeError("safety reserve recovery read-back failed: " + "; ".join(blockers))
        safety = _volume_row(before, safety_uuid)
        if (safety.get("Name") != VM_SAFETY_VOLUME_NAME
                or safety.get("CapacityReserve") != VM_PROTECTED_CAPACITY_RESERVE_BYTES):
            raise RuntimeError("recovery target is not the bound 15 GiB safety volume")
        safety_info = before.get("safety_volume_info")
        if (not isinstance(safety_info, Mapping)
                or safety_info.get("mount_state") != "UNMOUNTED_VERIFIED"):
            raise RuntimeError("recovery will not delete a mounted or unknown safety volume")
        update_active_session_marker(
            str(marker["session_id"]), state="FAILED",
            phase_id="RECOVERY_RESERVE_RELEASE_PENDING")
        # Resolve the transient device again immediately before the destructive
        # command; the stable volume UUID remains the authority.
        current = capture_host_observation()
        if pre_vm_recovery:
            blockers = _container_blockers(
                current, expected_vm_volume_uuid=None,
                expected_safety_volume_uuid=safety_uuid,
                expected_safety_volume_in_use_bytes=marker.get("safety_volume_in_use_bytes"))
        else:
            blockers = _session_container_blockers(current, volume_uuid=volume_uuid)
        if blockers:
            raise RuntimeError("safety reserve changed before recovery deletion: " + "; ".join(blockers))
        current_safety = _volume_row(current, safety_uuid)
        device = current_safety.get("DeviceIdentifier")
        if (current_safety.get("CapacityReserve") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
                or not isinstance(device, str)
                or not re.fullmatch(r"disk[0-9]+s[0-9]+", device)):
            raise RuntimeError("fresh recovery safety-volume UUID/reserve/device read-back is invalid")
        completed = subprocess.run(
            [_diskutil(), "apfs", "deleteVolume", f"/dev/{device}"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120.0, text=True)
        if completed.returncode != 0:
            update_active_session_marker(
                str(marker["session_id"]), state="FAILED",
                phase_id="RECOVERY_RESERVE_RELEASE_FAILED")
            raise RuntimeError("exact safety-volume recovery deletion failed: " + completed.stderr[-2000:])
        after = capture_host_observation()
        after_blockers = _container_blockers(
            after, expected_vm_volume_uuid=volume_uuid,
            expected_vm_quota_bytes=marker.get("quota_bytes"),
            expected_safety_volume_uuid=None,
            expected_vm_volume_name=marker.get("volume_name", VM_VOLUME_NAME))
        remaining_safety = [row for row in after.get("apfs_container", {}).get("Volumes", [])
                            if isinstance(row, Mapping)
                            and (row.get("APFSVolumeUUID") == safety_uuid
                                 or row.get("Name") == VM_SAFETY_VOLUME_NAME)]
        if remaining_safety:
            after_blockers.append("safety volume remains after recovery deleteVolume returned success")
        if after_blockers:
            update_active_session_marker(
                str(marker["session_id"]), state="FAILED",
                phase_id="RECOVERY_RESERVE_RELEASE_READBACK_FAILED")
            raise RuntimeError("safety reserve release read-back failed; sticky marker retained: "
                               + "; ".join(after_blockers))
        updated = update_active_session_marker(
            str(marker["session_id"]), state="FAILED", phase_id="RECOVERY",
            safety_reserve_released=True)
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "FAILED / RECOVERY",
            "session_id": marker["session_id"],
            "vm_volume_uuid": volume_uuid,
            "safety_volume_uuid": safety_uuid,
            "safety_volume_name": VM_SAFETY_VOLUME_NAME,
            "safety_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "safety_reserve_released": True,
            "pre_vm_creation_recovery": pre_vm_recovery,
            "phase_monitor_proof_sha256": _sha256(
                policy._canonical_pretty_json_bytes(stopped_proof)),
            "controller_identity": _controller_identity(),
            "marker_after_release_sha256": _sha256(
                policy._canonical_pretty_json_bytes(updated)),
            "before_observation": before,
            "after_observation": after,
            "delete_stdout_sha256": _sha256(completed.stdout.encode()),
            "delete_stderr_sha256": _sha256(completed.stderr.encode()),
            "blockers": [],
            "warnings": ["recovery-only reserve release permanently fails this VM session"],
        }
        sealed = _write_signed_json(
            receipt_path, record, "vm_recovery_reserve_release_hmac_sha256")
        final_marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        cleanup_proof = _seal({
            "schema_version": 1,
            "session_id": marker["session_id"],
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
            "recovery_only": True,
            "status": "FAILED / RECOVERY",
            "safety_volume_uuid": safety_uuid,
            "pre_vm_creation_recovery": pre_vm_recovery,
            "marker_sha256": _sha256(policy._canonical_pretty_json_bytes(final_marker)),
            "recovery_receipt_sha256": _file_sha256(receipt_path),
        }, "vm_stop_proof_hmac_sha256")
        remove_active_session_marker(str(marker["session_id"]), cleanup_proof)
        return sealed


def recover_partial_vm_volume_creation(
        *, receipt_path: Path = VM_RECOVERY_RESERVE_RELEASE_PATH) -> dict[str, Any]:
    """Close or release a failed P0 reserve attempt before any VM volume exists.

    This recovery-only entry point accepts only failed/unknown pre-VM phases,
    rechecks that no VM volume exists, and either clears a failed create marker
    when no reserve was created or binds and releases one exact unmounted
    15 GiB safety volume through the ordinary signed recovery path.
    """
    allowed_phases = {
        "SAFETY_RESERVE_CREATE_FAILED",
        "SAFETY_RESERVE_IDENTITY_UNKNOWN",
        "SAFETY_RESERVE_MOUNT_UNKNOWN",
        "SAFETY_RESERVE_MOUNTED",
        "SAFETY_RESERVE_ACCOUNTING_UNKNOWN",
        "SAFETY_RESERVE_READBACK_FAILED",
        "POST_RESERVE_FREE_UNKNOWN",
        "POST_RESERVE_FREE_UNBOUNDED",
        "POST_RESERVE_QUOTA_FAILED",
        "SAFETY_RESERVE_BOUND",
        "VM_VOLUME_CREATE_FAILED",
    }
    proof: dict[str, Any] | None = None
    no_reserve_record: dict[str, Any] | None = None
    with policy._acquire_storage_allocation_lock():
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("state") not in {"FAILED", "UNKNOWN"}
                or marker.get("phase_id") not in allowed_phases
                or marker.get("volume_uuid") is not None
                or marker.get("container_uuid") != EXPECTED_CONTAINER_UUID
                or not _integer(marker.get("pre_reserve_shared_free_bytes"),
                                minimum=VM_PROTECTED_CAPACITY_RESERVE_BYTES + 1)
                or marker.get("unreserved_free_ceiling_bytes")
                != marker.get("pre_reserve_shared_free_bytes") - VM_PROTECTED_CAPACITY_RESERVE_BYTES):
            raise RuntimeError("partial P0 recovery requires a failed pre-VM marker with a valid capacity ceiling")
        observation = capture_host_observation()
        volumes = observation.get("apfs_container", {}).get("Volumes", [])
        safety_rows = [row for row in volumes if isinstance(row, Mapping)
                       and row.get("Name") == VM_SAFETY_VOLUME_NAME]
        if len(safety_rows) > 1:
            raise RuntimeError("partial P0 recovery found multiple same-name safety volumes")
        if not safety_rows:
            if marker.get("phase_id") != "SAFETY_RESERVE_CREATE_FAILED":
                raise RuntimeError("partial P0 recovery cannot clear a missing reserve after a later P0 phase")
            blockers = _container_blockers(observation, expected_vm_volume_uuid=None)
            if blockers:
                raise RuntimeError("failed safety-volume creation has unresolved APFS state: "
                                   + "; ".join(blockers))
            updated = update_active_session_marker(
                str(marker["session_id"]), state="FAILED", phase_id="P0_RECOVERY_NO_RESERVE")
            stop_proof = _seal({
                "schema_version": 1,
                "session_id": marker["session_id"],
                "vm_state": "STOPPED",
                "vm_creation_completed": False,
                "all_writable_handles_released": True,
                "serial_and_attachment_handles_released": True,
                "recovery_only": True,
                "status": "FAILED / RECOVERY",
                "marker_sha256": _file_sha256(VM_ACTIVE_MARKER_PATH),
            }, "vm_stop_proof_hmac_sha256")
            remove_active_session_marker(str(marker["session_id"]), stop_proof)
            no_reserve_record = {
                "schema_version": 1,
                "mode": VM_MODE,
                "status": "FAILED / RECOVERY",
                "session_id": marker["session_id"],
                "pre_vm_creation_recovery": True,
                "safety_reserve_released": False,
                "no_safety_volume_present": True,
                "before_observation": observation,
                "controller_identity": _controller_identity(),
                "blockers": [],
                "warnings": ["failed volume creation left no safety reserve; VM session remains failed"],
            }
        else:
            candidate = safety_rows[0]
            safety_uuid = candidate.get("APFSVolumeUUID")
            safety_used = candidate.get("CapacityInUse")
            if (not isinstance(safety_uuid, str)
                    or not _integer(safety_used)
                    or marker.get("safety_volume_uuid") != safety_uuid
                    or marker.get("safety_volume_in_use_bytes") != safety_used
                    or marker.get("safety_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
                    or marker.get("safety_container_uuid") != EXPECTED_CONTAINER_UUID
                    or marker.get("safety_physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID):
                raise RuntimeError(
                    "partial P0 recovery requires the signed safety-volume UUID and empty-accounting "
                    "baseline captured before recovery")
            blockers = _container_blockers(
                observation, expected_vm_volume_uuid=None,
                expected_safety_volume_uuid=safety_uuid,
                expected_safety_volume_in_use_bytes=safety_used)
            if blockers:
                raise RuntimeError("partial P0 safety-volume recovery preflight failed: "
                                   + "; ".join(blockers))
            update_active_session_marker(
                str(marker["session_id"]), state="FAILED", phase_id="P0_RECOVERY_PREPARED",
                safety_volume_uuid=safety_uuid,
                safety_volume_in_use_bytes=safety_used,
                safety_container_uuid=EXPECTED_CONTAINER_UUID,
                safety_physical_store_uuid=EXPECTED_PHYSICAL_STORE_UUID)
            proof = _seal({
                "schema_version": 1,
                "status": "FAILED_STOPPED",
                "session_id": marker["session_id"],
                "volume_uuid": None,
                "safety_volume_uuid": safety_uuid,
                "phase_quiescent": True,
                "vm_state": "STOPPED",
                "vm_creation_completed": False,
                "pre_vm_creation_recovery": True,
                "prepared_marker_sha256": _file_sha256(VM_ACTIVE_MARKER_PATH),
                "all_writable_handles_released": True,
                "serial_and_attachment_handles_released": True,
            }, "vm_phase_monitor_hmac_sha256")
    if proof is not None:
        return release_safety_reserve_for_recovery(proof, receipt_path=receipt_path)
    assert no_reserve_record is not None
    return _write_signed_json(
        receipt_path, no_reserve_record, "vm_recovery_reserve_release_hmac_sha256")


def create_vm_volume(
        precreation_receipt_path: Path = VM_PRECREATION_ADMISSION_PATH, *,
        quota_probe_receipt_path: Path = VM_QUOTA_PROBE_RECEIPT_PATH,
        receipt_path: Path = VM_VOLUME_CREATION_RECEIPT_PATH) -> dict[str, Any]:
    """Create and bind the empty safety reserve before the dynamic VM quota volume."""
    receipt = _read_signed_record(precreation_receipt_path, "vm_admission_hmac_sha256")
    if (receipt.get("decision") != "PASS"
            or receipt.get("authorization") != "CREATE_VM_VOLUME_ONLY"
            or receipt.get("mode") != VM_MODE):
        raise RuntimeError("volume creation requires a signed PASS receipt scoped to volume creation")
    age = time.time() - float(receipt.get("created_at_unix", 0))
    if age < 0 or age > VM_MAX_OBSERVATION_AGE_SECONDS:
        raise RuntimeError("volume creation admission is stale")
    if receipt.get("controller_identity") != _controller_identity():
        raise RuntimeError("VM volume creation admission does not bind the current controller bytes")
    verified_g0c = verify_fresh_g0c_review()
    if receipt.get("g0c_identity") != verified_g0c:
        raise RuntimeError("VM volume creation admission does not bind the current G0C review")
    probe = _read_signed_record(quota_probe_receipt_path, "vm_quota_probe_hmac_sha256")
    if (probe.get("status") != "PASS" or probe.get("probe_volume_deleted") is not True
            or probe.get("controller_identity") != _controller_identity()
            or probe.get("g0c_identity") != verified_g0c
            or receipt.get("quota_probe_receipt_sha256") != _file_sha256(quota_probe_receipt_path)):
        raise RuntimeError("VM volume creation requires the current signed empirical quota-probe PASS")
    plan = receipt.get("phase_evaluation", {}).get("plan", {})
    restore_expected = plan.get("restore_expected_bytes")
    minimum_vm_quota = receipt.get("dynamic_vm_quota_pre_reserve", {}).get(
        "minimum_required_quota_bytes")
    if not _integer(minimum_vm_quota, minimum=1):
        raise RuntimeError("volume-creation admission lacks a reviewed minimum quota for known phase data")
    restore_metadata = receipt.get("restore_upstream_metadata")
    metadata_receipt_sha256 = receipt.get("restore_metadata_receipt_sha256")
    try:
        current_metadata_receipt = _read_latest_restore_metadata()
        current_metadata_sha = _file_sha256(VM_RESTORE_METADATA_PATH)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"fresh signed latest Apple restore metadata is unavailable: {error}") from error
    if (not isinstance(restore_metadata, Mapping)
            or restore_metadata.get("status") == "UNKNOWN"
            or restore_metadata.get("expected_length_bytes") != restore_expected
            or current_metadata_sha != metadata_receipt_sha256
            or current_metadata_receipt.get("metadata_sha256") != restore_metadata.get("metadata_sha256")):
        raise RuntimeError("volume creation admission lacks bound latest Apple restore metadata")
    session_id = uuid.uuid4().hex
    plan_hash = _sha256(policy._canonical_pretty_json_bytes(plan))
    with policy._acquire_storage_allocation_lock():
        observation = capture_host_observation()
        blockers = _container_blockers(observation, expected_vm_volume_uuid=None)
        try:
            before_free = conservative_container_free_bytes(observation)
        except (TypeError, ValueError) as error:
            before_free = None
            blockers.append(f"pre-reserve shared free-capacity readback is unknown: {error}")
        pre_reserve_quota = calculate_dynamic_vm_quota(
            free_bytes=before_free,
            deterministic_uncertainty_bytes=VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=minimum_vm_quota,
            maximum_quota_bytes=VM_REVIEWED_QUOTA_MAX_BYTES,
            reserve_already_reflected=False)
        blockers.extend(pre_reserve_quota["blockers"])
        reevaluation = evaluate_initial_plan(
            observation, restore_expected,
            vm_quota_bytes=pre_reserve_quota.get("quota_bytes"))
        blockers.extend(reevaluation["blockers"])
        if blockers:
            raise RuntimeError("pre-creation APFS identity or capacity revalidation failed: "
                               + "; ".join(blockers))
        quota_before_reserve = pre_reserve_quota["quota_bytes"]
        marker = write_active_session_marker(
            session_id=session_id, phase_id="VOLUME_CREATE", container_uuid=EXPECTED_CONTAINER_UUID,
            volume_uuid=None, committed_growth_bytes=quota_before_reserve,
            plan_sha256=plan_hash, quota_bytes=quota_before_reserve,
            pre_reserve_shared_free_bytes=before_free,
            unreserved_free_ceiling_bytes=(before_free - VM_PROTECTED_CAPACITY_RESERVE_BYTES))
        container_reference = observation["apfs_container"]["ContainerReference"]
        safety_create = subprocess.run(
            [_diskutil(), "apfs", "addVolume", str(container_reference), VM_FILESYSTEM,
             VM_SAFETY_VOLUME_NAME, "-reserve", "15GiB", "-nomount"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120.0, text=True,
        )
        if safety_create.returncode != 0:
            update_active_session_marker(session_id, state="FAILED", phase_id="SAFETY_RESERVE_CREATE_FAILED")
            raise RuntimeError("diskutil safety-volume creation failed; sticky marker retained: "
                               + safety_create.stderr[-2000:])
        safety_created = capture_host_observation()
        safety_row = _named_volume(safety_created, VM_SAFETY_VOLUME_NAME)
        if not isinstance(safety_row, Mapping):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="SAFETY_RESERVE_IDENTITY_UNKNOWN")
            raise RuntimeError("safety-volume identity is missing or ambiguous; sticky marker retained")
        safety_uuid = safety_row.get("APFSVolumeUUID")
        safety_device = safety_row.get("DeviceIdentifier")
        if (not isinstance(safety_uuid, str)
                or not isinstance(safety_device, str)
                or not re.fullmatch(r"disk[0-9]+s[0-9]+", safety_device)):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="SAFETY_RESERVE_IDENTITY_UNKNOWN")
            raise RuntimeError("safety-volume UUID or resolved device is unknown; sticky marker retained")
        safety_mount = safety_created.get("safety_volume_info")
        if (not isinstance(safety_mount, Mapping)
                or safety_mount.get("mount_state") not in _EXPECTED_VM_MOUNT_STATES):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="SAFETY_RESERVE_MOUNT_UNKNOWN")
            raise RuntimeError("safety-volume mount state is unknown; sticky marker retained")
        if safety_mount.get("mount_state") != "UNMOUNTED_VERIFIED":
            update_active_session_marker(session_id, state="FAILED", phase_id="SAFETY_RESERVE_MOUNTED")
            raise RuntimeError("new safety volume mounted despite -nomount; refusing to bind an empty baseline")
        after_safety = capture_host_observation()
        safety_row = _volume_row(after_safety, safety_uuid)
        safety_used = safety_row.get("CapacityInUse")
        if not _integer(safety_used):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="SAFETY_RESERVE_ACCOUNTING_UNKNOWN")
            raise RuntimeError("safety-volume empty-accounting baseline is unknown; sticky marker retained")
        safety_blockers = _container_blockers(
            after_safety, expected_safety_volume_uuid=safety_uuid,
            expected_safety_volume_in_use_bytes=safety_used)
        if safety_blockers:
            update_active_session_marker(session_id, state="FAILED", phase_id="SAFETY_RESERVE_READBACK_FAILED")
            raise RuntimeError("safety volume failed its exact reserve/identity/empty/unmounted read-back; "
                               "sticky marker retained: " + "; ".join(safety_blockers))
        try:
            after_safety_free = conservative_container_free_bytes(after_safety)
        except (TypeError, ValueError) as error:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="POST_RESERVE_FREE_UNKNOWN")
            raise RuntimeError(f"shared free after reserve creation is unknown; sticky marker retained: {error}") from error
        post_reserve_capacity = calculate_post_reserve_free_capacity(
            pre_reserve_free_bytes=before_free,
            post_reserve_free_bytes=after_safety_free)
        if not post_reserve_capacity["admitted"]:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="POST_RESERVE_FREE_UNBOUNDED")
            raise RuntimeError("post-reserve shared free capacity cannot be conservatively bounded; "
                               "sticky marker retained: "
                               + "; ".join(post_reserve_capacity["blockers"]))
        post_reserve_quota = calculate_dynamic_vm_quota(
            free_bytes=post_reserve_capacity["effective_unreserved_free_bytes"],
            deterministic_uncertainty_bytes=VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=minimum_vm_quota,
            maximum_quota_bytes=VM_REVIEWED_QUOTA_MAX_BYTES,
            reserve_already_reflected=True)
        if not post_reserve_quota["admitted"]:
            update_active_session_marker(session_id, state="FAILED", phase_id="POST_RESERVE_QUOTA_FAILED",
                                         safety_volume_uuid=safety_uuid,
                                         safety_volume_in_use_bytes=safety_used)
            raise RuntimeError("post-reserve dynamic quota admission failed; sticky marker retained: "
                               + "; ".join(post_reserve_quota["blockers"]))
        quota_bytes = post_reserve_quota["quota_bytes"]
        update_active_session_marker(
            session_id, phase_id="SAFETY_RESERVE_BOUND",
            safety_volume_uuid=safety_uuid,
            safety_volume_in_use_bytes=safety_used,
            quota_bytes=quota_bytes,
            committed_growth_bytes=quota_bytes)
        vm_create = subprocess.run(
            [_diskutil(), "apfs", "addVolume", str(container_reference), VM_FILESYSTEM,
             VM_VOLUME_NAME, "-quota", f"{quota_bytes // MIB}MiB"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120.0, text=True)
        if vm_create.returncode != 0:
            update_active_session_marker(session_id, state="FAILED", phase_id="VM_VOLUME_CREATE_FAILED")
            raise RuntimeError("diskutil VM-volume creation failed; sticky marker retained: "
                               + vm_create.stderr[-2000:])
        after = capture_host_observation()
        volumes = after["apfs_container"].get("Volumes", [])
        matches = [row for row in volumes if isinstance(row, Mapping)
                   and row.get("Name") == VM_VOLUME_NAME]
        if len(matches) != 1:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="VM_VOLUME_READBACK_UNKNOWN")
            raise RuntimeError("VM volume UUID is missing or ambiguous; sticky marker retained")
        vm_uuid = matches[0].get("APFSVolumeUUID")
        if not isinstance(vm_uuid, str):
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="VM_VOLUME_UUID_UNKNOWN")
            raise RuntimeError("new VM volume UUID is missing; sticky marker retained")
        update_active_session_marker(session_id, volume_uuid=vm_uuid,
                                     volume_name=VM_VOLUME_NAME)
        try:
            after_blockers = _session_container_blockers(after, volume_uuid=vm_uuid)
            mount = _volume_mount_info(after, vm_uuid)
        except (OSError, RuntimeError, ValueError) as error:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="VM_VOLUME_READBACK_UNKNOWN")
            raise RuntimeError(f"new VM volume failed identity read-back; sticky marker retained: {error}") from error
        if after_blockers:
            update_active_session_marker(session_id, state="UNKNOWN", phase_id="VM_VOLUME_READBACK_FAILED")
            raise RuntimeError("new VM volume failed quota/container verification; sticky marker retained: "
                               + "; ".join(after_blockers))
        update_active_session_marker(session_id, phase_id="P1_ADMISSION", volume_uuid=vm_uuid,
                                     committed_growth_bytes=reevaluation["plan"]["phases"]["P1"]["growth_bytes"])
        try:
            post_vm_free = conservative_session_unreserved_free_bytes(after, volume_uuid=vm_uuid)
            post_vm_used = _volume_row(after, vm_uuid).get("CapacityInUse")
            post_vm_capacity = calculate_vm_phase_admission(
                mode=VM_MODE, container_uuid=EXPECTED_CONTAINER_UUID,
                volume_uuid=vm_uuid, free_bytes=post_vm_free,
                quota_bytes=quota_bytes, quota_used_bytes=post_vm_used,
                growth_bytes=0, uncertainty_bytes=0,
                concurrent_growth_bytes=None, expected_quota_bytes=quota_bytes)
        except (TypeError, ValueError) as error:
            post_vm_capacity = {"admitted": False,
                                "blockers": [f"post-VM-creation live capacity is unknown: {error}"]}
        if not post_vm_capacity.get("admitted"):
            update_active_session_marker(session_id, state="FAILED", phase_id="POST_VM_CAPACITY_FAILED")
            raise RuntimeError("post-VM-creation live capacity check failed; sticky marker retained: "
                               + "; ".join(post_vm_capacity.get("blockers", [])))
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS",
            "session_id": session_id,
            "created_at_unix": time.time(),
            "volume_name": VM_VOLUME_NAME,
            "volume_uuid": vm_uuid,
            "safety_volume_name": VM_SAFETY_VOLUME_NAME,
            "safety_volume_uuid": safety_uuid,
            "safety_volume_in_use_bytes": safety_used,
            "safety_container_uuid": EXPECTED_CONTAINER_UUID,
            "safety_physical_store_uuid": EXPECTED_PHYSICAL_STORE_UUID,
            "safety_volume_quota_bytes": 0,
            "safety_volume_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "pre_reserve_shared_free_bytes": before_free,
            "post_reserve_observed_shared_free_bytes": after_safety_free,
            "unreserved_free_ceiling_bytes": post_reserve_capacity["unreserved_free_ceiling_bytes"],
            "post_reserve_effective_unreserved_free_bytes": (
                post_reserve_capacity["effective_unreserved_free_bytes"]),
            "safety_volume_unmounted": True,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "physical_store_uuid": EXPECTED_PHYSICAL_STORE_UUID,
            "quota_bytes": quota_bytes,
            "quota_max_bytes": VM_REVIEWED_QUOTA_MAX_BYTES,
            "reserve_bytes": 0,
            "dynamic_quota_pre_reserve": pre_reserve_quota,
            "dynamic_quota_post_reserve": post_reserve_quota,
            "post_vm_creation_capacity_evaluation": post_vm_capacity,
            "restore_upstream_metadata": dict(restore_metadata),
            "restore_metadata_receipt_sha256": metadata_receipt_sha256,
            "initial_phase_plan": plan,
            "concurrent_growth_observation_bytes": receipt.get("concurrent_growth_bound_bytes"),
            "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
            "g0c_identity": verified_g0c,
            "mount": mount,
            "before_observation": observation,
            "after_observation": after,
            "safety_volume_create_stdout_sha256": _sha256(safety_create.stdout.encode()),
            "vm_volume_create_stdout_sha256": _sha256(vm_create.stdout.encode()),
            "safety_volume_create_stderr_sha256": _sha256(safety_create.stderr.encode()),
            "vm_volume_create_stderr_sha256": _sha256(vm_create.stderr.encode()),
            "precreation_admission_sha256": _file_sha256(precreation_receipt_path),
            "quota_probe_receipt_sha256": _file_sha256(quota_probe_receipt_path),
            "active_marker_sha256": _sha256(policy._canonical_pretty_json_bytes(marker)),
        }
        return _write_signed_json(receipt_path, record,
                                  "vm_creation_hmac_sha256")


def create_p1_admission(
        restore_expected_bytes: int | None = None,
        restore_source_url: str | None = None,
        restore_metadata_sha256: str | None = None,
        creation_receipt_path: Path | None = None,
        *, lineage_path: Path,
        compatibility_basis_path: Path,
        compatibility_basis_review_path: Path,
        transition_path: Path,
        transition_review_path: Path,
        fresh_metadata_path: Path | None = None,
        receipt_path: Path | None = None) -> dict[str, Any]:
    """Issue a download-only P1 receipt from the reviewed additive P0 transition chain."""
    requested_restore_expected_bytes = restore_expected_bytes
    requested_restore_source_url = restore_source_url
    requested_restore_metadata_sha256 = restore_metadata_sha256
    p0 = _accepted_p0_chain()
    expected_creation_path = VM_P0_CREATION_RECEIPT_PATH
    if creation_receipt_path is not None:
        selected_creation_path, selected_creation_text = _repo_artifact_path(creation_receipt_path)
        if (selected_creation_path != expected_creation_path
                or selected_creation_text != p0["p0_ancestry"]["creation_receipt_path"]):
            raise RuntimeError("P1 must use the exact fixed run-specific P0 creation receipt")
    creation_path, creation_path_text = _repo_artifact_path(expected_creation_path)
    creation, _creation_raw, creation_sha, _creation_identity = _read_signed_record_snapshot(
        creation_path, "vm_creation_hmac_sha256")
    if (creation_sha != p0["p0_ancestry"]["creation_receipt_sha256"]
            or creation.get("status") != "PASS" or creation.get("mode") != VM_MODE
            or not _integer(creation.get("quota_bytes"), minimum=1)
            or creation.get("quota_bytes") > VM_REVIEWED_QUOTA_MAX_BYTES
            or creation.get("reserve_bytes") != 0
            or creation.get("safety_volume_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or creation.get("safety_volume_unmounted") is not True):
        raise RuntimeError("P1 admission requires the exact immutable P0 creation contract")

    lineage_absolute, lineage_path_text = _repo_artifact_path(lineage_path)
    lineage, _lineage_raw, lineage_sha, lineage_identity = _read_signed_record_snapshot(
        lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256")
    _require_direct_p0_metadata_lineage(
        lineage, path_text=lineage_path_text, p0_ancestry=p0["p0_ancestry"])
    _require_nonfuture_timestamp(lineage.get("created_at_unix"), label="metadata refresh lineage")

    basis_absolute, basis_path_text = _repo_artifact_path(compatibility_basis_path)
    basis, _basis_raw, basis_sha, basis_identity = _read_signed_record_snapshot(
        basis_absolute, "vm_restore_compatibility_basis_hmac_sha256")
    basis_review_absolute, basis_review_path_text = _repo_artifact_path(compatibility_basis_review_path)
    basis_review, basis_review_sha = _verify_review_artifact(
        basis_review_absolute, artifact_path=basis_path_text, artifact_sha256=basis_sha,
        not_before_unix=basis.get("created_at_unix"))
    if (basis.get("record_type") != "R6_VM_RESTORE_METADATA_COMPATIBILITY_BASIS"
            or basis.get("status") != "PASS"
            or basis.get("receipt_path") != basis_path_text
            or basis.get("p0_ancestry") != p0["p0_ancestry"]
            or basis.get("lineage_path") != lineage_path_text
            or basis.get("lineage_sha256") != lineage_sha
            or basis.get("p0_hardware_fields_were_not_captured") is not True
            or basis.get("retroactive_p0_hardware_equivalence_claim") is not False
            or basis.get("decision") != "ADOPT_CURRENT_HARDWARE_COMPATIBILITY_FOR_P1"
            or basis.get("restore_download_authorized") is not False
            or basis.get("p1_authorized") is not False
            or basis.get("p2_authorized") is not False
            or basis.get("vm_start_authorized") is not False):
        raise RuntimeError("P1 requires the reviewed additive basis with no retroactive P0 claim")
    _require_nonfuture_timestamp(basis.get("created_at_unix"), label="hardware compatibility basis")

    transition_absolute, transition_path_text = _repo_artifact_path(transition_path)
    transition, _transition_raw, transition_sha, transition_identity = _read_signed_record_snapshot(
        transition_absolute, "vm_p0_revalidation_hmac_sha256")
    transition_review_absolute, transition_review_path_text = _repo_artifact_path(transition_review_path)
    transition_review, transition_review_sha = _verify_review_artifact(
        transition_review_absolute, artifact_path=transition_path_text,
        artifact_sha256=transition_sha, not_before_unix=transition.get("created_at_unix"))
    if (transition.get("record_type") != "R6_VM_P0_REVALIDATED_FOR_P1"
            or transition.get("receipt_path") != transition_path_text
            or transition.get("decision") != "PASS"
            or transition.get("transition_scope") != "P0_COMPATIBILITY_REVALIDATION_ONLY"
            or transition.get("p0_ancestry") != p0["p0_ancestry"]
            or transition.get("restore_lineage") != {"path": lineage_path_text, "sha256": lineage_sha}
            or transition.get("compatibility_basis") != {
                "path": basis_path_text, "sha256": basis_sha,
                "review_path": basis_review_path_text, "review_sha256": basis_review_sha}
            or transition.get("historical_p0_controller_identity")
            != p0["historical_controller_identity"]
            or transition.get("historical_p0_g0c_identity") != p0["historical_g0c_identity"]
            or any(transition.get(field) is not False for field in (
                "p1_authorized", "p2_authorized", "vm_start_authorized",
                "restore_download_authorized"))
            or "authorized_phase" in transition):
        raise RuntimeError("P1 requires a reviewed read-only P0 transition with no execution authority")
    if (transition.get("compatibility_basis", {}).get("review_path") != basis_review_path_text
            or transition_review.get("reviewed_artifact_sha256") != transition_sha):
        raise RuntimeError("P0 transition review or compatibility-basis review binding differs")
    _require_nonfuture_timestamp(transition.get("created_at_unix"), label="P0 transition receipt")

    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    for artifact in (lineage, basis, transition):
        if (artifact.get("current_controller_identity", artifact.get("controller_identity"))
                != current_controller
                or artifact.get("current_g0c_identity", artifact.get("g0c_identity"))
                != current_g0c):
            raise RuntimeError("P0-to-P1 transition artifact binds different controller or current G0C bytes")
    if (lineage.get("historical_controller_identity") != p0["historical_controller_identity"]
            or lineage.get("historical_g0c_identity") != p0["historical_g0c_identity"]):
        raise RuntimeError("P1 lineage does not preserve the historical P0 controller/G0C identities")
    if (current_controller.get("storage_policy_sha256")
            != p0["historical_controller_identity"].get("storage_policy_sha256")):
        raise RuntimeError("P1 storage-policy identity differs from immutable P0 without reviewed scope")
    probe_source_transition = {
        "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
        "current_probe_sha256": current_controller["restore_metadata_probe_sha256"],
        "current_g0c_reviewed_current_probe": True,
    }
    if transition.get("probe_source_transition") != probe_source_transition:
        raise RuntimeError("P0 transition does not separately bind historical/current reviewed probe identities")

    fresh_ref = lineage.get("fresh_observation")
    if not isinstance(fresh_ref, Mapping):
        raise RuntimeError("P1 lineage omits its unique fresh metadata leaf")
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_ref.get("receipt_path", ""))
    if fresh_metadata_path is not None:
        supplied_fresh_path, supplied_fresh_text = _repo_artifact_path(fresh_metadata_path)
        if supplied_fresh_path != fresh_path or supplied_fresh_text != fresh_path_text:
            raise RuntimeError("P1 fresh metadata locator differs from its signed lineage")
    fresh, fresh_sha, fresh_identity = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    _fresh_captured_at, age_at_p1_admission = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at P1 admission",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
    if (fresh_path_text != fresh_ref.get("receipt_path")
            or fresh_sha != fresh_ref.get("receipt_sha256")
            or fresh_sha != basis.get("fresh_metadata_sha256")
            or fresh.get("metadata_sha256") != fresh_ref.get("metadata_observation_sha256")
            or fresh.get("metadata_sha256") != basis.get("fresh_metadata_observation_sha256")):
        raise RuntimeError("P1 fresh metadata receipt is not the exact current lineage/basis leaf")
    fresh_host = _host_tuple(fresh.get("host"))
    if fresh_ref.get("host") != fresh_host:
        raise RuntimeError("lineage host tuple differs from its signed fresh metadata observation")
    legacy_subject = _legacy_restore_subject(fresh, fresh_host)
    full_subject = _complete_restore_subject(legacy_subject, fresh)
    legacy_identity = _restore_subject_digest(legacy_subject, legacy=True)
    full_identity = _restore_subject_digest(full_subject)
    if (legacy_identity != p0["p0_ancestry"]["legacy_restore_subject_identity"]
            or legacy_identity != fresh_ref.get("legacy_restore_subject_identity")
            or legacy_identity != basis.get("legacy_restore_subject_identity")
            or full_identity != basis.get("restore_subject_identity")
            or full_subject != basis.get("restore_subject")
            or _hardware_compatibility_tuple(fresh) != basis.get("adopted_hardware_compatibility")):
        raise RuntimeError("P1 fresh legacy/full restore subject differs from the reviewed identity basis")
    basis_created_at = _require_nonfuture_timestamp(
        basis.get("created_at_unix"), label="hardware compatibility basis")
    _fresh_at_basis, fresh_age_at_basis = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at compatibility basis",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=basis_created_at)
    _require_recorded_age(basis.get("fresh_metadata_age_at_basis_seconds"), fresh_age_at_basis,
                         label="hardware compatibility basis")
    lineage_created_at = _require_nonfuture_timestamp(
        lineage.get("created_at_unix"), label="metadata refresh lineage")
    _fresh_captured_at, lineage_fresh_age = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at lineage creation",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=lineage_created_at)
    _require_recorded_age(fresh_ref.get("age_at_lineage_creation_seconds"), lineage_fresh_age,
                         label="metadata refresh lineage")
    transition_fresh = transition.get("fresh_metadata")
    if (not isinstance(transition_fresh, Mapping)
            or transition_fresh.get("path") != fresh_path_text
            or transition_fresh.get("receipt_sha256") != fresh_sha
            or transition_fresh.get("observation_sha256") != fresh.get("metadata_sha256")
            or transition_fresh.get("legacy_restore_subject_identity") != legacy_identity
            or transition_fresh.get("restore_subject_identity") != full_identity):
        raise RuntimeError("P0 transition does not bind the exact current fresh metadata subject")
    transition_created_at = _require_nonfuture_timestamp(
        transition.get("created_at_unix"), label="P0 transition receipt")
    _transition_fresh_at, transition_fresh_age = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at P0 transition",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=transition_created_at)
    _require_recorded_age(transition_fresh.get("age_at_transition_seconds"), transition_fresh_age,
                         label="P0 transition receipt")
    restore_expected_bytes = fresh["expected_length_bytes"]
    restore_source_url = fresh["source_url"]
    restore_metadata_sha256 = fresh["metadata_sha256"]
    if ((requested_restore_expected_bytes is not None
         and requested_restore_expected_bytes != fresh["expected_length_bytes"])
            or (requested_restore_source_url is not None
                and _canonical_apple_restore_url(requested_restore_source_url)
                != _canonical_apple_restore_url(fresh["source_url"]))
            or (requested_restore_metadata_sha256 is not None
                and requested_restore_metadata_sha256 != fresh["metadata_sha256"])):
        raise RuntimeError("caller restore metadata arguments disagree with the signed lineage leaf")
    restore_metadata = validate_restore_metadata(
        fresh["source_url"], restore_expected_bytes, restore_metadata_sha256, fresh["final_url"])
    restore_metadata.update({
        "metadata_receipt_path": fresh_path_text,
        "metadata_receipt_sha256": fresh_sha,
        "metadata_observation_sha256": fresh["metadata_sha256"],
        "legacy_restore_subject_identity": legacy_identity,
        "restore_subject_identity": full_identity,
    })

    marker, _marker_raw, marker_sha, marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    _require_unchanged_pending_p0_marker(marker, creation)
    if transition.get("active_marker", {}).get("sha256") != marker_sha:
        raise RuntimeError("P1 admission requires the exact unchanged pending P0 marker boundary")
    if (creation.get("volume_uuid") != EXPECTED_PRODUCTION_VM_VOLUME_UUID
            or creation.get("safety_volume_uuid") != EXPECTED_PRODUCTION_SAFETY_VOLUME_UUID
            or creation.get("quota_bytes") != EXPECTED_PRODUCTION_VM_QUOTA_BYTES):
        raise RuntimeError("P1 creation receipt does not match the exact accepted production P0 contract")

    run_id = _new_run_id()
    p1_path = receipt_path or _fresh_artifact_path(VM_P1_ADMISSION_PREFIX, run_id)
    p1_path, p1_path_text = _repo_artifact_path(p1_path)
    if p1_path.exists():
        raise FileExistsError("P1 admission path already exists")
    review_path_text = _fresh_artifact_path(VM_P1_REVIEW_PREFIX, run_id).relative_to(policy.REPO).as_posix()
    with policy._acquire_storage_allocation_lock():
        observation = capture_host_observation()
        _require_fresh_timestamp(observation.get("captured_at_unix"),
                                 label="P1 live APFS observation",
                                 maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
        vm_uuid = creation.get("volume_uuid")
        blockers = _session_container_blockers(observation, volume_uuid=vm_uuid)
        try:
            vm_volume = _volume_row(observation, vm_uuid)
            quota_used = vm_volume.get("CapacityInUse")
            if not _integer(quota_used):
                blockers.append("VM volume APFS allocation/accounting is unknown")
                quota_used = None
            _volume_mount_info(observation, vm_uuid)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            blockers.append(f"VM volume mount or accounting identity failed: {error}")
            quota_used = None
        try:
            concurrent_measurement = measure_concurrent_growth_bound(volume_uuid=vm_uuid)
            concurrent_growth_bound = concurrent_measurement.get("observed_max_interval_growth_bytes")
            if not _integer(concurrent_growth_bound):
                concurrent_growth_bound = None
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            concurrent_measurement = {"status": "UNKNOWN", "failure": str(error)}
            concurrent_growth_bound = None
        try:
            session_free = conservative_session_unreserved_free_bytes(
                observation, volume_uuid=vm_uuid)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            session_free = None
            blockers.append(f"session-bounded shared free capacity is unavailable: {error}")
        result = evaluate_initial_plan(
            observation, restore_expected_bytes,
            vm_volume_uuid=vm_uuid, vm_quota_used_bytes=quota_used or 0,
            vm_quota_bytes=creation.get("quota_bytes"),
            safety_volume_uuid=creation.get("safety_volume_uuid"),
            safety_volume_in_use_bytes=creation.get("safety_volume_in_use_bytes"),
            concurrent_growth_bound_bytes=concurrent_growth_bound,
            shared_free_bytes_override=session_free)
        blockers.extend(result["blockers"])
        if blockers:
            update_active_session_marker(str(creation["session_id"]), state="FAILED",
                                         phase_id="P1_ADMISSION_FAILED")
            raise RuntimeError("P1 restore admission blocked: " + "; ".join(blockers))
        project_usage = policy.measure_project_usage()
        project_bytes = policy.conservative_project_usage_bytes(project_usage)
        project_usage["conservative_allocated_inode_deduplicated_bytes"] = project_bytes
        created_at = time.time()
        if not math.isfinite(created_at):
            raise RuntimeError("P1 admission clock is not finite")
        _fresh_captured_at, age_at_p1_admission = _require_fresh_timestamp(
            fresh.get("captured_at_unix"), label="fresh metadata at P1 admission sealing",
            maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=created_at)
        _require_fresh_timestamp(
            observation.get("captured_at_unix"), label="P1 live APFS observation at admission sealing",
            maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS, now=created_at)
        fresh_final, fresh_sha_final, fresh_identity_final = _read_latest_restore_metadata_snapshot(
            fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
        if (fresh_sha_final != fresh_sha or fresh_identity_final != fresh_identity
                or fresh_final.get("metadata_sha256") != fresh.get("metadata_sha256")):
            raise RuntimeError("fresh metadata bytes changed while P1 capacity admission was being prepared")
        p0_final = _accepted_p0_chain()
        lineage_final, _lineage_raw_final, lineage_sha_final, lineage_identity_final = (
            _read_signed_record_snapshot(lineage_absolute, "vm_metadata_refresh_lineage_hmac_sha256"))
        basis_final, _basis_raw_final, basis_sha_final, basis_identity_final = (
            _read_signed_record_snapshot(basis_absolute, "vm_restore_compatibility_basis_hmac_sha256"))
        transition_final, _transition_raw_final, transition_sha_final, transition_identity_final = (
            _read_signed_record_snapshot(transition_absolute, "vm_p0_revalidation_hmac_sha256"))
        creation_final, _creation_raw_final, creation_sha_final, creation_identity_final = (
            _read_signed_record_snapshot(creation_path, "vm_creation_hmac_sha256"))
        if (p0_final["p0_ancestry"] != p0["p0_ancestry"]
                or lineage_sha_final != lineage_sha or lineage_identity_final != lineage_identity
                or basis_sha_final != basis_sha or basis_identity_final != basis_identity
                or transition_sha_final != transition_sha or transition_identity_final != transition_identity
                or creation_sha_final != creation_sha or creation_identity_final != _creation_identity
                or lineage_final.get("receipt_path") != lineage_path_text
                or basis_final.get("receipt_path") != basis_path_text
                or transition_final.get("receipt_path") != transition_path_text
                or creation_final.get("session_id") != creation.get("session_id")):
            raise RuntimeError("P0/P1 ancestry bytes changed while P1 admission was being prepared")
        _basis_review_final, basis_review_sha_final = _verify_review_artifact(
            basis_review_absolute, artifact_path=basis_path_text, artifact_sha256=basis_sha,
            not_before_unix=basis.get("created_at_unix"))
        _transition_review_final, transition_review_sha_final = _verify_review_artifact(
            transition_review_absolute, artifact_path=transition_path_text,
            artifact_sha256=transition_sha, not_before_unix=transition.get("created_at_unix"))
        if (basis_review_sha_final != basis_review_sha
                or transition_review_sha_final != transition_review_sha):
            raise RuntimeError("independent review bytes changed while P1 admission was being prepared")
        if (_controller_identity() != current_controller
                or verify_fresh_g0c_review() != current_g0c):
            raise RuntimeError("controller or current G0C changed while P1 admission was being prepared")
        _marker_final, _marker_raw_final, marker_sha_final, marker_identity_final = (
            _read_signed_record_snapshot(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256"))
        if marker_sha_final != marker_sha or marker_identity_final != marker_identity:
            raise RuntimeError("active P0 marker changed while P1 admission was being prepared")
        created_at = time.time()
        if not math.isfinite(created_at):
            raise RuntimeError("P1 admission clock is not finite")
        _fresh_captured_at, age_at_p1_admission = _require_fresh_timestamp(
            fresh_final.get("captured_at_unix"), label="fresh metadata immediately before P1 sealing",
            maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=created_at)
        _require_fresh_timestamp(
            observation.get("captured_at_unix"), label="P1 live APFS observation immediately before sealing",
            maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS, now=created_at)
        record = {
            "schema_version": 1,
            "record_type": "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION",
            "mode": VM_MODE,
            "decision": "PASS",
            "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "authorized_phase": "P1",
            "p1_authorized": True,
            "restore_download_authorized": True,
            "p2_authorized": False,
            "vm_start_authorized": False,
            "run_id": run_id,
            "created_at_unix": created_at,
            "required_independent_review_path": review_path_text,
            "session_id": creation.get("session_id"),
            "controller_identity": current_controller,
            "g0c_identity": current_g0c,
            "historical_p0_controller_identity": p0["historical_controller_identity"],
            "historical_p0_g0c_identity": p0["historical_g0c_identity"],
            "probe_source_transition": probe_source_transition,
            "p0_ancestry": p0["p0_ancestry"],
            "volume_creation_receipt_path": creation_path_text,
            "volume_creation_receipt_sha256": creation_sha,
            "metadata_ancestor_path": p0["p0_ancestry"]["metadata_receipt_path"],
            "metadata_ancestor_sha256": p0["p0_ancestry"]["metadata_receipt_sha256"],
            "fresh_metadata_path": fresh_path_text,
            "fresh_metadata_receipt_sha256": fresh_sha,
            "fresh_metadata_observation_sha256": fresh["metadata_sha256"],
            "fresh_metadata_age_at_p1_admission_seconds": age_at_p1_admission,
            "restore_lineage_path": lineage_path_text,
            "restore_lineage_sha256": lineage_sha,
            "compatibility_basis_path": basis_path_text,
            "compatibility_basis_sha256": basis_sha,
            "compatibility_basis_review_path": basis_review_path_text,
            "compatibility_basis_review_sha256": basis_review_sha,
            "p0_transition_path": transition_path_text,
            "p0_transition_sha256": transition_sha,
            "p0_transition_review_path": transition_review_path_text,
            "p0_transition_review_sha256": transition_review_sha,
            "legacy_restore_subject_identity": legacy_identity,
            "restore_subject_identity": full_identity,
            "restore_subject": full_subject,
            "physical_store_uuid": EXPECTED_PHYSICAL_STORE_UUID,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "capacity_ceiling_bytes": observation["apfs_container"].get("CapacityCeiling"),
            "shared_container_free_bytes": session_free,
            "unreserved_free_ceiling_bytes": creation.get("unreserved_free_ceiling_bytes"),
            "vm_volume_uuid": vm_uuid,
            "vm_volume_quota_bytes": creation.get("quota_bytes"),
            "vm_volume_reserve_bytes": 0,
            "safety_volume_uuid": creation.get("safety_volume_uuid"),
            "safety_volume_in_use_bytes": creation.get("safety_volume_in_use_bytes"),
            "safety_container_uuid": EXPECTED_CONTAINER_UUID,
            "safety_physical_store_uuid": EXPECTED_PHYSICAL_STORE_UUID,
            "safety_volume_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "vm_volume_usage_bytes": quota_used,
            "vm_quota_headroom_bytes": creation.get("quota_bytes") - quota_used,
            "restore_expected_bytes": restore_expected_bytes,
            "restore_upstream_metadata": restore_metadata,
            "restore_metadata_receipt_sha256": fresh_sha,
            "phase_evaluation": result,
            "concurrent_growth_measurement": concurrent_measurement,
            "concurrent_growth_bound_bytes": concurrent_growth_bound,
            "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
            "normal_project_usage": project_usage,
            "normal_project_scope_bytes": project_bytes,
            "normal_project_policy": {
                "project_ceiling_bytes": policy.PROJECT_BUDGET,
                "normal_projected_free_min_bytes": policy.FREE_MINIMUM,
                "preferred_free_bytes": policy.FREE_PREFERRED,
                "emergency_free_stop_bytes": policy.FREE_HARD_STOP,
                "status": "MEASURED_SEPARATELY_NO_VM_CREDIT",
            },
            "operational_free_min_bytes": VM_OPERATIONAL_UNRESERVED_FREE_MIN,
            "operational_warning_free_bytes": VM_OPERATIONAL_WARNING_FREE,
            "live_observation": observation,
            "blockers": [],
            "warnings": ["P1 PASS authorizes only restore download and local verification to the bound VM volume",
                         "P2 requires a fresh independent admission and reviewer",
                         "no VM start, Gate, execution admission, BuildLease, R6 test/build, or R6 acceptance authority"],
            "receipt_path": p1_path_text,
        }
        sealed = _write_signed_json_unique(p1_path, record, "vm_admission_hmac_sha256")
        _p1_readback, _p1_raw, p1_sha, _p1_identity = _read_signed_record_snapshot(
            p1_path, "vm_admission_hmac_sha256")
        if not verify_signed_record(sealed, "vm_admission_hmac_sha256"):
            raise RuntimeError("new P1 admission signature failed its read-back verification")
        update_active_session_marker(
            str(creation["session_id"]), phase_id="P1",
            committed_growth_bytes=result["plan"]["phases"]["P2"]["growth_bytes"],
            p1_admission_path=p1_path, p1_admission_sha256=p1_sha,
            expected_marker_sha256=marker_sha)
        return sealed


def _verify_p1_download_authority(
        p1_admission_path: Path | None = None,
        p1_review_path: Path | None = None, *,
        require_freshness_renewal: bool = True,
        allow_consumed_attempt_id: str | None = None) -> dict[str, Any]:
    """Verify immutable P1 authority and, by default, its current freshness ticket."""
    marker, _marker_raw, marker_sha, _marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    bound_path_text = marker.get("p1_admission_path")
    bound_p1_sha = marker.get("p1_admission_sha256")
    if (marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P1"
            or not isinstance(bound_path_text, str)
            or not isinstance(bound_p1_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", bound_p1_sha)):
        raise RuntimeError("pending marker or unbound P1 label cannot authorize restore download")
    bound_path, canonical_p1_path_text = _repo_artifact_path(bound_path_text)
    if canonical_p1_path_text != bound_path_text:
        raise RuntimeError("active marker P1 admission path is not canonical")
    if p1_admission_path is not None:
        supplied_path, supplied_path_text = _repo_artifact_path(p1_admission_path)
        if supplied_path != bound_path or supplied_path_text != bound_path_text:
            raise RuntimeError("requested P1 receipt is not the exact marker-bound admission")
    admission, _admission_raw, admission_sha, _admission_identity = _read_signed_record_snapshot(
        bound_path, "vm_admission_hmac_sha256")
    if (admission_sha != bound_p1_sha
            or admission.get("receipt_path") != bound_path_text
            or admission.get("record_type") != "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION"
            or admission.get("mode") != VM_MODE
            or admission.get("decision") != "PASS"
            or admission.get("authorization") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or admission.get("authorized_phase") != "P1"
            or admission.get("p1_authorized") is not True
            or admission.get("restore_download_authorized") is not True
            or admission.get("p2_authorized") is not False
            or admission.get("vm_start_authorized") is not False):
        raise RuntimeError("restore download requires the exact signed download-only P1 outer authority")
    p1_created_at = _require_nonfuture_timestamp(
        admission.get("created_at_unix"), label="historical P1 admission")
    required_review_path = admission.get("required_independent_review_path")
    if not isinstance(required_review_path, str):
        raise RuntimeError("P1 admission does not name its required independent review")
    required_review_absolute, required_review_text = _repo_artifact_path(required_review_path)
    if required_review_text != required_review_path:
        raise RuntimeError("P1 independent-review path is not canonical")
    if p1_review_path is not None:
        supplied_review_path, supplied_review_text = _repo_artifact_path(p1_review_path)
        if (supplied_review_path != required_review_absolute
                or supplied_review_text != required_review_text):
            raise RuntimeError("requested P1 independent review differs from the bound review path")
    p1_review, p1_review_sha = _verify_review_artifact(
        required_review_absolute, artifact_path=bound_path_text, artifact_sha256=admission_sha,
        not_before_unix=admission.get("created_at_unix"))
    accepted_p1_path_text = VM_P1_ACCEPTED_ADMISSION_PATH.relative_to(policy.REPO).as_posix()
    accepted_p1_review_path_text = VM_P1_ACCEPTED_REVIEW_PATH.relative_to(policy.REPO).as_posix()
    if bound_path_text == accepted_p1_path_text:
        if (admission_sha != VM_P1_ACCEPTED_ADMISSION_SHA256
                or required_review_text != accepted_p1_review_path_text
                or p1_review_sha != VM_P1_ACCEPTED_REVIEW_SHA256
                or p1_review.get("findings") != []):
            raise RuntimeError("accepted historical P1 admission/review bytes differ from their pinned pair")

    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    p1_controller = admission.get("controller_identity")
    p1_g0c = admission.get("g0c_identity")
    if not isinstance(p1_controller, Mapping) or not isinstance(p1_g0c, Mapping):
        raise RuntimeError("P1 admission omits its historical controller/G0C provenance")
    _verify_historical_g0c_identity(
        p1_g0c, p1_controller, p1_path_text=bound_path_text,
        p1_sha256=admission_sha, p1_review_sha256=p1_review_sha,
        current_controller_identity=current_controller,
        current_g0c_identity=current_g0c)
    p0 = _accepted_p0_chain()
    if (admission.get("p0_ancestry") != p0["p0_ancestry"]
            or admission.get("historical_p0_controller_identity")
            != p0["historical_controller_identity"]
            or admission.get("historical_p0_g0c_identity") != p0["historical_g0c_identity"]
            or admission.get("metadata_ancestor_path")
            != p0["p0_ancestry"]["metadata_receipt_path"]
            or admission.get("metadata_ancestor_sha256")
            != p0["p0_ancestry"]["metadata_receipt_sha256"]):
        raise RuntimeError("P1 admission does not bind the exact immutable historical P0 chain")

    creation_path, creation_path_text = _repo_artifact_path(
        admission.get("volume_creation_receipt_path", ""))
    if (creation_path_text != p0["p0_ancestry"]["creation_receipt_path"]
            or admission.get("volume_creation_receipt_sha256")
            != p0["p0_ancestry"]["creation_receipt_sha256"]):
        raise RuntimeError("P1 creation receipt path is not the exact run-specific P0 ancestor")
    creation, _creation_raw, creation_sha, _creation_identity = _read_signed_record_snapshot(
        creation_path, "vm_creation_hmac_sha256")
    if (creation_sha != admission.get("volume_creation_receipt_sha256")
            or creation_sha != p0["p0_ancestry"]["creation_receipt_sha256"]
            or creation.get("session_id") != admission.get("session_id")
            or creation.get("volume_uuid") != admission.get("vm_volume_uuid")
            or creation.get("restore_metadata_receipt_sha256")
            != p0["p0_ancestry"]["metadata_receipt_sha256"]):
        raise RuntimeError("P1, run-specific creation receipt, and P0 metadata ancestry disagree")

    lineage_path, lineage_path_text = _repo_artifact_path(admission.get("restore_lineage_path", ""))
    lineage, _lineage_raw, lineage_sha, _lineage_identity = _read_signed_record_snapshot(
        lineage_path, "vm_metadata_refresh_lineage_hmac_sha256")
    _require_direct_p0_metadata_lineage(
        lineage, path_text=lineage_path_text, p0_ancestry=p0["p0_ancestry"])
    if (lineage_sha != admission.get("restore_lineage_sha256")
            or lineage.get("historical_controller_identity") != p0["historical_controller_identity"]
            or lineage.get("historical_g0c_identity") != p0["historical_g0c_identity"]
            or lineage.get("current_controller_identity") != p1_controller
            or lineage.get("current_g0c_identity") != p1_g0c):
        raise RuntimeError("P1 metadata lineage is not the exact historical direct-P0 PASS receipt")
    _require_nonfuture_timestamp(lineage.get("created_at_unix"), label="metadata refresh lineage")

    basis_path, basis_path_text = _repo_artifact_path(admission.get("compatibility_basis_path", ""))
    basis, _basis_raw, basis_sha, _basis_identity = _read_signed_record_snapshot(
        basis_path, "vm_restore_compatibility_basis_hmac_sha256")
    if (basis_sha != admission.get("compatibility_basis_sha256")
            or basis.get("receipt_path") != basis_path_text
            or basis.get("record_type") != "R6_VM_RESTORE_METADATA_COMPATIBILITY_BASIS"
            or basis.get("status") != "PASS"
            or basis.get("p0_ancestry") != p0["p0_ancestry"]
            or basis.get("lineage_path") != lineage_path_text
            or basis.get("lineage_sha256") != lineage_sha
            or basis.get("p0_hardware_fields_were_not_captured") is not True
            or basis.get("retroactive_p0_hardware_equivalence_claim") is not False
            or basis.get("decision") != "ADOPT_CURRENT_HARDWARE_COMPATIBILITY_FOR_P1"
            or basis.get("restore_download_authorized") is not False
            or basis.get("p1_authorized") is not False
            or basis.get("p2_authorized") is not False
            or basis.get("vm_start_authorized") is not False
            or basis.get("controller_identity") != p1_controller
            or basis.get("g0c_identity") != p1_g0c):
        raise RuntimeError("P1 compatibility basis is not the exact historical no-retroactive-claim basis")
    _require_nonfuture_timestamp(basis.get("created_at_unix"), label="hardware compatibility basis")
    basis_review_path, basis_review_path_text = _repo_artifact_path(
        admission.get("compatibility_basis_review_path", ""))
    if (basis_review_path_text != basis.get("review_path", basis_review_path_text)
            or basis_review_path_text != admission.get("compatibility_basis_review_path")):
        raise RuntimeError("P1 compatibility-basis review locator differs from its reviewed artifact")
    _basis_review, basis_review_sha = _verify_review_artifact(
        basis_review_path, artifact_path=basis_path_text, artifact_sha256=basis_sha,
        not_before_unix=basis.get("created_at_unix"))
    if basis_review_sha != admission.get("compatibility_basis_review_sha256"):
        raise RuntimeError("P1 compatibility-basis review bytes differ from the admission hash")

    transition_path, transition_path_text = _repo_artifact_path(admission.get("p0_transition_path", ""))
    transition, _transition_raw, transition_sha, _transition_identity = _read_signed_record_snapshot(
        transition_path, "vm_p0_revalidation_hmac_sha256")
    probe_source_transition = {
        "historical_p0_probe_sha256": p0["historical_controller_identity"]["restore_metadata_probe_sha256"],
        "current_probe_sha256": p1_controller["restore_metadata_probe_sha256"],
        "current_g0c_reviewed_current_probe": True,
    }
    if (transition_sha != admission.get("p0_transition_sha256")
            or transition.get("record_type") != "R6_VM_P0_REVALIDATED_FOR_P1"
            or transition.get("receipt_path") != transition_path_text
            or transition.get("decision") != "PASS"
            or transition.get("transition_scope") != "P0_COMPATIBILITY_REVALIDATION_ONLY"
            or transition.get("p0_ancestry") != p0["p0_ancestry"]
            or transition.get("historical_p0_controller_identity")
            != p0["historical_controller_identity"]
            or transition.get("historical_p0_g0c_identity") != p0["historical_g0c_identity"]
            or transition.get("restore_lineage") != {"path": lineage_path_text, "sha256": lineage_sha}
            or transition.get("compatibility_basis") != {
                "path": basis_path_text, "sha256": basis_sha,
                "review_path": basis_review_path_text, "review_sha256": basis_review_sha}
            or transition.get("probe_source_transition") != probe_source_transition
            or admission.get("probe_source_transition") != probe_source_transition
            or any(transition.get(field) is not False for field in (
                "p1_authorized", "p2_authorized", "vm_start_authorized",
                "restore_download_authorized"))
            or transition.get("current_controller_identity") != p1_controller
            or transition.get("current_g0c_identity") != p1_g0c):
        raise RuntimeError("P1 read-only transition is missing, changed, or carries execution authority")
    _require_nonfuture_timestamp(transition.get("created_at_unix"), label="P0 transition receipt")
    transition_review_path, transition_review_path_text = _repo_artifact_path(
        admission.get("p0_transition_review_path", ""))
    if transition_review_path_text != admission.get("p0_transition_review_path"):
        raise RuntimeError("P1 transition-review locator is not canonical")
    _transition_review, transition_review_sha = _verify_review_artifact(
        transition_review_path, artifact_path=transition_path_text,
        artifact_sha256=transition_sha, not_before_unix=transition.get("created_at_unix"))
    if transition_review_sha != admission.get("p0_transition_review_sha256"):
        raise RuntimeError("P1 transition review bytes differ from the admission hash")

    fresh_ref = lineage.get("fresh_observation")
    if not isinstance(fresh_ref, Mapping):
        raise RuntimeError("P1 lineage omits the fresh metadata leaf")
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_ref.get("receipt_path", ""))
    if (fresh_path_text != admission.get("fresh_metadata_path")
            or fresh_path_text != basis.get("fresh_metadata_path")
            or fresh_ref.get("receipt_sha256") != admission.get("fresh_metadata_receipt_sha256")
            or fresh_ref.get("receipt_sha256") != basis.get("fresh_metadata_sha256")):
        raise RuntimeError("P1, lineage, and compatibility basis fresh metadata locators/hashes disagree")
    fresh, fresh_sha, _fresh_identity = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=None,
        expected_controller_identity=p1_controller,
        expected_g0c_identity=p1_g0c)
    historical_fresh_at = _require_nonfuture_timestamp(
        fresh.get("captured_at_unix"), label="historical P1 metadata leaf")
    fresh_age_at_download_authorization = time.time() - historical_fresh_at
    if (fresh_sha != fresh_ref.get("receipt_sha256")
            or fresh_sha != admission.get("fresh_metadata_receipt_sha256")
            or fresh.get("metadata_sha256") != fresh_ref.get("metadata_observation_sha256")
            or fresh.get("metadata_sha256") != admission.get("fresh_metadata_observation_sha256")
            or fresh.get("metadata_sha256") != basis.get("fresh_metadata_observation_sha256")):
        raise RuntimeError("P1 fresh metadata receipt is no longer the exact fresh lineage leaf")
    fresh_host = _host_tuple(fresh.get("host"))
    if fresh_ref.get("host") != fresh_host:
        raise RuntimeError("lineage host tuple differs from its signed fresh metadata observation")
    legacy_subject = _legacy_restore_subject(fresh, fresh_host)
    complete_subject = _complete_restore_subject(legacy_subject, fresh)
    legacy_identity = _restore_subject_digest(legacy_subject, legacy=True)
    complete_identity = _restore_subject_digest(complete_subject)
    if (legacy_identity != p0["p0_ancestry"]["legacy_restore_subject_identity"]
            or legacy_identity != fresh_ref.get("legacy_restore_subject_identity")
            or legacy_identity != admission.get("legacy_restore_subject_identity")
            or complete_identity != admission.get("restore_subject_identity")
            or complete_identity != basis.get("restore_subject_identity")
            or complete_subject != admission.get("restore_subject")
            or complete_subject != basis.get("restore_subject")
            or _hardware_compatibility_tuple(fresh) != basis.get("adopted_hardware_compatibility")):
        raise RuntimeError("P1 stable subject or adopted hardware compatibility tuple changed")
    _fresh_at_p1, fresh_age_at_p1 = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at P1 admission timestamp",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=p1_created_at)
    _require_recorded_age(admission.get("fresh_metadata_age_at_p1_admission_seconds"),
                         fresh_age_at_p1, label="P1 admission")
    basis_created_at = _require_nonfuture_timestamp(
        basis.get("created_at_unix"), label="hardware compatibility basis")
    _fresh_at_basis, fresh_age_at_basis = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at compatibility basis",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=basis_created_at)
    _require_recorded_age(basis.get("fresh_metadata_age_at_basis_seconds"), fresh_age_at_basis,
                         label="hardware compatibility basis")
    lineage_created_at = _require_nonfuture_timestamp(
        lineage.get("created_at_unix"), label="metadata refresh lineage")
    _fresh_at_lineage, fresh_age_at_lineage = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at metadata lineage",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=lineage_created_at)
    _require_recorded_age(fresh_ref.get("age_at_lineage_creation_seconds"), fresh_age_at_lineage,
                         label="metadata refresh lineage")
    transition_fresh = transition.get("fresh_metadata")
    if (not isinstance(transition_fresh, Mapping)
            or transition_fresh.get("path") != fresh_path_text
            or transition_fresh.get("receipt_sha256") != fresh_sha
            or transition_fresh.get("observation_sha256") != fresh.get("metadata_sha256")
            or transition_fresh.get("legacy_restore_subject_identity") != legacy_identity
            or transition_fresh.get("restore_subject_identity") != complete_identity):
        raise RuntimeError("P0 transition no longer binds the exact fresh metadata subject")
    transition_created_at = _require_nonfuture_timestamp(
        transition.get("created_at_unix"), label="P0 transition receipt")
    _fresh_at_transition, fresh_age_at_transition = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="fresh metadata at P0 transition",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=transition_created_at)
    _require_recorded_age(transition_fresh.get("age_at_transition_seconds"), fresh_age_at_transition,
                         label="P0 transition receipt")

    metadata = admission.get("restore_upstream_metadata")
    if not isinstance(metadata, Mapping):
        raise RuntimeError("P1 admission omits its exact source/final restore URLs")
    restore_metadata = validate_restore_metadata(
        fresh.get("source_url"), fresh.get("expected_length_bytes"),
        fresh.get("metadata_sha256"), fresh.get("final_url"))
    for key, expected in restore_metadata.items():
        if metadata.get(key) != expected:
            raise RuntimeError(f"P1 restore metadata field differs from its signed fresh leaf: {key}")
    if (metadata.get("metadata_receipt_path") != fresh_path_text
            or metadata.get("metadata_receipt_sha256") != fresh_sha
            or metadata.get("metadata_observation_sha256") != fresh.get("metadata_sha256")
            or metadata.get("legacy_restore_subject_identity") != legacy_identity
            or metadata.get("restore_subject_identity") != complete_identity
            or admission.get("restore_expected_bytes") != fresh.get("expected_length_bytes")
            or admission.get("restore_metadata_receipt_sha256") != fresh_sha):
        raise RuntimeError("P1 nested metadata does not bind the exact current fresh leaf")
    if (admission.get("session_id") != marker.get("session_id")
            or admission.get("vm_volume_uuid") != marker.get("volume_uuid")
            or creation.get("session_id") != marker.get("session_id")
            or creation.get("volume_uuid") != marker.get("volume_uuid")
            or marker.get("container_uuid") != EXPECTED_CONTAINER_UUID
            or marker.get("quota_bytes") != admission.get("vm_volume_quota_bytes")
            or marker.get("volume_name") != creation.get("volume_name")
            or marker.get("safety_volume_uuid") != admission.get("safety_volume_uuid")
            or marker.get("safety_volume_in_use_bytes") != admission.get("safety_volume_in_use_bytes")
            or marker.get("safety_reserve_bytes") != VM_PROTECTED_CAPACITY_RESERVE_BYTES
            or marker.get("safety_container_uuid") != EXPECTED_CONTAINER_UUID
            or marker.get("safety_physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID
            or marker.get("p1_admission_path") != bound_path_text
            or marker.get("p1_admission_sha256") != admission_sha):
        raise RuntimeError("P1, exact creation receipt, and marker session/volume/hash bindings disagree")
    authority = {
        "admission": admission,
        "p1_path": bound_path,
        "p1_path_text": bound_path_text,
        "p1_sha256": admission_sha,
        "p1_review": p1_review,
        "p1_review_path": required_review_absolute,
        "p1_review_path_text": required_review_text,
        "p1_review_sha256": p1_review_sha,
        "metadata": dict(metadata),
        "fresh_metadata": fresh,
        "fresh_metadata_receipt_sha256": fresh_sha,
        "fresh_metadata_age_seconds": fresh_age_at_download_authorization,
        "creation": creation,
        "creation_path": creation_path,
        "creation_path_text": creation_path_text,
        "lineage_sha256": lineage_sha,
        "basis_sha256": basis_sha,
        "basis_review_sha256": basis_review_sha,
        "transition_sha256": transition_sha,
        "transition_review_sha256": transition_review_sha,
        "compatibility_basis": basis,
        "p0_transition": transition,
        "lineage": lineage,
        "historical_controller_identity": dict(p1_controller),
        "historical_g0c_identity": dict(p1_g0c),
        "current_controller_identity": current_controller,
        "g0c_identity": current_g0c,
        "marker": marker,
        "marker_sha256": marker_sha,
    }
    if require_freshness_renewal:
        authority.update(_verify_p1_freshness_renewal(
            authority, allow_consumed_attempt_id=allow_consumed_attempt_id))
    return authority


def _require_same_p1_restore_subject(fresh: Mapping[str, Any],
                                     authority: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute and exactly compare the complete stable restore subject."""
    admission = authority.get("admission")
    basis = authority.get("compatibility_basis")
    if not isinstance(admission, Mapping) or not isinstance(basis, Mapping):
        raise RuntimeError("P1 authority omits its immutable subject or compatibility basis")
    try:
        host = _host_tuple(fresh.get("host"))
        legacy_subject = _legacy_restore_subject(fresh, host)
        subject = _complete_restore_subject(legacy_subject, fresh)
        subject_identity = _restore_subject_digest(subject)
        hardware_tuple = _hardware_compatibility_tuple(fresh)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"RESTORE SUBJECT CHANGED — STOP: {error}") from error
    if (subject_identity != admission.get("restore_subject_identity")
            or subject != admission.get("restore_subject")
            or subject_identity != basis.get("restore_subject_identity")
            or subject != basis.get("restore_subject")
            or hardware_tuple != basis.get("adopted_hardware_compatibility")):
        raise RuntimeError("RESTORE SUBJECT CHANGED — STOP")
    return {"restore_subject_identity": subject_identity, "restore_subject": subject,
            "hardware_compatibility": hardware_tuple}


def _capture_p1_renewal_live_storage(authority: Mapping[str, Any]) -> dict[str, Any]:
    """Capture current session/APFS state and require the restore destination to be empty."""
    admission = authority.get("admission")
    if not isinstance(admission, Mapping):
        raise RuntimeError("P1 renewal is missing its immutable admission")
    session_id = admission.get("session_id")
    volume_uuid = admission.get("vm_volume_uuid")
    expected_bytes = admission.get("restore_expected_bytes")
    if (not isinstance(session_id, str) or not isinstance(volume_uuid, str)
            or not _integer(expected_bytes, minimum=1)):
        raise RuntimeError("P1 renewal is missing exact session, volume, or restore-size identity")
    observation = capture_host_observation()
    _require_fresh_timestamp(observation.get("captured_at_unix"),
                             label="P1 freshness renewal live APFS observation",
                             maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
    blockers = _session_container_blockers(observation, volume_uuid=volume_uuid)
    try:
        mount = _volume_mount_info(observation, volume_uuid)
        volume = _volume_row(observation, volume_uuid)
        quota_used = volume.get("CapacityInUse")
        if not _integer(quota_used):
            raise ValueError("VM volume CapacityInUse is unknown")
        unreserved_free = conservative_session_unreserved_free_bytes(
            observation, volume_uuid=volume_uuid)
        sample = _check_runtime_phase_sample(
            observation, volume_uuid=volume_uuid,
            initial_quota_used_bytes=quota_used,
            maximum_phase_growth_bytes=expected_bytes + VM_P1_METADATA_MAX_BYTES,
            concurrent_growth_bytes=(admission.get("concurrent_growth_bound_bytes")
                                     if _integer(admission.get("concurrent_growth_bound_bytes")) else 0),
            session_id=session_id)
        blockers.extend(sample["blockers"])
        root_fd = policy._open_directory_nofollow(Path(mount["mount_point"]))
        try:
            entries = _restore_payload_entries(root_fd)
        finally:
            os.close(root_fd)
        if entries:
            blockers.append("restore body or partial payload already exists on the P1 VM volume")
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"P1 freshness renewal live storage is unavailable: {error}") from error
    if blockers:
        raise RuntimeError("P1 freshness renewal live storage blocked: " + "; ".join(blockers))
    marker = authority.get("marker")
    if not isinstance(marker, Mapping):
        raise RuntimeError("P1 freshness renewal has no signed active marker")
    return {
        "captured_at_unix": observation["captured_at_unix"],
        "session_id": session_id,
        "vm_volume_uuid": volume_uuid,
        "safety_volume_uuid": marker.get("safety_volume_uuid"),
        "container_uuid": EXPECTED_CONTAINER_UUID,
        "physical_store_uuid": EXPECTED_PHYSICAL_STORE_UUID,
        "quota_bytes": marker.get("quota_bytes"),
        "safety_reserve_bytes": marker.get("safety_reserve_bytes"),
        "unreserved_free_bytes": unreserved_free,
        "vm_volume_used_bytes": quota_used,
        "mount": mount,
        "restore_payload_absent": True,
        "observation": dict(observation),
        "capacity_sample": sample,
    }


def _verify_p1_freshness_renewal(
        authority: Mapping[str, Any], *,
        allow_consumed_attempt_id: str | None = None) -> dict[str, Any]:
    """Verify the marker's exact signed same-subject freshness receipt."""
    marker = authority.get("marker")
    admission = authority.get("admission")
    basis = authority.get("compatibility_basis")
    if not isinstance(marker, Mapping) or not isinstance(admission, Mapping) or not isinstance(basis, Mapping):
        raise RuntimeError("P1 freshness renewal lacks its marker-bound authority chain")
    binding = marker.get("p1_freshness_renewal")
    if not isinstance(binding, Mapping):
        raise RuntimeError("P1 restore download requires a marker-bound freshness renewal")
    binding_fields = {
        "path", "sha256", "sequence", "predecessor_path", "predecessor_sha256",
        "use_state", "attempt_id", "claimed_at_unix", "body_start_evidence",
    }
    if set(binding) != binding_fields:
        raise RuntimeError("P1 freshness renewal marker binding is malformed")
    use_state = binding.get("use_state")
    if allow_consumed_attempt_id is None:
        if use_state != "READY":
            raise RuntimeError("P1 freshness renewal is already consumed or unavailable")
    elif (use_state != "CONSUMED" or binding.get("attempt_id") != allow_consumed_attempt_id):
        raise RuntimeError("P1 freshness renewal is not the exact consumed download attempt")
    renewal_path, renewal_path_text = _repo_artifact_path(binding.get("path", ""))
    if renewal_path_text != binding.get("path"):
        raise RuntimeError("P1 freshness renewal path is not canonical")
    renewal, _renewal_raw, renewal_sha, _renewal_identity = _read_signed_record_snapshot(
        renewal_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD)
    expected_fields = {
        "schema_version", "mode", "record_type", "decision", "authority_effect", "authority_statement",
        "renewal_scope", "retained_authority", "p1_authorized", "restore_download_authorized",
        "p2_authorized", "vm_start_authorized", "session_id", "vm_volume_uuid",
        "original_p1", "original_p1_review", "p0_ancestry", "original_lineage",
        "compatibility_basis", "p0_transition", "historical_controller_identity",
        "historical_g0c_identity", "current_controller_identity", "current_g0c_identity",
        "marker_before", "fresh_metadata", "restore_subject_identity", "restore_subject",
        "subject_equality", "live_storage_observation", "sequence", "predecessor_path",
        "predecessor_sha256",
        "created_at_unix", "receipt_path", VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD,
    }
    if (set(renewal) != expected_fields
            or renewal.get("schema_version") != 1
            or renewal.get("mode") != VM_MODE
            or renewal.get("record_type") != "R6_VM_P1_FRESHNESS_RENEWAL"
            or renewal.get("decision") != "PASS_P1_FRESHNESS_REVALIDATED"
            or renewal.get("authority_effect") != "REVALIDATES_EXECUTION_TIME_FRESHNESS_ONLY"
            or renewal.get("authority_statement") != (
                "the existing P1 authority remains the authority; this fresh observation "
                "re-establishes its execution-time metadata freshness.")
            or renewal.get("renewal_scope") != "EXECUTION_TIME_FRESHNESS_ONLY"
            or renewal.get("retained_authority") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or any(renewal.get(field) is not False for field in (
                "p1_authorized", "restore_download_authorized", "p2_authorized", "vm_start_authorized"))
            or renewal.get("receipt_path") != renewal_path_text
            or renewal_sha != binding.get("sha256")
            or renewal.get("sequence") != binding.get("sequence")
            or renewal.get("predecessor_path") != binding.get("predecessor_path")
            or renewal.get("predecessor_sha256") != binding.get("predecessor_sha256")
            or renewal.get("session_id") != marker.get("session_id")
            or renewal.get("vm_volume_uuid") != marker.get("volume_uuid")
            or renewal.get("original_p1") != {
                "path": authority.get("p1_path_text"), "sha256": authority.get("p1_sha256")}
            or renewal.get("p0_ancestry") != admission.get("p0_ancestry")
            or renewal.get("original_lineage") != {
                "path": admission.get("restore_lineage_path"),
                "sha256": admission.get("restore_lineage_sha256")}
            or renewal.get("compatibility_basis") != {
                "path": admission.get("compatibility_basis_path"),
                "sha256": admission.get("compatibility_basis_sha256"),
                "review_path": admission.get("compatibility_basis_review_path"),
                "review_sha256": admission.get("compatibility_basis_review_sha256")}
            or renewal.get("p0_transition") != {
                "path": admission.get("p0_transition_path"),
                "sha256": admission.get("p0_transition_sha256"),
                "review_path": admission.get("p0_transition_review_path"),
                "review_sha256": admission.get("p0_transition_review_sha256")}
            or renewal.get("historical_controller_identity") != authority.get("historical_controller_identity")
            or renewal.get("historical_g0c_identity") != authority.get("historical_g0c_identity")
            or renewal.get("current_controller_identity") != _controller_identity()
            or renewal.get("current_controller_identity") != authority.get("current_controller_identity")
            or renewal.get("current_g0c_identity") != authority.get("g0c_identity")
            or renewal.get("current_g0c_identity") != verify_fresh_g0c_review()):
        raise RuntimeError("P1 freshness renewal receipt is invalid or exceeds its immutable authority bindings")
    sequence = renewal.get("sequence")
    predecessor_path_text = renewal.get("predecessor_path")
    predecessor_sha256 = renewal.get("predecessor_sha256")
    if (not _integer(sequence, minimum=1)
            or ((predecessor_path_text is None) != (predecessor_sha256 is None))
            or (predecessor_sha256 is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(predecessor_sha256)))):
        raise RuntimeError("P1 freshness renewal predecessor binding is malformed")
    if sequence == 1:
        if predecessor_path_text is not None or predecessor_sha256 is not None:
            raise RuntimeError("first P1 freshness renewal cannot have a predecessor")
    else:
        if not isinstance(predecessor_path_text, str):
            raise RuntimeError("subsequent P1 freshness renewal omits its predecessor path")
        predecessor_path, canonical_predecessor_path = _repo_artifact_path(predecessor_path_text)
        if canonical_predecessor_path != predecessor_path_text:
            raise RuntimeError("P1 freshness renewal predecessor path is not canonical")
        predecessor, _predecessor_raw, predecessor_read_sha, _predecessor_identity = (
            _read_signed_record_snapshot(
                predecessor_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD))
        if (set(predecessor) != expected_fields
                or predecessor_read_sha != predecessor_sha256
                or predecessor.get("record_type") != "R6_VM_P1_FRESHNESS_RENEWAL"
                or predecessor.get("receipt_path") != predecessor_path_text
                or predecessor.get("sequence") != sequence - 1
                or predecessor.get("session_id") != renewal.get("session_id")
                or predecessor.get("vm_volume_uuid") != renewal.get("vm_volume_uuid")
                or predecessor.get("original_p1") != renewal.get("original_p1")):
            raise RuntimeError("P1 freshness renewal does not bind the exact preceding receipt path and hash")
        previous_sequence = predecessor.get("sequence")
        previous_path = predecessor.get("predecessor_path")
        previous_sha = predecessor.get("predecessor_sha256")
        if (not _integer(previous_sequence, minimum=1)
                or ((previous_path is None) != (previous_sha is None))):
            raise RuntimeError("predecessor renewal binding is malformed")
        if previous_sequence == 1:
            if previous_path is not None or previous_sha is not None:
                raise RuntimeError("first predecessor renewal contains a predecessor binding")
        else:
            if (not isinstance(previous_path, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(previous_sha))):
                raise RuntimeError("predecessor renewal does not bind its own predecessor path and hash")
            _previous_predecessor_path, previous_path_text = _repo_artifact_path(previous_path)
            if previous_path_text != previous_path:
                raise RuntimeError("predecessor renewal path is not canonical")
    marker_before = renewal.get("marker_before")
    if (not isinstance(marker_before, Mapping)
            or marker_before.get("state") != "ACTIVE"
            or marker_before.get("phase_id") != "P1"
            or marker_before.get("session_id") != marker.get("session_id")
            or marker_before.get("volume_uuid") != marker.get("volume_uuid")
            or marker_before.get("p1_binding") != {
                "path": authority.get("p1_path_text"), "sha256": authority.get("p1_sha256")}
            or not re.fullmatch(r"[0-9a-f]{64}", str(marker_before.get("sha256")))):
        raise RuntimeError("P1 freshness renewal was sealed against a different marker boundary")
    original_review = renewal.get("original_p1_review")
    if (not isinstance(original_review, Mapping)
            or original_review.get("path") != authority.get("p1_review_path_text")
            or original_review.get("sha256") != authority.get("p1_review_sha256")):
        raise RuntimeError("P1 freshness renewal does not bind the exact historical independent review")
    fresh_ref = renewal.get("fresh_metadata")
    if not isinstance(fresh_ref, Mapping):
        raise RuntimeError("P1 freshness renewal omits its fresh metadata leaf")
    fresh_path, fresh_path_text = _repo_artifact_path(fresh_ref.get("path", ""))
    if fresh_path_text != fresh_ref.get("path"):
        raise RuntimeError("renewal metadata path is not canonical")
    fresh, fresh_sha, _fresh_identity = _read_latest_restore_metadata_snapshot(
        fresh_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS,
        expected_controller_identity=renewal.get("current_controller_identity"),
        expected_g0c_identity=renewal.get("current_g0c_identity"))
    verifier_entry_at = time.time()
    captured_at, age_now = _require_fresh_timestamp(
        fresh.get("captured_at_unix"), label="P1 renewal metadata at verifier entry",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS,
        now=verifier_entry_at)
    subject = _require_same_p1_restore_subject(fresh, authority)
    created_at = _require_nonfuture_timestamp(renewal.get("created_at_unix"),
                                               label="P1 freshness renewal")
    age_at_renewal = created_at - captured_at
    recorded_age_at_renewal = fresh_ref.get("age_at_renewal_seconds")
    if (not isinstance(recorded_age_at_renewal, (int, float))
            or isinstance(recorded_age_at_renewal, bool)):
        raise RuntimeError("P1 freshness renewal metadata age is malformed")
    try:
        recorded_age_at_renewal_value = float(recorded_age_at_renewal)
    except (OverflowError, TypeError, ValueError) as error:
        raise RuntimeError("P1 freshness renewal metadata age is malformed") from error
    if (not math.isfinite(age_at_renewal) or age_at_renewal < 0
            or age_at_renewal > VM_MAX_RESTORE_METADATA_AGE_SECONDS
            or not math.isfinite(recorded_age_at_renewal_value)
            or fresh_sha != fresh_ref.get("receipt_sha256")
            or fresh.get("metadata_sha256") != fresh_ref.get("observation_sha256")
            or captured_at != fresh_ref.get("captured_at_unix")
            or abs(age_at_renewal - recorded_age_at_renewal_value) > 0.001
            or subject.get("restore_subject_identity") != renewal.get("restore_subject_identity")
            or subject.get("restore_subject") != renewal.get("restore_subject")
            or renewal.get("subject_equality") != {
                "p1_admission_equal": True, "compatibility_basis_equal": True}):
        raise RuntimeError("P1 freshness renewal metadata, age, or stable subject binding is invalid")
    live = renewal.get("live_storage_observation")
    if (not isinstance(live, Mapping)
            or live.get("session_id") != marker.get("session_id")
            or live.get("vm_volume_uuid") != marker.get("volume_uuid")
            or live.get("safety_volume_uuid") != marker.get("safety_volume_uuid")
            or live.get("container_uuid") != EXPECTED_CONTAINER_UUID
            or live.get("physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID
            or live.get("restore_payload_absent") is not True
            or not _integer(live.get("unreserved_free_bytes"))):
        raise RuntimeError("P1 freshness renewal does not bind valid live storage/session evidence")
    return {
        "renewal": renewal,
        "renewal_path": renewal_path,
        "renewal_path_text": renewal_path_text,
        "renewal_sha256": renewal_sha,
        "renewal_binding": dict(binding),
        "renewal_fresh_metadata": fresh,
        "renewal_fresh_metadata_path": fresh_path,
        "renewal_fresh_metadata_path_text": fresh_path_text,
        "renewal_fresh_metadata_receipt_sha256": fresh_sha,
        "renewal_fresh_metadata_age_seconds": age_now,
        "renewal_fresh_metadata_verifier_entry_at_unix": verifier_entry_at,
        "renewal_fresh_metadata_verifier_entry_age_seconds": age_now,
        "renewal_subject": subject["restore_subject"],
    }


def create_p1_freshness_renewal(
        fresh_metadata_path: Path, *, receipt_path: Path | None = None
        ) -> dict[str, Any]:
    """Seal one same-subject freshness receipt under the immutable ACTIVE/P1 authority."""
    with policy._acquire_storage_allocation_lock():
        authority = _verify_p1_download_authority(require_freshness_renewal=False)
        metadata_path, metadata_path_text = _repo_artifact_path(fresh_metadata_path)
        current_controller = _controller_identity()
        current_g0c = verify_fresh_g0c_review()
        fresh, fresh_sha, fresh_identity = _read_latest_restore_metadata_snapshot(
            metadata_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS,
            expected_controller_identity=current_controller,
            expected_g0c_identity=current_g0c)
        subject = _require_same_p1_restore_subject(fresh, authority)
        live = _capture_p1_renewal_live_storage(authority)
        marker, _marker_raw, marker_sha, _marker_identity = _read_signed_record_snapshot(
            VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker_sha != authority.get("marker_sha256")
                or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P1"
                or marker.get("p1_admission_path") != authority.get("p1_path_text")
                or marker.get("p1_admission_sha256") != authority.get("p1_sha256")):
            raise RuntimeError("P1 marker changed while the freshness renewal was being prepared")
        previous = marker.get("p1_freshness_renewal")
        if previous is None:
            sequence = 1
            predecessor_path = None
            predecessor_sha256 = None
        elif isinstance(previous, Mapping):
            previous_sha256 = previous.get("sha256")
            previous_sequence = previous.get("sequence")
            previous_path, previous_path_text = _repo_artifact_path(previous.get("path", ""))
            if (previous_path_text != previous.get("path")
                    or not isinstance(previous_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", previous_sha256)
                    or not _integer(previous_sequence, minimum=1)):
                raise RuntimeError("previous P1 freshness renewal marker binding is malformed")
            previous_record, _previous_raw, previous_read_sha, _previous_identity = (
                _read_signed_record_snapshot(previous_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD))
            if (previous_read_sha != previous_sha256
                    or previous_record.get("record_type") != "R6_VM_P1_FRESHNESS_RENEWAL"
                    or previous_record.get("receipt_path") != previous_path_text
                    or previous_record.get("sequence") != previous_sequence
                    or previous_record.get("predecessor_path") != previous.get("predecessor_path")
                    or previous_record.get("predecessor_sha256")
                    != previous.get("predecessor_sha256")
                    or previous_record.get("session_id") != authority.get("admission", {}).get("session_id")
                    or previous_record.get("vm_volume_uuid") != authority.get("admission", {}).get("vm_volume_uuid")
                    or previous_record.get("original_p1") != {
                        "path": authority.get("p1_path_text"),
                        "sha256": authority.get("p1_sha256")}):
                raise RuntimeError("previous P1 freshness renewal bytes changed")
            if previous.get("use_state") == "READY":
                prior_fresh = previous_record.get("fresh_metadata")
                prior_capture = (prior_fresh.get("captured_at_unix")
                                 if isinstance(prior_fresh, Mapping) else None)
                _require_nonfuture_timestamp(
                    prior_capture, label="existing unconsumed P1 freshness renewal")
                prior_age = time.time() - float(prior_capture)
                if not math.isfinite(prior_age) or prior_age < 0:
                    raise RuntimeError("existing P1 freshness renewal timestamp is invalid")
                if prior_age <= VM_MAX_RESTORE_METADATA_AGE_SECONDS:
                    raise RuntimeError("an unconsumed, unexpired P1 freshness renewal cannot be replaced")
            elif previous.get("use_state") != "CONSUMED":
                raise RuntimeError("previous P1 freshness renewal has an unknown use state")
            sequence = previous_sequence + 1
            predecessor_path = previous_path_text
            predecessor_sha256 = previous_sha256
        else:
            raise RuntimeError("P1 freshness renewal marker binding is malformed")
        run_id = _new_run_id()
        output_path = receipt_path or _fresh_artifact_path(VM_P1_FRESHNESS_RENEWAL_PREFIX, run_id)
        output_path, output_path_text = _repo_artifact_path(output_path)
        if output_path.exists():
            raise FileExistsError("P1 freshness renewal receipt path already exists")
        captured_at, age_at_renewal = _require_fresh_timestamp(
            fresh.get("captured_at_unix"), label="fresh metadata at P1 renewal seal",
            maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS)
        created_at = time.time()
        age_at_renewal = created_at - captured_at
        if (not math.isfinite(created_at) or not math.isfinite(age_at_renewal)
                or age_at_renewal < 0 or age_at_renewal > VM_MAX_RESTORE_METADATA_AGE_SECONDS):
            raise RuntimeError("fresh metadata became stale before P1 renewal sealing")
        monotonic_at_seal = time.monotonic()
        fresh_again, fresh_sha_again, fresh_identity_again = _read_latest_restore_metadata_snapshot(
            metadata_path, maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS,
            expected_controller_identity=current_controller,
            expected_g0c_identity=current_g0c)
        marker_again, _marker_again_raw, marker_sha_again, _marker_again_identity = (
            _read_signed_record_snapshot(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256"))
        if (fresh_sha_again != fresh_sha or fresh_identity_again != fresh_identity
                or fresh_again.get("metadata_sha256") != fresh.get("metadata_sha256")
                or marker_sha_again != marker_sha or marker_again != marker
                or _controller_identity() != current_controller
                or verify_fresh_g0c_review() != current_g0c):
            raise RuntimeError("P1 authority, marker, metadata, controller, or G0C changed while sealing renewal")
        _require_fresh_timestamp(fresh_again.get("captured_at_unix"),
                                 label="fresh metadata at final P1 renewal seal",
                                 maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS,
                                 now=created_at)
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "record_type": "R6_VM_P1_FRESHNESS_RENEWAL",
            "decision": "PASS_P1_FRESHNESS_REVALIDATED",
            "authority_effect": "REVALIDATES_EXECUTION_TIME_FRESHNESS_ONLY",
            "authority_statement": (
                "the existing P1 authority remains the authority; this fresh observation "
                "re-establishes its execution-time metadata freshness."),
            "renewal_scope": "EXECUTION_TIME_FRESHNESS_ONLY",
            "retained_authority": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "p1_authorized": False,
            "restore_download_authorized": False,
            "p2_authorized": False,
            "vm_start_authorized": False,
            "session_id": authority["admission"]["session_id"],
            "vm_volume_uuid": authority["admission"]["vm_volume_uuid"],
            "original_p1": {"path": authority["p1_path_text"], "sha256": authority["p1_sha256"]},
            "original_p1_review": {
                "path": authority["p1_review_path_text"], "sha256": authority["p1_review_sha256"]},
            "p0_ancestry": authority["admission"]["p0_ancestry"],
            "original_lineage": {
                "path": authority["admission"]["restore_lineage_path"],
                "sha256": authority["admission"]["restore_lineage_sha256"]},
            "compatibility_basis": {
                "path": authority["admission"]["compatibility_basis_path"],
                "sha256": authority["admission"]["compatibility_basis_sha256"],
                "review_path": authority["admission"]["compatibility_basis_review_path"],
                "review_sha256": authority["admission"]["compatibility_basis_review_sha256"]},
            "p0_transition": {
                "path": authority["admission"]["p0_transition_path"],
                "sha256": authority["admission"]["p0_transition_sha256"],
                "review_path": authority["admission"]["p0_transition_review_path"],
                "review_sha256": authority["admission"]["p0_transition_review_sha256"]},
            "historical_controller_identity": authority["historical_controller_identity"],
            "historical_g0c_identity": authority["historical_g0c_identity"],
            "current_controller_identity": current_controller,
            "current_g0c_identity": current_g0c,
            "marker_before": {
                "state": marker["state"], "phase_id": marker["phase_id"],
                "session_id": marker["session_id"], "volume_uuid": marker["volume_uuid"],
                "sha256": marker_sha,
                "p1_binding": {"path": marker["p1_admission_path"],
                               "sha256": marker["p1_admission_sha256"]}},
            "fresh_metadata": {
                "path": metadata_path_text,
                "receipt_sha256": fresh_sha,
                "observation_sha256": fresh["metadata_sha256"],
                "captured_at_unix": captured_at,
                "age_at_renewal_seconds": age_at_renewal},
            "restore_subject_identity": subject["restore_subject_identity"],
            "restore_subject": subject["restore_subject"],
            "subject_equality": {"p1_admission_equal": True,
                                 "compatibility_basis_equal": True},
            "live_storage_observation": live,
            "sequence": sequence,
            "predecessor_path": predecessor_path,
            "predecessor_sha256": predecessor_sha256,
            "created_at_unix": created_at,
            "receipt_path": output_path_text,
        }
        sealed = _write_signed_json_unique(
            output_path, record, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD)
        sealed_sha = _file_sha256(output_path)
        readback, _readback_raw, readback_sha, _readback_identity = _read_signed_record_snapshot(
            output_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD)
        if readback_sha != sealed_sha or readback != sealed:
            raise RuntimeError("P1 freshness renewal did not pass exact signed read-back")
        marker_binding = {
            "path": output_path_text,
            "sha256": sealed_sha,
            "sequence": sequence,
            "predecessor_path": predecessor_path,
            "predecessor_sha256": predecessor_sha256,
            "use_state": "READY",
            "attempt_id": None,
            "claimed_at_unix": None,
            "body_start_evidence": None,
        }
        update_active_session_marker(
            str(authority["admission"]["session_id"]),
            p1_freshness_renewal=marker_binding,
            expected_marker_sha256=marker_sha)
        return {
            "renewal": sealed,
            "renewal_path": output_path,
            "renewal_path_text": output_path_text,
            "renewal_sha256": sealed_sha,
            "fresh_metadata": fresh,
            "fresh_metadata_path": metadata_path,
            "fresh_metadata_path_text": metadata_path_text,
            "fresh_metadata_receipt_sha256": fresh_sha,
            "age_at_renewal_seconds": age_at_renewal,
            "monotonic_at_seal": monotonic_at_seal,
            "sequence": sequence,
            "predecessor_path": predecessor_path,
        }


def calculate_stop_cushion(*, maximum_write_rate_bytes_per_second: int | None,
                           maximum_monitor_gap_microseconds: int | None,
                           maximum_stop_latency_microseconds: int | None,
                           concurrent_growth_during_stop_bytes: int | None) -> dict[str, Any]:
    values = (maximum_write_rate_bytes_per_second, maximum_monitor_gap_microseconds,
              maximum_stop_latency_microseconds)
    blockers: list[str] = []
    if (not all(_integer(value) for value in values)
            or (concurrent_growth_during_stop_bytes is not None
                and not _integer(concurrent_growth_during_stop_bytes))):
        blockers.append("stop-response inputs must be bounded non-negative integer measurements")
        return {"admitted": False, "required_cushion_bytes": None, "blockers": blockers}
    assert maximum_write_rate_bytes_per_second is not None
    assert maximum_monitor_gap_microseconds is not None
    assert maximum_stop_latency_microseconds is not None
    detection_and_stop_us = maximum_monitor_gap_microseconds + maximum_stop_latency_microseconds
    write_cushion = (maximum_write_rate_bytes_per_second * detection_and_stop_us + 999_999) // 1_000_000
    # Unrelated-container growth observed during one stop interval is useful
    # telemetry, but cannot bound future writers or change admission authority.
    required = write_cushion
    if required > VM_STOP_CUSHION_BYTES:
        blockers.append("measured VM stop-response estimate exceeds the 3 GiB warning-to-floor margin")
    return {
        "maximum_write_rate_bytes_per_second": maximum_write_rate_bytes_per_second,
        "maximum_monitor_gap_microseconds": maximum_monitor_gap_microseconds,
        "maximum_stop_latency_microseconds": maximum_stop_latency_microseconds,
        "concurrent_growth_during_stop_bytes": concurrent_growth_during_stop_bytes,
        "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
        "write_growth_during_detection_and_stop_bytes": write_cushion,
        "required_cushion_bytes": required,
        "available_cushion_bytes": VM_STOP_CUSHION_BYTES,
        "response_estimate_scope": "OPERATIONAL_ONLY_NOT_A_FUTURE_GROWTH_BOUND",
        "admitted": not blockers,
        "blockers": blockers,
    }


def _derive_stop_safety_measurements(evidence_path: Path) -> dict[str, Any]:
    """Derive the profile only from signed controller-produced monitor receipts."""
    raw = policy._read_file_nofollow(evidence_path, maximum_bytes=8 * 1024 * 1024)
    evidence = json.loads(raw)
    if (isinstance(evidence, Mapping)
            and verify_signed_record(evidence, "vm_phase_monitor_hmac_sha256")):
        runs = [evidence]
    else:
        runs = evidence.get("runs") if isinstance(evidence, Mapping) else None
    if not isinstance(runs, list) or not runs or len(runs) > 16:
        raise RuntimeError("stop-safety evidence must contain 1 to 16 signed VM monitor runs")
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    current_build = _host_build_version()
    sample_count = 0
    maximum_write_rate = 0
    maximum_gap_us = 0
    maximum_stop_latency_us = 0
    maximum_concurrent_stop_bytes: int | None = None
    maximum_concurrent_interval_bytes = 0
    evidence_volume_uuid: str | None = None
    for run in runs:
        if (not isinstance(run, Mapping)
                or not verify_signed_record(run, "vm_phase_monitor_hmac_sha256")
                or run.get("schema_version") != 1 or run.get("mode") != VM_MODE
                or run.get("status") != "PASS_PHASE_COMPLETE"
                or run.get("vm_state") != "STOPPED"
                or run.get("phase_quiescent") is not True
                or run.get("all_writable_handles_released") is not True
                or run.get("serial_and_attachment_handles_released") is not True
                or run.get("controller_identity") != current_controller
                or run.get("g0c_identity") != current_g0c
                or run.get("host_build_version") != current_build):
            raise RuntimeError("stop-safety run is unsigned, stale, unsuccessful, or lacks stop/handle proof")
        volume_uuid = run.get("volume_uuid")
        if (not isinstance(volume_uuid, str) or not re.fullmatch(r"[0-9A-Fa-f-]{36}", volume_uuid)
                or (evidence_volume_uuid is not None and evidence_volume_uuid != volume_uuid)):
            raise RuntimeError("stop-safety runs do not bind one exact VM APFS volume")
        evidence_volume_uuid = volume_uuid
        samples = run.get("samples")
        if not isinstance(samples, list) or len(samples) < 2:
            raise RuntimeError("each stop-safety run must contain at least two live capacity samples")
        sample_count += len(samples)
        previous: Mapping[str, Any] | None = None
        for sample in samples:
            if (not isinstance(sample, Mapping)
                    or not _integer(sample.get("vm_volume_used_bytes"))
                    or not _integer(sample.get("shared_container_free_bytes"))
                    or not isinstance(sample.get("monitor_sampled_at_unix"), (int, float))
                    or isinstance(sample.get("monitor_sampled_at_unix"), bool)):
                raise RuntimeError("stop-safety run contains an incomplete capacity sample")
            if previous is not None:
                gap_seconds = float(sample["monitor_sampled_at_unix"]) - float(previous["monitor_sampled_at_unix"])
                if not math.isfinite(gap_seconds) or gap_seconds <= 0:
                    raise RuntimeError("stop-safety sample timestamps are not strictly increasing")
                gap_us = math.ceil(gap_seconds * 1_000_000)
                maximum_gap_us = max(maximum_gap_us, gap_us)
                vm_growth = max(0, sample["vm_volume_used_bytes"] - previous["vm_volume_used_bytes"])
                total_growth = max(0, previous["shared_container_free_bytes"]
                                   - sample["shared_container_free_bytes"])
                concurrent_growth = max(0, total_growth - vm_growth)
                maximum_concurrent_interval_bytes = max(maximum_concurrent_interval_bytes, concurrent_growth)
                if vm_growth:
                    maximum_write_rate = max(maximum_write_rate,
                                             math.ceil(vm_growth / gap_seconds))
            previous = sample
        stop = run.get("stop_measurement")
        if (not isinstance(stop, Mapping)
                or stop.get("phase_quiescent") is not True
                or stop.get("vm_stopped_and_handles_released") is not True
                or not _integer(stop.get("stop_latency_microseconds"), minimum=1)
                or (stop.get("concurrent_growth_during_stop_bytes") is not None
                    and not _integer(stop.get("concurrent_growth_during_stop_bytes")))):
            raise RuntimeError("stop-safety run lacks measured stop latency or has malformed growth telemetry")
        maximum_stop_latency_us = max(maximum_stop_latency_us, stop["stop_latency_microseconds"])
        concurrent_stop = stop.get("concurrent_growth_during_stop_bytes")
        if concurrent_stop is not None:
            maximum_concurrent_stop_bytes = max(maximum_concurrent_stop_bytes or 0, concurrent_stop)
    if sample_count < 2 or maximum_write_rate < 1 or maximum_gap_us < 1:
        raise RuntimeError("stop-safety evidence does not contain measured VM allocation and monitor intervals")
    return {
        "host_build_version": current_build,
        "volume_uuid": evidence_volume_uuid,
        "controller_identity": current_controller,
        "g0c_identity": current_g0c,
        "sample_count": sample_count,
        "maximum_write_rate_bytes_per_second": maximum_write_rate,
        "maximum_monitor_gap_microseconds": maximum_gap_us,
        "maximum_stop_latency_microseconds": maximum_stop_latency_us,
        "concurrent_growth_during_stop_bytes": maximum_concurrent_stop_bytes,
        "maximum_concurrent_growth_per_sample_bytes": maximum_concurrent_interval_bytes,
    }


def _verify_stop_safety_profile(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    blockers: list[str] = []
    try:
        profile = _read_signed_record(path, "vm_stop_safety_profile_hmac_sha256")
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        return None, [f"signed VM stop-safety profile is missing or invalid: {error}"]
    created = profile.get("created_at_unix")
    age = time.time() - float(created) if isinstance(created, (int, float)) and not isinstance(created, bool) else float("inf")
    if (profile.get("status") != "PASS" or age < 0
            or age > VM_STOP_SAFETY_PROFILE_MAX_AGE_SECONDS):
        blockers.append("VM stop-safety profile is not a fresh PASS")
    evidence_path_value = profile.get("measurement_evidence_path")
    if not isinstance(evidence_path_value, str):
        blockers.append("VM stop-safety profile has no bound measurement evidence path")
        evidence_path = None
    else:
        evidence_path = Path(evidence_path_value)
        if not evidence_path.is_absolute():
            evidence_path = policy.REPO / evidence_path
    try:
        if evidence_path is None or _file_sha256(evidence_path) != profile.get("measurement_evidence_sha256"):
            raise RuntimeError("evidence file hash does not match the profile")
        derived = _derive_stop_safety_measurements(evidence_path)
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        derived = {}
        blockers.append(f"VM stop-safety evidence is not independently reproducible: {error}")
    for field in ("host_build_version", "volume_uuid", "controller_identity", "g0c_identity",
                  "sample_count", "maximum_write_rate_bytes_per_second",
                  "maximum_monitor_gap_microseconds", "maximum_stop_latency_microseconds",
                  "concurrent_growth_during_stop_bytes", "maximum_concurrent_growth_per_sample_bytes"):
        if profile.get(field) != derived.get(field):
            blockers.append(f"VM stop-safety profile field {field} does not match measured monitor evidence")
    if profile.get("maximum_monitor_gap_microseconds", 0) > 250_000:
        blockers.append("VM stop-safety monitor interval exceeds the 250 ms controller bound")
    cushion = calculate_stop_cushion(
        maximum_write_rate_bytes_per_second=profile.get("maximum_write_rate_bytes_per_second"),
        maximum_monitor_gap_microseconds=profile.get("maximum_monitor_gap_microseconds"),
        maximum_stop_latency_microseconds=profile.get("maximum_stop_latency_microseconds"),
        concurrent_growth_during_stop_bytes=profile.get("concurrent_growth_during_stop_bytes"),
    )
    blockers.extend(cushion["blockers"])
    if profile.get("stop_cushion") != cushion:
        blockers.append("VM stop-safety profile cushion calculation does not match its raw measurements")
    if blockers:
        return profile, blockers
    return profile, []


def create_stop_safety_profile(*, measurement_evidence_path: Path) -> dict[str, Any]:
    """Derive all stop-safety limits from signed live monitor measurements."""
    measurements = _derive_stop_safety_measurements(measurement_evidence_path)
    cushion = calculate_stop_cushion(
        maximum_write_rate_bytes_per_second=measurements["maximum_write_rate_bytes_per_second"],
        maximum_monitor_gap_microseconds=measurements["maximum_monitor_gap_microseconds"],
        maximum_stop_latency_microseconds=measurements["maximum_stop_latency_microseconds"],
        concurrent_growth_during_stop_bytes=measurements["concurrent_growth_during_stop_bytes"],
    )
    return _seal({
        "schema_version": 1,
        "mode": VM_MODE,
        "status": "PASS" if cushion["admitted"] else "FAIL",
        "created_at_unix": time.time(),
        "measurement_evidence_path": str(measurement_evidence_path),
        "measurement_evidence_sha256": _file_sha256(measurement_evidence_path),
        **measurements,
        "stop_cushion": cushion,
        "blockers": cushion["blockers"],
    }, "vm_stop_safety_profile_hmac_sha256")


def write_stop_safety_profile(*, measurement_evidence_path: Path,
                              profile_path: Path = VM_STOP_SAFETY_PROFILE_PATH) -> dict[str, Any]:
    signed = create_stop_safety_profile(measurement_evidence_path=measurement_evidence_path)
    raw = {key: value for key, value in signed.items()
           if key != "vm_stop_safety_profile_hmac_sha256"}
    return _write_signed_json(profile_path, raw, "vm_stop_safety_profile_hmac_sha256")


def create_p2_admission(
        *, stop_safety_profile_path: Path,
        creation_receipt_path: Path = VM_VOLUME_CREATION_RECEIPT_PATH,
        p1_admission_path: Path = VM_ADMISSION_PATH,
        restore_verification_path: Path = VM_RESTORE_VERIFICATION_PATH,
        receipt_path: Path = VM_P2_ADMISSION_PATH) -> dict[str, Any]:
    """Revalidate retained P1 restore and authorize only the bounded P2 installer phase."""
    creation = _read_signed_record(creation_receipt_path, "vm_creation_hmac_sha256")
    p1 = _read_signed_record(p1_admission_path, "vm_admission_hmac_sha256")
    restore = _read_signed_record(restore_verification_path, "restore_verification_hmac_sha256")
    session_id = creation.get("session_id")
    volume_uuid = creation.get("volume_uuid")
    blockers: list[str] = []
    if (creation.get("status") != "PASS" or creation.get("mode") != VM_MODE
            or p1.get("decision") != "PASS" or p1.get("authorized_phase") != "P1"
            or restore.get("status") != "PASS" or restore.get("phase_id") != "P1"
            or restore.get("local_sha256_state") != "VERIFIED"):
        blockers.append("P2 requires successful creation, signed P1 admission, and locally verified restore receipts")
    if (p1.get("session_id") != session_id or p1.get("vm_volume_uuid") != volume_uuid
            or restore.get("session_id") != session_id or restore.get("volume_uuid") != volume_uuid
            or restore.get("p1_admission_sha256") != _file_sha256(p1_admission_path)):
        blockers.append("P2 creation, P1 admission, restore, and VM volume identities disagree")
    current_controller = _controller_identity()
    if p1.get("controller_identity") != current_controller:
        blockers.append("P2 P1 admission does not bind current controller bytes")
    try:
        g0c_identity = verify_fresh_g0c_review()
    except RuntimeError as error:
        g0c_identity = {"status": "FAIL"}
        blockers.append(str(error))
    if (creation.get("g0c_identity") != g0c_identity
            or p1.get("g0c_identity") != g0c_identity
            or restore.get("g0c_identity") != g0c_identity):
        blockers.append("P2 phase evidence does not bind the current G0C review")
    profile_absent = _path_entry_is_absent(stop_safety_profile_path)
    canonical_profile_absent = _path_entry_is_absent(VM_STOP_SAFETY_PROFILE_PATH)
    if profile_absent:
        profile = None
        stop_profile_mode = VM_STOP_SAFETY_PROFILE_MODE_LIVE
        if not canonical_profile_absent:
            blockers.append("P2 cannot ignore a present canonical stop-safety profile")
    else:
        profile, profile_blockers = _verify_stop_safety_profile(stop_safety_profile_path)
        blockers.extend(profile_blockers)
        stop_profile_mode = VM_STOP_SAFETY_PROFILE_MODE_MEASURED
        if profile is not None:
            if profile.get("volume_uuid") != volume_uuid:
                blockers.append("P2 stop-safety profile is bound to a different VM APFS volume")
            if profile.get("g0c_identity") != g0c_identity:
                blockers.append("P2 stop-safety profile does not bind the current G0C review")
    try:
        host_build_version = _host_build_version()
        if (profile is not None
                and profile.get("host_build_version") != host_build_version):
            blockers.append("P2 stop-safety profile was measured on a different host build")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        host_build_version = None
        blockers.append(f"P2 host build identity is unavailable: {error}")
    with policy._acquire_storage_allocation_lock():
        observation = capture_host_observation()
        blockers.extend(_session_container_blockers(observation, volume_uuid=volume_uuid))
        capacity_contract: dict[str, Any] | None = None
        try:
            capacity_contract = _active_capacity_contract(volume_uuid=volume_uuid)
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
            blockers.append(f"P2 signed reserve/quota identity is unavailable: {error}")
        try:
            vm_volume = _volume_row(observation, volume_uuid)
            quota_used = vm_volume.get("CapacityInUse")
            if not _integer(quota_used):
                raise ValueError("VM quota usage is unknown")
            mount = _volume_mount_info(observation, volume_uuid)
            if (not mount.get("mounted") or restore.get("restore_path")
                    != str(Path(mount["mount_point"]) / "restore.ipsw")):
                raise ValueError("P1 restore path is not the exact path on the bound VM volume")
            root_fd = policy._open_directory_nofollow(Path(mount["mount_point"]))
            try:
                restore_fd = os.open("restore.ipsw", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                     dir_fd=root_fd)
                try:
                    info = os.fstat(restore_fd)
                    expected_file = restore.get("file_identity")
                    current_identity = {
                        "device": info.st_dev,
                        "inode": info.st_ino,
                        "logical_bytes": info.st_size,
                        "mtime_ns": info.st_mtime_ns,
                        "ctime_ns": getattr(info, "st_ctime_ns", 0),
                    }
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_dev != os.stat(mount["mount_point"]).st_dev
                            or not isinstance(expected_file, Mapping)
                            or any(expected_file.get(key) != value for key, value in current_identity.items())
                            or info.st_size != restore.get("expected_length_bytes")):
                        raise ValueError("locally verified restore file identity changed")
                finally:
                    os.close(restore_fd)
            finally:
                os.close(root_fd)
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            blockers.append(f"P2 restore or VM-volume identity check failed: {error}")
            quota_used = None
            mount = {}
        try:
            concurrent_measurement = measure_concurrent_growth_bound(volume_uuid=volume_uuid)
            concurrent_growth_bound = concurrent_measurement.get("observed_max_interval_growth_bytes")
            if not _integer(concurrent_growth_bound):
                concurrent_growth_bound = None
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            concurrent_measurement = {"status": "UNKNOWN", "failure": str(error)}
            concurrent_growth_bound = None
        shared_free: int | None = None
        if (isinstance(quota_used, int) and not isinstance(quota_used, bool)
                and _integer(quota_used)):
            p2_growth = (VM_P2_SYSTEM_FILE_DATA_MAX_BYTES
                         + VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES)
            try:
                capacity_contract = _active_capacity_contract(volume_uuid=volume_uuid)
                shared_free = conservative_session_unreserved_free_bytes(
                    observation, volume_uuid=volume_uuid, capacity_contract=capacity_contract)
                selected_quota = capacity_contract["quota_bytes"]
                p2_result = calculate_vm_phase_admission(
                    mode=VM_MODE, container_uuid=EXPECTED_CONTAINER_UUID,
                    volume_uuid=volume_uuid, free_bytes=shared_free,
                    quota_bytes=selected_quota, quota_used_bytes=quota_used,
                    growth_bytes=p2_growth, uncertainty_bytes=0,
                    concurrent_growth_bytes=concurrent_growth_bound,
                    expected_quota_bytes=selected_quota)
            except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
                p2_result = {"admitted": False,
                             "blockers": [f"P2 reserve/quota contract is unknown: {error}"]}
            blockers.extend(p2_result["blockers"])
        else:
            p2_growth = (VM_P2_SYSTEM_FILE_DATA_MAX_BYTES
                         + VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES)
            p2_result = {"admitted": False, "blockers": ["VM quota use is unknown"]}
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("session_id") != session_id or marker.get("volume_uuid") != volume_uuid
                or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P1_VERIFIED"):
            blockers.append("P2 requires the same active VM session at the P1_VERIFIED boundary")
        project_usage = policy.measure_project_usage()
        project_bytes = policy.conservative_project_usage_bytes(project_usage)
        project_usage["conservative_allocated_inode_deduplicated_bytes"] = project_bytes
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "decision": "PASS" if not blockers else "FAIL",
            "authorized_phase": "P2" if not blockers else None,
            "session_id": session_id,
            "created_at_unix": time.time(),
            "controller_identity": current_controller,
            "g0c_identity": g0c_identity,
            "host_build_version": host_build_version,
            "stop_safety_profile_mode": stop_profile_mode,
            "stop_safety_profile_sha256": _file_sha256(stop_safety_profile_path)
            if profile is not None else None,
            "stop_safety_profile": profile,
            "vm_volume_uuid": volume_uuid,
            "vm_volume_quota_bytes": capacity_contract.get("quota_bytes") if capacity_contract else None,
            "vm_volume_reserve_bytes": 0,
            "safety_volume_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "safety_volume_uuid": capacity_contract.get("safety_volume_uuid") if capacity_contract else None,
            "safety_volume_in_use_bytes": capacity_contract.get("safety_volume_in_use_bytes") if capacity_contract else None,
            "safety_container_uuid": capacity_contract.get("safety_container_uuid") if capacity_contract else None,
            "safety_physical_store_uuid": capacity_contract.get("safety_physical_store_uuid") if capacity_contract else None,
            "vm_volume_usage_bytes": quota_used,
            "vm_quota_headroom_bytes": capacity_contract["quota_bytes"] - quota_used
            if capacity_contract and _integer(quota_used) else None,
            "restore_verification_sha256": _file_sha256(restore_verification_path),
            "p1_admission_sha256": _file_sha256(p1_admission_path),
            "p2_component_maxima_bytes": {
                "guest_system_user_file_data": VM_P2_SYSTEM_FILE_DATA_MAX_BYTES,
                "auxiliary_storage_user_file_data": VM_P2_AUXILIARY_FILE_DATA_MAX_BYTES,
            },
            "p2_growth_bytes": p2_growth,
            "p2_uncertainty_bytes": 0,
            "p2_concurrent_growth_measurement": concurrent_measurement,
            "p2_concurrent_growth_bound_bytes": concurrent_growth_bound,
            "p2_concurrent_growth_authority": "DIAGNOSTIC_ONLY",
            "phase_evaluation": p2_result,
            "shared_container_free_bytes": shared_free,
            "unreserved_free_ceiling_bytes": capacity_contract.get("unreserved_free_ceiling_bytes")
            if capacity_contract else None,
            "normal_project_usage": project_usage,
            "normal_project_scope_bytes": project_bytes,
            "normal_project_policy": {
                "project_ceiling_bytes": policy.PROJECT_BUDGET,
                "projected_free_min_bytes": policy.FREE_MINIMUM,
                "preferred_free_bytes": policy.FREE_PREFERRED,
                "emergency_free_stop_bytes": policy.FREE_HARD_STOP,
                "status": "MEASURED_SEPARATELY_NO_VM_CREDIT",
            },
            "blockers": blockers,
            "warnings": ["P2 PASS authorizes only the bounded installer phase",
                         "no workspace or runtime-feasibility RAW assets are authorized"],
        }
        import build_retention

        with build_retention.host_buildlease_p2_admission_guard() as host_lease_blockers:
            blockers.extend(host_lease_blockers)
            record["decision"] = "PASS" if not blockers else "FAIL"
            record["authorized_phase"] = "P2" if not blockers else None
            record["blockers"] = blockers
            sealed = _write_signed_json(receipt_path, record, "vm_admission_hmac_sha256")
            if not blockers:
                update_active_session_marker(str(session_id), phase_id="P2_AUTHORIZED",
                                             committed_growth_bytes=p2_growth)
            else:
                update_active_session_marker(str(session_id), state="FAILED",
                                             phase_id="P2_ADMISSION_FAILED")
            return sealed


def record_p2_install_result(*, installer_result: Mapping[str, Any],
                             phase_monitor_result: Mapping[str, Any],
                             p2_admission_path: Path = VM_P2_ADMISSION_PATH,
                             receipt_path: Path = VM_P2_INSTALL_RECEIPT_PATH) -> dict[str, Any]:
    """Seal one completed P2 monitor+installer result without granting P3 authority."""
    p2 = _read_signed_record(p2_admission_path, "vm_admission_hmac_sha256")
    if p2.get("decision") != "PASS" or p2.get("authorized_phase") != "P2":
        raise RuntimeError("P2 result requires a signed P2-only admission PASS")
    session_id = p2.get("session_id")
    volume_uuid = p2.get("vm_volume_uuid")
    active = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    if (active.get("session_id") != session_id or active.get("volume_uuid") != volume_uuid
            or active.get("state") != "ACTIVE" or active.get("phase_id") != "P2_COMPLETE"):
        raise RuntimeError("P2 result requires a completed monitor and stopped P2 session marker")
    if p2.get("controller_identity") != _controller_identity():
        raise RuntimeError("P2 result admission does not bind current controller bytes")
    if p2.get("g0c_identity") != verify_fresh_g0c_review():
        raise RuntimeError("P2 result admission does not bind the current G0C review")
    if (not verify_signed_record(phase_monitor_result, "vm_phase_monitor_hmac_sha256")
            or phase_monitor_result.get("p2_admission_sha256") != _file_sha256(p2_admission_path)
            or phase_monitor_result.get("status") != "PASS_PHASE_COMPLETE"
            or phase_monitor_result.get("phase_id") != "P2"
            or phase_monitor_result.get("session_id") != session_id):
        raise RuntimeError("P2 monitor result is unsigned, stale, or bound to another admission")
    profile_mode = p2.get("stop_safety_profile_mode")
    if profile_mode == VM_STOP_SAFETY_PROFILE_MODE_MEASURED:
        profile = p2.get("stop_safety_profile")
        if (not isinstance(profile, Mapping)
                or phase_monitor_result.get("stop_safety_profile_mode")
                != VM_STOP_SAFETY_PROFILE_MODE_MEASURED
                or phase_monitor_result.get("maximum_observed_monitor_gap_seconds", float("inf"))
                > profile.get("maximum_monitor_gap_microseconds", -1) / 1_000_000):
            raise RuntimeError("P2 monitor result did not complete within its signed stop-safety profile")
    elif profile_mode == VM_STOP_SAFETY_PROFILE_MODE_LIVE:
        if (phase_monitor_result.get("stop_safety_profile_mode") != VM_STOP_SAFETY_PROFILE_MODE_LIVE
                or phase_monitor_result.get("maximum_observed_monitor_gap_seconds", float("inf"))
                > VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS):
            raise RuntimeError("P2 live-capacity monitor gap exceeded its operational sampling interval")
    else:
        raise RuntimeError("P2 result admission has an unknown stop-safety profile mode")
    required_strings = (
        "system_image_sha256", "auxiliary_storage_sha256", "hardware_model_sha256",
        "macos_version", "macos_build", "architecture")
    for field in required_strings:
        value = installer_result.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"P2 installer result is missing {field}")
    for field in ("system_image_sha256", "auxiliary_storage_sha256", "hardware_model_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", installer_result[field]):
            raise ValueError(f"P2 installer result has a malformed {field}")
    record = {
        "schema_version": 1,
        "mode": VM_MODE,
        "status": "PASS_INSTALL_COMPLETE" if installer_result.get("installer_completed") is True
        and installer_result.get("vm_state") == "STOPPED"
        and installer_result.get("all_writable_handles_released") is True
        and installer_result.get("serial_and_attachment_handles_released") is True
        else "FAIL",
        "phase_id": "P2",
        "session_id": session_id,
        "volume_uuid": volume_uuid,
        "created_at_unix": time.time(),
        "installer_completed": installer_result.get("installer_completed") is True,
        "vm_state": installer_result.get("vm_state"),
        "all_writable_handles_released": installer_result.get("all_writable_handles_released"),
        "serial_and_attachment_handles_released": installer_result.get("serial_and_attachment_handles_released"),
        "guest_bootstrap_receipt_verified": installer_result.get("guest_bootstrap_receipt_verified") is True,
        "bootstrap_identity_sha256": installer_result.get("bootstrap_identity_sha256"),
        "system_image_sha256": installer_result.get("system_image_sha256"),
        "auxiliary_storage_sha256": installer_result.get("auxiliary_storage_sha256"),
        "hardware_model_sha256": installer_result.get("hardware_model_sha256"),
        "system_image_logical_capacity_bytes": installer_result.get("system_image_logical_capacity_bytes"),
        "auxiliary_logical_capacity_bytes": installer_result.get("auxiliary_logical_capacity_bytes"),
        "macos_version": installer_result.get("macos_version"),
        "macos_build": installer_result.get("macos_build"),
        "architecture": installer_result.get("architecture"),
        "phase_monitor_result": dict(phase_monitor_result),
        "phase_monitor_result_sha256": _sha256(policy._canonical_pretty_json_bytes(phase_monitor_result)),
        "p2_admission_sha256": _file_sha256(p2_admission_path),
        "stop_safety_profile_sha256": p2.get("stop_safety_profile_sha256"),
        "stop_safety_profile_mode": profile_mode,
    }
    sealed = _write_signed_json(receipt_path, record, "vm_install_result_hmac_sha256")
    if record["status"] != "PASS_INSTALL_COMPLETE":
        update_active_session_marker(str(session_id), state="FAILED", phase_id="P2_INSTALL_FAILED")
    return sealed


def _hash_bound_volume_file(mount_path: Path, basename: str,
                            maximum_logical_bytes: int) -> dict[str, Any]:
    if (not isinstance(basename, str) or not basename or basename in {".", ".."}
            or "/" in basename or "\\" in basename or "\x00" in basename):
        raise ValueError("VM component path must be one fixed basename")
    root_fd = policy._open_directory_nofollow(mount_path)
    try:
        fd = os.open(basename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_dev != os.fstat(root_fd).st_dev
                    or before.st_size <= 0 or before.st_size > maximum_logical_bytes):
                raise ValueError(f"VM component {basename} has an invalid type, link count, device, or size")
            hasher = hashlib.sha256()
            length = 0
            while True:
                block = os.read(fd, VM_RESTORE_DOWNLOAD_CHUNK_BYTES)
                if not block:
                    break
                length += len(block)
                if length > maximum_logical_bytes:
                    raise ValueError(f"VM component {basename} exceeded its logical-size limit")
                hasher.update(block)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 getattr(before, "st_ctime_ns", 0))
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                        getattr(after, "st_ctime_ns", 0)) or length != after.st_size):
                raise RuntimeError(f"VM component {basename} changed while hashing")
            return {
                "basename": basename,
                "device": after.st_dev,
                "inode": after.st_ino,
                "logical_capacity_bytes": after.st_size,
                "physical_allocated_bytes": getattr(after, "st_blocks", 0) * 512,
                "mtime_ns": after.st_mtime_ns,
                "ctime_ns": getattr(after, "st_ctime_ns", 0),
                "sha256": hasher.hexdigest(),
            }
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def create_p3_sealed_base(
        *, install_result_path: Path = VM_P2_INSTALL_RECEIPT_PATH,
        p2_admission_path: Path = VM_P2_ADMISSION_PATH,
        creation_receipt_path: Path = VM_VOLUME_CREATION_RECEIPT_PATH,
        restore_verification_path: Path = VM_RESTORE_VERIFICATION_PATH,
        receipt_path: Path = VM_P3_SEAL_RECEIPT_PATH) -> dict[str, Any]:
    """Seal stopped P2 output and all required restore/base identities for P4."""
    install = _read_signed_record(install_result_path, "vm_install_result_hmac_sha256")
    p2 = _read_signed_record(p2_admission_path, "vm_admission_hmac_sha256")
    creation = _read_signed_record(creation_receipt_path, "vm_creation_hmac_sha256")
    restore = _read_signed_record(restore_verification_path, "restore_verification_hmac_sha256")
    session_id = creation.get("session_id")
    volume_uuid = creation.get("volume_uuid")
    if (install.get("status") != "PASS_INSTALL_COMPLETE" or p2.get("decision") != "PASS"
            or p2.get("authorized_phase") != "P2"
            or install.get("session_id") != session_id
            or p2.get("session_id") != session_id
            or install.get("volume_uuid") != volume_uuid
            or restore.get("session_id") != session_id
            or restore.get("volume_uuid") != volume_uuid):
        raise RuntimeError("P3 requires same-session signed P2 completion and P1 restore provenance")
    for field, expected in (
            ("vm_state", "STOPPED"),
            ("all_writable_handles_released", True),
            ("serial_and_attachment_handles_released", True),
            ("installer_completed", True),
            ("guest_bootstrap_receipt_verified", True)):
        if install.get(field) != expected:
            raise RuntimeError(f"P3 install evidence does not prove {field}={expected!r}")
    if (install.get("macos_version") != "27.0.1"
            or install.get("macos_build") != "26A434"
            or install.get("architecture") != "arm64"):
        raise RuntimeError("P3 guest identity is not the authorized macOS 27.0.1 arm64 target")
    current_controller = _controller_identity()
    g0c_identity = verify_fresh_g0c_review()
    if (p2.get("controller_identity") != current_controller
            or p2.get("g0c_identity") != g0c_identity
            or creation.get("g0c_identity") != g0c_identity):
        raise RuntimeError("P3 controller or G0C identity is stale")
    with policy._acquire_storage_allocation_lock():
        observation = capture_host_observation()
        blockers = _session_container_blockers(observation, volume_uuid=volume_uuid)
        if blockers:
            raise RuntimeError("P3 VM volume identity failed: " + "; ".join(blockers))
        mount = _volume_mount_info(observation, volume_uuid)
        if not mount.get("mounted"):
            raise RuntimeError("P3 requires all base image files on the mounted VM volume")
        components = {
            "system_image": _hash_bound_volume_file(
                Path(mount["mount_point"]), "guest-system.raw", VM_SYSTEM_LOGICAL_CAPACITY_BYTES),
            "auxiliary_storage": _hash_bound_volume_file(
                Path(mount["mount_point"]), "auxiliary-storage.bin", VM_AUXILIARY_LOGICAL_CAPACITY_BYTES),
            "hardware_model": _hash_bound_volume_file(
                Path(mount["mount_point"]), "hardware-model.bin", 16 * MIB),
            "bootstrap_identity": _hash_bound_volume_file(
                Path(mount["mount_point"]), "bootstrap-identity.json", 1 * MIB),
        }
        expected_component_hashes = {
            "system_image": install.get("system_image_sha256"),
            "auxiliary_storage": install.get("auxiliary_storage_sha256"),
            "hardware_model": install.get("hardware_model_sha256"),
            "bootstrap_identity": install.get("bootstrap_identity_sha256"),
        }
        for name, component in components.items():
            if expected_component_hashes[name] != component["sha256"]:
                raise RuntimeError(f"P3 {name} identity differs from signed P2 install evidence")
        if (not _integer(install.get("system_image_logical_capacity_bytes"), minimum=1)
                or install.get("system_image_logical_capacity_bytes")
                != components["system_image"]["logical_capacity_bytes"]
                or not _integer(install.get("auxiliary_logical_capacity_bytes"), minimum=1)
                or install.get("auxiliary_logical_capacity_bytes")
                != components["auxiliary_storage"]["logical_capacity_bytes"]):
            raise RuntimeError("P3 logical RAW capacities do not match installed image lengths")
        restore_sha = restore.get("local_sha256")
        if (restore.get("local_sha256_state") != "VERIFIED"
                or not isinstance(restore_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", restore_sha)):
            raise RuntimeError("P3 restore local hash provenance is missing")
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("session_id") != session_id or marker.get("volume_uuid") != volume_uuid
                or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P2_COMPLETE"):
            raise RuntimeError("P3 requires the same active session at the stopped P2_COMPLETE boundary")
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS",
            "session_id": session_id,
            "phase_id": "P3",
            "created_at_unix": time.time(),
            "controller_identity": current_controller,
            "g0c_identity": g0c_identity,
            "vm_volume_uuid": volume_uuid,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "mount": mount,
            "guest_identity": {
                "macos_version": install["macos_version"],
                "macos_build": install["macos_build"],
                "architecture": install["architecture"],
            },
            "components": components,
            "system_image_sha256": components["system_image"]["sha256"],
            "auxiliary_storage_sha256": components["auxiliary_storage"]["sha256"],
            "hardware_model_sha256": components["hardware_model"]["sha256"],
            "bootstrap_identity_sha256": components["bootstrap_identity"]["sha256"],
            "restore_provenance": {
                "verification_receipt_sha256": _file_sha256(restore_verification_path),
                "source_url": restore.get("restore_metadata", {}).get("source_url"),
                "upstream_metadata_sha256": restore.get("restore_metadata", {}).get("metadata_sha256"),
                "expected_length_bytes": restore.get("expected_length_bytes"),
                "actual_length_bytes": restore.get("actual_length_bytes"),
                "local_sha256": restore_sha,
                "download_time_unix": restore.get("created_at_unix"),
            },
            "p2_admission_sha256": _file_sha256(p2_admission_path),
            "install_result_sha256": _file_sha256(install_result_path),
            "shared_container_free_bytes": conservative_session_unreserved_free_bytes(
                observation, volume_uuid=volume_uuid),
            "vm_volume_usage_bytes": _volume_row(observation, volume_uuid).get("CapacityInUse"),
        }
        sealed = _write_signed_json(receipt_path, record, "sealed_base_hmac_sha256")
        update_active_session_marker(str(session_id), phase_id="P3_SEALED",
                                     committed_growth_bytes=0)
        return sealed


def create_p4_restore_cleanup_action(
        *, p3_seal_path: Path = VM_P3_SEAL_RECEIPT_PATH,
        restore_verification_path: Path = VM_RESTORE_VERIFICATION_PATH,
        action_path: Path = VM_P4_CLEANUP_ACTION_PATH) -> dict[str, Any]:
    """Sign a one-use P4 action bound to the exact locally hashed restore file."""
    p3 = _read_signed_record(p3_seal_path, "sealed_base_hmac_sha256")
    restore = _read_signed_record(restore_verification_path, "restore_verification_hmac_sha256")
    if (p3.get("status") != "PASS" or p3.get("phase_id") != "P3"
            or restore.get("status") != "PASS"
            or p3.get("session_id") != restore.get("session_id")
            or p3.get("vm_volume_uuid") != restore.get("volume_uuid")
            or p3.get("restore_provenance", {}).get("local_sha256") != restore.get("local_sha256")):
        raise RuntimeError("P4 cleanup requires a complete same-session P3 seal and P1 restore provenance")
    if action_path.exists() or action_path.is_symlink():
        raise FileExistsError("a P4 cleanup action already exists; one-use authorization will not be replaced")
    action = {
        "schema_version": 1,
        "mode": VM_MODE,
        "action": "DELETE_EXACT_P1_RESTORE_AFTER_P3_SEAL",
        "action_id": uuid.uuid4().hex,
        "created_at_unix": time.time(),
        "session_id": restore.get("session_id"),
        "container_uuid": EXPECTED_CONTAINER_UUID,
        "vm_volume_uuid": restore.get("volume_uuid"),
        "canonical_path": restore.get("restore_path"),
        "file_identity": restore.get("file_identity"),
        "logical_bytes": restore.get("actual_length_bytes"),
        "allocated_bytes": restore.get("file_identity", {}).get("allocated_bytes"),
        "restore_sha256": restore.get("local_sha256"),
        "p3_seal_sha256": _file_sha256(p3_seal_path),
        "sealed_base_sha256": _sha256(policy._canonical_pretty_json_bytes(p3)),
        "p3_system_image_sha256": p3.get("components", {}).get("system_image", {}).get("sha256"),
        "p3_auxiliary_storage_sha256": p3.get("components", {}).get("auxiliary_storage", {}).get("sha256"),
        "p3_hardware_model_sha256": p3.get("components", {}).get("hardware_model", {}).get("sha256"),
        "p3_bootstrap_identity_sha256": p3.get("components", {}).get("bootstrap_identity", {}).get("sha256"),
        "one_use": True,
    }
    sealed = _seal(action, "restore_cleanup_hmac_sha256")
    policy._write_json(action_path, sealed, strict_directory_sync=True)
    return _read_signed_record(action_path, "restore_cleanup_hmac_sha256")


def perform_p4_restore_cleanup(
        *, action_path: Path = VM_P4_CLEANUP_ACTION_PATH,
        p3_seal_path: Path = VM_P3_SEAL_RECEIPT_PATH,
        restore_verification_path: Path = VM_RESTORE_VERIFICATION_PATH,
        receipt_path: Path = VM_P4_CLEANUP_RECEIPT_PATH) -> dict[str, Any]:
    """Delete only the signed restore inode after P3, then measure actual reclaimed bytes."""
    action = _read_signed_record(action_path, "restore_cleanup_hmac_sha256")
    p3 = _read_signed_record(p3_seal_path, "sealed_base_hmac_sha256")
    restore = _read_signed_record(restore_verification_path, "restore_verification_hmac_sha256")
    if validate_restore_cleanup(restore_record=restore, sealed_base_record=p3,
                                cleanup_action=action):
        raise RuntimeError("P4 cleanup action is incomplete or not bound to authenticated P1/P3 evidence")
    if (action.get("one_use") is not True or action.get("action") != "DELETE_EXACT_P1_RESTORE_AFTER_P3_SEAL"
            or action.get("p3_seal_sha256") != _file_sha256(p3_seal_path)):
        raise RuntimeError("P4 cleanup action is stale, reused, or not bound to this P3 seal")
    session_id = action.get("session_id")
    volume_uuid = action.get("vm_volume_uuid")
    with policy._acquire_storage_allocation_lock():
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("session_id") != session_id or marker.get("volume_uuid") != volume_uuid
                or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P3_SEALED"):
            raise RuntimeError("P4 requires the same active VM session after the P3 seal")
        before = capture_host_observation()
        blockers = _session_container_blockers(before, volume_uuid=volume_uuid)
        if blockers:
            raise RuntimeError("P4 APFS identity check failed: " + "; ".join(blockers))
        mount = _volume_mount_info(before, volume_uuid)
        expected_path = Path(mount["mount_point"]) / "restore.ipsw"
        if (action.get("canonical_path") != str(expected_path)
                or restore.get("restore_path") != str(expected_path)):
            raise RuntimeError("P4 action path is not the canonical restore path on the bound volume")
        root_fd = policy._open_directory_nofollow(Path(mount["mount_point"]))
        try:
            fd = os.open("restore.ipsw", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                expected = action.get("file_identity")
                identity_fields = ("device", "inode", "logical_bytes", "mtime_ns", "ctime_ns")
                current = {
                    "device": info.st_dev, "inode": info.st_ino,
                    "logical_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": getattr(info, "st_ctime_ns", 0),
                }
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or not isinstance(expected, Mapping)
                        or any(expected.get(field) != current[field] for field in identity_fields)
                        or expected.get("allocated_bytes") != getattr(info, "st_blocks", 0) * 512
                        or info.st_size != action.get("logical_bytes")):
                    raise RuntimeError("P4 restore file type, identity, link count, size, or allocation changed")
                if action.get("allocated_bytes") != getattr(info, "st_blocks", 0) * 512:
                    raise RuntimeError("P4 restore allocated-byte identity changed")
                before_used = _volume_row(before, volume_uuid).get("CapacityInUse")
                before_free = conservative_session_unreserved_free_bytes(
                    before, volume_uuid=volume_uuid)
                if not _integer(before_used):
                    raise RuntimeError("P4 VM quota-accounted use is unknown")
                file_identity = dict(current)
                file_identity["allocated_bytes"] = getattr(info, "st_blocks", 0) * 512
            finally:
                os.close(fd)
            os.unlink("restore.ipsw", dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        update_active_session_marker(str(session_id), phase_id="P4_RECLAIM_MEASUREMENT")
        deadline = time.monotonic() + 20.0
        samples: list[dict[str, Any]] = []
        after: Mapping[str, Any] | None = None
        reclaimed = 0
        quota_reduction = 0
        minimum_reclaim = max(64 * MIB, file_identity["allocated_bytes"] - 128 * MIB)
        while True:
            after = capture_host_observation()
            after_blockers = _session_container_blockers(after, volume_uuid=volume_uuid)
            if after_blockers:
                break
            after_used = _volume_row(after, volume_uuid).get("CapacityInUse")
            after_free = conservative_session_unreserved_free_bytes(
                after, volume_uuid=volume_uuid)
            if _integer(after_used):
                quota_reduction = max(0, before_used - after_used)
            reclaimed = max(0, after_free - before_free)
            samples.append({
                "captured_at_unix": after.get("captured_at_unix"),
                "vm_volume_used_bytes": after_used,
                "shared_container_free_bytes": after_free,
                "quota_reduction_bytes": quota_reduction,
                "reclaimed_container_bytes": reclaimed,
            })
            if quota_reduction >= minimum_reclaim and reclaimed >= minimum_reclaim:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        after_blockers = _session_container_blockers(after, volume_uuid=volume_uuid) if after else [
            "P4 post-cleanup observation is missing"]
        if after_blockers:
            blockers.extend(after_blockers)
        if quota_reduction < minimum_reclaim or reclaimed < minimum_reclaim:
            blockers.append("P4 did not verify the minimum measured APFS quota and shared-container reclaim")
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS" if not blockers else "FAIL",
            "session_id": session_id,
            "phase_id": "P4",
            "created_at_unix": time.time(),
            "action_sha256": _file_sha256(action_path),
            "p3_seal_sha256": _file_sha256(p3_seal_path),
            "restore_sha256": restore.get("local_sha256"),
            "deleted_file_identity": file_identity,
            "before_observation": before,
            "after_observation": after,
            "before_vm_volume_used_bytes": before_used,
            "after_vm_volume_used_bytes": _volume_row(after, volume_uuid).get("CapacityInUse")
            if after else None,
            "before_shared_container_free_bytes": before_free,
            "after_shared_container_free_bytes": conservative_session_unreserved_free_bytes(
                after, volume_uuid=volume_uuid)
            if after else None,
            "measured_vm_quota_reduction_bytes": quota_reduction,
            "measured_container_reclaimed_bytes": reclaimed,
            "minimum_reclaim_bytes": minimum_reclaim,
            "samples": samples,
            "blockers": blockers,
        }
        sealed = _write_signed_json(receipt_path, record, "restore_cleanup_result_hmac_sha256")
        update_active_session_marker(str(session_id),
                                     state="ACTIVE" if not blockers else "FAILED",
                                     phase_id="P4_RECLAIMED" if not blockers else "P4_RECLAIM_FAILED",
                                     committed_growth_bytes=0)
        return sealed


def create_p5_runtime_admission(
        *, p3_seal_path: Path = VM_P3_SEAL_RECEIPT_PATH,
        p4_cleanup_receipt_path: Path = VM_P4_CLEANUP_RECEIPT_PATH,
        receipt_path: Path = VM_P5_ADMISSION_PATH) -> dict[str, Any]:
    """Admit only the bounded runtime-feasibility assets after measured P4 reclaim."""
    p3 = _read_signed_record(p3_seal_path, "sealed_base_hmac_sha256")
    p4 = _read_signed_record(p4_cleanup_receipt_path, "restore_cleanup_result_hmac_sha256")
    session_id = p3.get("session_id")
    volume_uuid = p3.get("vm_volume_uuid")
    blockers: list[str] = []
    if (p3.get("status") != "PASS" or p4.get("status") != "PASS"
            or p4.get("phase_id") != "P4" or p4.get("session_id") != session_id
            or p4.get("p3_seal_sha256") != _file_sha256(p3_seal_path)
            or p4.get("measured_container_reclaimed_bytes", 0) < p4.get("minimum_reclaim_bytes", 1)):
        blockers.append("P5 requires a same-session P3 seal and measured passing P4 cleanup receipt")
    controller = _controller_identity()
    try:
        g0c_identity = verify_fresh_g0c_review()
    except RuntimeError as error:
        g0c_identity = {"status": "FAIL"}
        blockers.append(str(error))
    if p3.get("controller_identity") != controller or p3.get("g0c_identity") != g0c_identity:
        blockers.append("P5 base provenance is stale for current controller/G0C bytes")
    with policy._acquire_storage_allocation_lock():
        observation = capture_host_observation()
        blockers.extend(_session_container_blockers(observation, volume_uuid=volume_uuid))
        capacity_contract: dict[str, Any] | None = None
        try:
            capacity_contract = _active_capacity_contract(volume_uuid=volume_uuid)
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as error:
            blockers.append(f"P5 signed reserve/quota identity is unavailable: {error}")
        vm_volume = _volume_row(observation, volume_uuid)
        quota_used = vm_volume.get("CapacityInUse")
        stop_profile, stop_profile_blockers = _verify_stop_safety_profile(VM_STOP_SAFETY_PROFILE_PATH)
        blockers.extend(stop_profile_blockers)
        try:
            concurrent_measurement = measure_concurrent_growth_bound(volume_uuid=volume_uuid)
            concurrent_growth_bound = concurrent_measurement.get("observed_max_interval_growth_bytes")
            if not _integer(concurrent_growth_bound):
                concurrent_growth_bound = None
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            concurrent_measurement = {"status": "UNKNOWN", "failure": str(error)}
            concurrent_growth_bound = None
        base_allocated = p3.get("components", {}).get("system_image", {}).get("physical_allocated_bytes")
        if not _integer(quota_used) or not _integer(base_allocated):
            blockers.append("P5 current quota use or sealed system-image allocation is unknown")
        p5_components = {
            "session_system_clone_cow": base_allocated,
            "session_system_runtime_growth": 4 * GIB,
            "read_only_input_tool_raw": GIB,
            "workspace_raw": 512 * MIB,
            "export_capsule_raw_payload": 4 * GIB,
            "runtime_metadata": 256 * MIB,
        }
        growth = sum(value for value in p5_components.values() if _integer(value))
        uncertainty = _phase_uncertainty(growth)
        try:
            free = conservative_session_unreserved_free_bytes(
                observation, volume_uuid=volume_uuid, capacity_contract=capacity_contract)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            free = None
            blockers.append(f"P5 shared physical free is unknown: {error}")
        if _integer(quota_used):
            selected_quota = (capacity_contract or {}).get("quota_bytes")
            result = calculate_vm_phase_admission(
                mode=VM_MODE, container_uuid=EXPECTED_CONTAINER_UUID,
                volume_uuid=volume_uuid, free_bytes=free,
                quota_bytes=selected_quota, quota_used_bytes=quota_used,
                growth_bytes=growth, uncertainty_bytes=uncertainty,
                concurrent_growth_bytes=concurrent_growth_bound,
                expected_quota_bytes=selected_quota if _integer(selected_quota, minimum=1)
                else VM_REVIEWED_QUOTA_MAX_BYTES)
            blockers.extend(result["blockers"])
        else:
            result = {"admitted": False, "blockers": ["VM quota use is unknown"]}
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("session_id") != session_id or marker.get("volume_uuid") != volume_uuid
                or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P4_RECLAIMED"):
            blockers.append("P5 requires the same active session after verified P4 reclamation")
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "decision": "PASS" if not blockers else "FAIL",
            "authorized_phase": "P5" if not blockers else None,
            "session_id": session_id,
            "created_at_unix": time.time(),
            "controller_identity": controller,
            "g0c_identity": g0c_identity,
            "vm_volume_uuid": volume_uuid,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "quota_bytes": (capacity_contract or {}).get("quota_bytes"),
            "reserve_bytes": 0,
            "safety_volume_uuid": (capacity_contract or {}).get("safety_volume_uuid"),
            "safety_volume_in_use_bytes": (capacity_contract or {}).get("safety_volume_in_use_bytes"),
            "safety_container_uuid": (capacity_contract or {}).get("safety_container_uuid"),
            "safety_physical_store_uuid": (capacity_contract or {}).get("safety_physical_store_uuid"),
            "safety_volume_reserve_bytes": VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "quota_used_bytes": quota_used,
            "quota_headroom_bytes": capacity_contract["quota_bytes"] - quota_used
            if capacity_contract and _integer(quota_used) else None,
            "shared_container_free_bytes": free,
            "component_physical_growth_maxima_bytes": p5_components,
            "component_logical_capacities_bytes": {
                "guest_system_raw": VM_SYSTEM_LOGICAL_CAPACITY_BYTES,
                "auxiliary_storage": VM_AUXILIARY_LOGICAL_CAPACITY_BYTES,
                **VM_P5_LOGICAL_LIMITS,
            },
            "growth_bytes": growth,
            "uncertainty_bytes": uncertainty,
            "concurrent_growth_measurement": concurrent_measurement,
            "concurrent_growth_bound_bytes": concurrent_growth_bound,
            "concurrent_growth_authority": "DIAGNOSTIC_ONLY",
            "runtime_writes_require_continuous_signed_per_image_monitor": True,
            "phase_evaluation": result,
            "p3_seal_sha256": _file_sha256(p3_seal_path),
            "p3_seal_path": str(p3_seal_path),
            "p4_cleanup_sha256": _file_sha256(p4_cleanup_receipt_path),
            "blockers": blockers,
            "warnings": ["P5 is runtime-feasibility only and excludes production R6 source/build outputs",
                         "each RAW image still has a fixed logical capacity and remains inside the quota volume"],
        }
        sealed = _write_signed_json(receipt_path, record, "vm_admission_hmac_sha256")
        if not blockers:
            update_active_session_marker(str(session_id), phase_id="P5_AUTHORIZED",
                                         committed_growth_bytes=growth)
        else:
            update_active_session_marker(str(session_id), state="FAILED", phase_id="P5_ADMISSION_FAILED")
        return sealed


def _clone_apfs_file(source: Path, destination: Path) -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("P5 guest-system COW clone requires native APFS clonefile support")
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    clonefile = library.clonefile
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    result = clonefile(os.fsencode(source), os.fsencode(destination), 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))


def create_p5_sparse_raw_asset(*, role: str, logical_capacity_bytes: int,
                               p5_admission_path: Path = VM_P5_ADMISSION_PATH,
                               receipt_root: Path = policy.REPO / "R6-VM-P5-COMPONENTS") -> dict[str, Any]:
    """Create one bounded RAW component or COW system clone after P5 PASS."""
    basename_by_role = {
        "guest_system_raw": "guest-system-p5.raw",
        "read_only_input_tool_raw": "read-only-input.raw",
        "workspace_raw": "workspace.raw",
        "export_capsule_raw": "export-capsule.raw",
    }
    if role not in basename_by_role:
        raise ValueError("P5 RAW role is not in the fixed runtime-feasibility asset allowlist")
    maximum_logical = (VM_SYSTEM_LOGICAL_CAPACITY_BYTES if role == "guest_system_raw"
                       else VM_P5_LOGICAL_LIMITS[role])
    maximum_physical = {
        "read_only_input_tool_raw": GIB,
        "workspace_raw": 512 * MIB,
        "export_capsule_raw": 4 * GIB,
    }.get(role)
    if not _integer(logical_capacity_bytes, minimum=4096) or logical_capacity_bytes > maximum_logical:
        raise ValueError("P5 RAW logical capacity is unknown or exceeds its per-image fixed limit")
    admission = _read_signed_record(p5_admission_path, "vm_admission_hmac_sha256")
    if (admission.get("decision") != "PASS" or admission.get("authorized_phase") != "P5"
            or admission.get("mode") != VM_MODE or admission.get("controller_identity") != _controller_identity()
            or admission.get("component_logical_capacities_bytes", {}).get(role) != maximum_logical):
        raise RuntimeError("P5 RAW creation requires a current signed phase PASS and role limit")
    age = time.time() - float(admission.get("created_at_unix", 0))
    if age < 0 or age > VM_MAX_OBSERVATION_AGE_SECONDS:
        raise RuntimeError("P5 RAW creation admission is stale")
    g0c_identity = verify_fresh_g0c_review()
    if admission.get("g0c_identity") != g0c_identity:
        raise RuntimeError("P5 RAW creation admission does not bind current G0C review")
    if role == "guest_system_raw":
        maxima = admission.get("component_physical_growth_maxima_bytes", {})
        base_cow = maxima.get("session_system_clone_cow") if isinstance(maxima, Mapping) else None
        runtime_growth = maxima.get("session_system_runtime_growth") if isinstance(maxima, Mapping) else None
        if not _integer(base_cow) or not _integer(runtime_growth):
            raise RuntimeError("P5 system clone lacks signed base and runtime physical allocation caps")
        maximum_physical = base_cow + runtime_growth
    assert maximum_physical is not None
    session_id = admission.get("session_id")
    volume_uuid = admission.get("vm_volume_uuid")
    basename = basename_by_role[role]
    with policy._acquire_storage_allocation_lock():
        before = capture_host_observation()
        blockers = _session_container_blockers(before, volume_uuid=volume_uuid)
        if blockers:
            raise RuntimeError("P5 APFS identity changed before RAW creation: " + "; ".join(blockers))
        marker = _read_signed_record(VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        if (marker.get("session_id") != session_id or marker.get("volume_uuid") != volume_uuid
                or marker.get("state") != "ACTIVE"
                or not str(marker.get("phase_id", "")).startswith("P5_")):
            raise RuntimeError("P5 RAW creation requires the same active P5 session")
        mount = _volume_mount_info(before, volume_uuid)
        if not mount.get("mounted"):
            raise RuntimeError("P5 VM quota volume is not mounted")
        p3_path_text = admission.get("p3_seal_path")
        if role == "guest_system_raw":
            if not isinstance(p3_path_text, str):
                raise RuntimeError("P5 system clone admission has no P3 seal path")
            p3_path = Path(p3_path_text)
            if not p3_path.is_absolute():
                p3_path = policy.REPO / p3_path
            p3 = _read_signed_record(p3_path, "sealed_base_hmac_sha256")
            if (_file_sha256(p3_path) != admission.get("p3_seal_sha256")
                    or p3.get("session_id") != session_id
                    or p3.get("vm_volume_uuid") != volume_uuid
                    or p3.get("status") != "PASS"):
                raise RuntimeError("P5 system clone does not bind the sealed same-session P3 base")
            source_record = _hash_bound_volume_file(
                Path(mount["mount_point"]), "guest-system.raw", VM_SYSTEM_LOGICAL_CAPACITY_BYTES)
            expected_source = p3.get("components", {}).get("system_image", {})
            if (source_record.get("sha256") != expected_source.get("sha256")
                    or source_record.get("physical_allocated_bytes") != expected_source.get("physical_allocated_bytes")):
                raise RuntimeError("P5 system clone source differs from the sealed P3 system image")
        root_fd = policy._open_directory_nofollow(Path(mount["mount_point"]))
        fd: int | None = None
        try:
            if role == "guest_system_raw":
                destination = Path(mount["mount_point"]) / basename
                try:
                    os.stat(basename, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError("P5 system clone destination already exists")
                _clone_apfs_file(Path(mount["mount_point"]) / "guest-system.raw", destination)
                fd = os.open(basename, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
            else:
                fd = os.open(basename,
                             os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=root_fd)
            before_file = os.fstat(fd)
            if (not stat.S_ISREG(before_file.st_mode) or before_file.st_nlink != 1
                    or before_file.st_dev != os.fstat(root_fd).st_dev):
                raise RuntimeError("P5 RAW path did not create a new regular file on the VM volume")
            if role == "guest_system_raw":
                if before_file.st_size != logical_capacity_bytes:
                    raise RuntimeError("P5 COW system clone logical capacity differs from its P3 base")
            else:
                os.ftruncate(fd, logical_capacity_bytes)
                os.fsync(fd)
            after_file = os.fstat(fd)
            if (after_file.st_size != logical_capacity_bytes
                    or (after_file.st_dev, after_file.st_ino) != (before_file.st_dev, before_file.st_ino)):
                raise RuntimeError("P5 RAW logical capacity or inode read-back failed")
            identity = {
                "device": after_file.st_dev,
                "inode": after_file.st_ino,
                "logical_capacity_bytes": after_file.st_size,
                "physical_allocated_bytes": getattr(after_file, "st_blocks", 0) * 512,
                "mtime_ns": after_file.st_mtime_ns,
                "ctime_ns": getattr(after_file, "st_ctime_ns", 0),
            }
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        after = capture_host_observation()
        after_blockers = _session_container_blockers(after, volume_uuid=volume_uuid)
        vm_used_before = _volume_row(before, volume_uuid).get("CapacityInUse")
        vm_used_after = _volume_row(after, volume_uuid).get("CapacityInUse")
        if (not _integer(vm_used_before) or not _integer(vm_used_after)
                or vm_used_after > _active_capacity_contract(volume_uuid=volume_uuid)["quota_bytes"]
                or identity["physical_allocated_bytes"] > maximum_physical
                or (vm_used_after - vm_used_before) > maximum_physical
                or conservative_session_unreserved_free_bytes(
                    after, volume_uuid=volume_uuid) <= VM_OPERATIONAL_WARNING_FREE):
            after_blockers.append("P5 RAW creation caused unknown/over-quota use or crossed the VM stop trigger")
        if after_blockers:
            update_active_session_marker(str(session_id), state="FAILED", phase_id="P5_RAW_READBACK_FAILED")
            raise RuntimeError("P5 RAW creation failed post-write safety read-back: " + "; ".join(after_blockers))
        raw_record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS",
            "session_id": session_id,
            "phase_id": "P5",
            "role": role,
            "volume_uuid": volume_uuid,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "basename": basename,
            "logical_capacity_bytes": logical_capacity_bytes,
            "maximum_logical_capacity_bytes": maximum_logical,
            "maximum_physical_allocation_bytes": maximum_physical,
            "physical_allocated_bytes": identity["physical_allocated_bytes"],
            "file_identity": identity,
            "quota_bytes": _active_capacity_contract(volume_uuid=volume_uuid)["quota_bytes"],
            "vm_volume_usage_before_bytes": vm_used_before,
            "vm_volume_usage_after_bytes": vm_used_after,
            "shared_container_free_before_bytes": conservative_session_unreserved_free_bytes(
                before, volume_uuid=volume_uuid),
            "shared_container_free_after_bytes": conservative_session_unreserved_free_bytes(
                after, volume_uuid=volume_uuid),
            "p5_admission_sha256": _file_sha256(p5_admission_path),
            "p3_seal_sha256": admission.get("p3_seal_sha256") if role == "guest_system_raw" else None,
            "controller_identity": _controller_identity(),
            "g0c_identity": g0c_identity,
            "blockers": [],
        }
        safe_role = role.replace("_", "-")
        receipt_path = receipt_root / f"{session_id}-{safe_role}.json"
        sealed = _write_signed_json(receipt_path, raw_record, "vm_component_hmac_sha256")
        update_active_session_marker(str(session_id), phase_id=f"P5_{safe_role.upper()}_CREATED",
                                     committed_growth_bytes=admission["growth_bytes"])
        return sealed


def _check_p1_renewal_freshness_now(
        renewal: Mapping[str, Any], fresh_metadata: Mapping[str, Any], *,
        monotonic_at_seal: float | None = None,
        now: float | None = None, monotonic_now: float | None = None) -> float:
    """Apply the 300-second boundary, including same-process monotonic elapsed time."""
    _captured_at, age = _require_fresh_timestamp(
        fresh_metadata.get("captured_at_unix"),
        label="P1 renewal metadata at restore-start boundary",
        maximum_age_seconds=VM_MAX_RESTORE_METADATA_AGE_SECONDS, now=now)
    effective_age = age
    if monotonic_at_seal is not None:
        monotonic_current = time.monotonic() if monotonic_now is None else monotonic_now
        age_at_seal = renewal.get("fresh_metadata", {}).get("age_at_renewal_seconds")
        if (not isinstance(monotonic_at_seal, (int, float)) or isinstance(monotonic_at_seal, bool)
                or not isinstance(monotonic_current, (int, float)) or isinstance(monotonic_current, bool)
                or not isinstance(age_at_seal, (int, float)) or isinstance(age_at_seal, bool)):
            raise RuntimeError("P1 renewal monotonic start clock is unavailable")
        try:
            elapsed = float(monotonic_current) - float(monotonic_at_seal)
            monotonic_age = float(age_at_seal) + elapsed
        except (OverflowError, TypeError, ValueError) as error:
            raise RuntimeError("P1 renewal monotonic start clock is invalid") from error
        if (not math.isfinite(elapsed) or elapsed < 0 or not math.isfinite(monotonic_age)
                or monotonic_age < 0 or monotonic_age > VM_MAX_RESTORE_METADATA_AGE_SECONDS):
            raise RuntimeError("P1 renewal expired before first body acceptance")
        effective_age = max(age, monotonic_age)
    return effective_age


def _record_p1_freshness_start(
        authority: Mapping[str, Any], *, renewal: Mapping[str, Any] | None,
        renewal_path: Path | None, renewal_path_text: str | None,
        renewal_sha256: str | None, renewal_binding: Mapping[str, Any] | None,
        fresh_metadata: Mapping[str, Any] | None,
        fresh_metadata_path: Path | None, fresh_metadata_path_text: str | None,
        fresh_metadata_receipt_sha256: str | None,
        renewal_subject: Mapping[str, Any] | None, attempt_id: str,
        first_body_read_at_unix: float, first_body_read_bytes: int,
        body_bytes_accepted: bool, age_seconds: float | None,
        source_url: str, final_url: str, expected_length: int,
        live_storage: Mapping[str, Any], response_boundary: Mapping[str, Any],
        metadata_ages: Mapping[str, Any], post_read_freshness: Mapping[str, Any],
        freshness_blockers: Sequence[str] = (), storage_blockers: Sequence[str] = (),
        blockers: Sequence[str] = (), evidence_ledger: dict[str, Any] | None = None
        ) -> dict[str, Any]:
    """Seal a fully checked first-body boundary against this exact consumed ticket."""
    if evidence_ledger is None:
        evidence_ledger = {}

    def reject(message: str) -> None:
        raise RuntimeError(message)

    admission = authority.get("admission")
    base_marker = authority.get("marker")
    if not isinstance(admission, Mapping) or not isinstance(base_marker, Mapping):
        reject("P1 body-start event lacks its verified base authority")
    if (not isinstance(renewal, Mapping) or not isinstance(renewal_path, Path)
            or not isinstance(renewal_path_text, str)
            or not isinstance(renewal_sha256, str)
            or not isinstance(renewal_binding, Mapping)):
        reject("P1 body-start event lacks its exact consumed freshness renewal")
    if (not isinstance(fresh_metadata, Mapping)
            or not isinstance(fresh_metadata_path, Path)
            or not isinstance(fresh_metadata_path_text, str)
            or not isinstance(fresh_metadata_receipt_sha256, str)
            or not isinstance(renewal_subject, Mapping)):
        reject("P1 body-start event lacks its exact consumed metadata and subject binding")

    session_id = admission.get("session_id")
    volume_uuid = admission.get("vm_volume_uuid")
    p1_path_text = authority.get("p1_path_text")
    p1_sha256 = authority.get("p1_sha256")
    review_path_text = authority.get("p1_review_path_text")
    review_sha256 = authority.get("p1_review_sha256")
    p1_path, canonical_p1_path = _repo_artifact_path(authority.get("p1_path", ""))
    review_path, canonical_review_path = _repo_artifact_path(
        authority.get("p1_review_path", review_path_text or ""))
    supplied_p1_path = authority.get("p1_path")
    supplied_review_path = authority.get("p1_review_path")
    if (not isinstance(supplied_p1_path, (Path, str))
            or p1_path != Path(os.path.abspath(supplied_p1_path))
            or (supplied_review_path is not None
                and (not isinstance(supplied_review_path, (Path, str))
                     or review_path != Path(os.path.abspath(supplied_review_path))))
            or canonical_p1_path != p1_path_text or canonical_review_path != review_path_text
            or not re.fullmatch(r"[0-9a-f]{64}", str(p1_sha256))
            or not re.fullmatch(r"[0-9a-f]{64}", str(review_sha256))
            or not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id)
            or not isinstance(volume_uuid, str)
            or admission.get("authorization") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or admission.get("authorized_phase") != "P1"
            or admission.get("p1_authorized") is not True
            or admission.get("restore_download_authorized") is not True
            or admission.get("p2_authorized") is not False
            or admission.get("vm_start_authorized") is not False):
        reject("P1 body-start event has invalid or expanded admission authority")
    if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
        reject("P1 body-start event attempt identity is malformed")

    selected_renewal_path, canonical_renewal_path = _repo_artifact_path(renewal_path)
    if (selected_renewal_path != Path(os.path.abspath(renewal_path))
            or canonical_renewal_path != renewal_path_text
            or renewal.get("receipt_path") != renewal_path_text
            or not re.fullmatch(r"[0-9a-f]{64}", renewal_sha256)
            or renewal_binding.get("path") != renewal_path_text
            or renewal_binding.get("sha256") != renewal_sha256
            or renewal_binding.get("sequence") != renewal.get("sequence")
            or renewal_binding.get("predecessor_path") != renewal.get("predecessor_path")
            or renewal_binding.get("predecessor_sha256") != renewal.get("predecessor_sha256")
            or renewal_binding.get("use_state") != "CONSUMED"
            or renewal_binding.get("attempt_id") != attempt_id
            or renewal_binding.get("body_start_evidence") is not None):
        reject("P1 body-start event does not identify the exact consumed renewal claim")
    if (not _integer(renewal.get("sequence"), minimum=1)
            or ((renewal.get("predecessor_path") is None)
                != (renewal.get("predecessor_sha256") is None))
            or (renewal.get("predecessor_sha256") is not None
                and not re.fullmatch(r"[0-9a-f]{64}", str(renewal.get("predecessor_sha256"))))):
        reject("P1 body-start event renewal sequence or predecessor is malformed")
    signed_renewal, _renewal_raw, signed_renewal_sha, _renewal_identity = (
        _read_signed_record_snapshot(
            selected_renewal_path, VM_P1_FRESHNESS_RENEWAL_SIGNATURE_FIELD))
    if signed_renewal_sha != renewal_sha256 or signed_renewal != dict(renewal):
        reject("P1 body-start event renewal bytes differ from the exact verified object")

    expected_p1 = {"path": p1_path_text, "sha256": p1_sha256}
    expected_review = {"path": review_path_text, "sha256": review_sha256}
    if (renewal.get("original_p1") != expected_p1
            or renewal.get("original_p1_review") != expected_review
            or renewal.get("session_id") != session_id
            or renewal.get("vm_volume_uuid") != volume_uuid
            or renewal.get("retained_authority") != "DOWNLOAD_AND_VERIFY_RESTORE_ONLY"
            or renewal.get("renewal_scope") != "EXECUTION_TIME_FRESHNESS_ONLY"
            or any(renewal.get(field) is not False for field in (
                "p1_authorized", "restore_download_authorized", "p2_authorized",
                "vm_start_authorized"))):
        reject("P1 body-start event renewal crosses its P1, review, session, or scope binding")
    if (renewal.get("restore_subject_identity") != admission.get("restore_subject_identity")
            or renewal.get("restore_subject") != admission.get("restore_subject")
            or renewal_subject != renewal.get("restore_subject")
            or renewal.get("subject_equality") != {
                "p1_admission_equal": True, "compatibility_basis_equal": True}
            or not isinstance(authority.get("compatibility_basis"), Mapping)
            or renewal.get("restore_subject_identity")
            != authority["compatibility_basis"].get("restore_subject_identity")
            or renewal.get("restore_subject")
            != authority["compatibility_basis"].get("restore_subject")):
        reject("P1 body-start event stable restore subject differs from admitted subject")

    fresh_ref = renewal.get("fresh_metadata")
    selected_fresh_path, canonical_fresh_path = _repo_artifact_path(fresh_metadata_path)
    if (not isinstance(fresh_ref, Mapping)
            or selected_fresh_path != Path(os.path.abspath(fresh_metadata_path))
            or canonical_fresh_path != fresh_metadata_path_text
            or fresh_ref.get("path") != fresh_metadata_path_text
            or fresh_ref.get("receipt_sha256") != fresh_metadata_receipt_sha256
            or fresh_ref.get("observation_sha256") != fresh_metadata.get("metadata_sha256")
            or fresh_ref.get("captured_at_unix") != fresh_metadata.get("captured_at_unix")
            or not re.fullmatch(r"[0-9a-f]{64}", fresh_metadata_receipt_sha256)
            or renewal.get("current_controller_identity") != authority.get("current_controller_identity")
            or renewal.get("current_g0c_identity") != authority.get("g0c_identity")):
        reject("P1 body-start event metadata leaf or controller/G0C binding differs")
    current_controller = _controller_identity()
    current_g0c = verify_fresh_g0c_review()
    if (current_controller != authority.get("current_controller_identity")
            or current_g0c != authority.get("g0c_identity")
            or renewal.get("current_controller_identity") != current_controller
            or renewal.get("current_g0c_identity") != current_g0c):
        reject("P1 body-start event controller or current G0C identity changed")

    expected_source_url = fresh_metadata.get("source_url")
    expected_final_url = fresh_metadata.get("final_url")
    expected_subject_length = fresh_metadata.get("expected_length_bytes")
    response_length_values = response_boundary.get("raw_content_length_values")
    parsed_response_length = response_boundary.get("content_length_bytes")
    redirect_class = response_boundary.get("redirect_chain_classification")
    redirect_count = response_boundary.get("redirect_count")
    redirect_target = response_boundary.get("redirect_target")
    redirect_attempts = response_boundary.get("redirect_attempts")
    raw_final_url = response_boundary.get("raw_final_url")
    if (not _integer(expected_length, minimum=1)
            or expected_length != expected_subject_length
            or source_url != expected_source_url
            or final_url != _canonical_apple_restore_url(str(expected_final_url))
            or not isinstance(raw_final_url, str)
            or _canonical_apple_restore_url(raw_final_url) != final_url
            or response_boundary.get("http_status") != 200
            or response_boundary.get("canonical_final_url") != final_url
            or response_boundary.get("expected_metadata_final_url") != expected_final_url
            or response_boundary.get("final_url_bound_to_metadata") is not True
            or response_boundary.get("response_validated") is not True
            or not isinstance(response_length_values, list)
            or len(response_length_values) != 1
            or not isinstance(response_length_values[0], str)
            or not re.fullmatch(r"[0-9]+", response_length_values[0])
            or parsed_response_length != expected_length
            or int(response_length_values[0]) != expected_length
            or response_boundary.get("content_length_parse_status") != "VALID_SINGLE_VALUE"
            or not isinstance(redirect_attempts, list)
            or (redirect_class == "DIRECT"
                and (redirect_count != 0 or redirect_target is not None or redirect_attempts))
            or (redirect_class == "ONE_EXACT_METADATA_BOUND_HOP"
                and (redirect_count != 1 or redirect_target != expected_final_url
                     or len(redirect_attempts) != 1
                     or redirect_attempts[0].get("target_url") != expected_final_url))
            or redirect_class not in {"DIRECT", "ONE_EXACT_METADATA_BOUND_HOP"}):
        reject("P1 body-start event response does not prove the exact renewed restore object")
    if (not isinstance(first_body_read_at_unix, (int, float))
            or isinstance(first_body_read_at_unix, bool)
            or not math.isfinite(float(first_body_read_at_unix))
            or not _integer(first_body_read_bytes, minimum=1)
            or (body_bytes_accepted and first_body_read_bytes > expected_length)):
        reject("P1 body-start event first non-empty read observation is malformed")

    if not isinstance(live_storage, Mapping):
        reject("P1 body-start event lacks its post-read live-storage result")
    live_status = live_storage.get("status")
    live_blockers = list(storage_blockers)
    reported_live_blockers = live_storage.get("blockers")
    if (not isinstance(reported_live_blockers, list)
            or reported_live_blockers != live_blockers):
        reject("P1 body-start event live-storage blockers differ from the observed result")
    if live_status == "PASS":
        live_observation = live_storage.get("observation")
        live_mount = live_storage.get("mount")
        live_capacity_sample = live_storage.get("capacity_sample")
        if (live_blockers or reported_live_blockers or live_storage.get("session_id") != session_id
                or live_storage.get("vm_volume_uuid") != volume_uuid
                or live_storage.get("safety_volume_uuid") != admission.get("safety_volume_uuid")
                or live_storage.get("container_uuid") != EXPECTED_CONTAINER_UUID
                or live_storage.get("physical_store_uuid") != EXPECTED_PHYSICAL_STORE_UUID
                or live_storage.get("quota_bytes") != admission.get("vm_volume_quota_bytes")
                or live_storage.get("safety_reserve_bytes") != base_marker.get("safety_reserve_bytes")
                or live_storage.get("restore_payload_absent") is not True
                or not _integer(live_storage.get("unreserved_free_bytes"))
                or not _integer(live_storage.get("vm_volume_used_bytes"))
                or not isinstance(live_storage.get("captured_at_unix"), (int, float))
                or isinstance(live_storage.get("captured_at_unix"), bool)
                or not math.isfinite(float(live_storage.get("captured_at_unix")))
                or not isinstance(live_observation, Mapping)
                or live_observation.get("captured_at_unix") != live_storage.get("captured_at_unix")
                or not isinstance(live_mount, Mapping)
                or live_mount.get("mounted") is not True
                or live_mount.get("mount_state") != "MOUNTED_VERIFIED"
                or live_mount.get("volume_uuid") != volume_uuid.upper()
                or live_mount.get("container_uuid") != EXPECTED_CONTAINER_UUID.upper()
                or live_mount.get("filesystem_type") != "apfs"
                or not isinstance(live_mount.get("mount_point"), str)
                or not isinstance(live_capacity_sample, Mapping)
                or live_capacity_sample.get("admitted") is not True
                or live_capacity_sample.get("blockers") != []):
            reject("P1 body-start event live storage result is not the exact current PASS")
        try:
            _require_fresh_timestamp(
                live_observation.get("captured_at_unix"),
                label="P1 body-start live storage source observation",
                maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
            if _session_container_blockers(live_observation, volume_uuid=volume_uuid):
                reject("P1 body-start live storage source observation is blocked")
            vm_volume = _volume_row(live_observation, volume_uuid)
            safety_volume = _volume_row(
                live_observation, str(admission.get("safety_volume_uuid")))
            if (vm_volume.get("CapacityInUse") != live_storage.get("vm_volume_used_bytes")
                    or vm_volume.get("CapacityQuota") != live_storage.get("quota_bytes")
                    or safety_volume.get("CapacityReserve")
                    != live_storage.get("safety_reserve_bytes")
                    or conservative_session_unreserved_free_bytes(
                        live_observation, volume_uuid=volume_uuid)
                    != live_storage.get("unreserved_free_bytes")):
                reject("P1 body-start live storage fields differ from the source observation")
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
            reject(f"P1 body-start live storage source observation is invalid: {error}")
        _require_fresh_timestamp(
            live_storage.get("captured_at_unix"), label="P1 body-start live storage observation",
            maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
    elif live_status != "FAIL" or not live_blockers:
        reject("P1 body-start event live storage result is missing or malformed")

    freshness_status = post_read_freshness.get("status")
    freshness_blockers = list(freshness_blockers)
    reported_freshness_blockers = post_read_freshness.get("blockers")
    if (freshness_status not in {"PASS", "FAIL"}
            or not isinstance(reported_freshness_blockers, list)
            or reported_freshness_blockers != freshness_blockers):
        reject("P1 body-start event post-read freshness result is missing")
    verifier_age = metadata_ages.get("verifier_entry")
    after_headers_age = metadata_ages.get("after_headers")
    before_read_age = metadata_ages.get("immediately_before_first_read")
    after_read_age = metadata_ages.get("immediately_after_first_nonempty_read")
    if (not isinstance(verifier_age, Mapping)
            or verifier_age.get("check_result") != "PASS"
            or not isinstance(verifier_age.get("age_seconds"), (int, float))
            or isinstance(verifier_age.get("age_seconds"), bool)
            or not 0 <= verifier_age.get("age_seconds") <= VM_MAX_RESTORE_METADATA_AGE_SECONDS
            or not isinstance(after_headers_age, Mapping)
            or after_headers_age.get("check_result") != "PASS"
            or not isinstance(after_headers_age.get("age_seconds"), (int, float))
            or after_headers_age.get("blockers") != []
            or not isinstance(before_read_age, Mapping)
            or before_read_age.get("check_result") != "PASS"
            or not isinstance(before_read_age.get("age_seconds"), (int, float))
            or before_read_age.get("blockers") != []
            or not isinstance(after_read_age, Mapping)
            or after_read_age.get("check_result") != freshness_status
            or after_read_age.get("observed_at_unix") != first_body_read_at_unix
            or after_read_age.get("age_seconds") != post_read_freshness.get("age_seconds")
            or after_read_age.get("blockers") != freshness_blockers):
        reject("P1 body-start event metadata-age checkpoints are incomplete or inconsistent")
    if body_bytes_accepted:
        if (freshness_status != "PASS" or freshness_blockers or live_status != "PASS"
                or blockers
                or not isinstance(age_seconds, (int, float)) or isinstance(age_seconds, bool)
                or not math.isfinite(float(age_seconds)) or age_seconds < 0
                or age_seconds > VM_MAX_RESTORE_METADATA_AGE_SECONDS):
            reject("P1 first body block cannot be accepted without fresh metadata and live storage")
    elif not blockers:
        reject("P1 rejected first body block must preserve its observed blocker")

    marker, _marker_raw, marker_sha, _marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    base_binding = base_marker.get("p1_freshness_renewal")
    if (base_marker.get("state") != "ACTIVE" or base_marker.get("phase_id") != "P1"
            or base_marker.get("session_id") != session_id
            or base_marker.get("volume_uuid") != volume_uuid
            or base_marker.get("p1_admission_path") != p1_path_text
            or base_marker.get("p1_admission_sha256") != p1_sha256
            or not isinstance(base_binding, Mapping)
            or dict(base_binding) != dict(renewal_binding)):
        reject("P1 verified base authority no longer matches the consumed renewal claim")
    current_binding = marker.get("p1_freshness_renewal")
    if (marker_sha != authority.get("marker_sha256")
            or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P1"
            or marker.get("session_id") != session_id
            or marker.get("volume_uuid") != volume_uuid
            or marker.get("p1_admission_path") != p1_path_text
            or marker.get("p1_admission_sha256") != p1_sha256
            or not isinstance(current_binding, Mapping)
            or dict(current_binding) != dict(renewal_binding)
            or current_binding.get("use_state") != "CONSUMED"
            or current_binding.get("attempt_id") != attempt_id
            or current_binding.get("body_start_evidence") is not None):
        reject("P1 marker no longer binds the exact unbound consumed body-start attempt")

    run_id = _new_run_id()
    path, path_text = _repo_artifact_path(
        _fresh_artifact_path(VM_P1_FRESHNESS_START_PREFIX, run_id))
    evidence_ledger.update({
        "start_evidence_path": path_text,
        "start_evidence_sha256": "unknown",
        "start_evidence_creation_state": "NOT_ATTEMPTED",
        "start_evidence_created": False,
        "start_evidence_marker_bound": False,
    })
    record = {
        "schema_version": 1,
        "mode": VM_MODE,
        "record_type": "R6_VM_P1_FRESHNESS_START",
        "status": "PASS_FIRST_BODY_ACCEPTED" if body_bytes_accepted else "FAIL_PRE_BODY_START",
        "authority_effect": "EVIDENCE_ONLY_NO_AUTHORITY_CHANGE",
        "p1_authorized": False,
        "restore_download_authorized": False,
        "p2_authorized": False,
        "vm_start_authorized": False,
        "session_id": session_id,
        "volume_uuid": volume_uuid,
        "original_p1_path": p1_path_text,
        "original_p1_sha256": p1_sha256,
        "original_p1_review_path": review_path_text,
        "original_p1_review_sha256": review_sha256,
        "renewal_path": renewal_path_text,
        "renewal_sha256": renewal_sha256,
        "renewal_sequence": renewal["sequence"],
        "renewal_predecessor_path": renewal["predecessor_path"],
        "renewal_predecessor_sha256": renewal["predecessor_sha256"],
        "renewal_use_state": renewal_binding["use_state"],
        "attempt_id": attempt_id,
        "restore_subject_identity": renewal["restore_subject_identity"],
        "source_url": source_url,
        "final_url": final_url,
        "content_length_bytes": expected_length,
        "first_body_read_at_unix": first_body_read_at_unix,
        "first_body_read_bytes": first_body_read_bytes,
        "body_bytes_accepted": body_bytes_accepted,
        "metadata_age_at_first_body_read_seconds": age_seconds,
        "fresh_metadata_path": fresh_metadata_path_text,
        "fresh_metadata_receipt_sha256": fresh_metadata_receipt_sha256,
        "fresh_metadata_observation_sha256": fresh_metadata["metadata_sha256"],
        "current_controller_identity": current_controller,
        "current_g0c_identity": current_g0c,
        "response_boundary": dict(response_boundary),
        "metadata_ages": dict(metadata_ages),
        "post_read_freshness": dict(post_read_freshness),
        "live_storage_revalidation": dict(live_storage),
        "post_read_freshness_blockers": list(freshness_blockers),
        "post_read_storage_blockers": live_blockers,
        "blockers": list(blockers),
        "receipt_path": path_text,
    }
    evidence_ledger["start_evidence_creation_state"] = "WRITE_ATTEMPTED"
    try:
        sealed = _write_signed_json_unique(path, record, VM_P1_FRESHNESS_START_SIGNATURE_FIELD)
    except BaseException:
        evidence_parent_fd: int | None = None
        try:
            evidence_parent_fd = policy._open_directory_nofollow(path.parent)
            os.stat(path.name, dir_fd=evidence_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            evidence_ledger.update({
                "start_evidence_creation_state": "NOT_CREATED",
                "start_evidence_created": False,
            })
        except OSError:
            evidence_ledger.update({
                "start_evidence_creation_state": "UNKNOWN_WRITE_OUTCOME",
                "start_evidence_created": "unknown",
            })
        else:
            evidence_ledger.update({
                "start_evidence_creation_state": "UNKNOWN_WRITE_OUTCOME",
                "start_evidence_created": "unknown",
            })
        finally:
            if evidence_parent_fd is not None:
                os.close(evidence_parent_fd)
        raise
    evidence_ledger["start_evidence_creation_state"] = "WRITTEN_UNBOUND"
    evidence_ledger["start_evidence_created"] = True
    event_sha = _file_sha256(path)
    evidence_ledger["start_evidence_sha256"] = event_sha
    try:
        readback, _raw, readback_sha, _identity = _read_signed_record_snapshot(
            path, VM_P1_FRESHNESS_START_SIGNATURE_FIELD)
    except BaseException:
        evidence_ledger["start_evidence_creation_state"] = "READBACK_FAILED_UNBOUND"
        raise
    if readback != sealed or readback_sha != event_sha:
        evidence_ledger["start_evidence_creation_state"] = "READBACK_FAILED_UNBOUND"
        raise RuntimeError("P1 first-body evidence failed exact signed read-back")
    evidence_ledger["start_evidence_creation_state"] = "READBACK_VERIFIED_UNBOUND"
    marker, _marker_raw, marker_sha, _marker_identity = _read_signed_record_snapshot(
        VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
    binding = marker.get("p1_freshness_renewal")
    if (marker_sha != authority.get("marker_sha256")
            or marker.get("state") != "ACTIVE" or marker.get("phase_id") != "P1"
            or marker.get("session_id") != session_id
            or marker.get("volume_uuid") != volume_uuid
            or marker.get("p1_admission_path") != p1_path_text
            or marker.get("p1_admission_sha256") != p1_sha256
            or not isinstance(binding, Mapping)
            or dict(binding) != dict(renewal_binding)):
        raise RuntimeError("P1 marker drifted after first-body evidence read-back")
    updated_binding = dict(binding)
    updated_binding["body_start_evidence"] = {"path": path_text, "sha256": event_sha}
    evidence_ledger["start_evidence_creation_state"] = "MARKER_CAS_ATTEMPTED"
    update_active_session_marker(
        str(session_id), p1_freshness_renewal=updated_binding,
        expected_marker_sha256=marker_sha)
    evidence_ledger["start_evidence_creation_state"] = "MARKER_BOUND"
    evidence_ledger["start_evidence_marker_bound"] = True
    return {"path": path, "path_text": path_text, "sha256": event_sha, "record": sealed}


def _renewal_live_boundary(authority: Mapping[str, Any]) -> dict[str, Any]:
    """Recheck session/storage identity immediately before accepting body bytes."""
    try:
        storage = _capture_p1_renewal_live_storage(authority)
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {"status": "FAIL", "blockers": [str(error)]}
    return {"status": "PASS", **storage, "blockers": []}


def download_and_verify_restore(p1_admission_path: Path | None = None,
                                *, p1_review_path: Path | None = None,
                                receipt_path: Path = VM_RESTORE_VERIFICATION_PATH,
                                _renewal_monotonic_at_seal: float | None = None) -> dict[str, Any]:
    """Consume one marker-bound P1 freshness renewal, then stream and hash its exact subject."""
    authority = _verify_p1_download_authority(p1_admission_path, p1_review_path)
    admission = authority["admission"]
    fresh = authority["renewal_fresh_metadata"]
    renewal = authority["renewal"]
    source_url = fresh["source_url"]
    expected_final_url = fresh["final_url"]
    expected_length = fresh["expected_length_bytes"]
    metadata_digest = fresh["metadata_sha256"]
    concurrent_growth_bound = admission.get("concurrent_growth_bound_bytes")
    if not _integer(concurrent_growth_bound):
        concurrent_growth_bound = None
    restore_metadata = validate_restore_metadata(
        source_url, expected_length, metadata_digest, expected_final_url)
    g0c_identity = authority["g0c_identity"]
    session_id = admission.get("session_id")
    volume_uuid = admission.get("vm_volume_uuid")
    with policy._acquire_storage_allocation_lock():
        base_authority = _verify_p1_download_authority(
            p1_admission_path, p1_review_path, require_freshness_renewal=False)
        if (base_authority["p1_sha256"] != authority["p1_sha256"]
                or base_authority["marker_sha256"] != authority["marker_sha256"]):
            raise RuntimeError("P1 marker or historical authority changed before renewal consumption")
        ready = _verify_p1_freshness_renewal(base_authority)
        if ready["renewal_sha256"] != authority["renewal_sha256"]:
            raise RuntimeError("P1 freshness renewal changed before download preflight")
        observation = capture_host_observation()
        _require_fresh_timestamp(observation.get("captured_at_unix"),
                                 label="P1 download live APFS observation",
                                 maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
        blockers = _session_container_blockers(observation, volume_uuid=volume_uuid)
        try:
            mount = _volume_mount_info(observation, volume_uuid)
            free_before = conservative_session_unreserved_free_bytes(
                observation, volume_uuid=volume_uuid)
            volume = _volume_row(observation, volume_uuid)
            quota_used_before = volume.get("CapacityInUse")
            if not _integer(quota_used_before):
                raise ValueError("VM volume CapacityInUse is unknown")
        except (OSError, RuntimeError, ValueError) as error:
            blockers.append(f"P1 volume identity or quota measurement failed: {error}")
            mount = {}
            free_before = None
            quota_used_before = None
        if blockers:
            update_active_session_marker(str(session_id), state="FAILED", phase_id="P1_PREFLIGHT_FAILED")
            raise RuntimeError("P1 restore preflight blocked: " + "; ".join(blockers))
        mount_path = Path(mount["mount_point"])
        restore_path = mount_path / "restore.ipsw"
        temporary_path = mount_path / f".restore.ipsw.partial-{session_id}"
        root_fd = policy._open_directory_nofollow(mount_path)
        try:
            root_info = os.fstat(root_fd)
            for name in (restore_path.name, temporary_path.name):
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise FileExistsError(f"P1 restore destination already exists: {mount_path / name}")
        except BaseException:
            os.close(root_fd)
            raise
        output_fd = -1
        output_info: os.stat_result | None = None
        hasher = hashlib.sha256()
        bytes_written = 0
        body_started = False
        start_evidence: dict[str, Any] | None = None
        attempt_id = uuid.uuid4().hex
        unknown = "unknown"
        response_boundary: dict[str, Any] = {
            "http_status": unknown,
            "raw_final_url": unknown,
            "canonical_final_url": unknown,
            "expected_metadata_final_url": expected_final_url,
            "final_url_bound_to_metadata": unknown,
            "raw_content_length_values": unknown,
            "content_length_bytes": unknown,
            "content_length_parse_status": unknown,
            "etag": unknown,
            "last_modified": unknown,
            "redirect_count": unknown,
            "redirect_target": unknown,
            "redirect_attempts": unknown,
            "redirect_chain_classification": unknown,
            "response_validated": unknown,
            "first_body_read_at_unix": unknown,
            "first_body_chunk_length_bytes": unknown,
            "first_body_bytes_accepted": False,
        }
        response_info: dict[str, Any] = {}
        metadata_ages: dict[str, Any] = {
            "verifier_entry": {
                "observed_at_unix": authority.get(
                    "renewal_fresh_metadata_verifier_entry_at_unix", unknown),
                "age_seconds": authority.get(
                    "renewal_fresh_metadata_verifier_entry_age_seconds", unknown),
                "check_result": "PASS" if isinstance(
                    authority.get("renewal_fresh_metadata_verifier_entry_age_seconds"),
                    (int, float)) else unknown,
                "blockers": [],
            },
            "after_headers": unknown,
            "immediately_before_first_read": unknown,
            "immediately_after_first_nonempty_read": unknown,
            "before_redirect": [],
        }
        post_read_freshness: dict[str, Any] = {"status": unknown, "age_seconds": unknown,
                                               "observed_at_unix": unknown, "blockers": []}
        post_read_freshness_blockers: list[str] = []
        post_read_storage: dict[str, Any] = {"status": unknown, "blockers": []}
        post_read_storage_blockers: list[str] = []
        evidence_ledger: dict[str, Any] = {
            "stage": "PRE_REQUEST",
            "failure_stage": unknown,
            "first_body_read_at_unix": unknown,
            "first_body_chunk_length_bytes": unknown,
            "first_body_bytes_accepted": False,
            "start_evidence_path": unknown,
            "start_evidence_sha256": unknown,
            "start_evidence_creation_state": "NOT_ATTEMPTED",
            "start_evidence_created": False,
            "start_evidence_marker_bound": False,
            "output_file_presence": {"temporary": unknown, "restore": unknown},
        }
        consumed: dict[str, Any] | None = None
        redirect_handler: _AppleOnlyRedirectHandler | None = None

        def checkpoint_age(name: str, *, now: float | None = None) -> float:
            observed_at = time.time() if now is None else now
            checkpoint: dict[str, Any] = {
                "observed_at_unix": observed_at,
                "age_seconds": unknown,
                "check_result": "unknown",
                "blockers": [],
            }
            try:
                age_value = _check_p1_renewal_freshness_now(
                    renewal, fresh, monotonic_at_seal=_renewal_monotonic_at_seal,
                    now=now)
            except RuntimeError as error:
                checkpoint.update({"check_result": "FAIL", "blockers": [str(error)]})
                metadata_ages[name] = checkpoint
                raise
            checkpoint.update({"age_seconds": age_value, "check_result": "PASS"})
            metadata_ages[name] = checkpoint
            return age_value

        def update_redirect_observations(*, response_opened: bool = False) -> None:
            if redirect_handler is None:
                return
            response_boundary["redirect_count"] = redirect_handler.redirect_count
            response_boundary["redirect_target"] = redirect_handler.redirect_target
            response_boundary["redirect_attempts"] = list(redirect_handler.redirect_attempts)
            if redirect_handler.redirect_attempts:
                if (redirect_handler.redirect_count == 1
                        and redirect_handler.redirect_target == expected_final_url):
                    response_boundary["redirect_chain_classification"] = (
                        "ONE_EXACT_METADATA_BOUND_HOP")
                else:
                    response_boundary["redirect_chain_classification"] = "REJECTED_HOP"
            elif response_opened:
                response_boundary["redirect_chain_classification"] = "DIRECT"
            else:
                response_boundary["redirect_chain_classification"] = "NO_REDIRECT_OBSERVED"

        def observed_file(name: str) -> bool | str:
            try:
                os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            except OSError:
                return unknown
            return True

        try:
            marker, _marker_raw, marker_sha, _marker_identity = _read_signed_record_snapshot(
                VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
            marker_binding = marker.get("p1_freshness_renewal")
            if (marker_sha != base_authority["marker_sha256"]
                    or not isinstance(marker_binding, Mapping)
                    or marker_binding.get("use_state") != "READY"
                    or marker_binding.get("sha256") != ready["renewal_sha256"]):
                raise RuntimeError("P1 freshness renewal is no longer the exact READY marker ticket")
            consumed_binding = dict(marker_binding)
            consumed_binding.update({
                "use_state": "CONSUMED",
                "attempt_id": attempt_id,
                "claimed_at_unix": time.time(),
                "body_start_evidence": None,
            })
            update_active_session_marker(
                str(session_id), p1_freshness_renewal=consumed_binding,
                expected_marker_sha256=marker_sha)
            evidence_ledger.update({
                "renewal_path": ready.get("renewal_path_text", unknown),
                "renewal_sha256": ready.get("renewal_sha256", unknown),
                "renewal_sequence": ready["renewal"].get("sequence", unknown),
                "renewal_predecessor_path": ready["renewal"].get(
                    "predecessor_path", unknown),
                "renewal_predecessor_sha256": ready["renewal"].get(
                    "predecessor_sha256", unknown),
                "renewal_use_state": consumed_binding.get("use_state", unknown),
                "renewal_claimed_at_unix": consumed_binding.get("claimed_at_unix", unknown),
                "attempt_id": attempt_id,
                "p1_admission_path": base_authority.get("p1_path_text", unknown),
                "p1_admission_sha256": base_authority.get("p1_sha256", unknown),
                "p1_review_path": base_authority.get("p1_review_path_text", unknown),
                "p1_review_sha256": base_authority.get("p1_review_sha256", unknown),
                "session_id": session_id if isinstance(session_id, str) else unknown,
                "volume_uuid": volume_uuid if isinstance(volume_uuid, str) else unknown,
                "restore_subject_identity": ready["renewal"].get(
                    "restore_subject_identity", unknown),
                "fresh_metadata_path": ready.get("renewal_fresh_metadata_path_text", unknown),
                "fresh_metadata_receipt_sha256": ready.get(
                    "renewal_fresh_metadata_receipt_sha256", unknown),
                "fresh_metadata_observation_sha256": ready.get(
                    "renewal_fresh_metadata", {}).get("metadata_sha256", unknown),
                "current_controller_identity": ready["renewal"].get(
                    "current_controller_identity", unknown),
                "current_g0c_identity": ready["renewal"].get(
                    "current_g0c_identity", unknown),
                "renewal_subject": ready.get("renewal_subject", unknown),
            })
            boundary_authority = _verify_p1_download_authority(
                p1_admission_path, p1_review_path, require_freshness_renewal=False)
            consumed = _verify_p1_freshness_renewal(
                boundary_authority, allow_consumed_attempt_id=attempt_id)
            if (boundary_authority["p1_sha256"] != authority["p1_sha256"]
                    or consumed["renewal_sha256"] != ready["renewal_sha256"]
                    or consumed["renewal_fresh_metadata"]["metadata_sha256"]
                    != fresh["metadata_sha256"]):
                raise RuntimeError("P1 or freshness renewal changed at the final pre-GET boundary")
            fresh = consumed["renewal_fresh_metadata"]
            renewal = consumed["renewal"]
            metadata_ages["verifier_entry"] = {
                "observed_at_unix": consumed.get(
                    "renewal_fresh_metadata_verifier_entry_at_unix", unknown),
                "age_seconds": consumed.get(
                    "renewal_fresh_metadata_verifier_entry_age_seconds", unknown),
                "check_result": ("PASS" if isinstance(consumed.get(
                    "renewal_fresh_metadata_verifier_entry_age_seconds"), (int, float))
                    else unknown),
                "blockers": [],
            }
            renewal_claim = consumed.get("renewal_binding")
            fresh_subject = consumed.get("renewal_subject")
            evidence_ledger.update({
                "renewal_path": consumed.get("renewal_path_text", unknown),
                "renewal_sha256": consumed.get("renewal_sha256", unknown),
                "renewal_sequence": renewal.get("sequence", unknown),
                "renewal_predecessor_path": renewal.get("predecessor_path", unknown),
                "renewal_predecessor_sha256": renewal.get("predecessor_sha256", unknown),
                "renewal_use_state": (renewal_claim.get("use_state", unknown)
                                       if isinstance(renewal_claim, Mapping) else unknown),
                "renewal_claimed_at_unix": (renewal_claim.get("claimed_at_unix", unknown)
                                             if isinstance(renewal_claim, Mapping) else unknown),
                "p1_admission_path": boundary_authority.get("p1_path_text", unknown),
                "p1_admission_sha256": boundary_authority.get("p1_sha256", unknown),
                "p1_review_path": boundary_authority.get("p1_review_path_text", unknown),
                "p1_review_sha256": boundary_authority.get("p1_review_sha256", unknown),
                "session_id": session_id if isinstance(session_id, str) else unknown,
                "volume_uuid": volume_uuid if isinstance(volume_uuid, str) else unknown,
                "attempt_id": attempt_id,
                "restore_subject_identity": renewal.get("restore_subject_identity", unknown),
                "fresh_metadata_path": consumed.get("renewal_fresh_metadata_path_text", unknown),
                "fresh_metadata_receipt_sha256": consumed.get(
                    "renewal_fresh_metadata_receipt_sha256", unknown),
                "fresh_metadata_observation_sha256": fresh.get("metadata_sha256", unknown),
                "current_controller_identity": renewal.get("current_controller_identity", unknown),
                "current_g0c_identity": renewal.get("current_g0c_identity", unknown),
                "renewal_subject": fresh_subject if isinstance(fresh_subject, Mapping) else unknown,
            })

            def check_freshness() -> float:
                return _check_p1_renewal_freshness_now(
                    renewal, fresh, monotonic_at_seal=_renewal_monotonic_at_seal)

            def check_before_redirect() -> None:
                observed_at = time.time()
                try:
                    age_value = check_freshness()
                except RuntimeError as error:
                    metadata_ages["before_redirect"].append({
                        "observed_at_unix": observed_at,
                        "age_seconds": unknown,
                        "check_result": "FAIL",
                        "blockers": [str(error)],
                    })
                    evidence_ledger["failure_stage"] = "REDIRECT_FRESHNESS_CHECK"
                    raise
                metadata_ages["before_redirect"].append({
                    "observed_at_unix": observed_at,
                    "age_seconds": age_value,
                    "check_result": "PASS",
                    "blockers": [],
                })

            request = Request(source_url, headers={"User-Agent": "FG-Metal-VM-Provisioner/1"})
            redirect_handler = _AppleOnlyRedirectHandler(
                expected_length, metadata_digest, source_url, expected_final_url,
                before_redirect=check_before_redirect)
            opener = build_opener(redirect_handler)
            evidence_ledger["failure_stage"] = "HTTP_GET"
            check_freshness()
            with opener.open(request, timeout=60.0) as response:
                evidence_ledger["stage"] = "RESPONSE_HEADERS_RECEIVED"
                evidence_ledger["failure_stage"] = "RESPONSE_HEADER_CAPTURE"
                update_redirect_observations(response_opened=True)
                response_boundary["http_status"] = response.status
                raw_final_url = response.geturl()
                response_boundary["raw_final_url"] = raw_final_url
                content_length_values = response.headers.get_all("Content-Length", [])
                response_boundary["raw_content_length_values"] = (
                    list(content_length_values) if isinstance(content_length_values, (list, tuple))
                    else content_length_values)
                response_boundary["etag"] = response.headers.get("ETag")
                response_boundary["last_modified"] = response.headers.get("Last-Modified")
                response_boundary["expected_metadata_final_url"] = expected_final_url
                response_boundary["redirect_attempts"] = list(redirect_handler.redirect_attempts)
                response_boundary["redirect_count"] = redirect_handler.redirect_count
                response_boundary["redirect_target"] = redirect_handler.redirect_target
                evidence_ledger["failure_stage"] = "FRESHNESS_AFTER_HEADERS"
                checkpoint_age("after_headers")
                evidence_ledger["failure_stage"] = "RESPONSE_HEADERS_VALIDATION"
                try:
                    canonical_final_url = redirect_handler.validate_final_url(raw_final_url)
                except RuntimeError:
                    response_boundary["final_url_bound_to_metadata"] = False
                    raise
                response_boundary["canonical_final_url"] = canonical_final_url
                response_boundary["final_url_bound_to_metadata"] = True
                if (len(content_length_values) == 1
                        and isinstance(content_length_values[0], str)
                        and re.fullmatch(r"[0-9]+", content_length_values[0])):
                    parsed_content_length = int(content_length_values[0])
                    response_boundary["content_length_bytes"] = parsed_content_length
                    response_boundary["content_length_parse_status"] = "VALID_SINGLE_VALUE"
                elif isinstance(content_length_values, (list, tuple)) and len(content_length_values) > 1:
                    response_boundary["content_length_parse_status"] = "AMBIGUOUS_MULTIPLE_VALUES"
                else:
                    response_boundary["content_length_parse_status"] = "INVALID_OR_MISSING"
                evidence_ledger["failure_stage"] = "RESPONSE_HEADERS_VALIDATION"
                if response.status != 200:
                    raise RuntimeError(f"Apple restore server returned HTTP {response.status}")
                final_meta = validate_restore_metadata(
                    source_url, expected_length, metadata_digest, canonical_final_url)
                if (len(content_length_values) != 1
                        or not re.fullmatch(r"[0-9]+", content_length_values[0])
                        or int(content_length_values[0]) != expected_length):
                    raise RuntimeError("Apple restore response Content-Length differs from the renewed exact length")
                response_boundary["response_validated"] = True
                evidence_ledger["stage"] = "RESPONSE_HEADERS_VALIDATED"
                evidence_ledger["failure_stage"] = unknown
                response_info = {
                    **response_boundary,
                    "http_status": response.status,
                    "final_url": raw_final_url,
                    "canonical_final_url": canonical_final_url,
                    "expected_metadata_final_url": expected_final_url,
                    "redirect_target": redirect_handler.redirect_target,
                    "redirect_count": redirect_handler.redirect_count,
                    "final_url_bound_to_metadata": True,
                    "content_length_bytes": int(content_length_values[0]),
                    "etag": response_boundary["etag"],
                    "last_modified": response_boundary["last_modified"],
                    "final_url_metadata": final_meta,
                }
                while True:
                    if not body_started:
                        evidence_ledger["failure_stage"] = "FRESHNESS_BEFORE_READ"
                        checkpoint_age("immediately_before_first_read")
                    evidence_ledger["failure_stage"] = "STREAM_READ"
                    try:
                        block = response.read(VM_RESTORE_DOWNLOAD_CHUNK_BYTES)
                    except BaseException:
                        raise
                    if not block:
                        break
                    if not body_started:
                        first_body_read_at = time.time()
                        first_body_read_bytes = len(block)
                        evidence_ledger["stage"] = "FIRST_BODY_RETURNED_UNACCEPTED"
                        evidence_ledger["failure_stage"] = "POST_READ_VALIDATION"
                        evidence_ledger["first_body_read_at_unix"] = first_body_read_at
                        evidence_ledger["first_body_chunk_length_bytes"] = first_body_read_bytes
                        response_boundary["first_body_read_at_unix"] = first_body_read_at
                        response_boundary["first_body_chunk_length_bytes"] = first_body_read_bytes
                        response_boundary["first_body_bytes_accepted"] = False
                        age_at_first_body_read: float | None = None
                        try:
                            age_at_first_body_read = checkpoint_age(
                                "immediately_after_first_nonempty_read", now=first_body_read_at)
                            post_read_freshness = {
                                "status": "PASS",
                                "age_seconds": age_at_first_body_read,
                                "observed_at_unix": first_body_read_at,
                                "blockers": [],
                            }
                        except RuntimeError as error:
                            post_read_freshness_blockers.append(str(error))
                            post_read_freshness = {
                                "status": "FAIL",
                                "age_seconds": unknown,
                                "observed_at_unix": first_body_read_at,
                                "blockers": list(post_read_freshness_blockers),
                            }
                        try:
                            post_read_storage = _renewal_live_boundary(boundary_authority)
                        except BaseException as error:
                            post_read_storage = {
                                "status": "FAIL",
                                "blockers": [f"post-read live-storage check failed: {error}"],
                            }
                        post_read_storage_blockers = list(post_read_storage.get("blockers", []))
                        if post_read_storage.get("status") != "PASS" and not post_read_storage_blockers:
                            post_read_storage_blockers.append(
                                "post-read live-storage result was not PASS")
                            post_read_storage["blockers"] = list(post_read_storage_blockers)
                        start_blockers = [*post_read_freshness_blockers,
                                          *post_read_storage_blockers]
                        if len(block) > expected_length:
                            start_blockers.append("restore stream exceeded its renewed exact byte length")
                        try:
                            start_evidence = _record_p1_freshness_start(
                                boundary_authority,
                                renewal=consumed.get("renewal") if consumed else None,
                                renewal_path=consumed.get("renewal_path") if consumed else None,
                                renewal_path_text=consumed.get("renewal_path_text") if consumed else None,
                                renewal_sha256=consumed.get("renewal_sha256") if consumed else None,
                                renewal_binding=consumed.get("renewal_binding") if consumed else None,
                                fresh_metadata=consumed.get("renewal_fresh_metadata") if consumed else None,
                                fresh_metadata_path=consumed.get("renewal_fresh_metadata_path") if consumed else None,
                                fresh_metadata_path_text=(consumed.get("renewal_fresh_metadata_path_text")
                                                          if consumed else None),
                                fresh_metadata_receipt_sha256=(
                                    consumed.get("renewal_fresh_metadata_receipt_sha256")
                                    if consumed else None),
                                renewal_subject=consumed.get("renewal_subject") if consumed else None,
                                attempt_id=attempt_id,
                                first_body_read_at_unix=first_body_read_at,
                                first_body_read_bytes=first_body_read_bytes,
                                body_bytes_accepted=not bool(start_blockers),
                                age_seconds=age_at_first_body_read,
                                source_url=source_url, final_url=canonical_final_url,
                                expected_length=expected_length,
                                live_storage=post_read_storage,
                                response_boundary=response_boundary,
                                metadata_ages=metadata_ages,
                                post_read_freshness=post_read_freshness,
                                freshness_blockers=post_read_freshness_blockers,
                                storage_blockers=post_read_storage_blockers,
                                blockers=start_blockers,
                                evidence_ledger=evidence_ledger)
                        except BaseException:
                            evidence_ledger["failure_stage"] = "START_EVIDENCE_FAILED"
                            raise
                        if start_blockers:
                            evidence_ledger["failure_stage"] = "POST_READ_VALIDATION"
                            raise RuntimeError("P1 first body read failed freshness/storage acceptance: "
                                               + "; ".join(start_blockers))
                        body_started = True
                        evidence_ledger["stage"] = "FIRST_BODY_ACCEPTED"
                        evidence_ledger["failure_stage"] = "OUTPUT_OPEN"
                        evidence_ledger["first_body_bytes_accepted"] = True
                        response_boundary["first_body_bytes_accepted"] = True
                        body_started = True
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                        output_fd = os.open(temporary_path.name, flags, 0o600, dir_fd=root_fd)
                        output_info = os.fstat(output_fd)
                        if (not stat.S_ISREG(output_info.st_mode)
                                or output_info.st_dev != root_info.st_dev
                                or output_info.st_nlink != 1):
                            raise RuntimeError("P1 temporary file is not a new regular file on the verified VM volume")
                    elif bytes_written + len(block) > expected_length:
                        evidence_ledger["failure_stage"] = "STREAM_LENGTH_VALIDATION"
                        raise RuntimeError("restore stream exceeded its renewed exact byte length")
                    evidence_ledger["stage"] = "STREAMING"
                    evidence_ledger["failure_stage"] = "RUNTIME_STORAGE_SAMPLE"
                    current = capture_host_observation()
                    sample = _check_runtime_phase_sample(
                        current, volume_uuid=volume_uuid,
                        initial_quota_used_bytes=quota_used_before,
                        maximum_phase_growth_bytes=expected_length + VM_P1_METADATA_MAX_BYTES,
                        concurrent_growth_bytes=concurrent_growth_bound)
                    if sample["blockers"]:
                        raise RuntimeError("P1 shared free-space/quota monitor stopped the download: "
                                           + "; ".join(sample["blockers"]))
                    view = memoryview(block)
                    offset = 0
                    evidence_ledger["failure_stage"] = "STREAM_WRITE"
                    while offset < len(view):
                        count = os.write(output_fd, view[offset:])
                        if count <= 0:
                            raise OSError("short restore payload write")
                        hasher.update(view[offset:offset + count])
                        offset += count
                        bytes_written += count
                    os.fsync(output_fd)
                    evidence_ledger["failure_stage"] = unknown
            evidence_ledger["stage"] = "FINAL_VALIDATION"
            evidence_ledger["failure_stage"] = "FINAL_VALIDATION"
            if bytes_written != expected_length:
                raise RuntimeError(f"restore byte length mismatch: {bytes_written} != {expected_length}")
            evidence_ledger["failure_stage"] = "FILE_FINALIZATION"
            os.fsync(output_fd)
            final_info = os.fstat(output_fd)
            if (not stat.S_ISREG(final_info.st_mode) or final_info.st_dev != root_info.st_dev
                    or final_info.st_nlink != 1 or final_info.st_size != expected_length):
                raise RuntimeError("restore temporary file identity or size changed during download")
            digest = hasher.hexdigest()
            os.close(output_fd)
            output_fd = -1
            evidence_ledger["failure_stage"] = "RENAME"
            os.rename(temporary_path.name, restore_path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
            evidence_ledger["failure_stage"] = "OUTPUT_READBACK"
            verified_fd = os.open(restore_path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                                  dir_fd=root_fd)
            try:
                verified_info = os.fstat(verified_fd)
                verify_hash = hashlib.sha256()
                verify_length = 0
                while True:
                    chunk = os.read(verified_fd, VM_RESTORE_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    verify_hash.update(chunk)
                    verify_length += len(chunk)
                if (verify_length != expected_length or verify_hash.hexdigest() != digest
                        or output_info is None
                        or (verified_info.st_dev, verified_info.st_ino)
                        != (output_info.st_dev, output_info.st_ino)):
                    raise RuntimeError("post-write restore local read-back hash or identity mismatch")
                allocated = getattr(verified_info, "st_blocks", 0) * 512
                file_identity = {
                    "device": verified_info.st_dev,
                    "inode": verified_info.st_ino,
                    "logical_bytes": verified_info.st_size,
                    "allocated_bytes": allocated,
                    "mtime_ns": verified_info.st_mtime_ns,
                    "ctime_ns": getattr(verified_info, "st_ctime_ns", 0),
                }
            finally:
                os.close(verified_fd)
            evidence_ledger["failure_stage"] = unknown
        except BaseException as error:
            update_redirect_observations(response_opened=response_boundary.get(
                "http_status") != unknown)
            if output_fd >= 0:
                os.close(output_fd)
                output_fd = -1
            try:
                os.fsync(root_fd)
            except OSError:
                pass
            temporary_present = observed_file(temporary_path.name)
            restore_present = observed_file(restore_path.name)
            evidence_ledger["output_file_presence"] = {
                "temporary": temporary_present,
                "restore": restore_present,
            }
            os.close(root_fd)
            terminal_marker_update: dict[str, Any] = {
                "status": "NOT_ATTEMPTED", "failure": unknown,
            }
            if body_started or bytes_written > 0:
                blockers = ["P1 restore body started; partial file retained under sticky VM reservation"]
                try:
                    update_active_session_marker(
                        str(session_id), state="FAILED", phase_id="P1_DOWNLOAD_FAILED")
                    terminal_marker_update["status"] = "PASS"
                except BaseException as marker_error:
                    terminal_marker_update.update({
                        "status": "FAIL",
                        "failure": str(marker_error),
                    })
                    blockers.append(f"terminal P1 marker failure update failed: {marker_error}")
                partial_path: str | None = (
                    str(temporary_path) if temporary_present is True else None)
            else:
                blockers = ["P1 attempt stopped before body acceptance; renewal was consumed"]
                partial_path = None
            if evidence_ledger.get("failure_stage") == unknown:
                evidence_ledger["failure_stage"] = "UNKNOWN_FAILURE"
            start_evidence_ref = None
            if evidence_ledger.get("start_evidence_marker_bound") is True:
                start_evidence_ref = {
                    "path": evidence_ledger.get("start_evidence_path", unknown),
                    "sha256": evidence_ledger.get("start_evidence_sha256", unknown),
                }
            response_boundary["first_body_bytes_accepted"] = bool(body_started)
            failed_record = {
                "schema_version": 2,
                "mode": VM_MODE,
                "status": "FAIL",
                "session_id": session_id,
                "phase_id": "P1",
                "volume_uuid": volume_uuid,
                "restore_expected_bytes": expected_length,
                "restore_bytes_written": bytes_written,
                "restore_sha256_partial": hasher.hexdigest(),
                "failure": str(error),
                "partial_path": partial_path,
                "p1_admission_sha256": authority["p1_sha256"],
                "p1_freshness_renewal_sha256": evidence_ledger.get(
                    "renewal_sha256", authority.get("renewal_sha256", unknown)),
                "p1_freshness_start_evidence": start_evidence_ref,
                "failure_evidence": {
                    "schema_version": 1,
                    "stage": evidence_ledger.get("stage", unknown),
                    "failure_stage": evidence_ledger.get("failure_stage", unknown),
                    "response_boundary": dict(response_boundary),
                    "metadata_ages": dict(metadata_ages),
                    "post_read_freshness": dict(post_read_freshness),
                    "post_read_freshness_blockers": list(post_read_freshness_blockers),
                    "post_read_storage_result": dict(post_read_storage),
                    "post_read_storage_blockers": list(post_read_storage_blockers),
                    "renewal": {
                        "path": evidence_ledger.get("renewal_path", unknown),
                        "sha256": evidence_ledger.get("renewal_sha256", unknown),
                        "sequence": evidence_ledger.get("renewal_sequence", unknown),
                        "predecessor_path": evidence_ledger.get(
                            "renewal_predecessor_path", unknown),
                        "predecessor_sha256": evidence_ledger.get(
                            "renewal_predecessor_sha256", unknown),
                        "use_state": evidence_ledger.get("renewal_use_state", unknown),
                        "claimed_at_unix": evidence_ledger.get(
                            "renewal_claimed_at_unix", unknown),
                        "attempt_id": evidence_ledger.get("attempt_id", attempt_id),
                    },
                    "p1": {
                        "admission_path": evidence_ledger.get("p1_admission_path", unknown),
                        "admission_sha256": evidence_ledger.get("p1_admission_sha256", unknown),
                        "review_path": evidence_ledger.get("p1_review_path", unknown),
                        "review_sha256": evidence_ledger.get("p1_review_sha256", unknown),
                    },
                    "session_id": evidence_ledger.get("session_id", unknown),
                    "volume_uuid": evidence_ledger.get("volume_uuid", unknown),
                    "restore_subject_identity": evidence_ledger.get(
                        "restore_subject_identity", unknown),
                    "fresh_metadata": {
                        "path": evidence_ledger.get("fresh_metadata_path", unknown),
                        "receipt_sha256": evidence_ledger.get(
                            "fresh_metadata_receipt_sha256", unknown),
                        "observation_sha256": evidence_ledger.get(
                            "fresh_metadata_observation_sha256", unknown),
                    },
                    "current_controller_identity": evidence_ledger.get(
                        "current_controller_identity", unknown),
                    "current_g0c_identity": evidence_ledger.get("current_g0c_identity", unknown),
                    "first_body_read_at_unix": evidence_ledger.get(
                        "first_body_read_at_unix", unknown),
                    "first_body_chunk_length_bytes": evidence_ledger.get(
                        "first_body_chunk_length_bytes", unknown),
                    "first_body_bytes_accepted": evidence_ledger.get(
                        "first_body_bytes_accepted", False),
                    "start_evidence_created": evidence_ledger.get(
                        "start_evidence_created", False),
                    "start_evidence_marker_bound": evidence_ledger.get(
                        "start_evidence_marker_bound", False),
                    "start_evidence_creation_state": evidence_ledger.get(
                        "start_evidence_creation_state", "NOT_ATTEMPTED"),
                    "start_evidence": {
                        "path": evidence_ledger.get("start_evidence_path", unknown),
                        "sha256": evidence_ledger.get("start_evidence_sha256", unknown),
                    },
                    "terminal_marker_update": terminal_marker_update,
                    "output_file_presence": evidence_ledger.get(
                        "output_file_presence", {"temporary": unknown, "restore": unknown}),
                },
                "blockers": blockers,
            }
            return _write_signed_json(receipt_path, failed_record,
                                      "restore_verification_hmac_sha256")
        os.close(root_fd)
        after = capture_host_observation()
        _require_fresh_timestamp(after.get("captured_at_unix"),
                                 label="P1 post-download live APFS observation",
                                 maximum_age_seconds=VM_MAX_OBSERVATION_AGE_SECONDS)
        after_blockers = _session_container_blockers(after, volume_uuid=volume_uuid)
        final_phase_sample = _check_runtime_phase_sample(
            after, volume_uuid=volume_uuid,
            initial_quota_used_bytes=quota_used_before,
            maximum_phase_growth_bytes=expected_length + VM_P1_METADATA_MAX_BYTES,
            concurrent_growth_bytes=concurrent_growth_bound)
        after_blockers.extend(final_phase_sample["blockers"])
        current_volume = _volume_row(after, volume_uuid)
        current_used = current_volume.get("CapacityInUse")
        if not _integer(current_used):
            after_blockers.append("post-download VM quota accounting is unknown")
        if after_blockers:
            update_active_session_marker(str(session_id), state="FAILED", phase_id="P1_READBACK_FAILED")
        if start_evidence is None:
            raise RuntimeError("P1 restore body completed without durable first-body evidence")
        record = {
            "schema_version": 1,
            "mode": VM_MODE,
            "status": "PASS" if not after_blockers else "FAIL",
            "session_id": session_id,
            "phase_id": "P1",
            "volume_uuid": volume_uuid,
            "container_uuid": EXPECTED_CONTAINER_UUID,
            "restore_path": str(restore_path),
            "restore_metadata": restore_metadata,
            "expected_length_bytes": expected_length,
            "actual_length_bytes": bytes_written,
            "local_sha256_state": "VERIFIED",
            "local_sha256": hasher.hexdigest(),
            "file_identity": file_identity,
            "response_metadata": response_info,
            "pre_download_free_bytes": free_before,
            "post_download_free_bytes": conservative_session_unreserved_free_bytes(
                after, volume_uuid=volume_uuid),
            "post_download_vm_quota_used_bytes": current_used,
            "post_download_phase_safety_sample": final_phase_sample,
            "p1_admission_path": authority["p1_path_text"],
            "p1_admission_sha256": authority["p1_sha256"],
            "p1_independent_review_path": authority["p1_review_path_text"],
            "p1_independent_review_sha256": authority["p1_review_sha256"],
            "p0_admission_sha256": admission["p0_ancestry"]["p0_admission_sha256"],
            "p0_review_sha256": admission["p0_ancestry"]["p0_review_sha256"],
            "volume_creation_receipt_path": authority["creation_path_text"],
            "volume_creation_receipt_sha256": admission["volume_creation_receipt_sha256"],
            "metadata_ancestor_sha256": admission["metadata_ancestor_sha256"],
            "fresh_metadata_path": authority["renewal_fresh_metadata_path_text"],
            "fresh_metadata_receipt_sha256": authority["renewal_fresh_metadata_receipt_sha256"],
            "fresh_metadata_observation_sha256": fresh["metadata_sha256"],
            "fresh_metadata_age_at_download_authorization_seconds": (
                authority["renewal_fresh_metadata_age_seconds"]),
            "p1_freshness_renewal_path": authority["renewal_path_text"],
            "p1_freshness_renewal_sha256": authority["renewal_sha256"],
            "p1_freshness_renewal_sequence": authority["renewal"]["sequence"],
            "p1_freshness_start_evidence_path": start_evidence["path_text"],
            "p1_freshness_start_evidence_sha256": start_evidence["sha256"],
            "restore_lineage_sha256": authority["lineage_sha256"],
            "compatibility_basis_sha256": authority["basis_sha256"],
            "compatibility_basis_review_sha256": authority["basis_review_sha256"],
            "p0_transition_sha256": authority["transition_sha256"],
            "p0_transition_review_sha256": authority["transition_review_sha256"],
            "restore_subject_identity": admission["restore_subject_identity"],
            "payload_upstream_content_digest_validated": False,
            "g0c_identity": g0c_identity,
            "blockers": after_blockers,
        }
        sealed = _write_signed_json(receipt_path, record, "restore_verification_hmac_sha256")
        if not after_blockers:
            update_active_session_marker(str(session_id), phase_id="P1_VERIFIED",
                                        committed_growth_bytes=0)
        return sealed


def renew_p1_and_download(*, p1_admission_path: Path | None = None,
                          p1_review_path: Path | None = None,
                          metadata_receipt_path: Path | None = None,
                          renewal_receipt_path: Path | None = None,
                          download_receipt_path: Path = VM_RESTORE_VERIFICATION_PATH
                          ) -> dict[str, Any]:
    """Perform one fresh metadata observation, renew same-subject P1, then immediately download."""
    _verify_p1_download_authority(
        p1_admission_path, p1_review_path, require_freshness_renewal=False)
    metadata = fetch_latest_restore_metadata(receipt_path=metadata_receipt_path)
    metadata_path_text = metadata.get("receipt_path")
    if not isinstance(metadata_path_text, str):
        raise RuntimeError("fresh metadata observation did not return its exact receipt path")
    renewal = create_p1_freshness_renewal(
        Path(metadata_path_text), receipt_path=renewal_receipt_path)
    return download_and_verify_restore(
        p1_admission_path, p1_review_path=p1_review_path,
        receipt_path=download_receipt_path,
        _renewal_monotonic_at_seal=renewal["monotonic_at_seal"])


def validate_restore_cleanup(*, restore_record: Mapping[str, Any],
                             sealed_base_record: Mapping[str, Any],
                             cleanup_action: Mapping[str, Any]) -> list[str]:
    """Require local hash and sealed identity provenance before P4 restore deletion."""
    blockers: list[str] = []
    if restore_record.get("phase_id") != "P1" or restore_record.get("local_sha256_state") != "VERIFIED":
        blockers.append("restore cleanup requires a locally hashed P1 restore record")
    if not isinstance(restore_record.get("local_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", restore_record.get("local_sha256", "")):
        blockers.append("restore local SHA-256 provenance is missing")
    if not verify_signed_record(sealed_base_record, "sealed_base_hmac_sha256"):
        blockers.append("restore cleanup lacks an authenticated sealed-base record")
    required_base = ("hardware_model_sha256", "auxiliary_storage_sha256",
                     "system_image_sha256", "bootstrap_identity_sha256")
    if any(not isinstance(sealed_base_record.get(field), str)
           or not re.fullmatch(r"[0-9a-f]{64}", sealed_base_record.get(field, ""))
           for field in required_base):
        blockers.append("sealed-base identity/provenance is incomplete")
    if (cleanup_action.get("restore_sha256") != restore_record.get("local_sha256")
            or cleanup_action.get("sealed_base_sha256") != _sha256(
                policy._canonical_pretty_json_bytes(sealed_base_record))):
        blockers.append("signed P4 cleanup action does not bind restore and sealed-base provenance")
    if not verify_signed_record(cleanup_action, "restore_cleanup_hmac_sha256"):
        blockers.append("P4 restore cleanup action is not authenticated")
    return blockers


def sparse_image_accounting(*, logical_capacity_bytes: int | None,
                            maximum_logical_capacity_bytes: int | None,
                            physical_allocated_bytes: int | None,
                            maximum_physical_allocation_bytes: int | None) -> dict[str, Any]:
    blockers: list[str] = []
    for name, value in (
            ("logical_capacity_bytes", logical_capacity_bytes),
            ("maximum_logical_capacity_bytes", maximum_logical_capacity_bytes),
            ("physical_allocated_bytes", physical_allocated_bytes),
            ("maximum_physical_allocation_bytes", maximum_physical_allocation_bytes)):
        if not _integer(value):
            blockers.append(f"sparse-image accounting is unknown or invalid: {name}")
    if not blockers:
        assert logical_capacity_bytes is not None
        assert maximum_logical_capacity_bytes is not None
        assert physical_allocated_bytes is not None
        assert maximum_physical_allocation_bytes is not None
        if logical_capacity_bytes > maximum_logical_capacity_bytes:
            blockers.append("sparse RAW logical capacity exceeds its per-image limit")
        if physical_allocated_bytes > maximum_physical_allocation_bytes:
            blockers.append("sparse RAW allocated bytes exceed the signed per-image physical cap")
    return {
        "logical_capacity_bytes": logical_capacity_bytes,
        "maximum_logical_capacity_bytes": maximum_logical_capacity_bytes,
        "physical_allocated_bytes": physical_allocated_bytes,
        "maximum_physical_allocation_bytes": maximum_physical_allocation_bytes,
        "admitted": not blockers,
        "blockers": blockers,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("observe", help="capture a read-only shared APFS observation")
    fetch_metadata = commands.add_parser(
        "fetch-restore-metadata", help="query latest supported VZ restore metadata and Apple HEAD headers")
    fetch_metadata.add_argument("--receipt", type=Path,
                                help="new unique repository-relative metadata receipt path")
    lineage = commands.add_parser(
        "create-metadata-lineage", help="bind one fresh metadata leaf directly to immutable P0")
    lineage.add_argument("--fresh-metadata", type=Path, required=True)
    lineage.add_argument("--receipt", type=Path)
    basis = commands.add_parser(
        "create-compatibility-basis", help="adopt the reviewed current VZ hardware tuple for P1")
    basis.add_argument("--lineage", type=Path, required=True)
    basis.add_argument("--receipt", type=Path)
    transition = commands.add_parser(
        "revalidate-p0-for-p1", help="write a read-only, no-authority P0 transition receipt")
    transition.add_argument("--lineage", type=Path, required=True)
    transition.add_argument("--basis", type=Path, required=True)
    transition.add_argument("--basis-review", type=Path, required=True)
    transition.add_argument("--receipt", type=Path)
    precreate = commands.add_parser("admit-precreation", help="write a signed bounded pre-creation admission")
    precreate.add_argument("--restore-url")
    precreate.add_argument("--restore-length", type=int)
    precreate.add_argument("--restore-metadata-sha256")
    precreate.add_argument("--restore-metadata", type=Path, default=VM_RESTORE_METADATA_PATH)
    precreate.add_argument("--scope", choices=("RUN_QUOTA_PROBE_ONLY", "CREATE_VM_VOLUME_ONLY"),
                           default="RUN_QUOTA_PROBE_ONLY")
    commands.add_parser("probe-quota", help="run the admitted bounded APFS quota-enforcement probe")
    commands.add_parser("create-volume", help="create the admitted empty safety reserve and live-quota VM APFS volumes")
    commands.add_parser("recover-p0", help="recover a failed pre-VM safety-reserve creation attempt")
    p1 = commands.add_parser("admit-p1", help="write a fresh P1-only restore admission")
    p1.add_argument("--restore-url")
    p1.add_argument("--restore-length", type=int)
    p1.add_argument("--restore-metadata-sha256")
    p1.add_argument("--fresh-metadata", type=Path)
    p1.add_argument("--lineage", type=Path, required=True)
    p1.add_argument("--basis", type=Path, required=True)
    p1.add_argument("--basis-review", type=Path, required=True)
    p1.add_argument("--transition", type=Path, required=True)
    p1.add_argument("--transition-review", type=Path, required=True)
    p1.add_argument("--receipt", type=Path)
    download = commands.add_parser("download-restore", help="stream and locally hash the admitted restore")
    download.add_argument("--admission", type=Path)
    download.add_argument("--review", type=Path)
    download.add_argument("--receipt", type=Path, default=VM_RESTORE_VERIFICATION_PATH)
    renew_download = commands.add_parser(
        "renew-P1-and-download",
        help="observe current metadata once, renew the same P1 subject, then immediately download")
    renew_download.add_argument("--admission", type=Path)
    renew_download.add_argument("--review", type=Path)
    renew_download.add_argument("--metadata-receipt", type=Path)
    renew_download.add_argument("--renewal-receipt", type=Path)
    renew_download.add_argument("--download-receipt", type=Path, default=VM_RESTORE_VERIFICATION_PATH)
    p2 = commands.add_parser("admit-p2", help="revalidate P2 installer growth and stop safety")
    p2.add_argument("--stop-profile", type=Path, default=VM_STOP_SAFETY_PROFILE_PATH)
    stop_profile = commands.add_parser("seal-stop-profile", help="seal measured write/stop safety evidence")
    stop_profile.add_argument("--evidence", type=Path, required=True)
    p2_result = commands.add_parser("record-p2-result", help="seal measured P2 installer completion")
    p2_result.add_argument("--installer-result", type=Path, required=True)
    p2_result.add_argument("--monitor-result", type=Path, required=True)
    commands.add_parser("seal-p3", help="seal stopped guest/base identities after P2")
    commands.add_parser("authorize-p4-cleanup", help="create the one-use signed P4 cleanup action")
    commands.add_parser("cleanup-p4", help="execute the signed restore deletion and measure reclaimed bytes")
    deferred_branch = commands.add_parser(
        "retire-deferred-p2-capability",
        help="supersede the exact unused R4 P2 capability for deferred VM containment")
    deferred_branch.add_argument("--admission", type=Path, default=VM_DEFERRED_R4_ADMISSION_PATH)
    deferred_branch.add_argument("--failed-attempt", type=Path, default=VM_DEFERRED_R4_FAILURE_PATH)
    deferred_branch.add_argument("--receipt", type=Path)
    deferred_authorize = commands.add_parser(
        "authorize-deferred-restore-retirement",
        help="sign a separate exact-file restore retirement action for deferred VM work")
    deferred_authorize.add_argument("--branch-retirement", type=Path, required=True)
    deferred_authorize.add_argument("--authorization", type=Path)
    deferred_retire = commands.add_parser(
        "retire-deferred-restore",
        help="retire the exact signed restore after an independent predelete review")
    deferred_retire.add_argument("--authorization", type=Path, required=True)
    deferred_retire.add_argument("--predelete-review", type=Path, required=True)
    deferred_retire.add_argument("--receipt", type=Path)
    deferred_retire.add_argument("--pending", type=Path)
    deferred_recovery_inspect = commands.add_parser(
        "inspect-deferred-restore-recovery",
        help="validate and hash the exact R13 PENDING quarantine after source review")
    deferred_recovery_inspect.add_argument("--path-review", type=Path, required=True)
    deferred_recovery_inspect.add_argument("--validation", type=Path)
    deferred_recovery_authorize = commands.add_parser(
        "authorize-deferred-restore-recovery",
        help="issue a fresh one-use recovery authority after content review")
    deferred_recovery_authorize.add_argument("--validation", type=Path, required=True)
    deferred_recovery_authorize.add_argument("--recovery-review", type=Path, required=True)
    deferred_recovery_authorize.add_argument("--authorization", type=Path)
    deferred_recover = commands.add_parser(
        "recover-deferred-restore-retirement",
        help="revalidate, unlink, and resolve the exact old deferred-restore PENDING action")
    deferred_recover.add_argument("--authorization", type=Path, required=True)
    deferred_recover.add_argument("--recovery-review", type=Path, required=True)
    deferred_recover.add_argument("--receipt", type=Path)
    deferred_resolve = commands.add_parser(
        "resolve-deferred-restore-measurement-failure",
        help="resolve the exact R13 PENDING unlink additively with reclaim left unknown")
    deferred_resolve.add_argument("--resolution-review", type=Path, required=True)
    deferred_resolve.add_argument("--receipt", type=Path)
    deferred_inventory = commands.add_parser(
        "capture-deferred-capacity-inventory",
        help="sign a direct current-state storage and deferred VM capacity inventory")
    deferred_inventory.add_argument("--branch-retirement", type=Path, required=True)
    deferred_inventory.add_argument("--restore-retirement", type=Path, required=True)
    deferred_inventory.add_argument("--storage-inventory", type=Path,
                                    default=policy.INVENTORY_PATH)
    deferred_inventory.add_argument("--receipt", type=Path)
    deferred_inactivate = commands.add_parser(
        "inactivate-deferred-capacity",
        help="release only future VM growth after fresh inventory and future-commitment PASS")
    deferred_inactivate.add_argument("--branch-retirement", type=Path, required=True)
    deferred_inactivate.add_argument("--restore-retirement", type=Path, required=True)
    deferred_inactivate.add_argument("--capacity-inventory", type=Path, required=True)
    deferred_inactivate.add_argument("--commitment-review", type=Path, required=True)
    deferred_inactivate.add_argument(
        "--inactivation", type=Path,
        default=policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION)
    commands.add_parser("admit-p5", help="admit minimum runtime-feasibility assets after P4")
    p5_asset = commands.add_parser("create-p5-raw", help="create one bounded sparse P5 RAW image")
    p5_asset.add_argument("--role", required=True,
                          choices=("guest_system_raw", "read_only_input_tool_raw",
                                   "workspace_raw", "export_capsule_raw"))
    p5_asset.add_argument("--logical-capacity", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            observation = capture_host_observation()
            blockers = _container_blockers(observation, expected_vm_volume_uuid=None)
            output = {
                "status": "PASS_READ_ONLY_OBSERVATION" if not blockers else "FAIL",
                "observation": observation,
                "conservative_shared_container_free_bytes": (
                    conservative_container_free_bytes(observation) if not blockers else None),
                "blockers": blockers,
            }
        elif args.command == "fetch-restore-metadata":
            output = fetch_latest_restore_metadata(receipt_path=args.receipt)
        elif args.command == "create-metadata-lineage":
            output = create_restore_metadata_refresh_lineage(
                args.fresh_metadata, receipt_path=args.receipt)
        elif args.command == "create-compatibility-basis":
            output = create_restore_metadata_compatibility_basis(
                args.lineage, receipt_path=args.receipt)
        elif args.command == "revalidate-p0-for-p1":
            output = create_p0_revalidation_transition(
                args.lineage, args.basis, args.basis_review, receipt_path=args.receipt)
        elif args.command == "admit-precreation":
            metadata = _read_latest_restore_metadata(args.restore_metadata)
            output = create_precreation_admission(
                capture_host_observation(),
                args.restore_length if args.restore_length is not None else metadata["expected_length_bytes"],
                args.restore_url if args.restore_url is not None else metadata["source_url"],
                args.restore_metadata_sha256 if args.restore_metadata_sha256 is not None
                else metadata["metadata_sha256"], authorization_scope=args.scope,
                restore_metadata_path=args.restore_metadata)
        elif args.command == "probe-quota":
            output = run_apfs_quota_enforcement_probe()
        elif args.command == "create-volume":
            output = create_vm_volume()
        elif args.command == "recover-p0":
            output = recover_partial_vm_volume_creation()
        elif args.command == "admit-p1":
            output = create_p1_admission(
                args.restore_length, args.restore_url, args.restore_metadata_sha256,
                lineage_path=args.lineage,
                compatibility_basis_path=args.basis,
                compatibility_basis_review_path=args.basis_review,
                transition_path=args.transition,
                transition_review_path=args.transition_review,
                fresh_metadata_path=args.fresh_metadata,
                receipt_path=args.receipt)
        elif args.command == "download-restore":
            output = download_and_verify_restore(
                args.admission, p1_review_path=args.review, receipt_path=args.receipt)
        elif args.command == "renew-P1-and-download":
            output = renew_p1_and_download(
                p1_admission_path=args.admission, p1_review_path=args.review,
                metadata_receipt_path=args.metadata_receipt,
                renewal_receipt_path=args.renewal_receipt,
                download_receipt_path=args.download_receipt)
        elif args.command == "admit-p2":
            output = create_p2_admission(stop_safety_profile_path=args.stop_profile)
        elif args.command == "seal-stop-profile":
            output = write_stop_safety_profile(
                measurement_evidence_path=args.evidence)
        elif args.command == "record-p2-result":
            installer_result = json.loads(policy._read_file_nofollow(args.installer_result))
            monitor_result = json.loads(policy._read_file_nofollow(args.monitor_result))
            if not isinstance(installer_result, dict) or not isinstance(monitor_result, dict):
                raise ValueError("P2 installer and monitor result files must be JSON objects")
            output = record_p2_install_result(
                installer_result=installer_result, phase_monitor_result=monitor_result)
        elif args.command == "seal-p3":
            output = create_p3_sealed_base()
        elif args.command == "authorize-p4-cleanup":
            output = create_p4_restore_cleanup_action()
        elif args.command == "cleanup-p4":
            output = perform_p4_restore_cleanup()
        elif args.command == "retire-deferred-p2-capability":
            output = retire_deferred_vm_p2_capability(
                admission_path=args.admission,
                failed_attempt_path=args.failed_attempt,
                receipt_path=args.receipt)
        elif args.command == "authorize-deferred-restore-retirement":
            output = authorize_deferred_vm_restore_retirement(
                args.branch_retirement, authorization_path=args.authorization)
        elif args.command == "retire-deferred-restore":
            output = perform_deferred_vm_restore_retirement(
                args.authorization, args.predelete_review,
                retirement_receipt_path=args.receipt,
                pending_receipt_path=args.pending)
        elif args.command == "inspect-deferred-restore-recovery":
            output = inspect_deferred_restore_recovery(
                args.path_review, validation_path=args.validation)
        elif args.command == "authorize-deferred-restore-recovery":
            output = authorize_deferred_restore_recovery(
                args.validation, args.recovery_review,
                authorization_path=args.authorization)
        elif args.command == "recover-deferred-restore-retirement":
            output = perform_deferred_restore_recovery(
                args.authorization, args.recovery_review,
                resolution_receipt_path=args.receipt)
        elif args.command == "resolve-deferred-restore-measurement-failure":
            output = resolve_deferred_restore_measurement_failure(
                args.resolution_review, receipt_path=args.receipt)
        elif args.command == "capture-deferred-capacity-inventory":
            output = create_deferred_capacity_current_inventory(
                args.storage_inventory,
                branch_retirement_path=args.branch_retirement,
                restore_retirement_path=args.restore_retirement,
                receipt_path=args.receipt)
        elif args.command == "inactivate-deferred-capacity":
            output = create_deferred_capacity_inactivation(
                args.branch_retirement, args.restore_retirement,
                args.commitment_review, args.capacity_inventory,
                inactivation_path=args.inactivation)
        elif args.command == "admit-p5":
            output = create_p5_runtime_admission()
        elif args.command == "create-p5-raw":
            output = create_p5_sparse_raw_asset(
                role=args.role, logical_capacity_bytes=args.logical_capacity)
        else:
            raise ValueError(f"unsupported VM capacity command: {args.command}")
        print(json.dumps(output, indent=2, sort_keys=True))
        status = output.get("status") or output.get("decision")
        return 0 if status in {"PASS", "PASS_VALIDATED", "AUTHORIZED", "SUPERSEDED",
                               "PASS_READ_ONLY_OBSERVATION", "PASS_INSTALL_COMPLETE"} else 1
    except BaseException as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
