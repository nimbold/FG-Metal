#include "framegen/pacing.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <string>

namespace framegen::pacing {
namespace {

constexpr auto kUnknownTimestamp = std::numeric_limits<std::int64_t>::min();
constexpr std::size_t kSourceHistoryLimit = 64;

std::int64_t saturating_add(std::int64_t a, std::int64_t b) noexcept {
    const __int128 sum = static_cast<__int128>(a) + static_cast<__int128>(b);
    if (sum > std::numeric_limits<std::int64_t>::max()) {
        return std::numeric_limits<std::int64_t>::max();
    }
    if (sum < std::numeric_limits<std::int64_t>::min() + 1) {
        return std::numeric_limits<std::int64_t>::min() + 1;
    }
    return static_cast<std::int64_t>(sum);
}

std::optional<std::int64_t> positive_difference(std::int64_t end,
                                                std::int64_t start) noexcept {
    const __int128 delta = static_cast<__int128>(end) -
                           static_cast<__int128>(start);
    if (delta <= 0 || delta > std::numeric_limits<std::int64_t>::max()) {
        return std::nullopt;
    }
    return static_cast<std::int64_t>(delta);
}

std::int64_t interpolate_timestamp(std::int64_t start, std::int64_t end,
                                   float fraction) noexcept {
    const long double delta = static_cast<long double>(end) -
                              static_cast<long double>(start);
    const long double value = static_cast<long double>(start) +
                              delta * static_cast<long double>(fraction);
    const auto rounded = std::round(value);
    if (rounded >= static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return std::numeric_limits<std::int64_t>::max();
    }
    if (rounded <= static_cast<long double>(std::numeric_limits<std::int64_t>::min() + 1)) {
        return std::numeric_limits<std::int64_t>::min() + 1;
    }
    return static_cast<std::int64_t>(rounded);
}

std::int64_t percentile(std::deque<std::int64_t> values, double p) {
    if (values.empty()) {
        return 0;
    }
    std::sort(values.begin(), values.end());
    const auto index = static_cast<std::size_t>(std::ceil(
        p * static_cast<double>(values.size()))) - 1;
    return values[std::min(index, values.size() - 1)];
}

Error invalid(std::string message) {
    return Error{ErrorCode::invalid_argument, std::move(message)};
}

} // namespace

PresentationScheduler::PresentationScheduler(SchedulerConfig config)
    : config_(config) {
    if (!std::isfinite(config_.interpolation_fraction) ||
        config_.interpolation_fraction <= 0.0F ||
        config_.interpolation_fraction >= 1.0F) {
        config_.interpolation_fraction = 0.5F;
    }
    config_.consecutive_misses_to_disable =
        std::max<std::uint32_t>(1, config_.consecutive_misses_to_disable);
    config_.recovery_cooldown_ns = std::max<std::int64_t>(
        1, config_.recovery_cooldown_ns);
    config_.diagnostics_capacity = std::clamp<std::size_t>(
        config_.diagnostics_capacity, 1, 4096);
}

Result<std::optional<GenerationRequest>> PresentationScheduler::submit_source_frame(
    SourceFrameStamp frame, std::int64_t now_ns) {
    if (frame.frame_id == 0 || frame.clock_domain == 0 ||
        frame.timestamp_ns == kUnknownTimestamp || now_ns == kUnknownTimestamp) {
        return std::unexpected(invalid(
            "source frame requires an ID, a clock domain, and known timestamps"));
    }

    std::scoped_lock lock(mutex_);
    if (!sources_.empty()) {
        const auto& previous = sources_.back().stamp;
        if (frame.clock_domain != clock_domain_) {
            return std::unexpected(invalid(
                "source clock domain changed; create a fresh presentation scheduler"));
        }
        const auto interval = positive_difference(frame.timestamp_ns,
                                                  previous.timestamp_ns);
        if (frame.frame_id <= previous.frame_id || !interval) {
            return std::unexpected(invalid(
                "source frame IDs must increase and timestamp intervals must be valid"));
        }
        source_period_ns_ = *interval;
    } else {
        clock_domain_ = frame.clock_domain;
    }

    ++submitted_frames_;
    last_source_timestamp_ns_ = frame.timestamp_ns;
    sources_.push_back(SourceRecord{frame});
    if (sources_.size() > kSourceHistoryLimit) {
        sources_.pop_front();
    }

    if (sources_.size() < 2) {
        return std::optional<GenerationRequest>{};
    }

    const auto& previous = sources_[sources_.size() - 2].stamp;
    const auto source_period_delta = positive_difference(frame.timestamp_ns,
                                                        previous.timestamp_ns);
    if (!source_period_delta) {
        return std::unexpected(invalid("source timestamp interval is not representable"));
    }
    const auto pair_period = *source_period_delta;
    const auto sample = interpolate_timestamp(
        previous.timestamp_ns, frame.timestamp_ns,
        config_.interpolation_fraction);
    const auto latency = output_latency_locked(pair_period);
    const auto target = saturating_add(sample, latency);
    const auto refresh = refresh_period_ns_ > 0
        ? refresh_period_ns_ : std::max<std::int64_t>(1, pair_period / 2);
    const auto guard_fraction = config_.policy == LatencyPolicy::quality
        ? 0.05 : 0.20;
    const auto guard = std::max<std::int64_t>(1, static_cast<std::int64_t>(
        std::llround(static_cast<long double>(refresh) * guard_fraction)));
    const auto generation_deadline = saturating_add(target, -guard);
    const auto presentation_deadline = saturating_add(
        target, std::max<std::int64_t>(refresh, 1));

    if (latest_opportunity_ && latest_opportunity_->desired_presentation_ns <= now_ns) {
        if (active_ && active_->request.request_id == latest_opportunity_->request_id) {
            mark_missed_locked(*active_, true, now_ns);
        }
        if (ready_ && ready_->request.request_id == latest_opportunity_->request_id) {
            mark_missed_locked(*ready_, true, now_ns);
        }
    }

    GenerationRequest request{
        .request_id = 0,
        .previous = previous,
        .current = frame,
        .interpolation_fraction = config_.interpolation_fraction,
        .interpolation_timestamp_ns = sample,
        .desired_presentation_ns = target,
        .generation_deadline_ns = generation_deadline,
        .presentation_deadline_ns = presentation_deadline,
        .timing_generation = timing_generation_,
    };
    latest_opportunity_ = request;

    if (now_ns >= request.generation_deadline_ns) {
        ++dropped_generated_frames_;
        note_unscheduled_miss_locked(request, now_ns);
        return std::optional<GenerationRequest>{};
    }
    const bool recovery_due = disabled_until_ns_ && now_ns >= *disabled_until_ns_;
    if (disabled_until_ns_ && !recovery_due) {
        ++dropped_generated_frames_;
        return std::optional<GenerationRequest>{};
    }
    if (active_ || ready_ || queue_depth_locked() >= queue_capacity_locked()) {
        ++dropped_generated_frames_;
        note_unscheduled_miss_locked(request, now_ns);
        return std::optional<GenerationRequest>{};
    }
    if (recovery_due) {
        disabled_until_ns_.reset();
        consecutive_misses_ = 0;
        ++recovery_count_;
    }
    if (next_request_id_ == 0 || next_request_id_ == UINT64_MAX) {
        return std::unexpected(Error{ErrorCode::internal_error,
            "generation request identifier sequence is exhausted"});
    }

    request.request_id = next_request_id_++;
    latest_opportunity_ = request;
    active_ = GenerationRecord{.request = request};
    ++interpolation_submissions_;
    refresh_queue_peak_locked();
    FrameTrace trace;
    trace.generation_request_id = request.request_id;
    trace.previous_source_frame_id = request.previous.frame_id;
    trace.current_source_frame_id = request.current.frame_id;
    trace.previous_source_timestamp_ns = request.previous.timestamp_ns;
    trace.current_source_timestamp_ns = request.current.timestamp_ns;
    trace.source_timestamp_ns = request.current.timestamp_ns;
    trace.interpolation_timestamp_ns = request.interpolation_timestamp_ns;
    trace.target_presentation_ns = request.desired_presentation_ns;
    trace.generation_deadline_ns = request.generation_deadline_ns;
    trace.presentation_deadline_ns = request.presentation_deadline_ns;
    trace.queue_depth = queue_depth_locked();
    append_trace_locked(std::move(trace));
    return std::optional<GenerationRequest>{request};
}

Result<void> PresentationScheduler::note_interpolation_submitted(
    std::uint64_t request_id, std::int64_t submitted_ns) {
    std::scoped_lock lock(mutex_);
    if (!active_ || active_->request.request_id != request_id ||
        submitted_ns == kUnknownTimestamp ||
        submitted_ns < active_->request.current.timestamp_ns ||
        active_->submitted_ns != kUnknownTimestamp) {
        return std::unexpected(invalid("submission timestamp does not match active generation"));
    }
    active_->submitted_ns = submitted_ns;
    last_interpolation_submission_ns_ = submitted_ns;
    if (auto* trace = find_trace_by_request_locked(request_id)) {
        trace->interpolation_submission_ns = submitted_ns;
    }
    return {};
}

Result<void> PresentationScheduler::note_gpu_started(
    std::uint64_t request_id, std::int64_t gpu_start_ns) {
    std::scoped_lock lock(mutex_);
    auto* record = active_ && active_->request.request_id == request_id
        ? &*active_ : (ready_ && ready_->request.request_id == request_id
            ? &*ready_ : nullptr);
    if (record == nullptr || gpu_start_ns == kUnknownTimestamp ||
        record->submitted_ns == kUnknownTimestamp ||
        record->ready_ns != kUnknownTimestamp ||
        record->gpu_start_ns != kUnknownTimestamp ||
        record->gpu_end_ns != kUnknownTimestamp ||
        gpu_start_ns < record->submitted_ns) {
        return std::unexpected(invalid("GPU start timestamp does not match a retained generation"));
    }
    record->gpu_start_ns = gpu_start_ns;
    last_gpu_start_ns_ = gpu_start_ns;
    if (auto* trace = find_trace_by_request_locked(request_id)) {
        trace->gpu_start_ns = gpu_start_ns;
    }
    return {};
}

Result<void> PresentationScheduler::note_gpu_ended(
    std::uint64_t request_id, std::int64_t gpu_end_ns) {
    std::scoped_lock lock(mutex_);
    auto* record = active_ && active_->request.request_id == request_id
        ? &*active_ : (ready_ && ready_->request.request_id == request_id
            ? &*ready_ : nullptr);
    if (record == nullptr || gpu_end_ns == kUnknownTimestamp ||
        record->gpu_start_ns == kUnknownTimestamp ||
        record->ready_ns != kUnknownTimestamp ||
        record->gpu_end_ns != kUnknownTimestamp ||
        gpu_end_ns < record->gpu_start_ns) {
        return std::unexpected(invalid("GPU end timestamp does not match a retained generation"));
    }
    record->gpu_end_ns = gpu_end_ns;
    last_gpu_end_ns_ = gpu_end_ns;
    if (auto* trace = find_trace_by_request_locked(request_id)) {
        trace->gpu_end_ns = gpu_end_ns;
    }
    return {};
}

Result<bool> PresentationScheduler::note_generated_ready(
    std::uint64_t request_id, std::int64_t ready_ns) {
    std::scoped_lock lock(mutex_);
    if (!active_ || active_->request.request_id != request_id ||
        ready_ns == kUnknownTimestamp ||
        active_->submitted_ns == kUnknownTimestamp ||
        ready_ns < active_->submitted_ns ||
        (active_->gpu_end_ns != kUnknownTimestamp &&
         ready_ns < active_->gpu_end_ns)) {
        return std::unexpected(invalid("ready timestamp does not match active generation"));
    }
    active_->ready_ns = ready_ns;
    last_generated_ready_ns_ = ready_ns;
    ++generated_ready_count_;
    if (auto* trace = find_trace_by_request_locked(request_id)) {
        trace->generated_ready_ns = ready_ns;
    }

    auto completed = std::move(*active_);
    active_.reset();
    if (completed.stale) {
        if (auto* trace = find_trace_by_request_locked(request_id)) {
            trace->disposition = PresentationDisposition::no_frame;
            trace->queue_depth = queue_depth_locked();
        }
        refresh_queue_peak_locked();
        return false;
    }
    if (completed.missed || ready_ns > completed.request.generation_deadline_ns || ready_) {
        mark_missed_locked(completed, true, ready_ns);
        if (auto* trace = find_trace_by_request_locked(request_id)) {
            trace->disposition = PresentationDisposition::no_frame;
            trace->queue_depth = queue_depth_locked();
        }
        refresh_queue_peak_locked();
        return false;
    }

    ready_ = std::move(completed);
    refresh_queue_peak_locked();
    return true;
}

PresentationDecision PresentationScheduler::on_display_opportunity(
    const DisplayOpportunity& opportunity) {
    std::scoped_lock lock(mutex_);
    std::vector<std::uint64_t> retired_request_ids;
    const bool timing_changed = display_timing_seen_ &&
        timing_generation_ != opportunity.timing_generation;
    if (timing_changed) {
        if (active_) {
            retired_request_ids.push_back(active_->request.request_id);
        }
        if (ready_) {
            retired_request_ids.push_back(ready_->request.request_id);
        }
    }
    update_display_generation_locked(opportunity);
    refresh_period_ns_ = std::max<std::int64_t>(1, opportunity.refresh_period_ns);
    ++target_presentations_;
    last_target_presentation_ns_ = opportunity.target_presentation_ns;

    const auto tolerance = std::max<std::int64_t>(
        1, refresh_period_ns_ / 2);
    const auto matches_target = [&](const GenerationRequest& request) {
        const auto delta = static_cast<__int128>(opportunity.target_presentation_ns) -
                           static_cast<__int128>(request.desired_presentation_ns);
        return delta >= -static_cast<__int128>(tolerance) &&
               delta <= static_cast<__int128>(tolerance);
    };

    PresentationDecision decision{
        .decision_id = next_decision_id_++,
        .retired_generation_request_ids = std::move(retired_request_ids),
        .target_presentation_ns = opportunity.target_presentation_ns,
        .render_submit_deadline_ns = opportunity.render_deadline_ns,
    };
    FrameTrace trace;
    trace.decision_id = decision.decision_id;
    trace.target_presentation_ns = opportunity.target_presentation_ns;
    trace.render_submit_deadline_ns = opportunity.render_deadline_ns;

    if (ready_ && opportunity.target_presentation_ns > saturating_add(
                      ready_->request.desired_presentation_ns, tolerance)) {
        const auto expired_id = ready_->request.request_id;
        mark_missed_locked(*ready_, true, opportunity.callback_timestamp_ns);
        ready_.reset();
        decision.retired_generation_request_ids.push_back(expired_id);
    }
    if (active_ && !active_->stale && opportunity.target_presentation_ns >
                       saturating_add(active_->request.desired_presentation_ns, tolerance)) {
        mark_missed_locked(*active_, true, opportunity.callback_timestamp_ns);
    }

    bool generated_opportunity = false;
    if (ready_ && matches_target(ready_->request) &&
        ready_->request.timing_generation == opportunity.timing_generation &&
        ready_->ready_ns <= ready_->request.generation_deadline_ns &&
        opportunity.callback_timestamp_ns <= opportunity.render_deadline_ns &&
        opportunity.target_presentation_ns < ready_->request.presentation_deadline_ns) {
        decision.disposition = PresentationDisposition::generated_frame;
        decision.source_frame_id = ready_->request.current.frame_id;
        decision.source_timestamp_ns = ready_->request.interpolation_timestamp_ns;
        decision.generation_request_id = ready_->request.request_id;
        decision.generation_deadline_ns = ready_->request.generation_deadline_ns;
        decision.presentation_deadline_ns = ready_->request.presentation_deadline_ns;
        trace = trace_for_generation_locked(*ready_, decision);
        trace.queue_depth = queue_depth_locked();
        ready_.reset();
        generated_opportunity = true;
    } else if (latest_opportunity_ && matches_target(*latest_opportunity_)) {
        generated_opportunity = true;
        bool trace_copied = false;
        if (active_ && active_->request.request_id == latest_opportunity_->request_id) {
            mark_missed_locked(*active_, true, opportunity.callback_timestamp_ns);
            trace = trace_for_generation_locked(*active_, decision);
            trace_copied = true;
        }
        if (ready_ && ready_->request.request_id == latest_opportunity_->request_id) {
            mark_missed_locked(*ready_, true, opportunity.callback_timestamp_ns);
            decision.retired_generation_request_ids.push_back(
                ready_->request.request_id);
            trace = trace_for_generation_locked(*ready_, decision);
            trace_copied = true;
            ready_.reset();
        }
        decision.disposition = PresentationDisposition::real_frame_fallback;
        decision.generation_request_id = latest_opportunity_->request_id;
        decision.generation_deadline_ns = latest_opportunity_->generation_deadline_ns;
        decision.presentation_deadline_ns = latest_opportunity_->presentation_deadline_ns;
        const auto previous = latest_opportunity_->previous;
        decision.source_frame_id = previous.frame_id;
        decision.source_timestamp_ns = previous.timestamp_ns;
        if (!trace_copied) {
            trace.previous_source_frame_id = latest_opportunity_->previous.frame_id;
            trace.current_source_frame_id = latest_opportunity_->current.frame_id;
            trace.previous_source_timestamp_ns = latest_opportunity_->previous.timestamp_ns;
            trace.current_source_timestamp_ns = latest_opportunity_->current.timestamp_ns;
            trace.interpolation_timestamp_ns = latest_opportunity_->interpolation_timestamp_ns;
            trace.generation_request_id = latest_opportunity_->request_id;
            trace.deadline_missed = false;
        }
        trace.decision_id = decision.decision_id;
        trace.disposition = decision.disposition;
        trace.source_timestamp_ns = decision.source_timestamp_ns;
        trace.target_presentation_ns = decision.target_presentation_ns;
        trace.render_submit_deadline_ns = opportunity.render_deadline_ns;
        trace.generation_deadline_ns = decision.generation_deadline_ns;
        trace.presentation_deadline_ns = decision.presentation_deadline_ns;
        trace.queue_depth = queue_depth_locked();
        ++real_frame_fallbacks_;
    } else {
        const auto latency = output_latency_locked(source_period_ns_);
        const auto source = fallback_source_locked(
            opportunity.target_presentation_ns, latency);
        if (source) {
            decision.disposition = PresentationDisposition::real_frame;
            decision.source_frame_id = source->stamp.frame_id;
            decision.source_timestamp_ns = source->stamp.timestamp_ns;
            trace.source_timestamp_ns = source->stamp.timestamp_ns;
            trace.current_source_frame_id = source->stamp.frame_id;
            trace.current_source_timestamp_ns = source->stamp.timestamp_ns;
            trace.disposition = decision.disposition;
            trace.queue_depth = queue_depth_locked();
        }
    }

    if (decision.disposition == PresentationDisposition::real_frame_fallback &&
        !generated_opportunity) {
        ++real_frame_fallbacks_;
    }
    if (decision.disposition == PresentationDisposition::no_frame) {
        trace.disposition = decision.disposition;
        trace.queue_depth = queue_depth_locked();
    }
    trace.decision_id = decision.decision_id;
    trace.target_presentation_ns = opportunity.target_presentation_ns;
    if (trace.generation_deadline_ns == kUnknownTimestamp) {
        trace.generation_deadline_ns = decision.generation_deadline_ns;
    }
    if (trace.presentation_deadline_ns == kUnknownTimestamp) {
        trace.presentation_deadline_ns = decision.presentation_deadline_ns;
    }
    bool updated_generation_trace = false;
    if (decision.generation_request_id != 0) {
        if (auto* generation_trace =
                find_trace_by_request_locked(decision.generation_request_id)) {
            generation_trace->decision_id = decision.decision_id;
            generation_trace->source_timestamp_ns = decision.source_timestamp_ns;
            generation_trace->target_presentation_ns =
                decision.target_presentation_ns;
            generation_trace->render_submit_deadline_ns =
                decision.render_submit_deadline_ns;
            generation_trace->generation_deadline_ns =
                decision.generation_deadline_ns;
            generation_trace->presentation_deadline_ns =
                decision.presentation_deadline_ns;
            generation_trace->disposition = decision.disposition;
            generation_trace->queue_depth = trace.queue_depth;
            generation_trace->deadline_missed =
                generation_trace->deadline_missed || trace.deadline_missed;
            updated_generation_trace = true;
        }
    }
    if (!updated_generation_trace) {
        append_trace_locked(std::move(trace));
    }
    return decision;
}

void PresentationScheduler::note_actual_presentation(
    const PresentationDecision& decision, std::int64_t actual_timestamp_ns) {
    if (actual_timestamp_ns == kUnknownTimestamp ||
        decision.decision_id == 0 ||
        decision.disposition == PresentationDisposition::no_frame) {
        return;
    }
    std::scoped_lock lock(mutex_);
    if (!remember_terminal_decision_locked(decision.decision_id)) {
        return;
    }
    const auto it = std::find_if(traces_.rbegin(), traces_.rend(), [&](const auto& trace) {
        return trace.decision_id == decision.decision_id;
    });
    if (decision.disposition == PresentationDisposition::generated_frame &&
        decision.presentation_deadline_ns != kUnknownTimestamp &&
        actual_timestamp_ns >= decision.presentation_deadline_ns) {
        if (it == traces_.rend() || !it->deadline_missed) {
            register_deadline_miss_locked(actual_timestamp_ns);
        }
        if (it != traces_.rend()) {
            it->deadline_missed = true;
        }
    } else if (decision.disposition == PresentationDisposition::generated_frame) {
        consecutive_misses_ = 0;
    }
    ++actual_presentations_;
    last_actual_presentation_ns_ = actual_timestamp_ns;
    if (decision.disposition == PresentationDisposition::generated_frame) {
        ++generated_frames_presented_;
    } else {
        ++real_frames_presented_;
    }
    if (actual_timestamp_ns >= decision.source_timestamp_ns) {
        const auto latency = positive_difference(
            actual_timestamp_ns, decision.source_timestamp_ns);
        if (latency) {
            added_latencies_.push_back(*latency);
        } else if (actual_timestamp_ns == decision.source_timestamp_ns) {
            added_latencies_.push_back(0);
        }
        if (added_latencies_.size() > config_.diagnostics_capacity) {
            added_latencies_.pop_front();
        }
    }
    if (it != traces_.rend()) {
        it->actual_presentation_ns = actual_timestamp_ns;
        it->disposition = decision.disposition;
    }
}

void PresentationScheduler::note_dropped_drawable(
    const PresentationDecision& decision, std::int64_t drop_timestamp_ns) {
    if (decision.decision_id == 0 || drop_timestamp_ns == kUnknownTimestamp ||
        decision.disposition == PresentationDisposition::no_frame) {
        return;
    }
    std::scoped_lock lock(mutex_);
    if (!remember_terminal_decision_locked(decision.decision_id)) {
        return;
    }
    if (decision.disposition == PresentationDisposition::generated_frame) {
        ++dropped_generated_frames_;
        register_deadline_miss_locked(drop_timestamp_ns);
    } else {
        ++dropped_real_frames_;
    }
    const auto it = std::find_if(traces_.rbegin(), traces_.rend(), [&](const auto& trace) {
        return trace.decision_id == decision.decision_id;
    });
    if (it != traces_.rend()) {
        it->drop_timestamp_ns = drop_timestamp_ns;
        it->disposition = PresentationDisposition::no_frame;
    }
}

DiagnosticsSnapshot PresentationScheduler::diagnostics() const {
    std::scoped_lock lock(mutex_);
    DiagnosticsSnapshot result{
        .source_frames = submitted_frames_,
        .interpolation_submissions = interpolation_submissions_,
        .generated_frames_ready = generated_ready_count_,
        .target_presentations = target_presentations_,
        .actual_presentations = actual_presentations_,
        .deadline_misses = deadline_misses_,
        .dropped_generated_frames = dropped_generated_frames_,
        .dropped_real_frames = dropped_real_frames_,
        .real_frame_fallbacks = real_frame_fallbacks_,
        .real_frames_presented = real_frames_presented_,
        .generated_frames_presented = generated_frames_presented_,
        .recovery_count = recovery_count_,
        .queue_depth = queue_depth_locked(),
        .peak_queue_depth = peak_queue_depth_,
        .queue_capacity = queue_capacity_locked(),
        .last_source_timestamp_ns = last_source_timestamp_ns_,
        .last_interpolation_submission_ns = last_interpolation_submission_ns_,
        .last_gpu_start_ns = last_gpu_start_ns_,
        .last_gpu_end_ns = last_gpu_end_ns_,
        .last_generated_ready_ns = last_generated_ready_ns_,
        .last_target_presentation_ns = last_target_presentation_ns_,
        .last_actual_presentation_ns = last_actual_presentation_ns_,
        .p50_added_latency_ns = percentile(added_latencies_, 0.50),
        .p95_added_latency_ns = percentile(added_latencies_, 0.95),
        .recent_frames = std::vector<FrameTrace>(traces_.begin(), traces_.end()),
    };
    return result;
}

std::uint32_t PresentationScheduler::maximum_queue_depth() const noexcept {
    return queue_capacity_locked();
}

std::int64_t PresentationScheduler::output_latency_locked(
    std::int64_t source_period_ns) const {
    const auto period = source_period_ns > 0 ? source_period_ns : source_period_ns_;
    if (period <= 0) {
        return 0;
    }
    const auto multiplier = config_.policy == LatencyPolicy::quality ? 1.5L : 1.0L;
    const auto value = static_cast<long double>(period) * multiplier;
    if (value >= static_cast<long double>(std::numeric_limits<std::int64_t>::max())) {
        return std::numeric_limits<std::int64_t>::max();
    }
    return static_cast<std::int64_t>(std::llround(value));
}

std::uint32_t PresentationScheduler::queue_depth_locked() const noexcept {
    return static_cast<std::uint32_t>(active_.has_value()) +
           static_cast<std::uint32_t>(ready_.has_value());
}

std::uint32_t PresentationScheduler::queue_capacity_locked() const noexcept {
    return 1;
}

void PresentationScheduler::append_trace_locked(FrameTrace trace) {
    if (config_.diagnostics_capacity == 0) {
        return;
    }
    traces_.push_back(std::move(trace));
    while (traces_.size() > config_.diagnostics_capacity) {
        traces_.pop_front();
    }
}

void PresentationScheduler::refresh_queue_peak_locked() {
    peak_queue_depth_ = std::max(peak_queue_depth_, queue_depth_locked());
}

void PresentationScheduler::mark_missed_locked(GenerationRecord& record,
                                               bool count_drop,
                                               std::int64_t observed_ns) {
    if (!record.missed) {
        record.missed = true;
        register_deadline_miss_locked(observed_ns);
        if (auto* trace = find_trace_by_request_locked(record.request.request_id)) {
            trace->deadline_missed = true;
        }
    }
    if (count_drop && !record.dropped) {
        record.dropped = true;
        ++dropped_generated_frames_;
    }
}

void PresentationScheduler::note_unscheduled_miss_locked(
    const GenerationRequest& request, std::int64_t now_ns) {
    if (now_ns >= request.generation_deadline_ns) {
        register_deadline_miss_locked(now_ns);
    } else if (active_ || ready_) {
        register_deadline_miss_locked(now_ns);
    }
}

bool PresentationScheduler::remember_terminal_decision_locked(
    std::uint64_t decision_id) {
    if (decision_id == 0 || std::find(terminal_decisions_.begin(),
                                     terminal_decisions_.end(), decision_id) !=
                                terminal_decisions_.end()) {
        return false;
    }
    terminal_decisions_.push_back(decision_id);
    while (terminal_decisions_.size() > config_.diagnostics_capacity) {
        terminal_decisions_.pop_front();
    }
    return true;
}

void PresentationScheduler::register_deadline_miss_locked(
    std::int64_t observed_ns) {
    ++deadline_misses_;
    if (consecutive_misses_ != std::numeric_limits<std::uint32_t>::max()) {
        ++consecutive_misses_;
    }
    if (consecutive_misses_ >= config_.consecutive_misses_to_disable) {
        const auto base = sources_.empty()
            ? observed_ns : std::max(observed_ns, last_source_timestamp_ns_);
        disabled_until_ns_ = saturating_add(base, config_.recovery_cooldown_ns);
    }
}

void PresentationScheduler::update_display_generation_locked(
    const DisplayOpportunity& opportunity) {
    if (!display_timing_seen_) {
        display_timing_seen_ = true;
        timing_generation_ = opportunity.timing_generation;
        refresh_period_ns_ = std::max<std::int64_t>(1, opportunity.refresh_period_ns);
        return;
    }
    if (timing_generation_ == opportunity.timing_generation) {
        return;
    }
    timing_generation_ = opportunity.timing_generation;
    if (active_) {
        active_->stale = true;
        if (!active_->dropped) {
            active_->dropped = true;
            ++dropped_generated_frames_;
        }
        if (auto* trace = find_trace_by_request_locked(active_->request.request_id)) {
            trace->disposition = PresentationDisposition::no_frame;
        }
    }
    if (ready_) {
        ready_->stale = true;
        if (!ready_->dropped) {
            ready_->dropped = true;
            ++dropped_generated_frames_;
        }
        if (auto* trace = find_trace_by_request_locked(ready_->request.request_id)) {
            trace->disposition = PresentationDisposition::no_frame;
        }
        ready_.reset();
    }
    latest_opportunity_.reset();
    refresh_period_ns_ = std::max<std::int64_t>(1, opportunity.refresh_period_ns);
}

FrameTrace* PresentationScheduler::find_trace_by_request_locked(
    std::uint64_t request_id) noexcept {
    if (request_id == 0) {
        return nullptr;
    }
    const auto it = std::find_if(traces_.rbegin(), traces_.rend(), [&](const auto& trace) {
        return trace.generation_request_id == request_id;
    });
    return it == traces_.rend() ? nullptr : &*it;
}

FrameTrace PresentationScheduler::trace_for_generation_locked(
    const GenerationRecord& record, const PresentationDecision& decision) const {
    FrameTrace trace;
    trace.decision_id = decision.decision_id;
    trace.generation_request_id = record.request.request_id;
    trace.previous_source_frame_id = record.request.previous.frame_id;
    trace.current_source_frame_id = record.request.current.frame_id;
    trace.previous_source_timestamp_ns = record.request.previous.timestamp_ns;
    trace.current_source_timestamp_ns = record.request.current.timestamp_ns;
    trace.source_timestamp_ns = decision.source_timestamp_ns;
    trace.interpolation_timestamp_ns = record.request.interpolation_timestamp_ns;
    trace.interpolation_submission_ns = record.submitted_ns;
    trace.gpu_start_ns = record.gpu_start_ns;
    trace.gpu_end_ns = record.gpu_end_ns;
    trace.generated_ready_ns = record.ready_ns;
    trace.target_presentation_ns = decision.target_presentation_ns;
    trace.generation_deadline_ns = record.request.generation_deadline_ns;
    trace.presentation_deadline_ns = record.request.presentation_deadline_ns;
    trace.disposition = decision.disposition;
    trace.queue_depth = queue_depth_locked();
    trace.deadline_missed = record.missed;
    return trace;
}

std::optional<PresentationScheduler::SourceRecord>
PresentationScheduler::fallback_source_locked(std::int64_t target_presentation_ns,
                                              std::int64_t latency_ns) const {
    const auto content_timestamp = saturating_add(target_presentation_ns,
                                                  -latency_ns);
    for (auto it = sources_.rbegin(); it != sources_.rend(); ++it) {
        if (it->stamp.timestamp_ns <= content_timestamp) {
            return *it;
        }
    }
    if (!sources_.empty()) {
        return sources_.front();
    }
    return std::nullopt;
}

} // namespace framegen::pacing
