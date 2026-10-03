#import <Metal/Metal.h>

#include "synthetic_metal_backend.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <limits>
#include <mutex>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
#include <cstdlib>
#include <cstring>
#import <mach/mach_time.h>

namespace framegen::dxmt_synthetic {
namespace {

constexpr std::string_view kBackendId = "org.framegen.dxmt.synthetic-metal";
std::atomic<std::uint64_t> g_next_device_id{1};

Error make_error(ErrorCode code, std::string message) {
    return Error{code, std::move(message)};
}

std::string ns_error_message(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) {
        return fallback;
    }
    const char* message = error.localizedDescription.UTF8String;
    return message == nullptr ? fallback : message;
}

std::int64_t monotonic_now_ns() {
    static mach_timebase_info_data_t timebase = [] {
        mach_timebase_info_data_t value{};
        mach_timebase_info(&value);
        return value;
    }();
    const auto ticks = mach_absolute_time();
    return static_cast<std::int64_t>(
        (static_cast<__uint128_t>(ticks) * timebase.numer) / timebase.denom);
}

bool fail_at(const char* stage) {
    const char* requested = std::getenv("DXMT_FRAMEGEN_FAIL");
    return requested != nullptr && std::strcmp(requested, stage) == 0;
}

class SyntheticMetalTexture final : public TextureResource {
public:
    SyntheticMetalTexture(id<MTLDevice> device, id<MTLTexture> texture,
                          TextureDescriptor descriptor, std::uint64_t device_id)
        : device_(device), texture_(texture), descriptor_(descriptor),
          device_id_(device_id), identity_((__bridge const void*)texture) {}

    [[nodiscard]] TextureDescriptor descriptor() const noexcept override {
        return descriptor_;
    }
    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_id_;
    }
    [[nodiscard]] const void* resource_identity() const noexcept override {
        return identity_;
    }
    [[nodiscard]] id<MTLTexture> native_texture() const noexcept {
        return texture_;
    }
    [[nodiscard]] bool belongs_to(id<MTLDevice> device) const noexcept {
        return device_ == device && texture_.device == device;
    }

private:
    __strong id<MTLDevice> device_;
    __strong id<MTLTexture> texture_;
    TextureDescriptor descriptor_;
    std::uint64_t device_id_{};
    const void* identity_{};
};

class NativeMetalEvent {
public:
    virtual ~NativeMetalEvent() = default;

    NativeMetalEvent(id<MTLSharedEvent> event, std::uint64_t value,
                     std::uint64_t device_id)
        : event_(event), value_(value), device_id_(device_id) {}

    [[nodiscard]] id<MTLSharedEvent> event() const noexcept { return event_; }
    [[nodiscard]] std::uint64_t value() const noexcept { return value_; }
    [[nodiscard]] std::uint64_t device_id() const noexcept { return device_id_; }
    [[nodiscard]] bool signaled() const noexcept {
        return event_ != nil && event_.signaledValue >= value_;
    }

protected:
    __strong id<MTLSharedEvent> event_;
    std::uint64_t value_{};
    std::uint64_t device_id_{};
};

class NativeEventPoint final : public GpuSyncPoint, public NativeMetalEvent {
public:
    using NativeMetalEvent::NativeMetalEvent;

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return NativeMetalEvent::device_id();
    }
    [[nodiscard]] std::optional<bool> is_signaled() const noexcept override {
        return signaled();
    }
};

