#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum {
    kWidth = 128,
    kHeight = 96,
    kObjectY0 = 32,
    kObjectY1 = 64,
    kObjectWidth = 16,
    kFrameCount = 4,
};

static const NSUInteger kFrameX[kFrameCount] = {16, 48, 80, 112};

static id<MTLTexture> makeTexture(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height,
                                  MTLTextureUsage usage, MTLStorageMode storage) {
    MTLTextureDescriptor *descriptor =
        [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                          width:width
                                                         height:height
                                                      mipmapped:NO];
    descriptor.usage = usage;
    descriptor.storageMode = storage;
    return [device newTextureWithDescriptor:descriptor];
}

static uint16_t floatToHalf(float value) {
    uint32_t bits = 0;
    memcpy(&bits, &value, sizeof(bits));
    const uint16_t sign = (uint16_t)((bits >> 16) & 0x8000u);
    int exponent = (int)((bits >> 23) & 0xffu) - 127 + 15;
    uint32_t mantissa = bits & 0x7fffffu;
    if (exponent <= 0) {
        if (exponent < -10) return sign;
        mantissa = (mantissa | 0x800000u) >> (1 - exponent);
        return (uint16_t)(sign | ((mantissa + 0x1000u) >> 13));
    }
    if (exponent >= 31) return (uint16_t)(sign | 0x7c00u);
    return (uint16_t)(sign | ((uint16_t)exponent << 10)
                      | ((mantissa + 0x1000u) >> 13));
}

static float halfToFloat(uint16_t value) {
    const uint32_t sign = (uint32_t)(value & 0x8000u) << 16;
    uint32_t exponent = (value >> 10) & 0x1fu;
    uint32_t mantissa = value & 0x03ffu;
    uint32_t bits = 0;
    if (exponent == 0) {
        if (mantissa == 0) {
            bits = sign;
        } else {
            int shift = 0;
            while ((mantissa & 0x0400u) == 0) {
                mantissa <<= 1;
                ++shift;
            }
            mantissa &= 0x03ffu;
            const uint32_t adjusted = (uint32_t)(127 - 15 - shift + 1);
            bits = sign | (adjusted << 23) | (mantissa << 13);
        }
    } else if (exponent == 0x1fu) {
        bits = sign | 0x7f800000u | (mantissa << 13);
    } else {
        exponent += 127 - 15;
        bits = sign | (exponent << 23) | (mantissa << 13);
    }
    float result = 0.0f;
    memcpy(&result, &bits, sizeof(result));
    return result;
}

static void fillColor(uint16_t *pixels, NSUInteger objectX) {
    const NSUInteger pixelCount = (NSUInteger)kWidth * kHeight;
    for (NSUInteger i = 0; i < pixelCount; ++i) pixels[i * 4 + 3] = 0x3c00u;
    for (NSUInteger y = kObjectY0; y < kObjectY1; ++y) {
        for (NSUInteger x = objectX; x < objectX + kObjectWidth; ++x) {
            const NSUInteger pixel = y * kWidth + x;
            pixels[pixel * 4 + 0] = 0x3c00u;
            pixels[pixel * 4 + 1] = 0x3c00u;
            pixels[pixel * 4 + 2] = 0x3c00u;
        }
    }
}

static void fillDepth(float *values, NSUInteger objectX) {
    for (NSUInteger i = 0; i < (NSUInteger)kWidth * kHeight; ++i) values[i] = 1.0f;
    for (NSUInteger y = kObjectY0; y < kObjectY1; ++y) {
        for (NSUInteger x = objectX; x < objectX + kObjectWidth; ++x)
            values[y * kWidth + x] = 0.25f;
    }
}

static void fillMotion(uint16_t *values, NSUInteger objectX, NSUInteger translation) {
    memset(values, 0, (NSUInteger)kWidth * kHeight * 2 * sizeof(uint16_t));
    // MetalFX vectors map each current-frame pixel back into the prior frame.
    const uint16_t motionX = floatToHalf(-(float)translation);
    for (NSUInteger y = kObjectY0; y < kObjectY1; ++y) {
        for (NSUInteger x = objectX; x < objectX + kObjectWidth; ++x)
            values[(y * kWidth + x) * 2] = motionX;
    }
}

