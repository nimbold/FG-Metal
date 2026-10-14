"""Measured, bounded persistent-output budget for Z9 Finder replays."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil

MIB = 1024 ** 2
GIB = 1024 ** 3
PREFIX_BYTES = 3 * GIB
MODEL_MARGIN_PERCENT = 10
MAX_TARGET_SECONDS = 1_540
MAX_COLLECTOR_SECONDS = 2_900
MAX_PRE_ACQUISITION_SECONDS = 180
MAX_RUNNER_STARTUP_SECONDS = 1_200
MAX_PREFIXLEASE_SETUP_SECONDS = 60
MAX_TARGET_HANDOFF_SECONDS = 1_200
MAX_CANONICAL_PRE_ACQUISITION_SECONDS = 180
MAX_SHUTDOWN_CLEANUP_SECONDS = 45
MEASURED_PRE_SECONDS = 20
MEASURED_COVER_SECONDS = 40
MEASURED_RESTORED_SECONDS = 20
MAX_OBSERVER_SECONDS = Fraction(3_603, 2)  # 1,800 s watchdog + 1.5 s sample age
MAX_CONTROLLER_SECONDS = 5_000
COLLECTOR_SAMPLE_HZ = 20  # fixed 50 ms start-to-start cadence
MAX_COVER_CANDIDATES_PER_SAMPLE = 64
MAX_COVER_CANDIDATE_ROW_BYTES = 192
MAX_COVER_SAMPLE_OVERHEAD_BYTES = 512


@dataclass(frozen=True)
class Stream:
    name: str
    observed_bytes: int
    observed_interval_ns: int
    horizon_seconds: Fraction

    def projected_bytes(self) -> Fraction:
        return Fraction(self.observed_bytes * 1_000_000_000, self.observed_interval_ns) * self.horizon_seconds


STREAMS = (
    Stream("target host trace", 364_651_052, 302_640_008_541, Fraction(1_540)),
    Stream("app trace", 197_310_536, 302_640_008_541, Fraction(1_540)),
    Stream("run result", 127_112_105, 302_640_008_541, Fraction(1_540)),
    Stream("runtime log", 1_179_148, 302_640_008_541, Fraction(1_540)),
    Stream("base WindowServer inventory", 90_296_534, 1_331_811_322_500, Fraction(2_900)),
    Stream("controller record", 5_490_362, 1_513_060_180_000, Fraction(5_000)),
    Stream("controller timeline", 3_893, 1_513_060_180_000, Fraction(5_000)),
    Stream("runner log", 1_053, 1_513_060_180_000, Fraction(5_000)),
    Stream("observer samples", 3_537_117, 1_332_422_000_000, MAX_OBSERVER_SECONDS),
    Stream("observer result", 5_365_398, 1_332_422_000_000, MAX_OBSERVER_SECONDS),
    Stream("observer latest", 5_365_398, 1_332_422_000_000, MAX_OBSERVER_SECONDS),
    Stream("observer stdout", 81_814, 1_332_422_000_000, MAX_OBSERVER_SECONDS),
)

# The collector records only same-query full-cover candidates, not every desktop
# window. Each serialized row is measured and rejected if it exceeds this bound.
MAX_COVER_CANDIDATE_BYTES_PER_SECOND = (
    MAX_COVER_CANDIDATES_PER_SAMPLE * MAX_COVER_CANDIDATE_ROW_BYTES
    + MAX_COVER_SAMPLE_OVERHEAD_BYTES
) * COLLECTOR_SAMPLE_HZ

# Fixed signed run-folder allocation: 29,865,008 bytes of copied runtime
# libraries plus 107,259 bytes of observed fixed metadata and cleanup files.
SIGNED_FIXED_OUTPUT_BYTES = 29_972_267
# Transient collector/observer binaries and handoff/control files are admitted
# separately from the signed run directory.
TRANSIENT_FIXED_OUTPUT_BYTES = MIB
FIXED_OUTPUT_BYTES = SIGNED_FIXED_OUTPUT_BYTES + TRANSIENT_FIXED_OUTPUT_BYTES

SIGNED_OUTPUT_STREAM_NAMES = frozenset({
    "target host trace", "app trace", "run result", "runtime log",
})
TRANSIENT_OUTPUT_STREAM_NAMES = frozenset({
    "controller record", "controller timeline", "runner log",
    "observer samples", "observer result", "observer latest", "observer stdout",
})


def _ceil_fraction(value: Fraction) -> int:
    return (value.numerator + value.denominator - 1) // value.denominator


def round_up_mib(value: int) -> int:
    if value < 0:
        raise ValueError("byte count must be non-negative")
    return ((value + MIB - 1) // MIB) * MIB


def controller_lifecycle_seconds() -> Fraction:
    # These phases are conservatively summed even though parts overlap.
    return Fraction(
        # The single 1,200 s controller wait is both runner startup and target handoff.
        MAX_RUNNER_STARTUP_SECONDS + MAX_PREFIXLEASE_SETUP_SECONDS
        + MAX_CANONICAL_PRE_ACQUISITION_SECONDS
        + MEASURED_PRE_SECONDS + MEASURED_COVER_SECONDS + MEASURED_RESTORED_SECONDS
        + MAX_TARGET_SECONDS + int(MAX_OBSERVER_SECONDS) + MAX_SHUTDOWN_CLEANUP_SECONDS
    ) + (MAX_OBSERVER_SECONDS - int(MAX_OBSERVER_SECONDS))


def result_file_limit_bytes() -> int:
    result_stream = next(stream for stream in STREAMS if stream.name == "run result")
    return round_up_mib(_ceil_fraction(result_stream.projected_bytes()))


def reservation_breakdown() -> dict:
    projected_streams = {
        stream.name: _ceil_fraction(stream.projected_bytes()) for stream in STREAMS
    }
    cover_candidate_bytes = (
        MAX_COVER_CANDIDATE_BYTES_PER_SECOND * MAX_COLLECTOR_SECONDS
    )
    collector_model_bytes = projected_streams["base WindowServer inventory"] + cover_candidate_bytes
    collector_output_limit = round_up_mib(collector_model_bytes)
    signed_variable_bytes = sum(projected_streams[name] for name in SIGNED_OUTPUT_STREAM_NAMES)
    transient_variable_bytes = (
        sum(projected_streams[name] for name in TRANSIENT_OUTPUT_STREAM_NAMES)
        + collector_output_limit
    )
    signed_subtotal = SIGNED_FIXED_OUTPUT_BYTES + signed_variable_bytes
    transient_subtotal = TRANSIENT_FIXED_OUTPUT_BYTES + transient_variable_bytes
    signed_margin = ceil(signed_subtotal * MODEL_MARGIN_PERCENT / 100)
    transient_margin = ceil(transient_subtotal * MODEL_MARGIN_PERCENT / 100)
    signed_limit = round_up_mib(signed_subtotal + signed_margin)
    transient_limit = round_up_mib(transient_subtotal + transient_margin)
    required_bytes = round_up_mib(signed_limit + transient_limit)
    variable_bytes = signed_variable_bytes + transient_variable_bytes
    subtotal = signed_subtotal + transient_subtotal
    model_margin = signed_margin + transient_margin
    return {
        "schema_version": 1,
        "fixed_output_bytes": FIXED_OUTPUT_BYTES,
        "fixed_output_split_bytes": {
            "signed_run_directory": SIGNED_FIXED_OUTPUT_BYTES,
            "transient_control_evidence": TRANSIENT_FIXED_OUTPUT_BYTES,
        },
        "projected_stream_bytes": projected_streams,
        "collector_jsonl_output_limit_bytes": collector_output_limit,
        "cover_candidate_cap": {
            "maximum_rows_per_sample": MAX_COVER_CANDIDATES_PER_SAMPLE,
            "maximum_serialized_row_bytes": MAX_COVER_CANDIDATE_ROW_BYTES,
            "maximum_sample_overhead_bytes": MAX_COVER_SAMPLE_OVERHEAD_BYTES,
            "samples_per_second": COLLECTOR_SAMPLE_HZ,
            "maximum_bytes_per_second": MAX_COVER_CANDIDATE_BYTES_PER_SECOND,
            "horizon_seconds": MAX_COLLECTOR_SECONDS,
            "maximum_bytes": cover_candidate_bytes,
        },
        "maximum_lifecycle_seconds": {
            "runner_startup": MAX_RUNNER_STARTUP_SECONDS,
            "prefixlease_setup": MAX_PREFIXLEASE_SETUP_SECONDS,
            "target_handoff": MAX_TARGET_HANDOFF_SECONDS,
            "canonical_pre_acquisition": MAX_CANONICAL_PRE_ACQUISITION_SECONDS,
            "target_output": MAX_TARGET_SECONDS,
            "windowserver_collector": MAX_COLLECTOR_SECONDS,
            "observer": float(MAX_OBSERVER_SECONDS),
            "controller_sum": float(controller_lifecycle_seconds()),
            "controller_cap": MAX_CONTROLLER_SECONDS,
            "pre_acquisition": MAX_PRE_ACQUISITION_SECONDS,
            "shutdown_cleanup": MAX_SHUTDOWN_CLEANUP_SECONDS,
            "measured_pre": MEASURED_PRE_SECONDS,
            "measured_cover": MEASURED_COVER_SECONDS,
            "measured_restored": MEASURED_RESTORED_SECONDS,
        },
        "run_result_file_limit_bytes": result_file_limit_bytes(),
        "signed_run_output_model_bytes_before_margin": signed_subtotal,
        "signed_run_output_limit_bytes": signed_limit,
        "transient_evidence_model_bytes_before_margin": transient_subtotal,
        "transient_evidence_limit_bytes": transient_limit,
        "variable_output_bytes": variable_bytes,
        "pre_margin_subtotal_bytes": subtotal,
        "model_margin_percent": MODEL_MARGIN_PERCENT,
        "model_margin_bytes": model_margin,
        "model_margin_split_bytes": {
            "signed_run_directory": signed_margin,
            "transient_control_evidence": transient_margin,
        },
        "required_persistent_output_bytes": required_bytes,
        "rounding_unit_bytes": MIB,
        "storage_policy_margin_separate": True,
    }


def persistent_output_reservation_bytes() -> int:
    return reservation_breakdown()["required_persistent_output_bytes"]