class SyntheticMetalCompletion final : public GpuCompletion,
                                       public NativeMetalEvent {
public:
    SyntheticMetalCompletion(id<MTLCommandBuffer> command_buffer,
                             id<MTLSharedEvent> event, std::uint64_t value,
                             std::uint64_t device_id,
                             std::vector<Texture> retained_inputs)
        : NativeMetalEvent(event, value, device_id),
          command_buffer_(command_buffer), retained_inputs_(std::move(retained_inputs)) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return NativeMetalEvent::device_id();
    }
    [[nodiscard]] std::optional<bool> is_signaled() const noexcept override {
        return signaled();
    }
    [[nodiscard]] bool is_complete() const noexcept override {
        return poll_status() != GpuCompletionStatus::pending;
    }
    [[nodiscard]] Result<void> wait() override {
        if (command_buffer_ == nil) {
            return std::unexpected(make_error(ErrorCode::backend_failure,
                                               "Metal completion lost its command buffer"));
        }
        [command_buffer_ waitUntilCompleted];
        if (command_buffer_.status != MTLCommandBufferStatusCompleted ||
            !signaled()) {
            return std::unexpected(make_error(ErrorCode::backend_failure,
                ns_error_message(command_buffer_.error,
                                 "synthetic Metal blend did not complete successfully")));
        }
        return {};
    }
    [[nodiscard]] GpuCompletionStatus poll_status() const noexcept override {
        const auto completed_status = completed_status_.load(std::memory_order_acquire);
        if (completed_status != GpuCompletionStatus::pending) {
            return completed_status;
        }
        if (command_buffer_ == nil) {
            return GpuCompletionStatus::failed;
        }
        if (command_buffer_.status == MTLCommandBufferStatusError) {
            return GpuCompletionStatus::failed;
        }
        if (command_buffer_.status != MTLCommandBufferStatusCompleted) {
            return GpuCompletionStatus::pending;
        }
        return signaled()
            ? GpuCompletionStatus::complete : GpuCompletionStatus::failed;
    }

    void add_completion_handler(
        SyntheticMetalBackend::CompletionCallback callback) {
        if (!callback) {
            return;
        }
        const auto status = completed_status_.load(std::memory_order_acquire);
        if (status != GpuCompletionStatus::pending) {
            callback(status, completed_at_ns_.load(std::memory_order_acquire));
            return;
        }
        std::optional<std::pair<GpuCompletionStatus, std::int64_t>> completed;
        {
            std::scoped_lock lock(callback_mutex_);
            const auto locked_status = completed_status_.load(std::memory_order_acquire);
            if (locked_status == GpuCompletionStatus::pending) {
                callbacks_.push_back(std::move(callback));
                return;
            }
            completed = std::pair{locked_status,
                completed_at_ns_.load(std::memory_order_acquire)};
        }
        callback(completed->first, completed->second);
    }

    void mark_completed(GpuCompletionStatus status, std::int64_t completed_at_ns) {
        completed_at_ns_.store(completed_at_ns, std::memory_order_relaxed);
        completed_status_.store(status, std::memory_order_release);
        std::vector<SyntheticMetalBackend::CompletionCallback> callbacks;
        {
            std::scoped_lock lock(callback_mutex_);
            callbacks.swap(callbacks_);
        }
        for (auto& callback : callbacks) {
            callback(status, completed_at_ns);
        }
    }

private:
    __strong id<MTLCommandBuffer> command_buffer_;
    // Retain inputs until queued GPU reads have retired, as required by the SPI.
    std::vector<Texture> retained_inputs_;
    std::atomic<GpuCompletionStatus> completed_status_{
        GpuCompletionStatus::pending};
    std::atomic<std::int64_t> completed_at_ns_{0};
    std::mutex callback_mutex_;
    std::vector<SyntheticMetalBackend::CompletionCallback> callbacks_;
};

constexpr const char* kBlendShader = R"metal(
#include <metal_stdlib>
using namespace metal;

kernel void blend_midpoint(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::write> output [[texture(2)]],
    constant float& interpolation [[buffer(0)]],
    uint2 position [[thread_position_in_grid]]) {
    if (position.x >= output.get_width() || position.y >= output.get_height()) {
        return;
    }
    const float4 a = previous.read(position);
    const float4 b = current.read(position);
    output.write(mix(a, b, interpolation), position);
}
)metal";

std::shared_ptr<SyntheticMetalTexture> as_synthetic_texture(const Texture& texture) {
    if (!texture) {
        return {};
    }
    return std::dynamic_pointer_cast<SyntheticMetalTexture>(texture.resource());
}

} // namespace

struct SyntheticMetalBackend::State {
    __strong id<MTLDevice> device;
    __strong id<MTLCommandQueue> queue;
    __strong id<MTLComputePipelineState> blend_pipeline;
    __strong id<MTLSharedEvent> completion_event;
    std::uint64_t device_id{};
    std::atomic<std::uint64_t> next_event_value{1};
    std::mutex submission_mutex;
};

Result<std::shared_ptr<SyntheticMetalBackend>> SyntheticMetalBackend::create(
    id<MTLDevice> device) {
    if (device == nil) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device must not be nil"));
    }

    const auto device_id = g_next_device_id.fetch_add(1, std::memory_order_relaxed);
    if (device_id == 0 || device_id == std::numeric_limits<std::uint64_t>::max()) {
        return std::unexpected(make_error(ErrorCode::internal_error,
                                           "synthetic Metal device ID sequence exhausted"));
    }

    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLSharedEvent> event = [device newSharedEvent];
    if (queue == nil || event == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal queue or shared event creation failed"));
    }

    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:
        [NSString stringWithUTF8String:kBlendShader] options:nil error:&error];
    id<MTLFunction> function = [library newFunctionWithName:@"blend_midpoint"];
    id<MTLComputePipelineState> pipeline = function == nil
        ? nil : [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            ns_error_message(error, "synthetic Metal blend pipeline creation failed")));
    }

    auto state = std::make_shared<State>();
    state->device = device;
    state->queue = queue;
    state->blend_pipeline = pipeline;
    state->completion_event = event;
    state->device_id = device_id;
    return std::shared_ptr<SyntheticMetalBackend>(
        new SyntheticMetalBackend(std::move(state)));
}

