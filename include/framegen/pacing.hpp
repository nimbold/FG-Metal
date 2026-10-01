#pragma once

#include "framegen/result.hpp"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <limits>
#include <mutex>
#include <optional>
#include <vector>

namespace framegen::pacing {

enum class LatencyPolicy : std::uint8_t {
    quality,
    balanced,
};

struct SourceFrameStamp {
    std::uint64_t frame_id{};
    std::uint64_t clock_domain{};
    std::int64_t timestamp_ns{};
};

// All values use the same monotonic clock and epoch as SourceFrameStamp.
// render_deadline_ns is the host deadline for submitting presentation work;
// target_presentation_ns is the estimated time the frame becomes visible.
struct DisplayOpportunity {
    std::uint64_t timing_generation{};
    std::int64_t callback_timestamp_ns{};
    std::int64_t render_deadline_ns{};
    std::int64_t target_presentation_ns{};
    std::int64_t refresh_period_ns{};
};

struct GenerationRequest {
    std::uint64_t request_id{};
    SourceFrameStamp previous{};
    SourceFrameStamp current{};
    float interpolation_fraction{};
    std::int64_t interpolation_timestamp_ns{};
    std::int64_t desired_presentation_ns{};
    // Deadline to have generated output ready for a nonblocking presentation.
    std::int64_t generation_deadline_ns{};
    // Latest timestamp at which the host may report this output as presented.
    std::int64_t presentation_deadline_ns{};
    std::uint64_t timing_generation{};
};

enum class PresentationDisposition : std::uint8_t {
    real_frame,
    generated_frame,
    real_frame_fallback,
    no_frame,
};

struct PresentationDecision {
    std::uint64_t decision_id{};
    PresentationDisposition disposition{PresentationDisposition::no_frame};
    std::uint64_t source_frame_id{};
    std::int64_t source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::uint64_t generation_request_id{};
    // Outputs invalidated by a display timing change or a missed target.
    std::vector<std::uint64_t> retired_generation_request_ids;
    std::int64_t target_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t render_submit_deadline_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t generation_deadline_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t presentation_deadline_ns{std::numeric_limits<std::int64_t>::min()};
};

struct FrameTrace {
    std::uint64_t decision_id{};
    std::uint64_t generation_request_id{};
    std::uint64_t previous_source_frame_id{};
    std::uint64_t current_source_frame_id{};
    std::int64_t previous_source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t current_source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t source_timestamp_ns{};
    std::int64_t interpolation_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t interpolation_submission_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t gpu_start_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t gpu_end_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t generated_ready_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t target_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t actual_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t drop_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t render_submit_deadline_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t generation_deadline_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t presentation_deadline_ns{std::numeric_limits<std::int64_t>::min()};
    PresentationDisposition disposition{PresentationDisposition::no_frame};
    std::uint32_t queue_depth{};
    bool deadline_missed{};
};

struct DiagnosticsSnapshot {
    std::uint64_t source_frames{};
    std::uint64_t interpolation_submissions{};
    std::uint64_t generated_frames_ready{};
    std::uint64_t target_presentations{};
    std::uint64_t actual_presentations{};
    std::uint64_t deadline_misses{};
    std::uint64_t dropped_generated_frames{};
    std::uint64_t dropped_real_frames{};
    std::uint64_t real_frame_fallbacks{};
    std::uint64_t real_frames_presented{};
    std::uint64_t generated_frames_presented{};
    std::uint64_t recovery_count{};
    std::uint32_t queue_depth{};
    std::uint32_t peak_queue_depth{};
    std::uint32_t queue_capacity{};
    std::int64_t last_source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_interpolation_submission_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_gpu_start_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_gpu_end_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_generated_ready_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_target_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_actual_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t p50_added_latency_ns{};
    std::int64_t p95_added_latency_ns{};
    std::vector<FrameTrace> recent_frames;
};

struct SchedulerConfig {
    LatencyPolicy policy{LatencyPolicy::balanced};
    // The generic policy accepts any interior sample; 0.5 is the initial 2x mode.
    float interpolation_fraction{0.5F};
    std::uint32_t consecutive_misses_to_disable{2};
    std::int64_t recovery_cooldown_ns{250'000'000};
    std::size_t diagnostics_capacity{128};
};

// Thread-safe, renderer/model-independent pacing state. The caller submits the
// returned GenerationRequest to a dedicated backend worker; no backend or GPU
// operation runs while the scheduler lock is held. There is never a FIFO of
// generation jobs: at most one backend job and one ready output are retained.
class PresentationScheduler final {
public:
    explicit PresentationScheduler(SchedulerConfig config = {});

    [[nodiscard]] Result<std::optional<GenerationRequest>> submit_source_frame(
        SourceFrameStamp frame, std::int64_t now_ns);

