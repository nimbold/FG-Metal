#import "metal_backend.hpp"

#import <Foundation/Foundation.h>
#import <mach-o/dyld.h>

#include "framegen/metal.hpp"
#include "rife_model.hpp"
#include "rife_model_shader.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <limits>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace framegen::metal {
namespace {

constexpr std::string_view kBackendId = "org.framegen.metal";

std::atomic<std::uint64_t> g_next_device_id{1};

std::optional<std::uint64_t> allocate_device_id() noexcept {
    auto next = g_next_device_id.load(std::memory_order_relaxed);
    while (next != std::numeric_limits<std::uint64_t>::max()) {
        if (g_next_device_id.compare_exchange_weak(
                next, next + 1, std::memory_order_relaxed,
                std::memory_order_relaxed)) {
            return next;
        }
    }
    return std::nullopt;
}

Error make_error(ErrorCode code, std::string message) {
    return Error{code, std::move(message)};
}

std::string error_message(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) {
        return fallback;
    }
    const char* utf8 = error.localizedDescription.UTF8String;
    return utf8 == nullptr ? fallback : utf8;
}

std::filesystem::path model_weights_path() {
    if (const char* configured = std::getenv("FRAMEGEN_MODEL_WEIGHTS");
        configured != nullptr && configured[0] != '\0') {
        return std::filesystem::path(configured);
    }
    const std::filesystem::path relative_weights(
        "models/local/rife-v4.26/rife-v4.26.fgweights");
    const auto search_parents = [&](std::filesystem::path base)
        -> std::optional<std::filesystem::path> {
        std::error_code filesystem_error;
        base = std::filesystem::weakly_canonical(base, filesystem_error);
        if (filesystem_error) return std::nullopt;
        for (std::size_t depth = 0; depth < 16; ++depth) {
            const auto candidate = base / relative_weights;
            if (std::filesystem::is_regular_file(candidate, filesystem_error) &&
                !filesystem_error) {
                return candidate;
            }
            filesystem_error.clear();
            const auto parent = base.parent_path();
            if (parent == base || parent.empty()) break;
            base = parent;
        }
        return std::nullopt;
    };

    std::error_code filesystem_error;
    if (auto from_working_directory = search_parents(
            std::filesystem::current_path(filesystem_error));
        from_working_directory) {
        return *from_working_directory;
    }
    std::array<char, 4096> executable_path{};
    std::uint32_t executable_path_size =
        static_cast<std::uint32_t>(executable_path.size());
    if (_NSGetExecutablePath(executable_path.data(), &executable_path_size) == 0) {
        if (auto from_executable = search_parents(
                std::filesystem::path(executable_path.data()).parent_path());
            from_executable) {
            return *from_executable;
        }
    }
    return relative_weights;
}

std::optional<detail::RifeMode> internal_model_mode(std::string& error) {
    const char* configured = std::getenv("FRAMEGEN_METAL_MODEL_VARIANT");
    if (configured == nullptr || configured[0] == '\0' ||
        std::string_view(configured) == "QUALITY") {
        error.clear();
        return detail::RifeMode::quality;
    }
    if (std::string_view(configured) == "BALANCED") {
        error.clear();
        return detail::RifeMode::balanced;
    }
    error = "FRAMEGEN_METAL_MODEL_VARIANT must be QUALITY or BALANCED";
    return std::nullopt;
}

std::optional<MTLPixelFormat> to_metal_format(PixelFormat format) {
    switch (format) {
    case PixelFormat::r8_unorm: return MTLPixelFormatR8Unorm;
    case PixelFormat::rg8_unorm: return MTLPixelFormatRG8Unorm;
    case PixelFormat::rgba8_unorm: return MTLPixelFormatRGBA8Unorm;
    case PixelFormat::rgba8_unorm_srgb: return MTLPixelFormatRGBA8Unorm_sRGB;
    case PixelFormat::bgra8_unorm: return MTLPixelFormatBGRA8Unorm;
    case PixelFormat::bgra8_unorm_srgb: return MTLPixelFormatBGRA8Unorm_sRGB;
    case PixelFormat::rgb10a2_unorm: return MTLPixelFormatRGB10A2Unorm;
    case PixelFormat::bgr10a2_unorm: return MTLPixelFormatBGR10A2Unorm;
    case PixelFormat::rg11b10_float: return MTLPixelFormatRG11B10Float;
    case PixelFormat::rgba16_float: return MTLPixelFormatRGBA16Float;
    case PixelFormat::rgba32_float: return MTLPixelFormatRGBA32Float;
    case PixelFormat::r16_float:
    case PixelFormat::rg16_float:
    case PixelFormat::rg32_float:
    case PixelFormat::r32_float:
    case PixelFormat::d16_unorm:
    case PixelFormat::d32_float:
    case PixelFormat::d24_unorm_s8_uint:
    case PixelFormat::unknown: return std::nullopt;
    }
    return std::nullopt;
}

std::optional<PixelFormat> from_metal_format(MTLPixelFormat format) {
    switch (format) {
    case MTLPixelFormatR8Unorm: return PixelFormat::r8_unorm;
    case MTLPixelFormatRG8Unorm: return PixelFormat::rg8_unorm;
    case MTLPixelFormatRGBA8Unorm: return PixelFormat::rgba8_unorm;
    case MTLPixelFormatRGBA8Unorm_sRGB: return PixelFormat::rgba8_unorm_srgb;
    case MTLPixelFormatBGRA8Unorm: return PixelFormat::bgra8_unorm;
    case MTLPixelFormatBGRA8Unorm_sRGB: return PixelFormat::bgra8_unorm_srgb;
    case MTLPixelFormatRGB10A2Unorm: return PixelFormat::rgb10a2_unorm;
    case MTLPixelFormatBGR10A2Unorm: return PixelFormat::bgr10a2_unorm;
    case MTLPixelFormatRG11B10Float: return PixelFormat::rg11b10_float;
    case MTLPixelFormatRGBA16Float: return PixelFormat::rgba16_float;
    case MTLPixelFormatRGBA32Float: return PixelFormat::rgba32_float;
    default: return std::nullopt;
    }
}

constexpr const char* kExplicitUiShader = R"metal(
#include <metal_stdlib>
using namespace metal;

struct ExplicitUiParameters {
    float interpolation;
    uint current_ui;
    uint alpha_mode;
    uint scene_is_srgb;
    uint ui_is_srgb;
    uint debug_visualization;
};

float3 srgb_to_linear(float3 value) {
    const float3 low = value / 12.92f;
    const float3 high = pow((value + 0.055f) / 1.055f, float3(2.4f));
    return select(high, low, value <= 0.04045f);
}

float3 linear_to_srgb(float3 value) {
    value = max(value, float3(0.0f));
    const float3 low = value * 12.92f;
    const float3 high = 1.055f * pow(value, float3(1.0f / 2.4f)) - 0.055f;
    return select(high, low, value <= 0.0031308f);
}

kernel void framegen_explicit_ui(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::read> previous_ui [[texture(2)]],
    texture2d<float, access::read> current_ui [[texture(3)]],
    texture2d<float, access::read> generated_scene [[texture(4)]],
    texture2d<float, access::write> output [[texture(5)]],
    constant ExplicitUiParameters& parameters [[buffer(0)]],
    uint2 position [[thread_position_in_grid]]) {
    if (position.x >= output.get_width() || position.y >= output.get_height()) return;
    const float4 a = previous.read(position);
    const float4 b = current.read(position);
    const float4 ui_encoded = parameters.current_ui != 0
        ? current_ui.read(position) : previous_ui.read(position);
    const float alpha = parameters.alpha_mode == 1
        ? 1.0f : clamp(ui_encoded.a, 0.0f, 1.0f);
    const float3 encoded_scene = generated_scene.read(position).rgb;
    const float3 scene = parameters.scene_is_srgb != 0
        ? srgb_to_linear(encoded_scene) : encoded_scene;
    const float3 ui_decoded = parameters.ui_is_srgb != 0
        ? srgb_to_linear(ui_encoded.rgb) : ui_encoded.rgb;
    const float3 composited = parameters.alpha_mode == 3
        ? ui_decoded + scene * (1.0f - alpha)
        : mix(scene, ui_decoded, alpha);

    float3 display = composited;
    switch (parameters.debug_visualization) {
    case 1:
        display = float3(alpha);
        break;
    case 2:
        display = float3(alpha);
        break;
    case 3:
        display = mix(scene, float3(1.0f, 0.08f, 0.04f), alpha * 0.65f);
        break;
    case 4:
        display = float3(1.0f - alpha);
        break;
    default:
        break;
    }
    const bool mask_visualization = parameters.debug_visualization == 1 ||
        parameters.debug_visualization == 2 || parameters.debug_visualization == 4;
    if (parameters.scene_is_srgb != 0 && !mask_visualization) {
        display = linear_to_srgb(display);
    }
    // Explicit mode requires opaque scene color, so the composite remains
    // opaque regardless of unused alpha bytes in the scene textures.
    output.write(float4(clamp(display, 0.0f, 1.0f), 1.0f), position);
}
)metal";

constexpr const char* kAutomaticHudShader = R"metal(
#include <metal_stdlib>
using namespace metal;

struct AutomaticHudParameters {
    float interpolation;
    float engagement_alpha;
    float release_alpha;
    uint current_ui;
    uint has_history;
    uint debug_visualization;
    uint reset_history;
    uint has_cut_summary;
};

float luminance(float3 value) {
    return dot(value, float3(0.2126f, 0.7152f, 0.0722f));
}

float pixel_change(texture2d<float, access::read> previous,
                   texture2d<float, access::read> current, uint2 p) {
    return (abs(previous.read(p).r - current.read(p).r) +
            abs(previous.read(p).g - current.read(p).g) +
            abs(previous.read(p).b - current.read(p).b)) / 3.0f;
}

kernel void framegen_automatic_hud(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::read> previous_mask [[texture(2)]],
    texture2d<float, access::read> generated_scene [[texture(3)]],
    texture2d<float, access::write> output [[texture(4)]],
    texture2d<float, access::write> stabilized_mask [[texture(5)]],
    constant AutomaticHudParameters& parameters [[buffer(0)]],
    device const uint* cut_summary_words [[buffer(1)]],
    uint2 position [[thread_position_in_grid]]) {
    const uint width = output.get_width();
    const uint height = output.get_height();
    if (position.x >= width || position.y >= height) return;

    const uint2 left = uint2(position.x == 0 ? 0 : position.x - 1, position.y);
    const uint2 right = uint2(min(position.x + 1, width - 1), position.y);
    const uint2 up = uint2(position.x, position.y == 0 ? 0 : position.y - 1);
    const uint2 down = uint2(position.x, min(position.y + 1, height - 1));
    const float change = pixel_change(previous, current, position);
    const float neighborhood_motion = 0.25f * (
        pixel_change(previous, current, left) + pixel_change(previous, current, right) +
        pixel_change(previous, current, up) + pixel_change(previous, current, down));
    const float screen_stability = 1.0f - smoothstep(0.012f, 0.105f, change);

    const float3 center_rgb = 0.5f * (previous.read(position).rgb + current.read(position).rgb);
    const float3 left_rgb = 0.5f * (previous.read(left).rgb + current.read(left).rgb);
    const float3 right_rgb = 0.5f * (previous.read(right).rgb + current.read(right).rgb);
    const float3 up_rgb = 0.5f * (previous.read(up).rgb + current.read(up).rgb);
    const float3 down_rgb = 0.5f * (previous.read(down).rgb + current.read(down).rgb);
    const float center_luma = luminance(center_rgb);
    const float horizontal_edge = abs(luminance(left_rgb) - luminance(right_rgb));
    const float vertical_edge = abs(luminance(up_rgb) - luminance(down_rgb));
    const float edge_strength = max(horizontal_edge, vertical_edge);
    const float horizontal_frequency = abs(
        center_luma - 0.5f * (luminance(left_rgb) + luminance(right_rgb)));
    const float vertical_frequency = abs(
        center_luma - 0.5f * (luminance(up_rgb) + luminance(down_rgb)));
    const float text_structure = smoothstep(0.035f, 0.14f,
        max(horizontal_frequency, vertical_frequency)) *
        smoothstep(0.06f, 0.20f, edge_strength);
    const float motion_disagreement = smoothstep(
        0.018f, 0.09f, neighborhood_motion - change);
    const float sharp_structure = smoothstep(0.045f, 0.19f, edge_strength);
    const bool detected_cut = parameters.has_cut_summary != 0 &&
        cut_summary_words[4] != 0;
    const bool reset = parameters.reset_history != 0 || detected_cut;
    const bool has_effective_history = parameters.has_history != 0 && !reset;
    const float prior = has_effective_history ? previous_mask.read(position).r : 0.0f;
    const float recurring_fixed_change =
        smoothstep(0.025f, 0.12f, change) * smoothstep(0.24f, 0.62f, prior);
    const float raw = clamp(max(
        screen_stability * sharp_structure * 0.22f,
        max(text_structure * (0.12f + 0.66f * screen_stability),
            max(motion_disagreement * sharp_structure * 0.32f,
                recurring_fixed_change * 0.7f))), 0.0f, 1.0f);

    const float stabilized = reset ? raw : (raw >= prior
        ? mix(prior, raw, parameters.engagement_alpha)
        : mix(prior, raw, parameters.release_alpha));
    const float neighbor_prior = has_effective_history ? 0.25f * (
        previous_mask.read(left).r + previous_mask.read(right).r +
        previous_mask.read(up).r + previous_mask.read(down).r) : 0.0f;
    const float soft_confidence = smoothstep(0.12f, 0.72f, stabilized);
    const float feathered_confidence = smoothstep(0.12f, 0.72f, neighbor_prior);
    const float confidence = clamp(
        soft_confidence * 0.84f + feathered_confidence * 0.16f, 0.0f, 1.0f);
    // Keep unsaturated temporal evidence here. Storing the thresholded output
    // confidence would feed 1.0 back through release hysteresis; values above
    // the confidence ramp's upper edge then remain pinned indefinitely.
    stabilized_mask.write(float4(stabilized, 0.0f, 0.0f, 1.0f), position);

    const float4 a = previous.read(position);
    const float4 b = current.read(position);
    const float4 generated = generated_scene.read(position);
    const float4 protected_source = parameters.current_ui != 0 ? b : a;
    float4 result = mix(generated, protected_source, confidence);
    if (parameters.debug_visualization == 1) {
        result = float4(raw, raw, raw, 1.0f);
    } else if (parameters.debug_visualization == 2) {
        result = float4(confidence, confidence, confidence, 1.0f);
    } else if (parameters.debug_visualization == 3) {
        result = float4(mix(generated.rgb, float3(0.05f, 1.0f, 0.1f), confidence * 0.7f), 1.0f);
    } else if (parameters.debug_visualization == 4) {
        const float certainty = 1.0f - confidence;
        result = float4(certainty, certainty, certainty, 1.0f);
    }
    output.write(result, position);
}
)metal";

