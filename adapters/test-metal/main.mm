#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <MetalKit/MetalKit.h>

#include "framegen/frame_generator.hpp"
#include "framegen/metal.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <utility>

namespace {

constexpr std::uint32_t kImageWidth = 1280;
constexpr std::uint32_t kImageHeight = 720;

constexpr const char* kPatternShader = R"metal(
#include <metal_stdlib>
using namespace metal;

kernel void make_test_pattern(
    texture2d<float, access::write> output [[texture(0)]],
    constant uint& frame_index [[buffer(0)]],
    uint2 position [[thread_position_in_grid]]) {
    const float2 uv = (float2(position) + 0.5) /
                      float2(output.get_width(), output.get_height());
    const float center = frame_index == 0 ? 0.31 : 0.69;
    const float bar = 1.0 - smoothstep(0.035, 0.055, abs(uv.x - center));
    const float3 background = float3(0.025 + uv.y * 0.08,
                                     0.045 + uv.x * 0.11,
                                     0.12 + uv.y * 0.13);
    const float3 accent = frame_index == 0
        ? float3(0.12, 0.78, 0.96)
        : float3(1.0, 0.34, 0.12);
    const float3 color = mix(background, accent, bar);
    output.write(float4(color, 1.0), position);
}
)metal";

constexpr const char* kDisplayShader = R"metal(
#include <metal_stdlib>
using namespace metal;

struct DisplayVertex {
    float4 position [[position]];
    float2 uv;
};

vertex DisplayVertex display_vertex(uint vertex_id [[vertex_id]]) {
    const float2 positions[4] = {
        float2(-1.0,  1.0), float2( 1.0,  1.0),
        float2(-1.0, -1.0), float2( 1.0, -1.0)
    };
    const float2 coordinates[4] = {
        float2(0.0, 0.0), float2(1.0, 0.0),
        float2(0.0, 1.0), float2(1.0, 1.0)
    };
    DisplayVertex result;
    result.position = float4(positions[vertex_id], 0.0, 1.0);
    result.uv = coordinates[vertex_id];
    return result;
}

fragment float4 display_fragment(
    DisplayVertex in [[stage_in]],
    texture2d<float> image [[texture(0)]],
    sampler image_sampler [[sampler(0)]]) {
    return image.sample(image_sampler, in.uv);
}
)metal";

std::string ns_error_text(NSError* error, const char* fallback) {
    if (error == nil || error.localizedDescription == nil) {
        return fallback;
    }
    const char* text = error.localizedDescription.UTF8String;
    return text == nullptr ? fallback : text;
}

bool encode_test_patterns(id<MTLDevice> device,
                          id<MTLCommandQueue> queue,
                          id<MTLSharedEvent> ready_event,
                          id<MTLTexture> previous,
                          id<MTLTexture> current,
                          std::string& error_text) {
    NSError* error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:
        [NSString stringWithUTF8String:kPatternShader] options:nil error:&error];
    if (library == nil) {
        error_text = ns_error_text(error, "test pattern shader compilation failed");
        return false;
    }
    id<MTLFunction> function = [library newFunctionWithName:@"make_test_pattern"];
    if (function == nil) {
        error_text = "test pattern compute function was not found";
        return false;
    }
    id<MTLComputePipelineState> pipeline =
        [device newComputePipelineStateWithFunction:function error:&error];
    if (pipeline == nil) {
        error_text = ns_error_text(error, "test pattern pipeline creation failed");
        return false;
    }
    id<MTLCommandBuffer> command_buffer = [queue commandBuffer];
    if (command_buffer == nil) {
        error_text = "could not create test pattern command buffer";
        return false;
    }
    id<MTLComputeCommandEncoder> encoder = [command_buffer computeCommandEncoder];
    if (encoder == nil) {
        error_text = "could not create test pattern compute encoder";
        return false;
    }

    [encoder setComputePipelineState:pipeline];
    const NSUInteger threads_x = pipeline.threadExecutionWidth;
    const NSUInteger threads_y = std::max<NSUInteger>(
        1, std::min<NSUInteger>(8, pipeline.maxTotalThreadsPerThreadgroup / threads_x));
    const MTLSize threads_per_group = MTLSizeMake(threads_x, threads_y, 1);
    const MTLSize grid = MTLSizeMake(previous.width, previous.height, 1);
    std::uint32_t frame_index = 0;
    [encoder setTexture:previous atIndex:0];
    [encoder setBytes:&frame_index length:sizeof(frame_index) atIndex:0];
    [encoder dispatchThreads:grid threadsPerThreadgroup:threads_per_group];

    frame_index = 1;
    [encoder setTexture:current atIndex:0];
    [encoder setBytes:&frame_index length:sizeof(frame_index) atIndex:0];
    [encoder dispatchThreads:grid threadsPerThreadgroup:threads_per_group];
    [encoder endEncoding];
    [command_buffer encodeSignalEvent:ready_event value:1];
    [command_buffer commit];
    return true;
}

} // namespace

