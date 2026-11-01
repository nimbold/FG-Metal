"""Fail-closed replay-2 gate tests for the persisted replay-1 classification."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

LOCAL_DIR = Path(__file__).resolve().parent
SOURCE = LOCAL_DIR / "z9_finder_replay.py"
SPEC = importlib.util.spec_from_file_location("z9_replay2_gate_test_target", SOURCE)
replay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(replay)


class Replay2ClassificationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.original_experiment = replay.EXPERIMENT
        self.original_analysis_source = replay.ANALYSIS_SOURCE
        replay.EXPERIMENT = self.root / "experiment"
        replay.ANALYSIS_SOURCE = self.root / "z9_event_aligned_analysis.py"
        replay.ANALYSIS_SOURCE.write_text("# fixed analyzer fixture\n", encoding="utf-8")

        self.run_id = "step11d3b-baseline-01-fixture"
        self.target_pid = 4321
        self.run_directory = self.root / "z9-replay-1-fixture"
        self.run_directory.mkdir()
        self.controller_path = self.run_directory / "controller.json"
        self.controller_path.write_text("{}\n", encoding="utf-8")
        self.trace_path = (replay.EXPERIMENT / "runs" / self.run_id / "host-traces"
                           / f"host-trace.{self.target_pid}.jsonl")
        self.trace_path.parent.mkdir(parents=True)
        self.trace_path.write_bytes(b'{"event":"fixture"}\n')
        self.controller = {
            "run_id": self.run_id,
            "target_pid": self.target_pid,
            "target_trace_path": str(self.trace_path),
            "poller_path": str(self.run_directory / "public-cgwindow-inventory.jsonl"),
            "accepted_public_cg_window_id": 880,
            "finder_identity": {"cg_window_id": 990},
        }
        self.analysis_path = self.run_directory / "event-aligned-analysis.json"
        self.write_analysis()
        self.addCleanup(self.restore_globals)

    def restore_globals(self) -> None:
        replay.EXPERIMENT = self.original_experiment
        replay.ANALYSIS_SOURCE = self.original_analysis_source

    def test_8192_is_visible_clear_for_the_covered_state_component(self) -> None:
        state = replay.telemetry_state({"window_occlusion_state": 8192})
        self.assertIs(state["visible_bit_set"], False)

    def analysis_record(self, replay_class: object = "R1") -> dict:
        return {
            "schema_version": 1,
            "analysis_source": str(replay.ANALYSIS_SOURCE.resolve()),
            "analysis_source_sha256": replay.sha256(replay.ANALYSIS_SOURCE),
            "controller_path": str(self.controller_path.resolve()),
            "trace_path": str(self.trace_path.resolve()),
            "trace_sha256": replay.sha256(self.trace_path),
            "poller_path": self.controller["poller_path"],
            "target_pid": self.target_pid,
            "target_cg_window_id": 880,
            "finder_cg_window_id": 990,
            "sequence_integrity_ok": True,
            "accepted_identity_continuity_ok": True,
            "finder_full_cover_hold_complete": True,
            "controller_technical_valid": True,
            "replay_classification": replay_class,
        }

    def write_analysis(self, record: dict | None = None) -> None:
        payload = record if record is not None else self.analysis_record()
        self.analysis_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_accepts_r1_to_r4_only_when_exact_controller_trace_and_ids_match(self) -> None:
        for replay_class in ("R1", "R2", "R3", "R4"):
            with self.subTest(replay_class=replay_class):
                self.write_analysis(self.analysis_record(replay_class))
                binding = replay.require_valid_replay1_analysis(
                    self.analysis_path, self.controller_path, self.controller)
                self.assertEqual(binding["replay_classification"], replay_class)
                self.assertEqual(binding["trace_sha256"], replay.sha256(self.trace_path))

    def test_missing_noncanonical_and_out_of_directory_reports_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires one canonical"):
            replay.require_valid_replay1_analysis(
                self.run_directory / "missing.json", self.controller_path, self.controller)
        other = self.root / "analysis.json"
        other.write_text(self.analysis_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "requires one canonical"):
            replay.require_valid_replay1_analysis(other, self.controller_path, self.controller)

    def test_stale_trace_hash_and_mismatched_controller_or_target_fail(self) -> None:
        self.trace_path.write_bytes(b'{"event":"new trace"}\n')
        with self.assertRaisesRegex(RuntimeError, "stale, mismatched, invalid"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, self.controller)

    def test_analysis_older_than_final_controller_is_stale(self) -> None:
        controller_mtime = self.controller_path.stat().st_mtime_ns
        os.utime(self.analysis_path, ns=(controller_mtime - 1, controller_mtime - 1))
        with self.assertRaisesRegex(RuntimeError, "predates the final controller"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, self.controller)

        self.write_analysis()
        wrong_controller = self.analysis_record()
        wrong_controller["controller_path"] = str(self.root / "other-controller.json")
        self.write_analysis(wrong_controller)
        with self.assertRaisesRegex(RuntimeError, "stale, mismatched, invalid"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, self.controller)

    def test_run_and_analyzer_source_bindings_must_match_current_evidence(self) -> None:
        other_run = dict(self.controller, run_id="step11d3b-baseline-02-fixture")
        with self.assertRaisesRegex(RuntimeError, "trace path is missing, stale, or mismatched"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, other_run)

        stale_source = self.analysis_record()
        stale_source["analysis_source_sha256"] = "0" * 64
        self.write_analysis(stale_source)
        with self.assertRaisesRegex(RuntimeError, "stale, mismatched, invalid"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, self.controller)

        wrong_target = self.analysis_record()
        wrong_target["target_pid"] = self.target_pid + 1
        self.write_analysis(wrong_target)
        with self.assertRaisesRegex(RuntimeError, "stale, mismatched, invalid"):
            replay.require_valid_replay1_analysis(
                self.analysis_path, self.controller_path, self.controller)

    def test_r5_and_unsupported_classifications_block_replay2(self) -> None:
        for replay_class in ("R5", "T8", "R6", None):
            with self.subTest(replay_class=replay_class):
                self.write_analysis(self.analysis_record(replay_class))
                with self.assertRaisesRegex(RuntimeError, "R5, or unsupported"):
                    replay.require_valid_replay1_analysis(
                        self.analysis_path, self.controller_path, self.controller)

    def test_invalid_analysis_integrity_or_hold_blocks_even_with_r1_label(self) -> None:
        for key in ("sequence_integrity_ok", "accepted_identity_continuity_ok",
                    "finder_full_cover_hold_complete", "controller_technical_valid"):
            with self.subTest(key=key):
                record = self.analysis_record()
                record[key] = False
                self.write_analysis(record)
                with self.assertRaisesRegex(RuntimeError, "R5, or unsupported"):
                    replay.require_valid_replay1_analysis(
                        self.analysis_path, self.controller_path, self.controller)

    def test_run2_analysis_gate_precedes_finder_and_observer_setup(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        call_lines = {
            node.func.id: node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {"require_valid_replay1", "fresh_replay_admission", "finder_pid"}
        }
        mkdir_line = next(
            node.lineno for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "mkdir")
        self.assertLess(call_lines["require_valid_replay1"], call_lines["fresh_replay_admission"])
        self.assertLess(call_lines["require_valid_replay1"], call_lines["finder_pid"])
        self.assertLess(call_lines["require_valid_replay1"], mkdir_line)
        self.assertIn("--replay1-analysis", source)


if __name__ == "__main__":
    unittest.main()
