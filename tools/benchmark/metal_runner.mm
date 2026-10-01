#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

#include "framegen/frame_generator.hpp"
#include "framegen/metal.hpp"

#include <algorithm>
#include <charconv>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
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
constexpr std::size_t kMaximumServerLineBytes = 64 * 1024;
constexpr std::size_t kMaximumServerJobs = 100'000;
constexpr std::size_t kMaximumServerTotalSamples = 1'000'000;
constexpr std::uint32_t kMaximumSamplesPerJob = 100'000;

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
    std::uint64_t previous_sequence{};
    std::uint64_t current_sequence{2};
    std::int64_t previous_timestamp_ns{};
    std::int64_t current_timestamp_ns{33'333'333};
    std::optional<std::int64_t> requested_target_timestamp_ns;
    bool reset_history{};
    std::string hud_mode{"none"};
    std::string ui_source{"nearest"};
    std::string hud_debug{"disabled"};
};

[[noreturn]] void fail(std::string message);

framegen::HudMode hud_mode(const std::string& value) {
    if (value == "none") return framegen::HudMode::no_hud_knowledge;
    if (value == "explicit") return framegen::HudMode::explicit_ui_plane;
    if (value == "automatic") return framegen::HudMode::automatic_protection;
    fail("--hud-mode must be none, explicit, or automatic");
}

framegen::UiTemporalSource ui_source(const std::string& value) {
    if (value == "previous") return framegen::UiTemporalSource::previous_frame;
    if (value == "current") return framegen::UiTemporalSource::current_frame;
    if (value == "nearest") return framegen::UiTemporalSource::nearest_presentation;
    fail("--ui-source must be previous, current, or nearest");
}

framegen::HudDebugVisualization hud_debug(const std::string& value) {
    if (value == "disabled") return framegen::HudDebugVisualization::disabled;
    if (value == "raw-mask") return framegen::HudDebugVisualization::raw_mask;
    if (value == "stabilized-mask") return framegen::HudDebugVisualization::stabilized_mask;
    if (value == "protected-regions") return framegen::HudDebugVisualization::protected_regions;
    if (value == "interpolation-confidence") return framegen::HudDebugVisualization::interpolation_confidence;
    if (value == "final-composite") return framegen::HudDebugVisualization::final_composite;
    fail("--hud-debug has an unknown visualization mode");
}

[[noreturn]] void fail(std::string message) {
    throw std::runtime_error(std::move(message));
}

void validate_sample_counts(const Options& options) {
    if (options.iterations == 0 || options.warmup > kMaximumSamplesPerJob ||
        options.iterations > kMaximumSamplesPerJob - options.warmup) {
        fail("warmup plus measured iterations must be between 1 and 100000");
    }
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

std::uint32_t parse_u32(std::string_view value, std::string_view label,
                        bool allow_zero = false) {
    std::size_t consumed{};
    const auto result = std::stoul(std::string(value), &consumed);
    if (consumed != value.size() || (!allow_zero && result == 0) ||
        result > std::numeric_limits<std::uint32_t>::max()) {
        fail("invalid PPM " + std::string(label));
    }
    return static_cast<std::uint32_t>(result);
}

std::uint64_t parse_u64(std::string_view value, std::string_view label) {
    std::uint64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        fail("invalid " + std::string(label));
    }
    return result;
}

std::int64_t parse_i64(std::string_view value, std::string_view label) {
    std::int64_t result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size()) {
        fail("invalid " + std::string(label));
    }
    return result;
}

std::uint64_t timestamp_distance(std::int64_t lower, std::int64_t upper) {
    return static_cast<std::uint64_t>(upper) - static_cast<std::uint64_t>(lower);
}

std::int64_t timestamp_plus_offset(std::int64_t lower, std::uint64_t offset) {
    if (lower >= 0) {
        return lower + static_cast<std::int64_t>(offset);
    }
    const auto magnitude = std::uint64_t{0} - static_cast<std::uint64_t>(lower);
    if (offset < magnitude) {
        const auto remaining = magnitude - offset;
        if (remaining == (std::uint64_t{1} << 63)) {
            return std::numeric_limits<std::int64_t>::min();
        }
        return -static_cast<std::int64_t>(remaining);
    }
    return static_cast<std::int64_t>(offset - magnitude);
}

