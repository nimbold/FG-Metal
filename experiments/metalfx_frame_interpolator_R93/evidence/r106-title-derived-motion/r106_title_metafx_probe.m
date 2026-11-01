#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <mach/mach_time.h>
#import <math.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>
#import "../../MetalFXFrameInterpolationProvider.h"

static NSString *const kPrepareSource = @"#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "kernel void r106PrepareTitle(texture2d<float, access::read> source [[texture(0)]], "
    "texture2d<half, access::write> color [[texture(1)]], "
    "texture2d<float, access::write> depth [[texture(2)]], "
    "uint2 gid [[thread_position_in_grid]]) {\n"
    "  if (gid.x >= color.get_width() || gid.y >= color.get_height()) return;\n"
    "  float4 c = source.read(gid);\n"
    "  color.write(half4(half3(c.rgb), half(c.a)), gid);\n"
    "  depth.write(float4(0.25f, 0.0f, 0.0f, 0.0f), gid);\n"
    "}\n";

static NSData *readFile(NSString *path) {
    NSError *error = nil;
    NSData *data = [NSData dataWithContentsOfFile:path options:0 error:&error];
    if (!data) fprintf(stderr, "read failed path=%s error=%s\n", path.UTF8String,
                       error.localizedDescription.UTF8String ?: "unknown");
    return data;
}

static BOOL writeData(NSData *data, NSString *path) {
    NSError *error = nil;
    BOOL ok = [data writeToFile:path options:NSDataWritingAtomic error:&error];
    if (!ok) fprintf(stderr, "write failed path=%s error=%s\n", path.UTF8String,
                     error.localizedDescription.UTF8String ?: "unknown");
    return ok;
}

static id<MTLTexture> makeInput(id<MTLDevice> device, NSUInteger width,
                                NSUInteger height, const void *bytes) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                     width:width height:height mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> texture = [device newTextureWithDescriptor:d];
    if (texture) [texture replaceRegion:MTLRegionMake2D(0, 0, width, height)
                            mipmapLevel:0 withBytes:bytes bytesPerRow:width * 4];
    return texture;
}

static id<MTLTexture> makePrivateTexture(id<MTLDevice> device,
                                        MTLPixelFormat format,
                                        NSUInteger width, NSUInteger height,
                                        MTLTextureUsage usage) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height
                                 mipmapped:NO];
    d.storageMode = MTLStorageModePrivate;
    d.usage = usage | MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    return [device newTextureWithDescriptor:d];
}

static id<MTLTexture> makeSharedTexture(id<MTLDevice> device,
                                       MTLPixelFormat format,
                                       NSUInteger width, NSUInteger height,
                                       MTLTextureUsage usage) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height
                                 mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = usage | MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    return [device newTextureWithDescriptor:d];
}

static float halfToFloat(uint16_t h) {
    uint16_t sign = (h >> 15) & 1, exp = (h >> 10) & 31, mant = h & 1023;
    if (exp == 0) return sign ? -ldexpf((float)mant, -24) : ldexpf((float)mant, -24);
    if (exp == 31) return mant ? NAN : (sign ? -INFINITY : INFINITY);
    float v = ldexpf(1.0f + (float)mant / 1024.0f, (int)exp - 15);
    return sign ? -v : v;
}

