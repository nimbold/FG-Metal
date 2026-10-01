#import "metal_backend.hpp"

#import <Foundation/Foundation.h>

#include "framegen/metal.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace framegen::metal {
namespace {

constexpr std::string_view kBackendId = "org.framegen.metal";

std::atomic<std::uint64_t> g_next_device_id{1};

Error make_error(ErrorCode code, std::string message) {
    return Error{code, std::move(message)};
}

std::string error_message(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) {
        return fallback;
    }
    const char* utf8 = error.localizedDescription.UTF8String;
    return utf8 == nullptr ? fallback : utf8;
}

std::optional<MTLPixelFormat> to_metal_format(PixelFormat format) {
    switch (format) {
    case PixelFormat::r8_unorm: return MTLPixelFormatR8Unorm;
    case PixelFormat::rg8_unorm: return MTLPixelFormatRG8Unorm;
    case PixelFormat::rgba8_unorm: return MTLPixelFormatRGBA8Unorm;
    case PixelFormat::rgba8_unorm_srgb: return MTLPixelFormatRGBA8Unorm_sRGB;
    case PixelFormat::bgra8_unorm: return MTLPixelFormatBGRA8Unorm;
    case PixelFormat::bgra8_unorm_srgb: return MTLPixelFormatBGRA8Unorm_sRGB;
    case PixelFormat::rgb10a2_unorm: return MTLPixelFormatRGB10A2Unorm;
    case PixelFormat::bgr10a2_unorm: return MTLPixelFormatBGR10A2Unorm;
    case PixelFormat::rg11b10_float: return MTLPixelFormatRG11B10Float;
    case PixelFormat::rgba16_float: return MTLPixelFormatRGBA16Float;
    case PixelFormat::rgba32_float: return MTLPixelFormatRGBA32Float;
    case PixelFormat::unknown: return std::nullopt;
    }
    return std::nullopt;
}

std::optional<PixelFormat> from_metal_format(MTLPixelFormat format) {
    switch (format) {
    case MTLPixelFormatR8Unorm: return PixelFormat::r8_unorm;
    case MTLPixelFormatRG8Unorm: return PixelFormat::rg8_unorm;
    case MTLPixelFormatRGBA8Unorm: return PixelFormat::rgba8_unorm;
    case MTLPixelFormatRGBA8Unorm_sRGB: return PixelFormat::rgba8_unorm_srgb;
    case MTLPixelFormatBGRA8Unorm: return PixelFormat::bgra8_unorm;
    case MTLPixelFormatBGRA8Unorm_sRGB: return PixelFormat::bgra8_unorm_srgb;
    case MTLPixelFormatRGB10A2Unorm: return PixelFormat::rgb10a2_unorm;
    case MTLPixelFormatBGR10A2Unorm: return PixelFormat::bgr10a2_unorm;
    case MTLPixelFormatRG11B10Float: return PixelFormat::rg11b10_float;
    case MTLPixelFormatRGBA16Float: return PixelFormat::rgba16_float;
    case MTLPixelFormatRGBA32Float: return PixelFormat::rgba32_float;
    default: return std::nullopt;
    }
}

bool is_blend_format(PixelFormat format) {
    // The first implementation keeps the kernel contract deliberately narrow.
    // Additional typed formats can be added with targeted Metal validation.
    return format == PixelFormat::rgba8_unorm;
}

constexpr const char* kBlendShader = R"metal(
#include <metal_stdlib>
using namespace metal;

kernel void framegen_blend(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::write> output [[texture(2)]],
    constant float& interpolation [[buffer(0)]],
    uint2 position [[thread_position_in_grid]]) {
    const float4 a = previous.read(position);
    const float4 b = current.read(position);
    output.write(mix(a, b, interpolation), position);
}
)metal";

std::shared_ptr<detail::MetalTextureResource> as_metal_resource(const Texture& texture) {
    return std::dynamic_pointer_cast<detail::MetalTextureResource>(texture.resource());
}

