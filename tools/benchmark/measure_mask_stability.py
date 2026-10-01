#!/usr/bin/env python3
"""Measure raw and stabilized HUD-mask changes over a synthetic source timeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile
from typing import Any


def parse_ppm(path: pathlib.Path, width: int, height: int) -> bytes:
    payload = path.read_bytes()
    header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", payload)
    if header is None:
        raise ValueError(f"invalid P6 output: {path}")
    actual_width, actual_height, max_value = map(int, header.groups())
    pixels = payload[header.end():]
    if (actual_width, actual_height, max_value) != (width, height, 255):
        raise ValueError(f"unexpected PPM dimensions or range: {path}")
    if len(pixels) != width * height * 3:
        raise ValueError(f"unexpected PPM payload length: {path}")
    return pixels


def parse_pgm_mask(path: pathlib.Path, width: int, height: int) -> bytes:
    payload = path.read_bytes()
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s", payload)
    if header is None:
        raise ValueError(f"invalid P5 mask: {path}")
    actual_width, actual_height, max_value = map(int, header.groups())
    pixels = payload[header.end():]
    if (actual_width, actual_height, max_value) != (width, height, 255):
        raise ValueError(f"unexpected PGM dimensions or range: {path}")
    if len(pixels) != width * height or any(value not in (0, 255) for value in pixels):
        raise ValueError(f"unexpected PGM mask payload: {path}")
    return pixels


def safe_frame_path(root: pathlib.Path, relative: str) -> pathlib.Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"frame path escapes corpus directory: {relative}")
    if not path.is_file():
        raise ValueError(f"missing frame: {relative}")
    return path


def run_mask_view(
    runner: pathlib.Path,
    root: pathlib.Path,
    manifest: dict[str, Any],
    indices: list[int],
    debug_view: str,
    temporary_directory: pathlib.Path,
) -> tuple[list[float], list[float], list[dict[str, int]], dict[str, Any]]:
    frames = {int(frame["index"]): frame for frame in manifest["frames"]}
    output_paths: list[pathlib.Path] = []
    target_masks: list[bytes] = []
    jobs: list[str] = []
    for pair_number, (previous_index, current_index) in enumerate(
        zip(indices, indices[1:])
    ):
        previous = frames[previous_index]
        current = frames[current_index]
        target_index = (previous_index + current_index) // 2
        target = frames.get(target_index)
        if target is None or target_index * 2 != previous_index + current_index:
            raise ValueError(
                f"source pair {previous_index},{current_index} has no exact midpoint HUD mask"
            )
        target_masks.append(parse_pgm_mask(
            safe_frame_path(root, target["masks"]["hud"]),
            int(manifest["width"]), int(manifest["height"]),
        ))
        output_path = temporary_directory / f"{debug_view}-{pair_number}.ppm"
        output_paths.append(output_path)
        fields = (
            str(safe_frame_path(root, previous["path"])),
            str(safe_frame_path(root, current["path"])),
            str(output_path),
            "0.5",
            "0",
            "1",
            str(previous_index),
            str(current_index),
            str(previous["timestamp_ns"]),
            str(current["timestamp_ns"]),
            "1" if pair_number == 0 else "0",
            "automatic",
            "nearest",
            debug_view,
            str(target["timestamp_ns"]),
        )
        jobs.append("\t".join(fields))

    completed = subprocess.run(
        [str(runner), "--server"],
        input="\n".join(jobs) + "\n",
        capture_output=True,
        check=False,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"runner failed for {debug_view}: {completed.stderr.strip()}"
        )
    rows = [json.loads(line) for line in completed.stdout.splitlines() if line]
    if len(rows) != len(output_paths):
        raise RuntimeError(
            f"runner returned {len(rows)} rows for {len(output_paths)} mask outputs"
        )

    images = [
        parse_ppm(path, int(manifest["width"]), int(manifest["height"]))
        for path in output_paths
    ]
    transition_mae = [
        sum(abs(left - right) for left, right in zip(previous, current))
        / (len(previous) * 255.0)
        for previous, current in zip(images, images[1:])
    ]
    coverage = [
        sum(image[::3]) / (int(manifest["width"]) * int(manifest["height"]) * 255.0)
        for image in images
    ]
    threshold = 128
    detection_counts = []
    for image, hud_mask in zip(images, target_masks):
        true_positive = false_positive = false_negative = true_negative = 0
        for confidence, is_hud in zip(image[::3], hud_mask):
            predicted = confidence >= threshold
            actual = is_hud != 0
            if predicted and actual:
                true_positive += 1
            elif predicted:
                false_positive += 1
            elif actual:
                false_negative += 1
            else:
                true_negative += 1
        detection_counts.append({
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        })
    return transition_mae, coverage, detection_counts, rows[0]


def summarize_detection(counts: list[dict[str, int]]) -> dict[str, float | int]:
    totals = {
        key: sum(frame[key] for frame in counts)
        for key in ("true_positive", "false_positive", "false_negative", "true_negative")
    }
    precision_denominator = totals["true_positive"] + totals["false_positive"]
    recall_denominator = totals["true_positive"] + totals["false_negative"]
    negative_denominator = totals["false_positive"] + totals["true_negative"]
    return {
        **totals,
        "precision": totals["true_positive"] / precision_denominator
            if precision_denominator else 0.0,
        "recall": totals["true_positive"] / recall_denominator
            if recall_denominator else 0.0,
        "false_positive_rate": totals["false_positive"] / negative_denominator
            if negative_denominator else 0.0,
    }


def summarize(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=pathlib.Path)
    parser.add_argument("--backend", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    corpus_path = args.corpus.resolve()
    root = corpus_path.parent
    manifest_bytes = corpus_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    runner = args.backend.resolve()
    if not runner.is_file():
        parser.error(f"backend executable does not exist: {runner}")

    source_indices = [int(index) for index in manifest["source_indices"]]
    if len(source_indices) < 3 or source_indices != sorted(set(source_indices)):
        parser.error("corpus must provide at least three increasing source indices")
    if any(index not in {int(frame["index"]) for frame in manifest["frames"]}
           for index in source_indices):
        parser.error("source_indices must refer to declared corpus frames")

    with tempfile.TemporaryDirectory(prefix="framegen-hud-mask-") as temporary:
        temporary_directory = pathlib.Path(temporary)
        raw_change, raw_coverage, raw_detection, backend_record = run_mask_view(
            runner, root, manifest, source_indices, "raw-mask", temporary_directory
        )
        stabilized_change, stabilized_coverage, stabilized_detection, _ = run_mask_view(
            runner,
            root,
            manifest,
            source_indices,
            "stabilized-mask",
            temporary_directory,
        )

    raw_mean = summarize(raw_change)
    stabilized_mean = summarize(stabilized_change)
    reduction = (raw_mean - stabilized_mean) / raw_mean if raw_mean > 0 else 0.0
    result = {
        "schema_version": 1,
        "corpus": manifest["sequence_id"],
        "corpus_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_indices": source_indices,
        "interpolation_t": 0.5,
        "backend": backend_record.get("backend_metadata", {}),
        "metric": "mean normalized RGB absolute difference between consecutive mask debug images",
        "raw_mask": {
            "mean_transition_mae": raw_mean,
            "per_transition_mae": raw_change,
            "per_frame_coverage": raw_coverage,
            "mean_coverage": summarize(raw_coverage),
            "detection_vs_midpoint_hud_roi_at_confidence_0_5":
                summarize_detection(raw_detection),
        },
        "stabilized_mask": {
            "mean_transition_mae": stabilized_mean,
            "per_transition_mae": stabilized_change,
            "per_frame_coverage": stabilized_coverage,
            "mean_coverage": summarize(stabilized_coverage),
            "detection_vs_midpoint_hud_roi_at_confidence_0_5":
                summarize_detection(stabilized_detection),
        },
        "transition_mae_reduction_fraction": reduction,
        "note": (
            "Detection precision and recall compare the 0.5 confidence threshold with "
            "the authored binary HUD ROI at each exact midpoint. The ROI is only a "
            "proxy for protected pixels, not ground-truth confidence. Lower transition "
            "difference can also mean slower response."
        ),
    }

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    summary_path = output_path.with_suffix(".md")
    summary_path.write_text(
        "\n".join(
            (
                "# HUD mask stability measurement",
                "",
                f"- Corpus: `{manifest['sequence_id']}` ({manifest['width']}×{manifest['height']})",
                f"- Source indices: `{source_indices}` at `t=0.5`",
                f"- Backend mode: `{result['backend'].get('hud_mode', 'unknown')}`; synthetic corpus only",
                f"- Raw mask transition MAE: {raw_mean:.6f}",
                f"- Stabilized mask transition MAE: {stabilized_mean:.6f}",
                f"- Transition-MAE reduction: {reduction * 100.0:.1f}%",
                f"- Raw HUD ROI precision/recall at confidence 0.5: `{summarize_detection(raw_detection)['precision']:.4f}` / `{summarize_detection(raw_detection)['recall']:.4f}`",
                f"- Stabilized HUD ROI precision/recall at confidence 0.5: `{summarize_detection(stabilized_detection)['precision']:.4f}` / `{summarize_detection(stabilized_detection)['recall']:.4f}`",
                f"- Raw per-frame coverage: `{[round(value, 6) for value in raw_coverage]}`",
                f"- Stabilized per-frame coverage: `{[round(value, 6) for value in stabilized_coverage]}`",
                "",
                "Detection scores compare a 0.5 confidence threshold with the authored binary HUD ROI at each exact midpoint. That ROI is a proxy for pixels intended for protection, not ground-truth confidence. Lower transition difference can also mean slower response.",
                "",
            )
        ),
        encoding="utf-8",
    )
    print(f"JSON: {output_path}")
    print(f"Summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
