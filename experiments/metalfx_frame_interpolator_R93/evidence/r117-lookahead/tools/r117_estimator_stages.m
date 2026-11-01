// R117 Phase 5 — per-stage estimator cost at the game's real resolution.
//
// R110's published isolated number is 10.4 ms, but that was measured at
// 1280x720. The title runs at 1470x956, which is 1.52x the pixels. This tool
// answers the only question that matters for throughput: where does the cost
// actually go, stage by stage, at the resolution the game uses?
//
// Every stage is encoded into its own command buffer so GPUEndTime-GPUStartTime
// attributes the cost to a single kernel. The estimator's total is measured
// separately in one command buffer, the way the product runs it, so the sum of
// the isolated stages can be compared against the real composite cost.
//
// usage: r117_estimator_stages sourceA.raw sourceB.raw width height shader.metal
//
// Build:
//   clang -fobjc-arc -O2 -framework Foundation -framework Metal \
//         r117_estimator_stages.m -o r117_estimator_stages

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>

static id<MTLTexture> makeTexture(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height,
                                  MTLStorageMode storage, MTLTextureUsage usage) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height mipmapped:NO];
    descriptor.storageMode = storage;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

static double runBuffer(id<MTLCommandBuffer> commandBuffer) {
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) return -1.0;
    return commandBuffer.GPUEndTime > commandBuffer.GPUStartTime
        ? (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0 : 0.0;
}

static int cmpDouble(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x < y) ? -1 : ((x > y) ? 1 : 0);
}

static double medianOf(double *v, int n) {
    if (n <= 0) return 0.0;
    double *s = malloc(sizeof(double) * (size_t)n);
    memcpy(s, v, sizeof(double) * (size_t)n);
    qsort(s, (size_t)n, sizeof(double), cmpDouble);
    double r = s[n / 2];
    free(s);
    return r;
}

static BOOL encodeCompute(id<MTLCommandBuffer> cb, id<MTLComputePipelineState> pipeline,
                          NSArray<id<MTLTexture>> *textures, MTLSize threads,
                          MTLSize groups) {
    id<MTLComputeCommandEncoder> encoder = [cb computeCommandEncoder];
    if (!encoder) return NO;
    [encoder setComputePipelineState:pipeline];
    for (NSUInteger i = 0; i < textures.count; ++i) [encoder setTexture:textures[i] atIndex:i];
    [encoder dispatchThreadgroups:groups threadsPerThreadgroup:threads];
    [encoder endEncoding];
    return YES;
}

