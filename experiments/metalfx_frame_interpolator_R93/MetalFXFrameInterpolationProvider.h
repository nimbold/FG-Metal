#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>

NS_ASSUME_NONNULL_BEGIN

/// One generated frame. Its identity is private to the INTERNAL presentation path.
@interface FGMetalInternalFrame : NSObject
@property(nonatomic, readonly) id<MTLTexture> texture;
@property(nonatomic, readonly) uint64_t internalPresentId;
- (instancetype)init NS_UNAVAILABLE;
@end

/// R116: optional extra encoding appended to the MetalFX transition command
/// buffer, between the interpolator encode and the commit.
///
/// The block receives the command buffer and the freshly allocated INTERNAL
/// output texture, so a caller can reduce over the interpolated result without a
/// second submission. Metal hazard tracking orders the appended work after the
/// interpolator's own passes because both use tracked (non-heap) resources.
///
/// This exists to remove a synchronous GPU round trip from the critical path:
/// work encoded here costs only its GPU time, not a full commit -> wait cycle.
typedef void (^FGMetalFXAdditionalEncodingBlock)(id<MTLCommandBuffer> commandBuffer,
                                                 id<MTLTexture> interpolatedOutput);

/// Experimental MetalFX provider for a single fixed-size SDR texture configuration.
/// Callers supply two source frames and their corresponding depth/motion inputs.
/// The first completed non-reset transition establishes temporal history and returns
/// nil without an error; only later completed transitions produce INTERNAL frames.
@interface MetalFXFrameInterpolationProvider : NSObject
+ (BOOL)supportsDevice:(id<MTLDevice>)device;
- (nullable instancetype)initWithDevice:(id<MTLDevice>)device
                                  width:(NSUInteger)width
                                 height:(NSUInteger)height
                                  error:(NSError * _Nullable * _Nullable)error NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

- (nullable FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                                currentColor:(id<MTLTexture>)currentColor
                                                 depthTexture:(id<MTLTexture>)depthTexture
                                                motionTexture:(id<MTLTexture>)motionTexture
                                                    deltaTime:(float)deltaTime
                                                        error:(NSError * _Nullable * _Nullable)error;

/// R116 variant. `additionalEncoding` may be nil, in which case behaviour is
/// identical to the method above. When non-nil it is invoked once, on the calling
/// thread, after the interpolator has been encoded into the transition command
/// buffer and before that command buffer is committed. The transition still
/// blocks on completion, so the caller may read any GPU result the block wrote
/// as soon as this method returns.
- (nullable FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                                currentColor:(id<MTLTexture>)currentColor
                                                 depthTexture:(id<MTLTexture>)depthTexture
                                                motionTexture:(id<MTLTexture>)motionTexture
                                                    deltaTime:(float)deltaTime
                                         additionalEncoding:(nullable FGMetalFXAdditionalEncodingBlock)additionalEncoding
                                                        error:(NSError * _Nullable * _Nullable)error;
@end

NS_ASSUME_NONNULL_END
