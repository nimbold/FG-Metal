#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <mach/mach_time.h>
#import <math.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>

static uint64_t nowNs(void) {
    static mach_timebase_info_data_t tb;
    if (!tb.denom) mach_timebase_info(&tb);
    return mach_absolute_time() * tb.numer / tb.denom;
}

static NSData *readFile(NSString *path) {
    NSError *error = nil;
    NSData *d = [NSData dataWithContentsOfFile:path options:0 error:&error];
    if (!d) fprintf(stderr, "READ_FAILED path=%s error=%s\n", path.UTF8String,
                    error.localizedDescription.UTF8String ?: "unknown");
    return d;
}

static id<MTLTexture> texture(id<MTLDevice> device, MTLPixelFormat format,
                              NSUInteger width, NSUInteger height,
                              MTLStorageMode storage, MTLTextureUsage usage) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height mipmapped:NO];
    d.storageMode = storage;
    d.usage = usage;
    return [device newTextureWithDescriptor:d];
}

static float halfToFloat(uint16_t h) {
    uint16_t sign = (h >> 15) & 1, exp = (h >> 10) & 31, mant = h & 1023;
    if (exp == 0) return sign ? -ldexpf((float)mant, -24) : ldexpf((float)mant, -24);
    if (exp == 31) return mant ? NAN : (sign ? -INFINITY : INFINITY);
    float v = ldexpf(1.0f + (float)mant / 1024.0f, (int)exp - 15);
    return sign ? -v : v;
}

static int compareFloat(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return (x > y) - (x < y);
}

static float median(NSMutableData *values) {
    NSUInteger n = values.length / sizeof(float);
    if (!n) return NAN;
    qsort(values.mutableBytes, n, sizeof(float), compareFloat);
    return ((float *)values.mutableBytes)[n / 2];
}

static void add(NSMutableData *data, float value) {
    [data appendBytes:&value length:sizeof(value)];
}

static void reportRegion(const char *label, const uint8_t *bgra, const uint16_t *flow,
                         NSUInteger width, NSUInteger height, int region,
                         float expectedX, float expectedY) {
    NSMutableData *xs = [NSMutableData data], *ys = [NSMutableData data];
    NSMutableData *errors = [NSMutableData data], *magnitudes = [NSMutableData data];
    NSUInteger count = 0, withinOne = 0, withinTwo = 0, exactZero = 0;
    for (NSUInteger y = 0; y < height; ++y) {
        for (NSUInteger x = 0; x < width; ++x) {
            const uint8_t *p = bgra + (y * width + x) * 4;
            BOOL mainShape = p[0] > 220 && p[1] > 160 && p[2] < 80;
            BOOL marker = p[0] < 80 && p[1] > 100 && p[2] > 220;
            BOOL selected = region == 0 ? mainShape : region == 1 ? marker :
                (x > 500 && y > 100 && y + 100 < height && !mainShape && !marker);
            if (!selected) continue;
            NSUInteger i = (y * width + x) * 2;
            float dx = halfToFloat(flow[i]), dy = halfToFloat(flow[i + 1]);
            add(xs, dx); add(ys, dy);
            float error = hypotf(dx - expectedX, dy - expectedY);
            add(errors, error);
            add(magnitudes, hypotf(dx, dy));
            withinOne += error <= 1.0f;
            withinTwo += error <= 2.0f;
            exactZero += dx == 0.0f && dy == 0.0f;
            ++count;
        }
    }
    float mx = median(xs), my = median(ys), me = median(errors), mm = median(magnitudes);
    NSUInteger n = errors.length / sizeof(float);
    float *e = errors.mutableBytes;
    NSUInteger p90Index = n ? (n * 9) / 10 : 0;
    if (n) qsort(e, n, sizeof(float), compareFloat);
    printf("FLOW_REGION variant=%s label=%s pixels=%lu expected=(%.5f,%.5f) median=(%.5f,%.5f) medianMagnitude=%.5f medianVectorError=%.5f regionalMedianError=%.5f p90VectorError=%.5f within1px=%lu within2px=%lu zeroVectors=%lu\n",
        getenv("R107_VARIANT") ?: "unknown", label, (unsigned long)count,
        expectedX, expectedY, mx, my, mm, me, hypotf(mx - expectedX, my - expectedY),
        n ? e[p90Index] : NAN, (unsigned long)withinOne, (unsigned long)withinTwo,
        (unsigned long)exactZero);
}

