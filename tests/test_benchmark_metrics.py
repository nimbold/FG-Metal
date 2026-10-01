"""Adversarial metric and compatibility checks for tools/benchmark/bench.py."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
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


def _sync_sparse_map_statistics(result: dict) -> None:
    """Keep test fixtures self-consistent with the production sparse-map contract."""
    strict_labels = result["corpus"]["strict_pixel_labels"]
    for frame in result["quality_frames"]:
        for label in strict_labels:
            region = frame["regions"][label]
            rows = frame["critical_pixel_errors"][label]
            active_count = region["active_pixel_count"]
            values = [sum(row[1:]) / (3 * 255.0) for row in rows]
            stats = bench._sparse_pixel_distribution(values, active_count)
            region.update({
                "pixel_abs_error_norm_mean": stats["mean"],
                "pixel_abs_error_norm_p95": stats["p95"],
                "frame_max_pixel_error_norm": stats["max"],
                "pixels_over_1pct_error_count": sum(value >= 0.01 for value in values),
                "pixels_over_5pct_error_count": sum(value >= 0.05 for value in values),
            })
            region["pixels_over_1pct_error_fraction"] = (
                region["pixels_over_1pct_error_count"] / active_count)
            region["pixels_over_5pct_error_fraction"] = (
                region["pixels_over_5pct_error_count"] / active_count)
            region["psnr_perfect_match"] = not rows
            region["psnr_db"] = bench._sparse_rgb_map_psnr(rows, active_count)

    fps = result["corpus"]["high_rate_fps"]
    for label in strict_labels:
        summary = result["quality_by_region"][label]
        frames = result["quality_frames"]
        for summary_key, frame_key in (
            ("psnr_db", "psnr_db"),
            ("pixel_abs_error_norm_mean_per_target", "pixel_abs_error_norm_mean"),
            ("pixel_abs_error_norm_p95_per_target", "pixel_abs_error_norm_p95"),
            ("frame_max_pixel_error_norm", "frame_max_pixel_error_norm"),
            ("pixels_over_1pct_error_fraction_per_target", "pixels_over_1pct_error_fraction"),
            ("pixels_over_1pct_error_count_per_target", "pixels_over_1pct_error_count"),
            ("pixels_over_5pct_error_fraction_per_target", "pixels_over_5pct_error_fraction"),
            ("pixels_over_5pct_error_count_per_target", "pixels_over_5pct_error_count"),
        ):
            summary[summary_key] = bench.distribution(
                [frame["regions"][label][frame_key] for frame in frames])

        temporal = summary["temporal"]
        residual = temporal["frame_to_frame_residual"]
        for row in residual["transitions"]:
            interval = (row["timestamps_ns"][1] - row["timestamps_ns"][0]) / (1e9 / fps)
            errors = [value / (3 * 255.0 * interval)
                      for _, value in row["critical_pixel_errors"]]
            stats = bench._sparse_pixel_distribution(errors, row["active_pixel_count"])
            row.update({key: stats[key] for key in ("mean", "p95", "max")})
        flicker = temporal["high_frequency_temporal_flicker"]
        for row in flicker["windows"]:
            color_stats = bench._sparse_pixel_distribution(
                [value for _, value in row["critical_pixel_errors"]],
                row["active_pixel_count"])
            edge_stats = bench._sparse_pixel_distribution(
                [value for _, value in row["critical_edge_errors"]],
                row["active_pixel_count"])
            row.update({key: color_stats[key] for key in ("mean", "p95", "max")})
            row.update({f"edge_{key}": edge_stats[key] for key in ("mean", "p95", "max")})
        for summary_key, value_key in (
            ("per_transition_mean", "mean"),
            ("per_transition_p95", "p95"),
            ("worst_pixel_per_transition", "max"),
        ):
            residual[summary_key] = bench.distribution(
                [row[value_key] for row in residual["transitions"]])
        for summary_key, value_key in (
            ("per_window_mean", "mean"),
            ("per_window_p95", "p95"),
            ("worst_pixel_per_window", "max"),
        ):
            flicker[summary_key] = bench.distribution(
                [row[value_key] for row in flicker["windows"]])
        temporal["edge_flicker"]["per_window_mean"] = bench.distribution(
            [row["edge_mean"] for row in flicker["windows"]])
        temporal["edge_flicker"]["per_window_p95"] = bench.distribution(
            [row["edge_p95"] for row in flicker["windows"]])
        temporal["edge_flicker"]["worst_pixel_per_window"] = bench.distribution(
            [row["edge_max"] for row in flicker["windows"]])
        temporal["temporal_consistency_score"] = max(
            0.0, 1.0 - sum(row["mean"] for row in residual["transitions"])
            / len(residual["transitions"]))


def _comparison_fixture() -> dict:
    def dist(count: int, value: float) -> dict:
        return {"count": count, "mean": value, "min": value,
                "p50": value, "p95": value, "p99": value, "max": value}

    targets = [
        {"source_pair": [0, 2], "target_index": 1, "source_timestamps_ns": [0, 2],
         "timestamp_ns": 1, "t": 0.5, "continuity_group": 0},
        {"source_pair": [2, 4], "target_index": 3, "source_timestamps_ns": [2, 4],
         "timestamp_ns": 3, "t": 0.5, "continuity_group": 0},
    ]
    temporal_timeline = [
        {"timestamp_ns": timestamp, "continuity_group": 0}
        for timestamp in range(5)
    ]
    config = {
        "warmup_iterations": 1, "measured_iterations_per_target": 100,
        "hud_mode": "none", "ui_source": "nearest", "hud_debug": "disabled",
        "temporal_policy": "none", "temporal_debug": "disabled",
        "deadline_ms": 16.666, "backend_timeout_seconds": 600.0,
        "max_analysis_memory_mib": bench.DEFAULT_MAX_ANALYSIS_MEMORY_MIB,
        "estimated_analysis_memory_bytes": 1000, "estimated_result_bytes": 1000,
        "strict_pixel_map_comparisons": 1000,
        "requested_t": None,
        "target_timeline": targets,
        "source_pair_plan": [
            {"source_pair": [0, 2], "source_timestamps_ns": [0, 2],
             "status": "selected", "continuity_group": 0,
             "targets": [{"timestamp_ns": 1, "t": 0.5, "target_index": 1}]},
            {"source_pair": [2, 4], "source_timestamps_ns": [2, 4],
             "status": "selected", "continuity_group": 0,
             "targets": [{"timestamp_ns": 3, "t": 0.5, "target_index": 3}]},
        ],
        "temporal_timeline": temporal_timeline,
        "metric_parameters": {"metric_contract_version": bench.METRIC_CONTRACT_VERSION},
    }
    device = {"id": "test-device-42", "name": "test-gpu"}
    host = {"platform": "test-platform", "system": "test-system", "machine": "test-machine",
            "gpu_device": device}
    corpus = {
        "content_sha256": "a" * 64, "manifest_sha256": "b" * 64,
        "sequence_id": "fixture-sequence", "license": "Apache-2.0",
        "width": 8, "height": 8, "high_rate_fps": 60.0,
        "low_rate_stride_frames": 2, "source_indices": [0, 2, 4],
        "mask_labels": list(bench.MASK_LABELS),
        "strict_pixel_labels": list(bench.MANDATORY_STRICT_PIXEL_LABELS),
        "analytic_provider": None,
        "frame_selection_metadata": [
            {"index": index, "timestamp_ns": index, "segment_id": "segment-0",
             "boundary_before": None, "interpolable": True}
            for index in range(5)
        ],
        "mask_coverage_pixels_by_frame": {
            str(index): {label: 64 for label in bench.MASK_LABELS}
            for index in range(5)
        },
    }
    compatibility = {
        "host": {key: host[key] for key in ("platform", "system", "machine", "gpu_device")},
        "corpus": {key: corpus[key] for key in (
            "content_sha256", "sequence_id", "width", "height", "high_rate_fps",
            "low_rate_stride_frames", "source_indices", "analytic_provider",
            "mask_labels", "strict_pixel_labels", "frame_selection_metadata")},
        "configuration": {key: config[key] for key in (
            "warmup_iterations", "measured_iterations_per_target", "deadline_ms",
            "backend_timeout_seconds", "max_analysis_memory_mib",
            "estimated_analysis_memory_bytes", "estimated_result_bytes",
            "strict_pixel_map_comparisons",
            "target_timeline", "source_pair_plan",
            "temporal_timeline", "metric_parameters")},
    }
    regions = {}
    for region in bench.REGIONS:
        transition_rows = [
            {"timestamps_ns": [timestamp, timestamp + 1], "continuity_group": 0,
             "mean": 0.0, "p95": 0.0, "max": 0.0, "active_pixel_count": 64,
             **({"critical_pixel_errors": []}
                if region in bench.MANDATORY_STRICT_PIXEL_LABELS else {})}
            for timestamp in range(4)
        ]
        flicker_rows = [
            {"timestamps_ns": [timestamp, timestamp + 1, timestamp + 2],
             "continuity_group": 0,
             "mean": 10.0 if timestamp == 0 else 0.0,
             "p95": 10.0 if timestamp == 0 else 0.0,
             "max": 10.0 if timestamp == 0 else 0.0,
             "edge_mean": 0.0, "edge_p95": 0.0, "edge_max": 0.0,
             "active_pixel_count": 64,
             **({"critical_pixel_errors": [], "critical_edge_errors": []}
                if region in bench.MANDATORY_STRICT_PIXEL_LABELS else {})}
            for timestamp in range(3)
        ]
        regions[region] = {
            "psnr_db": dist(2, 30.0),
            "temporal": {
                "frame_to_frame_residual": {
                    "per_transition_mean": dist(4, 0.0),
                    "per_transition_p95": dist(4, 0.0),
                    "worst_pixel_per_transition": dist(4, 0.0),
                    "transitions": transition_rows,
                },
                "temporal_consistency_score": 1.0,
                "high_frequency_temporal_flicker": {
                    # The first window is deliberately much worse. The candidate
                    # adds damage only to the second, non-worst timestamp window.
                    "per_window_mean": bench.distribution([10.0, 0.0, 0.0]),
                    "per_window_p95": bench.distribution([10.0, 0.0, 0.0]),
                    "worst_pixel_per_window": bench.distribution([10.0, 0.0, 0.0]),
                    "windows": flicker_rows,
                },
                "edge_flicker": {
                    "per_window_mean": dist(3, 0.0),
                    "per_window_p95": dist(3, 0.0),
                    "worst_pixel_per_window": dist(3, 0.0),
                },
            },
            "psnr_db": bench.distribution([30.0, 30.0]),
            "pixel_abs_error_norm_mean_per_target": bench.distribution([0.01, 0.005]),
            "pixel_abs_error_norm_p95_per_target": bench.distribution([0.02, 0.01]),
            "frame_max_pixel_error_norm": bench.distribution([0.9, 0.2]),
            "pixels_over_1pct_error_fraction_per_target": bench.distribution([1 / 64, 0.0]),
            "pixels_over_5pct_error_fraction_per_target": bench.distribution([1 / 64, 0.0]),
            "pixels_over_1pct_error_count_per_target": bench.distribution([1, 0]),
            "pixels_over_5pct_error_count_per_target": bench.distribution([1, 0]),
            "sample_count": 2,
            "ssim_7x7_masked_luminance": bench.distribution([1.0, 1.0]),
            "perceptual_similarity": bench.distribution([1.0, 1.0]),
            "cielab_delta_e76_mean": bench.distribution([0.0, 0.0]),
            "edge_location": {
                "symmetric_chamfer_norm": bench.distribution([0.0, 0.0]),
                "precision_at_1px": bench.distribution([1.0, 1.0]),
                "recall_at_1px": bench.distribution([1.0, 1.0]),
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
                "active_pixel_count": 64,
                "psnr_db": 30.0,
                "psnr_perfect_match": False,
                "pixel_abs_error_norm_mean": 0.01 if index == 0 else 0.005,
                "pixel_abs_error_norm_p95": 0.02 if index == 0 else 0.01,
                "pixels_over_1pct_error_fraction": 1 / 64 if index == 0 else 0.0,
                "pixels_over_5pct_error_fraction": 1 / 64 if index == 0 else 0.0,
                "ssim_7x7_masked_luminance": 1.0,
                "perceptual_similarity": 1.0,
                "cielab_delta_e76_mean": 0.0,
                "edge_location": {
                    "symmetric_chamfer_norm": 0.0,
                    "precision_at_1px": 1.0,
                    "recall_at_1px": 1.0,
                },
            }
        quality_frames.append({
            **target, "regions": target_regions,
            "mask_pixel_counts": {label: 64 for label in bench.MASK_LABELS},
            "critical_pixel_errors": {
                label: [] for label in bench.MANDATORY_STRICT_PIXEL_LABELS
            },
        })
    result = {
        "schema_version": 1,
        "metric_contract_version": bench.METRIC_CONTRACT_VERSION,
        "compatibility": compatibility,
        "configuration": config,
        "host": host,
        "corpus": corpus,
        "backend": {"id": "test", "kind": "test", "device": device},
        "quality_by_region": regions,
        "quality_frames": quality_frames,
        "performance": {
            "measurement_samples": 200,
            "expected_measurement_samples": 200,
            "expected_target_runs": 2,
            "interpolation_gpu_time_ms": {
                **dist(200, 1.0), "missing_samples": 0,
            },
            "gpu_memory_current_sample_count": 2,
            "gpu_memory_current_missing_target_runs": 0,
            "gpu_memory_sampled_max_sample_count": 2,
            "gpu_memory_sampled_max_missing_target_runs": 0,
            "runner_resident_memory_sample_count": 2,
            "runner_resident_memory_missing_target_runs": 0,
            "completion_latency_ms": dist(200, 1.0),
            "cpu_submit_overhead_ms": dist(200, 1.0),
            "gpu_allocated_bytes_current_max": 100,
            "gpu_allocated_bytes_sampled_max": 100,
            "runner_peak_resident_bytes_max": 100,
            "generated_frame_deadline_miss_rate": 0.0,
            "generated_frame_deadline_miss_count": 0,
            "deadline_ms": 16.666,
            "source_frame_fps_impact_percent": 0.0,
            "source_only_input_throughput_fps": dist(200, 60.0),
            "source_with_generation_input_throughput_fps": dist(200, 60.0),
        },
    }
    for frame in result["quality_frames"]:
        frame["critical_pixel_errors"]["hud"] = (
            [[10, 77, 0, 0]] if frame["timestamp_ns"] == 1 else [])
        frame["critical_pixel_errors"]["text"] = []
    hud_temporal = result["quality_by_region"]["hud"]["temporal"]
    hud_temporal["high_frequency_temporal_flicker"]["windows"][0][
        "critical_pixel_errors"] = [[10, 0.25]]
    hud_temporal["high_frequency_temporal_flicker"]["windows"][0][
        "critical_edge_errors"] = [[10, 0.25]]
    _sync_sparse_map_statistics(result)
    return result


def _enable_critical_roi(result: dict, roi_label: str) -> None:
    result["corpus"]["mask_labels"].append(roi_label)
    result["corpus"]["strict_pixel_labels"].append(roi_label)
    result["compatibility"]["corpus"]["mask_labels"] = list(result["corpus"]["mask_labels"])
    result["compatibility"]["corpus"]["strict_pixel_labels"] = list(
        result["corpus"]["strict_pixel_labels"])
    for counts in result["corpus"]["mask_coverage_pixels_by_frame"].values():
        counts[roi_label] = 64
    result["quality_by_region"][roi_label] = json.loads(
        json.dumps(result["quality_by_region"]["hud"]))
    for frame in result["quality_frames"]:
        frame["regions"][roi_label] = json.loads(json.dumps(frame["regions"]["hud"]))
        frame["mask_pixel_counts"][roi_label] = 64
        frame["critical_pixel_errors"][roi_label] = []
    for row in result["quality_by_region"][roi_label]["temporal"][
            "frame_to_frame_residual"]["transitions"]:
        row["critical_pixel_errors"] = []
    for row in result["quality_by_region"][roi_label]["temporal"][
            "high_frequency_temporal_flicker"]["windows"]:
        row["critical_pixel_errors"] = []
        row["critical_edge_errors"] = []
    _sync_sparse_map_statistics(result)


class BenchmarkMetricTests(unittest.TestCase):
    def _fake_backend_paths(self, directory: Path, source: str) -> tuple[Path, Path, Path, Path]:
        backend = directory / "fake-backend"
        backend.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
        backend.chmod(0o755)
        request = directory / "requests.tsv"
        request.write_text("", encoding="utf-8")
        return backend, request, directory / "stdout", directory / "stderr"

    def test_backend_session_timeout_terminates_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            backend, request, stdout, stderr = self._fake_backend_paths(
                Path(temporary_directory), "import time\ntime.sleep(30)\n")
            start = time.monotonic()
            with self.assertRaisesRegex(ValueError, "exceeded timeout"):
                bench._run_backend_session(backend, request, stdout, stderr, 0.1)
            self.assertLess(time.monotonic() - start, 5.0)

    def test_backend_session_enforces_combined_output_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            backend, request, stdout, stderr = self._fake_backend_paths(
                Path(temporary_directory),
                "import sys\nsys.stdout.write('x' * 1000000)\nsys.stdout.flush()\n")
            previous_limit = bench.MAX_BACKEND_SESSION_OUTPUT_BYTES
            bench.MAX_BACKEND_SESSION_OUTPUT_BYTES = 1024
            try:
                with self.assertRaisesRegex(ValueError, "sequence output exceeded"):
                    bench._run_backend_session(backend, request, stdout, stderr, 5.0)
            finally:
                bench.MAX_BACKEND_SESSION_OUTPUT_BYTES = previous_limit

    @unittest.skipUnless(hasattr(os, "killpg") and hasattr(signal, "SIGINT"),
                         "detached process-group cleanup requires POSIX signals")
    def test_interrupted_analyzer_kills_detached_backend_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            pid_file = temporary / "backend.pid"
            backend, request, stdout, stderr = self._fake_backend_paths(
                temporary,
                "import os, pathlib, time\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n")
            wrapper = "\n".join((
                "import importlib.util, pathlib, sys",
                "spec = importlib.util.spec_from_file_location('bench_under_test', sys.argv[1])",
                "bench = importlib.util.module_from_spec(spec)",
                "spec.loader.exec_module(bench)",
                "bench._run_backend_session(*(pathlib.Path(value) for value in sys.argv[2:6]), 60.0)",
            ))
            analyzer = subprocess.Popen(
                [sys.executable, "-c", wrapper, str(BENCH_PATH), str(backend),
                 str(request), str(stdout), str(stderr)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            backend_pid = None
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not pid_file.exists():
                    if analyzer.poll() is not None:
                        self.fail("analyzer exited before the backend started")
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), "backend did not report its PID")
                backend_pid = int(pid_file.read_text(encoding="ascii"))
                os.kill(analyzer.pid, signal.SIGINT)
                analyzer.wait(timeout=5.0)
                self.assertIsNotNone(analyzer.returncode)
                child_deadline = time.monotonic() + 3.0
                while time.monotonic() < child_deadline:
                    try:
                        os.kill(backend_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("detached backend process survived analyzer interruption")
            finally:
                if analyzer.poll() is None:
                    analyzer.kill()
                    analyzer.wait(timeout=2.0)
                if backend_pid is not None:
                    try:
                        os.killpg(backend_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_timestamp_planning_preserves_epoch_nanoseconds(self) -> None:
        corpus = bench.validate_corpus(
            ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion" / "manifest.json")
        epoch_offset = 1 << 60
        for frame in corpus["frames"]:
            frame["timestamp_ns"] += epoch_offset
        plan = bench._target_plan(corpus, None)
        self.assertIsInstance(plan[0]["time_ns"], int)
        self.assertEqual(plan[0]["time_ns"], epoch_offset + 16_666_667)

    def test_target_plan_uses_timestamp_quantized_fraction(self) -> None:
        frames = [
            {"index": index, "timestamp_ns": timestamp, "segment_id": "segment",
             "boundary_before": None, "interpolable": True}
            for index, timestamp in enumerate((0, 1, 2))
        ]
        corpus = {
            "frames": frames,
            "by_index": {frame["index"]: frame for frame in frames},
            "source_indices": [0, 2],
            "provider": object(),
        }
        plan = bench._target_plan(corpus, 0.26)
        self.assertEqual(plan[0]["time_ns"], 1)
        self.assertEqual(plan[0]["t"], 0.5)
        self.assertEqual(plan[0]["coordinate"], 1.0)

    def test_fraction_timestamp_validation_allows_float64_ratio_error(self) -> None:
        source_times = [0, 5_249_979_066_121_302_519]
        timestamp = 582_057_716_445_789_125
        fraction = timestamp / source_times[1]
        self.assertTrue(bench._timestamp_matches_interpolation(
            source_times, timestamp, fraction))
        self.assertFalse(bench._timestamp_matches_interpolation(
            source_times, timestamp + 1_000_000, fraction))

    def test_analysis_memory_estimate_accounts_for_named_masks(self) -> None:
        base = bench._estimated_analysis_bytes(128, 72, 11, len(bench.MASK_LABELS))
        named = bench._estimated_analysis_bytes(128, 72, 11, bench.MAX_CORPUS_MASK_LABELS)
        self.assertGreater(named, base)

    def test_analytic_provider_rejects_nonuniform_timestamps(self) -> None:
        import shutil

        source = ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion"
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "corpus"
            shutil.copytree(source, copied)
            manifest_path = copied / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["frames"][4]["timestamp_ns"] += 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nominal uniform high-rate timestamp cadence"):
                bench.validate_corpus(manifest_path)

    def test_analytic_provider_must_match_stored_integer_frames(self) -> None:
        import shutil

        source = ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion"
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "corpus"
            shutil.copytree(source, copied)
            provider_path = copied / "scene.py"
            provider_path.write_text(
                provider_path.read_text(encoding="utf-8")
                + "\n_original_render = render\n"
                  "def render(time_frame):\n"
                  "    result = _original_render(time_frame)\n"
                  "    if time_frame == 0.0:\n"
                  "        result['rgb'] = bytes(len(result['rgb']))\n"
                  "    return result\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "RGB does not match stored frame 0"):
                bench.validate_corpus(copied / "manifest.json")

    def test_analytic_provider_rejects_integer_as_pixel_buffer_without_allocating(self) -> None:
        import shutil

        source = ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion"
        with tempfile.TemporaryDirectory() as temporary_directory:
            copied = Path(temporary_directory) / "corpus"
            shutil.copytree(source, copied)
            provider_path = copied / "scene.py"
            provider_path.write_text(
                provider_path.read_text(encoding="utf-8")
                + "\n_original_render = render\n"
                  "def render(time_frame):\n"
                  "    result = _original_render(time_frame)\n"
                  "    if time_frame == 0.0:\n"
                  "        result['rgb'] = 10**12\n"
                  "    return result\n",
                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be a bytes-like object"):
                bench.validate_corpus(copied / "manifest.json")

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
        candidate["quality_frames"][1]["critical_pixel_errors"]["hud"] = [
            [11, 77, 0, 0]]
        flicker = candidate["quality_by_region"]["hud"]["temporal"][
            "high_frequency_temporal_flicker"]
        flicker["windows"][1]["critical_pixel_errors"] = [[11, 0.25]]
        flicker["windows"][1]["critical_edge_errors"] = [[11, 0.25]]
        _sync_sparse_map_statistics(candidate)
        hud_summary = candidate["quality_by_region"]["hud"]

        # The region maxima and already-worse first window remain unchanged.
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["pixels_over_5pct_error_count_per_target"]["max"],
            hud_summary["pixels_over_5pct_error_count_per_target"]["max"])
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["pixels_over_5pct_error_fraction_per_target"]["max"],
            hud_summary["pixels_over_5pct_error_fraction_per_target"]["max"])
        self.assertEqual(
            baseline["quality_by_region"]["hud"]["temporal"]["high_frequency_temporal_flicker"]["worst_pixel_per_window"]["max"],
            flicker["worst_pixel_per_window"]["max"])

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
        result = _comparison_fixture()
        result["quality_by_region"] = {"all": {}}
        with self.assertRaisesRegex(ValueError, "every required quality region"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_malformed_target_records_without_traceback(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][0] = None
        with self.assertRaisesRegex(ValueError, "malformed result structure"):
            bench._validate_result(result, "candidate")

    def test_result_validation_requires_a_corpus_content_digest(self) -> None:
        result = _comparison_fixture()
        result["corpus"]["content_sha256"] = None
        result["compatibility"]["corpus"]["content_sha256"] = None
        with self.assertRaisesRegex(ValueError, "invalid corpus content_sha256"):
            bench._validate_result(result, "candidate")

    def test_result_validation_requires_compatibility_gpu_identity(self) -> None:
        result = _comparison_fixture()
        bench._validate_result(result, "candidate")
        result["compatibility"]["host"]["gpu_device"] = None
        with self.assertRaisesRegex(ValueError, "compatibility metadata does not match"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_missing_device_identity(self) -> None:
        result = _comparison_fixture()
        result["host"]["gpu_device"] = None
        result["compatibility"]["host"]["gpu_device"] = None
        result["backend"]["device"] = None
        with self.assertRaisesRegex(ValueError, "missing a stable backend device identity"):
            bench._validate_result(result, "candidate")

    def test_result_validation_binds_temporal_timeline_to_targets(self) -> None:
        result = _comparison_fixture()
        result["configuration"]["temporal_timeline"] = [
            item for item in result["configuration"]["temporal_timeline"]
            if item["timestamp_ns"] != 1
        ]
        result["compatibility"]["configuration"]["temporal_timeline"] = result[
            "configuration"]["temporal_timeline"]
        with self.assertRaisesRegex(ValueError, "temporal timeline omits or adds"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_an_omitted_eligible_source_pair(self) -> None:
        result = _comparison_fixture()
        result["configuration"]["target_timeline"] = result["configuration"]["target_timeline"][:1]
        result["compatibility"]["configuration"]["target_timeline"] = result[
            "configuration"]["target_timeline"]
        with self.assertRaisesRegex(ValueError, "target list does not cover every eligible source pair"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_an_incomplete_source_pair_plan(self) -> None:
        result = _comparison_fixture()
        result["configuration"]["source_pair_plan"] = result["configuration"]["source_pair_plan"][:1]
        result["compatibility"]["configuration"]["source_pair_plan"] = result[
            "configuration"]["source_pair_plan"]
        with self.assertRaisesRegex(ValueError, "source-pair plan omits or misclassifies"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_stale_temporal_aggregates(self) -> None:
        result = _comparison_fixture()
        result["quality_by_region"]["hud"]["temporal"][
            "high_frequency_temporal_flicker"]["windows"][1]["mean"] = 1.0
        with self.assertRaisesRegex(ValueError, "does not match its sparse per-pixel map"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_stale_per_target_aggregates(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][1]["regions"]["scene"]["pixels_over_5pct_error_count"] = 1
        with self.assertRaisesRegex(ValueError, "aggregate mean does not match"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_nonfinite_diagnostic_psnr(self) -> None:
        result = _comparison_fixture()
        result["quality_by_region"]["all"]["psnr_db"]["p99"] = float("inf")
        with self.assertRaisesRegex(ValueError, "invalid diagnostic PSNR p99"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_erased_strict_pixel_map(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][0]["critical_pixel_errors"]["hud"] = []
        with self.assertRaisesRegex(ValueError, "does not match its sparse per-pixel map"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_erased_strict_temporal_map(self) -> None:
        result = _comparison_fixture()
        row = result["quality_by_region"]["hud"]["temporal"][
            "high_frequency_temporal_flicker"]["windows"][0]
        row["critical_pixel_errors"] = []
        with self.assertRaisesRegex(ValueError, "does not match its sparse per-pixel map"):
            bench._validate_result(result, "candidate")

    def test_result_validation_rejects_forged_strict_roi_psnr(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][0]["regions"]["hud"]["psnr_db"] = 30.0
        result["quality_by_region"]["hud"]["psnr_db"] = bench.distribution(
            [frame["regions"]["hud"]["psnr_db"] for frame in result["quality_frames"]])
        with self.assertRaisesRegex(ValueError, "PSNR does not match its sparse per-pixel map"):
            bench._validate_result(result, "candidate")

    def test_result_validation_binds_target_area_to_canvas_and_mask_coverage(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][0]["regions"]["all"]["active_pixel_count"] = 63
        with self.assertRaisesRegex(ValueError, "invalid active pixel count for all"):
            bench._validate_result(result, "candidate")

        result = _comparison_fixture()
        result["quality_frames"][0]["regions"]["hud"]["active_pixel_count"] = 65
        with self.assertRaisesRegex(ValueError, "invalid active pixel count for hud"):
            bench._validate_result(result, "candidate")

        result = _comparison_fixture()
        result["quality_frames"][0]["mask_pixel_counts"]["hud"] = 63
        with self.assertRaisesRegex(ValueError, "do not match source-frame coverage"):
            bench._validate_result(result, "candidate")

    def test_sparse_rgb_psnr_caps_finite_near_perfect_scores(self) -> None:
        self.assertEqual(
            bench._sparse_rgb_map_psnr([[0, 1, 0, 0]], 10_000_000), 120.0)

    def test_compare_detects_lost_gpu_timing_sample_coverage(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        gpu = candidate["performance"]["interpolation_gpu_time_ms"]
        gpu["count"] = 199
        gpu["missing_samples"] = 1
        gpu["p50"] = 1.0
        gpu["p95"] = 1.0
        gpu["p99"] = 1.0

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            baseline_path = temporary / "baseline.json"
            candidate_path = temporary / "candidate.json"
            output_path = temporary / "comparison.json"
            summary_path = temporary / "comparison.md"
            baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "incomplete GPU timing sample coverage"):
                bench._compare(argparse.Namespace(
                    baseline=str(baseline_path), candidate=str(candidate_path),
                    output=str(output_path), summary=str(summary_path)))

    def test_result_validation_rejects_nonfinite_target_edge_scores(self) -> None:
        result = _comparison_fixture()
        result["quality_frames"][0]["regions"]["hud"]["edge_location"]["precision_at_1px"] = float("nan")
        with self.assertRaisesRegex(ValueError, "invalid precision_at_1px"):
            bench._validate_result(result, "candidate")

    def test_compare_catches_a_single_new_deadline_miss(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        candidate["performance"]["generated_frame_deadline_miss_count"] = 1
        candidate["performance"]["generated_frame_deadline_miss_rate"] = 1 / 200

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
            self.assertTrue(any(
                row["metric"] == "generated_frame_deadline_miss_count"
                for row in comparison["regressions"]))

    def test_compare_catches_increased_source_throughput_impact(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        performance = candidate["performance"]
        performance["source_with_generation_input_throughput_fps"] = {
            key: (57.0 if key != "count" else 200)
            for key in performance["source_with_generation_input_throughput_fps"]
        }
        performance["source_frame_fps_impact_percent"] = 5.0

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
            self.assertTrue(any(
                row["metric"] == "source_frame_fps_impact_percent"
                for row in comparison["regressions"]))

    def test_compare_checks_per_target_perceptual_quality(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        candidate["quality_frames"][1]["regions"]["scene"]["perceptual_similarity"] = 0.9
        candidate["quality_by_region"]["scene"]["perceptual_similarity"] = bench.distribution(
            [frame["regions"]["scene"]["perceptual_similarity"]
             for frame in candidate["quality_frames"]])

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
            self.assertTrue(any(
                row["scope"] == "quality_target" and row["region"] == "scene"
                and row["target"]["timestamp_ns"] == 3
                and row["metric"] == "perceptual_similarity"
                for row in comparison["regressions"]))

    def test_compare_catches_per_target_error_severity_with_unchanged_pixel_counts(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        scene_frame = candidate["quality_frames"][1]["regions"]["scene"]
        scene_frame["pixel_abs_error_norm_mean"] = 0.008
        scene_frame["pixel_abs_error_norm_p95"] = 0.016
        scene_summary = candidate["quality_by_region"]["scene"]
        scene_summary["pixel_abs_error_norm_mean_per_target"] = bench.distribution(
            [frame["regions"]["scene"]["pixel_abs_error_norm_mean"]
             for frame in candidate["quality_frames"]])
        scene_summary["pixel_abs_error_norm_p95_per_target"] = bench.distribution(
            [frame["regions"]["scene"]["pixel_abs_error_norm_p95"]
             for frame in candidate["quality_frames"]])
        self.assertEqual(scene_frame["frame_max_pixel_error_norm"],
                         baseline["quality_frames"][1]["regions"]["scene"]["frame_max_pixel_error_norm"])
        self.assertEqual(scene_frame["pixels_over_1pct_error_count"], 0)
        self.assertEqual(scene_frame["pixels_over_5pct_error_count"], 0)

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
            regressions = json.loads(output_path.read_text(encoding="utf-8"))["regressions"]
            target_regressions = [row for row in regressions
                                  if row["scope"] == "quality_target"
                                  and row["region"] == "scene"
                                  and row["target"]["timestamp_ns"] == 3]
            self.assertEqual({row["metric"] for row in target_regressions}, {
                "pixel_abs_error_norm_mean", "pixel_abs_error_norm_p95"})

    def test_compare_catches_critical_roi_damage_relocated_from_another_pixel(self) -> None:
        baseline = _comparison_fixture()
        candidate = json.loads(json.dumps(baseline))
        _enable_critical_roi(baseline, "crosshair")
        _enable_critical_roi(candidate, "crosshair")
        baseline["quality_frames"][1]["critical_pixel_errors"]["crosshair"] = [[10, 200, 0, 0]]
        candidate["quality_frames"][1]["critical_pixel_errors"]["crosshair"] = [[11, 200, 0, 0]]
        _sync_sparse_map_statistics(baseline)
        _sync_sparse_map_statistics(candidate)

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
            regressions = json.loads(output_path.read_text(encoding="utf-8"))["regressions"]
            relocation = next(row for row in regressions
                              if row["scope"] == "critical_roi_pixel")
            self.assertEqual(relocation["region"], "crosshair")
            self.assertEqual(relocation["worsened_pixel_count"], 1)
            self.assertEqual(relocation["locations_sample"][0]["x"], 3)
            self.assertEqual(relocation["locations_sample"][0]["y"], 1)

    def test_compare_catches_temporal_error_relocation_with_equal_aggregates(self) -> None:
        cases = (
            ("frame_to_frame_residual", "transitions", "critical_pixel_errors",
             "first_difference_residual", [10, 200], [11, 200]),
            ("high_frequency_temporal_flicker", "windows", "critical_pixel_errors",
             "color_flicker", [10, 0.25], [11, 0.25]),
            ("high_frequency_temporal_flicker", "windows", "critical_edge_errors",
             "edge_flicker", [10, 0.25], [11, 0.25]),
        )
        for collection, row_key, field, expected_metric, old_error, new_error in cases:
            with self.subTest(metric=expected_metric), tempfile.TemporaryDirectory() as temporary_directory:
                baseline = _comparison_fixture()
                candidate = json.loads(json.dumps(baseline))
                base_rows = baseline["quality_by_region"]["hud"]["temporal"][collection][row_key]
                candidate_rows = candidate["quality_by_region"]["hud"]["temporal"][collection][row_key]
                base_rows[1][field] = [old_error]
                candidate_rows[1][field] = [new_error]
                _sync_sparse_map_statistics(baseline)
                _sync_sparse_map_statistics(candidate)

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
                regressions = json.loads(output_path.read_text(encoding="utf-8"))["regressions"]
                relocated = next(row for row in regressions
                                 if row["scope"] == "critical_roi_temporal"
                                 and row["metric"] == expected_metric)
                self.assertEqual(relocated["region"], "hud")
                self.assertEqual(relocated["worsened_pixel_count"], 1)

    def test_cut_and_loading_metadata_skip_intervals_and_reset_temporal_history(self) -> None:
        corpus = bench.validate_corpus(
            ROOT / "tools" / "benchmark" / "corpus" / "synthetic-motion" / "manifest.json")
        corpus["by_index"][1]["boundary_before"] = "scene_cut"
        corpus["by_index"][4]["interpolable"] = False
        plan = bench._target_plan(corpus, None)
        pair_plan = bench._source_pair_plan(corpus, plan)
        selected_pairs = {tuple(item["source_pair"]) for item in plan}
        self.assertNotIn((0, 2), selected_pairs)
        self.assertNotIn((2, 4), selected_pairs)
        self.assertIn((6, 8), selected_pairs)
        self.assertEqual({item["continuity_group"] for item in plan if item["source_pair"] == [6, 8]}, {3})
        self.assertEqual([item["status"] for item in pair_plan[:4]],
                         ["skipped", "skipped", "skipped", "selected"])
        self.assertEqual(pair_plan[0]["skip_reasons"], ["boundary_before"])

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