@interface OutputViewDelegate : NSObject <MTKViewDelegate> {
@private
    framegen::metal::Device _adapter;
    framegen::Texture _output;
    std::shared_ptr<framegen::GpuCompletion> _completion;
    framegen::metal::EventPoint _ready;
    __strong id<MTLCommandQueue> _display_queue;
    __strong id<MTLRenderPipelineState> _display_pipeline;
    __strong id<MTLSamplerState> _sampler;
}
- (instancetype)initWithDevice:(id<MTLDevice>)device
                       adapter:(const framegen::metal::Device*)adapter
                         frame:(const framegen::Texture*)frame
                    completion:(const std::shared_ptr<framegen::GpuCompletion>*)completion
                         event:(const framegen::metal::EventPoint*)event
                          view:(MTKView*)view
                         error:(NSError* __autoreleasing*)error;
@end

@implementation OutputViewDelegate

- (instancetype)initWithDevice:(id<MTLDevice>)device
                       adapter:(const framegen::metal::Device*)adapter
                         frame:(const framegen::Texture*)frame
                    completion:(const std::shared_ptr<framegen::GpuCompletion>*)completion
                         event:(const framegen::metal::EventPoint*)event
                          view:(MTKView*)view
                         error:(NSError* __autoreleasing*)out_error {
    self = [super init];
    if (self == nil) {
        return nil;
    }

    NSError* error = nil;
    id<MTLCommandQueue> queue = [device newCommandQueue];
    id<MTLLibrary> library = [device newLibraryWithSource:
        [NSString stringWithUTF8String:kDisplayShader] options:nil error:&error];
    id<MTLFunction> vertex = [library newFunctionWithName:@"display_vertex"];
    id<MTLFunction> fragment = [library newFunctionWithName:@"display_fragment"];
    MTLRenderPipelineDescriptor* pipeline_descriptor =
        [[MTLRenderPipelineDescriptor alloc] init];
    pipeline_descriptor.vertexFunction = vertex;
    pipeline_descriptor.fragmentFunction = fragment;
    pipeline_descriptor.colorAttachments[0].pixelFormat = view.colorPixelFormat;
    id<MTLRenderPipelineState> pipeline = queue == nil || library == nil ||
        vertex == nil || fragment == nil
        ? nil
        : [device newRenderPipelineStateWithDescriptor:pipeline_descriptor error:&error];
    if (pipeline == nil) {
        if (out_error != nullptr) {
            *out_error = [NSError errorWithDomain:@"FrameGenMetalTest"
                                              code:1
                                          userInfo:@{
                NSLocalizedDescriptionKey : [NSString stringWithUTF8String:
                    ns_error_text(error, "display pipeline creation failed").c_str()]
            }];
        }
        return nil;
    }

    MTLSamplerDescriptor* sampler_descriptor = [[MTLSamplerDescriptor alloc] init];
    sampler_descriptor.minFilter = MTLSamplerMinMagFilterLinear;
    sampler_descriptor.magFilter = MTLSamplerMinMagFilterLinear;
    sampler_descriptor.sAddressMode = MTLSamplerAddressModeClampToEdge;
    sampler_descriptor.tAddressMode = MTLSamplerAddressModeClampToEdge;
    id<MTLSamplerState> sampler = [device newSamplerStateWithDescriptor:sampler_descriptor];
    if (sampler == nil) {
        if (out_error != nullptr) {
            *out_error = [NSError errorWithDomain:@"FrameGenMetalTest"
                                              code:2
                                          userInfo:@{NSLocalizedDescriptionKey : @"sampler creation failed"}];
        }
        return nil;
    }

    if (adapter == nullptr || frame == nullptr || completion == nullptr ||
        event == nullptr || !*frame || !*completion || event->event == nil || event->value == 0) {
        if (out_error != nullptr) {
            *out_error = [NSError errorWithDomain:@"FrameGenMetalTest"
                                              code:3
                                          userInfo:@{NSLocalizedDescriptionKey : @"invalid display inputs"}];
        }
        return nil;
    }
    _adapter = *adapter;
    _output = *frame;
    _completion = *completion;
    _ready = *event;
    _display_queue = queue;
    _display_pipeline = pipeline;
    _sampler = sampler;
    return self;
}

