#!/usr/bin/env python3
"""Synthetic Wine launcher and DYLD policy tests; no Wine process is executed."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storage_policy as policy
import initialize_prefix_template


class WineLauncherPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-wine-launcher-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "run-output"
        self.output.mkdir()
        self.runtime = self.base / "runtime"
        self.runtime_lib = self.runtime / "lib"
        self.runtime_unix = self.runtime_lib / "wine/x86_64-unix"
        self.runtime_unix.mkdir(parents=True)
        self.runtime_manifest = self.base / "runtime-manifest.json"
        manifest_patcher = mock.patch.object(policy, "WINE_RUNTIME_TREE_MANIFEST",
                                             self.runtime_manifest)
        manifest_patcher.start()
        self.addCleanup(manifest_patcher.stop)
        self.candidate = self.output / "loader-override"
        self.candidate.mkdir()
        self.candidate_bytes = b"synthetic certified MoltenVK candidate\n"
        self.candidate_sha256 = hashlib.sha256(self.candidate_bytes).hexdigest()
        (self.candidate / "libMoltenVK.dylib").write_bytes(self.candidate_bytes)
        self.unrelated = self.output / "fgmetal-vulkan-link-sibling"
        self.unrelated.mkdir()
        (self.unrelated / "unrelated.txt").write_text("preserve this entry\n", encoding="utf-8")
        self.system_lib = self.output / "system-lib"
        self.system_lib.mkdir()
        self.alias = self.base / "fgmetal-vulkan-link"
        self.alias.mkdir()
        (self.alias / "libMoltenVK.dylib").symlink_to(self.candidate / "libMoltenVK.dylib")
        alias_patcher = mock.patch.object(policy, "_LEGACY_DYLD_MOLTENVK_ALIAS", self.alias)
        alias_patcher.start()
        self.addCleanup(alias_patcher.stop)
        runtime_hash_patcher = mock.patch.object(
            initialize_prefix_template, "actual_runtime_tree_hash", return_value=("a" * 64, 1, []))
        runtime_hash_patcher.start()
        self.addCleanup(runtime_hash_patcher.stop)

    @property
    def trusted_roots(self) -> tuple[Path, ...]:
        return self.output, self.runtime_lib, self.system_lib

    def _env(self, library: str, fallback: str | None = None) -> dict[str, str]:
        return {
            "DYLD_LIBRARY_PATH": library,
            "DYLD_FALLBACK_LIBRARY_PATH": fallback if fallback is not None else library,
            "DYLD_PRINT_LIBRARIES": "1",
        }

    def _normalize(self, env: dict[str, str], *,
                   candidate_fallback_roots: tuple[Path, ...] = ()) -> dict[str, str]:
        return policy._normalize_wine_dyld_environment(
            env,
            expected_moltenvk_sha256=self.candidate_sha256,
            trusted_roots=self.trusted_roots,
            candidate_fallback_roots=candidate_fallback_roots,
        )

    def test_exact_alias_component_is_rewritten_and_other_entries_are_preserved(self) -> None:
        raw_library = ":".join((str(self.candidate), str(self.runtime_unix), str(self.alias),
                                 str(self.unrelated), str(self.candidate)))
        raw_fallback = ":".join((raw_library, str(self.system_lib)))

        normalized = self._normalize(self._env(raw_library, raw_fallback))

        self.assertEqual(normalized["DYLD_LIBRARY_PATH"],
                         ":".join((str(self.candidate), str(self.runtime_unix), str(self.unrelated))))
        self.assertEqual(normalized["DYLD_FALLBACK_LIBRARY_PATH"],
                         ":".join((str(self.candidate), str(self.runtime_unix), str(self.unrelated),
                                   str(self.system_lib))))
        self.assertNotIn(str(self.alias), normalized["DYLD_LIBRARY_PATH"])

    def test_similarly_named_directory_is_not_rewritten_by_substring(self) -> None:
        raw = ":".join((str(self.candidate), str(self.alias), str(self.unrelated)))

        normalized = self._normalize(self._env(raw))

        self.assertEqual(normalized["DYLD_LIBRARY_PATH"],
                         ":".join((str(self.candidate), str(self.unrelated))))

    def test_duplicate_components_are_deduplicated_in_first_seen_order(self) -> None:
        raw = ":".join((str(self.candidate), str(self.runtime_unix), str(self.candidate),
                         str(self.alias), str(self.runtime_unix)))

        normalized = self._normalize(self._env(raw))

        self.assertEqual(normalized["DYLD_LIBRARY_PATH"],
                         ":".join((str(self.candidate), str(self.runtime_unix))))

    def test_empty_or_relative_components_fail_closed(self) -> None:
        for raw in (f"{self.candidate}::{self.runtime_unix}",
                    f"{self.candidate}:relative-library-dir"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self._normalize(self._env(raw))

    def test_parent_component_cannot_escape_a_trusted_search_root(self) -> None:
        escaped = self.base / "outside-output"
        escaped.mkdir()
        lexical_escape = self.output / ".." / escaped.name
        self.assertTrue(lexical_escape.relative_to(self.output))
        self.assertNotEqual(os.path.abspath(lexical_escape), str(lexical_escape))

        raw = self._env(":".join((str(self.candidate), str(lexical_escape))))
        with self.assertRaisesRegex(ValueError, "non-canonical absolute path"):
            self._normalize(raw)

    def test_unpinned_system_moltenvk_cannot_be_used_as_fallback(self) -> None:
        (self.system_lib / "libMoltenVK.1.dylib").write_bytes(b"unexpected system candidate")
        raw = self._env(":".join((str(self.candidate), str(self.system_lib))))
        with self.assertRaisesRegex(ValueError, "certified MoltenVK"):
            self._normalize(raw, candidate_fallback_roots=(self.runtime_lib,))

    def test_symlinked_directory_component_is_rejected(self) -> None:
        symlink_directory = self.output / "linked-library-root"
        symlink_directory.symlink_to(self.unrelated, target_is_directory=True)
        raw = ":".join((str(self.candidate), str(symlink_directory)))

        with self.assertRaises(OSError):
            self._normalize(self._env(raw))

    def test_unapproved_directory_and_similar_alias_name_fail_closed(self) -> None:
        unapproved = self.base / "fgmetal-vulkan-link-evil"
        unapproved.mkdir()
        raw = ":".join((str(self.candidate), str(unapproved)))

        with self.assertRaisesRegex(ValueError, "unapproved search directory"):
            self._normalize(self._env(raw))

    def test_certified_runtime_version_is_kept_only_after_the_selected_candidate(self) -> None:
        runtime_mvk = self.runtime_unix / "libMoltenVK.1.dylib"
        runtime_mvk.write_bytes(b"synthetic certified runtime fallback")
        (self.runtime_unix / "libMoltenVK.dylib").symlink_to(runtime_mvk.name)
        raw = self._env(":".join((str(self.candidate), str(self.runtime_unix))))

        normalized = self._normalize(raw, candidate_fallback_roots=(self.runtime_lib,))

        self.assertEqual(normalized["DYLD_LIBRARY_PATH"],
                         ":".join((str(self.candidate), str(self.runtime_unix))))
        with self.assertRaisesRegex(ValueError, "must precede the Wine runtime fallback"):
            self._normalize(self._env(":".join((str(self.runtime_unix), str(self.candidate)))),
                            candidate_fallback_roots=(self.runtime_lib,))

    def test_alias_with_wrong_or_extra_prerequisites_is_rejected(self) -> None:
        (self.alias / "libMoltenVK.dylib").unlink()
        (self.alias / "libMoltenVK.dylib").symlink_to(self.output / "wrong.dylib")
        (self.output / "wrong.dylib").write_bytes(b"unexpected MoltenVK")
        with self.assertRaisesRegex(ValueError, "does not select the certified MoltenVK"):
            self._normalize(self._env(":".join((str(self.candidate), str(self.alias)))))

        (self.alias / "libMoltenVK.dylib").unlink()
        (self.alias / "libMoltenVK.dylib").symlink_to(self.candidate / "libMoltenVK.dylib")
        (self.alias / "extra.dylib").write_bytes(b"unexpected loader")
        with self.assertRaisesRegex(ValueError, "unapproved files"):
            self._normalize(self._env(":".join((str(self.candidate), str(self.alias)))))

    def test_alias_symlink_with_parent_component_is_rejected(self) -> None:
        noncanonical_target = self.candidate / ".." / self.candidate.name / "libMoltenVK.dylib"
        (self.alias / "libMoltenVK.dylib").unlink()
        (self.alias / "libMoltenVK.dylib").symlink_to(noncanonical_target)

        with self.assertRaisesRegex(ValueError, "symlink target is not canonical"):
            self._normalize(self._env(":".join((str(self.candidate), str(self.alias)))))

    def _wine_lease(self, name: str = "wine-launch", *,
                    wine_loader_override: str | None = None,
                    wine_server_override: str | None = None,
                    wine_binary_sha256: str | None = None,
                    include_host_fields: bool = True,
                    include_host_sha256: bool = True,
                    include_host_candidate: bool = True,
                    include_host_manifest_entry: bool = True,
                    host_architecture: str = "x86_64",
                    candidate_runtime_tree_sha256: str = "a" * 64,
                    manifest_tree_sha256: str = "a" * 64,
                    launcher_path_override: str | None = None) -> tuple[policy.PrefixLease, dict[str, str]]:
        prefix_root = self.base / f"prefix-root-{name}"
        prefix_root.mkdir()
        prefix = prefix_root / "lease"
        prefix.mkdir()
        output = self.output / name
        output.mkdir()
        wine = self.runtime / "bin/wine"
        wine.parent.mkdir(parents=True, exist_ok=True)
        wine.write_bytes(self._macho("x86_64", b"synthetic launcher"))
        wine.chmod(0o755)
        wine_sha256, wine_identity = policy._wine_loader_identity(wine)
        host_image = self.runtime_unix / "wine"
        host_image.write_bytes(self._macho(host_architecture, b"synthetic host image"))
        host_image.chmod(0o755)
        host_sha256 = hashlib.sha256(host_image.read_bytes()).hexdigest()
        runtime_manifest = {
            "path": str(self.runtime),
            "tree_sha256": manifest_tree_sha256,
            "entries": [
                {"relative_path": "bin/wine", "kind": "file", "sha256": wine_sha256},
                *([{"relative_path": "lib/wine/x86_64-unix/wine", "kind": "file",
                    "sha256": host_sha256}] if include_host_manifest_entry else []),
            ],
        }
        self.runtime_manifest.write_text(json.dumps(runtime_manifest, sort_keys=True), encoding="utf-8")
        runtime_manifest_sha256 = hashlib.sha256(self.runtime_manifest.read_bytes()).hexdigest()
        candidate = output / "loader-override"
        candidate.mkdir()
        (candidate / "libMoltenVK.dylib").write_bytes(self.candidate_bytes)
        unrelated = output / "fgmetal-vulkan-link-sibling"
        unrelated.mkdir()
        (unrelated / "unrelated.txt").write_text("preserve this entry\n", encoding="utf-8")
        (self.alias / "libMoltenVK.dylib").unlink()
        (self.alias / "libMoltenVK.dylib").symlink_to(candidate / "libMoltenVK.dylib")
        raw = self._env(":".join((str(candidate), str(self.runtime_unix), str(self.alias),
                                   str(unrelated))))
        raw.update({
            "WINEPREFIX": str(prefix),
            "WINELOADER": wine_loader_override or str(wine),
            "WINESERVER": wine_server_override or str(self.runtime / "bin/wineserver"),
            "PATH": str(self.base / "shadow-bin"),
        })
        icd = output / "synthetic-icd.json"
        icd.write_text('{"synthetic": true}\n', encoding="utf-8")
        raw["VK_DRIVER_FILES"] = str(icd)
        raw["VK_ICD_FILENAMES"] = str(icd)
        shadow = self.base / "shadow-bin"
        shadow.mkdir(exist_ok=True)
        shadow_wine = shadow / "wine"
        shadow_wine.write_bytes(b"unexpected PATH shadow\n")
        shadow_wine.chmod(0o755)
        normalized = policy._normalize_wine_dyld_environment(
            raw,
            expected_moltenvk_sha256=self.candidate_sha256,
            trusted_roots=(output, self.runtime_lib),
            candidate_fallback_roots=(self.runtime_lib,),
        )
        lease = policy.PrefixLease(prefix, output, f"test-{name}", self.runtime / "bin/wineserver",
                                   template=self.base / "template", env=normalized)
        lease._materialized = True
        lease._template_marker = {"runtime_identity": {
            "wine_runtime_root": str(self.runtime),
            "wine_binary_sha256": wine_sha256,
            "wine_runtime_tree_manifest": str(self.runtime_manifest),
            "wine_runtime_tree_sha256": "a" * 64,
            "moltenvk_sha256_for_subsequent_graphics_runs": self.candidate_sha256,
            "moltenvk_icd_sha256": hashlib.sha256(icd.read_bytes()).hexdigest(),
        }}
        candidate_hashes = {
            "wine_runtime_tree": candidate_runtime_tree_sha256,
            "wine_binary": wine_binary_sha256 or wine_sha256,
            "wine_launcher": wine_sha256,
            "wine_launcher_binary": wine_sha256,
            "wine_runtime_manifest": runtime_manifest_sha256,
        }
        if include_host_candidate:
            candidate_hashes["wine_host_image"] = host_sha256
        lease._manifest = {
            "experiment_step": "11D.3-B",
            "candidate_hashes": candidate_hashes,
            "wine_runtime_tree_manifest_sha256": runtime_manifest_sha256,
        }
        lease._manifest.update({
            "wine_launcher_path": launcher_path_override or str(wine),
            "wine_launcher_sha256": wine_sha256,
        })
        if include_host_fields:
            lease._manifest["wine_host_image_path"] = str(host_image)
        if include_host_sha256:
            lease._manifest["wine_host_image_sha256"] = host_sha256
        with mock.patch.object(initialize_prefix_template, "actual_runtime_tree_hash",
                               return_value=("a" * 64, 1, [])):
            lease.set_env(raw)
        self.assertEqual(lease._env, normalized)
        self.assertEqual(lease._wine_loader_sha256, wine_sha256)
        self.assertEqual(lease._wine_loader_identity, wine_identity)
        lease._prefix_parent_fd = policy._open_directory_nofollow(prefix_root)
        self.addCleanup(lambda: os_close_if_open(lease._prefix_parent_fd))
        info = prefix.stat(follow_symlinks=False)
        lease._prefix_identity = (info.st_dev, info.st_ino)
        return lease, raw

    @staticmethod
    def _macho(architecture: str, payload: bytes) -> bytes:
        cpu_type = 0x01000007 if architecture == "x86_64" else 0x0100000C
        return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + bytes(24) + payload

    def test_set_env_rejects_noncanonical_wine_loader_and_server_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not select the certified Wine runtime"):
            self._wine_lease("wine-loader-parent-component",
                             wine_loader_override=str(self.runtime / "bin/../bin/wine"))
        with self.assertRaisesRegex(ValueError, "does not select the certified Wine runtime"):
            self._wine_lease("wine-server-parent-component",
                             wine_server_override=str(self.runtime / "bin/../bin/wineserver"))

    def test_br2_host_image_hash_cannot_be_mapped_to_wine_binary_launcher(self) -> None:
        with self.assertRaisesRegex(ValueError, "must bind the certified bin/wine launcher"):
            self._wine_lease("br2-host-hash-in-launcher-slot",
                             wine_binary_sha256=hashlib.sha256(
                                 self._macho("x86_64", b"synthetic host image")).hexdigest())

    def test_graphics_manifest_requires_the_separate_host_image_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "host image SHA-256 is missing"):
            self._wine_lease("missing-host-identity", include_host_sha256=False)
        with self.assertRaisesRegex(ValueError, "separate candidate hash"):
            self._wine_lease("missing-host-candidate", include_host_candidate=False)

    def test_graphics_host_must_be_the_exact_regular_file_in_the_runtime_manifest(self) -> None:
        with self.assertRaisesRegex(ValueError, "manifest does not bind the exact host image"):
            self._wine_lease("host-absent-from-runtime-tree", include_host_manifest_entry=False)

    def test_graphics_identity_rejects_alternate_runtime_tree_and_wrong_architecture(self) -> None:
        with self.assertRaisesRegex(ValueError, "runtime tree does not match"):
            self._wine_lease("alternate-tree-candidate", candidate_runtime_tree_sha256="c" * 64)
        with self.assertRaisesRegex(ValueError, "not thin x86_64"):
            self._wine_lease("arm64-host-image", host_architecture="arm64")

    def test_graphics_identity_rejects_launcher_symlink_aliases(self) -> None:
        launcher = self.runtime / "bin/wine"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(self._macho("x86_64", b"synthetic launcher"))
        alias = self.runtime / "bin/wine-alias"
        alias.symlink_to(self.runtime / "bin/wine")
        with self.assertRaisesRegex(ValueError, "launcher path is not the exact canonical runtime path"):
            self._wine_lease("launcher-alias", launcher_path_override=str(alias))

    def test_host_hash_cannot_be_replaced_by_launcher_hash(self) -> None:
        lease, _ = self._wine_lease("host-hash-swapped")
        manifest = {**lease._manifest,
                    "candidate_hashes": {**lease._manifest["candidate_hashes"]}}
        manifest["wine_host_image_sha256"] = manifest["wine_launcher_sha256"]
        manifest["candidate_hashes"]["wine_host_image"] = manifest["wine_launcher_sha256"]
        with self.assertRaisesRegex(ValueError, "exact host image file"):
            policy._wine_graphics_identity_contract(manifest, lease._template_marker["runtime_identity"])

    def test_host_from_alternate_tree_and_host_symlink_fail_closed(self) -> None:
        lease, _ = self._wine_lease("alternate-host-path")
        manifest = dict(lease._manifest)
        manifest["wine_host_image_path"] = str(self.base / "alternate-wine/lib/wine/x86_64-unix/wine")
        with self.assertRaisesRegex(ValueError, "host image path"):
            policy._wine_graphics_identity_contract(manifest, lease._template_marker["runtime_identity"])
        host = Path(lease._manifest["wine_host_image_path"])
        retained = host.with_name("wine-real")
        host.rename(retained)
        host.symlink_to(retained)
        with self.assertRaisesRegex(ValueError, "host image.*unsafe"):
            policy._wine_graphics_identity_contract(lease._manifest, lease._template_marker["runtime_identity"])

    def test_wrong_launcher_bytes_and_wrong_host_bytes_fail_closed(self) -> None:
        lease, _ = self._wine_lease("wrong-launcher-content")
        launcher = Path(lease._manifest["wine_launcher_path"])
        original = launcher.read_bytes()
        launcher.write_bytes(self._macho("x86_64", b"different launcher"))
        with self.assertRaisesRegex(ValueError, "launcher.*unsafe"):
            policy._wine_graphics_identity_contract(lease._manifest, lease._template_marker["runtime_identity"])
        launcher.write_bytes(original)
        Path(lease._manifest["wine_host_image_path"]).write_bytes(self._macho("x86_64", b"different host"))
        with self.assertRaisesRegex(ValueError, "host image.*unsafe"):
            policy._wine_graphics_identity_contract(lease._manifest, lease._template_marker["runtime_identity"])

    def test_missing_host_path_and_wrong_launcher_architecture_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "host image path"):
            self._wine_lease("missing-host-path", include_host_fields=False)
        # Keep manifest bytes/hash consistent while changing the declared runtime file;
        # architecture must still reject an arm64 launcher.
        lease, _ = self._wine_lease("arm64-launcher")
        launcher = Path(lease._manifest["wine_launcher_path"])
        launcher.write_bytes(self._macho("arm64", b"wrong architecture"))
        with self.assertRaisesRegex(ValueError, "launcher.*not thin x86_64"):
            policy._wine_graphics_identity_contract(lease._manifest, lease._template_marker["runtime_identity"])

    def test_path_shadow_cannot_select_wine_and_child_gets_normalized_dyld(self) -> None:
        lease, raw_env = self._wine_lease()
        process = mock.Mock()
        # Isolate loader/env selection here; the prefix contract suite exercises
        # durable manifest and marker bindings without this test-only stub.
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(lease, "_assert_bound_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd", return_value=process) as spawn:
            result = lease.start_wine_process(
                [str(lease._wine_loader_path), "--version"],
                env=raw_env, runner_path=Path(__file__).resolve())

        self.assertIs(result, process)
        self.assertEqual(spawn.call_args.args[1][0], str(lease._wine_loader_path))
        passed_env = spawn.call_args.kwargs["env"]
        self.assertNotIn(str(self.alias), passed_env["DYLD_LIBRARY_PATH"])
        self.assertEqual(passed_env["PATH"], str(self.base / "shadow-bin"))

    def test_candidate_replacement_after_environment_sealing_fails_closed(self) -> None:
        lease, raw_env = self._wine_lease("candidate-replaced-after-sealing")
        candidate_root = Path(lease._env["DYLD_LIBRARY_PATH"].split(":", 1)[0])
        (candidate_root / "libMoltenVK.dylib").write_bytes(b"replacement after sealing")

        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "differs from the certified MoltenVK binary"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=raw_env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_host_image_replacement_after_environment_sealing_fails_before_spawn(self) -> None:
        lease, raw_env = self._wine_lease("host-replaced-after-sealing")
        host_image = Path(lease._manifest["wine_host_image_path"])
        host_image.write_bytes(self._macho("x86_64", b"replacement host image"))

        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "certified Wine host image"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=raw_env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_caller_cannot_supply_trace_target_pid(self) -> None:
        lease = object.__new__(policy.PrefixLease)
        lease._wine_env_set_attempted = False
        with self.assertRaisesRegex(ValueError, "supervised exec wrapper"):
            lease.set_env({"FG_B_TARGET_UNIX_PID": "123"})

    def test_trace_target_pid_requires_verified_direct_launcher(self) -> None:
        fd = policy._open_directory_nofollow(self.base)
        self.addCleanup(os.close, fd)
        with self.assertRaisesRegex(ValueError, "verified direct Wine launcher"):
            policy._spawn_in_directory_fd(fd, ["/usr/bin/true"], bind_trace_target_pid=True)

    def test_trace_target_pid_is_assigned_after_runtime_checks_in_wrapper(self) -> None:
        lease, raw_env = self._wine_lease("trusted-target-pid-wrapper")
        contract = lease._wine_graphics_contract
        fd = policy._open_directory_nofollow(self.base)
        self.addCleanup(os.close, fd)
        records = tuple((str(contract[f"{role}_path"]), contract[f"{role}_sha256"],
                         contract[f"{role}_identity"], "x86_64", role == "launcher")
                        for role in ("launcher", "host_image"))
        with mock.patch.object(policy.subprocess, "Popen") as spawn:
            policy._spawn_in_directory_fd(fd, [str(lease._wine_loader_path), "app.exe"],
                                         expected_file_identities=records, bind_trace_target_pid=True)
        wrapper = spawn.call_args.args[0][4]
        self.assertLess(wrapper.index("hasher.hexdigest()!=wine_sha"),
                        wrapper.index("os.environ['FG_B_TARGET_UNIX_PID']=str(os.getpid())"))
        self.assertLess(wrapper.index("os.environ['FG_B_TARGET_UNIX_PID']=str(os.getpid())"),
                        wrapper.index("os.execvpe"))
        self.assertIn("caller supplied trace target PID", wrapper)

    def test_exec_wrapper_rechecks_launcher_and_host_image_before_exec(self) -> None:
        launcher = self.runtime / "bin/wine-for-exec-check"
        host_image = self.runtime_unix / "host-for-exec-check"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher_bytes = self._macho("x86_64", b"launcher before wrapper exec")
        host_bytes = self._macho("x86_64", b"host before wrapper exec")
        launcher.write_bytes(launcher_bytes)
        host_image.write_bytes(host_bytes)
        launcher.chmod(0o755)
        host_image.chmod(0o755)
        directory_fd = policy._open_directory_nofollow(self.base)
        self.addCleanup(os.close, directory_fd)
        for replacement_target in (launcher, host_image):
            with self.subTest(replacement_target=replacement_target.name):
                launcher.write_bytes(launcher_bytes)
                host_image.write_bytes(host_bytes)
                launcher_hash, launcher_identity = policy._bind_native_file_identity(
                    launcher, verify_macho=True, expected_architecture="x86_64")
                host_hash, host_identity = policy._bind_native_file_identity(
                    host_image, verify_macho=True, expected_architecture="x86_64")
                original = replacement_target.read_bytes()
                changed = bytearray(original)
                changed[-1] ^= 1
                replacement_target.write_bytes(changed)
                process = policy._spawn_in_directory_fd(
                    directory_fd,
                    [str(launcher), "--synthetic-test"],
                    expected_file_identities=(
                        (str(launcher), launcher_hash, launcher_identity, "x86_64", True),
                        (str(host_image), host_hash, host_identity, "x86_64", False),
                    ),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                _stdout, stderr = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn("certified Wine runtime image changed before exec", stderr)

    def test_runtime_fallback_replacement_after_sealing_fails_closed(self) -> None:
        runtime_fallback = self.runtime_unix / "libMoltenVK.1.dylib"
        runtime_fallback.write_bytes(b"synthetic pinned Wine runtime fallback")
        (self.runtime_unix / "libMoltenVK.dylib").symlink_to(runtime_fallback.name)
        original_digest = hashlib.sha256(runtime_fallback.read_bytes()).hexdigest()
        lease, raw_env = self._wine_lease("runtime-fallback-replaced-after-sealing")
        runtime_fallback.write_bytes(b"modified Wine runtime fallback")

        def synthetic_tree_hash(root: Path) -> tuple[str, int, list[str]]:
            self.assertEqual(root, self.runtime)
            current_digest = hashlib.sha256(runtime_fallback.read_bytes()).hexdigest()
            return ("a" * 64 if current_digest == original_digest else "b" * 64, 1, [])

        with mock.patch.object(initialize_prefix_template, "actual_runtime_tree_hash",
                               side_effect=synthetic_tree_hash), \
                mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "runtime changed after PrefixLease sealing"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=raw_env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_br2_proxy_name_and_absolute_delegate_are_bound(self) -> None:
        proxy_dir = self.output / "trace-loader"
        proxy_dir.mkdir()
        proxy = proxy_dir / "libMoltenVK.dylib"
        proxy_bytes = b"synthetic BR2 Vulkan trace proxy\n"
        proxy.write_bytes(proxy_bytes)
        delegate_dir = self.output / "mvk"
        delegate_dir.mkdir()
        delegate = delegate_dir / "libMoltenVK.1.dylib"
        delegate.write_bytes(self.candidate_bytes)
        (delegate_dir / "libMoltenVK.dylib").write_bytes(self.candidate_bytes)
        library = ":".join((str(proxy_dir), str(delegate_dir), str(self.runtime_unix)))
        env = self._env(library)
        env.update({
            "FG_B_TRACE_PROXY_PATH": str(proxy),
            "FG_B_TRACE_PROXY_SHA256": hashlib.sha256(proxy_bytes).hexdigest(),
            "FG_B_ORIGINAL_MOLTENVK_LIBRARY": str(delegate),
            "FG_B_ORIGINAL_MOLTENVK_SHA256": self.candidate_sha256,
        })

        normalized = self._normalize(env, candidate_fallback_roots=(self.runtime_unix,))

        self.assertEqual(normalized["DYLD_LIBRARY_PATH"].split(":" )[:2],
                         [str(proxy_dir), str(delegate_dir)])

    def test_br2_proxy_binding_rejects_wrong_hash_or_search_order(self) -> None:
        proxy_dir = self.output / "trace-loader"
        proxy_dir.mkdir()
        proxy = proxy_dir / "libMoltenVK.dylib"
        proxy_bytes = b"synthetic BR2 Vulkan trace proxy\n"
        proxy.write_bytes(proxy_bytes)
        delegate_dir = self.output / "mvk"
        delegate_dir.mkdir()
        delegate = delegate_dir / "libMoltenVK.1.dylib"
        delegate.write_bytes(self.candidate_bytes)
        (delegate_dir / "libMoltenVK.dylib").write_bytes(self.candidate_bytes)
        env = self._env(":".join((str(proxy_dir), str(delegate_dir), str(self.runtime_unix))))
        env.update({
            "FG_B_TRACE_PROXY_PATH": str(proxy),
            "FG_B_TRACE_PROXY_SHA256": "0" * 64,
            "FG_B_ORIGINAL_MOLTENVK_LIBRARY": str(delegate),
            "FG_B_ORIGINAL_MOLTENVK_SHA256": self.candidate_sha256,
        })
        with self.assertRaisesRegex(ValueError, "trace proxy SHA-256"):
            self._normalize(env, candidate_fallback_roots=(self.runtime_unix,))

        env["FG_B_TRACE_PROXY_SHA256"] = hashlib.sha256(proxy_bytes).hexdigest()
        env["DYLD_LIBRARY_PATH"] = ":".join((str(delegate_dir), str(proxy_dir),
                                                str(self.runtime_unix)))
        with self.assertRaisesRegex(ValueError, "first DYLD_LIBRARY_PATH"):
            self._normalize(env, candidate_fallback_roots=(self.runtime_unix,))

    def test_duplicate_moltenvk_candidate_directories_fail_closed(self) -> None:
        duplicate = self.output / "duplicate-loader-candidate"
        duplicate.mkdir()
        (duplicate / "libMoltenVK.dylib").write_bytes(self.candidate_bytes)
        env = self._env(":".join((str(self.candidate), str(duplicate))))
        with self.assertRaisesRegex(ValueError, "duplicate certified MoltenVK"):
            self._normalize(env)

    def test_path_shadow_and_similarly_named_wine_are_never_admitted_as_executable(self) -> None:
        lease, _raw_env = self._wine_lease("wine-shadow-rejected")
        shadow_wine = self.base / "shadow-bin/wine"
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            for command in (["wine", "--version"], [str(shadow_wine), "--version"]):
                with self.subTest(command=command), self.assertRaisesRegex(
                        ValueError, "must execute the certified Wine binary"):
                    lease.start_wine_process(command, env=lease._env,
                                             runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_unexpected_or_symlinked_wine_binary_fails_closed(self) -> None:
        lease, _raw_env = self._wine_lease("wine-binary-replaced")
        lease._wine_loader_path.write_bytes(b"unexpected Wine loader\n")
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "changed after PrefixLease sealing"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=lease._env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

        target = self.base / "real-wine-target"
        target.write_bytes(b"synthetic certified Wine loader\n")
        target.chmod(0o755)
        symlink = self.base / "wine-symlink"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "missing, not regular, or not executable"):
            policy._wine_loader_identity(symlink)


def os_close_if_open(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
