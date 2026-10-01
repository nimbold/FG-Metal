#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include "framegen/api.h"
#include "framegen/frame_generator.hpp"
#include "framegen/metal.hpp"
#include "framegen/metal_api.h"

#include <chrono>
#include <algorithm>
#include <array>
#include <cstdint>
#include <iostream>
#include <thread>
#include <utility>
#include <vector>

namespace {

template <typename T>
void init(T& value) {
    value = {};
    value.struct_size = sizeof(T);
    value.struct_version = FRAMEGEN_ABI_VERSION;
}

bool check(framegen_status_t status, const char* operation) {
    if (status == FRAMEGEN_STATUS_OK) {
        return true;
    }
    char message[512]{};
    std::uint32_t required{};
    (void)framegen_get_last_error(sizeof(message), message, &required);
    std::cerr << operation << " failed (" << status << "): " << message << '\n';
    return false;
}

bool run_contract_smoke() {
    if (!check(framegen_metal_register_backend(), "register Metal backend")) {
        return false;
    }
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    if (device == nil) {
        std::cerr << "Metal device is unavailable\n";
        return false;
    }

    framegen_backend_info_t backend_info{};
    init(backend_info);
    if (!check(framegen_query_backend("org.framegen.metal", &backend_info),
               "query Metal backend")) {
        return false;
    }
    if ((backend_info.supported_capabilities &
         (FRAMEGEN_CAP_COLOR_ONLY | FRAMEGEN_CAP_UI_PLANE |
          FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION |
          FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION |
          FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME)) !=
        (FRAMEGEN_CAP_COLOR_ONLY | FRAMEGEN_CAP_UI_PLANE |
         FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION |
         FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION |
         FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME)) {
        std::cerr << "Metal placeholder did not advertise HUD preservation capabilities\n";
        return false;
    }

    framegen_context_desc_t desc{};
    init(desc);
    desc.backend_id = "org.framegen.metal";
    init(desc.device);
    desc.device.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
    desc.device.resource_type = FRAMEGEN_RESOURCE_DEVICE;
    desc.device.native_handle = (__bridge void*)device;
    framegen_context_t* context{};
    if (!check(framegen_context_create(&desc, &context), "create Metal context")) {
        return false;
    }

    framegen_context_config_t config{};
    init(config);
    config.preferred_capabilities = FRAMEGEN_CAP_COLOR_ONLY |
                                    FRAMEGEN_CAP_UI_PLANE |
                                    FRAMEGEN_CAP_AUTOMATIC_HUD_PROTECTION |
                                    FRAMEGEN_CAP_HUD_DEBUG_VISUALIZATION |
                                    FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME;
    config.available_input_capabilities = FRAMEGEN_INPUT_UI_PLANE;
    config.history_capacity = 4;
    framegen_capability_result_t negotiated{};
    init(negotiated);
    if (!check(framegen_context_configure(context, &config, &negotiated),
               "configure Metal context")) {
        framegen_context_destroy(context);
        return false;
    }

    framegen_hud_options_t hud_options{};
    init(hud_options);
    hud_options.mode = FRAMEGEN_HUD_MODE_AUTOMATIC_PROTECTION;
    hud_options.ui_temporal_source = FRAMEGEN_UI_SOURCE_NEAREST_PRESENTATION;
    if (!check(framegen_context_set_hud_options(context, &hud_options),
               "select automatic HUD protection")) {
        framegen_context_destroy(context);
        return false;
    }
    hud_options.mode = FRAMEGEN_HUD_MODE_EXPLICIT_UI_PLANE;
    hud_options.ui_temporal_source = FRAMEGEN_UI_SOURCE_NEAREST_PRESENTATION;
    if (!check(framegen_context_set_hud_options(context, &hud_options),
               "select explicit UI plane")) {
        framegen_context_destroy(context);
        return false;
    }

    framegen_context_info_t context_info{};
    init(context_info);
    if (!check(framegen_context_get_info(context, &context_info),
               "query Metal context")) {
        framegen_context_destroy(context);
        return false;
    }

    MTLTextureDescriptor* texture_desc =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:64 height:32 mipmapped:NO];
    texture_desc.storageMode = MTLStorageModeShared;
    texture_desc.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> first_texture = [device newTextureWithDescriptor:texture_desc];
    id<MTLTexture> second_texture = [device newTextureWithDescriptor:texture_desc];
    id<MTLTexture> first_ui_texture = [device newTextureWithDescriptor:texture_desc];
    id<MTLTexture> second_ui_texture = [device newTextureWithDescriptor:texture_desc];
    if (first_texture == nil || second_texture == nil ||
        first_ui_texture == nil || second_ui_texture == nil) {
        std::cerr << "could not allocate Metal input or UI textures\n";
        framegen_context_destroy(context);
        return false;
    }
    std::vector<std::uint8_t> pixels(64 * 32 * 4);
    for (std::size_t pixel = 0; pixel < 64 * 32; ++pixel) {
        pixels[pixel * 4] = 0;
        pixels[pixel * 4 + 1] = 0;
        pixels[pixel * 4 + 2] = 255;
        pixels[pixel * 4 + 3] = 0;
    }
    const MTLRegion region = MTLRegionMake2D(0, 0, 64, 32);
    [first_texture replaceRegion:region mipmapLevel:0 withBytes:pixels.data()
                     bytesPerRow:64 * 4];
    for (std::size_t pixel = 0; pixel < 64 * 32; ++pixel) {
        pixels[pixel * 4] = 255;
        pixels[pixel * 4 + 1] = 0;
        pixels[pixel * 4 + 2] = 0;
        pixels[pixel * 4 + 3] = 0;
    }
    [second_texture replaceRegion:region mipmapLevel:0 withBytes:pixels.data()
                      bytesPerRow:64 * 4];
    for (std::size_t pixel = 0; pixel < 64 * 32; ++pixel) {
        pixels[pixel * 4] = 0;
        pixels[pixel * 4 + 1] = 255;
        pixels[pixel * 4 + 2] = 0;
        pixels[pixel * 4 + 3] = 128;
    }
    [first_ui_texture replaceRegion:region mipmapLevel:0 withBytes:pixels.data()
                        bytesPerRow:64 * 4];
    for (std::size_t pixel = 0; pixel < 64 * 32; ++pixel) {
        pixels[pixel * 4] = 0;
        pixels[pixel * 4 + 1] = 0;
        pixels[pixel * 4 + 2] = 255;
        pixels[pixel * 4 + 3] = 128;
    }
    [second_ui_texture replaceRegion:region mipmapLevel:0 withBytes:pixels.data()
                         bytesPerRow:64 * 4];

