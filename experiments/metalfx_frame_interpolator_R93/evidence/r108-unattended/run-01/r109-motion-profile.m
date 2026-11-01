#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>

typedef struct {
    id<MTLComputePipelineState> block;
    id<MTLComputePipelineState> propagate;
    id<MTLComputePipelineState> upsample;
    id<MTLComputePipelineState> prepare;
} Pipelines;

static id<MTLTexture> makeTexture(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height,
                                  MTLStorageMode storage, MTLTextureUsage usage) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height mipmapped:NO];
    descriptor.storageMode = storage;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

static BOOL encodePass(id<MTLCommandBuffer> commandBuffer, NSUInteger pass,
                       Pipelines pipelines, id<MTLTexture> previous,
                       id<MTLTexture> current, id<MTLTexture> grid,
                       id<MTLTexture> propagated, id<MTLTexture> motion,
                       id<MTLTexture> color, id<MTLTexture> depth,
                       NSUInteger width, NSUInteger height,
                       NSUInteger gridWidth, NSUInteger gridHeight) {
    id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
    if (!encoder) return NO;
    if (pass == 0) {
        [encoder setComputePipelineState:pipelines.block];
        [encoder setTexture:previous atIndex:0];
        [encoder setTexture:current atIndex:1];
        [encoder setTexture:grid atIndex:2];
        MTLSize group = MTLSizeMake(4, 4, 1);
        [encoder dispatchThreadgroups:MTLSizeMake((gridWidth + 3) / 4,
            (gridHeight + 3) / 4, 1) threadsPerThreadgroup:group];
    } else if (pass == 1) {
        [encoder setComputePipelineState:pipelines.propagate];
        [encoder setTexture:current atIndex:0];
        [encoder setTexture:grid atIndex:1];
        [encoder setTexture:propagated atIndex:2];
        MTLSize group = MTLSizeMake(4, 4, 1);
        [encoder dispatchThreadgroups:MTLSizeMake((gridWidth + 3) / 4,
            (gridHeight + 3) / 4, 1) threadsPerThreadgroup:group];
    } else if (pass == 2) {
        [encoder setComputePipelineState:pipelines.upsample];
        [encoder setTexture:propagated atIndex:0];
        [encoder setTexture:motion atIndex:1];
        MTLSize group = MTLSizeMake(16, 16, 1);
        [encoder dispatchThreadgroups:MTLSizeMake((width + 15) / 16,
            (height + 15) / 16, 1) threadsPerThreadgroup:group];
    } else {
        [encoder setComputePipelineState:pipelines.prepare];
        [encoder setTexture:current atIndex:0];
        [encoder setTexture:color atIndex:1];
        [encoder setTexture:depth atIndex:2];
        MTLSize group = MTLSizeMake(16, 16, 1);
        [encoder dispatchThreadgroups:MTLSizeMake((width + 15) / 16,
            (height + 15) / 16, 1) threadsPerThreadgroup:group];
    }
    [encoder endEncoding];
    return YES;
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

static double median(double values[4]) {
    double sorted[4];
    memcpy(sorted, values, sizeof(sorted));
    for (int i = 0; i < 4; ++i) for (int j = i + 1; j < 4; ++j)
        if (sorted[j] < sorted[i]) { double value = sorted[i]; sorted[i] = sorted[j]; sorted[j] = value; }
    return (sorted[1] + sorted[2]) * 0.5;
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
        id<MTLLibrary> library = shader ? [device newLibraryWithSource:shader
            options:nil error:&error] : nil;
        Pipelines p = {0};
        p.block = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r108BlockMatch"] error:&error];
        p.propagate = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r108Propagate"] error:&error];
        p.upsample = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r108Upsample"] error:&error];
        p.prepare = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"r108Prepare"] error:&error];
        if (!device || !queue || !library || !p.block || !p.propagate || !p.upsample || !p.prepare) {
            fprintf(stderr, "PIPELINE_BUILD_FAILED error=%s\n",
                error.localizedDescription.UTF8String ?: "unknown");
            return 3;
        }

        NSUInteger gridWidth = (width + 39) / 40;
        NSUInteger gridHeight = (height + 39) / 40;
        id<MTLTexture> sharedA = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLTexture> sharedB = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        [sharedA replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
            withBytes:sourceA.bytes bytesPerRow:width * 4];
        [sharedB replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
            withBytes:sourceB.bytes bytesPerRow:width * 4];
        id<MTLTexture> previous = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModePrivate, MTLTextureUsageShaderRead);
        id<MTLTexture> current = makeTexture(device, MTLPixelFormatBGRA8Unorm,
            width, height, MTLStorageModePrivate, MTLTextureUsageShaderRead);
        id<MTLTexture> grid = makeTexture(device, MTLPixelFormatRGBA32Float,
            gridWidth, gridHeight, MTLStorageModePrivate,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        id<MTLTexture> propagated = makeTexture(device, MTLPixelFormatRGBA32Float,
            gridWidth, gridHeight, MTLStorageModePrivate,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        id<MTLTexture> motion = makeTexture(device, MTLPixelFormatRG16Float,
            width, height, MTLStorageModePrivate,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        id<MTLTexture> color = makeTexture(device, MTLPixelFormatRGBA16Float,
            width, height, MTLStorageModePrivate,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        id<MTLTexture> depth = makeTexture(device, MTLPixelFormatR32Float,
            width, height, MTLStorageModePrivate,
            MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
        if (!sharedA || !sharedB || !previous || !current || !grid || !propagated || !motion || !color || !depth) {
            fprintf(stderr, "TEXTURE_ALLOCATION_FAILED\n");
            return 4;
        }
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

        printf("R109_MOTION_PROFILE device=%s dimensions=%lux%lu grid=%lux%lu shader=%s\n",
            device.name.UTF8String ?: "unknown", (unsigned long)width,
            (unsigned long)height, (unsigned long)gridWidth,
            (unsigned long)gridHeight, shaderPath.lastPathComponent.UTF8String);
        const char *passNames[] = {"blockMatch", "propagate", "upsample", "prepare"};
        for (NSUInteger pass = 0; pass < 4; ++pass) {
            double times[4] = {0};
            for (NSUInteger repeat = 0; repeat < 4; ++repeat) {
                id<MTLCommandBuffer> cb = [queue commandBuffer];
                if (!encodePass(cb, pass, p, previous, current, grid, propagated,
                    motion, color, depth, width, height, gridWidth, gridHeight)) return 6;
                times[repeat] = runBuffer(cb);
                if (times[repeat] < 0.0) return 7;
            }
            printf("R109_STAGE pass=%s warmupMs=%.3f sample1Ms=%.3f sample2Ms=%.3f sample3Ms=%.3f medianMs=%.3f\n",
                passNames[pass], times[0], times[1], times[2], times[3], median(times));
        }

        double total[4] = {0};
        for (NSUInteger repeat = 0; repeat < 4; ++repeat) {
            id<MTLCommandBuffer> cb = [queue commandBuffer];
            for (NSUInteger pass = 0; pass < 4; ++pass)
                if (!encodePass(cb, pass, p, previous, current, grid, propagated,
                    motion, color, depth, width, height, gridWidth, gridHeight)) return 8;
            total[repeat] = runBuffer(cb);
            if (total[repeat] < 0.0) return 9;
        }
        printf("R109_PIPELINE warmupMs=%.3f sample1Ms=%.3f sample2Ms=%.3f sample3Ms=%.3f medianMs=%.3f\n",
            total[0], total[1], total[2], total[3], median(total));

        id<MTLTexture> sharedMotion = makeTexture(device, MTLPixelFormatRG16Float,
            width, height, MTLStorageModeShared, MTLTextureUsageShaderRead);
        id<MTLCommandBuffer> readback = [queue commandBuffer];
        blit = [readback blitCommandEncoder];
        [blit copyFromTexture:motion sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0,0,0) sourceSize:MTLSizeMake(width,height,1)
            toTexture:sharedMotion destinationSlice:0 destinationLevel:0 destinationOrigin:MTLOriginMake(0,0,0)];
        [blit endEncoding];
        if (runBuffer(readback) < 0.0) return 10;
        NSMutableData *flow = [NSMutableData dataWithLength:width * height * 4];
        [sharedMotion getBytes:flow.mutableBytes bytesPerRow:width * 4
            fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
        if (![flow writeToFile:flowPath atomically:YES]) {
            fprintf(stderr, "FLOW_WRITE_FAILED path=%s\n", flowPath.UTF8String);
            return 11;
        }
        printf("R109_FLOW_SAVED path=%s bytes=%lu diagnosticReadback=post-processing\n",
            flowPath.UTF8String, (unsigned long)flow.length);
        return 0;
    }
}
