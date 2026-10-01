#include <metal_stdlib>
using namespace metal;

// GPU-only input/output and recurrent sampling kernels for the Practical-RIFE
// v4.26 graph. Neural-network convolutions remain MPSGraph operations.

struct PreprocessParameters {
    uint input_width;
    uint input_height;
    uint model_width;
    uint model_height;
    uint valid_model_width;
    uint valid_model_height;
    uint input_is_linear;
};

struct PackParameters {
    uint width;
    uint height;
    float interpolation;
};

struct FinishParameters {
    uint output_width;
    uint output_height;
    uint model_width;
    uint model_height;
    uint valid_model_width;
    uint valid_model_height;
    float interpolation;
    uint output_is_linear;
};

inline float3 encode_srgb(float3 value) {
    value = max(value, float3(0.0f));
    const float3 low = value * 12.92f;
    const float3 high = 1.055f * pow(value, float3(1.0f / 2.4f)) - 0.055f;
    return select(high, low, value <= 0.0031308f);
}

inline float3 decode_srgb(float3 value) {
    const float3 low = value / 12.92f;
    const float3 high = pow((value + 0.055f) / 1.055f, float3(2.4f));
    return select(high, low, value <= 0.04045f);
}

template <typename T>
inline float load_channel(device const T* values, uint pixel, uint channels,
                          uint channel) {
    return float(values[pixel * channels + channel]);
}

template <typename T>
inline float sample_channel(device const T* values, uint width, uint height,
                            uint channels, uint channel, float x, float y) {
    x = clamp(x, 0.0f, float(width - 1));
    y = clamp(y, 0.0f, float(height - 1));
    const uint x0 = uint(floor(x));
    const uint y0 = uint(floor(y));
    const uint x1 = min(x0 + 1, width - 1);
    const uint y1 = min(y0 + 1, height - 1);
    const float fx = x - float(x0);
    const float fy = y - float(y0);
    const float a = load_channel(values, y0 * width + x0, channels, channel);
    const float b = load_channel(values, y0 * width + x1, channels, channel);
    const float c = load_channel(values, y1 * width + x0, channels, channel);
    const float d = load_channel(values, y1 * width + x1, channels, channel);
    return mix(mix(a, b, fx), mix(c, d, fx), fy);
}

template <typename T>
inline float sample_padded_channel(device const T* values, uint stride_width,
                                   uint valid_width, uint valid_height,
                                   uint channels, uint channel, float x, float y) {
    x = clamp(x, 0.0f, float(valid_width - 1));
    y = clamp(y, 0.0f, float(valid_height - 1));
    const uint x0 = uint(floor(x));
    const uint y0 = uint(floor(y));
    const uint x1 = min(x0 + 1, valid_width - 1);
    const uint y1 = min(y0 + 1, valid_height - 1);
    const float fx = x - float(x0);
    const float fy = y - float(y0);
    const float a = load_channel(values, y0 * stride_width + x0, channels, channel);
    const float b = load_channel(values, y0 * stride_width + x1, channels, channel);
    const float c = load_channel(values, y1 * stride_width + x0, channels, channel);
    const float d = load_channel(values, y1 * stride_width + x1, channels, channel);
    return mix(mix(a, b, fx), mix(c, d, fx), fy);
}

struct WarpedImageAndFeatures {
    float3 image_rgb;
    float4 feature_rgba;
};

template <typename T>
inline WarpedImageAndFeatures warp_image_and_features(
                                    device const T* image,
                                    device const T* features,
                                    device const T* network,
                                    uint pixel, uint width, uint height,
                                    uint flow_channel) {
    const float fx = load_channel(network, pixel, 13, flow_channel);
    const float fy = load_channel(network, pixel, 13, flow_channel + 1);
    const uint x = pixel % width;
    const uint y = pixel / width;
    const float sx = float(x) + fx;
    const float sy = float(y) + fy;
    WarpedImageAndFeatures result;
    result.image_rgb = float3(
        sample_channel(image, width, height, 3, 0, sx, sy),
        sample_channel(image, width, height, 3, 1, sx, sy),
        sample_channel(image, width, height, 3, 2, sx, sy));
    result.feature_rgba = float4(
        sample_channel(features, width, height, 4, 0, sx, sy),
        sample_channel(features, width, height, 4, 1, sx, sy),
        sample_channel(features, width, height, 4, 2, sx, sy),
        sample_channel(features, width, height, 4, 3, sx, sy));
    return result;
}