constexpr const char* kTemporalQualityShader = R"metal(
#include <metal_stdlib>
using namespace metal;

struct TemporalCutTileStats {
    uint histogram_a[16];
    uint histogram_b[16];
    float difference_sum;
    float generated_residual_sum;
    uint changed_count;
    uint sample_count;
};

struct TemporalCutSummary {
    float histogram_difference;
    float downsampled_difference;
    float generated_residual;
    float changed_fraction;
    uint scene_cut;
};

struct TemporalCutTileParameters {
    uint groups_x;
    uint width;
    uint height;
    float interpolation;
};

struct TemporalCutReduceParameters {
    uint tile_count;
    uint force_reset;
};

struct TemporalQualityParameters {
    float interpolation;
    float history_time_scale;
    uint policy;
    uint debug_visualization;
    uint force_reset;
    uint force_real_fallback;
    uint advance_history;
    uint reuse_cached_output;
    uint current_source;
    uint scene_is_srgb;
    uint has_hud_mask;
};

float temporal_luminance(float3 value) {
    return dot(value, float3(0.2126f, 0.7152f, 0.0722f));
}

float temporal_rgb_distance(float3 left, float3 right) {
    return (abs(left.r - right.r) + abs(left.g - right.g) + abs(left.b - right.b)) / 3.0f;
}

float3 temporal_decode_srgb(float3 value) {
    const float3 low = value / 12.92f;
    const float3 high = pow((value + 0.055f) / 1.055f, float3(2.4f));
    return select(high, low, value <= 0.04045f);
}

float3 temporal_encode_srgb(float3 value) {
    value = max(value, float3(0.0f));
    const float3 low = value * 12.92f;
    const float3 high = 1.055f * pow(value, float3(1.0f / 2.4f)) - 0.055f;
    return select(high, low, value <= 0.0031308f);
}

kernel void framegen_temporal_cut_tiles(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::read> generated [[texture(2)]],
    device TemporalCutTileStats* tile_stats [[buffer(0)]],
    constant TemporalCutTileParameters& parameters [[buffer(1)]],
    uint2 group [[threadgroup_position_in_grid]],
    uint2 local [[thread_position_in_threadgroup]]) {
    threadgroup atomic_uint histogram_a[16];
    threadgroup atomic_uint histogram_b[16];
    threadgroup float differences[256];
    threadgroup float residuals[256];
    threadgroup uint changed[256];
    threadgroup uint samples[256];
    const uint lane = local.y * 16 + local.x;
    if (lane < 16) {
        atomic_store_explicit(&histogram_a[lane], 0u, memory_order_relaxed);
        atomic_store_explicit(&histogram_b[lane], 0u, memory_order_relaxed);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    const uint base_x = group.x * 64 + local.x * 4;
    const uint base_y = group.y * 64 + local.y * 4;
    const bool valid = base_x < parameters.width && base_y < parameters.height;
    if (valid) {
        const uint2 p = uint2(min(base_x + 2, parameters.width - 1),
                              min(base_y + 2, parameters.height - 1));
        const float3 a = previous.read(p).rgb;
        const float3 b = current.read(p).rgb;
        const float3 g = generated.read(p).rgb;
        const uint bin_a = min(uint(temporal_luminance(a) * 16.0f), 15u);
        const uint bin_b = min(uint(temporal_luminance(b) * 16.0f), 15u);
        atomic_fetch_add_explicit(&histogram_a[bin_a], 1u, memory_order_relaxed);
        atomic_fetch_add_explicit(&histogram_b[bin_b], 1u, memory_order_relaxed);
        differences[lane] = temporal_rgb_distance(a, b);
        residuals[lane] = temporal_rgb_distance(g, mix(a, b, parameters.interpolation));
        changed[lane] = differences[lane] > 0.22f ? 1u : 0u;
        samples[lane] = 1u;
    } else {
        differences[lane] = 0.0f;
        residuals[lane] = 0.0f;
        changed[lane] = 0u;
        samples[lane] = 0u;
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (lane < stride) {
            differences[lane] += differences[lane + stride];
            residuals[lane] += residuals[lane + stride];
            changed[lane] += changed[lane + stride];
            samples[lane] += samples[lane + stride];
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }

    const uint tile_index = group.y * parameters.groups_x + group.x;
    device TemporalCutTileStats& tile = tile_stats[tile_index];
    if (lane < 16) {
        tile.histogram_a[lane] = atomic_load_explicit(&histogram_a[lane], memory_order_relaxed);
        tile.histogram_b[lane] = atomic_load_explicit(&histogram_b[lane], memory_order_relaxed);
    }
    if (lane == 0) {
        tile.difference_sum = differences[0];
        tile.generated_residual_sum = residuals[0];
        tile.changed_count = changed[0];
        tile.sample_count = samples[0];
    }
}

kernel void framegen_temporal_cut_reduce(
    device const TemporalCutTileStats* tile_stats [[buffer(0)]],
    device TemporalCutSummary* summary [[buffer(1)]],
    constant TemporalCutReduceParameters& parameters [[buffer(2)]],
    uint lane [[thread_index_in_threadgroup]]) {
    threadgroup atomic_uint histogram_a[16];
    threadgroup atomic_uint histogram_b[16];
    threadgroup float differences[256];
    threadgroup float residuals[256];
    threadgroup float changed[256];
    threadgroup float samples[256];
    threadgroup float histogram_distance[16];
    if (lane < 16) {
        atomic_store_explicit(&histogram_a[lane], 0u, memory_order_relaxed);
        atomic_store_explicit(&histogram_b[lane], 0u, memory_order_relaxed);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);

    float lane_difference = 0.0f;
    float lane_residual = 0.0f;
    float lane_changed = 0.0f;
    float lane_samples = 0.0f;
    for (uint tile_index = lane; tile_index < parameters.tile_count; tile_index += 256) {
        const device TemporalCutTileStats& tile = tile_stats[tile_index];
        lane_difference += tile.difference_sum;
        lane_residual += tile.generated_residual_sum;
        lane_changed += float(tile.changed_count);
        lane_samples += float(tile.sample_count);
        for (uint bin = 0; bin < 16; ++bin) {
            atomic_fetch_add_explicit(&histogram_a[bin], tile.histogram_a[bin], memory_order_relaxed);
            atomic_fetch_add_explicit(&histogram_b[bin], tile.histogram_b[bin], memory_order_relaxed);
        }
    }
    differences[lane] = lane_difference;
    residuals[lane] = lane_residual;
    changed[lane] = lane_changed;
    samples[lane] = lane_samples;
    threadgroup_barrier(mem_flags::mem_threadgroup);

    for (uint stride = 128; stride > 0; stride >>= 1) {
        if (lane < stride) {
            differences[lane] += differences[lane + stride];
            residuals[lane] += residuals[lane + stride];
            changed[lane] += changed[lane + stride];
            samples[lane] += samples[lane + stride];
        }
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }

    if (lane < 16) {
        const float a = float(atomic_load_explicit(&histogram_a[lane], memory_order_relaxed));
        const float b = float(atomic_load_explicit(&histogram_b[lane], memory_order_relaxed));
        histogram_distance[lane] = abs(a - b);
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    for (uint stride = 8; stride > 0; stride >>= 1) {
        if (lane < stride) histogram_distance[lane] += histogram_distance[lane + stride];
        threadgroup_barrier(mem_flags::mem_threadgroup);
    }

    if (lane == 0) {
        const float count = max(samples[0], 1.0f);
        const float histogram_difference = histogram_distance[0] / (2.0f * count);
        const float mean_difference = differences[0] / count;
        const float generated_residual = residuals[0] / count;
        const float changed_fraction = changed[0] / count;
        const bool cut =
            (histogram_difference > 0.42f && mean_difference > 0.18f && changed_fraction > 0.55f) ||
            (histogram_difference < 0.05f && mean_difference > 0.30f && changed_fraction > 0.80f) ||
            (mean_difference > 0.50f && changed_fraction > 0.80f) ||
            (histogram_difference > 0.30f && mean_difference > 0.20f &&
             changed_fraction > 0.60f && generated_residual > 0.42f);
        summary->histogram_difference = histogram_difference;
        summary->downsampled_difference = mean_difference;
        summary->generated_residual = generated_residual;
        summary->changed_fraction = changed_fraction;
        summary->scene_cut = cut ? 1u : 0u;
        (void)parameters.force_reset;
    }
}

float2 temporal_neighbors(texture2d<float, access::read> image, uint2 p) {
    const uint width = image.get_width();
    const uint height = image.get_height();
    const uint2 left = uint2(p.x == 0 ? 0 : p.x - 1, p.y);
    const uint2 right = uint2(min(p.x + 1, width - 1), p.y);
    const uint2 up = uint2(p.x, p.y == 0 ? 0 : p.y - 1);
    const uint2 down = uint2(p.x, min(p.y + 1, height - 1));
    const float l = temporal_luminance(image.read(left).rgb);
    const float r = temporal_luminance(image.read(right).rgb);
    const float u = temporal_luminance(image.read(up).rgb);
    const float d = temporal_luminance(image.read(down).rgb);
    return float2(max(abs(l - r), abs(u - d)),
                  abs(temporal_luminance(image.read(p).rgb) - 0.25f * (l + r + u + d)));
}

kernel void framegen_temporal_quality(
    texture2d<float, access::read> previous [[texture(0)]],
    texture2d<float, access::read> current [[texture(1)]],
    texture2d<float, access::read> generated [[texture(2)]],
    texture2d<float, access::read> previous_generated [[texture(3)]],
    texture2d<float, access::write> output [[texture(4)]],
    texture2d<float, access::write> history_output [[texture(5)]],
    texture2d<float, access::read> hud_mask [[texture(6)]],
    device const TemporalCutSummary* cut_summary [[buffer(0)]],
    device uint* history_valid_state [[buffer(1)]],
    constant TemporalQualityParameters& parameters [[buffer(2)]],
    uint2 p [[thread_position_in_grid]]) {
    const uint width = output.get_width();
    const uint height = output.get_height();
    if (p.x >= width || p.y >= height) return;

    const uint2 left = uint2(p.x == 0 ? 0 : p.x - 1, p.y);
    const uint2 right = uint2(min(p.x + 1, width - 1), p.y);
    const uint2 up = uint2(p.x, p.y == 0 ? 0 : p.y - 1);
    const uint2 down = uint2(p.x, min(p.y + 1, height - 1));
    const float4 a = previous.read(p);
    const float4 b = current.read(p);
    const float4 g = generated.read(p);
    const float4 nearest_source = parameters.current_source != 0 ? b : a;
    const bool scene_cut = cut_summary[0].scene_cut != 0;
    // A reset can follow resource allocation that failed before its first GPU
    // initialization pass. Do not read private storage until a prior pass has
    // committed valid history.
    const uint history_state = parameters.force_reset != 0
        ? 0u : history_valid_state[0];
    const bool has_history = parameters.force_reset == 0 && !scene_cut &&
        history_state == 1;
    const bool hold_real_frame = history_state == 2 ||
        parameters.force_real_fallback != 0;

    const float source_change = abs(temporal_luminance(a.rgb) - temporal_luminance(b.rgb));
    const float source_neighborhood_motion = max(source_change, max(
        abs(temporal_luminance(previous.read(left).rgb) - temporal_luminance(current.read(left).rgb)),
        max(abs(temporal_luminance(previous.read(right).rgb) - temporal_luminance(current.read(right).rgb)),
            max(abs(temporal_luminance(previous.read(up).rgb) - temporal_luminance(current.read(up).rgb)),
                abs(temporal_luminance(previous.read(down).rgb) - temporal_luminance(current.read(down).rgb))))));

    float best_source_support = 1.0f;
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, previous.read(p).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, current.read(p).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, previous.read(left).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, previous.read(right).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, previous.read(up).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, previous.read(down).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, current.read(left).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, current.read(right).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, current.read(up).rgb));
    best_source_support = min(best_source_support, temporal_rgb_distance(g.rgb, current.read(down).rgb));
    const float source_support = 1.0f - smoothstep(0.04f, 0.22f, best_source_support);
    const float disocclusion = smoothstep(0.075f, 0.24f, source_neighborhood_motion) *
        (1.0f - source_support);

    const float2 edge_source = temporal_neighbors(previous, p);
    const float2 edge_current = temporal_neighbors(current, p);
    const float2 edge_generated = temporal_neighbors(generated, p);
    const float high_frequency = smoothstep(0.025f, 0.11f,
        max(edge_generated.y, max(edge_source.y, edge_current.y)));
    const float thin_feature = smoothstep(0.035f, 0.14f,
        max(edge_generated.y, max(edge_source.y, edge_current.y))) *
        smoothstep(0.06f, 0.22f,
            max(edge_generated.x, max(edge_source.x, edge_current.x)));
    const float source_neighbor_luma = 0.25f * (
        temporal_luminance(previous.read(left).rgb) + temporal_luminance(previous.read(right).rgb) +
        temporal_luminance(previous.read(up).rgb) + temporal_luminance(previous.read(down).rgb));
    const float particle_outlier = smoothstep(0.08f, 0.24f,
        max(temporal_luminance(a.rgb), temporal_luminance(b.rgb)) - source_neighbor_luma) *
        smoothstep(0.04f, 0.18f, source_neighborhood_motion) * high_frequency;

    float temporal_excess = 0.0f;
    if (has_history) {
        const float generated_change = temporal_rgb_distance(g.rgb, previous_generated.read(p).rgb);
        const float expected_source_change = temporal_rgb_distance(a.rgb, b.rgb) *
            parameters.history_time_scale;
        temporal_excess = smoothstep(0.025f, 0.15f,
            max(0.0f, generated_change - expected_source_change - 0.012f));
        // History is not motion-warped. Do not interpret same-pixel change as
        // instability when source motion makes that correspondence unreliable.
        const float history_motion_reliability = 1.0f - smoothstep(
            0.025f, 0.12f, source_neighborhood_motion);
        temporal_excess *= history_motion_reliability;
    }
    // Motion alone does not mean a thin feature is unstable: a valid moving
    // fence should still interpolate. Penalize it when the output's temporal
    // change exceeds the source-pair motion evidence.
    const float thin_instability = thin_feature * temporal_excess;
    const float high_frequency_instability = high_frequency * max(temporal_excess, particle_outlier * 0.35f);
    float confidence = 1.0f - clamp(max(disocclusion * 0.92f,
        max(temporal_excess * 0.86f,
            max(thin_instability * 0.95f, high_frequency_instability * 0.78f))), 0.0f, 1.0f);
    const float temporal_agreement =
        1.0f - smoothstep(0.025f, 0.15f, temporal_excess);
    const float stable_structure_confidence = max(
        thin_feature * temporal_agreement,
        high_frequency * temporal_agreement * (1.0f - particle_outlier * 0.75f));
    confidence = max(confidence, stable_structure_confidence * 0.92f);
    if (parameters.has_hud_mask != 0 && hud_mask.read(p).r > 0.5f) confidence = 1.0f;

    float4 source_blend = mix(a, b, parameters.interpolation);
    if (parameters.scene_is_srgb != 0) {
        source_blend.rgb = temporal_encode_srgb(mix(
            temporal_decode_srgb(a.rgb), temporal_decode_srgb(b.rgb), parameters.interpolation));
    }
    float4 result = g;
    if (scene_cut || hold_real_frame) {
        result = nearest_source;
    } else if (!has_history) {
        // A cold start has no temporal evidence yet, so emit the model output
        // unchanged while seeding history. Cuts and continuity failures take
        // the real-frame path above.
        result = g;
    } else if (parameters.reuse_cached_output != 0) {
        result = previous_generated.read(p);
    } else if (parameters.policy == 1) {
        result = mix(nearest_source, g, smoothstep(0.20f, 0.76f, confidence));
    } else if (parameters.policy == 2) {
        result = mix(source_blend, g, smoothstep(0.20f, 0.76f, confidence));
    } else if (parameters.policy == 3 && confidence < 0.34f) {
        result = nearest_source;
    }
    result.a = 1.0f;
    history_output.write(result, p);

    float4 debug_result = result;
    switch (parameters.debug_visualization) {
    case 1:
        debug_result = float4(confidence, confidence, confidence, 1.0f);
        break;
    case 2:
        debug_result = float4(disocclusion, 0.08f, 1.0f - disocclusion, 1.0f);
        break;
    case 3:
        debug_result = float4(thin_feature, 0.12f, thin_instability, 1.0f);
        break;
    case 4:
        debug_result = float4(high_frequency, high_frequency, high_frequency, 1.0f);
        break;
    case 5:
        debug_result = float4(particle_outlier, particle_outlier * 0.38f, 0.02f, 1.0f);
        break;
    case 6:
        debug_result = scene_cut ? float4(1.0f, 0.02f, 0.02f, 1.0f)
                                 : float4(0.02f, 0.85f, 0.04f, 1.0f);
        break;
    case 7: {
        float3 category = confidence > 0.76f ? float3(0.08f, 0.68f, 0.12f)
            : (confidence > 0.42f ? float3(0.92f, 0.68f, 0.05f) : float3(0.92f, 0.16f, 0.04f));
        if (disocclusion > 0.45f) category = float3(0.05f, 0.68f, 0.92f);
        if (thin_instability > 0.35f) category = float3(0.82f, 0.12f, 0.86f);
        if (particle_outlier > 0.52f) category = float3(1.0f, 0.32f, 0.03f);
        debug_result = float4(category, 1.0f);
        break;
    }
    default:
        break;
    }
    output.write(debug_result, p);
}

kernel void framegen_temporal_history_commit(
    device const TemporalCutSummary* cut_summary [[buffer(0)]],
    device uint* history_valid_state [[buffer(1)]],
    constant TemporalQualityParameters& parameters [[buffer(2)]],
    uint lane [[thread_position_in_grid]]) {
    if (lane == 0 && parameters.advance_history != 0) {
        history_valid_state[0] = cut_summary[0].scene_cut != 0 ? 2u : 1u;
    }
}
)metal";

