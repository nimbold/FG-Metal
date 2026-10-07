#!/usr/bin/env python3
"""Regression tests for the native MoltenVK build runner's source identity gate."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_native_moltenvk as builder  # noqa: E402


class MaterializedSourceIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = SimpleNamespace(source_tree_sha256="a" * 64)
        self.identity = {"base_source_file_count": 12}

    def test_matching_materialized_hash_and_pinned_file_count_pass(self) -> None:
        builder.verify_materialized_source_identity("a" * 64, 12, self.request, self.identity)

    def test_materialized_file_count_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "materialized source tree"):
            builder.verify_materialized_source_identity("a" * 64, 11, self.request, self.identity)

    def test_materialized_hash_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "materialized source tree"):
            builder.verify_materialized_source_identity("b" * 64, 12, self.request, self.identity)

    def test_malformed_pinned_file_count_fails_closed(self) -> None:
        for value in (None, True, 0, -1, "12"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    RuntimeError, "invalid base source file count"):
                builder.verify_materialized_source_identity(
                    "a" * 64, 12, self.request, {"base_source_file_count": value})


if __name__ == "__main__":
    unittest.main()