    // Use epoch-scale nanoseconds and put the presentation target one ns
    // before a nearest-UI tie. Floating-point subtraction at this magnitude
    // can erase that distinction on platforms where long double is double.
    using Clock = std::chrono::steady_clock;
    constexpr std::int64_t base_ns = 1'700'000'000'000'000'000LL;
    constexpr std::int64_t kFrameInterval = 16'000'000;
    auto make_frame = [&](std::uint64_t frame_id, id<MTLTexture> texture,
                          id<MTLTexture> ui_texture,
                          std::int64_t timestamp) {
        framegen_source_frame_t frame{};
        init(frame);
        frame.optional_inputs.struct_size = sizeof(frame.optional_inputs);
        frame.optional_inputs.struct_version = FRAMEGEN_ABI_VERSION;
        frame.stream_id = 1;
        frame.source_frame_id = frame_id;
        init(frame.color);
        init(frame.color.resource);
        frame.color.resource.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
        frame.color.resource.resource_type = FRAMEGEN_RESOURCE_TEXTURE_2D;
        frame.color.resource.device_id = context_info.device_id;
        frame.color.resource.resource_identity = static_cast<std::uint64_t>(
            reinterpret_cast<std::uintptr_t>((__bridge void*)texture));
        frame.color.resource.native_handle = (__bridge void*)texture;
        frame.color.width = 64;
        frame.color.height = 32;
        frame.color.pixel_format = FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM;
        frame.color.color_space = FRAMEGEN_COLOR_SPACE_SRGB;
        frame.color.transfer_function = FRAMEGEN_TRANSFER_SRGB;
        frame.color.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
        frame.color.alpha_mode = FRAMEGEN_ALPHA_OPAQUE;
        frame.optional_inputs.present_mask = FRAMEGEN_INPUT_UI_PLANE;
        init(frame.optional_inputs.ui_texture);
        init(frame.optional_inputs.ui_texture.resource);
        frame.optional_inputs.ui_texture.resource.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
        frame.optional_inputs.ui_texture.resource.resource_type = FRAMEGEN_RESOURCE_TEXTURE_2D;
        frame.optional_inputs.ui_texture.resource.device_id = context_info.device_id;
        frame.optional_inputs.ui_texture.resource.resource_identity =
            static_cast<std::uint64_t>(reinterpret_cast<std::uintptr_t>(
                (__bridge void*)ui_texture));
        frame.optional_inputs.ui_texture.resource.native_handle = (__bridge void*)ui_texture;
        frame.optional_inputs.ui_texture.width = 64;
        frame.optional_inputs.ui_texture.height = 32;
        frame.optional_inputs.ui_texture.pixel_format = FRAMEGEN_PIXEL_FORMAT_RGBA8_UNORM;
        frame.optional_inputs.ui_texture.color_space = FRAMEGEN_COLOR_SPACE_SRGB;
        frame.optional_inputs.ui_texture.transfer_function = FRAMEGEN_TRANSFER_SRGB;
        frame.optional_inputs.ui_texture.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
        frame.optional_inputs.ui_texture.alpha_mode = FRAMEGEN_ALPHA_STRAIGHT;
        frame.clock_domain = 1;
        frame.source_timestamp_ns = timestamp;
        frame.source_duration_ns = kFrameInterval;
        frame.render_completion_timestamp_ns = timestamp + 100'000;
        frame.desired_presentation_timestamp_ns = timestamp + kFrameInterval;
        init(frame.render_completion);
        frame.render_completion.backend_type = FRAMEGEN_GPU_BACKEND_METAL;
        frame.render_completion.device_id = context_info.device_id;
        frame.render_completion.sync_type = FRAMEGEN_SYNC_NONE_READY;
        return frame;
    };

