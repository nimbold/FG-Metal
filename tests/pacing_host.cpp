#include "framegen/pacing.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <iostream>
#include <iterator>
#include <limits>
#include <optional>
#include <queue>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <tuple>
#include <unordered_set>
#include <vector>

namespace {

using namespace framegen::pacing;

constexpr std::int64_t kSecond = 1'000'000'000;
constexpr std::int64_t kSimulationDuration = 2 * kSecond;
constexpr std::uint64_t kClockDomain = 0x53544550;

class TestFailure final : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

void check(bool condition, const char* message) {
    if (!condition) {
        throw TestFailure(message);
    }
}

enum class Disturbance {
    stable,
    jittery,
    long_source_frame,
    slow_backend,
    display_timing_change,
    late_display_callback,
};

struct Scenario {
    std::uint32_t source_fps{};
    std::uint32_t display_fps{};
    Disturbance disturbance{};
    LatencyPolicy policy{LatencyPolicy::balanced};
};

struct Event {
    std::int64_t at_ns{};
    std::uint32_t phase{};
    std::uint64_t sequence{};
    enum class Kind { completion, source, display } kind{};
    SourceFrameStamp source{};
    std::uint64_t request_id{};
    std::int64_t gpu_start_ns{};
    std::int64_t gpu_end_ns{};
    DisplayOpportunity display{};
};

struct LaterEvent {
    bool operator()(const Event& a, const Event& b) const noexcept {
        return std::tie(a.at_ns, a.phase, a.sequence) >
               std::tie(b.at_ns, b.phase, b.sequence);
    }
};

struct SimulationResult {
    Scenario scenario;
    DiagnosticsSnapshot diagnostics;
    std::uint64_t late_ready_rejections{};
    std::uint64_t fallbacks_while_backend_active{};
    std::uint64_t real_presentations_while_backend_active{};
    std::uint64_t late_requests_with_fallback{};
};

std::string_view disturbance_name(Disturbance disturbance) {
    switch (disturbance) {
    case Disturbance::stable: return "stable";
    case Disturbance::jittery: return "jittery";
    case Disturbance::long_source_frame: return "long-source";
    case Disturbance::slow_backend: return "slow-backend";
    case Disturbance::display_timing_change: return "display-change";
    case Disturbance::late_display_callback: return "late-display-callback";
    }
    return "unknown";
}

std::string_view policy_name(LatencyPolicy policy) {
    return policy == LatencyPolicy::quality ? "QUALITY" : "BALANCED";
}

std::int64_t jittered_period(std::int64_t base, std::size_t frame_index) {
    constexpr int jitter_percent[] = {0, 4, -5, 2, -3, 5, -2, 3, -4, 1};
    const auto percent = jitter_percent[frame_index % std::size(jitter_percent)];
    return base + (base * percent) / 100;
}

std::vector<DisplayOpportunity> display_schedule(const Scenario& scenario,
                                                 std::int64_t duration_ns) {
    std::vector<DisplayOpportunity> schedule;
    auto period = kSecond / scenario.display_fps;
    std::uint64_t generation = 1;
    std::int64_t target = period;
    const auto change_at = duration_ns / 2;
    bool changed = false;
    while (target < duration_ns + 2 * kSecond / scenario.source_fps) {
        if (scenario.disturbance == Disturbance::display_timing_change &&
            !changed && target >= change_at) {
            changed = true;
            ++generation;
            period = std::max<std::int64_t>(1, period / 2);
            target = change_at + period;
        }
        const auto callback = target - std::max<std::int64_t>(1, period / 4);
        const auto render_deadline = callback + std::max<std::int64_t>(1, period / 16);
        const bool late_callback =
            scenario.disturbance == Disturbance::late_display_callback;
        schedule.push_back(DisplayOpportunity{
            .timing_generation = generation,
            .callback_timestamp_ns = late_callback
                ? target - std::max<std::int64_t>(1, period / 8) : callback,
            .render_deadline_ns = late_callback
                ? target - std::max<std::int64_t>(1, period / 8) -
                    std::max<std::int64_t>(1, period / 16)
                : render_deadline,
            .target_presentation_ns = target,
            .refresh_period_ns = period,
        });
        target += period;
    }
    return schedule;
}

SimulationResult simulate(const Scenario& scenario, std::uint64_t scenario_index) {
    SchedulerConfig config;
    config.policy = scenario.policy;
    config.interpolation_fraction = 0.5F;
    config.consecutive_misses_to_disable = 2;
    config.recovery_cooldown_ns = 250'000'000;
    config.diagnostics_capacity = 512;
    PresentationScheduler scheduler(config);

    std::priority_queue<Event, std::vector<Event>, LaterEvent> events;
    std::uint64_t event_sequence = 1;
    const auto nominal_source_period = kSecond / scenario.source_fps;
    std::int64_t source_timestamp = 0;
    for (std::uint64_t frame = 1; source_timestamp < kSimulationDuration; ++frame) {
        events.push(Event{
            .at_ns = source_timestamp,
            .phase = 1,
            .sequence = event_sequence++,
            .kind = Event::Kind::source,
            .source = SourceFrameStamp{
                .frame_id = frame,
                .clock_domain = kClockDomain + scenario_index,
                .timestamp_ns = source_timestamp,
            },
        });
        auto period = nominal_source_period;
        if (scenario.disturbance == Disturbance::jittery) {
            period = jittered_period(period, static_cast<std::size_t>(frame));
        } else if (scenario.disturbance == Disturbance::long_source_frame && frame == 20) {
            period *= 2;
        }
        source_timestamp += period;
    }

    for (const auto& display : display_schedule(scenario, kSimulationDuration)) {
        events.push(Event{
            .at_ns = display.callback_timestamp_ns,
            .phase = 2,
            .sequence = event_sequence++,
            .kind = Event::Kind::display,
            .display = display,
        });
    }

    std::optional<std::uint64_t> active_request;
    std::unordered_set<std::uint64_t> late_request_ids;
    std::unordered_set<std::uint64_t> fallback_request_ids;
    std::uint64_t late_ready_rejections = 0;
    std::uint64_t fallbacks_while_backend_active = 0;
    std::uint64_t real_presentations_while_backend_active = 0;
    const auto slow_backend_latency = std::max<std::int64_t>(
        2'000'000, (nominal_source_period * 3) / 4);
    const auto normal_backend_latency = std::max<std::int64_t>(
        500'000, nominal_source_period / 16);

    while (!events.empty()) {
        const auto event = events.top();
        events.pop();
        switch (event.kind) {
        case Event::Kind::source: {
            auto update = scheduler.submit_source_frame(event.source, event.at_ns);
            check(update.has_value(), "source frame was rejected in simulation");
            if (*update) {
                check(!active_request.has_value(), "scheduler issued overlapping backend work");
                const auto& request = **update;
                active_request = request.request_id;
                const auto submit_at = event.at_ns + 100'000;
                check(scheduler.note_interpolation_submitted(request.request_id, submit_at).has_value(),
                      "interpolation submit timestamp was rejected");
                const auto latency = scenario.disturbance == Disturbance::slow_backend
                    ? slow_backend_latency : normal_backend_latency;
                const auto gpu_start = submit_at + std::max<std::int64_t>(1, latency / 5);
                const auto gpu_end = submit_at + latency;
                events.push(Event{
                    .at_ns = gpu_end,
                    .phase = 0,
                    .sequence = event_sequence++,
                    .kind = Event::Kind::completion,
                    .request_id = request.request_id,
                    .gpu_start_ns = gpu_start,
                    .gpu_end_ns = gpu_end,
                });
            }
            break;
        }
        case Event::Kind::completion: {
            if (!active_request || *active_request != event.request_id) {
                break;
            }
            check(scheduler.note_gpu_started(event.request_id, event.gpu_start_ns).has_value(),
                  "GPU start timestamp was rejected");
            check(scheduler.note_gpu_ended(event.request_id, event.gpu_end_ns).has_value(),
                  "GPU end timestamp was rejected");
            auto ready = scheduler.note_generated_ready(event.request_id, event.gpu_end_ns);
            check(ready.has_value(), "generated-ready timestamp was rejected");
            if (!*ready) {
                ++late_ready_rejections;
                late_request_ids.insert(event.request_id);
            }
            active_request.reset();
            break;
        }
        case Event::Kind::display: {
            const auto backend_request = active_request;
            const auto decision = scheduler.on_display_opportunity(event.display);
            if (backend_request && decision.generation_request_id == *backend_request &&
                decision.disposition == PresentationDisposition::real_frame_fallback) {
                ++fallbacks_while_backend_active;
                fallback_request_ids.insert(*backend_request);
            }
            if (backend_request &&
                (decision.disposition == PresentationDisposition::real_frame ||
                 decision.disposition == PresentationDisposition::real_frame_fallback)) {
                ++real_presentations_while_backend_active;
            }
            if (decision.disposition != PresentationDisposition::no_frame) {
                const auto actual = event.display.target_presentation_ns +
                    std::max<std::int64_t>(1, event.display.refresh_period_ns / 32);
                scheduler.note_actual_presentation(decision, actual);
            }
            break;
        }
        }
    }

    std::uint64_t late_requests_with_fallback = 0;
    for (const auto request_id : late_request_ids) {
        if (!fallback_request_ids.contains(request_id)) {
            throw TestFailure("late backend output did not fall back to its real source frame");
        }
        ++late_requests_with_fallback;
    }

    return SimulationResult{
        .scenario = scenario,
        .diagnostics = scheduler.diagnostics(),
        .late_ready_rejections = late_ready_rejections,
        .fallbacks_while_backend_active = fallbacks_while_backend_active,
        .real_presentations_while_backend_active = real_presentations_while_backend_active,
        .late_requests_with_fallback = late_requests_with_fallback,
    };
}

void check_scenario(const SimulationResult& result) {
    const auto& stats = result.diagnostics;
    check(stats.source_frames > 20, "simulation did not deliver enough real source frames");
    check(stats.target_presentations > 20, "simulation did not deliver enough display targets");
    check(stats.actual_presentations > 10, "simulation did not present real or generated frames");
    check(stats.queue_depth <= stats.queue_capacity,
          "scheduler queue depth exceeded its configured bound");
    check(stats.peak_queue_depth <= stats.queue_capacity,
          "scheduler queue grew beyond its configured bound");
    check(stats.p50_added_latency_ns > 0 && stats.p95_added_latency_ns > 0,
          "added latency was not measured");

    if (result.scenario.disturbance == Disturbance::slow_backend) {
        check(stats.deadline_misses > 0, "slow backend did not register deadline misses");
        check(stats.dropped_generated_frames > 0,
              "slow backend did not drop generated output");
        check(stats.real_frames_presented > 0,
              "slow backend stopped presenting real source frames");
        check(stats.real_frame_fallbacks > 0,
              "slow backend did not use real-frame fallback");
        check(stats.recovery_count > 0,
              "frame generation did not retry after its cooldown");
        check(stats.interpolation_submissions < stats.source_frames,
              "slow backend accumulated one generation job per source frame");
        check(result.late_ready_rejections > 0,
              "slow backend did not reject an output that completed after its deadline");
        check(result.fallbacks_while_backend_active > 0 &&
                  result.real_presentations_while_backend_active > 0,
              "real frames were not presented while generation remained in flight");
        check(result.late_requests_with_fallback == result.late_ready_rejections,
              "a late output was not paired with a real-frame fallback during its stall");
        const auto source_period = kSecond / result.scenario.source_fps;
        const auto display_period = kSecond / result.scenario.display_fps;
        if (stats.p95_added_latency_ns > 2 * source_period + display_period / 2) {
            throw TestFailure("slow backend fallback latency exceeded its bounded source/display window: fps=" +
                std::to_string(result.scenario.source_fps) + " p95_ns=" +
                std::to_string(stats.p95_added_latency_ns));
        }
    }

    if (result.scenario.disturbance == Disturbance::late_display_callback) {
        check(stats.real_frame_fallbacks > 0,
              "late display callback did not fall back to a real source frame");
        check(stats.dropped_generated_frames > 0 && stats.deadline_misses > 0,
              "late display callback did not retire and record the generated output");
    }

    if (result.scenario.disturbance == Disturbance::stable) {
        check(stats.generated_frames_presented > 5,
              "stable timing did not produce a steady stream of generated frames");
    }
}

void test_arbitrary_interpolation_fraction() {
    SchedulerConfig config;
    config.interpolation_fraction = 0.25F;
    PresentationScheduler scheduler(config);
    auto first = scheduler.submit_source_frame({1, 31, 1'000}, 1'000);
    auto second = scheduler.submit_source_frame({2, 31, 5'000}, 5'000);
    check(first.has_value() && !*first, "first source unexpectedly requested interpolation");
    check(second.has_value() && *second, "second source did not request interpolation");
    check((**second).interpolation_timestamp_ns == 2'000,
          "scheduler hard-coded a midpoint interpolation sample");
}

void test_timestamp_validation_and_pipeline_order() {
    SchedulerConfig config;
    PresentationScheduler scheduler(config);
    const auto initial = scheduler.diagnostics();
    check(initial.last_source_timestamp_ns == std::numeric_limits<std::int64_t>::min() &&
              initial.last_interpolation_submission_ns ==
                  std::numeric_limits<std::int64_t>::min() &&
              initial.last_target_presentation_ns ==
                  std::numeric_limits<std::int64_t>::min(),
          "empty diagnostics reported zero timestamps as real events");
    auto first = scheduler.submit_source_frame({1, 41, 1'000}, 1'000);
    auto second = scheduler.submit_source_frame({2, 41, 5'000}, 5'000);
    check(first.has_value() && !*first && second.has_value() && *second,
          "valid source timestamps did not start a generation request");
    const auto request = **second;

    check(!scheduler.note_gpu_started(request.request_id, 5'100),
          "GPU start was accepted before interpolation submission");
    check(!scheduler.note_interpolation_submitted(request.request_id, 4'999),
          "submission before the current source timestamp was accepted");
    check(scheduler.note_interpolation_submitted(request.request_id, 5'100).has_value(),
          "valid interpolation submission was rejected");
    check(!scheduler.note_gpu_started(request.request_id, 5'099),
          "GPU start before submission was accepted");
    check(scheduler.note_gpu_started(request.request_id, 5'200).has_value(),
          "valid GPU start was rejected");
    check(!scheduler.note_gpu_started(request.request_id, 5'201),
          "duplicate GPU start was accepted");
    check(!scheduler.note_gpu_ended(request.request_id, 5'199),
          "GPU end before GPU start was accepted");
    check(scheduler.note_gpu_ended(request.request_id, 5'300).has_value(),
          "valid GPU end was rejected");
    check(!scheduler.note_generated_ready(request.request_id, 5'299),
          "generated readiness before GPU completion was accepted");
    const auto ready = scheduler.note_generated_ready(request.request_id, 5'300);
    check(ready.has_value() && *ready,
          "valid completed output did not become presentation-ready");

    PresentationScheduler range_scheduler;
    const auto near_min = std::numeric_limits<std::int64_t>::min() + 1;
    auto range_first = range_scheduler.submit_source_frame({1, 42, near_min}, near_min);
    auto range_second = range_scheduler.submit_source_frame(
        {2, 42, std::numeric_limits<std::int64_t>::max()},
        std::numeric_limits<std::int64_t>::max());
    check(range_first.has_value() && !*range_first && !range_second,
          "unrepresentable source timestamp interval was not rejected safely");
}

void test_readiness_without_optional_gpu_timing() {
    PresentationScheduler scheduler;
    auto first = scheduler.submit_source_frame({1, 44, 0}, 0);
    auto second = scheduler.submit_source_frame({2, 44, 10'000'000}, 10'000'000);
    check(first.has_value() && !*first && second.has_value() && *second,
          "backend-timing test did not start generation");
    const auto request = **second;
    check(scheduler.note_interpolation_submitted(request.request_id,
                                                 10'100'000).has_value(),
          "backend-timing test submission was rejected");
    const auto ready = scheduler.note_generated_ready(request.request_id, 10'300'000);
    check(ready.has_value() && *ready,
          "scheduler required optional GPU timestamps before accepting ready output");
    const auto stats = scheduler.diagnostics();
    check(stats.generated_frames_ready == 1 &&
              stats.last_gpu_start_ns == std::numeric_limits<std::int64_t>::min() &&
              stats.last_gpu_end_ns == std::numeric_limits<std::int64_t>::min(),
          "missing GPU telemetry was not preserved as unknown");
}

void test_dropped_drawable_feedback_and_recovery() {
    SchedulerConfig config;
    config.policy = LatencyPolicy::balanced;
    config.consecutive_misses_to_disable = 2;
    config.recovery_cooldown_ns = 100'000'000;
    PresentationScheduler scheduler(config);
    const auto display_timing = DisplayOpportunity{
        .timing_generation = 1,
        .callback_timestamp_ns = 1,
        .render_deadline_ns = 2,
        .target_presentation_ns = 3,
        .refresh_period_ns = 10'000'000,
    };
    (void)scheduler.on_display_opportunity(display_timing);

    std::uint64_t next_id = 1;
    const auto seed = scheduler.submit_source_frame({next_id++, 43, 0}, 0);
    check(seed.has_value() && !*seed,
          "recovery test seed unexpectedly requested interpolation");
    auto generate_and_drop = [&](std::int64_t source_timestamp,
                                 std::int64_t target_timestamp) {
        auto result = scheduler.submit_source_frame(
            {next_id++, 43, source_timestamp}, source_timestamp);
        check(result.has_value() && *result,
              "expected recovery test interpolation request was not admitted");
        const auto request = **result;
        check(scheduler.note_interpolation_submitted(request.request_id,
                                                     source_timestamp + 100'000).has_value() &&
                  scheduler.note_gpu_started(request.request_id,
                                             source_timestamp + 200'000).has_value() &&
                  scheduler.note_gpu_ended(request.request_id,
                                           source_timestamp + 300'000).has_value(),
              "recovery test backend timing was rejected");
        const auto ready = scheduler.note_generated_ready(
            request.request_id, source_timestamp + 300'000);
        check(ready.has_value() && *ready,
              "recovery test output did not become ready");
        auto opportunity = display_timing;
        opportunity.callback_timestamp_ns = target_timestamp - 2'000'000;
        opportunity.render_deadline_ns = target_timestamp - 1'000'000;
        opportunity.target_presentation_ns = target_timestamp;
        const auto decision = scheduler.on_display_opportunity(opportunity);
        check(decision.disposition == PresentationDisposition::generated_frame,
              "ready output missed its expected display slot");
        scheduler.note_dropped_drawable(decision, target_timestamp + 1'000'000);
        return decision;
    };

    const auto first_drop = generate_and_drop(10'000'000, 15'000'000);
    scheduler.note_dropped_drawable(first_drop, 16'000'000);
    const auto after_first_drop = scheduler.diagnostics();
    check(after_first_drop.dropped_generated_frames == 1 &&
              after_first_drop.deadline_misses == 1,
          "duplicate drawable-drop feedback was not recorded exactly once");

    (void)generate_and_drop(20'000'000, 25'000'000);
    auto during_cooldown = scheduler.submit_source_frame(
        {next_id++, 43, 30'000'000}, 30'000'000);
    check(during_cooldown.has_value() && !*during_cooldown,
          "frame generation did not pause after repeated dropped outputs");

    bool recovered = false;
    for (std::int64_t timestamp = 40'000'000; timestamp <= 140'000'000;
         timestamp += 10'000'000) {
        auto result = scheduler.submit_source_frame(
            {next_id++, 43, timestamp}, timestamp);
        check(result.has_value(), "source was rejected during cooldown recovery");
        if (*result) {
            recovered = true;
            break;
        }
    }
    const auto stats = scheduler.diagnostics();
    check(recovered && stats.recovery_count == 1,
          "generation did not recover after the dropped-output cooldown");
}

void test_cooldown_with_negative_clock_epoch() {
    SchedulerConfig config;
    config.consecutive_misses_to_disable = 1;
    config.recovery_cooldown_ns = 100'000'000;
    PresentationScheduler scheduler(config);
    (void)scheduler.on_display_opportunity(DisplayOpportunity{
        .timing_generation = 1,
        .callback_timestamp_ns = -1,
        .render_deadline_ns = 0,
        .target_presentation_ns = 1,
        .refresh_period_ns = 10'000'000,
    });
    auto first = scheduler.submit_source_frame({1, 45, -116'000'000}, -116'000'000);
    auto second = scheduler.submit_source_frame({2, 45, -106'000'000}, -106'000'000);
    check(first.has_value() && !*first && second.has_value() && *second,
          "negative-clock source pair was rejected");
    const auto request = **second;
    check(scheduler.note_interpolation_submitted(request.request_id,
                                                 -105'900'000).has_value() &&
              scheduler.note_gpu_started(request.request_id,
                                         -105'800'000).has_value() &&
              scheduler.note_gpu_ended(request.request_id,
                                       -105'700'000).has_value(),
          "negative-clock backend timing was rejected");
    const auto ready = scheduler.note_generated_ready(request.request_id, -105'700'000);
    check(ready.has_value() && *ready,
          "negative-clock ready output was rejected");
    const auto decision = scheduler.on_display_opportunity(DisplayOpportunity{
        .timing_generation = 1,
        .callback_timestamp_ns = -104'000'000,
        .render_deadline_ns = -103'500'000,
        .target_presentation_ns = -101'000'000,
        .refresh_period_ns = 10'000'000,
    });
    check(decision.disposition == PresentationDisposition::generated_frame,
          "negative-clock generated output missed its display opportunity");
    scheduler.note_dropped_drawable(decision, -100'000'000);

    const auto during_cooldown = scheduler.submit_source_frame(
        {3, 45, -96'000'000}, -96'000'000);
    check(during_cooldown.has_value() && !*during_cooldown,
          "a cooldown ending at timestamp zero was treated as disabled state");
    check(scheduler.diagnostics().recovery_count == 0,
          "negative-clock cooldown recovered before its deadline");
}

void test_ready_output_retirement_on_missed_display_deadlines() {
    auto prepare_ready_output = [](PresentationScheduler& scheduler,
                                   std::uint64_t clock_domain) {
        (void)scheduler.on_display_opportunity(DisplayOpportunity{
            .timing_generation = 1,
            .callback_timestamp_ns = 1,
            .render_deadline_ns = 2,
            .target_presentation_ns = 3,
            .refresh_period_ns = 10'000'000,
        });
        auto first = scheduler.submit_source_frame({1, clock_domain, 0}, 0);
        auto second = scheduler.submit_source_frame(
            {2, clock_domain, 10'000'000}, 10'000'000);
        check(first.has_value() && !*first && second.has_value() && *second,
              "display-deadline test did not create an interpolation request");
        const auto request = **second;
        check(scheduler.note_interpolation_submitted(request.request_id,
                                                     10'100'000).has_value() &&
                  scheduler.note_gpu_started(request.request_id,
                                             10'200'000).has_value() &&
                  scheduler.note_gpu_ended(request.request_id,
                                           10'300'000).has_value(),
              "display-deadline test backend timing was rejected");
        const auto ready = scheduler.note_generated_ready(
            request.request_id, 10'300'000);
        check(ready.has_value() && *ready,
              "display-deadline test output did not become ready");
        return request.request_id;
    };

    PresentationScheduler late_callback_scheduler;
    const auto late_callback_id = prepare_ready_output(late_callback_scheduler, 51);
    const auto late_callback = late_callback_scheduler.on_display_opportunity(
        DisplayOpportunity{
            .timing_generation = 1,
            .callback_timestamp_ns = 14'000'000,
            .render_deadline_ns = 13'500'000,
            .target_presentation_ns = 15'000'000,
            .refresh_period_ns = 10'000'000,
        });
    check(late_callback.disposition == PresentationDisposition::real_frame_fallback,
          "callback after its render-submit deadline did not choose the real frame");
    check(std::find(late_callback.retired_generation_request_ids.begin(),
                    late_callback.retired_generation_request_ids.end(),
                    late_callback_id) !=
              late_callback.retired_generation_request_ids.end(),
          "late callback did not return its ready output for retirement");

    PresentationScheduler expired_scheduler;
    const auto expired_id = prepare_ready_output(expired_scheduler, 52);
    const auto expired = expired_scheduler.on_display_opportunity(DisplayOpportunity{
        .timing_generation = 1,
        .callback_timestamp_ns = 20'000'000,
        .render_deadline_ns = 20'500'000,
        .target_presentation_ns = 21'000'000,
        .refresh_period_ns = 10'000'000,
    });
    check(expired.disposition == PresentationDisposition::real_frame,
          "expired generated output did not leave a real source frame available");
    check(std::find(expired.retired_generation_request_ids.begin(),
                    expired.retired_generation_request_ids.end(), expired_id) !=
              expired.retired_generation_request_ids.end(),
          "expired ready output was cleared without notifying its owner");
    check(expired_scheduler.diagnostics().queue_depth == 0,
          "expired ready output remained in the scheduler queue");
}

void test_latency_policy_and_display_change_retirement() {
    SchedulerConfig quality_config;
    quality_config.policy = LatencyPolicy::quality;
    SchedulerConfig balanced_config;
    balanced_config.policy = LatencyPolicy::balanced;
    PresentationScheduler quality(quality_config);
    PresentationScheduler balanced(balanced_config);

    const DisplayOpportunity initial_display{
        .timing_generation = 1,
        .callback_timestamp_ns = 1,
        .render_deadline_ns = 2,
        .target_presentation_ns = 3,
        .refresh_period_ns = 2,
    };
    (void)quality.on_display_opportunity(initial_display);
    (void)balanced.on_display_opportunity(initial_display);

    auto quality_a = quality.submit_source_frame({1, 32, 0}, 0);
    auto quality_b = quality.submit_source_frame({2, 32, 10'000'000}, 10'000'000);
    auto balanced_a = balanced.submit_source_frame({1, 33, 0}, 0);
    auto balanced_b = balanced.submit_source_frame({2, 33, 10'000'000}, 10'000'000);
    check(quality_a.has_value() && !*quality_a && quality_b.has_value() && *quality_b,
          "QUALITY did not admit its first interpolation request");
    check(balanced_a.has_value() && !*balanced_a && balanced_b.has_value() && *balanced_b,
          "BALANCED did not admit its first interpolation request");
    check((**quality_b).interpolation_timestamp_ns ==
              (**balanced_b).interpolation_timestamp_ns &&
          (**quality_b).desired_presentation_ns >
              (**balanced_b).desired_presentation_ns,
          "latency policies did not preserve the sample while exposing different delay");

    const auto request = **quality_b;
    check(quality.note_interpolation_submitted(request.request_id, 10'100'000).has_value(),
          "QUALITY submit instrumentation failed");
    check(quality.note_gpu_started(request.request_id, 10'200'000).has_value(),
          "QUALITY GPU start instrumentation failed");
    check(quality.note_gpu_ended(request.request_id, 10'300'000).has_value(),
          "QUALITY GPU end instrumentation failed");
    auto ready = quality.note_generated_ready(request.request_id, 10'300'000);
    check(ready.has_value() && *ready, "QUALITY output did not become ready");

    const auto changed_display = DisplayOpportunity{
        .timing_generation = 2,
        .callback_timestamp_ns = 20'000'000,
        .render_deadline_ns = 20'100'000,
        .target_presentation_ns = 20'200'000,
        .refresh_period_ns = 8'000'000,
    };
    const auto decision = quality.on_display_opportunity(changed_display);
    check(std::find(decision.retired_generation_request_ids.begin(),
                    decision.retired_generation_request_ids.end(), request.request_id) !=
              decision.retired_generation_request_ids.end(),
          "display timing change did not retire a stale generated output");
    const auto stats = quality.diagnostics();
    check(stats.queue_depth == 0 && stats.dropped_generated_frames > 0,
          "display timing change retained a stale generated output");
    const auto generation_trace = std::find_if(
        stats.recent_frames.begin(), stats.recent_frames.end(), [&](const auto& trace) {
            return trace.generation_request_id == request.request_id;
        });
    check(generation_trace != stats.recent_frames.end() &&
          generation_trace->interpolation_submission_ns == 10'100'000 &&
          generation_trace->gpu_start_ns == 10'200'000 &&
          generation_trace->gpu_end_ns == 10'300'000 &&
          generation_trace->generated_ready_ns == 10'300'000,
          "pacing diagnostics omitted backend timing events");
}

void test_concurrent_source_and_display_updates() {
    SchedulerConfig config;
    config.policy = LatencyPolicy::balanced;
    config.diagnostics_capacity = 32;
    PresentationScheduler scheduler(config);
    std::atomic<bool> failed{};
    std::thread producer([&] {
        for (std::uint64_t i = 1; i <= 800; ++i) {
            const auto timestamp = static_cast<std::int64_t>(i) * 2'000'000;
            auto result = scheduler.submit_source_frame(
                {i, 97, timestamp}, timestamp);
            if (!result) {
                failed.store(true, std::memory_order_relaxed);
                return;
            }
            if (*result) {
                const auto& request = **result;
                const auto submitted = timestamp + 10'000;
                if (!scheduler.note_interpolation_submitted(request.request_id, submitted) ||
                    !scheduler.note_gpu_started(request.request_id, submitted + 1'000) ||
                    !scheduler.note_gpu_ended(request.request_id, submitted + 2'000) ||
                    !scheduler.note_generated_ready(request.request_id, submitted + 2'000)) {
                    failed.store(true, std::memory_order_relaxed);
                    return;
                }
            }
        }
    });
    std::thread presenter([&] {
        constexpr std::int64_t period = 1'000'000;
        for (std::uint64_t i = 1; i <= 1700; ++i) {
            const auto target = static_cast<std::int64_t>(i) * period;
            const DisplayOpportunity opportunity{
                .timing_generation = 1,
                .callback_timestamp_ns = target - period / 4,
                .render_deadline_ns = target - period / 8,
                .target_presentation_ns = target,
                .refresh_period_ns = period,
            };
            const auto decision = scheduler.on_display_opportunity(opportunity);
            if (decision.disposition != PresentationDisposition::no_frame) {
                scheduler.note_actual_presentation(decision, target + 1'000);
            }
        }
    });
    producer.join();
    presenter.join();
    const auto stats = scheduler.diagnostics();
    check(!failed.load(std::memory_order_relaxed),
          "concurrent scheduler operation returned an inconsistent request");
    check(stats.peak_queue_depth <= 1 && stats.queue_depth <= 1,
          "concurrent source/presentation calls grew a hidden queue");
}

} // namespace

int main() {
    try {
        test_arbitrary_interpolation_fraction();
        test_timestamp_validation_and_pipeline_order();
        test_readiness_without_optional_gpu_timing();
        test_dropped_drawable_feedback_and_recovery();
        test_cooldown_with_negative_clock_epoch();
        test_ready_output_retirement_on_missed_display_deadlines();
        test_latency_policy_and_display_change_retirement();
        test_concurrent_source_and_display_updates();

        const std::uint32_t rates[][2] = {{30, 60}, {40, 80}, {60, 120}};
        constexpr Disturbance disturbances[] = {
            Disturbance::stable,
            Disturbance::jittery,
            Disturbance::long_source_frame,
            Disturbance::slow_backend,
            Disturbance::display_timing_change,
            Disturbance::late_display_callback,
        };
        std::uint64_t scenario_index = 0;
        std::vector<SimulationResult> results;
        for (const auto& rate : rates) {
            for (const auto disturbance : disturbances) {
                const auto policy = disturbance == Disturbance::stable && rate[0] == 30
                    ? LatencyPolicy::quality : LatencyPolicy::balanced;
                Scenario scenario{
                    .source_fps = rate[0],
                    .display_fps = rate[1],
                    .disturbance = disturbance,
                    .policy = policy,
                };
                auto result = simulate(scenario, ++scenario_index);
                check_scenario(result);
                results.push_back(std::move(result));
            }
        }

        std::cout << "source_fps,display_fps,disturbance,policy,generated,real,fallback,misses,dropped,peak_queue,p50_added_latency_ns,p95_added_latency_ns,recovery_probes\n";
        for (const auto& result : results) {
            const auto& stats = result.diagnostics;
            std::cout << result.scenario.source_fps << ','
                      << result.scenario.display_fps << ','
                      << disturbance_name(result.scenario.disturbance) << ','
                      << policy_name(result.scenario.policy) << ','
                      << stats.generated_frames_presented << ','
                      << stats.real_frames_presented << ','
                      << stats.real_frame_fallbacks << ','
                      << stats.deadline_misses << ','
                      << stats.dropped_generated_frames << ','
                      << stats.peak_queue_depth << ','
                      << stats.p50_added_latency_ns << ','
                      << stats.p95_added_latency_ns << ','
                      << stats.recovery_count << '\n';
        }
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "pacing host failed: " << error.what() << '\n';
        return 1;
    }
}