class MetalEventSyncPoint {
public:
    virtual ~MetalEventSyncPoint() = default;
    [[nodiscard]] virtual id<MTLSharedEvent> event() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t value() const noexcept = 0;
};

class MetalEventDependency final : public GpuSyncPoint, public MetalEventSyncPoint {
public:
    MetalEventDependency(id<MTLSharedEvent> event, std::uint64_t value,
                         std::uint64_t device_id)
        : event_(event), value_(value), device_id_(device_id) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_id_;
    }
    [[nodiscard]] id<MTLSharedEvent> event() const noexcept override { return event_; }
    [[nodiscard]] std::uint64_t value() const noexcept override { return value_; }

private:
    __strong id<MTLSharedEvent> event_;
    std::uint64_t value_;
    std::uint64_t device_id_;
};

class MetalCompletion final : public GpuCompletion, public MetalEventSyncPoint {
public:
    MetalCompletion(id<MTLCommandBuffer> command_buffer,
                    id<MTLSharedEvent> event, std::uint64_t value,
                    std::uint64_t device_id,
                    std::shared_ptr<TextureResource> previous,
                    std::shared_ptr<TextureResource> current,
                    std::shared_ptr<TextureResource> output,
                    std::vector<std::shared_ptr<const GpuSyncPoint>> dependencies)
        : command_buffer_(command_buffer), event_(event), value_(value),
          device_id_(device_id), previous_(std::move(previous)),
          current_(std::move(current)), output_(std::move(output)),
          dependencies_(std::move(dependencies)) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_id_;
    }
    [[nodiscard]] id<MTLSharedEvent> event() const noexcept override { return event_; }
    [[nodiscard]] std::uint64_t value() const noexcept override { return value_; }
    [[nodiscard]] bool is_complete() const noexcept override {
        return event_.signaledValue >= value_ ||
               command_buffer_.status == MTLCommandBufferStatusError;
    }

    [[nodiscard]] std::optional<std::uint64_t>
    gpu_execution_time_ns() const noexcept override {
        const NSTimeInterval started = command_buffer_.GPUStartTime;
        const NSTimeInterval ended = command_buffer_.GPUEndTime;
        if (!std::isfinite(started) || !std::isfinite(ended) ||
            started <= 0.0 || ended < started) {
            return std::nullopt;
        }
        const long double elapsed_ns =
            static_cast<long double>(ended - started) * 1'000'000'000.0L;
        if (elapsed_ns > static_cast<long double>(
                             std::numeric_limits<std::uint64_t>::max())) {
            return std::nullopt;
        }
        return static_cast<std::uint64_t>(elapsed_ns);
    }

    [[nodiscard]] Result<void> wait() override {
        [command_buffer_ waitUntilCompleted];
        if (command_buffer_.status == MTLCommandBufferStatusError) {
            return std::unexpected(make_error(
                ErrorCode::backend_failure,
                error_message(command_buffer_.error, "Metal command buffer failed")));
        }
        return {};
    }

private:
    __strong id<MTLCommandBuffer> command_buffer_;
    __strong id<MTLSharedEvent> event_;
    std::uint64_t value_;
    std::uint64_t device_id_;
    std::shared_ptr<TextureResource> previous_;
    std::shared_ptr<TextureResource> current_;
    std::shared_ptr<TextureResource> output_;
    std::vector<std::shared_ptr<const GpuSyncPoint>> dependencies_;
};

} // namespace

