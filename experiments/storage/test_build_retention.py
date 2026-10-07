#!/usr/bin/env python3
"""Synthetic-only tests for fail-closed build retention; never create graphics builds."""
from __future__ import annotations

import hashlib
import dataclasses
import io
import os
import plistlib
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import build_retention as retention
import storage_policy
import storage_inventory
import storage_gate0

_ORIGINAL_REQUIRE_NO_LIVE_PROCESS = retention._require_no_live_process


def _synthetic_native_toolchain_observation(request: retention.NativeMoltenVKBuildRequest) -> dict[str, str]:
    return {
        "xcode_version_string": request.xcode_version_string,
        "clang_version_string": request.clang_version_string,
        "clang_binary_sha256": request.clang_binary_sha256,
        "sdk_version_string": request.sdk_version_string,
        "linker_version_string": request.linker_version_string,
        "toolchain_identity_sha256": request.toolchain_identity_sha256,
    }


class BuildRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-retention-test-", dir="/private/tmp")
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.cache = base / "cache"
        self.repo.mkdir(mode=0o700)
        self.cache.mkdir(mode=0o700)
        self.manifests = self.repo / "experiments/storage/manifests"
        self.manifests.mkdir(parents=True, mode=0o700)
        self.gate_attestation = self.repo / "storage-gate0-attestation.json"
        self.gate_attestation.write_text("synthetic-attestation", encoding="utf-8")
        self.runner_file = self.repo / "synthetic-runner.py"
        self.runner_file.write_text("# synthetic runner\n", encoding="utf-8")
        temp_roots = base / "tmp-roots"
        home = base / "home"
        temp_roots.mkdir()
        home.mkdir()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            mock.patch.object(retention, "REPO", self.repo),
            mock.patch.object(retention, "CACHE_ROOT", self.cache),
            mock.patch.object(retention, "BUILD_ROOT", self.cache / "builds"),
            mock.patch.object(retention, "ARTIFACT_ROOT", self.cache / "artifacts/sha256"),
            mock.patch.object(retention, "STORAGE_ROOT", self.repo / "experiments/storage"),
            mock.patch.object(retention, "LEASE_ROOT", self.repo / "experiments/storage/build-leases"),
            mock.patch.object(retention, "MANIFEST_ROOT", self.repo / "experiments/storage/build-manifests"),
            mock.patch.object(retention, "RECEIPT_ROOT", self.repo / "experiments/storage/build-cleanup-receipts"),
            mock.patch.object(retention, "LEGACY_BUILD_ROOTS", ()),
            mock.patch.object(retention, "TEMP_ROOT", temp_roots),
            mock.patch.object(retention, "HOME", home),
            mock.patch.object(retention, "_require_no_live_process", return_value=None),
            mock.patch.object(retention, "_key", return_value=b"r" * 32),
            mock.patch.object(storage_policy, "REPO", self.repo),
            mock.patch.object(storage_policy, "GATE0_ATTESTATION_PATH", self.gate_attestation),
            mock.patch.object(storage_policy, "_experiment_manifest_path",
                              side_effect=lambda experiment_id: self.manifests / f"{experiment_id}.json"),
            mock.patch.object(storage_policy, "_verify_manifest_signature", return_value=True),
            mock.patch.object(storage_policy, "_validated_manifest_estimates", side_effect=self._manifest_estimates),
            mock.patch.object(storage_policy, "disk_free_bytes", return_value=100 * retention.GIB),
            mock.patch.object(storage_policy, "measure_project_usage",
                              return_value={"allocated_inode_deduplicated_bytes": 0}),
            mock.patch.object(retention, "_gate_blockers", return_value=[]),
            mock.patch.object(retention, "_capture_identity", side_effect=self._capture_identity_for_build),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._write_owner_manifest()

    def _manifest_estimates(self, manifest: dict) -> dict[str, int]:
        return {"expected_build_bytes": 64 * 1024 ** 2,
                "estimated_max_new_disk_bytes": 64 * 1024 ** 2}

    def _write_owner_manifest(self, source_hash: str | None = None, config_hash: str | None = None,
                              build_kind: str = "dxvk", build_type: str = "release",
                              preserve_debug_reason: str | None = None,
                              estimated_debug_bytes: int | None = None,
                              failure_capture_paths: list[str] | None = None,
                              dependency_state: dict | None = None) -> None:
        state = dependency_state if dependency_state is not None else {"status": "captured", "lock": "synthetic dependency state"}
        source_hash = source_hash or hashlib.sha256(b"completed source tree").hexdigest()
        config_hash = config_hash or retention.configuration_key_from_identity(
            self._synthetic_identity(source_hash, state), dependency_state=state)
        manifest = {"experiment_id": "synthetic-test", "decision": "ALLOW",
                    "storage_policy_smoke": False, "expected_build_bytes": 64 * 1024 ** 2,
                    "build_source_tree_hash": source_hash,
                    "build_configuration_key_sha256": config_hash,
                    "build_kind": build_kind,
                    "build_type": build_type,
                    "dependency_state": state,
                    "preserve_debug_reason": preserve_debug_reason,
                    "estimated_debug_bytes": estimated_debug_bytes,
                    "failure_capture_paths": failure_capture_paths or [],
                    "authorized_runner_paths": [str(self.runner_file.resolve())],
                    "authorized_runner_sha256": {
                        str(self.runner_file.resolve()): hashlib.sha256(self.runner_file.read_bytes()).hexdigest()},
                    "gate0_attestation_sha256": hashlib.sha256(self.gate_attestation.read_bytes()).hexdigest()}
        (self.manifests / "synthetic-test.json").write_text(__import__("json").dumps(manifest), encoding="utf-8")

    @staticmethod
    def _synthetic_identity(source_hash: str, dependency_state: dict, build_type: str = "release") -> dict:
        return {"build_system": "meson", "source_identity": {"content_tree_sha256": source_hash},
                "patch_set": {"patch_set_sha256": hashlib.sha256(b"no patches").hexdigest()},
                "cross_file": {"status": "absent"},
                "meson_options": [{"name": "buildtype", "value": "release"}],
                "compiler_identity": {"c": {"version": "synthetic cc 1"}},
                "compiler_identity_sha256": hashlib.sha256(b"synthetic cc identity").hexdigest(),
                "linker_identity": [{"command": "ld -v", "output": "synthetic ld"}],
                "generated_shader_identity": [], "build_type": build_type,
                "dependency_state": dependency_state}

    def _capture_identity(self, lease: retention.BuildLease, evidence: Path) -> dict:
        identity = self._synthetic_identity(lease.source_tree_hash, lease.metadata["dependency_state"],
                                            lease.metadata["build_type"])
        rows = {
            "meson_log_evidence": ("logs/meson-log.txt", b"synthetic meson configure log"),
            "meson_command_line_evidence": ("configuration/meson-cmd-line.txt", b"meson setup synthetic"),
            "meson_options_evidence": ("configuration/meson-build-options.json", b"synthetic options"),
            "compiler_evidence": ("configuration/meson-compilers.json", b"synthetic compiler"),
            "build_ninja_evidence": ("configuration/build.ninja", b"synthetic graph"),
            "ninja_log_evidence": ("logs/ninja-log.txt", b"synthetic ninja execution"),
            "intro-dependencies_json_evidence": ("configuration/meson-intro-dependencies.json", b"[]"),
        }
        for key, (relative, contents) in rows.items():
            record = retention._write_evidence_bytes(evidence / relative, contents)
            identity[key] = record
        identity.update({
            "meson_log_sha256": identity["meson_log_evidence"]["sha256"],
            "meson_options_sha256": hashlib.sha256(b"synthetic options").hexdigest(),
            "meson_command_line_sha256": hashlib.sha256(b"meson setup synthetic").hexdigest(),
            "ninja_log_sha256": identity["ninja_log_evidence"]["sha256"],
            "ninja_execution_log_status": "captured",
            "build_log_sha256": None,
            "build_log_status": "no stdout build transcript present; configure log and Ninja execution log retained separately",
        })
        identity["config_key_sha256"] = retention.configuration_key_from_identity(
            identity, dependency_state=identity["dependency_state"])
        return identity

    def _capture_identity_for_build(self, build: Path, evidence: Path) -> dict:
        lease = retention._load_lease(build.name)
        lease_view = type("LeaseView", (), {
            "source_tree_hash": lease["source_tree_hash"], "metadata": lease.get("metadata", {})})()
        return self._capture_identity(lease_view, evidence)

    def _lease(self, build_id: str, *, kind: str = "dxvk", preserve: bool = False,
                source_tree_hash: str | None = None, build_type: str = "release",
                failure_capture_paths: list[str] | None = None,
                debug_reason: str | None = None, estimated_debug_bytes: int | None = None) -> retention.BuildLease:
        metadata = {"preserve_debug_expiry_unix": time.time() + 3600} if preserve else None
        dependency_state = {"status": "captured", "lock": "synthetic dependency state"}
        preserve_reason = debug_reason or ("needed to test debug retention" if preserve else None)
        metadata = {**(metadata or {}), "dependency_state": dependency_state, "build_type": build_type,
                    "failure_capture_paths": failure_capture_paths or [],
                    "estimated_debug_bytes": estimated_debug_bytes}
        source_hash = source_tree_hash or hashlib.sha256(b"completed source tree").hexdigest()
        config_hash = retention.configuration_key_from_identity(
            self._synthetic_identity(source_hash, dependency_state, build_type), dependency_state=dependency_state)
        self._write_owner_manifest(source_hash, config_hash, build_kind=kind, build_type=build_type,
                                   preserve_debug_reason=preserve_reason,
                                   estimated_debug_bytes=estimated_debug_bytes,
                                   failure_capture_paths=failure_capture_paths)
        return retention.BuildLease(
            build_id, kind=kind, owner_experiment="synthetic-test", purpose="retention test",
            source_tree_hash=source_hash,
            expected_bytes=64 * 1024 * 1024,
            metadata=metadata, preserve_debug_reason=preserve_reason,
            runner_path=self.runner_file,
            configuration_key_sha256=config_hash,
        )

    @staticmethod
    def _output(lease: retention.BuildLease, payload: bytes = b"synthetic DLL") -> None:
        (lease.path / "synthetic.dll").write_bytes(payload)
        (lease.path / "build.ninja").write_text("synthetic graph", encoding="utf-8")
        (lease.path / ".ninja_log").write_text("synthetic execution", encoding="utf-8")

    def _complete_without_retirement(self, lease: retention.BuildLease) -> dict:
        self._authorize_synthetic_execution(lease)
        with mock.patch.object(retention, "_retire", return_value={"result": "PENDING"}):
            result = lease.complete()
        lease._release_lock()
        return result

    @staticmethod
    def _authorize_synthetic_execution(lease: retention.BuildLease) -> None:
        (lease.path / "build.log").write_bytes(b"synthetic successful build log\n")
        with (lease.path / "build.log").open("rb") as stream:
            os.fsync(stream.fileno())
        native_request_data = lease.metadata.get("native_moltenvk_request")
        native_kwargs = {}
        if native_request_data:
            request = retention._dict_to_native_request(native_request_data)
            declared_environment = dict(request.normalized_environment)
            temporary_path = str(lease.path / ".tmp")
            effective_environment = {
                **declared_environment,
                "TMPDIR": temporary_path,
                "TMP": temporary_path,
                "TEMP": temporary_path,
            }
            native_kwargs = {
                "declared_environment": declared_environment,
                "effective_environment": effective_environment,
                "native_toolchain_observation": _synthetic_native_toolchain_observation(request),
            }
        lease._record_successful_sandboxed_run(
            ["meson", "compile", "-C", str(lease.path)], time.time(),
            retention._sandbox_profile_text(lease.path),
            retention._hash_path(Path("/usr/bin/sandbox-exec")),
            **native_kwargs)

    @staticmethod
    def _complete_native(lease: retention.BuildLease) -> dict:
        request = retention._dict_to_native_request(lease.metadata["native_moltenvk_request"])
        with mock.patch.object(
                retention, "_capture_native_xcode_toolchain",
                return_value=_synthetic_native_toolchain_observation(request)):
            return lease.complete()

    def test_completed_build_is_automatically_removed(self) -> None:
        lease = self._lease("completed-auto")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertFalse(lease.path.exists())
        self.assertEqual(result["cleanup_receipt"]["result"], "REMOVED")

    def test_new_build_kind_is_bound_to_owner_manifest_and_capacity_class(self) -> None:
        with self.assertRaisesRegex(ValueError, "capped DXVK, MoltenVK, or reference/debug kind"):
            self._lease("uncapped-wine-alias", kind="wine")

        lease = self._lease("kind-manifest-mismatch")
        self._write_owner_manifest(lease.source_tree_hash,
                                   lease.configuration_key_sha256, build_kind="moltenvk")
        with self.assertRaisesRegex(retention.RetentionError, "differs from its signed experiment build contract"):
            lease.reserve()

    def test_active_build_is_preserved(self) -> None:
        lease = self._lease("active-preserved")
        lease.reserve()
        self._output(lease)
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "ACTIVE")
        lease.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_preserve_debug_build_is_preserved(self) -> None:
        lease = self._lease("debug-preserved", preserve=True)
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete(preserve_debug=True)
        self.assertEqual(result["status"], "PRESERVE_DEBUG")
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "PRESERVE_DEBUG")
        lease._release_lock()
        with mock.patch.object(retention, "_retire", side_effect=retention.RetentionError("injected close retry")):
            with self.assertRaisesRegex(retention.RetentionError, "injected close retry"):
                retention.close_debug_preservation(lease.build_id, reason="synthetic debug task closed")
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "COMPLETED")
        receipt = retention.close_debug_preservation(lease.build_id, reason="synthetic debug task closed")
        self.assertEqual(receipt["result"], "REMOVED")
        self.assertFalse(lease.path.exists())

    def test_full_debug_build_requires_signed_reason_and_estimate(self) -> None:
        with self.assertRaisesRegex(ValueError, "full debug/unstripped builds require a reason"):
            self._lease("debug-without-opt-in", build_type="debug")
        lease = self._lease("debug-with-explicit-opt-in", build_type="debug",
                            debug_reason="Inspect optimizer-sensitive compiler output",
                            estimated_debug_bytes=32 * 1024 * 1024)
        lease.reserve()
        saved = retention._load_lease(lease.build_id)
        self.assertEqual(saved["metadata"]["build_type"], "debug")
        self.assertEqual(saved["metadata"]["estimated_debug_bytes"], 32 * 1024 * 1024)
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete()
        manifest, _ = retention._verify_build_manifest(lease.build_id)
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertEqual(manifest["requested_build_type"], "debug")
        self.assertEqual(manifest["build_type"], "debug")
        self.assertEqual(manifest["estimated_debug_bytes"], 32 * 1024 * 1024)
        self.assertEqual(manifest["preserve_debug_reason"], "Inspect optimizer-sensitive compiler output")

    def test_signed_failure_capture_path_is_retained_then_build_is_removed(self) -> None:
        lease = self._lease("selected-generated-failure",
                            failure_capture_paths=["generated/diagnostic.c", "generated__diagnostic.c"])
        lease.reserve()
        self._output(lease)
        generated = lease.path / "generated/diagnostic.c"
        generated.parent.mkdir()
        generated.write_text("compiler diagnostic source\n", encoding="utf-8")
        colliding_flattened_name = lease.path / "generated__diagnostic.c"
        colliding_flattened_name.write_text("different file with former flat-name collision\n", encoding="utf-8")
        receipt = lease.fail(command=["synthetic"], return_code=2, error="compile failed")
        self.assertEqual(receipt["result"], "REMOVED")
        self.assertFalse(lease.path.exists())
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.failure" / "failure-manifest.json"
        failure = retention._read_json(evidence)
        retained = {row["relative_path"]: row for row in failure["generated_diagnostics"]}
        self.assertEqual(retained["generated/diagnostic.c"]["status"], "CAPTURED")
        self.assertEqual(retained["generated__diagnostic.c"]["status"], "CAPTURED")
        self.assertNotEqual(retained["generated/diagnostic.c"]["retained_path"],
                            retained["generated__diagnostic.c"]["retained_path"])
        self.assertEqual(retained["generated/diagnostic.c"]["sha256"],
                         hashlib.sha256(b"compiler diagnostic source\n").hexdigest())

    def test_missing_optional_failure_capture_is_recorded_and_build_is_removed(self) -> None:
        lease = self._lease("missing-optional-diagnostic", failure_capture_paths=["generated/maybe.c"])
        lease.reserve()
        self._output(lease)
        result = lease.fail(command=["synthetic"], return_code=1, error="failed before source generation")
        self.assertEqual(result["result"], "REMOVED")
        manifest = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure" / "failure-manifest.json")
        self.assertEqual(manifest["generated_diagnostics"], [
            {"relative_path": "generated/maybe.c", "status": "MISSING"}])
        self.assertFalse(lease.path.exists())

    def test_symlinked_selected_failure_diagnostic_refuses_retirement(self) -> None:
        lease = self._lease("unsafe-selected-diagnostic", failure_capture_paths=["generated/link.c"])
        lease.reserve()
        outside = self.repo / "outside-diagnostic.c"
        outside.write_text("outside source must remain untouched\n", encoding="utf-8")
        selected = lease.path / "generated/link.c"
        selected.parent.mkdir()
        selected.symlink_to(outside)
        with self.assertRaisesRegex(retention.RetentionError, "is a symlink"):
            lease.fail(command=["synthetic"], return_code=1, error="unsafe diagnostic")
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside source must remain untouched\n")
        selected.unlink()
        lease.fail(command=["synthetic"], return_code=1, error="retry without unsafe link")

    def test_expired_preserved_debug_build_is_retired_during_reconciliation(self) -> None:
        lease = self._lease("debug-expiry-reconcile", preserve=True)
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        lease.complete(preserve_debug=True)
        expires = retention._load_lease(lease.build_id)["preserve_debug_expires_at_unix"]
        lock_fds = retention._lock()
        try:
            with mock.patch.object(retention.time, "time", return_value=expires + 1):
                receipts = retention._retire_pending_locked()
        finally:
            retention._unlock(*lock_fds)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["result"], "REMOVED")
        self.assertEqual(retention._load_lease(lease.build_id)["retirement_reason"],
                         "debug-preservation-expired")
        self.assertFalse(lease.path.exists())

    def test_preserved_debug_slot_blocks_legacy_promotion_before_artifactization(self) -> None:
        retained = self._lease("debug-slot-occupant", preserve=True)
        retained.reserve()
        self._output(retained)
        self._authorize_synthetic_execution(retained)
        retained.complete(preserve_debug=True)

        second_debug = self._lease("debug-slot-second-request", preserve=True,
                                   source_tree_hash=hashlib.sha256(b"different debug source").hexdigest())
        second_debug.reserve()
        self._output(second_debug)
        self._authorize_synthetic_execution(second_debug)
        with self.assertRaisesRegex(retention.RetentionError, "preservation slot is occupied"):
            second_debug.complete(preserve_debug=True)
        self.assertTrue(second_debug.path.is_dir())
        second_debug.fail(command=["synthetic"], return_code=-1, error="preservation cap refused")

        parent = self.repo / "legacy-debug-promotion-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-debug-promotion"
        legacy.mkdir(mode=0o700)
        (legacy / "synthetic.dll").write_bytes(b"debug slot probe")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(
                legacy, kind="reference", owner_experiment="synthetic-test", purpose="slot promotion test",
                expected_expiry_unix=time.time() + 3600,
                preserve_debug_reason="Retain only if the configured debug slot is available")
            with self.assertRaisesRegex(retention.RetentionError, "preservation slot is occupied"):
                retention.complete_adopted(adopted["build_id"], preserve_debug=True)
            self.assertTrue(Path(adopted["path"]).is_dir())
            self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.json").exists())
            retention.fail_adopted(adopted["build_id"], reason="promotion refused", capture_paths=())
        retention.close_debug_preservation(retained.build_id, reason="synthetic debug task closed")

    def test_legacy_adoption_respects_global_build_cap_before_move(self) -> None:
        dxvk = self._lease("adoption-cap-dxvk")
        dxvk.reserve()
        dxvk._release_lock()
        moltenvk = self._lease("adoption-cap-moltenvk", kind="moltenvk")
        moltenvk.reserve()
        moltenvk._release_lock()
        parent = self.repo / "legacy-adoption-cap-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-adoption-cap"
        legacy.mkdir(mode=0o700)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            with self.assertRaisesRegex(retention.RetentionError, "global active large-build cap"):
                retention.adopt_legacy(legacy, kind="reference", owner_experiment="synthetic-test",
                                       purpose="adoption cap test")
        self.assertTrue(legacy.is_dir())
        self.assertFalse(list(retention.BUILD_ROOT.glob("legacy-adoption-cap-*")))
        moltenvk.fail(command=["synthetic"], return_code=1, error="test cleanup")
        dxvk.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_active_dxvk_cap_blocks_another_build(self) -> None:
        first = self._lease("cap-first")
        first.reserve()
        first._release_lock()
        with self.assertRaises(retention.RetentionError):
            self._lease("cap-second").reserve()
        first.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_one_dxvk_and_one_moltenvk_can_coexist_but_third_active_build_is_blocked(self) -> None:
        dxvk = self._lease("active-dxvk-serialized")
        dxvk.reserve()
        dxvk._release_lock()
        moltenvk = self._lease("active-moltenvk-serialized", kind="moltenvk")
        moltenvk.reserve()
        moltenvk._release_lock()
        extra = self._lease("active-extra-dxvk-serialized", source_tree_hash=hashlib.sha256(b"extra source").hexdigest())
        with self.assertRaisesRegex(retention.RetentionError, "global active large-build cap"):
            extra.reserve()
        self.assertFalse(extra.path.exists())
        moltenvk.fail(command=["synthetic"], return_code=1, error="test cleanup")
        dxvk.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_completed_build_retires_before_new_capacity_is_granted(self) -> None:
        first = self._lease("cap-retire-first")
        first.reserve()
        self._output(first)
        self._complete_without_retirement(first)
        self.assertTrue(first.path.exists())
        second = self._lease("cap-retire-second", source_tree_hash=hashlib.sha256(b"different source").hexdigest())
        second.reserve()
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())
        second.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_missing_artifact_refuses_deletion(self) -> None:
        lease = self._lease("missing-artifact")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        manifest = retention._read_json(retention.MANIFEST_ROOT / f"{lease.build_id}.json")
        payload = Path(manifest["artifacts"][0]["canonical_artifact_path"])
        payload.unlink()
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def test_artifact_hash_mismatch_refuses_deletion(self) -> None:
        lease = self._lease("bad-artifact-hash")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        manifest = retention._read_json(retention.MANIFEST_ROOT / f"{lease.build_id}.json")
        payload = Path(manifest["artifacts"][0]["canonical_artifact_path"])
        payload.chmod(0o600)
        payload.write_bytes(b"tampered")
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def test_symlink_escape_is_refused(self) -> None:
        lease = self._lease("symlink-escape")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        outside = self.repo / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        (lease.path / "escape").symlink_to(outside)
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(outside.exists())
        self.assertTrue(lease.path.exists())

    def test_path_replacement_race_is_refused(self) -> None:
        lease = self._lease("path-replaced")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        displaced = lease.path.with_name(lease.path.name + ".displaced")
        lease.path.rename(displaced)
        lease.path.mkdir(mode=0o700)
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(displaced.exists())
        self.assertTrue(lease.path.exists())

    def test_namespace_swap_at_quarantine_is_refused_without_deleting_original(self) -> None:
        lease = self._lease("quarantine-race")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        real_rename = retention._rename_exclusive
        displaced = lease.path.with_name(lease.path.name + ".raced-original")
        def swap_then_rename(parent_fd: int, source: str, target: str) -> None:
            os.rename(source, displaced.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(source, 0o700, dir_fd=parent_fd)
            real_rename(parent_fd, source, target)
        with mock.patch.object(retention, "_rename_exclusive", side_effect=swap_then_rename):
            lock_fds = retention._lock()
            try:
                with self.assertRaises(retention.RetentionError):
                    retention._retire(retention._load_lease(lease.build_id), reason="test-race")
            finally:
                retention._unlock(*lock_fds)
        self.assertTrue((displaced / "synthetic.dll").is_file())
        current = retention._load_lease(lease.build_id)
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).is_dir())

    def test_legacy_adoption_replacement_is_rolled_back_and_lease_removed(self) -> None:
        parent = self.repo / "legacy-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "original.txt").write_text("original directory\n", encoding="utf-8")
        replacement_backup = parent / "original-renamed-by-racer"
        original_rename = retention._rename_exclusive_between

        def replace_source_then_move(source_fd: int, source: str, target_fd: int, target: str) -> None:
            if source == legacy.name and target_fd != source_fd:
                os.rename(source, replacement_backup.name, src_dir_fd=source_fd, dst_dir_fd=source_fd)
                os.mkdir(source, 0o700, dir_fd=source_fd)
                replacement_fd = os.open(source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=source_fd)
                try:
                    descriptor = os.open("replacement.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=replacement_fd)
                    os.write(descriptor, b"replacement directory\n")
                    os.close(descriptor)
                finally:
                    os.close(replacement_fd)
            original_rename(source_fd, source, target_fd, target)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_rename_exclusive_between", side_effect=replace_source_then_move):
            with self.assertRaisesRegex(retention.RetentionError, "namespace was rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="race regression")
        self.assertTrue((legacy / "replacement.txt").is_file())
        self.assertTrue((replacement_backup / "original.txt").is_file())
        self.assertFalse(list(retention.BUILD_ROOT.iterdir()))
        self.assertFalse(list(retention.LEASE_ROOT.glob("legacy-*.json")))

    def test_legacy_adoption_lease_persistence_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-lease-write-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        original_store = retention._store_lease

        def fail_moved_lease(lease):
            if lease.get("adopted_at_unix") is not None:
                raise OSError("injected lease persistence failure")
            return original_store(lease)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_store_lease", side_effect=fail_moved_lease):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="lease persistence failure recovery")
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_adoption_marker_write_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-marker-write-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_write_marker", side_effect=OSError("injected marker write failure")):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="marker persistence failure recovery")
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_adoption_directory_fsync_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-fsync-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        original_identity = legacy.stat()
        original_fsync = os.fsync
        root_fsyncs = 0

        def fail_final_root_fsync(fd: int) -> None:
            nonlocal root_fsyncs
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
                    original_identity.st_dev, original_identity.st_ino):
                root_fsyncs += 1
                if root_fsyncs == 2:
                    raise OSError("injected final directory fsync failure")
            original_fsync(fd)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention.os, "fsync", side_effect=fail_final_root_fsync):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="directory fsync failure recovery")
        self.assertEqual(root_fsyncs, 3)  # marker durability, injected failure, then marker removal
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_failure_capture_is_selective_and_retryable(self) -> None:
        parent = self.repo / "legacy-failure-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-failure-build"
        legacy.mkdir(mode=0o700)
        (legacy / "config.log").write_text("configure failed: synthetic diagnostic\n", encoding="utf-8")
        (legacy / "unneeded-bulk.bin").write_bytes(b"bulk output" * 64)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="wine", owner_experiment="synthetic-test",
                                             purpose="selected failure evidence test")
            build_id = adopted["build_id"]
            with mock.patch.object(retention, "_retire", side_effect=retention.RetentionError("injected")):
                with self.assertRaisesRegex(retention.RetentionError, "injected"):
                    retention.fail_adopted(build_id, reason="synthetic configure failure",
                                           capture_paths=("config.log",))
            self.assertEqual(retention._load_lease(build_id)["state"], "FAILED")
            result = retention.fail_adopted(build_id, reason="synthetic configure failure",
                                             capture_paths=("config.log",))
        self.assertEqual(result["result"], "REMOVED")
        self.assertFalse(Path(adopted["path"]).exists())
        evidence = retention.MANIFEST_ROOT / f"{build_id}.failure"
        failure_manifest = retention._read_json(evidence / "failure-manifest.json")
        self.assertEqual(failure_manifest["capture_paths"], ["config.log"])
        self.assertEqual(len(failure_manifest["retained_files"]), 1)
        retained = Path(failure_manifest["retained_files"][0]["retained_path"])
        self.assertTrue(retained.is_file())
        self.assertNotIn("unneeded-bulk.bin", retained.name)

    def test_legacy_copy_all_failure_capture_requires_a_bounded_small_root(self) -> None:
        parent = self.repo / "legacy-large-failure-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-large-failure-build"
        legacy.mkdir(mode=0o700)
        with (legacy / "oversized-diagnostic.bin").open("wb") as stream:
            stream.truncate(retention.MAX_COPY_ALL_FAILURE_ROOT_BYTES + 1)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="copy-all size refusal")
            with self.assertRaisesRegex(retention.RetentionError, "copy-all legacy failure evidence"):
                retention.fail_adopted(adopted["build_id"], reason="synthetic failure")
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure" / "failure-manifest.json").exists())

    def test_legacy_copy_all_failure_capture_bounds_many_empty_files_before_capture(self) -> None:
        parent = self.repo / "legacy-many-empty-files-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-many-empty-files-build"
        legacy.mkdir(mode=0o700)
        for index in range(9):
            (legacy / f"empty-{index:02d}.log").touch()
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="bounded empty-file scan")
            with mock.patch.object(retention, "MAX_FAILURE_CAPTURE_SCAN_ENTRIES", 8):
                with self.assertRaisesRegex(retention.RetentionError, "bounded entry-count limit"):
                    retention.fail_adopted(adopted["build_id"], reason="too many empty diagnostics")
        evidence = retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure"
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((evidence / "failure-manifest.json").exists())
        self.assertFalse((evidence / "configuration" / "files").exists())

    def test_bounded_tree_scan_closes_queued_directory_descriptors_on_refusal(self) -> None:
        root = self.repo / "nested-scan-bound"
        root.mkdir(mode=0o700)
        for index in range(8):
            (root / f"child-{index:02d}").mkdir(mode=0o700)
        root_fd = retention._open_dir(root)
        opened_child_fds: list[int] = []
        real_open = os.open

        def tracking_open(*args: object, **kwargs: object) -> int:
            descriptor = real_open(*args, **kwargs)
            opened_child_fds.append(descriptor)
            return descriptor

        try:
            with mock.patch.object(retention.os, "open", side_effect=tracking_open):
                with self.assertRaisesRegex(retention.RetentionError, "bounded entry-count limit"):
                    retention._tree_stats(root_fd, os.fstat(root_fd).st_dev, root,
                                          max_scan_entries=2)
            self.assertGreater(len(opened_child_fds), 0)
            for descriptor in opened_child_fds:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        finally:
            os.close(root_fd)

    def test_legacy_copy_all_failure_capture_bounds_empty_symlink_fanout(self) -> None:
        parent = self.repo / "legacy-symlink-fanout-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-symlink-fanout"
        legacy.mkdir(mode=0o700)
        for index in range(retention.MAX_FAILURE_CAPTURE_FILES + 1):
            (legacy / f"link-{index:02d}").symlink_to("target")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="symlink fanout limit")
            with self.assertRaisesRegex(retention.RetentionError, "bounded evidence entry count"):
                retention.fail_adopted(adopted["build_id"], reason="too many symlink descriptions")
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure" / "failure-manifest.json").exists())

    def test_evidence_copy_rejects_fifo_without_blocking(self) -> None:
        source = self.repo / "failure-fifo"
        os.mkfifo(source, 0o600)
        with self.assertRaisesRegex(retention.RetentionError, "not a regular file"):
            retention._copy_evidence(source, self.repo / "evidence/fifo-copy")

    def test_legacy_artifactization_refuses_missing_dependency_state(self) -> None:
        parent = self.repo / "legacy-complete-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-complete-build"
        legacy.mkdir(mode=0o700)
        (legacy / "synthetic.dll").write_bytes(b"legacy product")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="legacy artifactization provenance test")
            with mock.patch.object(retention, "_capture_identity", return_value=retention._empty_identity()):
                with self.assertRaisesRegex(retention.RetentionError, "lacks dependency-state evidence"):
                    retention.complete_adopted(adopted["build_id"])
        self.assertEqual(retention._load_lease(adopted["build_id"])["state"], "ACTIVE")
        self.assertTrue(Path(adopted["path"]).is_dir())

    def test_legacy_reference_debug_preservation_binds_outputs_and_expires(self) -> None:
        parent = self.repo / "legacy-debug-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-debug-build"
        legacy.mkdir(mode=0o700)
        outputs = {"FGMetalBridge.dll": b"synthetic PE output", "fgmetalbridge.so": b"synthetic Mach-O output"}
        for name, payload in outputs.items():
            (legacy / name).write_bytes(payload)
        (legacy / "bridge_pe.o").write_bytes(b"debug object PE")
        (legacy / "bridge_unix.o").write_bytes(b"debug object Unix")
        context = self.repo / "experiments/storage/legacy-run.json"
        context.write_text(__import__("json").dumps({
            "dll_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in outputs.items()}}),
            encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(
                legacy, kind="reference", owner_experiment="synthetic-test",
                purpose="legacy debug retention validation",
                expected_expiry_unix=time.time() + 3600,
                preserve_debug_reason="Synthetic reference build is retained for one-hour debug validation.")
            with mock.patch.object(retention, "_capture_identity", return_value=retention._empty_identity()):
                result = retention.complete_adopted(adopted["build_id"], preserve_debug=True,
                                                    legacy_debug_context_paths=(context,))
        self.assertEqual(result["status"], "PRESERVE_DEBUG")
        self.assertTrue(Path(adopted["path"]).is_dir())
        manifest, _ = retention._verify_build_manifest(adopted["build_id"])
        self.assertIsNone(manifest["config_key_sha256"])
        self.assertEqual(manifest["configuration_key_provenance"], "legacy-debug-preservation-only")
        self.assertEqual(len(manifest["build_identity"]["legacy_debug_symbols"]), 2)

    def test_child_name_swap_between_stat_and_open_is_refused(self) -> None:
        root = retention.BUILD_ROOT / "child-race"
        root.mkdir(parents=True, mode=0o700)
        child = root / "payload"
        child.write_text("old", encoding="utf-8")
        root_fd = retention._open_dir(root)
        root_device = os.fstat(root_fd).st_dev
        real_stat = os.stat
        swapped = False
        def swap_after_stat(path, *args, **kwargs):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if path == "payload" and kwargs.get("dir_fd") == root_fd and not swapped:
                swapped = True
                os.rename("payload", "old-payload", src_dir_fd=root_fd, dst_dir_fd=root_fd)
                fd = os.open("payload", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
                os.write(fd, b"replacement")
                os.close(fd)
            return result
        try:
            with mock.patch.object(retention.os, "stat", side_effect=swap_after_stat):
                with self.assertRaises(retention.RetentionError):
                    retention._remove_tree_contents(root_fd, root_device, root)
        finally:
            os.close(root_fd)
        self.assertEqual((root / "payload").read_text(encoding="utf-8"), "replacement")
        self.assertEqual((root / "old-payload").read_text(encoding="utf-8"), "old")

    def test_cleanup_failure_is_fatal_and_recoverable(self) -> None:
        lease = self._lease("cleanup-failure")
        lease.reserve()
        self._output(lease)
        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("injected cleanup failure")):
            self._authorize_synthetic_execution(lease)
            with self.assertRaises(retention.RetentionError):
                lease.complete()
        lease._release_lock()
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).exists())
        receipts = retention.reconcile_pending()
        self.assertTrue(receipts)
        self.assertFalse(lease.path.exists())

    def test_cleanup_failure_fails_the_build_command(self) -> None:
        lease = self._lease("cleanup-failure-command")
        command = ["meson", "compile", "-C", "."]

        class CompletedSyntheticBuild:
            returncode = None
            args = command
            pid = os.getpid()

            def wait(self):
                (lease.path / "synthetic.dll").write_bytes(b"command output")
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def communicate(self, input=None, timeout=None):
                return (b"", b"")

        real_popen = retention.subprocess.Popen

        def synthetic_popen(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command[:1] == ["/usr/bin/sandbox-exec"]:
                return CompletedSyntheticBuild()
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("injected cleanup failure")):
            with mock.patch.object(retention.subprocess, "Popen", side_effect=synthetic_popen):
                with self.assertRaises(retention.RetentionError):
                    lease.run(command)
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).is_dir())
        self.assertTrue(retention.reconcile_pending())
        self.assertFalse(lease.path.exists())

    def test_failed_build_closes_supervisor_log_before_liveness_check(self) -> None:
        lease = self._lease("failed-build-closes-log")
        command = ["meson", "compile", "-C", "."]

        class FailedSyntheticBuild:
            pid = os.getpid()

            def wait(self):
                return 41

            def poll(self):
                return 41

        real_popen = retention.subprocess.Popen

        def synthetic_popen(args, *positional, **options):
            if isinstance(args, (list, tuple)) and args[:1] == ["/usr/bin/sandbox-exec"]:
                return FailedSyntheticBuild()
            return real_popen(args, *positional, **options)

        with mock.patch.object(retention, "_require_no_live_process",
                               side_effect=_ORIGINAL_REQUIRE_NO_LIVE_PROCESS):
            with mock.patch.object(retention.subprocess, "Popen", side_effect=synthetic_popen):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    lease.run(command)

        self.assertEqual(raised.exception.returncode, 41)
        self.assertFalse(lease.path.exists())
        failure = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
        self.assertEqual(failure["return_code"], 41)
        self.assertIsNone(failure["error"])

    def test_duplicate_completion_is_idempotent(self) -> None:
        lease = self._lease("duplicate-completion")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        first = lease.complete()
        second = lease.complete()
        self.assertEqual(first["build_manifest_id"], second["build_manifest_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertFalse(lease.path.exists())

    def test_manifest_write_replay_before_lease_transition_is_idempotent(self) -> None:
        lease = self._lease("manifest-write-replay")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
        evidence_fd = retention._open_dir(evidence, create=True)
        os.close(evidence_fd)
        execution_evidence = retention._copy_evidence(
            lease.path / retention.BUILD_EXECUTION_RECEIPT, evidence / "execution/build-execution.json")
        artifacts, identity = retention._copy_tree_artifacts(lease.path, lease.build_id, evidence)
        identity["sandboxed_execution_evidence"] = execution_evidence
        identity["sandboxed_execution_status"] = "verified_successful_seatbelt_run"
        identity["sandboxed_execution_profile_sha256"] = hashlib.sha256(
            retention._sandbox_profile_text(lease.path).encode("utf-8")).hexdigest()
        identity["config_key_sha256"] = retention.configuration_key_from_identity(
            identity, dependency_state=identity["dependency_state"])
        lease_record = retention._load_lease(lease.build_id)
        first, first_digest = retention._build_manifest(
            lease_record, artifacts, identity, evidence,
            provenance_scope="captured during successful build completion")
        second, second_digest = retention._build_manifest(
            lease_record, artifacts, identity, evidence,
            provenance_scope="captured during successful build completion")
        self.assertEqual(second["build_manifest_id"], first["build_manifest_id"])
        self.assertEqual(second_digest, first_digest)
        result = lease.complete()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")

    def test_duplicate_configuration_reuses_verified_canonical_artifacts(self) -> None:
        source_hash = hashlib.sha256(b"same source tree").hexdigest()
        first = self._lease("canonical-first", source_tree_hash=source_hash)
        first.reserve()
        self._output(first)
        self._authorize_synthetic_execution(first)
        first.complete()
        second = self._lease("canonical-second", source_tree_hash=source_hash)
        result = second.run([os.sys.executable, "-c", "raise SystemExit('must not build')"])
        self.assertEqual(result["status"], "REUSED_CANONICAL")
        self.assertFalse(second.path.exists())

    def test_existing_canonical_artifact_is_not_replaced(self) -> None:
        first = self.repo / "first.dll"
        second = self.repo / "second.dll"
        first.write_bytes(b"canonical bytes")
        second.write_bytes(b"canonical bytes")
        original = retention._artifactize(first)
        reused = retention._artifactize(second)
        self.assertEqual(original["sha256"], reused["sha256"])
        canonical = Path(original["canonical_artifact_path"])
        self.assertEqual(canonical.read_bytes(), b"canonical bytes")
        canonical.chmod(0o600)
        canonical.write_bytes(b"conflicting bytes")
        with self.assertRaises(retention.RetentionError):
            retention._artifactize(first)

    def test_artifact_store_cross_filesystem_group_is_rejected_by_verify_and_copy(self) -> None:
        retention.ARTIFACT_ROOT.mkdir(parents=True)
        payload = b"synthetic mounted CAS payload"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir()
        (group / "payload.dll").write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 2, "sha256": digest,
            "canonical_path": str(group / "payload.dll"), "size_bytes": len(payload),
        }), encoding="utf-8")
        group_info = group.stat()
        real_fstatvfs = retention.os.fstatvfs

        def mounted_group_fsid(fd: int):
            result = real_fstatvfs(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (group_info.st_dev, group_info.st_ino):
                return SimpleNamespace(f_fsid=getattr(result, "f_fsid", 0) + 1)
            return result

        row = {"sha256": digest, "canonical_artifact_path": str(group / "payload.dll")}
        source = self.repo / "new-output.dll"
        source.write_bytes(b"new artifact must not be copied into a mounted group")
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        source_group = retention.ARTIFACT_ROOT / source_digest
        source_group.mkdir()
        source_group_info = source_group.stat()

        def mounted_either_group_fsid(fd: int):
            result = real_fstatvfs(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) in {
                    (group_info.st_dev, group_info.st_ino),
                    (source_group_info.st_dev, source_group_info.st_ino)}:
                return SimpleNamespace(f_fsid=getattr(result, "f_fsid", 0) + 1)
            return result

        with mock.patch.object(retention.os, "fstatvfs", side_effect=mounted_either_group_fsid):
            self.assertFalse(retention._verify_artifact(row))
            with self.assertRaisesRegex(retention.RetentionError, "crossed its verified store filesystem"):
                retention._artifactize(source)
        self.assertFalse((source_group / "new-output.dll").exists())

    def test_hardlinked_or_fifo_cas_payloads_are_rejected_without_blocking(self) -> None:
        payload = b"synthetic CAS object"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        object_path = group / "object.dll"
        object_path.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 2, "sha256": digest, "canonical_path": str(object_path),
            "size_bytes": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "mutable-hardlink.dll"
        os.link(object_path, alias)
        row = {"sha256": digest, "canonical_artifact_path": str(object_path)}
        self.assertFalse(retention._verify_artifact(row))
        with self.assertRaisesRegex(retention.RetentionError, "non-legacy canonical artifact is hardlinked"):
            retention.legacy_artifact_hardlink_estimate([digest])
        alias.unlink()
        object_path.unlink()
        os.mkfifo(object_path)
        self.assertFalse(retention._verify_artifact(row))

    def test_legacy_hardlinked_cas_object_is_detached_without_changing_identity(self) -> None:
        payload = b"legacy output shared with historical evidence"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "historical-evidence.dll"
        os.link(canonical, alias)
        before_inode = canonical.stat().st_ino
        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": len(payload),
                                     "link_count": 2,
                                     "copy_required": True, "recovery_required": False,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="synthetic migration test")
        after = canonical.stat()
        self.assertEqual(len(actions), 1)
        self.assertNotEqual(after.st_ino, before_inode)
        self.assertEqual(after.st_nlink, 1)
        self.assertEqual(alias.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertTrue(retention._verify_artifact({
            "sha256": digest, "canonical_artifact_path": str(canonical)}))
        self.assertEqual(retention.detach_legacy_artifact_hardlinks(
            [digest], reason="idempotency check"), [])

    def test_canonical_artifact_inventory_includes_unreferenced_store_objects(self) -> None:
        payload = b"historical run manifest is the only reference"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "historical.bin"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        self.assertEqual(retention.canonical_artifact_digests(), [digest])
        (retention.ARTIFACT_ROOT / "unrecognized-entry").write_text("must fail closed", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "unrecognized entry"):
            retention.canonical_artifact_digests()

    def test_legacy_hardlink_detachment_refuses_mismatched_bytes(self) -> None:
        payload = b"expected canonical bytes"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(b"tampered canonical bytes")
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "preserved-alias.dll"
        os.link(canonical, alias)
        original_inode = canonical.stat().st_ino
        with self.assertRaises(retention.RetentionError):
            retention.detach_legacy_artifact_hardlinks([digest], reason="mismatch test")
        self.assertEqual(canonical.stat().st_ino, original_inode)
        self.assertEqual(alias.read_bytes(), b"tampered canonical bytes")

    def test_legacy_hardlink_detachment_recovers_durable_intent_after_receipt_write_failure(self) -> None:
        payload = b"recover canonical isolation receipt after replacement"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "historical-evidence.dll"
        os.link(canonical, alias)
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        original_write = retention._write_json
        failed = False

        def fail_once(path, record, *, replace=True, sealed=False):
            nonlocal failed
            if (not failed and path == receipt
                    and record.get("result") == "DETACHED_WITH_HASH_PRESERVED"):
                failed = True
                raise OSError("synthetic final receipt write failure")
            return original_write(path, record, replace=replace, sealed=sealed)

        with mock.patch.object(retention, "_write_json", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "synthetic final receipt write failure"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="receipt recovery test")
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(retention._read_json(receipt, sealed=True)["result"], "ISOLATION_PREPARED")
        recovery_estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(recovery_estimate, [{"sha256": digest, "size_bytes": 0,
                                              "link_count": 1, "copy_required": False,
                                              "recovery_required": True,
                                              "canonical_artifact_path": str(canonical)}])
        recovered = retention.detach_legacy_artifact_hardlinks([digest], reason="receipt recovery test")
        self.assertEqual(recovered[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(retention._read_json(receipt, sealed=True)["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_aliases_disappearing_during_prepared_isolation_recovers_by_previous_inode(self) -> None:
        payload = b"prepared detached inode is redundant after aliases disappear"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "vanishing-alias.dll"
        os.link(canonical, alias)
        old = canonical.stat()
        temporary_name = f".{canonical.name}.{('a' * 32)}.detach"
        temporary = group / temporary_name
        temporary.write_bytes(payload)
        isolated = temporary.stat()
        alias.unlink()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_inode": old.st_ino,
            "previous_link_count": 2, "isolated_inode": isolated.st_ino,
            "isolated_link_count": 1, "temporary_device": isolated.st_dev,
            "temporary_inode": isolated.st_ino, "temporary_name": temporary_name,
            "result": "ISOLATION_PREPARED",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 1, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="alias-free recovery")
        self.assertEqual(actions[0]["result"], "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED")
        self.assertEqual(canonical.stat().st_ino, old.st_ino)
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertFalse(temporary.exists())
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_copy_planned_unbound_temp_from_create_progress_crash_is_rebound(self) -> None:
        payload = b"durably recover copy after O_EXCL before COPY_IN_PROGRESS receipt"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "crash-boundary-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('c' * 32)}.detach"
        temporary = group / temporary_name
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(temp_fd, b"partial")
        os.fsync(temp_fd)
        os.close(temp_fd)
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_name": temporary_name, "result": "COPY_PLANNED",
        }, sealed=True)

        estimates = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertTrue(estimates[0]["copy_required"])
        actions = retention.detach_legacy_artifact_hardlinks(
            [digest], reason="recover crash after exclusive temp creation")
        self.assertEqual(actions[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(alias.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_copy_planned_unbound_temp_with_disappeared_alias_preflights_zero_copy(self) -> None:
        payload = b"aliases may disappear after planned temp creation"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "vanishing-copy-planned-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('d' * 32)}.detach"
        temporary = group / temporary_name
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(temp_fd, b"partial")
        os.fsync(temp_fd)
        os.close(temp_fd)
        alias.unlink()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_name": temporary_name, "result": "COPY_PLANNED",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 1, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks(
            [digest], reason="recover alias-free copy-planned temp")
        self.assertEqual(actions[0]["result"], "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED")
        self.assertFalse(temporary.exists())
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_complete_in_progress_temp_reduces_preflight_copy_requirement_to_zero(self) -> None:
        payload = b"reuse complete durable copy after interrupted CAS isolation"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "complete-copy-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('d' * 32)}.detach"
        temporary = group / temporary_name
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        isolated = temporary.stat()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_device": isolated.st_dev, "temporary_inode": isolated.st_ino,
            "temporary_name": temporary_name, "result": "COPY_IN_PROGRESS",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 2, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="reuse completed durable copy")
        self.assertEqual(actions[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.stat().st_nlink, 1)

    def test_post_exchange_alias_mutation_does_not_block_receipted_recovery(self) -> None:
        payload = b"canonical bytes before legacy alias changes"
        mutated_alias = b"external legacy alias mutation remains intact"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "mutable-legacy-alias.dll"
        os.link(canonical, alias)
        real_unlink = retention._unlink_pinned_regular_at
        interrupted = False

        def mutate_then_interrupt(directory_fd: int, name: str, expected: os.stat_result) -> None:
            nonlocal interrupted
            if not interrupted and retention._detach_temp_name_is_safe(canonical.name, name):
                interrupted = True
                alias.write_bytes(mutated_alias)
                raise OSError("synthetic process death after swap and alias mutation")
            real_unlink(directory_fd, name, expected)

        with mock.patch.object(retention, "_unlink_pinned_regular_at", side_effect=mutate_then_interrupt):
            with self.assertRaisesRegex(OSError, "after swap and alias mutation"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="alias mutation recovery")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.read_bytes(), mutated_alias)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate[0]["recovery_required"], True)
        recovered = retention.detach_legacy_artifact_hardlinks([digest], reason="alias mutation recovery")
        self.assertEqual(recovered[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.read_bytes(), mutated_alias)

    def test_failed_atomic_exchange_preserves_known_isolated_temporary_for_recovery(self) -> None:
        payload = b"isolated temp survives a failed exchange verification"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "exchange-alias.dll"
        os.link(canonical, alias)
        real_swap = retention._rename_swap_at
        calls = 0

        def swap_twice(directory_fd: int, left: str, right: str) -> None:
            nonlocal calls
            real_swap(directory_fd, left, right)
            calls += 1
            if calls == 1:
                # Return to the pre-exchange names before the caller's identity
                # check to model a namespace change during the exchange window.
                real_swap(directory_fd, left, right)

        with mock.patch.object(retention, "_rename_swap_at", side_effect=swap_twice):
            with self.assertRaisesRegex(retention.RetentionError, "identity changed during atomic exchange"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="exchange race test")
        receipt = retention._read_json(
            retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json", sealed=True)
        temporary = group / receipt["temporary_name"]
        self.assertTrue(temporary.is_file())
        self.assertEqual((temporary.stat().st_dev, temporary.stat().st_ino),
                         (receipt["temporary_device"], receipt["isolated_inode"]))
        self.assertEqual(hashlib.sha256(temporary.read_bytes()).hexdigest(), digest)
        self.assertEqual(canonical.stat().st_nlink, 2)
        self.assertTrue(alias.exists())

    def test_replaced_receipted_temp_is_refused_and_left_untouched(self) -> None:
        payload = b"expected CAS source"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "temp-replacement-alias.dll"
        os.link(canonical, alias)
        old = canonical.stat()
        temporary_name = f".{canonical.name}.{('b' * 32)}.detach"
        temporary = group / temporary_name
        old_temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(old_temp_fd, payload)
            old_temp = os.fstat(old_temp_fd)
            receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
            retention._write_json(receipt, {
                "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
                "sha256": digest, "canonical_artifact_path": str(canonical),
                "size_bytes": len(payload), "previous_inode": old.st_ino,
                "previous_link_count": 2, "temporary_device": old_temp.st_dev,
                "temporary_inode": old_temp.st_ino, "temporary_name": temporary_name,
                "result": "COPY_IN_PROGRESS",
            }, sealed=True)
            temporary.unlink()
            temporary.write_bytes(b"foreign replacement")
            foreign = temporary.stat()
            self.assertNotEqual((foreign.st_dev, foreign.st_ino),
                                (old_temp.st_dev, old_temp.st_ino))
            with self.assertRaisesRegex(retention.RetentionError, "temporary lacks a durable inode binding"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="replacement test")
            self.assertEqual(temporary.read_bytes(), b"foreign replacement")
            self.assertEqual(canonical.stat().st_nlink, 2)
        finally:
            os.close(old_temp_fd)

    def test_unmanifested_fifo_cas_payload_is_rejected_without_blocking(self) -> None:
        source = self.repo / "fifo-output.dll"
        source.write_bytes(b"canonical copy candidate")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        os.mkfifo(group / source.name)
        with self.assertRaises(retention.RetentionError):
            retention._artifactize(source)

    def test_fifo_artifact_manifest_is_rejected_without_blocking(self) -> None:
        group = retention.ARTIFACT_ROOT / ("a" * 64)
        group.mkdir(parents=True)
        os.mkfifo(group / "manifest.json")
        group_fd = retention._open_dir(group)
        try:
            with self.assertRaisesRegex(retention.RetentionError, "regular file"):
                retention._read_at(group_fd, "manifest.json")
        finally:
            os.close(group_fd)

    def test_wine_prefix_signature_scan_refuses_a_replaced_root(self) -> None:
        root = self.repo / "replace-during-prefix-classification"
        root.mkdir()
        (root / "system.reg").write_text("wine registry", encoding="utf-8")
        (root / "drive_c").mkdir()
        (root / "dosdevices").mkdir()
        displaced = root.with_name(root.name + ".original")
        open_directory = retention._open_dir

        def open_then_replace(path, *, create=False):
            descriptor = open_directory(path, create=create)
            os.rename(root, displaced)
            root.mkdir()
            (root / "build.ninja").write_text("replacement build", encoding="utf-8")
            return descriptor

        with mock.patch.object(retention, "_open_dir", side_effect=open_then_replace):
            with self.assertRaisesRegex(retention.RetentionError, "replaced while classifying Wine prefix"):
                retention._is_wine_prefix_tree(root)
        self.assertTrue((displaced / "system.reg").is_file())
        self.assertTrue((root / "build.ninja").is_file())

    def test_mount_discovery_rejects_failed_hdiutil_enumeration(self) -> None:
        mount = self.cache / "readonly-mount"
        mount.mkdir()
        failed = mock.Mock(returncode=1, stdout=b"partial mount data")
        with mock.patch.object(retention.os.path, "ismount", return_value=True), \
                mock.patch.object(retention.os, "statvfs",
                                  return_value=SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1))), \
                mock.patch.object(retention.subprocess, "run", return_value=failed):
            self.assertFalse(retention._known_readonly_cache_image_mount(mount, self.cache))

    def test_mount_discovery_accepts_only_exact_plist_cache_binding(self) -> None:
        mount = self.cache / "readonly-mount"
        mount.mkdir()
        image = self.cache / "source.dmg"
        image.write_bytes(b"synthetic disk image")
        metadata = {"images": [{
            "image-path": str(image),
            "writeable": False,
            "system-entities": [{"mount-point": str(mount)}],
        }]}
        result = mock.Mock(returncode=0, stdout=plistlib.dumps(metadata))
        with mock.patch.object(retention.os.path, "ismount", return_value=True), \
                mock.patch.object(retention.os, "statvfs",
                                  return_value=SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1))), \
                mock.patch.object(retention.subprocess, "run", return_value=result) as run:
            self.assertTrue(retention._known_readonly_cache_image_mount(mount, self.cache))
        self.assertEqual(run.call_args.args[0], ["hdiutil", "info", "-plist"])

    def test_inventory_refuses_external_build_root_symlink_alias(self) -> None:
        target = Path(self.temp.name) / "outside-inventory-build"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic external build", encoding="utf-8")
        alias = self.repo / "out"
        alias.symlink_to(target, target_is_directory=True)
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", self.cache), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", Path(self.temp.name)), \
                mock.patch.object(retention, "_all_root_candidates", return_value=[alias]):
            with self.assertRaisesRegex(RuntimeError, "cannot account for unresolved build-root alias"):
                storage_inventory.discover_roots()

    def test_signed_receipt_from_another_build_does_not_attest_retirement(self) -> None:
        first = self._lease("receipt-binding-a")
        first.reserve()
        self._output(first)
        self._authorize_synthetic_execution(first)
        first.complete()
        second = self._lease("receipt-binding-b", source_tree_hash=hashlib.sha256(b"receipt other source").hexdigest())
        second.reserve()
        self._output(second)
        self._authorize_synthetic_execution(second)
        second.complete()
        lease = retention._load_lease(first.build_id)
        other_receipt = retention.RECEIPT_ROOT / f"{second.build_id}.json"
        lease["cleanup_receipt_path"] = str(other_receipt)
        retention._store_lease(lease)
        snapshot = retention.status_snapshot()
        self.assertEqual(snapshot["gate_status"], "BLOCKED")
        self.assertTrue(any("canonical receipt path" in row["reason"] for row in snapshot["unknown"]))

    def test_unleased_dxvk_cache_root_is_discovered_as_a_cap_blocker(self) -> None:
        root = Path(self.temp.name) / "dxvk-cache"
        root.mkdir()
        (root / "build.ninja").write_text("synthetic", encoding="utf-8")
        self.assertTrue(retention._has_build_signature(root))
        with mock.patch.object(retention, "_all_root_candidates", return_value=[root]):
            blockers = retention.capacity_blockers()
        self.assertIn("ambiguous or unleased build roots require reconciliation", blockers)

    def test_unbranded_ninja_build_root_is_discovered_by_real_scan(self) -> None:
        root = retention.TEMP_ROOT / "ninja-build-unbranded"
        root.mkdir()
        (root / "build.ninja").write_text("# synthetic ninja tree\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_nested_unbranded_temporary_build_root_is_discovered(self) -> None:
        root = retention.TEMP_ROOT / "scratch" / "project" / "out" / "build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# nested synthetic build\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_unleased_nested_build_in_step11d_cache_blocks_the_cap(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic cache build root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_nested_build_in_every_project_cache_generation_blocks_the_cap(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic non-R cache build root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_noncanonical_artifacts_directory_is_scanned_for_builds(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/artifacts/old-copy/build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# not the canonical content-addressed store\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_unbranded_cache_symlink_is_reported_without_following(self) -> None:
        cache_root = retention.HOME / "Library/Caches/FGMetalStep11D-R"
        (cache_root / "prefix-cache").mkdir(parents=True)
        target = Path(self.temp.name) / "external-build-root"
        target.mkdir()
        (target / "build.ninja").write_text("# external synthetic build\n", encoding="utf-8")
        alias = cache_root / "prefix-cache" / "out"
        alias.symlink_to(target, target_is_directory=True)
        candidates = retention._all_root_candidates()
        self.assertIn(alias, candidates)
        state = retention._snapshot()
        self.assertTrue(any(row.get("path") == str(alias) for row in state["unknown"]))

    def test_external_temp_symlink_named_out_is_a_blocker(self) -> None:
        target = Path(self.temp.name) / "outside-build-tree"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic", encoding="utf-8")
        alias = retention.TEMP_ROOT / "scratch" / "prefix-cache" / "out"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target, target_is_directory=True)
        self.assertIn(alias, retention._all_root_candidates())

    def test_external_worktree_symlink_named_out_is_a_blocker(self) -> None:
        target = Path(self.temp.name) / "outside-worktree-build"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic", encoding="utf-8")
        alias = self.repo / "out"
        alias.symlink_to(target, target_is_directory=True)
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(retention.subprocess, "run", return_value=listing):
            self.assertIn(alias, retention._worktree_build_root_candidates())

    def test_queued_cache_mount_replacement_fails_closed(self) -> None:
        mount = retention.HOME / "Library/Caches/FGMetalStep11D-R/mount"
        mount.mkdir(parents=True)
        expected = retention._verified_cache_scan_root(mount)
        old = mount.with_name("mount-old")
        mount.rename(old)
        mount.mkdir()
        with self.assertRaisesRegex(retention.RetentionError, "identity changed after discovery"):
            retention._verified_cache_scan_root(mount, expected)

    def test_lsfg_scoped_cache_build_is_not_omitted_from_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/lsfg-metal/cache-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic scoped cache build\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_unlinked_mounted_image_length_is_read_from_exact_lsof_vnode(self) -> None:
        path = "/Users/nima/Library/Caches/FGMetalStep11C1R/MetalSharp.dmg"
        result = mock.Mock(returncode=0,
                           stdout=("p51186\ncdiskimages-helper\nftxt\ns77412\n"
                                   "/private/var/db/diagnostics/logd/cache.plist\n"
                                   "f3\ns1436105885\nn" + path + "\n"))
        with mock.patch.object(storage_inventory.subprocess, "run", return_value=result) as run:
            self.assertEqual(storage_inventory._unlinked_image_length(
                {"pid": 51186, "image_path": path}), 1436105885)
        self.assertEqual(run.call_args.args[0],
                         ["lsof", "-nP", "-a", "-p", "51186", "+L1", "-Fpcfsn"])

    def test_unlinked_mounted_image_without_matching_vnode_has_no_estimate(self) -> None:
        result = mock.Mock(returncode=0, stdout="p51186\nfdiskimages-helper\ns9\nnot-the-image\n")
        with mock.patch.object(storage_inventory.subprocess, "run", return_value=result):
            self.assertIsNone(storage_inventory._unlinked_image_length(
                {"pid": 51186, "image_path": "/missing/image.dmg"}))

    def test_non_r_cache_build_is_listed_in_storage_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11C1R/project/out/build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic legacy cache build root\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_nested_step11d_cache_build_is_listed_in_storage_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic cache build root\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_nested_temp_build_root_is_in_storage_inventory_scope(self) -> None:
        root = retention.TEMP_ROOT / "scratch" / "project" / "out" / "build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# nested synthetic build\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", self.cache.parent), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "TEMP_BUILD_ROOT"), roots)

    def test_registered_worktree_symlink_fails_closed(self) -> None:
        registered = self.repo / "registered-worktree"
        registered.symlink_to(self.cache, target_is_directory=True)
        result = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\nworktree {registered}\n", stderr="")
        with mock.patch.object(retention.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(retention.RetentionError, "registered Git worktree path is a symlink"):
                retention._registered_worktree_roots()

    def test_completion_requires_successful_sandboxed_execution_receipt(self) -> None:
        lease = self._lease("completion-requires-run-proof")
        lease.reserve()
        self._output(lease)
        with self.assertRaisesRegex(retention.RetentionError, "successful sandboxed build execution evidence"):
            lease.complete()
        lease._release_lock()
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "ACTIVE")
        lease.fail(command=["synthetic"], return_code=1, error="completion correctly refused")

    def test_gate0_finalize_command_dispatches_final_mode(self) -> None:
        report = self.repo / "prefix-smoke-report.json"
        with mock.patch.object(storage_gate0, "_refresh", return_value={"status": "PASS"}) as refresh, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(storage_gate0.main(["finalize", "--smoke-report", str(report)]), 0)
        refresh.assert_called_once_with("final", report)

    def test_repository_worktree_build_root_is_discovered(self) -> None:
        root = self.repo / "experiments/dxvk-synthetic/work-output"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic worktree build root\n", encoding="utf-8")
        with mock.patch.object(retention, "_registered_worktree_roots", return_value=[self.repo]):
            self.assertIn(root, retention._worktree_build_root_candidates())

    def test_worktree_scan_fails_closed_on_traversal_error(self) -> None:
        def failing_walk(path, *, followlinks, onerror):
            onerror(PermissionError("synthetic unreadable directory"))
            return iter(())

        with mock.patch.object(retention, "_registered_worktree_roots", return_value=[self.repo]), \
                mock.patch.object(retention.os, "walk", side_effect=failing_walk):
            with self.assertRaisesRegex(retention.RetentionError, "cannot completely scan"):
                retention._worktree_build_root_candidates()

    def test_step10b6_1_name_class_is_not_a_broad_build_scan_exclusion(self) -> None:
        root = retention.TEMP_ROOT / "step10b6_1-anything"
        root.mkdir()
        (root / "build.ninja").write_text("# synthetic hidden root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_build_tool_rejects_external_makefiles_install_modes_and_wrappers(self) -> None:
        root = self.cache / "builds" / "unsafe-command-check"
        root.mkdir(parents=True)
        source = self.repo / "source"
        source.mkdir()
        with self.assertRaisesRegex(retention.RetentionError, "build tool makefile"):
            retention._validate_build_command_paths(["make", "-C", str(root), "-f", "/private/tmp/Makefile"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "install targets"):
            retention._validate_build_command_paths(["ninja", "install"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "unsupported build executable"):
            retention._validate_build_command_paths([os.sys.executable, "-c", "pass"], root, root)

    def test_sandbox_build_command_confines_synthetic_writes(self) -> None:
        root = self.cache / "builds" / "sandbox-confinement"
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside-build-write"
        if os.sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            with self.assertRaisesRegex(retention.RetentionError, "requires macOS Seatbelt"):
                retention._sandbox_build_command(["/usr/bin/touch", str(root / "inside")], root)
            return
        inside_args, child_env = retention._sandbox_build_command(
            ["/usr/bin/touch", str(root / "inside")], root)
        inside = retention.subprocess.run(inside_args, env=child_env, stdout=retention.subprocess.PIPE,
                                          stderr=retention.subprocess.PIPE, check=False)
        self.assertEqual(inside.returncode, 0, inside.stderr.decode("utf-8", errors="replace"))
        self.assertTrue((root / "inside").is_file())
        outside_args, child_env = retention._sandbox_build_command(
            ["/usr/bin/touch", str(outside)], root)
        outside_result = retention.subprocess.run(outside_args, env=child_env,
                                                  stdout=retention.subprocess.PIPE,
                                                  stderr=retention.subprocess.PIPE, check=False)
        self.assertNotEqual(outside_result.returncode, 0)
        self.assertFalse(outside.exists())

    def test_descriptor_bound_removal_refuses_replacement_and_unlinks_symlink_only(self) -> None:
        parent = self.cache / "removal-root"
        parent.mkdir()
        target = parent / "target"
        target.mkdir()
        (target / "payload").write_text("original", encoding="utf-8")
        outside = self.cache / "outside-sentinel"
        outside.write_text("keep", encoding="utf-8")
        (target / "escape").symlink_to(outside)
        parent_fd = retention._open_dir(parent)
        try:
            original = target.stat()
            expected = (original.st_dev, original.st_ino)
            moved = parent / "original-moved"
            target.rename(moved)
            target.mkdir()
            (target / "payload").write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "replaced directory"):
                storage_policy._remove_child_directory_nofollow(parent_fd, "target", expected_identity=expected)
            self.assertEqual((target / "payload").read_text(encoding="utf-8"), "replacement")
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            target.rename(parent / "safe")
            safe_info = (parent / "safe").stat()
            storage_policy._remove_child_directory_nofollow(
                parent_fd, "safe", expected_identity=(safe_info.st_dev, safe_info.st_ino))
            self.assertFalse((parent / "safe").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        finally:
            os.close(parent_fd)

    def test_build_tool_output_directory_must_stay_in_lease(self) -> None:
        root = self.cache / "builds" / "output-argument-check"
        root.mkdir(parents=True)
        source = self.repo / "source"
        source.mkdir()
        retention._validate_build_command_paths(["meson", "setup", ".", str(source)], root, root)
        retention._validate_build_command_paths(["cmake", "-S", str(source), "-B", "."], root, root)
        retention._validate_build_command_paths(["ninja", "-C", str(root)], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "Meson build directory"):
            retention._validate_build_command_paths(["meson", "setup", "../outside", str(source)], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "CMake build directory"):
            retention._validate_build_command_paths(["cmake", "-S", str(source), "-B", "/private/tmp/outside"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "build tool directory"):
            retention._validate_build_command_paths(["ninja", "-C", "../../outside"], root, root)

    def test_historical_source_evidence_corpus_is_explicit_and_fingerprinted(self) -> None:
        corpus = retention.TEMP_ROOT / "step11d2-diagnostic-corpus"
        (corpus / "source").mkdir(parents=True)
        (corpus / "evidence").mkdir()
        (corpus / "build" / "variant-a").mkdir(parents=True)
        (corpus / "REPORT.md").write_text("historical report\n", encoding="utf-8")
        (corpus / "HASHES.json").write_text("{}\n", encoding="utf-8")
        (corpus / "source/bridge_protocol.h").write_text("protocol\n", encoding="utf-8")
        (corpus / "evidence/run.log").write_text("run evidence\n", encoding="utf-8")
        build_log = corpus / "build/variant-a/build.log"
        build_log.write_text("historical build output\n", encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_STEP11D2_EVIDENCE_CORPUS", corpus), \
                mock.patch.object(retention, "LEGACY_STEP11D2_EVIDENCE_CORPORA", {corpus}), \
                mock.patch.object(retention, "LEGACY_STEP11D2_REPORT_SHA256",
                                  hashlib.sha256((corpus / "REPORT.md").read_bytes()).hexdigest()), \
                mock.patch.object(retention, "LEGACY_STEP11D2_HASHES_SHA256",
                                  hashlib.sha256((corpus / "HASHES.json").read_bytes()).hexdigest()), \
                mock.patch.object(retention, "_legacy_step11d2_evidence_symlink_roots", return_value=[]):
            first = next(row for row in retention._classified_non_retention_tmp_roots()
                         if row["path"] == str(corpus))
            self.assertEqual(first["classification"], "HISTORICAL-SOURCE-AND-EVIDENCE-CORPUS")
            self.assertEqual(first["nested_build_directory_names"], ["variant-a"])
            self.assertNotIn(corpus, retention._all_root_candidates())
            initial_hash = first["nested_build_content_tree_sha256"]
            build_log.write_text("changed historical output\n", encoding="utf-8")
            second = next(row for row in retention._classified_non_retention_tmp_roots()
                          if row["path"] == str(corpus))
        self.assertNotEqual(initial_hash, second["nested_build_content_tree_sha256"])

    def test_generated_spirv_tables_are_classified_as_source_not_build_output(self) -> None:
        dependency = Path(self.temp.name) / "SPIRV-Tools"
        source = dependency / "source"
        tables = dependency / "build"
        source.mkdir(parents=True)
        tables.mkdir()
        (dependency / "CMakeLists.txt").write_text("# dependency source\n", encoding="utf-8")
        for name in retention.SPIRV_GENERATED_TABLE_FILES:
            (tables / name).write_text(f"// {name}\n", encoding="utf-8")
        with mock.patch.object(retention, "SPIRV_GENERATED_TABLE_ROOT", tables):
            self.assertTrue(retention._is_spirv_generated_table_root(tables))
            self.assertFalse(retention._has_build_signature(tables))

    def test_evidence_mutation_refuses_retirement(self) -> None:
        lease = self._lease("evidence-mutation")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
        (evidence / "attacker.txt").write_text("changed", encoding="utf-8")
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def _native_request(self, **overrides) -> retention.NativeMoltenVKBuildRequest:
        developer_dir = self.repo / "Synthetic Xcode/Contents/Developer"
        xcodebuild = developer_dir / "usr/bin/xcodebuild"
        xcodebuild.parent.mkdir(parents=True, exist_ok=True)
        xcodebuild.write_text("synthetic xcodebuild binary\n", encoding="utf-8")
        defaults = {
            "pristine_git_commit": "db66022459ffb663aa2b50f6b018bc2e124f5edf",
            "pristine_git_tree": "efebd918bbe772eead074100c0a18789ad39606d",
            "source_tree_sha256": hashlib.sha256(b"pristine tree").hexdigest(),
            "external_revisions": {"SPIRV-Tools": "abc1234"},
            "vulkan_headers_revision": "e3b1eec",
            "vulkan_headers_tree_sha256": hashlib.sha256(b"vulkan headers").hexdigest(),
            "patch_sha256": hashlib.sha256(b"ring buffer patch").hexdigest(),
            "patch_size_bytes": 12345,
            "modified_files": (("MoltenVK/GPUObjects/MVKImage.mm", "1111", "2222"),),
            "build_system": "xcode_native",
            "project_relative_path": "MoltenVKPackaging.xcodeproj",
            "project_file_sha256": hashlib.sha256(b"project file").hexdigest(),
            "target_or_scheme": "MoltenVK-macOS-dylib",
            "configuration": "Release",
            "architectures": ("x86_64",),
            "deployment_target": "12.0",
            "sdk_name": "macosx",
            "xcode_version_string": "Xcode 16.0",
            "clang_version_string": "Apple Clang 16.0.0",
            "clang_binary_sha256": hashlib.sha256(b"clang binary").hexdigest(),
            "sdk_version_string": "15.0",
            "linker_version_string": "ld64-1115.7.3",
            "toolchain_identity_sha256": retention.native_toolchain_identity_sha256(
                xcode_version_string="Xcode 16.0",
                clang_version_string="Apple Clang 16.0.0",
                clang_binary_sha256=hashlib.sha256(b"clang binary").hexdigest(),
                sdk_version_string="15.0",
                linker_version_string="ld64-1115.7.3",
            ),
            "normalized_environment": {"DEVELOPER_DIR": str(developer_dir)},
            "normalized_arguments": (str(xcodebuild), "-project", "MoltenVKPackaging.xcodeproj",
                                     "-scheme", "MoltenVK-macOS-dylib", "-configuration", "Release",
                                     "-derivedDataPath", ".", "ARCHS=x86_64"),
            "declared_outputs": (
                retention.DeclaredOutputRole(
                    role="moltenvk_dylib",
                    relative_path="Build/Products/Release/libMoltenVK.dylib",
                    artifact_name="libMoltenVK.dylib",
                    mandatory=True,
                    is_macho=True,
                    expected_architectures=("x86_64",),
                ),
            ),
        }
        defaults.update(overrides)
        return retention.NativeMoltenVKBuildRequest(**defaults)

    @staticmethod
    def _macho_dylib_bytes() -> bytes:
        import struct
        return struct.pack("<4I", 0xfeedfacf, 0x01000007, 0, 6) + b"\x00" * 16

    @staticmethod
    def _arm64_macho_dylib_bytes() -> bytes:
        import struct
        return struct.pack("<4I", 0xfeedfacf, 0x0100000c, 0, 6) + b"\x00" * 16

    def _write_execution_receipt(self, lease: retention.BuildLease) -> None:
        self._authorize_synthetic_execution(lease)

    def _native_lease(self, build_id: str, request: retention.NativeMoltenVKBuildRequest) -> retention.BuildLease:
        config_hash = retention.compute_native_moltenvk_config_key(request)
        dep_state = {"status": "captured", "lock": "native moltenvk"}
        metadata = {
            "build_type": request.configuration.casefold(),
            "dependency_state": dep_state,
            "native_moltenvk_request": retention._native_request_to_dict(request),
            "failure_capture_paths": [],
        }
        self._write_owner_manifest(
            source_hash=request.source_tree_sha256,
            config_hash=config_hash,
            build_kind="moltenvk",
            build_type=request.configuration.casefold(),
            dependency_state=dep_state,
        )
        return retention.BuildLease(
            build_id,
            kind="moltenvk",
            owner_experiment="synthetic-test",
            purpose="test native moltenvk build",
            source_tree_hash=request.source_tree_sha256,
            expected_bytes=64 * 1024 ** 2,
            metadata=metadata,
            runner_path=self.runner_file,
            configuration_key_sha256=config_hash,
        )

    def test_native_moltenvk_config_key_invariance_to_generated_metadata(self) -> None:
        req = self._native_request()
        key1 = retention.compute_native_moltenvk_config_key(req)
        # Recomputing key strictly depends on declared inputs
        key2 = retention.compute_native_moltenvk_config_key(req)
        self.assertEqual(key1, key2)

    def _prepared_source_fixture(self) -> tuple[Path, retention.NativeMoltenVKBuildRequest]:
        build_root = Path(self.temp.name) / f"native-source-build-{time.time_ns()}"
        source = build_root / "MoltenVKSource"
        source.mkdir(parents=True)
        git_env = dict(os.environ)
        git_env.update({"GIT_AUTHOR_NAME": "Synthetic Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                        "GIT_COMMITTER_NAME": "Synthetic Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})

        def run_git(directory: Path, *arguments: str) -> str:
            result = subprocess.run(["/usr/bin/git", "-C", str(directory), *arguments], env=git_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
            return result.stdout.decode("utf-8").strip()

        run_git(source, "init", "-q")
        (source / ".gitignore").write_text("External/\n", encoding="utf-8")
        (source / "tracked.txt").write_text("pristine source\n", encoding="utf-8")
        project_file = source / "MoltenVK.xcodeproj" / "project.pbxproj"
        project_file.parent.mkdir()
        project_file.write_text("pristine project\n", encoding="utf-8")
        run_git(source, "add", ".gitignore", "tracked.txt", "MoltenVK.xcodeproj/project.pbxproj")
        run_git(source, "commit", "-q", "-m", "pristine")
        commit = run_git(source, "rev-parse", "HEAD")
        tree = run_git(source, "rev-parse", "HEAD^{tree}")

        external_names = ("SPIRV-Cross", "SPIRV-Tools", "Volk", "Vulkan-Headers", "Vulkan-Tools", "cereal")
        external_revisions: dict[str, str] = {}
        for name in external_names:
            dependency = source / "External" / name
            dependency.mkdir(parents=True)
            run_git(dependency, "init", "-q")
            (dependency / "source.txt").write_text(name + "\n", encoding="utf-8")
            run_git(dependency, "add", "source.txt")
            run_git(dependency, "commit", "-q", "-m", "dependency")
            external_revisions[name] = run_git(dependency, "rev-parse", "HEAD")

        pristine = (source / "tracked.txt").read_bytes()
        (source / "tracked.txt").write_text("prepared source\n", encoding="utf-8")
        project_pristine = project_file.read_bytes()
        project_file.write_text("prepared project\n", encoding="utf-8")
        added = source / "added.txt"
        added.write_text("new patch file\n", encoding="utf-8")
        modified_files = (
            ("added.txt", "ABSENT", hashlib.sha256(added.read_bytes()).hexdigest()),
            ("MoltenVK.xcodeproj/project.pbxproj", hashlib.sha256(project_pristine).hexdigest(),
             hashlib.sha256(project_file.read_bytes()).hexdigest()),
            ("tracked.txt", hashlib.sha256(pristine).hexdigest(), hashlib.sha256((source / "tracked.txt").read_bytes()).hexdigest()),
        )
        headers_tree, _ = retention._tree_hash(source / "External" / "Vulkan-Headers")
        request = self._native_request(
            pristine_git_commit=commit,
            pristine_git_tree=tree,
            prepared_source_root_relative_path="MoltenVKSource",
            external_revisions=external_revisions,
            vulkan_headers_revision=external_revisions["Vulkan-Headers"],
            vulkan_headers_tree_sha256=headers_tree,
            project_file_sha256=hashlib.sha256(project_file.read_bytes()).hexdigest(),
            modified_files=modified_files,
            project_relative_path="MoltenVKSource/MoltenVK.xcodeproj",
        )
        return build_root, request

    def test_native_prepared_source_verification_binds_pins_patch_and_clean_status(self) -> None:
        build_root, request = self._prepared_source_fixture()
        result = retention._verify_native_prepared_source(build_root, request)
        self.assertEqual(result["status"], "verified_prepared_source")
        self.assertEqual(result["pristine_git_commit"], request.pristine_git_commit)
        self.assertEqual(result["external_revisions"], dict(request.external_revisions))
        self.assertEqual(len(result["modified_files"]), 3)

    def test_native_prepared_source_verification_rejects_mutation_and_extra_files(self) -> None:
        build_root, request = self._prepared_source_fixture()
        target = build_root / "MoltenVKSource" / "tracked.txt"
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "patched source hash mismatch"):
            retention._verify_native_prepared_source(build_root, request)

        build_root, request = self._prepared_source_fixture()
        (build_root / "MoltenVKSource" / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "unexpected changes after build"):
            retention._verify_native_prepared_source(build_root, request)

    def test_native_moltenvk_altered_toolchain_changes_key(self) -> None:
        req1 = self._native_request(clang_version_string="Apple Clang 16.0.0")
        changed_clang = "Apple Clang 16.0.1"
        req2 = self._native_request(
            clang_version_string=changed_clang,
            toolchain_identity_sha256=retention.native_toolchain_identity_sha256(
                xcode_version_string="Xcode 16.0",
                clang_version_string=changed_clang,
                clang_binary_sha256=hashlib.sha256(b"clang binary").hexdigest(),
                sdk_version_string="15.0",
                linker_version_string="ld64-1115.7.3",
            ),
        )
        key1 = retention.compute_native_moltenvk_config_key(req1)
        key2 = retention.compute_native_moltenvk_config_key(req2)
        self.assertNotEqual(key1, key2)

    def test_native_moltenvk_altered_patch_hash_fails_reservation(self) -> None:
        req = self._native_request(patch_sha256=hashlib.sha256(b"altered patch").hexdigest())
        lease = self._native_lease("native-altered-patch", req)
        # Manifest has tampered key so manifest passes but native request validation fails
        self._write_owner_manifest(
            source_hash=req.source_tree_sha256,
            config_hash="0" * 64,
            build_kind="moltenvk",
            build_type=req.configuration.casefold(),
            dependency_state=lease.metadata["dependency_state"],
        )
        lease.configuration_key_sha256 = "0" * 64
        with self.assertRaisesRegex(retention.RetentionError, "configuration_key_sha256 does not match"):
            lease.reserve()

    def test_native_moltenvk_altered_source_tree_fails_reservation(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-altered-source", req)
        lease.source_tree_hash = "f" * 64
        with self.assertRaisesRegex(retention.RetentionError, "BuildLease request differs"):
            lease.reserve()

    def test_native_moltenvk_altered_build_args_changes_key(self) -> None:
        req1 = self._native_request(normalized_arguments=("xcodebuild", "-configuration", "Release"))
        req2 = self._native_request(normalized_arguments=("xcodebuild", "-configuration", "Debug"))
        key1 = retention.compute_native_moltenvk_config_key(req1)
        key2 = retention.compute_native_moltenvk_config_key(req2)
        self.assertNotEqual(key1, key2)

    def test_native_moltenvk_execution_must_match_signed_command_environment_and_toolchain(self) -> None:
        request = self._native_request()
        build_root = self.cache / "builds/native-command-binding"
        build_root.mkdir(parents=True)
        observed = {
            "xcode_version_string": request.xcode_version_string,
            "clang_version_string": request.clang_version_string,
            "clang_binary_sha256": request.clang_binary_sha256,
            "sdk_version_string": request.sdk_version_string,
            "linker_version_string": request.linker_version_string,
            "toolchain_identity_sha256": request.toolchain_identity_sha256,
        }
        self.assertEqual(
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                build_root, build_root, observed_toolchain=observed),
            observed)
        with self.assertRaisesRegex(retention.RetentionError, "command differs"):
            retention._validate_native_build_execution_binding(
                request, [*request.normalized_arguments, "-quiet"], request.normalized_environment,
                build_root, build_root, observed_toolchain=observed)
        with self.assertRaisesRegex(retention.RetentionError, "environment differs"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments),
                {**request.normalized_environment, "DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib"},
                build_root, build_root, observed_toolchain=observed)
        changed_toolchain = {**observed, "clang_binary_sha256": "0" * 64}
        with self.assertRaisesRegex(retention.RetentionError, "toolchain differs.*clang_binary_sha256"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                build_root, build_root, observed_toolchain=changed_toolchain)
        with self.assertRaisesRegex(retention.RetentionError, "cwd must be the leased build root"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                self.repo, build_root, observed_toolchain=observed)

    def test_native_moltenvk_architecture_changes_key(self) -> None:
        request_x86 = self._native_request()
        request_arm = self._native_request(
            architectures=("arm64",),
            declared_outputs=(dataclasses.replace(
                request_x86.declared_outputs[0], expected_architectures=("arm64",)),))
        self.assertNotEqual(retention.compute_native_moltenvk_config_key(request_x86),
                            retention.compute_native_moltenvk_config_key(request_arm))

    def test_native_moltenvk_rejects_unknown_or_multiple_architectures(self) -> None:
        for architectures in [("universal",), ("x86_64", "arm64")]:
            with self.subTest(architectures=architectures):
                request = self._native_request(architectures=architectures)
                with self.assertRaisesRegex(retention.RetentionError, "one supported, explicit architecture"):
                    retention.compute_native_moltenvk_config_key(request)

    def test_native_moltenvk_missing_mandatory_output_fails_completion(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-missing-out", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        # Create empty products dir without libMoltenVK.dylib
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        with self.assertRaisesRegex(retention.RetentionError, "mandatory output role 'moltenvk_dylib' missing"):
            self._complete_native(lease)

    def test_native_moltenvk_rejects_output_architecture_mismatch(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-wrong-arch-output", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._arm64_macho_dylib_bytes())
        with self.assertRaisesRegex(retention.RetentionError, "architecture mismatch.*actual=arm64"):
            self._complete_native(lease)

    def test_native_moltenvk_rejects_fat_output(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-fat-output", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(
            b"\xca\xfe\xba\xbe\x00\x00\x00\x01" + b"\x00" * 20)
        with self.assertRaisesRegex(retention.RetentionError, "fat Mach-O output is unsupported"):
            self._complete_native(lease)

    def test_native_moltenvk_unexpected_binary_fails_completion(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-unexpected-bin", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        # Create mandatory output
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        # Create unexpected binary escape
        (products / "libextra.dylib").write_bytes(self._macho_dylib_bytes())
        with self.assertRaisesRegex(retention.RetentionError, "undeclared binary output discovered"):
            self._complete_native(lease)

    def test_native_moltenvk_output_hash_mismatch_refuses_retirement(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-hash-mismatch", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        # Mock _artifactize to corrupt sha
        with mock.patch.object(retention, "_artifactize", side_effect=retention.RetentionError("bit corruption")):
            with self.assertRaises(retention.RetentionError):
                self._complete_native(lease)
        self.assertTrue(lease.path.exists())

    def test_native_moltenvk_cleanup_failure_records_recovery_intent(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-cleanup-fail", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("permission denied")):
            with self.assertRaises(retention.RetentionError):
                self._complete_native(lease)
        lease._release_lock()
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).exists())

    def test_native_moltenvk_idempotent_completion_replay(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-idempotent", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        result1 = self._complete_native(lease)
        self.assertEqual(result1["status"], "COMPLETED_AND_AUTO_RETIRED")
        # Second call returns idempotent replay
        result2 = self._complete_native(lease)
        self.assertTrue(result2.get("idempotent_replay"))
        self.assertEqual(result2["build_manifest_id"], result1["build_manifest_id"])

    def test_native_moltenvk_canonical_equivalent_reuse(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-canonical-first", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        result = self._complete_native(lease)
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")

        # Second reservation with identical key returns None (reused)
        lease2 = self._native_lease("native-canonical-second", req)
        reserved_path = lease2.reserve()
        self.assertIsNone(reserved_path)
        self.assertIsNotNone(lease2.reused_manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
