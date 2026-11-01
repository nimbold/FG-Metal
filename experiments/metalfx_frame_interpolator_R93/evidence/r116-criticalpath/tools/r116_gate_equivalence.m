// R116 offline gate-equivalence harness.
//
// Replays recorded R114/R115 pairs through two implementations of the frozen
// R110 fast gate and compares them:
//
//   legacy : separate command buffer, r110FastGate only, CPU walks every group
//            record and accumulates sequentially (the R110/R115 behaviour).
//   r116   : one command buffer, r110FastGate + r110FastGateReduce, CPU reads a
//            single 40-byte struct (the new critical-path behaviour).
//
// Both paths consume byte-identical input textures and the first-stage kernel is
// compiled from the same header the shipped observer uses, so any difference is
// attributable to the second-stage reduction alone.
//
// Usage:
//   r116_gate_equivalence --evidence DIR --width 1472 --height 956 \
//       --pairs 2749:2750,5827:5828 --iterations 20 --out results.json

#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import "r116_gate_kernels.h"

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

typedef struct {
    uint64_t finiteMotion;
    uint64_t activeMotion;
    uint64_t motionSamples;
    uint64_t finiteOutput;
    uint64_t outputSamples;
    uint64_t colorSamples;
    double maxMagnitude;
    double meanAbsDifferenceFromPrevious;
    double meanAbsDifferenceFromCurrent;
} R116Metrics;

static NSString *const kPrepareKernel =
    @"#include <metal_stdlib>\n"
     "using namespace metal;\n"
     "kernel void r108Prepare(texture2d<float, access::read> source [[texture(0)]],texture2d<half, access::write> color [[texture(1)]],texture2d<float, access::write> depth [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {\n"
     "  if(gid.x>=source.get_width()||gid.y>=source.get_height())return;float4 c=source.read(gid);color.write(half4(half3(c.rgb),half(c.a)),gid);depth.write(float4(0.25f,0.0f,0.0f,0.0f),gid);\n"
     "}\n";

static NSData *loadFile(NSString *path) {
    NSError *error = nil;
    NSData *data = [NSData dataWithContentsOfFile:path options:0 error:&error];
    if (!data) {
        fprintf(stderr, "missing: %s (%s)\n", path.UTF8String,
                error.localizedDescription.UTF8String ?: "unreadable");
    }
    return data;
}

static id<MTLTexture> makeTexture(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height,
                                  MTLTextureUsage usage) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height
        mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

// Uploads host bytes into a private texture through a shared staging buffer.
static BOOL uploadBytes(id<MTLCommandQueue> queue, id<MTLTexture> texture,
                        const void *bytes, NSUInteger bytesPerRow) {
    id<MTLDevice> device = texture.device;
    NSUInteger length = bytesPerRow * texture.height;
    id<MTLBuffer> staging = [device newBufferWithBytes:bytes length:length
        options:MTLResourceStorageModeShared];
    if (!staging) return NO;
    id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [commandBuffer blitCommandEncoder];
    if (!commandBuffer || !blit) return NO;
    [blit copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:bytesPerRow
        sourceBytesPerImage:length
        sourceSize:MTLSizeMake(texture.width, texture.height, 1)
        toTexture:texture destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit endEncoding];
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    return commandBuffer.status == MTLCommandBufferStatusCompleted &&
        commandBuffer.error == nil;
}

