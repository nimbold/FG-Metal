// R130 actual-observer regression probe.
//
// This translation unit includes the production observer so the test invokes
// the real hooked title present path, ownership decision, record lookup, and
// real R123DisplayPump object. The fake drawable only replaces the platform
// drawable at the API boundary; it does not reproduce ownership decisions.
//
// Build and run with build_r130_observer_integration.sh. The current R127
// implementation is expected to fail this invariant: it suppresses the title
// present even though no private capture image exists yet, leaving 0 WSI
// dispatches. R130 should make the same test pass by committing TITLE and
// allowing the original title dispatch.

#import "observer-title-inprocess-r127.m"

@interface R130FakeTexture : NSObject
@property(nonatomic) NSUInteger width;
@property(nonatomic) NSUInteger height;
@property(nonatomic) NSUInteger pixelFormat;
@property(nonatomic) NSUInteger usage;
@property(nonatomic) BOOL framebufferOnly;
@property(nonatomic, strong) id device;
@end

@interface R132RecordingProvider : NSObject
@property(nonatomic, strong) id<MTLTexture> previousColor;
@property(nonatomic, strong) id<MTLTexture> currentColor;
@property(nonatomic, strong) id<MTLTexture> depthTexture;
@property(nonatomic, strong) id<MTLTexture> motionTexture;
@property(nonatomic) float deltaTime;
@property(nonatomic) NSUInteger invocationCount;
- (FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                       currentColor:(id<MTLTexture>)currentColor
                                        depthTexture:(id<MTLTexture>)depthTexture
                                       motionTexture:(id<MTLTexture>)motionTexture
                                           deltaTime:(float)deltaTime
                                additionalEncoding:(FGMetalFXAdditionalEncodingBlock)additionalEncoding
                                               error:(NSError **)error;
@end

@interface R130FakeLayer : NSObject
@property(nonatomic, strong) id device;
@property(nonatomic) BOOL presentsWithTransaction;
@end

@interface R130FakeDrawable : NSObject
@property(nonatomic, strong) R130FakeTexture *texture;
@property(nonatomic, strong) R130FakeLayer *layer;
@property(nonatomic) NSUInteger drawableID;
@property(nonatomic) NSUInteger titleDispatches;
@property(nonatomic) BOOL isFGDrawable;
@property(nonatomic) uint64_t sourceSequence;
@end

@interface R130FakeEncoder : NSObject
@end

@interface R130FakeCaptureDevice : NSObject
@property(nonatomic) BOOL failAllocation;
@property(nonatomic) uint64_t registryID;
@property(nonatomic) NSUInteger allocationAttempts;
- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)descriptor;
@end

@interface R130FakeCommandBuffer : NSObject
@property(nonatomic) MTLCommandBufferStatus status;
@property(nonatomic, strong) NSError *error;
@property(nonatomic) NSTimeInterval GPUStartTime;
@property(nonatomic) NSTimeInterval GPUEndTime;
@property(nonatomic) BOOL failCopy;
@property(nonatomic) BOOL usesBlit;
@property(nonatomic, copy) MTLCommandBufferHandler completionHandler;
- (id<MTLRenderCommandEncoder>)renderCommandEncoderWithDescriptor:(MTLRenderPassDescriptor *)descriptor;
- (id<MTLBlitCommandEncoder>)blitCommandEncoder;
- (void)addCompletedHandler:(MTLCommandBufferHandler)handler;
- (void)commit;
@end

@interface R130FakeCommandQueue : NSObject
@property(nonatomic, strong) id device;
- (id<MTLCommandBuffer>)commandBuffer;
@end

static R130FakeDrawable *r130NextFGDrawable;
static BOOL r130FailNextDrawableAcquire;
static BOOL r130FailNextCopy;
static NSUInteger r130FGWsiDispatches;
static NSUInteger r130PumpDrawableAcquires;
static NSUInteger r130CaptureBlitCommits;
static uint64_t r130WsiDispatchesBeforeCase;
enum { R130TestSequenceCapacity = 512, R130DispatchOrderCapacity = 1024 };
static uint32_t r130TitleDispatchCount[R130TestSequenceCapacity];
static uint32_t r130FGDispatchCount[R130TestSequenceCapacity];
static uint64_t r130DispatchOrder[R130DispatchOrderCapacity];
static NSUInteger r130DispatchOrderCount;

static id r130FakeNextDrawable(id layer, SEL selector);
static void r130RunPumpPresentation(uint64_t nowNs);

@implementation R130FakeTexture
- (BOOL)isFramebufferOnly { return self.framebufferOnly; }
@end

@implementation R132RecordingProvider
- (FGMetalInternalFrame *)interpolatePreviousColor:(id<MTLTexture>)previousColor
                                       currentColor:(id<MTLTexture>)currentColor
                                        depthTexture:(id<MTLTexture>)depthTexture
                                       motionTexture:(id<MTLTexture>)motionTexture
                                           deltaTime:(float)deltaTime
                                additionalEncoding:(FGMetalFXAdditionalEncodingBlock)additionalEncoding
                                               error:(NSError **)error {
    self.previousColor = previousColor;
    self.currentColor = currentColor;
    self.depthTexture = depthTexture;
    self.motionTexture = motionTexture;
    self.deltaTime = deltaTime;
    ++self.invocationCount;
    (void)additionalEncoding;
    if (error) *error = nil;
    return nil;
}
@end

@implementation R130FakeCaptureDevice
- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)descriptor {
    ++self.allocationAttempts;
    if (self.failAllocation) return nil;
    R130FakeTexture *texture = [R130FakeTexture new];
    texture.width = descriptor.width;
    texture.height = descriptor.height;
    texture.pixelFormat = descriptor.pixelFormat;
    texture.usage = descriptor.usage;
    texture.device = self;
    return (id<MTLTexture>)texture;
}
@end

@implementation R130FakeLayer
@end

@implementation R130FakeDrawable
- (void)addPresentedHandler:(void (^)(id<MTLDrawable> presentedDrawable))handler {
    (void)handler;
}

- (void)present {
    const uint64_t sequence = self.sourceSequence;
    if (sequence < R130TestSequenceCapacity) {
        if (self.isFGDrawable) ++r130FGDispatchCount[sequence];
        else ++r130TitleDispatchCount[sequence];
    }
    if (self.isFGDrawable) ++r130FGWsiDispatches;
    else ++self.titleDispatches;
    if (r130DispatchOrderCount < R130DispatchOrderCapacity)
        r130DispatchOrder[r130DispatchOrderCount++] = sequence;
}
@end

@implementation R130FakeEncoder
- (void)setRenderPipelineState:(id<MTLRenderPipelineState>)pipeline { (void)pipeline; }
- (void)setFragmentTexture:(id<MTLTexture>)texture atIndex:(NSUInteger)index {
    (void)texture;
    (void)index;
}
- (void)drawPrimitives:(MTLPrimitiveType)type vertexStart:(NSUInteger)vertexStart
            vertexCount:(NSUInteger)vertexCount {
    (void)type;
    (void)vertexStart;
    (void)vertexCount;
}
- (void)endEncoding {}
- (void)copyFromTexture:(id<MTLTexture>)source sourceSlice:(NSUInteger)sourceSlice
             sourceLevel:(NSUInteger)sourceLevel sourceOrigin:(MTLOrigin)sourceOrigin
              sourceSize:(MTLSize)sourceSize toTexture:(id<MTLTexture>)destination
        destinationSlice:(NSUInteger)destinationSlice destinationLevel:(NSUInteger)destinationLevel
       destinationOrigin:(MTLOrigin)destinationOrigin {
    (void)source; (void)sourceSlice; (void)sourceLevel; (void)sourceOrigin;
    (void)sourceSize; (void)destination; (void)destinationSlice;
    (void)destinationLevel; (void)destinationOrigin;
}
@end

@implementation R130FakeCommandBuffer
- (id<MTLRenderCommandEncoder>)renderCommandEncoderWithDescriptor:(MTLRenderPassDescriptor *)descriptor {
    (void)descriptor;
    return (id<MTLRenderCommandEncoder>)[R130FakeEncoder new];
}

- (id<MTLBlitCommandEncoder>)blitCommandEncoder {
    self.usesBlit = YES;
    return (id<MTLBlitCommandEncoder>)[R130FakeEncoder new];
}

- (void)addCompletedHandler:(MTLCommandBufferHandler)handler {
    self.completionHandler = handler;
}

- (void)commit {
    if (self.usesBlit) ++r130CaptureBlitCommits;
    self.GPUStartTime = CACurrentMediaTime();
    self.GPUEndTime = self.GPUStartTime + 0.0001;
    if (self.failCopy) {
        self.status = MTLCommandBufferStatusError;
        self.error = [NSError errorWithDomain:@"r130.fake-copy" code:1 userInfo:nil];
    } else {
        self.status = MTLCommandBufferStatusCompleted;
        self.error = nil;
    }
    if (self.completionHandler) self.completionHandler((id<MTLCommandBuffer>)self);
}
@end

@implementation R130FakeCommandQueue
- (id<MTLCommandBuffer>)commandBuffer {
    R130FakeCommandBuffer *commandBuffer = [R130FakeCommandBuffer new];
    commandBuffer.failCopy = r130FailNextCopy;
    r130FailNextCopy = NO;
    return (id<MTLCommandBuffer>)commandBuffer;
}
@end

static id r130FakeNextDrawable(id layer, SEL selector) {
    (void)layer;
    (void)selector;
    ++r130PumpDrawableAcquires;
    if (r130FailNextDrawableAcquire) {
        r130FailNextDrawableAcquire = NO;
        return nil;
    }
    return r130NextFGDrawable;
}

static void r130InstallFakePresenter(R130FakeDrawable *titleDrawable) {
    R130FakeTexture *destinationTexture = [R130FakeTexture new];
    destinationTexture.device = titleDrawable.texture.device;
    destinationTexture.width = titleDrawable.texture.width;
    destinationTexture.height = titleDrawable.texture.height;
    destinationTexture.usage = MTLTextureUsageRenderTarget;
    destinationTexture.framebufferOnly = NO;

    r130NextFGDrawable = [R130FakeDrawable new];
    r130NextFGDrawable.layer = titleDrawable.layer;
    r130NextFGDrawable.texture = destinationTexture;
    r130NextFGDrawable.drawableID = (NSUInteger)(23000 + titleDrawable.drawableID);
    r130NextFGDrawable.isFGDrawable = YES;
    r130NextFGDrawable.sourceSequence = titleDrawable.sourceSequence;
    r130FailNextDrawableAcquire = NO;
    r130FailNextCopy = NO;

    r123Layer = (__bridge void *)titleDrawable.layer;
    r123Queue = (id<MTLCommandQueue>)[R130FakeCommandQueue new];
    r123Pipeline = (id<MTLRenderPipelineState>)[NSObject new];
    originalNextDrawable = (IMP)r130FakeNextDrawable;
}

// This is the production pump claim operation (`takeForSlotAtNs:`) plus the
// same observer in-flight publication performed by r123PumpMain. The claimed
// item then enters the unmodified production r123PresentItem path.
static void r130RunPumpPresentation(uint64_t nowNs) {
    R123DisplayItem *item = nil;
    R123DisplayItem *missed = nil;
    pthread_mutex_lock(&r127RuntimeLock);
    const uint32_t mode = r127RuntimeModeSnapshot();
    const uint64_t epoch = r127RuntimeEpochSnapshot();
    if (mode != R127RuntimeModeOff && r123Pump) {
        (void)[r123Pump takeForSlotAtNs:nowNs outItem:&item outMissed:&missed];
        if (item && item.runtimeEpoch == epoch && !r127PumpItemInFlight)
            r127PumpItemInFlight = item;
        else if (item) {
            [r123Pump rejectUndispatchedItem:item];
            item = nil;
        }
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    (void)missed;
    if (item) r123PresentItem(item, nowNs);
}

enum {
    R130FaultNone = 0,
    R130FaultDrawableAcquire = 1,
    R130FaultPresentationCopy = 2,
    R130FaultPreWsiAbort = 3,
};

static BOOL r130RunFGDispatchWithRetry(uint64_t sequence, int faultStage,
                                       uint64_t *outWsiDelta) {
    r130WsiDispatchesBeforeCase = nextWsiPresentId;
    uint64_t tickNs = r105NowNs() + 1000000000ULL;
    BOOL retryPreserved = YES;
    if (faultStage == R130FaultDrawableAcquire) {
        r130FailNextDrawableAcquire = YES;
        r130RunPumpPresentation(tickNs);
        retryPreserved = r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed &&
            [r123Pump activeSourceReservations] == 1 && [r123Pump depth] == 1 &&
            r127PumpItemInFlight == nil && r130FGWsiDispatches == 0 &&
            nextWsiPresentId == r130WsiDispatchesBeforeCase;
        tickNs += 100000000ULL;
    } else if (faultStage == R130FaultPresentationCopy) {
        r130FailNextCopy = YES;
        r130RunPumpPresentation(tickNs);
        retryPreserved = r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed &&
            [r123Pump activeSourceReservations] == 1 && [r123Pump depth] == 1 &&
            r127PumpItemInFlight == nil && r130FGWsiDispatches == 0 &&
            nextWsiPresentId == r130WsiDispatchesBeforeCase;
        tickNs += 100000000ULL;
    } else if (faultStage == R130FaultPreWsiAbort) {
        const size_t originalHookCount = hookCount;
        hookCount = 0;
        r130RunPumpPresentation(tickNs);
        hookCount = originalHookCount;
        retryPreserved = r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed &&
            [r123Pump activeSourceReservations] == 1 && [r123Pump depth] == 1 &&
            r127PumpItemInFlight == nil && r130FGWsiDispatches == 0 &&
            nextWsiPresentId == r130WsiDispatchesBeforeCase;
        tickNs += 100000000ULL;
    }

    r130RunPumpPresentation(tickNs);
    if (outWsiDelta)
        *outWsiDelta = nextWsiPresentId - r130WsiDispatchesBeforeCase;
    return retryPreserved &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerDispatched &&
        r130FGWsiDispatches == 1 && [r123Pump presentedSource] == 1 &&
        [r123Pump activeSourceReservations] == 0 && [r123Pump depth] == 0 &&
        r127PumpItemInFlight == nil && r130PendingFGDispatches == 0;
}

static BOOL r130FinishOffWithoutPhysicalAck(uint64_t sequence) {
    R123RuntimeEpochResult drain = {0};
    pthread_mutex_lock(&r127RuntimeLock);
    drain = r127AdvanceRuntimeEpochLocked(R127RuntimeModeSourceOnlyDrain);
    r127DisableRequestId = 130;
    r127DisableRequestCutoffSequence = sequence;
    r127LatestSuppressedSequence = sequence;
    pthread_mutex_unlock(&r127RuntimeLock);
    r127TryCompleteDisable(sequence + 1);
    return drain.discardedSource == 0 &&
        r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
        r127LastDisplayedTime == 0.0 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerDispatched &&
        r130FGDispatchCount[sequence] == 1 &&
        r130TitleDispatchCount[sequence] == 0;
}

static void r130NoopOnce(void) {}

static void r130RegisterDrawable(R130FakeDrawable *drawable, uint64_t sequence) {
    if (!drawablesByTexture)
        drawablesByTexture = [NSMutableDictionary dictionary];
    drawablesByTexture[r125PointerKey((__bridge const void *)drawable.texture)] = @{
        @"sequence": @(sequence),
        @"drawableID": @(drawable.drawableID),
        @"drawablePtr": @((uintptr_t)(__bridge void *)drawable),
        @"texturePtr": @((uintptr_t)(__bridge void *)drawable.texture),
        @"layer": @((uintptr_t)(__bridge void *)drawable.layer),
        @"device": @((uintptr_t)(__bridge void *)drawable.layer.device),
        @"acquiredAtNs": @(r105NowNs()),
    };
}

static R130FakeDrawable *r130MakeDrawable(uint64_t sequence) {
    R130FakeLayer *layer = [R130FakeLayer new];
    R130FakeTexture *texture = [R130FakeTexture new];
    R130FakeDrawable *drawable = [R130FakeDrawable new];
    texture.width = 16;
    texture.height = 16;
    texture.usage = 1;
    texture.device = [NSObject new];
    layer.device = texture.device;
    drawable.layer = layer;
    drawable.texture = texture;
    drawable.drawableID = (NSUInteger)(13000 + sequence);
    drawable.sourceSequence = sequence;
    selectedHarnessLayer = (__bridge void *)layer;
    r130RegisterDrawable(drawable, sequence);

    return drawable;
}

static void r130ConfigureEpoch(uint32_t mode, uint64_t epoch,
                               uint64_t minimumSequence) {
    r132ResetForIntegrationTest();
    memset(&r127SourceOwnership, 0, sizeof(r127SourceOwnership));
    memset(r130AcquireTickets, 0, sizeof(r130AcquireTickets));
    r130TitleAcquisitionsOutstanding = 0;
    r130PendingFGDispatches = 0;
    r130NextSourcePresentOrdinal = 1;
    r130LastSourcePresentOrdinalDispatched = 0;
    r130SourcePresentOrderViolations = 0;
    r130AcquireAccountingAmbiguous = NO;
    r130NextFGDrawable = nil;
    r130FailNextDrawableAcquire = NO;
    r130FailNextCopy = NO;
    r130FGWsiDispatches = 0;
    r130PumpDrawableAcquires = 0;
    memset(r130TitleDispatchCount, 0, sizeof(r130TitleDispatchCount));
    memset(r130FGDispatchCount, 0, sizeof(r130FGDispatchCount));
    memset(r130DispatchOrder, 0, sizeof(r130DispatchOrder));
    r130DispatchOrderCount = 0;
    r127RuntimeMode = mode;
    r127RuntimeEpoch = epoch;
    r127MinimumSourceSequence = minimumSequence;
    r127EnableBoundaryResolved = YES;
    r127RuntimeControlReady = NO;
    r127CachedSourceTexture = nil;
    r127CachedSourceSequence = 0;
    r130CachedSourceEpoch = 0;
    r127LastQueuedSourceSequence = 0;
    r127PumpItemInFlight = nil;
    r123WorkerReady = YES;
    r123Pump = [[R123DisplayPump alloc] initWithMaxDepth:kR123MaxDepth
                                           halfPeriodNs:r123HalfPeriodNs()];
    (void)[r123Pump setRuntimeEnabled:mode != R127RuntimeModeOff epoch:epoch];
}

static void r130InstallOriginalIMP(R130FakeDrawable *drawable) {
    if (hookCount != 0) return;
    hooks[hookCount++] = (Hook){
        .cls = object_getClass(drawable),
        .sel = @selector(present),
        .original = class_getMethodImplementation(object_getClass(drawable),
                                                   @selector(present)),
    };
}

static BOOL r130RunCase(const char *name, uint32_t mode, uint64_t sequence,
                        BOOL captureBeforePresent, BOOL lateCapture,
                        BOOL expectsDirectTitleDispatch, int postCommitFaultStage,
                        BOOL duplicateHook, BOOL lateCaptureAfterFgTerminal,
                        BOOL offAfterDispatch) {
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    // Model an active, ready pump epoch without starting the real-time worker.
    // The generating case has no private capture image: only the ordinary
    // title present hook has run when it reaches the ownership decision.
    r130ConfigureEpoch(mode, 1, sequence);
    // Reproduce the production acquisition ticket pair that hookNextDrawable
    // creates before the title calls present. This lets the observer's real
    // ticket/sequence gate run instead of failing for missing test setup.
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    const R127SourceOwner prePresentLookup = r127SourceOwnerForSequence(sequence);
    if (captureBeforePresent)
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);

    // This executes hookDrawablePresent -> r123TrySuppressSourcePresent and,
    // for a TITLE-owned result, the same original-IMP dispatch used in process.
    hookDrawablePresent(drawable, @selector(present));
    if (duplicateHook) hookDrawablePresent(drawable, @selector(present));

    if (lateCapture)
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);

    BOOL fgDispatchPassed = YES;
    BOOL retryPreserved = YES;
    const BOOL retryChecked = postCommitFaultStage != R130FaultNone;
    uint64_t fgWsiIdDelta = 0;
    BOOL pumpDispatchStepRan = NO;
    if (captureBeforePresent && !expectsDirectTitleDispatch) {
        r130InstallFakePresenter(drawable);
        pumpDispatchStepRan = YES;
        fgDispatchPassed = r130RunFGDispatchWithRetry(sequence,
            postCommitFaultStage, &fgWsiIdDelta);
        retryPreserved = fgDispatchPassed;
    }

    BOOL lateFgCaptureRejected = YES;
    if (lateCaptureAfterFgTerminal) {
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);
        lateFgCaptureRejected = r127CachedSourceSequence != sequence &&
            [r123Pump sourceEnqueued] == 1 &&
            r130FGDispatchCount[sequence] == 1;
    }
    BOOL offAfterDispatchPassed = YES;
    if (offAfterDispatch)
        offAfterDispatchPassed = r130FinishOffWithoutPhysicalAck(sequence);

    const R127SourceOwner owner = r127SourceOwnerForSequence(sequence);
    const NSUInteger titleDispatches = drawable.titleDispatches;
    const uint64_t fgQueueAccepts = [r123Pump sourceEnqueued];
    const NSUInteger fgReservations = r123Pump.activeSourceReservations;
    const uint64_t fgPumpDispatches = [r123Pump presentedSource];
    const uint64_t totalObservedDispatches = titleDispatches + fgPumpDispatches;
    const BOOL expectedTitle = expectsDirectTitleDispatch;
    const BOOL cachedAfterLateCapture = r127CachedSourceSequence == sequence;
    const BOOL passed = expectedTitle
        ? owner == R127SourceOwnerTitleDispatched && titleDispatches == 1 &&
            totalObservedDispatches == 1 && fgQueueAccepts == 0 &&
            fgReservations == 0 && !cachedAfterLateCapture &&
            r130TitleDispatchCount[sequence] == 1 &&
            r130FGDispatchCount[sequence] == 0
        : owner == R127SourceOwnerDispatched && titleDispatches == 0 &&
            fgQueueAccepts == 1 && fgReservations == 0 &&
            fgPumpDispatches == 1 && r130FGWsiDispatches == 1 &&
            fgWsiIdDelta == 1 && fgDispatchPassed &&
            r130TitleDispatchCount[sequence] == 0 &&
            r130FGDispatchCount[sequence] == 1 &&
            lateFgCaptureRejected && offAfterDispatchPassed;

    printf("R130_ACTUAL_OBSERVER_CASE=%s %s owner=%u titleWSI=%lu "
           "fgReservations=%lu fgQueued=%llu fgPumpDispatches=%llu "
           "fgWsi=%lu fgWsiIdDelta=%llu totalObserved=%llu "
           "dispatchStep=%s retry=%s lateFgCapture=%s offWithoutDisplayAck=%s "
           "duplicateHook=%d prePresentLookup=%u\n",
        name, passed ? "PASS" : "FAIL", (unsigned)owner,
        (unsigned long)titleDispatches,
        (unsigned long)fgReservations,
        (unsigned long long)fgQueueAccepts,
        (unsigned long long)fgPumpDispatches,
        (unsigned long)r130FGWsiDispatches,
        (unsigned long long)fgWsiIdDelta,
        (unsigned long long)totalObservedDispatches,
        pumpDispatchStepRan ? "REAL-PUMP+R123-PRESENT-ITEM" : "NOT_RUN",
        !retryChecked ? "NOT_RUN" : (retryPreserved ? "PASS" : "FAIL"),
        !lateCaptureAfterFgTerminal ? "NOT_RUN" : (lateFgCaptureRejected ? "PASS" : "FAIL"),
        !offAfterDispatch ? "NOT_RUN" : (offAfterDispatchPassed ? "PASS" : "FAIL"),
        duplicateHook ? 1 : 0, (unsigned)prePresentLookup);
    if (!passed) {
        fprintf(stderr, "Actual observer path violated case `%s`.\n", name);
    }
    return passed;
}

