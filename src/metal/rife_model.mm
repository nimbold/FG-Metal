#import "rife_model.hpp"

#import <Foundation/Foundation.h>
#import <MetalPerformanceShaders/MetalPerformanceShaders.h>

#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <deque>
#include <condition_variable>
#include <fstream>
#include <functional>
#include <limits>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

// Architecture transcription reference: Practical-RIFE v4.26, IFNet_HDv3.py
// and RIFE_HDv3.py, MIT license, Copyright (c) 2021 hzwer.
// https://github.com/hzwer/Practical-RIFE

namespace framegen::metal::detail {
namespace {

constexpr std::array<std::uint8_t, 8> kWeightsMagic{
    'F', 'G', 'R', 'W', 'G', 'T', '1', '\0'};
constexpr std::uint64_t kMaxWeightsBytes = 512ULL * 1024ULL * 1024ULL;
constexpr std::uint32_t kMaxWeightCount = 4096;
constexpr std::uint32_t kMaxDimension = 32768;
constexpr std::uint32_t kPadMultiple = 64;
constexpr std::uint32_t kModelChannels = 13;
constexpr std::size_t kMaxCachedPlans = 4;
constexpr std::size_t kMaxCachedScratchSets = 3;
constexpr std::size_t kMaxScratchSetBytes = 1'280ULL * 1024ULL * 1024ULL;
constexpr std::size_t kMaxCachedScratchBytes = 2ULL * 1024ULL * 1024ULL * 1024ULL;
// RGB endpoints (6), endpoint features (8), initial/recurrent packs (39),
// ping-pong network state (26), and the final RGB estimate (3).
constexpr std::size_t kScratchScalarsPerPixel = 3 + 3 + 4 + 4 + 15 + 24 +
    2 * kModelChannels + 3;

struct PreprocessParameters final {
    std::uint32_t input_width;
    std::uint32_t input_height;
    std::uint32_t model_width;
    std::uint32_t model_height;
    std::uint32_t valid_model_width;
    std::uint32_t valid_model_height;
    std::uint32_t input_is_linear;
};

struct PackParameters final {
    std::uint32_t width;
    std::uint32_t height;
    float interpolation;
};

struct FinishParameters final {
    std::uint32_t output_width;
    std::uint32_t output_height;
    std::uint32_t model_width;
    std::uint32_t model_height;
    std::uint32_t valid_model_width;
    std::uint32_t valid_model_height;
    float interpolation;
    std::uint32_t output_is_linear;
};

enum class Precision : std::uint8_t { f32, f16 };

MPSDataType mps_type(Precision precision) {
    return precision == Precision::f16 ? MPSDataTypeFloat16 : MPSDataTypeFloat32;
}

std::string metal_error(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) return fallback;
    const char* description = error.localizedDescription.UTF8String;
    return description == nullptr ? fallback : description;
}

std::size_t checked_product(const std::vector<std::uint32_t>& dims) {
    std::size_t elements = 1;
    for (std::uint32_t dim : dims) {
        if (dim == 0 || elements > std::numeric_limits<std::size_t>::max() / dim) {
            throw std::runtime_error("invalid or overflowing RIFE tensor shape");
        }
        elements *= dim;
    }
    return elements;
}

class BinaryReader final {
public:
    explicit BinaryReader(const std::filesystem::path& path) : stream_(path, std::ios::binary) {
        if (!stream_) throw std::runtime_error("cannot open converted RIFE weights file");
        stream_.seekg(0, std::ios::end);
        const auto length = stream_.tellg();
        if (length < 0 || static_cast<std::uint64_t>(length) > kMaxWeightsBytes) {
            throw std::runtime_error("converted RIFE weights file has an invalid size");
        }
        size_ = static_cast<std::uint64_t>(length);
        stream_.seekg(0, std::ios::beg);
    }

    std::uint8_t u8() { return read<std::uint8_t>(); }
    std::uint16_t u16() { return read<std::uint16_t>(); }
    std::uint32_t u32() { return read<std::uint32_t>(); }

    void bytes(void* output, std::size_t length) {
        if (length > size_ - offset_) throw std::runtime_error("truncated RIFE weights file");
        stream_.read(static_cast<char*>(output), static_cast<std::streamsize>(length));
        if (!stream_) throw std::runtime_error("cannot read converted RIFE weights file");
        offset_ += length;
    }

    [[nodiscard]] std::uint64_t remaining() const noexcept { return size_ - offset_; }

private:
    template <typename T>
    T read() {
        std::array<std::uint8_t, sizeof(T)> bytes{};
        this->bytes(bytes.data(), bytes.size());
        T result = 0;
        for (std::size_t i = 0; i < bytes.size(); ++i) {
            result |= static_cast<T>(bytes[i]) << (8U * i);
        }
        return result;
    }

    std::ifstream stream_;
    std::uint64_t size_{};
    std::uint64_t offset_{};
};

struct WeightTensor final {
    std::vector<std::uint32_t> dims;
    std::vector<float> values;
    __strong id<MTLBuffer> buffer_f32;
    __strong id<MTLBuffer> buffer_f16;
    __strong NSMutableDictionary<NSNumber*, MPSGraphTensorData*>* tensor_data;

    id<MTLBuffer> buffer(id<MTLDevice> device, Precision precision) {
        if (precision == Precision::f32) {
            if (buffer_f32 == nil) {
                const auto bytes = values.size() * sizeof(float);
                buffer_f32 = [device newBufferWithBytes:values.data()
                                                 length:bytes
                                                options:MTLResourceStorageModeShared];
                if (buffer_f32 == nil) throw std::runtime_error("Metal failed to allocate RIFE f32 weights");
            }
            return buffer_f32;
        }
        if (buffer_f16 == nil) {
            std::vector<__fp16> packed(values.size());
            std::transform(values.begin(), values.end(), packed.begin(),
                           [] (float value) { return static_cast<__fp16>(value); });
            const auto bytes = packed.size() * sizeof(__fp16);
            buffer_f16 = [device newBufferWithBytes:packed.data()
                                              length:bytes
                                             options:MTLResourceStorageModeShared];
            if (buffer_f16 == nil) throw std::runtime_error("Metal failed to allocate RIFE f16 weights");
        }
        return buffer_f16;
    }

