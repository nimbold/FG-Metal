#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <mach/mach_time.h>
#import <math.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>

static NSString *const kKernelSource = @"#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "inline float r106Luma(texture2d<float, access::read> t, int2 p) {\n"
    "  p = clamp(p, int2(0), int2(int(t.get_width()) - 1, int(t.get_height()) - 1));\n"
    "  float3 c = t.read(uint2(p)).rgb;\n"
    "  return dot(c, float3(0.2126f, 0.7152f, 0.0722f));\n"
    "}\n"
    "inline float r106PatchCost(texture2d<float, access::read> previous, texture2d<float, access::read> current, int2 center, float3 targetColor, int2 shift, int sampleStep) {\n"
    "  float sum = 0.0f;\n"
    "  float weightSum = 0.0f;\n"
    "  for (int y = -96; y <= 96; y += sampleStep) {\n"
    "    for (int x = -96; x <= 96; x += sampleStep) {\n"
    "      int2 p = center + int2(x, y);\n"
    "      float3 a = current.read(uint2(clamp(p, int2(0), int2(int(current.get_width()) - 1, int(current.get_height()) - 1)))).rgb;\n"
    "      float3 b = previous.read(uint2(clamp(p + shift, int2(0), int2(int(previous.get_width()) - 1, int(previous.get_height()) - 1)))).rgb;\n"
    "      float targetY = dot(targetColor, float3(0.2126f,0.7152f,0.0722f));\n"
    "      float sampleY = dot(a, float3(0.2126f,0.7152f,0.0722f));\n"
    "      float colorDifference = length(a - targetColor);\n"
    "      if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;\n"
    "      float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f) ? 4.0f : 1.0f;\n"
    "      sum += weight * abs(dot(a, float3(0.2126f,0.7152f,0.0722f)) - dot(b, float3(0.2126f,0.7152f,0.0722f)));\n"
    "      weightSum += weight;\n"
    "    }\n"
    "  }\n"
    "  return sum / max(weightSum, 1e-5f);\n"
    "}\n"
    "kernel void r106InputProbe(texture2d<float, access::read> previous [[texture(0)]], texture2d<float, access::read> current [[texture(1)]], texture2d<float, access::write> samples [[texture(2)]], uint2 gid [[thread_position_in_grid]]) {\n"
    "  if (gid.x >= samples.get_width() || gid.y >= samples.get_height()) return;\n"
    "  int2 center = int2(float(gid.x) * float(current.get_width()) / float(samples.get_width()), float(gid.y) * float(current.get_height()) / float(samples.get_height()));\n"
    "  float3 a = previous.read(uint2(center)).rgb; float3 b = current.read(uint2(center)).rgb;\n"
    "  float3 target = current.read(uint2(center)).rgb;\n"
    "  samples.write(float4(r106PatchCost(previous,current,center,target,int2(-4,0),4), r106PatchCost(previous,current,center,target,int2(-15,0),4), r106PatchCost(previous,current,center,target,int2(0,0),4), r106PatchCost(previous,current,center,target,int2(-12,0),4)), gid);\n"
    "}\n"
    "kernel void r106BlockMatch(texture2d<float, access::read> previous [[texture(0)]], texture2d<float, access::read> current [[texture(1)]], texture2d<float, access::write> grid [[texture(2)]], uint2 gid [[thread_position_in_grid]]) {\n"
    "  if (gid.x >= grid.get_width() || gid.y >= grid.get_height()) return;\n"
    "  int2 center = int2(float(gid.x) * float(current.get_width()) / float(grid.get_width()), float(gid.y) * float(current.get_height()) / float(grid.get_height()));\n"
    "  float3 targetColor = current.read(uint2(center)).rgb;\n"
    "  int bestX = 0; float bestXCost = INFINITY;\n"
    "  for (int dx = -20; dx <= 20; dx += 2) { float c = r106PatchCost(previous,current,center,targetColor,int2(dx,0),8) + 0.000001f*float(abs(dx)); if (c < bestXCost) { bestXCost=c; bestX=dx; } }\n"
    "  bestXCost = INFINITY;\n"
    "  for (int dx = max(-20,bestX-4); dx <= min(20,bestX+4); ++dx) { float c=r106PatchCost(previous,current,center,targetColor,int2(dx,0),4)+0.000001f*float(abs(dx)); if(c<bestXCost){bestXCost=c;bestX=dx;} }\n"
    "  int bestY = 0; float bestYCost = INFINITY;\n"
    "  for (int dy = -20; dy <= 20; dy += 2) { float c = r106PatchCost(previous,current,center,targetColor,int2(0,dy),8) + 0.000001f*float(abs(dy)); if (c < bestYCost) { bestYCost=c; bestY=dy; } }\n"
    "  bestYCost = INFINITY;\n"
    "  for (int dy = max(-20,bestY-4); dy <= min(20,bestY+4); ++dy) { float c=r106PatchCost(previous,current,center,targetColor,int2(0,dy),4)+0.000001f*float(abs(dy)); if(c<bestYCost){bestYCost=c;bestY=dy;} }\n"
    "  float costCenterX = r106PatchCost(previous,current,center,targetColor,int2(bestX,0),4);\n"
    "  float costLeft = r106PatchCost(previous,current,center,targetColor,int2(bestX-1,0),4);\n"
    "  float costRight = r106PatchCost(previous,current,center,targetColor,int2(bestX+1,0),4);\n"
    "  float costCenterY = r106PatchCost(previous,current,center,targetColor,int2(0,bestY),4);\n"
    "  float costUp = r106PatchCost(previous,current,center,targetColor,int2(0,bestY-1),4);\n"
    "  float costDown = r106PatchCost(previous,current,center,targetColor,int2(0,bestY+1),4);\n"
    "  float refineX = abs(costLeft-2.0f*costCenterX+costRight)>1e-6f ? clamp(0.5f*(costLeft-costRight)/(costLeft-2.0f*costCenterX+costRight),-0.5f,0.5f):0.0f;\n"
    "  float refineY = abs(costUp-2.0f*costCenterY+costDown)>1e-6f ? clamp(0.5f*(costUp-costDown)/(costUp-2.0f*costCenterY+costDown),-0.5f,0.5f):0.0f;\n"
    "  float farX=INFINITY, farY=INFINITY;\n"
    "  for (int d=-20; d<=20; d+=2) { if(abs(d-bestX)>10){float c=r106PatchCost(previous,current,center,targetColor,int2(d,0),8);if(c<farX)farX=c;} if(abs(d-bestY)>10){float c=r106PatchCost(previous,current,center,targetColor,int2(0,d),8);if(c<farY)farY=c;} }\n"
    "  float bestCostX=r106PatchCost(previous,current,center,targetColor,int2(bestX,0),8);\n"
    "  float bestCostY=r106PatchCost(previous,current,center,targetColor,int2(0,bestY),8);\n"
    "  float confidenceX=isfinite(farX)&&farX>1e-5f?clamp((farX-bestCostX)/farX,0.0f,1.0f):0.0f;\n"
    "  float confidenceY=isfinite(farY)&&farY>1e-5f?clamp((farY-bestCostY)/farY,0.0f,1.0f):0.0f;\n"
    "  float motionX=confidenceX>0.05f?float(bestX)+refineX:0.0f;\n"
    "  float motionY=confidenceY>0.05f?float(bestY)+refineY:0.0f;\n"
    "  grid.write(float4(motionX,motionY,max(confidenceX,confidenceY),min(bestCostX,bestCostY)),gid);\n"
    "}\n"
    "kernel void r106PropagateFlow(texture2d<float, access::read> current [[texture(0)]], texture2d<float, access::read> input [[texture(1)]], texture2d<float, access::write> output [[texture(2)]], uint2 gid [[thread_position_in_grid]]) {\n"
    "  if (gid.x >= input.get_width() || gid.y >= input.get_height()) return;\n"
    "  float4 base = input.read(gid);\n"
    "  if (base.z >= 0.05f) { output.write(base, gid); return; }\n"
    "  int2 center = int2(float(gid.x) * float(current.get_width()) / float(input.get_width()), float(gid.y) * float(current.get_height()) / float(input.get_height()));\n"
    "  float3 c = current.read(uint2(center)).rgb;\n"
    "  if (dot(c, float3(0.2126f,0.7152f,0.0722f)) < 0.05f) { output.write(float4(0.0f,0.0f,0.0f,0.0f), gid); return; }\n"
    "  float bestScore = 0.0f; float4 best = base;\n"
    "  for (int y = -3; y <= 3; ++y) for (int x = -3; x <= 3; ++x) {\n"
    "    if (x == 0 && y == 0) continue;\n"
    "    int2 q = int2(gid) + int2(x,y);\n"
    "    if (q.x < 0 || q.y < 0 || q.x >= int(input.get_width()) || q.y >= int(input.get_height())) continue;\n"
    "    float4 f = input.read(uint2(q));\n"
    "    if (f.z <= 0.05f) continue;\n"
    "    int2 qCenter = int2(float(q.x) * float(current.get_width()) / float(input.get_width()), float(q.y) * float(current.get_height()) / float(input.get_height()));\n"
    "    float3 qc = current.read(uint2(qCenter)).rgb;\n"
    "    float colorDifference = length(c - qc);\n"
    "    if (colorDifference > 0.12f) continue;\n"
    "    float score = f.z / (1.0f + float(abs(x) + abs(y)));\n"
    "    if (score > bestScore) { bestScore = score; best = f; }\n"
    "  }\n"
    "  if (bestScore > 0.0f) { best.z *= 0.75f; output.write(best, gid); } else { output.write(float4(0.0f,0.0f,0.0f,0.0f), gid); }\n"
    "}\n"
    "kernel void r106Upsample(texture2d<float, access::read> grid [[texture(0)]], texture2d<half, access::write> motion [[texture(1)]], uint2 gid [[thread_position_in_grid]]) {\n"
    "  if (gid.x >= motion.get_width() || gid.y >= motion.get_height()) return;\n"
    "  uint2 gridPos = uint2(min(grid.get_width() - 1, uint((float(gid.x) + float(motion.get_width()) / float(grid.get_width()) * 0.5f) * float(grid.get_width()) / float(motion.get_width()))), min(grid.get_height() - 1, uint((float(gid.y) + float(motion.get_height()) / float(grid.get_height()) * 0.5f) * float(grid.get_height()) / float(motion.get_height()))));\n"
    "  float2 v = grid.read(gridPos).xy;\n"
    "  motion.write(half4(half(v.x), half(v.y), half(0.0f), half(0.0f)), gid);\n"
    "}\n";

static uint64_t nowNs(void) {
    static mach_timebase_info_data_t timebase;
    if (!timebase.denom) mach_timebase_info(&timebase);
    return mach_absolute_time() * timebase.numer / timebase.denom;
}

static NSData *readFile(NSString *path) {
    NSError *error = nil;
    NSData *data = [NSData dataWithContentsOfFile:path options:0 error:&error];
    if (!data) fprintf(stderr, "read failed: %s\n", error.localizedDescription.UTF8String);
    return data;
}

static id<MTLTexture> makeInput(id<MTLDevice> device, NSUInteger width, NSUInteger height,
                                 const void *bytes, NSUInteger rowBytes) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:width height:height mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> t = [device newTextureWithDescriptor:d];
    [t replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:bytes bytesPerRow:rowBytes];
    return t;
}

