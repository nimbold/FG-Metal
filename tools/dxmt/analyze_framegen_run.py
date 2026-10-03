#!/usr/bin/env python3
"""Summarize retained app/native CSVs from one DXMT framegen run.

The analyzer accepts the Step 10B.1 schema and newer per-source/stage timing
columns. Missing optional instrumentation is reported as unavailable; it is
never inferred from a nearby event with different semantics.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


SOURCE_ACCEPT_EVENTS = {
    "source_accepted",
    "source_owned",
    "source_ownership_accepted",
    "source_ownership_transfer",
}
SOURCE_TERMINAL_EVENTS = {
    "source_terminal",
    "source_terminal_presented",
    "source_terminal_replayed",
    "source_terminal_fallback",
}
SOURCE_SAFETY_VIOLATION_EVENTS = {"source_safety_violation"}
SOURCE_DISPLAY_EVENTS = {
    "source_displayed",
    "source_replayed_displayed",
    "source_fallback_displayed",
    "source_repeat_presented",
}
GENERATED_DISPLAY_EVENTS = {"generated_displayed"}
ZERO_EVENTS = {
    "presentation_dropped",
    "zero_presented_time",
    "drawable_presented_zero",
    "presented_time_zero",
}
PRESENTATION_STAGE_EVENTS = {"presentation_stages", "presentation_stage"}
GENERATED_FAILURE_EVENTS = {
    "generated_failed",
    "generated_completion_failed",
    "generation_failed",
    "generation_exception",
}
HRESULT_FAILURE_BIT = 0x80000000
DXGI_STATUS_OCCLUDED = 0x087A0001


def rows(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _int(row: dict[str, str], *names: str) -> int | None:
    for name in names:
        value = row.get(name)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _hresult_value(value: object) -> int | None:
    """Parse a CSV HRESULT and normalize it to its unsigned 32-bit value."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        parsed = int(text, 0)
    except ValueError:
        # Decimal HRESULTs with leading zeroes are valid input too; int(..., 0)
        # intentionally rejects those strings.
        try:
            parsed = int(text, 10)
        except ValueError:
            return None
    return parsed & 0xFFFFFFFF


def _hresult_failed(value: int | None) -> bool:
    return value is not None and bool(value & HRESULT_FAILURE_BIT)


def _safe_terminal(
    row: dict[str, str], positive_renderer_feedback: bool = False
) -> bool:
    """Whether a terminal record proves the accepted source had a safe outcome."""
    event = row.get("event", "")
    detail = (_text(row, "outcome", "detail")).lower()
    actual = _int(row, "actual_presented_ns", "presented_time_ns")
    if "display_submitted_unconfirmed" in detail:
        # DXMT commits the source presentation at successful GPU submission
        # and drawable.present(). Keep this distinct from positive visibility
        # feedback, but do not call a logged, completed submission lost.
        return True
    if event == "source_terminal":
        if "ordinary_fallback" in detail:
            return True
        return "renderer_presented" in detail and (
            positive_renderer_feedback or actual is not None and actual > 0
        )
    if "ordinary_fallback" in detail:
        return True
    return actual is not None and actual > 0