    [[nodiscard]] Result<void> note_interpolation_submitted(
        std::uint64_t request_id, std::int64_t submitted_ns);
    [[nodiscard]] Result<void> note_gpu_started(
        std::uint64_t request_id, std::int64_t gpu_start_ns);
    [[nodiscard]] Result<void> note_gpu_ended(
        std::uint64_t request_id, std::int64_t gpu_end_ns);
    // Returns true only when the result is still eligible to be presented.
    // A false result tells the worker to release the generated output.
    [[nodiscard]] Result<bool> note_generated_ready(
        std::uint64_t request_id, std::int64_t ready_ns);

    [[nodiscard]] PresentationDecision on_display_opportunity(
        const DisplayOpportunity& opportunity);
    void note_actual_presentation(const PresentationDecision& decision,
                                  std::int64_t actual_timestamp_ns);
    void note_dropped_drawable(const PresentationDecision& decision,
                               std::int64_t drop_timestamp_ns);

    [[nodiscard]] DiagnosticsSnapshot diagnostics() const;
    [[nodiscard]] std::uint32_t maximum_queue_depth() const noexcept;

private:
    struct SourceRecord {
        SourceFrameStamp stamp;
    };

    struct GenerationRecord {
        GenerationRequest request;
        std::int64_t submitted_ns{std::numeric_limits<std::int64_t>::min()};
        std::int64_t gpu_start_ns{std::numeric_limits<std::int64_t>::min()};
        std::int64_t gpu_end_ns{std::numeric_limits<std::int64_t>::min()};
        std::int64_t ready_ns{std::numeric_limits<std::int64_t>::min()};
        bool missed{};
        bool dropped{};
        bool stale{};
    };

    [[nodiscard]] std::int64_t output_latency_locked(std::int64_t source_period_ns) const;
    [[nodiscard]] std::uint32_t queue_depth_locked() const noexcept;
    [[nodiscard]] std::uint32_t queue_capacity_locked() const noexcept;
    void append_trace_locked(FrameTrace trace);
    void refresh_queue_peak_locked();
    void mark_missed_locked(GenerationRecord& record, bool count_drop,
                            std::int64_t observed_ns);
    void note_unscheduled_miss_locked(const GenerationRequest& request,
                                      std::int64_t now_ns);
    void update_display_generation_locked(const DisplayOpportunity& opportunity);
    [[nodiscard]] FrameTrace* find_trace_by_request_locked(
        std::uint64_t request_id) noexcept;
    [[nodiscard]] FrameTrace trace_for_generation_locked(
        const GenerationRecord& record, const PresentationDecision& decision) const;
    [[nodiscard]] bool remember_terminal_decision_locked(
        std::uint64_t decision_id);
    void register_deadline_miss_locked(std::int64_t observed_ns);
    [[nodiscard]] std::optional<SourceRecord> fallback_source_locked(
        std::int64_t target_presentation_ns,
        std::int64_t latency_ns) const;

    SchedulerConfig config_;
    mutable std::mutex mutex_;
    std::deque<SourceRecord> sources_;
    std::optional<GenerationRecord> active_;
    std::optional<GenerationRecord> ready_;
    std::optional<GenerationRequest> latest_opportunity_;
    std::uint64_t clock_domain_{};
    std::uint64_t timing_generation_{};
    bool display_timing_seen_{};
    std::int64_t refresh_period_ns_{};
    std::int64_t source_period_ns_{};
    std::optional<std::int64_t> disabled_until_ns_;
    std::uint32_t consecutive_misses_{};
    std::uint64_t next_request_id_{1};
    std::uint64_t next_decision_id_{1};
    std::uint64_t submitted_frames_{};
    std::uint64_t interpolation_submissions_{};
    std::uint64_t generated_ready_count_{};
    std::uint64_t target_presentations_{};
    std::uint64_t actual_presentations_{};
    std::uint64_t deadline_misses_{};
    std::uint64_t dropped_generated_frames_{};
    std::uint64_t dropped_real_frames_{};
    std::uint64_t real_frame_fallbacks_{};
    std::uint64_t real_frames_presented_{};
    std::uint64_t generated_frames_presented_{};
    std::uint64_t recovery_count_{};
    std::uint32_t peak_queue_depth_{};
    std::int64_t last_source_timestamp_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_interpolation_submission_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_gpu_start_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_gpu_end_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_generated_ready_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_target_presentation_ns_{std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_actual_presentation_ns_{std::numeric_limits<std::int64_t>::min()};
    std::deque<FrameTrace> traces_;
    std::deque<std::int64_t> added_latencies_;
    std::deque<std::uint64_t> terminal_decisions_;
};

} // namespace framegen::pacing