static BOOL complete(id<MTLCommandBuffer> commandBuffer, const char *label) {
    if (!commandBuffer) {
        printf("%s=missing_command_buffer\n", label);
        return NO;
    }
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (commandBuffer.status != MTLCommandBufferStatusCompleted) {
        printf("%s=failed status=%ld error=%s\n", label, (long)commandBuffer.status,
               commandBuffer.error.localizedDescription.UTF8String ?: "none");
        return NO;
    }
    printf("%s=completed\n", label);
    return YES;
}

static float luminance(const uint16_t *pixel) {
    return (halfToFloat(pixel[0]) + halfToFloat(pixel[1]) + halfToFloat(pixel[2])) / 3.0f;
}

static void analyze(const uint16_t *pixels, NSUInteger previousX, NSUInteger currentX,
                    double *centroidX, double *centroidY, NSUInteger *maskCount,
                    double mae[3], double iou[3]) {
    const NSUInteger area = (NSUInteger)kWidth * kHeight;
    const NSUInteger midpointX = (previousX + currentX) / 2;
    const NSUInteger maskX[3] = {previousX, currentX, midpointX};
    double weightedX = 0.0, weightedY = 0.0;
    NSUInteger intersections[3] = {0, 0, 0};
    NSUInteger unions[3] = {0, 0, 0};
    double errors[3] = {0.0, 0.0, 0.0};
    NSUInteger count = 0;
    for (NSUInteger y = 0; y < kHeight; ++y) {
        for (NSUInteger x = 0; x < kWidth; ++x) {
            const float output = luminance(&pixels[(y * kWidth + x) * 4]);
            const BOOL outputMask = output > 0.5f;
            if (outputMask) {
                ++count;
                weightedX += x;
                weightedY += y;
            }
            for (NSUInteger i = 0; i < 3; ++i) {
                const BOOL expected = x >= maskX[i] && x < maskX[i] + kObjectWidth
                                   && y >= kObjectY0 && y < kObjectY1;
                intersections[i] += outputMask && expected;
                unions[i] += outputMask || expected;
                errors[i] += fabs((double)output - (expected ? 1.0 : 0.0));
            }
        }
    }
    *centroidX = count ? weightedX / count : NAN;
    *centroidY = count ? weightedY / count : NAN;
    *maskCount = count;
    for (NSUInteger i = 0; i < 3; ++i) {
        mae[i] = errors[i] / area;
        iou[i] = unions[i] ? (double)intersections[i] / unions[i] : 0.0;
    }
}

static BOOL readAndMeasure(id<MTLCommandQueue> queue, id<MTLTexture> output,
                           id<MTLTexture> readback, NSUInteger previousX,
                           NSUInteger currentX, NSUInteger step) {
    const NSUInteger rowBytes = (NSUInteger)kWidth * 4 * sizeof(uint16_t);
    id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [commandBuffer blitCommandEncoder];
    if (!commandBuffer || !blit) {
        printf("step%lu_readback=setup_failed\n", (unsigned long)step);
        return NO;
    }
    [blit copyFromTexture:output toTexture:readback];
    [blit endEncoding];
    char label[64];
    snprintf(label, sizeof(label), "step%lu_readback", (unsigned long)step);
    if (!complete(commandBuffer, label)) return NO;

    uint16_t *pixels = calloc((NSUInteger)kWidth * kHeight * 4, sizeof(uint16_t));
    if (!pixels) return NO;
    [readback getBytes:pixels bytesPerRow:rowBytes
            fromRegion:MTLRegionMake2D(0, 0, kWidth, kHeight) mipmapLevel:0];
    double centroidX = 0.0, centroidY = 0.0, mae[3] = {0.0, 0.0, 0.0};
    double iou[3] = {0.0, 0.0, 0.0};
    NSUInteger maskCount = 0;
    analyze(pixels, previousX, currentX, &centroidX, &centroidY,
            &maskCount, mae, iou);
    const double expectedMidX = (previousX + currentX) / 2.0
                              + (kObjectWidth - 1) / 2.0;
    const double expectedMidY = (kObjectY0 + kObjectY1 - 1) / 2.0;
    const BOOL intermediate = iou[2] >= 0.60 && mae[2] < mae[0] && mae[2] < mae[1]
                           && fabs(centroidX - expectedMidX) <= 3.0
                           && fabs(centroidY - expectedMidY) <= 3.0;
    const BOOL endpoint = iou[0] >= 0.95 || iou[1] >= 0.95;
    printf("step%lu_pair_x=%lu->%lu midpoint_x=%.2f output_mask=%lu centroid=(%.3f,%.3f)\n",
           (unsigned long)step, (unsigned long)previousX, (unsigned long)currentX,
           expectedMidX, (unsigned long)maskCount, centroidX, centroidY);
    printf("step%lu_mae=(previous=%.6f,current=%.6f,midpoint=%.6f)\n",
           (unsigned long)step, mae[0], mae[1], mae[2]);
    printf("step%lu_iou=(previous=%.6f,current=%.6f,midpoint=%.6f)\n",
           (unsigned long)step, iou[0], iou[1], iou[2]);
    printf("step%lu_class=%s\n", (unsigned long)step,
           intermediate ? "INTERMEDIATE" : endpoint ? "SOURCE_ENDPOINT" : "OTHER");
    free(pixels);
    return intermediate;
}