    MPSGraphTensorData* graph_data(id<MTLDevice> device, Precision precision,
                                  const std::vector<std::uint32_t>& graph_dims) {
        if (tensor_data == nil) tensor_data = [NSMutableDictionary new];
        const MPSDataType type = mps_type(precision);
        NSNumber* key = @(type);
        MPSGraphTensorData* cached = tensor_data[key];
        if (cached != nil) return cached;
        NSMutableArray<NSNumber*>* shape = [NSMutableArray arrayWithCapacity:graph_dims.size()];
        for (std::uint32_t dim : graph_dims) [shape addObject:@(dim)];
        MPSGraphTensorData* data = [[MPSGraphTensorData alloc]
            initWithMTLBuffer:buffer(device, precision) shape:shape dataType:type];
        if (data == nil) throw std::runtime_error("MPSGraph could not wrap a RIFE weight buffer");
        tensor_data[key] = data;
        return data;
    }
};

using WeightMap = std::map<std::string, WeightTensor, std::less<>>;

WeightMap read_weights(const std::filesystem::path& path) {
    BinaryReader reader(path);
    std::array<std::uint8_t, kWeightsMagic.size()> magic{};
    reader.bytes(magic.data(), magic.size());
    if (magic != kWeightsMagic) throw std::runtime_error("RIFE weights file has an invalid FGRWGT1 header");
    const std::uint32_t tensor_count = reader.u32();
    if (tensor_count == 0 || tensor_count > kMaxWeightCount) {
        throw std::runtime_error("RIFE weights file has an invalid tensor count");
    }

    WeightMap result;
    for (std::uint32_t i = 0; i < tensor_count; ++i) {
        const std::uint16_t name_length = reader.u16();
        const std::uint8_t rank = reader.u8();
        const std::uint8_t dtype = reader.u8();
        if (name_length == 0 || name_length > 4096 || rank == 0 || rank > 8 || dtype != 1) {
            throw std::runtime_error("RIFE weights file contains an unsupported tensor record");
        }
        WeightTensor tensor;
        tensor.dims.reserve(rank);
        for (std::uint8_t axis = 0; axis < rank; ++axis) {
            const std::uint32_t dim = reader.u32();
            if (dim == 0 || dim > kMaxDimension) {
                throw std::runtime_error("RIFE weights file contains an invalid tensor dimension");
            }
            tensor.dims.push_back(dim);
        }
        std::string name(name_length, '\0');
        reader.bytes(name.data(), name.size());
        const std::size_t elements = checked_product(tensor.dims);
        if (elements > reader.remaining() / sizeof(std::uint32_t)) {
            throw std::runtime_error("RIFE weights file contains a truncated tensor");
        }
        tensor.values.resize(elements);
        for (std::size_t element = 0; element < elements; ++element) {
            tensor.values[element] = std::bit_cast<float>(reader.u32());
            if (!std::isfinite(tensor.values[element])) {
                throw std::runtime_error("RIFE weights file contains a non-finite weight");
            }
        }
        if (!result.emplace(std::move(name), std::move(tensor)).second) {
            throw std::runtime_error("RIFE weights file contains a duplicate tensor name");
        }
    }
    if (reader.remaining() != 0) throw std::runtime_error("RIFE weights file has trailing data");
    return result;
}

MPSShape* mps_shape(const std::vector<std::uint32_t>& dims) {
    NSMutableArray<NSNumber*>* shape = [NSMutableArray arrayWithCapacity:dims.size()];
    for (std::uint32_t dim : dims) [shape addObject:@(dim)];
    return shape;
}

struct GraphProgram final {
    __strong MPSGraph* graph;
    __strong MPSGraphExecutable* executable;
    __strong MPSGraphTensor* input;
    __strong MPSGraphTensor* flow;
    __strong MPSGraphTensor* output;
    __strong NSMutableDictionary<MPSGraphTensor*, MPSGraphTensorData*>* static_feeds;
    __strong NSMutableDictionary<MPSGraphTensor*, MPSGraphShapedType*>* compile_feeds;
    std::vector<std::string>* required_weight_names{};
    Precision precision{Precision::f32};
    std::uint32_t width{};
    std::uint32_t height{};
    std::uint32_t input_channels{};
    bool has_flow{};

    MPSGraphTensor* add_weight(const std::string& name,
                               const std::vector<std::uint32_t>& expected_source_dims,
                               const std::vector<std::uint32_t>& graph_dims,
                               WeightMap& weights, id<MTLDevice> device) {
        auto found = weights.find(name);
        if (found == weights.end()) throw std::runtime_error("missing RIFE weight: " + name);
        if (found->second.dims != expected_source_dims) {
            throw std::runtime_error("RIFE weight has unexpected dimensions: " + name);
        }
        auto data = found->second.graph_data(device, precision, graph_dims);
        MPSGraphTensor* tensor = [graph placeholderWithShape:mps_shape(graph_dims)
                                                     dataType:mps_type(precision)
                                                         name:[NSString stringWithUTF8String:name.c_str()]];
        static_feeds[tensor] = data;
        compile_feeds[tensor] = [[MPSGraphShapedType alloc]
            initWithShape:mps_shape(graph_dims) dataType:mps_type(precision)];
        required_weight_names->push_back(name);
        return tensor;
    }

    MPSGraphTensor* conv2d(MPSGraphTensor* source, const std::string& prefix,
                           std::uint32_t input_channels, std::uint32_t output_channels,
                           std::uint32_t kernel, std::uint32_t stride,
                           std::uint32_t dilation = 1) {
        const std::vector<std::uint32_t> weight_shape{
            output_channels, input_channels, kernel, kernel};
        MPSGraphTensor* weights_tensor = add_weight(
            prefix + ".weight", weight_shape, weight_shape, *weight_map, device);
        MPSGraphTensor* bias_tensor = add_weight(
            prefix + ".bias", {output_channels}, {output_channels}, *weight_map, device);
        MPSGraphConvolution2DOpDescriptor* descriptor =
            [MPSGraphConvolution2DOpDescriptor
                descriptorWithStrideInX:stride strideInY:stride
                dilationRateInX:dilation dilationRateInY:dilation groups:1
                paddingLeft:dilation paddingRight:dilation
                paddingTop:dilation paddingBottom:dilation
                paddingStyle:MPSGraphPaddingStyleExplicit
                dataLayout:MPSGraphTensorNamedDataLayoutNHWC
                weightsLayout:MPSGraphTensorNamedDataLayoutOIHW];
        MPSGraphTensor* result = [graph convolution2DWithSourceTensor:source
                                                       weightsTensor:weights_tensor
                                                          descriptor:descriptor
                                                                name:nil];
        result = [graph additionWithPrimaryTensor:result secondaryTensor:bias_tensor name:nil];
        return [graph leakyReLUWithTensor:result alpha:0.2 name:nil];
    }

