#import <Metal/Metal.h>

#include "framegen/metal_api.h"
#include "framegen/metal.hpp"
#include "metal_backend.hpp"
#include "../core/backend_registry.hpp"

#include <algorithm>
#include <cstring>
#include <memory>
#include <string>
#include <utility>

namespace framegen::metal {
namespace {

constexpr const char* kBackendId = "org.framegen.metal";

Error make_error(ErrorCode code, const char* message) {
    return Error{code, message};
}

ColorSpace c_color_space(const framegen_image_t& image) {
    switch (image.color_space) {
    case FRAMEGEN_COLOR_SPACE_SRGB:
        return image.transfer_function == FRAMEGEN_TRANSFER_LINEAR
            ? ColorSpace::linear_srgb : ColorSpace::srgb;
    case FRAMEGEN_COLOR_SPACE_DISPLAY_P3: return ColorSpace::display_p3;
    case FRAMEGEN_COLOR_SPACE_REC2020:
        if (image.transfer_function == FRAMEGEN_TRANSFER_PQ) {
            return ColorSpace::rec2020_pq;
        }
        if (image.transfer_function == FRAMEGEN_TRANSFER_HLG) {
            return ColorSpace::rec2020_hlg;
        }
        return ColorSpace::rec2020;
    default: return ColorSpace::unknown;
    }
}

AlphaMode c_alpha_mode(const framegen_image_t& image) {
    switch (image.alpha_mode) {
    case FRAMEGEN_ALPHA_OPAQUE: return AlphaMode::opaque;
    case FRAMEGEN_ALPHA_STRAIGHT: return AlphaMode::straight;
    case FRAMEGEN_ALPHA_PREMULTIPLIED: return AlphaMode::premultiplied;
    default: return AlphaMode::unknown;
    }
}

std::uint32_t c_pixel_format(PixelFormat format) {
    switch (format) {
    case PixelFormat::r8_unorm: return FRAMEGEN_PIXEL_FORMAT_R8_UNORM;
    case PixelFormat::rg8_unorm: return FRAMEGEN_PIXEL_FORMAT_RG8_UNORM;
    case PixelFormat::rgba8_unorm: return FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM;
    case PixelFormat::rgba8_unorm_srgb: return FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM_SRGB;
    case PixelFormat::bgra8_unorm: return FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM;
    case PixelFormat::bgra8_unorm_srgb: return FRAMEGEN_PIXEL_FORMAT_BGRA8_UNORM_SRGB;
    case PixelFormat::rgb10a2_unorm: return FRAMEGEN_PIXEL_FORMAT_RGB10A2_UNORM;
    case PixelFormat::bgr10a2_unorm: return FRAMEGEN_PIXEL_FORMAT_BGR10A2_UNORM;
    case PixelFormat::rg11b10_float: return FRAMEGEN_PIXEL_FORMAT_RG11B10_FLOAT;
    case PixelFormat::rgba16_float: return FRAMEGEN_PIXEL_FORMAT_RGBA16_FLOAT;
    case PixelFormat::rgba32_float: return FRAMEGEN_PIXEL_FORMAT_RGBA32_FLOAT;
    case PixelFormat::r16_float: return FRAMEGEN_PIXEL_FORMAT_R16_FLOAT;
    case PixelFormat::rg16_float: return FRAMEGEN_PIXEL_FORMAT_RG16_FLOAT;
    case PixelFormat::rg32_float: return FRAMEGEN_PIXEL_FORMAT_RG32_FLOAT;
    case PixelFormat::r32_float: return FRAMEGEN_PIXEL_FORMAT_R32_FLOAT;
    case PixelFormat::d16_unorm: return FRAMEGEN_PIXEL_FORMAT_D16_UNORM;
    case PixelFormat::d32_float: return FRAMEGEN_PIXEL_FORMAT_D32_FLOAT;
    case PixelFormat::d24_unorm_s8_uint: return FRAMEGEN_PIXEL_FORMAT_D24_UNORM_S8_UINT;
    case PixelFormat::unknown: return FRAMEGEN_PIXEL_FORMAT_UNKNOWN;
    }
    return FRAMEGEN_PIXEL_FORMAT_UNKNOWN;
}

std::uint32_t c_alpha_mode(AlphaMode mode) {
    switch (mode) {
    case AlphaMode::opaque: return FRAMEGEN_ALPHA_OPAQUE;
    case AlphaMode::straight: return FRAMEGEN_ALPHA_STRAIGHT;
    case AlphaMode::premultiplied: return FRAMEGEN_ALPHA_PREMULTIPLIED;
    case AlphaMode::unknown: return FRAMEGEN_ALPHA_UNKNOWN;
    }
    return FRAMEGEN_ALPHA_UNKNOWN;
}

class MetalBackendProvider final : public ::framegen::detail::BackendProvider {
public:
    [[nodiscard]] framegen_backend_info_t info() const override {
        framegen_backend_info_t result{};
        result.struct_size = sizeof(result);
        result.struct_version = FRAMEGEN_ABI_VERSION;
        std::strncpy(result.backend_id, kBackendId, sizeof(result.backend_id) - 1);
        result.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
        result.available = detail::rife_runtime_available() ? 1U : 0U;
        result.supported_capabilities = FRAMEGEN_CAP_COLOR_ONLY |
            FRAMEGEN_CAP_UI_PLANE | FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION |
            FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION |
            FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME;
        result.required_capabilities = FRAMEGEN_CAP_COLOR_ONLY;
        result.required_input_capabilities = 0;
        result.supported_pixel_formats =
            FRAMEGEN_PIXEL_FORMAT_BIT(FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM);
        result.max_width = 16384;
        result.max_height = 16384;
        result.max_in_flight_requests = 3;
        return result;
    }

