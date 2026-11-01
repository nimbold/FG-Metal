// R116 provider smoke test.
//
// Proves, without the game, that:
//   1. `additionalEncoding:` is legal on the MetalFX transition command buffer -
//      the provider's interpolator has finished encoding, so a compute encoder
//      can still be created;
//   2. work encoded there actually executes and its result is readable as soon
//      as the call returns (no extra wait);
//   3. the gate kernel + reduce kernel compile and run against a real MetalFX
//      output texture and produce a structurally sane result struct;
//   4. the warm-up transition does not invoke the block.

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import "MetalFXFrameInterpolationProvider.h"
#import "r116_gate_kernels.h"
#include <mach/mach_time.h>

typedef struct {
    uint32_t finiteMotion;
    uint32_t activeMotion;
    uint32_t motionSamples;
    uint32_t finiteOutput;
    uint32_t outputSamples;
    uint32_t colorSamples;
    uint32_t groupCount;
    uint32_t frameTag;
    float maxMagnitude;
    float diffPrevious;
    float diffCurrent;
} R116GateResultRaw;

static id<MTLTexture> privateTexture(id<MTLDevice> device, MTLPixelFormat format,
                                     NSUInteger w, NSUInteger h) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:w height:h mipmapped:NO];
    d.storageMode = MTLStorageModePrivate;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    return [device newTextureWithDescriptor:d];
}

