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
/// A call on a provider without established temporal history first seeds history
/// from the pair's A frame (warmup pass) and then yields that same pair's real
/// A->B INTERNAL frame. R138: the pre-R138 discard of that first frame left the
/// seed pair structurally unproducible and amplified the measured heavy-area
/// starvation cascade.
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

/// Drops MetalFX's temporal history at a runtime epoch boundary. The next
/// interpolation call seeds history from its SOURCE pair (a warmup pass inside
/// the same call) and still yields that pair's real A->B INTERNAL frame.
/// R138: the pre-R138 discard of the first post-reset output left the seed
/// pair structurally unproducible and chained the measured heavy-area
/// starvation cascade.
- (void)invalidateHistory;

/// R136 diagnostics: YES when temporal history is established, i.e. the next
/// interpolation is a real pair rather than a seeding transition. Read-only.
- (BOOL)historyValid;

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