namespace detail {

MetalTextureResource::MetalTextureResource(id<MTLDevice> device,
                                           id<MTLTexture> texture,
                                           TextureDescriptor descriptor,
                                           std::uint64_t device_id)
    : device_(device), texture_(texture), descriptor_(descriptor),
      device_id_(device_id) {}

TextureDescriptor MetalTextureResource::descriptor() const noexcept {
    return descriptor_;
}

std::string_view MetalTextureResource::backend_id() const noexcept {
    return kBackendId;
}

std::uint64_t MetalTextureResource::device_id() const noexcept {
    return device_id_;
}

id<MTLTexture> MetalTextureResource::native_texture() const noexcept {
    return texture_;
}

bool MetalTextureResource::belongs_to(id<MTLDevice> device) const noexcept {
    return device_ == device;
}

const void* MetalTextureResource::resource_identity() const noexcept {
    return (__bridge const void*)texture_;
}

MetalBackend::MetalBackend(id<MTLDevice> device, id<MTLCommandQueue> queue,
                           id<MTLComputePipelineState> blend_pipeline,
                           id<MTLSharedEvent> completion_event,
                           std::uint64_t device_id)
    : device_(device), queue_(queue), blend_pipeline_(blend_pipeline),
      completion_event_(completion_event), device_id_(device_id) {}

std::string_view MetalBackend::backend_id() const noexcept {
    return kBackendId;
}

std::uint64_t MetalBackend::device_id() const noexcept {
    return device_id_;
}

id<MTLDevice> MetalBackend::native_device() const noexcept {
    return device_;
}

Result<GeneratedFrame> MetalBackend::submit(const FrameSubmission& submission) {
    const auto& previous = submission.previous.texture;
    const auto& current = submission.current.texture;
    // The blend placeholder has no temporal model or history to invalidate.
    (void)submission.reset_history;
    if (!previous || !current) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "both Metal input textures must be valid"));
    }
    if (!std::isfinite(submission.interpolation) ||
        submission.interpolation < 0.0F || submission.interpolation > 1.0F) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "interpolation must be finite and in [0, 1]"));
    }
    if (previous.backend_id() != kBackendId || current.backend_id() != kBackendId ||
        previous.device_id() != device_id_ || current.device_id() != device_id_) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal textures belong to another backend device"));
    }

    auto previous_resource = as_metal_resource(previous);
    auto current_resource = as_metal_resource(current);
    if (!previous_resource || !current_resource ||
        !previous_resource->belongs_to(device_) || !current_resource->belongs_to(device_)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal texture resources are not owned by this device"));
    }

    const auto previous_descriptor = previous.descriptor();
    const auto current_descriptor = current.descriptor();
    if (previous_descriptor.width != current_descriptor.width ||
        previous_descriptor.height != current_descriptor.height ||
        previous_descriptor.format != current_descriptor.format ||
        previous_descriptor.color_space != current_descriptor.color_space ||
        previous_descriptor.alpha_mode != current_descriptor.alpha_mode) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal input texture descriptions must match"));
    }
    if (!is_blend_format(previous_descriptor.format)) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "placeholder Metal blending currently supports rgba8_unorm"));
    }

    id<MTLTexture> previous_native = previous_resource->native_texture();
    id<MTLTexture> current_native = current_resource->native_texture();
    if ((previous_native.usage & MTLTextureUsageShaderRead) == 0 ||
        (current_native.usage & MTLTextureUsageShaderRead) == 0) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal input textures must allow shader reads"));
    }

    MTLTextureDescriptor* output_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:previous_descriptor.width
                                                          height:previous_descriptor.height
                                                       mipmapped:NO];
    output_descriptor.storageMode = MTLStorageModePrivate;
    output_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> output_native = [device_ newTextureWithDescriptor:output_descriptor];
    if (output_native == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not allocate the output texture"));
    }

    TextureDescriptor result_descriptor = previous_descriptor;
    auto output_resource = std::make_shared<MetalTextureResource>(
        device_, output_native, result_descriptor, device_id_);
    auto output_texture = Texture::from_resource(output_resource);
    if (!output_texture) {
        return std::unexpected(output_texture.error());
    }

    id<MTLCommandBuffer> command_buffer = [queue_ commandBuffer];
    if (command_buffer == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal could not allocate a command buffer"));
    }

    for (const auto& dependency : submission.gpu_dependencies) {
        if (!dependency || dependency->backend_id() != kBackendId ||
            dependency->device_id() != device_id_) {
            return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                               "GPU dependency belongs to another backend device"));
        }
        const auto* metal_dependency = dynamic_cast<const MetalEventSyncPoint*>(dependency.get());
        if (metal_dependency == nullptr || metal_dependency->event() == nil ||
            metal_dependency->value() == 0) {
            return std::unexpected(make_error(ErrorCode::invalid_argument,
                                               "GPU dependency is not a valid Metal event point"));
        }
        [command_buffer encodeWaitForEvent:metal_dependency->event()
                                      value:metal_dependency->value()];
    }

    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal could not create a compute encoder"));
    }
    [encoder setComputePipelineState:blend_pipeline_];
    [encoder setTexture:previous_native atIndex:0];
    [encoder setTexture:current_native atIndex:1];
    [encoder setTexture:output_native atIndex:2];
    const float interpolation = submission.interpolation;
    [encoder setBytes:&interpolation length:sizeof(interpolation) atIndex:0];

    const NSUInteger threads_x = blend_pipeline_.threadExecutionWidth;
    const NSUInteger threads_y = std::max<NSUInteger>(
        1, std::min<NSUInteger>(8, blend_pipeline_.maxTotalThreadsPerThreadgroup / threads_x));
    [encoder dispatchThreads:MTLSizeMake(output_native.width, output_native.height, 1)
        threadsPerThreadgroup:MTLSizeMake(threads_x, threads_y, 1)];
    [encoder endEncoding];

    std::uint64_t event_value{};
    {
        // Event values must follow the command queue's commit order. Holding
        // this lock through commit prevents concurrent callers from signaling
        // a larger value before an earlier frame has finished.
        std::scoped_lock lock(submission_mutex_);
        event_value = next_event_value_.fetch_add(1, std::memory_order_relaxed);
        if (event_value == 0) {
            return std::unexpected(make_error(ErrorCode::backend_failure,
                                               "Metal completion event sequence was exhausted"));
        }
        [command_buffer encodeSignalEvent:completion_event_ value:event_value];
        [command_buffer commit];
    }

    auto completion = std::make_shared<MetalCompletion>(
        command_buffer, completion_event_, event_value, device_id_,
        previous.resource(), current.resource(), output_texture->resource(),
        submission.gpu_dependencies);
    return GeneratedFrame{std::move(*output_texture), std::move(completion)};
}

} // namespace detail

