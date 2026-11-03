#import "MetalFXFrameInterpolationProvider.h"
#include <math.h>
#include <stdint.h>

static NSString * const FGMetalFXProviderErrorDomain = @"FGMetalFXFrameInterpolationProvider";

typedef NS_ENUM(NSInteger, FGMetalFXProviderError) {
    FGMetalFXProviderErrorUnsupported = 1,
    FGMetalFXProviderErrorInvalidConfiguration,
    FGMetalFXProviderErrorTextureMismatch,
    FGMetalFXProviderErrorAllocation,
    FGMetalFXProviderErrorEncoding,
    FGMetalFXProviderErrorIdentityExhausted,
};

static void FGSetError(NSError **error, FGMetalFXProviderError code, NSString *description) {
    if (error) {
        *error = [NSError errorWithDomain:FGMetalFXProviderErrorDomain
                                     code:code
                                 userInfo:@{NSLocalizedDescriptionKey: description}];
    }
}

@interface FGMetalInternalFrame ()
- (instancetype)initWithTexture:(id<MTLTexture>)texture internalPresentId:(uint64_t)internalPresentId;
@end

@implementation FGMetalInternalFrame
- (instancetype)initWithTexture:(id<MTLTexture>)texture internalPresentId:(uint64_t)internalPresentId {
    self = [super init];
    if (self) {
        _texture = texture;
        _internalPresentId = internalPresentId;
    }
    return self;
}
@end

@interface MetalFXFrameInterpolationProvider ()
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) id<MTLFXFrameInterpolator> interpolator;
@property(nonatomic, strong) NSLock *lock;
@property(nonatomic) NSUInteger width;
@property(nonatomic) NSUInteger height;
@property(nonatomic) uint64_t nextInternalPresentId;
@property(nonatomic) BOOL hasValidHistory;
@end

@implementation MetalFXFrameInterpolationProvider

+ (BOOL)supportsDevice:(id<MTLDevice>)device {
    if (@available(macOS 26.0, *)) {
        return device != nil && [MTLFXFrameInterpolatorDescriptor supportsDevice:device];
    }
    return NO;
}

- (nullable instancetype)initWithDevice:(id<MTLDevice>)device
                                  width:(NSUInteger)width
                                 height:(NSUInteger)height
                                  error:(NSError **)error {
    if (!device || width == 0 || height == 0) {
        FGSetError(error, FGMetalFXProviderErrorInvalidConfiguration, @"A Metal device and nonzero dimensions are required.");
        return nil;
    }
    if (![MetalFXFrameInterpolationProvider supportsDevice:device]) {
        FGSetError(error, FGMetalFXProviderErrorUnsupported, @"The Metal device does not support MetalFX frame interpolation.");
        return nil;
    }

    self = [super init];
    if (!self) return nil;

    MTLFXFrameInterpolatorDescriptor *descriptor = [MTLFXFrameInterpolatorDescriptor new];
    descriptor.colorTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.depthTextureFormat = MTLPixelFormatR32Float;
    descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
    descriptor.inputWidth = width;
    descriptor.inputHeight = height;
    descriptor.outputWidth = width;
    descriptor.outputHeight = height;

    id<MTLFXFrameInterpolator> interpolator = [descriptor newFrameInterpolatorWithDevice:device];
    id<MTLCommandQueue> commandQueue = [device newCommandQueue];
    if (!interpolator || !commandQueue) {
        FGSetError(error, FGMetalFXProviderErrorAllocation, @"MetalFX interpolator or Metal command queue creation failed.");
        return nil;
    }

    _device = device;
    _commandQueue = commandQueue;
    _interpolator = interpolator;
    _lock = [NSLock new];
    _width = width;
    _height = height;
    _nextInternalPresentId = 1;
    return self;
}