    MPSGraphTensor* conv_transpose(MPSGraphTensor* source, const std::string& prefix,
                                   std::uint32_t input_channels,
                                   std::uint32_t output_channels,
                                   std::uint32_t input_height,
                                   std::uint32_t input_width) {
        const std::vector<std::uint32_t> weight_shape{
            input_channels, output_channels, 4, 4};
        MPSGraphTensor* weights_tensor = add_weight(
            prefix + ".weight", weight_shape, weight_shape, *weight_map, device);
        MPSGraphTensor* bias_tensor = add_weight(
            prefix + ".bias", {output_channels}, {output_channels}, *weight_map, device);
        MPSGraphConvolution2DOpDescriptor* descriptor =
            [MPSGraphConvolution2DOpDescriptor
                descriptorWithStrideInX:2 strideInY:2
                dilationRateInX:1 dilationRateInY:1 groups:1
                paddingLeft:1 paddingRight:1 paddingTop:1 paddingBottom:1
                paddingStyle:MPSGraphPaddingStyleExplicit
                dataLayout:MPSGraphTensorNamedDataLayoutNHWC
                weightsLayout:MPSGraphTensorNamedDataLayoutOIHW];
        MPSGraphTensor* result = [graph convolutionTranspose2DWithSourceTensor:source
                                                                 weightsTensor:weights_tensor
                                                                   outputShape:@[@1, @(input_height * 2), @(input_width * 2), @(output_channels)]
                                                                    descriptor:descriptor name:nil];
        return [graph additionWithPrimaryTensor:result secondaryTensor:bias_tensor name:nil];
    }

    MPSGraphTensor* resize(MPSGraphTensor* source, std::uint32_t height,
                           std::uint32_t width, const char* name = nullptr) {
        return [graph resizeTensor:source size:@[@(height), @(width)]
                              mode:MPSGraphResizeBilinear centerResult:YES
                      alignCorners:NO layout:MPSGraphTensorNamedDataLayoutNHWC
                              name:name == nullptr ? nil : [NSString stringWithUTF8String:name]];
    }