static BOOL evaluate(const R116Metrics *m, NSUInteger pixels, char *reason,
                     size_t reasonSize) {
    double activeFraction = m->motionSamples
        ? (double)m->activeMotion / (double)m->motionSamples : 0.0;
    BOOL motionFinite = m->motionSamples == pixels &&
        m->finiteMotion == m->motionSamples &&
        isfinite(m->maxMagnitude) && m->maxMagnitude <= 256.0;
    BOOL motionMoving = motionFinite && activeFraction >= 0.15;
    BOOL outputFinite = m->outputSamples == pixels &&
        m->finiteOutput == m->outputSamples;
    BOOL outputDistinct = m->colorSamples > 0 &&
        m->meanAbsDifferenceFromPrevious >= 0.002 &&
        m->meanAbsDifferenceFromCurrent >= 0.002;
    const char *r = !motionFinite ? "motion-field-nonfinite-or-unbounded" :
        !motionMoving ? "motion-below-frozen-pair-derived-threshold" :
        !outputFinite ? "metalfx-output-nonfinite" :
        !outputDistinct ? "metalfx-output-too-close-to-an-endpoint" : "none";
    snprintf(reason, reasonSize, "%s", r);
    return motionFinite && motionMoving && outputFinite && outputDistinct;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *evidence = nil;
        NSString *outPath = nil;
        NSString *pairsArg = nil;
        NSUInteger width = 1472, height = 956;
        NSUInteger iterations = 20;
        BOOL identityMidpoint = NO;
        for (int i = 1; i < argc; ++i) {
            NSString *flag = @(argv[i]);
            if ([flag isEqualToString:@"--evidence"] && i + 1 < argc) evidence = @(argv[++i]);
            else if ([flag isEqualToString:@"--out"] && i + 1 < argc) outPath = @(argv[++i]);
            else if ([flag isEqualToString:@"--pairs"] && i + 1 < argc) pairsArg = @(argv[++i]);
            else if ([flag isEqualToString:@"--width"] && i + 1 < argc) width = strtoul(argv[++i], NULL, 10);
            else if ([flag isEqualToString:@"--height"] && i + 1 < argc) height = strtoul(argv[++i], NULL, 10);
            else if ([flag isEqualToString:@"--iterations"] && i + 1 < argc) iterations = strtoul(argv[++i], NULL, 10);
            else if ([flag isEqualToString:@"--identity-midpoint"]) identityMidpoint = YES;
        }
        if (!evidence || !pairsArg) {
            fprintf(stderr, "usage: --evidence DIR --pairs A:B[,A:B...] [--out FILE] [--width W] [--height H]\n");
            return 2;
        }
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) { fprintf(stderr, "no Metal device\n"); return 3; }
        id<MTLCommandQueue> queue = [device newCommandQueue];

        NSError *error = nil;
        NSString *source = [kPrepareKernel stringByAppendingString:@R116_GATE_KERNEL_SOURCE];
        id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
        if (!library) {
            fprintf(stderr, "library: %s\n", error.localizedDescription.UTF8String);
            return 4;
        }
        id<MTLComputePipelineState> gatePipeline =
            [device newComputePipelineStateWithFunction:
                [library newFunctionWithName:@"r110FastGate"] error:&error];
        id<MTLComputePipelineState> reducePipeline =
            [device newComputePipelineStateWithFunction:
                [library newFunctionWithName:@"r110FastGateReduce"] error:&error];
        id<MTLComputePipelineState> preparePipeline =
            [device newComputePipelineStateWithFunction:
                [library newFunctionWithName:@"r108Prepare"] error:&error];
        if (!gatePipeline || !reducePipeline || !preparePipeline) {
            fprintf(stderr, "pipelines: %s\n", error.localizedDescription.UTF8String);
            return 5;
        }

        const NSUInteger pixels = width * height;
        const NSUInteger groupsX = (width + 63) / 64;
        const NSUInteger groupsY = (height + 63) / 64;
        const NSUInteger groupCount = groupsX * groupsY;
        const uint32_t groupTotal = (uint32_t)groupCount;

        id<MTLBuffer> groupBuffer = [device newBufferWithLength:
            groupCount * sizeof(R110GateGroupStats) options:MTLResourceStorageModeShared];
        id<MTLBuffer> resultBuffer = [device newBufferWithLength:
            sizeof(R116GateResultRaw) options:MTLResourceStorageModeShared];

        NSMutableArray *records = [NSMutableArray array];
        NSArray *pairTokens = [pairsArg componentsSeparatedByString:@","];
        for (NSString *token in pairTokens) {
            NSArray *ends = [token componentsSeparatedByString:@":"];
            if (ends.count != 2) continue;
            uint64_t pairA = strtoull([ends[0] UTF8String], NULL, 10);
            uint64_t pairB = strtoull([ends[1] UTF8String], NULL, 10);
            NSString *rawA = [evidence stringByAppendingPathComponent:
                [NSString stringWithFormat:@"source-sequence-%llu.raw", pairA]];
            NSString *rawB = [evidence stringByAppendingPathComponent:
                [NSString stringWithFormat:@"source-sequence-%llu.raw", pairB]];
            NSString *motionPath = [evidence stringByAppendingPathComponent:
                [NSString stringWithFormat:@"motion-pair-%llu-%llu.rg16f", pairA, pairB]];
            NSString *midpointPath = [evidence stringByAppendingPathComponent:
                [NSString stringWithFormat:@"title-M-for-%llu-%llu.rgba16f", pairA, pairB]];
            NSData *dataA = loadFile(rawA);
            NSData *dataB = loadFile(rawB);
            NSData *dataMotion = loadFile(motionPath);
            NSData *dataMidpoint = loadFile(midpointPath);
            if (!dataA || !dataB || !dataMotion) {
                fprintf(stderr, "skip pair %llu->%llu (missing inputs)\n", pairA, pairB);
                continue;
            }
            if (dataA.length != pixels * 4 || dataB.length != pixels * 4 ||
                dataMotion.length != pixels * 4) {
                fprintf(stderr, "skip pair %llu->%llu (size mismatch a=%lu b=%lu m=%lu expect=%lu)\n",
                        pairA, pairB, (unsigned long)dataA.length,
                        (unsigned long)dataB.length, (unsigned long)dataMotion.length,
                        (unsigned long)(pixels * 4));
                continue;
            }
            BOOL hasMidpoint = dataMidpoint.length == pixels * 8;

            id<MTLTexture> sourceA = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                MTLTextureUsageShaderRead);
            id<MTLTexture> sourceB = makeTexture(device, MTLPixelFormatBGRA8Unorm, width, height,
                MTLTextureUsageShaderRead);
            id<MTLTexture> colorA = makeTexture(device, MTLPixelFormatRGBA16Float, width, height,
                MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
            id<MTLTexture> colorB = makeTexture(device, MTLPixelFormatRGBA16Float, width, height,
                MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
            id<MTLTexture> depth = makeTexture(device, MTLPixelFormatR32Float, width, height,
                MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
            id<MTLTexture> motion = makeTexture(device, MTLPixelFormatRG16Float, width, height,
                MTLTextureUsageShaderRead);
            id<MTLTexture> midpoint = makeTexture(device, MTLPixelFormatRGBA16Float, width, height,
                MTLTextureUsageShaderRead);
            if (!hasMidpoint) {
                // A rejected pair has no MetalFX output. The motion term alone
                // decides it, and both paths read the same stand-in, so the
                // comparison stays exact; only the output terms are synthetic.
                dataMidpoint = nil;
            }
            BOOL uploaded =
                uploadBytes(queue, sourceA, dataA.bytes, width * 4) &&
                uploadBytes(queue, sourceB, dataB.bytes, width * 4) &&
                uploadBytes(queue, motion, dataMotion.bytes, width * 4);
            if (hasMidpoint) uploaded = uploaded &&
                uploadBytes(queue, midpoint, dataMidpoint.bytes, width * 8);
            if (!uploaded) { fprintf(stderr, "upload failed %llu->%llu\n", pairA, pairB); continue; }
            if (!hasMidpoint) {
                // Zero fill keeps outputFinite true and outputDistinct false,
                // which is what an absent M means for the endpoint terms.
                NSUInteger length = pixels * 8;
                void *zeros = calloc(1, length);
                uploadBytes(queue, midpoint, zeros, width * 8);
                free(zeros);
            }

            // Build the two RGBA16F colour inputs with the shipped prepare kernel.
            id<MTLCommandBuffer> prep = [queue commandBuffer];
            id<MTLComputeCommandEncoder> enc = [prep computeCommandEncoder];
            MTLSize imageGroups = MTLSizeMake((width + 15) / 16, (height + 15) / 16, 1);
            MTLSize imageGroup = MTLSizeMake(16, 16, 1);
            [enc setComputePipelineState:preparePipeline];
            [enc setTexture:sourceA atIndex:0];
            [enc setTexture:colorA atIndex:1];
            [enc setTexture:depth atIndex:2];
            [enc dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
            [enc endEncoding];
            enc = [prep computeCommandEncoder];
            [enc setComputePipelineState:preparePipeline];
            [enc setTexture:sourceB atIndex:0];
            [enc setTexture:colorB atIndex:1];
            [enc setTexture:depth atIndex:2];
            [enc dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
            [enc endEncoding];
            if (identityMidpoint) {
                // Deterministic endpoint-branch probe: M := A. Both gate paths
                // then see meanAbsDiffA == 0 and must reject on
                // metalfx-output-too-close-to-an-endpoint.
                id<MTLBlitCommandEncoder> blit = [prep blitCommandEncoder];
                [blit copyFromTexture:colorA sourceSlice:0 sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(width, height, 1)
                           toTexture:midpoint destinationSlice:0 destinationLevel:0
                    destinationOrigin:MTLOriginMake(0, 0, 0)];
                [blit endEncoding];
            }
            [prep commit];
            [prep waitUntilCompleted];

            R116Metrics legacy = {0};
            R116Metrics modern = {0};
            double legacyGpuMs = 0.0, modernGpuMs = 0.0;
            for (NSUInteger iteration = 0; iteration < iterations; ++iteration) {
                // legacy
                id<MTLCommandBuffer> legacyBuffer = [queue commandBuffer];
                enc = [legacyBuffer computeCommandEncoder];
                [enc setComputePipelineState:gatePipeline];
                [enc setTexture:motion atIndex:0];
                [enc setTexture:colorA atIndex:1];
                [enc setTexture:colorB atIndex:2];
                [enc setTexture:midpoint atIndex:3];
                [enc setBuffer:groupBuffer offset:0 atIndex:0];
                [enc dispatchThreadgroups:MTLSizeMake(groupsX, groupsY, 1)
                    threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
                [enc endEncoding];
                [legacyBuffer commit];
                [legacyBuffer waitUntilCompleted];
                double ms = (legacyBuffer.GPUEndTime - legacyBuffer.GPUStartTime) * 1000.0;
                if (iteration == 0) legacyGpuMs = ms;
                const R110GateGroupStats *groups = groupBuffer.contents;
                R116Metrics m = {0};
                double diffPrevious = 0.0, diffCurrent = 0.0;
                for (NSUInteger i = 0; i < groupCount; ++i) {
                    m.finiteMotion += groups[i].finiteMotion;
                    m.activeMotion += groups[i].activeMotion;
                    m.motionSamples += groups[i].motionSamples;
                    m.finiteOutput += groups[i].finiteOutput;
                    m.outputSamples += groups[i].outputSamples;
                    m.colorSamples += groups[i].colorSamples;
                    if (groups[i].maxMagnitude > m.maxMagnitude)
                        m.maxMagnitude = groups[i].maxMagnitude;
                    diffPrevious += groups[i].diffPrevious;
                    diffCurrent += groups[i].diffCurrent;
                }
                if (m.colorSamples) {
                    m.meanAbsDifferenceFromPrevious = diffPrevious / m.colorSamples;
                    m.meanAbsDifferenceFromCurrent = diffCurrent / m.colorSamples;
                }
                legacy = m;

                // r116
                id<MTLCommandBuffer> modernBuffer = [queue commandBuffer];
                enc = [modernBuffer computeCommandEncoder];
                [enc setComputePipelineState:gatePipeline];
                [enc setTexture:motion atIndex:0];
                [enc setTexture:colorA atIndex:1];
                [enc setTexture:colorB atIndex:2];
                [enc setTexture:midpoint atIndex:3];
                [enc setBuffer:groupBuffer offset:0 atIndex:0];
                [enc dispatchThreadgroups:MTLSizeMake(groupsX, groupsY, 1)
                    threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
                [enc setComputePipelineState:reducePipeline];
                [enc setBuffer:groupBuffer offset:0 atIndex:0];
                [enc setBuffer:resultBuffer offset:0 atIndex:1];
                [enc setBytes:&groupTotal length:sizeof(groupTotal) atIndex:2];
                uint32_t frameTag = (uint32_t)(iteration + 1);
                [enc setBytes:&frameTag length:sizeof(frameTag) atIndex:3];
                [enc dispatchThreadgroups:MTLSizeMake(1, 1, 1)
                    threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
                [enc endEncoding];
                [modernBuffer commit];
                [modernBuffer waitUntilCompleted];
                double ms2 = (modernBuffer.GPUEndTime - modernBuffer.GPUStartTime) * 1000.0;
                if (iteration == 0) modernGpuMs = ms2;
                R116GateResultRaw raw;
                memcpy(&raw, resultBuffer.contents, sizeof(raw));
                R116Metrics n = {0};
                n.finiteMotion = raw.finiteMotion;
                n.activeMotion = raw.activeMotion;
                n.motionSamples = raw.motionSamples;
                n.finiteOutput = raw.finiteOutput;
                n.outputSamples = raw.outputSamples;
                n.colorSamples = raw.colorSamples;
                n.maxMagnitude = raw.maxMagnitude;
                n.meanAbsDifferenceFromPrevious = raw.colorSamples
                    ? (double)raw.diffPrevious / (double)raw.colorSamples : 0.0;
                n.meanAbsDifferenceFromCurrent = raw.colorSamples
                    ? (double)raw.diffCurrent / (double)raw.colorSamples : 0.0;
                modern = n;
            }

            char legacyReason[128], modernReason[128];
            BOOL legacyAccepted = evaluate(&legacy, pixels, legacyReason, sizeof(legacyReason));
            BOOL modernAccepted = evaluate(&modern, pixels, modernReason, sizeof(modernReason));

            NSDictionary *record = @{
                @"pairA": @(pairA), @"pairB": @(pairB),
                @"hasRecordedMidpoint": @(hasMidpoint),
                @"legacyAccepted": @(legacyAccepted),
                @"r116Accepted": @(modernAccepted),
                @"decisionAgrees": @(legacyAccepted == modernAccepted),
                @"legacyReason": @(legacyReason), @"r116Reason": @(modernReason),
                @"legacy": @{@"finiteMotion": @(legacy.finiteMotion),
                             @"activeMotion": @(legacy.activeMotion),
                             @"motionSamples": @(legacy.motionSamples),
                             @"finiteOutput": @(legacy.finiteOutput),
                             @"outputSamples": @(legacy.outputSamples),
                             @"colorSamples": @(legacy.colorSamples),
                             @"maxMagnitude": @(legacy.maxMagnitude),
                             @"meanAbsDiffA": @(legacy.meanAbsDifferenceFromPrevious),
                             @"meanAbsDiffB": @(legacy.meanAbsDifferenceFromCurrent)},
                @"r116": @{@"finiteMotion": @(modern.finiteMotion),
                           @"activeMotion": @(modern.activeMotion),
                           @"motionSamples": @(modern.motionSamples),
                           @"finiteOutput": @(modern.finiteOutput),
                           @"outputSamples": @(modern.outputSamples),
                           @"colorSamples": @(modern.colorSamples),
                           @"maxMagnitude": @(modern.maxMagnitude),
                           @"meanAbsDiffA": @(modern.meanAbsDifferenceFromPrevious),
                           @"meanAbsDiffB": @(modern.meanAbsDifferenceFromCurrent)},
                @"delta": @{@"finiteMotion": @((double)modern.finiteMotion - (double)legacy.finiteMotion),
                            @"activeMotion": @((double)modern.activeMotion - (double)legacy.activeMotion),
                            @"motionSamples": @((double)modern.motionSamples - (double)legacy.motionSamples),
                            @"finiteOutput": @((double)modern.finiteOutput - (double)legacy.finiteOutput),
                            @"outputSamples": @((double)modern.outputSamples - (double)legacy.outputSamples),
                            @"colorSamples": @((double)modern.colorSamples - (double)legacy.colorSamples),
                            @"maxMagnitude": @(modern.maxMagnitude - legacy.maxMagnitude),
                            @"meanAbsDiffA": @(modern.meanAbsDifferenceFromPrevious - legacy.meanAbsDifferenceFromPrevious),
                            @"meanAbsDiffB": @(modern.meanAbsDifferenceFromCurrent - legacy.meanAbsDifferenceFromCurrent)},
                @"legacyGateGpuMs": @(legacyGpuMs),
                @"r116GateGpuMs": @(modernGpuMs),
                @"r116AddedGpuMs": @(modernGpuMs - legacyGpuMs),
                @"iterations": @(iterations),
                @"groupCount": @(groupCount),
                @"pixels": @(pixels),
            };
            [records addObject:record];
            fprintf(stdout,
                "pair %llu->%llu legacy=%s r116=%s agree=%d maxMag=%.6f/%.6f "
                "meanA=%.6f/%.6f meanB=%.6f/%.6f gateGpuMs=%.4f/%.4f\n",
                pairA, pairB, legacyAccepted ? "ACCEPT" : "REJECT",
                modernAccepted ? "ACCEPT" : "REJECT",
                legacyAccepted == modernAccepted,
                legacy.maxMagnitude, modern.maxMagnitude,
                legacy.meanAbsDifferenceFromPrevious, modern.meanAbsDifferenceFromPrevious,
                legacy.meanAbsDifferenceFromCurrent, modern.meanAbsDifferenceFromCurrent,
                legacyGpuMs, modernGpuMs);
        }

        NSDictionary *summary = @{
            @"device": device.name ?: @"unknown",
            @"pairCount": @(records.count),
            @"decisionsAgree": @([[records valueForKeyPath:@"@sum.decisionAgrees"] integerValue] == (NSInteger)records.count),
            @"records": records,
        };
        NSData *json = [NSJSONSerialization dataWithJSONObject:summary
            options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys error:&error];
        if (outPath) {
            [json writeToFile:outPath atomically:YES];
            fprintf(stdout, "wrote %s\n", outPath.UTF8String);
        } else {
            fwrite(json.bytes, 1, json.length, stdout);
        }
    }
    return 0;
}