static BOOL readback(id<MTLDevice> device, id<MTLCommandQueue> queue,
                     id<MTLTexture> source, id<MTLTexture> destination) {
    id<MTLCommandBuffer> cb = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [cb blitCommandEncoder];
    if (!cb || !blit) return NO;
    [blit copyFromTexture:source toTexture:destination];
    [blit endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
    return cb.status == MTLCommandBufferStatusCompleted && !cb.error;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 9) {
            fprintf(stderr, "usage: %s PREVIOUS.raw CURRENT.raw NEXT.raw MOTION.rg16f WIDTH HEIGHT OUTPUT.rgba16f INPUT_FORMAT(bgra8)\n", argv[0]);
            return 2;
        }
        NSUInteger width = (NSUInteger)strtoul(argv[5], NULL, 10);
        NSUInteger height = (NSUInteger)strtoul(argv[6], NULL, 10);
        if (strcmp(argv[8], "bgra8") != 0 || width == 0 || height == 0) {
            fprintf(stderr, "invalid format or dimensions\n");
            return 2;
        }
        NSString *previousPath = [NSString stringWithUTF8String:argv[1]];
        NSString *currentPath = [NSString stringWithUTF8String:argv[2]];
        NSString *nextPath = [NSString stringWithUTF8String:argv[3]];
        NSString *motionPath = [NSString stringWithUTF8String:argv[4]];
        NSString *outputPath = [NSString stringWithUTF8String:argv[7]];
        NSData *previousBytes = readFile(previousPath);
        NSData *currentBytes = readFile(currentPath);
        NSData *nextBytes = readFile(nextPath);
        NSData *motionBytes = readFile(motionPath);
        const NSUInteger pixelCount = width * height;
        if (!previousBytes || !currentBytes || !nextBytes || !motionBytes ||
            previousBytes.length != pixelCount * 4 ||
            currentBytes.length != pixelCount * 4 ||
            nextBytes.length != pixelCount * 4 ||
            motionBytes.length != pixelCount * 4) return 2;

        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device || ![MetalFXFrameInterpolationProvider supportsDevice:device]) {
            fprintf(stderr, "MetalFX frame interpolation unsupported\n");
            return 3;
        }
        MTLFXFrameInterpolatorDescriptor *descriptor =
            [MTLFXFrameInterpolatorDescriptor new];
        descriptor.colorTextureFormat = MTLPixelFormatRGBA16Float;
        descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
        descriptor.depthTextureFormat = MTLPixelFormatR32Float;
        descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
        descriptor.inputWidth = width;
        descriptor.inputHeight = height;
        descriptor.outputWidth = width;
        descriptor.outputHeight = height;
        id<MTLFXFrameInterpolator> usageProbe =
            [descriptor newFrameInterpolatorWithDevice:device];
        if (!usageProbe) {
            fprintf(stderr, "MetalFX usage probe allocation failed\n");
            return 3;
        }

        NSError *error = nil;
        MetalFXFrameInterpolationProvider *provider =
            [[MetalFXFrameInterpolationProvider alloc] initWithDevice:device
                width:width height:height error:&error];
        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!provider || !queue) {
            fprintf(stderr, "provider init failed: %s\n",
                error.localizedDescription.UTF8String ?: "command queue allocation failed");
            return 3;
        }

        id<MTLTexture> sources[3] = {
            makeInput(device, width, height, previousBytes.bytes),
            makeInput(device, width, height, currentBytes.bytes),
            makeInput(device, width, height, nextBytes.bytes),
        };
        id<MTLTexture> colors[3];
        for (NSUInteger i = 0; i < 3; ++i)
            colors[i] = makePrivateTexture(device, MTLPixelFormatRGBA16Float,
                width, height, usageProbe.colorTextureUsage);
        id<MTLTexture> depth = makePrivateTexture(device, MTLPixelFormatR32Float,
            width, height, usageProbe.depthTextureUsage);
        id<MTLTexture> motion = makeSharedTexture(device, MTLPixelFormatRG16Float,
            width, height, usageProbe.motionTextureUsage);
        id<MTLTexture> zeroMotion = makeSharedTexture(device, MTLPixelFormatRG16Float,
            width, height, usageProbe.motionTextureUsage);
        if (!sources[0] || !sources[1] || !sources[2] || !colors[0] ||
            !colors[1] || !colors[2] || !depth || !motion || !zeroMotion) {
            fprintf(stderr, "input texture allocation failed\n");
            return 3;
        }
        [motion replaceRegion:MTLRegionMake2D(0, 0, width, height)
                   mipmapLevel:0 withBytes:motionBytes.bytes bytesPerRow:width * 4];
        NSMutableData *zeroBytes = [NSMutableData dataWithLength:pixelCount * 4];
        [zeroMotion replaceRegion:MTLRegionMake2D(0, 0, width, height)
                        mipmapLevel:0 withBytes:zeroBytes.bytes bytesPerRow:width * 4];

        id<MTLLibrary> library = [device newLibraryWithSource:kPrepareSource
            options:nil error:&error];
        id<MTLFunction> function = [library newFunctionWithName:@"r106PrepareTitle"];
        id<MTLComputePipelineState> prepare = function
            ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
        if (!prepare) {
            fprintf(stderr, "preprocess pipeline failed: %s\n",
                error.localizedDescription.UTF8String ?: "unknown");
            return 3;
        }
        id<MTLCommandBuffer> prepareCB = [queue commandBuffer];
        for (NSUInteger i = 0; i < 3; ++i) {
            id<MTLComputeCommandEncoder> encoder = [prepareCB computeCommandEncoder];
            [encoder setComputePipelineState:prepare];
            [encoder setTexture:sources[i] atIndex:0];
            [encoder setTexture:colors[i] atIndex:1];
            [encoder setTexture:depth atIndex:2];
            [encoder dispatchThreads:MTLSizeMake(width, height, 1)
                threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
            [encoder endEncoding];
        }
        [prepareCB commit];
        [prepareCB waitUntilCompleted];
        if (prepareCB.status != MTLCommandBufferStatusCompleted || prepareCB.error) {
            fprintf(stderr, "GPU input preparation failed: %s\n",
                prepareCB.error.localizedDescription.UTF8String ?: "unknown");
            return 4;
        }

        static mach_timebase_info_data_t timebase;
        if (!timebase.denom) mach_timebase_info(&timebase);
        uint64_t start = mach_absolute_time();
        FGMetalInternalFrame *seed = [provider interpolatePreviousColor:colors[0]
            currentColor:colors[1] depthTexture:depth motionTexture:zeroMotion
            deltaTime:1.0f / 60.0f error:&error];
        if (error || seed) {
            fprintf(stderr, "history seed transition unexpected result=%p error=%s\n",
                (__bridge void *)seed, error.localizedDescription.UTF8String ?: "none");
            return 4;
        }
        error = nil;
        FGMetalInternalFrame *frame = [provider interpolatePreviousColor:colors[1]
            currentColor:colors[2] depthTexture:depth motionTexture:motion
            deltaTime:1.0f / 60.0f error:&error];
        uint64_t end = mach_absolute_time();
        if (!frame || error) {
            fprintf(stderr, "title MetalFX transition failed: %s\n",
                error.localizedDescription.UTF8String ?: "no internal frame returned");
            return 4;
        }

        MTLTextureDescriptor *readbackDescriptor = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                width:width height:height mipmapped:NO];
        readbackDescriptor.storageMode = MTLStorageModeShared;
        readbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        id<MTLTexture> outputReadback =
            [device newTextureWithDescriptor:readbackDescriptor];
        if (!outputReadback || !readback(device, queue, frame.texture, outputReadback)) {
            fprintf(stderr, "output readback failed\n");
            return 4;
        }
        NSMutableData *outputBytes = [NSMutableData dataWithLength:pixelCount * 8];
        [outputReadback getBytes:outputBytes.mutableBytes bytesPerRow:width * 8
            fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        if (!writeData(outputBytes, outputPath)) return 4;

        const uint16_t *out = (const uint16_t *)outputBytes.bytes;
        const uint8_t *previous = (const uint8_t *)currentBytes.bytes;
        const uint8_t *current = (const uint8_t *)nextBytes.bytes;
        double maePrevious = 0.0, maeCurrent = 0.0, alphaSum = 0.0;
        float alphaMin = INFINITY, alphaMax = -INFINITY;
        uint64_t finiteValues = 0, nonfiniteValues = 0, meaningfulPixels = 0, opaquePixels = 0;
        for (NSUInteger i = 0; i < pixelCount; ++i) {
            double luminance = 0.0;
            for (NSUInteger c = 0; c < 4; ++c) {
                float value = halfToFloat(out[i * 4 + c]);
                if (isfinite(value)) ++finiteValues; else ++nonfiniteValues;
                if (c == 3) {
                    if (isfinite(value)) {
                        if (value < alphaMin) alphaMin = value;
                        if (value > alphaMax) alphaMax = value;
                        alphaSum += value;
                        if (fabsf(value - 1.0f) <= 0.001f) ++opaquePixels;
                    }
                    continue;
                }
                const NSUInteger channel = c == 0 ? 2 : (c == 2 ? 0 : 1);
                float pv = (float)previous[i * 4 + channel] / 255.0f;
                float cv = (float)current[i * 4 + channel] / 255.0f;
                maePrevious += fabs((double)value - pv);
                maeCurrent += fabs((double)value - cv);
                luminance += value;
            }
            if (isfinite(luminance) && luminance > 0.15) ++meaningfulPixels;
        }
        const double colorDenom = (double)pixelCount * 3.0;
        maePrevious /= colorDenom;
        maeCurrent /= colorDenom;
        double alphaMean = isfinite(alphaMin) ? alphaSum / (double)pixelCount : NAN;
        printf("R106_TITLE_METAFX_OUTPUT status=READY device=%s dimensions=%lux%lu internalPresentId=%llu privateOutput=%d sourceSequenceTriplet=INPUT0,INPUT1,INPUT2 motionConvention=current-to-previous-pixels constantDepth=0.25 finiteValues=%llu nonfiniteValues=%llu meaningfulPixels=%llu maePrevious=%.8f maeCurrent=%.8f alphaMin=%.6f alphaMax=%.6f alphaMean=%.6f opaquePixels=%llu outputPath=%s elapsedMs=%.3f\n",
            device.name.UTF8String ?: "unknown", (unsigned long)width,
            (unsigned long)height, (unsigned long long)frame.internalPresentId,
            frame.texture.storageMode == MTLStorageModePrivate,
            (unsigned long long)finiteValues, (unsigned long long)nonfiniteValues,
            (unsigned long long)meaningfulPixels, maePrevious, maeCurrent,
            alphaMin, alphaMax, alphaMean, (unsigned long long)opaquePixels,
            outputPath.UTF8String,
            (double)(end - start) * (double)timebase.numer /
                (double)timebase.denom / 1e6);
        return 0;
    }
}
