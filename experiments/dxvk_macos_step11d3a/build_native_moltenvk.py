#!/usr/bin/env python3
"""Build the pinned x86_64 MoltenVK control under BuildLease.

This runner is source-hash authorized by Gate 0. It copies only the pinned
pristine source seed into the reserved build tree, applies the exact retained
instrumentation patch, builds the declared Xcode scheme, and requires
BuildLease completion to reconcile and retire all outputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
STORAGE_DIR = REPO / "experiments/storage"
IDENTITY_PATH = EXPERIMENT_DIR / "prepared-source-identity.json"
PATCH_PATH = EXPERIMENT_DIR / "moltenvk-instrumentation.patch"
SEED_ROOT = Path("/private/tmp/fgmetal-step11b/MoltenVK")
DEVELOPER_DIR = Path("/Applications/Xcode.app/Contents/Developer")
ARCHITECTURE = "x86_64"
EXPECTED_BUILD_BYTES = 4_500_000_000
PERSISTENT_BYTES = 128 * 1024 * 1024

sys.path.insert(0, str(STORAGE_DIR))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          check=False)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def verify_materialized_source_identity(
    actual_hash: str,
    actual_file_count: int,
    request: build_retention.NativeMoltenVKBuildRequest,
    identity: dict[str, Any],
) -> None:
    expected_file_count = identity.get("base_source_file_count")
    require(type(expected_file_count) is int and expected_file_count > 0,
            "prepared source identity has an invalid base source file count")
    require(actual_hash == request.source_tree_sha256 and actual_file_count == expected_file_count,
            "materialized source tree differs from the pristine source identity")


def toolchain_environment(build_root: Path) -> dict[str, str]:
    return {
        "DEVELOPER_DIR": str(DEVELOPER_DIR),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(build_root / ".tmp/home"),
        "LANG": "en_US.UTF-8",
    }


def command_for(build_root: Path, project: Path) -> tuple[str, ...]:
    return (
        str(DEVELOPER_DIR / "usr/bin/xcodebuild"),
        "-project", str(project),
        "-scheme", "MoltenVK-macOS-dylib",
        "-configuration", "Release",
        "-sdk", "macosx",
        "-destination", "platform=macOS,arch=x86_64",
        "-derivedDataPath", str(build_root / "DerivedData"),
        f"SYMROOT={build_root / 'Build/Products'}",
        f"OBJROOT={build_root / 'Build/Intermediates.noindex'}",
        f"CLANG_MODULE_CACHE_PATH={build_root / 'ModuleCache'}",
        f"COMPILATION_CACHE_CAS_PATH={build_root / 'CompilationCache'}",
        "COMPILER_INDEX_STORE_ENABLE=NO",
        "SDK_STAT_CACHE_ENABLE=NO",
        f"ARCHS={ARCHITECTURE}",
        "ONLY_ACTIVE_ARCH=NO",
        "CODE_SIGNING_ALLOWED=NO",
        "MACOSX_DEPLOYMENT_TARGET=12.0",
        "DEBUG_INFORMATION_FORMAT=dwarf",
        "build",
    )


def declared_outputs() -> tuple[build_retention.DeclaredOutputRole, ...]:
    return (
        build_retention.DeclaredOutputRole(
            role="moltenvk_dylib", relative_path="Build/Products/Release/libMoltenVK.dylib",
            artifact_name="libMoltenVK.dylib", mandatory=True, is_macho=True,
            expected_architectures=(ARCHITECTURE,),
        ),
        build_retention.DeclaredOutputRole(
            role="native_control", relative_path="Build/Products/Release/FGMetalNativeControl",
            artifact_name="FGMetalNativeControl", mandatory=True, is_macho=True,
            expected_architectures=(ARCHITECTURE,),
        ),
        build_retention.DeclaredOutputRole(
            role="moltenvk_static_archive", relative_path="Build/Products/Release/libMoltenVK.a",
            artifact_name="libMoltenVK.a", mandatory=False, is_macho=False,
        ),
        build_retention.DeclaredOutputRole(
            role="shader_converter_static_archive",
            relative_path="Build/Products/Release/libMoltenVKShaderConverter.a",
            artifact_name="libMoltenVKShaderConverter.a", mandatory=False, is_macho=False,
        ),
        build_retention.DeclaredOutputRole(
            role="shader_converter_executable",
            relative_path="Build/Products/Release/MoltenVKShaderConverter",
            artifact_name="MoltenVKShaderConverter", mandatory=False, is_macho=True,
            expected_architectures=(ARCHITECTURE,),
        ),
    )


def make_request(build_id: str) -> tuple[build_retention.NativeMoltenVKBuildRequest, dict[str, Any], Path]:
    identity_bytes = storage_policy._read_file_nofollow(IDENTITY_PATH)
    patch_bytes = storage_policy._read_file_nofollow(PATCH_PATH)
    identity = json.loads(identity_bytes)
    require(identity.get("status") == "PATCH_VERIFIED_NOT_YET_MATERIALIZED_OR_BUILT",
            "prepared source identity is not in the expected pinned prebuild state")
    require(identity.get("base_git_commit") == "db66022459ffb663aa2b50f6b018bc2e124f5edf",
            "prepared source identity has the wrong MoltenVK commit")
    require(identity.get("primary_architecture") == ARCHITECTURE,
            "prepared source identity has the wrong primary architecture")
    require(sha256_bytes(patch_bytes) == identity.get("patch_sha256")
            and len(patch_bytes) == identity.get("patch_size_bytes"),
            "instrumentation patch hash/size does not match prepared-source identity")
    require(SEED_ROOT.is_dir() and not SEED_ROOT.is_symlink(),
            "pinned pristine MoltenVK source seed is unavailable")

    seed_commit = subprocess.run(["/usr/bin/git", "-C", str(SEED_ROOT), "rev-parse", "HEAD"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    seed_tree = subprocess.run(["/usr/bin/git", "-C", str(SEED_ROOT), "rev-parse", "HEAD^{tree}"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(seed_commit.returncode == 0 and seed_tree.returncode == 0
            and seed_commit.stdout.decode().strip() == identity["base_git_commit"]
            and seed_tree.stdout.decode().strip() == identity["base_git_tree"],
            "pristine source seed commit/tree differs from the pinned identity")
    source_hash, file_count = build_retention._tree_hash(SEED_ROOT)
    require(source_hash == identity["base_source_tree_sha256"]
            and file_count == identity["base_source_file_count"],
            "pristine source seed content tree differs from the pinned identity")

    external_revisions = dict(identity["external_revisions"])
    request_root = "MoltenVKSource"
    project_relative = f"{request_root}/{identity['project_relative_path']}"
    build_root = build_retention.BUILD_ROOT / build_id
    project_file_sha = identity["project_file_sha256"]
    modified_files = tuple(sorted(
        (row["path"], row["pristine_sha256"], row["prepared_sha256"])
        for row in identity["changed_files"]
    ))
    environment = toolchain_environment(build_root)
    command = command_for(build_root, build_root / project_relative)
    capture_environment = dict(environment)
    capture_environment["HOME"] = str(Path.home())
    toolchain = build_retention._capture_native_xcode_toolchain(capture_environment)
    request = build_retention.NativeMoltenVKBuildRequest(
        pristine_git_commit=identity["base_git_commit"],
        pristine_git_tree=identity["base_git_tree"],
        source_tree_sha256=identity["base_source_tree_sha256"],
        external_revisions=external_revisions,
        vulkan_headers_revision=external_revisions["Vulkan-Headers"],
        vulkan_headers_tree_sha256=identity["vulkan_headers_tree_sha256"],
        patch_sha256=identity["patch_sha256"],
        patch_size_bytes=identity["patch_size_bytes"],
        modified_files=modified_files,
        build_system="xcode_native",
        project_relative_path=project_relative,
        project_file_sha256=project_file_sha,
        target_or_scheme="MoltenVK-macOS-dylib",
        configuration="Release",
        architectures=(ARCHITECTURE,),
        deployment_target="12.0",
        sdk_name="macosx",
        xcode_version_string=toolchain["xcode_version_string"],
        clang_version_string=toolchain["clang_version_string"],
        clang_binary_sha256=toolchain["clang_binary_sha256"],
        sdk_version_string=toolchain["sdk_version_string"],
        linker_version_string=toolchain["linker_version_string"],
        toolchain_identity_sha256=toolchain["toolchain_identity_sha256"],
        normalized_environment=environment,
        normalized_arguments=command,
        declared_outputs=declared_outputs(),
        prepared_source_root_relative_path=request_root,
    )
    return request, identity, build_root


def main() -> int:
    if len(sys.argv) != 1:
        print("usage: build_native_moltenvk.py", file=sys.stderr)
        return 2
    build_id = "step11d3a-mvk-x86_64-20261008-02"
    output_dir = EXPERIMENT_DIR / "evidence/native-build-contract-20261008-02"
    request, identity, build_root = make_request(build_id)
    config_key = build_retention.compute_native_moltenvk_config_key(request)
    dependency_state = {
        "status": "pinned_source_and_external_revisions",
        "identity_sha256": sha256_file(IDENTITY_PATH),
        "source_seed_path": str(SEED_ROOT),
        "source_tree_sha256": request.source_tree_sha256,
        "patch_sha256": request.patch_sha256,
        "external_revisions": dict(request.external_revisions),
        "architecture": ARCHITECTURE,
    }
    runner_path = Path(__file__).resolve()
    manifest = storage_policy.begin_experiment(
        experiment_id="step11d3a-native-mvk-build-20261008-02",
        purpose="Fresh leased x86_64 MoltenVK v1.4.2 instrumentation build and linked native control",
        candidate_hashes={
            "prepared_source_identity": sha256_file(IDENTITY_PATH),
            "instrumentation_patch": request.patch_sha256,
            "native_control_source": dict((row[0], row[2]) for row in request.modified_files)
                ["NativeControl/native_control.mm"],
            "build_runner": sha256_file(runner_path),
            "pinned_source_tree": request.source_tree_sha256,
        },
        expected_duration_seconds=1800,
        expected_prefix_bytes=0,
        expected_build_bytes=EXPECTED_BUILD_BYTES,
        estimated_persistent_bytes=PERSISTENT_BYTES,
        storage_justification="One serialized native diagnostic build; all temporary products are retired by BuildLease and only declared CAS artifacts and compact provenance are retained.",
        authorized_runner_paths=[runner_path],
        build_source_tree_hash=request.source_tree_sha256,
        build_configuration_key_sha256=config_key,
        build_kind="moltenvk",
        build_type="release",
        dependency_state=dependency_state,
        native_architecture=ARCHITECTURE,
        output_dir=output_dir,
        additional={
            "native_moltenvk_build_request": build_retention._native_request_to_dict(request),
            "build_id": build_id,
            "source_identity_path": str(IDENTITY_PATH),
            "source_identity_sha256": sha256_file(IDENTITY_PATH),
        },
    )
    require(manifest["decision"] == "ALLOW", "Gate 0 refused native build admission: "
            + "; ".join(manifest.get("blockers", [])))

    lease = build_retention.BuildLease(
        build_id,
        kind="moltenvk",
        owner_experiment=manifest["experiment_id"],
        purpose="fresh STEP 11D.3-A instrumented MoltenVK build",
        source_tree_hash=request.source_tree_sha256,
        expected_bytes=EXPECTED_BUILD_BYTES,
        metadata={
            "build_type": "release",
            "dependency_state": dependency_state,
            "native_moltenvk_request": build_retention._native_request_to_dict(request),
            "failure_capture_paths": [],
        },
        runner_path=runner_path,
        configuration_key_sha256=config_key,
    )
    reserved_path = lease.reserve()
    require(reserved_path == build_root and lease.started,
            "fresh native build was not reserved (canonical reuse is not accepted)")
    source_root = build_root / request.prepared_source_root_relative_path
    try:
        tmp_home = build_root / ".tmp/home"
        tmp_home.mkdir(parents=True, mode=0o700)
        shutil.copytree(SEED_ROOT, source_root, symlinks=True, copy_function=shutil.copy2)
        materialized_hash, materialized_count = build_retention._tree_hash(source_root)
        verify_materialized_source_identity(materialized_hash, materialized_count, request, identity)
        patch_check = run(["/usr/bin/git", "-C", str(source_root), "apply", "--check",
                           "--whitespace=error", str(PATCH_PATH)])
        require(patch_check.returncode == 0,
                "instrumentation patch does not apply to the materialized pinned source: "
                + patch_check.stdout.decode("utf-8", errors="replace")[-4000:])
        patch_apply = run(["/usr/bin/git", "-C", str(source_root), "apply",
                           "--whitespace=error", str(PATCH_PATH)])
        require(patch_apply.returncode == 0,
                "instrumentation patch apply failed: "
                + patch_apply.stdout.decode("utf-8", errors="replace")[-4000:])
        diff_check = run(["/usr/bin/git", "-C", str(source_root), "diff", "--check"])
        require(diff_check.returncode == 0,
                "prepared native source has whitespace errors: "
                + diff_check.stdout.decode("utf-8", errors="replace")[-4000:])
        for relative, _pre_sha, expected_post_sha in request.modified_files:
            actual = sha256_file(source_root / relative)
            require(actual == expected_post_sha, f"prepared source hash mismatch: {relative}")
        expected_status = sorted(
            (path, "??" if pre_sha == "ABSENT" else " M")
            for path, pre_sha, _post_sha in request.modified_files
        )
        status = subprocess.run(["/usr/bin/git", "-C", str(source_root), "status",
                                 "--porcelain=v1", "-z", "--untracked-files=all"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        require(status.returncode == 0, "could not read prepared source Git status")
        rows = []
        for record in status.stdout.split(b"\0"):
            if record:
                rows.append((os.fsdecode(record[3:]), os.fsdecode(record[:2])))
        require(sorted(rows) == expected_status,
                f"prepared source Git status differs from exact patch file set: {sorted(rows)!r}")

        result = lease.run(list(request.normalized_arguments), cwd=build_root,
                           env=dict(request.normalized_environment))
        require(result.get("status") == "COMPLETED_AND_AUTO_RETIRED",
                f"native build was not completed and auto-retired: {result.get('status')}")
        build_manifest, build_manifest_sha = build_retention._verify_build_manifest(build_id)
        output_records = build_manifest.get("artifacts", [])
        roles = {row.get("role"): row for row in output_records}
        require(set(("moltenvk_dylib", "native_control")).issubset(roles),
                "build manifest omits the required MoltenVK dylib or native control")
        for role in ("moltenvk_dylib", "native_control"):
            require(build_retention._verify_artifact(roles[role]),
                    f"canonical artifact verification failed for {role}")
        snapshot = build_retention.status_snapshot()
        require(snapshot.get("active_build_count") == 0,
                "active build lease remains after native build completion")
        require(snapshot.get("retained_build_count") == 0,
                "temporary native build remains retained after completion")
        output_dir.mkdir(parents=True, exist_ok=False)
        output = {
            "status": "PASS",
            "experiment_id": manifest["experiment_id"],
            "build_id": build_id,
            "build_manifest_id": build_manifest["build_manifest_id"],
            "build_manifest_path": str(STORAGE_DIR / "build-manifests" / f"{build_id}.json"),
            "build_manifest_sha256": build_manifest_sha,
            "cleanup_receipt": result.get("cleanup_receipt"),
            "architecture": ARCHITECTURE,
            "prepared_source": build_manifest["build_identity"].get("prepared_source_verification"),
            "artifacts": output_records,
            "native_moltenvk_request": build_retention._native_request_to_dict(request),
            "toolchain": {
                "xcode_version": request.xcode_version_string,
                "clang_version": request.clang_version_string,
                "clang_binary_sha256": request.clang_binary_sha256,
                "sdk_version": request.sdk_version_string,
                "linker_version": request.linker_version_string,
                "toolchain_identity_sha256": request.toolchain_identity_sha256,
            },
            "lease_status_after_completion": snapshot,
            "completed_at_unix": time.time(),
        }
        (output_dir / "build-result.json").write_text(
            json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": output["status"], "build_manifest_id": output["build_manifest_id"],
                          "build_manifest_sha256": build_manifest_sha,
                          "artifacts": [{"role": row["role"], "sha256": row["sha256"],
                                         "path": row["canonical_artifact_path"]}
                                        for row in output_records]},
                         sort_keys=True))
        return 0
    except BaseException as error:
        if lease.started:
            try:
                current = build_retention._load_lease(build_id)
                if current.get("state") == "ACTIVE":
                    log = build_root / "build.log"
                    if not log.exists():
                        log.write_text(repr(error) + "\n", encoding="utf-8")
                    lease.fail(command=list(request.normalized_arguments), return_code=-1,
                               log_path=log if log.is_file() else None, error=repr(error))
            except BaseException as cleanup_error:
                print(f"BUILD_LEASE_FAILURE={cleanup_error!r}", file=sys.stderr)
        print(f"NATIVE_BUILD_FAILURE={error!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
