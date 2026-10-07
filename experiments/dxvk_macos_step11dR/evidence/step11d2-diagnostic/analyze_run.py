#!/usr/bin/env python3
"""Summarize the Step 11D.2 common-epoch DXVK/Metal diagnostic log."""
import collections
import json
import math
import re
import statistics
import sys
from pathlib import Path


KV = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]+)")
TIMELINE = re.compile(r"R5Timeline event=([^ ]+)")
METAL = re.compile(r"R5METAL event=([^ ]+)")
JOB = "InteropJobId"


def parse_line(line):
    return {k: v.rstrip(",;") for k, v in KV.findall(line)}


def as_int(value):
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        try:
            return int(value)
        except ValueError:
            return None


def percentile(values, p):
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * p / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def main():
    root = Path(sys.argv[1])
    lines = (root / "runtime.log").read_text(errors="replace").splitlines()
    jobs = collections.defaultdict(dict)
    frames = collections.defaultdict(dict)
    internal_presenter_events = collections.defaultdict(dict)
    raw_stalls = []
    internal_present_to_job = {}
    pending_wsi_retirements = []
    for line in lines:
        match = TIMELINE.search(line)
        if match:
            event = match.group(1)
            d = parse_line(line)
            if event == "wsi_retirement":
                pending_wsi_retirements.append(d)
                continue
            key = as_int(d.get(JOB))
            if key == 0 and d.get(JOB) is not None:
                continue
            if key is None:
                key = as_int(d.get("AppFrameId", d.get("app")))
                if key is not None:
                    table = frames
                else:
                    key = as_int(d.get("internal", d.get("InternalPresentId")))
                    table = internal_presenter_events
            else:
                table = jobs
            if key is not None:
                table[key].setdefault(event, []).append(d)
            continue
        match = METAL.search(line)
        if match:
            event = match.group(1)
            d = parse_line(line)
            key = as_int(d.get(JOB))
            if key is not None and key > 0:
                jobs[key].setdefault("metal_" + event, []).append(d)
            continue
        if "MetalInterop: WSI-submit InteropJobId=" in line:
            d = parse_line(line)
            key = as_int(d.get(JOB))
            if key is not None and key > 0:
                jobs[key].setdefault("wsi_submit", []).append(d)
                internal = as_int(d.get("InternalPresentId"))
                if internal is not None:
                    internal_present_to_job[internal] = key
                    for name, rows in internal_presenter_events.pop(internal, {}).items():
                        jobs[key].setdefault(name, []).extend(rows)
            continue
        if "R5Timeline event=wsi_retirement" in line:
            d = parse_line(line)
            pending_wsi_retirements.append(d)
            continue
        if "R5STALL event=" in line:
            raw_stalls.append(line.strip())

    for d in pending_wsi_retirements:
        internal = as_int(d.get("InternalPresentId"))
        key = internal_present_to_job.get(internal)
        if key is not None:
            jobs[key].setdefault("wsi_retirement", []).append(d)

    def first(job, event):
        rows = job.get(event, [])
        return rows[0] if rows else {}

    def t(job, event, field="t_ns"):
        return as_int(first(job, event).get(field))

    invalid_negative_durations = 0
    def dur(a, b):
        nonlocal invalid_negative_durations
        if a is None or b is None:
            return None
        if b < a:
            invalid_negative_durations += 1
            return None
        return (b - a) / 1_000_000.0

    stages = collections.defaultdict(list)
    per_job = {}
    provider_depths = []
    internal_depths = []
    for jid, job in jobs.items():
        claim = t(job, "history_slot_claim")
        copy_begin = t(job, "vulkan_copy_record_begin")
        copy_end = t(job, "vulkan_copy_record_end_ready_recorded")
        qinsert = t(job, "source_present_queue_insert")
        qtake = t(job, "source_present_worker_take")
        present_begin = t(job, "source_vk_present_begin")
        present_end = t(job, "source_vk_present_end")
        dispatch = t(job, "metal_commit_dispatch")
        submit_admitted = t(job, "source_submit_admitted")
        submit_insert = t(job, "source_submit_queue_insert")
        submit_take = t(job, "source_submit_worker_take")
        submit_queue_lock = t(job, "source_vk_submit_queue_lock")
        submit_begin = t(job, "source_vk_submit_begin")
        ready_submit = t(job, "vulkan_ready_signal_submit")
        source_present_insert = t(job, "source_present_queue_insert")
        source_present_take = t(job, "source_present_worker_take")
        worker_enter = t(job, "commit_worker_enter")
        poll_done = t(job, "commit_poll_slots_done")
        bridge = first(job, "bridge_call")
        metal_submit = first(job, "metal_submit")
        completion = first(job, "metal_completion")
        provider_entry = as_int(bridge.get("provider_entry_ns"))
        cb_begin = as_int(metal_submit.get("cbCreateBegin"))
        cb_created = as_int(metal_submit.get("cbCreated"))
        encoder_begin = as_int(metal_submit.get("encoderBegin"))
        encoder_created = as_int(metal_submit.get("encoderCreated"))
        encode_begin = as_int(metal_submit.get("encodeBegin"))
        encode_end = as_int(metal_submit.get("encodeEnd"))
        commit_begin = as_int(metal_submit.get("commitBegin"))
        commit_end = as_int(metal_submit.get("commitEnd"))
        provider_return = as_int(bridge.get("provider_return_ns"))
        unix_return = as_int(bridge.get("unix_return_ns"))
        pe_return = as_int(bridge.get("pe_return_ns"))
        end = as_int(bridge.get("t_end_ns"))
        completion_ns = as_int(completion.get("t_ns"))
        done_observed = t(job, "metal_done_observed")
        consumed = t(job, "done_to_consumed_submit")
        consumer_end = t(job, "consumer_submit_end")
        wsi_submit = first(job, "wsi_submit")
        wsi_retirement = t(job, "wsi_retirement")
        release = t(job, "history_output_slot_release")
        terminal = t(job, "job_terminal")
        internal_present_return = t(job, "internal_wsi_present_return")
        app_id = as_int(first(job, "history_slot_claim").get("AppFrameId"))
        frame = frames.get(app_id, {})
        source_begin = as_int(first(frame, "source_present_begin").get("t_ns"))
        source_end = as_int(first(frame, "source_present_end").get("t_ns"))
        context_begin = as_int(first(frame, "context_lock_acquired").get("begin_ns"))
        context_acquired = as_int(first(frame, "context_lock_acquired").get("t_ns"))
        flush_begin = as_int(first(frame, "execute_flush_return").get("begin_ns"))
        flush_end = as_int(first(frame, "execute_flush_return").get("t_ns"))
        source_acquire_begin = as_int(first(frame, "source_wsi_acquire_begin").get("t_ns"))
        source_acquire_end = as_int(first(frame, "source_wsi_acquire_end").get("t_ns"))
        app_present_return = as_int(first(frame, "present_image_return").get("t_ns"))
        source_latency_return = as_int(first(frame, "sync_frame_latency_return").get("t_ns"))
        internal_insert = t(job, "internal_queue_insert")
        internal_take = t(job, "internal_worker_take")
        internal_acquire_begin = t(job, "internal_wsi_acquire_begin")
        internal_acquire_end = t(job, "internal_wsi_acquire_end")
        internal_present_begin = t(job, "internal_wsi_present_begin")
        internal_target_ns = as_int(first(job, "internal_queue_insert").get("target_ns"))
        internal_deadline_ns = as_int(first(job, "internal_queue_insert").get("deadline_ns"))
        # The diagnostic source logs m_internalQueue.size() immediately before
        # push(), so queueDepth=0 means this accepted request makes occupancy 1.
        # Report occupancy including the request being inserted.
        internal_depth_before_insert = as_int(
            first(job, "internal_queue_insert").get("queueDepth"))
        internal_depth = (internal_depth_before_insert + 1
                          if internal_depth_before_insert is not None else None)
        provider_depth = as_int(metal_submit.get("queueDepth"))
        if internal_depth is not None:
            internal_depths.append(internal_depth)
        if provider_depth is not None:
            provider_depths.append(provider_depth)

        values = {
            "capture_request_to_history_claim": dur(
                as_int(first(frame, "capture_requested").get("t_ns")), claim),
            "history_claim_to_copy_record_begin": dur(claim, copy_begin),
            "history_claim_to_copy_record_end_ready_recorded": dur(claim, copy_end),
            "history_claim_to_READY_submit_complete": dur(claim, ready_submit),
            "copy_submit_to_READY_available": dur(submit_begin, ready_submit),
            "READY_available_to_provider_entry": dur(ready_submit, provider_entry),
            "READY_available_to_metal_commit": dur(ready_submit, commit_end),
            "copy_record_end_to_source_submit_admitted": dur(copy_end, submit_admitted),
            "source_submit_admission_wait": (
                as_int(first(job, "source_submit_admitted").get("admission_wait_us")) / 1000.0
                if first(job, "source_submit_admitted").get("admission_wait_us") is not None else None),
            "source_submit_admitted_to_queue_insert": dur(submit_admitted, submit_insert),
            "source_submit_queue_wait": dur(submit_insert, submit_take),
            "source_submit_worker_to_queue_lock": dur(submit_take, submit_queue_lock),
            "source_submit_queue_lock_to_vk_submit": dur(submit_queue_lock, submit_begin),
            "vulkan_submit_api": dur(submit_begin, ready_submit),
            "READY_submit_to_source_present_queue_insert": dur(ready_submit, source_present_insert),
            "source_present_queue_insert_to_worker_take": dur(source_present_insert, source_present_take),
            "copy_record_end_to_source_present_queue_insert": dur(copy_end, qinsert),
            "source_present_queue_wait": dur(qinsert, qtake),
            "source_present_worker_to_vk_present_begin": dur(qtake, present_begin),
            "source_vk_present": dur(present_begin, present_end),
            "source_vk_present_end_to_metal_dispatch": dur(present_end, dispatch),
            "source_present_api_begin_to_end": dur(source_begin, source_end),
            "source_context_lock_wait": dur(context_begin, context_acquired),
            "source_execute_flush": dur(flush_begin, flush_end),
            "source_wsi_acquire": dur(source_acquire_begin, source_acquire_end),
            "source_acquire_surface_lock_wait": dur(
                as_int(first(frame, "source_acquire_surface_lock").get("begin_ns")),
                as_int(first(frame, "source_acquire_surface_lock").get("t_ns"))),
            "source_acquire_present_pending_wait": dur(
                as_int(first(frame, "source_acquire_present_pending_wait").get("begin_ns")),
                as_int(first(frame, "source_acquire_present_pending_wait").get("t_ns"))),
            "source_acquire_swapchain_update": dur(
                as_int(first(frame, "source_acquire_swapchain_update").get("begin_ns")),
                as_int(first(frame, "source_acquire_swapchain_update").get("t_ns"))),
            "source_acquire_fence_wait": dur(
                as_int(first(frame, "source_acquire_fence_wait").get("begin_ns")),
                as_int(first(frame, "source_acquire_fence_wait").get("t_ns"))),
            "source_vk_acquire_next_image": dur(
                as_int(first(frame, "source_vk_acquire_next_image").get("begin_ns")),
                as_int(first(frame, "source_vk_acquire_next_image").get("t_ns"))),
            "source_present_call_return_to_frame_latency": dur(app_present_return, source_latency_return),
            "source_vk_queue_present_call": dur(
                as_int(first(frame, "vk_queue_present_begin").get("t_ns")),
                as_int(first(frame, "vk_queue_present_end").get("t_ns"))),
            "source_presenter_next_fence_wait": dur(
                as_int(first(frame, "presenter_next_fence_wait").get("begin_ns")),
                as_int(first(frame, "presenter_next_fence_wait").get("t_ns"))),
            "source_presenter_next_acquire": dur(
                as_int(first(frame, "presenter_next_acquire_begin").get("t_ns")),
                as_int(first(frame, "presenter_next_acquire_end").get("t_ns"))),
            "source_presenter_timing_lock_wait": dur(
                as_int(first(frame, "presenter_timing_lock").get("begin_ns")),
                as_int(first(frame, "presenter_timing_lock").get("acquired_ns"))),
            "source_presenter_frame_lock_wait": dur(
                as_int(first(frame, "presenter_frame_queue_push").get("begin_ns")),
                as_int(first(frame, "presenter_frame_queue_push").get("lock_acquired_ns"))),
            "source_presenter_frame_capacity_wait": dur(
                as_int(first(frame, "presenter_frame_queue_push").get("lock_acquired_ns")),
                as_int(first(frame, "presenter_frame_queue_push").get("queue_available_ns"))),
            "metal_dispatch_to_commit_worker_enter": dur(dispatch, worker_enter),
            "commit_worker_enter_to_slot_poll_done": dur(worker_enter, poll_done),
            "command_list_descriptor_sync": dur(
                as_int(first(job, "command_list_descriptor_sync").get("begin_ns")),
                as_int(first(job, "command_list_descriptor_sync").get("t_ns"))),
            "ready_record_to_PE_call": dur(copy_end, as_int(bridge.get("pe_begin_ns"))),
            "PE_call_to_Unix_entry": dur(as_int(bridge.get("pe_begin_ns")), as_int(bridge.get("unix_entry_ns"))),
            "Unix_entry_to_provider_entry": dur(as_int(bridge.get("unix_entry_ns")), provider_entry),
            "provider_entry_to_command_buffer_created": dur(provider_entry, cb_created),
            "command_buffer_creation": dur(cb_begin, cb_created),
            "encoder_creation": dur(encoder_begin, encoder_created),
            "metal_encoding": dur(encode_begin, encode_end),
            "encode_end_to_commit_begin": dur(encode_end, commit_begin),
            "metal_commit_call": dur(commit_begin, commit_end),
            "provider_return_after_commit": dur(commit_end, provider_return),
            "provider_return_to_Unix_return": dur(provider_return, unix_return),
            "Unix_return_to_PE_return": dur(unix_return, pe_return),
            "Metal_commit_to_completion_callback": dur(commit_end, completion_ns),
            "completion_callback_to_DONE_observed": dur(completion_ns, done_observed),
            "DONE_observed_to_consumer_submit_end": dur(done_observed, consumer_end),
            "consumer_submit_end_to_WSI_retirement": dur(consumer_end, wsi_retirement),
            "WSI_present_return_to_retirement": dur(internal_present_return, wsi_retirement),
            "consumer_submit_end_to_slot_release": dur(consumer_end, release),
            "terminal_to_slot_release": dur(terminal, release),
            "internal_queue_wait_insert_to_take": dur(internal_insert, internal_take),
            "internal_target_to_worker_take": dur(internal_target_ns, internal_take),
            "internal_worker_take_deadline_margin": dur(internal_take, internal_deadline_ns),
            "internal_worker_take_to_wsi_acquire": dur(internal_take, internal_acquire_begin),
            "internal_wsi_acquire": dur(internal_acquire_begin, internal_acquire_end),
            "internal_acquire_end_to_present_begin": dur(internal_acquire_end, internal_present_begin),
            "internal_wsi_present_api": dur(internal_present_begin, internal_present_return),
            "internal_vk_queue_present_call": dur(
                as_int(first(job, "vk_queue_present_begin").get("t_ns")),
                as_int(first(job, "vk_queue_present_end").get("t_ns"))),
            "internal_presenter_surface_lock_wait": dur(
                as_int(first(job, "presenter_surface_lock").get("begin_ns")),
                as_int(first(job, "presenter_surface_lock").get("t_ns"))),
            "internal_presenter_next_fence_wait": dur(
                as_int(first(job, "presenter_next_fence_wait").get("begin_ns")),
                as_int(first(job, "presenter_next_fence_wait").get("t_ns"))),
            "internal_presenter_next_acquire": dur(
                as_int(first(job, "presenter_next_acquire_begin").get("t_ns")),
                as_int(first(job, "presenter_next_acquire_end").get("t_ns"))),
            "history_hold_claim_to_release": dur(claim, release),
            "output_hold_claim_to_release": dur(claim, release),
            "total_job_lifetime_claim_to_terminal": dur(claim, terminal),
            "total_job_lifetime_claim_to_slot_release": dur(claim, release),
            "total_job_lifetime_claim_to_WSI_retirement": dur(claim, wsi_retirement),
            "wsi_submit_to_retirement": dur(internal_present_return, wsi_retirement),
        }
        per_job[jid] = {k: v for k, v in values.items() if v is not None}
        for name, value in values.items():
            if value is not None:
                stages[name].append(value)

        for row in job.get("vk_queue_submit2", []):
            value = dur(as_int(row.get("begin_ns")), as_int(row.get("t_ns")))
            if value is not None:
                role = row.get("submitRole", "unknown")
                queue = row.get("queue", "unknown")
                stages["vk_queue_submit2_api"].append(value)
                stages[f"vk_queue_submit2_api_{role}_{queue}"].append(value)
        for event, stage in (("slot_provider_poll", "slot_provider_status_query"),
                             ("slot_reclaim_poll", "slot_reclaim_counter_query")):
            for row in job.get(event, []):
                value = dur(as_int(row.get("begin_ns")), as_int(row.get("t_ns")))
                if value is not None:
                    stages[stage].append(value)

        reclaim_polls = job.get("slot_reclaim_poll", [])
        ready_polls = [row for row in reclaim_polls
                       if as_int(row.get("status")) == 0
                       and as_int(row.get("semaphoreValue")) is not None
                       and as_int(row.get("reclaimValue")) is not None
                       and as_int(row.get("semaphoreValue")) >= as_int(row.get("reclaimValue"))]
        first_poll_ns = min((as_int(row.get("t_ns")) for row in reclaim_polls
                             if as_int(row.get("t_ns")) is not None), default=None)
        first_ready_ns = min((as_int(row.get("t_ns")) for row in ready_polls
                              if as_int(row.get("t_ns")) is not None), default=None)
        per_job[jid]["slot_reclaim_poll_count"] = len(reclaim_polls)
        first_poll_delay = dur(terminal, first_poll_ns)
        first_ready_delay = dur(consumer_end, first_ready_ns)
        if first_poll_delay is not None:
            per_job[jid]["terminal_to_first_reclaim_poll_ms"] = first_poll_delay
            stages["terminal_to_first_reclaim_poll"].append(first_poll_delay)
        if first_ready_delay is not None:
            per_job[jid]["consumer_submit_to_counter_ready_ms"] = first_ready_delay
            stages["consumer_submit_to_counter_ready"].append(first_ready_delay)

    report = {
        "run": root.name,
        "job_count": len(jobs),
        "completed_metal_submits": sum("metal_submit" in x for x in jobs.values()),
        "completion_callbacks": sum("metal_completion" in x for x in jobs.values()),
        "terminal_jobs": sum("job_terminal" in x for x in jobs.values()),
        "slot_releases": sum("history_output_slot_release" in x for x in jobs.values()),
        "wsi_submitted_consumers": sum("wsi_submit" in x for x in jobs.values()),
        "wsi_retirements": sum("wsi_retirement" in x for x in jobs.values()),
        "stall_records": raw_stalls,
        "negative_timestamp_order_pairs_filtered": invalid_negative_durations,
        "terminal_reasons": dict(collections.Counter(
            row.get("terminal", "unknown")
            for job in jobs.values() for row in job.get("job_terminal", []))),
        "no_free_history_count": sum("reason=no-free-history-output-slot" in line for line in lines),
        "internal_queue_full_count": sum("reason=queue-full" in line for line in lines),
        "cadence": {},
        "stages_ms": {
            name: {
                "n": len(vals),
                "p50": round(percentile(vals, 50), 4),
                "p95": round(percentile(vals, 95), 4),
                "p99": round(percentile(vals, 99), 4),
                "max": round(max(vals), 4),
            }
            for name, vals in sorted(stages.items())
        },
            "jobs": per_job,
        "queue_depths": {
                "provider_pending_max": max(provider_depths) if provider_depths else None,
                "provider_pending_histogram": dict(collections.Counter(provider_depths)),
                "internal_pending_max": max(internal_depths) if internal_depths else None,
                "internal_pending_histogram": dict(collections.Counter(internal_depths)),
                "internal_queue_depth_semantics": "post-insert occupancy; input queueDepth is measured before push",
                "internal_queue_capacity": 1,
        },
        "timed_waits": {
            "count": sum(len(x.get("internal_timed_wait_return", [])) for x in jobs.values()),
            "source_or_stop_wake_count": sum(
                str(row.get("sourceOrStopWake", "")).lower() in ("1", "true")
                for x in jobs.values() for row in x.get("internal_timed_wait_return", [])),
            "wait_duration_ms": [
                round((as_int(row.get("t_ns")) - as_int(row.get("begin_ns"))) / 1_000_000.0, 4)
                for x in jobs.values() for row in x.get("internal_timed_wait_return", [])
                if as_int(row.get("t_ns")) is not None and as_int(row.get("begin_ns")) is not None
            ],
            "target_to_wait_return_ms": [
                round((as_int(row.get("t_ns")) - as_int(row.get("target_ns"))) / 1_000_000.0, 4)
                for x in jobs.values() for row in x.get("internal_timed_wait_return", [])
                if as_int(row.get("t_ns")) is not None and as_int(row.get("target_ns")) is not None
            ],
        },
        }
    result_path = root / "result.json"
    if result_path.exists():
        run_result = json.loads(result_path.read_text())
        duration = run_result.get("duration_seconds")
        internal = run_result.get("internal_metal_presents")
        source = run_result.get("source_present_rows")
        if duration and isinstance(source, int) and isinstance(internal, int):
            report["cadence"] = {
                "duration_seconds": duration,
                "source_present_rows": source,
                "internal_metal_presents": internal,
                "combined_wsi_per_second": round((source + internal) / duration, 3),
            }
    (root / "timeline_summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"{root.name}: jobs={report['job_count']} Metal commits={report['completed_metal_submits']} "
          f"terminals={report['terminal_jobs']} releases={report['slot_releases']} "
          f"WSI submits={report['wsi_submitted_consumers']} retirements={report['wsi_retirements']} stalls={len(raw_stalls)} "
          f"no_free={report['no_free_history_count']} queue_full={report['internal_queue_full_count']} "
          f"combined_wsi_s={report['cadence'].get('combined_wsi_per_second')}")
    print("stage_ms,p50,p95,p99,max,n")
    for name, stats in report["stages_ms"].items():
        print(f"{name},{stats['p50']},{stats['p95']},{stats['p99']},{stats['max']},{stats['n']}")
    for line in raw_stalls:
        print("STALL " + line)


if __name__ == "__main__":
    main()
