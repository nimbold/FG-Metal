#pragma once

#ifndef __OBJC__
#error "The synthetic Metal backend is an Objective-C++ test component."
#endif

#import <Metal/Metal.h>

#include "framegen/backend.hpp"

#include <cstdint>
#include <functional>
#include <memory>

namespace framegen::dxmt_synthetic {

struct EventPoint {
    __strong id<MTLSharedEvent> event;
    std::uint64_t value{};
};

class SyntheticMetalBackend final : public FrameGenerationBackend {
public:
    using CompletionCallback = std::function<void(
        GpuCompletionStatus, std::int64_t)>;

    [[nodiscard]] static Result<std::shared_ptr<SyntheticMetalBackend>> create(
        id<MTLDevice> device);

    [[nodiscard]] std::string_view backend_id() const noexcept override;
    [[nodiscard]] std::uint64_t device_id() const noexcept override;
    [[nodiscard]] Result<GeneratedFrame> submit(
        const FrameSubmission& submission) override;

    // Imports the caller's Metal texture directly. The resource wrapper
    // retains the id<MTLTexture>; no staging or CPU pixel access is used.
    [[nodiscard]] Result<Texture> wrap_texture(
        id<MTLTexture> texture,
        ColorSpace color_space = ColorSpace::linear_srgb,
        AlphaMode alpha_mode = AlphaMode::opaque) const;
    [[nodiscard]] Result<Texture> create_texture(
        const TextureDescriptor& descriptor) const;
    [[nodiscard]] id<MTLTexture> native_texture(
        const Texture& texture) const noexcept;

    [[nodiscard]] Result<std::shared_ptr<const GpuSyncPoint>> make_event_dependency(
        const EventPoint& point) const;
    [[nodiscard]] Result<EventPoint> event_point(
        const GpuSyncPoint& point) const;
    // Test-host hook: runs from Metal's command-buffer completion callback and
    // reports the host monotonic time without waiting or reading pixels.
    [[nodiscard]] Result<void> add_completion_handler(
        const std::shared_ptr<GpuCompletion>& completion,
        CompletionCallback callback) const;

private:
    struct State;
    explicit SyntheticMetalBackend(std::shared_ptr<State> state)
        : state_(std::move(state)) {}

    std::shared_ptr<State> state_;
};

} // namespace framegen::dxmt_synthetic