    auto aliased_ui_frame = make_frame(1, first_texture, first_texture, base_ns);
    aliased_ui_frame.optional_inputs.ui_texture.resource.resource_identity ^= 1U;
    framegen_frame_receipt_t aliased_receipt{};
    init(aliased_receipt);
    if (!check(framegen_submit_source_frame(context, &aliased_ui_frame, &aliased_receipt),
               "submit aliased UI test frame A")) {
        framegen_context_destroy(context);
        return false;
    }
    auto aliased_ui_frame_b = make_frame(
        2, first_texture, first_texture, base_ns + kFrameInterval);
    aliased_ui_frame_b.optional_inputs.ui_texture.resource.resource_identity ^= 1U;
    framegen_frame_receipt_t aliased_receipt_b{};
    init(aliased_receipt_b);
    if (!check(framegen_submit_source_frame(context, &aliased_ui_frame_b,
                                            &aliased_receipt_b),
               "submit aliased UI test frame B")) {
        framegen_context_destroy(context);
        return false;
    }
    framegen_interpolation_request_t aliased_request{};
    init(aliased_request);
    aliased_request.previous_source_frame_id = 1;
    aliased_request.current_source_frame_id = 2;
    aliased_request.history_generation = aliased_receipt_b.history_generation;
    aliased_request.clock_domain = 1;
    aliased_request.interpolation_timestamp_ns = base_ns + kFrameInterval / 2;
    aliased_request.desired_presentation_timestamp_ns =
        base_ns + kFrameInterval / 2;
    aliased_request.presentation_deadline_ns = base_ns + kFrameInterval * 100;
    framegen_ticket_t* aliased_ticket{};
    const auto alias_status = framegen_request_interpolation(
        context, &aliased_request, &aliased_ticket);
    if (alias_status != FRAMEGEN_STATUS_INVALID_FRAME || aliased_ticket != nullptr) {
        std::cerr << "explicit UI plane did not reject a native Metal texture alias "
                     "with a forged resource identity (status " << alias_status << ")\n";
        if (aliased_ticket != nullptr) framegen_ticket_release(aliased_ticket);
        framegen_context_destroy(context);
        return false;
    }

    auto first = make_frame(3, first_texture, first_ui_texture,
                            base_ns + kFrameInterval * 2);
    auto second = make_frame(4, second_texture, second_ui_texture,
                             base_ns + kFrameInterval * 3);
    framegen_frame_receipt_t first_receipt{};
    framegen_frame_receipt_t second_receipt{};
    init(first_receipt);
    init(second_receipt);
    bool success = check(framegen_submit_source_frame(context, &first, &first_receipt),
                         "submit first Metal frame") &&
        check(framegen_submit_source_frame(context, &second, &second_receipt),
              "submit second Metal frame");
    if (!success) {
        framegen_context_destroy(context);
        return false;
    }

