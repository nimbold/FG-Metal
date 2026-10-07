#!/usr/bin/env python3
"""Synthetic Wine launcher and DYLD policy tests; no Wine process is executed."""
from __future__ import annotations

import hashlib
import os
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
                    wine_server_override: str | None = None) -> tuple[policy.PrefixLease, dict[str, str]]:
        prefix_root = self.base / f"prefix-root-{name}"
        prefix_root.mkdir()
        prefix = prefix_root / "lease"
        prefix.mkdir()
        output = self.output / name
        output.mkdir()
        wine = self.runtime / "bin/wine"
        wine.parent.mkdir(parents=True, exist_ok=True)
        wine.write_bytes(b"synthetic certified Wine loader\n")
        wine.chmod(0o755)
        wine_sha256, wine_identity = policy._wine_loader_identity(wine)
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
        runtime_manifest = (Path(policy.__file__).resolve().parent
                            / "runtime-manifests/step11d_r_active_runtime.json")
        lease._template_marker = {"runtime_identity": {
            "wine_runtime_root": str(self.runtime),
            "wine_binary_sha256": wine_sha256,
            "wine_runtime_tree_manifest": str(runtime_manifest),
            "wine_runtime_tree_sha256": "a" * 64,
            "moltenvk_sha256_for_subsequent_graphics_runs": self.candidate_sha256,
            "moltenvk_icd_sha256": hashlib.sha256(icd.read_bytes()).hexdigest(),
        }}
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

    def test_set_env_rejects_noncanonical_wine_loader_and_server_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not select the certified Wine runtime"):
            self._wine_lease("wine-loader-parent-component",
                             wine_loader_override=str(self.runtime / "bin/../bin/wine"))
        with self.assertRaisesRegex(ValueError, "does not select the certified Wine runtime"):
            self._wine_lease("wine-server-parent-component",
                             wine_server_override=str(self.runtime / "bin/../bin/wineserver"))

    def test_path_shadow_cannot_select_wine_and_child_gets_normalized_dyld(self) -> None:
        lease, raw_env = self._wine_lease()
        process = mock.Mock()
        with mock.patch.object(policy, "assert_prefix_lease"), \
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

        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "differs from the certified MoltenVK binary"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=raw_env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

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
                mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "runtime changed after PrefixLease sealing"):
                lease.start_wine_process([str(lease._wine_loader_path), "--version"],
                                         env=raw_env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_path_shadow_and_similarly_named_wine_are_never_admitted_as_executable(self) -> None:
        lease, _raw_env = self._wine_lease("wine-shadow-rejected")
        shadow_wine = self.base / "shadow-bin/wine"
        with mock.patch.object(policy, "assert_prefix_lease"), \
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
        with mock.patch.object(policy, "assert_prefix_lease"), \
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