std::shared_ptr<detail::MetalTextureResource> as_metal_resource(const Texture& texture) {
    return std::dynamic_pointer_cast<detail::MetalTextureResource>(texture.resource());
}

bool select_current_ui(const FrameSubmission& submission) {
    switch (submission.hud_options.ui_source) {
    case UiTemporalSource::previous_frame: return false;
    case UiTemporalSource::current_frame: return true;
    case UiTemporalSource::nearest_presentation: break;
    }
    const auto endpoint_time = [](const FrameTiming& timing) {
        return timing.desired_presentation_timestamp_ns != unknown_timestamp_ns
            ? timing.desired_presentation_timestamp_ns : timing.timestamp_ns;
    };
    std::int64_t target{};
    if (submission.desired_presentation_timestamp_ns != unknown_timestamp_ns) {
        target = submission.desired_presentation_timestamp_ns;
    } else if (submission.interpolation_timestamp_ns != unknown_timestamp_ns) {
        target = submission.interpolation_timestamp_ns;
    } else {
        // The target is defined by interpolation itself here, so comparing
        // its fraction avoids converting large epoch timestamps to floating
        // point and losing nanosecond distinctions.
        return submission.interpolation >= 0.5F;
    }

    const auto distance = [](std::int64_t left, std::int64_t right) {
        const auto left_unsigned = static_cast<std::uint64_t>(left);
        const auto right_unsigned = static_cast<std::uint64_t>(right);
        return left <= right ? right_unsigned - left_unsigned
                             : left_unsigned - right_unsigned;
    };
    const auto previous_distance = distance(
        target, endpoint_time(submission.previous.timing));
    const auto current_distance = distance(
        target, endpoint_time(submission.current.timing));
    // An exact tie selects the newer source frame to keep UI state responsive.
    return current_distance <= previous_distance;
}

std::optional<bool> transfer_is_srgb(
    const TextureDescriptor& descriptor, const FrameColorMetadata& metadata) {
    if (metadata.transfer_function == TransferFunction::srgb &&
        descriptor.color_space == ColorSpace::srgb) {
        return true;
    }
    if (metadata.transfer_function == TransferFunction::linear &&
        descriptor.color_space == ColorSpace::linear_srgb) {
        return false;
    }
    if (metadata.transfer_function != TransferFunction::unknown) {
        return std::nullopt;
    }
    if (descriptor.color_space == ColorSpace::srgb) return true;
    if (descriptor.color_space == ColorSpace::linear_srgb) return false;
    return std::nullopt;
}

bool is_sdr_or_unspecified(const FrameColorMetadata& metadata) {
    return metadata.dynamic_range == DynamicRange::unknown ||
        metadata.dynamic_range == DynamicRange::sdr;
}

bool same_texture_descriptor(const TextureDescriptor& left,
                             const TextureDescriptor& right) {
    return left.width == right.width && left.height == right.height &&
        left.format == right.format && left.color_space == right.color_space &&
        left.alpha_mode == right.alpha_mode;
}

bool same_color_metadata(const FrameColorMetadata& left,
                         const FrameColorMetadata& right) {
    return left.transfer_function == right.transfer_function &&
        left.dynamic_range == right.dynamic_range;
}

std::shared_ptr<detail::MetalTextureResource> make_mask_texture(
    id<MTLDevice> device, std::uint32_t width, std::uint32_t height,
    std::uint64_t device_id) {
    MTLTextureDescriptor* descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR8Unorm
                                                           width:width
                                                          height:height
                                                       mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> texture = [device newTextureWithDescriptor:descriptor];
    if (texture == nil) return {};
    return std::make_shared<detail::MetalTextureResource>(
        device, texture,
        TextureDescriptor{width, height, PixelFormat::r8_unorm,
                          ColorSpace::unknown, AlphaMode::opaque},
        device_id);
}

std::shared_ptr<detail::MetalTextureResource> make_temporal_history_texture(
    id<MTLDevice> device, const TextureDescriptor& source,
    std::uint64_t device_id) {
    MTLTextureDescriptor* descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:source.width
                                                          height:source.height
                                                       mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> texture = [device newTextureWithDescriptor:descriptor];
    if (texture == nil) return {};
    return std::make_shared<detail::MetalTextureResource>(
        device, texture,
        TextureDescriptor{source.width, source.height, PixelFormat::rgba8_unorm,
                          source.color_space, AlphaMode::opaque},
        device_id);
}

struct TemporalCutTileStats {
    std::uint32_t histogram_a[16];
    std::uint32_t histogram_b[16];
    float difference_sum;
    float generated_residual_sum;
    std::uint32_t changed_count;
    std::uint32_t sample_count;
};

struct TemporalCutSummary {
    float histogram_difference;
    float downsampled_difference;
    float generated_residual;
    float changed_fraction;
    std::uint32_t scene_cut;
};

static_assert(sizeof(TemporalCutTileStats) == 144);
static_assert(sizeof(TemporalCutSummary) == 20);

struct TemporalCutTileParameters {
    std::uint32_t groups_x;
    std::uint32_t width;
    std::uint32_t height;
    float interpolation;
};

struct TemporalCutReduceParameters {
    std::uint32_t tile_count;
    std::uint32_t force_reset;
};

struct TemporalQualityParameters {
    float interpolation;
    float history_time_scale;
    std::uint32_t policy;
    std::uint32_t debug_visualization;
    std::uint32_t force_reset;
    std::uint32_t force_real_fallback;
    std::uint32_t advance_history;
    std::uint32_t reuse_cached_output;
    std::uint32_t current_source;
    std::uint32_t scene_is_srgb;
    std::uint32_t has_hud_mask;
};

bool temporal_quality_active(const TemporalQualityOptions& options) {
    return options.policy != TemporalQualityPolicy::disabled ||
        options.debug_visualization != TemporalQualityDebugVisualization::disabled;
}

