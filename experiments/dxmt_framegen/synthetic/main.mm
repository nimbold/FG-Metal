#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <MetalKit/MetalKit.h>
#import <QuartzCore/QuartzCore.h>

#include "framegen/frame_generator.hpp"
#include "framegen/pacing.hpp"
#include "synthetic_metal_backend.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace {

constexpr std::uint32_t kImageWidth = 640;
constexpr std::uint32_t kImageHeight = 360;
constexpr std::int64_t kSourcePeriodNs = 33'333'333;
constexpr std::int64_t kDefaultRefreshPeriodNs = 16'666'667;
constexpr std::uint64_t kClockDomain = 0x534E594D4554414CULL;
constexpr const char* kPatternShader = R"metal(
#include <metal_stdlib>
using namespace metal;

kernel void make_test_pattern(
    texture2d<float, access::write> output [[texture(0)]],
    constant uint& frame_index [[buffer(0)]],
    uint2 position [[thread_position_in_grid]]) {
    const float2 uv = (float2(position) + 0.5) /
                      float2(output.get_width(), output.get_height());
    const float center = frame_index == 0 ? 0.30 : 0.70;
    const float bar = 1.0 - smoothstep(0.025, 0.045, abs(uv.x - center));
    const float3 background = float3(0.025 + uv.y * 0.10,
                                     0.035 + uv.x * 0.08,
                                     0.09 + uv.y * 0.11);
    const float3 accent = frame_index == 0
        ? float3(0.10, 0.82, 0.98)
        : float3(1.0, 0.30, 0.08);
    output.write(float4(mix(background, accent, bar), 1.0), position);
}
)metal";

constexpr const char* kDisplayShader = R"metal(
#include <metal_stdlib>
using namespace metal;

struct DisplayVertex {
    float4 position [[position]];
    float2 uv;
};

vertex DisplayVertex display_vertex(uint vertex_id [[vertex_id]]) {
    const float2 positions[4] = {
        float2(-1.0,  1.0), float2( 1.0,  1.0),
        float2(-1.0, -1.0), float2( 1.0, -1.0)
    };
    const float2 coordinates[4] = {
        float2(0.0, 0.0), float2(1.0, 0.0),
        float2(0.0, 1.0), float2(1.0, 1.0)
    };
    DisplayVertex result;
    result.position = float4(positions[vertex_id], 0.0, 1.0);
    result.uv = coordinates[vertex_id];
    return result;
}

fragment float4 display_fragment(
    DisplayVertex in [[stage_in]],
    texture2d<float> image [[texture(0)]],
    sampler image_sampler [[sampler(0)]]) {
    return image.sample(image_sampler, in.uv);
}
)metal";

std::int64_t steady_now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::atomic<int> g_run_exit_code{0};

bool reserve_output_directory(const std::filesystem::path& output_directory,
                              std::string& error_text) {
    std::error_code error;
    auto parent = output_directory.parent_path();
    if (parent.empty()) {
        parent = ".";
    }
    std::filesystem::create_directories(parent, error);
    if (error) {
        error_text = "could not create evidence parent directory: " + error.message();
        return false;
    }
    if (std::filesystem::create_directory(output_directory, error)) {
        return true;
    }
    error_text = error
        ? "could not reserve evidence directory: " + error.message()
        : "evidence output directory already exists: " + output_directory.string();
    return false;
}

void write_csv_field(std::ostream& stream, const std::string& value) {
    stream.put('"');
    for (const char character : value) {
        if (character == '"') {
            stream.put('"');
        }
        stream.put(character);
    }
    stream.put('"');
}

std::optional<std::int64_t> checked_subtract_ns(std::int64_t left,
                                                std::int64_t right) {
    if ((right > 0 && left < std::numeric_limits<std::int64_t>::min() + right) ||
        (right < 0 && left > std::numeric_limits<std::int64_t>::max() + right)) {
        return std::nullopt;
    }
    return left - right;
}

std::string ns_error_text(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) {
        return fallback;
    }
    const char* text = error.localizedDescription.UTF8String;
    return text == nullptr ? fallback : text;
}

bool encode_test_patterns(id<MTLDevice> device,
                          id<MTLCommandQueue> queue,
                          id<MTLSharedEvent> ready_event,
                          id<MTLTexture> texture_a,
                          id<MTLTexture> texture_b,
                          std::atomic<std::int64_t>* ready_timestamp,
                          std::string& error_text) {
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:
        [NSString stringWithUTF8String:kPatternShader] options:nil error:&error];
    id<MTLFunction> function = [library newFunctionWithName:@"make_test_pattern"];
    id<MTLComputePipelineState> pipeline = function == nil
        ? nil : [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
        error_text = ns_error_text(error, "test pattern pipeline creation failed");
        return false;
    }

    id<MTLCommandBuffer> command_buffer = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
    if (command_buffer == nil || encoder == nil) {
        error_text = "could not create test pattern command resources";
        return false;
    }
    [encoder setComputePipelineState:pipeline];
    const NSUInteger threads_x = pipeline.threadExecutionWidth;
    const NSUInteger threads_y = std::max<NSUInteger>(
        1, std::min<NSUInteger>(8, pipeline.maxTotalThreadsPerThreadgroup / threads_x));
    const MTLSize threads_per_group = MTLSizeMake(threads_x, threads_y, 1);
    const MTLSize grid = MTLSizeMake(texture_a.width, texture_a.height, 1);
    std::uint32_t frame_index = 0;
    [encoder setTexture:texture_a atIndex:0];
    [encoder setBytes:&frame_index length:sizeof(frame_index) atIndex:0];
    [encoder dispatchThreads:grid threadsPerThreadgroup:threads_per_group];
    frame_index = 1;
    [encoder setTexture:texture_b atIndex:0];
    [encoder setBytes:&frame_index length:sizeof(frame_index) atIndex:0];
    [encoder dispatchThreads:grid threadsPerThreadgroup:threads_per_group];
    [encoder endEncoding];
    [command_buffer encodeSignalEvent:ready_event value:1];
    [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        if (completed.status == MTLCommandBufferStatusCompleted && ready_timestamp != nullptr) {
            ready_timestamp->store(steady_now_ns(), std::memory_order_release);
        }
    }];
    [command_buffer commit];
    return true;
}

id<MTLTexture> make_private_rgba_texture(id<MTLDevice> device) {
    MTLTextureDescriptor* descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                     width:kImageWidth
                                    height:kImageHeight
                                 mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    return [device newTextureWithDescriptor:descriptor];
}

