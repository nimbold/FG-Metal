#!/usr/bin/env python3
"""Synthetic process-ownership and path tests for loader evidence parsing."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from experiments.dxvk_macos_step11d3b.loader_evidence import (
    LoaderEvidenceError,
    _canonical_windows_path,
    parse_loader_evidence,
)


TARGET_UNIX_PID = 26083
TARGET_WINDOWS_PID = 0x24
EXPECTED_VERSION = "MoltenVK v1.4.2"
EXPECTED_ROLES = {
    "wine", "wine_launcher", "wineserver", "winevulkan_dll", "winevulkan_so",
    "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
}
IDENTITY_KINDS = {
    "wine": "dyld-main-executable",
    "wine_launcher": "prefixlease-spawn-executable",
    "wineserver": "prefixlease-bound-executable",
    "winevulkan_dll": "win32-hmodule-proof-in-paired-app-trace",
    "winevulkan_so": "dyld-loaded-image",
    "wine_host_loader": "dyld-loaded-image",
    "winemac_so": "dyld-loaded-image",
    "trace_proxy": "dyld-loaded-image",
    "moltenvk": "dyld-loaded-image",
}


class LoaderEvidenceParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.identities: dict[str, dict[str, str]] = {}
        self.prefix_root = self.root / "prefix"
        role_paths = {
            "wine": self.root / "runtime/wine/lib/wine/x86_64-unix/wine",
            "wine_launcher": self.root / "runtime/wine/bin/wine",
            "wineserver": self.root / "runtime/wine/bin/wineserver",
            "winevulkan_dll": self.root / "prefix/drive_c/windows/system32/winevulkan.dll",
            "winevulkan_so": self.root / "runtime/wine/lib/wine/x86_64-unix/winevulkan.so",
            "wine_host_loader": self.root / "runtime/wine/lib/wine/x86_64-unix/win32u.so",
            "winemac_so": self.root / "runtime/wine/lib/wine/x86_64-unix/winemac.so",
            "trace_proxy": self.run_dir / "trace-loader/libMoltenVK.dylib",
            "moltenvk": self.run_dir / "mvk/libMoltenVK.1.dylib",
        }
        for role, path in role_paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"fixture:{role}".encode()
            path.write_bytes(payload)
            self.identities[role] = {
                "path": str(path.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}
        self.expected_runtime_winevulkan_path = self.root / "retained-runtime/winevulkan.dll"
        self.expected_runtime_winevulkan_path.parent.mkdir(parents=True)
        self.expected_runtime_winevulkan_path.write_bytes(b"fixture:winevulkan_dll")
        self.expected_runtime_winevulkan_path = self.expected_runtime_winevulkan_path.resolve()
        (self.run_dir / "run-manifest.json").write_text(json.dumps({
            "prefix_path": str(self.prefix_root.resolve()),
            "prefix_winevulkan_path": self.identities["winevulkan_dll"]["path"],
        }) + "\n", encoding="utf-8")
        self.app_path = self.root / "control/bare_vulkan_app.exe"
        self.app_path.parent.mkdir(parents=True)
        self.app_path.write_bytes(b"fixture:app")
        self.app_path = self.app_path.resolve()
        self.app_sha256 = hashlib.sha256(self.app_path.read_bytes()).hexdigest()
        self.app_windows_path = r"Z:\tmp\control\bare_vulkan_app.exe"
        self.app_config = {
            "origin": "APP_CONFIG", "event": "app_configuration",
            "process_id": TARGET_WINDOWS_PID, "target_unix_pid": TARGET_UNIX_PID,
            "host_runtime_startup_gate_passed": True,
            "winevulkan_dll_module_loaded": True,
            "winevulkan_dll_before_frame_loop": True,
            "winevulkan_dll_module_path": r"C:\windows\system32\winevulkan.dll",
            "winevulkan_dll_host_path": self.identities["winevulkan_dll"]["path"],
            "winevulkan_dll_sha256": self.identities["winevulkan_dll"]["sha256"],
        }
        self.host_trace = self.root / "host-trace.jsonl"
        self.runtime_log = self.root / "runtime.log"
        self._write_host_trace()
        self._write_runtime_log()

    def _write_host_trace(self, *, version: str = EXPECTED_VERSION,
                          target_pid: int = TARGET_UNIX_PID) -> None:
        startup_id = f"pid-{target_pid}"
        process = {
            "origin": "INSTRUMENTATION", "event": "process_identity",
            "pid": target_pid, "startup_id": startup_id,
            "process_architecture": "x86_64", "process_translated": 1,
            "main_executable_path": self.identities["wine"]["path"],
            "main_executable_sha256": self.identities["wine"]["sha256"],
            "wine_launcher_path": self.identities["wine_launcher"]["path"],
            "wine_launcher_sha256": self.identities["wine_launcher"]["sha256"],
        }
        startup = {
            "origin": "INSTRUMENTATION", "event": "host_runtime_startup",
            "startup_id": startup_id, "startup_valid": True,
            "process_architecture": "x86_64", "process_translated": 1,
            "moltenvk_version": version,
            "moltenvk_version_source": "vkGetVersionStringsMVK from loaded delegate",
            "vulkan_runtime_version": "1.4.357",
            "moltenvk_path": self.identities["moltenvk"]["path"],
            "moltenvk_sha256": self.identities["moltenvk"]["sha256"],
            "trace_proxy_path": self.identities["trace_proxy"]["path"],
            "trace_proxy_sha256": self.identities["trace_proxy"]["sha256"],
            "winevulkan_so_path": self.identities["winevulkan_so"]["path"],
            "winevulkan_so_sha256": self.identities["winevulkan_so"]["sha256"],
            "wine_host_loader_path": self.identities["wine_host_loader"]["path"],
            "wine_host_loader_sha256": self.identities["wine_host_loader"]["sha256"],
            "host_loader_role": (
                "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; "
                "no Khronos libvulkan"),
            "loaded_moltenvk_count": 1, "loaded_proxy_count": 1,
            "loaded_winevulkan_count": 1, "loaded_host_loader_count": 1,
            "loaded_libvulkan_dylib_count": 0,
        }
        rows = [process, startup]
        for role in sorted(EXPECTED_ROLES):
            identity = self.identities[role]
            rows.append({
                "origin": "INSTRUMENTATION", "event": "startup_module_identity",
                "startup_id": startup_id, "startup_phase": True, "role": role,
                "path": identity["path"], "sha256": identity["sha256"],
                "observed_path": identity["path"],
                "observed_sha256": identity["sha256"],
                "identity_kind": IDENTITY_KINDS[role],
                "loaded_in_process": role not in {"wine_launcher", "wineserver"},
                "identity_match": True,
            })
        self.host_trace.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def _wine_line(self, windows_pid: int, escaped_path: str, kind: str = "builtin",
                   tid: int = 0x31) -> str:
        return (f"{windows_pid:04x}:{tid:04x}:trace:loaddll:build_module "
                f'Loaded L"{escaped_path}" at 00006FFFFB1A0000: {kind}')

    def _write_runtime_log(self, *, dll_path: str = r"C:\\windows\\system32\\winevulkan.dll",
                           dll_kind: str = "builtin",
                           app_path: str | None = None,
                           extra_wine_rows: list[str] | None = None,
                           omit_target_dyld: bool = False) -> None:
        if app_path is None:
            app_path = self.app_windows_path.replace("\\", "\\\\")
        rows = [
            # Unrelated processes are deliberately present in both log formats.
            self._wine_line(0x9, r"C:\\windows\\system32\\winevulkan.dll"),
            self._wine_line(TARGET_WINDOWS_PID, r"C:\\windows\\system32\\kernel32.dll"),
            self._wine_line(TARGET_WINDOWS_PID, dll_path, dll_kind),
            self._wine_line(TARGET_WINDOWS_PID, app_path, "native"),
        ]
        rows.extend(extra_wine_rows or [])
        for index, role in enumerate(
                ("winevulkan_so", "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk"),
                start=1):
            image_uuid = f"00000000-0000-0000-0000-{index:012x}"
            rows.append(f"dyld[{TARGET_UNIX_PID + 1}]: <{image_uuid}> /unrelated/{role}")
            if not omit_target_dyld:
                path = self.identities[role]["path"]
                rows.append(f"dyld[{TARGET_UNIX_PID}]: <{image_uuid}> {path}")
        # This untagged helper banner is intentionally contradictory. Only the
        # target-owned host startup block may establish the runtime version.
        rows.append("[mvk-info] MoltenVK version 1.4.3, supporting Vulkan version 1.9.999.")
        self.runtime_log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    def _parse(self) -> dict[str, Any]:
        return parse_loader_evidence(
            self.runtime_log, self.run_dir,
            self.identities["winevulkan_dll"],
            target_unix_pid=TARGET_UNIX_PID,
            app_windows_pid=TARGET_WINDOWS_PID,
            host_trace_path=self.host_trace,
            expected_module_identities=self.identities,
            expected_app_path=self.app_path,
            expected_app_sha256=self.app_sha256,
            app_windows_path=self.app_windows_path,
            expected_moltenvk_version=EXPECTED_VERSION,
            expected_runtime_winevulkan_path=self.expected_runtime_winevulkan_path,
            app_config=self.app_config,
        )

    def test_uses_owned_loader_rows_and_target_startup_version(self) -> None:
        # PrefixLease cleanup removed the clone before this post-run parsing.
        Path(self.identities["winevulkan_dll"]["path"]).unlink()
        evidence = self._parse()
        self.assertEqual(evidence["winevulkan_dll_reported_path"],
                         r"C:\windows\system32\winevulkan.dll")
        self.assertEqual(evidence["target_unix_pid"], TARGET_UNIX_PID)
        self.assertEqual(evidence["target_windows_pid"], TARGET_WINDOWS_PID)
        self.assertEqual(evidence["moltenvk_version"], EXPECTED_VERSION)
        self.assertEqual(evidence["moltenvk_runtime_banners"], [
            {"moltenvk_version": "1.4.2", "vulkan_version": "1.4.357"}])
        self.assertNotIn("/unrelated/", "\n".join(evidence["dyld_loaded_paths"]))
        self.assertEqual({row["pid"] for row in evidence["wine_module_records"]},
                         {TARGET_WINDOWS_PID})
        app_row = next(row for row in evidence["wine_module_records"]
                       if row["path"].casefold() == self.app_windows_path.casefold())
        self.assertEqual(app_row["path"], self.app_windows_path)
        self.assertIn(r'Loaded L"Z:\\tmp\\control\\bare_vulkan_app.exe"', app_row["line"])
        dll_row = next(row for row in evidence["wine_module_records"]
                       if row["path"].casefold() == r"c:\windows\system32\winevulkan.dll")
        self.assertEqual(dll_row["path"], r"C:\windows\system32\winevulkan.dll")
        self.assertIn(r'Loaded L"C:\\windows\\system32\\winevulkan.dll"', dll_row["line"])
        self.assertEqual(evidence["winevulkan_dll_path"], self.identities["winevulkan_dll"]["path"])

    def test_actual_br3_loader_row_without_pid_tid_cannot_satisfy_target(self) -> None:
        # This is the BR3 runtime.log form: WINEDEBUG had +loaddll but no +pid,+tid.
        self.runtime_log.write_text(
            '0024:trace:loaddll:build_module Loaded L"C:\\\\windows\\\\system32\\\\winevulkan.dll" '
            "at 00006FFFFB1A0000: builtin\n", encoding="utf-8")
        with self.assertRaisesRegex(LoaderEvidenceError, "pid:tid"):
            self._parse()

    def test_other_wine_pid_cannot_supply_required_module(self) -> None:
        self._write_runtime_log()
        rows = self.runtime_log.read_text(encoding="utf-8").splitlines()
        rows = [row for row in rows if not row.startswith(f"{TARGET_WINDOWS_PID:04x}:")]
        rows.extend([
            self._wine_line(0x9, r"C:\\windows\\system32\\kernel32.dll"),
            self._wine_line(0x9, r"C:\\windows\\system32\\winevulkan.dll"),
            self._wine_line(0x9, self.app_windows_path.replace("\\", "\\\\"), "native"),
        ])
        self.runtime_log.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LoaderEvidenceError, "no loader records"):
            self._parse()

    def test_malformed_escape_traversal_syswow64_and_backup_paths_fail(self) -> None:
        cases = [
            (r"C:\windows\system32\winevulkan.dll", "malformed path escape"),
            (r"C:\\windows\\..\\system32\\winevulkan.dll", "path traversal"),
            (r"C:\\windows\\syswow64\\winevulkan.dll", "SysWOW64"),
            (r"C:\\windows\\system32\\winevulkan.dll.backup", "backup image"),
            (r"C:\\windows\\system32\\winevulkan.dll.bak", "backup image"),
        ]
        for escaped_path, message in cases:
            with self.subTest(path=escaped_path):
                self._write_runtime_log(dll_path=escaped_path)
                with self.assertRaisesRegex(LoaderEvidenceError, message):
                    self._parse()

    def test_duplicate_or_wrong_kind_builtin_record_fails(self) -> None:
        duplicate = self._wine_line(
            TARGET_WINDOWS_PID, r"C:\\windows\\system32\\winevulkan.dll")
        self._write_runtime_log(extra_wine_rows=[duplicate])
        with self.assertRaisesRegex(LoaderEvidenceError, "duplicate target Wine module path"):
            self._parse()

        self._write_runtime_log(dll_kind="native")
        with self.assertRaisesRegex(LoaderEvidenceError, "builtin C:"):
            self._parse()

    def test_exact_native_app_path_and_pid_are_required(self) -> None:
        self._write_runtime_log(app_path=r"Z:\\tmp\\control\\other.exe")
        with self.assertRaisesRegex(LoaderEvidenceError, "exact native PE app path"):
            self._parse()

        with self.assertRaisesRegex(LoaderEvidenceError, "APP_CONFIG process identity"):
            parse_loader_evidence(
                self.runtime_log, self.run_dir, self.identities["winevulkan_dll"],
                target_unix_pid=TARGET_UNIX_PID, app_windows_pid=TARGET_WINDOWS_PID + 1,
                host_trace_path=self.host_trace,
                expected_module_identities=self.identities,
                expected_app_path=self.app_path, expected_app_sha256=self.app_sha256,
                app_windows_path=self.app_windows_path,
                expected_moltenvk_version=EXPECTED_VERSION,
                expected_runtime_winevulkan_path=self.expected_runtime_winevulkan_path,
                app_config=self.app_config,
            )

    def test_dyld_images_must_belong_to_supervised_unix_pid(self) -> None:
        self._write_runtime_log(omit_target_dyld=True)
        with self.assertRaisesRegex(LoaderEvidenceError, "no loaded images"):
            self._parse()

    def test_target_startup_version_and_module_hashes_are_authoritative(self) -> None:
        self._write_host_trace(version="MoltenVK v1.4.3")
        with self.assertRaisesRegex(LoaderEvidenceError, "invalid module identity or MoltenVK version"):
            self._parse()

        self._write_host_trace()
        broken = json.loads(self.host_trace.read_text(encoding="utf-8").splitlines()[2])
        broken["observed_sha256"] = "f" * 64
        rows = self.host_trace.read_text(encoding="utf-8").splitlines()
        rows[2] = json.dumps(broken)
        self.host_trace.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LoaderEvidenceError, "pinned path or SHA-256"):
            self._parse()

    def test_target_dyld_duplicate_fails_even_when_one_path_is_pinned(self) -> None:
        self._write_runtime_log()
        rows = self.runtime_log.read_text(encoding="utf-8").splitlines()
        target = next(row for row in rows if row.startswith(f"dyld[{TARGET_UNIX_PID}]:"))
        rows.append(target)
        self.runtime_log.write_text("\n".join(rows) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LoaderEvidenceError, "duplicate target dyld image path"):
            self._parse()

    def test_runtime_image_path_through_ancestor_symlink_fails(self) -> None:
        alias = self.root / "runtime-alias"
        alias.symlink_to(self.root / "runtime", target_is_directory=True)
        original = self.identities["wine"]["path"]
        self.identities["wine"]["path"] = str(
            alias / Path(original).relative_to(self.root / "runtime"))
        with self.assertRaisesRegex(LoaderEvidenceError, "ancestor symlink"):
            self._parse()

    def test_prefix_dll_must_be_the_system32_builtin(self) -> None:
        wrong_path = self.root / "prefix/drive_c/windows/syswow64/winevulkan.dll"
        wrong_path.parent.mkdir(parents=True)
        wrong_path.write_bytes(b"fixture:winevulkan_dll")
        self.identities["winevulkan_dll"] = {
            "path": str(wrong_path.resolve()),
            "sha256": hashlib.sha256(wrong_path.read_bytes()).hexdigest(),
        }
        self._write_host_trace()
        with self.assertRaisesRegex(LoaderEvidenceError, "differs from the run manifest"):
            self._parse()

    def test_windows_expected_path_forms_and_lookalike_basename(self) -> None:
        self.assertEqual(_canonical_windows_path(r"C:\foo\winevulkan.dll", "fixture"),
                         r"c:\foo\winevulkan.dll")
        self.assertEqual(_canonical_windows_path(r'"C:\foo\winevulkan.dll"', "fixture"),
                         r"c:\foo\winevulkan.dll")

        # A lookalike basename is an additional image, not the exact required
        # system32 winevulkan.dll record.
        self._write_runtime_log(extra_wine_rows=[self._wine_line(
            TARGET_WINDOWS_PID, r"C:\\windows\\system32\\winevulkan2.dll")])
        self.assertTrue(self._parse()["winevulkan_dll_loaded"])

        # The expected APP_CONFIG path may be quoted; Wine log path quoting
        # and doubled-separator escaping remain parsed independently.
        self.app_windows_path = '"' + self.app_windows_path + '"'
        canonical_app = self.app_windows_path[1:-1]
        self._write_runtime_log(app_path=canonical_app.replace("\\", "\\\\"))
        self.assertTrue(self._parse()["winevulkan_dll_loaded"])

    def test_alternate_directory_and_lookalike_cannot_replace_exact_dll(self) -> None:
        for path in (r"C:\\foo\\winevulkan.dll",
                     r"C:\\windows\\system32\\winevulkan2.dll",
                     r"C:\\windows\\system32\\winevulkan.dll.bak"):
            with self.subTest(path=path):
                self._write_runtime_log(dll_path=path)
                with self.assertRaises(LoaderEvidenceError):
                    self._parse()

    def test_removed_prefix_dll_uses_captured_hmodule_and_retained_hash(self) -> None:
        Path(self.identities["winevulkan_dll"]["path"]).unlink()
        self.assertTrue(self._parse()["winevulkan_dll_loaded"])

        self.app_config["winevulkan_dll_sha256"] = "f" * 64
        with self.assertRaisesRegex(LoaderEvidenceError, "APP_CONFIG prefix DLL path or SHA-256"):
            self._parse()

        self.app_config["winevulkan_dll_sha256"] = self.identities["winevulkan_dll"]["sha256"]
        self.expected_runtime_winevulkan_path.write_bytes(b"wrong retained runtime DLL")
        with self.assertRaisesRegex(LoaderEvidenceError, "SHA-256 differs"):
            self._parse()

        self.expected_runtime_winevulkan_path.write_bytes(b"fixture:winevulkan_dll")
        self.app_config["process_id"] = TARGET_WINDOWS_PID + 1
        with self.assertRaisesRegex(LoaderEvidenceError, "APP_CONFIG process identity"):
            self._parse()


if __name__ == "__main__":
    unittest.main()
