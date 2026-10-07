#!/usr/bin/env python3
"""Create the explicit, reviewable Step 11D.3 storage cleanup plan."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()
TMP = Path("/private/tmp")
INV = REPO / "experiments/storage/storage-inventory.json"
OUT = REPO / "experiments/storage/storage-cleanup-plan.json"
KEEP_REPRO = HOME / "Library/Caches/FGMetalStep11D-R/step11d2-diag-60s-07-submit-reclaim-split-prefix"

BUILD_CANDIDATES = [
    TMP / "fgmetal-step11d-cache-source-build-1",
    TMP / "fgmetal-step11d-r-cache-build-traced",
    TMP / "fgmetal-step11d-r-cleanbuild-11d-final-a-20261006",
    TMP / "fgmetal-step11d-r-cleanbuild-11d-final-b-20261006",
    TMP / "fgmetal-step11d-r-cleanbuild-diag2-20261006",
    TMP / "fgmetal-step11d-r-current-r3-build-A-20261006",
    TMP / "fgmetal-step11d-r-current-r3-build-B-20261006",
    TMP / "fgmetal-step11d-r-current-r4-build-A-20261006",
    TMP / "fgmetal-step11d-r-current-r5-build-A-20261006",
    TMP / "fgmetal-step11d-r-current-r5-trace-only-build-20261006",
    TMP / "fgmetal-step11d-r-current-r6-build-A-20261006",
    TMP / "fgmetal-step11d-r-exact-build",
    TMP / "fgmetal-step11d-r-repro-a",
    TMP / "fgmetal-step11d-r-repro-b",
    TMP / "fgmetal-step11d2-r5-diagnostic-build-c-20261006",
    HOME / "Library/Caches/FGMetalStep11C1R/build-d1",
    HOME / "Library/Caches/FGMetalStep11C1R/build-d1-repro",
]
MIXED_DISPOSABLES = [
    TMP / "fgmetal-step11b/runtime-candidate/wine",
    TMP / "fgmetal-dxvk-macos-audit-run/runtime-candidate/wine",
    TMP / "fgmetal-step11b/MoltenVK/External/build",
]
REQUIRED_HASHES = {
    "R5_D3D11": "e747ca5a5c2087d1f788762a5c4bae13939593a2cb89dedf2f7c04999ab94de5",
    "R5_DXGI": "58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132",
    "R5_PE_BRIDGE": "44cdaa3ff503a1beb3afd735620c3b33eac4bca17377fa78e5c3be5f852c18cf",
    "R5_NATIVE_PROVIDER": "033e3a8cbd957b1e16131fc690256aba8684d51b17688514a5ff7eaa351c2fe2",
    "MVK_1_4_2_C1R": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
    "MVK_1_4_2_STEP11B": "df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e",
    "STEP11D2_RUNTIME_MVK_VARIANT": "8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf",
    "STEP11B1_PATCH": "4f4973000d0ce0783d05e11b7acae0efdff2d6bfe898bf49305408d72280e758",
    "STEP11C_INTERNAL_PATCH": "4df3af55c9491a44d5e8cec79d458bf7cecc18d71b61312b19e80b461023d352",
    "D1_PATCH": "a8a9d41b562ae79eae9ee82e3aa1da97562c28a6e6f0c08c7419687d2730b46b",
    "D2_PATCH": "3ed09bdbb36dc4ae85dff2ab8a40d5e9d69c1136a8977917a7e6064ac082cc3a",
    "D4_PATCH": "07b460c16b6c7ae670658be2ed24071e4127835543ac4983ae96dcac5170d7b8",
    "D2_MATCHED_STALL_SAMPLE": "8b6f9ffb3a865f0307f257753fc054930df24a2b89935929ff0519ab29c2c6ca",
}
REQUIRED_ARTIFACT_PATHS = {
    "R5_D3D11": REPO / "experiments/dxvk_macos_step11dR/evidence/flipseq-r5-exact-20261006/r5-dlls/d3d11.dll",
    "R5_DXGI": REPO / "experiments/dxvk_macos_step11dR/evidence/flipseq-r5-exact-20261006/r5-dlls/dxgi.dll",
    "R5_PE_BRIDGE": HOME / ".codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof/build/status-v3-current-20261006/overlay/x86_64-windows/FGMetalBridge.dll",
    "R5_NATIVE_PROVIDER": HOME / ".codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof/build/status-v3-current-20261006/overlay/x86_64-unix/fgmetalbridge.so",
    "MVK_1_4_2_C1R": HOME / "Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib",
    "MVK_1_4_2_STEP11B": TMP / "fgmetal-step11b/MoltenVK/Package/Release/MoltenVK/dynamic/dylib/macOS/libMoltenVK.dylib",
    "STEP11D2_RUNTIME_MVK_VARIANT": HOME / "Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine/lib/moltenvk-vkmt/libMoltenVK.dylib",
    "STEP11B1_PATCH": REPO / "experiments/dxvk_macos_step11b1/evidence/final-fix.patch",
    "STEP11C_INTERNAL_PATCH": REPO / "experiments/dxvk_macos_step11c/patches/internal-presentation-experiment.patch",
    "D1_PATCH": REPO / "experiments/dxvk_macos_step11c1r/patches/D1-shutdown-lifetime.patch",
    "D2_PATCH": REPO / "experiments/dxvk_macos_step11c1r2/patches/D2-condition-variable.patch",
    "D4_PATCH": REPO / "experiments/dxvk_macos_step11c1r/patches/D4-resize-extent.patch",
    "D2_MATCHED_STALL_SAMPLE": REPO / "experiments/dxvk_macos_step11dR/evidence/step11d2-diagnostic/diag-60s-07-submit-reclaim-split/os-sample-01.txt",
}


def bytes_tree(path: Path) -> dict[str, int]:
    logical = allocated_paths = files = 0
    seen: set[tuple[int, int]] = set()
    unique_allocated = 0
    if path.is_file() and not path.is_symlink():
        info = path.stat()
        return {"logical_bytes": info.st_size, "allocated_path_bytes": info.st_blocks * 512,
                "allocated_unique_inode_bytes": info.st_blocks * 512, "file_count": 1}
    if not path.is_dir() or path.is_symlink():
        return {"logical_bytes": 0, "allocated_path_bytes": 0,
                "allocated_unique_inode_bytes": 0, "file_count": 0}
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(directory)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            file_path = current / name
            try:
                info = file_path.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            logical += info.st_size
            allocated_paths += info.st_blocks * 512
            files += 1
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                unique_allocated += info.st_blocks * 512
    return {"logical_bytes": logical, "allocated_path_bytes": allocated_paths,
            "allocated_unique_inode_bytes": unique_allocated, "file_count": files}


def add_action(actions: list[dict], path: Path, category: str, decision: str,
               reason: str, prerequisite: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        actions.append({"path": str(path), "category": category, "decision": decision,
                        "reason": reason, "prerequisite": prerequisite,
                        "symlink": True, "estimated": bytes_tree(path)})
        return
    actions.append({"path": str(path), "category": category, "decision": decision,
                    "reason": reason, "prerequisite": prerequisite,
                    "symlink": False, "estimated": bytes_tree(path)})


def discover_prefixes(inventory: dict) -> list[Path]:
    roots = [Path(item["path"]) for item in inventory["roots"]]
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if "system.reg" in filenames and ("dosdevices" in dirnames or "drive_c" in dirnames):
                found.add(Path(dirpath))
            dirnames[:] = [name for name in dirnames if not (Path(dirpath) / name).is_symlink()]
    return sorted(found)


def main() -> int:
    inventory = json.loads(INV.read_text(encoding="utf-8"))
    free = inventory["filesystem"]["free_bytes"]
    prefixes = discover_prefixes(inventory)
    prefix_actions = []
    for path in prefixes:
        if path == KEEP_REPRO:
            prefix_actions.append({"path": str(path), "category": "PRESERVED EVIDENCE / FAILURE REPRODUCTION",
                                  "decision": "PRESERVE_PREFIX", "reason": "Unique 11D.2 matched-stall reproducer; preserve its registry state and exact failure environment.",
                                  "estimated": bytes_tree(path)})
        else:
            prefix_actions.append({"path": str(path), "category": "REGENERATABLE WINE PREFIX",
                                  "decision": "SNAPSHOT_REGISTRY_THEN_DELETE",
                                  "reason": "Completed run or diagnostic environment; retain registry/config snapshot and run evidence, then enforce the two-prefix cap.",
                                  "prerequisite": "Verify no live Wine process; archive system.reg, user.reg, userdef.reg, dosdevices links, and prefix file/hash summary.",
                                  "estimated": bytes_tree(path)})

    actions: list[dict] = []
    for path in BUILD_CANDIDATES:
        add_action(actions, path, "COMPLETED BUILD DIRECTORY", "ARTIFACTIZE_THEN_DELETE",
                   "Completed build output is not permanent evidence. Preserve binary hashes, source/patch identity, Meson options, cross-file hash, compiler versions, and build log first.",
                   "Create per-build record; canonicalize every required output hash; reconcile manifest drift explicitly; verify no live process references the directory.")
    for path in MIXED_DISPOSABLES:
        category = "DISPOSABLE PREFIX" if path.name == "prefix" else "COMPLETED RUNTIME/BUILD SUBTREE"
        add_action(actions, path, category, "SNAPSHOT_AND_ARTIFACTIZE_THEN_DELETE",
                   "Mixed parent root contains preserved reports/traces/source; remove only this exact prefix or runtime/build subtree after recording hashes and provenance.",
                   "No live Wine process; registry snapshot for prefixes; canonical hash records for runtime/binary payloads; preserve parent evidence.")

    # All completed C1R build outputs need identity records before their directories go.
    # Step 11D cache prefixes and all prior Step 8 prefixes are found from registry paths.
    duplicate_bytes = 0
    duplicate_hashes = []
    for group in inventory.get("duplicate_binary_hash_groups", []):
        records = [row for row in inventory["large_objects"]
                   if row.get("sha256") == group["sha256"] and row.get("category") != "REGENERATABLE"]
        inode_bytes: dict[tuple[int, int], int] = {}
        for row in records:
            key = (row["filesystem_device"], row["inode"])
            inode_bytes[key] = max(inode_bytes.get(key, 0), row["allocated_size_bytes"])
        if len(inode_bytes) > 1:
            reclaim = sum(sorted(inode_bytes.values(), reverse=True)[1:])
            duplicate_bytes += reclaim
            duplicate_hashes.append({"sha256": group["sha256"], "physical_inode_count": len(inode_bytes),
                                     "estimated_duplicate_allocated_bytes": reclaim,
                                     "paths": group["paths"]})

    plan = {
        "schema_version": 1,
        "created_at_unix": time.time(),
        "source_inventory": str(INV),
        "filesystem_free_before_bytes": free,
        "filesystem_gate": {"current_state": "EMERGENCY_CLEANUP_ONLY" if free < 15 * 1024**3 else "CLEANUP_ONLY_BELOW_45_GIB",
                            "minimum_experiment_free_bytes": 45 * 1024**3,
                            "preferred_free_bytes": 60 * 1024**3,
                            "current_experiment_decision": "REFUSE"},
        "project_usage_before": inventory["totals"],
        "prefix_policy": {"maximum_preserved": 2,
                          "preserve_existing": str(KEEP_REPRO),
                          "create_after_cleanup": str(HOME / "Library/Caches/FGMetal/prefixes/wine-11.17-mvk142-pristine"),
                          "remove_other_prefixes_after_registry_snapshot": True},
        "required_hashes_to_verify_before_mutation": REQUIRED_HASHES,
        "required_artifact_paths": {key: str(path) for key, path in REQUIRED_ARTIFACT_PATHS.items()},
        "prefix_actions": prefix_actions,
        "exact_subtree_actions": actions,
        "binary_consolidation": {
            "store_root": str(HOME / "Library/Caches/FGMetal/artifacts/sha256"),
            "strategy": "One inode per immutable binary SHA-256; hardlink same-volume immutable copies, leave distinct hashes separate; never include Wine prefixes in canonical binary store.",
            "estimated_duplicate_allocated_bytes": duplicate_bytes,
            "apfs_clone_aware": False,
            "candidate_hashes": duplicate_hashes,
        },
        "protected_paths": [
            str(REPO),
            str(HOME / ".codex/worktrees/step-11d/FG-Metal"),
            str(HOME / ".codex/worktrees/step11d-r/FG-Metal"),
            str(TMP / "fgmetal-step11d-r-audit-preserved-20261006"),
            str(HOME / "Library/Caches/FGMetalCleanup-20261005"),
            str(KEEP_REPRO),
        ],
        "execution_order": [
            "Verify the required hashes and confirm no Wine/DXVK/MoltenVK experiment process is live.",
            "Capture all listed Wine-prefix registry/config snapshots and complete prefix manifests.",
            "Create build records and hash-keyed artifact-store entries for outputs of every planned build deletion.",
            "Hardlink identical immutable binaries to the canonical store; confirm hashes, modes, device, inode, and link count.",
            "Delete only exact paths marked after their prerequisites pass; keep mixed parent roots and protected paths.",
            "Create one clean Wine template and retain the marked 11D.2 reproducer as the two allowed prefixes.",
            "Regenerate inventory, measure df, verify the 45 GiB and 50 GiB project gates, and write the cleanup receipt.",
        ],
        "notes": [
            "All source/build manifest mismatches remain explicit in build records; current bytes are recorded under their actual SHA-256, not attributed to the earlier manifest.",
            "APFS clone sharing is not visible through st_blocks; actual df delta is authoritative.",
            "No worktree or evidence directory is eligible for deletion in this plan.",
        ],
    }
    OUT.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"plan": str(OUT), "prefix_count": len(prefix_actions),
                      "build_or_mixed_actions": len(actions),
                      "duplicate_binary_bytes_estimate": duplicate_bytes,
                      "free_before": free}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