const char* disposition_name(framegen::pacing::PresentationDisposition disposition) {
    using framegen::pacing::PresentationDisposition;
    switch (disposition) {
        case PresentationDisposition::real_frame: return "real_frame";
        case PresentationDisposition::generated_frame: return "generated_frame";
        case PresentationDisposition::real_frame_fallback: return "real_frame_fallback";
        case PresentationDisposition::no_frame: return "no_frame";
    }
    return "unknown";
}

struct ClockBridge {
    CFTimeInterval ca_origin{};
    std::int64_t steady_origin_ns{};

    [[nodiscard]] std::optional<std::int64_t> to_steady_ns(CFTimeInterval ca_time) const {
        if (!std::isfinite(ca_time) || ca_time <= 0.0) {
            return std::nullopt;
        }
        const long double delta_ns =
            (static_cast<long double>(ca_time) - static_cast<long double>(ca_origin)) * 1.0e9L;
        const long double mapped = static_cast<long double>(steady_origin_ns) + delta_ns;
        if (mapped > static_cast<long double>(std::numeric_limits<std::int64_t>::max()) ||
            mapped < static_cast<long double>(std::numeric_limits<std::int64_t>::min())) {
            return std::nullopt;
        }
        return static_cast<std::int64_t>(std::llround(mapped));
    }
};

struct SourceFrame {
    framegen::pacing::SourceFrameStamp stamp;
    framegen::Texture texture;
    char pattern{'A'};
};

struct GeneratedOutput {
    std::uint64_t request_id{};
    framegen::GeneratedFrame frame;
    framegen::dxmt_synthetic::EventPoint ready_event;
    std::int64_t ready_ns{std::numeric_limits<std::int64_t>::min()};
};

struct FrameRow {
    std::mutex mutex;
    std::uint64_t callback_index{};
    std::int64_t callback_ns{};
    std::int64_t scheduler_target_ns{};
    std::int64_t render_deadline_ns{};
    std::int64_t metal_target_presentation_ns{};
    bool render_deadline_valid{};
    bool metal_target_valid{};
    std::int64_t actual_presentation_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t scheduler_feedback_actual_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t actual_minus_metal_target_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t generated_ready_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t previous_source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t current_source_timestamp_ns{std::numeric_limits<std::int64_t>::min()};
    std::int64_t source_ready_ns{std::numeric_limits<std::int64_t>::min()};
    std::uint64_t source_count{};
    std::uint64_t source_frame_id{};
    std::uint64_t generation_request_id{};
    framegen::pacing::PresentationDecision decision;
    framegen::pacing::PresentationDisposition disposition{
        framegen::pacing::PresentationDisposition::no_frame};
    char selection{'-'};
    bool generated_ready{};
    bool actual_drop{};
    bool terminal{};
    std::atomic_bool pending_counted{false};
    std::string drop_reason;
};

struct RunOptions {
    double duration_seconds{60.0};
    std::filesystem::path output_directory{
        "build/step10b-synthetic-x86_64/evidence"};
};

struct HostState : std::enable_shared_from_this<HostState> {
    __strong id<MTLDevice> device;
    __strong id<MTLCommandQueue> pattern_queue;
    __strong id<MTLCommandQueue> display_queue;
    __strong id<MTLSharedEvent> input_ready_event;
    __strong id<MTLRenderPipelineState> display_pipeline;
    __strong id<MTLSamplerState> sampler;
    __strong id<MTLTexture> texture_a_native;
    __strong id<MTLTexture> texture_b_native;
    std::shared_ptr<framegen::dxmt_synthetic::SyntheticMetalBackend> backend;
    std::optional<framegen::Texture> texture_a;
    std::optional<framegen::Texture> texture_b;
    std::optional<framegen::FrameGenerator> generator;
    std::shared_ptr<framegen::pacing::PresentationScheduler> scheduler;
    ClockBridge clock;
    std::filesystem::path output_directory;
    double requested_duration_seconds{};
    std::int64_t start_ns{};
    std::int64_t stop_ns{};
    std::int64_t next_source_deadline_ns{};
    std::uint64_t source_count{};
    std::uint64_t callback_count{};
    std::uint64_t next_source_id{1};
    std::uint64_t interpolation_submissions{};
    std::atomic<std::uint64_t> generated_ready_count{0};
    std::atomic<std::uint64_t> generation_failures{0};
    std::atomic<std::uint64_t> generation_drops{0};
    std::uint64_t missing_drawables{};
    std::uint64_t unavailable_selected_outputs{};
    std::uint64_t encode_failures{};
    std::uint64_t expired_render_deadlines{};
    std::uint64_t invalid_timing_updates{};
    std::atomic<std::uint64_t> invalid_feedback_phases{0};
    std::uint64_t skipped_source_ticks{};
    std::uint64_t selected_a{};
    std::uint64_t selected_g{};
    std::uint64_t selected_b{};
    std::atomic<std::uint64_t> dropped_presentations{0};
    std::atomic<std::uint64_t> pending_presentations{0};
    std::atomic<std::uint64_t> pending_display_completions{0};
    std::atomic_bool stopping{false};
    std::mutex rows_mutex;
    std::mutex generation_mutex;
    std::vector<std::shared_ptr<FrameRow>> rows;
    std::map<std::uint64_t, SourceFrame> sources;
    std::map<std::uint64_t, GeneratedOutput> pending_outputs;
    std::map<std::uint64_t, GeneratedOutput> ready_outputs;
    std::map<std::uint64_t, std::pair<std::int64_t, std::int64_t>> request_source_timestamps;
    std::atomic<std::int64_t> input_ready_ns{
        std::numeric_limits<std::int64_t>::min()};
    std::int64_t last_callback_ns{};