    // Set by the builder for compact access from conv helpers.
    WeightMap* weight_map{};
    id<MTLDevice> device;
};

MPSGraphTensor* add_head_convolution(GraphProgram& program, MPSGraphTensor* source,
                                    const std::string& layer,
                                    std::uint32_t in_channels,
                                    std::uint32_t out_channels,
                                    std::uint32_t stride) {
    const std::vector<std::uint32_t> shape{out_channels, in_channels, 3, 3};
    auto* weight = program.add_weight("encode." + layer + ".weight", shape, shape,
                                      *program.weight_map, program.device);
    auto* bias = program.add_weight("encode." + layer + ".bias", {out_channels},
                                    {out_channels}, *program.weight_map, program.device);
    auto* descriptor = [MPSGraphConvolution2DOpDescriptor
        descriptorWithStrideInX:stride strideInY:stride
        dilationRateInX:1 dilationRateInY:1 groups:1
        paddingLeft:1 paddingRight:1 paddingTop:1 paddingBottom:1
        paddingStyle:MPSGraphPaddingStyleExplicit
        dataLayout:MPSGraphTensorNamedDataLayoutNHWC
        weightsLayout:MPSGraphTensorNamedDataLayoutOIHW];
    auto* result = [program.graph convolution2DWithSourceTensor:source
                                                 weightsTensor:weight
                                                    descriptor:descriptor name:nil];
    result = [program.graph additionWithPrimaryTensor:result secondaryTensor:bias name:nil];
    return [program.graph leakyReLUWithTensor:result alpha:0.2 name:nil];
}

GraphProgram make_head_program(WeightMap& weights, std::vector<std::string>& required,
                              id<MTLDevice> device, Precision precision,
                              std::uint32_t width, std::uint32_t height) {
    GraphProgram program;
    program.graph = [MPSGraph new];
    program.static_feeds = [NSMutableDictionary new];
    program.compile_feeds = [NSMutableDictionary new];
    program.required_weight_names = &required;
    program.precision = precision;
    program.width = width;
    program.height = height;
    program.input_channels = 3;
    program.weight_map = &weights;
    program.device = device;
    program.input = [program.graph placeholderWithShape:@[@1, @(height), @(width), @3]
                                                  dataType:mps_type(precision) name:@"head.input"];
    program.compile_feeds[program.input] = [[MPSGraphShapedType alloc]
        initWithShape:@[@1, @(height), @(width), @3] dataType:mps_type(precision)];
    auto* cnn0 = add_head_convolution(program, program.input, "cnn0", 3, 16, 2);
    auto* cnn1 = add_head_convolution(program, cnn0, "cnn1", 16, 16, 1);
    auto* cnn2 = add_head_convolution(program, cnn1, "cnn2", 16, 16, 1);
    const std::vector<std::uint32_t> deconv_shape{16, 4, 4, 4};
    auto* deconv_weight = program.add_weight("encode.cnn3.weight", deconv_shape,
        deconv_shape, weights, device);
    auto* deconv_bias = program.add_weight("encode.cnn3.bias", {4}, {4}, weights, device);
    auto* deconv_descriptor = [MPSGraphConvolution2DOpDescriptor
        descriptorWithStrideInX:2 strideInY:2
        dilationRateInX:1 dilationRateInY:1 groups:1
        paddingLeft:1 paddingRight:1 paddingTop:1 paddingBottom:1
        paddingStyle:MPSGraphPaddingStyleExplicit
        dataLayout:MPSGraphTensorNamedDataLayoutNHWC
        weightsLayout:MPSGraphTensorNamedDataLayoutOIHW];
    program.output = [program.graph convolutionTranspose2DWithSourceTensor:cnn2
        weightsTensor:deconv_weight outputShape:@[@1, @(height), @(width), @4]
        descriptor:deconv_descriptor name:@"head.output.deconv"];
    program.output = [program.graph additionWithPrimaryTensor:program.output
        secondaryTensor:deconv_bias name:@"head.output.bias"];
    return program;
}

MPSGraphTensor* add_resconv(GraphProgram& program, MPSGraphTensor* source,
                            const std::string& prefix,
                            std::uint32_t channels) {
    const std::vector<std::uint32_t> shape{channels, channels, 3, 3};
    auto* weight = program.add_weight(prefix + ".conv.weight", shape, shape,
                                      *program.weight_map, program.device);
    auto* bias = program.add_weight(prefix + ".conv.bias", {channels}, {channels},
                                    *program.weight_map, program.device);
    auto* beta = program.add_weight(prefix + ".beta", {1, channels, 1, 1},
                                    {1, 1, 1, channels}, *program.weight_map, program.device);
    auto* descriptor = [MPSGraphConvolution2DOpDescriptor
        descriptorWithStrideInX:1 strideInY:1
        dilationRateInX:1 dilationRateInY:1 groups:1
        paddingLeft:1 paddingRight:1 paddingTop:1 paddingBottom:1
        paddingStyle:MPSGraphPaddingStyleExplicit
        dataLayout:MPSGraphTensorNamedDataLayoutNHWC
        weightsLayout:MPSGraphTensorNamedDataLayoutOIHW];
    auto* conv = [program.graph convolution2DWithSourceTensor:source
        weightsTensor:weight descriptor:descriptor name:nil];
    conv = [program.graph additionWithPrimaryTensor:conv secondaryTensor:bias name:nil];
    conv = [program.graph multiplicationWithPrimaryTensor:conv secondaryTensor:beta name:nil];
    conv = [program.graph additionWithPrimaryTensor:conv secondaryTensor:source name:nil];
    return [program.graph leakyReLUWithTensor:conv alpha:0.2 name:nil];
}

GraphProgram make_block_program(WeightMap& weights, std::vector<std::string>& required,
                                id<MTLDevice> device, Precision precision,
                                std::uint32_t width, std::uint32_t height,
                                std::uint32_t block_index, std::uint32_t scale) {
    constexpr std::array<std::uint32_t, 5> kChannels{192, 128, 96, 64, 32};
    constexpr std::array<std::uint32_t, 5> kBlockInputs{15, 24, 24, 24, 24};
    GraphProgram program;
    program.graph = [MPSGraph new];
    program.static_feeds = [NSMutableDictionary new];
    program.compile_feeds = [NSMutableDictionary new];
    program.required_weight_names = &required;
    program.precision = precision;
    program.width = width;
    program.height = height;
    program.input_channels = kBlockInputs[block_index];
    program.has_flow = block_index != 0;
    program.weight_map = &weights;
    program.device = device;

    const auto dtype = mps_type(precision);
    program.input = [program.graph placeholderWithShape:
        @[@1, @(height), @(width), @(program.input_channels)]
        dataType:dtype name:@"ifblock.input"];
    program.compile_feeds[program.input] = [[MPSGraphShapedType alloc]
        initWithShape:@[@1, @(height), @(width), @(program.input_channels)] dataType:dtype];
    MPSGraphTensor* scaled_x = program.resize(program.input, height / scale, width / scale);
    MPSGraphTensor* block_input = scaled_x;
    if (program.has_flow) {
        program.flow = [program.graph placeholderWithShape:@[@1, @(height), @(width), @13]
            dataType:dtype name:@"ifblock.flow"];
        program.compile_feeds[program.flow] = [[MPSGraphShapedType alloc]
            initWithShape:@[@1, @(height), @(width), @13] dataType:dtype];
        auto* flow_values = [program.graph sliceTensor:program.flow dimension:3
            start:0 length:4 name:@"ifblock.previous_flow"];
        auto* scaled_flow = program.resize(flow_values, height / scale, width / scale);
        auto* scalar = [program.graph constantWithScalar:(1.0 / double(scale)) dataType:dtype];
        scaled_flow = [program.graph multiplicationWithPrimaryTensor:scaled_flow
            secondaryTensor:scalar name:nil];
        block_input = [program.graph concatTensors:@[scaled_x, scaled_flow]
            dimension:3 name:nil];
    }

    const std::string prefix = "block" + std::to_string(block_index);
    const std::uint32_t channels = kChannels[block_index];
    auto* feature = program.conv2d(block_input, prefix + ".conv0.0.0",
        kBlockInputs[block_index] + (program.has_flow ? 4U : 0U), channels / 2, 3, 2);
    feature = program.conv2d(feature, prefix + ".conv0.1.0", channels / 2,
        channels, 3, 2);
    for (std::uint32_t layer = 0; layer < 8; ++layer) {
        feature = add_resconv(program, feature,
            prefix + ".convblock." + std::to_string(layer), channels);
    }

    const std::uint32_t conv_height = height / scale / 4;
    const std::uint32_t conv_width = width / scale / 4;
    auto* transposed = program.conv_transpose(feature, prefix + ".lastconv.0",
        channels, 52, conv_height, conv_width);
    auto* shuffled = [program.graph reshapeTensor:transposed
        withShape:@[@1, @(conv_height * 2), @(conv_width * 2), @13, @2, @2] name:nil];
    shuffled = [program.graph transposeTensor:shuffled
        permutation:@[@0, @1, @4, @2, @5, @3] name:nil];
    shuffled = [program.graph reshapeTensor:shuffled
        withShape:@[@1, @(height / scale), @(width / scale), @13] name:nil];
    auto* full_resolution = program.resize(shuffled, height, width);

    auto* flow_delta = [program.graph sliceTensor:full_resolution dimension:3 start:0 length:4 name:nil];
    auto* scale_scalar = [program.graph constantWithScalar:double(scale) dataType:dtype];
    flow_delta = [program.graph multiplicationWithPrimaryTensor:flow_delta
        secondaryTensor:scale_scalar name:nil];
    if (program.has_flow) {
        auto* previous_flow = [program.graph sliceTensor:program.flow dimension:3
            start:0 length:4 name:@"ifblock.previous_flow_accumulate"];
        program.output = [program.graph additionWithPrimaryTensor:previous_flow
            secondaryTensor:flow_delta name:@"ifblock.flow.accumulate"];
    } else {
        program.output = flow_delta;
    }
    auto* mask = [program.graph sliceTensor:full_resolution dimension:3 start:4 length:1 name:nil];
    auto* feat = [program.graph sliceTensor:full_resolution dimension:3 start:5 length:8 name:nil];
    program.output = [program.graph concatTensors:@[program.output, mask, feat]
        dimension:3 name:@"ifblock.output"];
    return program;
}

void compile_program(GraphProgram& program, id<MTLDevice> device) {
    NSMutableDictionary<MPSGraphTensor*, MPSGraphShapedType*>* feeds = program.compile_feeds;
    MPSGraphDevice* graph_device = [MPSGraphDevice deviceWithMTLDevice:device];
    program.executable = [program.graph compileWithDevice:graph_device
        feeds:feeds targetTensors:@[program.output] targetOperations:nil
        compilationDescriptor:nil];
    if (program.executable == nil) throw std::runtime_error("MPSGraph failed to compile a RIFE graph");
}

struct InferencePlan final {
    GraphProgram head;
    std::array<GraphProgram, 5> blocks;
    std::uint32_t model_width{};
    std::uint32_t model_height{};
    Precision precision{Precision::f32};
    RifeMode mode{RifeMode::quality};
    std::vector<std::string> required_weight_names;
};

struct PlanKey final {
    std::uint32_t model_width{};
    std::uint32_t model_height{};
    RifeMode mode{};
    Precision precision{};
    bool operator<(const PlanKey& other) const noexcept {
        return std::tie(model_width, model_height, mode, precision) <
               std::tie(other.model_width, other.model_height, other.mode, other.precision);
    }
};

struct ScratchSet final {
    __strong id<MTLBuffer> previous_pixels;
    __strong id<MTLBuffer> current_pixels;
    __strong id<MTLBuffer> previous_features;
    __strong id<MTLBuffer> current_features;
    __strong id<MTLBuffer> initial_input;
    __strong id<MTLBuffer> recurrent_input;
    std::array<__strong id<MTLBuffer>, 2> network_buffers;
    __strong id<MTLBuffer> final_output;
    __strong MPSGraphTensorData* previous_data;
    __strong MPSGraphTensorData* current_data;
    __strong MPSGraphTensorData* previous_features_data;
    __strong MPSGraphTensorData* current_features_data;
    __strong MPSGraphTensorData* initial_input_data;
    __strong MPSGraphTensorData* recurrent_input_data;
    std::array<__strong MPSGraphTensorData*, 2> network_data;
    __strong MPSGraphTensorData* final_data;
    std::size_t size_bytes{};
};

struct ScratchLease final {
    std::shared_ptr<ScratchSet> scratch;
    std::function<void()> notify_available;