static BOOL r130RunReverseAcquisitionPresentationOrder(void) {
    const uint64_t acquiredFirst = 105;
    const uint64_t acquiredSecond = 106;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, acquiredFirst);
    R130FakeDrawable *first = r130MakeDrawable(acquiredFirst);
    R130FakeDrawable *second = r130MakeDrawable(acquiredSecond);
    R130FakeLayer *sharedLayer = first.layer;
    second.layer = sharedLayer;
    selectedHarnessLayer = (__bridge void *)sharedLayer;
    r130RegisterDrawable(first, acquiredFirst);
    r130RegisterDrawable(second, acquiredSecond);
    r130InstallOriginalIMP(first);

    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(acquiredFirst, (id<CAMetalDrawable>)first);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(acquiredSecond, (id<CAMetalDrawable>)second);

    // Complete both private captures in acquisition order. The deterministic
    // TITLE path then presents them in the opposite order.
    r123WorkerReady = NO;
    r123EnqueueSource(acquiredFirst, acquiredFirst,
                      (id<MTLTexture>)first.texture, 1);
    r123EnqueueSource(acquiredSecond, acquiredSecond,
                      (id<MTLTexture>)second.texture, 1);

    // Reclassifies R130's measured acquire-5, acquire-6, present-6, present-5
    // schedule under presentation-order semantics. Both are title-owned; their
    // ordinals follow the two actual, serialized WSI issue calls.
    hookDrawablePresent(second, @selector(present));
    hookDrawablePresent(first, @selector(present));
    const uint64_t ordinalSecond = R127SourceOwnershipPresentOrdinal(
        &r127SourceOwnership, acquiredSecond);
    const uint64_t ordinalFirst = R127SourceOwnershipPresentOrdinal(
        &r127SourceOwnership, acquiredFirst);
    R132RecordingProvider *recordingProvider = [R132RecordingProvider new];
    NSError *providerError = nil;
    BOOL providerInvoked = NO;
    FGMetalInternalFrame *recordedFrame = nil;
    if (r132TestIngestCount == 2) {
        recordedFrame = r132InterpolateAdjacentPair(
            (MetalFXFrameInterpolationProvider *)recordingProvider,
            r132TestIngestTexture[0], r132TestIngestTexture[1],
            r132TestIngestTexture[0], r132TestIngestTexture[1],
            r132TestIngestSequence[0], r132TestIngestOrdinal[0],
            r132TestIngestSequence[1], r132TestIngestOrdinal[1],
            1.0f / 60.0f, nil, &providerError, &providerInvoked);
    }
    const uint64_t providerPairA =
        recordingProvider.previousColor == r132TestIngestTexture[0]
            ? r132TestIngestSequence[0] :
        recordingProvider.previousColor == r132TestIngestTexture[1]
            ? r132TestIngestSequence[1] : 0;
    const uint64_t providerPairB =
        recordingProvider.currentColor == r132TestIngestTexture[0]
            ? r132TestIngestSequence[0] :
        recordingProvider.currentColor == r132TestIngestTexture[1]
            ? r132TestIngestSequence[1] : 0;
    const BOOL providerBoundaryPassed = providerInvoked &&
        recordingProvider.invocationCount == 1 && !providerError && !recordedFrame &&
        recordingProvider.previousColor == r132TestIngestTexture[0] &&
        recordingProvider.currentColor == r132TestIngestTexture[1] &&
        providerPairA == acquiredSecond && providerPairB == acquiredFirst;
    const BOOL passed = first.titleDispatches == 1 && second.titleDispatches == 1 &&
        r130TitleDispatchCount[acquiredFirst] == 1 &&
        r130TitleDispatchCount[acquiredSecond] == 1 &&
        r130FGDispatchCount[acquiredFirst] == 0 &&
        r130FGDispatchCount[acquiredSecond] == 0 &&
        r130DispatchOrderCount == 2 &&
        r130DispatchOrder[0] == acquiredSecond &&
        r130DispatchOrder[1] == acquiredFirst &&
        ordinalSecond == 1 && ordinalFirst == 2 &&
        r130LastSourcePresentOrdinalDispatched == 2 &&
        r130SourcePresentOrderViolations == 0 &&
        r130TitleAcquisitionsOutstanding == 0 &&
        !r130AcquireAccountingAmbiguous &&
        r132TestIngestCount == 2 &&
        r132TestIngestSequence[0] == acquiredSecond &&
        r132TestIngestOrdinal[0] == ordinalSecond &&
        r132TestIngestSequence[1] == acquiredFirst &&
        r132TestIngestOrdinal[1] == ordinalFirst &&
        r132TestPairA[0] == 0 && r132TestPairB[0] == 0 &&
        r132TestPairA[1] == acquiredSecond &&
        r132TestPairB[1] == acquiredFirst &&
        providerBoundaryPassed &&
        r127SourceOwnerForSequence(acquiredFirst) == R127SourceOwnerTitleDispatched &&
        r127SourceOwnerForSequence(acquiredSecond) == R127SourceOwnerTitleDispatched;
    printf("R131_REVERSE_ACQUISITION_PRESENTATION_ORDER %s acquire=%llu,%llu "
           "wsi=%llu,%llu ordinals=%llu,%llu titleDispatches=%u,%u "
           "fgDispatches=0,0 duplicate=0 zeroDispatch=0 violations=%llu ambiguous=%d\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)acquiredFirst, (unsigned long long)acquiredSecond,
        (unsigned long long)(r130DispatchOrderCount > 0 ? r130DispatchOrder[0] : 0),
        (unsigned long long)(r130DispatchOrderCount > 1 ? r130DispatchOrder[1] : 0),
        (unsigned long long)ordinalSecond, (unsigned long long)ordinalFirst,
        r130TitleDispatchCount[acquiredFirst],
        r130TitleDispatchCount[acquiredSecond],
        (unsigned long long)r130SourcePresentOrderViolations,
        r130AcquireAccountingAmbiguous ? 1 : 0);
    printf("R132_REVERSED_ESTIMATOR_ORDER %s AcquisitionIdX=%llu AcquisitionIdY=%llu "
           "SourcePresentOrdinalY=%llu SourcePresentOrdinalX=%llu ingestion=%llu,%llu "
           "ESTIMATOR_PAIR=%llu->%llu METALFX_PAIR=%llu->%llu providerCalls=%lu "
           "WRONG_PAIR_X_TO_Y=%llu\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)acquiredFirst, (unsigned long long)acquiredSecond,
        (unsigned long long)ordinalSecond, (unsigned long long)ordinalFirst,
        (unsigned long long)r132TestIngestSequence[0],
        (unsigned long long)r132TestIngestSequence[1],
        (unsigned long long)r132TestPairA[1],
        (unsigned long long)r132TestPairB[1],
        (unsigned long long)providerPairA,
        (unsigned long long)providerPairB,
        (unsigned long)recordingProvider.invocationCount,
        (unsigned long long)((providerPairA == acquiredFirst &&
                              providerPairB == acquiredSecond) ? 1 : 0));
    return passed;
}

static R130FakeTexture *r132MakeTestTexture(id device) {
    R130FakeTexture *texture = [R130FakeTexture new];
    texture.width = 16;
    texture.height = 16;
    texture.pixelFormat = MTLPixelFormatBGRA8Unorm;
    texture.usage = MTLTextureUsageShaderRead;
    texture.device = device ?: [NSObject new];
    return texture;
}

static NSUInteger r132TestStageOccupancy(void) {
    pthread_mutex_lock(&r132EstimatorLock);
    const NSUInteger count = r132OccupiedStageCountLocked();
    pthread_mutex_unlock(&r132EstimatorLock);
    return count;
}

static BOOL r132RunGapDispositionTest(void) {
    r132ResetForIntegrationTest();
    id device = [NSObject new];
    R130FakeTexture *sourceA = r132MakeTestTexture(device);
    R130FakeTexture *sourceC = r132MakeTestTexture(device);
    R130FakeTexture *sourceD = r132MakeTestTexture(device);
    const BOOL stagedA = r132StageCapture(100, 100, MTLPixelFormatBGRA8Unorm,
        (id<MTLTexture>)sourceA, (id<MTLDevice>)device, @"gap-A", 1, NO);
    r132PublishPresentation(100, 1, R127SourceOwnerDirect);
    const BOOL stagedC = r132StageCapture(102, 102, MTLPixelFormatBGRA8Unorm,
        (id<MTLTexture>)sourceC, (id<MTLDevice>)device, @"gap-C", 1, NO);
    r132PublishPresentation(101, 2, R127SourceOwnerDirect); // explicit NO_CAPTURE
    r132PublishPresentation(102, 3, R127SourceOwnerDirect);
    const BOOL stagedD = r132StageCapture(103, 103, MTLPixelFormatBGRA8Unorm,
        (id<MTLTexture>)sourceD, (id<MTLDevice>)device, @"gap-D", 1, NO);
    r132PublishPresentation(103, 4, R127SourceOwnerDirect);

    const BOOL passed = stagedA && stagedC && stagedD &&
        r132TestNoCaptureCount == 1 && r132TestIngestCount == 3 &&
        r132TestIngestSequence[0] == 100 && r132TestPairA[0] == 0 &&
        r132TestIngestSequence[1] == 102 && r132TestPairA[1] == 0 &&
        r132TestIngestSequence[2] == 103 &&
        r132TestPairA[2] == 102 && r132TestPairB[2] == 103 &&
        r132EstimatorOrderViolations == 0 && r132TestStageOccupancy() == 0;
    printf("R132_ESTIMATOR_GAP %s dispositions=100:CAPTURE_READY,101:NO_CAPTURE,"
           "102:CAPTURE_READY,103:CAPTURE_READY ingestion=100,102,103 "
           "falsePair100_to_102=%llu nextPair=%llu->%llu noCapture=%llu stagePeak=%llu/%u\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)((r132TestPairA[1] == 100 &&
                              r132TestPairB[1] == 102) ? 1 : 0),
        (unsigned long long)r132TestPairA[2],
        (unsigned long long)r132TestPairB[2],
        (unsigned long long)r132EstimatorNoCaptureCount,
        (unsigned long long)r132EstimatorStagePeak,
        kR132EstimatorStageCapacity);
    return passed;
}

static BOOL r132RunLateCaptureTest(void) {
    const uint64_t sequence = 701;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    r123WorkerReady = NO;
    hookDrawablePresent(drawable, @selector(present));
    const R127SourceOwner ownerAfterNoCapture =
        r127SourceOwnerForSequence(sequence);
    const uint64_t lateBefore = r132EstimatorLateCaptureCount;
    __weak R130FakeTexture *weakLateTexture = nil;
    @autoreleasepool {
        R130FakeTexture *lateTexture = r132MakeTestTexture(drawable.layer.device);
        weakLateTexture = lateTexture;
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)lateTexture, 1);
        lateTexture = nil;
    }
    const BOOL released = weakLateTexture == nil;
    const BOOL passed = ownerAfterNoCapture == R127SourceOwnerTitleDispatched &&
        r127SourceOwnerForSequence(sequence) == ownerAfterNoCapture &&
        r132EstimatorNoCaptureCount == 1 &&
        r132EstimatorLateCaptureCount == lateBefore + 1 &&
        r132TestIngestCount == 0 && r132TestStageOccupancy() == 0 &&
        r127CachedSourceSequence != sequence && released;
    printf("R132_LATE_CAPTURE %s AcquisitionId=%llu SourcePresentOrdinal=1 "
           "disposition=NO_CAPTURE frontier=%llu retroactiveIngestion=%d "
           "oldPairGenerated=%d ownerUnchanged=%d stageOccupancy=%lu released=%d lateCapture=%llu\n",
        passed ? "PASS" : "FAIL", (unsigned long long)sequence,
        (unsigned long long)(r132NextEstimatorOrdinal - 1),
        r132TestIngestCount ? 1 : 0, r132TestPairB[0] ? 1 : 0,
        r127SourceOwnerForSequence(sequence) == ownerAfterNoCapture ? 1 : 0,
        (unsigned long)r132TestStageOccupancy(), released ? 1 : 0,
        (unsigned long long)(r132EstimatorLateCaptureCount - lateBefore));
    return passed;
}

static BOOL r132RunStagingCapacityTest(void) {
    enum { sourceCount = 9, idBase = 100 };
    R130FakeDrawable *__strong drawables[sourceCount] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, idBase);
    R130FakeLayer *layer = [R130FakeLayer new];
    id device = [NSObject new];
    layer.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r123WorkerReady = NO;

    for (uint64_t index = 0; index < sourceCount; ++index) {
        const uint64_t sequence = idBase + index;
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        drawable.layer = layer;
        drawable.texture.device = device;
        selectedHarnessLayer = (__bridge void *)layer;
        r130RegisterDrawable(drawable, sequence);
        if (index == 0) r130InstallOriginalIMP(drawable);
        drawables[index] = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);
    }
    const uint64_t peakBeforePresentation = r132EstimatorStagePeak;
    const uint64_t refusalsBeforePresentation = r132EstimatorStageRefusalCount;
    for (uint64_t index = 0; index < sourceCount; ++index)
        hookDrawablePresent(drawables[index], @selector(present));

    uint64_t titleWsi = 0, fgWsi = 0;
    for (uint64_t index = 0; index < sourceCount; ++index) {
        const uint64_t sequence = idBase + index;
        titleWsi += r130TitleDispatchCount[sequence];
        fgWsi += r130FGDispatchCount[sequence];
    }
    const BOOL passed = peakBeforePresentation == kR132EstimatorStageCapacity &&
        refusalsBeforePresentation == 1 &&
        r132EstimatorStageRefusalCount == 1 &&
        r132EstimatorStagePeak == kR132EstimatorStageCapacity &&
        r132TestIngestCount == kR132EstimatorStageCapacity &&
        r132TestNoCaptureCount == 1 && titleWsi == sourceCount && fgWsi == 0 &&
        r130DispatchOrderCount == sourceCount &&
        r130SourcePresentOrderViolations == 0 && r132NextEstimatorOrdinal == 10 &&
        r132TestStageOccupancy() == 0;
    printf("R132_STAGING_CAPACITY %s stagedBeforePresent=%llu refusal=%llu "
           "capacity=%u ready=%lu noCapture=%llu titleWsi=%llu fgWsi=%llu "
           "frontier=%llu remainingStage=%lu\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)peakBeforePresentation,
        (unsigned long long)refusalsBeforePresentation,
        kR132EstimatorStageCapacity, (unsigned long)r132TestIngestCount,
        (unsigned long long)r132TestNoCaptureCount,
        (unsigned long long)titleWsi, (unsigned long long)fgWsi,
        (unsigned long long)(r132NextEstimatorOrdinal - 1),
        (unsigned long)r132TestStageOccupancy());
    return passed;
}

