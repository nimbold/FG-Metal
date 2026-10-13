from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import coverer_sequence as controller


def base_sample(stamp: int, *, eligible: bool = True, owner_pid: int = 2201,
                window_id: int = 91) -> dict:
    return {
        "event": "sample",
        "sampledAtMonotonicNS": stamp,
        "canonicalUncoveredEligible": eligible,
        "restoredEligible": eligible,
        "target": {"ownerPID": owner_pid, "windowID": window_id, "titleMatched": True},
    }


def wine_sample(stamp: int, sequence: int, *, eligible: bool = True,
                owner_pid: int = 2201, telemetry_stamp: int | None = None,
                fresh: bool = True) -> dict:
    row = base_sample(stamp, eligible=eligible, owner_pid=owner_pid)
    row["windowTelemetryExpected"] = True
    row["inProcessTelemetry"] = {
        "available": True,
        "fresh": fresh,
        "telemetrySequence": sequence,
        "timestampMachNS": stamp - 10_000_000 if telemetry_stamp is None else telemetry_stamp,
    }
    return row


class LifecycleControllerTests(unittest.TestCase):
    def test_uncovered_and_restored_samples_require_the_expected_title(self) -> None:
        sample = base_sample(1_000_000_000)
        self.assertTrue(controller.canonical_sample(sample))
        self.assertTrue(controller.restored_sample(sample))
        sample["target"]["titleMatched"] = False
        self.assertFalse(controller.canonical_sample(sample))
        self.assertFalse(controller.restored_sample(sample))

    def test_stable_state_requires_three_samples_across_two_seconds(self) -> None:
        samples = [base_sample(1_000_000_000 + i * 500_000_000) for i in range(5)]
        streak = controller.current_qualifying_streak(samples, controller.canonical_sample)
        self.assertIsNotNone(streak)
        self.assertEqual(streak["sample_count"], 5)
        self.assertEqual(streak["stable_duration_ns"], 2_000_000_000)

    def test_stable_interval_accepts_a_2_001_second_interval_at_0_1_2_plus_cadence(self) -> None:
        samples = [wine_sample(10_000_000_000, 4),
                   wine_sample(11_000_000_000, 5),
                   wine_sample(12_001_000_000, 6)]
        streak = controller.current_qualifying_streak(samples, controller.canonical_sample,
                                                      expected_target_pid=2201)
        self.assertIsNotNone(streak)
        self.assertEqual(streak["stable_duration_ns"], 2_001_000_000)

    def test_stable_interval_does_not_round_1_999_seconds_up(self) -> None:
        samples = [wine_sample(10_000_000_000, 4),
                   wine_sample(11_000_000_000, 5),
                   wine_sample(11_999_000_000, 6)]
        self.assertIsNone(controller.current_qualifying_streak(
            samples, controller.canonical_sample, expected_target_pid=2201))

    def test_1_986948458_second_pair_cannot_attain_but_later_sample_can_prove_two_seconds(self) -> None:
        start = 10_000_000_000
        samples = [wine_sample(start, 10),
                   wine_sample(start + 1_000_000_000, 11),
                   wine_sample(start + 1_986_948_458, 12)]
        self.assertIsNone(controller.current_qualifying_streak(
            samples, controller.canonical_sample, expected_target_pid=2201))
        samples.append(wine_sample(start + 2_001_000_000, 13))
        streak = controller.current_qualifying_streak(
            samples, controller.canonical_sample, expected_target_pid=2201)
        self.assertIsNotNone(streak)
        self.assertEqual(streak["stable_duration_ns"], 2_001_000_000)

    def test_qualifying_invalid_qualifying_resets_the_two_second_timer(self) -> None:
        samples = [wine_sample(10_000_000_000, 4),
                   wine_sample(11_000_000_000, 5),
                   wine_sample(11_500_000_000, 6, eligible=False),
                   wine_sample(12_000_000_000, 7),
                   wine_sample(13_000_000_000, 8),
                   wine_sample(13_999_000_000, 9)]
        self.assertIsNone(controller.current_qualifying_streak(
            samples, controller.canonical_sample, expected_target_pid=2201))

    def test_stale_and_nonadvancing_telemetry_are_rejected(self) -> None:
        stale = [wine_sample(10_000_000_000, 4),
                 wine_sample(11_000_000_000, 5, fresh=False),
                 wine_sample(12_001_000_000, 6)]
        self.assertIsNone(controller.current_qualifying_streak(
            stale, controller.canonical_sample, expected_target_pid=2201))

        nonadvancing = [wine_sample(10_000_000_000, 4, telemetry_stamp=10_000_000_000),
                        wine_sample(11_000_000_000, 4, telemetry_stamp=10_000_000_000),
                        wine_sample(12_000_000_000, 4, telemetry_stamp=10_000_000_000)]
        self.assertIsNone(controller.current_qualifying_streak(
            nonadvancing, controller.canonical_sample, expected_target_pid=2201))

    def test_sample_gap_above_limit_and_target_ownership_mismatch_fail_closed(self) -> None:
        gap = [wine_sample(10_000_000_000, 4),
               wine_sample(11_500_000_001, 5),
               wine_sample(12_000_000_000, 6)]
        self.assertIsNone(controller.current_qualifying_streak(
            gap, controller.canonical_sample, expected_target_pid=2201))

        wrong_owner = [wine_sample(10_000_000_000, 4),
                       wine_sample(11_000_000_000, 5),
                       wine_sample(12_001_000_000, 6, owner_pid=2202)]
        self.assertIsNone(controller.current_qualifying_streak(
            wrong_owner, controller.canonical_sample, expected_target_pid=2201))

        wrong_window = [wine_sample(10_000_000_000, 4),
                        wine_sample(11_000_000_000, 5),
                        {**wine_sample(12_001_000_000, 6),
                         "target": {"ownerPID": 2201, "windowID": 92, "titleMatched": True}}]
        self.assertIsNone(controller.current_qualifying_streak(
            wrong_window, controller.canonical_sample,
            expected_target_pid=2201, expected_window_id=91))

    def test_action_phase_uses_one_bounded_boundary_sample_without_extending_duration(self) -> None:
        start, end = 10_000_000_000, 30_000_000_000
        samples = [wine_sample(stamp, 100 + index)
                   for index, stamp in enumerate(range(start, end + 1, 500_000_000))]
        samples.append(wine_sample(end + 12_000_000, 142))
        evidence = controller.timeline_evidence(
            samples, start_ns=start, end_ns=end,
            predicate=controller.canonical_sample,
            observation_grace_ns=controller.STATE_OBSERVATION_GRACE_NS,
            require_boundary_sample=True,
        )
        self.assertTrue(evidence["pass"], evidence)
        self.assertEqual(evidence["action_phase_duration_ns"], 20_000_000_000)
        self.assertEqual(evidence["observation_end_monotonic_ns"], end + 12_000_000)
        self.assertTrue(evidence["final_boundary_sample_observed"])
        self.assertTrue(controller.proof_within_action_phase(
            proof_ns=end, start_ns=start, end_ns=end))
        self.assertFalse(controller.proof_within_action_phase(
            proof_ns=end + 12_000_000, start_ns=start, end_ns=end))

        outside_grace = [row for row in samples if row["sampledAtMonotonicNS"] < end]
        outside_grace.append(wine_sample(end + controller.STATE_OBSERVATION_GRACE_NS + 1, 143))
        rejected = controller.timeline_evidence(
            outside_grace, start_ns=start, end_ns=end,
            predicate=controller.canonical_sample,
            observation_grace_ns=controller.STATE_OBSERVATION_GRACE_NS,
            require_boundary_sample=True,
        )
        self.assertFalse(rejected["pass"])

    def test_restoration_attainment_must_precede_nominal_action_end_not_grace_end(self) -> None:
        start, end = 40_000_000_000, 60_000_000_000
        self.assertTrue(controller.proof_within_action_phase(
            proof_ns=end, start_ns=start, end_ns=end))
        self.assertFalse(controller.proof_within_action_phase(
            proof_ns=end + controller.STATE_OBSERVATION_GRACE_NS,
            start_ns=start, end_ns=end))
        self.assertFalse(controller.proof_within_action_phase(
            proof_ns=start, start_ns=start, end_ns=end))

    def test_calibration_state_machine_requires_cover_action_and_covered_attainment(self) -> None:
        machine = controller.CalibrationStateMachine()
        with self.assertRaisesRegex(RuntimeError, "WAIT_UNCOVERED -> RESTORE_ACTION_ISSUED"):
            machine.transition("RESTORE_ACTION_ISSUED", monotonic_ns=1)

        machine = controller.CalibrationStateMachine()
        machine.transition("UNCOVERED_ATTAINED", monotonic_ns=1)
        with self.assertRaisesRegex(RuntimeError, "UNCOVERED_ATTAINED -> RESTORE_ACTION_ISSUED"):
            machine.transition("RESTORE_ACTION_ISSUED", monotonic_ns=2)
        machine.transition("COVER_ACTION_ISSUED", monotonic_ns=3)
        with self.assertRaisesRegex(RuntimeError, "COVER_ACTION_ISSUED -> RESTORE_ACTION_ISSUED"):
            machine.transition("RESTORE_ACTION_ISSUED", monotonic_ns=4)

    def test_valid_uncovered_covered_restored_sequence_completes(self) -> None:
        machine = controller.CalibrationStateMachine()
        for timestamp, state in enumerate((
                "UNCOVERED_ATTAINED", "COVER_ACTION_ISSUED", "COVERED_ATTAINED",
                "RESTORE_ACTION_ISSUED", "RESTORED_ATTAINED", "COMPLETE"), start=1):
            machine.transition(state, monotonic_ns=timestamp)
        self.assertEqual(machine.state, "COMPLETE")
        self.assertEqual([item["state"] for item in machine.history], [
            "WAIT_UNCOVERED", "UNCOVERED_ATTAINED", "COVER_ACTION_ISSUED",
            "COVERED_ATTAINED", "RESTORE_ACTION_ISSUED", "RESTORED_ATTAINED", "COMPLETE",
        ])

    def test_phase_reducer_counts_each_wine_api_origin_once(self) -> None:
        pid = 40750
        token = "a" * 32
        base = {"host_pid": pid, "target_unix_pid": pid,
                "experiment_token": token, "trace_role": "target"}
        rows = [
            {**base, "origin": "WINE_SOURCE", "event": "vkAcquireNextImageKHR_begin",
             "ts_mach_ns": 10_000_000_000},
            {**base, "origin": "MVK_BOUNDARY", "event": "vkAcquireNextImageKHR_begin",
             "ts_mach_ns": 10_000_000_001},
            {**base, "origin": "WINE_SOURCE", "event": "vkQueueSubmit2_end",
             "ts_mach_ns": 10_000_000_100, "duration_ns": 12_000_000},
            {**base, "origin": "MVK_BOUNDARY", "event": "vkQueueSubmit2_end",
             "ts_mach_ns": 10_000_000_101, "duration_ns": 12_000_001},
            {**base, "origin": "LAYER_SOURCE", "event": "next_drawable_end",
             "ts_mach_ns": 10_000_000_200, "duration_ns": 10_000_000},
            {**base, "origin": "LAYER_SOURCE", "event": "presented_callback",
             "ts_mach_ns": 10_000_000_300, "presented_time_mach_ns": 10_000_000_000},
        ]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.jsonl"
            trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            metrics = controller.summarize_trace_interval(
                trace, kind="wine", start_ns=10_000_000_000,
                end_ns=11_000_000_000, target_pid=pid, experiment_token=token)
        self.assertEqual(metrics["acquire_count"], 1)
        self.assertEqual(metrics["acquired_fps"], 1.0)
        self.assertEqual(metrics["submit_ms"]["count"], 1)
        self.assertEqual(metrics["next_drawable_ms"]["count"], 1)
        self.assertEqual(metrics["callback_count"], 1)

    def test_invalid_latest_sample_breaks_current_streak(self) -> None:
        samples = [base_sample(1_000_000_000 + i * 500_000_000) for i in range(5)]
        samples.append(base_sample(3_500_000_000, eligible=False))
        self.assertIsNone(controller.current_qualifying_streak(samples, controller.canonical_sample))

    def test_exact_coverage_binds_pid_window_rank_display_and_occlusion(self) -> None:
        sample = {
            "targetKind": "wine",
            "windowTelemetryExpected": True,
            "target": {
                "ownerPID": 2201,
                "windowID": 92,
                "titleMatched": True,
                "onScreen": True,
                "processAlive": True,
                "processIdentityMatches": True,
                "windowIdentityAmbiguous": False,
                "rankFrontToBack": 4,
                "rankSource": "onScreenWindowList",
                "covererOnExpectedDisplay": True,
            },
            "neutralCovererProcesses": [{
                "pid": 3302,
                "sha256": controller.EXPECTED_COVERER_SHA256,
                "sha256MatchesPinnedCoverer": True,
            }],
            "neutralCovererWindows": [{
                "ownerPID": 3302,
                "windowID": 93,
                "onScreen": True,
                "rankSource": "onScreenWindowList",
                "fullyContainsTarget": True,
                "alpha": 1.0,
                "rankFrontToBack": 1,
            }],
            "inProcessTelemetry": {
                "available": True,
                "fresh": True,
                "windowOccluded": True,
                "visibleBitSet": False,
            },
            "allWindowListQuerySucceeded": True,
            "onScreenWindowListQuerySucceeded": True,
            "covererProcessEnumerationSucceeded": True,
        }
        self.assertTrue(controller.exact_covered_sample(
            sample, target_pid=2201, target_window_id=92,
            coverer_pid=3302, coverer_window_id=93, telemetry_expected=True,
        ))
        sample["neutralCovererWindows"][0]["rankFrontToBack"] = 5
        self.assertFalse(controller.exact_covered_sample(
            sample, target_pid=2201, target_window_id=92,
            coverer_pid=3302, coverer_window_id=93, telemetry_expected=True,
        ))

    def test_native_coverage_uses_external_identity_without_wine_telemetry(self) -> None:
        sample = {
            "targetKind": "native",
            "windowTelemetryExpected": False,
            "target": {
                "ownerPID": 2201, "windowID": 92, "onScreen": True,
                "titleMatched": True,
                "processAlive": True, "processIdentityMatches": True,
                "windowIdentityAmbiguous": False, "rankFrontToBack": 4,
                "rankSource": "onScreenWindowList",
                "covererOnExpectedDisplay": True,
            },
            "neutralCovererProcesses": [{
                "pid": 3302, "sha256": controller.EXPECTED_COVERER_SHA256,
                "sha256MatchesPinnedCoverer": True,
            }],
            "neutralCovererWindows": [{
                "ownerPID": 3302, "windowID": 93, "onScreen": True,
                "rankSource": "onScreenWindowList",
                "fullyContainsTarget": True, "alpha": 1.0, "rankFrontToBack": 1,
            }],
            "allWindowListQuerySucceeded": True,
            "onScreenWindowListQuerySucceeded": True,
            "covererProcessEnumerationSucceeded": True,
        }
        self.assertTrue(controller.exact_covered_sample(
            sample, target_pid=2201, target_window_id=92,
            coverer_pid=3302, coverer_window_id=93, telemetry_expected=False,
        ))
        sample["neutralCovererWindows"][0].pop("windowID")
        self.assertFalse(controller.exact_covered_sample(
            sample, target_pid=2201, target_window_id=92,
            coverer_pid=3302, coverer_window_id=93, telemetry_expected=False,
        ))

    def test_covered_sample_requires_a_live_identity_verified_target(self) -> None:
        sample = {
            "targetKind": "native", "windowTelemetryExpected": False,
            "target": {
                "ownerPID": 2201, "windowID": 92, "onScreen": True,
                "titleMatched": True,
                "processAlive": True, "processIdentityMatches": True,
                "windowIdentityAmbiguous": False, "rankFrontToBack": 4,
                "rankSource": "onScreenWindowList", "covererOnExpectedDisplay": True,
            },
            "neutralCovererProcesses": [{
                "pid": 3302, "sha256": controller.EXPECTED_COVERER_SHA256,
                "sha256MatchesPinnedCoverer": True,
            }],
            "neutralCovererWindows": [{
                "ownerPID": 3302, "windowID": 93, "onScreen": True,
                "rankSource": "onScreenWindowList", "fullyContainsTarget": True,
                "alpha": 1.0, "rankFrontToBack": 1,
            }],
            "allWindowListQuerySucceeded": True,
            "onScreenWindowListQuerySucceeded": True,
            "covererProcessEnumerationSucceeded": True,
        }
        arguments = {
            "target_pid": 2201, "target_window_id": 92,
            "coverer_pid": 3302, "coverer_window_id": 93,
            "telemetry_expected": False,
        }
        self.assertTrue(controller.exact_covered_sample(sample, **arguments))
        for key, value in (("titleMatched", False), ("processAlive", False),
                           ("processIdentityMatches", False)):
            with self.subTest(key=key):
                original = sample["target"][key]
                sample["target"][key] = value
                self.assertFalse(controller.exact_covered_sample(sample, **arguments))
                sample["target"][key] = original

    def test_timeline_fails_on_missing_or_invalid_public_samples(self) -> None:
        start, end = 2_000_000_000, 5_000_000_000
        samples = [base_sample(stamp) for stamp in range(start, end + 1, 500_000_000)]
        good = controller.timeline_evidence(samples, start_ns=start, end_ns=end,
                                            predicate=controller.canonical_sample)
        self.assertTrue(good["pass"])
        samples[3]["canonicalUncoveredEligible"] = False
        bad = controller.timeline_evidence(samples, start_ns=start, end_ns=end,
                                           predicate=controller.canonical_sample)
        self.assertFalse(bad["pass"])

    def test_wine_stability_requires_advancing_sequence_and_timestamp(self) -> None:
        samples = []
        for index in range(7):
            stamp = 1_000_000_000 + index * 500_000_000
            telemetry_stamp = 1_000_000_000 + (index // 2) * 1_000_000_000
            samples.append({
                **base_sample(stamp), "windowTelemetryExpected": True,
                "inProcessTelemetry": {
                    "available": True, "fresh": True,
                    "telemetrySequence": 20 + index // 2,
                    "timestampMachNS": telemetry_stamp,
                },
            })
        progress = controller.telemetry_progress_evidence(
            samples, start_ns=1_000_000_000, end_ns=4_000_000_000)
        self.assertTrue(progress["verified"], progress)

        for sample in samples:
            sample["inProcessTelemetry"]["telemetrySequence"] = 20
            sample["inProcessTelemetry"]["timestampMachNS"] = 1_000_000_000
        stale = controller.telemetry_progress_evidence(
            samples, start_ns=1_000_000_000, end_ns=4_000_000_000)
        self.assertFalse(stale["verified"])
        timeline = controller.timeline_evidence(
            samples, start_ns=1_000_000_000, end_ns=4_000_000_000,
            predicate=controller.canonical_sample)
        self.assertFalse(timeline["pass"])

    def test_signature_thresholds_separate_healthy_and_fast_zero(self) -> None:
        self.assertEqual(controller.classify_metrics({
            "acquired_fps": 59.0, "displayed_fps": 58.2, "raw_zero_fraction": 0.01,
        }), "HEALTHY_REFRESH")
        self.assertEqual(controller.classify_metrics({
            "acquired_fps": 119.0, "displayed_fps": 0.0, "raw_zero_fraction": 1.0,
        }), "FAST_ZERO")
        self.assertEqual(controller.classify_metrics({
            "acquired_fps": 75.0, "displayed_fps": 19.0, "raw_zero_fraction": 0.2,
        }), "DIFFERENT_OR_UNHEALTHY")


if __name__ == "__main__":
    unittest.main()