bool interpolation_matches_timestamp(const FrameSubmission& submission) {
    if (submission.interpolation_timestamp_ns == unknown_timestamp_ns) return true;
    const auto previous = submission.previous.timing.timestamp_ns;
    const auto current = submission.current.timing.timestamp_ns;
    const auto target = submission.interpolation_timestamp_ns;
    if (previous == unknown_timestamp_ns || current == unknown_timestamp_ns ||
        target <= previous || target >= current) {
        return false;
    }
    const auto interval = static_cast<std::uint64_t>(current) -
        static_cast<std::uint64_t>(previous);
    const auto offset = static_cast<std::uint64_t>(target) -
        static_cast<std::uint64_t>(previous);
    const long double timestamp_fraction = static_cast<long double>(offset) /
        static_cast<long double>(interval);
    return std::abs(timestamp_fraction -
        static_cast<long double>(submission.interpolation)) <= 1.0e-6L;
}

std::uint32_t temporal_policy_value(TemporalQualityPolicy policy) {
    switch (policy) {
    case TemporalQualityPolicy::continuous_endpoint_blend: return 1;
    case TemporalQualityPolicy::continuous_source_blend: return 2;
    case TemporalQualityPolicy::nearest_endpoint_fallback: return 3;
    default: return 0;
    }
}

std::uint32_t temporal_debug_value(TemporalQualityDebugVisualization visualization) {
    switch (visualization) {
    case TemporalQualityDebugVisualization::confidence: return 1;
    case TemporalQualityDebugVisualization::disocclusion: return 2;
    case TemporalQualityDebugVisualization::unstable_thin_features: return 3;
    case TemporalQualityDebugVisualization::high_frequency_texture: return 4;
    case TemporalQualityDebugVisualization::specular_or_particles: return 5;
    case TemporalQualityDebugVisualization::scene_cut: return 6;
    case TemporalQualityDebugVisualization::confidence_classes: return 7;
    default: return 0;
    }
}

bool select_temporal_current_source(const FrameSubmission& submission) {
    if (submission.interpolation_timestamp_ns == unknown_timestamp_ns) {
        return submission.interpolation >= 0.5F;
    }
    const auto previous_timestamp = submission.previous.timing.timestamp_ns;
    const auto current_timestamp = submission.current.timing.timestamp_ns;
    const auto target = submission.interpolation_timestamp_ns;
    const auto absolute_distance = [](std::int64_t left, std::int64_t right) {
        const auto a = static_cast<std::uint64_t>(left);
        const auto b = static_cast<std::uint64_t>(right);
        return left <= right ? b - a : a - b;
    };
    const auto current_distance = absolute_distance(target, current_timestamp);
    const auto previous_distance = absolute_distance(target, previous_timestamp);
    // Integer nanosecond conversion can put a mathematical midpoint one tick
    // closer to A on alternating 60 Hz pairs. Keep midpoint fallback phase
    // canonical so it cannot crawl between the two source samples.
    return current_distance <= previous_distance ||
        current_distance - previous_distance <= 1;
}

id<MTLComputePipelineState> make_compute_pipeline(
    id<MTLDevice> device, const char* source, const char* function_name,
    std::string& failure) {
    NSError* error = nil;
    NSString* shader_source = [NSString stringWithUTF8String:source];
    id<MTLLibrary> library = [device newLibraryWithSource:shader_source
                                                  options:nil
                                                    error:&error];
    if (library == nil) {
        failure = error_message(error, "Metal shader compilation failed");
        return nil;
    }
    NSString* function_name_string = [NSString stringWithUTF8String:function_name];
    id<MTLFunction> function = [library newFunctionWithName:function_name_string];
    if (function == nil) {
        failure = std::string("Metal function not found: ") + function_name;
        return nil;
    }
    id<MTLComputePipelineState> pipeline =
        [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
        failure = error_message(error, "Metal pipeline creation failed");
        return nil;
    }
    return pipeline;
}

class MetalEventSyncPoint {
public:
    virtual ~MetalEventSyncPoint() = default;
    [[nodiscard]] virtual id<MTLSharedEvent> event() const noexcept = 0;
    [[nodiscard]] virtual std::uint64_t value() const noexcept = 0;
};

class MetalEventDependency final : public GpuSyncPoint, public MetalEventSyncPoint {
public:
    MetalEventDependency(id<MTLSharedEvent> event, std::uint64_t value,
                         std::uint64_t device_id)
        : event_(event), value_(value), device_id_(device_id) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_id_;
    }
    [[nodiscard]] std::optional<bool> is_signaled() const noexcept override {
        return event_.signaledValue >= value_;
    }
    [[nodiscard]] id<MTLSharedEvent> event() const noexcept override { return event_; }
    [[nodiscard]] std::uint64_t value() const noexcept override { return value_; }

private:
    __strong id<MTLSharedEvent> event_;
    std::uint64_t value_;
    std::uint64_t device_id_;
};

class MetalCompletion final : public GpuCompletion, public MetalEventSyncPoint {
public:
    MetalCompletion(id<MTLCommandBuffer> first_command_buffer,
                    id<MTLCommandBuffer> completion_command_buffer,
                    id<MTLSharedEvent> event, std::uint64_t value,
                    std::uint64_t device_id,
                    std::shared_ptr<TextureResource> previous,
                    std::shared_ptr<TextureResource> current,
                    std::shared_ptr<TextureResource> output,
                    std::vector<std::shared_ptr<TextureResource>> auxiliary,
                    std::vector<std::shared_ptr<const GpuSyncPoint>> dependencies)
        : first_command_buffer_(first_command_buffer),
          command_buffer_(completion_command_buffer), event_(event), value_(value),
          device_id_(device_id), previous_(std::move(previous)),
          current_(std::move(current)), output_(std::move(output)),
          auxiliary_(std::move(auxiliary)),
          dependencies_(std::move(dependencies)) {}

    [[nodiscard]] std::string_view backend_id() const noexcept override {
        return kBackendId;
    }
    [[nodiscard]] std::uint64_t device_id() const noexcept override {
        return device_id_;
    }
    [[nodiscard]] id<MTLSharedEvent> event() const noexcept override { return event_; }
    [[nodiscard]] std::uint64_t value() const noexcept override { return value_; }
    [[nodiscard]] bool is_complete() const noexcept override {
        return poll_status() != GpuCompletionStatus::pending;
    }
    [[nodiscard]] GpuCompletionStatus poll_status() const noexcept override {
        if (command_buffer_.status == MTLCommandBufferStatusError) {
            return GpuCompletionStatus::failed;
        }
        return command_buffer_.status == MTLCommandBufferStatusCompleted
            ? GpuCompletionStatus::complete : GpuCompletionStatus::pending;
    }

    [[nodiscard]] std::optional<std::uint64_t>
    gpu_execution_time_ns() const noexcept override {
        const auto range = gpu_time_range_ns();
        if (!range || range->second < range->first) {
            return std::nullopt;
        }
        const __int128 elapsed = static_cast<__int128>(range->second) -
                                 static_cast<__int128>(range->first);
        if (elapsed > static_cast<__int128>(
                          std::numeric_limits<std::uint64_t>::max())) {
            return std::nullopt;
        }
        return static_cast<std::uint64_t>(elapsed);
    }

    [[nodiscard]] std::optional<std::pair<std::int64_t, std::int64_t>>
    gpu_time_range_ns() const noexcept override {
        // MPSGraph may commit and continue across multiple command buffers.
        // The first and final GPU timestamps span every ordered segment.
        const NSTimeInterval started = first_command_buffer_.GPUStartTime;
        const NSTimeInterval ended = command_buffer_.GPUEndTime;
        if (!std::isfinite(started) || !std::isfinite(ended) ||
            started <= 0.0 || ended < started) {
            return std::nullopt;
        }
        const long double started_ns =
            static_cast<long double>(started) * 1'000'000'000.0L;
        const long double ended_ns =
            static_cast<long double>(ended) * 1'000'000'000.0L;
        const auto minimum = static_cast<long double>(
            std::numeric_limits<std::int64_t>::min() + 1);
        const auto maximum = static_cast<long double>(
            std::numeric_limits<std::int64_t>::max());
        if (started_ns < minimum || ended_ns > maximum) {
            return std::nullopt;
        }
        return std::pair{
            static_cast<std::int64_t>(started_ns),
            static_cast<std::int64_t>(ended_ns),
        };
    }

    [[nodiscard]] Result<void> wait() override {
        [command_buffer_ waitUntilCompleted];
        if (command_buffer_.status == MTLCommandBufferStatusError) {
            return std::unexpected(make_error(
                ErrorCode::backend_failure,
                error_message(command_buffer_.error, "Metal command buffer failed")));
        }
        return {};
    }

private:
    __strong id<MTLCommandBuffer> first_command_buffer_;
    __strong id<MTLCommandBuffer> command_buffer_;
    __strong id<MTLSharedEvent> event_;
    std::uint64_t value_;
    std::uint64_t device_id_;
    std::shared_ptr<TextureResource> previous_;
    std::shared_ptr<TextureResource> current_;
    std::shared_ptr<TextureResource> output_;
    std::vector<std::shared_ptr<TextureResource>> auxiliary_;
    std::vector<std::shared_ptr<const GpuSyncPoint>> dependencies_;
};

} // namespace

