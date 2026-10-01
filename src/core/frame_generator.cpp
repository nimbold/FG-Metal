#include "framegen/frame_generator.hpp"

#include <array>
#include <cmath>
#include <cstdint>
#include <new>
#include <utility>

namespace framegen {
namespace {

Error error(ErrorCode code, const char* message) {
    return Error{code, message};
}

std::uint64_t timestamp_distance(std::int64_t lower,
                                 std::int64_t upper) noexcept {
    return static_cast<std::uint64_t>(upper) -
        static_cast<std::uint64_t>(lower);
}

bool same_backend_device(const Texture& a, const Texture& b) {
    return a.backend_id() == b.backend_id() && a.device_id() == b.device_id();
}

bool compatible_frame_descriptions(const TextureDescriptor& a,
                                   const TextureDescriptor& b) {
    return a.width == b.width && a.height == b.height && a.format == b.format &&
           a.color_space == b.color_space && a.alpha_mode == b.alpha_mode;
}

bool valid_hud_options(const HudOptions& options) {
    const bool valid_mode = options.mode == HudMode::no_hud_knowledge ||
        options.mode == HudMode::explicit_ui_plane ||
        options.mode == HudMode::automatic_protection;
    const bool valid_source = options.ui_source == UiTemporalSource::previous_frame ||
        options.ui_source == UiTemporalSource::current_frame ||
        options.ui_source == UiTemporalSource::nearest_presentation;
    const bool valid_debug =
        options.debug_visualization == HudDebugVisualization::disabled ||
        options.debug_visualization == HudDebugVisualization::raw_mask ||
        options.debug_visualization == HudDebugVisualization::stabilized_mask ||
        options.debug_visualization == HudDebugVisualization::protected_regions ||
        options.debug_visualization == HudDebugVisualization::interpolation_confidence ||
        options.debug_visualization == HudDebugVisualization::final_composite;
    return valid_mode && valid_source && valid_debug &&
        (options.mode != HudMode::no_hud_knowledge ||
         options.debug_visualization == HudDebugVisualization::disabled ||
         options.debug_visualization == HudDebugVisualization::final_composite);
}

bool valid_temporal_quality_options(const TemporalQualityOptions& options) {
    const bool valid_policy =
        options.policy == TemporalQualityPolicy::disabled ||
        options.policy == TemporalQualityPolicy::continuous_endpoint_blend ||
        options.policy == TemporalQualityPolicy::continuous_source_blend ||
        options.policy == TemporalQualityPolicy::nearest_endpoint_fallback;
    const bool valid_debug =
        options.debug_visualization == TemporalQualityDebugVisualization::disabled ||
        options.debug_visualization == TemporalQualityDebugVisualization::confidence ||
        options.debug_visualization == TemporalQualityDebugVisualization::disocclusion ||
        options.debug_visualization == TemporalQualityDebugVisualization::unstable_thin_features ||
        options.debug_visualization == TemporalQualityDebugVisualization::high_frequency_texture ||
        options.debug_visualization == TemporalQualityDebugVisualization::specular_or_particles ||
        options.debug_visualization == TemporalQualityDebugVisualization::scene_cut ||
        options.debug_visualization == TemporalQualityDebugVisualization::confidence_classes;
    return valid_policy && valid_debug;
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

    try {
        auto stream_state = backend->create_stream_state();
        return FrameGenerator(std::move(backend), std::move(stream_state));
    } catch (const std::bad_alloc&) {
        return std::unexpected(error(ErrorCode::allocation_failure,
                                     "backend stream state allocation failed"));
    }
}

FrameGenerator::FrameGenerator(const FrameGenerator& other)
    : backend_(other.backend_),
      stream_state_(backend_ ? backend_->create_stream_state() : nullptr) {}

FrameGenerator& FrameGenerator::operator=(const FrameGenerator& other) {
    if (this == &other) {
        return *this;
    }
    auto stream_state = other.backend_
        ? other.backend_->create_stream_state()
        : std::shared_ptr<BackendStreamState>{};
    backend_ = other.backend_;
    stream_state_ = std::move(stream_state);
    return *this;
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
    if (!valid_hud_options(submission.hud_options)) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "HUD mode, UI source, or debug visualization is invalid"));
    }
    if (!valid_temporal_quality_options(submission.temporal_quality)) {
        return std::unexpected(error(
            ErrorCode::invalid_argument,
            "temporal quality policy or debug visualization is invalid"));
    }
    const auto& previous_timing = submission.previous.timing;
    const auto& current_timing = submission.current.timing;
    if (previous_timing.clock_domain == 0 || current_timing.clock_domain == 0 ||
        previous_timing.clock_domain != current_timing.clock_domain) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "input timestamps must share a non-zero clock domain"));
    }
    if (current_timing.sequence <= previous_timing.sequence ||
        current_timing.timestamp_ns == unknown_timestamp_ns ||
        previous_timing.timestamp_ns == unknown_timestamp_ns ||
        current_timing.timestamp_ns <= previous_timing.timestamp_ns) {
        return std::unexpected(error(ErrorCode::invalid_argument,
                                     "source sequence and timestamps must be known and increase"));
    }
    if (submission.interpolation_timestamp_ns != unknown_timestamp_ns) {
        if (submission.interpolation_timestamp_ns <= previous_timing.timestamp_ns ||
            submission.interpolation_timestamp_ns >= current_timing.timestamp_ns) {
            return std::unexpected(error(
                ErrorCode::invalid_argument,
                "interpolation timestamp must be strictly between the source timestamps"));
        }
        const auto interval = timestamp_distance(
            previous_timing.timestamp_ns, current_timing.timestamp_ns);
        const auto offset = timestamp_distance(
            previous_timing.timestamp_ns, submission.interpolation_timestamp_ns);
        const long double timestamp_fraction = static_cast<long double>(offset) /
            static_cast<long double>(interval);
        if (std::abs(timestamp_fraction -
                     static_cast<long double>(submission.interpolation)) > 1.0e-6L) {
            return std::unexpected(error(
                ErrorCode::invalid_argument,
                "interpolation fraction must match the supplied interpolation timestamp"));
        }
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

    if (submission.hud_options.mode == HudMode::explicit_ui_plane) {
        const auto& previous_ui = submission.previous.optional_inputs.ui_texture;
        const auto& current_ui = submission.current.optional_inputs.ui_texture;
        if (!previous_ui || !current_ui) {
            return std::unexpected(error(
                ErrorCode::invalid_argument,
                "explicit UI-plane mode requires a UI texture on both source frames"));
        }
        const auto scene_desc = previous.descriptor();
        const auto previous_ui_desc = previous_ui.descriptor();
        const auto current_ui_desc = current_ui.descriptor();
        if (previous_ui_desc.width != scene_desc.width ||
            previous_ui_desc.height != scene_desc.height ||
            current_ui_desc.width != scene_desc.width ||
            current_ui_desc.height != scene_desc.height ||
            previous_ui_desc.format != current_ui_desc.format ||
            previous_ui_desc.color_space != current_ui_desc.color_space ||
            previous_ui_desc.alpha_mode != current_ui_desc.alpha_mode) {
            return std::unexpected(error(
                ErrorCode::incompatible_resource,
                "explicit UI planes must match scene dimensions and each other"));
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

    FrameSubmission backend_submission;
    try {
        backend_submission = submission;
    } catch (const std::bad_alloc&) {
        return std::unexpected(error(ErrorCode::allocation_failure,
                                     "backend submission copy allocation failed"));
    }
    backend_submission.backend_stream_state = stream_state_;
    auto generated = backend_->submit(backend_submission);
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