#define DECLARE_RIFE_KERNELS(T, SUFFIX) \
kernel void rife_preprocess_##SUFFIX( \
    texture2d<float, access::sample> source [[texture(0)]], \
    device T* output [[buffer(0)]], \
    constant PreprocessParameters& parameters [[buffer(1)]], \
    uint2 position [[thread_position_in_grid]]) { \
    if (position.x >= parameters.model_width || position.y >= parameters.model_height) return; \
    constexpr sampler linear_clamp(coord::pixel, address::clamp_to_edge, filter::linear); \
    const uint2 valid_position = min(position, uint2(parameters.valid_model_width - 1, \
                                                     parameters.valid_model_height - 1)); \
    const float2 source_position = (float2(valid_position) + 0.5f) * \
        float2(float(parameters.input_width) / float(parameters.valid_model_width), \
               float(parameters.input_height) / float(parameters.valid_model_height)); \
    float3 rgb = source.sample(linear_clamp, source_position).rgb; \
    if (parameters.input_is_linear != 0) rgb = encode_srgb(rgb); \
    const uint base = (position.y * parameters.model_width + position.x) * 3; \
    output[base + 0] = T(rgb.r); \
    output[base + 1] = T(rgb.g); \
    output[base + 2] = T(rgb.b); \
} \
kernel void rife_pack_initial_##SUFFIX( \
    device const T* previous [[buffer(0)]], \
    device const T* current [[buffer(1)]], \
    device const T* previous_features [[buffer(2)]], \
    device const T* current_features [[buffer(3)]], \
    device T* output [[buffer(4)]], \
    constant PackParameters& parameters [[buffer(5)]], \
    uint2 position [[thread_position_in_grid]]) { \
    if (position.x >= parameters.width || position.y >= parameters.height) return; \
    const uint pixel = position.y * parameters.width + position.x; \
    const uint dst = pixel * 15; \
    for (uint c = 0; c < 3; ++c) { \
        output[dst + c] = previous[pixel * 3 + c]; \
        output[dst + 3 + c] = current[pixel * 3 + c]; \
    } \
    for (uint c = 0; c < 4; ++c) { \
        output[dst + 6 + c] = previous_features[pixel * 4 + c]; \
        output[dst + 10 + c] = current_features[pixel * 4 + c]; \
    } \
    output[dst + 14] = T(parameters.interpolation); \
} \
kernel void rife_warp_pack_##SUFFIX( \
    device const T* previous [[buffer(0)]], \
    device const T* current [[buffer(1)]], \
    device const T* previous_features [[buffer(2)]], \
    device const T* current_features [[buffer(3)]], \
    device const T* network [[buffer(4)]], \
    device T* output [[buffer(5)]], \
    constant PackParameters& parameters [[buffer(6)]], \
    uint2 position [[thread_position_in_grid]]) { \
    if (position.x >= parameters.width || position.y >= parameters.height) return; \
    const uint pixel = position.y * parameters.width + position.x; \
    const WarpedImageAndFeatures warped0 = warp_image_and_features(previous, previous_features, \
        network, pixel, parameters.width, parameters.height, 0); \
    const WarpedImageAndFeatures warped1 = warp_image_and_features(current, current_features, \
        network, pixel, parameters.width, parameters.height, 2); \
    const uint dst = pixel * 24; \
    for (uint c = 0; c < 3; ++c) { \
        output[dst + c] = T(warped0.image_rgb[c]); \
        output[dst + 3 + c] = T(warped1.image_rgb[c]); \
    } \
    for (uint c = 0; c < 4; ++c) { \
        output[dst + 6 + c] = T(warped0.feature_rgba[c]); \
        output[dst + 10 + c] = T(warped1.feature_rgba[c]); \
    } \
    output[dst + 14] = T(parameters.interpolation); \
    output[dst + 15] = network[pixel * 13 + 4]; \
    for (uint c = 0; c < 8; ++c) output[dst + 16 + c] = network[pixel * 13 + 5 + c]; \
} \
kernel void rife_warp_final_##SUFFIX( \
    device const T* previous [[buffer(0)]], \
    device const T* current [[buffer(1)]], \
    device const T* network [[buffer(2)]], \
    device T* output [[buffer(3)]], \
    constant PackParameters& parameters [[buffer(4)]], \
    uint2 position [[thread_position_in_grid]]) { \
    if (position.x >= parameters.width || position.y >= parameters.height) return; \
    const uint pixel = position.y * parameters.width + position.x; \
    const float fx0 = load_channel(network, pixel, 13, 0); \
    const float fy0 = load_channel(network, pixel, 13, 1); \
    const float fx1 = load_channel(network, pixel, 13, 2); \
    const float fy1 = load_channel(network, pixel, 13, 3); \
    const float x = float(position.x), y = float(position.y); \
    const float3 warped0( \
        sample_channel(previous, parameters.width, parameters.height, 3, 0, x + fx0, y + fy0), \
        sample_channel(previous, parameters.width, parameters.height, 3, 1, x + fx0, y + fy0), \
        sample_channel(previous, parameters.width, parameters.height, 3, 2, x + fx0, y + fy0)); \
    const float3 warped1( \
        sample_channel(current, parameters.width, parameters.height, 3, 0, x + fx1, y + fy1), \
        sample_channel(current, parameters.width, parameters.height, 3, 1, x + fx1, y + fy1), \
        sample_channel(current, parameters.width, parameters.height, 3, 2, x + fx1, y + fy1)); \
    const float mask_logit = load_channel(network, pixel, 13, 4); \
    const float mask = 1.0f / (1.0f + exp(-mask_logit)); \
    const float3 merged = warped0 * mask + warped1 * (1.0f - mask); \
    const uint dst = pixel * 3; \
    output[dst + 0] = T(merged.r); \
    output[dst + 1] = T(merged.g); \
    output[dst + 2] = T(merged.b); \
} \
kernel void rife_postprocess_##SUFFIX( \
    texture2d<float, access::read> previous [[texture(0)]], \
    texture2d<float, access::read> current [[texture(1)]], \
    texture2d<float, access::write> output_texture [[texture(2)]], \
    device const T* model_output [[buffer(0)]], \
    constant FinishParameters& parameters [[buffer(1)]], \
    uint2 position [[thread_position_in_grid]]) { \
    if (position.x >= parameters.output_width || position.y >= parameters.output_height) return; \
    const float sx = clamp((float(position.x) + 0.5f) * \
        (float(parameters.valid_model_width) / float(parameters.output_width)) - 0.5f, \
        0.0f, float(parameters.valid_model_width - 1)); \
    const float sy = clamp((float(position.y) + 0.5f) * \
        (float(parameters.valid_model_height) / float(parameters.output_height)) - 0.5f, \
        0.0f, float(parameters.valid_model_height - 1)); \
    const float3 rgb( \
        sample_padded_channel(model_output, parameters.model_width, parameters.valid_model_width, parameters.valid_model_height, 3, 0, sx, sy), \
        sample_padded_channel(model_output, parameters.model_width, parameters.valid_model_width, parameters.valid_model_height, 3, 1, sx, sy), \
        sample_padded_channel(model_output, parameters.model_width, parameters.valid_model_width, parameters.valid_model_height, 3, 2, sx, sy)); \
    const float4 a = previous.read(position); \
    const float4 b = current.read(position); \
    const float3 output_rgb = parameters.output_is_linear != 0 ? decode_srgb(rgb) : rgb; \
    output_texture.write(float4(clamp(output_rgb, 0.0f, 1.0f), \
        mix(a.a, b.a, parameters.interpolation)), position); \
}

DECLARE_RIFE_KERNELS(float, f32)
DECLARE_RIFE_KERNELS(half, f16)

#undef DECLARE_RIFE_KERNELS