namespace detail {

std::filesystem::path rife_model_weights_path() {
    return model_weights_path();
}

bool rife_runtime_available() noexcept {
    try {
        std::error_code filesystem_error;
        if (!std::filesystem::is_regular_file(rife_model_weights_path(), filesystem_error) ||
            filesystem_error) {
            return false;
        }
        const char* configured = std::getenv("FRAMEGEN_METAL_MODEL_VARIANT");
        if (configured != nullptr && configured[0] != '\0' &&
            std::string_view(configured) != "QUALITY" &&
            std::string_view(configured) != "BALANCED") {
            return false;
        }
        return MTLCreateSystemDefaultDevice() != nil;
    } catch (...) {
        return false;
    }
}

MetalTextureResource::MetalTextureResource(id<MTLDevice> device,
                                           id<MTLTexture> texture,
                                           TextureDescriptor descriptor,
                                           std::uint64_t device_id,
                                           const void* resource_identity)
    : device_(device), texture_(texture), descriptor_(descriptor),
      device_id_(device_id), resource_identity_(resource_identity) {}

TextureDescriptor MetalTextureResource::descriptor() const noexcept {
    return descriptor_;
}

std::string_view MetalTextureResource::backend_id() const noexcept {
    return kBackendId;
}

std::uint64_t MetalTextureResource::device_id() const noexcept {
    return device_id_;
}

id<MTLTexture> MetalTextureResource::native_texture() const noexcept {
    return texture_;
}

bool MetalTextureResource::belongs_to(id<MTLDevice> device) const noexcept {
    return device_ == device;
}

const void* MetalTextureResource::resource_identity() const noexcept {
    return resource_identity_ != nullptr
        ? resource_identity_ : (__bridge const void*)texture_;
}

MetalBackend::MetalBackend(id<MTLDevice> device, id<MTLCommandQueue> queue,
                           id<MTLComputePipelineState> explicit_ui_pipeline,
                           id<MTLComputePipelineState> automatic_hud_pipeline,
                           id<MTLComputePipelineState> temporal_cut_tiles_pipeline,
                           id<MTLComputePipelineState> temporal_cut_reduce_pipeline,
                           id<MTLComputePipelineState> temporal_quality_pipeline,
                           id<MTLComputePipelineState> temporal_history_commit_pipeline,
                           id<MTLSharedEvent> completion_event,
                           std::shared_ptr<MetalRifeModel> rife_model,
                           RifeMode rife_mode,
                           std::uint64_t device_id)
    : device_(device), queue_(queue),
      explicit_ui_pipeline_(explicit_ui_pipeline),
      automatic_hud_pipeline_(automatic_hud_pipeline),
      temporal_cut_tiles_pipeline_(temporal_cut_tiles_pipeline),
      temporal_cut_reduce_pipeline_(temporal_cut_reduce_pipeline),
      temporal_quality_pipeline_(temporal_quality_pipeline),
      temporal_history_commit_pipeline_(temporal_history_commit_pipeline),
      completion_event_(completion_event), rife_model_(std::move(rife_model)),
      rife_mode_(rife_mode), device_id_(device_id),
      backend_identity_(std::make_shared<const std::uint8_t>(0)) {}

std::string_view MetalBackend::backend_id() const noexcept {
    return kBackendId;
}

std::uint64_t MetalBackend::device_id() const noexcept {
    return device_id_;
}

id<MTLDevice> MetalBackend::native_device() const noexcept {
    return device_;
}

std::shared_ptr<BackendStreamState> MetalBackend::create_stream_state() {
    auto stream_state = std::make_shared<MetalBackendStreamState>();
    stream_state->device_id = device_id_;
    stream_state->backend_identity = backend_identity_;
    return stream_state;
}

Result<GeneratedFrame> MetalBackend::submit(const FrameSubmission& submission) {
    const auto& previous = submission.previous.texture;
    const auto& current = submission.current.texture;
    if (!previous || !current) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "both Metal input textures must be valid"));
    }
    if (!std::isfinite(submission.interpolation) ||
        submission.interpolation < 0.0F || submission.interpolation > 1.0F) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "interpolation must be finite and in [0, 1]"));
    }
    if (previous.backend_id() != kBackendId || current.backend_id() != kBackendId ||
        previous.device_id() != device_id_ || current.device_id() != device_id_) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal textures belong to another backend device"));
    }

    auto previous_resource = as_metal_resource(previous);
    auto current_resource = as_metal_resource(current);
    if (!previous_resource || !current_resource ||
        !previous_resource->belongs_to(device_) || !current_resource->belongs_to(device_)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal texture resources are not owned by this device"));
    }

    auto stream_state = std::dynamic_pointer_cast<MetalBackendStreamState>(
        submission.backend_stream_state);
    const bool temporal_requested = temporal_quality_active(submission.temporal_quality);
    if ((submission.hud_options.mode == HudMode::automatic_protection || temporal_requested) &&
        !stream_state) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "temporal Metal quality features require a backend stream state; submit through FrameGenerator or provide one"));
    }
    if (!stream_state) {
        stream_state = std::make_shared<MetalBackendStreamState>();
        stream_state->device_id = device_id_;
        stream_state->backend_identity = backend_identity_;
    } else if (stream_state->device_id != device_id_ ||
               stream_state->backend_identity != backend_identity_) {
        return std::unexpected(make_error(
            ErrorCode::incompatible_resource,
            "Metal backend stream state belongs to another backend instance or device"));
    }
    const auto invalidate_temporal_on_rejection = [&] {
        if (temporal_requested) {
            stream_state->temporal_async_reset_requested.store(
                true, std::memory_order_release);
        }
    };
    if (temporal_requested) {
        const auto& previous_timing = submission.previous.timing;
        const auto& current_timing = submission.current.timing;
        if (previous_timing.sequence >= current_timing.sequence ||
            previous_timing.timestamp_ns == unknown_timestamp_ns ||
            current_timing.timestamp_ns == unknown_timestamp_ns ||
            previous_timing.timestamp_ns >= current_timing.timestamp_ns ||
            previous_timing.clock_domain == 0 ||
            previous_timing.clock_domain != current_timing.clock_domain) {
            invalidate_temporal_on_rejection();
            return std::unexpected(make_error(ErrorCode::invalid_argument,
                "temporal quality requires ordered source timing in one known clock domain"));
        }
    }
    if (!interpolation_matches_timestamp(submission)) {
        invalidate_temporal_on_rejection();
        return std::unexpected(make_error(ErrorCode::invalid_argument,
            "interpolation timestamp must be strictly bracketed and match the interpolation fraction"));
    }

    const auto previous_descriptor = previous.descriptor();
    const auto current_descriptor = current.descriptor();
    if (previous_descriptor.width != current_descriptor.width ||
        previous_descriptor.height != current_descriptor.height ||
        previous_descriptor.format != current_descriptor.format ||
        previous_descriptor.color_space != current_descriptor.color_space ||
        previous_descriptor.alpha_mode != current_descriptor.alpha_mode) {
        invalidate_temporal_on_rejection();
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal input texture descriptions must match"));
    }
    const auto previous_srgb = transfer_is_srgb(
        previous_descriptor, submission.previous.color_metadata);
    const auto current_srgb = transfer_is_srgb(
        current_descriptor, submission.current.color_metadata);
    if (previous_descriptor.format != PixelFormat::rgba8_unorm ||
        (previous_descriptor.color_space != ColorSpace::srgb &&
         previous_descriptor.color_space != ColorSpace::linear_srgb) ||
        previous_descriptor.alpha_mode != AlphaMode::opaque ||
        !previous_srgb || !current_srgb || *previous_srgb != *current_srgb ||
        !is_sdr_or_unspecified(submission.previous.color_metadata) ||
        !is_sdr_or_unspecified(submission.current.color_metadata)) {
        invalidate_temporal_on_rejection();
        return std::unexpected(make_error(ErrorCode::unsupported_format,
            "RIFE requires matching RGBA8 opaque sRGB or linear-sRGB SDR textures"));
    }

    auto dimensions_valid = MetalRifeModel::validate_dimensions(
        previous_descriptor.width, previous_descriptor.height, rife_mode_);
    if (!dimensions_valid) {
        invalidate_temporal_on_rejection();
        return std::unexpected(dimensions_valid.error());
    }

    id<MTLTexture> previous_native = previous_resource->native_texture();
    id<MTLTexture> current_native = current_resource->native_texture();
    if ((previous_native.usage & MTLTextureUsageShaderRead) == 0 ||
        (current_native.usage & MTLTextureUsageShaderRead) == 0) {
        invalidate_temporal_on_rejection();
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal input textures must allow shader reads"));
    }

    auto& automatic_history_mutex_ = stream_state->automatic_history_mutex;
    auto& automatic_mask_history_ = stream_state->automatic_mask_history;
    auto& automatic_mask_width_ = stream_state->automatic_mask_width;
    auto& automatic_mask_height_ = stream_state->automatic_mask_height;
    auto& automatic_mask_pair_descriptor_ = stream_state->automatic_mask_pair_descriptor;
    auto& automatic_mask_pair_previous_color_metadata_ =
        stream_state->automatic_mask_pair_previous_color_metadata;
    auto& automatic_mask_pair_current_color_metadata_ =
        stream_state->automatic_mask_pair_current_color_metadata;
    auto& automatic_mask_history_index_ = stream_state->automatic_mask_history_index;
    auto& automatic_mask_history_valid_ = stream_state->automatic_mask_history_valid;
    auto& automatic_mask_pair_has_prior_history_ =
        stream_state->automatic_mask_pair_has_prior_history;
    auto& automatic_mask_pair_clock_domain_ = stream_state->automatic_mask_pair_clock_domain;
    auto& automatic_mask_pair_previous_sequence_ =
        stream_state->automatic_mask_pair_previous_sequence;
    auto& automatic_mask_pair_current_sequence_ =
        stream_state->automatic_mask_pair_current_sequence;
    auto& automatic_mask_pair_previous_timestamp_ns_ =
        stream_state->automatic_mask_pair_previous_timestamp_ns;
    auto& automatic_mask_pair_current_timestamp_ns_ =
        stream_state->automatic_mask_pair_current_timestamp_ns;
    auto& automatic_mask_pair_engagement_alpha_ =
        stream_state->automatic_mask_pair_engagement_alpha;
    auto& automatic_mask_pair_release_alpha_ =
        stream_state->automatic_mask_pair_release_alpha;
    auto& last_hud_mode_ = stream_state->last_hud_mode;
    std::unique_lock history_lock(automatic_history_mutex_);
    const bool asynchronous_temporal_reset =
        stream_state->temporal_async_reset_requested.exchange(
            false, std::memory_order_acq_rel);
    auto temporal_plan = stream_state->temporal_controller.begin(
        submission, previous_descriptor, asynchronous_temporal_reset);
    if (temporal_plan.stale_submission && asynchronous_temporal_reset) {
        // An out-of-order request is rendered without reusable temporal state,
        // but it must not consume a reset needed by the newest timeline.
        stream_state->temporal_async_reset_requested.store(
            true, std::memory_order_release);
    }
    bool temporal_force_reset = temporal_plan.force_reset;
    bool temporal_resources_rebuilt = false;
    std::uint32_t temporal_write_index = 1U - stream_state->temporal_history_index;
    if (temporal_plan.enabled) {
        const bool descriptor_changed = stream_state->temporal_history[0] &&
            (!same_texture_descriptor(previous_descriptor,
                                      stream_state->temporal_history_descriptor) ||
             !same_color_metadata(submission.current.color_metadata,
                                  stream_state->temporal_history_color_metadata));
        const bool dimensions_changed = stream_state->temporal_history_width !=
                previous_descriptor.width ||
            stream_state->temporal_history_height != previous_descriptor.height;
        const std::uint32_t group_count_x = (previous_descriptor.width + 63U) / 64U;
        const std::uint32_t group_count_y = (previous_descriptor.height + 63U) / 64U;
        const std::uint64_t tile_count = static_cast<std::uint64_t>(group_count_x) * group_count_y;
        if (tile_count == 0 ||
            tile_count > std::numeric_limits<std::uint32_t>::max() || tile_count >
                std::numeric_limits<NSUInteger>::max() / sizeof(TemporalCutTileStats)) {
            return std::unexpected(make_error(ErrorCode::allocation_failure,
                "temporal scene-cut statistics exceed Metal buffer limits"));
        }
        const bool buffers_changed =
            !stream_state->temporal_cut_tile_buffer ||
            !stream_state->temporal_cut_summary_buffer ||
            !stream_state->temporal_history_valid_buffer ||
            stream_state->temporal_cut_group_count_x != group_count_x ||
            stream_state->temporal_cut_group_count_y != group_count_y;
        temporal_resources_rebuilt = !stream_state->temporal_history[0] ||
            !stream_state->temporal_history[1] || descriptor_changed ||
            dimensions_changed || buffers_changed;
        if (temporal_resources_rebuilt) {
            auto first = make_temporal_history_texture(
                device_, previous_descriptor, device_id_);
            auto second = make_temporal_history_texture(
                device_, previous_descriptor, device_id_);
            id<MTLBuffer> tile_buffer = [device_ newBufferWithLength:
                static_cast<NSUInteger>(tile_count * sizeof(TemporalCutTileStats))
                options:MTLResourceStorageModePrivate];
            id<MTLBuffer> summary_buffer = [device_ newBufferWithLength:
                sizeof(TemporalCutSummary) options:MTLResourceStorageModePrivate];
            id<MTLBuffer> history_valid_buffer = [device_ newBufferWithLength:
                sizeof(std::uint32_t) options:MTLResourceStorageModePrivate];
            if (!first || !second || tile_buffer == nil || summary_buffer == nil ||
                history_valid_buffer == nil) {
                return std::unexpected(make_error(ErrorCode::allocation_failure,
                    "Metal could not allocate temporal quality history or scene-cut buffers"));
            }
            stream_state->temporal_history[0] = std::move(first);
            stream_state->temporal_history[1] = std::move(second);
            stream_state->temporal_history_descriptor = previous_descriptor;
            stream_state->temporal_history_color_metadata = submission.current.color_metadata;
            stream_state->temporal_history_width = previous_descriptor.width;
            stream_state->temporal_history_height = previous_descriptor.height;
            stream_state->temporal_history_index = 0;
            stream_state->temporal_cut_tile_buffer = tile_buffer;
            stream_state->temporal_cut_summary_buffer = summary_buffer;
            stream_state->temporal_history_valid_buffer = history_valid_buffer;
            stream_state->temporal_cut_group_count_x = group_count_x;
            stream_state->temporal_cut_group_count_y = group_count_y;
            temporal_write_index = 1;
        }
        temporal_force_reset = temporal_force_reset || temporal_resources_rebuilt;
    }
    const bool temporal_gpu_reset = temporal_plan.enabled &&
        (temporal_force_reset ||
         (!temporal_plan.use_history && !temporal_plan.reuse_cached_output));
    const bool current_ui = select_current_ui(submission);
    std::shared_ptr<MetalTextureResource> selected_ui_resource;
    id<MTLTexture> previous_ui_native = nil;
    id<MTLTexture> current_ui_native = nil;
    std::shared_ptr<MetalTextureResource> temporal_hud_mask_resource;
    id<MTLTexture> temporal_hud_mask_native = nil;
    std::vector<std::shared_ptr<TextureResource>> auxiliary_resources;

    if (submission.hud_options.mode == HudMode::explicit_ui_plane) {
        const auto& previous_ui = submission.previous.optional_inputs.ui_texture;
        const auto& current_ui_texture = submission.current.optional_inputs.ui_texture;
        if (!previous_ui || !current_ui_texture) {
            return std::unexpected(make_error(
                ErrorCode::invalid_argument,
                "explicit UI-plane mode requires a UI texture on both source frames"));
        }
        const auto previous_ui_descriptor = previous_ui.descriptor();
        const auto current_ui_descriptor = current_ui_texture.descriptor();
        const auto previous_scene_srgb = transfer_is_srgb(
            previous_descriptor, submission.previous.color_metadata);
        const auto current_scene_srgb = transfer_is_srgb(
            current_descriptor, submission.current.color_metadata);
        const auto previous_ui_srgb = transfer_is_srgb(
            previous_ui_descriptor, submission.previous.optional_inputs.ui_color_metadata);
        const auto current_ui_srgb = transfer_is_srgb(
            current_ui_descriptor, submission.current.optional_inputs.ui_color_metadata);
        const auto valid_ui_descriptor = [&](const TextureDescriptor& descriptor) {
            return descriptor.width == previous_descriptor.width &&
                descriptor.height == previous_descriptor.height &&
                descriptor.format == PixelFormat::rgba8_unorm &&
                (descriptor.color_space == ColorSpace::srgb ||
                 descriptor.color_space == ColorSpace::linear_srgb) &&
                descriptor.alpha_mode != AlphaMode::unknown;
        };
        if (!valid_ui_descriptor(previous_ui_descriptor) ||
            !valid_ui_descriptor(current_ui_descriptor) ||
            previous_ui_descriptor.alpha_mode != current_ui_descriptor.alpha_mode ||
            !previous_scene_srgb || !current_scene_srgb ||
            *previous_scene_srgb != *current_scene_srgb ||
            !previous_ui_srgb || !current_ui_srgb ||
            !is_sdr_or_unspecified(submission.previous.color_metadata) ||
            !is_sdr_or_unspecified(submission.current.color_metadata) ||
            !is_sdr_or_unspecified(submission.previous.optional_inputs.ui_color_metadata) ||
            !is_sdr_or_unspecified(submission.current.optional_inputs.ui_color_metadata) ||
            previous_descriptor.alpha_mode != AlphaMode::opaque) {
            return std::unexpected(make_error(
                ErrorCode::unsupported_format,
                "explicit UI composition requires opaque sRGB/linear-sRGB scene color and matching RGBA8 SDR UI planes with declared alpha"));
        }
        if (previous_ui.resource_identity() == previous.resource_identity() ||
            previous_ui.resource_identity() == current.resource_identity() ||
            current_ui_texture.resource_identity() == previous.resource_identity() ||
            current_ui_texture.resource_identity() == current.resource_identity()) {
            return std::unexpected(make_error(
                ErrorCode::incompatible_resource,
                "scene color and explicit UI plane must use distinct texture storage"));
        }
        auto previous_ui_resource = as_metal_resource(previous_ui);
        auto current_ui_resource = as_metal_resource(current_ui_texture);
        if (!previous_ui_resource || !current_ui_resource ||
            !previous_ui_resource->belongs_to(device_) ||
            !current_ui_resource->belongs_to(device_)) {
            return std::unexpected(make_error(
                ErrorCode::incompatible_resource,
                "explicit UI plane is not owned by this Metal device"));
        }
        previous_ui_native = previous_ui_resource->native_texture();
        current_ui_native = current_ui_resource->native_texture();
        if ((previous_ui_native.usage & MTLTextureUsageShaderRead) == 0 ||
            (current_ui_native.usage & MTLTextureUsageShaderRead) == 0) {
            return std::unexpected(make_error(
                ErrorCode::unsupported_format,
                "Metal UI textures must allow shader reads"));
        }
        if (previous_ui_native == previous_native ||
            previous_ui_native == current_native ||
            current_ui_native == previous_native ||
            current_ui_native == current_native) {
            return std::unexpected(make_error(
                ErrorCode::incompatible_resource,
                "scene color and explicit UI plane must not alias the same Metal texture"));
        }
        selected_ui_resource = current_ui ? current_ui_resource : previous_ui_resource;
        // Both endpoints are bound to the command buffer, even though only one
        // is selected per pixel. Retain both through completion for direct C++
        // callers whose FrameSubmission may go out of scope immediately.
        auxiliary_resources.push_back(previous_ui_resource);
        auxiliary_resources.push_back(current_ui_resource);
    }

    const auto& previous_hud_mask = submission.previous.optional_inputs.ui_mask;
    const auto& current_hud_mask = submission.current.optional_inputs.ui_mask;
    if (temporal_plan.enabled && (previous_hud_mask || current_hud_mask)) {
        const Texture* selected_mask = current_ui
            ? (current_hud_mask ? &current_hud_mask : &previous_hud_mask)
            : (previous_hud_mask ? &previous_hud_mask : &current_hud_mask);
        if (!selected_mask || !*selected_mask) {
            return std::unexpected(make_error(ErrorCode::invalid_argument,
                "temporal HUD-mask selection did not produce a valid texture"));
        }
        const auto mask_descriptor = selected_mask->descriptor();
        if (mask_descriptor.width != previous_descriptor.width ||
            mask_descriptor.height != previous_descriptor.height ||
            mask_descriptor.format != PixelFormat::r8_unorm) {
            return std::unexpected(make_error(ErrorCode::unsupported_format,
                "temporal HUD masks must be matching single-channel R8 textures"));
        }
        temporal_hud_mask_resource = as_metal_resource(*selected_mask);
        if (!temporal_hud_mask_resource ||
            !temporal_hud_mask_resource->belongs_to(device_)) {
            return std::unexpected(make_error(ErrorCode::incompatible_resource,
                "temporal HUD mask is not owned by this Metal device"));
        }
        temporal_hud_mask_native = temporal_hud_mask_resource->native_texture();
        if ((temporal_hud_mask_native.usage & MTLTextureUsageShaderRead) == 0) {
            return std::unexpected(make_error(ErrorCode::unsupported_format,
                "Metal temporal HUD masks must allow shader reads"));
        }
        auxiliary_resources.push_back(temporal_hud_mask_resource);
    }

    bool has_mask_history{};
    std::uint32_t history_write_index{};
    std::shared_ptr<MetalTextureResource> history_read_resource;
    std::shared_ptr<MetalTextureResource> history_write_resource;
    bool advance_mask_history{};
    float mask_engagement_alpha{1.0F};
    float mask_release_alpha{1.0F};
    const bool automatic_hud_reset_history = submission.reset_history || temporal_gpu_reset;
    if (submission.hud_options.mode == HudMode::automatic_protection) {
        const auto previous_sequence = submission.previous.timing.sequence;
        const auto current_sequence = submission.current.timing.sequence;
        const auto previous_timestamp = submission.previous.timing.timestamp_ns;
        const auto current_timestamp = submission.current.timing.timestamp_ns;
        const auto clock_domain = submission.current.timing.clock_domain;
        const auto previous_color_metadata = submission.previous.color_metadata;
        const auto current_color_metadata = submission.current.color_metadata;
        if (current_sequence <= previous_sequence ||
            previous_timestamp == unknown_timestamp_ns ||
            current_timestamp == unknown_timestamp_ns ||
            current_timestamp <= previous_timestamp || clock_domain == 0 ||
            submission.previous.timing.clock_domain != clock_domain) {
            return std::unexpected(make_error(
                ErrorCode::invalid_argument,
                "automatic HUD protection requires ordered source timing in one clock domain"));
        }
        if (!same_color_metadata(previous_color_metadata, current_color_metadata)) {
            return std::unexpected(make_error(
                ErrorCode::unsupported_format,
                "automatic HUD protection requires matching source color metadata"));
        }
        const bool dimensions_changed = automatic_mask_width_ != previous_descriptor.width ||
            automatic_mask_height_ != previous_descriptor.height;
        if (dimensions_changed || !automatic_mask_history_[0] ||
            !automatic_mask_history_[1] || !automatic_mask_history_[2]) {
            auto first = make_mask_texture(device_, previous_descriptor.width,
                                           previous_descriptor.height, device_id_);
            auto second = make_mask_texture(device_, previous_descriptor.width,
                                            previous_descriptor.height, device_id_);
            auto scratch = make_mask_texture(device_, previous_descriptor.width,
                                             previous_descriptor.height, device_id_);
            if (!first || !second || !scratch) {
                return std::unexpected(make_error(
                    ErrorCode::allocation_failure,
                    "Metal could not allocate automatic HUD mask history"));
            }
            automatic_mask_history_[0] = std::move(first);
            automatic_mask_history_[1] = std::move(second);
            automatic_mask_history_[2] = std::move(scratch);
            automatic_mask_width_ = previous_descriptor.width;
            automatic_mask_height_ = previous_descriptor.height;
            automatic_mask_history_index_ = 0;
            automatic_mask_history_valid_ = false;
            automatic_mask_pair_has_prior_history_ = false;
        }

        const bool previous_history_valid = automatic_mask_history_valid_ &&
            last_hud_mode_ == HudMode::automatic_protection && !dimensions_changed;
        const bool same_source_pair = previous_history_valid &&
            clock_domain == automatic_mask_pair_clock_domain_ &&
            previous_sequence == automatic_mask_pair_previous_sequence_ &&
            current_sequence == automatic_mask_pair_current_sequence_ &&
            previous_timestamp == automatic_mask_pair_previous_timestamp_ns_ &&
            current_timestamp == automatic_mask_pair_current_timestamp_ns_ &&
            same_texture_descriptor(previous_descriptor,
                                    automatic_mask_pair_descriptor_) &&
            same_color_metadata(previous_color_metadata,
                                automatic_mask_pair_previous_color_metadata_) &&
            same_color_metadata(current_color_metadata,
                                automatic_mask_pair_current_color_metadata_);
        const bool follows_source_pair = previous_history_valid &&
            clock_domain == automatic_mask_pair_clock_domain_ &&
            previous_sequence == automatic_mask_pair_current_sequence_ &&
            previous_timestamp == automatic_mask_pair_current_timestamp_ns_ &&
            same_texture_descriptor(previous_descriptor,
                                    automatic_mask_pair_descriptor_) &&
            same_color_metadata(previous_color_metadata,
                                automatic_mask_pair_current_color_metadata_) &&
            current_sequence > automatic_mask_pair_current_sequence_ &&
            current_timestamp > automatic_mask_pair_current_timestamp_ns_;
        const bool same_history_basis = previous_history_valid &&
            clock_domain == automatic_mask_pair_clock_domain_ &&
            same_texture_descriptor(previous_descriptor,
                                    automatic_mask_pair_descriptor_) &&
            same_color_metadata(previous_color_metadata,
                                automatic_mask_pair_current_color_metadata_);
        const bool older_than_history = same_history_basis && !same_source_pair &&
            (current_sequence <= automatic_mask_pair_current_sequence_ ||
             current_timestamp <= automatic_mask_pair_current_timestamp_ns_);

        if (same_source_pair && !automatic_hud_reset_history) {
            // Interpolating the same source pair at multiple target times must
            // reuse the same pair evidence. Recompute from the prior-pair mask
            // and leave the current history slot unchanged so output does not
            // depend on how many target requests the host makes.
            has_mask_history = automatic_mask_pair_has_prior_history_;
            history_write_index = automatic_mask_history_index_;
            history_read_resource = automatic_mask_history_[1U - history_write_index];
            mask_engagement_alpha = automatic_mask_pair_engagement_alpha_;
            mask_release_alpha = automatic_mask_pair_release_alpha_;
        } else if (follows_source_pair && !automatic_hud_reset_history) {
            has_mask_history = true;
            advance_mask_history = true;
            history_write_index = 1U - automatic_mask_history_index_;
            history_read_resource = automatic_mask_history_[automatic_mask_history_index_];
            // For adjacent pairs [a,b] and [b,c], their center-to-center
            // interval is exactly (c-a)/2. Compute the signed-timestamp
            // distance with unsigned arithmetic so epoch-scale nanoseconds
            // retain their ordering even where long double is only double.
            const auto elapsed_span_ns =
                static_cast<std::uint64_t>(current_timestamp) -
                static_cast<std::uint64_t>(automatic_mask_pair_previous_timestamp_ns_);
            const double cadence = static_cast<double>(elapsed_span_ns) * 0.5 /
                33'333'333.0;
            mask_engagement_alpha = static_cast<float>(
                1.0 - std::pow(0.36, cadence));
            mask_release_alpha = static_cast<float>(
                1.0 - std::pow(0.79, cadence));
        } else if (!older_than_history || automatic_hud_reset_history) {
            // A first pair, explicit discontinuity, mode/size change, or
            // forward gap starts fresh evidence. Out-of-order work writes only
            // to scratch storage and cannot poison the most recent mask.
            advance_mask_history = true;
            history_write_index = 1U - automatic_mask_history_index_;
            history_read_resource = automatic_mask_history_[automatic_mask_history_index_];
        } else {
            history_write_index = 2U;
            history_read_resource = automatic_mask_history_[automatic_mask_history_index_];
        }
        history_write_resource = automatic_mask_history_[history_write_index];
        auxiliary_resources.push_back(history_read_resource);
        auxiliary_resources.push_back(history_write_resource);
    }

    MTLTextureDescriptor* output_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:previous_descriptor.width
                                                          height:previous_descriptor.height
                                                       mipmapped:NO];
    output_descriptor.storageMode = MTLStorageModePrivate;
    output_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> output_native = [device_ newTextureWithDescriptor:output_descriptor];
    if (output_native == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not allocate the output texture"));
    }
    const bool temporal_debug_requested =
        submission.temporal_quality.debug_visualization !=
            TemporalQualityDebugVisualization::disabled;
    const bool compose_hud =
        submission.hud_options.mode != HudMode::no_hud_knowledge &&
        !temporal_debug_requested;
    id<MTLTexture> rife_scene_native = output_native;
    if (compose_hud || temporal_plan.enabled) {
        MTLTextureDescriptor* scene_descriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                               width:previous_descriptor.width
                                                              height:previous_descriptor.height
                                                           mipmapped:NO];
        scene_descriptor.storageMode = MTLStorageModePrivate;
        scene_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        rife_scene_native = [device_ newTextureWithDescriptor:scene_descriptor];
        if (rife_scene_native == nil) {
            return std::unexpected(make_error(ErrorCode::allocation_failure,
                "Metal could not allocate an intermediate RIFE scene texture"));
        }
    }
    id<MTLTexture> temporal_scene_native = rife_scene_native;
    if (temporal_plan.enabled && compose_hud) {
        MTLTextureDescriptor* scene_descriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                               width:previous_descriptor.width
                                                              height:previous_descriptor.height
                                                           mipmapped:NO];
        scene_descriptor.storageMode = MTLStorageModePrivate;
        scene_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        temporal_scene_native = [device_ newTextureWithDescriptor:scene_descriptor];
        if (temporal_scene_native == nil) {
            return std::unexpected(make_error(ErrorCode::allocation_failure,
                "Metal could not allocate the stabilized scene texture for HUD composition"));
        }
    } else if (temporal_plan.enabled) {
        temporal_scene_native = output_native;
    }

    TextureDescriptor result_descriptor = previous_descriptor;
    auto output_resource = std::make_shared<MetalTextureResource>(
        device_, output_native, result_descriptor, device_id_);
    auto output_texture = Texture::from_resource(output_resource);
    if (!output_texture) {
        return std::unexpected(output_texture.error());
    }
    if (rife_scene_native != output_native) {
        auxiliary_resources.push_back(std::make_shared<MetalTextureResource>(
            device_, rife_scene_native,
            TextureDescriptor{previous_descriptor.width, previous_descriptor.height,
                              PixelFormat::rgba8_unorm, previous_descriptor.color_space,
                              AlphaMode::opaque},
            device_id_));
    }
    if (temporal_scene_native != output_native &&
        temporal_scene_native != rife_scene_native) {
        auxiliary_resources.push_back(std::make_shared<MetalTextureResource>(
            device_, temporal_scene_native,
            TextureDescriptor{previous_descriptor.width, previous_descriptor.height,
                              PixelFormat::rgba8_unorm, previous_descriptor.color_space,
                              AlphaMode::opaque},
            device_id_));
    }

    // MPSGraph can commit and replace the root command buffer while encoding.
    // Serialize the complete encode/signal/commit section so its continuation
    // buffers cannot interleave with another submission on this queue.
    std::unique_lock submission_order_lock(submission_mutex_);
    if (next_event_value_ == std::numeric_limits<std::uint64_t>::max()) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal completion event sequence was exhausted"));
    }
    id<MTLCommandBuffer> command_buffer = [queue_ commandBuffer];
    if (command_buffer == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal could not allocate a command buffer"));
    }

    for (const auto& dependency : submission.gpu_dependencies) {
        if (!dependency || dependency->backend_id() != kBackendId ||
            dependency->device_id() != device_id_) {
            return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                               "GPU dependency belongs to another backend device"));
        }
        const auto* metal_dependency = dynamic_cast<const MetalEventSyncPoint*>(dependency.get());
        if (metal_dependency == nullptr || metal_dependency->event() == nil ||
            metal_dependency->value() == 0) {
            return std::unexpected(make_error(ErrorCode::invalid_argument,
                                               "GPU dependency is not a valid Metal event point"));
        }
        [command_buffer encodeWaitForEvent:metal_dependency->event()
                                      value:metal_dependency->value()];
    }

    id<MTLCommandBuffer> first_command_buffer = command_buffer;
    std::shared_ptr<MetalRifeInvocation> retained_rife;
    std::string rife_error;
    MPSCommandBuffer* graph_command_buffer = rife_model_->encode(
        command_buffer, previous_native, current_native, rife_scene_native,
        submission.interpolation, rife_mode_, !*previous_srgb, !*previous_srgb,
        retained_rife, rife_error);
    if (graph_command_buffer == nil || !retained_rife) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "RIFE inference could not be encoded: " + rife_error));
    }
    // The returned MPS wrapper can refer to a root different from the initial
    // command buffer after commitAndContinue. Use that live root for HUD work,
    // the completion event, and final commit.
    command_buffer = graph_command_buffer.rootCommandBuffer;
    const auto commit_failed_inference = [&] {
        const auto retained_for_completion = retained_rife;
        [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> completed_command_buffer) {
            (void)completed_command_buffer;
            (void)retained_for_completion;
        }];
        [command_buffer commit];
        submission_order_lock.unlock();
    };
    if (!rife_error.empty()) {
        commit_failed_inference();
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "RIFE inference could not be encoded: " + rife_error));
    }

    if (temporal_plan.enabled) {
        if (temporal_resources_rebuilt) {
            id<MTLBlitCommandEncoder> blit = [command_buffer blitCommandEncoder];
            if (blit == nil) {
                commit_failed_inference();
                return std::unexpected(make_error(ErrorCode::backend_failure,
                    "Metal could not initialize temporal history state"));
            }
            [blit fillBuffer:stream_state->temporal_history_valid_buffer
                       range:NSMakeRange(0, sizeof(std::uint32_t)) value:0];
            [blit endEncoding];
        }

        const std::uint32_t group_count_x = stream_state->temporal_cut_group_count_x;
        const std::uint32_t group_count_y = stream_state->temporal_cut_group_count_y;
        const std::uint32_t tile_count = group_count_x * group_count_y;
        TemporalCutTileParameters tile_parameters{
            group_count_x, previous_descriptor.width, previous_descriptor.height,
            submission.interpolation};
        id<MTLComputeCommandEncoder> tile_encoder = [command_buffer computeCommandEncoder];
        if (tile_encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the temporal scene-cut sampling encoder"));
        }
        [tile_encoder setComputePipelineState:temporal_cut_tiles_pipeline_];
        [tile_encoder setTexture:previous_native atIndex:0];
        [tile_encoder setTexture:current_native atIndex:1];
        [tile_encoder setTexture:rife_scene_native atIndex:2];
        [tile_encoder setBuffer:stream_state->temporal_cut_tile_buffer offset:0 atIndex:0];
        [tile_encoder setBytes:&tile_parameters length:sizeof(tile_parameters) atIndex:1];
        [tile_encoder dispatchThreadgroups:MTLSizeMake(group_count_x, group_count_y, 1)
            threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
        [tile_encoder endEncoding];

        TemporalCutReduceParameters reduce_parameters{
            tile_count, temporal_gpu_reset ? 1U : 0U};
        id<MTLComputeCommandEncoder> reduce_encoder = [command_buffer computeCommandEncoder];
        if (reduce_encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the temporal scene-cut reduction encoder"));
        }
        [reduce_encoder setComputePipelineState:temporal_cut_reduce_pipeline_];
        [reduce_encoder setBuffer:stream_state->temporal_cut_tile_buffer offset:0 atIndex:0];
        [reduce_encoder setBuffer:stream_state->temporal_cut_summary_buffer offset:0 atIndex:1];
        [reduce_encoder setBytes:&reduce_parameters length:sizeof(reduce_parameters) atIndex:2];
        [reduce_encoder dispatchThreadgroups:MTLSizeMake(1, 1, 1)
            threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
        [reduce_encoder endEncoding];

        TemporalQualityParameters quality_parameters{
            submission.interpolation,
            temporal_plan.history_time_scale,
            temporal_policy_value(submission.temporal_quality.policy),
            temporal_debug_value(submission.temporal_quality.debug_visualization),
            temporal_gpu_reset ? 1U : 0U,
            temporal_plan.force_real_fallback ? 1U : 0U,
            temporal_plan.advance_history ? 1U : 0U,
            temporal_plan.reuse_cached_output ? 1U : 0U,
            select_temporal_current_source(submission) ? 1U : 0U,
            *previous_srgb ? 1U : 0U,
            temporal_hud_mask_native != nil ? 1U : 0U};
        id<MTLComputeCommandEncoder> quality_encoder = [command_buffer computeCommandEncoder];
        if (quality_encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the temporal confidence and fallback encoder"));
        }
        quality_encoder.label = @"FrameGen temporal quality controller";
        [quality_encoder setComputePipelineState:temporal_quality_pipeline_];
        [quality_encoder setTexture:previous_native atIndex:0];
        [quality_encoder setTexture:current_native atIndex:1];
        [quality_encoder setTexture:rife_scene_native atIndex:2];
        [quality_encoder setTexture:stream_state->temporal_history[
            stream_state->temporal_history_index]->native_texture() atIndex:3];
        [quality_encoder setTexture:temporal_scene_native atIndex:4];
        [quality_encoder setTexture:stream_state->temporal_history[
            temporal_write_index]->native_texture() atIndex:5];
        [quality_encoder setTexture:temporal_hud_mask_native atIndex:6];
        [quality_encoder setBuffer:stream_state->temporal_cut_summary_buffer offset:0 atIndex:0];
        [quality_encoder setBuffer:stream_state->temporal_history_valid_buffer offset:0 atIndex:1];
        [quality_encoder setBytes:&quality_parameters length:sizeof(quality_parameters) atIndex:2];
        const NSUInteger quality_threads_x = temporal_quality_pipeline_.threadExecutionWidth;
        const NSUInteger quality_threads_y = std::max<NSUInteger>(1,
            std::min<NSUInteger>(8,
                temporal_quality_pipeline_.maxTotalThreadsPerThreadgroup / quality_threads_x));
        [quality_encoder dispatchThreads:MTLSizeMake(output_native.width, output_native.height, 1)
            threadsPerThreadgroup:MTLSizeMake(quality_threads_x, quality_threads_y, 1)];
        [quality_encoder endEncoding];

        id<MTLComputeCommandEncoder> history_encoder = [command_buffer computeCommandEncoder];
        if (history_encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the temporal history commit encoder"));
        }
        [history_encoder setComputePipelineState:temporal_history_commit_pipeline_];
        [history_encoder setBuffer:stream_state->temporal_cut_summary_buffer offset:0 atIndex:0];
        [history_encoder setBuffer:stream_state->temporal_history_valid_buffer offset:0 atIndex:1];
        [history_encoder setBytes:&quality_parameters length:sizeof(quality_parameters) atIndex:2];
        [history_encoder dispatchThreads:MTLSizeMake(1, 1, 1)
            threadsPerThreadgroup:MTLSizeMake(1, 1, 1)];
        [history_encoder endEncoding];

        auxiliary_resources.push_back(stream_state->temporal_history[
            stream_state->temporal_history_index]);
        auxiliary_resources.push_back(stream_state->temporal_history[temporal_write_index]);
    }

    id<MTLComputePipelineState> selected_pipeline = nil;
    if (compose_hud && submission.hud_options.mode == HudMode::explicit_ui_plane) {
        id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
        if (encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the explicit UI composition encoder"));
        }
        selected_pipeline = explicit_ui_pipeline_;
        [encoder setComputePipelineState:selected_pipeline];
        [encoder setTexture:previous_native atIndex:0];
        [encoder setTexture:current_native atIndex:1];
        [encoder setTexture:previous_ui_native atIndex:2];
        [encoder setTexture:current_ui_native atIndex:3];
        [encoder setTexture:temporal_scene_native atIndex:4];
        [encoder setTexture:output_native atIndex:5];
        struct ExplicitUiParameters {
            float interpolation;
            std::uint32_t current_ui;
            std::uint32_t alpha_mode;
            std::uint32_t scene_is_srgb;
            std::uint32_t ui_is_srgb;
            std::uint32_t debug_visualization;
        } parameters{
            submission.interpolation,
            current_ui ? 1U : 0U,
            static_cast<std::uint32_t>(selected_ui_resource->descriptor().alpha_mode),
            *transfer_is_srgb(previous_descriptor,
                              submission.previous.color_metadata) ? 1U : 0U,
            *(current_ui
                ? transfer_is_srgb(selected_ui_resource->descriptor(),
                    submission.current.optional_inputs.ui_color_metadata)
                : transfer_is_srgb(selected_ui_resource->descriptor(),
                    submission.previous.optional_inputs.ui_color_metadata)) ? 1U : 0U,
            static_cast<std::uint32_t>(submission.hud_options.debug_visualization),
        };
        [encoder setBytes:&parameters length:sizeof(parameters) atIndex:0];
        const NSUInteger threads_x = selected_pipeline.threadExecutionWidth;
        const NSUInteger threads_y = std::max<NSUInteger>(
            1, std::min<NSUInteger>(8, selected_pipeline.maxTotalThreadsPerThreadgroup / threads_x));
        [encoder dispatchThreads:MTLSizeMake(output_native.width, output_native.height, 1)
            threadsPerThreadgroup:MTLSizeMake(threads_x, threads_y, 1)];
        [encoder endEncoding];
    } else if (compose_hud &&
               submission.hud_options.mode == HudMode::automatic_protection) {
        id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
        if (encoder == nil) {
            commit_failed_inference();
            return std::unexpected(make_error(ErrorCode::backend_failure,
                "Metal could not create the automatic HUD composition encoder"));
        }
        selected_pipeline = automatic_hud_pipeline_;
        [encoder setComputePipelineState:selected_pipeline];
        [encoder setTexture:previous_native atIndex:0];
        [encoder setTexture:current_native atIndex:1];
        [encoder setTexture:history_read_resource->native_texture() atIndex:2];
        [encoder setTexture:temporal_scene_native atIndex:3];
        [encoder setTexture:output_native atIndex:4];
        [encoder setTexture:history_write_resource->native_texture() atIndex:5];
        struct AutomaticHudParameters {
            float interpolation;
            float engagement_alpha;
            float release_alpha;
            std::uint32_t current_ui;
            std::uint32_t has_history;
            std::uint32_t debug_visualization;
            std::uint32_t reset_history;
            std::uint32_t has_cut_summary;
        } parameters{
            submission.interpolation,
            mask_engagement_alpha,
            mask_release_alpha,
            current_ui ? 1U : 0U,
            has_mask_history ? 1U : 0U,
            static_cast<std::uint32_t>(submission.hud_options.debug_visualization),
            automatic_hud_reset_history ? 1U : 0U,
            temporal_plan.enabled ? 1U : 0U,
        };
        [encoder setBuffer:(temporal_plan.enabled
            ? stream_state->temporal_cut_summary_buffer : nil) offset:0 atIndex:1];
        [encoder setBytes:&parameters length:sizeof(parameters) atIndex:0];
        const NSUInteger threads_x = selected_pipeline.threadExecutionWidth;
        const NSUInteger threads_y = std::max<NSUInteger>(
            1, std::min<NSUInteger>(8, selected_pipeline.maxTotalThreadsPerThreadgroup / threads_x));
        [encoder dispatchThreads:MTLSizeMake(output_native.width, output_native.height, 1)
            threadsPerThreadgroup:MTLSizeMake(threads_x, threads_y, 1)];
        [encoder endEncoding];
    }

    std::uint64_t event_value{};
    event_value = next_event_value_++;
    [command_buffer encodeSignalEvent:completion_event_ value:event_value];
    const auto retained_for_completion = retained_rife;
    const std::weak_ptr<MetalBackendStreamState> weak_stream_state = stream_state;
    [command_buffer addCompletedHandler:^(id<MTLCommandBuffer> completed_command_buffer) {
        if (completed_command_buffer.status == MTLCommandBufferStatusError) {
            if (const auto failed_stream_state = weak_stream_state.lock()) {
                failed_stream_state->temporal_async_reset_requested.store(
                    true, std::memory_order_release);
            }
        }
        (void)retained_for_completion;
    }];
    [command_buffer commit];
    submission_order_lock.unlock();

    stream_state->temporal_controller.commit(submission, previous_descriptor, temporal_plan);
    if (temporal_plan.enabled && temporal_plan.advance_history) {
        stream_state->temporal_history_index = temporal_write_index;
    }
    last_hud_mode_ = compose_hud
        ? submission.hud_options.mode : HudMode::no_hud_knowledge;
    if (compose_hud &&
        submission.hud_options.mode == HudMode::automatic_protection) {
        if (advance_mask_history) {
            automatic_mask_history_index_ = history_write_index;
            automatic_mask_history_valid_ = true;
            automatic_mask_pair_has_prior_history_ = has_mask_history;
            automatic_mask_pair_clock_domain_ = submission.current.timing.clock_domain;
            automatic_mask_pair_previous_sequence_ = submission.previous.timing.sequence;
            automatic_mask_pair_current_sequence_ = submission.current.timing.sequence;
            automatic_mask_pair_previous_timestamp_ns_ =
                submission.previous.timing.timestamp_ns;
            automatic_mask_pair_current_timestamp_ns_ =
                submission.current.timing.timestamp_ns;
            automatic_mask_pair_descriptor_ = previous_descriptor;
            automatic_mask_pair_previous_color_metadata_ =
                submission.previous.color_metadata;
            automatic_mask_pair_current_color_metadata_ =
                submission.current.color_metadata;
            automatic_mask_pair_engagement_alpha_ = mask_engagement_alpha;
            automatic_mask_pair_release_alpha_ = mask_release_alpha;
        }
    } else {
        automatic_mask_history_valid_ = false;
        automatic_mask_pair_has_prior_history_ = false;
    }
    history_lock.unlock();

    auto completion = std::make_shared<MetalCompletion>(
        first_command_buffer, command_buffer, completion_event_, event_value, device_id_,
        previous.resource(), current.resource(), output_texture->resource(),
        std::move(auxiliary_resources),
        submission.gpu_dependencies);
    return GeneratedFrame{std::move(*output_texture), std::move(completion)};
}

} // namespace detail