    [[nodiscard]] static std::shared_ptr<HostState> create(
        id<MTLDevice> native_device, const RunOptions& options, std::string& error_text) {
        auto state = std::make_shared<HostState>();
        state->device = native_device;
        state->requested_duration_seconds = options.duration_seconds;
        state->output_directory = options.output_directory;
        state->clock.ca_origin = CACurrentMediaTime();
        state->clock.steady_origin_ns = steady_now_ns();
        state->start_ns = state->clock.steady_origin_ns;
        state->next_source_deadline_ns = state->start_ns;
        state->scheduler = std::make_shared<framegen::pacing::PresentationScheduler>(
            framegen::pacing::SchedulerConfig{
                .policy = framegen::pacing::LatencyPolicy::balanced,
                .interpolation_fraction = 0.5F,
                .consecutive_misses_to_disable = 3,
                .recovery_cooldown_ns = 250'000'000,
                .diagnostics_capacity = 4096,
            });

        auto backend_result = framegen::dxmt_synthetic::SyntheticMetalBackend::create(
            native_device);
        if (!backend_result) {
            error_text = backend_result.error().message;
            return {};
        }
        state->backend = std::move(*backend_result);
        state->texture_a_native = make_private_rgba_texture(native_device);
        state->texture_b_native = make_private_rgba_texture(native_device);
        state->pattern_queue = [native_device newCommandQueue];
        state->input_ready_event = [native_device newSharedEvent];
        if (state->texture_a_native == nil || state->texture_b_native == nil ||
            state->pattern_queue == nil || state->input_ready_event == nil) {
            error_text = "native source textures, pattern queue, or shared event creation failed";
            return {};
        }
        auto a_result = state->backend->wrap_texture(state->texture_a_native);
        auto b_result = state->backend->wrap_texture(state->texture_b_native);
        if (!a_result || !b_result) {
            error_text = !a_result ? a_result.error().message : b_result.error().message;
            return {};
        }
        state->texture_a = std::move(*a_result);
        state->texture_b = std::move(*b_result);
        if (!encode_test_patterns(native_device, state->pattern_queue,
                                  state->input_ready_event,
                                  state->texture_a_native, state->texture_b_native,
                                  &state->input_ready_ns,
                                  error_text)) {
            return {};
        }
        auto generator_result = framegen::FrameGenerator::create(state->backend);
        if (!generator_result) {
            error_text = generator_result.error().message;
            return {};
        }
        state->generator.emplace(std::move(*generator_result));
        return state;
    }

    void begin_capture() {
        start_ns = steady_now_ns();
        next_source_deadline_ns = start_ns;
    }

    [[nodiscard]] bool initialize_display(MTKView* view, std::string& error_text) {
        NSError* error = nil;
        display_queue = [device newCommandQueue];
        id<MTLLibrary> library = [device newLibraryWithSource:
            [NSString stringWithUTF8String:kDisplayShader] options:nil error:&error];
        id<MTLFunction> vertex = [library newFunctionWithName:@"display_vertex"];
        id<MTLFunction> fragment = [library newFunctionWithName:@"display_fragment"];
        MTLRenderPipelineDescriptor* descriptor = [[MTLRenderPipelineDescriptor alloc] init];
        descriptor.vertexFunction = vertex;
        descriptor.fragmentFunction = fragment;
        descriptor.colorAttachments[0].pixelFormat = view.colorPixelFormat;
        display_pipeline = display_queue == nil || library == nil || vertex == nil ||
            fragment == nil ? nil : [device newRenderPipelineStateWithDescriptor:descriptor
                                                                            error:&error];
        MTLSamplerDescriptor* sampler_descriptor = [[MTLSamplerDescriptor alloc] init];
        sampler_descriptor.minFilter = MTLSamplerMinMagFilterLinear;
        sampler_descriptor.magFilter = MTLSamplerMinMagFilterLinear;
        sampler_descriptor.sAddressMode = MTLSamplerAddressModeClampToEdge;
        sampler_descriptor.tAddressMode = MTLSamplerAddressModeClampToEdge;
        sampler = [device newSamplerStateWithDescriptor:sampler_descriptor];
        if (display_queue == nil || display_pipeline == nil || sampler == nil) {
            error_text = ns_error_text(error, "display queue, pipeline, or sampler creation failed");
            return false;
        }
        view.paused = YES;
        view.enableSetNeedsDisplay = NO;
        view.framebufferOnly = YES;
        CAMetalLayer* layer = (CAMetalLayer*)view.layer;
        layer.device = device;
        layer.pixelFormat = view.colorPixelFormat;
        layer.framebufferOnly = YES;
        layer.displaySyncEnabled = YES;
        return true;
    }

    void add_row(const std::shared_ptr<FrameRow>& row) {
        std::scoped_lock lock(rows_mutex);
        rows.push_back(row);
    }

    void on_generation_complete(
        std::uint64_t request_id,
        framegen::GpuCompletionStatus status,
        std::int64_t ready_ns) {
        std::scoped_lock lock(generation_mutex);
        auto it = pending_outputs.find(request_id);
        if (it == pending_outputs.end()) {
            return;
        }
        if (status != framegen::GpuCompletionStatus::complete) {
            ++generation_failures;
            ++generation_drops;
            pending_outputs.erase(it);
            return;
        }
        const auto ready_result = scheduler->note_generated_ready(request_id, ready_ns);
        if (!ready_result) {
            ++generation_failures;
            pending_outputs.erase(it);
            return;
        }
        ++generated_ready_count;
        if (*ready_result) {
            it->second.ready_ns = ready_ns;
            ready_outputs.emplace(request_id, std::move(it->second));
        } else {
            ++generation_drops;
        }
        pending_outputs.erase(it);
    }

    [[nodiscard]] std::uint64_t pending_generation_completions() {
        std::scoped_lock lock(generation_mutex);
        return pending_outputs.size();
    }

    [[nodiscard]] bool has_pending_async_work() {
        if (pending_presentations.load(std::memory_order_acquire) != 0 ||
            pending_display_completions.load(std::memory_order_acquire) != 0) {
            return true;
        }
        return pending_generation_completions() != 0;
    }

