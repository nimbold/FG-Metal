#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include "framegen/frame_generator.hpp"
#include "framegen/metal.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <sys/resource.h>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;

struct Image {
    std::uint32_t width{};
    std::uint32_t height{};
    std::vector<std::uint8_t> rgb;
};

struct Options {
    std::string previous_path;
    std::string current_path;
    std::string output_path;
    float interpolation{0.5F};
    std::uint32_t warmup{3};
    std::uint32_t iterations{100};
};

void fail(std::string message) {
    throw std::runtime_error(std::move(message));
}

std::string next_ppm_token(const std::vector<std::uint8_t>& bytes, std::size_t& cursor) {
    while (cursor < bytes.size()) {
        const auto c = bytes[cursor];
        if (c == '#') {
            while (cursor < bytes.size() && bytes[cursor] != '\n') {
                ++cursor;
            }
        } else if (std::isspace(c)) {
            ++cursor;
        } else {
            break;
        }
    }
    const auto start = cursor;
    while (cursor < bytes.size() && !std::isspace(bytes[cursor]) && bytes[cursor] != '#') {
        ++cursor;
    }
    if (start == cursor) {
        fail("invalid or truncated PPM header");
    }
    return std::string(bytes.begin() + static_cast<std::ptrdiff_t>(start),
                       bytes.begin() + static_cast<std::ptrdiff_t>(cursor));
}

std::uint32_t parse_u32(std::string_view value, std::string_view label) {
    std::size_t consumed{};
    const auto result = std::stoul(std::string(value), &consumed);
    if (consumed != value.size() || result == 0 ||
        result > std::numeric_limits<std::uint32_t>::max()) {
        fail("invalid PPM " + std::string(label));
    }
    return static_cast<std::uint32_t>(result);
}

Image read_ppm(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        fail("cannot open PPM input: " + path);
    }
    std::vector<std::uint8_t> bytes((std::istreambuf_iterator<char>(input)), {});
    std::size_t cursor{};
    if (next_ppm_token(bytes, cursor) != "P6") {
        fail("benchmark inputs must be binary PPM (P6): " + path);
    }
    Image image;
    image.width = parse_u32(next_ppm_token(bytes, cursor), "width");
    image.height = parse_u32(next_ppm_token(bytes, cursor), "height");
    if (next_ppm_token(bytes, cursor) != "255") {
        fail("benchmark PPM inputs must use max value 255: " + path);
    }
    if (cursor >= bytes.size() || !std::isspace(bytes[cursor])) {
        fail("invalid PPM pixel-data separator: " + path);
    }
    if (bytes[cursor] == '\r' && cursor + 1 < bytes.size() && bytes[cursor + 1] == '\n') {
        cursor += 2;
    } else {
        ++cursor;
    }
    const std::uint64_t byte_count = static_cast<std::uint64_t>(image.width) * image.height * 3;
    if (byte_count > std::numeric_limits<std::size_t>::max() ||
        bytes.size() - cursor != static_cast<std::size_t>(byte_count)) {
        fail("PPM pixel payload size does not match its dimensions: " + path);
    }
    image.rgb.assign(bytes.begin() + static_cast<std::ptrdiff_t>(cursor), bytes.end());
    return image;
}