struct Device::State {
    __strong id<MTLDevice> native_device;
    __strong id<MTLCommandQueue> queue;
    __strong id<MTLComputePipelineState> explicit_ui_pipeline;
    __strong id<MTLComputePipelineState> automatic_hud_pipeline;
    __strong id<MTLComputePipelineState> temporal_cut_tiles_pipeline;
    __strong id<MTLComputePipelineState> temporal_cut_reduce_pipeline;
    __strong id<MTLComputePipelineState> temporal_quality_pipeline;
    __strong id<MTLComputePipelineState> temporal_history_commit_pipeline;
    __strong id<MTLSharedEvent> completion_event;
    std::uint64_t device_id{};
    std::shared_ptr<FrameGenerationBackend> backend;
};

Result<Device> Device::create(id<MTLDevice> native_device) {
    if (native_device == nil) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device must not be nil"));
    }

    std::string model_error;
    auto model_mode = internal_model_mode(model_error);
    if (!model_mode) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, model_error));
    }

    const auto model_path = detail::rife_model_weights_path();
    std::error_code filesystem_error;
    if (!std::filesystem::is_regular_file(model_path, filesystem_error) ||
        filesystem_error) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "RIFE weights are unavailable at " + model_path.string() +
            ". Convert the external checkpoint with tools/rife/convert_checkpoint.py "
            "or set FRAMEGEN_MODEL_WEIGHTS to a converted .fgweights file."));
    }

    id<MTLCommandQueue> queue = [native_device newCommandQueue];
    if (queue == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not create a command queue"));
    }

    NSError* rife_library_error = nil;
    id<MTLLibrary> rife_library = [native_device newLibraryWithSource:
        [NSString stringWithUTF8String:detail::kRifeMetalShaderSource]
        options:nil error:&rife_library_error];
    if (rife_library == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "RIFE Metal kernels failed to compile: " +
            error_message(rife_library_error, "Metal shader compilation failed")));
    }
    auto model = detail::MetalRifeModel::load(
        native_device, rife_library, model_path, model_error);
    if (!model) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "RIFE model could not be loaded: " + model_error +
            ". Convert the external checkpoint with tools/rife/convert_checkpoint.py "
            "or set FRAMEGEN_MODEL_WEIGHTS to a converted .fgweights file."));
    }
    std::string pipeline_error;
    id<MTLComputePipelineState> explicit_ui_pipeline = make_compute_pipeline(
        native_device, kExplicitUiShader, "framegen_explicit_ui", pipeline_error);
    if (explicit_ui_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal explicit UI pipeline failed: " + pipeline_error));
    }
    id<MTLComputePipelineState> automatic_hud_pipeline = make_compute_pipeline(
        native_device, kAutomaticHudShader, "framegen_automatic_hud", pipeline_error);
    if (automatic_hud_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal automatic HUD pipeline failed: " + pipeline_error));
    }
    id<MTLComputePipelineState> temporal_cut_tiles_pipeline = make_compute_pipeline(
        native_device, kTemporalQualityShader, "framegen_temporal_cut_tiles", pipeline_error);
    if (temporal_cut_tiles_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "Metal temporal cut statistics pipeline failed: " + pipeline_error));
    }
    id<MTLComputePipelineState> temporal_cut_reduce_pipeline = make_compute_pipeline(
        native_device, kTemporalQualityShader, "framegen_temporal_cut_reduce", pipeline_error);
    if (temporal_cut_reduce_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "Metal temporal cut reduction pipeline failed: " + pipeline_error));
    }
    id<MTLComputePipelineState> temporal_quality_pipeline = make_compute_pipeline(
        native_device, kTemporalQualityShader, "framegen_temporal_quality", pipeline_error);
    if (temporal_quality_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "Metal temporal quality pipeline failed: " + pipeline_error));
    }
    id<MTLComputePipelineState> temporal_history_commit_pipeline = make_compute_pipeline(
        native_device, kTemporalQualityShader, "framegen_temporal_history_commit", pipeline_error);
    if (temporal_history_commit_pipeline == nil) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
            "Metal temporal history commit pipeline failed: " + pipeline_error));
    }

    id<MTLSharedEvent> event = [native_device newSharedEvent];
    if (event == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not create a shared completion event"));
    }

    const auto device_id = allocate_device_id();
    if (!device_id) {
        return std::unexpected(make_error(ErrorCode::backend_failure,
                                           "Metal device identifier sequence was exhausted"));
    }

    auto state = std::make_shared<State>();
    state->native_device = native_device;
    state->queue = queue;
    state->explicit_ui_pipeline = explicit_ui_pipeline;
    state->automatic_hud_pipeline = automatic_hud_pipeline;
    state->temporal_cut_tiles_pipeline = temporal_cut_tiles_pipeline;
    state->temporal_cut_reduce_pipeline = temporal_cut_reduce_pipeline;
    state->temporal_quality_pipeline = temporal_quality_pipeline;
    state->temporal_history_commit_pipeline = temporal_history_commit_pipeline;
    state->completion_event = event;
    state->device_id = *device_id;
    state->backend = std::make_shared<detail::MetalBackend>(
        native_device, queue, explicit_ui_pipeline, automatic_hud_pipeline,
        temporal_cut_tiles_pipeline, temporal_cut_reduce_pipeline,
        temporal_quality_pipeline, temporal_history_commit_pipeline,
        event, std::move(model), *model_mode, *device_id);
    return Device(std::move(state));
}