    void submit_source_update(std::int64_t timestamp_ns) {
        ++source_count;
        const std::uint64_t frame_id = next_source_id++;
        const bool use_a = (frame_id & 1U) != 0;
        const framegen::Texture source_texture = use_a ? *texture_a : *texture_b;
        const auto pattern = use_a ? 'A' : 'B';
        const framegen::pacing::SourceFrameStamp stamp{
            .frame_id = frame_id,
            .clock_domain = kClockDomain,
            .timestamp_ns = timestamp_ns,
        };
        sources.emplace(frame_id, SourceFrame{stamp, source_texture, pattern});
        while (sources.size() > 8) {
            sources.erase(sources.begin());
        }

        auto request_result = scheduler->submit_source_frame(stamp, timestamp_ns);
        if (!request_result) {
            ++generation_failures;
            return;
        }
        if (!*request_result) {
            return;
        }
        const auto request = **request_result;
        {
            std::scoped_lock lock(generation_mutex);
            request_source_timestamps[request.request_id] = {
                request.previous.timestamp_ns, request.current.timestamp_ns};
        }
        const auto previous = sources.find(request.previous.frame_id);
        const auto current = sources.find(request.current.frame_id);
        if (previous == sources.end() || current == sources.end()) {
            ++generation_failures;
            return;
        }
        const std::int64_t submit_ns = steady_now_ns();
        auto noted = scheduler->note_interpolation_submitted(request.request_id, submit_ns);
        if (!noted) {
            ++generation_failures;
            return;
        }
        auto dependency_result = backend->make_event_dependency(
            framegen::dxmt_synthetic::EventPoint{input_ready_event, 1});
        if (!dependency_result) {
            ++generation_failures;
            return;
        }
        const auto duration_ns = request.current.timestamp_ns - request.previous.timestamp_ns;
        framegen::FrameSubmission submission{
            .previous = {
                .texture = previous->second.texture,
                .timing = {.sequence = request.previous.frame_id,
                           .timestamp_ns = request.previous.timestamp_ns,
                           .clock_domain = request.previous.clock_domain,
                           .duration_ns = duration_ns},
            },
            .current = {
                .texture = current->second.texture,
                .timing = {.sequence = request.current.frame_id,
                           .timestamp_ns = request.current.timestamp_ns,
                           .clock_domain = request.current.clock_domain,
                           .duration_ns = duration_ns},
            },
            .interpolation = request.interpolation_fraction,
            .reset_history = request.request_id == 1,
            .gpu_dependencies = {*dependency_result},
        };
        auto generated_result = generator->submit(submission);
        if (!generated_result) {
            ++generation_failures;
            return;
        }
        auto event_result = backend->event_point(*generated_result->completion);
        if (!event_result) {
            ++generation_failures;
            return;
        }
        auto completion = generated_result->completion;
        {
            std::scoped_lock lock(generation_mutex);
            pending_outputs.emplace(request.request_id, GeneratedOutput{
                request.request_id, std::move(*generated_result), std::move(*event_result),
                std::numeric_limits<std::int64_t>::min()});
        }
        std::weak_ptr<HostState> weak_self = shared_from_this();
        auto callback_result = backend->add_completion_handler(
            completion,
            [weak_self, request_id = request.request_id](
                framegen::GpuCompletionStatus status, std::int64_t completed_ns) {
                if (auto self = weak_self.lock()) {
                    self->on_generation_complete(request_id, status, completed_ns);
                }
            });
        if (!callback_result) {
            std::scoped_lock lock(generation_mutex);
            pending_outputs.erase(request.request_id);
            ++generation_failures;
            return;
        }
        ++interpolation_submissions;
    }

    void finish_presentation(const std::shared_ptr<FrameRow>& row,
                             std::optional<std::int64_t> actual_ns,
                             std::string reason) {
        bool scheduler_feedback_valid = false;
        {
            std::scoped_lock lock(row->mutex);
            if (row->terminal) {
                return;
            }
            row->terminal = true;
            if (actual_ns) {
                row->actual_presentation_ns = *actual_ns;
                if (row->metal_target_valid) {
                    const auto target_offset = checked_subtract_ns(
                        *actual_ns, row->metal_target_presentation_ns);
                    const auto callback_to_target = checked_subtract_ns(
                        row->metal_target_presentation_ns, row->callback_ns);
                    std::optional<std::int64_t> scheduler_actual;
                    if (callback_to_target) {
                        scheduler_actual = checked_subtract_ns(
                            *actual_ns, *callback_to_target);
                    }
                    if (target_offset && scheduler_actual) {
                        row->actual_minus_metal_target_ns = *target_offset;
                        row->scheduler_feedback_actual_ns = *scheduler_actual;
                        scheduler_feedback_valid = true;
                    } else {
                        ++invalid_feedback_phases;
                    }
                } else {
                    ++invalid_feedback_phases;
                }
            } else {
                row->actual_drop = true;
                row->drop_reason = std::move(reason);
            }
        }
        if (actual_ns && scheduler_feedback_valid) {
            // The scheduler decision target is the callback-time content
            // opportunity. Translate the native drawable timestamp back by
            // the measured CAMetalDisplayLink presentation phase for scheduler
            // feedback; raw native target and actual remain in the CSV.
            scheduler->note_actual_presentation(
                row->decision, row->scheduler_feedback_actual_ns);
        } else if (!actual_ns) {
            scheduler->note_dropped_drawable(row->decision, steady_now_ns());
            ++dropped_presentations;
        }
        if (row->pending_counted.exchange(false, std::memory_order_relaxed)) {
            pending_presentations.fetch_sub(1, std::memory_order_relaxed);
        }
    }