// A capture can be staged and then never presented: the runtime turns OFF, the
// drawable is abandoned, or the title drops the frame. The ownership transaction
// terminalizes that acquisition without ever assigning an ordinal, so no
// disposition can ever consume the staged texture. R132 must reclaim such an
// entry rather than hold its slot for the life of the process -- otherwise eight
// abandoned captures wedge estimator staging permanently while presentation
// stays correct and the failure is invisible.
static BOOL r132RunStageReclamationTest(void) {
    enum { orphanCount = kR132EstimatorStageCapacity, idBase = 900 };
    R130FakeDrawable *__strong drawables[orphanCount] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, idBase);
    R130FakeLayer *layer = [R130FakeLayer new];
    id device = [NSObject new];
    layer.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r123WorkerReady = NO;

    for (uint64_t index = 0; index < orphanCount; ++index) {
        const uint64_t sequence = idBase + index;
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        drawable.layer = layer;
        drawable.texture.device = device;
        selectedHarnessLayer = (__bridge void *)layer;
        r130RegisterDrawable(drawable, sequence);
        drawables[index] = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);
        // Never presented: the acquisition terminalizes with no ordinal.
        r130FinishTitleAcquisition((id<CAMetalDrawable>)drawable);
    }

    const NSUInteger occupiedWithOrphans = r132TestStageOccupancy();
    const uint64_t refusalsBefore = r132EstimatorStageRefusalCount;

    const uint64_t liveSequence = idBase + orphanCount;
    R130FakeDrawable *live = r130MakeDrawable(liveSequence);
    live.layer = layer;
    live.texture.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r130RegisterDrawable(live, liveSequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(liveSequence, (id<CAMetalDrawable>)live);
    r123EnqueueSource(liveSequence, liveSequence,
                      (id<MTLTexture>)live.texture, 1);

    const BOOL passed = occupiedWithOrphans == kR132EstimatorStageCapacity &&
        r132EstimatorStageReclaimCount == orphanCount &&
        r132EstimatorStageRefusalCount == refusalsBefore &&
        r132TestStageOccupancy() == 1;
    printf("R132_STAGE_RECLAMATION %s orphaned=%lu capacity=%u reclaimed=%llu "
           "refusalsBefore=%llu refusalsAfter=%llu accepted=1 remainingStage=%lu\n",
        passed ? "PASS" : "FAIL", (unsigned long)occupiedWithOrphans,
        kR132EstimatorStageCapacity,
        (unsigned long long)r132EstimatorStageReclaimCount,
        (unsigned long long)refusalsBefore,
        (unsigned long long)r132EstimatorStageRefusalCount,
        (unsigned long)r132TestStageOccupancy());
    return passed;
}

// The ticket test cannot see an acquisition that is acquired and then never
// presented: its ticket is never finished, so it stays "active" forever. The
// second reclamation rule bounds that case by the presentation frontier -- once
// more than a full staging window of presentations has passed, a still-unclaimed
// entry can no longer be a legitimate reorder and must be released.
static BOOL r132RunFrontierReclaimTest(void) {
    enum { orphanCount = kR132EstimatorStageCapacity, idBase = 1000 };
    R130FakeDrawable *__strong drawables[orphanCount] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, idBase);
    R130FakeLayer *layer = [R130FakeLayer new];
    id device = [NSObject new];
    layer.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r123WorkerReady = NO;

    for (uint64_t index = 0; index < orphanCount; ++index) {
        const uint64_t sequence = idBase + index;
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        drawable.layer = layer;
        drawable.texture.device = device;
        selectedHarnessLayer = (__bridge void *)layer;
        r130RegisterDrawable(drawable, sequence);
        drawables[index] = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);
        // Deliberately not finished: the ticket stays active.
    }
    const NSUInteger occupiedWithOrphans = r132TestStageOccupancy();

    // Advance the presentation frontier past a full staging window. These
    // acquisitions were never captured, so each publishes NO_CAPTURE.
    for (uint64_t index = 0; index < orphanCount; ++index)
        r132PublishPresentation(idBase + 50 + index, index + 1,
                                R127SourceOwnerDirect);

    const uint64_t reclaimedBefore = r132EstimatorStageReclaimCount;
    const uint64_t liveSequence = idBase + 80;
    R130FakeDrawable *live = r130MakeDrawable(liveSequence);
    live.layer = layer;
    live.texture.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r130RegisterDrawable(live, liveSequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(liveSequence, (id<CAMetalDrawable>)live);
    r123EnqueueSource(liveSequence, liveSequence,
                      (id<MTLTexture>)live.texture, 1);

    const BOOL passed = occupiedWithOrphans == kR132EstimatorStageCapacity &&
        r132EstimatorStageReclaimCount == reclaimedBefore + orphanCount &&
        r132TestStageOccupancy() == 1;
    printf("R132_FRONTIER_RECLAIM %s orphans=%lu ticketActive=1 capacity=%u "
           "reclaimed=%llu accepted=1 remainingStage=%lu\n",
        passed ? "PASS" : "FAIL", (unsigned long)occupiedWithOrphans,
        kR132EstimatorStageCapacity,
        (unsigned long long)(r132EstimatorStageReclaimCount - reclaimedBefore),
        (unsigned long)r132TestStageOccupancy());
    return passed;
}

static R130FakeDrawable *r132PrepareMixedSource(uint64_t sequence,
                                                R130FakeLayer *layer,
                                                id device,
                                                BOOL captureReady) {    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    drawable.layer = layer;
    drawable.texture.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r130RegisterDrawable(drawable, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    if (captureReady)
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawable.texture, 1);
    return drawable;
}

static BOOL r132RunMixedOwnershipTest(void) {
    const uint64_t first = 400;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, first);
    R130FakeLayer *layer = [R130FakeLayer new];
    id device = [NSObject new];
    layer.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r123WorkerReady = YES;

    R130FakeDrawable *fgA = r132PrepareMixedSource(first, layer, device, YES);
    r130InstallOriginalIMP(fgA);
    r130InstallFakePresenter(fgA);
    hookDrawablePresent(fgA, @selector(present));
    r130RunPumpPresentation(r105NowNs() + 1000000000ULL);

    r123WorkerReady = NO;
    R130FakeDrawable *titleB = r132PrepareMixedSource(first + 1, layer, device, YES);
    hookDrawablePresent(titleB, @selector(present));
    R130FakeDrawable *titleGap = r132PrepareMixedSource(first + 2, layer, device, NO);
    hookDrawablePresent(titleGap, @selector(present));

    r123WorkerReady = YES;
    R130FakeDrawable *fgD = r132PrepareMixedSource(first + 3, layer, device, YES);
    r130InstallFakePresenter(fgD);
    hookDrawablePresent(fgD, @selector(present));
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);

    r123WorkerReady = NO;
    R130FakeDrawable *later = r132PrepareMixedSource(first + 4, layer, device, YES);
    hookDrawablePresent(later, @selector(present));

    uint64_t totalWsi = 0;
    for (uint64_t sequence = first; sequence <= first + 4; ++sequence)
        totalWsi += r130TitleDispatchCount[sequence] + r130FGDispatchCount[sequence];
    const BOOL passed = r132TestNoCaptureCount == 1 &&
        r132TestIngestCount == 4 &&
        r132TestIngestSequence[0] == first &&
        r132TestIngestOwner[0] == R127SourceOwnerSuppressed &&
        r132TestIngestSequence[1] == first + 1 &&
        r132TestIngestOwner[1] == R127SourceOwnerDirect &&
        r132TestPairA[1] == first && r132TestPairB[1] == first + 1 &&
        r132TestIngestSequence[2] == first + 3 &&
        r132TestIngestOwner[2] == R127SourceOwnerSuppressed &&
        r132TestPairA[2] == 0 && r132TestPairB[2] == 0 &&
        r132TestIngestSequence[3] == first + 4 &&
        r132TestIngestOwner[3] == R127SourceOwnerDirect &&
        r132TestPairA[3] == first + 3 && r132TestPairB[3] == first + 4 &&
        r130FGDispatchCount[first] == 1 && r130FGDispatchCount[first + 3] == 1 &&
        r130TitleDispatchCount[first + 1] == 1 &&
        r130TitleDispatchCount[first + 2] == 1 &&
        r130TitleDispatchCount[first + 4] == 1 && totalWsi == 5 &&
        r132EstimatorOrderViolations == 0 &&
        r132EstimatorStagePeak <= kR132EstimatorStageCapacity &&
        r132TestStageOccupancy() == 0;
    printf("R132_MIXED_OWNERSHIP %s sequence=FG_READY,TITLE_READY,TITLE_NO_CAPTURE,"
           "FG_READY,TITLE_READY ordinals=1,2,3,4,5 ingestion=400,401,403,404 "
           "pairs=400->401,SEED,403->404 falsePair401_to_403=%llu totalWsi=%llu "
           "stagePeak=%llu/%u\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)((r132TestPairA[2] == first + 1 &&
                              r132TestPairB[2] == first + 3) ? 1 : 0),
        (unsigned long long)totalWsi,
        (unsigned long long)r132EstimatorStagePeak,
        kR132EstimatorStageCapacity);
    return passed;
}

// ---------------------------------------------------------------------------
// R133 deterministic matrix.
//
// The in-producer-command-buffer copy is modelled by staging a committed but
// not yet complete private capture and installing it in the same cache slot the
// commit hook fills. Everything below the staging call is the production path:
// r133CaptureCommittedForSequence, the present hook's ownership decision, the
// real R123 pump and the production estimator-ordinal functions.
// ---------------------------------------------------------------------------
static BOOL r133StageCommittedForTest(uint64_t sequence, id<MTLTexture> texture,
                                      id<MTLDevice> device,
                                      uintptr_t commandBuffer) {
    const BOOL staged = r133StageCommittedCapture(sequence, sequence,
        MTLPixelFormatBGRA8Unorm, texture, device, @"r133-test", 1, NO,
        commandBuffer, r105NowNs(), NULL);
    if (staged) {
        pthread_mutex_lock(&r127RuntimeLock);
        r127CachedSourceTexture = texture;
        r127CachedSourceSequence = sequence;
        r130CachedSourceEpoch = 1;
        pthread_mutex_unlock(&r127RuntimeLock);
    }
    return staged;
}

static BOOL r133RunPendingLateCaptureInsideGrace(void) {
    const uint64_t sequence = 460;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    // The private capture must match the drawable's own device/shape, because
    // the production pump validates that before it presents.
    id device = drawable.texture.device;
    R130FakeTexture *texture = r132MakeTestTexture(device);
    const BOOL staged = r133StageCommittedForTest(sequence, (id<MTLTexture>)texture,
                                                  (id<MTLDevice>)device, 0x460);

    hookDrawablePresent(drawable, @selector(present));
    const R127SourceOwner ownerAfterPresent = r127SourceOwnerForSequence(sequence);
    const BOOL pendingBefore = r133PendingCount == 1 && r132TestIngestCount == 0 &&
        r132EstimatorReadyCount == 0 && ownerAfterPresent == R127SourceOwnerSuppressed &&
        r130TitleDispatchCount[sequence] == 0 &&
        [r123Pump activeSourceReservations] == 1;

    r133CaptureDidComplete(sequence, 0x460, YES, r105NowNs());
    const BOOL resolved = r133PendingCount == 0 && r132TestIngestCount == 1 &&
        r132TestIngestSequence[0] == sequence && r132TestIngestOrdinal[0] == 1 &&
        r133PendingResolvedReady == 1 && r132EstimatorReadyCount == 1 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed;

    r130InstallFakePresenter(drawable);
    r130RunPumpPresentation(r105NowNs() + 1000000000ULL);
    const BOOL presented = r130FGDispatchCount[sequence] == 1 &&
        r130TitleDispatchCount[sequence] == 0 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerDispatched &&
        [r123Pump activeSourceReservations] == 0 && r130PendingFGDispatches == 0;

    // A duplicate completion must not dispatch anything or mutate the owner.
    r133CaptureDidComplete(sequence, 0x460, YES, r105NowNs());
    const BOOL immutable = r127SourceOwnerForSequence(sequence) ==
        R127SourceOwnerDispatched && r130FGDispatchCount[sequence] == 1 &&
        r132TestIngestCount == 1;
    const BOOL passed = staged && pendingBefore && resolved && presented && immutable;
    printf("R133_PENDING_CAPTURE_WITHIN_GRACE %s AcquisitionId=%llu ordinal=1 "
           "pendingBefore=1 pendingAfter=%lu ingestion=%lu ownerAfterPresent=%u "
           "ownerAfterWsi=%u fgWsi=%u titleWsi=%lu secondSourceWsi=%d\n",
        passed ? "PASS" : "FAIL", (unsigned long long)sequence,
        (unsigned long)r133PendingCount, (unsigned long)r132TestIngestCount,
        (unsigned)ownerAfterPresent,
        (unsigned)r127SourceOwnerForSequence(sequence),
        r130FGDispatchCount[sequence],
        (unsigned long)r130TitleDispatchCount[sequence],
        (r130FGDispatchCount[sequence] == 1 && r130TitleDispatchCount[sequence] == 0) ? 0 : 1);
    return passed;
}

static BOOL r133RunPendingExpiryAndLateRejection(void) {
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 461);
    id device = [NSObject new];
    R130FakeTexture *textureA = r132MakeTestTexture(device);
    const BOOL staged = r133StageCommittedForTest(461, (id<MTLTexture>)textureA,
                                                  (id<MTLDevice>)device, 0x461);
    r132PublishPresentation(461, 1, R127SourceOwnerSuppressed);
    const BOOL pending = r133PendingCount == 1 && r132TestIngestCount == 0;
    // Publishing ordinal 2 closes ordinal 1's one-ordinal window.
    r132PublishPresentation(462, 2, R127SourceOwnerDirect);
    const BOOL expired = r133PendingCount == 0 && r133PendingExpired == 1 &&
        r132EstimatorNoCaptureCount == 2 && r132TestNoCaptureCount == 2 &&
        r132TestIngestCount == 0 && r133LastResolvedOrdinal == 2 &&
        r132NextEstimatorOrdinal == 3;

    // The copy completes after the window closed. It must be rejected, release
    // its slot, and never rewrite history.
    const uint64_t lateBefore = r133LateExpiredCaptures;
    r133CaptureDidComplete(461, 0x461, YES, r105NowNs());
    const BOOL rejected = r133LateExpiredCaptures == lateBefore + 1 &&
        r132TestIngestCount == 0 && r132TestStageOccupancy() == 0 &&
        r133PendingCount == 0 && r132EstimatorReadyCount == 0 &&
        r127SourceOwnerForSequence(461) == R127SourceOwnerUnknown;
    const BOOL passed = staged && pending && expired && rejected;
    printf("R133_PENDING_CAPTURE_EXPIRY %s ordinal=1 grace=%llu expired=%llu "
           "noCapture=%llu frontier=%llu lateRejected=%llu retroactiveIngestion=%lu\n",
        passed ? "PASS" : "FAIL", (unsigned long long)r133PendingGraceBound,
        (unsigned long long)r133PendingExpired,
        (unsigned long long)r132EstimatorNoCaptureCount,
        (unsigned long long)(r132NextEstimatorOrdinal - 1),
        (unsigned long long)(r133LateExpiredCaptures - lateBefore),
        (unsigned long)r132TestIngestCount);
    return passed;
}

static BOOL r133RunExpiredGapIsNotAdjacent(void) {
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 470);
    id device = [NSObject new];
    R130FakeTexture *textureA = r132MakeTestTexture(device);
    R130FakeTexture *textureC = r132MakeTestTexture(device);
    R130FakeTexture *textureD = r132MakeTestTexture(device);
    const BOOL stagedA = r133StageCommittedForTest(470, (id<MTLTexture>)textureA,
                                                   (id<MTLDevice>)device, 0x470);
    r132PublishPresentation(470, 1, R127SourceOwnerSuppressed);
    // 471 has no image: publishing it expires 470 and is itself NO_CAPTURE.
    r132PublishPresentation(471, 2, R127SourceOwnerDirect);
    const BOOL bothGaps = r133PendingExpired == 1 &&
        r132EstimatorNoCaptureCount == 2;
    const BOOL stagedC = r133StageCommittedForTest(472, (id<MTLTexture>)textureC,
                                                   (id<MTLDevice>)device, 0x472);
    r132PublishPresentation(472, 3, R127SourceOwnerDirect);
    r133CaptureDidComplete(472, 0x472, YES, r105NowNs());
    const BOOL stagedD = r133StageCommittedForTest(473, (id<MTLTexture>)textureD,
                                                   (id<MTLDevice>)device, 0x473);
    r132PublishPresentation(473, 4, R127SourceOwnerDirect);
    r133CaptureDidComplete(473, 0x473, YES, r105NowNs());

    const BOOL passed = stagedA && bothGaps && stagedC && stagedD &&
        r132TestIngestCount == 2 &&
        r132TestIngestSequence[0] == 472 && r132TestPairA[0] == 0 &&
        r132TestIngestSequence[1] == 473 &&
        r132TestPairA[1] == 472 && r132TestPairB[1] == 473 &&
        !(r132TestPairA[1] == 471) && r132EstimatorOrderViolations == 0 &&
        r132TestStageOccupancy() == 0;
    printf("R133_EXPIRED_GAP_NOT_ADJACENT %s ordinals=1:NO_CAPTURE,2:NO_CAPTURE,"
           "3:472,4:473 ingestion=472,473 pair=%llu->%llu falsePair471_to_472=%d "
           "noCapture=%llu\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)r132TestPairA[1],
        (unsigned long long)r132TestPairB[1],
        (r132TestPairA[1] == 471) ? 1 : 0,
        (unsigned long long)r132EstimatorNoCaptureCount);
    return passed;
}