std::int64_t interpolate_timestamp(std::int64_t previous, std::int64_t current,
                                   float interpolation) {
    const auto span = timestamp_distance(previous, current);
    const auto rounded_offset = std::round(
        static_cast<long double>(span) * static_cast<long double>(interpolation));
    const auto bounded_offset = rounded_offset >= static_cast<long double>(span)
        ? span : static_cast<std::uint64_t>(rounded_offset);
    return timestamp_plus_offset(previous, bounded_offset);
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
    const std::uint64_t pixel_count = static_cast<std::uint64_t>(image.width) * image.height;
    if (pixel_count > std::numeric_limits<std::size_t>::max() / 4 ||
        pixel_count > std::numeric_limits<std::uint64_t>::max() / 3) {
        fail("PPM dimensions exceed addressable benchmark texture storage: " + path);
    }
    const std::uint64_t byte_count = pixel_count * 3;
    if (bytes.size() - cursor != static_cast<std::size_t>(byte_count)) {
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
            options.warmup = parse_u32(value(), "warmup count", true);
        } else if (arg == "--iterations") {
            options.iterations = parse_u32(value(), "iteration count");
        } else if (arg == "--previous-sequence") {
            options.previous_sequence = parse_u64(value(), "previous sequence number");
        } else if (arg == "--current-sequence") {
            options.current_sequence = parse_u64(value(), "current sequence number");
        } else if (arg == "--previous-timestamp-ns") {
            options.previous_timestamp_ns = parse_i64(value(), "previous timestamp");
        } else if (arg == "--current-timestamp-ns") {
            options.current_timestamp_ns = parse_i64(value(), "current timestamp");
        } else if (arg == "--reset-history") {
            const auto reset = value();
            if (reset != "0" && reset != "1") fail("--reset-history must be 0 or 1");
            options.reset_history = reset == "1";
        } else if (arg == "--hud-mode") {
            options.hud_mode = value();
        } else if (arg == "--ui-source") {
            options.ui_source = value();
        } else if (arg == "--hud-debug") {
            options.hud_debug = value();
        } else if (arg == "--help" || arg == "-h") {
            std::cout << "Usage: framegen-benchmark-metal --previous F.ppm --current F.ppm "
                         "--output G.ppm [--t 0.5] [--warmup 3] [--iterations 100] "
                         "[--previous-sequence N --current-sequence N] "
                         "[--previous-timestamp-ns N --current-timestamp-ns N] "
                         "[--reset-history 0|1] [--hud-mode none|explicit|automatic] "
                         "[--ui-source previous|current|nearest] [--hud-debug MODE]\n"
                         "       framegen-benchmark-metal --server  # read 11-, 14-, or 15-column TSV jobs from stdin\n";
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
    validate_sample_counts(options);
    (void)hud_mode(options.hud_mode);
    (void)ui_source(options.ui_source);
    (void)hud_debug(options.hud_debug);
    if (options.current_sequence <= options.previous_sequence ||
        options.previous_timestamp_ns == framegen::unknown_timestamp_ns ||
        options.current_timestamp_ns == framegen::unknown_timestamp_ns ||
        options.current_timestamp_ns <= options.previous_timestamp_ns) {
        fail("source sequences and timestamps must be known and increase within the pair");
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

Options parse_server_job(std::string_view line) {
    std::vector<std::string_view> columns;
    std::size_t start{};
    while (true) {
        const auto tab = line.find('\t', start);
        columns.push_back(line.substr(start, tab == std::string_view::npos
                                                ? line.size() - start
                                                : tab - start));
        if (tab == std::string_view::npos) break;
        start = tab + 1;
    }
    if (columns.size() != 11 && columns.size() != 14 && columns.size() != 15) {
        fail("server input requires exactly 11, 14, or 15 tab-separated columns");
    }
    Options options;
    options.previous_path = columns[0];
    options.current_path = columns[1];
    options.output_path = columns[2];
    std::size_t t_consumed{};
    const double parsed_interpolation =
        std::stod(std::string(columns[3]), &t_consumed);
    if (t_consumed != columns[3].size()) fail("invalid server interpolation t");
    options.interpolation = static_cast<float>(parsed_interpolation);
    options.warmup = parse_u32(columns[4], "server warmup count", true);
    options.iterations = parse_u32(columns[5], "server iteration count");
    options.previous_sequence = parse_u64(columns[6], "previous sequence number");
    options.current_sequence = parse_u64(columns[7], "current sequence number");
    options.previous_timestamp_ns = parse_i64(columns[8], "previous timestamp");
    options.current_timestamp_ns = parse_i64(columns[9], "current timestamp");
    if (columns[10] != "0" && columns[10] != "1") {
        fail("server reset_history must be 0 or 1");
    }
    options.reset_history = columns[10] == "1";
    if (columns.size() >= 14) {
        options.hud_mode = columns[11];
        options.ui_source = columns[12];
        options.hud_debug = columns[13];
    }
    if (columns.size() == 15) {
        options.requested_target_timestamp_ns =
            parse_i64(columns[14], "requested target timestamp");
    }

    if (options.previous_path.empty() || options.current_path.empty() ||
        options.output_path.empty()) {
        fail("server input paths must not be empty");
    }
    if (!std::isfinite(parsed_interpolation) || parsed_interpolation <= 0.0 ||
        parsed_interpolation >= 1.0) {
        fail("server interpolation t must be finite and strictly between 0 and 1");
    }
    validate_sample_counts(options);
    (void)hud_mode(options.hud_mode);
    (void)ui_source(options.ui_source);
    (void)hud_debug(options.hud_debug);
    if (options.current_sequence <= options.previous_sequence ||
        options.previous_timestamp_ns == framegen::unknown_timestamp_ns ||
        options.current_timestamp_ns == framegen::unknown_timestamp_ns ||
        options.current_timestamp_ns <= options.previous_timestamp_ns) {
        fail("server source sequences and timestamps must be known and increase within the pair");
    }
    if (options.requested_target_timestamp_ns &&
        (*options.requested_target_timestamp_ns <= options.previous_timestamp_ns ||
         *options.requested_target_timestamp_ns >= options.current_timestamp_ns)) {
        fail("requested target timestamp must be strictly inside the source pair");
    }
    if (options.requested_target_timestamp_ns) {
        const auto span = timestamp_distance(options.previous_timestamp_ns,
                                             options.current_timestamp_ns);
        const auto offset = timestamp_distance(options.previous_timestamp_ns,
            *options.requested_target_timestamp_ns);
        const long double timestamp_fraction =
            static_cast<long double>(offset) / static_cast<long double>(span);
        const long double quantization_tolerance =
            0.5L / static_cast<long double>(span) +
            8.0L * std::numeric_limits<double>::epsilon();
        if (std::fabs(timestamp_fraction - parsed_interpolation) >
            quantization_tolerance) {
            fail("requested target timestamp does not match interpolation t after quantization");
        }
        // The exact integer timestamp is authoritative for this server request.
        // Deriving t from it keeps scene interpolation and nearest-UI selection
        // on the same timeline after the accepted nanosecond quantization.
        options.interpolation = static_cast<float>(timestamp_fraction);
        if (options.interpolation <= 0.0F) {
            options.interpolation = std::nextafter(0.0F, 1.0F);
        } else if (options.interpolation >= 1.0F) {
            options.interpolation = std::nextafter(1.0F, 0.0F);
        }
    } else if (!std::isfinite(options.interpolation) ||
               options.interpolation <= 0.0F || options.interpolation >= 1.0F) {
        fail("server interpolation t must remain strictly inside (0, 1) as float");
    }
    return options;
}

class BenchmarkContext final {
public:
    [[nodiscard]] static BenchmarkContext create() {
        id<MTLDevice> native_device = MTLCreateSystemDefaultDevice();
        auto device_result = framegen::metal::Device::create(native_device);
        if (!device_result) {
            fail("Metal backend initialization failed: " + device_result.error().message);
        }
        auto device = std::move(*device_result);
        auto generator_result = framegen::FrameGenerator::create(device.backend());
        if (!generator_result) {
            fail("FrameGenerator creation failed: " + generator_result.error().message);
        }
        return BenchmarkContext(native_device, std::move(device),
                                std::move(*generator_result));
    }

    [[nodiscard]] id<MTLDevice> native_device() const noexcept { return native_device_; }
    [[nodiscard]] framegen::metal::Device& device() noexcept { return device_; }
    [[nodiscard]] framegen::FrameGenerator& generator() noexcept { return generator_; }

private:
    BenchmarkContext(id<MTLDevice> native_device, framegen::metal::Device device,
                     framegen::FrameGenerator generator)
        : native_device_(native_device), device_(std::move(device)),
          generator_(std::move(generator)) {}

    __strong id<MTLDevice> native_device_;
    framegen::metal::Device device_;
    framegen::FrameGenerator generator_;
};

void run_job(const Options& options, BenchmarkContext& context) {
    const auto previous = read_ppm(options.previous_path);
    const auto current = read_ppm(options.current_path);
    if (previous.width != current.width || previous.height != current.height) {
        fail("input frame dimensions must match");
    }

    id<MTLDevice> native_device = context.native_device();
    NSString* native_device_name = native_device.name;
    const char* native_device_name_utf8 = native_device_name.UTF8String;
    const std::string device_name = native_device_name_utf8 != nullptr
        ? native_device_name_utf8 : "unknown Metal device";
    const std::string device_id = "metal-registry-" +
        std::to_string(static_cast<std::uint64_t>(native_device.registryID));
    auto& device = context.device();
    auto& generator = context.generator();
    std::vector<std::uint8_t> previous_rgba;
    std::vector<std::uint8_t> current_rgba;
    const auto previous_texture = make_input_texture(native_device, previous, previous_rgba);
    const auto current_texture = make_input_texture(native_device, current, current_rgba);
    auto wrapped_previous = device.wrap_texture(previous_texture, framegen::ColorSpace::srgb,
                                                framegen::AlphaMode::opaque);
    auto wrapped_current = device.wrap_texture(current_texture, framegen::ColorSpace::srgb,
                                               framegen::AlphaMode::opaque);
    if (!wrapped_previous || !wrapped_current) fail("could not wrap benchmark input textures");
    const auto source_start = Clock::now();
    for (std::uint32_t i = 0; i < options.iterations; ++i) {
        upload_input(previous_texture, previous, previous_rgba);
        upload_input(current_texture, current, current_rgba);
    }
    const auto source_end = Clock::now();
    const double source_only_fps =
        static_cast<double>(options.iterations) * 2.0 /
        std::chrono::duration<double>(source_end - source_start).count();

    const auto target_timestamp_ns = options.requested_target_timestamp_ns
        ? *options.requested_target_timestamp_ns
        : interpolate_timestamp(options.previous_timestamp_ns,
                                options.current_timestamp_ns,
                                options.interpolation);
    framegen::FrameSubmission submission{
        .previous = framegen::FrameInput{
            .texture = *wrapped_previous,
            .timing = framegen::FrameTiming{.sequence = options.previous_sequence,
                                             .timestamp_ns = options.previous_timestamp_ns,
                                             .clock_domain = 1,
                                             .desired_presentation_timestamp_ns =
                                                 options.previous_timestamp_ns},
        },
        .current = framegen::FrameInput{
            .texture = *wrapped_current,
            .timing = framegen::FrameTiming{.sequence = options.current_sequence,
                                             .timestamp_ns = options.current_timestamp_ns,
                                             .clock_domain = 1,
                                             .desired_presentation_timestamp_ns =
                                                 options.current_timestamp_ns},
        },
        .interpolation = options.interpolation,
        .reset_history = options.reset_history,
        .interpolation_timestamp_ns = target_timestamp_ns,
        .desired_presentation_timestamp_ns = target_timestamp_ns,
        .hud_options = framegen::HudOptions{
            .mode = hud_mode(options.hud_mode),
            .ui_source = ui_source(options.ui_source),
            .debug_visualization = hud_debug(options.hud_debug),
        },
    };

    std::vector<std::uint64_t> gpu_ns;
    std::vector<std::uint64_t> submit_ns;
    std::vector<std::uint64_t> latency_ns;
    std::uint64_t gpu_allocated_bytes_sampled_max{};
    std::uint64_t measured_generation_cycle_total_ns{};
    framegen::GeneratedFrame quality_generated;
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
        gpu_allocated_bytes_sampled_max = std::max<std::uint64_t>(
            gpu_allocated_bytes_sampled_max,
            static_cast<std::uint64_t>(native_device.currentAllocatedSize));
        if (i >= options.warmup) {
            submit_ns.push_back(static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(submitted - submission_start).count()));
            latency_ns.push_back(static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(completed - submission_start).count()));
            measured_generation_cycle_total_ns += static_cast<std::uint64_t>(
                std::chrono::duration_cast<std::chrono::nanoseconds>(completed - cycle_start).count());
            gpu_ns.push_back(generated->completion->gpu_execution_time_ns().value_or(0));
        }
        // Repeated direct-run samples are for timing, not additional temporal
        // observations. Keep the first one-pass result for quality readback so
        // mask output cannot depend on the requested iteration count.
        if (i == 0 && options.output_path != "-") {
            quality_generated = std::move(*generated);
        }
    }

    if (options.output_path != "-") {
        const auto native_output = device.native_texture(quality_generated.texture);
        if (native_output == nil) fail("generated Metal output could not be read back");
        const auto rgba = readback_texture(native_device, native_output,
                                           previous.width, previous.height);
        write_ppm(options.output_path, previous.width, previous.height, rgba);
    }
    const double source_with_generation_fps = measured_generation_cycle_total_ns == 0
        ? 0.0
        : static_cast<double>(latency_ns.size()) * 2.0 * 1'000'000'000.0 /
              static_cast<double>(measured_generation_cycle_total_ns);

    std::cout << std::setprecision(std::numeric_limits<float>::max_digits10)
              << "{\"backend_id\":\"" << generator.backend_id()
              << "\",\"backend_kind\":\"metal_placeholder_blend\",\"device_id\":\""
              << json_escape(device_id) << "\",\"device_name\":\""
              << json_escape(device_name) << "\",\"width\":"
              << previous.width << ",\"height\":" << previous.height
              << ",\"interpolation_t\":" << options.interpolation
              << ",\"previous_sequence\":" << options.previous_sequence
              << ",\"current_sequence\":" << options.current_sequence
              << ",\"previous_timestamp_ns\":" << options.previous_timestamp_ns
              << ",\"current_timestamp_ns\":" << options.current_timestamp_ns
              << ",\"interpolation_timestamp_ns\":" << target_timestamp_ns
              << ",\"desired_presentation_timestamp_ns\":" << target_timestamp_ns
              << ",\"reset_history\":" << (options.reset_history ? "true" : "false")
              << ",\"warmup_samples\":" << options.warmup
              << ",\"measured_samples\":" << options.iterations
              << ",\"backend_metadata\":{\"hud_mode\":\""
              << json_escape(options.hud_mode) << "\",\"ui_temporal_source\":\""
              << json_escape(options.ui_source) << "\",\"debug_visualization\":\""
              << json_escape(options.hud_debug) << "\",\"automatic_mask\":\""
              << (options.hud_mode == "automatic"
                      ? "soft confidence; temporal hysteresis; one-pixel feather"
                      : "not used") << "\"}"
              << ",\"gpu_execution_time_ns\":";
    print_array(gpu_ns);
    std::cout << ",\"cpu_submit_overhead_ns\":";
    print_array(submit_ns);
    std::cout << ",\"completion_latency_ns\":";
    print_array(latency_ns);
    std::cout << ",\"peak_resident_bytes\":" << peak_resident_bytes()
              << ",\"gpu_allocated_bytes_sampled_max\":" << gpu_allocated_bytes_sampled_max
              << ",\"gpu_allocated_bytes_current\":"
              << static_cast<std::uint64_t>(native_device.currentAllocatedSize)
              << ",\"source_only_input_fps\":" << source_only_fps
              << ",\"source_with_generation_input_fps\":" << source_with_generation_fps
              << ",\"source_with_generation_throughput_method\":\"two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation\""
              << ",\"output_path\":";
    if (options.output_path == "-") {
        std::cout << "null";
    } else {
        std::cout << "\"" << json_escape(options.output_path) << "\"";
    }
    std::cout << "}\n" << std::flush;
}