    void on_display_update(CAMetalDisplayLinkUpdate* update) {
        if (stopping.load(std::memory_order_relaxed)) {
            return;
        }
        const std::int64_t now_ns = steady_now_ns();
        ++callback_count;
        if (last_callback_ns > 0) {
            const auto callback_delta = now_ns - last_callback_ns;
            if (callback_delta > 0 && callback_delta < 100'000'000) {
                // Keep the default stable at 60 Hz; callback jitter is retained in rows.
            }
        }
        last_callback_ns = now_ns;
        const auto render_deadline = clock.to_steady_ns(update.targetTimestamp);
        const auto metal_target =
            clock.to_steady_ns(update.targetPresentationTimestamp);
        const std::int64_t render_deadline_ns = render_deadline.value_or(now_ns - 1);
        const std::int64_t metal_target_ns = metal_target.value_or(0);
        const std::int64_t refresh_ns = kDefaultRefreshPeriodNs;
        // The scheduler makes its content choice on this callback. Preserve
        // CAMetalDisplayLink's later targetPresentationTimestamp separately in
        // the CSV and compare actual drawable time against that native target.
        // targetTimestamp is the raw render-submit deadline: leave it expired
        // when callback delivery is late so the scheduler can reject G.
        if (!render_deadline || !metal_target) {
            ++invalid_timing_updates;
        }
        if (render_deadline && now_ns > render_deadline_ns) {
            ++expired_render_deadlines;
        }
        const framegen::pacing::DisplayOpportunity opportunity{
            .timing_generation = 1,
            .callback_timestamp_ns = now_ns,
            .render_deadline_ns = render_deadline_ns,
            .target_presentation_ns = now_ns,
            .refresh_period_ns = refresh_ns,
        };
        auto decision = scheduler->on_display_opportunity(opportunity);
        if (!decision.retired_generation_request_ids.empty()) {
            std::scoped_lock lock(generation_mutex);
            for (const auto request_id : decision.retired_generation_request_ids) {
                ready_outputs.erase(request_id);
            }
        }
        auto row = std::make_shared<FrameRow>();
        row->callback_index = callback_count;
        row->callback_ns = now_ns;
        row->scheduler_target_ns = decision.target_presentation_ns;
        row->render_deadline_ns = render_deadline_ns;
        row->metal_target_presentation_ns = metal_target_ns;
        row->render_deadline_valid = render_deadline.has_value();
        row->metal_target_valid = metal_target.has_value();
        row->source_count = source_count;
        row->disposition = decision.disposition;
        row->decision = decision;
        row->source_frame_id = decision.source_frame_id;
        row->generation_request_id = decision.generation_request_id;
        row->source_ready_ns = input_ready_ns.load(std::memory_order_acquire);
        if (decision.generation_request_id != 0) {
            std::scoped_lock lock(generation_mutex);
            const auto times = request_source_timestamps.find(decision.generation_request_id);
            if (times != request_source_timestamps.end()) {
                row->previous_source_timestamp_ns = times->second.first;
                row->current_source_timestamp_ns = times->second.second;
            }
        } else if (decision.source_frame_id != 0) {
            const auto source = sources.find(decision.source_frame_id);
            if (source != sources.end()) {
                row->previous_source_timestamp_ns = source->second.stamp.timestamp_ns;
                row->current_source_timestamp_ns = source->second.stamp.timestamp_ns;
            }
        }

        id<MTLTexture> selected_texture = nil;
        id<MTLSharedEvent> selected_event = nil;
        std::uint64_t selected_event_value = 0;
        std::uint64_t selected_source_id = decision.source_frame_id;
        std::string selection_error;
        bool generated_output_found = false;
        if (decision.disposition == framegen::pacing::PresentationDisposition::generated_frame) {
            std::scoped_lock lock(generation_mutex);
            auto generated_it = ready_outputs.find(decision.generation_request_id);
            if (generated_it != ready_outputs.end()) {
                generated_output_found = true;
                selected_texture = backend->native_texture(generated_it->second.frame.texture);
                selected_event = generated_it->second.ready_event.event;
                selected_event_value = generated_it->second.ready_event.value;
                row->selection = 'G';
                row->generated_ready = true;
                row->generated_ready_ns = generated_it->second.ready_ns;
                ready_outputs.erase(generated_it);
                ++selected_g;
            } else {
                row->selection = '-';
                ++generation_drops;
                selection_error = "selected_generated_output_unavailable";
            }
            if (generated_output_found &&
                (selected_texture == nil || selected_event == nil)) {
                selection_error = "selected_generated_texture_or_event_unavailable";
            }
        } else if (decision.disposition == framegen::pacing::PresentationDisposition::real_frame ||
                   decision.disposition == framegen::pacing::PresentationDisposition::real_frame_fallback) {
            auto source_it = sources.find(selected_source_id);
            if (source_it != sources.end()) {
                selected_texture = backend->native_texture(source_it->second.texture);
                selected_event = input_ready_event;
                selected_event_value = 1;
                row->selection = source_it->second.pattern;
                if (row->selection == 'A') {
                    ++selected_a;
                } else if (row->selection == 'B') {
                    ++selected_b;
                }
            } else {
                selection_error = "selected_source_unavailable";
            }
        }
        add_row(row);

        if (decision.disposition != framegen::pacing::PresentationDisposition::no_frame &&
            (selected_texture == nil || selected_event == nil)) {
            ++unavailable_selected_outputs;
            finish_presentation(row, std::nullopt,
                selection_error.empty() ? "selected_texture_or_event_unavailable"
                                        : std::move(selection_error));
        } else if (selected_texture != nil && selected_event != nil) {
            id<CAMetalDrawable> drawable = update.drawable;
            if (drawable == nil) {
                ++missing_drawables;
                finish_presentation(row, std::nullopt, "nil_drawable");
            } else {
                MTLRenderPassDescriptor* pass = [MTLRenderPassDescriptor renderPassDescriptor];
                pass.colorAttachments[0].texture = drawable.texture;
                pass.colorAttachments[0].loadAction = MTLLoadActionClear;
                pass.colorAttachments[0].storeAction = MTLStoreActionStore;
                pass.colorAttachments[0].clearColor =
                    MTLClearColorMake(0.015, 0.02, 0.03, 1.0);
                id<MTLCommandBuffer> command_buffer = [display_queue commandBuffer];
                if (command_buffer == nil) {
                    ++encode_failures;
                    finish_presentation(row, std::nullopt, "command_buffer_unavailable");
                } else {
                    // Encode the GPU wait before creating an encoder. The CPU
                    // polls completion but never waits for pixels or GPU work.
                    [command_buffer encodeWaitForEvent:selected_event value:selected_event_value];
                    id<MTLRenderCommandEncoder> encoder =
                        [command_buffer renderCommandEncoderWithDescriptor:pass];
                    if (encoder == nil) {
                        ++encode_failures;
                        finish_presentation(row, std::nullopt, "render_encoder_unavailable");
                    } else {
                        [encoder setRenderPipelineState:display_pipeline];
                        [encoder setFragmentTexture:selected_texture atIndex:0];
                        [encoder setFragmentSamplerState:sampler atIndex:0];
                        [encoder drawPrimitives:MTLPrimitiveTypeTriangleStrip
                                    vertexStart:0 vertexCount:4];
                        [encoder endEncoding];
                        auto self = shared_from_this();
                        [drawable addPresentedHandler:^(id<MTLDrawable> presented_drawable) {
                            const auto presented_time = presented_drawable.presentedTime;
                            const auto actual_time = self->clock.to_steady_ns(presented_time);
                            if (actual_time) {
                                self->finish_presentation(row,
                                    *actual_time, {});
                            } else if (presented_time > 0.0) {
                                self->finish_presentation(row, std::nullopt,
                                                          "drawable_presented_time_invalid");
                            } else {
                                self->finish_presentation(row, std::nullopt,
                                                          "drawable_presented_time_unavailable");
                            }
                        }];
                        pending_display_completions.fetch_add(1, std::memory_order_relaxed);
                        [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
                            self->pending_display_completions.fetch_sub(
                                1, std::memory_order_relaxed);
                            if (completed.status == MTLCommandBufferStatusError) {
                                self->finish_presentation(row, std::nullopt,
                                    completed.error.localizedDescription.UTF8String == nullptr
                                        ? "display_command_failed"
                                        : completed.error.localizedDescription.UTF8String);
                            }
                        }];
                        // CAMetalDisplayLink supplied this frame's drawable.
                        // No atTime call or CPU sleep is used.
                        [command_buffer presentDrawable:drawable];
                        row->pending_counted.store(true, std::memory_order_relaxed);
                        pending_presentations.fetch_add(1, std::memory_order_relaxed);
                        [command_buffer commit];
                    }
                }
            }
        }