struct Device::State {
    __strong id<MTLDevice> native_device;
    __strong id<MTLCommandQueue> queue;
    __strong id<MTLComputePipelineState> blend_pipeline;
    __strong id<MTLSharedEvent> completion_event;
    std::uint64_t device_id{};
    std::shared_ptr<FrameGenerationBackend> backend;
};

Result<Device> Device::create(id<MTLDevice> native_device) {
    if (native_device == nil) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device must not be nil"));
    }

    id<MTLCommandQueue> queue = [native_device newCommandQueue];
    if (queue == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not create a command queue"));
    }

    NSError* error = nil;
    NSString* shader_source = [NSString stringWithUTF8String:kBlendShader];
    id<MTLLibrary> library = [native_device newLibraryWithSource:shader_source
                                                         options:nil
                                                           error:&error];
    if (library == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           error_message(error, "Metal blend shader compilation failed")));
    }
    id<MTLFunction> function = [library newFunctionWithName:@"framegen_blend"];
    if (function == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal blend function was not found"));
    }
    id<MTLComputePipelineState> pipeline =
        [native_device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           error_message(error, "Metal compute pipeline creation failed")));
    }

    id<MTLSharedEvent> event = [native_device newSharedEvent];
    if (event == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not create a shared completion event"));
    }

    const std::uint64_t device_id = g_next_device_id.fetch_add(1, std::memory_order_relaxed);
    if (device_id == 0) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal device identifier sequence was exhausted"));
    }

    auto state = std::make_shared<State>();
    state->native_device = native_device;
    state->queue = queue;
    state->blend_pipeline = pipeline;
    state->completion_event = event;
    state->device_id = device_id;
    state->backend = std::make_shared<detail::MetalBackend>(
        native_device, queue, pipeline, event, device_id);
    return Device(std::move(state));
}