- (void)mtkView:(MTKView*)view drawableSizeWillChange:(CGSize)size {
    (void)view;
    (void)size;
}

- (void)drawInMTKView:(MTKView*)view {
    // Retain the completion through presentation so its event and resource
    // owners outlive the display queue's queued wait and texture sampling.
    (void)_completion;
    MTLRenderPassDescriptor* pass = view.currentRenderPassDescriptor;
    id<CAMetalDrawable> drawable = view.currentDrawable;
    id<MTLTexture> image = _adapter.native_texture(_output);
    if (pass == nil || drawable == nil || image == nil || _ready.event == nil) {
        return;
    }

    id<MTLCommandBuffer> command_buffer = [_display_queue commandBuffer];
    if (command_buffer == nil) {
        return;
    }
    // Presentation consumes the generated GPU texture directly. The shared
    // event orders this renderer queue after frame generation without making
    // the CPU wait or mapping any pixels.
    [command_buffer encodeWaitForEvent:_ready.event value:_ready.value];
    id<MTLRenderCommandEncoder> encoder = [command_buffer renderCommandEncoderWithDescriptor:pass];
    if (encoder == nil) {
        return;
    }
    [encoder setRenderPipelineState:_display_pipeline];
    [encoder setFragmentTexture:image atIndex:0];
    [encoder setFragmentSamplerState:_sampler atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
    [encoder endEncoding];
    [command_buffer presentDrawable:drawable];
    [command_buffer commit];
}

@end

@interface FrameGenAppDelegate : NSObject <NSApplicationDelegate>
@end

@implementation FrameGenAppDelegate
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication*)sender {
    (void)sender;
    return YES;
}
@end