static BOOL r133RunMultiplePendingBounded(void) {
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 480);
    setenv("FGMETAL_R133_PENDING_GRACE", "3", 1);
    r133PendingGraceCached = -1;
    id device = [NSObject new];
    R130FakeTexture *textures[3] = { nil, nil, nil };
    BOOL staged = YES;
    for (NSUInteger i = 0; i < 3; ++i) {
        textures[i] = r132MakeTestTexture(device);
        staged = staged && r133StageCommittedForTest(480 + i,
            (id<MTLTexture>)textures[i], (id<MTLDevice>)device, 0x480 + i);
    }
    r132PublishPresentation(480, 1, R127SourceOwnerDirect);
    r132PublishPresentation(481, 2, R127SourceOwnerDirect);
    r132PublishPresentation(482, 3, R127SourceOwnerDirect);
    const BOOL bounded = r133PendingCount == 3 && r133PendingPeak == 3 &&
        r132TestIngestCount == 0 && r132EstimatorOrderViolations == 0;

    // Out-of-order completion must not reorder ingestion.
    r133CaptureDidComplete(482, 0x482, YES, r105NowNs());
    const BOOL heldForOrder = r132TestIngestCount == 0 && r133PendingCount == 3;
    r133CaptureDidComplete(481, 0x481, YES, r105NowNs());
    const BOOL stillHeld = r132TestIngestCount == 0 && r133PendingCount == 3;
    r133CaptureDidComplete(480, 0x480, YES, r105NowNs());
    const BOOL ordered = r132TestIngestCount == 3 && r133PendingCount == 0 &&
        r132TestIngestOrdinal[0] == 1 && r132TestIngestOrdinal[1] == 2 &&
        r132TestIngestOrdinal[2] == 3 &&
        r132TestIngestSequence[0] == 480 && r132TestIngestSequence[1] == 481 &&
        r132TestIngestSequence[2] == 482 &&
        r132TestPairA[1] == 480 && r132TestPairB[1] == 481 &&
        r132TestPairA[2] == 481 && r132TestPairB[2] == 482 &&
        r132EstimatorOrderViolations == 0 && r132TestStageOccupancy() == 0;
    const BOOL passed = staged && bounded && heldForOrder && stillHeld && ordered;
    printf("R133_MULTIPLE_PENDING_BOUNDED %s grace=%llu pendingPeak=%llu "
           "boundedBy=%d ingestionOrder=%llu,%llu,%llu pairs=%llu->%llu,%llu->%llu "
           "orderViolations=%llu\n",
        passed ? "PASS" : "FAIL", (unsigned long long)r133PendingGraceBound,
        (unsigned long long)r133PendingPeak,
        r133PendingCount <= kR133MaxPendingOrdinals ? 1 : 0,
        (unsigned long long)r132TestIngestSequence[0],
        (unsigned long long)r132TestIngestSequence[1],
        (unsigned long long)r132TestIngestSequence[2],
        (unsigned long long)r132TestPairA[1],
        (unsigned long long)r132TestPairB[1],
        (unsigned long long)r132TestPairA[2],
        (unsigned long long)r132TestPairB[2],
        (unsigned long long)r132EstimatorOrderViolations);
    unsetenv("FGMETAL_R133_PENDING_GRACE");
    r133PendingGraceCached = -1;
    r133PendingGraceBound = kR133DefaultPendingGrace;
    return passed;
}

static BOOL r133RunCommitPrerequisiteMatrix(void) {
    // (1) prerequisite present -> FG commit -> exactly one FG SOURCE WSI.
    const uint64_t fgSequence = 490;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, fgSequence);
    R130FakeDrawable *fgDrawable = r130MakeDrawable(fgSequence);
    r130InstallOriginalIMP(fgDrawable);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(fgSequence, (id<CAMetalDrawable>)fgDrawable);
    R130FakeTexture *fgTexture = r132MakeTestTexture(fgDrawable.texture.device);
    const BOOL fgStaged = r133StageCommittedForTest(fgSequence,
        (id<MTLTexture>)fgTexture, (id<MTLDevice>)fgDrawable.texture.device, 0x490);
    hookDrawablePresent(fgDrawable, @selector(present));
    const BOOL fgCommitted =
        r127SourceOwnerForSequence(fgSequence) == R127SourceOwnerSuppressed &&
        r130TitleDispatchCount[fgSequence] == 0 &&
        [r123Pump activeSourceReservations] == 1;
    r133CaptureDidComplete(fgSequence, 0x490, YES, r105NowNs());
    const BOOL fgEstimatorReady = r132TestIngestCount == 1;
    r130InstallFakePresenter(fgDrawable);
    r130RunPumpPresentation(r105NowNs() + 1000000000ULL);
    const BOOL fgExactlyOnce = r130FGDispatchCount[fgSequence] == 1 &&
        r127SourceOwnerForSequence(fgSequence) == R127SourceOwnerDispatched &&
        [r123Pump activeSourceReservations] == 0;

    // (2) prerequisite absent -> TITLE direct exactly once.
    const uint64_t titleSequence = 491;
    r130BeginTitleAcquisition();
    R130FakeDrawable *titleDrawable = r130MakeDrawable(titleSequence);
    r130InstallOriginalIMP(titleDrawable);
    r130RegisterTitleAcquisition(titleSequence, (id<CAMetalDrawable>)titleDrawable);
    hookDrawablePresent(titleDrawable, @selector(present));
    const BOOL titleOnce =
        r127SourceOwnerForSequence(titleSequence) == R127SourceOwnerTitleDispatched &&
        r130TitleDispatchCount[titleSequence] == 1 &&
        r130FGDispatchCount[titleSequence] == 0 &&
        [r123Pump activeSourceReservations] == 0;

    // (3) prerequisite fails before commit -> TITLE direct exactly once.
    const uint64_t failedSequence = 492;
    r130BeginTitleAcquisition();
    R130FakeDrawable *failedDrawable = r130MakeDrawable(failedSequence);
    r130InstallOriginalIMP(failedDrawable);
    r130RegisterTitleAcquisition(failedSequence, (id<CAMetalDrawable>)failedDrawable);
    R130FakeTexture *failedTexture = r132MakeTestTexture(failedDrawable.texture.device);
    const BOOL failedStaged = r133StageCommittedForTest(failedSequence,
        (id<MTLTexture>)failedTexture, (id<MTLDevice>)failedDrawable.texture.device,
        0x492);
    r133CaptureDidComplete(failedSequence, 0x492, NO, r105NowNs());
    const BOOL prerequisiteWithdrawn =
        !r133CaptureCommittedForSequence(failedSequence);
    hookDrawablePresent(failedDrawable, @selector(present));
    const BOOL failedTitleOnce =
        r127SourceOwnerForSequence(failedSequence) == R127SourceOwnerTitleDispatched &&
        r130TitleDispatchCount[failedSequence] == 1 &&
        r130FGDispatchCount[failedSequence] == 0 &&
        [r123Pump activeSourceReservations] == 0;

    // (4) copy failure after FG commit, before WSI: the R131 retry contract
    // owns the SOURCE, and exactly one FG WSI still results.
    const uint64_t afterCommitSequence = 493;
    r130BeginTitleAcquisition();
    R130FakeDrawable *afterCommitDrawable = r130MakeDrawable(afterCommitSequence);
    r130InstallOriginalIMP(afterCommitDrawable);
    r130RegisterTitleAcquisition(afterCommitSequence,
                                 (id<CAMetalDrawable>)afterCommitDrawable);
    R130FakeTexture *afterCommitTexture =
        r132MakeTestTexture(afterCommitDrawable.texture.device);
    const BOOL afterCommitStaged = r133StageCommittedForTest(afterCommitSequence,
        (id<MTLTexture>)afterCommitTexture,
        (id<MTLDevice>)afterCommitDrawable.texture.device, 0x493);
    hookDrawablePresent(afterCommitDrawable, @selector(present));
    const BOOL afterCommitCommitted =
        r127SourceOwnerForSequence(afterCommitSequence) == R127SourceOwnerSuppressed;
    r133CaptureDidComplete(afterCommitSequence, 0x493, NO, r105NowNs());
    const BOOL estimatorRejected = r132TestIngestCount == 1; // only fgSequence
    r130InstallFakePresenter(afterCommitDrawable);
    r130RunPumpPresentation(r105NowNs() + 1000000000ULL);
    const BOOL exactlyOnceAfterFailure =
        r130FGDispatchCount[afterCommitSequence] == 1 &&
        r130TitleDispatchCount[afterCommitSequence] == 0 &&
        r127SourceOwnerForSequence(afterCommitSequence) == R127SourceOwnerDispatched &&
        [r123Pump activeSourceReservations] == 0;

    // (6) owner is immutable after WSI.
    const BOOL ownerImmutable =
        r127SourceOwnerForSequence(afterCommitSequence) == R127SourceOwnerDispatched &&
        R127SourceOwnershipMarkTitleDispatched(&r127SourceOwnership,
                                               afterCommitSequence) == 0 &&
        r127SourceOwnerForSequence(afterCommitSequence) == R127SourceOwnerDispatched;

    const BOOL passed = fgStaged && fgCommitted && fgEstimatorReady &&
        fgExactlyOnce && titleOnce &&
        failedStaged && prerequisiteWithdrawn && failedTitleOnce &&
        afterCommitStaged && afterCommitCommitted && estimatorRejected &&
        exactlyOnceAfterFailure && ownerImmutable;
    printf("R133_COMMIT_PREREQUISITE_MATRIX %s satisfied=%d fgWsi=%u titleWsi=%lu "
           "absentTitleWsi=%lu failedTitleWsi=%lu failureAfterCommitFgWsi=%u "
           "failureAfterCommitTitleWsi=%lu estimatorIngestion=%lu ownerImmutable=%d "
           "prerequisiteSatisfied=%llu prerequisiteAbsent=%llu\n",
        passed ? "PASS" : "FAIL",
        fgCommitted ? 1 : 0, r130FGDispatchCount[fgSequence],
        (unsigned long)r130TitleDispatchCount[fgSequence],
        (unsigned long)r130TitleDispatchCount[titleSequence],
        (unsigned long)r130TitleDispatchCount[failedSequence],
        r130FGDispatchCount[afterCommitSequence],
        (unsigned long)r130TitleDispatchCount[afterCommitSequence],
        (unsigned long)r132TestIngestCount, ownerImmutable ? 1 : 0,
        (unsigned long long)r133CommitPrerequisiteSatisfied,
        (unsigned long long)r133CommitPrerequisiteAbsent);
    return passed;
}

// Runtime transition coverage uses the production control-file poll and the
// real present hook. The writer is exclusive and refuses to overwrite an
// existing control request, so the harness cannot consume an operator file.
static BOOL r130WriteRuntimeControl(const char *command, uint64_t requestId) {
    struct stat directoryInfo;
    if (lstat(kR127RuntimeDirectory, &directoryInfo) != 0 ||
        !S_ISDIR(directoryInfo.st_mode) || directoryInfo.st_uid != getuid())
        return NO;
    struct stat controlInfo;
    if (lstat(kR127RuntimeControlPath, &controlInfo) == 0 || errno != ENOENT)
        return NO;
    const int fd = open(kR127RuntimeControlPath,
                        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0) return NO;
    char contents[64];
    const int count = snprintf(contents, sizeof(contents), "%s %llu\n", command,
                               (unsigned long long)requestId);
    BOOL ok = count > 0 && (size_t)count < sizeof(contents);
    size_t offset = 0;
    while (ok && offset < (size_t)count) {
        const ssize_t written = write(fd, contents + offset, (size_t)count - offset);
        if (written <= 0) ok = NO;
        else offset += (size_t)written;
    }
    if (close(fd) != 0) ok = NO;
    if (!ok) (void)unlink(kR127RuntimeControlPath);
    return ok;
}

static R130FakeDrawable *r130PrepareRuntimeControlDrawable(uint64_t sequence) {
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130RegisterDrawable(drawable, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    return drawable;
}

typedef struct {
    R130FakeDrawable *drawable;
} R130PresentThreadContext;
static void *r130PresentOnWorkerThread(void *opaque);

static BOOL r130RunRuntimeControlTransitions(void) {
    const uint64_t base = 300;
    r130ConfigureEpoch(R127RuntimeModeOff, 1, base);
    (void)pthread_once(&r125SwitchOnce, r125InitSwitches);
    gR125Pristine = NO;
    gR125CaptureAllowed = YES;
    setenv("FGMETAL_R108_CAPTURE", "1", 1);
    // Keep r123EnsureStarted on its existing-worker branch so this actual
    // control transition test never starts a display worker or touches a real
    // CAMetalLayer.
    r123ThreadStarted = YES;
    r123WorkerReady = YES;
    r127RuntimeControlReady = YES;

    if (!r130WriteRuntimeControl("ON", 7001)) {
        printf("R131_RUNTIME_CONTROL_TRANSITIONS FAIL reason=exclusive-ON-request-unavailable\n");
        return NO;
    }
    R130FakeDrawable *onBoundary = r130PrepareRuntimeControlDrawable(base);
    r130InstallOriginalIMP(onBoundary);
    hookDrawablePresent(onBoundary, @selector(present));
    const BOOL onApplied = r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
        r127RuntimeEpochSnapshot() == 2 && onBoundary.titleDispatches == 1 &&
        r127SourceOwnerForSequence(base) == R127SourceOwnerTitleDispatched;

    // A completion carrying the pre-ON epoch is late even though its source
    // identity has not otherwise been seen by the new generating epoch.
    R130FakeDrawable *stale = r130PrepareRuntimeControlDrawable(base + 1);
    r123EnqueueSource(base + 1, base + 1, (id<MTLTexture>)stale.texture, 1);
    const BOOL staleRejected = r127CachedSourceSequence != base + 1 &&
        r127SourceOwnerForSequence(base + 1) == R127SourceOwnerUnknown;
    hookDrawablePresent(stale, @selector(present));

    // A fresh image from the ON epoch commits through the actual hook. Claim
    // it before OFF to exercise queued/claimed retention while the control-file
    // transition advances the pump into source-only drain.
    R130FakeDrawable *committed = r130PrepareRuntimeControlDrawable(base + 2);
    r123EnqueueSource(base + 2, base + 2,
                      (id<MTLTexture>)committed.texture, 2);
    hookDrawablePresent(committed, @selector(present));
    R123DisplayItem *claimed = nil, *missedInternal = nil;
    pthread_mutex_lock(&r127RuntimeLock);
    (void)[r123Pump takeForSlotAtNs:r105NowNs() + 1000000000ULL
                             outItem:&claimed outMissed:&missedInternal];
    if (claimed) r127PumpItemInFlight = claimed;
    pthread_mutex_unlock(&r127RuntimeLock);
    const BOOL claimedAtBoundary = claimed && claimed.seqB == base + 2 &&
        [r123Pump activeSourceReservations] == 1;

    R130FakeDrawable *queued = r130PrepareRuntimeControlDrawable(base + 3);
    r123EnqueueSource(base + 3, base + 3,
                      (id<MTLTexture>)queued.texture, 2);
    hookDrawablePresent(queued, @selector(present));
    const BOOL queuedAtBoundary =
        r127SourceOwnerForSequence(base + 3) == R127SourceOwnerSuppressed &&
        [r123Pump activeSourceReservations] == 2;

    R130FakeDrawable *offBoundary = r130PrepareRuntimeControlDrawable(base + 4);
    if (!r130WriteRuntimeControl("OFF", 7002)) {
        printf("R131_RUNTIME_CONTROL_TRANSITIONS FAIL reason=exclusive-OFF-request-unavailable\n");
        return NO;
    }
    r127PollRuntimeRequests(base + 4);
    r123EnqueueSource(base + 4, base + 4,
                      (id<MTLTexture>)offBoundary.texture, 2);
    const BOOL captureDuringOffRejected =
        r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain &&
        r127CachedSourceSequence != base + 4;
    R130PresentThreadContext context = { .drawable = offBoundary };
    pthread_t titleThread;
    if (pthread_create(&titleThread, NULL, r130PresentOnWorkerThread, &context) != 0)
        return NO;
    const uint64_t drainDeadlineNs = r105NowNs() + 1000000000ULL;
    while (r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
           r105NowNs() < drainDeadlineNs) {
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 1000000 };
        nanosleep(&pause, NULL);
    }
    const BOOL drainApplied =
        r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain &&
        [r123Pump activeSourceReservations] == 2;
    r130InstallFakePresenter(committed);
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);
    r130InstallFakePresenter(queued);
    r130RunPumpPresentation(r105NowNs() + 2100000000ULL);
    pthread_join(titleThread, NULL);
    const BOOL offOrdered = r130FGDispatchCount[base + 2] == 1 &&
        r130FGDispatchCount[base + 3] == 1 &&
        offBoundary.titleDispatches == 1 &&
        R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, base + 2) == 3 &&
        R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, base + 3) == 4 &&
        R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, base + 4) == 5 &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0;

    R130FakeDrawable *drainCompletion = r130PrepareRuntimeControlDrawable(base + 5);
    hookDrawablePresent(drainCompletion, @selector(present));
    const BOOL offCompleted = r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
        drainCompletion.titleDispatches == 1;

    BOOL repeatedTransitions = YES;
    uint64_t requestId = 7010;
    for (uint64_t cycle = 0; cycle < 2; ++cycle) {
        R130FakeDrawable *on = r130PrepareRuntimeControlDrawable(base + 6 + cycle * 2);
        if (!r130WriteRuntimeControl("ON", requestId++) ) {
            repeatedTransitions = NO;
            break;
        }
        hookDrawablePresent(on, @selector(present));
        R130FakeDrawable *off = r130PrepareRuntimeControlDrawable(base + 7 + cycle * 2);
        if (!r130WriteRuntimeControl("OFF", requestId++)) {
            repeatedTransitions = NO;
            break;
        }
        hookDrawablePresent(off, @selector(present));
        repeatedTransitions &= r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
            on.titleDispatches == 1 && off.titleDispatches == 1;
    }

    // The final OFF is requested only after FG WSI dispatch. The fake drawable
    // deliberately never fires addPresentedHandler, so physical display is
    // still unconfirmed when the real control poll runs.
    R130FakeDrawable *unconfirmedOn =
        r130PrepareRuntimeControlDrawable(base + 10);
    BOOL offAfterUnconfirmedWsi = r130WriteRuntimeControl("ON", requestId++);
    if (offAfterUnconfirmedWsi) hookDrawablePresent(unconfirmedOn, @selector(present));
    const uint64_t unconfirmedEpoch = r127RuntimeEpochSnapshot();
    R130FakeDrawable *unconfirmedFg =
        r130PrepareRuntimeControlDrawable(base + 11);
    r123EnqueueSource(base + 11, base + 11,
                      (id<MTLTexture>)unconfirmedFg.texture, unconfirmedEpoch);
    hookDrawablePresent(unconfirmedFg, @selector(present));
    const BOOL unconfirmedFgCommitted =
        r127SourceOwnerForSequence(base + 11) == R127SourceOwnerSuppressed;
    r130InstallFakePresenter(unconfirmedFg);
    r130RunPumpPresentation(r105NowNs() + 3000000000ULL);
    const BOOL noPhysicalCallback = r130FGDispatchCount[base + 11] == 1;
    offAfterUnconfirmedWsi &= r130WriteRuntimeControl("OFF", requestId++);
    if (offAfterUnconfirmedWsi) {
        r127PollRuntimeRequests(base + 12);
        r127TryCompleteDisable(base + 12);
    }
    offAfterUnconfirmedWsi &= unconfirmedFgCommitted && noPhysicalCallback &&
        r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
        r127SourceOwnerForSequence(base + 11) == R127SourceOwnerDispatched &&
        r130FGDispatchCount[base + 11] == 1 &&
        r130TitleDispatchCount[base + 11] == 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0;

    const BOOL passed = onApplied && staleRejected && claimedAtBoundary &&
        captureDuringOffRejected &&
        drainApplied && offOrdered && offCompleted && repeatedTransitions &&
        offAfterUnconfirmedWsi &&
        r130SourcePresentOrderViolations == 0 &&
        r130LastSourcePresentOrdinalDispatched == 12 &&
        r127SourceOwnership.overwriteNonTerminal == 0;
    printf("R131_RUNTIME_CONTROL_TRANSITIONS %s controlPoll=actual onEpoch=2 "
           "staleCaptureRejected=%d offClaimedPreserved=%d repeatedOnOff=%d "
           "committedOwner=%u committedFgWsi=%u committedOrdinal=%llu "
           "offTitleWsi=%u offOrdinal=%llu captureDuringOffRejected=%d queuedAtOff=%d "
           "offAfterUnconfirmedWsi=%d sourceOrdinals=%llu orderViolations=%llu "
           "pending=%llu reservations=%lu\n",
        passed ? "PASS" : "FAIL", staleRejected ? 1 : 0,
        offOrdered ? 1 : 0, repeatedTransitions ? 1 : 0,
        (unsigned)r127SourceOwnerForSequence(base + 2),
        r130FGDispatchCount[base + 2],
        (unsigned long long)R127SourceOwnershipPresentOrdinal(
            &r127SourceOwnership, base + 2),
        r130TitleDispatchCount[base + 4],
        (unsigned long long)R127SourceOwnershipPresentOrdinal(
            &r127SourceOwnership, base + 4),
        captureDuringOffRejected ? 1 : 0,
        queuedAtBoundary ? 1 : 0,
        offAfterUnconfirmedWsi ? 1 : 0,
        (unsigned long long)r130LastSourcePresentOrdinalDispatched,
        (unsigned long long)r130SourcePresentOrderViolations,
        (unsigned long long)r130PendingFGDispatches,
        (unsigned long)[r123Pump activeSourceReservations]);
    return passed;
}