        if (now_ns >= next_source_deadline_ns &&
            now_ns - start_ns < static_cast<std::int64_t>(requested_duration_seconds * 1.0e9)) {
            submit_source_update(now_ns);
            next_source_deadline_ns += kSourcePeriodNs;
            while (next_source_deadline_ns <= now_ns) {
                next_source_deadline_ns += kSourcePeriodNs;
                ++skipped_source_ticks;
            }
        }
        if (callback_count == 1 || callback_count % 300 == 0) {
            std::fprintf(stdout,
                "progress callbacks=%llu sources=%llu submits=%llu ready=%llu "
                "presented_pending=%llu drops=%llu\n",
                static_cast<unsigned long long>(callback_count),
                static_cast<unsigned long long>(source_count),
                static_cast<unsigned long long>(interpolation_submissions),
                static_cast<unsigned long long>(generated_ready_count.load()),
                static_cast<unsigned long long>(pending_presentations.load()),
                static_cast<unsigned long long>(dropped_presentations.load()));
            std::fflush(stdout);
        }
    }

    bool write_evidence() {
        const auto pending_presentation_count =
            pending_presentations.load(std::memory_order_acquire);
        const auto pending_display_completion_count =
            pending_display_completions.load(std::memory_order_acquire);
        const auto pending_generator_count = pending_generation_completions();
        std::vector<std::shared_ptr<FrameRow>> rows_snapshot;
        {
            std::scoped_lock lock(rows_mutex);
            rows_snapshot = rows;
        }
        const auto csv_path = output_directory / "synthetic-display.csv";
        std::ofstream csv(csv_path);
        if (!csv) {
            std::fprintf(stderr, "evidence CSV could not be opened: %s\n",
                         csv_path.c_str());
            return false;
        }
        csv << "callback_index,callback_ns,scheduler_target_ns,render_deadline_ns,"
               "metal_target_presentation_ns,actual_presentation_ns,"
               "scheduler_feedback_actual_ns,"
               "actual_minus_metal_target_ns,source_count,source_frame_id,"
               "previous_source_timestamp_ns,current_source_timestamp_ns,source_ready_ns,"
               "generation_request_id,disposition,selection,generated_ready,"
               "generated_ready_ns,actual_drop,drop_reason\n";
        std::uint64_t actual_count = 0;
        std::uint64_t actual_drops = 0;
        std::uint64_t rows_without_terminal_status = 0;
        std::uint64_t no_frame_rows = 0;
        std::uint64_t unresolved_selected_rows = 0;
        std::vector<std::int64_t> target_deltas;
        for (const auto& row : rows_snapshot) {
            std::scoped_lock lock(row->mutex);
            csv << row->callback_index << ',' << row->callback_ns << ','
                << row->scheduler_target_ns << ',';
            if (row->render_deadline_valid) {
                csv << row->render_deadline_ns;
            }
            csv << ',';
            if (row->metal_target_valid) {
                csv << row->metal_target_presentation_ns;
            }
            csv << ',';
            if (row->actual_presentation_ns == std::numeric_limits<std::int64_t>::min()) {
                csv << "";
            } else {
                csv << row->actual_presentation_ns;
                ++actual_count;
                if (row->actual_minus_metal_target_ns !=
                    std::numeric_limits<std::int64_t>::min()) {
                    target_deltas.push_back(row->actual_minus_metal_target_ns);
                }
            }
            csv << ',';
            if (row->scheduler_feedback_actual_ns ==
                std::numeric_limits<std::int64_t>::min()) {
                csv << "";
            } else {
                csv << row->scheduler_feedback_actual_ns;
            }
            if (!row->terminal) {
                ++rows_without_terminal_status;
                if (row->disposition == framegen::pacing::PresentationDisposition::no_frame) {
                    ++no_frame_rows;
                } else {
                    ++unresolved_selected_rows;
                }
            }
            csv << ',';
            if (row->actual_minus_metal_target_ns == std::numeric_limits<std::int64_t>::min()) {
                csv << "";
            } else {
                csv << row->actual_minus_metal_target_ns;
            }
            const auto write_timestamp = [&csv](std::int64_t value) {
                if (value == std::numeric_limits<std::int64_t>::min()) {
                    csv << "";
                } else {
                    csv << value;
                }
            };
            csv << ',' << row->source_count << ',' << row->source_frame_id << ',';
            write_timestamp(row->previous_source_timestamp_ns);
            csv << ',';
            write_timestamp(row->current_source_timestamp_ns);
            csv << ',';
            write_timestamp(row->source_ready_ns);
            csv << ',' << row->generation_request_id << ',' << disposition_name(row->disposition)
                << ',' << row->selection << ',' << (row->generated_ready ? 1 : 0) << ',';
            if (row->generated_ready_ns == std::numeric_limits<std::int64_t>::min()) {
                csv << "";
            } else {
                csv << row->generated_ready_ns;
            }
            csv << ',' << (row->actual_drop ? 1 : 0) << ',';
            write_csv_field(csv, row->drop_reason);
            csv << '\n';
            actual_drops += row->actual_drop ? 1 : 0;
        }
        csv.flush();
        if (!csv) {
            std::fprintf(stderr, "evidence CSV write failed: %s\n", csv_path.c_str());
            return false;
        }
        csv.close();
        if (!csv) {
            std::fprintf(stderr, "evidence CSV close failed: %s\n", csv_path.c_str());
            return false;
        }

        std::sort(target_deltas.begin(), target_deltas.end());
        const auto percentile = [&target_deltas](double p) -> std::int64_t {
            if (target_deltas.empty()) return 0;
            const auto index = static_cast<std::size_t>(std::llround(
                p * static_cast<double>(target_deltas.size() - 1)));
            return target_deltas[std::min(index, target_deltas.size() - 1)];
        };
        const auto diagnostics = scheduler->diagnostics();
        const auto elapsed_ns = std::max<std::int64_t>(1, stop_ns - start_ns);
        const auto summary_path = output_directory / "synthetic-summary.txt";
        std::ofstream summary(summary_path);
        if (!summary) {
            std::fprintf(stderr, "evidence summary could not be opened: %s\n",
                         summary_path.c_str());
            return false;
        }
        summary << "harness=isolated synthetic Framegen Metal backend/display harness\n"
                << "dxmt_presenter_integration=false\n"
                << "framegen_public_core_api_changed=false\n"
#if defined(__x86_64__)
                << "process_arch=x86_64 (required Wine/Metal target)\n"
#elif defined(__arm64__)
                << "process_arch=arm64 (targeted native diagnostic)\n"
#else
                << "process_arch=other\n"
#endif
                << "metal_device=" << (device.name.UTF8String == nullptr
                                         ? "unknown" : device.name.UTF8String) << '\n'
                << "duration_requested_seconds=" << std::fixed << std::setprecision(3)
                << requested_duration_seconds << '\n'
                << "duration_observed_seconds="
                << static_cast<double>(elapsed_ns) / 1.0e9 << '\n'
                << "source_update_target_hz=30\n"
                << "source_update_count=" << source_count << '\n'
                << "source_input_timestamps_in_csv=true\n"
                << "source_ready_timestamp=GPU pattern command-buffer completion callback\n"
                << "source_update_observed_hz="
                << static_cast<double>(source_count) / (static_cast<double>(elapsed_ns) / 1.0e9)
                << '\n'
                << "display_link_target_hz=60\n"
                << "display_callback_count=" << callback_count << '\n'
                << "display_callback_observed_hz="
                << static_cast<double>(callback_count) / (static_cast<double>(elapsed_ns) / 1.0e9)
                << '\n'
                << "source_ticks_skipped=" << skipped_source_ticks << '\n'
                << "interpolation_submissions=" << interpolation_submissions << '\n'
                << "generated_ready_count=" << generated_ready_count.load() << '\n'
                << "generation_failures=" << generation_failures.load() << '\n'
                << "generation_drops=" << generation_drops.load() << '\n'
                << "selected_A=" << selected_a << '\n'
                << "selected_G=" << selected_g << '\n'
                << "selected_B=" << selected_b << '\n'
                << "actual_presentations=" << actual_count << '\n'
                << "actual_presentation_drops=" << actual_drops << '\n'
                << "scheduler_deadline_misses=" << diagnostics.deadline_misses << '\n'
                << "scheduler_dropped_generated=" << diagnostics.dropped_generated_frames << '\n'
                << "scheduler_dropped_real=" << diagnostics.dropped_real_frames << '\n'
                << "missing_drawables=" << missing_drawables << '\n'
                << "display_encode_failures=" << encode_failures << '\n'
                << "expired_render_deadlines=" << expired_render_deadlines << '\n'
                << "invalid_timing_updates=" << invalid_timing_updates << '\n'
                << "invalid_feedback_phases=" << invalid_feedback_phases.load() << '\n'
                << "unavailable_selected_outputs=" << unavailable_selected_outputs << '\n'
                << "pending_callbacks_at_finalize=" << pending_presentation_count << '\n'
                << "pending_display_completions_at_finalize="
                << pending_display_completion_count << '\n'
                << "pending_generator_completions_at_finalize="
                << pending_generator_count << '\n'
                << "rows_without_terminal_status=" << rows_without_terminal_status << '\n'
                << "no_frame_rows_without_terminal_status=" << no_frame_rows << '\n'
                << "unresolved_selected_rows=" << unresolved_selected_rows << '\n'
                << "rows_without_terminal_presentation=" << rows_without_terminal_status << '\n'
                << "scheduler_target_is_callback_choice_time=true\n"
                << "render_submit_deadline=raw CAMetalDisplayLink targetTimestamp; no clamp\n"
                << "scheduler_actual_feedback=raw native actual minus measured callback-to-target phase\n"
                << "raw_native_actual_retained_in_csv=true\n"
                << "metal_target_presentation_timestamp_recorded_separately=true\n"
                << "actual_minus_metal_target_p50_ns=" << percentile(0.50) << '\n'
                << "actual_minus_metal_target_p95_ns=" << percentile(0.95) << '\n'
                << "present_method=commandBuffer presentDrawable:update.drawable\n"
                << "gpu_sync=shared-event waits in command buffers; completion callback is nonblocking\n"
                << "cpu_pixel_readback=none\n"
                << "evidence_csv=" << csv_path.string() << '\n';
        summary.flush();
        if (!summary) {
            std::fprintf(stderr, "evidence summary write failed: %s\n",
                         summary_path.c_str());
            return false;
        }
        summary.close();
        if (!summary) {
            std::fprintf(stderr, "evidence summary close failed: %s\n",
                         summary_path.c_str());
            return false;
        }
        std::fprintf(stdout, "evidence_csv=%s\nevidence_summary=%s\n",
            csv_path.c_str(), summary_path.c_str());
        std::fprintf(stdout,
            "final callbacks=%llu sources=%llu submissions=%llu ready=%llu "
            "A=%llu G=%llu B=%llu actual=%llu drops=%llu pending=%llu\n",
            static_cast<unsigned long long>(callback_count),
            static_cast<unsigned long long>(source_count),
            static_cast<unsigned long long>(interpolation_submissions),
            static_cast<unsigned long long>(generated_ready_count.load()),
            static_cast<unsigned long long>(selected_a),
            static_cast<unsigned long long>(selected_g),
            static_cast<unsigned long long>(selected_b),
            static_cast<unsigned long long>(actual_count),
            static_cast<unsigned long long>(actual_drops),
            static_cast<unsigned long long>(pending_presentation_count));
        std::fflush(stdout);
        return pending_presentation_count == 0 &&
               pending_display_completion_count == 0 &&
               pending_generator_count == 0 && unresolved_selected_rows == 0;
    }
};

} // namespace

