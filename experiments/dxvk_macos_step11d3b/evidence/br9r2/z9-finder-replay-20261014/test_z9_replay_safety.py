"""Deterministic budget and fail-closed evidence tests for Z9 replay."""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from typing import Any

LOCAL_DIR = Path(__file__).resolve().parent
EXPERIMENT = LOCAL_DIR.parents[2]
sys.path.insert(0, str(LOCAL_DIR))
sys.path.insert(0, str(EXPERIMENT.parents[0] / "storage"))

import storage_policy
import z9_replay_budget as budget
from z9_replay_safety import (
    covered_sampling_summary, desktop_preflight_eligible, identity_stability_summary,
    renderer_healthy, replay_execution_path_eligible, same_query_cover_state,
    transition_bracket_summary, _renderer_stream_coverage,
)


class Z9ReplayControllerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controller_path = LOCAL_DIR / "z9_finder_replay.py"
        cls.controller_source = cls.controller_path.read_text(encoding="utf-8")
        cls.controller_ast = ast.parse(cls.controller_source)

    def test_appkit_visible_mask_accepts_8194_and_rejects_8192(self) -> None:
        assignment = next(
            node for node in ast.walk(self.controller_ast)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "NS_WINDOW_OCCLUSION_STATE_VISIBLE"
                    for target in node.targets))
        self.assertEqual(ast.unparse(assignment.value), "1 << 1")
        visible_mask = eval(
            compile(ast.Expression(assignment.value), str(self.controller_path), "eval"),
            {"__builtins__": {}},
        )
        self.assertEqual(visible_mask, 2)

        telemetry_function = next(
            node for node in self.controller_ast.body
            if isinstance(node, ast.FunctionDef) and node.name == "telemetry_state")
        namespace: dict[str, Any] = {
            "Any": Any,
            "NS_WINDOW_OCCLUSION_STATE_VISIBLE": visible_mask,
        }
        isolated_module = ast.Module(body=[telemetry_function], type_ignores=[])
        exec(compile(isolated_module, str(self.controller_path), "exec"), namespace)
        telemetry_state = namespace["telemetry_state"]
        self.assertTrue(telemetry_state({"window_occlusion_state": 8194})["visible_bit_set"])
        self.assertFalse(telemetry_state({"window_occlusion_state": 8192})["visible_bit_set"])

    def test_finder_treatment_actions_follow_successful_action_free_measured_pre(self) -> None:
        main_function = next(
            node for node in self.controller_ast.body
            if isinstance(node, ast.FunctionDef) and node.name == "main")
        loops = [node for node in ast.walk(main_function) if isinstance(node, ast.While)]
        canonical_wait = next(
            node for node in loops if "precondition_deadline" in ast.unparse(node.test))
        measured_pre = next(
            node for node in loops if "pre_end_deadline" in ast.unparse(node.test))

        def treatment_action_calls(node: ast.AST) -> list[ast.Call]:
            names = {
                "finder_activate_request", "finder_window_creation",
                "finder_exact_window_raise_request", "finder_disjoint_stage_request",
                "finder_full_cover_position_request", "finder_restore_staged_bounds_request",
                "finder_close_exact_window_request", "cleanup_close_only_exact_new_finder_window",
            }
            return [
                call for call in ast.walk(node)
                if isinstance(call, ast.Call) and call.args
                and isinstance(call.args[0], ast.Constant)
                and call.args[0].value in names
            ]

        for name, loop in (("canonical PRE acquisition", canonical_wait),
                           ("measured PRE", measured_pre)):
            self.assertEqual(treatment_action_calls(loop), [], name)
            loop_source = ast.unparse(loop)
            self.assertNotIn("make new Finder window", loop_source, name)

        successful_pre_line = next(
            node.lineno for node in ast.walk(main_function)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "measured_pre_completed" for target in node.targets)
            and isinstance(node.value, ast.Constant) and node.value.value is True)
        action_calls = treatment_action_calls(main_function)
        self.assertTrue(action_calls)
        self.assertGreater(min(call.lineno for call in action_calls), successful_pre_line)

        creation_line = self.controller_source.index("set z9Window to make new Finder window")
        completion_line = self.controller_source.index("measured_pre_completed = True")
        self.assertGreater(creation_line, completion_line)