def _text(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return str(value)
    return ""


def _kind(row: dict[str, str]) -> str:
    value = (_text(row, "kind", "frame_kind") or "unknown").lower()
    if value in {"1", "source", "source_frame", "real_source"}:
        return "source"
    if value in {"2", "generated", "generated_frame", "synthetic"}:
        return "generated"
    return value


def _id_key(row: dict[str, str]) -> tuple[str, str] | None:
    source_id = _int(row, "source_id")
    if source_id is None or source_id <= 0:
        return None
    # Source IDs are monotonic for the presenter in the controlled harness.
    # Keeping epoch out of the identity lets a retained source be replayed
    # after a lifecycle epoch transition.
    return (_text(row, "presenter_id") or "0", str(source_id))


def percentile(values: list[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def show_metric(label: str, values: list[int], divisor: float = 1_000_000.0) -> str:
    if not values:
        return f"{label}: n=0 (unavailable)"
    return (
        f"{label}: n={len(values)} "
        f"p50={percentile(values, 0.50) / divisor:.3f} "
        f"p95={percentile(values, 0.95) / divisor:.3f} "
        f"p99={percentile(values, 0.99) / divisor:.3f} "
        f"max={max(values) / divisor:.3f}"
    )


def _presentation_key(row: dict[str, str], row_index: int) -> tuple[str, ...]:
    presenter = _text(row, "presenter_id") or "0"
    epoch = _text(row, "epoch") or "0"
    sequence = _int(row, "presentation_seq")
    if sequence is not None and sequence > 0:
        return (presenter, epoch, "presentation", str(sequence))
    tick = _int(row, "tick_id")
    if tick is not None and tick > 0:
        return (presenter, epoch, "tick", str(tick))
    return (presenter, epoch, "row", str(row_index))


def _merge_nonzero(target: dict[str, str], row: dict[str, str]) -> None:
    for key, value in row.items():
        if value in (None, ""):
            continue
        old = target.get(key)
        if old in (None, "") or old == "0" and value != "0":
            target[key] = value


def _duration(record: dict[str, str], end_names: tuple[str, ...], start_names: tuple[str, ...]) -> int | None:
    end = _int(record, *end_names)
    start = _int(record, *start_names)
    if end is None or start is None or end <= 0 or start <= 0 or end < start:
        return None
    return end - start


def _metric_values(records: Iterable[dict[str, str]], end: tuple[str, ...], start: tuple[str, ...]) -> list[int]:
    result: list[int] = []
    for record in records:
        duration = _duration(record, end, start)
        if duration is not None:
            result.append(duration)
    return result


def _callback_ledger(native: list[dict[str, str]]) -> dict[str, object]:
    """Validate callback summaries against the unique tick-terminal ledger."""
    terminal_event_names = PRESENTATION_STAGE_EVENTS | {"display_tick_terminal"}
    terminal_event_rows = [
        row for row in native if row.get("event") in terminal_event_names
    ]
    terminal_rows = [
        row for row in terminal_event_rows if (_int(row, "tick_id") or 0) > 0
    ]
    tick_counts = Counter(
        (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0",
         str(_int(row, "tick_id") or 0))
        for row in terminal_rows
    )
    terminal_classes = Counter(
        (_text(row, "terminal_class") or "unavailable").lower()
        for row in terminal_rows
    )
    summary_rows: dict[str, list[dict[str, int]]] = defaultdict(list)
    malformed_summaries = 0
    for row in native:
        if row.get("event") != "display_callback_summary":
            continue
        presenter = _text(row, "presenter_id") or "0"
        parsed: dict[str, int] = {}
        for token in _text(row, "detail").split(";"):
            key, separator, value = token.partition("=")
            if not separator:
                continue
            try:
                parsed[key] = int(value)
            except ValueError:
                continue
        if ("valid_callback_count" not in parsed or "terminal_count" not in parsed or
                parsed["valid_callback_count"] < 0 or parsed["terminal_count"] < 0):
            malformed_summaries += 1
        summary_rows[presenter].append(parsed)

    # NativeState counters are cumulative across lifecycle epochs. Use each
    # presenter's final snapshot and flag any counter rollback in the stream.
    summaries: dict[str, dict[str, int]] = {}
    counter_regressions = 0
    for presenter, snapshots in summary_rows.items():
        previous: dict[str, int] | None = None
        for snapshot in snapshots:
            if previous is not None:
                for key in ("valid_callback_count", "terminal_count"):
                    if (key in previous and key in snapshot and
                            snapshot[key] < previous[key]):
                        counter_regressions += 1
            previous = snapshot
        summaries[presenter] = snapshots[-1]

    callback_count = sum(
        item.get("valid_callback_count", 0) for item in summaries.values()
    )
    summary_terminal_count = sum(
        item.get("terminal_count", 0) for item in summaries.values()
    )
    summary_terminal_delta = callback_count - summary_terminal_count
    high_water = max(
        (item.get("drawable_outstanding_high_water", 0)
         for item in summaries.values()), default=0
    )
    missing_tick_ids = len(terminal_event_rows) - len(terminal_rows)
    missing_classes = sum(
        not _text(row, "terminal_class") for row in terminal_rows
    )
    duplicates = sum(max(0, count - 1) for count in tick_counts.values())

    if not summary_rows:
        status = "UNVERIFIED"
    else:
        mismatch = (
            malformed_summaries > 0 or counter_regressions > 0 or
            any(item.get("valid_callback_count") != item.get("terminal_count")
                for item in summaries.values()) or
            summary_terminal_delta != 0 or
            callback_count != len(tick_counts) or
            summary_terminal_count != len(terminal_rows) or
            len(terminal_rows) != len(tick_counts) or duplicates > 0 or
            missing_tick_ids > 0 or missing_classes > 0
        )
        status = "MISMATCH" if mismatch else "PASS"

    return {
        "status": status,
        "has_summaries": bool(summary_rows),
        "callback_count": callback_count,
        "terminal_count": summary_terminal_count,
        "terminal_delta": summary_terminal_delta,
        "high_water": high_water,
        "terminal_rows": terminal_rows,
        "tick_counts": tick_counts,
        "terminal_classes": terminal_classes,
        "duplicates": duplicates,
        "missing_tick_ids": missing_tick_ids,
        "missing_classes": missing_classes,
        "malformed_summaries": malformed_summaries,
        "counter_regressions": counter_regressions,
    }


def _id_list(keys: Iterable[tuple[str, str]], limit: int = 80) -> str:
    values = sorted(keys, key=lambda item: (item[0], int(item[1])))
    labels = [f"{presenter}:{source_id}" for presenter, source_id in values[:limit]]
    suffix = f",... (+{len(values) - limit})" if len(values) > limit else ""
    return ",".join(labels) + suffix


def summarize_rows(native: list[dict[str, str]], app: list[dict[str, str]]) -> str:
    counts = Counter(row.get("event", "") for row in native)
    presents = [row for row in app if row.get("event") == "present"]
    elapsed = [value for row in presents if (value := _int(row, "elapsed_us")) is not None]
    elapsed_s = (max(elapsed) - min(elapsed)) / 1_000_000 if len(elapsed) > 1 else 0.0
    app_rate = len(presents) / elapsed_s if elapsed_s > 0 else 0.0
    app_hresult_values = [_hresult_value(row.get("present_hr")) for row in presents]
    buffer_hresult_values = [_hresult_value(row.get("buffer0_query_hr")) for row in presents]
    app_errors = sum(_hresult_failed(value) for value in app_hresult_values)
    app_nonzero_success_statuses = sum(
        value is not None and value != 0 and not _hresult_failed(value)
        for value in app_hresult_values
    )
    app_occluded_statuses = sum(value == DXGI_STATUS_OCCLUDED for value in app_hresult_values)
    buffer_errors = sum(_hresult_failed(value) for value in buffer_hresult_values)
    window_visible = Counter(_int(row, "window_visible") for row in presents)
    window_iconic = Counter(_int(row, "window_iconic") for row in presents)
    window_foreground = Counter(_int(row, "window_foreground") for row in presents)

    # Merge asynchronous submit/completion/feedback rows by presentation ID.
    # Tick-only events are retained separately and can supply callback-stage
    # timing without being mistaken for presentation outcomes.
    presentation_records: dict[tuple[str, ...], dict[str, str]] = {}
    presentation_events: dict[tuple[str, ...], set[str]] = defaultdict(set)
    tick_records: dict[tuple[str, str, str], dict[str, str]] = {}
    tick_events: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for index, row in enumerate(native):
        pkey = _presentation_key(row, index)
        merged = presentation_records.setdefault(pkey, {})
        _merge_nonzero(merged, row)
        presentation_events[pkey].add(row.get("event", ""))
        tick = _int(row, "tick_id")
        if tick is not None and tick > 0:
            tkey = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0", str(tick))
            _merge_nonzero(tick_records.setdefault(tkey, {}), row)
            tick_events[tkey].add(row.get("event", ""))

    # Presentation feedback has a nonzero sequence; callback and stage rows
    # are keyed only by the display tick. Join those fields into the matching
    # presentation record without replacing presentation-specific values.
    for key, record in presentation_records.items():
        if len(key) < 3 or key[2] != "presentation":
            continue
        tick = _int(record, "tick_id")
        if tick is None or tick <= 0:
            continue
        tkey = (_text(record, "presenter_id") or "0", _text(record, "epoch") or "0", str(tick))
        _merge_nonzero(record, tick_records.get(tkey, {}))

    # Positive timestamps define visible order. Keep presenters and epochs
    # separate for chronology and exact immediate A/G/B checks.
    visible = [
        (index, row)
        for index, row in enumerate(native)
        if row.get("event") in SOURCE_DISPLAY_EVENTS | GENERATED_DISPLAY_EVENTS
        and (_int(row, "actual_presented_ns", "presented_time_ns") or 0) > 0
    ]
    visible_by_epoch: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for _, row in visible:
        group = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0")
        visible_by_epoch[group].append(row)
    for group in visible_by_epoch.values():
        group.sort(key=lambda row: (_int(row, "actual_presented_ns", "presented_time_ns") or 0,
                                    _int(row, "presentation_seq") or 0))

    exact_brackets = 0
    immediate_triples = 0
    immediate_missing_a = 0
    immediate_missing_b = 0
    immediate_missing_both = 0
    nearest_missing_a = 0
    nearest_missing_b = 0
    nearest_missing_both = 0
    source_regressions = 0
    repeated_sources = 0
    displayed_g = 0
    malformed_displayed_pairs = 0
    unbracketed_endpoint_ids: set[tuple[str, str]] = set()
    immediate_missing_endpoint_ids: set[tuple[str, str]] = set()
    source_zero_ids: set[tuple[str, str]] = set()

    for group_key, ordered in visible_by_epoch.items():
        last_source_id = 0
        previous_source_id: int | None = None
        for index, event in enumerate(ordered):
            if event.get("event") in SOURCE_DISPLAY_EVENTS:
                source_id = _int(event, "source_id") or 0
                if source_id < last_source_id:
                    source_regressions += 1
                last_source_id = max(last_source_id, source_id)
                if previous_source_id == source_id:
                    repeated_sources += 1
                previous_source_id = source_id
                continue

            displayed_g += 1
            pair_a = _int(event, "pair_a_id") or 0
            pair_b = _int(event, "pair_b_id") or 0
            source_id = _int(event, "source_id") or 0
            if pair_a <= 0 or pair_b != pair_a + 1 or source_id != pair_b:
                malformed_displayed_pairs += 1
            before = ordered[index - 1] if index > 0 else None
            after = ordered[index + 1] if index + 1 < len(ordered) else None
            immediate_a = before is not None and before.get("event") in SOURCE_DISPLAY_EVENTS and _int(before, "source_id") == pair_a
            immediate_b = after is not None and after.get("event") in SOURCE_DISPLAY_EVENTS and _int(after, "source_id") == pair_b
            if immediate_a and immediate_b:
                immediate_triples += 1
            else:
                missing_a = not immediate_a
                missing_b = not immediate_b
                immediate_missing_a += missing_a
                immediate_missing_b += missing_b
                immediate_missing_both += missing_a and missing_b
                if not immediate_a and pair_a > 0:
                    immediate_missing_endpoint_ids.add((group_key[0], str(pair_a)))
                if not immediate_b and pair_b > 0:
                    immediate_missing_endpoint_ids.add((group_key[0], str(pair_b)))

            previous_source = next(
                (row for row in reversed(ordered[:index]) if row.get("event") in SOURCE_DISPLAY_EVENTS),
                None,
            )
            next_source = next(
                (row for row in ordered[index + 1:] if row.get("event") in SOURCE_DISPLAY_EVENTS),
                None,
            )
            nearest_a = previous_source is not None and _int(previous_source, "source_id") == pair_a
            nearest_b = next_source is not None and _int(next_source, "source_id") == pair_b
            if nearest_a and nearest_b:
                exact_brackets += 1
            else:
                missing_a = not nearest_a
                missing_b = not nearest_b
                nearest_missing_a += missing_a
                nearest_missing_b += missing_b
                nearest_missing_both += missing_a and missing_b
                if not nearest_a and pair_a > 0:
                    unbracketed_endpoint_ids.add((group_key[0], str(pair_a)))
                if not nearest_b and pair_b > 0:
                    unbracketed_endpoint_ids.add((group_key[0], str(pair_b)))

    generated_frames = [row for _, row in visible if row.get("event") in GENERATED_DISPLAY_EVENTS]
    generated_requests = [row for row in native if row.get("event") == "generated_requested"]
    malformed_requested_pairs = sum(
        (_int(row, "pair_a_id") or 0) <= 0
        or (_int(row, "pair_b_id") or 0) != (_int(row, "pair_a_id") or 0) + 1
        or (_int(row, "source_id") or 0) != (_int(row, "pair_b_id") or 0)
        for row in generated_requests
    )

    # Explicit terminal rows are authoritative in the newer schema. For old
    # CSVs, a source's first positive source_displayed record is the best
    # available terminal proxy; repeated refreshes do not create duplicate
    # terminal outcomes.
    accepted_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in native:
        if row.get("event") in SOURCE_ACCEPT_EVENTS:
            key = _id_key(row)
            if key is not None:
                accepted_rows[key].append(row)
    acceptance_details = " ".join(
        row.get("detail", "").lower()
        for row in native
        if row.get("event") in SOURCE_ACCEPT_EVENTS
    )
    if "copy completed" in acceptance_details or "copy complete" in acceptance_details:
        acceptance_semantics = "copy_completion_event_only"
    else:
        acceptance_semantics = "ownership_acceptance_event"
    explicit_terminal_rows = [row for row in native if row.get("event") in SOURCE_TERMINAL_EVENTS]
    explicit_terminal_mode = bool(explicit_terminal_rows)
    positive_source_display_ids = {
        key
        for row in native
        if row.get("event") in SOURCE_DISPLAY_EVENTS
        and (_int(row, "actual_presented_ns", "presented_time_ns") or 0) > 0
        and (key := _id_key(row)) is not None
    }
    terminal_counts: Counter[tuple[str, str]] = Counter()
    terminal_event_counts: Counter[tuple[str, str]] = Counter()
    terminal_bad_feedback: set[tuple[str, str]] = set()
    unsafe_terminal_ids: set[tuple[str, str]] = set()
    submitted_unconfirmed_ids: set[tuple[str, str]] = set()
    if explicit_terminal_mode:
        for row in explicit_terminal_rows:
            key = _id_key(row)
            if key is None:
                continue
            terminal_event_counts[key] += 1
            if "display_submitted_unconfirmed" in _text(
                row, "outcome", "detail"
            ).lower():
                submitted_unconfirmed_ids.add(key)
            actual = _int(row, "actual_presented_ns", "presented_time_ns")
            safe = _safe_terminal(row, key in positive_source_display_ids)
            if not safe:
                unsafe_terminal_ids.add(key)
            if (actual is not None and actual <= 0
                    and "ordinary_fallback" not in _text(row, "outcome", "detail").lower()
                    and "display_submitted_unconfirmed" not in _text(
                        row, "outcome", "detail"
                    ).lower()
                    and key not in positive_source_display_ids):
                terminal_bad_feedback.add(key)
            if safe:
                terminal_counts[key] += 1
    else:
        for row in native:
            if row.get("event") in SOURCE_DISPLAY_EVENTS:
                key = _id_key(row)
                if key is not None and (_int(row, "actual_presented_ns", "presented_time_ns") or 0) > 0:
                    terminal_counts[key] = 1
                    terminal_event_counts[key] = 1

    accepted_ids = set(accepted_rows)
    missing_terminal_ids = {
        key for key in accepted_ids
        if terminal_event_counts[key] == 0
    }
    duplicate_terminal_ids = {
        key for key in accepted_ids
        if terminal_event_counts[key] > 1
    }
    orphan_terminal_ids = set(terminal_event_counts) - accepted_ids
    duplicate_accept_ids = {key for key, records in accepted_rows.items() if len(records) > 1}
    missing_terminal_records = sum(
        max(0, len(accepted_rows[key]) - min(len(accepted_rows[key]), terminal_event_counts[key]))
        for key in accepted_ids
    )
    duplicate_terminal_records = sum(
        max(0, terminal_event_counts[key] - 1)
        for key in accepted_ids
    )
    safety_violation_ids = {
        key
        for row in native
        if row.get("event") in SOURCE_SAFETY_VIOLATION_EVENTS
        and (key := _id_key(row)) is not None
    }
    unsafe_accepted_ids = unsafe_terminal_ids & accepted_ids
    safety_violation_accepted_ids = safety_violation_ids & accepted_ids
    safe_terminal_accepted_ids = {
        key for key in accepted_ids
        if terminal_counts[key] > 0 and key not in unsafe_accepted_ids
        and key not in safety_violation_accepted_ids
    }
    lost_source_ids = (
        missing_terminal_ids | unsafe_accepted_ids | (terminal_bad_feedback & accepted_ids)
        | safety_violation_accepted_ids
    )
    terminal_details = Counter(
        _text(row, "detail") or "unspecified"
        for row in explicit_terminal_rows
    )

    source_state_transitions = Counter()
    for row in native:
        detail = _text(row, "detail")
        if (row.get("event") in {"source_state", "source_state_transition"}
                or _text(row, "source_state") and "->" in detail):
            if "->" in detail:
                source_state_transitions[detail] += 1
            else:
                source_state_transitions["transition detail unavailable"] += 1

    zero_rows: list[tuple[int, dict[str, str]]] = []
    for index, row in enumerate(native):
        event = row.get("event", "")
        actual = _int(row, "actual_presented_ns", "presented_time_ns")
        detail = row.get("detail", "").lower()
        zero_column = row.get("presented_time_ns") not in (None, "") and actual == 0
        explicit_zero = (
            event in {"zero_presented_time", "presented_time_zero", "drawable_presented_zero"}
            or event in ZERO_EVENTS and (
                zero_column
                or "presentedtime=0" in detail
                or "presented_time=0" in detail
            )
        )
        if explicit_zero:
            zero_rows.append((index, row))
            if _kind(row) == "source":
                key = _id_key(row)
                if key is not None:
                    source_zero_ids.add(key)

    zero_by_kind = Counter(_kind(row) for _, row in zero_rows)
    zero_gpu_status = Counter()
    zero_circuit_state = Counter()
    zero_lifecycle_epoch = Counter()
    zero_feedback_records: set[tuple[str, ...]] = set()
    zero_stage_records: set[tuple[str, ...]] = set()
    for index, row in zero_rows:
        pkey = _presentation_key(row, index)
        zero_feedback_records.add(pkey)
        record = presentation_records.get(pkey, {})
        tick = _int(row, "tick_id")
        tkey = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0", str(tick)) if tick else None
        if tkey is not None and tick_events.get(tkey, set()) & PRESENTATION_STAGE_EVENTS:
            zero_stage_records.add(pkey)
        status = (_text(record, "gpu_status", "gpu_completion_status") or
                  _text(row, "gpu_status", "gpu_completion_status"))
        if not status:
            events = presentation_events.get(pkey, set())
            if "presentation_gpu_completed" in events:
                status = "completed"
            elif "presentation_failed" in events:
                status = "failed"
            else:
                status = "unobserved"
        zero_gpu_status[status.lower()] += 1
        circuit = _text(record, "circuit_state") or _text(row, "circuit_state") or "unavailable"
        zero_circuit_state[circuit.lower()] += 1
        lifecycle = _text(record, "lifecycle_epoch", "epoch") or _text(row, "lifecycle_epoch", "epoch") or "unavailable"
        zero_lifecycle_epoch[lifecycle.lower()] += 1

    # Reconcile source IDs involved in unbracketed Gs with sources that also
    # received zero-presentedTime feedback. This is a correlation count only.
    unbracketed_zero_overlap = unbracketed_endpoint_ids & source_zero_ids
    immediate_zero_overlap = immediate_missing_endpoint_ids & source_zero_ids
    zero_source_lost_overlap = source_zero_ids & lost_source_ids

    # Stage data may be spread across submit, GPU completion, and presented
    # feedback rows. Merge those by presentation sequence; tick-only stages
    # remain available for callback-to-worker timing.
    presentation_ticks = {
        (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0", str(_int(row, "tick_id")))
        for row in native
        if (_int(row, "presentation_seq") or 0) > 0 and (_int(row, "tick_id") or 0) > 0
    }
    stage_records = [
        record for key, record in presentation_records.items()
        if len(key) >= 3 and key[2] == "presentation"
    ]
    stage_records.extend(
        record for key, record in tick_records.items()
        if key not in presentation_ticks
    )
    callback_worker: list[int] = []
    callback_selection: list[int] = []
    mutex_wait: list[int] = []
    mutex_hold: list[int] = []
    encode_time: list[int] = []
    callback_gpu_submit: list[int] = []
    gpu_complete: list[int] = []
    callback_present_call: list[int] = []
    gpu_submit_present_call: list[int] = []
    present_feedback: list[int] = []
    callback_feedback: list[int] = []
    actual_target_offset: list[int] = []
    legacy_submit_complete: list[int] = []
    legacy_callback_submit: list[int] = []
    for record in stage_records:
        tick_key = (_text(record, "presenter_id") or "0", _text(record, "epoch") or "0",
                    _text(record, "tick_id") or "0")
        timing = dict(tick_records.get(tick_key, {}))
        _merge_nonzero(timing, record)
        value = _duration(timing, ("worker_wake_ns", "worker_wakeup_ns"), ("callback_ns",))
        if value is not None:
            callback_worker.append(value)
        value = _duration(timing, ("selection_ns",), ("callback_ns",))
        if value is not None:
            callback_selection.append(value)
        value = _duration(timing, ("mutex_acquired_ns", "device_mutex_acquired_ns"),
                          ("mutex_wait_start_ns", "device_mutex_wait_start_ns"))
        if value is not None:
            mutex_wait.append(value)
        value = _duration(timing, ("device_mutex_release_ns",),
                          ("mutex_acquired_ns", "device_mutex_acquired_ns"))
        if value is not None:
            mutex_hold.append(value)
        value = _duration(timing, ("presenter_encode_end_ns", "encoder_end_ns"),
                          ("presenter_encode_start_ns", "encoder_start_ns"))
        if value is not None:
            encode_time.append(value)
        gpu_submit = ("gpu_submit_ns", "command_buffer_commit_ns")
        gpu_done = ("gpu_complete_ns", "gpu_completion_ns")
        value = _duration(timing, gpu_done, gpu_submit)
        if value is not None:
            gpu_complete.append(value)
        value = _duration(timing, gpu_submit, ("callback_ns",))
        if value is not None:
            callback_gpu_submit.append(value)
        present_call = ("present_call_ns", "drawable_present_call_ns")
        value = _duration(timing, present_call, ("callback_ns",))
        if value is not None:
            callback_present_call.append(value)
        value = _duration(timing, present_call, gpu_submit)
        if value is not None:
            gpu_submit_present_call.append(value)
        feedback = ("drawable_feedback_ns", "feedback_ns", "present_feedback_ns", "feedback_callback_ns")
        value = _duration(timing, feedback, present_call)
        if value is not None:
            present_feedback.append(value)
        value = _duration(timing, feedback, ("callback_ns",))
        if value is not None:
            callback_feedback.append(value)
        # Step 10B.1 recorded this before command_buffer.commit() and before
        # drawable.present(); keep it visibly separate from true GPU submit.
        old_submit = _int(timing, "present_submit_ns")
        old_done = _int(timing, "gpu_complete_ns")
        old_callback = _int(timing, "callback_ns")
        is_legacy_submit_record = _text(record, "event") == "presentation_submitted"
        if is_legacy_submit_record and old_submit and old_done and old_done >= old_submit:
            legacy_submit_complete.append(old_done - old_submit)
        if is_legacy_submit_record and old_submit and old_callback and old_submit >= old_callback:
            legacy_callback_submit.append(old_submit - old_callback)

    for _, row in visible:
        target = _int(row, "target_ns", "target_presentation_ns")
        actual = _int(row, "actual_presented_ns", "presented_time_ns")
        if target is not None and actual is not None and actual > 0:
            actual_target_offset.append(actual - target)

    # Avoid duplicate callback samples from merged presentation and tick rows.
    stage_rows = [row for row in native if row.get("event") in PRESENTATION_STAGE_EVENTS]
    tick_callback_durations = [
        value for row in stage_rows
        if (value := _int(row, "callback_duration_ns")) is not None
    ]
    queue_depths = [
        value for row in stage_rows
        if (value := _int(row, "queue_depth")) is not None and value > 0
    ]
    copy_latencies = [
        ready - source
        for row in native
        if row.get("event") == "history_copy_complete"
        and (source := _int(row, "source_ns")) is not None
        and (ready := _int(row, "copy_ready_ns")) is not None
        and ready >= source
    ]
    requests = {
        (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0",
         str(_int(row, "pair_a_id") or 0), str(_int(row, "pair_b_id") or 0)):
        _int(row, "generated_requested_ns")
        for row in generated_requests
        if _int(row, "generated_requested_ns") is not None
    }
    generation_latencies = []
    for row in native:
        if row.get("event") != "generated_ready":
            continue
        key = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0",
               str(_int(row, "pair_a_id") or 0), str(_int(row, "pair_b_id") or 0))
        request_ns = requests.get(key)
        ready_ns = _int(row, "generated_ready_ns")
        if request_ns is not None and ready_ns is not None and ready_ns >= request_ns:
            generation_latencies.append(ready_ns - request_ns)
    display_times = sorted(
        _int(row, "actual_presented_ns", "presented_time_ns") or 0
        for _, row in visible
    )
    display_intervals = [right - left for left, right in zip(display_times, display_times[1:])]
    display_span_s = (display_times[-1] - display_times[0]) / 1_000_000_000 if len(display_times) > 1 else 0.0
    display_rate = len(display_times) / display_span_s if display_span_s > 0 else 0.0

    # Step 10B.4 has one callback-terminal row per valid display update and a
    # separate fully joined result for every submitted drawable.
    feedback_results = [
        row for row in native if row.get("event") == "presentation_feedback_result"
    ]
    callback_ledger = _callback_ledger(native)
    terminal_rows = callback_ledger["terminal_rows"]
    terminal_tick_counts = callback_ledger["tick_counts"]
    terminal_classes = callback_ledger["terminal_classes"]
    terminal_duplicates = callback_ledger["duplicates"]
    summary_callback_count = callback_ledger["callback_count"]
    summary_terminal_count = callback_ledger["terminal_count"]
    summary_terminal_delta = callback_ledger["terminal_delta"]
    summary_high_water = callback_ledger["high_water"]
    callback_summaries = callback_ledger["has_summaries"]
    callback_count_text = str(summary_callback_count) if callback_summaries else "unavailable"
    terminal_count_text = str(summary_terminal_count) if callback_summaries else "unavailable"
    callback_hz_text = f"{summary_callback_count / elapsed_s:.3f}" if elapsed_s > 0 and callback_summaries else "unavailable"
    zero_result_rows = [
        row for row in feedback_results
        if (_int(row, "actual_presented_ns") or 0) <= 0
    ]
    positive_result_rows = [
        row for row in feedback_results
        if (_int(row, "actual_presented_ns") or 0) > 0
    ]
    gpu_result_status = Counter(
        (_text(row, "gpu_status") or "unavailable").lower()
        for row in feedback_results
    )
    positive_result_times = sorted(
        _int(row, "actual_presented_ns") or 0 for row in positive_result_rows
    )
    feedback_intervals = [
        right - left
        for left, right in zip(positive_result_times, positive_result_times[1:])
    ]
    long_feedback_gaps = [gap for gap in feedback_intervals if gap > 25_000_000]
    gap_correlations = Counter()
    for left, right in zip(positive_result_times, positive_result_times[1:]):
        if right - left <= 25_000_000:
            continue
        in_target_window = [
            row for row in terminal_rows
            if (target := _int(row, "target_ns")) is not None
            and left < target < right
        ]
        in_target_results = [
            row for row in feedback_results
            if (target := _int(row, "target_ns")) is not None
            and left < target < right
        ]
        if any((_int(row, "actual_presented_ns") or 0) <= 0
               for row in in_target_results):
            gap_correlations["zero_feedback_in_target_window"] += 1
        else:
            missed = [
                row for row in in_target_window
                if (_text(row, "terminal_class") or "") in {
                    "source_deadline_missed", "generated_deadline_dropped",
                    "drawable_unavailable", "command_buffer_unavailable",
                    "queue_overflow", "worker_exception", "stale_epoch",
                    "shutdown_drain",
                }
            ]
            if missed:
                for row in missed:
                    gap_correlations[_text(row, "terminal_class").lower()] += 1
            else:
                gap_correlations["positive_feedback_spacing_or_target_phase"] += 1
    selected_kinds = Counter(_kind(row) for row in stage_rows)
    late_generated_feedback = sum(
        (_int(row, "actual_presented_ns") or 0) > (_int(row, "source_b_scheduled_ns") or 0)
        for row in feedback_results
        if _kind(row) == "generated"
        and (_int(row, "source_b_scheduled_ns") or 0) > 0
        and (_int(row, "actual_presented_ns") or 0) > 0
    )
    source_attempts_by_id: dict[tuple[str, str, int], list[dict[str, str]]] = defaultdict(list)
    for row in feedback_results:
        source_id = _int(row, "source_id") or 0
        target_ns = _int(row, "target_ns") or 0
        if _kind(row) != "source" or source_id <= 0 or target_ns <= 0:
            continue
        key = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0", source_id)
        source_attempts_by_id[key].append(row)
    generated_feedback_rows = [
        row for row in feedback_results if _kind(row) == "generated"
    ]
    generated_b_target_after_count = 0
    generated_b_target_before_count = 0
    generated_b_target_unavailable = 0
    generated_after_b_positive_count = 0
    generated_b_positive_unavailable = 0
    generated_timestamp_order = Counter()
    for row in generated_feedback_rows:
        actual_ns = _int(row, "actual_presented_ns") or 0
        if actual_ns <= 0:
            continue
        pair_a = _int(row, "pair_a_id") or 0
        pair_b = _int(row, "pair_b_id") or 0
        owner_key = (_text(row, "presenter_id") or "0", _text(row, "epoch") or "0")
        source_a_attempts = source_attempts_by_id.get((*owner_key, pair_a), [])
        source_b_attempts = source_attempts_by_id.get((*owner_key, pair_b), [])
        source_a_positive = [
            value for source_row in source_a_attempts
            if (value := _int(source_row, "actual_presented_ns") or 0) > 0
        ]
        source_b_positive = [
            value for source_row in source_b_attempts
            if (value := _int(source_row, "actual_presented_ns") or 0) > 0
        ]
        a_actual_ns = min(source_a_positive, default=0)
        b_actual_ns = min(source_b_positive, default=0)

        if a_actual_ns <= 0:
            generated_timestamp_order["missing_A"] += 1
        if b_actual_ns <= 0:
            generated_timestamp_order["missing_B"] += 1
        if a_actual_ns <= 0 and b_actual_ns <= 0:
            generated_timestamp_order["missing_both"] += 1
        elif a_actual_ns > 0 and b_actual_ns > 0:
            generated_timestamp_order["both_endpoints_positive"] += 1
            if actual_ns < a_actual_ns:
                generated_timestamp_order["before_A"] += 1
            elif actual_ns > b_actual_ns:
                generated_timestamp_order["after_B"] += 1
            elif actual_ns == a_actual_ns or actual_ns == b_actual_ns:
                generated_timestamp_order["tied_endpoint"] += 1
            else:
                generated_timestamp_order["between_A_B"] += 1

        if not source_b_attempts:
            generated_b_target_unavailable += 1
            generated_b_positive_unavailable += 1
            continue
        source_attempts = sorted(source_b_attempts, key=lambda item: (
            _int(item, "presentation_seq") or 0,
            _int(item, "callback_ns") or 0,
        ))
        first_b = source_attempts[0]
        b_target_ns = _int(first_b, "target_ns") or 0
        if b_target_ns <= 0:
            generated_b_target_unavailable += 1
        elif actual_ns > b_target_ns:
            generated_b_target_after_count += 1
        else:
            generated_b_target_before_count += 1
        first_b_actual_ns = _int(first_b, "actual_presented_ns") or 0
        if first_b_actual_ns <= 0:
            generated_b_positive_unavailable += 1
        elif actual_ns > first_b_actual_ns:
            generated_after_b_positive_count += 1

    generated_request_rows = [
        row for row in native if row.get("event") == "generated_requested"
    ]
    generated_ready_rows = [
        row for row in native if row.get("event") == "generated_ready"
    ]
    generated_selected_rows = [
        row for row in native if row.get("event") == "generated_selected"
    ]
    generated_submitted_rows = [
        row for row in native if row.get("event") == "generated_presentation_submitted"
    ]
    generated_drop_rows = [
        row for row in native if row.get("event") == "generated_dropped"
    ]
    submitted_generated_pairs = {
        (
            _text(row, "presenter_id") or "0",
            _text(row, "epoch") or "0",
            _int(row, "pair_a_id") or 0,
            _int(row, "pair_b_id") or 0,
        )
        for row in generated_submitted_rows
        if (_int(row, "pair_a_id") or 0) > 0
        and (_int(row, "pair_b_id") or 0) == (_int(row, "pair_a_id") or 0) + 1
    }
    requested_generated_pairs = {
        (
            _text(row, "presenter_id") or "0",
            _text(row, "epoch") or "0",
            _int(row, "pair_a_id") or 0,
            _int(row, "pair_b_id") or 0,
        )
        for row in generated_request_rows
        if (_int(row, "pair_a_id") or 0) > 0
        and (_int(row, "pair_b_id") or 0) == (_int(row, "pair_a_id") or 0) + 1
    }
    dropped_generated_pairs = {
        (
            _text(row, "presenter_id") or "0",
            _text(row, "epoch") or "0",
            _int(row, "pair_a_id") or 0,
            _int(row, "pair_b_id") or 0,
        )
        for row in generated_drop_rows
        if (_int(row, "pair_a_id") or 0) > 0
        and (_int(row, "pair_b_id") or 0) == (_int(row, "pair_a_id") or 0) + 1
    }
    generated_dropped_before_submit = len(
        (dropped_generated_pairs - submitted_generated_pairs) & requested_generated_pairs
    )
    submitted_source_count = counts["real_presentation_submitted"]
    submitted_generated_count = counts["generated_presentation_submitted"]
    gpu_completed_count = gpu_result_status["completed"]
    gpu_failed_count = gpu_result_status["failed"]
    positive_feedback_count = len(positive_result_rows)
    feedback_duration = elapsed_s
    zero_in_accepted = source_zero_ids & accepted_ids
    zero_gpu_text = ",".join(f"{key}:{zero_gpu_status[key]}" for key in sorted(zero_gpu_status)) or "none"
    zero_circuit_text = ",".join(f"{key}:{zero_circuit_state[key]}" for key in sorted(zero_circuit_state)) or "unavailable"
    zero_lifecycle_text = ",".join(
        f"{key}:{zero_lifecycle_epoch[key]}" for key in sorted(zero_lifecycle_epoch)
    ) or "unavailable"
    generated_failures = sum(row.get("event") in GENERATED_FAILURE_EVENTS for row in native)
    generated_failure_drop_records = sum(
        row.get("event") == "generated_dropped"
        and any(token in row.get("detail", "").lower()
                for token in ("failed", "failure", "error", "exception"))
        for row in native
    )
    generated_presentation_failures = sum(
        row.get("event") == "presentation_failed" and _kind(row) == "generated"
        for row in native
    )
    generated_drop_count = sum(row.get("event") == "generated_dropped" for row in native)
    generated_zero_feedback = sum(_kind(row) == "generated" for _, row in zero_rows)
    source_zero_feedback = sum(_kind(row) == "source" for _, row in zero_rows)

    lines = [
        f"app_presents={len(presents)} duration_s={elapsed_s:.3f} rate_hz={app_rate:.3f}",
        f"app_present_errors={app_errors} "
        f"app_present_nonzero_success_statuses={app_nonzero_success_statuses} "
        f"app_present_occluded_statuses={app_occluded_statuses} "
        f"buffer0_query_errors={buffer_errors}",
        "app_window_state: "
        f"visible={window_visible[1]} hidden={window_visible[0]} "
        f"iconic={window_iconic[1]} not_iconic={window_iconic[0]} "
        f"foreground={window_foreground[1]} background={window_foreground[0]} "
        f"unavailable_visible={window_visible[None]} "
        f"unavailable_iconic={window_iconic[None]} "
        f"unavailable_foreground={window_foreground[None]}",
        f"native_events={len(native)} positive_display_events={len(visible)} display_rate_hz={display_rate:.3f}",
        "native_event_counts=" + ",".join(f"{key}:{counts[key]}" for key in sorted(counts)),
        f"accepted_source_ids={len(accepted_ids)} acceptance_mode={acceptance_semantics}",
        "source_acceptance_coverage=successful_copy_records_only"
        if acceptance_semantics == "copy_completion_event_only"
        else "source_acceptance_coverage=ownership_acceptance_records",
        f"source_terminal_mode={'explicit' if explicit_terminal_mode else 'positive_source_display_fallback'} "
        f"missing_terminal_ids={len(missing_terminal_ids)} missing_terminal_records={missing_terminal_records} "
        f"duplicate_terminal_ids={len(duplicate_terminal_ids)} duplicate_terminal_records={duplicate_terminal_records} "
        f"duplicate_accept_ids={len(duplicate_accept_ids)} orphan_terminal_ids={len(orphan_terminal_ids)} "
        f"lost_source_ids={len(lost_source_ids)}",
        "lost_source_id_list=" + (_id_list(lost_source_ids) if lost_source_ids else "none"),
        f"source_terminal_records={sum(terminal_event_counts.values())} "
        f"safe_terminal_ids={len(safe_terminal_accepted_ids)} "
        f"unsafe_terminal_ids={len(unsafe_accepted_ids)} "
        f"source_safety_violation_ids={len(safety_violation_accepted_ids)}",
        f"submitted_unconfirmed_accepted_ids={len(submitted_unconfirmed_ids & accepted_ids)}",
        "unsafe_terminal_id_list=" + (_id_list(unsafe_accepted_ids) if unsafe_accepted_ids else "none"),
        "source_safety_violation_id_list=" +
        (_id_list(safety_violation_accepted_ids) if safety_violation_accepted_ids else "none"),
        f"source_state_transition_records={sum(source_state_transitions.values())} "
        "transitions=" + (",".join(f"{key}:{source_state_transitions[key]}"
                                    for key in sorted(source_state_transitions)) or "unavailable"),
        "source_terminal_details=" + (",".join(f"{key}:{terminal_details[key]}"
                                                  for key in sorted(terminal_details)) or "unavailable"),
        f"source_zero_feedback_ids={len(source_zero_ids)} accepted_source_zero_feedback={len(zero_in_accepted)} "
        f"lost_ids_with_zero_feedback={len(zero_source_lost_overlap)}",
        f"displayed_generated_frames={displayed_g} immediate_A_G_B_triples={immediate_triples} "
        f"unbracketed_G={displayed_g - immediate_triples} immediate_missing_A={immediate_missing_a} "
        f"immediate_missing_B={immediate_missing_b} immediate_missing_both={immediate_missing_both}",
        f"nearest_source_pair_brackets={exact_brackets} nearest_missing_A={nearest_missing_a} "
        f"nearest_missing_B={nearest_missing_b} nearest_unmatched_endpoints_with_zero_feedback={len(unbracketed_zero_overlap)} "
        f"nearest_missing_both={nearest_missing_both} "
        f"immediate_missing_endpoints_with_zero_feedback={len(immediate_zero_overlap)}",
        f"malformed_requested_pairs={malformed_requested_pairs} malformed_displayed_pairs={malformed_displayed_pairs} "
        f"source_regressions={source_regressions} repeated_source_events={repeated_sources}",
        f"generated_drops={generated_drop_count} generated_failures={generated_failures} "
        f"generated_failure_drop_details={generated_failure_drop_records} "
        f"generated_presentation_failures={generated_presentation_failures}",
        f"generated_zero_presentedTime={generated_zero_feedback} source_zero_presentedTime={source_zero_feedback}",
        f"zero_presentedTime={len(zero_rows)} by_kind=" + ",".join(
            f"{key}:{zero_by_kind[key]}" for key in sorted(zero_by_kind)),
        f"zero_presentedTime_gpu_status={zero_gpu_text} circuit_state={zero_circuit_text} "
        f"lifecycle_epoch={zero_lifecycle_text} "
        f"zero_feedback_presentation_records={len(zero_feedback_records)} "
        f"zero_feedback_with_stage_records={len(zero_stage_records)}",
        show_metric("display_interval_ms", display_intervals),
        show_metric("history_copy_completion_ms", copy_latencies),
        show_metric("generation_submit_to_ready_ms", generation_latencies),
        show_metric("callback_to_worker_wake_ms", callback_worker),
        show_metric("callback_to_selection_ms", callback_selection),
        show_metric("device_mutex_wait_ms", mutex_wait),
        show_metric("device_mutex_hold_ms", mutex_hold),
        show_metric("presenter_encode_ms", encode_time),
        show_metric("callback_to_gpu_submit_ms", callback_gpu_submit),
        show_metric("gpu_submit_to_completion_ms", gpu_complete),
        show_metric("callback_to_present_call_ms", callback_present_call),
        show_metric("gpu_submit_to_present_call_ms", gpu_submit_present_call),
        show_metric("present_call_to_feedback_ms", present_feedback),
        show_metric("callback_to_feedback_ms", callback_feedback),
        show_metric("positive_presentedTime_gap_ms", feedback_intervals),
        show_metric("presentedTime_minus_target_ms", actual_target_offset),
        show_metric("legacy_precommit_record_to_gpu_completion_ms", legacy_submit_complete),
        show_metric("callback_to_legacy_precommit_record_ms", legacy_callback_submit),
        show_metric("display_callback_duration_us", tick_callback_durations, divisor=1_000.0),
        show_metric("tick_queue_depth", queue_depths, divisor=1.0),
        f"callback_tick_ledger: status={callback_ledger['status']} "
        f"callback_count={callback_count_text} "
        f"callback_hz={callback_hz_text} terminal_count={terminal_count_text} "
        f"callback_terminal_delta={summary_terminal_delta if callback_summaries else 'unavailable'} "
        f"terminal_rows={len(terminal_rows)} unique_ticks={len(terminal_tick_counts)} "
        f"duplicate_tick_terminals={terminal_duplicates} "
        f"terminal_rows_without_tick_id={callback_ledger['missing_tick_ids']} "
        f"terminal_rows_without_class={callback_ledger['missing_classes']} "
        f"malformed_callback_summaries={callback_ledger['malformed_summaries']} "
        f"callback_summary_counter_regressions={callback_ledger['counter_regressions']} "
        f"outstanding_drawable_high_water={summary_high_water if callback_summaries else 'unavailable'} "
        f"classes=" + (",".join(f"{key}:{terminal_classes[key]}" for key in sorted(terminal_classes)) or "unavailable"),
        f"display_drawable_feedback: submitted_source={submitted_source_count} "
        f"submitted_generated={submitted_generated_count} "
        f"gpu_completed={gpu_completed_count} gpu_failed={gpu_failed_count} "
        f"positive_presentedTime={positive_feedback_count} "
        f"zero_presentedTime={len(zero_result_rows)} "
        f"positive_feedback_hz={positive_feedback_count / feedback_duration:.3f} "
        f"source_submission_hz={submitted_source_count / feedback_duration:.3f} "
        f"generated_submission_hz={submitted_generated_count / feedback_duration:.3f}"
        if feedback_duration > 0 else
        f"display_drawable_feedback: submitted_source={submitted_source_count} "
        f"submitted_generated={submitted_generated_count} gpu_completed={gpu_completed_count} "
        f"gpu_failed={gpu_failed_count} positive_presentedTime={positive_feedback_count} "
        f"zero_presentedTime={len(zero_result_rows)} rates=unavailable",
        f"generated_frame_feedback: requested={len(generated_request_rows)} "
        f"ready={len(generated_ready_rows)} selected={len(generated_selected_rows)} "
        f"submitted={len(generated_submitted_rows)} "
        f"positive={sum((_int(row, 'actual_presented_ns') or 0) > 0 for row in generated_feedback_rows)} "
        f"zero={sum((_int(row, 'actual_presented_ns') or 0) <= 0 for row in generated_feedback_rows)} "
        f"dropped_before_submit_unique_pairs={generated_dropped_before_submit}",
        "gpu_result_status=" + (",".join(f"{key}:{gpu_result_status[key]}" for key in sorted(gpu_result_status)) or "unavailable"),
        f"long_positive_feedback_gaps_over_25ms={len(long_feedback_gaps)} "
        "correlated_target_window_outcomes=" +
        (",".join(f"{key}:{gap_correlations[key]}" for key in sorted(gap_correlations)) or "none"),
        f"selection_counts_by_kind=" +
        (",".join(f"{key}:{selected_kinds[key]}" for key in sorted(selected_kinds)) or "unavailable") +
        f" late_G_feedback_after_source_B_schedule={late_generated_feedback} "
        f"late_G_feedback_after_B_display_target={generated_b_target_after_count} "
        f"before_B_display_target={generated_b_target_before_count} "
        f"B_target_unavailable={generated_b_target_unavailable} "
        f"late_G_after_B_positive_feedback={generated_after_b_positive_count} "
        f"B_positive_feedback_unavailable={generated_b_positive_unavailable}",
        "positive_generated_timestamp_order=" + (
            ",".join(
                f"{item}:{generated_timestamp_order[item]}"
                for item in (
                    "both_endpoints_positive", "between_A_B", "before_A", "after_B",
                    "tied_endpoint", "missing_A", "missing_B", "missing_both",
                )
                if generated_timestamp_order[item]
            ) or "none"
        ),
    ]
    return "\n".join(lines) + "\n"


def summarize(native_path: Path, app_path: Path) -> str:
    return summarize_rows(rows(native_path), rows(app_path))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("native_csv", type=Path)
    parser.add_argument("app_csv", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = summarize_rows(rows(args.native_csv), rows(args.app_csv))
    if args.output:
        args.output.write_text(summary)
    print(summary, end="")
    ledger_line = next(
        (line for line in summary.splitlines()
         if line.startswith("callback_tick_ledger: ")),
        "",
    )
    return 1 if "status=MISMATCH" in ledger_line else 0


if __name__ == "__main__":
    raise SystemExit(main())