int run(const Options& options) {
    auto context = BenchmarkContext::create();
    run_job(options, context);
    return 0;
}

bool read_server_line(std::string& line) {
    line.clear();
    char value{};
    while (std::cin.get(value)) {
        if (value == '\n') return true;
        if (line.size() == kMaximumServerLineBytes) {
            fail("benchmark server input line exceeds 64 KiB");
        }
        line.push_back(value);
    }
    if (std::cin.bad()) fail("failed while reading benchmark server jobs");
    return !line.empty();
}

int run_server() {
    auto context = BenchmarkContext::create();
    std::string line;
    std::size_t job_count{};
    std::size_t sample_count{};
    while (read_server_line(line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        if (job_count == kMaximumServerJobs) {
            fail("benchmark server job count exceeds 100000");
        }
        const auto options = parse_server_job(line);
        if (options.warmup != 0 || options.iterations != 1) {
            fail("server jobs must contain one chronological sample; replay the sequence for warmup and measurement passes");
        }
        const auto job_samples = static_cast<std::size_t>(options.warmup) + options.iterations;
        if (job_samples > kMaximumServerTotalSamples - sample_count) {
            fail("benchmark server total sample count exceeds 1000000");
        }
        ++job_count;
        sample_count += job_samples;
        @autoreleasepool {
            run_job(options, context);
        }
    }
    return 0;
}

} // namespace

int main(int argc, char** argv) {
    @autoreleasepool {
        try {
            if (argc >= 2 && std::string_view(argv[1]) == "--server") {
                if (argc != 2) fail("--server does not accept additional command-line arguments");
                return run_server();
            }
            return run(parse_options(argc, argv));
        } catch (const std::exception& exception) {
            std::cerr << "framegen-benchmark-metal: " << exception.what() << '\n';
            return 2;
        }
    }
}
