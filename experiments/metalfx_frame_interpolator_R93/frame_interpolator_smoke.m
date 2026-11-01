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
    kObjectX0 = 16,
    kObjectX1 = 64,
    kObjectY0 = 32,
    kObjectY1 = 64,
    kObjectWidth = 16,
    kTranslation = kObjectX1 - kObjectX0,
};

static id<MTLTexture> makeTexture(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height,
                                  MTLTextureUsage usage, MTLStorageMode storage) {
    MTLTextureDescriptor *td = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format
                                                                                  width:width
                                                                                 height:height
                                                                              mipmapped:NO];
    td.usage = usage;
    td.storageMode = storage;
    return [device newTextureWithDescriptor:td];
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
                      | (uint16_t)((mantissa + 0x1000u) >> 13));
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
        exponent = exponent + (127 - 15);
        bits = sign | (exponent << 23) | (mantissa << 13);
    }
    float result = 0.0f;
    memcpy(&result, &bits, sizeof(result));
    return result;
}

static void fillColor(uint16_t *pixels, NSUInteger x0) {
    const NSUInteger count = (NSUInteger)kWidth * kHeight;
    for (NSUInteger i = 0; i < count; ++i) pixels[i * 4 + 3] = 0x3c00u;
    for (NSUInteger y = kObjectY0; y < kObjectY1; ++y) {
        for (NSUInteger x = x0; x < x0 + kObjectWidth; ++x) {
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

static void fillMotion(uint16_t *values, BOOL currentFrame) {
    memset(values, 0, (NSUInteger)kWidth * kHeight * 2 * sizeof(uint16_t));
    if (!currentFrame) return;
    // MetalFX vectors point from the current pixel to its location in the
    // previous frame. This scene moves right, so the current-frame vector is
    // the negative translation in top-left Metal coordinates.
    const uint16_t motionX = floatToHalf(-(float)kTranslation);
    for (NSUInteger y = kObjectY0; y < kObjectY1; ++y) {
        for (NSUInteger x = kObjectX1; x < kObjectX1 + kObjectWidth; ++x)
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

static void measure(const uint16_t *pixels, double *centroidX, double *centroidY,
                    NSUInteger *maskCount, double *maeA, double *maeB, double *maeMid,
                    double *iouA, double *iouB, double *iouMid) {
    const NSUInteger area = (NSUInteger)kWidth * kHeight;
    double weightedX = 0.0, weightedY = 0.0;
    NSUInteger count = 0, intersectionA = 0, intersectionB = 0, intersectionMid = 0;
    NSUInteger unionA = 0, unionB = 0, unionMid = 0;
    double errorA = 0.0, errorB = 0.0, errorMid = 0.0;
    const NSUInteger midX = (kObjectX0 + kObjectX1) / 2;
    for (NSUInteger y = 0; y < kHeight; ++y) {
        for (NSUInteger x = 0; x < kWidth; ++x) {
            const float output = luminance(&pixels[(y * kWidth + x) * 4]);
            const BOOL out = output > 0.5f;
            const BOOL inA = x >= kObjectX0 && x < kObjectX0 + kObjectWidth
                          && y >= kObjectY0 && y < kObjectY1;
            const BOOL inB = x >= kObjectX1 && x < kObjectX1 + kObjectWidth
                          && y >= kObjectY0 && y < kObjectY1;
            const BOOL inMid = x >= midX && x < midX + kObjectWidth
                            && y >= kObjectY0 && y < kObjectY1;
            if (out) {
                ++count;
                weightedX += x;
                weightedY += y;
            }
            intersectionA += out && inA;
            intersectionB += out && inB;
            intersectionMid += out && inMid;
            unionA += out || inA;
            unionB += out || inB;
            unionMid += out || inMid;
            errorA += fabs((double)output - (inA ? 1.0 : 0.0));
            errorB += fabs((double)output - (inB ? 1.0 : 0.0));
            errorMid += fabs((double)output - (inMid ? 1.0 : 0.0));
        }
    }
    *centroidX = count ? weightedX / count : NAN;
    *centroidY = count ? weightedY / count : NAN;
    *maskCount = count;
    *maeA = errorA / area;
    *maeB = errorB / area;
    *maeMid = errorMid / area;
    *iouA = unionA ? (double)intersectionA / unionA : 0.0;
    *iouB = unionB ? (double)intersectionB / unionB : 0.0;
    *iouMid = unionMid ? (double)intersectionMid / unionMid : 0.0;
}

int main(void) {
    @autoreleasepool {
        const NSUInteger width = kWidth, height = kHeight;
        const NSUInteger colorRowBytes = width * 4 * sizeof(uint16_t);
        const NSUInteger motionRowBytes = width * 2 * sizeof(uint16_t);
        const NSUInteger depthRowBytes = width * sizeof(float);
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { puts("device=none"); return 2; }
        printf("device=%s\n", device.name.UTF8String);
        printf("sdk_fraction_api=absent;deltaTime_is_source_interval=true\n");
        printf("explicit_fraction_requests=unsupported;tested_generated_frame=single_2x_intermediate\n");
        if (![MTLFXFrameInterpolatorDescriptor supportsDevice:device]) {
            puts("supportsDevice=false");
            return 3;
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
        if (!interpolator) { puts("interpolator_created=false"); return 4; }
        printf("interpolator_created=true requiresPrevColorTexture=%s\n",
               descriptor.requiresPrevColorTexture ? "true" : "false");
        printf("texture_usage=color:0x%lx output:0x%lx depth:0x%lx motion:0x%lx\n",
               (unsigned long)interpolator.colorTextureUsage,
               (unsigned long)interpolator.outputTextureUsage,
               (unsigned long)interpolator.depthTextureUsage,
               (unsigned long)interpolator.motionTextureUsage);
        printf("formats=color:%u output:%u depth:%u motion:%u\n",
               (unsigned)interpolator.colorTextureFormat,
               (unsigned)interpolator.outputTextureFormat,
               (unsigned)interpolator.depthTextureFormat,
               (unsigned)interpolator.motionTextureFormat);

        const MTLTextureUsage colorUsage = interpolator.colorTextureUsage | MTLTextureUsageShaderRead;
        const MTLTextureUsage depthUsage = interpolator.depthTextureUsage | MTLTextureUsageShaderRead;
        const MTLTextureUsage motionUsage = interpolator.motionTextureUsage | MTLTextureUsageShaderRead;
        const MTLTextureUsage outputUsage = interpolator.outputTextureUsage | MTLTextureUsageShaderRead;
        id<MTLTexture> previousColor = makeTexture(device, descriptor.colorTextureFormat,
            width, height, colorUsage, MTLStorageModeShared);
        id<MTLTexture> currentColor = makeTexture(device, descriptor.colorTextureFormat,
            width, height, colorUsage, MTLStorageModeShared);
        id<MTLTexture> previousDepth = makeTexture(device, descriptor.depthTextureFormat,
            width, height, depthUsage, MTLStorageModeShared);
        id<MTLTexture> currentDepth = makeTexture(device, descriptor.depthTextureFormat,
            width, height, depthUsage, MTLStorageModeShared);
        id<MTLTexture> previousMotion = makeTexture(device, descriptor.motionTextureFormat,
            width, height, motionUsage, MTLStorageModeShared);
        id<MTLTexture> currentMotion = makeTexture(device, descriptor.motionTextureFormat,
            width, height, motionUsage, MTLStorageModeShared);
        id<MTLTexture> output = makeTexture(device, descriptor.outputTextureFormat,
            width, height, outputUsage, MTLStorageModePrivate);
        if (!previousColor || !currentColor || !previousDepth || !currentDepth
                || !previousMotion || !currentMotion || !output) {
            puts("texture_creation=failed");
            return 5;
        }

        uint16_t *pixelsA = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *pixelsB = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *motionA = calloc(width * height * 2, sizeof(uint16_t));
        uint16_t *motionB = calloc(width * height * 2, sizeof(uint16_t));
        float *depthA = malloc(width * height * sizeof(float));
        float *depthB = malloc(width * height * sizeof(float));
        if (!pixelsA || !pixelsB || !motionA || !motionB || !depthA || !depthB) {
            puts("input_allocation=failed");
            return 6;
        }
        fillColor(pixelsA, kObjectX0);
        fillColor(pixelsB, kObjectX1);
        fillDepth(depthA, kObjectX0);
        fillDepth(depthB, kObjectX1);
        fillMotion(motionA, NO);
        fillMotion(motionB, YES);
        [previousColor replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                            withBytes:pixelsA bytesPerRow:colorRowBytes];
        [currentColor replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                           withBytes:pixelsB bytesPerRow:colorRowBytes];
        [previousDepth replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                            withBytes:depthA bytesPerRow:depthRowBytes];
        [currentDepth replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                           withBytes:depthB bytesPerRow:depthRowBytes];
        [previousMotion replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                             withBytes:motionA bytesPerRow:motionRowBytes];
        [currentMotion replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                            withBytes:motionB bytesPerRow:motionRowBytes];

        interpolator.colorTexture = previousColor;
        interpolator.prevColorTexture = previousColor;
        interpolator.depthTexture = previousDepth;
        interpolator.motionTexture = previousMotion;
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

        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!queue) { puts("command_queue=failed"); return 7; }
        id<MTLCommandBuffer> warmup = [queue commandBuffer];
        [interpolator encodeToCommandBuffer:warmup];
        if (!complete(warmup, "warmup")) return 8;

        interpolator.colorTexture = currentColor;
        interpolator.prevColorTexture = previousColor;
        interpolator.depthTexture = currentDepth;
        interpolator.motionTexture = currentMotion;
        interpolator.shouldResetHistory = NO;
        id<MTLCommandBuffer> interpolation = [queue commandBuffer];
        [interpolator encodeToCommandBuffer:interpolation];
        if (!complete(interpolation, "interpolation")) return 9;

        id<MTLTexture> readback = makeTexture(device, descriptor.outputTextureFormat,
            width, height, MTLTextureUsageShaderWrite | MTLTextureUsageShaderRead,
            MTLStorageModeShared);
        id<MTLCommandBuffer> readbackBuffer = [queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [readbackBuffer blitCommandEncoder];
        if (!readback || !readbackBuffer || !blit) {
            puts("output_readback=setup_failed");
            return 10;
        }
        [blit copyFromTexture:output toTexture:readback];
        [blit endEncoding];
        if (!complete(readbackBuffer, "output_readback")) return 11;

        uint16_t *result = calloc(width * height * 4, sizeof(uint16_t));
        if (!result) return 12;
        [readback getBytes:result bytesPerRow:colorRowBytes
                fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        double centroidX = 0.0, centroidY = 0.0, maeA = 0.0, maeB = 0.0, maeMid = 0.0;
        double iouA = 0.0, iouB = 0.0, iouMid = 0.0;
        NSUInteger maskCount = 0;
        measure(result, &centroidX, &centroidY, &maskCount,
                &maeA, &maeB, &maeMid, &iouA, &iouB, &iouMid);
        const double expectedX = (kObjectX0 + kObjectX1) / 2.0 + (kObjectWidth - 1) / 2.0;
        const double expectedY = (kObjectY0 + kObjectY1 - 1) / 2.0;
        const BOOL differsFromA = maeA > 0.02 && iouA < 0.35;
        const BOOL differsFromB = maeB > 0.02 && iouB < 0.35;
        const BOOL atMidpoint = fabs(centroidX - expectedX) <= 3.0
                             && fabs(centroidY - expectedY) <= 3.0
                             && iouMid >= 0.60 && maskCount >= 350 && maskCount <= 700;
        const BOOL pass = differsFromA && differsFromB && atMidpoint;
        printf("scene_translation_pixels=%d\n", kTranslation);
        printf("delta_time_seconds=%.8f\n", interpolator.deltaTime);
        printf("source_centroid_A_x=%.2f source_centroid_B_x=%.2f ground_truth_midpoint_x=%.2f\n",
               kObjectX0 + (kObjectWidth - 1) / 2.0,
               kObjectX1 + (kObjectWidth - 1) / 2.0, expectedX);
        printf("ground_truth_midpoint_y=%.2f output_mask_pixels=%lu output_centroid_x=%.3f output_centroid_y=%.3f\n",
               expectedY, (unsigned long)maskCount, centroidX, centroidY);
        printf("mae_to_A=%.6f mae_to_B=%.6f mae_to_midpoint=%.6f\n", maeA, maeB, maeMid);
        printf("mask_iou_A=%.6f mask_iou_B=%.6f mask_iou_midpoint=%.6f\n", iouA, iouB, iouMid);
        printf("differs_materially_from_A=%s differs_materially_from_B=%s midpoint_consistent=%s\n",
               differsFromA ? "true" : "false", differsFromB ? "true" : "false",
               atMidpoint ? "true" : "false");
        printf("METALFX_CONTROLLED_MIDPOINT=%s\n", pass ? "VALIDATED" : "NOT_VALIDATED");

        free(pixelsA); free(pixelsB); free(motionA); free(motionB);
        free(depthA); free(depthB); free(result);
        return pass ? 0 : 13;
    }
}
