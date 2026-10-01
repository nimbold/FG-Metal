"""Adversarial metric and compatibility checks for tools/benchmark/bench.py."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH_PATH = ROOT / "tools" / "benchmark" / "bench.py"
SPEC = importlib.util.spec_from_file_location("framegen_bench", BENCH_PATH)
assert SPEC and SPEC.loader
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def solid(width: int, height: int, value: int) -> bytes:
    return bytes([value]) * (width * height * 3)


def masks(count: int) -> dict[str, bytes]:
    mask = bytes([255]) * count
    return {label: mask for label in bench.MASK_LABELS}


def _comparison_fixture() -> dict:
    targets = [
        {"source_pair": [0, 2], "timestamp_ns": 1, "t": 0.5},
        {"source_pair": [2, 4], "timestamp_ns": 3, "t": 0.5},
    ]
    regions = {}
    for region in bench.REGIONS:
        regions[region] = {
            "temporal": {
                "frame_to_frame_residual": {
                    "per_transition_p95": {"p95": 0.0},
                    "worst_pixel_per_transition": {"max": 0.0},
                    "transitions": [
                        {"timestamps_ns": [0, 1], "continuity_group": 0,
                         "mean": 0.0, "p95": 0.0, "max": 0.0},
                    ],
                },
                "high_frequency_temporal_flicker": {
                    # The first window is deliberately much worse. The candidate
                    # adds damage only to the second, non-worst timestamp window.
                    "per_window_p95": {"p95": 10.0},
                    "worst_pixel_per_window": {"max": 10.0},
                    "windows": [
                        {"timestamps_ns": [0, 1, 2], "continuity_group": 0,
                         "mean": 10.0, "p95": 10.0, "max": 10.0,
                         "edge_mean": 0.0, "edge_p95": 0.0, "edge_max": 0.0},
                        {"timestamps_ns": [1, 2, 3], "continuity_group": 0,
                         "mean": 0.0, "p95": 0.0, "max": 0.0,
                         "edge_mean": 0.0, "edge_p95": 0.0, "edge_max": 0.0},
                    ],
                },
                "edge_flicker": {
                    "per_window_p95": {"p95": 0.0},
                    "worst_pixel_per_window": {"max": 0.0},
                },
            },
            "frame_max_pixel_error_norm": {"max": 0.9},
            "pixels_over_5pct_error_fraction_per_target": {"max": 0.001},
            "pixels_over_1pct_error_count_per_target": {"max": 1},
            "pixels_over_5pct_error_count_per_target": {"max": 1},
            "ssim_7x7_masked_luminance": {"mean": 1.0, "p50": 1.0},
            "perceptual_similarity": {"mean": 1.0, "min": 1.0},
            "cielab_delta_e76_mean": {"p95": 0.0},
            "edge_location": {
                "symmetric_chamfer_norm": {"mean": 0.0},
                "worst_target_chamfer_norm": 0.0,
                "minimum_target_precision_at_1px": 1.0,
                "minimum_target_recall_at_1px": 1.0,
            },
        }
    quality_frames = []
    for index, target in enumerate(targets):
        target_regions = {}
        for region in bench.REGIONS:
            # Target 0 already contains one severe pixel. Target 1 has the
            # same maximum error but no severe pixels until the candidate.
            target_regions[region] = {
                "frame_max_pixel_error_norm": 0.9 if index == 0 else 0.2,
                "pixels_over_1pct_error_count": 1 if index == 0 else 0,
                "pixels_over_5pct_error_count": 1 if index == 0 else 0,
                "edge_location": {
                    "symmetric_chamfer_norm": 0.0,
                    "precision_at_1px": 1.0,
                    "recall_at_1px": 1.0,
                },
            }
        quality_frames.append({**target, "regions": target_regions})
    return {
        "schema_version": 1,
        "metric_contract_version": bench.METRIC_CONTRACT_VERSION,
        "compatibility": {"corpus": {"content_sha256": "fixture"},
                          "configuration": {"target_timeline": targets}},
        "configuration": {"target_timeline": targets},
        "backend": {"id": "test", "kind": "test"},
        "quality_by_region": regions,
        "quality_frames": quality_frames,
        "performance": {
            "completion_latency_ms": {"p50": 1.0, "p95": 1.0, "p99": 1.0},
            "interpolation_gpu_time_ms": {"p50": 1.0, "p95": 1.0, "p99": 1.0},
            "cpu_submit_overhead_ms": {"p95": 1.0, "p99": 1.0},
            "gpu_allocated_bytes_current_max": 100,
            "gpu_allocated_bytes_peak_max": 100,
            "runner_peak_resident_bytes_max": 100,
            "generated_frame_deadline_miss_rate": 0.0,
            "source_frame_fps_impact_percent": 0.0,
        },
    }


class BenchmarkMetricTests(unittest.TestCase):
    def test_single_pixel_hud_defect_reaches_worst_pixel_regression_gate(self) -> None:
        width, height = 8, 8
        reference = solid(width, height, 80)
        candidate = bytearray(reference)
        candidate[(3 * width + 4) * 3] = 255
        hud = bytes([255]) * (width * height)
        metric = bench.evaluate_frame(reference, bytes(candidate), hud, width, height)
        self.assertGreater(metric["frame_max_pixel_error_norm"], 0.005)
        self.assertTrue(bench._compare_values(
            0.0, metric["frame_max_pixel_error_norm"], "lower", 0.005))

    def test_added_sparse_hud_damage_is_caught_even_when_worst_error_is_unchanged(self) -> None:
        width, height = 128, 72
        reference = solid(width, height, 80)
        baseline = bytearray(reference)
        candidate = bytearray(reference)
        first = (4 * width + 4) * 3
        second = (4 * width + 5) * 3
        baseline[first:first + 3] = bytes((180, 80, 80))
        candidate[first:first + 3] = bytes((180, 80, 80))
        candidate[second:second + 3] = bytes((180, 80, 80))
        hud = bytes([255]) * (width * height)
        baseline_metric = bench.evaluate_frame(reference, bytes(baseline), hud, width, height)
        candidate_metric = bench.evaluate_frame(reference, bytes(candidate), hud, width, height)
        self.assertEqual(
            baseline_metric["frame_max_pixel_error_norm"],
            candidate_metric["frame_max_pixel_error_norm"])
        self.assertTrue(bench._compare_values(
            baseline_metric["pixels_over_5pct_error_count"],
            candidate_metric["pixels_over_5pct_error_count"], "lower", 0.0))

    def test_alternating_one_frame_shimmer_raises_high_frequency_flicker(self) -> None:
        timeline = []
        for index in range(5):
            reference = solid(1, 1, 100)
            output = solid(1, 1, 130 if index % 2 else 100)
            timeline.append({
                "time_ns": index * 16_666_667,
                "rgb": output,
                "reference_rgb": reference,
                "masks": masks(1),
                "continuity_group": 0,
            })
        metrics = bench.temporal_metrics(timeline, 1, 1, 60.0)["hud"]
        self.assertGreater(
            metrics["high_frequency_temporal_flicker"]["per_window_mean"]["mean"], 0.0)
        self.assertGreater(
            metrics["high_frequency_temporal_flicker"]["worst_pixel_per_window"]["max"], 0.0)

    def test_new_shimmer_window_is_retained_even_when_another_window_is_worse(self) -> None:
        baseline, candidate = [], []
        for index, values in enumerate(((100, 100), (160, 160), (100, 100), (100, 110))):
            timestamp = index * 16_666_667
            reference = solid(1, 1, 100)
            for timeline, output_value in ((baseline, values[0]), (candidate, values[1])):
                timeline.append({
                    "time_ns": timestamp, "rgb": solid(1, 1, output_value),
                    "reference_rgb": reference, "masks": masks(1),
                    "continuity_group": 0,
                })
        base_metrics = bench.temporal_metrics(baseline, 1, 1, 60.0)["all"]
        cand_metrics = bench.temporal_metrics(candidate, 1, 1, 60.0)["all"]
        base_windows = base_metrics["high_frequency_temporal_flicker"]["windows"]
        cand_windows = cand_metrics["high_frequency_temporal_flicker"]["windows"]
        self.assertEqual(
            base_metrics["high_frequency_temporal_flicker"]["worst_pixel_per_window"]["max"],
            cand_metrics["high_frequency_temporal_flicker"]["worst_pixel_per_window"]["max"])
        self.assertTrue(bench._compare_values(
            base_windows[1]["mean"], cand_windows[1]["mean"], "lower", 0.0))

    def test_compare_catches_new_hud_pixel_and_nonworst_shimmer_window(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        hud_summary = candidate["quality_by_region"]["hud"]
        hud_frame = candidate["quality_frames"][1]["regions"]["hud"]
        hud_frame["pixels_over_1pct_error_count"] = 1
        hud_frame["pixels_over_5pct_error_count"] = 1
        flicker = hud_summary["temporal"]["high_frequency_temporal_flicker"]
        second_window = flicker["windows"][1]
        second_window.update({"mean": 1.0, "p95": 1.0, "max": 1.0})

        # Region summaries and the already-worse first window remain unchanged.
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["pixels_over_5pct_error_count_per_target"],
            hud_summary["pixels_over_5pct_error_count_per_target"])
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["pixels_over_5pct_error_fraction_per_target"],
            hud_summary["pixels_over_5pct_error_fraction_per_target"])
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["temporal"]["high_frequency_temporal_flicker"]["worst_pixel_per_window"],
            flicker["worst_pixel_per_window"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            baseline_path = temporary / "baseline.json"
            candidate_path = temporary / "candidate.json"
            output_path = temporary / "comparison.json"
            summary_path = temporary / "comparison.md"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            status = bench._compare(argparse.Namespace(
                baseline=str(baseline_path), candidate=str(candidate_path),
                output=str(output_path), summary=str(summary_path)))

            self.assertEqual(status, 1)
            comparison = json.loads(output_path.read_text(encoding="utf-8"))
            regressions = comparison["regressions"]
            self.assertTrue(any(
                row["scope"] == "quality_target" and row["region"] == "hud"
                and row["target"]["timestamp_ns"] == 3
                and row["metric"] == "pixels_over_5pct_error_count"
                for row in regressions))
            self.assertTrue(any(
                row["scope"] == "temporal_window" and row["region"] == "hud"
                and row["timestamps_ns"] == [1, 2, 3]
                for row in regressions))

    def test_steady_one_pixel_edge_shift_has_spatial_edge_error(self) -> None:
        width = height = 16
        reference = bytearray(width * height * 3)
        candidate = bytearray(width * height * 3)
        for y in range(3, 13):
            reference[(y * width + 6) * 3:(y * width + 7) * 3] = b"\xff" * 3
            candidate[(y * width + 7) * 3:(y * width + 8) * 3] = b"\xff" * 3
        result = bench.edge_similarity(
            bench._edge_map(bytes(reference), width, height),
            bench._edge_map(bytes(candidate), width, height),
            bytes([255]) * (width * height), width, height)
        self.assertGreater(result["symmetric_chamfer_norm"], 0.0)

    def test_identical_frames_produce_finite_json_metrics(self) -> None:
        image = solid(8, 8, 127)
        result = bench.evaluate_frame(image, image, bytes([255]) * 64, 8, 8)
        encoded = json.dumps(result, allow_nan=False)
        self.assertIn('"psnr_db": 120.0', encoded)
        self.assertTrue(result["psnr_perfect_match"])

    def test_comparison_rejects_different_corpus_or_target_timestamps(self) -> None:
        baseline = {
            "compatibility": {
                "corpus": {"content_sha256": "same-corpus"},
                "configuration": {"target_timeline": [{"timestamp_ns": 10}]},
            }
        }
        different_corpus = {
            "compatibility": {
                "corpus": {"content_sha256": "different-corpus"},
                "configuration": {"target_timeline": [{"timestamp_ns": 10}]},
            }
        }
        different_time = {
            "compatibility": {
                "corpus": {"content_sha256": "same-corpus"},
                "configuration": {"target_timeline": [{"timestamp_ns": 11}]},
            }
        }
        with self.assertRaisesRegex(ValueError, "incompatible"):
            bench.require_compatible(baseline, different_corpus)
        with self.assertRaisesRegex(ValueError, "incompatible"):
            bench.require_compatible(baseline, different_time)

    def test_result_validation_rejects_missing_required_regions(self) -> None:
        result = {
            "schema_version": 1,
            "metric_contract_version": bench.METRIC_CONTRACT_VERSION,
            "compatibility": {},
            "quality_by_region": {"all": {}},
            "performance": {},
        }
        with self.assertRaisesRegex(ValueError, "every required quality region"):
            bench._validate_result(result, "candidate")

    def test_cut_and_loading_metadata_skip_intervals_and_reset_temporal_history(self) -> None:
        corpus = bench.validate_corpus(
            ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion" / "manifest.json")
        corpus["by_index"][1]["boundary_before"] = "scene_cut"
        corpus["by_index"][4]["interpolable"] = False
        plan = bench._target_plan(corpus, None)
        selected_pairs = {tuple(item["source_pair"]) for item in plan}
        self.assertNotIn((0, 2), selected_pairs)
        self.assertNotIn((2, 4), selected_pairs)
        self.assertIn((6, 8), selected_pairs)
        self.assertEqual({item["continuity_group"] for item in plan if item["source_pair"] == [6, 8]}, {3})

        timeline = []
        for index, group in enumerate((0, 0, 1, 1)):
            timeline.append({
                "time_ns": index * 16_666_667,
                "rgb": solid(1, 1, 100 + index),
                "reference_rgb": solid(1, 1, 100 + index),
                "masks": masks(1), "continuity_group": group,
            })
        temporal = bench.temporal_metrics(timeline, 1, 1, 60.0)["all"]
        transitions = temporal["frame_to_frame_residual"]["transitions"]
        self.assertEqual(
            [row["timestamps_ns"] for row in transitions],
            [[0, 16_666_667], [33_333_334, 50_000_001]])


if __name__ == "__main__":
    unittest.main()
