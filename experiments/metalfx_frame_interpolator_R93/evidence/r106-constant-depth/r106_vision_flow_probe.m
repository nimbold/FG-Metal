#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static BOOL readFile(const char *path, void *bytes, size_t length) {
    FILE *file = fopen(path, "rb");
    if (!file) return NO;
    BOOL ok = fread(bytes, 1, length, file) == length && fgetc(file) == EOF;
    fclose(file);
    return ok;
}

static CVPixelBufferRef makePixelBuffer(NSUInteger width, NSUInteger height,
                                        const uint8_t *source) {
    NSDictionary *attributes = @{
        (id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_32BGRA),
        (id)kCVPixelBufferWidthKey: @(width),
        (id)kCVPixelBufferHeightKey: @(height),
        (id)kCVPixelBufferMetalCompatibilityKey: @YES,
        (id)kCVPixelBufferIOSurfacePropertiesKey: @{},
    };
    CVPixelBufferRef pixelBuffer = NULL;
    CVReturn status = CVPixelBufferCreate(kCFAllocatorDefault, width, height,
        kCVPixelFormatType_32BGRA, (__bridge CFDictionaryRef)attributes,
        &pixelBuffer);
    if (status != kCVReturnSuccess || !pixelBuffer) return NULL;
    if (CVPixelBufferLockBaseAddress(pixelBuffer, 0) != kCVReturnSuccess) {
        CVPixelBufferRelease(pixelBuffer);
        return NULL;
    }
    uint8_t *destination = CVPixelBufferGetBaseAddress(pixelBuffer);
    size_t destinationRowBytes = CVPixelBufferGetBytesPerRow(pixelBuffer);
    for (NSUInteger y = 0; y < height; ++y)
        memcpy(destination + y * destinationRowBytes,
               source + y * width * 4, width * 4);
    CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);
    return pixelBuffer;
}

static BOOL isMain(const uint8_t *p) {
    return p[0] > 220 && p[1] > 160 && p[2] < 80;
}

static BOOL isMarker(const uint8_t *p) {
    return p[0] < 80 && p[1] > 100 && p[2] > 220;
}

