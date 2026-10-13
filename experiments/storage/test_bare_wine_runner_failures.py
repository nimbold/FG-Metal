"""Offline tests for runner_b failures before Wine produces an execution record."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3b.runner_b import read_execution_result


class RunnerExecutionResultTests(unittest.TestCase):
    def test_missing_execution_result_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result, error = read_execution_result(Path(temporary) / "execution-result.json")

        self.assertIsNone(result)
        self.assertEqual(error, "execution result is missing")

    def test_malformed_execution_result_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution-result.json"
            path.write_text("{broken", encoding="utf-8")

            result, error = read_execution_result(path)

        self.assertIsNone(result)
        self.assertIn("execution result could not be read", error)

    def test_non_object_execution_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution-result.json"
            path.write_text(json.dumps([{"process_returncode": 0}]), encoding="utf-8")

            result, error = read_execution_result(path)

        self.assertIsNone(result)
        self.assertEqual(error, "execution result must contain a JSON object")

    def test_execution_result_symlink_is_not_followed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.json"
            target.write_text(json.dumps({"process_returncode": 0}), encoding="utf-8")
            path = root / "execution-result.json"
            path.symlink_to(target)

            result, error = read_execution_result(path)

        self.assertIsNone(result)
        self.assertEqual(error, "execution result is a symlink")

    def test_valid_execution_result_is_returned(self) -> None:
        expected = {"process_returncode": 0, "target_unix_pid": 12345}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "execution-result.json"
            path.write_text(json.dumps(expected), encoding="utf-8")

            result, error = read_execution_result(path)

        self.assertEqual(result, expected)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