static id<MTLTexture> makeOutput(id<MTLDevice> device, MTLPixelFormat format,
                                  NSUInteger width, NSUInteger height) {
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format width:width height:height mipmapped:NO];
    d.storageMode = MTLStorageModeShared;
    d.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    return [device newTextureWithDescriptor:d];
}

static BOOL writeTexture(id<MTLTexture> texture, NSString *path, NSUInteger bytesPerPixel) {
    NSUInteger rowBytes = texture.width * bytesPerPixel;
    NSMutableData *data = [NSMutableData dataWithLength:rowBytes * texture.height];
    [texture getBytes:data.mutableBytes bytesPerRow:rowBytes fromRegion:MTLRegionMake2D(0, 0, texture.width, texture.height) mipmapLevel:0];
    return [data writeToFile:path atomically:YES];
}

static BOOL buildPipeline(id<MTLDevice> device, NSString *name,
                          id<MTLComputePipelineState> *outPipeline) {
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:kKernelSource options:nil error:&error];
    id<MTLFunction> function = [library newFunctionWithName:name];
    id<MTLComputePipelineState> pipeline = function ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
    if (!pipeline) fprintf(stderr, "%s pipeline failed: %s\n", name.UTF8String ?: "unknown", error.localizedDescription.UTF8String ?: "no function");
    *outPipeline = pipeline;
    return pipeline != nil;
}