static uint64_t nowNs(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int compareFloat(const void *a, const void *b) {
    float lhs = *(const float *)a, rhs = *(const float *)b;
    return (lhs > rhs) - (lhs < rhs);
}

static void summarize(const char *label, double sumX, double sumY,
                      const float *magnitudes, size_t count,
                      double expectedX, double expectedY) {
    if (!count) {
        printf("R106_FLOW_REGION=%s samples=0\n", label);
        return;
    }
    float *sorted = malloc(count * sizeof(float));
    if (!sorted) return;
    memcpy(sorted, magnitudes, count * sizeof(float));
    qsort(sorted, count, sizeof(float), compareFloat);
    double meanX = sumX / (double)count;
    double meanY = sumY / (double)count;
    double errorX = meanX - expectedX, errorY = meanY - expectedY;
    printf("R106_FLOW_REGION=%s samples=%zu meanX=%.6f meanY=%.6f medianMagnitude=%.6f p90Magnitude=%.6f expectedX=%.6f expectedY=%.6f vectorError=%.6f\n",
        label, count, meanX, meanY, sorted[count / 2],
        sorted[(count * 9) / 10], expectedX, expectedY,
        hypot(errorX, errorY));
    free(sorted);
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 7) {
            fprintf(stderr, "usage: %s PREVIOUS.raw CURRENT.raw WIDTH HEIGHT FLOW.rg32f ACCURACY(low|medium)\n", argv[0]);
            return 2;
        }
        const char *previousPath = argv[1], *currentPath = argv[2];
        NSUInteger width = (NSUInteger)strtoul(argv[3], NULL, 10);
        NSUInteger height = (NSUInteger)strtoul(argv[4], NULL, 10);
        const char *outputPath = argv[5], *accuracyText = argv[6];
        if (!width || !height || width > 4096 || height > 2160 ||
            strcmp(accuracyText, "low") && strcmp(accuracyText, "medium")) {
            fprintf(stderr, "invalid dimensions or accuracy\n");
            return 2;
        }
        size_t byteCount = (size_t)width * (size_t)height * 4;
        uint8_t *previousBytes = malloc(byteCount), *currentBytes = malloc(byteCount);
        if (!previousBytes || !currentBytes ||
            !readFile(previousPath, previousBytes, byteCount) ||
            !readFile(currentPath, currentBytes, byteCount)) {
            fprintf(stderr, "input read/size mismatch\n");
            free(previousBytes); free(currentBytes);
            return 3;
        }
        CVPixelBufferRef previous = makePixelBuffer(width, height, previousBytes);
        CVPixelBufferRef current = makePixelBuffer(width, height, currentBytes);
        if (!previous || !current) {
            fprintf(stderr, "CVPixelBuffer creation failed\n");
            if (previous) CVPixelBufferRelease(previous);
            if (current) CVPixelBufferRelease(current);
            free(previousBytes); free(currentBytes);
            return 4;
        }

        VNGenerateOpticalFlowRequest *request =
            [[VNGenerateOpticalFlowRequest alloc] initWithTargetedCVPixelBuffer:current
                options:@{}];
        request.computationAccuracy = !strcmp(accuracyText, "low")
            ? VNGenerateOpticalFlowRequestComputationAccuracyLow
            : VNGenerateOpticalFlowRequestComputationAccuracyMedium;
        request.outputPixelFormat = kCVPixelFormatType_TwoComponent32Float;
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
            initWithCVPixelBuffer:previous options:@{}];
        uint64_t begin = nowNs();
        NSError *error = nil;
        BOOL performed = [handler performRequests:@[request] error:&error];
        uint64_t end = nowNs();
        if (!performed || request.results.count == 0) {
            fprintf(stderr, "VISION_FLOW=FAILED accuracy=%s elapsedMs=%.3f error=%s\n",
                accuracyText, (double)(end - begin) / 1e6,
                error.localizedDescription.UTF8String ?: "no result");
            CVPixelBufferRelease(previous); CVPixelBufferRelease(current);
            free(previousBytes); free(currentBytes);
            return 5;
        }
        VNPixelBufferObservation *observation = request.results.firstObject;
        CVPixelBufferRef flow = observation.pixelBuffer;
        if (!flow || CVPixelBufferGetWidth(flow) != width ||
            CVPixelBufferGetHeight(flow) != height ||
            CVPixelBufferGetPixelFormatType(flow) != kCVPixelFormatType_TwoComponent32Float) {
            fprintf(stderr, "VISION_FLOW=FAILED reason=unexpected-output width=%zu height=%zu format=%u\n",
                flow ? CVPixelBufferGetWidth(flow) : 0,
                flow ? CVPixelBufferGetHeight(flow) : 0,
                flow ? CVPixelBufferGetPixelFormatType(flow) : 0);
            CVPixelBufferRelease(previous); CVPixelBufferRelease(current);
            free(previousBytes); free(currentBytes);
            return 6;
        }
        if (CVPixelBufferLockBaseAddress(flow, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
            fprintf(stderr, "VISION_FLOW=FAILED reason=output-lock\n");
            CVPixelBufferRelease(previous); CVPixelBufferRelease(current);
            free(previousBytes); free(currentBytes);
            return 7;
        }
        const uint8_t *flowBase = CVPixelBufferGetBaseAddress(flow);
        size_t flowRowBytes = CVPixelBufferGetBytesPerRow(flow);
        const uint64_t pixelCount = (uint64_t)width * height;
        float *flowBytes = malloc((size_t)pixelCount * 2 * sizeof(float));
        float *magnitudes = malloc((size_t)pixelCount * sizeof(float));
        float *mainMags = malloc((size_t)pixelCount * sizeof(float));
        float *markerMags = malloc((size_t)pixelCount * sizeof(float));
        float *staticMags = malloc((size_t)pixelCount * sizeof(float));
        if (!flowBytes || !magnitudes || !mainMags || !markerMags || !staticMags) {
            fprintf(stderr, "VISION_FLOW=FAILED reason=stats-allocation\n");
            CVPixelBufferUnlockBaseAddress(flow, kCVPixelBufferLock_ReadOnly);
            CVPixelBufferRelease(previous); CVPixelBufferRelease(current);
            free(previousBytes); free(currentBytes); free(flowBytes); free(magnitudes);
            free(mainMags); free(markerMags); free(staticMags);
            return 8;
        }
        uint64_t finite = 0, invalid = 0;
        double sumX = 0.0, sumY = 0.0;
        float minMagnitude = INFINITY, maxMagnitude = 0.0f;
        size_t mainCount = 0, markerCount = 0, staticCount = 0;
        double mainX = 0.0, mainY = 0.0, markerX = 0.0, markerY = 0.0;
        double staticX = 0.0, staticY = 0.0;
        for (NSUInteger y = 0; y < height; ++y) {
            const float *row = (const float *)(flowBase + y * flowRowBytes);
            float *destination = flowBytes + (size_t)y * width * 2;
            for (NSUInteger x = 0; x < width; ++x) {
                float vx = row[x * 2 + 0], vy = row[x * 2 + 1];
                destination[x * 2 + 0] = vx;
                destination[x * 2 + 1] = vy;
                if (!isfinite(vx) || !isfinite(vy)) { ++invalid; continue; }
                ++finite;
                sumX += vx; sumY += vy;
                float magnitude = hypotf(vx, vy);
                magnitudes[finite - 1] = magnitude;
                if (magnitude < minMagnitude) minMagnitude = magnitude;
                if (magnitude > maxMagnitude) maxMagnitude = magnitude;
                const uint8_t *pixel = currentBytes + ((size_t)y * width + x) * 4;
                BOOL main = isMain(pixel), marker = !main && isMarker(pixel);
                // Skip five pixels at the frame edge and trim colored-shape edges.
                BOOL erodedMain = main && x > 5 && y > 5 && x + 5 < width && y + 5 < height;
                BOOL erodedMarker = marker && x > 5 && y > 5 && x + 5 < width && y + 5 < height;
                if (erodedMain && isMain(pixel - 5 * 4) && isMain(pixel + 5 * 4) &&
                    isMain(pixel - 5 * (size_t)width * 4) && isMain(pixel + 5 * (size_t)width * 4)) {
                    mainMags[mainCount++] = magnitude; mainX += vx; mainY += vy;
                } else if (erodedMarker && isMarker(pixel - 5 * 4) && isMarker(pixel + 5 * 4) &&
                    isMarker(pixel - 5 * (size_t)width * 4) && isMarker(pixel + 5 * (size_t)width * 4)) {
                    markerMags[markerCount++] = magnitude; markerX += vx; markerY += vy;
                } else if (x < width / 10 || x >= width * 9 / 10 ||
                           y < height / 10 || y >= height * 9 / 10) {
                    staticMags[staticCount++] = magnitude;
                    staticX += vx; staticY += vy;
                }
            }
        }
        CVPixelBufferUnlockBaseAddress(flow, kCVPixelBufferLock_ReadOnly);
        FILE *out = fopen(outputPath, "wb");
        BOOL saved = out && fwrite(flowBytes, sizeof(float),
            (size_t)pixelCount * 2, out) == (size_t)pixelCount * 2;
        if (out) fclose(out);
        if (!saved) fprintf(stderr, "VISION_FLOW=FAILED reason=output-write path=%s\n", outputPath);
        printf("R106_VISION_FLOW=COMPLETED api=VNGenerateOpticalFlowRequest accuracy=%s revision=%lu input=CPU_BGRA_CVPixelBuffer output=TwoComponent32Float dimensions=%zux%zu elapsedMs=%.3f finite=%llu invalid=%llu minMagnitude=%.6f maxMagnitude=%.6f meanX=%.6f meanY=%.6f saved=%d path=%s\n",
            accuracyText, (unsigned long)request.revision, width, height,
            (double)(end - begin) / 1e6, (unsigned long long)finite,
            (unsigned long long)invalid, minMagnitude, maxMagnitude,
            finite ? sumX / finite : 0.0, finite ? sumY / finite : 0.0,
            saved, outputPath);
        summarize("foreground-main", mainX, mainY, mainMags, mainCount, -4.38679, 0.0);
        summarize("fast-marker", markerX, markerY, markerMags, markerCount, -15.0, 0.0);
        summarize("static-border", staticX, staticY, staticMags, staticCount, 0.0, 0.0);

        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        CVMetalTextureCacheRef cache = NULL;
        CVReturn cacheStatus = device ? CVMetalTextureCacheCreate(kCFAllocatorDefault,
            NULL, device, NULL, &cache) : kCVReturnError;
        CVMetalTextureRef metalFlow = NULL;
        CVReturn wrapStatus = (cacheStatus == kCVReturnSuccess && cache)
            ? CVMetalTextureCacheCreateTextureFromImage(kCFAllocatorDefault, cache,
                flow, NULL, MTLPixelFormatRG32Float, width, height, 0, &metalFlow)
            : kCVReturnError;
        id<MTLTexture> wrapped = metalFlow ? CVMetalTextureGetTexture(metalFlow) : nil;
        printf("R106_VISION_OUTPUT_TRANSPORT=%s outputPixelBufferFormat=%u iosurface=%d metalWrapStatus=%d metalFormat=%lu texture=%p\n",
            wrapped ? "IOSURFACE_METAL_WRAPPED" : "CPU_READBACK_OR_UPLOAD_REQUIRED",
            CVPixelBufferGetPixelFormatType(flow), CVPixelBufferGetIOSurface(flow) != NULL,
            (int)wrapStatus, (unsigned long)wrapped.pixelFormat,
            (__bridge void *)wrapped);
        if (metalFlow) CFRelease(metalFlow);
        if (cache) CFRelease(cache);
        CVPixelBufferRelease(previous); CVPixelBufferRelease(current);
        free(previousBytes); free(currentBytes); free(flowBytes); free(magnitudes);
        free(mainMags); free(markerMags); free(staticMags);
        return saved ? 0 : 9;
    }
}
