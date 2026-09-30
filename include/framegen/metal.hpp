#pragma once

// This adapter header is for Objective-C++ translation units. It is kept
// separate from framegen.hpp so the renderer-neutral core never imports Metal.
#ifndef __OBJC__
#error "framegen/metal.hpp must be included from Objective-C++"
#endif

#import <Metal/Metal.h>

#include "framegen/backend.hpp"

#include <cstdint>
#include <memory>
#include <utility>

namespace framegen::metal {

// A Metal GPU synchronization point. This value strongly retains the shared
// event; event values are ordered in that MTLSharedEvent's signal domain.
struct EventPoint {
    __strong id<MTLSharedEvent> event;
    std::uint64_t value{};
};

// Owns the adapter state for one MTLDevice. Textures created or imported by
// this object are retained by their TextureResource wrappers. The wrapper can
// be copied; copies refer to the same device/backend identity.
class Device final {
public:
    Device() = default;

    [[nodiscard]] static Result<Device> create(id<MTLDevice> native_device);

    [[nodiscard]] bool valid() const noexcept {
        return static_cast<bool>(state_);
    }

    [[nodiscard]] Result<Texture> create_texture(
        const TextureDescriptor& descriptor) const;

    // Imports a native texture without copying pixels. Metal texture objects
    // do not carry the renderer's color-space or alpha interpretation, so the
    // adapter supplies those semantics explicitly when known. Wrapping does
    // not synchronize producer writes: pass their shared-event dependency in
    // FrameSubmission, or ensure the texture is ready before submit(). The
    // resource wrapper retains the MTLTexture and rejects other-device textures.
    [[nodiscard]] Result<Texture> wrap_texture(
        id<MTLTexture> native_texture,
        ColorSpace color_space = ColorSpace::unknown,
        AlphaMode alpha_mode = AlphaMode::unknown) const;

    // Wraps a producer event/value pair as an input dependency. The framegen
    // backend will encode a GPU wait before reading submitted textures.
    // MTLSharedEvent.device is expected to be nil; the caller must supply an
    // event usable by this Metal device. A non-nil device must match this one.
    [[nodiscard]] Result<std::shared_ptr<const GpuSyncPoint>> make_event_dependency(
        const EventPoint& point) const;

    // Returns a borrowed native handle for adapter-side encoding/display. It
    // is nil for an invalid texture or one from a different Metal device, and
    // remains valid while the supplied Texture keeps its resource alive.
    [[nodiscard]] id<MTLTexture> native_texture(const Texture& texture) const noexcept;

    // Extracts the event/value pair from a dependency or returned completion,
    // allowing a renderer to encode a GPU wait for generated output. The event
    // is borrowed from the sync point, which must remain alive through use.
    [[nodiscard]] Result<EventPoint> event_point(const GpuSyncPoint& point) const;

    [[nodiscard]] std::shared_ptr<FrameGenerationBackend> backend() const noexcept;

private:
    struct State;
    explicit Device(std::shared_ptr<State> state) : state_(std::move(state)) {}

    std::shared_ptr<State> state_;
};

} // namespace framegen::metal
