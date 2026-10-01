#pragma once

#ifndef __OBJC__
#error "The Metal backend implementation must be compiled as Objective-C++"
#endif

#import <Metal/Metal.h>

#include "framegen/backend.hpp"

#include <cstdint>
#include <memory>
#include <mutex>
#include <string_view>

namespace framegen::metal::detail {

class MetalTextureResource final : public TextureResource {
public:
    MetalTextureResource(id<MTLDevice> device, id<MTLTexture> texture,
                         TextureDescriptor descriptor, std::uint64_t device_id,
                         const void* resource_identity = nullptr);

    [[nodiscard]] TextureDescriptor descriptor() const noexcept override;
    [[nodiscard]] std::string_view backend_id() const noexcept override;
    [[nodiscard]] std::uint64_t device_id() const noexcept override;
    [[nodiscard]] const void* resource_identity() const noexcept override;
    [[nodiscard]] id<MTLTexture> native_texture() const noexcept;
    [[nodiscard]] bool belongs_to(id<MTLDevice> device) const noexcept;

private:
    __strong id<MTLDevice> device_;
    __strong id<MTLTexture> texture_;
    TextureDescriptor descriptor_;
    std::uint64_t device_id_;
    const void* resource_identity_{};
};

class MetalBackend final : public FrameGenerationBackend {
public:
    MetalBackend(id<MTLDevice> device, id<MTLCommandQueue> queue,
                 id<MTLComputePipelineState> blend_pipeline,
                 id<MTLSharedEvent> completion_event,
                 std::uint64_t device_id);

    [[nodiscard]] std::string_view backend_id() const noexcept override;
    [[nodiscard]] std::uint64_t device_id() const noexcept override;
    [[nodiscard]] id<MTLDevice> native_device() const noexcept;

    [[nodiscard]] Result<GeneratedFrame> submit(
        const FrameSubmission& submission) override;

private:
    __strong id<MTLDevice> device_;
    __strong id<MTLCommandQueue> queue_;
    __strong id<MTLComputePipelineState> blend_pipeline_;
    __strong id<MTLSharedEvent> completion_event_;
    std::uint64_t device_id_;
    std::uint64_t next_event_value_{1};
    std::mutex submission_mutex_;
};

} // namespace framegen::metal::detail