- (nullable FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                                currentColor:(id<MTLTexture>)currentColor
                                                 depthTexture:(id<MTLTexture>)depthTexture
                                                motionTexture:(id<MTLTexture>)motionTexture
                                                    deltaTime:(float)deltaTime
                                                        error:(NSError **)error {
    return [self interpolatePreviousColor:previousColor
                             currentColor:currentColor
                             depthTexture:depthTexture
                            motionTexture:motionTexture
                                deltaTime:deltaTime
                     additionalEncoding:nil
                                    error:error];
}

- (void)invalidateHistory {
    [self.lock lock];
    self.hasValidHistory = NO;
    [self.lock unlock];
}

- (BOOL)historyValid {
    [self.lock lock];
    const BOOL valid = self.hasValidHistory;
    [self.lock unlock];
    return valid;
}

- (nullable FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                                currentColor:(id<MTLTexture>)currentColor
                                                 depthTexture:(id<MTLTexture>)depthTexture
                                                motionTexture:(id<MTLTexture>)motionTexture
                                                    deltaTime:(float)deltaTime
                                         additionalEncoding:(FGMetalFXAdditionalEncodingBlock)additionalEncoding
                                                        error:(NSError **)error {
    if (!previousColor || !currentColor || !depthTexture || !motionTexture || !isfinite(deltaTime) || deltaTime <= 0.0f) {
        FGSetError(error, FGMetalFXProviderErrorInvalidConfiguration, @"Previous/current color, depth, motion, and a positive finite delta time are required.");
        return nil;
    }

    [_lock lock];
    @try {
        const uint64_t deviceID = self.device.registryID;
        BOOL validTextures = previousColor.device.registryID == deviceID &&
            currentColor.device.registryID == deviceID && depthTexture.device.registryID == deviceID &&
            motionTexture.device.registryID == deviceID &&
            previousColor.width == self.width && previousColor.height == self.height &&
            currentColor.width == self.width && currentColor.height == self.height &&
            depthTexture.width == self.width && depthTexture.height == self.height &&
            motionTexture.width == self.width && motionTexture.height == self.height &&
            previousColor.pixelFormat == MTLPixelFormatRGBA16Float &&
            currentColor.pixelFormat == MTLPixelFormatRGBA16Float &&
            depthTexture.pixelFormat == MTLPixelFormatR32Float &&
            motionTexture.pixelFormat == MTLPixelFormatRG16Float &&
            (previousColor.usage & self.interpolator.colorTextureUsage) == self.interpolator.colorTextureUsage &&
            (currentColor.usage & self.interpolator.colorTextureUsage) == self.interpolator.colorTextureUsage &&
            (depthTexture.usage & self.interpolator.depthTextureUsage) == self.interpolator.depthTextureUsage &&
            (motionTexture.usage & self.interpolator.motionTextureUsage) == self.interpolator.motionTextureUsage;
        if (!validTextures) {
            FGSetError(error, FGMetalFXProviderErrorTextureMismatch, @"Input textures must match this provider's device, dimensions, formats, and MetalFX usage requirements.");
            return nil;
        }
        if (self.nextInternalPresentId == UINT64_MAX) {
            FGSetError(error, FGMetalFXProviderErrorIdentityExhausted, @"The INTERNAL frame identity counter is exhausted.");
            return nil;
        }

        MTLTextureDescriptor *outputDescriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:self.interpolator.outputTextureFormat
                                                                                                    width:self.interpolator.outputWidth
                                                                                                   height:self.interpolator.outputHeight
                                                                                                mipmapped:NO];
        outputDescriptor.storageMode = MTLStorageModePrivate;
        // MetalFX reports the minimum usage it needs. Keep those flags and add
        // shader-read access so the presentation path can sample the result.
        outputDescriptor.usage = self.interpolator.outputTextureUsage | MTLTextureUsageShaderRead;
        id<MTLTexture> output = [self.device newTextureWithDescriptor:outputDescriptor];
        if (!output) {
            FGSetError(error, FGMetalFXProviderErrorAllocation, @"Private MetalFX output texture allocation failed.");
            return nil;
        }

        if (!self.hasValidHistory) {
            // Seed MetalFX history with A before asking it to interpolate A -> B.
            self.interpolator.colorTexture = previousColor;
            self.interpolator.prevColorTexture = previousColor;
            self.interpolator.depthTexture = depthTexture;
            self.interpolator.motionTexture = motionTexture;
            self.interpolator.outputTexture = output;
            self.interpolator.deltaTime = deltaTime;
            self.interpolator.nearPlane = 0.1f;
            self.interpolator.farPlane = 100.0f;
            self.interpolator.fieldOfView = 60.0f;
            self.interpolator.aspectRatio = (float)self.width / (float)self.height;
            self.interpolator.motionVectorScaleX = 1.0f;
            self.interpolator.motionVectorScaleY = 1.0f;
            self.interpolator.depthReversed = NO;
            self.interpolator.shouldResetHistory = YES;
            id<MTLCommandBuffer> warmupBuffer = [self.commandQueue commandBuffer];
            if (!warmupBuffer) {
                FGSetError(error, FGMetalFXProviderErrorEncoding, @"Metal command buffer allocation for history priming failed.");
                return nil;
            }
            [self.interpolator encodeToCommandBuffer:warmupBuffer];
            [warmupBuffer commit];
            [warmupBuffer waitUntilCompleted];
            if (warmupBuffer.status != MTLCommandBufferStatusCompleted) {
                NSString *detail = warmupBuffer.error.localizedDescription ?: @"MetalFX history priming command did not complete.";
                FGSetError(error, FGMetalFXProviderErrorEncoding, detail);
                return nil;
            }
            // R138: the warmup pass seeded MetalFX history with A. The real
            // A->B pass encoded below is a genuine interpolation, so the same
            // call must yield it. Discarding it (the pre-R138 behavior) made
            // the first pair after every history reset structurally
            // unproducible, and the measured heavy-area cascade (skip ->
            // gap reset -> seed -> NO-OUTPUT) chained that loss into
            // multi-slot FG-only starvation bursts.
            self.hasValidHistory = YES;
        }

        self.interpolator.colorTexture = currentColor;
        self.interpolator.prevColorTexture = previousColor;
        self.interpolator.depthTexture = depthTexture;
        self.interpolator.motionTexture = motionTexture;
        self.interpolator.outputTexture = output;
        self.interpolator.deltaTime = deltaTime;
        self.interpolator.nearPlane = 0.1f;
        self.interpolator.farPlane = 100.0f;
        self.interpolator.fieldOfView = 60.0f;
        self.interpolator.aspectRatio = (float)self.width / (float)self.height;
        self.interpolator.motionVectorScaleX = 1.0f;
        self.interpolator.motionVectorScaleY = 1.0f;
        self.interpolator.depthReversed = NO;
        self.interpolator.shouldResetHistory = NO;

        id<MTLCommandBuffer> commandBuffer = [self.commandQueue commandBuffer];
        if (!commandBuffer) {
            FGSetError(error, FGMetalFXProviderErrorEncoding, @"Metal command buffer allocation failed.");
            return nil;
        }
        [self.interpolator encodeToCommandBuffer:commandBuffer];
        // R116: caller work appended to the same submission. Encoded after the
        // interpolator so hazard tracking orders it after the output write, and
        // before commit so it costs GPU time only - not a second round trip.
        if (additionalEncoding) {
            additionalEncoding(commandBuffer, output);
        }
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        if (commandBuffer.status != MTLCommandBufferStatusCompleted) {
            self.hasValidHistory = NO;
            NSString *detail = commandBuffer.error.localizedDescription ?: @"MetalFX interpolation command did not complete.";
            FGSetError(error, FGMetalFXProviderErrorEncoding, detail);
            return nil;
        }

        uint64_t internalPresentId = self.nextInternalPresentId++;
        return [[FGMetalInternalFrame alloc] initWithTexture:output internalPresentId:internalPresentId];
    } @finally {
        [_lock unlock];
    }
}

@end