static float halfToFloat(uint16_t h);

static int compareFloat(const void *a, const void *b) {
    float x = *(const float *)a, y = *(const float *)b;
    return (x > y) - (x < y);
}

static void regionStats(const uint8_t *raw, const uint16_t *flow, NSUInteger width, NSUInteger height,
                        const char *label, int kind) {
    NSMutableData *xData = [NSMutableData data];
    NSMutableData *yData = [NSMutableData data];
    NSMutableData *magData = [NSMutableData data];
    const uint16_t *xy = flow;
    for (NSUInteger y = 0; y < height; ++y) {
        for (NSUInteger x = 0; x < width; ++x) {
            const uint8_t *p = raw + (y * width + x) * 4;
            BOOL main = p[0] > 220 && p[1] > 160 && p[2] < 80;
            BOOL marker = p[0] < 80 && p[1] > 100 && p[2] > 220;
            BOOL selected = kind == 0 ? main : (kind == 1 ? marker : (x > 500 && y > 100 && y + 100 < height));
            if (kind == 0) selected = main;
            if (kind == 1) selected = marker;
            if (kind == 2) selected = selected && !main && !marker;
            if (!selected) continue;
            NSUInteger i = (y * width + x) * 2;
            float fx = halfToFloat(xy[i]);
            float fy = halfToFloat(xy[i + 1]);
            [xData appendBytes:&fx length:sizeof(fx)];
            [yData appendBytes:&fy length:sizeof(fy)];
            float mag = hypotf(fx, fy);
            [magData appendBytes:&mag length:sizeof(mag)];
        }
    }
    NSUInteger n = xData.length / sizeof(float);
    if (!n) { printf("FLOW_REGION label=%s pixels=0\n", label); return; }
    float *xs = (float *)xData.mutableBytes, *ys = (float *)yData.mutableBytes, *ms = (float *)magData.mutableBytes;
    qsort(xs, n, sizeof(float), compareFloat);
    qsort(ys, n, sizeof(float), compareFloat);
    qsort(ms, n, sizeof(float), compareFloat);
    printf("FLOW_REGION label=%s pixels=%lu median=(%.6f,%.6f) medianMagnitude=%.6f p90Magnitude=%.6f\n", label, (unsigned long)n, xs[n/2], ys[n/2], ms[n/2], ms[(n*9)/10]);
}