std::uint64_t Device::device_id() const noexcept {
    return state_ ? state_->device_id : 0;
}

Result<Texture> Device::create_texture(const TextureDescriptor& descriptor) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (descriptor.width == 0 || descriptor.height == 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal texture dimensions must be non-zero"));
    }
    const auto pixel_format = to_metal_format(descriptor.format);
    if (!pixel_format || descriptor.format != PixelFormat::rgba8_unorm) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal texture allocation currently supports rgba8_unorm"));
    }

    MTLTextureDescriptor* native_descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:*pixel_format
                                                           width:descriptor.width
                                                          height:descriptor.height
                                                       mipmapped:NO];
    native_descriptor.storageMode = MTLStorageModePrivate;
    native_descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> texture = [state_->native_device newTextureWithDescriptor:native_descriptor];
    if (texture == nil) {
        return std::unexpected(make_error(ErrorCode::allocation_failure,
                                           "Metal could not allocate the requested texture"));
    }

    auto resource = std::make_shared<detail::MetalTextureResource>(
        state_->native_device, texture, descriptor, state_->device_id);
    return Texture::from_resource(std::move(resource));
}

Result<Texture> Device::wrap_texture(id<MTLTexture> native_texture,
                                    ColorSpace color_space,
                                    AlphaMode alpha_mode,
                                    const void* resource_identity) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (native_texture == nil || native_texture.device != state_->native_device) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "native Metal texture must belong to this device"));
    }
    if (native_texture.textureType != MTLTextureType2D || native_texture.sampleCount != 1 ||
        native_texture.width > std::numeric_limits<std::uint32_t>::max() ||
        native_texture.height > std::numeric_limits<std::uint32_t>::max()) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "only single-sample 2D Metal textures are supported"));
    }
    const auto pixel_format = from_metal_format(native_texture.pixelFormat);
    if (!pixel_format) {
        return std::unexpected(make_error(ErrorCode::unsupported_format,
                                           "Metal texture format is not represented by framegen"));
    }
    TextureDescriptor descriptor{
        static_cast<std::uint32_t>(native_texture.width),
        static_cast<std::uint32_t>(native_texture.height),
        *pixel_format,
        color_space,
        alpha_mode,
    };
    auto resource = std::make_shared<detail::MetalTextureResource>(
        state_->native_device, native_texture, descriptor, state_->device_id,
        resource_identity);
    return Texture::from_resource(std::move(resource));
}

