#include "framegen/frame_generator.hpp"

#include <array>
#include <cmath>
#include <utility>

namespace framegen {
namespace {

Error error(ErrorCode code, const char* message) {
    return Error{code, message};
}

bool same_backend_device(const Texture& a, const Texture& b) {
    return a.backend_id() == b.backend_id() && a.device_id() == b.device_id();
}

bool compatible_frame_descriptions(const TextureDescriptor& a,
                                   const TextureDescriptor& b) {
    return a.width == b.width && a.height == b.height && a.format == b.format &&
           a.color_space == b.color_space && a.alpha_mode == b.alpha_mode;
}

} // namespace

Result<FrameGenerator> FrameGenerator::create(
    std::shared_ptr<FrameGenerationBackend> backend) {
    if (!backend) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "frame generation backend must not be null"));
    }
    if (backend->backend_id().empty()) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "backend identifier must not be empty"));
    }
    if (backend->device_id() == 0) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "backend device identifier must be non-zero"));
    }

    return FrameGenerator(std::move(backend));
}

Result<GeneratedFrame> FrameGenerator::submit(const FrameSubmission& submission) {
    const auto& previous = submission.previous.texture;
    const auto& current = submission.current.texture;
    if (!previous || !current) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "both input textures must be valid"));
    }
    if (!std::isfinite(submission.interpolation) || submission.interpolation < 0.0F ||
        submission.interpolation > 1.0F) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "interpolation must be finite and in [0, 1]"));
    }
    const auto& previous_timing = submission.previous.timing;
    const auto& current_timing = submission.current.timing;
    if (previous_timing.clock_domain == 0 || current_timing.clock_domain == 0 ||
        previous_timing.clock_domain != current_timing.clock_domain) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "input timestamps must share a non-zero clock domain"));
    }
    if (!submission.reset_history &&
        (current_timing.sequence <= previous_timing.sequence ||
         current_timing.timestamp_ns <= previous_timing.timestamp_ns)) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "input timing must increase unless history is reset"));
    }
    if (!same_backend_device(previous, current)) {
        return std::unexpected(error(ErrorCode::incompatible_resource,
                                     "input textures belong to different backend devices"));
    }
    if (previous.backend_id() != backend_->backend_id() ||
        previous.device_id() != backend_->device_id()) {
        return std::unexpected(error(ErrorCode::incompatible_resource,
                                     "input textures do not belong to this generator device"));
    }
    if (!compatible_frame_descriptions(previous.descriptor(), current.descriptor())) {
        return std::unexpected(error(ErrorCode::incompatible_resource,
                                     "input texture descriptions must match"));
    }

    const std::array<const Texture*, 10> optional_textures{
        &submission.previous.optional_inputs.motion_vectors,
        &submission.previous.optional_inputs.depth,
        &submission.previous.optional_inputs.ui_texture,
        &submission.previous.optional_inputs.ui_mask,
        &submission.previous.optional_inputs.reactive_mask,
        &submission.current.optional_inputs.motion_vectors,
        &submission.current.optional_inputs.depth,
        &submission.current.optional_inputs.ui_texture,
        &submission.current.optional_inputs.ui_mask,
        &submission.current.optional_inputs.reactive_mask,
    };
    for (const auto* texture : optional_textures) {
        if (*texture && (texture->backend_id() != backend_->backend_id() ||
                         texture->device_id() != backend_->device_id())) {
            return std::unexpected(error(
                ErrorCode::incompatible_resource,
                "optional input texture belongs to another backend device"));
        }
    }

    for (const auto& dependency : submission.gpu_dependencies) {
        if (!dependency) {
            return std::unexpected(error(ErrorCode::invalid_argument,
                                         "GPU dependencies must not contain null points"));
        }
        if (dependency->backend_id() != backend_->backend_id() ||
            dependency->device_id() != backend_->device_id()) {
            return std::unexpected(error(ErrorCode::incompatible_resource,
                                         "GPU dependency belongs to another backend device"));
        }
    }

    auto generated = backend_->submit(submission);
    if (!generated) {
        return std::unexpected(generated.error());
    }

    auto& frame = *generated;
    if (!frame.texture) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend returned an invalid output texture"));
    }
    if (frame.texture.backend_id() != backend_->backend_id() ||
        frame.texture.device_id() != backend_->device_id()) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend returned a texture from another device"));
    }
    if (!compatible_frame_descriptions(previous.descriptor(), frame.texture.descriptor())) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend output description does not match the input frames"));
    }
    if (frame.texture.resource_identity() == previous.resource_identity() ||
        frame.texture.resource_identity() == current.resource_identity()) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend output must not alias either input texture"));
    }
    if (!frame.completion) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend must return a GPU completion handle"));
    }
    if (frame.completion->backend_id() != backend_->backend_id() ||
        frame.completion->device_id() != backend_->device_id()) {
        return std::unexpected(error(ErrorCode::backend_failure,
                                     "backend returned a completion point from another device"));
    }

    return std::move(frame);
}

std::string_view FrameGenerator::backend_id() const noexcept {
    return backend_->backend_id();
}

std::uint64_t FrameGenerator::device_id() const noexcept {
    return backend_->device_id();
}

} // namespace framegen
