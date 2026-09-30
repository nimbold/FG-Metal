#include "framegen/texture.hpp"

#include <utility>

namespace framegen {

Result<Texture> Texture::from_resource(std::shared_ptr<TextureResource> resource) {
    if (!resource) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture resource must not be null"});
    }

    const auto description = resource->descriptor();
    if (description.width == 0 || description.height == 0) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture dimensions must be non-zero"});
    }
    if (description.format == PixelFormat::unknown) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture pixel format must be specified"});
    }
    if (resource->backend_id().empty()) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture backend identifier must not be empty"});
    }
    if (resource->device_id() == 0) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture device identifier must be non-zero"});
    }
    if (resource->resource_identity() == nullptr) {
        return std::unexpected(Error{ErrorCode::invalid_argument,
                                     "texture resource identity must not be null"});
    }

    return Texture(std::move(resource));
}

TextureDescriptor Texture::descriptor() const noexcept {
    return resource_ ? resource_->descriptor() : TextureDescriptor{};
}

std::string_view Texture::backend_id() const noexcept {
    return resource_ ? resource_->backend_id() : std::string_view{};
}

std::uint64_t Texture::device_id() const noexcept {
    return resource_ ? resource_->device_id() : 0;
}

const void* Texture::resource_identity() const noexcept {
    return resource_ ? resource_->resource_identity() : nullptr;
}

} // namespace framegen