Result<std::shared_ptr<const GpuSyncPoint>> Device::make_event_dependency(
    const EventPoint& point) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    const id<MTLDevice> event_device = point.event.device;
    if (point.event == nil || point.value == 0 ||
        (event_device != nil && event_device != state_->native_device)) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "Metal event must be non-nil and usable on this device"));
    }
    std::shared_ptr<const GpuSyncPoint> dependency =
        std::make_shared<MetalEventDependency>(point.event, point.value, state_->device_id);
    return dependency;
}

id<MTLTexture> Device::native_texture(const Texture& texture) const noexcept {
    if (!state_ || !texture || texture.backend_id() != kBackendId ||
        texture.device_id() != state_->device_id) {
        return nil;
    }
    const auto resource = as_metal_resource(texture);
    if (!resource || !resource->belongs_to(state_->native_device)) {
        return nil;
    }
    return resource->native_texture();
}

Result<EventPoint> Device::event_point(const GpuSyncPoint& point) const {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "Metal device adapter is not initialized"));
    }
    if (point.backend_id() != kBackendId || point.device_id() != state_->device_id) {
        return std::unexpected(make_error(ErrorCode::incompatible_resource,
                                           "GPU synchronization point belongs to another device"));
    }
    const auto* metal_point = dynamic_cast<const MetalEventSyncPoint*>(&point);
    if (metal_point == nullptr || metal_point->event() == nil || metal_point->value() == 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                           "GPU synchronization point is not a valid Metal event"));
    }
    return EventPoint{metal_point->event(), metal_point->value()};
}

std::shared_ptr<FrameGenerationBackend> Device::backend() const noexcept {
    return state_ ? state_->backend : nullptr;
}

} // namespace framegen::metal