static uint64_t r130OwnershipSlotRefusals(void) {
    pthread_mutex_lock(&r127SourceOwnershipLock);
    const uint64_t refusals = r127SourceOwnership.slotRefusals;
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    return refusals;
}

static BOOL r130RunUnresolvedOwnershipCollision(void) {
    const uint64_t oldSequence = 1;
    const uint64_t newSequence = oldSequence + R127_SOURCE_OWNERSHIP_CAPACITY;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, oldSequence);
    R130FakeDrawable *oldSource = r130MakeDrawable(oldSequence);
    r130InstallOriginalIMP(oldSource);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(oldSequence, (id<CAMetalDrawable>)oldSource);
    r123EnqueueSource(oldSequence, oldSequence,
                      (id<MTLTexture>)oldSource.texture, 1);
    hookDrawablePresent(oldSource, @selector(present));
    const BOOL oldCommitted =
        r127SourceOwnerForSequence(oldSequence) == R127SourceOwnerSuppressed &&
        [r123Pump activeSourceReservations] == 1;

    R130FakeDrawable *newSource = r130MakeDrawable(newSequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(newSequence, (id<CAMetalDrawable>)newSource);
    r123EnqueueSource(newSequence, newSequence,
                      (id<MTLTexture>)newSource.texture, 1);
    const BOOL newCaptureReady = r127CachedSourceSequence == newSequence;
    R130PresentThreadContext context = { .drawable = newSource };
    pthread_t titleThread;
    if (pthread_create(&titleThread, NULL, r130PresentOnWorkerThread, &context) != 0)
        return NO;
    const uint64_t collisionDeadlineNs = r105NowNs() + 1000000000ULL;
    while (r130OwnershipSlotRefusals() == 0 && r105NowNs() < collisionDeadlineNs) {
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 1000000 };
        nanosleep(&pause, NULL);
    }
    const BOOL fgRefusedAndSourceProtected = r130OwnershipSlotRefusals() > 0 &&
        [r123Pump activeSourceReservations] == 1 &&
        r127SourceOwnerForSequence(oldSequence) == R127SourceOwnerSuppressed &&
        r130FGDispatchCount[newSequence] == 0;
    r130InstallFakePresenter(oldSource);
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);
    pthread_join(titleThread, NULL);
    hookDrawablePresent(newSource, @selector(present));
    const uint64_t staleBefore = r127StaleCallbackRejections;
    r123EnqueueSource(oldSequence, oldSequence,
                      (id<MTLTexture>)oldSource.texture, 1);
    const BOOL staleRejected = r127CachedSourceSequence != oldSequence &&
        r127StaleCallbackRejections == staleBefore + 1;
    const BOOL passed = oldCommitted && newCaptureReady && fgRefusedAndSourceProtected &&
        r127SourceOwnerForSequence(oldSequence) == R127SourceOwnerUnknown &&
        r127SourceOwnerForSequence(newSequence) == R127SourceOwnerTitleDispatched &&
        r130FGDispatchCount[oldSequence] == 1 &&
        r130TitleDispatchCount[newSequence] == 1 &&
        r130DispatchOrderCount == 2 && r130DispatchOrder[0] == oldSequence &&
        r130DispatchOrder[1] == newSequence && staleRejected &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0 && r130SourcePresentOrderViolations == 0 &&
        r127SourceOwnership.overwriteNonTerminal == 0;
    printf("R131_UNRESOLVED_LEDGER_COLLISION %s oldAcquisition=%llu newAcquisition=%llu "
           "fgRefusedBeforeSuppression=%d oldSourceRetained=%d oldFgWsi=%u "
           "newTitleWsi=%u staleCallbackRejected=%d slotRefusals=%llu "
           "overwriteNonTerminal=%llu orderViolations=%llu\n",
        passed ? "PASS" : "FAIL", (unsigned long long)oldSequence,
        (unsigned long long)newSequence, fgRefusedAndSourceProtected ? 1 : 0,
        oldCommitted ? 1 : 0, r130FGDispatchCount[oldSequence],
        r130TitleDispatchCount[newSequence], staleRejected ? 1 : 0,
        (unsigned long long)r130OwnershipSlotRefusals(),
        (unsigned long long)r127SourceOwnership.overwriteNonTerminal,
        (unsigned long long)r130SourcePresentOrderViolations);
    return passed;
}

static BOOL r130RunOwnershipLedgerLifetime(void) {
    enum { sourceCount = 300 };
    r130ConfigureEpoch(R127RuntimeModeOff, 1, 1);
    R130FakeDrawable *first = nil;
    BOOL passed = YES;
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        if (sequence == 1) first = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
        hookDrawablePresent(drawable, @selector(present));
        if (r130TitleDispatchCount[sequence] != 1 ||
            r130FGDispatchCount[sequence] != 0 ||
            r127SourceOwnerForSequence(sequence) != R127SourceOwnerTitleDispatched ||
            R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, sequence) != sequence)
            passed = NO;
    }
    const uint64_t staleBefore = r127StaleCallbackRejections;
    r123EnqueueSource(1, 1, (id<MTLTexture>)first.texture, 1);
    const BOOL staleAfterReuseRejected = r127CachedSourceSequence != 1 &&
        r127StaleCallbackRejections == staleBefore + 1;
    const BOOL allDispatched = r130DispatchOrderCount == sourceCount &&
        r130LastSourcePresentOrdinalDispatched == sourceCount &&
        r130SourcePresentOrderViolations == 0 &&
        r127SourceOwnership.overwriteNonTerminal == 0 &&
        r130TitleAcquisitionsOutstanding == 0 && !r130AcquireAccountingAmbiguous;
    passed &= staleAfterReuseRejected && allDispatched;
    printf("R131_OWNERSHIP_LEDGER_LIFETIME %s logical=%d capacity=%u "
           "terminalSlotReuse=PASS staleCallbackRejected=%d "
           "lastOrdinal=%llu orderViolations=%llu overwriteNonTerminal=%llu "
           "outstandingTickets=%llu\n",
        passed ? "PASS" : "FAIL", sourceCount,
        R127_SOURCE_OWNERSHIP_CAPACITY, staleAfterReuseRejected ? 1 : 0,
        (unsigned long long)r130LastSourcePresentOrdinalDispatched,
        (unsigned long long)r130SourcePresentOrderViolations,
        (unsigned long long)r127SourceOwnership.overwriteNonTerminal,
        (unsigned long long)r130TitleAcquisitionsOutstanding);
    return passed;
}

static void *r130PresentOnWorkerThread(void *opaque) {
    R130PresentThreadContext *context = opaque;
    @autoreleasepool {
        hookDrawablePresent(context->drawable, @selector(present));
    }
    return NULL;
}

typedef struct {
    uint64_t sequence;
} R130PollThreadContext;

static void *r130PollRuntimeOnWorkerThread(void *opaque) {
    R130PollThreadContext *context = opaque;
    @autoreleasepool {
        r127PollRuntimeRequests(context->sequence);
    }
    return NULL;
}

static BOOL r130RunLookupTransitionRace(void) {
    const uint64_t sequence = 200;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    r123ThreadStarted = YES;
    r123WorkerReady = YES;
    r127RuntimeControlReady = YES;
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    r123EnqueueSource(sequence, sequence, (id<MTLTexture>)drawable.texture, 1);

    // Hold only the ownership lookup lock. The title hook acquires the runtime
    // lock and blocks at its owner lookup; an OFF control poll then claims the
    // real command and waits for that runtime lock. Releasing the lookup gives
    // the present decision a defined linearization point before OFF.
    pthread_mutex_lock(&r127SourceOwnershipLock);
    R130PresentThreadContext presentContext = { .drawable = drawable };
    pthread_t presentThread;
    if (pthread_create(&presentThread, NULL, r130PresentOnWorkerThread,
                       &presentContext) != 0) {
        pthread_mutex_unlock(&r127SourceOwnershipLock);
        return NO;
    }
    const uint64_t lockDeadlineNs = r105NowNs() + 1000000000ULL;
    BOOL presentOwnsRuntimeLock = NO;
    while (r105NowNs() < lockDeadlineNs) {
        const int result = pthread_mutex_trylock(&r127RuntimeLock);
        if (result == EBUSY) {
            presentOwnsRuntimeLock = YES;
            break;
        }
        if (result == 0) pthread_mutex_unlock(&r127RuntimeLock);
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 1000000 };
        nanosleep(&pause, NULL);
    }
    if (!presentOwnsRuntimeLock || !r130WriteRuntimeControl("OFF", 8001)) {
        pthread_mutex_unlock(&r127SourceOwnershipLock);
        pthread_join(presentThread, NULL);
        printf("R131_LOOKUP_TRANSITION_RACE FAIL reason=deterministic-lock-window-unavailable\n");
        return NO;
    }
    R130PollThreadContext pollContext = { .sequence = sequence };
    pthread_t pollThread;
    if (pthread_create(&pollThread, NULL, r130PollRuntimeOnWorkerThread,
                       &pollContext) != 0) {
        pthread_mutex_unlock(&r127SourceOwnershipLock);
        pthread_join(presentThread, NULL);
        (void)unlink(kR127RuntimeControlPath);
        return NO;
    }
    const uint64_t pollDeadlineNs = r105NowNs() + 1000000000ULL;
    BOOL pollClaimedCommand = NO;
    while (r105NowNs() < pollDeadlineNs) {
        struct stat info;
        if (lstat(kR127RuntimeControlPath, &info) != 0 && errno == ENOENT) {
            pollClaimedCommand = YES;
            break;
        }
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 1000000 };
        nanosleep(&pause, NULL);
    }
    const BOOL transitionWaitedForDecision = pollClaimedCommand &&
        r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
        R127SourceOwnershipRead(&r127SourceOwnership, sequence) ==
            R127SourceOwnerUnknown;
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    pthread_join(presentThread, NULL);
    pthread_join(pollThread, NULL);

    const BOOL decisionCommittedBeforeOff = transitionWaitedForDecision &&
        r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed &&
        r130TitleDispatchCount[sequence] == 0 &&
        [r123Pump activeSourceReservations] == 1 &&
        r130PendingFGDispatches == 1;
    r130InstallFakePresenter(drawable);
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);
    r127TryCompleteDisable(sequence + 1);
    const BOOL passed = decisionCommittedBeforeOff &&
        r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerDispatched &&
        r130FGDispatchCount[sequence] == 1 &&
        r130TitleDispatchCount[sequence] == 0 &&
        R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, sequence) == 1 &&
        r130LastSourcePresentOrdinalDispatched == 1 &&
        r130SourcePresentOrderViolations == 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0 &&
        r127SourceOwnership.overwriteNonTerminal == 0;
    printf("R131_LOOKUP_TRANSITION_RACE %s actualHook=1 actualControlPoll=1 "
           "presentDecisionLinearizedFirst=%d ownerAfterDrain=%u titleWsi=%u "
           "fgWsi=%u ordinal=%llu orderViolations=%llu reservations=%lu "
           "droppedSource=%llu\n",
        passed ? "PASS" : "FAIL", transitionWaitedForDecision ? 1 : 0,
        (unsigned)r127SourceOwnerForSequence(sequence),
        r130TitleDispatchCount[sequence], r130FGDispatchCount[sequence],
        (unsigned long long)R127SourceOwnershipPresentOrdinal(
            &r127SourceOwnership, sequence),
        (unsigned long long)r130SourcePresentOrderViolations,
        (unsigned long)[r123Pump activeSourceReservations],
        (unsigned long long)[r123Pump droppedSource]);
    return passed;
}

static BOOL r130RunSourceCapacityPressure(void) {
    enum { sourceCount = 9, idBase = 201 };
    R130FakeDrawable *__strong drawables[sourceCount] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, idBase);
    r130InstallOriginalIMP([R130FakeDrawable new]);
    R130FakeLayer *sharedLayer = [R130FakeLayer new];
    id sharedDevice = [NSObject new];
    sharedLayer.device = sharedDevice;
    selectedHarnessLayer = (__bridge void *)sharedLayer;

    uint64_t peakReservations = 0;
    for (uint64_t index = 0; index < sourceCount; ++index) {
        const uint64_t acquisitionId = idBase + index * 2;
        R130FakeDrawable *drawable = r130MakeDrawable(acquisitionId);
        drawable.layer = sharedLayer;
        drawable.texture.device = sharedDevice;
        selectedHarnessLayer = (__bridge void *)sharedLayer;
        r130RegisterDrawable(drawable, acquisitionId);
        drawables[index] = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(acquisitionId, (id<CAMetalDrawable>)drawable);
        r123EnqueueSource(acquisitionId, acquisitionId,
                          (id<MTLTexture>)drawable.texture, 1);

        if (index < kR123MaxDepth) {
            hookDrawablePresent(drawable, @selector(present));
            if (r127SourceOwnerForSequence(acquisitionId) !=
                    R127SourceOwnerSuppressed ||
                [r123Pump activeSourceReservations] != index + 1) {
                fprintf(stderr, "R131_PRESSURE_FAIL stage=commit index=%llu owner=%u reservations=%lu\n",
                    (unsigned long long)index,
                    (unsigned)r127SourceOwnerForSequence(acquisitionId),
                    (unsigned long)[r123Pump activeSourceReservations]);
                return NO;
            }
            if ([r123Pump activeSourceReservations] > peakReservations)
                peakReservations = [r123Pump activeSourceReservations];
        } else {
            const BOOL suppressed = r123TrySuppressSourcePresent(
                (id<CAMetalDrawable>)drawable);
            if (suppressed || r127SourceOwnerForSequence(acquisitionId) !=
                    R127SourceOwnerDirect ||
                [r123Pump activeSourceReservations] != kR123MaxDepth) return NO;
        }
    }

    R123DisplayItem *claimedSource = nil, *missedInternal = nil;
    pthread_mutex_lock(&r127RuntimeLock);
    (void)[r123Pump takeForSlotAtNs:r105NowNs() + 1000000000ULL
                             outItem:&claimedSource outMissed:&missedInternal];
    if (claimedSource) r127PumpItemInFlight = claimedSource;
    pthread_mutex_unlock(&r127RuntimeLock);
    const BOOL queuedAndClaimedAtCapacity = claimedSource &&
        claimedSource.seqB == idBase &&
        [r123Pump activeSourceReservations] == kR123MaxDepth &&
        [r123Pump depth] == kR123MaxDepth - 1;

    const uint64_t internalEvictionsBeforeDispatch = [r123Pump droppedInternal];
    const BOOL refusedBeforeSuppression =
        r127SourceOwnerForSequence(idBase + (sourceCount - 1) * 2) ==
            R127SourceOwnerDirect &&
        r130TitleDispatchCount[idBase + (sourceCount - 1) * 2] == 0 &&
        r130FGDispatchCount[idBase + (sourceCount - 1) * 2] == 0 &&
        [r123Pump activeSourceReservations] == kR123MaxDepth;

    R130PresentThreadContext context = { .drawable = drawables[sourceCount - 1] };
    pthread_t titleThread;
    if (pthread_create(&titleThread, NULL, r130PresentOnWorkerThread, &context) != 0)
        return NO;

    uint64_t pumpTickNs = r105NowNs() + 2000000000ULL;
    r130InstallFakePresenter(drawables[0]);
    if (claimedSource) r123PresentItem(claimedSource, pumpTickNs);
    pumpTickNs += 100000000ULL;
    for (uint64_t index = 1; index < kR123MaxDepth; ++index) {
        r130InstallFakePresenter(drawables[index]);
        r130RunPumpPresentation(pumpTickNs);
        pumpTickNs += 100000000ULL;
    }
    pthread_join(titleThread, NULL);

    uint64_t totalDispatches = 0;
    uint64_t duplicates = 0;
    uint64_t zeroDispatch = 0;
    BOOL ordered = r130DispatchOrderCount == sourceCount;
    for (uint64_t index = 0; index < sourceCount; ++index) {
        const uint64_t acquisitionId = idBase + index * 2;
        const uint64_t title = r130TitleDispatchCount[acquisitionId];
        const uint64_t fg = r130FGDispatchCount[acquisitionId];
        const uint64_t count = title + fg;
        totalDispatches += count;
        if (count > 1) duplicates += count - 1;
        if (count == 0) ++zeroDispatch;
        if (count != 1) ordered = NO;
        if (index < kR123MaxDepth && fg != 1) ordered = NO;
        if (index == kR123MaxDepth && title != 1) ordered = NO;
        if (index < r130DispatchOrderCount &&
            r130DispatchOrder[index] != acquisitionId) ordered = NO;
        if (R127SourceOwnershipPresentOrdinal(&r127SourceOwnership, acquisitionId) !=
            index + 1) ordered = NO;
    }
    const BOOL passed = queuedAndClaimedAtCapacity && refusedBeforeSuppression && ordered &&
        peakReservations == kR123MaxDepth &&
        [r123Pump sourceCapacity] == kR123MaxDepth &&
        internalEvictionsBeforeDispatch > 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0 && r130PendingFGDispatches == 0 &&
        totalDispatches == sourceCount && duplicates == 0 && zeroDispatch == 0 &&
        r130SourcePresentOrderViolations == 0 &&
        r127SourceOwnership.overwriteNonTerminal == 0 &&
        r132EstimatorStagePeak <= kR132EstimatorStageCapacity;
    printf("R131_SOURCE_CAPACITY_PRESSURE %s logical=%d peakReservations=%llu "
           "capacity=%lu internalEvictions=%llu refusedBeforeSuppression=%d "
           "queuedAndClaimed=%d refusedOwner=TITLE totalWsi=%llu duplicates=%llu zeroDispatch=%llu "
           "committedDropped=%llu orderViolations=%llu estimatorStagePeak=%llu/%u\n",
        passed ? "PASS" : "FAIL", sourceCount,
        (unsigned long long)peakReservations,
        (unsigned long)[r123Pump sourceCapacity],
        (unsigned long long)internalEvictionsBeforeDispatch,
        refusedBeforeSuppression ? 1 : 0,
        queuedAndClaimedAtCapacity ? 1 : 0,
        (unsigned long long)totalDispatches,
        (unsigned long long)duplicates, (unsigned long long)zeroDispatch,
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)r130SourcePresentOrderViolations,
        (unsigned long long)r132EstimatorStagePeak,
        kR132EstimatorStageCapacity);
    return passed;
}