Result<Texture> Device::create_texture(const TextureDescriptor& descriptor) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (descriptor.width == 0 || descriptor.height == 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal texture dimensions must be non-zero"));
    }
    const auto pixel_format = to_metal_format(descriptor.format);
    if (!pixel_format || descriptor.format != PixelFormat::rgba8_unorm) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal texture allocation currently supports rgba8_unorm"));
    }

    MTLTextureDescriptor* native_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:*pixel_format
                                                           width:descriptor.width
                                                          height:descriptor.height
                                                       mipmapped:NO];
    native_descriptor.storageMode = MTLStorageModePrivate;
    native_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> texture = [state_->native_device newTextureWithDescriptor:native_descriptor];
    if (texture == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not allocate the requested texture"));
    }

    auto resource = std::make_shared<detail::MetalTextureResource>(
        state_->native_device, texture, descriptor, state_->device_id);
    return Texture::from_resource(std::move(resource));
}

Result<Texture> Device::wrap_texture(id<MTLTexture> native_texture,
                                    ColorSpace color_space,
                                    AlphaMode alpha_mode) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (native_texture == nil || native_texture.device != state_->native_device) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "native Metal texture must belong to this device"));
    }
    if (native_texture.textureType != MTLTextureType2D || native_texture.sampleCount != 1 ||
        native_texture.width > std::numeric_limits<std::uint32_t>::max() ||
        native_texture.height > std::numeric_limits<std::uint32_t>::max()) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "only single-sample 2D Metal textures are supported"));
    }
    const auto pixel_format = from_metal_format(native_texture.pixelFormat);
    if (!pixel_format) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal texture format is not represented by framegen"));
    }
    TextureDescriptor descriptor{
        static_cast<std::uint32_t>(native_texture.width),
        static_cast<std::uint32_t>(native_texture.height),
        *pixel_format,
        color_space,
        alpha_mode,
    };
    auto resource = std::make_shared<detail::MetalTextureResource>(
        state_->native_device, native_texture, descriptor, state_->device_id);
    return Texture::from_resource(std::move(resource));
}

Result<std::shared_ptr<const GpuSyncPoint>> Device::make_event_dependency(
    const EventPoint& point) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    const id<MTLDevice> event_device = point.event.device;
    if (point.event == nil || point.value == 0 ||
        (event_device != nil && event_device != state_->native_device)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal event must be non-nil and usable on this device"));
    }
    std::shared_ptr<const GpuSyncPoint> dependency =
        std::make_shared<MetalEventDependency>(point.event, point.value, state_->device_id);
    return dependency;
}

id<MTLTexture> Device::native_texture(const Texture& texture) const noexcept {
    if (!state_ || !texture || texture.backend_id() != kBackendId ||
        texture.device_id() != state_->device_id) {
        return nil;
    }
    const auto resource = as_metal_resource(texture);
    if (!resource || !resource->belongs_to(state_->native_device)) {
        return nil;
    }
    return resource->native_texture();
}

Result<EventPoint> Device::event_point(const GpuSyncPoint& point) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (point.backend_id() != kBackendId || point.device_id() != state_->device_id) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "GPU synchronization point belongs to another device"));
    }
    const auto* metal_point = dynamic_cast<const MetalEventSyncPoint*>(&point);
    if (metal_point == nullptr || metal_point->event() == nil || metal_point->value() == 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "GPU synchronization point is not a valid Metal event"));
    }
    return EventPoint{metal_point->event(), metal_point->value()};
}

std::shared_ptr<FrameGenerationBackend> Device::backend() const noexcept {
    return state_ ? state_->backend : nullptr;
}

} // namespace framegen::metal
