#pragma once

#include "framegen/result.hpp"

#include <cstdint>
#include <memory>
#include <string_view>
#include <utility>

namespace framegen {

enum class PixelFormat : std::uint8_t {
    unknown,
    r8_unorm,
    rg8_unorm,
    rgba8_unorm,
    rgba8_unorm_srgb,
    bgra8_unorm,
    bgra8_unorm_srgb,
    rgb10a2_unorm,
    bgr10a2_unorm,
    rg11b10_float,
    rgba16_float,
    rgba32_float,
    r16_float,
    rg16_float,
    rg32_float,
    r32_float,
    d16_unorm,
    d32_float,
    d24_unorm_s8_uint,
};

enum class ColorSpace : std::uint8_t {
    unknown,
    linear_srgb,
    srgb,
    display_p3,
    rec2020_pq,
    rec2020_hlg,
    rec2020,
};

// For premultiplied textures, RGB represents linear-light color multiplied by
// alpha, then encoded with the transfer function declared by ColorSpace.
enum class AlphaMode : std::uint8_t {
    unknown,
    opaque,
    straight,
    premultiplied,
};

struct TextureDescriptor {
    std::uint32_t width{};
    std::uint32_t height{};
    PixelFormat format{PixelFormat::unknown};
    ColorSpace color_space{ColorSpace::unknown};
    AlphaMode alpha_mode{AlphaMode::unknown};
};

// Backend implementations own the native GPU object for at least as long as
// this resource is referenced. For example, a Metal adapter retains its
// MTLTexture here. The core never maps or reads texture pixels on the CPU.
class TextureResource {
public:
    virtual ~TextureResource() = default;

    // These metadata values are immutable for a resource's lifetime.
    [[nodiscard]] virtual TextureDescriptor descriptor() const noexcept = 0;

    // Stable identifier for the backend implementation, such as
    // "org.example.framegen.metal". The returned view must remain valid for
    // the lifetime of the resource.
    [[nodiscard]] virtual std::string_view backend_id() const noexcept = 0;

    // Non-zero identifier unique to a backend device instance. Textures can
    // only be submitted together when both backend and device identifiers
    // match.
    [[nodiscard]] virtual std::uint64_t device_id() const noexcept = 0;

    // Opaque token for the underlying GPU storage identity. It must be
    // non-null and stable while this resource is alive. Distinct wrappers for
    // the same texture or aliased storage must return equal tokens. The default
    // treats each resource object as unique; native adapters should override
    // this when they can wrap one GPU texture more than once.
    [[nodiscard]] virtual const void* resource_identity() const noexcept {
        return this;
    }
};

// A reference-counted, type-erased GPU texture. A backend adapter can wrap a
// native texture by implementing TextureResource and passing the shared owner
// to from_resource(). No CPU image storage or pixel access is exposed here.
class Texture final {
public:
    Texture() = default;

    [[nodiscard]] static Result<Texture> from_resource(
        std::shared_ptr<TextureResource> resource);

    [[nodiscard]] bool valid() const noexcept { return static_cast<bool>(resource_); }
    [[nodiscard]] explicit operator bool() const noexcept { return valid(); }

    [[nodiscard]] TextureDescriptor descriptor() const noexcept;
    [[nodiscard]] std::string_view backend_id() const noexcept;
    [[nodiscard]] std::uint64_t device_id() const noexcept;
    [[nodiscard]] const void* resource_identity() const noexcept;

    // Exposes only the backend-neutral resource interface. Native access is
    // intentionally confined to the matching adapter implementation.
    [[nodiscard]] const std::shared_ptr<TextureResource>& resource() const noexcept {
        return resource_;
    }

private:
    explicit Texture(std::shared_ptr<TextureResource> resource)
        : resource_(std::move(resource)) {}

    std::shared_ptr<TextureResource> resource_;
};

} // namespace framegen