static R130FakeDrawable *r130PreparePressureDrawable(uint64_t acquisitionId,
                                                     R130FakeLayer *layer,
                                                     id device) {
    R130FakeDrawable *drawable = r130MakeDrawable(acquisitionId);
    drawable.layer = layer;
    drawable.texture.device = device;
    selectedHarnessLayer = (__bridge void *)layer;
    r130RegisterDrawable(drawable, acquisitionId);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(acquisitionId, (id<CAMetalDrawable>)drawable);
    r123EnqueueSource(acquisitionId, acquisitionId,
                      (id<MTLTexture>)drawable.texture, 1);
    return drawable;
}

static BOOL r130Run80SourceCapacityPressure(void) {
    enum { sourceCount = 80, fullBatches = 8 };
    R130FakeDrawable *__strong drawables[sourceCount + 1] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 1);
    r130InstallOriginalIMP([R130FakeDrawable new]);
    R130FakeLayer *sharedLayer = [R130FakeLayer new];
    id sharedDevice = [NSObject new];
    sharedLayer.device = sharedDevice;
    selectedHarnessLayer = (__bridge void *)sharedLayer;

    uint64_t nextAcquisitionId = 1;
    uint64_t peakReservations = 0;
    uint64_t capacityRefusals = 0;
    uint64_t internalEvictionBatches = 0;
    uint64_t tickNs = r105NowNs() + 1000000000ULL;
    BOOL scheduleOk = YES;

    for (uint64_t batch = 0; batch < fullBatches; ++batch) {
        const uint64_t internalDropsAtBatchStart = [r123Pump droppedInternal];
        for (uint64_t slot = 0; slot < kR123MaxDepth; ++slot) {
            const uint64_t acquisitionId = nextAcquisitionId++;
            R130FakeDrawable *drawable = r130PreparePressureDrawable(
                acquisitionId, sharedLayer, sharedDevice);
            drawables[acquisitionId] = drawable;
            hookDrawablePresent(drawable, @selector(present));
            const NSUInteger expectedReservations = (NSUInteger)slot + 1;
            if (r127SourceOwnerForSequence(acquisitionId) !=
                    R127SourceOwnerSuppressed ||
                [r123Pump activeSourceReservations] != expectedReservations) {
                scheduleOk = NO;
                break;
            }
            if ([r123Pump activeSourceReservations] > peakReservations)
                peakReservations = [r123Pump activeSourceReservations];
        }
        if (!scheduleOk) break;
        if ([r123Pump droppedInternal] > internalDropsAtBatchStart)
            ++internalEvictionBatches;

        const uint64_t refusedId = nextAcquisitionId++;
        R130FakeDrawable *refused = r130PreparePressureDrawable(
            refusedId, sharedLayer, sharedDevice);
        drawables[refusedId] = refused;
        const BOOL suppressed = r123TrySuppressSourcePresent(
            (id<CAMetalDrawable>)refused);
        const BOOL refusedBeforeSuppression = !suppressed &&
            r127SourceOwnerForSequence(refusedId) == R127SourceOwnerDirect &&
            [r123Pump activeSourceReservations] == kR123MaxDepth &&
            r130TitleDispatchCount[refusedId] == 0 &&
            r130FGDispatchCount[refusedId] == 0;
        if (!refusedBeforeSuppression) {
            scheduleOk = NO;
            break;
        }
        ++capacityRefusals;

        R130PresentThreadContext context = { .drawable = refused };
        pthread_t titleThread;
        if (pthread_create(&titleThread, NULL,
                           r130PresentOnWorkerThread, &context) != 0) {
            scheduleOk = NO;
            break;
        }
        const uint64_t firstInBatch = refusedId - kR123MaxDepth;
        for (uint64_t index = firstInBatch; index < refusedId; ++index) {
            r130InstallFakePresenter(drawables[index]);
            r130RunPumpPresentation(tickNs);
            tickNs += 100000000ULL;
        }
        pthread_join(titleThread, NULL);
    }

    // Eight more ready SOURCE frames finish the 80-frame accounting after the
    // eight full-capacity/refusal cycles.
    if (scheduleOk) {
        for (uint64_t slot = 0; slot < kR123MaxDepth; ++slot) {
            const uint64_t acquisitionId = nextAcquisitionId++;
            R130FakeDrawable *drawable = r130PreparePressureDrawable(
                acquisitionId, sharedLayer, sharedDevice);
            drawables[acquisitionId] = drawable;
            hookDrawablePresent(drawable, @selector(present));
            if (r127SourceOwnerForSequence(acquisitionId) !=
                    R127SourceOwnerSuppressed) {
                scheduleOk = NO;
                break;
            }
            if ([r123Pump activeSourceReservations] > peakReservations)
                peakReservations = [r123Pump activeSourceReservations];
        }
        for (uint64_t index = nextAcquisitionId - kR123MaxDepth;
             scheduleOk && index < nextAcquisitionId; ++index) {
            r130InstallFakePresenter(drawables[index]);
            r130RunPumpPresentation(tickNs);
            tickNs += 100000000ULL;
        }
    }

    uint64_t titleWsi = 0, fgWsi = 0, duplicates = 0, zeroDispatch = 0;
    BOOL ordered = scheduleOk && nextAcquisitionId == sourceCount + 1 &&
        r130DispatchOrderCount == sourceCount;
    for (uint64_t acquisitionId = 1; acquisitionId <= sourceCount;
        ++acquisitionId) {
        const uint64_t titleCount = r130TitleDispatchCount[acquisitionId];
        const uint64_t fgCount = r130FGDispatchCount[acquisitionId];
        const uint64_t total = titleCount + fgCount;
        const uint64_t ordinal = R127SourceOwnershipPresentOrdinal(
            &r127SourceOwnership, acquisitionId);
        const R127SourceOwner owner = r127SourceOwnerForSequence(acquisitionId);
        titleWsi += titleCount;
        fgWsi += fgCount;
        if (total > 1) duplicates += total - 1;
        if (total == 0) ++zeroDispatch;
        if (total != 1 || titleCount + fgCount != 1 ||
            ordinal != acquisitionId || (titleCount && owner != R127SourceOwnerTitleDispatched) ||
            (fgCount && owner != R127SourceOwnerDispatched) ||
            r130DispatchOrder[acquisitionId - 1] != acquisitionId)
            ordered = NO;
        printf("R131_80_SOURCE_RECORD AcquisitionId=%llu SourcePresentOrdinal=%llu "
               "owner=%s titleWsi=%llu fgWsi=%llu totalWsi=%llu\n",
            (unsigned long long)acquisitionId,
            (unsigned long long)ordinal,
            titleCount ? "TITLE" : fgCount ? "FG" : "UNRESOLVED",
            (unsigned long long)titleCount, (unsigned long long)fgCount,
            (unsigned long long)total);
    }

    const BOOL passed = ordered && peakReservations == kR123MaxDepth &&
        [r123Pump sourceCapacity] == kR123MaxDepth && capacityRefusals == 8 &&
        internalEvictionBatches == fullBatches &&
        titleWsi == 8 && fgWsi == 72 && titleWsi + fgWsi == sourceCount &&
        duplicates == 0 && zeroDispatch == 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        [r123Pump droppedSource] == 0 && r130PendingFGDispatches == 0 &&
        r130SourcePresentOrderViolations == 0 &&
        r127SourceOwnership.overwriteNonTerminal == 0 &&
        r132EstimatorStagePeak <= kR132EstimatorStageCapacity;
    printf("R131_80_SOURCE_CAPACITY_PRESSURE %s logical=%d titleWsi=%llu "
           "fgWsi=%llu capacityRefusals=%llu peakReservations=%llu capacity=%lu "
           "internalEvictionBatches=%llu totalWsi=%llu duplicates=%llu "
           "zeroDispatch=%llu committedDropped=%llu orderViolations=%llu "
           "overwriteNonTerminal=%llu estimatorStagePeak=%llu/%u\n",
        passed ? "PASS" : "FAIL", sourceCount,
        (unsigned long long)titleWsi, (unsigned long long)fgWsi,
        (unsigned long long)capacityRefusals,
        (unsigned long long)peakReservations,
        (unsigned long)[r123Pump sourceCapacity],
        (unsigned long long)internalEvictionBatches,
        (unsigned long long)(titleWsi + fgWsi),
        (unsigned long long)duplicates, (unsigned long long)zeroDispatch,
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)r130SourcePresentOrderViolations,
        (unsigned long long)r127SourceOwnership.overwriteNonTerminal,
        (unsigned long long)r132EstimatorStagePeak,
        kR132EstimatorStageCapacity);
    return passed;
}