int main(int argc, const char* argv[]) {
    (void)argc;
    (void)argv;
    @autoreleasepool {
        id<MTLDevice> native_device = MTLCreateSystemDefaultDevice();
        if (native_device == nil) {
            std::fprintf(stderr, "FrameGen Metal test: no Metal device is available.\n");
            return 1;
        }

        auto adapter_result = framegen::metal::Device::create(native_device);
        if (!adapter_result) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         adapter_result.error().message.c_str());
            return 1;
        }
        framegen::metal::Device adapter = std::move(*adapter_result);

        const framegen::TextureDescriptor descriptor{
            kImageWidth,
            kImageHeight,
            framegen::PixelFormat::rgba8_unorm,
            framegen::ColorSpace::linear_srgb,
            framegen::AlphaMode::opaque,
        };
        auto previous_result = adapter.create_texture(descriptor);
        auto current_result = adapter.create_texture(descriptor);
        if (!previous_result || !current_result) {
            const auto& error = !previous_result ? previous_result.error()
                                                 : current_result.error();
            std::fprintf(stderr, "FrameGen Metal test: %s\n", error.message.c_str());
            return 1;
        }
        framegen::Texture previous = std::move(*previous_result);
        framegen::Texture current = std::move(*current_result);

        id<MTLTexture> previous_native = adapter.native_texture(previous);
        id<MTLTexture> current_native = adapter.native_texture(current);
        id<MTLCommandQueue> producer_queue = [native_device newCommandQueue];
        id<MTLSharedEvent> input_ready_event = [native_device newSharedEvent];
        if (previous_native == nil || current_native == nil || producer_queue == nil ||
            input_ready_event == nil) {
            std::fprintf(stderr, "FrameGen Metal test: could not create GPU input resources.\n");
            return 1;
        }

        std::string pattern_error;
        if (!encode_test_patterns(native_device, producer_queue, input_ready_event,
                                  previous_native, current_native, pattern_error)) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n", pattern_error.c_str());
            return 1;
        }

        auto dependency_result = adapter.make_event_dependency(
            framegen::metal::EventPoint{input_ready_event, 1});
        if (!dependency_result) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         dependency_result.error().message.c_str());
            return 1;
        }

        auto generator_result = framegen::FrameGenerator::create(adapter.backend());
        if (!generator_result) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         generator_result.error().message.c_str());
            return 1;
        }
        framegen::FrameGenerator generator = std::move(*generator_result);
        framegen::FrameSubmission submission{
            .previous = {.texture = previous,
                         .timing = {.sequence = 1, .timestamp_ns = 0,
                                    .clock_domain = 1},
                         .optional_inputs = {},
                         .color_metadata = {}},
            .current = {.texture = current,
                        .timing = {.sequence = 2, .timestamp_ns = 16'666'667,
                                   .clock_domain = 1},
                        .optional_inputs = {},
                        .color_metadata = {}},
            .interpolation = 0.5F,
            .reset_history = true,
            .gpu_dependencies = {*dependency_result},
        };
        auto generated_result = generator.submit(submission);
        if (!generated_result) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         generated_result.error().message.c_str());
            return 1;
        }
        framegen::GeneratedFrame generated = std::move(*generated_result);
        auto output_event_result = adapter.event_point(*generated.completion);
        if (!output_event_result) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         output_event_result.error().message.c_str());
            return 1;
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        FrameGenAppDelegate* app_delegate = [[FrameGenAppDelegate alloc] init];
        NSApp.delegate = app_delegate;

        NSRect content_rect = NSMakeRect(0, 0, 960, 540);
        NSWindow* window = [[NSWindow alloc]
            initWithContentRect:content_rect
                      styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
                        backing:NSBackingStoreBuffered
                          defer:NO];
        window.title = @"FrameGen Metal blend preview";
        [window center];

        MTKView* view = [[MTKView alloc] initWithFrame:content_rect device:native_device];
        view.colorPixelFormat = MTLPixelFormatBGRA8Unorm;
        view.clearColor = MTLClearColorMake(0.015, 0.02, 0.03, 1.0);
        view.preferredFramesPerSecond = 60;
        NSError* display_error = nil;
        OutputViewDelegate* view_delegate = [[OutputViewDelegate alloc]
            initWithDevice:native_device
                   adapter:&adapter
                     frame:&generated.texture
                completion:&generated.completion
                     event:&*output_event_result
                      view:view
                     error:&display_error];
        if (view_delegate == nil) {
            std::fprintf(stderr, "FrameGen Metal test: %s\n",
                         ns_error_text(display_error, "display initialization failed").c_str());
            return 1;
        }
        view.delegate = view_delegate;
        window.contentView = view;
        [window makeKeyAndOrderFront:nil];
        [NSApp activateIgnoringOtherApps:YES];
        [NSApp run];
    }
    return 0;
}
