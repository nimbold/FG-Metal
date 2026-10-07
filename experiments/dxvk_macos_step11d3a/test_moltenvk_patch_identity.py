#!/usr/bin/env python3
"""Check that Git applies every added instrumentation file byte-for-byte."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


EXPERIMENT_DIR = Path(__file__).resolve().parent


class MoltenVKPatchIdentityTests(unittest.TestCase):
    def test_patch_application_matches_complete_prepared_source_identity(self) -> None:
        patch_bytes = (EXPERIMENT_DIR / "moltenvk-instrumentation.patch").read_bytes()
        identity = json.loads((EXPERIMENT_DIR / "prepared-source-identity.json").read_text())
        self.assertEqual(identity["patch_sha256"], hashlib.sha256(patch_bytes).hexdigest())
        self.assertEqual(identity["patch_size_bytes"], len(patch_bytes))

        sections = re.split(rb"(?=^diff --git )", patch_bytes, flags=re.MULTILINE)
        patch_sections: list[tuple[str, bytes]] = []
        for section in sections:
            if not section.startswith(b"diff --git "):
                continue
            header = section.splitlines()[0].decode("utf-8")
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", header)
            self.assertIsNotNone(match, f"malformed patch header: {header}")
            assert match is not None
            self.assertEqual(match.group(1), match.group(2), "patch paths differ")
            patch_sections.append((match.group(1), section))

        expected = {row["path"]: row for row in identity["changed_files"]}
        paths = [path for path, _section in patch_sections]
        self.assertEqual(len(paths), len(set(paths)), "patch repeats a changed path")
        self.assertEqual(set(paths), set(expected), "patch paths differ from prepared identity")

        with tempfile.TemporaryDirectory(prefix="mvk-patch-identity-") as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            seed_root = Path("/private/tmp/fgmetal-step11b/MoltenVK")
            for relative_path, row in expected.items():
                if row["pristine_sha256"] == "ABSENT":
                    continue
                if not seed_root.is_dir():
                    self.skipTest(f"pinned pristine MoltenVK source is unavailable: {seed_root}")
                pristine = seed_root / relative_path
                self.assertEqual(
                    hashlib.sha256(pristine.read_bytes()).hexdigest(), row["pristine_sha256"],
                    f"pristine source identity mismatch: {relative_path}",
                )
                target = root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pristine, target)
                os.chmod(target, pristine.stat().st_mode & 0o777)
            additions = root / "additions.patch"
            additions.write_bytes(b"".join(section for _path, section in patch_sections))
            subprocess.run(
                ["git", "-C", str(root), "apply", "--check", "--whitespace=error", str(additions)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "apply", "--whitespace=error", str(additions)],
                check=True,
            )
            for relative_path, row in expected.items():
                actual_hash = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
                self.assertEqual(actual_hash, row["prepared_sha256"], relative_path)


if __name__ == "__main__":
    unittest.main()