    ~ScratchLease() {
        scratch.reset();
        if (notify_available) notify_available();
    }
};

struct PipelineSet final {
    __strong id<MTLComputePipelineState> preprocess;
    __strong id<MTLComputePipelineState> initial_pack;
    __strong id<MTLComputePipelineState> warp_pack;
    __strong id<MTLComputePipelineState> final_warp;
    __strong id<MTLComputePipelineState> postprocess;
};

id<MTLComputePipelineState> make_pipeline(id<MTLDevice> device, id<MTLLibrary> library,
                                          const std::string& name) {
    NSError* error = nil;
    NSString* function_name = [NSString stringWithUTF8String:name.c_str()];
    id<MTLFunction> function = [library newFunctionWithName:function_name];
    if (function == nil) throw std::runtime_error("Metal library is missing RIFE kernel " + name);
    id<MTLComputePipelineState> pipeline =
        [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) throw std::runtime_error(metal_error(error, "could not create RIFE Metal pipeline"));
    return pipeline;
}

PipelineSet make_pipelines(id<MTLDevice> device, id<MTLLibrary> library,
                           Precision precision) {
    const char* suffix = precision == Precision::f16 ? "f16" : "f32";
    PipelineSet result;
    result.preprocess = make_pipeline(device, library, std::string("rife_preprocess_") + suffix);
    result.initial_pack = make_pipeline(device, library, std::string("rife_pack_initial_") + suffix);
    result.warp_pack = make_pipeline(device, library, std::string("rife_warp_pack_") + suffix);
    result.final_warp = make_pipeline(device, library, std::string("rife_warp_final_") + suffix);
    result.postprocess = make_pipeline(device, library, std::string("rife_postprocess_") + suffix);
    return result;
}

std::uint32_t align_up(std::uint32_t value, std::uint32_t alignment) {
    if (value > std::numeric_limits<std::uint32_t>::max() - alignment + 1) {
        throw std::runtime_error("RIFE padded resolution overflows");
    }
    return (value + alignment - 1) / alignment * alignment;
}

id<MTLBuffer> make_private_buffer(id<MTLDevice> device, std::size_t length) {
    id<MTLBuffer> result = [device newBufferWithLength:length options:MTLResourceStorageModePrivate];
    if (result == nil) throw std::runtime_error("Metal failed to allocate RIFE inference scratch memory");
    return result;
}

MPSGraphTensorData* tensor_data(id<MTLBuffer> buffer, std::uint32_t width,
                                std::uint32_t height, std::uint32_t channels,
                                Precision precision) {
    return [[MPSGraphTensorData alloc] initWithMTLBuffer:buffer
        shape:@[@1, @(height), @(width), @(channels)] dataType:mps_type(precision)];
}

NSArray<MPSGraphTensorData*>* program_inputs(
    GraphProgram& program, MPSGraphTensorData* input_data,
    MPSGraphTensorData* flow_data = nil) {
    NSMutableArray<MPSGraphTensorData*>* inputs = [NSMutableArray array];
    for (MPSGraphTensor* tensor in program.executable.feedTensors) {
        if (tensor == program.input) {
            [inputs addObject:input_data];
        } else if (tensor == program.flow) {
            if (flow_data == nil) throw std::runtime_error("RIFE graph is missing its flow input");
            [inputs addObject:flow_data];
        } else {
            MPSGraphTensorData* data = program.static_feeds[tensor];
            if (data == nil) throw std::runtime_error("RIFE graph has an unbound weight input");
            [inputs addObject:data];
        }
    }
    return inputs;
}

void encode_graph(GraphProgram& program, MPSCommandBuffer* command_buffer,
                  MPSGraphTensorData* input, MPSGraphTensorData* output,
                  MPSGraphTensorData* flow = nil) {
    NSArray<MPSGraphTensorData*>* inputs = program_inputs(program, input, flow);
    [program.executable encodeToCommandBuffer:command_buffer inputsArray:inputs
        resultsArray:@[output] executionDescriptor:nil];
}

void dispatch(id<MTLComputeCommandEncoder> encoder,
              id<MTLComputePipelineState> pipeline,
              std::uint32_t width, std::uint32_t height) {
    [encoder setComputePipelineState:pipeline];
    const NSUInteger thread_width = std::max<NSUInteger>(
        1, std::min<NSUInteger>(pipeline.threadExecutionWidth, width));
    const NSUInteger thread_height = std::max<NSUInteger>(
        1, std::min<NSUInteger>(8, pipeline.maxTotalThreadsPerThreadgroup / thread_width));
    [encoder dispatchThreads:MTLSizeMake(width, height, 1)
        threadsPerThreadgroup:MTLSizeMake(thread_width, thread_height, 1)];
}

void encode_texture_preprocess(MPSCommandBuffer* command_buffer,
                               id<MTLComputePipelineState> pipeline,
                               id<MTLTexture> source, id<MTLBuffer> output,
                               std::uint32_t input_width, std::uint32_t input_height,
                               std::uint32_t valid_width, std::uint32_t valid_height,
                               std::uint32_t model_width, std::uint32_t model_height,
                               bool input_is_linear) {
    PreprocessParameters parameters{
        input_width, input_height, model_width, model_height,
        valid_width, valid_height,
        input_is_linear ? 1U : 0U};
    auto* encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) throw std::runtime_error("Metal could not create the RIFE preprocessing encoder");
    [encoder setTexture:source atIndex:0];
    [encoder setBuffer:output offset:0 atIndex:0];
    [encoder setBytes:&parameters length:sizeof(parameters) atIndex:1];
    dispatch(encoder, pipeline, model_width, model_height);
    [encoder endEncoding];
}

void encode_pack_initial(MPSCommandBuffer* command_buffer,
                         id<MTLComputePipelineState> pipeline,
                         id<MTLBuffer> previous, id<MTLBuffer> current,
                         id<MTLBuffer> previous_features, id<MTLBuffer> current_features,
                         id<MTLBuffer> output, std::uint32_t width,
                         std::uint32_t height, float interpolation) {
    PackParameters parameters{width, height, interpolation};
    auto* encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) throw std::runtime_error("Metal could not create the RIFE input packing encoder");
    [encoder setBuffer:previous offset:0 atIndex:0];
    [encoder setBuffer:current offset:0 atIndex:1];
    [encoder setBuffer:previous_features offset:0 atIndex:2];
    [encoder setBuffer:current_features offset:0 atIndex:3];
    [encoder setBuffer:output offset:0 atIndex:4];
    [encoder setBytes:&parameters length:sizeof(parameters) atIndex:5];
    dispatch(encoder, pipeline, width, height);
    [encoder endEncoding];
}

