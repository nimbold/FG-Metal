#pragma once

#ifndef __OBJC__
#error "The RIFE Metal implementation must be compiled as Objective-C++"
#endif

#import <Metal/Metal.h>
#import <MetalPerformanceShadersGraph/MetalPerformanceShadersGraph.h>

#include <filesystem>
#include <memory>
#include <string>

namespace framegen::metal::detail {

// These modes are private to the Metal implementation. Both run the complete
// v4.26 network at input resolution; BALANCED uses float16 arithmetic.
enum class RifeMode : unsigned char { quality, balanced };

class MetalRifeInvocation final {
public:
    MetalRifeInvocation();
    ~MetalRifeInvocation();
    MetalRifeInvocation(MetalRifeInvocation&&) noexcept;
    MetalRifeInvocation& operator=(MetalRifeInvocation&&) noexcept;
    MetalRifeInvocation(const MetalRifeInvocation&) = delete;
    MetalRifeInvocation& operator=(const MetalRifeInvocation&) = delete;

private:
    friend class MetalRifeModel;
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

class MetalRifeModel final : public std::enable_shared_from_this<MetalRifeModel> {
public:
    struct Impl;
    // Reads only the converted FGRWGT1 model file. The file is never modified
    // and model weights remain external to the repository.
    [[nodiscard]] static std::shared_ptr<MetalRifeModel> load(
        id<MTLDevice> device, id<MTLLibrary> kernel_library,
        const std::filesystem::path& weights_path, std::string& error);

    ~MetalRifeModel();
    MetalRifeModel(const MetalRifeModel&) = delete;
    MetalRifeModel& operator=(const MetalRifeModel&) = delete;

    // Encodes preprocessing, the five v4.26 IFBlocks, recurrent GPU warps, and
    // output conversion into the supplied queue command buffer. MPSGraph may
    // commit and continue the root command buffer; callers must use the
    // returned wrapper's current root and retain the invocation through the
    // final root's completion.
    //
    // Inputs are expected to be ready for GPU reads. `input_is_linear` asks
    // preprocessing to encode RGB into the sRGB-like domain used by RIFE;
    // output_is_linear asks the finishing kernel to decode model RGB from the
    // sRGB-like domain before writing linear RGB. Otherwise model RGB remains
    // sRGB-coded, including when written to an RGBA8Unorm texture. Alpha is
    // linearly interpolated from the two inputs.
    [[nodiscard]] MPSCommandBuffer* encode(
        id<MTLCommandBuffer> command_buffer,
        id<MTLTexture> previous, id<MTLTexture> current, id<MTLTexture> output,
        float interpolation, RifeMode mode, bool input_is_linear,
        bool output_is_linear, std::shared_ptr<MetalRifeInvocation>& retained,
        std::string& error);

private:
    MetalRifeModel();
    std::unique_ptr<Impl> impl_;
};

} // namespace framegen::metal::detail