int main(void) {
    @autoreleasepool {
        const NSUInteger width = kWidth, height = kHeight;
        const NSUInteger colorRowBytes = width * 4 * sizeof(uint16_t);
        const NSUInteger motionRowBytes = width * 2 * sizeof(uint16_t);
        const NSUInteger depthRowBytes = width * sizeof(float);
        const NSUInteger translation = kFrameX[1] - kFrameX[0];
        for (NSUInteger i = 1; i < kFrameCount; ++i) {
            if (kFrameX[i] - kFrameX[i - 1] != translation) {
                puts("sequence=invalid_nonconstant_velocity");
                return 2;
            }
        }
        printf("sequence=A@%lu,B@%lu,C@%lu,D@%lu constant_velocity_pixels=%lu\n",
               (unsigned long)kFrameX[0], (unsigned long)kFrameX[1],
               (unsigned long)kFrameX[2], (unsigned long)kFrameX[3],
               (unsigned long)translation);
        printf("delta_time_seconds=%.8f reset_history=first_frame_only\n", 1.0 / 60.0);

        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { puts("device=none"); return 3; }
        printf("device=%s\n", device.name.UTF8String);
        if (![MTLFXFrameInterpolatorDescriptor supportsDevice:device]) {
            puts("supportsDevice=false");
            return 4;
        }

        MTLFXFrameInterpolatorDescriptor *descriptor = [MTLFXFrameInterpolatorDescriptor new];
        descriptor.colorTextureFormat = MTLPixelFormatRGBA16Float;
        descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
        descriptor.depthTextureFormat = MTLPixelFormatR32Float;
        descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
        descriptor.requiresPrevColorTexture = YES;
        descriptor.inputWidth = width;
        descriptor.inputHeight = height;
        descriptor.outputWidth = width;
        descriptor.outputHeight = height;
        id<MTLFXFrameInterpolator> interpolator =
            [descriptor newFrameInterpolatorWithDevice:device];
        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!interpolator || !queue) { puts("interpolator_or_queue=failed"); return 5; }

        NSMutableArray<id<MTLTexture>> *colors = [NSMutableArray arrayWithCapacity:kFrameCount];
        NSMutableArray<id<MTLTexture>> *depths = [NSMutableArray arrayWithCapacity:kFrameCount];
        NSMutableArray<id<MTLTexture>> *motions = [NSMutableArray arrayWithCapacity:kFrameCount];
        const MTLTextureUsage colorUsage = interpolator.colorTextureUsage | MTLTextureUsageShaderRead;
        const MTLTextureUsage depthUsage = interpolator.depthTextureUsage | MTLTextureUsageShaderRead;
        const MTLTextureUsage motionUsage = interpolator.motionTextureUsage | MTLTextureUsageShaderRead;
        for (NSUInteger i = 0; i < kFrameCount; ++i) {
            id<MTLTexture> color = makeTexture(device, descriptor.colorTextureFormat,
                width, height, colorUsage, MTLStorageModeShared);
            id<MTLTexture> depth = makeTexture(device, descriptor.depthTextureFormat,
                width, height, depthUsage, MTLStorageModeShared);
            id<MTLTexture> motion = makeTexture(device, descriptor.motionTextureFormat,
                width, height, motionUsage, MTLStorageModeShared);
            if (!color || !depth || !motion) { puts("input_texture_creation=failed"); return 6; }
            uint16_t *colorPixels = calloc(width * height * 4, sizeof(uint16_t));
            uint16_t *motionPixels = calloc(width * height * 2, sizeof(uint16_t));
            float *depthPixels = malloc(width * height * sizeof(float));
            if (!colorPixels || !motionPixels || !depthPixels) {
                puts("input_allocation=failed"); return 7;
            }
            fillColor(colorPixels, kFrameX[i]);
            fillDepth(depthPixels, kFrameX[i]);
            fillMotion(motionPixels, kFrameX[i], i == 0 ? 0 : translation);
            [color replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                       withBytes:colorPixels bytesPerRow:colorRowBytes];
            [depth replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                       withBytes:depthPixels bytesPerRow:depthRowBytes];
            [motion replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                       withBytes:motionPixels bytesPerRow:motionRowBytes];
            free(colorPixels); free(depthPixels); free(motionPixels);
            [colors addObject:color]; [depths addObject:depth]; [motions addObject:motion];
        }

        const MTLTextureUsage outputUsage = interpolator.outputTextureUsage | MTLTextureUsageShaderRead;
        id<MTLTexture> output = makeTexture(device, descriptor.outputTextureFormat,
            width, height, outputUsage, MTLStorageModePrivate);
        id<MTLTexture> readback = makeTexture(device, descriptor.outputTextureFormat,
            width, height, MTLTextureUsageShaderWrite | MTLTextureUsageShaderRead,
            MTLStorageModeShared);
        if (!output || !readback) { puts("output_texture_creation=failed"); return 8; }

        interpolator.colorTexture = colors[0];
        interpolator.prevColorTexture = colors[0];
        interpolator.depthTexture = depths[0];
        interpolator.motionTexture = motions[0];
        interpolator.outputTexture = output;
        interpolator.deltaTime = 1.0f / 60.0f;
        interpolator.nearPlane = 0.1f;
        interpolator.farPlane = 100.0f;
        interpolator.fieldOfView = 60.0f;
        interpolator.aspectRatio = (float)width / (float)height;
        interpolator.motionVectorScaleX = 1.0f;
        interpolator.motionVectorScaleY = 1.0f;
        interpolator.depthReversed = NO;
        interpolator.shouldResetHistory = YES;
        id<MTLCommandBuffer> warmup = [queue commandBuffer];
        [interpolator encodeToCommandBuffer:warmup];
        if (!complete(warmup, "warmup_A")) return 9;

        NSUInteger intermediateCount = 0;
        for (NSUInteger current = 1; current < kFrameCount; ++current) {
            const NSUInteger previous = current - 1;
            interpolator.colorTexture = colors[current];
            interpolator.prevColorTexture = colors[previous];
            interpolator.depthTexture = depths[current];
            interpolator.motionTexture = motions[current];
            interpolator.shouldResetHistory = NO;
            id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
            if (!commandBuffer) { puts("sequence_encode=missing_command_buffer"); return 10; }
            [interpolator encodeToCommandBuffer:commandBuffer];
            char label[64];
            snprintf(label, sizeof(label), "encode_%c", (int)('A' + current));
            if (!complete(commandBuffer, label)) return 11;
            if (readAndMeasure(queue, output, readback, kFrameX[previous],
                               kFrameX[current], current)) ++intermediateCount;
        }
        printf("intermediate_outputs=%lu/3\n", (unsigned long)intermediateCount);
        printf("METALFX_TEMPORAL_HISTORY_INTERMEDIATE=%s\n",
               intermediateCount > 0 ? "YES" : "NO");
        return 0;
    }
}