@interface SyntheticOutputDelegate : NSObject <CAMetalDisplayLinkDelegate> {
@private
    std::shared_ptr<HostState> _state;
    __strong CAMetalDisplayLink* _display_link;
    __strong MTKView* _view;
    std::int64_t _finalize_deadline_ns;
    BOOL _evidence_finalized;
}
@property(nonatomic, strong) NSTimer* finalizeTimer;
- (instancetype)initWithState:(std::shared_ptr<HostState>)state
                          view:(MTKView*)view
                         error:(NSError* __autoreleasing*)error;
- (void)stopCapture:(NSTimer*)timer;
- (void)finalizeCapture:(NSTimer*)timer;
- (BOOL)isEvidenceFinalized;
@end

@implementation SyntheticOutputDelegate

- (instancetype)initWithState:(std::shared_ptr<HostState>)state
                          view:(MTKView*)view
                         error:(NSError* __autoreleasing*)out_error {
    self = [super init];
    if (self == nil) return nil;
    _state = std::move(state);
    _view = view;
    std::string error_text;
    if (!_state || ![_view isKindOfClass:[MTKView class]] ||
        !_state->initialize_display(_view, error_text)) {
        if (out_error != nullptr) {
            *out_error = [NSError errorWithDomain:@"FramegenSyntheticMetal" code:1
                userInfo:@{NSLocalizedDescriptionKey:
                    [NSString stringWithUTF8String:error_text.empty()
                        ? "display initialization failed" : error_text.c_str()]}];
        }
        return nil;
    }
    if (@available(macOS 14.0, *)) {
        CAMetalLayer* layer = (CAMetalLayer*)_view.layer;
        _display_link = [[CAMetalDisplayLink alloc] initWithMetalLayer:layer];
        _display_link.delegate = self;
        _display_link.preferredFrameLatency = 1.0F;
        _display_link.preferredFrameRateRange = CAFrameRateRangeMake(60.0F, 60.0F, 60.0F);
    } else {
        if (out_error != nullptr) {
            *out_error = [NSError errorWithDomain:@"FramegenSyntheticMetal" code:2
                userInfo:@{NSLocalizedDescriptionKey:
                    @"CAMetalDisplayLink requires macOS 14 or later"}];
        }
        return nil;
    }
    return self;
}