void encode_warp_pack(MPSCommandBuffer* command_buffer,
                      id<MTLComputePipelineState> pipeline,
                      id<MTLBuffer> previous, id<MTLBuffer> current,
                      id<MTLBuffer> previous_features, id<MTLBuffer> current_features,
                      id<MTLBuffer> network, id<MTLBuffer> output,
                      std::uint32_t width, std::uint32_t height, float interpolation) {
    PackParameters parameters{width, height, interpolation};
    auto* encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) throw std::runtime_error("Metal could not create the RIFE recurrent packing encoder");
    [encoder setBuffer:previous offset:0 atIndex:0];
    [encoder setBuffer:current offset:0 atIndex:1];
    [encoder setBuffer:previous_features offset:0 atIndex:2];
    [encoder setBuffer:current_features offset:0 atIndex:3];
    [encoder setBuffer:network offset:0 atIndex:4];
    [encoder setBuffer:output offset:0 atIndex:5];
    [encoder setBytes:&parameters length:sizeof(parameters) atIndex:6];
    dispatch(encoder, pipeline, width, height);
    [encoder endEncoding];
}

void encode_final_warp(MPSCommandBuffer* command_buffer,
                       id<MTLComputePipelineState> pipeline,
                       id<MTLBuffer> previous, id<MTLBuffer> current,
                       id<MTLBuffer> network, id<MTLBuffer> output,
                       std::uint32_t width, std::uint32_t height,
                       float interpolation) {
    PackParameters parameters{width, height, interpolation};
    auto* encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) throw std::runtime_error("Metal could not create the RIFE final warp encoder");
    [encoder setBuffer:previous offset:0 atIndex:0];
    [encoder setBuffer:current offset:0 atIndex:1];
    [encoder setBuffer:network offset:0 atIndex:2];
    [encoder setBuffer:output offset:0 atIndex:3];
    [encoder setBytes:&parameters length:sizeof(parameters) atIndex:4];
    dispatch(encoder, pipeline, width, height);
    [encoder endEncoding];
}

void encode_postprocess(MPSCommandBuffer* command_buffer,
                        id<MTLComputePipelineState> pipeline,
                        id<MTLTexture> previous, id<MTLTexture> current,
                        id<MTLTexture> output, id<MTLBuffer> model_output,
                        std::uint32_t output_width, std::uint32_t output_height,
                        std::uint32_t model_width, std::uint32_t model_height,
                        std::uint32_t valid_model_width,
                        std::uint32_t valid_model_height,
                        float interpolation, bool output_is_linear) {
    FinishParameters parameters{output_width, output_height, model_width, model_height,
        valid_model_width, valid_model_height, interpolation, output_is_linear ? 1U : 0U};
    auto* encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) throw std::runtime_error("Metal could not create the RIFE output encoder");
    [encoder setTexture:previous atIndex:0];
    [encoder setTexture:current atIndex:1];
    [encoder setTexture:output atIndex:2];
    [encoder setBuffer:model_output offset:0 atIndex:0];
    [encoder setBytes:&parameters length:sizeof(parameters) atIndex:1];
    dispatch(encoder, pipeline, output_width, output_height);
    [encoder endEncoding];
}

} // namespace

struct MetalRifeInvocation::Impl final {
    std::shared_ptr<const void> model;
    std::shared_ptr<const void> plan;
    std::unique_ptr<ScratchLease> scratch_lease;
    __strong NSMutableArray* retained_objects;
};

struct MetalRifeModel::Impl final {
    __strong id<MTLDevice> device;
    __strong id<MTLLibrary> kernel_library;
    __strong MPSGraphDevice* graph_device;
    WeightMap weights;
    std::map<PlanKey, std::shared_ptr<InferencePlan>> plans;
    std::deque<PlanKey> plan_order;
    std::map<PlanKey, std::vector<std::shared_ptr<ScratchSet>>> scratch_sets;
    std::map<Precision, PipelineSet> pipelines;
    std::mutex mutex;
    std::condition_variable scratch_available;
};

MetalRifeInvocation::MetalRifeInvocation() = default;
MetalRifeInvocation::~MetalRifeInvocation() = default;
MetalRifeInvocation::MetalRifeInvocation(MetalRifeInvocation&&) noexcept = default;
MetalRifeInvocation& MetalRifeInvocation::operator=(MetalRifeInvocation&&) noexcept = default;

MetalRifeModel::MetalRifeModel() : impl_(std::make_unique<Impl>()) {}
MetalRifeModel::~MetalRifeModel() = default;

std::shared_ptr<MetalRifeModel> MetalRifeModel::load(
    id<MTLDevice> device, id<MTLLibrary> kernel_library,
    const std::filesystem::path& weights_path, std::string& error) {
    try {
        if (device == nil || kernel_library == nil) {
            throw std::runtime_error("Metal device and RIFE kernel library are required");
        }
        auto model = std::shared_ptr<MetalRifeModel>(new MetalRifeModel());
        model->impl_->device = device;
        model->impl_->kernel_library = kernel_library;
        model->impl_->graph_device = [MPSGraphDevice deviceWithMTLDevice:device];
        model->impl_->weights = read_weights(weights_path);
        model->impl_->pipelines.emplace(Precision::f32,
            make_pipelines(device, kernel_library, Precision::f32));
        model->impl_->pipelines.emplace(Precision::f16,
            make_pipelines(device, kernel_library, Precision::f16));
        error.clear();
        return model;
    } catch (const std::exception& exception) {
        error = exception.what();
    } catch (...) {
        error = "unknown error loading RIFE model";
    }
    return {};
}