    framegen_interpolation_request_t request{};
    init(request);
    request.previous_source_frame_id = 3;
    request.current_source_frame_id = 4;
    request.history_generation = second_receipt.history_generation;
    request.clock_domain = 1;
    request.interpolation_timestamp_ns = base_ns + kFrameInterval * 2 + kFrameInterval / 2;
    // The renderer presentation target is later than the sample midpoint, but
    // still nearer the previous endpoint's presentation time. This exercises
    // the intentional separation between interpolation and presentation time.
    request.desired_presentation_timestamp_ns = base_ns + kFrameInterval * 3 +
        kFrameInterval / 2 - 1;
    request.presentation_deadline_ns = base_ns + kFrameInterval * 100;
    framegen_ticket_t* ticket{};
    success = check(framegen_request_interpolation(context, &request, &ticket),
                    "request Metal interpolation");
    if (!success) {
        framegen_context_destroy(context);
        return false;
    }

    framegen_ticket_result_t result{};
    init(result);
    const auto deadline = Clock::now() + std::chrono::seconds(3);
    do {
        const auto now_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
            Clock::now().time_since_epoch()).count();
        if (!check(framegen_ticket_poll(ticket, now_ns, &result), "poll Metal ticket")) {
            success = false;
            break;
        }
        if (result.status == FRAMEGEN_TICKET_READY) {
            break;
        }
        if (result.status != FRAMEGEN_TICKET_PENDING) {
            std::cerr << "Metal interpolation ticket ended with status "
                      << result.status << '\n';
            success = false;
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    } while (Clock::now() < deadline);

    if (result.status != FRAMEGEN_TICKET_READY) {
        std::cerr << "Metal interpolation did not complete within the bounded smoke-test window\n";
        success = false;
    } else {
        id<MTLTexture> output = (__bridge id<MTLTexture>)
            result.generated_frame.image.resource.native_handle;
        id<MTLSharedEvent> completion = (__bridge id<MTLSharedEvent>)
            result.generated_frame.completion.native_handle;
        success = output != nil && completion != nil &&
                  completion.signaledValue >= result.generated_frame.completion.value;
        if (success) {
            MTLTextureDescriptor* staging_descriptor =
                [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                   width:64 height:32 mipmapped:NO];
            staging_descriptor.storageMode = MTLStorageModeShared;
            staging_descriptor.usage = MTLTextureUsageUnknown;
            id<MTLTexture> staging = [device newTextureWithDescriptor:staging_descriptor];
            id<MTLCommandQueue> readback_queue = [device newCommandQueue];
            id<MTLCommandBuffer> readback_command = [readback_queue commandBuffer];
            id<MTLBlitCommandEncoder> blit = [readback_command blitCommandEncoder];
            if (staging == nil || readback_queue == nil || readback_command == nil || blit == nil) {
                success = false;
            } else {
                [blit copyFromTexture:output
                          sourceSlice:0
                          sourceLevel:0
                         sourceOrigin:MTLOriginMake(0, 0, 0)
                           sourceSize:MTLSizeMake(64, 32, 1)
                            toTexture:staging
                     destinationSlice:0
                     destinationLevel:0
                    destinationOrigin:MTLOriginMake(0, 0, 0)];
                [blit endEncoding];
                [readback_command commit];
                [readback_command waitUntilCompleted];
                std::array<std::uint8_t, 64 * 32 * 4> bytes{};
                if (readback_command.status != MTLCommandBufferStatusCompleted) {
                    success = false;
                } else {
                    [staging getBytes:bytes.data()
                          bytesPerRow:64 * 4
                           fromRegion:MTLRegionMake2D(0, 0, 64, 32)
                          mipmapLevel:0];
                    const auto red = bytes[0];
                    const auto green = bytes[1];
                    const auto blue = bytes[2];
                    success = red >= 134 && red <= 140 && green >= 185 && green <= 191 &&
                              blue >= 134 && blue <= 140 && bytes[3] == 255;
                    if (!success) {
                        std::cerr << "explicit UI-plane composite did not linearly interpolate sRGB scene and select nearest-presentation UI: "
                                  << static_cast<int>(red) << ','
                                  << static_cast<int>(green) << ','
                                  << static_cast<int>(blue) << ','
                                  << static_cast<int>(bytes[3]) << '\n';
                    }
                }
            }
        }
        framegen_presentation_event_t presentation{};
        init(presentation);
        init(presentation.consumer_completion);
        presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
        presentation.source_frame_id = 4;
        presentation.ticket_id = result.generated_frame.ticket_id;
        presentation.clock_domain = 1;
        presentation.presentation_timestamp_ns =
            request.desired_presentation_timestamp_ns;
        success = success && check(framegen_notify_presentation(context, &presentation),
                                   "notify Metal presentation");
    }
    framegen_ticket_release(ticket);
    framegen_context_destroy(context);
    return success;
}