static float halfToFloat(uint16_t h) {
    uint16_t sign = (h >> 15) & 1, exp = (h >> 10) & 31, mant = h & 1023;
    if (exp == 0) return sign ? -ldexpf((float)mant, -24) : ldexpf((float)mant, -24);
    if (exp == 31) return mant ? NAN : (sign ? -INFINITY : INFINITY);
    float v = ldexpf(1.0f + (float)mant / 1024.0f, (int)exp - 15);
    return sign ? -v : v;
}

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 5) { fprintf(stderr, "usage: %s previous.raw current.raw width height\n", argv[0]); return 2; }
        NSUInteger width = (NSUInteger)strtoul(argv[3], NULL, 10), height = (NSUInteger)strtoul(argv[4], NULL, 10);
        NSString *previousPath = [NSString stringWithUTF8String:argv[1]], *currentPath = [NSString stringWithUTF8String:argv[2]];
        NSData *previousBytes = readFile(previousPath), *currentBytes = readFile(currentPath);
        if (!previousBytes || !currentBytes || previousBytes.length != width * height * 4 || currentBytes.length != width * height * 4) return 2;
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLCommandQueue> queue = [device newCommandQueue];
        const NSUInteger gridWidth = (width + 39) / 40;
        const NSUInteger gridHeight = (height + 39) / 40;
        id<MTLTexture> previous = makeInput(device, width, height, previousBytes.bytes, width * 4);
        id<MTLTexture> current = makeInput(device, width, height, currentBytes.bytes, width * 4);
        id<MTLTexture> grid = makeOutput(device, MTLPixelFormatRGBA32Float, gridWidth, gridHeight);
        id<MTLTexture> propagatedGrid = makeOutput(device, MTLPixelFormatRGBA32Float, gridWidth, gridHeight);
        id<MTLTexture> sampleGrid = makeOutput(device, MTLPixelFormatRGBA32Float, gridWidth, gridHeight);
        id<MTLTexture> motion = makeOutput(device, MTLPixelFormatRG16Float, width, height);
        id<MTLComputePipelineState> probe = nil, match = nil, propagate = nil, upsample = nil;
        if (!device || !queue || !previous || !current || !grid || !propagatedGrid || !sampleGrid || !motion || !buildPipeline(device, @"r106InputProbe", &probe) || !buildPipeline(device, @"r106BlockMatch", &match) || !buildPipeline(device, @"r106PropagateFlow", &propagate) || !buildPipeline(device, @"r106Upsample", &upsample)) return 3;
        id<MTLCommandBuffer> cb = [queue commandBuffer];
        id<MTLComputeCommandEncoder> enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:probe]; [enc setTexture:previous atIndex:0]; [enc setTexture:current atIndex:1]; [enc setTexture:sampleGrid atIndex:2];
        [enc dispatchThreads:MTLSizeMake(gridWidth, gridHeight, 1) threadsPerThreadgroup:MTLSizeMake(4, 4, 1)];
        [enc endEncoding];
        enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:match];
        [enc setTexture:previous atIndex:0]; [enc setTexture:current atIndex:1]; [enc setTexture:grid atIndex:2];
        [enc dispatchThreads:MTLSizeMake(gridWidth, gridHeight, 1) threadsPerThreadgroup:MTLSizeMake(4, 4, 1)];
        [enc endEncoding];
        enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:propagate]; [enc setTexture:current atIndex:0]; [enc setTexture:grid atIndex:1]; [enc setTexture:propagatedGrid atIndex:2];
        [enc dispatchThreads:MTLSizeMake(gridWidth, gridHeight, 1) threadsPerThreadgroup:MTLSizeMake(4, 4, 1)];
        [enc endEncoding];
        enc = [cb computeCommandEncoder];
        [enc setComputePipelineState:upsample]; [enc setTexture:propagatedGrid atIndex:0]; [enc setTexture:motion atIndex:1];
        [enc dispatchThreads:MTLSizeMake(width, height, 1) threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
        [enc endEncoding];
        uint64_t start = nowNs(); [cb commit]; [cb waitUntilCompleted]; uint64_t elapsed = nowNs() - start;
        printf("R106_GPU_MOTION_PROBE status=%lu error=%s device=%s dimensions=%lux%lu grid=%lux%lu cpuElapsedMs=%.3f gpuElapsedMs=%.3f\n", (unsigned long)cb.status, cb.error.localizedDescription.UTF8String ?: "none", device.name.UTF8String ?: "unknown", (unsigned long)width, (unsigned long)height, (unsigned long)gridWidth, (unsigned long)gridHeight, elapsed / 1e6, (cb.GPUEndTime > cb.GPUStartTime ? (cb.GPUEndTime - cb.GPUStartTime) * 1000.0 : 0.0));
        if (cb.status != MTLCommandBufferStatusCompleted || cb.error) return 4;
        NSString *base = [currentPath stringByDeletingLastPathComponent];
        writeTexture(sampleGrid, [base stringByAppendingPathComponent:@"motion-input-samples.rgba32f"], 16);
        writeTexture(grid, [base stringByAppendingPathComponent:@"motion-grid-pair-3-4-unpropagated.rgba32f"], 16);
        writeTexture(propagatedGrid, [base stringByAppendingPathComponent:@"motion-grid-pair-3-4.rgba32f"], 16);
        writeTexture(motion, [base stringByAppendingPathComponent:@"motion-pair-3-4.rg16f"], 4);
        NSMutableData *motionBytes = [NSMutableData dataWithLength:width * height * 4];
        [motion getBytes:motionBytes.mutableBytes bytesPerRow:width * 4 fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        regionStats(currentBytes.bytes, motionBytes.bytes, width, height, "main", 0);
        regionStats(currentBytes.bytes, motionBytes.bytes, width, height, "fast-marker", 1);
        regionStats(currentBytes.bytes, motionBytes.bytes, width, height, "static-background", 2);
        return 0;
    }
}