std::string_view SyntheticMetalBackend::backend_id() const noexcept {
    return kBackendId;
}

std::uint64_t SyntheticMetalBackend::device_id() const noexcept {
    return state_ ? state_->device_id : 0;
}

Result<Texture> SyntheticMetalBackend::wrap_texture(
    id<MTLTexture> texture, ColorSpace color_space, AlphaMode alpha_mode) const {
    if (!state_ || texture == nil || texture.device != state_->device) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
            "imported texture must belong to the synthetic backend Metal device"));
    }
    if (texture.textureType != MTLTextureType2D || texture.sampleCount != 1 ||
        (texture.pixelFormat != MTLPixelFormatRGBA8Unorm &&
         texture.pixelFormat != MTLPixelFormatBGRA8Unorm) ||
        (texture.usage & MTLTextureUsageShaderRead) == 0 ||
        texture.width > std::numeric_limits<std::uint32_t>::max() ||
        texture.height > std::numeric_limits<std::uint32_t>::max()) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
            "synthetic blend requires shader-readable, single-sample RGBA8/BGRA8 2D textures"));
    }

    const TextureDescriptor descriptor{
        static_cast<std::uint32_t>(texture.width),
        static_cast<std::uint32_t>(texture.height),
        texture.pixelFormat == MTLPixelFormatBGRA8Unorm
            ? PixelFormat::bgra8_unorm : PixelFormat::rgba8_unorm,
        color_space,
        alpha_mode,
    };
    return Texture::from_resource(std::make_shared<SyntheticMetalTexture>(
        state_->device, texture, descriptor, state_->device_id));
}

Result<Texture> SyntheticMetalBackend::create_texture(
    const TextureDescriptor& descriptor) const {
    if (!state_ || descriptor.width == 0 || descriptor.height == 0 ||
        (descriptor.format != PixelFormat::rgba8_unorm &&
         descriptor.format != PixelFormat::bgra8_unorm)) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
            "synthetic blend texture allocation supports nonzero RGBA8/BGRA8 dimensions"));
    }
    const MTLPixelFormat native_format = descriptor.format == PixelFormat::bgra8_unorm
        ? MTLPixelFormatBGRA8Unorm : MTLPixelFormatRGBA8Unorm;
    MTLTextureDescriptor* native_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:native_format
                                                           width:descriptor.width
                                                          height:descriptor.height
                                                       mipmapped:NO];
    native_descriptor.storageMode = MTLStorageModePrivate;
    native_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> texture = [state_->device newTextureWithDescriptor:native_descriptor];
    if (texture == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal texture allocation failed"));
    }
    return Texture::from_resource(std::make_shared<SyntheticMetalTexture>(
        state_->device, texture, descriptor, state_->device_id));
}

id<MTLTexture> SyntheticMetalBackend::native_texture(
    const Texture& texture) const noexcept {
    if (!state_ || texture.backend_id() != kBackendId ||
        texture.device_id() != state_->device_id) {
        return nil;
    }
    auto resource = as_synthetic_texture(texture);
    return resource && resource->belongs_to(state_->device)
        ? resource->native_texture() : nil;
}

Result<std::shared_ptr<const GpuSyncPoint>>
SyntheticMetalBackend::make_event_dependency(const EventPoint& point) const {
    if (!state_ || point.event == nil || point.value == 0 ||
        (point.event.device != nil && point.event.device != state_->device)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "shared event is not valid for this Metal device"));
    }
    std::shared_ptr<const GpuSyncPoint> result = std::make_shared<NativeEventPoint>(
        point.event, point.value, state_->device_id);
    return result;
}

Result<EventPoint> SyntheticMetalBackend::event_point(
    const GpuSyncPoint& point) const {
    if (!state_ || point.backend_id() != kBackendId ||
        point.device_id() != state_->device_id) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "sync point belongs to another synthetic Metal device"));
    }
    const auto* native = dynamic_cast<const NativeMetalEvent*>(&point);
    if (native == nullptr || native->event() == nil || native->value() == 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "sync point has no synthetic Metal event"));
    }
    return EventPoint{native->event(), native->value()};
}

Result<void> SyntheticMetalBackend::add_completion_handler(
    const std::shared_ptr<GpuCompletion>& completion,
    CompletionCallback callback) const {
    if (!state_ || !completion || completion->backend_id() != kBackendId ||
        completion->device_id() != state_->device_id || !callback) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
            "completion callback requires a live completion from this synthetic device"));
    }
    auto native = std::dynamic_pointer_cast<SyntheticMetalCompletion>(completion);
    if (!native) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
            "completion does not expose the synthetic Metal callback hook"));
    }
    native->add_completion_handler(std::move(callback));
    return {};
}