Options parse_options(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string_view arg(argv[i]);
        auto value = [&]() -> std::string_view {
            if (i + 1 >= argc) {
                fail("missing value for " + std::string(arg));
            }
            return argv[++i];
        };
        if (arg == "--previous") options.previous_path = value();
        else if (arg == "--current") options.current_path = value();
        else if (arg == "--output") options.output_path = value();
        else if (arg == "--t") {
            const auto text = value();
            std::size_t consumed{};
            options.interpolation = std::stof(std::string(text), &consumed);
            if (consumed != text.size()) fail("--t must be a number");
        } else if (arg == "--warmup") {
            options.warmup = parse_u32(value(), "warmup count");
        } else if (arg == "--iterations") {
            options.iterations = parse_u32(value(), "iteration count");
        } else if (arg == "--help" || arg == "-h") {
            std::cout << "Usage: framegen-benchmark-metal --previous F.ppm --current F.ppm "
                         "--output G.ppm [--t 0.5] [--warmup 3] [--iterations 100]\n";
            std::exit(0);
        } else {
            fail("unknown option: " + std::string(arg));
        }
    }
    if (options.previous_path.empty() || options.current_path.empty() ||
        options.output_path.empty()) {
        fail("--previous, --current, and --output are required");
    }
    if (!std::isfinite(options.interpolation) || options.interpolation <= 0.0F ||
        options.interpolation >= 1.0F) {
        fail("--t must be finite and strictly between 0 and 1");
    }
    if (options.warmup == 0 || options.iterations == 0) {
        fail("warmup and iteration counts must be non-zero");
    }
    return options;
}

id<MTLTexture> make_input_texture(id<MTLDevice> native_device,
                                 const Image& image,
                                 std::vector<std::uint8_t>& rgba) {
    MTLTextureDescriptor* descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:image.width
                                                          height:image.height
                                                       mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> texture = [native_device newTextureWithDescriptor:descriptor];
    if (texture == nil) fail("Metal could not allocate a shared input texture");

    rgba.resize(static_cast<std::size_t>(image.width) * image.height * 4);
    for (std::size_t pixel = 0; pixel < static_cast<std::size_t>(image.width) * image.height; ++pixel) {
        rgba[pixel * 4] = image.rgb[pixel * 3];
        rgba[pixel * 4 + 1] = image.rgb[pixel * 3 + 1];
        rgba[pixel * 4 + 2] = image.rgb[pixel * 3 + 2];
        rgba[pixel * 4 + 3] = 255;
    }
    [texture replaceRegion:MTLRegionMake2D(0, 0, image.width, image.height)
               mipmapLevel:0
                 withBytes:rgba.data()
               bytesPerRow:static_cast<NSUInteger>(image.width) * 4];
    return texture;
}

void upload_input(id<MTLTexture> texture, const Image& image,
                  const std::vector<std::uint8_t>& rgba) {
    [texture replaceRegion:MTLRegionMake2D(0, 0, image.width, image.height)
               mipmapLevel:0
                 withBytes:rgba.data()
               bytesPerRow:static_cast<NSUInteger>(image.width) * 4];
}

std::vector<std::uint8_t> readback_texture(id<MTLDevice> device, id<MTLTexture> source,
                                           std::uint32_t width, std::uint32_t height) {
    MTLTextureDescriptor* descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                           width:width
                                                          height:height
                                                       mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageUnknown;
    id<MTLTexture> staging = [device newTextureWithDescriptor:descriptor];
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLCommandBuffer> command = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [command blitCommandEncoder];
    if (staging == nil || queue == nil || command == nil || blit == nil) {
        fail("Metal could not create an offline readback staging resource");
    }
    [blit copyFromTexture:source
              sourceSlice:0
              sourceLevel:0
             sourceOrigin:MTLOriginMake(0, 0, 0)
               sourceSize:MTLSizeMake(width, height, 1)
                toTexture:staging
         destinationSlice:0
         destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit endEncoding];
    [command commit];
    [command waitUntilCompleted];
    if (command.status == MTLCommandBufferStatusError) {
        fail("Metal offline readback blit failed");
    }
    std::vector<std::uint8_t> rgba(static_cast<std::size_t>(width) * height * 4);
    [staging getBytes:rgba.data()
          bytesPerRow:static_cast<NSUInteger>(width) * 4
           fromRegion:MTLRegionMake2D(0, 0, width, height)
          mipmapLevel:0];
    return rgba;
}

std::uint64_t peak_resident_bytes() {
    rusage usage{};
    if (getrusage(RUSAGE_SELF, &usage) != 0 || usage.ru_maxrss < 0) return 0;
    return static_cast<std::uint64_t>(usage.ru_maxrss);
}

