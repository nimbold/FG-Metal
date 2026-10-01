#pragma once

#ifndef __OBJC__
#error "The Metal backend implementation must be compiled as Objective-C++"
#endif

#import <Metal/Metal.h>

#include "framegen/backend.hpp"
#include "../quality/temporal_quality_controller.hpp"

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string_view>
#include <array>

namespace framegen::metal::detail {

class MetalRifeModel;
enum class RifeMode : unsigned char;
[[nodiscard]] std::filesystem::path rife_model_weights_path();
[[nodiscard]] bool rife_runtime_available() noexcept;

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

class MetalBackendStreamState final : public BackendStreamState {
public:
    std::uint64_t device_id{};
    std::shared_ptr<const std::uint8_t> backend_identity;
    std::mutex automatic_history_mutex;
    std::array<std::shared_ptr<MetalTextureResource>, 3> automatic_mask_history;
    std::uint32_t automatic_mask_width{};
    std::uint32_t automatic_mask_height{};
    TextureDescriptor automatic_mask_pair_descriptor{};
    FrameColorMetadata automatic_mask_pair_previous_color_metadata{};
    FrameColorMetadata automatic_mask_pair_current_color_metadata{};
    std::uint32_t automatic_mask_history_index{};
    bool automatic_mask_history_valid{};
    bool automatic_mask_pair_has_prior_history{};
    std::uint64_t automatic_mask_pair_clock_domain{};
    std::uint64_t automatic_mask_pair_previous_sequence{};
    std::uint64_t automatic_mask_pair_current_sequence{};
    std::int64_t automatic_mask_pair_previous_timestamp_ns{unknown_timestamp_ns};
    std::int64_t automatic_mask_pair_current_timestamp_ns{unknown_timestamp_ns};
    float automatic_mask_pair_engagement_alpha{1.0F};
    float automatic_mask_pair_release_alpha{1.0F};
    HudMode last_hud_mode{HudMode::no_hud_knowledge};

    quality::TemporalQualityController temporal_controller;
    std::array<std::shared_ptr<MetalTextureResource>, 2> temporal_history;
    TextureDescriptor temporal_history_descriptor{};
    FrameColorMetadata temporal_history_color_metadata{};
    std::uint32_t temporal_history_width{};
    std::uint32_t temporal_history_height{};
    std::uint32_t temporal_history_index{};
    __strong id<MTLBuffer> temporal_history_valid_buffer;
    __strong id<MTLBuffer> temporal_cut_tile_buffer;
    __strong id<MTLBuffer> temporal_cut_summary_buffer;
    std::uint32_t temporal_cut_group_count_x{};
    std::uint32_t temporal_cut_group_count_y{};
    std::atomic<bool> temporal_async_reset_requested{};
};

class MetalBackend final : public FrameGenerationBackend {
public:
    MetalBackend(id<MTLDevice> device, id<MTLCommandQueue> queue,
                 id<MTLComputePipelineState> explicit_ui_pipeline,
                 id<MTLComputePipelineState> automatic_hud_pipeline,
                 id<MTLComputePipelineState> temporal_cut_tiles_pipeline,
                 id<MTLComputePipelineState> temporal_cut_reduce_pipeline,
                 id<MTLComputePipelineState> temporal_quality_pipeline,
                 id<MTLComputePipelineState> temporal_history_commit_pipeline,
                 id<MTLSharedEvent> completion_event,
                 std::shared_ptr<MetalRifeModel> rife_model,
                 RifeMode rife_mode,
                 std::uint64_t device_id);

    [[nodiscard]] std::string_view backend_id() const noexcept override;
    [[nodiscard]] std::uint64_t device_id() const noexcept override;
    [[nodiscard]] id<MTLDevice> native_device() const noexcept;
    [[nodiscard]] std::shared_ptr<BackendStreamState>
    create_stream_state() override;

    [[nodiscard]] Result<GeneratedFrame> submit(
        const FrameSubmission& submission) override;

private:
    __strong id<MTLDevice> device_;
    __strong id<MTLCommandQueue> queue_;
    __strong id<MTLComputePipelineState> explicit_ui_pipeline_;
    __strong id<MTLComputePipelineState> automatic_hud_pipeline_;
    __strong id<MTLComputePipelineState> temporal_cut_tiles_pipeline_;
    __strong id<MTLComputePipelineState> temporal_cut_reduce_pipeline_;
    __strong id<MTLComputePipelineState> temporal_quality_pipeline_;
    __strong id<MTLComputePipelineState> temporal_history_commit_pipeline_;
    __strong id<MTLSharedEvent> completion_event_;
    std::shared_ptr<MetalRifeModel> rife_model_;
    RifeMode rife_mode_;
    std::uint64_t device_id_;
    std::shared_ptr<const std::uint8_t> backend_identity_;
    std::uint64_t next_event_value_{1};
    std::mutex submission_mutex_;
};

} // namespace framegen::metal::detail