Result<GeneratedFrame> SyntheticMetalBackend::submit(
    const FrameSubmission& submission) {
    if (!state_ || fail_at("generation")) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            state_ ? "injected synthetic generation failure"
                   : "synthetic Metal backend is not initialized"));
    }
    auto previous = as_synthetic_texture(submission.previous.texture);
    auto current = as_synthetic_texture(submission.current.texture);
    if (!previous || !current ||
        !previous->belongs_to(state_->device) || !current->belongs_to(state_->device)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "blend inputs are not native textures on this device"));
    }
    if (previous->descriptor().width != current->descriptor().width ||
        previous->descriptor().height != current->descriptor().height ||
        previous->descriptor().format != current->descriptor().format) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "blend inputs must have matching RGBA8 dimensions"));
    }
    for (const auto& dependency : submission.gpu_dependencies) {
        if (!dependency) {
            return std::unexpected(make_error(ErrorCode::invalid_argument,
                                               "GPU dependency must not be null"));
        }
        const auto* native = dynamic_cast<const NativeMetalEvent*>(dependency.get());
        if (native == nullptr || native->event() == nil || native->value() == 0 ||
            native->device_id() != state_->device_id ||
            (native->event().device != nil && native->event().device != state_->device)) {
            return std::unexpected(make_error(ErrorCode::incompatible_resource,
                "GPU dependency is not a valid event for this synthetic Metal device"));
        }
    }

    const TextureDescriptor output_descriptor = previous->descriptor();
    auto output_result = create_texture(output_descriptor);
    if (!output_result) {
        return std::unexpected(output_result.error());
    }
    Texture output = std::move(*output_result);
    id<MTLTexture> output_native = native_texture(output);
    id<MTLTexture> previous_native = previous->native_texture();
    id<MTLTexture> current_native = current->native_texture();

    std::scoped_lock lock(state_->submission_mutex);
    const std::uint64_t event_value =
        state_->next_event_value.fetch_add(1, std::memory_order_relaxed);
    if (event_value == 0 || event_value == std::numeric_limits<std::uint64_t>::max()) {
        return std::unexpected(make_error(ErrorCode::internal_error,
                                           "synthetic Metal event value sequence exhausted"));
    }
    id<MTLCommandBuffer> command_buffer = [state_->queue commandBuffer];
    if (command_buffer == nil || output_native == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal blend command resources could not be created"));
    }
    // Command-buffer event waits must be encoded before opening an encoder.
    for (const auto& dependency : submission.gpu_dependencies) {
        const auto* native = dynamic_cast<const NativeMetalEvent*>(dependency.get());
        [command_buffer encodeWaitForEvent:native->event() value:native->value()];
    }
    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal compute encoder creation failed"));
    }
    std::vector<Texture> retained_inputs;
    retained_inputs.reserve(2);
    retained_inputs.push_back(submission.previous.texture);
    retained_inputs.push_back(submission.current.texture);
    auto completion = std::make_shared<SyntheticMetalCompletion>(
        command_buffer, state_->completion_event, event_value, state_->device_id,
        std::move(retained_inputs));

    [encoder setComputePipelineState:state_->blend_pipeline];
    [encoder setTexture:previous_native atIndex:0];
    [encoder setTexture:current_native atIndex:1];
    [encoder setTexture:output_native atIndex:2];
    const float interpolation = submission.interpolation;
    [encoder setBytes:&interpolation length:sizeof(interpolation) atIndex:0];
    const MTLSize grid = MTLSizeMake(output_descriptor.width,
                                     output_descriptor.height, 1);
    const NSUInteger width = state_->blend_pipeline.threadExecutionWidth;
    const NSUInteger height = std::max<NSUInteger>(
        1, std::min<NSUInteger>(8,
            state_->blend_pipeline.maxTotalThreadsPerThreadgroup / width));
    [encoder dispatchThreads:grid
        threadsPerThreadgroup:MTLSizeMake(width, height, 1)];
    [encoder endEncoding];
    [command_buffer encodeSignalEvent:state_->completion_event value:event_value];
    [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> completed_command) {
        const auto status = completed_command.status == MTLCommandBufferStatusCompleted &&
                            completion->signaled() && !fail_at("completion")
            ? GpuCompletionStatus::complete : GpuCompletionStatus::failed;
        completion->mark_completed(status, monotonic_now_ns());
    }];
    [command_buffer commit];

    return GeneratedFrame{std::move(output), std::move(completion)};
}

} // namespace framegen::dxmt_synthetic