namespace {

std::shared_ptr<InferencePlan> make_plan(MetalRifeModel::Impl& model,
                                         const PlanKey& key) {
    const std::uint32_t width = key.model_width;
    const std::uint32_t height = key.model_height;
    auto plan = std::make_shared<InferencePlan>();
    plan->model_width = width;
    plan->model_height = height;
    plan->precision = key.precision;
    plan->mode = key.mode;
    plan->head = make_head_program(model.weights, plan->required_weight_names,
                                   model.device, key.precision, width, height);
    const std::array<std::uint32_t, 5> scales{16, 8, 4, 2, 1};
    for (std::uint32_t block = 0; block < plan->blocks.size(); ++block) {
        plan->blocks[block] = make_block_program(model.weights,
            plan->required_weight_names, model.device, key.precision,
            width, height, block, scales[block]);
    }
    compile_program(plan->head, model.device);
    for (auto& block : plan->blocks) compile_program(block, model.device);

    std::set<std::string> required(plan->required_weight_names.begin(),
                                   plan->required_weight_names.end());
    if (required.size() != model.weights.size()) {
        throw std::runtime_error("RIFE checkpoint tensor set does not match the v4.26 inference architecture");
    }
    for (const auto& [name, tensor] : model.weights) {
        (void)tensor;
        if (!required.contains(name)) throw std::runtime_error("unexpected RIFE checkpoint tensor: " + name);
    }
    return plan;
}

id<MTLBuffer> allocate_model_buffer(id<MTLDevice> device, std::size_t pixels,
                                    std::size_t channels, Precision precision) {
    const std::size_t element_size = precision == Precision::f16 ? sizeof(__fp16) : sizeof(float);
    if (pixels > std::numeric_limits<std::size_t>::max() / channels / element_size) {
        throw std::runtime_error("RIFE scratch buffer size overflow");
    }
    return make_private_buffer(device, pixels * channels * element_size);
}

std::size_t scratch_set_size_bytes(std::uint32_t width, std::uint32_t height,
                                   Precision precision) {
    const std::size_t element_size = precision == Precision::f16 ? sizeof(__fp16) : sizeof(float);
    if (width == 0 || height == 0 ||
        static_cast<std::size_t>(width) > std::numeric_limits<std::size_t>::max() / height) {
        throw std::runtime_error("invalid or overflowing RIFE scratch resolution");
    }
    const std::size_t pixels = static_cast<std::size_t>(width) * height;
    if (pixels > std::numeric_limits<std::size_t>::max() /
        kScratchScalarsPerPixel / element_size) {
        throw std::runtime_error("RIFE scratch buffer size overflow");
    }
    return pixels * kScratchScalarsPerPixel * element_size;
}

std::shared_ptr<ScratchSet> make_scratch_set(id<MTLDevice> device,
                                             const InferencePlan& plan) {
    const std::size_t pixels = static_cast<std::size_t>(plan.model_width) * plan.model_height;
    auto scratch = std::make_shared<ScratchSet>();
    scratch->size_bytes = scratch_set_size_bytes(plan.model_width, plan.model_height,
                                                 plan.precision);
    scratch->previous_pixels = allocate_model_buffer(device, pixels, 3, plan.precision);
    scratch->current_pixels = allocate_model_buffer(device, pixels, 3, plan.precision);
    scratch->previous_features = allocate_model_buffer(device, pixels, 4, plan.precision);
    scratch->current_features = allocate_model_buffer(device, pixels, 4, plan.precision);
    scratch->initial_input = allocate_model_buffer(device, pixels, 15, plan.precision);
    scratch->recurrent_input = allocate_model_buffer(device, pixels, 24, plan.precision);
    scratch->network_buffers = {
        allocate_model_buffer(device, pixels, kModelChannels, plan.precision),
        allocate_model_buffer(device, pixels, kModelChannels, plan.precision)};
    scratch->final_output = allocate_model_buffer(device, pixels, 3, plan.precision);
    scratch->previous_data = tensor_data(scratch->previous_pixels, plan.model_width,
        plan.model_height, 3, plan.precision);
    scratch->current_data = tensor_data(scratch->current_pixels, plan.model_width,
        plan.model_height, 3, plan.precision);
    scratch->previous_features_data = tensor_data(scratch->previous_features,
        plan.model_width, plan.model_height, 4, plan.precision);
    scratch->current_features_data = tensor_data(scratch->current_features,
        plan.model_width, plan.model_height, 4, plan.precision);
    scratch->initial_input_data = tensor_data(scratch->initial_input, plan.model_width,
        plan.model_height, 15, plan.precision);
    scratch->recurrent_input_data = tensor_data(scratch->recurrent_input, plan.model_width,
        plan.model_height, 24, plan.precision);
    for (std::size_t index = 0; index < scratch->network_data.size(); ++index) {
        scratch->network_data[index] = tensor_data(scratch->network_buffers[index],
            plan.model_width, plan.model_height, kModelChannels, plan.precision);
    }
    scratch->final_data = tensor_data(scratch->final_output, plan.model_width,
        plan.model_height, 3, plan.precision);
    return scratch;
}

std::unique_ptr<ScratchLease> acquire_scratch(MetalRifeModel::Impl& model,
    id<MTLDevice> device, const InferencePlan& plan, const PlanKey& key,
    std::size_t requested_size_bytes) {
    std::unique_lock lock(model.mutex);
    const auto count_sets = [&] {
        std::size_t count = 0;
        for (const auto& [unused, sets] : model.scratch_sets) {
            (void)unused;
            count += sets.size();
        }
        return count;
    };
    const auto total_bytes = [&] {
        std::size_t bytes = 0;
        for (const auto& [unused, sets] : model.scratch_sets) {
            (void)unused;
            for (const auto& set : sets) bytes += set->size_bytes;
        }
        return bytes;
    };
    const auto find_available = [&]() -> std::shared_ptr<ScratchSet> {
        auto found = model.scratch_sets.find(key);
        if (found == model.scratch_sets.end()) return {};
        for (const auto& candidate : found->second) {
            if (candidate.use_count() == 1) return candidate;
        }
        return {};
    };

    std::shared_ptr<ScratchSet> scratch = find_available();
    while (!scratch) {
        const std::size_t cached_bytes = total_bytes();
        if (count_sets() < kMaxCachedScratchSets &&
            cached_bytes <= kMaxCachedScratchBytes - requested_size_bytes) {
            scratch = make_scratch_set(device, plan);
            model.scratch_sets[key].push_back(scratch);
            break;
        }
        // Reuse the bounded cache for a new shape when a prior shape has no
        // live invocations. Active sets remain owned by their invocation until
        // its final command buffer completion handler releases it.
        bool removed_idle_set = false;
        for (auto entry = model.scratch_sets.begin(); entry != model.scratch_sets.end(); ++entry) {
            if (entry->first.model_width == key.model_width &&
                entry->first.model_height == key.model_height &&
                entry->first.mode == key.mode && entry->first.precision == key.precision) {
                continue;
            }
            auto& sets = entry->second;
            auto idle = std::find_if(sets.begin(), sets.end(),
                [](const auto& candidate) { return candidate.use_count() == 1; });
            if (idle != sets.end()) {
                sets.erase(idle);
                if (sets.empty()) model.scratch_sets.erase(entry);
                removed_idle_set = true;
                break;
            }
        }
        if (removed_idle_set) continue;
        model.scratch_available.wait(lock);
        scratch = find_available();
    }

    auto lease = std::make_unique<ScratchLease>();
    lease->scratch = std::move(scratch);
    auto* pool_mutex = &model.mutex;
    auto* availability_condition = &model.scratch_available;
    lease->notify_available = [pool_mutex, availability_condition] {
        std::lock_guard pool_lock(*pool_mutex);
        availability_condition->notify_one();
    };
    return lease;
}

} // namespace

