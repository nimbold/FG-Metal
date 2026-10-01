#pragma once

#include "framegen/backend.hpp"

namespace framegen::quality {

struct TemporalQualityPlan {
    bool enabled{};
    bool force_reset{};
    bool use_history{};
    bool reuse_cached_output{};
    bool stale_submission{};
    bool force_real_fallback{};
    bool advance_history{};
    float history_time_scale{};
    std::int64_t target_timestamp_ns{unknown_timestamp_ns};
};

// Model-independent host-side temporal state. The backend uses this plan to
// decide whether GPU history is valid; image confidence and policy blending
// are evaluated by the backend's GPU implementation.
class TemporalQualityController final {
public:
    [[nodiscard]] TemporalQualityPlan begin(
        const FrameSubmission& submission,
        const TextureDescriptor& descriptor,
        bool asynchronous_reset_requested) const noexcept;

    void commit(const FrameSubmission& submission,
                const TextureDescriptor& descriptor,
                const TemporalQualityPlan& plan) noexcept;
    void reset() noexcept;

private:
    bool has_state_{};
    bool active_{};
    TemporalQualityPolicy policy_{TemporalQualityPolicy::disabled};
    HudMode hud_mode_{HudMode::no_hud_knowledge};
    TextureDescriptor descriptor_{};
    FrameColorMetadata color_metadata_{};
    std::uint64_t clock_domain_{};
    std::uint64_t previous_sequence_{};
    std::uint64_t current_sequence_{};
    std::int64_t previous_timestamp_ns_{unknown_timestamp_ns};
    std::int64_t current_timestamp_ns_{unknown_timestamp_ns};
    std::int64_t last_target_timestamp_ns_{unknown_timestamp_ns};
    long double source_cadence_ns_{};
};

} // namespace framegen::quality
