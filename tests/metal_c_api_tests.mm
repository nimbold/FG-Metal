#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include "framegen/api.h"
#include "framegen/metal_api.h"

#include <chrono>
#include <algorithm>
#include <cstdint>
#include <iostream>
#include <thread>

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
         (FRAMEGEN_CAP_COLOR_ONLY | FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME)) !=
        (FRAMEGEN_CAP_COLOR_ONLY | FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME)) {
        std::cerr << "Metal placeholder did not advertise its color-only capabilities\n";
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
                                    FRAMEGEN_CAP_ARBITRARY_INTERPOLATION_TIME;
    config.history_capacity = 4;
    framegen_capability_result_t negotiated{};
    init(negotiated);
    if (!check(framegen_context_configure(context, &config, &negotiated),
               "configure Metal context")) {
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
    if (first_texture == nil || second_texture == nil) {
        std::cerr << "could not allocate Metal input textures\n";
        framegen_context_destroy(context);
        return false;
    }
    std::uint32_t pixels[64 * 32]{};
    std::fill(std::begin(pixels), std::end(pixels), 0xff3060c0u);
    const MTLRegion region = MTLRegionMake2D(0, 0, 64, 32);
    [first_texture replaceRegion:region mipmapLevel:0 withBytes:pixels
                     bytesPerRow:64 * sizeof(std::uint32_t)];
    std::fill(std::begin(pixels), std::end(pixels), 0xffc05030u);
    [second_texture replaceRegion:region mipmapLevel:0 withBytes:pixels
                      bytesPerRow:64 * sizeof(std::uint32_t)];

    using Clock = std::chrono::steady_clock;
    const auto base_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        Clock::now().time_since_epoch()).count();
    constexpr std::int64_t kFrameInterval = 16'000'000;
    auto make_frame = [&](std::uint64_t frame_id, id<MTLTexture> texture,
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
        frame.color.transfer_function = FRAMEGEN_TRANSFER_LINEAR;
        frame.color.dynamic_range = FRAMEGEN_DYNAMIC_RANGE_SDR;
        frame.color.alpha_mode = FRAMEGEN_ALPHA_OPAQUE;
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

    auto first = make_frame(1, first_texture, base_ns);
    auto second = make_frame(2, second_texture, base_ns + kFrameInterval);
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
    request.previous_source_frame_id = 1;
    request.current_source_frame_id = 2;
    request.history_generation = second_receipt.history_generation;
    request.clock_domain = 1;
    request.interpolation_timestamp_ns = base_ns + kFrameInterval / 2;
    request.desired_presentation_timestamp_ns = base_ns + 2 * kFrameInterval;
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
        framegen_presentation_event_t presentation{};
        init(presentation);
        init(presentation.consumer_completion);
        presentation.disposition = FRAMEGEN_PRESENTED_GENERATED_FRAME;
        presentation.source_frame_id = 2;
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

} // namespace

int main() {
    return run_contract_smoke() ? 0 : 1;
}