- (void)startCapture {
    if (_display_link == nil) return;
    _state->begin_capture();
    [_display_link addToRunLoop:[NSRunLoop mainRunLoop] forMode:NSRunLoopCommonModes];
}

- (void)dealloc {
    [self.finalizeTimer invalidate];
    [_display_link invalidate];
}

- (void)metalDisplayLink:(CAMetalDisplayLink*)link
              needsUpdate:(CAMetalDisplayLinkUpdate*)update {
    (void)link;
    @autoreleasepool {
        _state->on_display_update(update);
    }
}

- (void)stopCapture:(NSTimer*)timer {
    (void)timer;
    if (_state->stopping.exchange(true)) return;
    _state->stop_ns = steady_now_ns();
    _display_link.paused = YES;
    [_display_link invalidate];
    // Drain existing GPU, generation, and drawable handlers for a bounded
    // period without blocking the AppKit run loop or waiting on the GPU.
    _finalize_deadline_ns = steady_now_ns() + 5'000'000'000LL;
    self.finalizeTimer = [NSTimer scheduledTimerWithTimeInterval:0.05
                                                         target:self
                                                       selector:@selector(finalizeCapture:)
                                                       userInfo:nil
                                                        repeats:YES];
    if (self.finalizeTimer == nil) {
        _finalize_deadline_ns = steady_now_ns();
        [self finalizeCapture:nil];
    }
}

- (BOOL)isEvidenceFinalized {
    return _evidence_finalized;
}

- (void)finalizeCapture:(NSTimer*)timer {
    const bool drained = !_state->has_pending_async_work();
    if (!drained && steady_now_ns() < _finalize_deadline_ns) {
        return;
    }
    [timer invalidate];
    [self.finalizeTimer invalidate];
    self.finalizeTimer = nil;
    const bool evidence_written = _state->write_evidence();
    _evidence_finalized = YES;
    if (!drained || !evidence_written) {
        g_run_exit_code.store(1, std::memory_order_release);
    }
    [NSApp terminate:nil];
}

@end

@interface SyntheticAppDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, weak) SyntheticOutputDelegate* controller;
@end

@implementation SyntheticAppDelegate
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication*)sender {
    (void)sender;
    return YES;
}
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication*)sender {
    (void)sender;
    if (self.controller != nil && ![self.controller isEvidenceFinalized]) {
        [self.controller stopCapture:nil];
        return NSTerminateCancel;
    }
    return NSTerminateNow;
}
@end

namespace {

bool parse_options(int argc, const char* argv[], RunOptions& options, std::string& error_text) {
    for (int i = 1; i < argc; ++i) {
        const std::string_view argument(argv[i]);
        if (argument == "--duration-seconds" && i + 1 < argc) {
            char* end = nullptr;
            const double duration = std::strtod(argv[++i], &end);
            if (end == argv[i] || *end != '\0' || !std::isfinite(duration) ||
                duration < 1.0 || duration > 600.0) {
                error_text = "--duration-seconds must be between 1 and 600";
                return false;
            }
            options.duration_seconds = duration;
        } else if (argument == "--output" && i + 1 < argc) {
            options.output_directory = argv[++i];
            if (options.output_directory.empty()) {
                error_text = "--output must name a new evidence directory";
                return false;
            }
        } else {
            error_text = "usage: framegen-dxmt-synthetic-metal "
                         "[--duration-seconds 60] [--output DIR]";
            return false;
        }
    }
    return true;
}

} // namespace

int main(int argc, const char* argv[]) {
    @autoreleasepool {
        RunOptions options;
        std::string error_text;
        if (!parse_options(argc, argv, options, error_text)) {
            std::fprintf(stderr, "%s\n", error_text.c_str());
            return 2;
        }
        if (!reserve_output_directory(options.output_directory, error_text)) {
            std::fprintf(stderr, "Synthetic Metal host: %s\n", error_text.c_str());
            return 2;
        }
        id<MTLDevice> native_device = MTLCreateSystemDefaultDevice();
        if (native_device == nil) {
            std::fprintf(stderr, "Synthetic Metal host: no Metal device is available.\n");
            return 1;
        }
#if defined(__x86_64__)
        std::printf("process_arch=x86_64\n");
#elif defined(__arm64__)
        std::printf("process_arch=arm64\n");
#else
        std::printf("process_arch=other\n");
#endif
        std::printf("metal_device=%s\n",
                    native_device.name.UTF8String == nullptr
                        ? "unknown" : native_device.name.UTF8String);
        auto state = HostState::create(native_device, options, error_text);
        if (!state) {
            std::fprintf(stderr, "Synthetic Metal host: %s\n", error_text.c_str());
            return 1;
        }
        std::printf("submission=FrameGenerator::submit interpolation=0.5 "
                    "source_updates_hz=30 display_link_hz=60 duration_seconds=%.1f "
                    "cpu_readback=none\n", options.duration_seconds);
        std::fflush(stdout);

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        SyntheticAppDelegate* app_delegate = [[SyntheticAppDelegate alloc] init];
        NSApp.delegate = app_delegate;
        const NSRect content_rect = NSMakeRect(0, 0, 960, 540);
        NSWindow* window = [[NSWindow alloc]
            initWithContentRect:content_rect
                      styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                        backing:NSBackingStoreBuffered
                          defer:NO];
        window.title = @"Framegen Synthetic Metal — bounded A/G/B display harness";
        [window center];
        MTKView* view = [[MTKView alloc] initWithFrame:content_rect device:native_device];
        view.colorPixelFormat = MTLPixelFormatBGRA8Unorm;
        view.clearColor = MTLClearColorMake(0.015, 0.02, 0.03, 1.0);
        NSError* display_error = nil;
        SyntheticOutputDelegate* controller = [[SyntheticOutputDelegate alloc]
            initWithState:std::move(state) view:view error:&display_error];
        if (controller == nil) {
            std::fprintf(stderr, "Synthetic Metal host: %s\n",
                         ns_error_text(display_error, "display initialization failed").c_str());
            return 1;
        }
        app_delegate.controller = controller;
        window.contentView = view;
        [window makeKeyAndOrderFront:nil];
        [NSApp activateIgnoringOtherApps:YES];
        [controller startCapture];
        [NSTimer scheduledTimerWithTimeInterval:options.duration_seconds
                                         target:controller
                                       selector:@selector(stopCapture:)
                                       userInfo:nil
                                        repeats:NO];
        [NSApp run];
    }
    return g_run_exit_code.load(std::memory_order_acquire);
}
