#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <mach/mach_time.h>
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
    if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) {
        fprintf(stderr, "GPU_RUN_FAILED status=%lu error=%s\n",
            (unsigned long)commandBuffer.status,
            commandBuffer.error.localizedDescription.UTF8String ?: "none");
        return -1.0;
    }
    return commandBuffer.GPUEndTime > commandBuffer.GPUStartTime
        ? (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0 : 0.0;
}

static double median(double *samples, NSUInteger count) {
    double sorted[8] = {0};
    memcpy(sorted, samples, count * sizeof(double));
    for (NSUInteger i = 0; i < count; ++i) for (NSUInteger j = i + 1; j < count; ++j)
        if (sorted[j] < sorted[i]) { double value = sorted[i]; sorted[i] = sorted[j]; sorted[j] = value; }
    return sorted[count / 2];
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

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 7) {
            fprintf(stderr, "usage: %s sourceA.raw sourceB.raw width height shader.metal motion-out.rg16f\n", argv[0]);
            return 2;
        }
        NSUInteger width = (NSUInteger)strtoul(argv[3], NULL, 10);
        NSUInteger height = (NSUInteger)strtoul(argv[4], NULL, 10);
        NSString *aPath = [NSString stringWithUTF8String:argv[1]];
        NSString *bPath = [NSString stringWithUTF8String:argv[2]];
        NSString *shaderPath = [NSString stringWithUTF8String:argv[5]];
        NSString *flowPath = [NSString stringWithUTF8String:argv[6]];
        NSData *sourceA = [NSData dataWithContentsOfFile:aPath];
        NSData *sourceB = [NSData dataWithContentsOfFile:bPath];
        if (!width || !height || !sourceA || !sourceB ||
            sourceA.length != width * height * 4 || sourceB.length != width * height * 4) {
            fprintf(stderr, "INPUT_INVALID sourceA=%lu sourceB=%lu expected=%lu\n",
                (unsigned long)sourceA.length, (unsigned long)sourceB.length,
                (unsigned long)(width * height * 4));
            return 2;
        }

        NSError *error = nil;
        NSString *shader = [NSString stringWithContentsOfFile:shaderPath
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
        if (!device || !queue || !library || !downsample || !coarse || !refine || !propagate || !upsample) {
            fprintf(stderr, "PIPELINE_BUILD_FAILED error=%s\n",
                error.localizedDescription.UTF8String ?: "unknown");
            return 3;
        }

        NSUInteger quarterWidth = (width + 3) / 4;
        NSUInteger quarterHeight = (height + 3) / 4;
        NSUInteger gridWidth = (width + 39) / 40;
        NSUInteger gridHeight = (height + 39) / 40;
        MTLTextureUsage readWrite = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        id<MTLTexture> sharedA = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLTexture> sharedB = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLTexture> previous = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModePrivate, MTLTextureUsageShaderRead);
        id<MTLTexture> current = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModePrivate, MTLTextureUsageShaderRead);
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
        id<MTLTexture> motion = makeTexture(device, MTLPixelFormatRG16Float,
            width, height, MTLStorageModePrivate, readWrite);
        id<MTLTexture> sharedMotion = makeTexture(device, MTLPixelFormatRG16Float,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        if (!sharedA || !sharedB || !previous || !current || !previousQuarter || !currentQuarter ||
            !coarseGrid || !refinedGrid || !propagatedGrid || !motion || !sharedMotion) {
            fprintf(stderr, "TEXTURE_ALLOCATION_FAILED\n");
            return 4;
        }
        [sharedA replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
            withBytes:sourceA.bytes bytesPerRow:width * 4];
        [sharedB replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
            withBytes:sourceB.bytes bytesPerRow:width * 4];
        id<MTLCommandBuffer> upload = [queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [upload blitCommandEncoder];
        [blit copyFromTexture:sharedA sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(width,height,1)
            toTexture:previous destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
        [blit copyFromTexture:sharedB sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(width,height,1)
            toTexture:current destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
        [blit endEncoding];
        if (runBuffer(upload) < 0.0) return 5;

        double samples[7] = {0};
        printf("R110_COARSE_TO_FINE device=%s dimensions=%lux%lu pairInput=%s,%s quarter=%lux%lu grid=%lux%lu sourceStorage=Private allEstimatorStages=GPU\n",
            device.name.UTF8String ?: "unknown", (unsigned long)width, (unsigned long)height,
            aPath.lastPathComponent.UTF8String, bPath.lastPathComponent.UTF8String,
            (unsigned long)quarterWidth, (unsigned long)quarterHeight,
            (unsigned long)gridWidth, (unsigned long)gridHeight);
        for (NSUInteger repeat = 0; repeat < 7; ++repeat) {
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            BOOL ok = encodeCompute(cb, downsample, @[previous, current, previousQuarter, currentQuarter],
                    MTLSizeMake(16, 16, 1), MTLSizeMake((quarterWidth + 15) / 16, (quarterHeight + 15) / 16, 1)) &&
                encodeCompute(cb, coarse, @[previousQuarter, currentQuarter, coarseGrid],
                    MTLSizeMake(4, 4, 1), MTLSizeMake((gridWidth + 3) / 4, (gridHeight + 3) / 4, 1)) &&
                encodeCompute(cb, refine, @[previous, current, coarseGrid, refinedGrid],
                    MTLSizeMake(4, 4, 1), MTLSizeMake((gridWidth + 3) / 4, (gridHeight + 3) / 4, 1)) &&
                encodeCompute(cb, propagate, @[current, refinedGrid, propagatedGrid],
                    MTLSizeMake(4, 4, 1), MTLSizeMake((gridWidth + 3) / 4, (gridHeight + 3) / 4, 1)) &&
                encodeCompute(cb, upsample, @[propagatedGrid, motion],
                    MTLSizeMake(16, 16, 1), MTLSizeMake((width + 15) / 16, (height + 15) / 16, 1));
            if (!ok) return 6;
            samples[repeat] = runBuffer(cb);
            if (samples[repeat] < 0.0) return 7;
            printf("R110_ESTIMATOR_SAMPLE repeat=%lu warmup=%d gpuMs=%.3f\n",
                (unsigned long)repeat, repeat == 0, samples[repeat]);
        }
        printf("R110_ESTIMATOR_MEDIAN_GPU_MS=%.3f samplesExcludingWarmup=6\n", median(samples + 1, 6));

        id<MTLCommandBuffer> readback = [queue commandBuffer];
        blit = [readback blitCommandEncoder];
        [blit copyFromTexture:motion sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(width,height,1)
            toTexture:sharedMotion destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
        [blit endEncoding];
        if (runBuffer(readback) < 0.0) return 8;
        NSMutableData *flow = [NSMutableData dataWithLength:width * height * 4];
        [sharedMotion getBytes:flow.mutableBytes bytesPerRow:width * 4
            fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        if (![flow writeToFile:flowPath atomically:YES]) {
            fprintf(stderr, "FLOW_WRITE_FAILED path=%s\n", flowPath.UTF8String);
            return 9;
        }
        printf("R110_FLOW_SAVED path=%s bytes=%lu readback=post-estimator-diagnostic-only\n",
            flowPath.UTF8String, (unsigned long)flow.length);
        return 0;
    }
}