static int r130RunPresentBeforeCapture(void) {
    // The production constructor has already selected (or rejected) this
    // process once. Pin it to the test fixture after that one-time probe.
    (void)pthread_once(&targetOnce, r130NoopOnce);
    selectedHarnessProcess = YES;

    const BOOL directControl = r130RunCase("runtime-off-title-direct",
        R127RuntimeModeOff, 1, NO, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL captureRace = r130RunCase("present-before-capture",
        R127RuntimeModeGenerating, 2, NO, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL readyBeforePresent = r130RunCase("capture-before-present-commits",
        R127RuntimeModeGenerating, 3, YES, NO, NO, R130FaultNone, YES, NO, NO);
    const BOOL lateCapture = r130RunCase("late-capture-after-title-terminal",
        R127RuntimeModeGenerating, 4, NO, YES, YES, R130FaultNone, YES, NO, NO);
    const BOOL acquireRetry = r130RunCase("drawable-acquire-failure-retry",
        R127RuntimeModeGenerating, 5, YES, NO, NO, R130FaultDrawableAcquire, YES, NO, NO);
    const BOOL copyRetry = r130RunCase("presentation-copy-failure-retry",
        R127RuntimeModeGenerating, 6, YES, NO, NO, R130FaultPresentationCopy, YES, NO, NO);
    const BOOL preWsiRetry = r130RunCase("pre-wsi-abort-retry",
        R127RuntimeModeGenerating, 7, YES, NO, NO, R130FaultPreWsiAbort, YES, NO, NO);
    const BOOL terminalCapture = r130RunCase("late-capture-after-fg-terminal",
        R127RuntimeModeGenerating, 8, YES, NO, NO, R130FaultNone, YES, YES, NO);
    const BOOL offWithoutAck = r130RunCase("off-after-wsi-before-display-callback",
        R127RuntimeModeGenerating, 9, YES, NO, NO, R130FaultNone, NO, NO, YES);
    const BOOL preflightNoImage = r130RunCase(
        "preflight-failure-equivalent-no-ready-image",
        R127RuntimeModeGenerating, 10, NO, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL allocationNoImage = r130RunCase(
        "allocation-failure-equivalent-no-ready-image",
        R127RuntimeModeGenerating, 11, NO, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL copyNoImage = r130RunCase(
        "capture-copy-failure-equivalent-no-ready-image",
        R127RuntimeModeGenerating, 12, NO, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL captureDuringOff = r130RunCase("capture-ready-during-OFF",
        R127RuntimeModeOff, 13, YES, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL captureDuringDrain = r130RunCase("capture-ready-during-OFF-drain",
        R127RuntimeModeSourceOnlyDrain, 14, YES, NO, YES, R130FaultNone, YES, NO, NO);
    const BOOL reversePresentationOrder =
        r130RunReverseAcquisitionPresentationOrder();
    const BOOL sourceCapacityPressure = r130RunSourceCapacityPressure();
        const BOOL endToEndPressure = r130Run80SourceCapacityPressure();
    return directControl && captureRace && readyBeforePresent && lateCapture &&
        acquireRetry && copyRetry && preWsiRetry && terminalCapture &&
        offWithoutAck && preflightNoImage && allocationNoImage && copyNoImage &&
        captureDuringOff && captureDuringDrain && reversePresentationOrder &&
        sourceCapacityPressure && endToEndPressure ? 0 : 1;
}

static BOOL r130RunStaleEpochCapture(void) {
    const uint64_t sequence = 20;
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130ConfigureEpoch(R127RuntimeModeGenerating, 2, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    const uint64_t staleBefore = r127StaleCallbackRejections;
    r123EnqueueSource(sequence, sequence,
                      (id<MTLTexture>)drawable.texture, 1);
    const BOOL staleWasRejected = r127CachedSourceSequence != sequence &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerUnknown;
    hookDrawablePresent(drawable, @selector(present));
    hookDrawablePresent(drawable, @selector(present));
    const BOOL passed = staleWasRejected &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerTitleDispatched &&
        r130TitleDispatchCount[sequence] == 1 &&
        r130FGDispatchCount[sequence] == 0 &&
        [r123Pump sourceEnqueued] == 0 &&
        r127StaleCallbackRejections > staleBefore;
    printf("R130_STALE_EPOCH_CAPTURE %s sequence=%llu callbackEpoch=1 "
           "runtimeEpoch=2 titleWSI=%u fgWSI=%u cached=%d staleRejected=%llu\n",
        passed ? "PASS" : "FAIL", (unsigned long long)sequence,
        r130TitleDispatchCount[sequence], r130FGDispatchCount[sequence],
        r127CachedSourceSequence == sequence ? 1 : 0,
        (unsigned long long)r127StaleCallbackRejections);
    return passed;
}

static BOOL r130RunRepeatedTransitions(void) {
    static const uint32_t modes[] = {
        R127RuntimeModeSourceOnlyDrain,
        R127RuntimeModeOff,
        R127RuntimeModeGenerating,
        R127RuntimeModeSourceOnlyDrain,
        R127RuntimeModeOff,
        R127RuntimeModeGenerating,
    };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 100);
    BOOL passed = YES;
    uint64_t expectedEpoch = 1;
    for (NSUInteger i = 0; i < sizeof(modes) / sizeof(modes[0]); ++i) {
        pthread_mutex_lock(&r127RuntimeLock);
        R123RuntimeEpochResult result = r127AdvanceRuntimeEpochLocked(modes[i]);
        pthread_mutex_unlock(&r127RuntimeLock);
        ++expectedEpoch;
        if (r127RuntimeEpochSnapshot() != expectedEpoch ||
            r127RuntimeModeSnapshot() != modes[i] ||
            [r123Pump runtimeEpoch] != expectedEpoch || result.discardedSource != 0 ||
            [r123Pump activeSourceReservations] != 0) passed = NO;
    }
    printf("R130_REPEATED_ON_OFF_EPOCHS %s transitions=6 finalMode=%u "
           "runtimeEpoch=%llu pumpEpoch=%llu\n",
        passed ? "PASS" : "FAIL", r127RuntimeModeSnapshot(),
        (unsigned long long)r127RuntimeEpochSnapshot(),
        (unsigned long long)[r123Pump runtimeEpoch]);
    return passed;
}

static BOOL r130RunPumpCapacityAccounting(void) {
    R123DisplayPump *pump = [[R123DisplayPump alloc] initWithMaxDepth:3
        halfPeriodNs:1000000ULL];
    (void)[pump setRuntimeEnabled:YES epoch:1];
    BOOL initialCapacityAvailable = YES;
    for (uint64_t sequence = 1; sequence <= 3; ++sequence) {
        R123DisplayItem *reservation = [pump reserveSource:sequence
            appFrameId:sequence payload:@(sequence) atNs:sequence * 1000000ULL
            runtimeEpoch:1];
        reservation.sourcePresentOrdinal = sequence;
        if (!reservation || ![pump commitSourceReservation:reservation]) {
            initialCapacityAvailable = NO;
            break;
        }
    }
    [pump primeAtNs:1];
    const NSUInteger beforeClaimReservations = pump.activeSourceReservations;
    R123DisplayItem *claimed = nil;
    R123DisplayItem *missed = nil;
    (void)[pump takeForSlotAtNs:100000000ULL outItem:&claimed outMissed:&missed];
    const NSUInteger afterClaimReservations = pump.activeSourceReservations;
    R123DisplayItem *overCapacity = [pump reserveSource:4 appFrameId:4
        payload:@4 atNs:4000000ULL runtimeEpoch:1];
    const BOOL claimedHead = claimed && claimed.kind == R123ItemKindSource &&
        claimed.seqB == 1;
    const BOOL passed = initialCapacityAvailable && beforeClaimReservations == 3 &&
        afterClaimReservations == 3 && pump.depth == 2 && overCapacity == nil &&
        pump.activeSourceReservations == 3 && pump.droppedInternal > 0 &&
        pump.droppedSource == 0 && claimedHead;
    printf("R130_PUMP_CAPACITY_ACCOUNTING %s sourceCapacity=3 beforeClaim=%lu "
           "afterClaim=%lu queuedDepth=%lu fourthReservation=%s "
           "droppedInternal=%llu droppedSource=%llu claimedSeq=%llu\n",
        passed ? "PASS" : "FAIL", (unsigned long)beforeClaimReservations,
        (unsigned long)afterClaimReservations, (unsigned long)pump.depth,
        overCapacity ? "ACCEPTED" : "REFUSED",
        (unsigned long long)pump.droppedInternal,
        (unsigned long long)pump.droppedSource,
        (unsigned long long)(claimed ? claimed.seqB : 0));
    [pump rejectUndispatchedItem:claimed];
    return passed;
}

// The ownership ticket must remain usable when the separate bounded R125
// correlation registry no longer contains this still-live title drawable.
// Exercise captureRealDrawable's real preflight, allocation and completion
// branches. Only the Metal device, queue and command buffer are faked.
static BOOL r130RunOnWithUnresolvedCapture(void) {
    const uint64_t firstSequence = 60, nextSequence = 61;
    R130FakeDrawable *first = r130MakeDrawable(firstSequence);
    r130InstallOriginalIMP(first);
    r130ConfigureEpoch(R127RuntimeModeOff, 1, firstSequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(firstSequence, (id<CAMetalDrawable>)first);
    const R127SourceOwner priorLookup = r127SourceOwnerForSequence(firstSequence);
    pthread_mutex_lock(&r127RuntimeLock);
    (void)r127AdvanceRuntimeEpochLocked(R127RuntimeModeGenerating);
    r127MinimumSourceSequence = firstSequence;
    pthread_mutex_unlock(&r127RuntimeLock);
    // Copy completion from the pre-ON epoch must not become a ready image in
    // the new epoch, even though ownership was looked up before transition.
    r123EnqueueSource(firstSequence, firstSequence, (id<MTLTexture>)first.texture, 1);
    hookDrawablePresent(first, @selector(present));
    const BOOL firstDirect = priorLookup == R127SourceOwnerUnknown &&
        r127SourceOwnerForSequence(firstSequence) == R127SourceOwnerTitleDispatched &&
        r130TitleDispatchCount[firstSequence] == 1 && r130FGDispatchCount[firstSequence] == 0;
    R130FakeDrawable *next = r130MakeDrawable(nextSequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(nextSequence, (id<CAMetalDrawable>)next);
    r123EnqueueSource(nextSequence, nextSequence, (id<MTLTexture>)next.texture, 2);
    hookDrawablePresent(next, @selector(present));
    r130InstallFakePresenter(next);
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);
    const BOOL passed = firstDirect && r130FGDispatchCount[nextSequence] == 1 &&
        r130TitleDispatchCount[nextSequence] == 0 &&
        r127SourceOwnerForSequence(nextSequence) == R127SourceOwnerDispatched &&
        [r123Pump activeSourceReservations] == 0;
    printf("R130_ON_UNRESOLVED_CAPTURE %s oldTitleWSI=%u oldFgWSI=%u "
           "nextTitleWSI=%u nextFgWSI=%u transitionEpoch=%llu\n", passed ? "PASS" : "FAIL",
        r130TitleDispatchCount[firstSequence], r130FGDispatchCount[firstSequence],
        r130TitleDispatchCount[nextSequence], r130FGDispatchCount[nextSequence],
        (unsigned long long)r127RuntimeEpochSnapshot());
    return passed;
}

static BOOL r130RunOffWithCommittedSource(BOOL claimedBeforeOff) {
    const uint64_t sequence = claimedBeforeOff ? 52 : 50;
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    r123EnqueueSource(sequence, sequence, (id<MTLTexture>)drawable.texture, 1);
    hookDrawablePresent(drawable, @selector(present));
    r130InstallFakePresenter(drawable);
    BOOL claimOK = YES;
    if (claimedBeforeOff) {
        R123DisplayItem *claimed = nil, *missed = nil;
        pthread_mutex_lock(&r127RuntimeLock);
        (void)[r123Pump takeForSlotAtNs:r105NowNs() + 1000000000ULL
                                 outItem:&claimed outMissed:&missed];
        if (claimed) r127PumpItemInFlight = claimed;
        pthread_mutex_unlock(&r127RuntimeLock);
        claimOK = claimed && claimed.seqB == sequence &&
            [r123Pump activeSourceReservations] == 1;
    }
    pthread_mutex_lock(&r127RuntimeLock);
    R123RuntimeEpochResult result = r127AdvanceRuntimeEpochLocked(R127RuntimeModeSourceOnlyDrain);
    r127DisableRequestId = 130;
    r127DisableRequestCutoffSequence = sequence;
    pthread_mutex_unlock(&r127RuntimeLock);
    r127TryCompleteDisable(sequence + 1);
    const BOOL retained = claimOK && result.discardedSource == 0 &&
        r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain &&
        [r123Pump activeSourceReservations] == 1 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerSuppressed;
    r130RunPumpPresentation(r105NowNs() + 2000000000ULL);
    r127TryCompleteDisable(sequence + 1);
    const BOOL passed = retained && r127RuntimeModeSnapshot() == R127RuntimeModeOff &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerDispatched &&
        r130FGDispatchCount[sequence] == 1 && r130TitleDispatchCount[sequence] == 0 &&
        [r123Pump activeSourceReservations] == 0 && [r123Pump droppedSource] == 0;
    printf("R130_OFF_WITH_%s_SOURCE %s fgWSI=%u titleWSI=%u reservations=%lu "
           "droppedSource=%llu physicalCallback=NONE\n", claimedBeforeOff ? "CLAIMED" : "QUEUED",
        passed ? "PASS" : "FAIL", r130FGDispatchCount[sequence],
        r130TitleDispatchCount[sequence], (unsigned long)[r123Pump activeSourceReservations],
        (unsigned long long)[r123Pump droppedSource]);
    return passed;
}

static BOOL r130RunCaptureFailure(unsigned stage) {
    const uint64_t sequence = 40 + stage;
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);

    (void)pthread_once(&r125SwitchOnce, r125InitSwitches);
    gR125CaptureAllowed = YES;
    setenv("FGMETAL_R108_CAPTURE", "1", 1);
    R130FakeCaptureDevice *device = [R130FakeCaptureDevice new];
    device.failAllocation = stage == 2;
    device.registryID = 130;
    drawable.texture.device = device;
    drawable.layer.device = device;
    drawable.texture.width = 64;
    drawable.texture.height = 64;
    drawable.texture.pixelFormat = MTLPixelFormatBGRA8Unorm;
    drawable.texture.usage = MTLTextureUsageShaderRead;
    drawable.texture.framebufferOnly = stage == 1;
    R130FakeCommandQueue *queue = [R130FakeCommandQueue new];
    queue.device = device;
    const uint64_t attemptsBefore = captureAttempts;
    const NSUInteger blitsBefore = r130CaptureBlitCommits;
    r130FailNextCopy = stage == 3;
    captureRealDrawable((id<MTLCommandQueue>)queue,
        (id<MTLTexture>)drawable.texture, drawable.drawableID, sequence, 1, nil);
    const BOOL branchReached = captureAttempts == attemptsBefore + 1 &&
        (stage == 1 ? device.allocationAttempts == 0 : device.allocationAttempts > 0) &&
        r130CaptureBlitCommits - blitsBefore == (stage == 3 ? 1u : 0u);
    hookDrawablePresent(drawable, @selector(present));
    const BOOL passed = branchReached && drawable.titleDispatches == 1 &&
        r130FGDispatchCount[sequence] == 0 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerTitleDispatched &&
        r127CachedSourceSequence != sequence && [r123Pump activeSourceReservations] == 0;
    const char *name = stage == 1 ? "preflight" : stage == 2 ? "allocation" : "capture-copy";
    printf("R130_CAPTURE_FAILURE_%s %s realCapturePath=1 titleWSI=%lu fgWSI=%u "
           "allocationAttempts=%lu blitCommits=%lu\n", name, passed ? "PASS" : "FAIL",
        (unsigned long)drawable.titleDispatches, r130FGDispatchCount[sequence],
        (unsigned long)device.allocationAttempts,
        (unsigned long)(r130CaptureBlitCommits - blitsBefore));
    return passed;
}

static BOOL r130RunEvictedCorrelationRecord(void) {
    const uint64_t sequence = 31;
    R130FakeDrawable *drawable = r130MakeDrawable(sequence);
    r130InstallOriginalIMP(drawable);
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, sequence);
    r130BeginTitleAcquisition();
    r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    pthread_mutex_lock(&stateLock);
    [drawablesByTexture removeObjectForKey:
        r125PointerKey((__bridge const void *)drawable.texture)];
    pthread_mutex_unlock(&stateLock);
    const BOOL registryMissing = trackedRecordForDrawable((id<CAMetalDrawable>)drawable) == nil;
    hookDrawablePresent(drawable, @selector(present));
    hookDrawablePresent(drawable, @selector(present));
    const BOOL passed = registryMissing && drawable.titleDispatches == 1 &&
        r127SourceOwnerForSequence(sequence) == R127SourceOwnerTitleDispatched &&
        r130TitleAcquisitionsOutstanding == 0 && !r130AcquireAccountingAmbiguous &&
        [r123Pump activeSourceReservations] == 0;
    printf("R130_EVICTED_CORRELATION_RECORD %s registryMissing=%d titleWSI=%lu "
           "ticketOutstanding=%llu ambiguous=%d\n", passed ? "PASS" : "FAIL",
        registryMissing ? 1 : 0, (unsigned long)drawable.titleDispatches,
        (unsigned long long)r130TitleAcquisitionsOutstanding,
        r130AcquireAccountingAmbiguous ? 1 : 0);
    return passed;
}

static BOOL r130Run80SourceEndToEnd(void) {
    const uint64_t sourceCount = 80;
    const uint64_t fgReadyCount = 28;
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 1);
    uint64_t titleOwned = 0;
    uint64_t fgOwned = 0;
    uint64_t peakReservations = 0;
    uint64_t pumpTickNs = r105NowNs() + 1000000000ULL;

    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        r130InstallOriginalIMP(drawable);
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
        if (sequence <= fgReadyCount) {
            // The first 28 captures are ready at the actual title hook. The
            // remaining 52 deliberately have no private image and therefore
            // exercise the pre-commit TITLE fallback path.
            r123EnqueueSource(sequence, sequence,
                              (id<MTLTexture>)drawable.texture, 1);
        }

        const R127SourceOwner before = r127SourceOwnerForSequence(sequence);
        hookDrawablePresent(drawable, @selector(present));
        hookDrawablePresent(drawable, @selector(present));
        const R127SourceOwner committed = r127SourceOwnerForSequence(sequence);
        if (sequence <= fgReadyCount) {
            if (before != R127SourceOwnerUnknown ||
                committed != R127SourceOwnerSuppressed ||
                [r123Pump activeSourceReservations] != 1) return NO;
            ++fgOwned;
            if ([r123Pump activeSourceReservations] > peakReservations)
                peakReservations = [r123Pump activeSourceReservations];
            r130InstallFakePresenter(drawable);
            r130RunPumpPresentation(pumpTickNs);
            pumpTickNs += 100000000ULL;
            if (r127SourceOwnerForSequence(sequence) != R127SourceOwnerDispatched ||
                r130FGDispatchCount[sequence] != 1 ||
                r130TitleDispatchCount[sequence] != 0 ||
                [r123Pump activeSourceReservations] != 0 ||
                r130PendingFGDispatches != 0) return NO;
        } else {
            ++titleOwned;
            if (committed != R127SourceOwnerTitleDispatched ||
                r130TitleDispatchCount[sequence] != 1 ||
                r130FGDispatchCount[sequence] != 0 ||
                [r123Pump activeSourceReservations] != 0) return NO;
        }
    }

    uint64_t titleWsi = 0;
    uint64_t fgWsi = 0;
    uint64_t duplicates = 0;
    uint64_t zeroDispatch = 0;
    BOOL ordered = r130DispatchOrderCount == sourceCount;
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        const uint64_t titleCount = r130TitleDispatchCount[sequence];
        const uint64_t fgCount = r130FGDispatchCount[sequence];
        const uint64_t total = titleCount + fgCount;
        titleWsi += titleCount;
        fgWsi += fgCount;
        if (total > 1) duplicates += total - 1;
        if (total == 0) ++zeroDispatch;
        if (total != 1 || (titleCount && fgCount)) ordered = NO;
        if (sequence - 1 >= r130DispatchOrderCount ||
            r130DispatchOrder[sequence - 1] != sequence) ordered = NO;
    }
    const BOOL passed = titleOwned == 52 && fgOwned == 28 &&
        titleWsi == 52 && fgWsi == 28 && titleWsi + fgWsi == sourceCount &&
        duplicates == 0 && zeroDispatch == 0 && ordered &&
        [r123Pump droppedSource] == 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        r130PendingFGDispatches == 0 && peakReservations <= kR123MaxDepth &&
        r127SourceOwnership.overwriteNonTerminal == 0;
    printf("R130_80_SOURCE_END_TO_END %s logical=80 titleOwned=%llu fgOwned=%llu "
           "titleWsi=%llu fgWsi=%llu totalWsi=%llu duplicates=%llu "
           "zeroDispatch=%llu ordered=%d droppedSource=%llu "
           "peakReservations=%llu capacity=%d overwriteNonTerminal=%llu "
           "distribution=28-ready-FG/52-no-image-TITLE\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)titleOwned, (unsigned long long)fgOwned,
        (unsigned long long)titleWsi, (unsigned long long)fgWsi,
        (unsigned long long)(titleWsi + fgWsi), (unsigned long long)duplicates,
        (unsigned long long)zeroDispatch, ordered ? 1 : 0,
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)peakReservations, kR123MaxDepth,
        (unsigned long long)r127SourceOwnership.overwriteNonTerminal);
    return passed;
}

static BOOL r130Run80AcquisitionBacklog(void) {
    const uint64_t sourceCount = 80;
    R130FakeDrawable *__strong drawables[R130TestSequenceCapacity] = { nil };
    r130ConfigureEpoch(R127RuntimeModeGenerating, 1, 1);
    r130InstallOriginalIMP([R130FakeDrawable new]);
    R130FakeLayer *sharedLayer = [R130FakeLayer new];
    id sharedDevice = [NSObject new];
    sharedLayer.device = sharedDevice;

    // All title acquisitions are already outstanding before any present hook
    // runs. The ticket gate must keep every frame direct until only one ticket
    // remains and its matching private image is ready.
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        R130FakeDrawable *drawable = r130MakeDrawable(sequence);
        drawable.layer = sharedLayer;
        drawable.texture.device = sharedDevice;
        selectedHarnessLayer = (__bridge void *)sharedLayer;
        r130RegisterDrawable(drawable, sequence);
        drawables[sequence] = drawable;
        r130BeginTitleAcquisition();
        r130RegisterTitleAcquisition(sequence, (id<CAMetalDrawable>)drawable);
    }
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        r123EnqueueSource(sequence, sequence,
                          (id<MTLTexture>)drawables[sequence].texture, 1);
    }

    uint64_t titleOwned = 0;
    uint64_t fgOwned = 0;
    uint64_t peakReservations = 0;
    uint64_t pumpTickNs = r105NowNs() + 1000000000ULL;
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        hookDrawablePresent(drawables[sequence], @selector(present));
        const R127SourceOwner owner = r127SourceOwnerForSequence(sequence);
        if (sequence < sourceCount) {
            if (owner != R127SourceOwnerTitleDispatched ||
                r130TitleDispatchCount[sequence] != 1 ||
                r130FGDispatchCount[sequence] != 0) {
                fprintf(stderr, "R130_BACKLOG_FAIL stage=title sequence=%llu owner=%u title=%u fg=%u outstanding=%llu cacheSequence=%llu\n",
                    (unsigned long long)sequence, (unsigned)owner,
                    r130TitleDispatchCount[sequence], r130FGDispatchCount[sequence],
                    (unsigned long long)r130TitleAcquisitionsOutstanding,
                    (unsigned long long)r127CachedSourceSequence);
                return NO;
            }
            ++titleOwned;
        } else {
            if (owner != R127SourceOwnerSuppressed ||
                r130TitleDispatchCount[sequence] != 0 ||
                [r123Pump activeSourceReservations] != 1) {
                fprintf(stderr, "R130_BACKLOG_FAIL stage=final-admit sequence=%llu owner=%u title=%u reservations=%lu outstanding=%llu cacheSequence=%llu pending=%llu\n",
                    (unsigned long long)sequence, (unsigned)owner,
                    r130TitleDispatchCount[sequence],
                    (unsigned long)[r123Pump activeSourceReservations],
                    (unsigned long long)r130TitleAcquisitionsOutstanding,
                    (unsigned long long)r127CachedSourceSequence,
                    (unsigned long long)r130PendingFGDispatches);
                return NO;
            }
            ++fgOwned;
            peakReservations = [r123Pump activeSourceReservations];
            r130InstallFakePresenter(drawables[sequence]);
            r130RunPumpPresentation(pumpTickNs);
            if (r127SourceOwnerForSequence(sequence) != R127SourceOwnerDispatched ||
                r130FGDispatchCount[sequence] != 1 ||
                [r123Pump activeSourceReservations] != 0) {
                fprintf(stderr, "R130_BACKLOG_FAIL stage=final-dispatch sequence=%llu owner=%u fg=%u reservations=%lu depth=%lu\n",
                    (unsigned long long)sequence,
                    (unsigned)r127SourceOwnerForSequence(sequence),
                    r130FGDispatchCount[sequence],
                    (unsigned long)[r123Pump activeSourceReservations],
                    (unsigned long)[r123Pump depth]);
                return NO;
            }
        }
    }

    uint64_t duplicates = 0;
    uint64_t zeroDispatch = 0;
    uint64_t titleWsi = 0;
    uint64_t fgWsi = 0;
    BOOL ordered = r130DispatchOrderCount == sourceCount;
    for (uint64_t sequence = 1; sequence <= sourceCount; ++sequence) {
        const uint64_t title = r130TitleDispatchCount[sequence];
        const uint64_t fg = r130FGDispatchCount[sequence];
        const uint64_t total = title + fg;
        titleWsi += title;
        fgWsi += fg;
        if (total > 1) duplicates += total - 1;
        if (total == 0) ++zeroDispatch;
        if (total != 1 || (title && fg)) ordered = NO;
        if (sequence - 1 >= r130DispatchOrderCount ||
            r130DispatchOrder[sequence - 1] != sequence) ordered = NO;
    }
    const BOOL passed = titleOwned == 79 && fgOwned == 1 &&
        titleWsi == 79 && fgWsi == 1 && titleWsi + fgWsi == sourceCount &&
        duplicates == 0 && zeroDispatch == 0 && ordered &&
        [r123Pump droppedSource] == 0 &&
        [r123Pump activeSourceReservations] == 0 &&
        r130TitleAcquisitionsOutstanding == 0 && r130PendingFGDispatches == 0 &&
        peakReservations == 1 && r127SourceOwnership.overwriteNonTerminal == 0;
    printf("R130_80_ACQUISITION_BACKLOG %s logical=80 outstandingAtStart=80 "
           "titleOwned=%llu fgOwned=%llu titleWsi=%llu fgWsi=%llu totalWsi=%llu "
           "duplicates=%llu zeroDispatch=%llu ordered=%d droppedSource=%llu "
           "peakReservations=%llu capacity=%d overwriteNonTerminal=%llu "
           "pressure=ACQUISITION-TICKET-BACKPRESSURE capacity-refusal=NOT-OBSERVED\n",
        passed ? "PASS" : "FAIL",
        (unsigned long long)titleOwned, (unsigned long long)fgOwned,
        (unsigned long long)titleWsi, (unsigned long long)fgWsi,
        (unsigned long long)(titleWsi + fgWsi), (unsigned long long)duplicates,
        (unsigned long long)zeroDispatch, ordered ? 1 : 0,
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)peakReservations, kR123MaxDepth,
        (unsigned long long)r127SourceOwnership.overwriteNonTerminal);
    return passed;
}