static BOOL runVariant(id<MTLDevice> device, id<MTLCommandQueue> queue,
                       NSData *sourceA, NSData *sourceB, const uint8_t *sourceBBytes,
                       NSUInteger width, NSUInteger height, NSString *shaderPath,
                       NSString *flowPath, NSString *variant) {
    NSError *error = nil;
    NSString *shader = [NSString stringWithContentsOfFile:shaderPath
        encoding:NSUTF8StringEncoding error:&error];
    if (!shader) {
        fprintf(stderr, "SHADER_READ_FAILED path=%s error=%s\n", shaderPath.UTF8String,
            error.localizedDescription.UTF8String ?: "unknown");
        return NO;
    }
    id<MTLLibrary> library = [device newLibraryWithSource:shader options:nil error:&error];
    if (!library) {
        fprintf(stderr, "SHADER_BUILD_FAILED variant=%s error=%s\n", variant.UTF8String,
            error.localizedDescription.UTF8String ?: "unknown");
        return NO;
    }
    id<MTLComputePipelineState> block = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107BlockMatch"] error:&error];
    id<MTLComputePipelineState> propagate = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107Propagate"] error:&error];
    id<MTLComputePipelineState> upsample = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107Upsample"] error:&error];
    if (!block || !propagate || !upsample) {
        fprintf(stderr, "PIPELINE_BUILD_FAILED variant=%s error=%s\n", variant.UTF8String,
            error.localizedDescription.UTF8String ?: "unknown");
        return NO;
    }

    const NSUInteger gridWidth = (width + 39) / 40, gridHeight = (height + 39) / 40;
    id<MTLTexture> previous = texture(device, MTLPixelFormatBGRA8Unorm, width, height,
        MTLStorageModeShared, MTLTextureUsageShaderRead);
    id<MTLTexture> current = texture(device, MTLPixelFormatBGRA8Unorm, width, height,
        MTLStorageModeShared, MTLTextureUsageShaderRead);
    id<MTLTexture> grid = texture(device, MTLPixelFormatRGBA32Float, gridWidth, gridHeight,
        MTLStorageModePrivate, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> propagated = texture(device, MTLPixelFormatRGBA32Float, gridWidth, gridHeight,
        MTLStorageModePrivate, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> flow = texture(device, MTLPixelFormatRG16Float, width, height,
        MTLStorageModeShared, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    if (!previous || !current || !grid || !propagated || !flow) {
        fprintf(stderr, "TEXTURE_ALLOCATION_FAILED variant=%s\n", variant.UTF8String);
        return NO;
    }
    [previous replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
        withBytes:sourceA.bytes bytesPerRow:width * 4];
    [current replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
        withBytes:sourceB.bytes bytesPerRow:width * 4];

    double gpuSamples[4] = {0}, elapsedSamples[4] = {0};
    for (NSUInteger repeat = 0; repeat < 4; ++repeat) {
        id<MTLCommandBuffer> cb = [queue commandBuffer];
        MTLSize gridGroup = MTLSizeMake(4, 4, 1);
        MTLSize gridGroups = MTLSizeMake((gridWidth + 3) / 4, (gridHeight + 3) / 4, 1);
        id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:block]; [enc setTexture:previous atIndex:0];
        [enc setTexture:current atIndex:1]; [enc setTexture:grid atIndex:2];
        [enc dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup]; [enc endEncoding];

        enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:propagate]; [enc setTexture:current atIndex:0];
        [enc setTexture:grid atIndex:1]; [enc setTexture:propagated atIndex:2];
        [enc dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup]; [enc endEncoding];

        MTLSize imageGroup = MTLSizeMake(16, 16, 1);
        MTLSize imageGroups = MTLSizeMake((width + 15) / 16, (height + 15) / 16, 1);
        enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:upsample]; [enc setTexture:propagated atIndex:0];
        [enc setTexture:flow atIndex:1];
        [enc dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup]; [enc endEncoding];

        uint64_t begin = nowNs(); [cb commit]; [cb waitUntilCompleted];
        uint64_t elapsed = nowNs() - begin;
        if (cb.status != MTLCommandBufferStatusCompleted || cb.error) {
            fprintf(stderr, "RUN_FAILED variant=%s repeat=%lu status=%lu error=%s\n",
                variant.UTF8String, (unsigned long)repeat, (unsigned long)cb.status,
                cb.error.localizedDescription.UTF8String ?: "none");
            return NO;
        }
        gpuSamples[repeat] = cb.GPUEndTime > cb.GPUStartTime
            ? (cb.GPUEndTime - cb.GPUStartTime) * 1000.0 : 0.0;
        elapsedSamples[repeat] = elapsed / 1e6;
        printf("TIMING variant=%s repeat=%lu warmup=%d gpuMs=%.3f elapsedMs=%.3f grid=%lux%lu\n",
            variant.UTF8String, (unsigned long)repeat, repeat == 0,
            gpuSamples[repeat], elapsedSamples[repeat],
            (unsigned long)gridWidth, (unsigned long)gridHeight);
    }

    NSUInteger rowBytes = width * 4;
    NSMutableData *flowBytes = [NSMutableData dataWithLength:rowBytes * height];
    [flow getBytes:flowBytes.mutableBytes bytesPerRow:rowBytes
        fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
    if (![flowBytes writeToFile:flowPath atomically:YES]) {
        fprintf(stderr, "FLOW_WRITE_FAILED path=%s\n", flowPath.UTF8String);
        return NO;
    }
    const uint16_t *v = flowBytes.bytes;
    setenv("R107_VARIANT", variant.UTF8String, 1);
    reportRegion("main", sourceBBytes, v, width, height, 0, -4.38679f, 0.0f);
    reportRegion("fast-marker", sourceBBytes, v, width, height, 1, -15.0f, 0.0f);
    reportRegion("static-background", sourceBBytes, v, width, height, 2, 0.0f, 0.0f);

    double sorted[3] = {gpuSamples[1], gpuSamples[2], gpuSamples[3]};
    for (int i = 0; i < 3; ++i) for (int j = i + 1; j < 3; ++j)
        if (sorted[j] < sorted[i]) { double t = sorted[i]; sorted[i] = sorted[j]; sorted[j] = t; }
    printf("VARIANT_SUMMARY variant=%s gpuMedianMs=%.3f elapsedMedianMs=%.3f flow=%s\n",
        variant.UTF8String, sorted[1], (elapsedSamples[1] + elapsedSamples[2] + elapsedSamples[3]) / 3.0,
        flowPath.UTF8String);
    return YES;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 8) {
            fprintf(stderr, "usage: %s source-3.raw source-4.raw exact.metal step4.metal exact-flow.rg16f step4-flow.rg16f output.log\n", argv[0]);
            return 2;
        }
        const NSUInteger width = 1280, height = 720;
        NSString *aPath = [NSString stringWithUTF8String:argv[1]];
        NSString *bPath = [NSString stringWithUTF8String:argv[2]];
        NSData *a = readFile(aPath), *b = readFile(bPath);
        if (!a || !b || a.length != width * height * 4 || b.length != width * height * 4) return 2;
        freopen(argv[7], "w", stdout);
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLCommandQueue> queue = [device newCommandQueue];
        printf("R107_OFFLINE_BENCH device=%s dimensions=%lux%lu pair=R104_SOURCE_3_TO_4 regionCalibration=R104_VALIDATED\n",
            device.name.UTF8String ?: "unknown", (unsigned long)width, (unsigned long)height);
        BOOL exact = runVariant(device, queue, a, b, b.bytes, width, height,
            [NSString stringWithUTF8String:argv[3]], [NSString stringWithUTF8String:argv[5]], @"R107_STEP8");
        BOOL step4 = runVariant(device, queue, a, b, b.bytes, width, height,
            [NSString stringWithUTF8String:argv[4]], [NSString stringWithUTF8String:argv[6]], @"R107_STEP4_BASELINE");
        fflush(stdout);
        return exact && step4 ? 0 : 3;
    }
}