#define SAMPLES 12

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 6) {
            fprintf(stderr, "usage: %s sourceA.raw sourceB.raw width height shader.metal\n", argv[0]);
            return 2;
        }
        NSUInteger width = (NSUInteger)strtoul(argv[3], NULL, 10);
        NSUInteger height = (NSUInteger)strtoul(argv[4], NULL, 10);
        NSData *sourceA = [NSData dataWithContentsOfFile:@(argv[1])];
        NSData *sourceB = [NSData dataWithContentsOfFile:@(argv[2])];
        if (!width || !height || !sourceA || !sourceB ||
            sourceA.length != width * height * 4 || sourceB.length != width * height * 4) {
            fprintf(stderr, "INPUT_INVALID\n");
            return 2;
        }

        NSError *error = nil;
        NSString *shader = [NSString stringWithContentsOfFile:@(argv[5])
                                                     encoding:NSUTF8StringEncoding error:&error];
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLLibrary> library = shader ? [device newLibraryWithSource:shader options:nil error:&error] : nil;
        id<MTLComputePipelineState> downsample = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r110Downsample"] error:&error];
        id<MTLComputePipelineState> coarse = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r110CoarseMatch"] error:&error];
        id<MTLComputePipelineState> refine = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r110FineRefine"] error:&error];
        id<MTLComputePipelineState> propagate = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r110Propagate"] error:&error];
        id<MTLComputePipelineState> upsample = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r110Upsample"] error:&error];
        if (!device || !library || !downsample || !coarse || !refine || !propagate || !upsample) {
            fprintf(stderr, "PIPELINE_BUILD_FAILED error=%s\n",
                    error.localizedDescription.UTF8String ?: "unknown");
            return 3;
        }

        NSUInteger quarterWidth = (width + 3) / 4;
        NSUInteger quarterHeight = (height + 3) / 4;
        NSUInteger gridWidth = (width + 39) / 40;
        NSUInteger gridHeight = (height + 39) / 40;
        MTLTextureUsage readWrite = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;

        id<MTLTexture> sharedA = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                                             MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLTexture> sharedB = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                                             MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLTexture> previous = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                                              MTLStorageModePrivate, MTLTextureUsageShaderRead);
        id<MTLTexture> current = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                                             MTLStorageModePrivate, MTLTextureUsageShaderRead);
        id<MTLTexture> previousQuarter = makeTexture(device, MTLPixelFormatRGBA16Float,
            quarterWidth, quarterHeight, MTLStorageModePrivate, readWrite);
        id<MTLTexture> currentQuarter = makeTexture(device, MTLPixelFormatRGBA16Float,
            quarterWidth, quarterHeight, MTLStorageModePrivate, readWrite);
        id<MTLTexture> coarseGrid = makeTexture(device, MTLPixelFormatRGBA32Float,
            gridWidth, gridHeight, MTLStorageModePrivate, readWrite);
        id<MTLTexture> refinedGrid = makeTexture(device, MTLPixelFormatRGBA32Float,
            gridWidth, gridHeight, MTLStorageModePrivate, readWrite);
        id<MTLTexture> propagatedGrid = makeTexture(device, MTLPixelFormatRGBA32Float,
            gridWidth, gridHeight, MTLStorageModePrivate, readWrite);
        id<MTLTexture> motion = makeTexture(device, MTLPixelFormatRG16Float, width, height,
                                            MTLStorageModePrivate, readWrite);

        [sharedA replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                     withBytes:sourceA.bytes bytesPerRow:width * 4];
        [sharedB replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
                     withBytes:sourceB.bytes bytesPerRow:width * 4];
        id<MTLCommandBuffer> upload = [queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [upload blitCommandEncoder];
        [blit copyFromTexture:sharedA sourceSlice:0 sourceLevel:0
                sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(width, height, 1)
                  toTexture:previous destinationSlice:0 destinationLevel:0
          destinationOrigin:MTLOriginMake(0, 0, 0)];
        [blit copyFromTexture:sharedB sourceSlice:0 sourceLevel:0
                sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(width, height, 1)
                  toTexture:current destinationSlice:0 destinationLevel:0
          destinationOrigin:MTLOriginMake(0, 0, 0)];
        [blit endEncoding];
        if (runBuffer(upload) < 0.0) return 4;

        MTLSize quarterGroups = MTLSizeMake((quarterWidth + 15) / 16, (quarterHeight + 15) / 16, 1);
        MTLSize gridGroups = MTLSizeMake((gridWidth + 3) / 4, (gridHeight + 3) / 4, 1);
        MTLSize fullGroups = MTLSizeMake((width + 15) / 16, (height + 15) / 16, 1);

        printf("R117_EST_STAGES device=%s dimensions=%lux%lu quarter=%lux%lu grid=%lux%lu\n",
               device.name.UTF8String ?: "unknown", (unsigned long)width, (unsigned long)height,
               (unsigned long)quarterWidth, (unsigned long)quarterHeight,
               (unsigned long)gridWidth, (unsigned long)gridHeight);

        // ---- isolated stages ----
        const char *names[5] = {"downsample", "coarse", "refine", "propagate", "upsample"};
        double stageMedians[5] = {0};

        for (int stage = 0; stage < 5; ++stage) {
            double samples[SAMPLES];
            for (int i = 0; i < SAMPLES; ++i) {
                id<MTLCommandBuffer> cb = [queue commandBuffer];
                BOOL ok = NO;
                switch (stage) {
                    case 0: ok = encodeCompute(cb, downsample,
                        @[previous, current, previousQuarter, currentQuarter],
                        MTLSizeMake(16, 16, 1), quarterGroups); break;
                    case 1: ok = encodeCompute(cb, coarse,
                        @[previousQuarter, currentQuarter, coarseGrid],
                        MTLSizeMake(4, 4, 1), gridGroups); break;
                    case 2: ok = encodeCompute(cb, refine,
                        @[previous, current, coarseGrid, refinedGrid],
                        MTLSizeMake(4, 4, 1), gridGroups); break;
                    case 3: ok = encodeCompute(cb, propagate,
                        @[current, refinedGrid, propagatedGrid],
                        MTLSizeMake(4, 4, 1), gridGroups); break;
                    default: ok = encodeCompute(cb, upsample,
                        @[propagatedGrid, motion],
                        MTLSizeMake(16, 16, 1), fullGroups); break;
                }
                if (!ok) return 5;
                samples[i] = runBuffer(cb);
                if (samples[i] < 0.0) return 6;
            }
            // Drop the first sample: it pays the pipeline's cold-cache cost.
            stageMedians[stage] = medianOf(samples + 1, SAMPLES - 1);
            printf("R117_EST_STAGE name=%s isolatedMedianMs=%.4f\n", names[stage], stageMedians[stage]);
        }

        double stageSum = 0.0;
        for (int i = 0; i < 5; ++i) stageSum += stageMedians[i];
        printf("R117_EST_STAGE_SUM sumOfIsolatedMediansMs=%.4f\n", stageSum);

        // ---- composite: the way the product actually runs it ----
        double composite[SAMPLES];
        for (int i = 0; i < SAMPLES; ++i) {
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            BOOL ok = encodeCompute(cb, downsample,
                    @[previous, current, previousQuarter, currentQuarter],
                    MTLSizeMake(16, 16, 1), quarterGroups) &&
                encodeCompute(cb, coarse, @[previousQuarter, currentQuarter, coarseGrid],
                    MTLSizeMake(4, 4, 1), gridGroups) &&
                encodeCompute(cb, refine, @[previous, current, coarseGrid, refinedGrid],
                    MTLSizeMake(4, 4, 1), gridGroups) &&
                encodeCompute(cb, propagate, @[current, refinedGrid, propagatedGrid],
                    MTLSizeMake(4, 4, 1), gridGroups) &&
                encodeCompute(cb, upsample, @[propagatedGrid, motion],
                    MTLSizeMake(16, 16, 1), fullGroups);
            if (!ok) return 7;
            composite[i] = runBuffer(cb);
            if (composite[i] < 0.0) return 8;
        }
        double compositeMedian = medianOf(composite + 1, SAMPLES - 1);
        printf("R117_EST_COMPOSITE medianGpuMs=%.4f sumOfStagesMs=%.4f overheadMs=%.4f\n",
               compositeMedian, stageSum, compositeMedian - stageSum);
        return 0;
    }
}
