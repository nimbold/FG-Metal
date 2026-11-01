#import "MetalFXFrameInterpolationProvider.h"
#include <stdio.h>
#include <stdlib.h>

static id<MTLTexture> makeInput(id<MTLDevice> device, MTLPixelFormat format, NSUInteger width, NSUInteger height) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:format width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead;
    return [device newTextureWithDescriptor:descriptor];
}

int main(void) {
    @autoreleasepool {
        const NSUInteger width = 64, height = 64;
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        NSError *error = nil;
        if (!device || ![MetalFXFrameInterpolationProvider supportsDevice:device]) return 2;
        MetalFXFrameInterpolationProvider *provider = [[MetalFXFrameInterpolationProvider alloc] initWithDevice:device width:width height:height error:&error];
        if (!provider) { printf("provider_create=failed error=%s\n", error.localizedDescription.UTF8String); return 3; }
        id<MTLTexture> previous = makeInput(device, MTLPixelFormatRGBA16Float, width, height);
        id<MTLTexture> current = makeInput(device, MTLPixelFormatRGBA16Float, width, height);
        id<MTLTexture> next = makeInput(device, MTLPixelFormatRGBA16Float, width, height);
        id<MTLTexture> depth = makeInput(device, MTLPixelFormatR32Float, width, height);
        id<MTLTexture> motion = makeInput(device, MTLPixelFormatRG16Float, width, height);
        uint16_t *a = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *b = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *c = calloc(width * height * 4, sizeof(uint16_t));
        float *z = malloc(width * height * sizeof(float));
        uint16_t *v = calloc(width * height * 2, sizeof(uint16_t));
        for (NSUInteger i = 0; i < width * height; ++i) {
            a[i * 4 + 2] = 0x3c00; a[i * 4 + 3] = 0x3c00;
            b[i * 4 + 0] = 0x3c00; b[i * 4 + 3] = 0x3c00;
            c[i * 4 + 1] = 0x3c00; c[i * 4 + 3] = 0x3c00;
            z[i] = 0.5f;
        }
        [previous replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:a bytesPerRow:width * 8];
        [current replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:b bytesPerRow:width * 8];
        [next replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:c bytesPerRow:width * 8];
        [depth replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:z bytesPerRow:width * 4];
        [motion replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:v bytesPerRow:width * 4];
        FGMetalInternalFrame *establishment = [provider interpolatePreviousColor:previous currentColor:current depthTexture:depth motionTexture:motion deltaTime:1.0f / 60.0f error:&error];
        if (establishment || error) {
            printf("provider_history_establishment=failed frame=%s error=%s\n",
                   establishment ? "unexpected" : "none", error.localizedDescription.UTF8String ?: "none");
            return 4;
        }
        puts("provider_history_establishment=completed eligible_frame=none error=none");
        error = nil;
        FGMetalInternalFrame *frame = [provider interpolatePreviousColor:current currentColor:next depthTexture:depth motionTexture:motion deltaTime:1.0f / 60.0f error:&error];
        if (!frame) { printf("provider_interpolation=failed error=%s\n", error.localizedDescription.UTF8String); return 4; }
        MTLTextureDescriptor *readbackDescriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float width:width height:height mipmapped:NO];
        readbackDescriptor.storageMode = MTLStorageModeShared;
        readbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        id<MTLTexture> readback = [device newTextureWithDescriptor:readbackDescriptor];
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> readbackBuffer = [queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [readbackBuffer blitCommandEncoder];
        [blit copyFromTexture:frame.texture toTexture:readback];
        [blit endEncoding];
        [readbackBuffer commit];
        [readbackBuffer waitUntilCompleted];
        uint16_t *pixels = calloc(width * height * 4, sizeof(uint16_t));
        [readback getBytes:pixels bytesPerRow:width * 8 fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        NSUInteger center = ((height / 2) * width + width / 2) * 4;
        printf("provider_create=success supportsDevice=true output_private=%s internal_present_id=%llu output_format=%u "
               "output_readback=completed center_rgba16=0x%04x,0x%04x,0x%04x,0x%04x\n",
               frame.texture.storageMode == MTLStorageModePrivate ? "true" : "false",
               (unsigned long long)frame.internalPresentId, (unsigned)frame.texture.pixelFormat,
               pixels[center], pixels[center + 1], pixels[center + 2], pixels[center + 3]);
        free(a); free(b); free(c); free(z); free(v); free(pixels);
        return frame.internalPresentId == 1 && frame.texture.storageMode == MTLStorageModePrivate &&
               readbackBuffer.status == MTLCommandBufferStatusCompleted ? 0 : 5;
    }
}