void write_ppm(const std::string& path, std::uint32_t width, std::uint32_t height,
               const std::vector<std::uint8_t>& rgba) {
    std::ofstream output(path, std::ios::binary);
    if (!output) fail("cannot create output PPM: " + path);
    output << "P6\n" << width << ' ' << height << "\n255\n";
    std::vector<std::uint8_t> rgb(static_cast<std::size_t>(width) * height * 3);
    for (std::size_t pixel = 0; pixel < static_cast<std::size_t>(width) * height; ++pixel) {
        rgb[pixel * 3] = rgba[pixel * 4];
        rgb[pixel * 3 + 1] = rgba[pixel * 4 + 1];
        rgb[pixel * 3 + 2] = rgba[pixel * 4 + 2];
    }
    output.write(reinterpret_cast<const char*>(rgb.data()),
                 static_cast<std::streamsize>(rgb.size()));
    if (!output) fail("failed writing output PPM: " + path);
}

void print_array(const std::vector<std::uint64_t>& values) {
    std::cout << '[';
    for (std::size_t i = 0; i < values.size(); ++i) {
        if (i != 0) std::cout << ',';
        std::cout << values[i];
    }
    std::cout << ']';
}

std::string json_escape(std::string_view value) {
    constexpr char hex[] = "0123456789abcdef";
    std::string escaped;
    escaped.reserve(value.size());
    for (const unsigned char c : value) {
        switch (c) {
        case '"': escaped += "\\\""; break;
        case '\\': escaped += "\\\\"; break;
        case '\b': escaped += "\\b"; break;
        case '\f': escaped += "\\f"; break;
        case '\n': escaped += "\\n"; break;
        case '\r': escaped += "\\r"; break;
        case '\t': escaped += "\\t"; break;
        default:
            if (c < 0x20) {
                escaped += "\\u00";
                escaped += hex[(c >> 4) & 0x0f];
                escaped += hex[c & 0x0f];
            } else {
                escaped += static_cast<char>(c);
            }
        }
    }
    return escaped;
}