bool run_automatic_mask_release_smoke(id<MTLDevice> native_device) {
    constexpr std::uint32_t width = 64;
    constexpr std::uint32_t height = 32;
    constexpr std::size_t frame_count = 21;
    auto device_result = framegen::metal::Device::create(native_device);
    if (!device_result) {
        std::cerr << "mask-release Metal device creation failed: "
                  << device_result.error().message << '\n';
        return false;
    }
    auto device = std::move(*device_result);
    auto generator_result = framegen::FrameGenerator::create(device.backend());
    if (!generator_result) {
        std::cerr << "mask-release generator creation failed: "
                  << generator_result.error().message << '\n';
        return false;
    }
    auto generator = std::move(*generator_result);

    MTLTextureDescriptor* input_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:width height:height mipmapped:NO];
    input_descriptor.storageMode = MTLStorageModeShared;
    input_descriptor.usage = MTLTextureUsageShaderRead;
    std::array<id<MTLTexture>, frame_count> textures{};
    std::array<id<MTLTexture>, frame_count> alternate_textures{};
    std::vector<std::uint8_t> pixels(width * height * 4, 0);
    for (std::size_t frame_index = 0; frame_index < frame_count; ++frame_index) {
        auto texture = [native_device newTextureWithDescriptor:input_descriptor];
        if (texture == nil) {
            std::cerr << "could not allocate mask-release input textures\n";
            return false;
        }
        for (std::uint32_t y = 0; y < height; ++y) {
            for (std::uint32_t x = 0; x < width; ++x) {
                const auto offset = (static_cast<std::size_t>(y) * width + x) * 4;
                const bool visible_ui = frame_index < 2 &&
                    (x == width / 2 || x == width / 2 + 1);
                pixels[offset] = visible_ui ? 255 : 0;
                pixels[offset + 1] = visible_ui ? 255 : 0;
                pixels[offset + 2] = visible_ui ? 255 : 0;
                pixels[offset + 3] = 255;
            }
        }
        [texture replaceRegion:MTLRegionMake2D(0, 0, width, height)
                   mipmapLevel:0 withBytes:pixels.data() bytesPerRow:width * 4];
        textures[frame_index] = texture;
    }
    alternate_textures = textures;

    MTLTextureDescriptor* staging_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:width height:height mipmapped:NO];
    staging_descriptor.storageMode = MTLStorageModeShared;
    staging_descriptor.usage = MTLTextureUsageUnknown;
    id<MTLTexture> staging = [native_device newTextureWithDescriptor:staging_descriptor];
    id<MTLCommandQueue> readback_queue = [native_device newCommandQueue];
    if (staging == nil || readback_queue == nil) {
        std::cerr << "could not allocate mask-release readback resources\n";
        return false;
    }

    auto* active_generator = &generator;
    auto* active_textures = &textures;
    auto submit_mask_pair = [&](std::size_t previous_index,
                                std::size_t current_index,
                                bool reset_history,
                                std::uint8_t& center_confidence,
                                framegen::ColorSpace color_space =
                                    framegen::ColorSpace::linear_srgb,
                                framegen::FrameColorMetadata color_metadata = {},
                                std::uint64_t clock_domain = 1,
                                std::int64_t timestamp_base_ns = 0,
                                std::int64_t timestamp_interval_ns = 16'000'000) -> bool {
        auto previous = device.wrap_texture((*active_textures)[previous_index],
            color_space, framegen::AlphaMode::opaque);
        auto current = device.wrap_texture((*active_textures)[current_index],
            color_space, framegen::AlphaMode::opaque);
        if (!previous || !current) {
            std::cerr << "could not wrap mask-release input textures\n";
            return false;
        }
        const auto previous_time = timestamp_base_ns +
            static_cast<std::int64_t>(previous_index) * timestamp_interval_ns;
        const auto current_time = timestamp_base_ns +
            static_cast<std::int64_t>(current_index) * timestamp_interval_ns;
        const auto target_time = previous_time + (current_time - previous_time) / 2;
        framegen::FrameSubmission submission{
            .previous = framegen::FrameInput{
                .texture = std::move(*previous),
                .timing = framegen::FrameTiming{
                    .sequence = previous_index + 1, .timestamp_ns = previous_time,
                    .clock_domain = clock_domain,
                    .desired_presentation_timestamp_ns = previous_time,
                },
                .color_metadata = color_metadata,
            },
            .current = framegen::FrameInput{
                .texture = std::move(*current),
                .timing = framegen::FrameTiming{
                    .sequence = current_index + 1, .timestamp_ns = current_time,
                    .clock_domain = clock_domain,
                    .desired_presentation_timestamp_ns = current_time,
                },
                .color_metadata = color_metadata,
            },
            .interpolation = 0.5F,
            .reset_history = reset_history,
            .interpolation_timestamp_ns = target_time,
            .desired_presentation_timestamp_ns = target_time,
            .hud_options = framegen::HudOptions{
                .mode = framegen::HudMode::automatic_protection,
                .ui_source = framegen::UiTemporalSource::nearest_presentation,
                .debug_visualization = framegen::HudDebugVisualization::stabilized_mask,
            },
        };
        auto generated = active_generator->submit(submission);
        if (!generated) {
            std::cerr << "automatic mask-release submission failed: "
                      << generated.error().message << '\n';
            return false;
        }
        auto completed = generated->completion->wait();
        id<MTLTexture> output = device.native_texture(generated->texture);
        id<MTLCommandBuffer> readback = [readback_queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [readback blitCommandEncoder];
        if (!completed || output == nil || readback == nil || blit == nil) {
            std::cerr << "automatic mask-release output could not be read back\n";
            return false;
        }
        [blit copyFromTexture:output
                  sourceSlice:0 sourceLevel:0
                 sourceOrigin:MTLOriginMake(0, 0, 0)
                   sourceSize:MTLSizeMake(width, height, 1)
                    toTexture:staging destinationSlice:0 destinationLevel:0
          destinationOrigin:MTLOriginMake(0, 0, 0)];
        [blit endEncoding];
        [readback commit];
        [readback waitUntilCompleted];
        if (readback.status != MTLCommandBufferStatusCompleted) {
            std::cerr << "automatic mask-release readback command failed\n";
            return false;
        }
        std::vector<std::uint8_t> output_pixels(width * height * 4);
        [staging getBytes:output_pixels.data() bytesPerRow:width * 4
               fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        const auto center = (static_cast<std::size_t>(height / 2) * width + width / 2) * 4;
        center_confidence = output_pixels[center];
        return true;
    };

    std::uint8_t initial_confidence{};
    std::uint8_t released_confidence{};
    std::uint8_t pair_two_confidence{};
    for (std::size_t pair = 0; pair + 1 < frame_count; ++pair) {
        std::uint8_t confidence{};
        if (!submit_mask_pair(pair, pair + 1, pair == 0, confidence)) return false;
        if (pair == 0) {
            initial_confidence = confidence;
            std::uint8_t repeated_confidence{};
            if (!submit_mask_pair(0, 1, false, repeated_confidence)) return false;
            if (repeated_confidence != initial_confidence) {
                std::cerr << "same-pair mask changed with repeated interpolation requests: "
                          << static_cast<int>(initial_confidence) << " -> "
                          << static_cast<int>(repeated_confidence) << '\n';
                return false;
            }
            std::uint8_t changed_metadata_confidence{};
            std::uint8_t explicit_reset_confidence{};
            if (!submit_mask_pair(1, 2, false, changed_metadata_confidence,
                                  framegen::ColorSpace::linear_srgb,
                                  framegen::FrameColorMetadata{
                                      .transfer_function = framegen::TransferFunction::linear,
                                      .dynamic_range = framegen::DynamicRange::sdr,
                                  }) ||
                !submit_mask_pair(1, 2, true, explicit_reset_confidence,
                                  framegen::ColorSpace::linear_srgb,
                                  framegen::FrameColorMetadata{
                                      .transfer_function = framegen::TransferFunction::linear,
                                      .dynamic_range = framegen::DynamicRange::sdr,
                                  })) {
                return false;
            }
            if (changed_metadata_confidence != explicit_reset_confidence) {
                std::cerr << "automatic HUD mask carried evidence across a color-space change: "
                          << static_cast<int>(changed_metadata_confidence) << " vs reset "
                          << static_cast<int>(explicit_reset_confidence) << '\n';
                return false;
            }
        }
        if (pair == 2) {
            pair_two_confidence = confidence;
            std::uint8_t stale_confidence{};
            if (!submit_mask_pair(0, 1, false, stale_confidence)) return false;
            std::uint8_t repeated_pair_two_confidence{};
            if (!submit_mask_pair(2, 3, false, repeated_pair_two_confidence)) return false;
            if (repeated_pair_two_confidence != pair_two_confidence) {
                std::cerr << "out-of-order source pair changed the current pair mask: "
                          << static_cast<int>(pair_two_confidence) << " -> "
                          << static_cast<int>(repeated_pair_two_confidence) << '\n';
                return false;
            }
        }
        released_confidence = confidence;
    }
    if (initial_confidence < 96 || released_confidence > 26) {
        std::cerr << "automatic HUD mask did not engage and then release: initial="
                  << static_cast<int>(initial_confidence) << ", final="
                  << static_cast<int>(released_confidence) << '\n';
        return false;
    }
    std::uint8_t restarted_stream_confidence{};
    std::uint8_t restarted_stream_reset_confidence{};
    if (!submit_mask_pair(0, 1, false, initial_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 2) ||
        !submit_mask_pair(1, 2, false, restarted_stream_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 2) ||
        !submit_mask_pair(1, 2, true, restarted_stream_reset_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 2)) {
        return false;
    }
    if (restarted_stream_confidence < 96 ||
        restarted_stream_reset_confidence > 26) {
        std::cerr << "automatic HUD history did not start fresh on a clock-domain change: continued="
                  << static_cast<int>(restarted_stream_confidence) << ", reset="
                  << static_cast<int>(restarted_stream_reset_confidence) << '\n';
        return false;
    }
    constexpr std::int64_t epoch_base_ns = 1'700'000'000'000'000'000LL;
    std::uint8_t epoch_first_confidence{};
    std::uint8_t other_stream_confidence{};
    std::uint8_t epoch_transition_confidence{};
    std::uint8_t epoch_continued_confidence{};
    std::uint8_t epoch_reset_confidence{};
    // Copying a generator creates independent backend temporal state.
    auto other_generator = generator;
    std::swap(alternate_textures[0], alternate_textures[2]);
    std::swap(alternate_textures[1], alternate_textures[3]);
    if (!submit_mask_pair(0, 1, true, epoch_first_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 3,
                          epoch_base_ns, 2)) {
        return false;
    }
    active_generator = &other_generator;
    active_textures = &alternate_textures;
    if (!submit_mask_pair(0, 1, true, other_stream_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 3,
                          epoch_base_ns, 2)) {
        return false;
    }
    active_generator = &generator;
    active_textures = &textures;
    if (!submit_mask_pair(1, 2, false, epoch_transition_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 3,
                          epoch_base_ns, 2) ||
        !submit_mask_pair(2, 3, false, epoch_continued_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 3,
                          epoch_base_ns, 2) ||
        !submit_mask_pair(2, 3, true, epoch_reset_confidence,
                          framegen::ColorSpace::linear_srgb, {}, 3,
                          epoch_base_ns, 2)) {
        return false;
    }
    if (epoch_first_confidence < 96 || other_stream_confidence > 26 ||
        epoch_continued_confidence <= epoch_reset_confidence) {
        std::cerr << "automatic HUD mask lost continuity for adjacent nanosecond source pairs at epoch-scale timestamps: first="
                  << static_cast<int>(epoch_first_confidence) << ", transition="
                  << static_cast<int>(epoch_transition_confidence) << ", continued="
                  << static_cast<int>(epoch_continued_confidence) << ", reset="
                  << static_cast<int>(epoch_reset_confidence) << ", other stream="
                  << static_cast<int>(other_stream_confidence) << '\n';
        return false;
    }
    return true;
}

} // namespace

int main() {
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    return device != nil && run_contract_smoke() &&
        run_automatic_mask_release_smoke(device) ? 0 : 1;
}