MPSCommandBuffer* MetalRifeModel::encode(
    id<MTLCommandBuffer> command_buffer, id<MTLTexture> previous,
    id<MTLTexture> current, id<MTLTexture> output, float interpolation,
    RifeMode mode, bool input_is_linear, bool output_is_linear,
    std::shared_ptr<MetalRifeInvocation>& retained, std::string& error) {
    retained.reset();
    MPSCommandBuffer* graph_command_buffer = nil;
    try {
        if (command_buffer == nil || previous == nil || current == nil || output == nil) {
            throw std::runtime_error("RIFE encode requires a command buffer and three textures");
        }
        if (previous.width != current.width || previous.height != current.height ||
            output.width != previous.width || output.height != previous.height ||
            previous.width == 0 || previous.height == 0 ||
            previous.width > kMaxDimension || previous.height > kMaxDimension) {
            throw std::runtime_error("RIFE requires matching, non-empty texture dimensions");
        }
        if (!std::isfinite(interpolation) || interpolation < 0.0F || interpolation > 1.0F) {
            throw std::runtime_error("RIFE interpolation fraction must be finite and in [0, 1]");
        }

        const Precision precision = mode == RifeMode::balanced ? Precision::f16 : Precision::f32;
        const std::uint32_t valid_model_width = static_cast<std::uint32_t>(previous.width);
        const std::uint32_t valid_model_height = static_cast<std::uint32_t>(previous.height);
        const std::uint32_t model_width = align_up(valid_model_width, kPadMultiple);
        const std::uint32_t model_height = align_up(valid_model_height, kPadMultiple);
        const std::size_t scratch_size_bytes =
            scratch_set_size_bytes(model_width, model_height, precision);
        if (scratch_size_bytes > kMaxScratchSetBytes) {
            throw std::runtime_error(
                "RIFE padded resolution requires " +
                std::to_string(scratch_size_bytes / (1024ULL * 1024ULL)) +
                " MiB of scratch buffers, above the 1280 MiB per-request limit");
        }
        const PlanKey key{model_width, model_height, mode, precision};
        std::shared_ptr<InferencePlan> plan;
        {
            std::lock_guard lock(impl_->mutex);
            auto found = impl_->plans.find(key);
            if (found == impl_->plans.end()) {
                plan = make_plan(*impl_, key);
                if (impl_->plans.size() >= kMaxCachedPlans) {
                    const PlanKey evicted = impl_->plan_order.front();
                    impl_->plans.erase(evicted);
                    auto scratch_entry = impl_->scratch_sets.find(evicted);
                    if (scratch_entry != impl_->scratch_sets.end()) {
                        auto& sets = scratch_entry->second;
                        std::erase_if(sets, [](const auto& candidate) {
                            return candidate.use_count() == 1;
                        });
                        if (sets.empty()) impl_->scratch_sets.erase(scratch_entry);
                    }
                    impl_->plan_order.pop_front();
                }
                impl_->plans.emplace(key, plan);
                impl_->plan_order.push_back(key);
            } else {
                plan = found->second;
            }
        }

        auto scratch_lease = acquire_scratch(*impl_, impl_->device, *plan, key,
                                             scratch_size_bytes);
        ScratchSet& scratch = *scratch_lease->scratch;
        NSMutableArray* held = [NSMutableArray array];

        graph_command_buffer = [MPSCommandBuffer commandBufferWithCommandBuffer:command_buffer];
        if (graph_command_buffer == nil) {
            throw std::runtime_error("MPS could not wrap the RIFE command buffer");
        }

        auto invocation = std::make_shared<MetalRifeInvocation>();
        invocation->impl_ = std::make_unique<MetalRifeInvocation::Impl>();
        invocation->impl_->model = shared_from_this();
        invocation->impl_->plan = plan;
        invocation->impl_->scratch_lease = std::move(scratch_lease);
        [held addObjectsFromArray:@[previous, current, output, plan->head.graph,
                                    plan->head.executable]];
        for (auto& block : plan->blocks) {
            [held addObject:block.graph];
            [held addObject:block.executable];
        }
        invocation->impl_->retained_objects = held;
        retained = invocation;

        const auto& gpu_pipelines = impl_->pipelines.at(precision);
        encode_texture_preprocess(graph_command_buffer, gpu_pipelines.preprocess,
            previous, scratch.previous_pixels, static_cast<std::uint32_t>(previous.width),
            static_cast<std::uint32_t>(previous.height), valid_model_width,
            valid_model_height, plan->model_width, plan->model_height, input_is_linear);
        encode_texture_preprocess(graph_command_buffer, gpu_pipelines.preprocess,
            current, scratch.current_pixels, static_cast<std::uint32_t>(current.width),
            static_cast<std::uint32_t>(current.height), valid_model_width,
            valid_model_height, plan->model_width, plan->model_height, input_is_linear);

        encode_graph(plan->head, graph_command_buffer,
                     scratch.previous_data, scratch.previous_features_data);
        encode_graph(plan->head, graph_command_buffer,
                     scratch.current_data, scratch.current_features_data);

        encode_pack_initial(graph_command_buffer, gpu_pipelines.initial_pack,
            scratch.previous_pixels, scratch.current_pixels, scratch.previous_features,
            scratch.current_features, scratch.initial_input, plan->model_width,
            plan->model_height, interpolation);
        encode_graph(plan->blocks[0], graph_command_buffer, scratch.initial_input_data,
                     scratch.network_data[0]);

        for (std::uint32_t block = 1; block < 5; ++block) {
            const std::uint32_t previous_slot = (block - 1) & 1U;
            const std::uint32_t output_slot = block & 1U;
            encode_warp_pack(graph_command_buffer, gpu_pipelines.warp_pack,
                scratch.previous_pixels, scratch.current_pixels, scratch.previous_features,
                scratch.current_features, scratch.network_buffers[previous_slot],
                scratch.recurrent_input, plan->model_width,
                plan->model_height, interpolation);
            encode_graph(plan->blocks[block], graph_command_buffer,
                         scratch.recurrent_input_data, scratch.network_data[output_slot],
                         scratch.network_data[previous_slot]);
        }

        // Block four writes slot zero after block three has consumed slot one.
        encode_final_warp(graph_command_buffer, gpu_pipelines.final_warp,
            scratch.previous_pixels, scratch.current_pixels, scratch.network_buffers[0],
            scratch.final_output,
            plan->model_width, plan->model_height, interpolation);
        encode_postprocess(graph_command_buffer, gpu_pipelines.postprocess,
            previous, current, output, scratch.final_output,
            static_cast<std::uint32_t>(previous.width),
            static_cast<std::uint32_t>(previous.height), plan->model_width,
            plan->model_height, valid_model_width, valid_model_height,
            interpolation, output_is_linear);

        error.clear();
        return graph_command_buffer;
    } catch (const std::exception& exception) {
        error = exception.what();
    } catch (...) {
        error = "unknown error encoding RIFE inference";
    }
    return retained ? graph_command_buffer : nil;
}

} // namespace framegen::metal::detail