int run(const Options& options) {
    const auto previous = read_ppm(options.previous_path);
    const auto current = read_ppm(options.current_path);
    if (previous.width != current.width || previous.height != current.height) {
        fail("input frame dimensions must match");
    }

    id<MTLDevice> native_device = MTLCreateSystemDefaultDevice();
    auto device_result = framegen::metal::Device::create(native_device);
    if (!device_result) fail("Metal backend initialization failed: " + device_result.error().message);
    auto device = std::move(*device_result);
    std::vector<std::uint8_t> previous_rgba;
    std::vector<std::uint8_t> current_rgba;
    const auto previous_texture = make_input_texture(native_device, previous, previous_rgba);
    const auto current_texture = make_input_texture(native_device, current, current_rgba);
    auto wrapped_previous = device.wrap_texture(previous_texture, framegen::ColorSpace::srgb,
                                                framegen::AlphaMode::opaque);
    auto wrapped_current = device.wrap_texture(current_texture, framegen::ColorSpace::srgb,
                                               framegen::AlphaMode::opaque);
    if (!wrapped_previous || !wrapped_current) fail("could not wrap benchmark input textures");
    auto generator_result = framegen::FrameGenerator::create(device.backend());
    if (!generator_result) fail("FrameGenerator creation failed: " + generator_result.error().message);
    auto generator = std::move(*generator_result);

    const auto source_start = Clock::now();
    for (std::uint32_t i = 0; i < options.iterations; ++i) {
        upload_input(previous_texture, previous, previous_rgba);
        upload_input(current_texture, current, current_rgba);
    }
    const auto source_end = Clock::now();
    const double source_only_fps =
        static_cast<double>(options.iterations) * 2.0 /
        std::chrono::duration<double>(source_end - source_start).count();

    framegen::FrameSubmission submission{
        .previous = framegen::FrameInput{
            .texture = *wrapped_previous,
            .timing = framegen::FrameTiming{.sequence = 0, .timestamp_ns = 0,
                                             .clock_domain = 1},
        },
        .current = framegen::FrameInput{
            .texture = *wrapped_current,
            .timing = framegen::FrameTiming{.sequence = 2, .timestamp_ns = 33'333'333,
                                             .clock_domain = 1},
        },
        .interpolation = options.interpolation,
    };

    std::vector<std::uint64_t> gpu_ns;
    std::vector<std::uint64_t> submit_ns;
    std::vector<std::uint64_t> latency_ns;
    std::uint64_t gpu_allocated_bytes_peak{};
    std::uint64_t measured_generation_cycle_total_ns{};
    framegen::GeneratedFrame last_generated;
    for (std::uint32_t i = 0; i < options.warmup + options.iterations; ++i) {
        const auto cycle_start = Clock::now();
        upload_input(previous_texture, previous, previous_rgba);
        upload_input(current_texture, current, current_rgba);
        const auto submission_start = Clock::now();
        auto generated = generator.submit(submission);
        const auto submitted = Clock::now();
        if (!generated) fail("Metal placeholder interpolation failed: " + generated.error().message);
        auto waited = generated->completion->wait();
        const auto completed = Clock::now();
        if (!waited) fail("Metal completion wait failed: " + waited.error().message);
        gpu_allocated_bytes_peak = std::max<std::uint64_t>(
            gpu_allocated_bytes_peak, static_cast<std::uint64_t>(native_device.currentAllocatedSize));
        if (i >= options.warmup) {
            submit_ns.push_back(static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(submitted - submission_start).count()));
            latency_ns.push_back(static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(completed - submission_start).count()));
            measured_generation_cycle_total_ns += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(completed - cycle_start).count());
            gpu_ns.push_back(generated->completion->gpu_execution_time_ns().value_or(0));
        }
        last_generated = std::move(*generated);
    }

    const auto native_output = device.native_texture(last_generated.texture);
    if (native_output == nil) fail("generated Metal output could not be read back");
    const auto rgba = readback_texture(native_device, native_output,
                                       previous.width, previous.height);
    write_ppm(options.output_path, previous.width, previous.height, rgba);
    const double source_with_generation_fps = measured_generation_cycle_total_ns == 0
        ? 0.0
        : static_cast<double>(latency_ns.size()) * 2.0 * 1'000'000'000.0 /
              static_cast<double>(measured_generation_cycle_total_ns);

    std::cout << "{\"backend_id\":\"" << generator.backend_id()
              << "\",\"backend_kind\":\"metal_placeholder_blend\",\"width\":"
              << previous.width << ",\"height\":" << previous.height
              << ",\"interpolation_t\":" << options.interpolation
              << ",\"warmup_samples\":" << options.warmup
              << ",\"measured_samples\":" << options.iterations
              << ",\"gpu_execution_time_ns\":";
    print_array(gpu_ns);
    std::cout << ",\"cpu_submit_overhead_ns\":";
    print_array(submit_ns);
    std::cout << ",\"completion_latency_ns\":";
    print_array(latency_ns);
    std::cout << ",\"peak_resident_bytes\":" << peak_resident_bytes()
              << ",\"gpu_allocated_bytes_peak\":" << gpu_allocated_bytes_peak
              << ",\"gpu_allocated_bytes_current\":"
              << static_cast<std::uint64_t>(native_device.currentAllocatedSize)
              << ",\"source_only_input_fps\":" << source_only_fps
              << ",\"source_with_generation_input_fps\":" << source_with_generation_fps
              << ",\"source_with_generation_throughput_method\":\"two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation\""
              << ",\"output_path\":\"" << json_escape(options.output_path) << "\"}\n";
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    @autoreleasepool {
        try {
            return run(parse_options(argc, argv));
        } catch (const std::exception& exception) {
            std::cerr << "framegen-benchmark-metal: " << exception.what() << '\n';
            return 2;
        }
    }
}
