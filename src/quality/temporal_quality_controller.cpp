#include "temporal_quality_controller.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>
#include <limits>

namespace framegen::quality {
namespace {

std::uint64_t distance(std::int64_t lower, std::int64_t upper) noexcept {
    return static_cast<std::uint64_t>(upper) - static_cast<std::uint64_t>(lower);
}

std::uint64_t fractional_offset(std::uint64_t span, float fraction) noexcept {
    if (!(fraction > 0.0F)) return 0;
    if (fraction >= 1.0F) return span;

    const std::uint32_t bits = std::bit_cast<std::uint32_t>(fraction);
    const std::uint32_t exponent = (bits >> 23) & 0xffU;
    const std::uint32_t mantissa = bits & 0x7fffffU;
    const std::uint32_t significand = exponent == 0
        ? mantissa : (0x800000U | mantissa);
    const std::uint32_t shift = exponent == 0 ? 149U : 150U - exponent;
    // span * significand is below 2^88, so a fraction below 2^-88 cannot
    // round to one nanosecond. This also avoids oversized integer shifts for
    // float subnormals.
    if (significand == 0 || shift >= 89U) return 0;

    using WideUnsigned = __uint128_t;
    const WideUnsigned product = static_cast<WideUnsigned>(span) * significand;
    const WideUnsigned rounded = (product + (WideUnsigned{1} << (shift - 1U))) >> shift;
    return static_cast<std::uint64_t>(std::min<WideUnsigned>(rounded, span));
}

std::int64_t interpolate_timestamp(const FrameSubmission& submission) noexcept {
    if (submission.interpolation_timestamp_ns != unknown_timestamp_ns) {
        return submission.interpolation_timestamp_ns;
    }
    const auto lower = submission.previous.timing.timestamp_ns;
    const auto upper = submission.current.timing.timestamp_ns;
    const auto span = distance(lower, upper);
    const auto offset = fractional_offset(span, submission.interpolation);
    if (lower >= 0) {
        return lower + static_cast<std::int64_t>(std::min(offset, span));
    }
    const auto magnitude = std::uint64_t{0} - static_cast<std::uint64_t>(lower);
    const auto bounded = std::min(offset, span);
    if (bounded < magnitude) {
        const auto remaining = magnitude - bounded;
        if (remaining == (std::uint64_t{1} << 63)) {
            return std::numeric_limits<std::int64_t>::min();
        }
        return -static_cast<std::int64_t>(remaining);
    }
    return static_cast<std::int64_t>(bounded - magnitude);
}

bool same_descriptor(const TextureDescriptor& left,
                     const TextureDescriptor& right) noexcept {
    return left.width == right.width && left.height == right.height &&
        left.format == right.format && left.color_space == right.color_space &&
        left.alpha_mode == right.alpha_mode;
}

bool same_color_metadata(const FrameColorMetadata& left,
                         const FrameColorMetadata& right) noexcept {
    return left.transfer_function == right.transfer_function &&
        left.dynamic_range == right.dynamic_range;
}

long double source_cadence(const FrameSubmission& submission) noexcept {
    const auto sequence_delta = submission.current.timing.sequence -
        submission.previous.timing.sequence;
    if (sequence_delta == 0) return 0.0L;
    return static_cast<long double>(distance(
        submission.previous.timing.timestamp_ns,
        submission.current.timing.timestamp_ns)) /
        static_cast<long double>(sequence_delta);
}

bool enabled(const TemporalQualityOptions& options) noexcept {
    return options.policy != TemporalQualityPolicy::disabled ||
        options.debug_visualization != TemporalQualityDebugVisualization::disabled;
}

} // namespace

TemporalQualityPlan TemporalQualityController::begin(
    const FrameSubmission& submission, const TextureDescriptor& descriptor,
    bool asynchronous_reset_requested) const noexcept {
    TemporalQualityPlan plan;
    plan.enabled = enabled(submission.temporal_quality);
    plan.target_timestamp_ns = interpolate_timestamp(submission);
    if (!plan.enabled) return plan;

    const auto& previous = submission.previous.timing;
    const auto& current = submission.current.timing;
    const auto metadata = submission.current.color_metadata;
    const auto new_cadence = source_cadence(submission);

    const bool same_pair = has_state_ &&
        previous.clock_domain == clock_domain_ &&
        previous.sequence == previous_sequence_ &&
        current.sequence == current_sequence_ &&
        previous.timestamp_ns == previous_timestamp_ns_ &&
        current.timestamp_ns == current_timestamp_ns_;
    const bool follows_pair = has_state_ &&
        previous.clock_domain == clock_domain_ &&
        previous.sequence == current_sequence_ &&
        previous.timestamp_ns == current_timestamp_ns_ &&
        current.sequence > current_sequence_ &&
        current.timestamp_ns > current_timestamp_ns_;
    const bool target_is_new = !has_state_ ||
        plan.target_timestamp_ns > last_target_timestamp_ns_;
    const bool duplicate_target = has_state_ && same_pair &&
        plan.target_timestamp_ns == last_target_timestamp_ns_;
    const bool stale_submission = has_state_ && !submission.reset_history &&
        (plan.target_timestamp_ns < last_target_timestamp_ns_ ||
         current.sequence < current_sequence_ ||
         current.timestamp_ns < current_timestamp_ns_);

    const bool descriptor_changed = has_state_ && !same_descriptor(descriptor, descriptor_);
    const bool color_changed = has_state_ && !same_color_metadata(metadata, color_metadata_);
    const bool clock_changed = has_state_ && previous.clock_domain != clock_domain_;
    const bool policy_changed = has_state_ &&
        (policy_ != submission.temporal_quality.policy ||
         active_ != plan.enabled || hud_mode_ != submission.hud_options.mode);
    const bool timeline_discontinuity = has_state_ && !same_pair && !follows_pair;

    bool severe_timestamp_discontinuity = false;
    if (has_state_ && source_cadence_ns_ > 0.0L && new_cadence > 0.0L) {
        severe_timestamp_discontinuity = new_cadence > source_cadence_ns_ * 4.0L ||
            new_cadence * 4.0L < source_cadence_ns_;
    }

    plan.stale_submission = stale_submission;
    plan.force_real_fallback = stale_submission || submission.reset_history ||
        asynchronous_reset_requested || descriptor_changed || color_changed || clock_changed ||
        (!submission.reset_history && (policy_changed || timeline_discontinuity ||
                                       severe_timestamp_discontinuity));
    plan.force_reset = stale_submission || submission.reset_history || asynchronous_reset_requested ||
        !has_state_ || descriptor_changed || color_changed || clock_changed ||
        policy_changed || timeline_discontinuity || severe_timestamp_discontinuity;
    plan.use_history = has_state_ && active_ && !plan.force_reset &&
        plan.target_timestamp_ns > last_target_timestamp_ns_ &&
        (same_pair || follows_pair);
    plan.reuse_cached_output = has_state_ && active_ && duplicate_target &&
        !stale_submission && !submission.reset_history && !asynchronous_reset_requested &&
        !descriptor_changed && !color_changed && !clock_changed && !policy_changed;
    // Explicit/automatic reset establishes a new timeline even if the host is
    // replaying timestamps from an earlier run or seeking backwards. Without
    // this, the controller would keep comparing every replayed target to the
    // stale final timestamp and force a reset forever.
    plan.advance_history = !stale_submission && (target_is_new || plan.force_reset);
    if (has_state_ && target_is_new) {
        const auto source_span = distance(previous.timestamp_ns, current.timestamp_ns);
        const auto target_delta = static_cast<long double>(
            distance(last_target_timestamp_ns_, plan.target_timestamp_ns));
        if (source_span != 0) {
            plan.history_time_scale = static_cast<float>(std::clamp(
                target_delta / static_cast<long double>(source_span), 0.0L, 2.0L));
        }
    }
    return plan;
}

void TemporalQualityController::commit(
    const FrameSubmission& submission, const TextureDescriptor& descriptor,
    const TemporalQualityPlan& plan) noexcept {
    if (!plan.enabled) {
        reset();
        return;
    }
    if (!plan.advance_history) return;

    const auto& previous = submission.previous.timing;
    const auto& current = submission.current.timing;
    has_state_ = true;
    active_ = true;
    policy_ = submission.temporal_quality.policy;
    hud_mode_ = submission.hud_options.mode;
    descriptor_ = descriptor;
    color_metadata_ = submission.current.color_metadata;
    clock_domain_ = current.clock_domain;
    previous_sequence_ = previous.sequence;
    current_sequence_ = current.sequence;
    previous_timestamp_ns_ = previous.timestamp_ns;
    current_timestamp_ns_ = current.timestamp_ns;
    last_target_timestamp_ns_ = plan.target_timestamp_ns;
    source_cadence_ns_ = source_cadence(submission);
}

void TemporalQualityController::reset() noexcept {
    has_state_ = false;
    active_ = false;
    policy_ = TemporalQualityPolicy::disabled;
    hud_mode_ = HudMode::no_hud_knowledge;
    descriptor_ = {};
    color_metadata_ = {};
    clock_domain_ = 0;
    previous_sequence_ = 0;
    current_sequence_ = 0;
    previous_timestamp_ns_ = unknown_timestamp_ns;
    current_timestamp_ns_ = unknown_timestamp_ns;
    last_target_timestamp_ns_ = unknown_timestamp_ns;
    source_cadence_ns_ = 0.0L;
}

} // namespace framegen::quality