class Z9ReplayBudgetTests(unittest.TestCase):
    def test_replay_execution_path_and_replay2_interlock_fail_closed(self) -> None:
        valid = {"status": "RUNNER_COMPLETED"}
        self.assertTrue(replay_execution_path_eligible(valid))
        self.assertTrue(replay_execution_path_eligible({
            **valid, "transient_output_budget_guard": {"triggered": False},
        }))
        self.assertFalse(replay_execution_path_eligible({
            **valid, "transient_output_budget_guard": {"triggered": True},
        }))
        self.assertFalse(replay_execution_path_eligible({
            **valid, "transient_output_budget_guard": "malformed",
        }))
        self.assertFalse(replay_execution_path_eligible({
            **valid, "controller_error": "RuntimeError: evidence guard",
        }))
        self.assertFalse(replay_execution_path_eligible({
            **valid, "status": "INVALID_OR_INCOMPLETE",
        }))

    def test_nominal_replay_keeps_the_predeclared_20_40_20_sequence(self) -> None:
        self.assertEqual(budget.MEASURED_PRE_SECONDS, 20)
        self.assertEqual(budget.MEASURED_COVER_SECONDS, 40)
        self.assertEqual(budget.MEASURED_RESTORED_SECONDS, 20)
        self.assertEqual(
            budget.MEASURED_PRE_SECONDS + budget.MEASURED_COVER_SECONDS
            + budget.MEASURED_RESTORED_SECONDS,
            80,
        )

    def test_full_pre_acquisition_and_shutdown_lifetimes_are_bounded(self) -> None:
        detail = budget.reservation_breakdown()
        self.assertEqual(detail["maximum_lifecycle_seconds"]["pre_acquisition"], 180)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["runner_startup"], 1200)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["prefixlease_setup"], 60)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["target_handoff"], 1200)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["canonical_pre_acquisition"], 180)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["target_output"], 1_540)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["windowserver_collector"], 2_900)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["observer"], 1_801.5)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["shutdown_cleanup"], 45)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["controller_sum"], 4_906.5)
        self.assertEqual(detail["maximum_lifecycle_seconds"]["controller_cap"], 5_000)

    def test_collector_budget_includes_every_row_at_the_accepted_50ms_cadence(self) -> None:
        cap = budget.reservation_breakdown()["cover_candidate_cap"]
        self.assertEqual(cap["samples_per_second"], 20)
        self.assertEqual(cap["maximum_rows_per_sample"], 64)
        self.assertEqual(cap["maximum_serialized_row_bytes"], 192)
        self.assertEqual(cap["maximum_sample_overhead_bytes"], 512)
        self.assertEqual(cap["maximum_bytes"], (64 * 192 + 512) * 20 * 2_900)

    def test_worst_case_trace_growth_and_result_file_limit_are_derived(self) -> None:
        detail = budget.reservation_breakdown()
        self.assertEqual(detail["projected_stream_bytes"]["target host trace"], 1_855_546_538)
        self.assertEqual(detail["projected_stream_bytes"]["app trace"], 1_004_025_301)
        self.assertEqual(detail["run_result_file_limit_bytes"], 617 * budget.MIB)
        self.assertEqual(detail["storage_policy_margin_separate"], True)
        self.assertEqual(detail["collector_jsonl_output_limit_bytes"], 896 * budget.MIB)
        self.assertEqual(detail["required_persistent_output_bytes"], 4_974_444_544)

    def test_signed_and_transient_caps_are_independent_and_cover_total_reservation(self) -> None:
        detail = budget.reservation_breakdown()
        self.assertEqual(detail["signed_run_output_limit_bytes"], 3_897_556_992)
        self.assertEqual(detail["transient_evidence_limit_bytes"], 1_076_887_552)
        self.assertEqual(
            detail["required_persistent_output_bytes"],
            budget.round_up_mib(
                detail["signed_run_output_limit_bytes"]
                + detail["transient_evidence_limit_bytes"]),
        )
        self.assertGreaterEqual(
            detail["collector_jsonl_output_limit_bytes"],
            detail["projected_stream_bytes"]["base WindowServer inventory"]
            + detail["cover_candidate_cap"]["maximum_bytes"],
        )

    def test_exact_derived_reservation_and_policy_margin_admit_at_both_boundaries(self) -> None:
        peak = budget.PREFIX_BYTES + budget.persistent_output_reservation_bytes()
        peak += storage_policy.admission_uncertainty_margin_bytes(peak)
        result = storage_policy.projected_storage_admission(
            current_free_bytes=50 * storage_policy.GIB + peak,
            current_scoped_bytes=55 * storage_policy.GIB - peak,
            peak_additional_bytes=peak,
        )
        self.assertTrue(result["admitted"], result["blockers"])
        self.assertEqual(result["projected_free_bytes"], 50 * storage_policy.GIB)
        self.assertEqual(result["projected_scoped_bytes"], 55 * storage_policy.GIB)

    def test_one_byte_output_reservation_overflow_fails_scope_admission(self) -> None:
        peak = budget.PREFIX_BYTES + budget.persistent_output_reservation_bytes() + 1
        peak += storage_policy.admission_uncertainty_margin_bytes(peak)
        result = storage_policy.projected_storage_admission(
            current_free_bytes=80 * storage_policy.GIB,
            current_scoped_bytes=55 * storage_policy.GIB - peak + 1,
            peak_additional_bytes=peak,
        )
        self.assertFalse(result["admitted"])
        self.assertIn("projected scoped FG-Metal usage exceeds the 55 GiB ceiling",
                      result["blockers"])

    def test_projected_free_one_byte_below_50_gib_fails(self) -> None:
        peak = budget.PREFIX_BYTES + budget.persistent_output_reservation_bytes()
        peak += storage_policy.admission_uncertainty_margin_bytes(peak)
        result = storage_policy.projected_storage_admission(
            current_free_bytes=50 * storage_policy.GIB + peak - 1,
            current_scoped_bytes=40 * storage_policy.GIB,
            peak_additional_bytes=peak,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_free_bytes"], 50 * storage_policy.GIB - 1)

    def test_projected_scope_one_byte_above_55_gib_fails(self) -> None:
        peak = budget.PREFIX_BYTES + budget.persistent_output_reservation_bytes()
        peak += storage_policy.admission_uncertainty_margin_bytes(peak)
        result = storage_policy.projected_storage_admission(
            current_free_bytes=80 * storage_policy.GIB,
            current_scoped_bytes=55 * storage_policy.GIB - peak + 1,
            peak_additional_bytes=peak,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_scoped_bytes"], 55 * storage_policy.GIB + 1)

    def test_covered_interval_requires_350_complete_samples_and_100ms_max_gap(self) -> None:
        identity = (77, 880)
        start = 10_000_000_000
        end = start + 20_000_000_000

        def sample(index: int) -> dict:
            return {
                "event": "sample", "sample_index": index,
                "target_unix_pid": 77,
                "run_id": "run", "experiment_token": "token",
                "sample_started_monotonic_ns": start + index * 50_000_000,
                "sample_started_mach_absolute_ns": start + index * 50_000_000,
                "all_window_query_succeeded": True,
                "onscreen_window_query_succeeded": True,
                "onscreen_windows": [{
                    "owner_pid": 77, "cg_window_id": 990,
                    "bounds_cg": {"x": 0, "y": 0, "width": 640, "height": 392},
                    "rank_onscreen_windows": 2, "onscreen": True,
                }],
                "same_query_cover_evidence": {
                    "target_join_status": "PASS", "cover_evidence_complete": True,
                    "opaque_full_cover_state": "COVERED",
                    "target_cg_window_id": 990, "target_onscreen_rank": 2,
                    "opaque_full_cover_rows": [[identity[1], identity[0], 0, 0, 640, 392, 0, 1.0, 1]],
                },
            }

        good = [sample(i) for i in range(400)]
        result = covered_sampling_summary(
            good, start_monotonic_ns=start, end_monotonic_ns=end,
            required_cover_identity=identity, expected_target_pid=77,
            expected_run_id="run", expected_token="token")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["sample_count"], 400)
        self.assertEqual(result["maximum_sample_start_gap_ns"], 50_000_000)
        too_sparse = [sample(i) for i in range(400) if i not in {100, 101}]
        result = covered_sampling_summary(
            too_sparse, start_monotonic_ns=start, end_monotonic_ns=end,
            required_cover_identity=identity, expected_target_pid=77,
            expected_run_id="run", expected_token="token")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["maximum_sample_start_gap_ns"], 150_000_000)

    def test_i1_identity_stability_uses_a_real_two_second_sample_span(self) -> None:
        now = 20_000_000_000

        def point(stamp: int, window_id: int = 400) -> dict:
            return {
                "ts_mach_ns": stamp, "window_associated": True,
                "layer_id": 100, "window_view_id": 200,
                "window_lookup_path": "direct_delegate", "window_id": window_id,
                "window_number": 400,
            }

        four_points = [point(now - 1_500_000_000 + index * 500_000_000)
                       for index in range(4)]
        result = identity_stability_summary(four_points, now_mach_ns=now)
        self.assertFalse(result["stable"])
        self.assertEqual(result["interval_ns"], 1_500_000_000)

        five_points = [point(now - 2_000_000_000 + index * 500_000_000)
                       for index in range(5)]
        result = identity_stability_summary(five_points, now_mach_ns=now)
        self.assertTrue(result["stable"], result)
        self.assertEqual(result["point_count"], 5)
        self.assertEqual(result["interval_ns"], 2_000_000_000)

        changed = [point(now - 2_000_000_000 + index * 500_000_000,
                         401 if index == 3 else 400) for index in range(5)]
        result = identity_stability_summary(changed, now_mach_ns=now)
        self.assertFalse(result["stable"])

    def test_same_query_cover_unknown_and_unexpected_id_fail_closed(self) -> None:
        def sample(state: str, rows: list) -> dict:
            return {
                "target_unix_pid": 77,
                "onscreen_windows": [{
                    "owner_pid": 77, "cg_window_id": 990,
                    "bounds_cg": {"x": 0, "y": 0, "width": 640, "height": 392},
                    "rank_onscreen_windows": 2, "onscreen": True,
                }],
                "same_query_cover_evidence": {
                    "target_join_status": "PASS", "cover_evidence_complete": state != "UNKNOWN",
                    "opaque_full_cover_state": state, "target_cg_window_id": 990,
                    "target_onscreen_rank": 2, "opaque_full_cover_rows": rows,
                },
            }

        clear = sample("CLEAR", [])
        unknown = sample("UNKNOWN", [])
        unexpected = sample("COVERED", [[881, 77, 0, 0, 640, 392, 0, 1.0, 1]])
        self.assertEqual(same_query_cover_state(clear)["status"], "CLEAR")
        self.assertEqual(same_query_cover_state(unknown)["status"], "UNKNOWN")
        self.assertEqual(same_query_cover_state(
            unexpected, allowed_cover_identity=(77, 880))["status"], "UNEXPECTED_COVER")

    def test_desktop_preflight_rejects_lock_opaque_cover_and_incomplete_query(self) -> None:
        good = {
            "status": "PASS", "evidence_complete": True,
            "all_window_query_succeeded": True, "onscreen_window_query_succeeded": True,
            "session_login_complete": True, "session_on_console": True,
            "session_user_id": 501, "expected_user_id": 501,
            "frontmost_application_name": "Finder",
            "lock_screen_name_suspected": False, "lock_screen_owner_suspected": False,
            "expected_target_inside_display": True, "opaque_full_cover_candidates": [],
            "unclassifiable_onscreen_window_count": 0, "candidate_overflow": False,
        }
        self.assertTrue(desktop_preflight_eligible(good, 501))
        locked = {**good, "lock_screen_owner_suspected": True}
        covered = {**good, "opaque_full_cover_candidates": [[1, 2, 0, 0, 640, 392, 0, 1.0, 0]]}
        incomplete = {**good, "onscreen_window_query_succeeded": False}
        self.assertFalse(desktop_preflight_eligible(locked, 501))
        self.assertFalse(desktop_preflight_eligible(covered, 501))
        self.assertFalse(desktop_preflight_eligible(incomplete, 501))

    @staticmethod
    def healthy_trace_events(start: int = 10_000_000_000,
                             *, pid: int = 77, token: str = "token") -> list[dict]:
        events = []
        event_id = 1

        def common(origin: str, event: str, stamp: int, **extra: object) -> dict:
            nonlocal event_id
            row = {
                "origin": origin, "event": event, "event_id": event_id,
                "ts_mach_ns": stamp, "ts_clock_domain": "mach_absolute_time_ns",
                "target_unix_pid": pid, "host_pid": pid, "trace_role": "target",
                "experiment_token": token,
            }
            row.update(extra)
            event_id += 1
            return row

        for index in range(120):
            stamp = start + 10_000_000 + index * 16_666_667
            sequence = index + 1
            events.append(common("WINE_SOURCE", "vkAcquireNextImageKHR_begin", stamp,
                                 wine_call_seq=sequence))
            events.append(common("WINE_SOURCE", "vkAcquireNextImageKHR_end", stamp + 1000,
                                 wine_call_seq=sequence, vk_result=0))
            events.append(common("WINE_SOURCE", "vkQueueSubmit2_end", stamp + 2000,
                                 wine_call_seq=sequence, vk_result=0))
            events.append(common("LAYER_SOURCE", "presented_callback", stamp + 3000,
                                 driver_present_seq=sequence,
                                 presentation_clock_domain="mach_absolute_time_ns",
                                 callback_delivery_clock_domain="mach_absolute_time_ns",
                                 presented_time_mach_ns=stamp + 3000,
                                 is_displayed=True))
            events.append(common("LAYER_SOURCE", "next_drawable_begin", stamp + 4000,
                                 next_drawable_sequence=sequence,
                                 call_begin_mach_ns=stamp + 4000))
        return events

    def test_renderer_pre_gate_accepts_only_integral_target_bound_healthy_refresh(self) -> None:
        events = self.healthy_trace_events()
        result = renderer_healthy(events, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertTrue(result["healthy"], result)
        self.assertTrue(result["stream_coverage_pass"], result)
        duplicate = [*events, dict(events[-2], event_id=999_999)]
        result = renderer_healthy(duplicate, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertFalse(result["healthy"])
        self.assertIn("present callback sequence is duplicated", result["integrity_errors"])

    def test_renderer_pre_gate_rejects_refresh_burst_followed_by_idle_tail(self) -> None:
        events = self.healthy_trace_events()
        for row in events:
            sequence = row.get("wine_call_seq")
            if type(sequence) is not int:
                continue
            burst_stamp = 10_010_000_000 + (sequence - 1) * 1_000_000
            event = row.get("event")
            if event == "vkAcquireNextImageKHR_begin":
                row["ts_mach_ns"] = burst_stamp
            elif event == "vkAcquireNextImageKHR_end":
                row["ts_mach_ns"] = burst_stamp + 1_000
            elif event == "vkQueueSubmit2_end":
                row["ts_mach_ns"] = burst_stamp + 2_000
            elif event == "presented_callback":
                row["ts_mach_ns"] = burst_stamp + 3_000
                row["presented_time_mach_ns"] = burst_stamp + 3_000

        result = renderer_healthy(events, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertEqual(result["acquire_rate_per_second"], 60)
        self.assertEqual(result["submit_rate_per_second"], 60)
        self.assertEqual(result["display_rate_per_second"], 60)
        self.assertFalse(result["stream_coverage_pass"], result)
        self.assertFalse(result["renderer_stream_coverage"]["successful_acquisitions"]["continuous"])
        self.assertFalse(result["healthy"], result)

    def test_renderer_pre_gate_rejects_next_drawable_only_gap_hidden_by_p90(self) -> None:
        events = [row for row in self.healthy_trace_events()
                  if not (row.get("event") == "next_drawable_begin"
                          and 31 <= row.get("next_drawable_sequence", 0) <= 40)]
        result = renderer_healthy(events, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertGreaterEqual(result["next_drawable_gap_median_ns"], 14_000_000)
        self.assertLessEqual(result["next_drawable_gap_p90_ns"], 22_000_000)
        self.assertTrue(result["renderer_stream_coverage"]["successful_acquisitions"]["continuous"])
        self.assertFalse(result["renderer_stream_coverage"]["next_drawable_starts"]["continuous"])
        self.assertFalse(result["healthy"], result)

    def test_renderer_stream_coverage_accepts_exact_100ms_boundary(self) -> None:
        start = 10_000_000_000
        stamps = [start, start + 100_000_000,
                  start + 200_000_000, start + 300_000_000]
        result = _renderer_stream_coverage(stamps, start, start + 300_000_000)
        self.assertTrue(result["continuous"], result)
        self.assertEqual(result["maximum_inter_event_gap_ns"], 100_000_000)

    def test_unknown_callback_outcome_and_wrong_target_fail_pre_gate(self) -> None:
        events = self.healthy_trace_events()
        callback = next(row for row in events if row.get("event") == "presented_callback")
        callback["is_displayed"] = False
        result = renderer_healthy(events, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertFalse(result["healthy"])
        self.assertIn("callback presentation timestamp and display flag disagree",
                      result["integrity_errors"])
        events = self.healthy_trace_events()
        events[0]["experiment_token"] = "stale"
        result = renderer_healthy(events, 12_000_000_000,
                                  expected_pid=77, expected_token="token")
        self.assertFalse(result["healthy"])

    def test_transition_bracket_requires_adjacent_successful_same_query_samples(self) -> None:
        def sample(index: int, state: str) -> dict:
            rows = [] if state == "CLEAR" else [[880, 77, 0, 0, 640, 392, 0, 1.0, 1]]
            return {
                "event": "sample", "sample_index": index,
                "target_unix_pid": 77,
                "run_id": "run", "experiment_token": "token",
                "sample_started_monotonic_ns": index * 50_000_000,
                "sample_started_mach_absolute_ns": index * 50_000_000,
                "onscreen_query_started_mach_absolute_ns": index * 50_000_000,
                "onscreen_query_completed_mach_absolute_ns": index * 50_000_000 + 1_000_000,
                "all_window_query_succeeded": True,
                "onscreen_window_query_succeeded": True,
                "onscreen_windows": [{
                    "owner_pid": 77, "cg_window_id": 990,
                    "bounds_cg": {"x": 0, "y": 0, "width": 640, "height": 392},
                    "rank_onscreen_windows": 2, "onscreen": True,
                }],
                "same_query_cover_evidence": {
                    "target_join_status": "PASS", "cover_evidence_complete": True,
                    "opaque_full_cover_state": "CLEAR" if state == "CLEAR" else "COVERED",
                    "target_cg_window_id": 990, "target_onscreen_rank": 2,
                    "opaque_full_cover_rows": rows,
                },
            }

        rows = [sample(0, "CLEAR"), sample(1, "CLEAR"),
                sample(2, "EXPECTED_COVER"), sample(3, "EXPECTED_COVER")]
        result = transition_bracket_summary(
            rows, from_state="CLEAR", to_state="EXPECTED_COVER",
            action_end_mach_ns=75_000_000, expected_target_pid=77,
            expected_run_id="run", expected_token="token",
            allowed_cover_identity=(77, 880))
        self.assertEqual(result["status"], "PASS")
        rows[2]["onscreen_window_query_succeeded"] = False
        result = transition_bracket_summary(
            rows, from_state="CLEAR", to_state="EXPECTED_COVER",
            action_end_mach_ns=75_000_000, expected_target_pid=77,
            expected_run_id="run", expected_token="token",
            allowed_cover_identity=(77, 880))
        self.assertEqual(result["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