    [[nodiscard]] Result<::framegen::detail::BackendInstance> create(
        const framegen_device_ref_t& device) const override {
        id<MTLDevice> native_device = (__bridge id<MTLDevice>)device.native_handle;
        auto adapter_result = Device::create(native_device);
        if (!adapter_result) {
            return std::unexpected(adapter_result.error());
        }
        auto adapter = std::make_shared<Device>(std::move(*adapter_result));
        ::framegen::detail::BackendInstance instance;
        instance.backend = adapter->backend();
        instance.import_image = [adapter](const framegen_image_t& image) {
            if (image.resource.backend_type != FRAMEGEN_GPU_BACKEND_METAL ||
                image.resource.device_id != adapter->device_id()) {
                return Result<Texture>{std::unexpected(make_error(
                    ErrorCode::incompatible_resource,
                    "image belongs to a different Metal device"))};
            }
            id<MTLTexture> native_texture =
                (__bridge id<MTLTexture>)image.resource.native_handle;
            const void* resource_identity = image.resource.resource_identity == 0
                ? nullptr
                : reinterpret_cast<const void*>(static_cast<std::uintptr_t>(
                      image.resource.resource_identity));
            return adapter->wrap_texture(native_texture, c_color_space(image),
                                         c_alpha_mode(image), resource_identity);
        };
        instance.import_sync = [adapter](const framegen_sync_point_t& sync) {
            if (sync.backend_type != FRAMEGEN_GPU_BACKEND_METAL ||
                sync.device_id != adapter->device_id() ||
                sync.sync_type != FRAMEGEN_SYNC_METAL_SHARED_EVENT ||
                sync.native_handle == nullptr || sync.value == 0) {
                return Result<std::shared_ptr<const GpuSyncPoint>>{
                    std::unexpected(make_error(ErrorCode::invalid_argument,
                        "synchronization point is not a valid Metal shared event"))};
            }
            EventPoint point{
                .event = (__bridge id<MTLSharedEvent>)sync.native_handle,
                .value = sync.value,
            };
            return adapter->make_event_dependency(point);
        };
        instance.export_image = [adapter](const Texture& texture)
            -> Result<framegen_image_t> {
            id<MTLTexture> native_texture = adapter->native_texture(texture);
            if (native_texture == nil) {
                return std::unexpected(make_error(ErrorCode::backend_failure,
                    "Metal backend returned a texture from another device"));
            }
            const auto descriptor = texture.descriptor();
            framegen_image_t output{};
            output.struct_size = sizeof(output);
            output.struct_version = FRAMEGEN_ABI_VERSION;
            output.resource.struct_size = sizeof(output.resource);
            output.resource.struct_version = FRAMEGEN_ABI_VERSION;
            output.resource.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
            output.resource.resource_type = FRAMEGEN_RESOURCE_TEXTURE_2D;
            output.resource.device_id = adapter->device_id();
            output.resource.resource_identity = static_cast<std::uint64_t>(
                reinterpret_cast<std::uintptr_t>(texture.resource_identity()));
            output.resource.native_handle = (__bridge void*)native_texture;
            output.width = descriptor.width;
            output.height = descriptor.height;
            output.pixel_format = c_pixel_format(descriptor.format);
            output.color_space = FRAMEGEN_COLOR_SPACE_SRGB;
            output.transfer_function = descriptor.color_space == ColorSpace::linear_srgb
                ? FRAMEGEN_TRANSFER_LINEAR : FRAMEGEN_TRANSFER_SRGB;
            output.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
            output.alpha_mode = c_alpha_mode(descriptor.alpha_mode);
            return output;
        };
        instance.export_sync = [adapter](const GpuSyncPoint& sync)
            -> Result<framegen_sync_point_t> {
            auto point = adapter->event_point(sync);
            if (!point) {
                return std::unexpected(point.error());
            }
            framegen_sync_point_t output{};
            output.struct_size = sizeof(output);
            output.struct_version = FRAMEGEN_ABI_VERSION;
            output.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
            output.sync_type = FRAMEGEN_SYNC_METAL_SHARED_EVENT;
            output.device_id = adapter->device_id();
            output.value = point->value;
            output.native_handle = (__bridge void*)point->event;
            return output;
        };
        instance.invalidate = [](framegen_invalidation_reason_t) {
            // The core marks the next submission reset_history after invalidation.
        };
        instance.notify_presentation = [](const framegen_presentation_event_t&) {
            // Each generated frame owns a distinct output texture.
        };
        return instance;
    }
};

} // namespace
} // namespace framegen::metal

extern "C" framegen_status_t framegen_metal_register_backend(void) {
    try {
        ::framegen::detail::register_backend_provider(
            std::make_shared<framegen::metal::MetalBackendProvider>());
        return FRAMEGEN_STATUS_OK;
    } catch (...) {
        return FRAMEGEN_STATUS_INTERNAL_ERROR;
    }
}