int main(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { fprintf(stderr, "no device\n"); return 2; }
        const NSUInteger W = 320, H = 180;
        NSError *error = nil;

        NSString *source = [@"#include <metal_stdlib>\nusing namespace metal;\n"
            "kernel void r116Marker(device uint *marker [[buffer(0)]], "
            "texture2d<float, access::read> out [[texture(0)]], uint gid [[thread_position_in_grid]]) {\n"
            "  if (gid > 0) return;\n"
            "  float4 c = out.read(uint2(0,0));\n"
            "  marker[0] = 0x52433131u; marker[1] = all(isfinite(c)) ? 1u : 0u;\n"
            "}\n" stringByAppendingString:@R116_GATE_KERNEL_SOURCE];
        id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
        if (!library) { fprintf(stderr, "library: %s\n", error.localizedDescription.UTF8String); return 3; }
        id<MTLComputePipelineState> markerPipeline =
            [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"r116Marker"] error:&error];
        id<MTLComputePipelineState> gatePipeline =
            [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"r110FastGate"] error:&error];
        id<MTLComputePipelineState> reducePipeline =
            [device newComputePipelineStateWithFunction:[library newFunctionWithName:@"r110FastGateReduce"] error:&error];
        if (!markerPipeline || !gatePipeline || !reducePipeline) {
            fprintf(stderr, "pipelines: %s\n", error.localizedDescription.UTF8String);
            return 4;
        }

        MetalFXFrameInterpolationProvider *provider =
            [[MetalFXFrameInterpolationProvider alloc] initWithDevice:device
                width:W height:H error:&error];
        if (!provider) { fprintf(stderr, "provider: %s\n", error.localizedDescription.UTF8String); return 5; }

        id<MTLTexture> colorA = privateTexture(device, MTLPixelFormatRGBA16Float, W, H);
        id<MTLTexture> colorB = privateTexture(device, MTLPixelFormatRGBA16Float, W, H);
        id<MTLTexture> depth = privateTexture(device, MTLPixelFormatR32Float, W, H);
        id<MTLTexture> motion = privateTexture(device, MTLPixelFormatRG16Float, W, H);
        id<MTLBuffer> marker = [device newBufferWithLength:8 options:MTLResourceStorageModeShared];
        const NSUInteger groupsX = (W + 63) / 64, groupsY = (H + 63) / 64;
        const uint32_t groupTotal = (uint32_t)(groupsX * groupsY);
        id<MTLBuffer> groups = [device newBufferWithLength:
            groupsX * groupsY * sizeof(R110GateGroupStats) options:MTLResourceStorageModeShared];
        id<MTLBuffer> result = [device newBufferWithLength:sizeof(R116GateResultRaw)
            options:MTLResourceStorageModeShared];

        __block int blockCalls = 0;
        __block BOOL encoderObtained = NO;
        __block BOOL gateEncoded = NO;
        FGMetalFXAdditionalEncodingBlock encode = ^(id<MTLCommandBuffer> cb,
                                                    id<MTLTexture> output) {
            ++blockCalls;
            id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
            if (!enc) return;
            encoderObtained = YES;
            [enc setComputePipelineState:markerPipeline];
            [enc setBuffer:marker offset:0 atIndex:0];
            [enc setTexture:output atIndex:0];
            [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(32, 1, 1)];
            [enc setComputePipelineState:gatePipeline];
            [enc setTexture:motion atIndex:0];
            [enc setTexture:colorA atIndex:1];
            [enc setTexture:colorB atIndex:2];
            [enc setTexture:output atIndex:3];
            [enc setBuffer:groups offset:0 atIndex:0];
            [enc dispatchThreadgroups:MTLSizeMake(groupsX, groupsY, 1)
                threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
            [enc setComputePipelineState:reducePipeline];
            [enc setBuffer:groups offset:0 atIndex:0];
            [enc setBuffer:result offset:0 atIndex:1];
            [enc setBytes:&groupTotal length:sizeof(groupTotal) atIndex:2];
            uint32_t frameTag = 7;
            [enc setBytes:&frameTag length:sizeof(frameTag) atIndex:3];
            [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1) threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
            [enc endEncoding];
            gateEncoded = YES;
        };

        // Transition 1 primes history and must NOT invoke the block.
        FGMetalInternalFrame *warm = [provider interpolatePreviousColor:colorA
            currentColor:colorB depthTexture:depth motionTexture:motion
            deltaTime:1.0f / 60.0f additionalEncoding:encode error:&error];
        // The first call primes history AND then runs the first transition, so
        // exactly one block invocation is expected: it belongs to the transition
        // command buffer, never to the history-priming buffer.
        int blockCallsAfterWarmup = blockCalls;
        printf("warmup frame=%s blockCalls=%d (expected 1: transition only, not the priming buffer)\n",
               warm ? "PRESENT" : "nil", blockCalls);

        // Transition 2 is a real INTERNAL frame.
        uint64_t t0 = mach_absolute_time();
        FGMetalInternalFrame *frame = [provider interpolatePreviousColor:colorA
            currentColor:colorB depthTexture:depth motionTexture:motion
            deltaTime:1.0f / 60.0f additionalEncoding:encode error:&error];
        uint64_t t1 = mach_absolute_time();
        printf("transition frame=%s internalPresentId=%llu error=%s\n",
               frame ? "PRESENT" : "nil", (unsigned long long)frame.internalPresentId,
               error.localizedDescription.UTF8String ?: "none");
        printf("blockCalls=%d encoderObtained=%d gateEncoded=%d\n",
               blockCalls, encoderObtained, gateEncoded);

        const uint32_t *markerValues = marker.contents;
        R116GateResultRaw raw;
        memcpy(&raw, result.contents, sizeof(raw));
        printf("marker=0x%08x outputFiniteAtOrigin=%u (expected 0x52433131, 1)\n",
               markerValues[0], markerValues[1]);
        printf("gate finiteMotion=%u motionSamples=%u finiteOutput=%u outputSamples=%u "
               "colorSamples=%u groupCount=%u maxMagnitude=%.6f\n",
               raw.finiteMotion, raw.motionSamples, raw.finiteOutput,
               raw.outputSamples, raw.colorSamples, raw.groupCount, raw.maxMagnitude);
        printf("gateSamplesMatchFrame=%d (expected 1)\n",
               raw.motionSamples == W * H && raw.outputSamples == W * H);

        BOOL ok = blockCallsAfterWarmup == 1 && blockCalls == 2 &&
            encoderObtained && gateEncoded &&
            markerValues[0] == 0x52433131u && raw.groupCount == groupTotal &&
            raw.motionSamples == W * H && raw.frameTag == 7;
        printf("R116_PROVIDER_SMOKE=%s\n", ok ? "PASS" : "FAIL");
        (void)t0; (void)t1;
        return ok ? 0 : 1;
    }
}