static BOOL r137RunTraceSelectionSelfTest(void) {
    const BOOL routineShortGap =
        r137TracePromotionTrigger(66666700, 0, 74000000) == 0;
    const BOOL sustainedGap =
        r137TracePromotionTrigger(100000000, 0, 0) == 1;
    const BOOL directDuringGap =
        r137TracePromotionTrigger(50000000, 1, 0) == 2;
    const BOOL directTooEarly =
        r137TracePromotionTrigger(49999999, 1, 0) == 0;
    const BOOL agedCommittedSource =
        r137TracePromotionTrigger(0, 0, 100000000) == 3;
    const BOOL sustainedHasPriority =
        r137TracePromotionTrigger(100000000, 1, 100000000) == 1;
    const BOOL directHasPriorityOverAge =
        r137TracePromotionTrigger(50000000, 1, 100000000) == 2;
    const BOOL fgRoutineBudgetAvailable =
        r137TracePromotionCategory(NO, 5, 4, 0, NO) == 1;
    const BOOL fgRoutineBudgetBounded =
        r137TracePromotionCategory(NO, 6, 0, 0, NO) == 0;
    const BOOL fgSevereBudgetReserved =
        r137TracePromotionCategory(NO, 6, 4, 0, YES) == 3;
    const BOOL fgSevereBudgetBounded =
        r137TracePromotionCategory(NO, 6, 0, 2, YES) == 0;
    const BOOL fgSeverePriority =
        r137TracePromotionCategory(NO, 5, 0, 0, YES) == 3;
    const BOOL fgRoutineFallback =
        r137TracePromotionCategory(NO, 5, 0, 2, YES) == 1;
    const BOOL titleBudgetAvailable =
        r137TracePromotionCategory(YES, 6, 3, 2, YES) == 2;
    const BOOL titleBudgetBounded =
        r137TracePromotionCategory(YES, 0, 4, 0, NO) == 0;
    const BOOL fgBudgetReservedFromTitle =
        r137TracePromotionCategory(NO, 0, 4, 0, NO) == 1;
    const BOOL titleBudgetSurvivesFgLimit =
        r137TracePromotionCategory(YES, 6, 0, 2, YES) == 2;
    const BOOL pumpSleepReturnNamed = strcmp(
        r137EventName(R137EventPumpSleepReturn), "PUMP_SLEEP_RETURN") == 0;
    const BOOL pumpSummaryDelayNamed = strcmp(
        r137EventName(R137EventPumpSummaryDelay), "PUMP_SUMMARY_DELAY") == 0;
    const BOOL pumpLoopLockWaitNamed = strcmp(
        r137EventName(R137EventPumpLoopLockWait), "PUMP_LOOP_LOCK_WAIT") == 0;
    const BOOL pumpDispatchLockWaitNamed = strcmp(
        r137EventName(R137EventPumpDispatchLockWait),
        "PUMP_DISPATCH_LOCK_WAIT") == 0;
    const BOOL presentationStageNamed = strcmp(
        r137EventName(R137EventPresentationStage), "PRESENTATION_STAGE") == 0;
    const BOOL metalFxNoOutputClassified = strcmp(r137MetalFxResultName(
        r137MetalFxResultFlags(YES, NO, NO)), "NO-OUTPUT") == 0;
    const BOOL metalFxErrorClassified = strcmp(r137MetalFxResultName(
        r137MetalFxResultFlags(YES, NO, YES)), "ERROR") == 0;
    const BOOL metalFxFrameClassified = strcmp(r137MetalFxResultName(
        r137MetalFxResultFlags(YES, YES, NO)), "FRAME") == 0;
    const BOOL metalFxFrameAndErrorClassified = strcmp(r137MetalFxResultName(
        r137MetalFxResultFlags(YES, YES, YES)), "FRAME-AND-ERROR") == 0;
    const BOOL metalFxSkippedClassified = strcmp(r137MetalFxResultName(
        r137MetalFxResultFlags(NO, NO, NO)), "PROVIDER-SKIPPED") == 0;
    const uint32_t stageReadyFlags = r137PresentationStageFlags(
        YES, YES, YES, YES, YES, NO, YES, NO, YES, YES, NO, NO, NO,
        R127SourceOwnerSuppressed);
    const uint32_t stagePendingFlags = r137PresentationStageFlags(
        YES, YES, YES, YES, NO, NO, NO, YES, YES, NO, NO, NO, NO,
        R127SourceOwnerDirect);
    const uint32_t stageNoEntryFlags = r137PresentationStageFlags(
        NO, NO, NO, NO, NO, NO, NO, NO, NO, NO, NO, NO, NO,
        R127SourceOwnerUnknown);
    const uint32_t stageCommittedNoEntryFlags = r137PresentationStageFlags(
        NO, NO, NO, NO, NO, NO, NO, NO, NO, NO, NO, YES, NO,
        R127SourceOwnerSuppressed);
    const uint32_t stageCopyFailedFlags = r137PresentationStageFlags(
        YES, YES, YES, YES, YES, YES, NO, NO, YES, NO, NO, NO, NO,
        R127SourceOwnerSuppressed);
    const uint32_t stageSlotRefusedFlags = r137PresentationStageFlags(
        YES, YES, YES, YES, YES, NO, NO, NO, YES, NO, NO, NO, NO,
        R127SourceOwnerSuppressed);
    const BOOL stageReadyClassified =
        (stageReadyFlags & R137StageReady) != 0 &&
        strcmp(r137PresentationDispositionName(stageReadyFlags), "READY") == 0;
    const BOOL stageOwnerEncoded =
        (stageReadyFlags & R137StageOwnerMask) ==
            ((uint32_t)R127SourceOwnerSuppressed << R137StageOwnerShift);
    const BOOL stagePendingClassified =
        strcmp(r137PresentationDispositionName(stagePendingFlags),
               "PENDING-COPY") == 0;
    const BOOL stageNoEntryClassified =
        strcmp(r137PresentationDispositionName(stageNoEntryFlags),
               "NO-STAGE") == 0;
    const BOOL stageCommittedNoEntryClassified =
        strcmp(r137PresentationDispositionName(stageCommittedNoEntryFlags),
               "COMMITTED-COPY-NO-STAGE") == 0;
    const BOOL stageCopyFailedClassified =
        strcmp(r137PresentationDispositionName(stageCopyFailedFlags),
               "COPY-FAILED") == 0;
    const BOOL stageSlotRefusedClassified =
        strcmp(r137PresentationDispositionName(stageSlotRefusedFlags),
               "GENERATION-SLOT-REFUSED") == 0;
    const BOOL passed = routineShortGap && sustainedGap && directDuringGap &&
        directTooEarly && agedCommittedSource && sustainedHasPriority &&
        directHasPriorityOverAge && fgRoutineBudgetAvailable &&
        fgRoutineBudgetBounded && fgSevereBudgetReserved &&
        fgSevereBudgetBounded && fgSeverePriority && fgRoutineFallback &&
        titleBudgetAvailable && titleBudgetBounded &&
        fgBudgetReservedFromTitle && titleBudgetSurvivesFgLimit &&
        pumpSleepReturnNamed && pumpSummaryDelayNamed &&
        pumpLoopLockWaitNamed && pumpDispatchLockWaitNamed &&
        presentationStageNamed && metalFxNoOutputClassified &&
        metalFxErrorClassified && metalFxFrameClassified &&
        metalFxFrameAndErrorClassified && metalFxSkippedClassified &&
        stageReadyClassified && stageOwnerEncoded && stagePendingClassified &&
        stageNoEntryClassified && stageCommittedNoEntryClassified &&
        stageCopyFailedClassified && stageSlotRefusedClassified;
    printf("R137_TRACE_SELECTION_SELFTEST=%s cases=34 routine-66ms=skip "
           "sustained-100ms=trace direct-after-50ms=trace "
           "committed-age-100ms=trace budgets=6-FG+2-severe+4-title "
           "pump-stage-wake-summary-and-lock-events=PASS "
           "metalFx-frame-nil-error=PASS precedence=PASS\n",
        passed ? "PASS" : "FAIL");
    return passed;
}

// R138: deterministic reproduction of the measured 216.7 ms FG-only starvation
// cascade (r138-evidence/live-r138 heavy-01 outage index 91). The live final10
// trace showed three history seeds and three provider NO-OUTPUTs across five
// consecutive unproducible midpoints: a generation-slot-busy skip queues a
// history gap reset, and the first real pair after that reset seeded MetalFX
// history and then discarded its own valid A->B interpolation output. This
// test reproduces the exact provider transition: after invalidateHistory the
// FIRST successful interpolation attempt must yield an INTERNAL frame, not a
// by-design NO-OUTPUT, and identity must continue monotonically. It fails
// against the pre-R138 provider (first pair returned nil).
static BOOL r138RunProviderSeedWarmupProducesFrame(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device || ![MetalFXFrameInterpolationProvider supportsDevice:device]) {
            printf("R138_PROVIDER_SEED_WARMUP SKIP reason=metalfx-device-unavailable\n");
            return YES;
        }
        NSError *error = nil;
        MetalFXFrameInterpolationProvider *provider =
            [[MetalFXFrameInterpolationProvider alloc] initWithDevice:device
                                                                width:64 height:64 error:&error];
        if (!provider) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=provider-create error=%s\n",
                   error.localizedDescription.UTF8String ?: "none");
            return NO;
        }
        const NSUInteger width = 64, height = 64;
        MTLTextureDescriptor *inputDescriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                                                               width:width height:height mipmapped:NO];
        inputDescriptor.storageMode = MTLStorageModeShared;
        inputDescriptor.usage = MTLTextureUsageShaderRead;
        MTLTextureDescriptor *depthDescriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR32Float
                                                               width:width height:height mipmapped:NO];
        depthDescriptor.storageMode = MTLStorageModeShared;
        depthDescriptor.usage = MTLTextureUsageShaderRead;
        MTLTextureDescriptor *motionDescriptor =
            [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Float
                                                               width:width height:height mipmapped:NO];
        motionDescriptor.storageMode = MTLStorageModeShared;
        motionDescriptor.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> a = [device newTextureWithDescriptor:inputDescriptor];
        id<MTLTexture> b = [device newTextureWithDescriptor:inputDescriptor];
        id<MTLTexture> c = [device newTextureWithDescriptor:inputDescriptor];
        id<MTLTexture> depth = [device newTextureWithDescriptor:depthDescriptor];
        id<MTLTexture> motion = [device newTextureWithDescriptor:motionDescriptor];
        if (!a || !b || !c || !depth || !motion) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=input-allocation\n");
            return NO;
        }
        uint16_t *aPixels = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *bPixels = calloc(width * height * 4, sizeof(uint16_t));
        uint16_t *cPixels = calloc(width * height * 4, sizeof(uint16_t));
        float *depthPixels = malloc(width * height * sizeof(float));
        uint16_t *motionPixels = calloc(width * height * 2, sizeof(uint16_t));
        for (NSUInteger i = 0; i < width * height; ++i) {
            aPixels[i * 4 + 2] = 0x3c00; aPixels[i * 4 + 3] = 0x3c00;
            bPixels[i * 4 + 0] = 0x3c00; bPixels[i * 4 + 3] = 0x3c00;
            cPixels[i * 4 + 1] = 0x3c00; cPixels[i * 4 + 3] = 0x3c00;
            depthPixels[i] = 0.5f;
        }
        [a replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
               withBytes:aPixels bytesPerRow:width * 8];
        [b replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
               withBytes:bPixels bytesPerRow:width * 8];
        [c replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
               withBytes:cPixels bytesPerRow:width * 8];
        [depth replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
               withBytes:depthPixels bytesPerRow:width * 4];
        [motion replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0
               withBytes:motionPixels bytesPerRow:width * 4];
        const float deltaTime = 1.0f / 30.0f;

        // Transition under test: history reset, then the first real pair.
        [provider invalidateHistory];
        if ([provider historyValid]) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=history-valid-after-invalidate\n");
            free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
            return NO;
        }
        NSError *firstError = nil;
        FGMetalInternalFrame *first =
            [provider interpolatePreviousColor:a currentColor:b
                                  depthTexture:depth motionTexture:motion
                                    deltaTime:deltaTime error:&firstError];
        if (!first || firstError) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=first-pair-after-reset-no-output "
                   "frame=%s error=%s (the measured live amplifier: first pair after a "
                   "history reset returned NO-OUTPUT, leaving its midpoint unproducible)\n",
                   first ? "present" : "nil",
                   firstError.localizedDescription.UTF8String ?: "none");
            free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
            return NO;
        }
        if (![provider historyValid]) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=history-not-established-after-first-frame\n");
            free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
            return NO;
        }
        FGMetalInternalFrame *second =
            [provider interpolatePreviousColor:b currentColor:c
                                  depthTexture:depth motionTexture:motion
                                    deltaTime:deltaTime error:nil];
        if (!second || second.internalPresentId != first.internalPresentId + 1) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=continuation-broken "
                   "first=%llu second=%s\n",
                   (unsigned long long)first.internalPresentId,
                   second ? "mismatch" : "nil");
            free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
            return NO;
        }
        // Repeatability: a second reset must also yield on its first pair; the
        // live cascade chained through repeated resets in the heavy area.
        [provider invalidateHistory];
        FGMetalInternalFrame *third =
            [provider interpolatePreviousColor:a currentColor:b
                                  depthTexture:depth motionTexture:motion
                                    deltaTime:deltaTime error:nil];
        if (!third || third.internalPresentId != second.internalPresentId + 1) {
            printf("R138_PROVIDER_SEED_WARMUP FAIL reason=repeat-reset-no-output "
                   "second=%llu third=%s\n",
                   (unsigned long long)second.internalPresentId,
                   third ? "mismatch" : "nil");
            free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
            return NO;
        }
        printf("R138_PROVIDER_SEED_WARMUP PASS first-pair-yields-frame "
               "identity=%llu->%llu->%llu historyValid=%d\n",
               (unsigned long long)first.internalPresentId,
               (unsigned long long)second.internalPresentId,
               (unsigned long long)third.internalPresentId,
               [provider historyValid] ? 1 : 0);
        free(aPixels); free(bPixels); free(cPixels); free(depthPixels); free(motionPixels);
        return YES;
    }
}

int main(void) {
    @autoreleasepool {
        const int ownership = r130RunPresentBeforeCapture();
        const BOOL estimatorGap = r132RunGapDispositionTest();
        const BOOL estimatorLateCapture = r132RunLateCaptureTest();
        const BOOL estimatorStageCapacity = r132RunStagingCapacityTest();
        const BOOL estimatorStageReclamation = r132RunStageReclamationTest();
        const BOOL estimatorFrontierReclaim = r132RunFrontierReclaimTest();
        const BOOL estimatorMixedOwnership = r132RunMixedOwnershipTest();
        const BOOL pendingWithinGrace = r133RunPendingLateCaptureInsideGrace();
        const BOOL pendingExpiry = r133RunPendingExpiryAndLateRejection();
        const BOOL expiredGap = r133RunExpiredGapIsNotAdjacent();
        const BOOL multiplePending = r133RunMultiplePendingBounded();
        const BOOL commitPrerequisite = r133RunCommitPrerequisiteMatrix();
        const BOOL runtimeControl = r130RunRuntimeControlTransitions();
        const BOOL staleEpoch = r130RunStaleEpochCapture();
        const BOOL transitions = r130RunRepeatedTransitions();
        const BOOL capacity = r130RunPumpCapacityAccounting();
        const BOOL evictedCorrelation = r130RunEvictedCorrelationRecord();
        const BOOL capturePreflight = r130RunCaptureFailure(1);
        const BOOL captureAllocation = r130RunCaptureFailure(2);
        const BOOL captureCopy = r130RunCaptureFailure(3);
        const BOOL onUnresolved = r130RunOnWithUnresolvedCapture();
        const BOOL offQueued = r130RunOffWithCommittedSource(NO);
        const BOOL offClaimed = r130RunOffWithCommittedSource(YES);
        const BOOL overload = r130Run80SourceEndToEnd();
        const BOOL backlog = r130Run80AcquisitionBacklog();
        const BOOL lookupRace = r130RunLookupTransitionRace();
        const BOOL ledgerCollision = r130RunUnresolvedOwnershipCollision();
        const BOOL ledgerLifetime = r130RunOwnershipLedgerLifetime();
        const BOOL r137TraceSelection = r137RunTraceSelectionSelfTest();
        const BOOL r138SeedWarmup = r138RunProviderSeedWarmupProducesFrame();
        return ownership == 0 && estimatorGap && estimatorLateCapture &&
            estimatorStageCapacity && estimatorStageReclamation &&
            estimatorFrontierReclaim &&
            estimatorMixedOwnership && pendingWithinGrace && pendingExpiry &&
            expiredGap && multiplePending && commitPrerequisite &&
            runtimeControl && staleEpoch && transitions && capacity && evictedCorrelation &&
            capturePreflight && captureAllocation && captureCopy && onUnresolved && offQueued && offClaimed &&
            overload && backlog && lookupRace && ledgerCollision && ledgerLifetime &&
            r137TraceSelection && r138SeedWarmup ? 0 : 1;
    }
}
