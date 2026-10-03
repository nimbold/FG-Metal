"""Focused compatibility and accounting checks for the DXMT run analyzer."""

from __future__ import annotations

import importlib.util
import csv
import gzip
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
ANALYZER_PATH = ROOT / "tools" / "dxmt" / "analyze_framegen_run.py"
SPEC = importlib.util.spec_from_file_location("dxmt_run_analyzer", ANALYZER_PATH)
assert SPEC and SPEC.loader
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def row(event: str, **values: object) -> dict[str, str]:
    base = {
        "event": event,
        "presenter_id": "1",
        "epoch": "1",
        "presentation_seq": "0",
        "tick_id": "0",
        "kind": "0",
        "source_id": "0",
        "pair_a_id": "0",
        "pair_b_id": "0",
        "actual_presented_ns": "0",
        "callback_ns": "0",
        "target_ns": "0",
        "detail": "",
    }
    base.update({key: str(value) for key, value in values.items()})
    return base


class AnalyzeFramegenRunTests(unittest.TestCase):
    def test_present_hresult_counts_failures_by_high_bit_and_keeps_success_statuses(self) -> None:
        app = [
            row("present", present_hr="0x00000000", elapsed_us=0),
            row("present", present_hr="0x087A0001", elapsed_us=1),
            row("present", present_hr="0x80004005", elapsed_us=2),
            # Signed HRESULT input must normalize to the same high-bit failure.
            row("present", present_hr="-2147024891", elapsed_us=3),
            # Decimal strings with leading zeroes are accepted HRESULT values.
            row("present", present_hr="00000001", elapsed_us=4),
        ]

        summary = analyzer.summarize_rows([], app)

        self.assertIn("app_present_errors=2", summary)
        self.assertIn("app_present_nonzero_success_statuses=2", summary)
        self.assertIn("app_present_occluded_statuses=1", summary)

    def test_old_schema_uses_positive_source_display_as_terminal_proxy(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_accepted", source_id=2),
            row("source_displayed", kind=1, source_id=1, presentation_seq=1,
                tick_id=1, actual_presented_ns=100, target_ns=100),
            # Re-presenting a source is a cadence event, not a second terminal.
            row("source_displayed", kind=1, source_id=1, presentation_seq=2,
                tick_id=2, actual_presented_ns=200, target_ns=200),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("source_terminal_mode=positive_source_display_fallback", summary)
        self.assertIn("missing_terminal_ids=1", summary)
        self.assertIn("duplicate_terminal_ids=0", summary)
        self.assertIn("lost_source_ids=1", summary)
        self.assertIn("lost_source_id_list=1:2", summary)
        self.assertIn("repeated_source_events=1", summary)

    def test_explicit_terminals_report_missing_duplicate_and_orphan_ids(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_accepted", source_id=2),
            row("source_terminal_presented", source_id=1, actual_presented_ns=100,
                detail="first submission"),
            row("source_terminal_presented", source_id=1, actual_presented_ns=101,
                detail="first submission"),
            row("source_terminal_presented", source_id=3, actual_presented_ns=102),
            row("source_state", source_id=1,
                detail="SOURCE_PRESENT_PENDING->SOURCE_PRESENTED"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("source_terminal_mode=explicit", summary)
        self.assertIn("missing_terminal_ids=1", summary)
        self.assertIn("duplicate_terminal_ids=1", summary)
        self.assertIn("orphan_terminal_ids=1", summary)
        self.assertIn("lost_source_id_list=1:2", summary)
        self.assertIn("SOURCE_PRESENT_PENDING->SOURCE_PRESENTED:1", summary)
        self.assertIn("first submission:2", summary)

    def test_generic_source_terminal_separates_safe_and_unsafe_outcomes(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_accepted", source_id=2),
            row("source_accepted", source_id=3),
            row("source_accepted", source_id=4),
            row("source_accepted", source_id=5),
            row("source_accepted", source_id=6),
            row("source_terminal", source_id=1, detail="renderer_presented",
                actual_presented_ns=0),
            # The bridge logs positive drawable feedback on source_displayed;
            # its separate source_terminal summary row carries no timestamp.
            row("source_displayed", kind=1, source_id=1, presentation_seq=1,
                tick_id=1, actual_presented_ns=100),
            # Ordinary fallback is a safe terminal even without renderer feedback.
            row("source_terminal", source_id=2, detail="ordinary_fallback",
                actual_presented_ns=0),
            # A renderer outcome counts only with positive presentedTime.
            row("source_terminal", source_id=3, detail="renderer_presented",
                actual_presented_ns=0),
            row("source_terminal", source_id=4, detail="snapshot_failed"),
            row("source_terminal", source_id=5, detail="destroyed_before_presentation"),
            row("source_safety_violation", source_id=6, detail="source abandoned"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("source_terminal_mode=explicit", summary)
        self.assertIn("source_terminal_records=5 safe_terminal_ids=2 unsafe_terminal_ids=3", summary)
        self.assertIn("source_safety_violation_ids=1", summary)
        self.assertIn("missing_terminal_ids=1", summary)
        self.assertIn("lost_source_ids=4", summary)
        self.assertIn("lost_source_id_list=1:3,1:4,1:5,1:6", summary)
        self.assertIn("unsafe_terminal_id_list=1:3,1:4,1:5", summary)

    def test_dxmt_transfer_and_completed_unconfirmed_submission_are_accounted(self) -> None:
        native = [
            row("source_ownership_transfer", source_id=7,
                detail="Framegen owns immutable SourceEscrow after containing command buffer commit"),
            row("source_terminal", source_id=7,
                detail="display_submitted_unconfirmed: presentedTime did not confirm the already-submitted source"),
            row("presented_time_zero", kind=1, source_id=7,
                presentation_seq=4, tick_id=11, actual_presented_ns=0),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("accepted_source_ids=1 acceptance_mode=ownership_acceptance_event", summary)
        self.assertIn("missing_terminal_ids=0", summary)
        self.assertIn("safe_terminal_ids=1", summary)
        self.assertIn("submitted_unconfirmed_accepted_ids=1", summary)
        self.assertIn("lost_source_ids=0", summary)
        self.assertIn("accepted_source_zero_feedback=1", summary)

    def test_immediate_triples_and_zero_feedback_overlap_are_exact(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_accepted", source_id=2),
            row("source_accepted", source_id=3),
            row("source_displayed", kind=1, source_id=1, presentation_seq=1,
                tick_id=1, actual_presented_ns=100, target_ns=100),
            row("generated_displayed", kind=2, source_id=2, pair_a_id=1,
                pair_b_id=2, presentation_seq=2, tick_id=2,
                actual_presented_ns=200, target_ns=200),
            row("source_displayed", kind=1, source_id=2, presentation_seq=3,
                tick_id=3, actual_presented_ns=300, target_ns=300),
            row("source_displayed", kind=1, source_id=3, presentation_seq=4,
                tick_id=4, actual_presented_ns=400, target_ns=400),
            row("presentation_dropped", kind=1, source_id=2, presentation_seq=5,
                tick_id=5, present_submit_ns=500,
                detail="Metal drawable presentedTime=0; associated frame was dropped"),
            # An unrelated pre-present drop is not a presentedTime==0 result.
            row("presentation_dropped", kind=1, source_id=3, presentation_seq=6,
                tick_id=6, detail="worker encoding reached the render deadline"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("immediate_A_G_B_triples=1 unbracketed_G=0", summary)
        self.assertIn("zero_presentedTime=1 by_kind=source:1", summary)
        self.assertIn("nearest_unmatched_endpoints_with_zero_feedback=0", summary)
        self.assertIn("immediate_missing_endpoints_with_zero_feedback=0", summary)

    def test_immediate_pair_reports_when_both_positive_endpoints_are_missing(self) -> None:
        native = [
            row("generated_displayed", kind=2, source_id=2, pair_a_id=1,
                pair_b_id=2, presentation_seq=2, tick_id=2,
                actual_presented_ns=200, target_ns=200),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("immediate_missing_A=1 immediate_missing_B=1 immediate_missing_both=1", summary)

    def test_missing_endpoint_and_zero_feedback_are_reported_as_overlap(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_accepted", source_id=2),
            row("source_accepted", source_id=3),
            row("source_displayed", kind=1, source_id=1, presentation_seq=1,
                tick_id=1, actual_presented_ns=100, target_ns=100),
            row("generated_displayed", kind=2, source_id=2, pair_a_id=1,
                pair_b_id=2, presentation_seq=2, tick_id=2,
                actual_presented_ns=200, target_ns=200),
            row("source_displayed", kind=1, source_id=3, presentation_seq=3,
                tick_id=3, actual_presented_ns=300, target_ns=300),
            row("presentation_dropped", kind=1, source_id=2, presentation_seq=4,
                tick_id=4, present_submit_ns=400,
                detail="Metal drawable presentedTime=0; associated frame was dropped"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("immediate_A_G_B_triples=0 unbracketed_G=1", summary)
        self.assertIn("immediate_missing_A=0 immediate_missing_B=1", summary)
        self.assertIn("nearest_unmatched_endpoints_with_zero_feedback=1", summary)
        self.assertIn("immediate_missing_endpoints_with_zero_feedback=1", summary)
        self.assertIn("lost_source_ids=1", summary)

    def test_optional_stage_fields_are_joined_across_async_events(self) -> None:
        native = [
            row("source_accepted", source_id=1),
            row("source_terminal_presented", source_id=1, presentation_seq=1,
                tick_id=1, actual_presented_ns=12_000_000, target_ns=12_000_000,
                callback_ns=1_000_000, worker_wake_ns=2_000_000,
                mutex_wait_start_ns=3_000_000, mutex_acquired_ns=5_000_000,
                presenter_encode_start_ns=6_000_000,
                presenter_encode_end_ns=9_000_000, gpu_submit_ns=10_000_000,
                present_call_ns=10_500_000, feedback_ns=12_500_000),
            row("presentation_gpu_completed", source_id=1, presentation_seq=1,
                tick_id=1, gpu_complete_ns=11_000_000, gpu_status="completed"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("callback_to_worker_wake_ms: n=1 p50=1.000", summary)
        self.assertIn("device_mutex_wait_ms: n=1 p50=2.000", summary)
        self.assertIn("presenter_encode_ms: n=1 p50=3.000", summary)
        self.assertIn("gpu_submit_to_completion_ms: n=1 p50=1.000", summary)
        self.assertIn("callback_to_present_call_ms: n=1 p50=9.500", summary)
        self.assertIn("present_call_to_feedback_ms: n=1 p50=2.000", summary)

    def test_step10b4_balances_tick_terminals_and_separates_feedback_outcomes(self) -> None:
        native = [
            row("presentation_stages", tick_id=11, epoch=7, kind=1,
                terminal_class="source_submitted", queue_depth=1,
                callback_ns=100_000_000, selection_ns=101_000_000,
                present_call_ns=102_000_000, command_buffer_commit_ns=103_000_000),
            row("presentation_stages", tick_id=12, epoch=7, kind=2,
                terminal_class="generated_submitted", queue_depth=2,
                callback_ns=116_000_000, selection_ns=117_000_000,
                present_call_ns=118_000_000, command_buffer_commit_ns=119_000_000),
            row("real_presentation_submitted", tick_id=11, epoch=7, presentation_seq=1),
            row("generated_presentation_submitted", tick_id=12, epoch=7, presentation_seq=2),
            row("presentation_feedback_result", tick_id=11, epoch=7, presentation_seq=1,
                kind=1, source_id=42, callback_ns=100_000_000,
                present_call_ns=102_000_000, drawable_feedback_ns=120_000_000,
                actual_presented_ns=0, gpu_complete_ns=121_000_000,
                gpu_status="completed", terminal_class="zero_feedback"),
            row("presentation_feedback_result", tick_id=12, epoch=7, presentation_seq=2,
                kind=2, source_id=44, pair_a_id=43, pair_b_id=44,
                callback_ns=116_000_000, present_call_ns=118_000_000,
                drawable_feedback_ns=136_000_000, actual_presented_ns=140_000_000,
                gpu_complete_ns=137_000_000, gpu_status="completed",
                source_b_scheduled_ns=130_000_000,
                terminal_class="positive_feedback"),
            row("display_callback_summary", detail=(
                "valid_callback_count=2;terminal_count=2;queue_overflow_count=0;"
                "drawable_outstanding_high_water=2")),
        ]
        app = [
            row("present", elapsed_us=0, present_hr=0),
            row("present", elapsed_us=1_000_000, present_hr=0),
        ]

        summary = analyzer.summarize_rows(native, app)

        self.assertIn("callback_tick_ledger: status=PASS", summary)
        self.assertIn("callback_count=2 callback_hz=2.000 terminal_count=2 callback_terminal_delta=0", summary)
        self.assertIn("duplicate_tick_terminals=0", summary)
        self.assertIn("gpu_completed=2 gpu_failed=0 positive_presentedTime=1 zero_presentedTime=1", summary)
        self.assertIn("positive_feedback_hz=1.000 source_submission_hz=1.000 generated_submission_hz=1.000", summary)
        self.assertIn("late_G_feedback_after_source_B_schedule=1", summary)
        self.assertIn("tick_queue_depth: n=2 p50=1.000", summary)

    def test_callback_ledger_uses_latest_cumulative_lifecycle_summary(self) -> None:
        native = [
            row("presentation_stages", tick_id=1, epoch=1,
                terminal_class="source_submitted"),
            row("display_callback_summary",
                detail="valid_callback_count=1;terminal_count=1"),
            row("presentation_stages", tick_id=2, epoch=2,
                terminal_class="source_submitted"),
            row("display_callback_summary",
                detail="valid_callback_count=2;terminal_count=2"),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("callback_tick_ledger: status=PASS callback_count=2", summary)
        self.assertIn("terminal_rows=2 unique_ticks=2 duplicate_tick_terminals=0", summary)

    def test_callback_ledger_mismatch_is_reported_and_cli_fails(self) -> None:
        native = [
            row("presentation_stages", tick_id=1,
                terminal_class="source_submitted"),
            row("display_callback_summary",
                detail="valid_callback_count=2;terminal_count=2"),
        ]
        summary = analyzer.summarize_rows(native, [])
        self.assertIn("callback_tick_ledger: status=MISMATCH", summary)
        self.assertIn("callback_count=2", summary)
        self.assertIn("terminal_rows=1 unique_ticks=1", summary)

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            native_path = temporary / "native.csv.gz"
            app_path = temporary / "app.csv"
            output_path = temporary / "summary.txt"
            native_fields = list(dict.fromkeys(
                key for native_row in native for key in native_row
            ))
            with gzip.open(native_path, "wt", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=native_fields)
                writer.writeheader()
                writer.writerows(native)
            with app_path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=["event", "elapsed_us"])
                writer.writeheader()
            arguments = [
                "analyze_framegen_run.py", str(native_path), str(app_path),
                "--output", str(output_path),
            ]
            with patch.object(sys, "argv", arguments), redirect_stdout(io.StringIO()):
                exit_code = analyzer.main()

            self.assertEqual(exit_code, 1)
            self.assertIn("status=MISMATCH", output_path.read_text())

    def test_generated_feedback_uses_B_display_target_and_separates_display_order(self) -> None:
        native = [
            row("generated_requested", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2),
            row("generated_ready", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2),
            row("generated_selected", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2),
            row("generated_presentation_submitted", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2, presentation_seq=1),
            row("presentation_feedback_result", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2, presentation_seq=1,
                actual_presented_ns=300, target_ns=200,
                source_b_scheduled_ns=150),
            # G feedback landed after B's display target but before B's
            # positively confirmed presentation timestamp.
            row("presentation_feedback_result", kind=1, source_id=2,
                presentation_seq=2, actual_presented_ns=350,
                target_ns=250),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("generated_frame_feedback: requested=1 ready=1 selected=1 submitted=1 positive=1 zero=0 dropped_before_submit_unique_pairs=0", summary)
        self.assertIn("late_G_feedback_after_B_display_target=1", summary)
        self.assertIn("late_G_after_B_positive_feedback=0", summary)
        self.assertIn("positive_generated_timestamp_order=missing_A:1", summary)

    def test_positive_generated_timestamp_order_requires_both_observed_endpoints(self) -> None:
        native = [
            row("presentation_feedback_result", kind=1, source_id=1,
                presentation_seq=1, actual_presented_ns=100, target_ns=90),
            row("presentation_feedback_result", kind=2, source_id=2,
                pair_a_id=1, pair_b_id=2, presentation_seq=2,
                actual_presented_ns=200, target_ns=190),
            row("presentation_feedback_result", kind=1, source_id=2,
                presentation_seq=3, actual_presented_ns=300, target_ns=290),
            row("presentation_feedback_result", kind=1, source_id=10,
                presentation_seq=4, actual_presented_ns=100, target_ns=90),
            row("presentation_feedback_result", kind=1, source_id=11,
                presentation_seq=5, actual_presented_ns=150, target_ns=140),
            row("presentation_feedback_result", kind=2, source_id=11,
                pair_a_id=10, pair_b_id=11, presentation_seq=6,
                actual_presented_ns=200, target_ns=190),
            row("presentation_feedback_result", kind=1, source_id=20,
                presentation_seq=7, actual_presented_ns=150, target_ns=140),
            row("presentation_feedback_result", kind=1, source_id=21,
                presentation_seq=8, actual_presented_ns=250, target_ns=240),
            row("presentation_feedback_result", kind=2, source_id=21,
                pair_a_id=20, pair_b_id=21, presentation_seq=9,
                actual_presented_ns=100, target_ns=90),
            row("presentation_feedback_result", kind=2, source_id=31,
                pair_a_id=30, pair_b_id=31, presentation_seq=10,
                actual_presented_ns=150, target_ns=140),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn(
            "positive_generated_timestamp_order=both_endpoints_positive:3,"
            "between_A_B:1,before_A:1,after_B:1,missing_A:1,missing_B:1,missing_both:1",
            summary,
        )

    def test_zero_feedback_joins_tick_stages_lifecycle_and_circuit(self) -> None:
        native = [
            row("presentation_stages", presentation_seq=0, tick_id=11,
                epoch=7, lifecycle_epoch=7, circuit_state="open",
                callback_ns=1_000_000, worker_wake_ns=2_000_000,
                gpu_status="completed"),
            row("presented_time_zero", presentation_seq=4, tick_id=11,
                epoch=7, kind=1, source_id=42, presented_time_ns=0,
                actual_presented_ns=0),
        ]

        summary = analyzer.summarize_rows(native, [])

        self.assertIn("zero_presentedTime=1 by_kind=source:1", summary)
        self.assertIn("gpu_status=completed:1 circuit_state=open:1", summary)
        self.assertIn("lifecycle_epoch=7:1", summary)
        self.assertIn("zero_feedback_with_stage_records=1", summary)
        self.assertIn("callback_to_worker_wake_ms: n=1 p50=1.000", summary)


if __name__ == "__main__":
    unittest.main()
