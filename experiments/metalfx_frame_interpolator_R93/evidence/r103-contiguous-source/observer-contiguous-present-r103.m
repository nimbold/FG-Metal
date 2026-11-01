#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <QuartzCore/CAMetalLayer.h>
#import "MetalFXFrameInterpolationProvider.h"
#import <objc/runtime.h>
#import <libproc.h>
#import <dlfcn.h>
#import <dispatch/dispatch.h>
#import <mach-o/dyld.h>
#import <mach/mach_time.h>
#import <fcntl.h>
#import <pthread.h>
#import <stdarg.h>
#import <math.h>
#import <stdint.h>
#import <stdio.h>
#import <stdlib.h>
#import <string.h>
#include <time.h>
#include <errno.h>
#import <sys/stat.h>
#import <sys/sysctl.h>
#import <unistd.h>

// Bounded public Metal seam experiment for the selected Highball/D3DMetal process.
// It correlates real D3DMetal drawable production, captures at most eight
// source frames in sequence order, validates MetalFX output, and can insert
// one generated frame on the same CAMetalLayer before matching source B.

typedef struct { Class cls; SEL sel; IMP original; } Hook;
typedef struct {
    uint64_t count;
    uint64_t sumX;
    uint32_t minX, maxX, minY, maxY;
} ColorStats;

@interface R101CapturedFrame : NSObject
@property(nonatomic) uint64_t sequence;
@property(nonatomic) uint64_t drawableID;
@property(nonatomic) MTLPixelFormat pixelFormat;
@property(nonatomic, copy) NSData *sourceBytes;
@property(nonatomic) ColorStats mainShape;
@property(nonatomic) ColorStats markerShape;
@property(nonatomic, strong) id<MTLDevice> device;
@end

@implementation R101CapturedFrame
@end

typedef NS_ENUM(uint8_t, R102DeferredTicketState) {
    R102DeferredTicketEmpty = 0,
    R102DeferredTicketDeferred,
    R102DeferredTicketInternalDispatching,
    R102DeferredTicketWaitingForInternalDisplay,
    R102DeferredTicketForwardingSource,
};

typedef NS_ENUM(uint8_t, R103SourceFrontierState) {
    R103SourceFrontierOpen = 0,
    R103SourceFrontierClosingForPair,
    R103SourceFrontierClosed,
    R103SourceFrontierReleasing,
};

@interface R102DeferredSourceTicket : NSObject
@property(nonatomic, strong) id<CAMetalDrawable> drawable;
@property(nonatomic, strong) id<MTLTexture> texture;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandBuffer> producerCommandBuffer;
@property(nonatomic) SEL originalSelector;
@property(nonatomic) IMP originalImplementation;
@property(nonatomic) CFTimeInterval originalArgument;
@property(nonatomic) uint64_t appFrameId;
@property(nonatomic) uint64_t sourceSequence;
@property(nonatomic) uint64_t drawableID;
@property(nonatomic) uint64_t sourcePairA;
@property(nonatomic) uint64_t sourcePairB;
@property(nonatomic) uint64_t ticketId;
@property(nonatomic) BOOL producerCompletedWhileDeferredLogged;
@property(nonatomic) R102DeferredTicketState state;
@end

@implementation R102DeferredSourceTicket
@end

static Hook hooks[256];
static size_t hookCount;
static pthread_mutex_t stateLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t hookLock = PTHREAD_MUTEX_INITIALIZER;
static IMP originalNextDrawable;
static int logFd = -1;
static pthread_once_t logOnce = PTHREAD_ONCE_INIT;
static pthread_once_t targetOnce = PTHREAD_ONCE_INIT;
static BOOL selectedHarnessProcess;
static NSMutableDictionary<NSValue *, NSDictionary *> *drawablesByTexture;
static NSMutableDictionary<NSValue *, NSDictionary *> *r102ProducerRecordsByCommandBuffer;
static NSMutableDictionary<NSValue *, NSDictionary *> *r102EncoderRecords;
static NSMutableSet<NSValue *> *r102ScheduledHandlerKeys;
static uint64_t r102NextDrawableCallId;
static R102DeferredSourceTicket *r102DeferredTicket;
static uint64_t r102NextTicketId;
static R103SourceFrontierState r103SourceFrontierState;
static uint64_t r103FrontierTicketId;
static uint64_t r103FrontierPairA;
static uint64_t r103FrontierPairB;
static uint64_t r103FrontierDisplayedBaseline;
static uint64_t r103FrontierOperationsInFlight;
static void *r103FrontierLayer;
static void *r103FrontierDevice;
static void *r103FrontierProducer;
static uint64_t r103FrontierAttempts;
static uint64_t r103FrontierContiguousSuccesses;
static uint64_t r103FrontierFallbacks;
static uint64_t r103FrontierBlockedAcquisitions;
static uint64_t r103FrontierBlockedDispatches;
static uint64_t r103FutureSourceLeaks;
static BOOL r103FrontierSpent;
static uint64_t r102SuccessfulTupleCount;
static uint64_t r102FallbackCount;
static uint64_t r102StaleCallbackCount;
static uint64_t r102DuplicateInternalDispatchCount;
static uint64_t r102WrongPairDispatchCount;
static NSMutableSet<NSString *> *completedHookKeys;
static uint64_t drawableSequence;
static uint64_t consecutiveCorrelationCount;
static uint64_t lastCorrelatedSequence;
static uint64_t consecutiveCompletionCount;
static uint64_t lastCompletedSequence;
static void *correlatedQueue;
static void *correlatedDevice;
static void *selectedHarnessLayer;
static BOOL framebufferOnlyAttempted;
static BOOL framebufferOnlyValidated;
static BOOL correlationMilestoneLogged;
static BOOL completionMilestoneLogged;
static BOOL accessibilityMilestoneLogged;
static const NSUInteger kRetainedDrawableLimit = 32;
static const NSUInteger kCaptureLimit = 8;
static NSUInteger captureAttempts;
static dispatch_queue_t captureProcessingQueue;
static NSMutableDictionary<NSNumber *, id> *pendingCapturedFrames;
static uint64_t nextCaptureProcessingSequence = 1;
static id<MTLCommandQueue> sourceCaptureReadbackQueue;
static MetalFXFrameInterpolationProvider *realFrameProvider;
static id<MTLCommandQueue> outputReadbackQueue;
static id<MTLTexture> previousCaptureColor;
static id<MTLTexture> previousCaptureDepth;
static id<MTLTexture> previousCaptureMotion;
static NSData *previousCaptureFloatPixels;
static ColorStats previousMainShape;
static ColorStats previousMarkerShape;
static uint64_t previousCaptureSequence;
static BOOL realProviderInitialized;
static MTLTextureUsage colorInputUsage;
static MTLTextureUsage depthInputUsage;
static MTLTextureUsage motionInputUsage;
typedef NS_ENUM(uint8_t, R101InternalSlotState) {
    R101InternalSlotEmpty = 0,
    R101InternalSlotGenerating,
    R101InternalSlotReady,
    R101InternalSlotClaimed,
    R101InternalSlotPresented,
};
static R101InternalSlotState internalSlotState;
static pthread_cond_t presentationReadyCondition = PTHREAD_COND_INITIALIZER;
static id<MTLTexture> pendingInternalPresentationTexture;
static uint64_t pendingInternalPresentId;
static uint64_t pendingInternalPreviousSequence;
static uint64_t pendingInternalCurrentSequence;
static void *pendingInternalLayer;
static void *pendingInternalDevice;
static BOOL internalPresentationAttempted;
static uint64_t lastPresentationAttemptSequence;
static uint64_t lastCompletedPresentationSequence;
static uint64_t presentationAttempts;
static uint64_t internalPresentationSuccesses;
static uint64_t internalPresentationDisplayedCount;
static uint64_t internalPresentationFallbacks;
static uint64_t duplicateInternalClaims;
static uint64_t stalePairRejections;
static NSMutableSet<NSNumber *> *capturedSourceSequences;
static double pendingExpectedMainX;
static double pendingExpectedMarkerX;
static id<MTLRenderPipelineState> internalPresentationPipeline;
static __thread BOOL isInternalPresentationCall;
static __thread BOOL isInternalDrawableAcquisition;
static uint64_t nextWsiPresentId = 1;
static const char *const kHarnessExecutable =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r100-product-harness/fgmetal-r100-d3d11-harness.exe";

static void installHook(Class cls, SEL selector, IMP replacement);
static void installDrawablePresentationHooks(id<CAMetalDrawable> drawable);
static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<CAMetalDrawable> drawable,
                                uint64_t sourceSequence,
                                id<MTLCommandBuffer> producerCommandBuffer);
static uint64_t allocateWsiPresentId(void);
static void observeDisplayedDrawable(id<MTLDrawable> drawable,
                                     const char *origin,
                                     uint64_t wsiPresentId,
                                     uint64_t appFrameId,
                                     uint64_t internalPresentId);
static uint64_t logSourceWsiDispatch(id self, SEL selector);
static void releaseClaimedSlot(uint64_t sourceSequence, const char *reason);
static void hookDrawablePresent(id self, SEL selector);
static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when);
static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration);
static BOOL r102TryDeferSourcePresent(id<CAMetalDrawable> drawable, SEL selector,
                                     CFTimeInterval argument);
static void r102WaitForNextDrawableGate(uint64_t callId, CAMetalLayer *layer);
static void r102AttemptInternalDispatch(void);
static void r102FallbackTicket(R102DeferredSourceTicket *ticket, const char *reason);
static void r102FallbackForSequence(uint64_t sourceSequence, const char *reason);
static void r102ForwardSourceTicket(R102DeferredSourceTicket *ticket, const char *reason);
static BOOL r103BeginSourceAcquisition(uint64_t callId, CAMetalLayer *layer);
static void r103EndSourceOperation(BOOL operationLease, const char *phase,
                                   uint64_t identity);
static BOOL r103BeginSourceDispatch(id<CAMetalDrawable> drawable);
static void r103CloseFrontierAfterProducerCommit(NSDictionary *record,
                                                 id<MTLCommandBuffer> producer);
static void r103RecoverFrontier(uint64_t ticketId, const char *reason);
static BOOL renderInternalFrameToDrawable(id<MTLTexture> source,
                                         id<CAMetalDrawable> destinationDrawable,
                                         id<MTLCommandQueue> queue,
                                         uint64_t internalPresentId,
                                         double expectedMainX,
                                         double expectedMarkerX);
static NSDictionary *trackedDrawableForTexture(id<MTLTexture> texture);
static uint64_t r102MachTimeNs(void);
static void logR102Event(const char *event, NSDictionary *record,
                         id<CAMetalDrawable> explicitDrawable,
                         id<MTLCommandBuffer> explicitCommandBuffer,
                         id encoder, SEL selector, double argument);

static void initLog(void) {
    const char *configuredLogDir = getenv("FGMETAL_R103_LOG_DIR");
    const char *logDir = configuredLogDir && configuredLogDir[0]
        ? configuredLogDir : "/tmp/fgmetal-r103-contiguous";
    (void)mkdir(logDir, 0700);
    char path[1024];
    snprintf(path, sizeof(path), "%s/observer.%d.log", logDir, getpid());
    logFd = open(path, O_CREAT | O_APPEND | O_WRONLY | O_CLOEXEC, 0600);
}

static void logLine(const char *fmt, ...) {
    pthread_once(&logOnce, initLog);
    if (logFd < 0) return;
    char body[1600];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);
    if (n < 0) return;
    if ((size_t)n >= sizeof(body)) n = (int)sizeof(body) - 1;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t timestampNs = (uint64_t)now.tv_sec * UINT64_C(1000000000) +
                           (uint64_t)now.tv_nsec;
    char line[1800];
    int m = snprintf(line, sizeof(line), "t_ns=%llu %s\n",
                     (unsigned long long)timestampNs, body);
    if (m > 0) (void)write(logFd, line, (size_t)m);
}

static const char *className(id object) {
    return object ? class_getName(object_getClass(object)) : "nil";
}

static const char *procPath(void) {
    static char path[PROC_PIDPATHINFO_MAXSIZE];
    if (!path[0] && proc_pidpath(getpid(), path, sizeof(path)) <= 0)
        snprintf(path, sizeof(path), "unknown");
    return path;
}

static void identifySelectedProcess(void) {
    int mib[] = { CTL_KERN, KERN_PROCARGS2, getpid() };
    size_t size = 0;
    if (sysctl(mib, 3, NULL, &size, NULL, 0) != 0 || size < sizeof(int)) return;
    char *buffer = malloc(size);
    if (!buffer) return;
    if (sysctl(mib, 3, buffer, &size, NULL, 0) != 0 || size < sizeof(int)) { free(buffer); return; }

    int argc = 0;
    memcpy(&argc, buffer, sizeof(argc));
    char *cursor = buffer + sizeof(argc);
    char *end = buffer + size;
    size_t remaining = (size_t)(end - cursor);
    size_t execPathLength = strnlen(cursor, remaining);
    if (execPathLength < remaining) cursor += execPathLength + 1;
    while (cursor < end && *cursor == '\0') cursor++;
    size_t targetLength = strlen(kHarnessExecutable);
    for (int i = 0; i < argc && cursor < end; ++i) {
        size_t argumentLength = strnlen(cursor, (size_t)(end - cursor));
        if (argumentLength == targetLength && memcmp(cursor, kHarnessExecutable, targetLength) == 0) {
            selectedHarnessProcess = YES;
            break;
        }
        if (argumentLength >= (size_t)(end - cursor)) break;
        cursor += argumentLength + 1;
    }
    free(buffer);
}

static BOOL isSelectedProcess(void) {
    pthread_once(&targetOnce, identifySelectedProcess);
    return selectedHarnessProcess;
}

static BOOL isSelectedLayer(CAMetalLayer *layer) {
    return isSelectedProcess() && selectedHarnessLayer == (__bridge void *)layer;
}

static void retainDrawableForCorrelation(id<CAMetalDrawable> drawable) {
    id<MTLTexture> texture = drawable.texture;
    id<MTLDevice> layerDevice = drawable.layer.device;
    NSValue *textureKey = [NSValue valueWithPointer:(__bridge const void *)texture];
    pthread_mutex_lock(&stateLock);
    if (!drawablesByTexture) drawablesByTexture = [NSMutableDictionary dictionary];
    uint64_t sequence = ++drawableSequence;
    drawablesByTexture[textureKey] = @{
        @"drawable": drawable,
        @"sequence": @(sequence),
        @"drawableID": @(drawable.drawableID),
        @"layer": [NSValue valueWithPointer:(__bridge const void *)drawable.layer],
    };
    while (drawablesByTexture.count > kRetainedDrawableLimit) {
        NSValue *oldestKey = nil;
        uint64_t oldestSequence = UINT64_MAX;
        for (NSValue *key in drawablesByTexture) {
            uint64_t candidate = [drawablesByTexture[key][@"sequence"] unsignedLongLongValue];
            if (candidate < oldestSequence) { oldestSequence = candidate; oldestKey = key; }
        }
        if (!oldestKey) break;
        [drawablesByTexture removeObjectForKey:oldestKey];
    }
    pthread_mutex_unlock(&stateLock);
    logLine("DRAWABLE_TRACKED pid=%d seq=%llu drawableID=%lu layer=%p texture=%p layerDevice=%p textureDevice=%p sameDevice=%d registryID=%llu queueCorrelation=awaiting-render-pass framebufferOnly=%d width=%lu height=%lu pixelFormat=%lu usage=%lu",
        getpid(), (unsigned long long)sequence, (unsigned long)drawable.drawableID,
        (__bridge void *)drawable.layer, (__bridge void *)texture,
        (__bridge void *)layerDevice, (__bridge void *)texture.device,
        layerDevice == texture.device,
        (unsigned long long)texture.device.registryID, texture.framebufferOnly,
        (unsigned long)texture.width, (unsigned long)texture.height,
        (unsigned long)texture.pixelFormat, (unsigned long)texture.usage);

    BOOL accessibilityMilestone = NO;
    pthread_mutex_lock(&stateLock);
    if (framebufferOnlyValidated && !accessibilityMilestoneLogged &&
        !texture.framebufferOnly &&
        (texture.usage & MTLTextureUsageShaderRead) != 0 &&
        layerDevice == texture.device) {
        accessibilityMilestoneLogged = YES;
        accessibilityMilestone = YES;
    }
    pthread_mutex_unlock(&stateLock);
    if (accessibilityMilestone) {
        logLine("FRAMEBUFFER_ONLY_ACCESSIBILITY=VALIDATED pid=%d seq=%llu drawableID=%lu texture=%p device=%p registryID=%llu usage=%lu width=%lu height=%lu pixelFormat=%lu",
            getpid(), (unsigned long long)sequence, (unsigned long)drawable.drawableID,
            (__bridge void *)texture, (__bridge void *)texture.device,
            (unsigned long long)texture.device.registryID, (unsigned long)texture.usage,
            (unsigned long)texture.width, (unsigned long)texture.height,
            (unsigned long)texture.pixelFormat);
    }
}

static id hookNextDrawable(id self, SEL selector) {
    CAMetalLayer *layer = (CAMetalLayer *)self;
    BOOL internalAcquisition = isInternalDrawableAcquisition || isInternalPresentationCall;
    BOOL selected = NO;
    if (isSelectedProcess() && !internalAcquisition) {
        pthread_mutex_lock(&stateLock);
        if (!selectedHarnessLayer) {
            selectedHarnessLayer = (__bridge void *)layer;
            logLine("HARNESS_LAYER_SELECTED pid=%d path=%s layer=%p device=%p deviceName=%s registryID=%llu size=%.1fx%.1f pixelFormat=%lu framebufferOnly=%d",
                getpid(), procPath(), selectedHarnessLayer, (__bridge void *)layer.device,
                layer.device.name.UTF8String ?: "unknown",
                (unsigned long long)layer.device.registryID,
                layer.drawableSize.width, layer.drawableSize.height,
                (unsigned long)layer.pixelFormat, layer.framebufferOnly);
        }
        selected = selectedHarnessLayer == (__bridge void *)layer;
        pthread_mutex_unlock(&stateLock);
    }

    if (selected && getenv("FGMETAL_R101_FRAMEBUFFER_ONLY") &&
        strcmp(getenv("FGMETAL_R101_FRAMEBUFFER_ONLY"), "1") == 0) {
        pthread_mutex_lock(&stateLock);
        if (!framebufferOnlyAttempted) {
            framebufferOnlyAttempted = YES;
            BOOL before = layer.framebufferOnly;
            @try {
                layer.framebufferOnly = NO;
                framebufferOnlyValidated = !layer.framebufferOnly;
                logLine("FRAMEBUFFER_ONLY_SET pid=%d layer=%p before=%d requested=0 after=%d result=%s",
                    getpid(), selectedHarnessLayer, before, layer.framebufferOnly,
                    framebufferOnlyValidated ? "PASS" : "FAIL");
                if (!framebufferOnlyValidated && before) layer.framebufferOnly = YES;
            } @catch (NSException *exception) {
                @try { layer.framebufferOnly = before; } @catch (__unused NSException *restoreException) {}
                logLine("FRAMEBUFFER_ONLY_SET_EXCEPTION pid=%d layer=%p before=%d name=%s reason=%s restored=%d",
                    getpid(), selectedHarnessLayer, before,
                    exception.name.UTF8String ?: "unknown",
                    exception.reason.UTF8String ?: "unknown",
                    layer.framebufferOnly == before);
            }
        }
        pthread_mutex_unlock(&stateLock);
    }

    uint64_t callId = 0;
    BOOL acquisitionLease = NO;
    if (selected && isSelectedLayer(layer)) {
        pthread_mutex_lock(&stateLock);
        callId = ++r102NextDrawableCallId;
        pthread_mutex_unlock(&stateLock);
        logLine("R102_EVENT t_ns=%llu event=TARGET_NEXT_DRAWABLE_CALL sourceSequence=0 drawableID=0 drawable=0 texture=0 commandBuffer=0 encoder=0 queue=0 layer=%p device=%p selector=%s argument=0.000000000 callId=%llu",
            (unsigned long long)r102MachTimeNs(), (__bridge void *)layer,
            (__bridge void *)layer.device, sel_getName(selector),
            (unsigned long long)callId);
        acquisitionLease = r103BeginSourceAcquisition(callId, layer);
        r102WaitForNextDrawableGate(callId, layer);
    }
    id<CAMetalDrawable> drawable = ((id (*)(id, SEL))originalNextDrawable)(self, selector);
    if (drawable && selected && isSelectedLayer(layer)) {
        installDrawablePresentationHooks(drawable);
        retainDrawableForCorrelation(drawable);
        NSDictionary *record = trackedDrawableForTexture(drawable.texture);
        logR102Event("TARGET_NEXT_DRAWABLE_RETURN", record, drawable, nil, nil,
                     selector, 0.0);
        logLine("R102_EVENT_CALL_ID callId=%llu sourceSequence=%llu drawableID=%lu texture=%p layer=%p device=%p",
            (unsigned long long)callId,
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long)drawable.drawableID, (__bridge void *)drawable.texture,
            (__bridge void *)layer, (__bridge void *)drawable.texture.device);
    }
    if (selected && isSelectedLayer(layer))
        r103EndSourceOperation(acquisitionLease, "nextDrawable", callId);
    return drawable;
}

static IMP originalFor(id self, SEL selector) {
    Class cls = object_getClass(self);
    pthread_mutex_lock(&hookLock);
    IMP found = NULL;
    for (Class walk = cls; walk && !found; walk = class_getSuperclass(walk)) {
        for (size_t i = 0; i < hookCount; ++i) {
            if (hooks[i].cls == walk && hooks[i].sel == selector) {
                found = hooks[i].original;
                break;
            }
        }
    }
    pthread_mutex_unlock(&hookLock);
    return found;
}

static NSDictionary *trackedDrawableForTexture(id<MTLTexture> texture) {
    if (!texture) return nil;
    NSValue *key = [NSValue valueWithPointer:(__bridge const void *)texture];
    pthread_mutex_lock(&stateLock);
    NSDictionary *record = drawablesByTexture[key];
    pthread_mutex_unlock(&stateLock);
    return record;
}

static uint64_t r102MachTimeNs(void) {
    static mach_timebase_info_data_t timebase;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{ mach_timebase_info(&timebase); });
    uint64_t ticks = mach_absolute_time();
    return (uint64_t)(((__uint128_t)ticks * timebase.numer) / timebase.denom);
}

static BOOL r103FrontierEnabled(void) {
    const char *enabled = getenv("FGMETAL_R103_CLOSE_FRONTIER");
    return enabled && strcmp(enabled, "1") == 0;
}

static BOOL r103FrontierTargetsLayerLocked(CAMetalLayer *layer, id<MTLDevice> device) {
    return !r103FrontierSpent &&
        r103SourceFrontierState != R103SourceFrontierOpen &&
        r103FrontierLayer == (__bridge void *)layer &&
        r103FrontierDevice == (__bridge void *)device;
}

static void r103ClearFrontierLocked(void) {
    r103FrontierTicketId = 0;
    r103FrontierPairA = 0;
    r103FrontierPairB = 0;
    r103FrontierDisplayedBaseline = 0;
    r103FrontierLayer = NULL;
    r103FrontierDevice = NULL;
    r103FrontierProducer = NULL;
}

static BOOL r103BeginSourceAcquisition(uint64_t callId, CAMetalLayer *layer) {
    if (!r103FrontierEnabled() || isInternalDrawableAcquisition ||
        isInternalPresentationCall || !isSelectedProcess() || !isSelectedLayer(layer))
        return NO;

    BOOL loggedBlocked = NO;
    BOOL blockedEventLogged = NO;
    BOOL operationLease = NO;
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;

    pthread_mutex_lock(&stateLock);
    for (;;) {
        if (r103FrontierSpent || r103SourceFrontierState == R103SourceFrontierOpen ||
            !r103FrontierTargetsLayerLocked(layer, layer.device)) {
            if (!r103FrontierSpent) {
                ++r103FrontierOperationsInFlight;
                operationLease = YES;
            }
            pthread_mutex_unlock(&stateLock);
            if (loggedBlocked) {
                logLine("R103_FRONTIER_ACQUISITION_RELEASED callId=%llu layer=%p device=%p result=ADMITTED",
                    (unsigned long long)callId, (__bridge void *)layer,
                    (__bridge void *)layer.device);
            }
            return operationLease;
        }

        if (!loggedBlocked) {
            ++r103FrontierBlockedAcquisitions;
            loggedBlocked = YES;
        }
        uint64_t ticketId = r103FrontierTicketId;
        uint64_t pairA = r103FrontierPairA;
        uint64_t pairB = r103FrontierPairB;
        uint8_t frontierState = r103SourceFrontierState;
        if (loggedBlocked && !blockedEventLogged) {
            // Emit the attempt once per blocked acquisition, before waiting.
            pthread_mutex_unlock(&stateLock);
            logLine("R103_FRONTIER_ACQUISITION_BLOCKED callId=%llu ticketId=%llu pair=%llu->%llu layer=%p device=%p frontierState=%u origin=SOURCE",
                (unsigned long long)callId, (unsigned long long)ticketId,
                (unsigned long long)pairA, (unsigned long long)pairB,
                (__bridge void *)layer, (__bridge void *)layer.device,
                (unsigned)frontierState);
            pthread_mutex_lock(&stateLock);
            blockedEventLogged = YES;
        }
        int waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                                &stateLock, &deadline);
        if (waitResult == ETIMEDOUT) {
            ticketId = r103FrontierTicketId;
            pthread_mutex_unlock(&stateLock);
            r103RecoverFrontier(ticketId, "source-acquisition-recovery-bound");
            pthread_mutex_lock(&stateLock);
        }
    }
}

static void r103EndSourceOperation(BOOL operationLease, const char *phase,
                                   uint64_t identity) {
    if (!operationLease) return;
    pthread_mutex_lock(&stateLock);
    if (r103FrontierOperationsInFlight > 0)
        --r103FrontierOperationsInFlight;
    uint64_t inFlight = r103FrontierOperationsInFlight;
    BOOL closing = r103SourceFrontierState == R103SourceFrontierClosingForPair;
    pthread_cond_broadcast(&presentationReadyCondition);
    pthread_mutex_unlock(&stateLock);
    if (closing) {
        logLine("R103_FRONTIER_OPERATION_DRAINED phase=%s identity=%llu operationsInFlight=%llu",
            phase ?: "unknown", (unsigned long long)identity,
            (unsigned long long)inFlight);
    }
}

static BOOL r103BeginSourceDispatch(id<CAMetalDrawable> drawable) {
    if (!r103FrontierEnabled() || !drawable || isInternalPresentationCall ||
        isInternalDrawableAcquisition || !isSelectedProcess() ||
        !isSelectedLayer((CAMetalLayer *)drawable.layer)) return NO;

    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    uint64_t sourceSequence = [record[@"sequence"] unsignedLongLongValue];
    CAMetalLayer *layer = (CAMetalLayer *)drawable.layer;
    id<MTLDevice> device = drawable.texture.device;
    BOOL loggedBlocked = NO;
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;

    pthread_mutex_lock(&stateLock);
    for (;;) {
        if (r103FrontierSpent || r103SourceFrontierState == R103SourceFrontierOpen ||
            !r103FrontierTargetsLayerLocked(layer, device)) {
            if (!r103FrontierSpent) ++r103FrontierOperationsInFlight;
            BOOL lease = !r103FrontierSpent;
            pthread_mutex_unlock(&stateLock);
            if (loggedBlocked) {
                logLine("R103_FRONTIER_DISPATCH_RELEASED sourceSequence=%llu drawableID=%lu result=ADMITTED",
                    (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID);
            }
            return lease;
        }

        if (sourceSequence == r103FrontierPairB &&
            (!r102DeferredTicket || r102DeferredTicket.ticketId != r103FrontierTicketId)) {
            uint64_t ticketId = r103FrontierTicketId;
            pthread_mutex_unlock(&stateLock);
            r103RecoverFrontier(ticketId, "B-present-not-deferred");
            pthread_mutex_lock(&stateLock);
            continue;
        }

        uint64_t ticketId = r103FrontierTicketId;
        uint64_t pairA = r103FrontierPairA;
        uint64_t pairB = r103FrontierPairB;
        uint8_t frontierState = r103SourceFrontierState;
        if (!loggedBlocked) {
            ++r103FrontierBlockedDispatches;
            loggedBlocked = YES;
            pthread_mutex_unlock(&stateLock);
            logLine("R103_FRONTIER_DISPATCH_BLOCKED ticketId=%llu sourceSequence=%llu drawableID=%lu pair=%llu->%llu layer=%p device=%p frontierState=%u origin=SOURCE",
                (unsigned long long)ticketId,
                (unsigned long long)sourceSequence,
                (unsigned long)drawable.drawableID,
                (unsigned long long)pairA, (unsigned long long)pairB,
                (__bridge void *)layer, (__bridge void *)device,
                (unsigned)frontierState);
            pthread_mutex_lock(&stateLock);
        }
        int waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                                &stateLock, &deadline);
        if (waitResult == ETIMEDOUT) {
            ticketId = r103FrontierTicketId;
            pthread_mutex_unlock(&stateLock);
            r103RecoverFrontier(ticketId, "source-dispatch-recovery-bound");
            pthread_mutex_lock(&stateLock);
        }
    }
}

static void r103RecoverFrontier(uint64_t ticketId, const char *reason) {
    R102DeferredSourceTicket *ticket = nil;
    BOOL opened = NO;
    uint64_t pairA = 0, pairB = 0;
    void *layer = NULL, *device = NULL;

    pthread_mutex_lock(&stateLock);
    if (!r103FrontierSpent && r103SourceFrontierState != R103SourceFrontierOpen &&
        (!ticketId || ticketId == r103FrontierTicketId) &&
        r103SourceFrontierState != R103SourceFrontierReleasing) {
        pairA = r103FrontierPairA;
        pairB = r103FrontierPairB;
        layer = r103FrontierLayer;
        device = r103FrontierDevice;
        if (r102DeferredTicket &&
            r102DeferredTicket.ticketId == r103FrontierTicketId) {
            ticket = r102DeferredTicket;
        } else {
            ++r103FrontierFallbacks;
            r103SourceFrontierState = R103SourceFrontierOpen;
            r103FrontierSpent = YES;
            r103ClearFrontierLocked();
            pthread_cond_broadcast(&presentationReadyCondition);
            opened = YES;
        }
    }
    pthread_mutex_unlock(&stateLock);

    if (ticket) {
        logLine("R103_FRONTIER_RECOVERY ticketId=%llu pair=%llu->%llu action=FORWARD_RETAINED_B reason=%s",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB, reason ?: "unspecified");
        r102FallbackTicket(ticket, reason ?: "source-frontier-recovery-bound");
    } else if (opened) {
        logLine("R103_SOURCE_FRONTIER=OPEN ticketId=%llu pair=%llu->%llu layer=%p device=%p result=FALLBACK reason=%s",
            (unsigned long long)ticketId, (unsigned long long)pairA,
            (unsigned long long)pairB, layer, device,
            reason ?: "source-frontier-recovery-bound");
    }
}

static void r103CloseFrontierAfterProducerCommit(NSDictionary *record,
                                                 id<MTLCommandBuffer> producer) {
    if (!r103FrontierEnabled() || !record || !producer ||
        [record[@"sequence"] unsignedLongLongValue] != 4) return;

    id<CAMetalDrawable> drawable = record[@"drawable"];
    CAMetalLayer *layer = (CAMetalLayer *)drawable.layer;
    id<MTLDevice> device = drawable.texture.device;
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    if (!drawable || !layer || !isSelectedLayer(layer) ||
        producer != record[@"producerCommandBuffer"] ||
        producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError || producer.error ||
        !queue || queue.device != device || layer.device != device) {
        logLine("R103_FRONTIER_CLOSE_REJECTED sourceSequence=4 drawable=%p layer=%p device=%p producer=%p producerStatus=%lu queue=%p queueDevice=%p reason=producer-or-target-mismatch",
            (__bridge void *)drawable, (__bridge void *)layer,
            (__bridge void *)device, (__bridge void *)producer,
            (unsigned long)producer.status, (__bridge void *)queue,
            (__bridge void *)queue.device);
        return;
    }

    pthread_mutex_lock(&stateLock);
    if (r103FrontierSpent || r103SourceFrontierState != R103SourceFrontierOpen ||
        r102DeferredTicket || selectedHarnessLayer != (__bridge void *)layer) {
        uint8_t state = r103SourceFrontierState;
        BOOL activeTicket = r102DeferredTicket != nil;
        pthread_mutex_unlock(&stateLock);
        logLine("R103_FRONTIER_CLOSE_REJECTED sourceSequence=4 layer=%p device=%p frontierState=%u activeTicket=%d reason=frontier-not-open",
            (__bridge void *)layer, (__bridge void *)device, (unsigned)state,
            activeTicket);
        return;
    }
    r103SourceFrontierState = R103SourceFrontierClosingForPair;
    r103FrontierPairA = 3;
    r103FrontierPairB = 4;
    r103FrontierTicketId = ++r102NextTicketId;
    r103FrontierDisplayedBaseline = internalPresentationDisplayedCount;
    r103FrontierLayer = (__bridge void *)layer;
    r103FrontierDevice = (__bridge void *)device;
    r103FrontierProducer = (__bridge void *)producer;
    ++r103FrontierAttempts;
    uint64_t ticketId = r103FrontierTicketId;
    pthread_mutex_unlock(&stateLock);

    logLine("R103_SOURCE_FRONTIER=CLOSING_FOR_PAIR t_ns=%llu ticketId=%llu pair=3->4 sourceSequenceB=4 producerCommandBuffer=%p producerStatus=%lu layer=%p device=%p origin=SOURCE",
        (unsigned long long)r102MachTimeNs(), (unsigned long long)ticketId,
        (__bridge void *)producer, (unsigned long)producer.status,
        (__bridge void *)layer, (__bridge void *)device);

    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;
    pthread_mutex_lock(&stateLock);
    int waitResult = 0;
    while (r103SourceFrontierState == R103SourceFrontierClosingForPair &&
           r103FrontierTicketId == ticketId &&
           r103FrontierOperationsInFlight > 0 && waitResult == 0) {
        waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                            &stateLock, &deadline);
    }
    BOOL drained = r103SourceFrontierState == R103SourceFrontierClosingForPair &&
        r103FrontierTicketId == ticketId && r103FrontierOperationsInFlight == 0;
    uint64_t remainingOperations = r103FrontierOperationsInFlight;
    if (drained) {
        r103SourceFrontierState = R103SourceFrontierClosed;
        pthread_cond_broadcast(&presentationReadyCondition);
    } else if (r103SourceFrontierState == R103SourceFrontierClosingForPair &&
               r103FrontierTicketId == ticketId) {
        ++r103FrontierFallbacks;
        r103SourceFrontierState = R103SourceFrontierOpen;
        r103FrontierSpent = YES;
        r103ClearFrontierLocked();
        pthread_cond_broadcast(&presentationReadyCondition);
    }
    pthread_mutex_unlock(&stateLock);

    if (!drained) {
        logLine("R103_SOURCE_FRONTIER=OPEN ticketId=%llu pair=3->4 layer=%p device=%p result=FALLBACK reason=commit-close-drain-recovery-bound operationsInFlight=%llu",
            (unsigned long long)ticketId, (__bridge void *)layer,
            (__bridge void *)device, (unsigned long long)remainingOperations);
        return;
    }

    logLine("R103_SOURCE_FRONTIER=CLOSED t_ns=%llu ticketId=%llu pair=3->4 sourceSequenceB=4 producerCommandBuffer=%p producerStatus=%lu layer=%p device=%p operationsInFlight=0 origin=SOURCE",
        (unsigned long long)r102MachTimeNs(), (unsigned long long)ticketId,
        (__bridge void *)producer, (unsigned long)producer.status,
        (__bridge void *)layer, (__bridge void *)device);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r103RecoverFrontier(ticketId, "source-frontier-recovery-bound");
    });
}

static NSDictionary *r102RecordForCommandBuffer(id<MTLCommandBuffer> commandBuffer) {
    if (!commandBuffer) return nil;
    NSValue *key = [NSValue valueWithPointer:(__bridge const void *)commandBuffer];
    pthread_mutex_lock(&stateLock);
    NSDictionary *record = r102ProducerRecordsByCommandBuffer[key];
    pthread_mutex_unlock(&stateLock);
    return record;
}

static void logR102Event(const char *event, NSDictionary *record,
                         id<CAMetalDrawable> explicitDrawable,
                         id<MTLCommandBuffer> explicitCommandBuffer,
                         id encoder, SEL selector, double argument) {
    id<CAMetalDrawable> drawable = explicitDrawable ?: record[@"drawable"];
    id<MTLTexture> texture = drawable ? drawable.texture : record[@"texture"];
    id<MTLCommandBuffer> commandBuffer = explicitCommandBuffer ?: record[@"producerCommandBuffer"];
    id<MTLCommandQueue> queue = record[@"producerQueue"] ?: commandBuffer.commandQueue;
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    uint64_t drawableID = drawable ? drawable.drawableID :
        [record[@"drawableID"] unsignedLongLongValue];
    void *layer = drawable ? (__bridge void *)drawable.layer :
        [record[@"layer"] pointerValue];
    void *device = texture ? (__bridge void *)texture.device :
        [record[@"device"] pointerValue];
    logLine("R102_EVENT t_ns=%llu event=%s sourceSequence=%llu drawableID=%llu drawable=%p texture=%p commandBuffer=%p encoder=%p queue=%p layer=%p device=%p selector=%s argument=%.9f",
        (unsigned long long)r102MachTimeNs(), event,
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)drawable, (__bridge void *)texture,
        (__bridge void *)commandBuffer, (__bridge void *)encoder,
        (__bridge void *)queue, layer, device,
        selector ? sel_getName(selector) : "none", argument);
}

static void logR102DrawableEvent(const char *event, id<CAMetalDrawable> drawable,
                                 SEL selector, double argument) {
    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    if (!record && drawable) {
        record = @{@"drawable": drawable, @"texture": drawable.texture,
                   @"drawableID": @(drawable.drawableID),
                   @"layer": [NSValue valueWithPointer:(__bridge const void *)drawable.layer],
                   @"device": [NSValue valueWithPointer:(__bridge const void *)drawable.texture.device]};
    }
    id<MTLCommandBuffer> producer = record[@"producerCommandBuffer"];
    logR102Event(event, record, drawable, producer, nil, selector, argument);
}

static BOOL r102TicketIsCurrentLocked(R102DeferredSourceTicket *ticket) {
    return ticket && r102DeferredTicket == ticket &&
        ticket.state != R102DeferredTicketEmpty;
}

static void r102ClearPendingInternalLocked(uint64_t sourceSequence) {
    (void)sourceSequence;
    internalSlotState = R101InternalSlotEmpty;
    pendingInternalPresentationTexture = nil;
    pendingInternalPresentId = 0;
    pendingInternalPreviousSequence = 0;
    pendingInternalCurrentSequence = 0;
    pendingInternalLayer = NULL;
    pendingInternalDevice = NULL;
}

static void r102ForwardSourceTicket(R102DeferredSourceTicket *ticket,
                                    const char *reason) {
    BOOL frontierReleasing = NO;
    pthread_mutex_lock(&stateLock);
    BOOL shouldForward = r102TicketIsCurrentLocked(ticket) &&
        ticket.state != R102DeferredTicketForwardingSource;
    if (shouldForward) {
        ticket.state = R102DeferredTicketForwardingSource;
        if (reason) ++r102FallbackCount;
        if (r103FrontierEnabled() &&
            r103SourceFrontierState == R103SourceFrontierClosed &&
            r103FrontierTicketId == ticket.ticketId &&
            r103FrontierPairA == ticket.sourcePairA &&
            r103FrontierPairB == ticket.sourcePairB) {
            r103SourceFrontierState = R103SourceFrontierReleasing;
            frontierReleasing = YES;
        }
    } else if (ticket && ticket.state != R102DeferredTicketEmpty) {
        ++r102StaleCallbackCount;
    }
    pthread_mutex_unlock(&stateLock);
    if (!shouldForward) return;

    if (frontierReleasing) {
        logLine("R103_SOURCE_FRONTIER=RELEASING ticketId=%llu pair=%llu->%llu reason=%s producerCommandBuffer=%p producerStatus=%lu layer=%p device=%p",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            reason ?: "M-displayed", (__bridge void *)ticket.producerCommandBuffer,
            (unsigned long)ticket.producerCommandBuffer.status,
            (__bridge void *)ticket.layer, (__bridge void *)ticket.device);
    }

    logLine("R102_SOURCE_FORWARD_BEGIN ticketId=%llu appFrameId=%llu sourceSequence=%llu state=%u reason=%s",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned)ticket.state, reason ?: "none");

    if (reason) {
        logLine("R102_SOURCE_TICKET_FALLBACK ticketId=%llu appFrameId=%llu sourceSequence=%llu drawableID=%llu reason=%s producer=%p producerStatus=%lu pair=%llu->%llu",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.appFrameId,
            (unsigned long long)ticket.sourceSequence,
            (unsigned long long)ticket.drawableID, reason,
            (__bridge void *)ticket.producerCommandBuffer,
            (unsigned long)ticket.producerCommandBuffer.status,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB);
    }

    uint64_t wsiPresentId = allocateWsiPresentId();
    logLine("R102_SOURCE_FORWARD_STAGE ticketId=%llu stage=WSI_ID_ALLOCATED wsiPresentId=%llu",
        (unsigned long long)ticket.ticketId, (unsigned long long)wsiPresentId);
    NSDictionary *record = trackedDrawableForTexture(ticket.texture);
    logLine("R102_WSI_DISPATCH origin=SOURCE ticketId=%llu appFrameId=%llu wsiPresentId=%llu sourceSequence=%llu drawableID=%llu drawable=%p texture=%p layer=%p device=%p producerCommandBuffer=%p selector=%s argument=%.9f fallbackReason=%s",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)ticket.drawableID,
        (__bridge void *)ticket.drawable, (__bridge void *)ticket.texture,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        (__bridge void *)ticket.producerCommandBuffer,
        sel_getName(ticket.originalSelector), ticket.originalArgument,
        reason ?: "none");
    logLine("R102_EVENT t_ns=%llu event=SOURCE_B_WSI_DISPATCH ticketId=%llu sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu drawableID=%llu drawable=%p texture=%p commandBuffer=%p encoder=0 queue=%p layer=%p device=%p selector=%s argument=%.9f",
        (unsigned long long)r102MachTimeNs(),
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.drawableID,
        (__bridge void *)ticket.drawable, (__bridge void *)ticket.texture,
        (__bridge void *)ticket.producerCommandBuffer,
        (__bridge void *)ticket.producerCommandBuffer.commandQueue,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        sel_getName(ticket.originalSelector), ticket.originalArgument);
    logR102Event("SOURCE_B_ORIGINAL_PRESENT_CALL", record, ticket.drawable,
                 ticket.producerCommandBuffer, nil,
                 ticket.originalSelector, ticket.originalArgument);
    logLine("R102_SOURCE_FORWARD_STAGE ticketId=%llu stage=ORIGINAL_PRESENT_CALL wsiPresentId=%llu",
        (unsigned long long)ticket.ticketId, (unsigned long long)wsiPresentId);
    if (ticket.originalSelector == @selector(present)) {
        ((void (*)(id, SEL))ticket.originalImplementation)(ticket.drawable,
                                                            ticket.originalSelector);
    } else {
        ((void (*)(id, SEL, CFTimeInterval))ticket.originalImplementation)(
            ticket.drawable, ticket.originalSelector, ticket.originalArgument);
    }
    logR102Event("SOURCE_B_ORIGINAL_PRESENT_RETURN", record, ticket.drawable,
                 ticket.producerCommandBuffer, nil,
                 ticket.originalSelector, ticket.originalArgument);
    logLine("R102_SOURCE_FORWARD_STAGE ticketId=%llu stage=ORIGINAL_PRESENT_RETURN wsiPresentId=%llu",
        (unsigned long long)ticket.ticketId, (unsigned long long)wsiPresentId);

    BOOL frontierOpened = NO;
    BOOL frontierContiguous = NO;
    pthread_mutex_lock(&stateLock);
    if (r102TicketIsCurrentLocked(ticket)) {
        if (!reason) ++r102SuccessfulTupleCount;
        lastCompletedPresentationSequence = ticket.sourceSequence;
        r102ClearPendingInternalLocked(ticket.sourceSequence);
        ticket.state = R102DeferredTicketEmpty;
        r102DeferredTicket = nil;
        if (frontierReleasing &&
            r103SourceFrontierState == R103SourceFrontierReleasing &&
            r103FrontierTicketId == ticket.ticketId) {
            frontierContiguous = !reason &&
                internalPresentationDisplayedCount > r103FrontierDisplayedBaseline &&
                r103FutureSourceLeaks == 0;
            if (reason) ++r103FrontierFallbacks;
            if (frontierContiguous) ++r103FrontierContiguousSuccesses;
            r103SourceFrontierState = R103SourceFrontierOpen;
            r103FrontierSpent = YES;
            r103ClearFrontierLocked();
            frontierOpened = YES;
        }
        pthread_cond_broadcast(&presentationReadyCondition);
    }
    pthread_mutex_unlock(&stateLock);
    logLine("R102_SOURCE_TICKET_RELEASED ticketId=%llu sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu outcome=%s",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        reason ? "FALLBACK_FORWARDED" : "A_TO_M_TO_B_FORWARDED");
    if (frontierOpened) {
        logLine("R103_SOURCE_FRONTIER=OPEN ticketId=%llu pair=%llu->%llu reason=%s contiguousCandidate=%d futureSourceLeaks=%llu result=%s",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            reason ?: "B-original-present-returned", frontierContiguous,
            (unsigned long long)r103FutureSourceLeaks,
            reason ? "FALLBACK" : "FORWARDED");
    }
}

static void r102FallbackTicket(R102DeferredSourceTicket *ticket,
                               const char *reason) {
    r102ForwardSourceTicket(ticket, reason ?: "unspecified-recovery");
}

static void r102FallbackForSequence(uint64_t sourceSequence, const char *reason) {
    pthread_mutex_lock(&stateLock);
    R102DeferredSourceTicket *ticket =
        r102DeferredTicket.sourceSequence == sourceSequence ? r102DeferredTicket : nil;
    pthread_mutex_unlock(&stateLock);
    if (ticket) r102FallbackTicket(ticket, reason);
}

static BOOL r102TryDeferSourcePresent(id<CAMetalDrawable> drawable, SEL selector,
                                     CFTimeInterval argument) {
    const char *enabled = getenv("FGMETAL_R102_DEFER_SOURCE");
    if (!enabled || strcmp(enabled, "1") != 0 || isInternalPresentationCall ||
        !isSelectedLayer((CAMetalLayer *)drawable.layer)) return NO;
    if (selector != @selector(present) && selector != @selector(presentAtTime:) &&
        selector != @selector(presentAfterMinimumDuration:)) return NO;

    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    if (sequence != 4) return NO;
    id<MTLCommandBuffer> producer = record[@"producerCommandBuffer"];
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    IMP original = originalFor(drawable, selector);
    if (!producer || !queue || !original || producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError || producer.error ||
        queue.device != drawable.texture.device || drawable.layer.device != drawable.texture.device) {
        logLine("R102_SOURCE_DEFER_REJECTED sourceSequence=%llu drawableID=%lu reason=producer-or-target-not-eligible producer=%p producerStatus=%lu queue=%p queueDevice=%p layer=%p layerDevice=%p textureDevice=%p original=%p",
            (unsigned long long)sequence, (unsigned long)drawable.drawableID,
            (__bridge void *)producer, (unsigned long)producer.status,
            (__bridge void *)queue, (__bridge void *)queue.device,
            (__bridge void *)drawable.layer, (__bridge void *)drawable.layer.device,
            (__bridge void *)drawable.texture.device, original);
        return NO;
    }

    BOOL requireFrontier = r103FrontierEnabled();
    if (requireFrontier) {
        struct timespec deadline;
        clock_gettime(CLOCK_REALTIME, &deadline);
        deadline.tv_sec += 5;
        pthread_mutex_lock(&stateLock);
        int waitResult = 0;
        while (r103SourceFrontierState == R103SourceFrontierClosingForPair &&
               r103FrontierPairB == sequence && waitResult == 0) {
            waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                                &stateLock, &deadline);
        }
        BOOL frontierReady = r103SourceFrontierState == R103SourceFrontierClosed &&
            r103FrontierPairA == sequence - 1 && r103FrontierPairB == sequence &&
            r103FrontierLayer == (__bridge void *)drawable.layer &&
            r103FrontierDevice == (__bridge void *)drawable.texture.device &&
            r103FrontierProducer == (__bridge void *)producer &&
            !r103FrontierSpent;
        uint64_t frontierTicketId = r103FrontierTicketId;
        pthread_mutex_unlock(&stateLock);
        if (!frontierReady) {
            if (frontierTicketId)
                r103RecoverFrontier(frontierTicketId,
                    waitResult == ETIMEDOUT ? "B-present-frontier-close-timeout"
                                             : "B-present-frontier-not-closed");
            logLine("R103_FRONTIER_DEFER_REJECTED sourceSequence=%llu producer=%p layer=%p device=%p frontierTicketId=%llu waitResult=%d reason=source-frontier-not-closed",
                (unsigned long long)sequence, (__bridge void *)producer,
                (__bridge void *)drawable.layer,
                (__bridge void *)drawable.texture.device,
                (unsigned long long)frontierTicketId, waitResult);
            return NO;
        }
    }

    R102DeferredSourceTicket *ticket = [R102DeferredSourceTicket new];
    ticket.drawable = drawable;
    ticket.texture = drawable.texture;
    ticket.layer = (CAMetalLayer *)drawable.layer;
    ticket.device = drawable.texture.device;
    ticket.producerCommandBuffer = producer;
    ticket.originalSelector = selector;
    ticket.originalImplementation = original;
    ticket.originalArgument = argument;
    ticket.appFrameId = sequence;
    ticket.sourceSequence = sequence;
    ticket.drawableID = drawable.drawableID;
    ticket.sourcePairA = sequence - 1;
    ticket.sourcePairB = sequence;
    ticket.state = R102DeferredTicketDeferred;

    uint64_t failedFrontierTicketId = 0;
    pthread_mutex_lock(&stateLock);
    if (r102DeferredTicket || internalSlotState == R101InternalSlotClaimed ||
        internalSlotState == R101InternalSlotPresented) {
        failedFrontierTicketId = requireFrontier ? r103FrontierTicketId : 0;
        pthread_mutex_unlock(&stateLock);
        if (failedFrontierTicketId)
            r103RecoverFrontier(failedFrontierTicketId,
                                "deferred-ticket-collision");
        return NO;
    }
    if (requireFrontier) {
        BOOL frontierReady = r103SourceFrontierState == R103SourceFrontierClosed &&
            r103FrontierPairA == sequence - 1 && r103FrontierPairB == sequence &&
            r103FrontierLayer == (__bridge void *)drawable.layer &&
            r103FrontierDevice == (__bridge void *)drawable.texture.device &&
            r103FrontierProducer == (__bridge void *)producer &&
            !r103FrontierSpent;
        if (!frontierReady) {
            failedFrontierTicketId = r103FrontierTicketId;
            pthread_mutex_unlock(&stateLock);
            if (failedFrontierTicketId)
                r103RecoverFrontier(failedFrontierTicketId,
                                    "frontier-changed-before-ticket-publication");
            logLine("R103_FRONTIER_DEFER_REJECTED sourceSequence=%llu producer=%p layer=%p device=%p frontierTicketId=%llu reason=frontier-publication-race",
                (unsigned long long)sequence, (__bridge void *)producer,
                (__bridge void *)drawable.layer,
                (__bridge void *)drawable.texture.device,
                (unsigned long long)failedFrontierTicketId);
            return NO;
        }
        ticket.ticketId = r103FrontierTicketId;
    } else {
        ticket.ticketId = ++r102NextTicketId;
    }
    r102DeferredTicket = ticket;
    ++presentationAttempts;
    pthread_mutex_unlock(&stateLock);

    logLine("R102_SOURCE_TICKET=DEFERRED ticketId=%llu appFrameId=%llu sourceSequence=%llu drawableID=%llu drawable=%p texture=%p layer=%p device=%p selector=%s argument=%.9f producerCommandBuffer=%p producerQueue=%p producerStatus=%lu pair=%llu->%llu wsiPresentId=UNALLOCATED state=DEFERRED frontierClosed=%d origin=SOURCE",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)ticket.drawableID,
        (__bridge void *)ticket.drawable, (__bridge void *)ticket.texture,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        sel_getName(ticket.originalSelector), ticket.originalArgument,
        (__bridge void *)ticket.producerCommandBuffer,
        (__bridge void *)queue, (unsigned long)producer.status,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        requireFrontier ? 1 : 0);
    logR102Event("SOURCE_PRESENT_DEFERRED", record, drawable, producer,
                 nil, selector, argument);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r102FallbackTicket(ticket, "no-matching-M-before-recovery-deadline");
    });
    return YES;
}

static void r102WaitForNextDrawableGate(uint64_t callId, CAMetalLayer *layer) {
    if (isInternalDrawableAcquisition || isInternalPresentationCall) return;
    pthread_mutex_lock(&stateLock);
    R102DeferredSourceTicket *ticket = r102DeferredTicket;
    if (!ticket || ticket.layer != layer || ticket.state == R102DeferredTicketEmpty) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    uint64_t ticketId = ticket.ticketId;
    pthread_mutex_unlock(&stateLock);
    logLine("R102_NEXTDRAWABLE_GATE=BLOCKED callId=%llu ticketId=%llu sourceSequenceB=%llu layer=%p producerCommandBuffer=%p producerStatus=%lu producerWasCommittedBeforeGate=1 boundMs=5000",
        (unsigned long long)callId, (unsigned long long)ticketId,
        (unsigned long long)ticket.sourceSequence,
        (__bridge void *)layer, (__bridge void *)ticket.producerCommandBuffer,
        (unsigned long)ticket.producerCommandBuffer.status);

    pthread_mutex_lock(&stateLock);
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;
    int waitResult = 0;
    while (r102TicketIsCurrentLocked(ticket) &&
           ticket.state != R102DeferredTicketEmpty && waitResult == 0) {
        waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                            &stateLock, &deadline);
    }
    BOOL timedOut = r102TicketIsCurrentLocked(ticket) &&
        ticket.state != R102DeferredTicketEmpty;
    pthread_mutex_unlock(&stateLock);
    if (timedOut) r102FallbackTicket(ticket, "next-drawable-gate-recovery-bound");
    logLine("R102_NEXTDRAWABLE_GATE=RELEASED callId=%llu ticketId=%llu timedOut=%d producerStatus=%lu",
        (unsigned long long)callId, (unsigned long long)ticketId, timedOut,
        (unsigned long)ticket.producerCommandBuffer.status);
}

static void r102InternalPresented(R102DeferredSourceTicket *ticket,
                                  id<MTLDrawable> drawable,
                                  uint64_t internalPresentId,
                                  uint64_t wsiPresentId) {
    BOOL reachedDisplay = drawable.presentedTime > 0.0;
    if (reachedDisplay) {
        pthread_mutex_lock(&stateLock);
        ++internalPresentationDisplayedCount;
        pthread_mutex_unlock(&stateLock);
    }
    logLine("R102_EVENT t_ns=%llu event=INTERNAL_DISPLAYED_CALLBACK ticketId=%llu sourceSequence=%llu internalPresentId=%llu wsiPresentId=%llu drawableID=%lu drawable=%p texture=%p commandBuffer=0 encoder=0 queue=0 layer=%p device=%p selector=addPresentedHandler: argument=%.9f reachedDisplay=%d",
        (unsigned long long)r102MachTimeNs(),
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)internalPresentId,
        (unsigned long long)wsiPresentId,
        (unsigned long)drawable.drawableID, (__bridge void *)drawable,
        (__bridge void *)((id<CAMetalDrawable>)drawable).texture,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        drawable.presentedTime, reachedDisplay);
    logLine("R102_INTERNAL_PRESENTED_CALLBACK ticketId=%llu internalPresentId=%llu wsiPresentId=%llu sourcePair=%llu->%llu drawableID=%lu drawable=%p layer=%p device=%p presentedTime=%.9f reachedDisplay=%d",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (unsigned long)drawable.drawableID, (__bridge void *)drawable,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        drawable.presentedTime, reachedDisplay);
    if (!reachedDisplay) {
        r102FallbackTicket(ticket, "internal-presented-callback-not-displayed");
        return;
    }
    pthread_mutex_lock(&stateLock);
    BOOL current = r102TicketIsCurrentLocked(ticket) &&
        ticket.state != R102DeferredTicketForwardingSource;
    void *currentTicketPointer = (__bridge void *)r102DeferredTicket;
    R102DeferredTicketState ticketState = ticket.state;
    if (!current) ++r102StaleCallbackCount;
    pthread_mutex_unlock(&stateLock);
    logLine("R102_INTERNAL_CALLBACK_STATE ticketId=%llu ticket=%p currentTicket=%p ticketState=%u current=%d reachedDisplay=%d",
        (unsigned long long)ticket.ticketId, (__bridge void *)ticket,
        currentTicketPointer, (unsigned)ticketState, current, reachedDisplay);
    if (current) r102ForwardSourceTicket(ticket, NULL);
}

static void r102AttemptInternalDispatch(void) {
    R102DeferredSourceTicket *ticket = nil;
    id<MTLTexture> generatedTexture = nil;
    uint64_t internalPresentId = 0;
    double expectedMainX = NAN, expectedMarkerX = NAN;
    pthread_mutex_lock(&stateLock);
    ticket = r102DeferredTicket;
    if (!ticket || ticket.state != R102DeferredTicketDeferred) {
        if (ticket && (ticket.state == R102DeferredTicketInternalDispatching ||
                       ticket.state == R102DeferredTicketWaitingForInternalDisplay ||
                       ticket.state == R102DeferredTicketForwardingSource))
            ++r102DuplicateInternalDispatchCount;
        pthread_mutex_unlock(&stateLock);
        return;
    }
    if (internalSlotState != R101InternalSlotReady ||
        pendingInternalPreviousSequence != ticket.sourcePairA ||
        pendingInternalCurrentSequence != ticket.sourcePairB ||
        pendingInternalLayer != (__bridge void *)ticket.layer ||
        pendingInternalDevice != (__bridge void *)ticket.device ||
        !pendingInternalPresentationTexture ||
        pendingInternalPresentationTexture.device != ticket.device) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    generatedTexture = pendingInternalPresentationTexture;
    internalPresentId = pendingInternalPresentId;
    expectedMainX = pendingExpectedMainX;
    expectedMarkerX = pendingExpectedMarkerX;
    internalSlotState = R101InternalSlotClaimed;
    ticket.state = R102DeferredTicketInternalDispatching;
    pthread_mutex_unlock(&stateLock);

    if (ticket.producerCommandBuffer.status != MTLCommandBufferStatusCompleted ||
        ticket.producerCommandBuffer.error) {
        r102FallbackTicket(ticket, "producer-not-completed-before-internal-dispatch");
        return;
    }
    id<MTLCommandQueue> queue = ticket.producerCommandBuffer.commandQueue;
    if (!queue || queue.device != ticket.device || ticket.layer.device != ticket.device) {
        r102FallbackTicket(ticket, "internal-queue-layer-device-mismatch");
        return;
    }

    logLine("R102_INTERNAL_DRAWABLE_ACQUIRE_BEGIN ticketId=%llu origin=INTERNAL internalPresentId=%llu sourcePair=%llu->%llu layer=%p device=%p",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device);
    isInternalDrawableAcquisition = YES;
    isInternalPresentationCall = YES;
    id<CAMetalDrawable> internalDrawable = originalNextDrawable
        ? ((id (*)(id, SEL))originalNextDrawable)(ticket.layer, @selector(nextDrawable)) : nil;
    isInternalPresentationCall = NO;
    isInternalDrawableAcquisition = NO;
    if (!internalDrawable || internalDrawable.layer != ticket.layer ||
        internalDrawable.texture.device != ticket.device) {
        logLine("R102_INTERNAL_DRAWABLE_ACQUIRE=FAILED ticketId=%llu drawable=%p layer=%p expectedLayer=%p device=%p expectedDevice=%p",
            (unsigned long long)ticket.ticketId, (__bridge void *)internalDrawable,
            (__bridge void *)internalDrawable.layer, (__bridge void *)ticket.layer,
            (__bridge void *)internalDrawable.texture.device,
            (__bridge void *)ticket.device);
        r102FallbackTicket(ticket, "internal-drawable-unavailable-or-device-mismatch");
        return;
    }
    logLine("R102_INTERNAL_DRAWABLE_ACQUIRED ticketId=%llu origin=INTERNAL drawable=%p drawableID=%lu texture=%p layer=%p device=%p size=%lux%lu pixelFormat=%lu framebufferOnly=%d usage=%lu",
        (unsigned long long)ticket.ticketId, (__bridge void *)internalDrawable,
        (unsigned long)internalDrawable.drawableID,
        (__bridge void *)internalDrawable.texture,
        (__bridge void *)internalDrawable.layer,
        (__bridge void *)internalDrawable.texture.device,
        (unsigned long)internalDrawable.texture.width,
        (unsigned long)internalDrawable.texture.height,
        (unsigned long)internalDrawable.texture.pixelFormat,
        internalDrawable.texture.framebufferOnly,
        (unsigned long)internalDrawable.texture.usage);

    BOOL previousInternalCall = isInternalPresentationCall;
    isInternalPresentationCall = YES;
    BOOL internalCopySucceeded = renderInternalFrameToDrawable(
        generatedTexture, internalDrawable, queue, internalPresentId,
        expectedMainX, expectedMarkerX);
    isInternalPresentationCall = previousInternalCall;
    if (!internalCopySucceeded) {
        logLine("R102_INTERNAL_COPY=FAILED ticketId=%llu internalPresentId=%llu sourcePair=%llu->%llu reason=copy-or-visual-check-failed",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)internalPresentId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB);
        r102FallbackTicket(ticket, "internal-copy-or-visual-validation-failed");
        return;
    }

    installDrawablePresentationHooks(internalDrawable);
    IMP presentOriginal = originalFor(internalDrawable, @selector(present));
    if (!presentOriginal || ![internalDrawable respondsToSelector:@selector(addPresentedHandler:)]) {
        r102FallbackTicket(ticket, "internal-present-method-or-displayed-callback-unavailable");
        return;
    }

    __block uint64_t internalWsiPresentId = 0;
    [internalDrawable addPresentedHandler:^(id<MTLDrawable> presented) {
        r102InternalPresented(ticket, presented, internalPresentId,
                              internalWsiPresentId);
    }];
    pthread_mutex_lock(&stateLock);
    if (r102TicketIsCurrentLocked(ticket)) {
        ticket.state = R102DeferredTicketWaitingForInternalDisplay;
        internalSlotState = R101InternalSlotPresented;
    }
    pthread_mutex_unlock(&stateLock);
    internalWsiPresentId = allocateWsiPresentId();
    logLine("R102_WSI_DISPATCH origin=INTERNAL ticketId=%llu internalPresentId=%llu wsiPresentId=%llu appFrameId=NONE sourcePair=%llu->%llu drawable=%p drawableID=%lu texture=%p layer=%p device=%p queue=%p",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)internalWsiPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (__bridge void *)internalDrawable,
        (unsigned long)internalDrawable.drawableID,
        (__bridge void *)internalDrawable.texture,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        (__bridge void *)queue);
    logLine("R102_EVENT t_ns=%llu event=INTERNAL_WSI_DISPATCH ticketId=%llu sourceSequence=%llu appFrameId=NONE internalPresentId=%llu wsiPresentId=%llu sourcePair=%llu->%llu drawableID=%lu drawable=%p texture=%p commandBuffer=0 encoder=0 queue=%p layer=%p device=%p selector=present argument=0.000000000",
        (unsigned long long)r102MachTimeNs(),
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)internalPresentId,
        (unsigned long long)internalWsiPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (unsigned long)internalDrawable.drawableID,
        (__bridge void *)internalDrawable,
        (__bridge void *)internalDrawable.texture,
        (__bridge void *)queue, (__bridge void *)ticket.layer,
        (__bridge void *)ticket.device);
    isInternalPresentationCall = YES;
    ((void (*)(id, SEL))presentOriginal)(internalDrawable, @selector(present));
    isInternalPresentationCall = NO;
    pthread_mutex_lock(&stateLock);
    ++internalPresentationSuccesses;
    pthread_mutex_unlock(&stateLock);
    logLine("R102_INTERNAL_PRESENT_DISPATCH_RETURN ticketId=%llu internalPresentId=%llu wsiPresentId=%llu drawableID=%lu state=WAITING_FOR_INTERNAL_DISPLAY",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)internalWsiPresentId,
        (unsigned long)internalDrawable.drawableID);
}

static BOOL verifyHarnessPixels(id<MTLTexture> texture, const uint8_t *bytes,
                                size_t bytesPerRow, ColorStats *mainShape,
                                ColorStats *marker, BOOL *backgroundMatched);

static BOOL renderInternalFrameToDrawable(id<MTLTexture> source,
                                          id<CAMetalDrawable> destinationDrawable,
                                          id<MTLCommandQueue> queue,
                                          uint64_t internalPresentId,
                                          double expectedMainX,
                                          double expectedMarkerX) {
    id<MTLTexture> destination = destinationDrawable.texture;
    if (!source || !destination || !queue ||
        source.device != destination.device || source.device != queue.device ||
        source.width != destination.width || source.height != destination.height ||
        source.pixelFormat != MTLPixelFormatRGBA16Float ||
        destination.pixelFormat != MTLPixelFormatBGRA8Unorm ||
        destination.framebufferOnly ||
        (source.usage & MTLTextureUsageShaderRead) == 0 ||
        (destination.usage & MTLTextureUsageRenderTarget) == 0 ||
        !internalPresentationPipeline) {
        logLine("INTERNAL_PRESENT_PREFLIGHT=FAILED source=%p destination=%p sourceDevice=%p destinationDevice=%p queueDevice=%p sourceSize=%lux%lu destinationSize=%lux%lu sourceFormat=%lu destinationFormat=%lu destinationFramebufferOnly=%d sourceUsage=%lu destinationUsage=%lu pipeline=%p",
            (__bridge void *)source, (__bridge void *)destination,
            (__bridge void *)source.device, (__bridge void *)destination.device,
            (__bridge void *)queue.device, (unsigned long)source.width,
            (unsigned long)source.height, (unsigned long)destination.width,
            (unsigned long)destination.height, (unsigned long)source.pixelFormat,
            (unsigned long)destination.pixelFormat, destination.framebufferOnly,
            (unsigned long)source.usage, (unsigned long)destination.usage,
            (__bridge void *)internalPresentationPipeline);
        return NO;
    }

    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = destination;
    pass.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLCommandBuffer> commandBuffer = [queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = commandBuffer
        ? [commandBuffer renderCommandEncoderWithDescriptor:pass] : nil;
    if (!commandBuffer || !encoder) {
        logLine("INTERNAL_PRESENT_ENCODER=FAILED drawable=%p queue=%p commandBuffer=%p encoder=%p",
            (__bridge void *)destinationDrawable, (__bridge void *)queue,
            (__bridge void *)commandBuffer, (__bridge void *)encoder);
        return NO;
    }
    [encoder setRenderPipelineState:internalPresentationPipeline];
    [encoder setFragmentTexture:source atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    MTLTextureDescriptor *readbackDescriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:destination.pixelFormat
        width:destination.width height:destination.height mipmapped:NO];
    readbackDescriptor.storageMode = MTLStorageModeShared;
    readbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> destinationReadback =
        [destination.device newTextureWithDescriptor:readbackDescriptor];
    id<MTLBlitCommandEncoder> verifyBlit = destinationReadback
        ? [commandBuffer blitCommandEncoder] : nil;
    if (!destinationReadback || !verifyBlit) {
        logLine("R101_INTERNAL_DESTINATION_READBACK=FAILED drawableID=%lu texture=%p readback=%p blit=%p",
            (unsigned long)destinationDrawable.drawableID,
            (__bridge void *)destination, (__bridge void *)destinationReadback,
            (__bridge void *)verifyBlit);
        return NO;
    }
    [verifyBlit copyFromTexture:destination toTexture:destinationReadback];
    [verifyBlit endEncoding];
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    BOOL completed = commandBuffer.status == MTLCommandBufferStatusCompleted &&
        commandBuffer.error == nil;
    logLine("INTERNAL_PRESENT_RENDER=%s drawable=%p drawableID=%lu layer=%p sourceTexture=%p destinationTexture=%p commandBuffer=%p queue=%p device=%p status=%lu error=%s",
        completed ? "COMPLETED" : "FAILED", (__bridge void *)destinationDrawable,
        (unsigned long)destinationDrawable.drawableID,
        (__bridge void *)destinationDrawable.layer, (__bridge void *)source,
        (__bridge void *)destination, (__bridge void *)commandBuffer,
        (__bridge void *)queue, (__bridge void *)queue.device,
        (unsigned long)commandBuffer.status,
        commandBuffer.error.localizedDescription.UTF8String ?: "none");
    if (!completed) return NO;
    const size_t bytesPerRow = (size_t)destinationReadback.width * 4;
    NSMutableData *bytes = [NSMutableData dataWithLength:
        bytesPerRow * (size_t)destinationReadback.height];
    [destinationReadback getBytes:bytes.mutableBytes bytesPerRow:bytesPerRow
        fromRegion:MTLRegionMake2D(0, 0, destinationReadback.width,
                                   destinationReadback.height) mipmapLevel:0];
    ColorStats mainShape = {0}, markerShape = {0};
    BOOL backgroundMatched = NO;
    BOOL readable = verifyHarnessPixels(destinationReadback,
        (const uint8_t *)bytes.bytes, bytesPerRow, &mainShape, &markerShape,
        &backgroundMatched);
    double mainX = mainShape.count ?
        (double)mainShape.sumX / mainShape.count : NAN;
    double markerX = markerShape.count ?
        (double)markerShape.sumX / markerShape.count : NAN;
    uint64_t hash = UINT64_C(1469598103934665603);
    const uint8_t *pixelBytes = (const uint8_t *)bytes.bytes;
    for (NSUInteger i = 0; i < bytes.length; ++i) {
        hash ^= pixelBytes[i];
        hash *= UINT64_C(1099511628211);
    }
    BOOL intermediate = readable && backgroundMatched &&
        fabs(mainX - expectedMainX) <= 3.0 &&
        fabs(markerX - expectedMarkerX) <= 3.0;
    uint8_t alphaMin = 255, alphaMax = 0;
    uint64_t alphaSum = 0;
    for (NSUInteger i = 3; i < bytes.length; i += 4) {
        uint8_t alpha = pixelBytes[i];
        if (alpha < alphaMin) alphaMin = alpha;
        if (alpha > alphaMax) alphaMax = alpha;
        alphaSum += alpha;
    }
    logLine("R101_INTERNAL_DESTINATION_READBACK=%s internalPresentId=%llu drawableID=%lu texture=%p readback=%p hash=%016llx backgroundMatched=%d mainPixels=%llu mainCentroidX=%.3f expectedMainX=%.3f markerPixels=%llu markerCentroidX=%.3f expectedMarkerX=%.3f alphaMin=%u alphaMax=%u alphaMean=%.3f",
        intermediate ? "INTERMEDIATE_IMAGE_VALIDATED" : "MISMATCH",
        (unsigned long long)internalPresentId,
        (unsigned long)destinationDrawable.drawableID,
        (__bridge void *)destination, (__bridge void *)destinationReadback,
        (unsigned long long)hash, backgroundMatched,
        (unsigned long long)mainShape.count, mainX, expectedMainX,
        (unsigned long long)markerShape.count, markerX, expectedMarkerX,
        (unsigned)alphaMin, (unsigned)alphaMax,
        (double)alphaSum / (double)(bytes.length / 4));
    if (!intermediate) return NO;
    return completed;
}

static void hookDrawablePresent(id self, SEL selector) {
    if (isSelectedProcess()) {
        logR102DrawableEvent("SOURCE_PRESENT_HOOK_ENTRY", self, selector, 0.0);
        if (r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, 0.0)) return;
    }
    BOOL dispatchLease = r103BeginSourceDispatch((id<CAMetalDrawable>)self);
    uint64_t wsiPresentId = logSourceWsiDispatch(self, selector);
    if (wsiPresentId) {
        id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
        NSDictionary *record = trackedDrawableForTexture(drawable.texture);
        logLine("R102_EVENT t_ns=%llu event=SOURCE_WSI_DISPATCH sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu drawableID=%lu drawable=%p texture=%p commandBuffer=%p encoder=0 queue=%p layer=%p device=%p selector=%s argument=0.000000000",
            (unsigned long long)r102MachTimeNs(),
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)wsiPresentId, (unsigned long)drawable.drawableID,
            (__bridge void *)drawable, (__bridge void *)drawable.texture,
            (__bridge void *)record[@"producerCommandBuffer"],
            (__bridge void *)record[@"producerQueue"],
            (__bridge void *)drawable.layer, (__bridge void *)drawable.texture.device,
            sel_getName(selector));
    }
    IMP original = originalFor(self, selector);
    if (original) {
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_CALL", self, selector, 0.0);
        ((void (*)(id, SEL))original)(self, selector);
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_RETURN", self, selector, 0.0);
    }
    r103EndSourceOperation(dispatchLease, "present", wsiPresentId);
}

static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when) {
    if (isSelectedProcess()) {
        logR102DrawableEvent("SOURCE_PRESENT_HOOK_ENTRY", self, selector, when);
        if (r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, when)) return;
    }
    BOOL dispatchLease = r103BeginSourceDispatch((id<CAMetalDrawable>)self);
    uint64_t wsiPresentId = logSourceWsiDispatch(self, selector);
    if (wsiPresentId) {
        id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
        NSDictionary *record = trackedDrawableForTexture(drawable.texture);
        logLine("R102_EVENT t_ns=%llu event=SOURCE_WSI_DISPATCH sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu drawableID=%lu drawable=%p texture=%p commandBuffer=%p encoder=0 queue=%p layer=%p device=%p selector=%s argument=%.9f",
            (unsigned long long)r102MachTimeNs(),
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)wsiPresentId, (unsigned long)drawable.drawableID,
            (__bridge void *)drawable, (__bridge void *)drawable.texture,
            (__bridge void *)record[@"producerCommandBuffer"],
            (__bridge void *)record[@"producerQueue"],
            (__bridge void *)drawable.layer, (__bridge void *)drawable.texture.device,
            sel_getName(selector), when);
    }
    IMP original = originalFor(self, selector);
    if (original) {
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_CALL", self, selector, when);
        ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_RETURN", self, selector, when);
    }
    r103EndSourceOperation(dispatchLease, "presentAtTime", wsiPresentId);
}

static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration) {
    if (isSelectedProcess()) {
        logR102DrawableEvent("SOURCE_PRESENT_HOOK_ENTRY", self, selector, duration);
        if (r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, duration)) return;
    }
    BOOL dispatchLease = r103BeginSourceDispatch((id<CAMetalDrawable>)self);
    uint64_t wsiPresentId = logSourceWsiDispatch(self, selector);
    if (wsiPresentId) {
        id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
        NSDictionary *record = trackedDrawableForTexture(drawable.texture);
        logLine("R102_EVENT t_ns=%llu event=SOURCE_WSI_DISPATCH sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu drawableID=%lu drawable=%p texture=%p commandBuffer=%p encoder=0 queue=%p layer=%p device=%p selector=%s argument=%.9f",
            (unsigned long long)r102MachTimeNs(),
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)wsiPresentId, (unsigned long)drawable.drawableID,
            (__bridge void *)drawable, (__bridge void *)drawable.texture,
            (__bridge void *)record[@"producerCommandBuffer"],
            (__bridge void *)record[@"producerQueue"],
            (__bridge void *)drawable.layer, (__bridge void *)drawable.texture.device,
            sel_getName(selector), duration);
    }
    IMP original = originalFor(self, selector);
    if (original) {
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_CALL", self, selector, duration);
        ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
        if (isSelectedProcess()) logR102DrawableEvent("SOURCE_ORIGINAL_PRESENT_RETURN", self, selector, duration);
    }
    r103EndSourceOperation(dispatchLease, "presentAfterMinimumDuration", wsiPresentId);
}

static void installDrawablePresentationHooks(id<CAMetalDrawable> drawable) {
    Protocol *drawableProtocol = objc_getProtocol("MTLDrawable");
    Class cls = object_getClass(drawable);
    while (cls) {
        if (!drawableProtocol || class_conformsToProtocol(cls, drawableProtocol)) {
            installHook(cls, @selector(present), (IMP)hookDrawablePresent);
            installHook(cls, @selector(presentAtTime:), (IMP)hookDrawablePresentAtTime);
            installHook(cls, @selector(presentAfterMinimumDuration:),
                        (IMP)hookDrawablePresentDuration);
        }
        cls = class_getSuperclass(cls);
    }
}

static uint64_t allocateWsiPresentId(void) {
    pthread_mutex_lock(&stateLock);
    uint64_t identity = nextWsiPresentId++;
    pthread_mutex_unlock(&stateLock);
    return identity;
}

static void observeDisplayedDrawable(id<MTLDrawable> drawable,
                                     const char *origin,
                                     uint64_t wsiPresentId,
                                     uint64_t appFrameId,
                                     uint64_t internalPresentId) {
    if (![drawable respondsToSelector:@selector(addPresentedHandler:)]) return;
    [drawable addPresentedHandler:^(id<MTLDrawable> presented) {
        BOOL reachedDisplay = presented.presentedTime > 0.0;
        if (internalPresentId != 0 && reachedDisplay) {
            pthread_mutex_lock(&stateLock);
            ++internalPresentationDisplayedCount;
            pthread_mutex_unlock(&stateLock);
        }
        logLine("R101_WSI_DISPLAYED origin=%s wsiPresentId=%llu appFrameId=%llu internalPresentId=%llu drawableID=%lu presentedTime=%.9f reachedDisplay=%d",
            origin, (unsigned long long)wsiPresentId,
            (unsigned long long)appFrameId,
            (unsigned long long)internalPresentId,
            (unsigned long)presented.drawableID, presented.presentedTime,
            reachedDisplay);
    }];
}

static uint64_t logSourceWsiDispatch(id self, SEL selector) {
    if (!isSelectedProcess() || isInternalPresentationCall) return 0;
    id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
    if (!isSelectedLayer((CAMetalLayer *)drawable.layer)) return 0;
    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    uint64_t appFrameId = [record[@"sequence"] unsignedLongLongValue];
    pthread_mutex_lock(&stateLock);
    BOOL futureSourceAtClosedFrontier =
        r103FrontierTargetsLayerLocked((CAMetalLayer *)drawable.layer,
                                       drawable.texture.device) &&
        r103SourceFrontierState == R103SourceFrontierClosed &&
        appFrameId != r103FrontierPairB;
    uint64_t frontierTicketId = r103FrontierTicketId;
    if (futureSourceAtClosedFrontier) ++r103FutureSourceLeaks;
    pthread_mutex_unlock(&stateLock);
    if (futureSourceAtClosedFrontier) {
        logLine("R103_FUTURE_SOURCE_LEAK=DETECTED sourceSequence=%llu appFrameId=%llu drawableID=%lu ticketId=%llu reason=closed-frontier-reached-wsi-admission",
            (unsigned long long)appFrameId, (unsigned long long)appFrameId,
            (unsigned long)drawable.drawableID,
            (unsigned long long)frontierTicketId);
        r103RecoverFrontier(frontierTicketId, "source-wsi-gate-invariant");
        return logSourceWsiDispatch(self, selector);
    }
    uint64_t wsiPresentId = allocateWsiPresentId();
    logLine("R101_WSI_DISPATCH origin=SOURCE appFrameId=%llu wsiPresentId=%llu drawableID=%lu layer=%p texture=%p selector=%s",
        (unsigned long long)appFrameId, (unsigned long long)wsiPresentId,
        (unsigned long)drawable.drawableID, (__bridge void *)drawable.layer,
        (__bridge void *)drawable.texture, sel_getName(selector));
    observeDisplayedDrawable(drawable, "SOURCE", wsiPresentId, appFrameId, 0);
    return wsiPresentId;
}

static void releaseClaimedSlot(uint64_t sourceSequence, const char *reason) {
    pthread_mutex_lock(&stateLock);
    if (internalSlotState == R101InternalSlotClaimed &&
        pendingInternalCurrentSequence == sourceSequence) {
        internalSlotState = R101InternalSlotEmpty;
        pendingInternalPresentationTexture = nil;
        pendingInternalLayer = NULL;
        pendingInternalDevice = NULL;
        ++internalPresentationFallbacks;
        lastCompletedPresentationSequence = sourceSequence;
        pthread_cond_broadcast(&presentationReadyCondition);
    }
    pthread_mutex_unlock(&stateLock);
    logLine("R101_INTERNAL_PRESENT=FALLBACK appFrameIdB=%llu reason=%s",
        (unsigned long long)sourceSequence, reason);
}

static BOOL nearByte(uint8_t actual, uint8_t expected) {
    return abs((int)actual - (int)expected) <= 6;
}

static BOOL verifyHarnessPixels(id<MTLTexture> texture, const uint8_t *bytes,
                                size_t bytesPerRow, ColorStats *mainShape,
                                ColorStats *marker, BOOL *backgroundMatched) {
    BOOL bgra = texture.pixelFormat == MTLPixelFormatBGRA8Unorm;
    BOOL rgba = texture.pixelFormat == MTLPixelFormatRGBA8Unorm;
    if (!bgra && !rgba) return NO;
    memset(mainShape, 0, sizeof(*mainShape));
    memset(marker, 0, sizeof(*marker));
    mainShape->minX = mainShape->minY = UINT32_MAX;
    marker->minX = marker->minY = UINT32_MAX;

    uint32_t sx = (uint32_t)texture.width - 8;
    uint32_t sy = (uint32_t)texture.height - 8;
    const uint8_t *background = bytes + (size_t)sy * bytesPerRow + (size_t)sx * 4;
    *backgroundMatched = bgra
        ? nearByte(background[0], 10) && nearByte(background[1], 5) && nearByte(background[2], 3)
        : nearByte(background[0], 3) && nearByte(background[1], 5) && nearByte(background[2], 10);

    for (uint32_t y = 0; y < texture.height; ++y) {
        const uint8_t *row = bytes + (size_t)y * bytesPerRow;
        for (uint32_t x = 0; x < texture.width; ++x) {
            const uint8_t *pixel = row + (size_t)x * 4;
            BOOL mainPixel = bgra
                ? pixel[0] > 220 && pixel[1] > 160 && pixel[2] < 80
                : pixel[0] < 80 && pixel[1] > 160 && pixel[2] > 220;
            BOOL markerPixel = bgra
                ? pixel[0] < 80 && pixel[1] > 100 && pixel[2] > 220
                : pixel[0] > 220 && pixel[1] > 100 && pixel[2] < 80;
            ColorStats *stats = mainPixel ? mainShape : (markerPixel ? marker : NULL);
            if (!stats) continue;
            ++stats->count;
            stats->sumX += x;
            if (x < stats->minX) stats->minX = x;
            if (x > stats->maxX) stats->maxX = x;
            if (y < stats->minY) stats->minY = y;
            if (y > stats->maxY) stats->maxY = y;
        }
    }
    return *backgroundMatched && mainShape->count > 0 && marker->count > 0;
}

static uint16_t floatToHalf(float value) {
    uint32_t bits = 0;
    memcpy(&bits, &value, sizeof(bits));
    const uint16_t sign = (uint16_t)((bits >> 16) & 0x8000u);
    int exponent = (int)((bits >> 23) & 0xffu) - 127 + 15;
    uint32_t mantissa = bits & 0x7fffffu;
    if (exponent <= 0) {
        if (exponent < -10) return sign;
        mantissa = (mantissa | 0x800000u) >> (1 - exponent);
        return (uint16_t)(sign | ((mantissa + 0x1000u) >> 13));
    }
    if (exponent >= 31) return (uint16_t)(sign | 0x7c00u);
    return (uint16_t)(sign | ((uint16_t)exponent << 10) |
                      ((mantissa + 0x1000u) >> 13));
}

static float halfToFloat(uint16_t value) {
    uint32_t sign = (uint32_t)(value & 0x8000u) << 16;
    uint32_t exponent = (value >> 10) & 0x1fu;
    uint32_t mantissa = value & 0x03ffu;
    uint32_t bits = 0;
    if (exponent == 0) {
        if (mantissa == 0) bits = sign;
        else {
            int shift = 0;
            while ((mantissa & 0x0400u) == 0) { mantissa <<= 1; ++shift; }
            mantissa &= 0x03ffu;
            bits = sign | ((uint32_t)(127 - 15 - shift + 1) << 23) |
                   (mantissa << 13);
        }
    } else if (exponent == 0x1fu) {
        bits = sign | 0x7f800000u | (mantissa << 13);
    } else {
        bits = sign | ((exponent + 127 - 15) << 23) | (mantissa << 13);
    }
    float result = 0.0f;
    memcpy(&result, &bits, sizeof(result));
    return result;
}

static id<MTLTexture> makeSharedTexture(id<MTLDevice> device,
                                       MTLPixelFormat format,
                                       NSUInteger width, NSUInteger height,
                                       MTLTextureUsage usage) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height
        mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

static id<MTLRenderPipelineState> makeInternalPresentationPipeline(
    id<MTLDevice> device, MTLPixelFormat destinationFormat) {
    NSString *source = @"#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "struct R100VertexOut { float4 position [[position]]; float2 uv; };\n"
        "vertex R100VertexOut r100Vertex(uint vertexId [[vertex_id]]) {\n"
        "  float2 uv = float2((vertexId << 1) & 2, vertexId & 2);\n"
        "  R100VertexOut out;\n"
        "  out.position = float4(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0, 0.0, 1.0);\n"
        "  out.uv = uv; return out;\n"
        "}\n"
        "fragment float4 r100Fragment(R100VertexOut in [[stage_in]], "
        "texture2d<float> source [[texture(0)]]) {\n"
        "  constexpr sampler s(coord::normalized, address::clamp_to_edge, filter::linear);\n"
        "  return source.sample(s, in.uv);\n"
        "}\n";
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    id<MTLFunction> vertex = [library newFunctionWithName:@"r100Vertex"];
    id<MTLFunction> fragment = [library newFunctionWithName:@"r100Fragment"];
    if (!library || !vertex || !fragment) {
        logLine("INTERNAL_PRESENT_PIPELINE=FAILED library=%p vertex=%p fragment=%p error=%s",
            (__bridge void *)library, (__bridge void *)vertex,
            (__bridge void *)fragment,
            error.localizedDescription.UTF8String ?: "shader function unavailable");
        return nil;
    }
    MTLRenderPipelineDescriptor *descriptor = [MTLRenderPipelineDescriptor new];
    descriptor.vertexFunction = vertex;
    descriptor.fragmentFunction = fragment;
    descriptor.colorAttachments[0].pixelFormat = destinationFormat;
    id<MTLRenderPipelineState> pipeline =
        [device newRenderPipelineStateWithDescriptor:descriptor error:&error];
    logLine("INTERNAL_PRESENT_PIPELINE=%s device=%p destinationFormat=%lu pipeline=%p error=%s",
        pipeline ? "READY" : "FAILED", (__bridge void *)device,
        (unsigned long)destinationFormat, (__bridge void *)pipeline,
        error.localizedDescription.UTF8String ?: "none");
    return pipeline;
}

static void initializeMetalFXProvider(id<MTLDevice> device) {
    if (realProviderInitialized) return;
    realProviderInitialized = YES;
    if (![MetalFXFrameInterpolationProvider supportsDevice:device]) {
        logLine("REAL_METALFX_PROVIDER=UNSUPPORTED device=%p registryID=%llu",
            (__bridge void *)device, (unsigned long long)device.registryID);
        return;
    }
    MTLFXFrameInterpolatorDescriptor *descriptor =
        [MTLFXFrameInterpolatorDescriptor new];
    descriptor.colorTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.outputTextureFormat = MTLPixelFormatRGBA16Float;
    descriptor.depthTextureFormat = MTLPixelFormatR32Float;
    descriptor.motionTextureFormat = MTLPixelFormatRG16Float;
    descriptor.inputWidth = 1280;
    descriptor.inputHeight = 720;
    descriptor.outputWidth = 1280;
    descriptor.outputHeight = 720;
    id<MTLFXFrameInterpolator> usageProbe =
        [descriptor newFrameInterpolatorWithDevice:device];
    if (!usageProbe) {
        logLine("REAL_METALFX_USAGE_PROBE=FAILED device=%p", (__bridge void *)device);
        return;
    }
    colorInputUsage = usageProbe.colorTextureUsage | MTLTextureUsageShaderRead;
    depthInputUsage = usageProbe.depthTextureUsage | MTLTextureUsageShaderRead;
    motionInputUsage = usageProbe.motionTextureUsage | MTLTextureUsageShaderRead;
    NSError *error = nil;
    realFrameProvider = [[MetalFXFrameInterpolationProvider alloc]
        initWithDevice:device width:1280 height:720 error:&error];
    outputReadbackQueue = [device newCommandQueue];
    if (!realFrameProvider || !outputReadbackQueue) {
        logLine("REAL_METALFX_PROVIDER=FAILED device=%p error=%s",
            (__bridge void *)device,
            error.localizedDescription.UTF8String ?: "queue allocation failed");
        realFrameProvider = nil;
        outputReadbackQueue = nil;
        r102FallbackForSequence(previousCaptureSequence + 1,
                                "metalfx-provider-or-queue-unavailable");
        return;
    }
    const char *presentFlag = getenv("FGMETAL_R101_PRESENT");
    if (presentFlag && strcmp(presentFlag, "1") == 0) {
        internalPresentationPipeline = makeInternalPresentationPipeline(
            device, MTLPixelFormatBGRA8Unorm);
    }
    logLine("REAL_METALFX_PROVIDER=READY device=%p registryID=%llu width=1280 height=720 colorUsage=%lu depthUsage=%lu motionUsage=%lu",
        (__bridge void *)device, (unsigned long long)device.registryID,
        (unsigned long)colorInputUsage, (unsigned long)depthInputUsage,
        (unsigned long)motionInputUsage);
}

static void processCapturedFrame(uint64_t sequence, NSUInteger drawableID,
                                 MTLPixelFormat sourceFormat, NSData *sourceBytes,
                                 ColorStats mainShape, ColorStats marker,
                                 id<MTLDevice> device) {
    if (previousCaptureSequence && sequence != previousCaptureSequence + 1) {
        logLine("REAL_METALFX_SOURCE_SEQUENCE_GAP previous=%llu current=%llu; resetting provider history",
            (unsigned long long)previousCaptureSequence, (unsigned long long)sequence);
        realFrameProvider = nil;
        outputReadbackQueue = nil;
        realProviderInitialized = NO;
        previousCaptureColor = nil;
        previousCaptureDepth = nil;
        previousCaptureMotion = nil;
        previousCaptureFloatPixels = nil;
        previousCaptureSequence = 0;
    }
    initializeMetalFXProvider(device);
    if (!realFrameProvider || !outputReadbackQueue) return;

    const BOOL bgra = sourceFormat == MTLPixelFormatBGRA8Unorm;
    const NSUInteger pixelCount = 1280 * 720;
    const uint8_t *source = (const uint8_t *)sourceBytes.bytes;
    uint16_t *colorPixels = calloc(pixelCount * 4, sizeof(uint16_t));
    float *depthPixels = malloc(pixelCount * sizeof(float));
    uint16_t *motionPixels = calloc(pixelCount * 2, sizeof(uint16_t));
    if (!colorPixels || !depthPixels || !motionPixels) {
        logLine("REAL_METALFX_INPUT_ALLOCATION=FAILED seq=%llu",
            (unsigned long long)sequence);
        free(colorPixels); free(depthPixels); free(motionPixels);
        r102FallbackForSequence(sequence, "metalfx-input-allocation-failed");
        return;
    }

    double mainDx = 0.0, markerDx = 0.0;
    if (previousCaptureSequence && previousMainShape.count && mainShape.count)
        mainDx = (double)mainShape.sumX / mainShape.count -
                 (double)previousMainShape.sumX / previousMainShape.count;
    if (previousCaptureSequence && previousMarkerShape.count && marker.count)
        markerDx = (double)marker.sumX / marker.count -
                   (double)previousMarkerShape.sumX / previousMarkerShape.count;

    for (NSUInteger i = 0; i < pixelCount; ++i) {
        const uint8_t *pixel = source + i * 4;
        const uint8_t red = bgra ? pixel[2] : pixel[0];
        const uint8_t green = pixel[1];
        const uint8_t blue = bgra ? pixel[0] : pixel[2];
        colorPixels[i * 4 + 0] = floatToHalf((float)red / 255.0f);
        colorPixels[i * 4 + 1] = floatToHalf((float)green / 255.0f);
        colorPixels[i * 4 + 2] = floatToHalf((float)blue / 255.0f);
        colorPixels[i * 4 + 3] = floatToHalf((float)pixel[3] / 255.0f);

        BOOL mainPixel = bgra
            ? blue > 220 && green > 160 && red < 80
            : red < 80 && green > 160 && blue > 220;
        BOOL markerPixel = bgra
            ? blue < 80 && green > 100 && red > 220
            : red > 220 && green > 100 && blue < 80;
        depthPixels[i] = (mainPixel || markerPixel) ? 0.25f : 1.0f;
        if (mainPixel) motionPixels[i * 2] = floatToHalf((float)-mainDx);
        else if (markerPixel) motionPixels[i * 2] = floatToHalf((float)-markerDx);
    }

    id<MTLTexture> color = makeSharedTexture(device, MTLPixelFormatRGBA16Float,
        1280, 720, colorInputUsage);
    id<MTLTexture> depth = makeSharedTexture(device, MTLPixelFormatR32Float,
        1280, 720, depthInputUsage);
    id<MTLTexture> motion = makeSharedTexture(device, MTLPixelFormatRG16Float,
        1280, 720, motionInputUsage);
    if (!color || !depth || !motion) {
        logLine("REAL_METALFX_INPUT_TEXTURES=FAILED seq=%llu color=%p depth=%p motion=%p",
            (unsigned long long)sequence, (__bridge void *)color,
            (__bridge void *)depth, (__bridge void *)motion);
        free(colorPixels); free(depthPixels); free(motionPixels);
        r102FallbackForSequence(sequence, "metalfx-input-texture-allocation-failed");
        return;
    }
    [color replaceRegion:MTLRegionMake2D(0, 0, 1280, 720) mipmapLevel:0
        withBytes:colorPixels bytesPerRow:1280 * 4 * sizeof(uint16_t)];
    [depth replaceRegion:MTLRegionMake2D(0, 0, 1280, 720) mipmapLevel:0
        withBytes:depthPixels bytesPerRow:1280 * sizeof(float)];
    [motion replaceRegion:MTLRegionMake2D(0, 0, 1280, 720) mipmapLevel:0
        withBytes:motionPixels bytesPerRow:1280 * 2 * sizeof(uint16_t)];
    NSData *currentFloatPixels = [NSData dataWithBytes:colorPixels
        length:pixelCount * 4 * sizeof(uint16_t)];
    free(colorPixels); free(depthPixels); free(motionPixels);

    logLine("REAL_METALFX_SOURCE_FRAME seq=%llu drawableID=%lu sourceBytes=%p colorTexture=%p depthTexture=%p motionTexture=%p mainDx=%.3f markerDx=%.3f",
        (unsigned long long)sequence, (unsigned long)drawableID,
        sourceBytes.bytes, (__bridge void *)color, (__bridge void *)depth,
        (__bridge void *)motion, mainDx, markerDx);

    BOOL generationSlotReserved = NO;
    // Sequences 1 and 2 establish provider history only. Publish candidates
    // solely for a still-live source B whose present hook can claim this pair.
    if (previousCaptureColor && previousCaptureSequence + 1 == sequence &&
        sequence >= 3 && sequence <= 4) {
        pthread_mutex_lock(&stateLock);
        if (sequence == 4 && r102DeferredTicket &&
            r102DeferredTicket.sourceSequence == sequence &&
            r102DeferredTicket.state == R102DeferredTicketDeferred &&
            sequence > lastCompletedPresentationSequence &&
            internalPresentationPipeline) {
            if (internalSlotState == R101InternalSlotReady &&
                pendingInternalCurrentSequence < sequence) {
                ++stalePairRejections;
                logLine("R101_INTERNAL_SLOT=STALE_DISCARDED previous=%llu->%llu next=%llu",
                    (unsigned long long)pendingInternalPreviousSequence,
                    (unsigned long long)pendingInternalCurrentSequence,
                    (unsigned long long)sequence);
                pendingInternalPresentationTexture = nil;
                pendingInternalLayer = NULL;
                pendingInternalDevice = NULL;
                internalSlotState = R101InternalSlotEmpty;
            }
            if (internalSlotState == R101InternalSlotEmpty) {
                internalSlotState = R101InternalSlotGenerating;
                pendingInternalPreviousSequence = previousCaptureSequence;
                pendingInternalCurrentSequence = sequence;
                pendingInternalLayer = selectedHarnessLayer;
                pendingInternalDevice = (__bridge void *)device;
                pendingExpectedMainX = (previousMainShape.count && mainShape.count)
                    ? 0.5 * ((double)previousMainShape.sumX / previousMainShape.count +
                             (double)mainShape.sumX / mainShape.count) : NAN;
                pendingExpectedMarkerX = (previousMarkerShape.count && marker.count)
                    ? 0.5 * ((double)previousMarkerShape.sumX / previousMarkerShape.count +
                             (double)marker.sumX / marker.count) : NAN;
                generationSlotReserved = YES;
                pthread_cond_broadcast(&presentationReadyCondition);
                logLine("R101_INTERNAL_SLOT=GENERATING pair=%llu->%llu layer=%p device=%p",
                    (unsigned long long)pendingInternalPreviousSequence,
                    (unsigned long long)pendingInternalCurrentSequence,
                    pendingInternalLayer, pendingInternalDevice);
            }
        }
        pthread_mutex_unlock(&stateLock);
        NSError *error = nil;
        FGMetalInternalFrame *internalFrame =
            [realFrameProvider interpolatePreviousColor:previousCaptureColor
                currentColor:color depthTexture:depth motionTexture:motion
                deltaTime:1.0f / 60.0f error:&error];
        logLine("REAL_METALFX_TRANSITION previousSourceSequence=%llu currentSourceSequence=%llu result=%s internalPresentId=%llu error=%s",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            internalFrame ? "INTERNAL_FRAME" : (error ? "FAILED" : "HISTORY_ESTABLISHMENT"),
            (unsigned long long)internalFrame.internalPresentId,
            error.localizedDescription.UTF8String ?: "none");
        if (internalFrame && !error) {
            BOOL outputTextureValid = internalFrame.internalPresentId > 0 &&
                internalFrame.texture.device == device &&
                internalFrame.texture.width == 1280 &&
                internalFrame.texture.height == 720 &&
                internalFrame.texture.pixelFormat == MTLPixelFormatRGBA16Float &&
                (internalFrame.texture.usage & MTLTextureUsageShaderRead) != 0 &&
                internalFrame.texture.storageMode == MTLStorageModePrivate;
            pthread_mutex_lock(&stateLock);
            uint32_t internalSlotStateSnapshot = internalSlotState;
            uint64_t ticketIdSnapshot = r102DeferredTicket.ticketId;
            R102DeferredTicketState ticketStateSnapshot = r102DeferredTicket.state;
            uint64_t pendingPreviousSnapshot = pendingInternalPreviousSequence;
            uint64_t pendingCurrentSnapshot = pendingInternalCurrentSequence;
            pthread_mutex_unlock(&stateLock);
            logLine("R102_METALFX_CANDIDATE_CHECK sourceSequence=%llu internalPresentId=%llu texture=%p device=%p expectedDevice=%p width=%lu height=%lu pixelFormat=%lu usage=%lu storageMode=%lu outputTextureValid=%d generationSlotReserved=%d slotState=%u pendingPair=%llu->%llu ticketId=%llu ticketState=%u",
                (unsigned long long)sequence,
                (unsigned long long)internalFrame.internalPresentId,
                (__bridge void *)internalFrame.texture,
                (__bridge void *)internalFrame.texture.device,
                (__bridge void *)device,
                (unsigned long)internalFrame.texture.width,
                (unsigned long)internalFrame.texture.height,
                (unsigned long)internalFrame.texture.pixelFormat,
                (unsigned long)internalFrame.texture.usage,
                (unsigned long)internalFrame.texture.storageMode,
                outputTextureValid, generationSlotReserved,
                internalSlotStateSnapshot,
                (unsigned long long)pendingPreviousSnapshot,
                (unsigned long long)pendingCurrentSnapshot,
                (unsigned long long)ticketIdSnapshot,
                (unsigned)ticketStateSnapshot);
            BOOL outputPublished = NO;
            if (outputTextureValid && generationSlotReserved) {
                pthread_mutex_lock(&stateLock);
                BOOL pairStillCurrent = internalSlotState == R101InternalSlotGenerating &&
                    pendingInternalPreviousSequence == previousCaptureSequence &&
                    pendingInternalCurrentSequence == sequence &&
                    pendingInternalLayer == selectedHarnessLayer &&
                    pendingInternalDevice == (__bridge void *)device &&
                    lastCompletedPresentationSequence < sequence;
                if (pairStillCurrent) {
                    pendingInternalPresentationTexture = internalFrame.texture;
                    pendingInternalPresentId = internalFrame.internalPresentId;
                    pendingInternalPreviousSequence = previousCaptureSequence;
                    pendingInternalCurrentSequence = sequence;
                    internalSlotState = R101InternalSlotReady;
                    outputPublished = YES;
                    pthread_cond_broadcast(&presentationReadyCondition);
                    logLine("R101_INTERNAL_SLOT=READY internalPresentId=%llu pair=%llu->%llu texture=%p layer=%p device=%p",
                        (unsigned long long)pendingInternalPresentId,
                        (unsigned long long)pendingInternalPreviousSequence,
                        (unsigned long long)pendingInternalCurrentSequence,
                        (__bridge void *)pendingInternalPresentationTexture,
                        pendingInternalLayer, pendingInternalDevice);
                } else {
                    ++stalePairRejections;
                    logLine("R101_INTERNAL_SLOT=REJECTED_STALE internalPresentId=%llu pair=%llu->%llu state=%u lastCompletedB=%llu",
                        (unsigned long long)internalFrame.internalPresentId,
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (unsigned)internalSlotState,
                        (unsigned long long)lastCompletedPresentationSequence);
                }
                pthread_mutex_unlock(&stateLock);
                if (outputPublished) r102AttemptInternalDispatch();
                else if (sequence == 4)
                    r102FallbackForSequence(sequence, "metalfx-output-rejected-as-stale-pair");
            } else if (sequence == 4) {
                r102FallbackForSequence(sequence, "metalfx-output-invalid-or-no-generation-slot");
            }
            MTLTextureDescriptor *readbackDescriptor = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                width:1280 height:720 mipmapped:NO];
            readbackDescriptor.storageMode = MTLStorageModeShared;
            readbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
            id<MTLTexture> outputReadback =
                [device newTextureWithDescriptor:readbackDescriptor];
            id<MTLCommandBuffer> readbackCommandBuffer =
                [outputReadbackQueue commandBuffer];
            id<MTLBlitCommandEncoder> blit =
                [readbackCommandBuffer blitCommandEncoder];
            if (outputReadback && readbackCommandBuffer && blit) {
                [blit copyFromTexture:internalFrame.texture toTexture:outputReadback];
                [blit endEncoding];
                [readbackCommandBuffer commit];
                [readbackCommandBuffer waitUntilCompleted];
                if (readbackCommandBuffer.status == MTLCommandBufferStatusCompleted) {
                    NSMutableData *outputBytes = [NSMutableData dataWithLength:
                        1280 * 720 * 4 * sizeof(uint16_t)];
                    [outputReadback getBytes:outputBytes.mutableBytes
                        bytesPerRow:1280 * 4 * sizeof(uint16_t)
                        fromRegion:MTLRegionMake2D(0, 0, 1280, 720) mipmapLevel:0];
                    const uint16_t *out = (const uint16_t *)outputBytes.bytes;
                    const uint16_t *previous = (const uint16_t *)previousCaptureFloatPixels.bytes;
                    const uint16_t *current = (const uint16_t *)currentFloatPixels.bytes;
                    double previousError = 0.0, currentError = 0.0;
                    double alphaMin = INFINITY, alphaMax = -INFINITY, alphaSum = 0.0;
                    uint64_t alphaOpaquePixels = 0;
                    BOOL alphaFinite = YES;
                    uint64_t meaningfulPixels = 0;
                    for (NSUInteger i = 0; i < pixelCount; ++i) {
                        double luminance = 0.0;
                        float alpha = halfToFloat(out[i * 4 + 3]);
                        if (!isfinite(alpha)) alphaFinite = NO;
                        if (alpha < alphaMin) alphaMin = alpha;
                        if (alpha > alphaMax) alphaMax = alpha;
                        alphaSum += alpha;
                        if (fabs((double)alpha - 1.0) <= 0.01) ++alphaOpaquePixels;
                        for (NSUInteger c = 0; c < 3; ++c) {
                            float ov = halfToFloat(out[i * 4 + c]);
                            float pv = halfToFloat(previous[i * 4 + c]);
                            float cv = halfToFloat(current[i * 4 + c]);
                            previousError += fabs((double)ov - pv);
                            currentError += fabs((double)ov - cv);
                            luminance += ov;
                        }
                        if (luminance > 0.15) ++meaningfulPixels;
                    }
                    const double denom = (double)pixelCount * 3.0;
                    previousError /= denom;
                    currentError /= denom;
                    BOOL distinctPrevious = previousError > 0.0001;
                    BOOL distinctCurrent = currentError > 0.0001;
                    BOOL outputReadable = meaningfulPixels > 0;
                    logLine("REAL_METALFX_OUTPUT internalPresentId=%llu sourceSequence=%llu->%llu outputTexture=%p readbackTexture=%p readbackStatus=%lu privateOutput=%d readable=%d meaningfulPixels=%llu maePrevious=%.8f maeCurrent=%.8f distinctPrevious=%d distinctCurrent=%d alphaFinite=%d alphaMin=%.5f alphaMax=%.5f alphaMean=%.5f alphaOpaquePixels=%llu alphaOpaquePercent=%.3f",
                        (unsigned long long)internalFrame.internalPresentId,
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (__bridge void *)internalFrame.texture,
                        (__bridge void *)outputReadback,
                        (unsigned long)readbackCommandBuffer.status,
                        internalFrame.texture.storageMode == MTLStorageModePrivate,
                        outputReadable, (unsigned long long)meaningfulPixels,
                        previousError, currentError, distinctPrevious, distinctCurrent,
                        alphaFinite, alphaMin, alphaMax, alphaSum / (double)pixelCount,
                        (unsigned long long)alphaOpaquePixels,
                        100.0 * (double)alphaOpaquePixels / (double)pixelCount);
                    if (outputTextureValid && outputReadable && alphaFinite &&
                        distinctPrevious && distinctCurrent &&
                        generationSlotReserved) {
                        logLine("R101_METALFX_ALPHA=CHECKED internalPresentId=%llu sourceSequence=%llu->%llu finite=%d min=%.5f max=%.5f mean=%.5f opaquePercent=%.3f expected=opaque pass=%d",
                            (unsigned long long)internalFrame.internalPresentId,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence, alphaFinite, alphaMin,
                            alphaMax, alphaSum / (double)pixelCount,
                            100.0 * (double)alphaOpaquePixels / (double)pixelCount,
                            alphaOpaquePixels == pixelCount);
                        logLine("REAL_METALFX_INTERNAL_TEXTURE=VALIDATED internalPresentId=%llu outputTexture=%p readbackTexture=%p sourceSequence=%llu->%llu maePrevious=%.8f maeCurrent=%.8f",
                            (unsigned long long)internalFrame.internalPresentId,
                            (__bridge void *)internalFrame.texture,
                            (__bridge void *)outputReadback,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            previousError, currentError);
                    }
                } else {
                    logLine("REAL_METALFX_OUTPUT_READBACK=FAILED internalPresentId=%llu status=%lu error=%s",
                        (unsigned long long)internalFrame.internalPresentId,
                        (unsigned long)readbackCommandBuffer.status,
                        readbackCommandBuffer.error.localizedDescription.UTF8String ?: "none");
                }
            }
        } else if (sequence == 4) {
            r102FallbackForSequence(sequence, "metalfx-generation-failed");
        }
        pthread_mutex_lock(&stateLock);
        if (generationSlotReserved &&
            internalSlotState == R101InternalSlotGenerating &&
            pendingInternalCurrentSequence == sequence) {
            internalSlotState = R101InternalSlotEmpty;
            pendingInternalPresentationTexture = nil;
            pendingInternalLayer = NULL;
            pendingInternalDevice = NULL;
            pthread_cond_broadcast(&presentationReadyCondition);
        }
        pthread_mutex_unlock(&stateLock);
    }

    previousCaptureColor = color;
    previousCaptureDepth = depth;
    previousCaptureMotion = motion;
    previousCaptureFloatPixels = currentFloatPixels;
    previousMainShape = mainShape;
    previousMarkerShape = marker;
    previousCaptureSequence = sequence;
    if (sequence == 4) {
        pthread_mutex_lock(&stateLock);
        R102DeferredSourceTicket *ticket = r102DeferredTicket;
        BOOL stillDeferredWithoutMatchingOutput = ticket &&
            ticket.sourceSequence == sequence &&
            ticket.state == R102DeferredTicketDeferred &&
            !(internalSlotState == R101InternalSlotReady &&
              pendingInternalPreviousSequence == ticket.sourcePairA &&
              pendingInternalCurrentSequence == ticket.sourcePairB);
        pthread_mutex_unlock(&stateLock);
        if (stillDeferredWithoutMatchingOutput)
            r102FallbackForSequence(sequence, "matching-metalfx-output-not-published");
    }
}

static void drainCapturedFramesInSourceOrder(void) {
    if (!pendingCapturedFrames)
        pendingCapturedFrames = [NSMutableDictionary dictionary];
    for (;;) {
        NSNumber *key = @(nextCaptureProcessingSequence);
        id entry = pendingCapturedFrames[key];
        if (!entry) break;
        [pendingCapturedFrames removeObjectForKey:key];
        if (entry == [NSNull null]) {
            logLine("R101_CAPTURE_PROCESS=SKIPPED sequence=%llu reason=source-capture-failed",
                (unsigned long long)nextCaptureProcessingSequence);
        } else {
            R101CapturedFrame *frame = (R101CapturedFrame *)entry;
            logLine("R101_CAPTURE_PROCESS=ORDERED sequence=%llu drawableID=%llu",
                (unsigned long long)frame.sequence,
                (unsigned long long)frame.drawableID);
            processCapturedFrame(frame.sequence, frame.drawableID,
                frame.pixelFormat, frame.sourceBytes, frame.mainShape,
                frame.markerShape, frame.device);
        }
        ++nextCaptureProcessingSequence;
    }
}

static void queueSkippedCaptureSequence(uint64_t sequence, const char *reason) {
    dispatch_async(captureProcessingQueue, ^{
        if (!pendingCapturedFrames)
            pendingCapturedFrames = [NSMutableDictionary dictionary];
        NSNumber *key = @(sequence);
        if (!pendingCapturedFrames[key] && sequence >= nextCaptureProcessingSequence)
            pendingCapturedFrames[key] = [NSNull null];
        logLine("R101_CAPTURE_PROCESS=SKIP_QUEUED sequence=%llu reason=%s",
            (unsigned long long)sequence, reason);
        r102FallbackForSequence(sequence, reason ?: "source-capture-failed");
        drainCapturedFramesInSourceOrder();
    });
}

static void queueCapturedFrame(uint64_t sequence, uint64_t drawableID,
                               MTLPixelFormat pixelFormat, NSData *sourceBytes,
                               ColorStats mainShape, ColorStats markerShape,
                               id<MTLDevice> device) {
    R101CapturedFrame *frame = [R101CapturedFrame new];
    frame.sequence = sequence;
    frame.drawableID = drawableID;
    frame.pixelFormat = pixelFormat;
    frame.sourceBytes = sourceBytes;
    frame.mainShape = mainShape;
    frame.markerShape = markerShape;
    frame.device = device;
    dispatch_async(captureProcessingQueue, ^{
        if (!pendingCapturedFrames)
            pendingCapturedFrames = [NSMutableDictionary dictionary];
        NSNumber *key = @(sequence);
        if (!pendingCapturedFrames[key] && sequence >= nextCaptureProcessingSequence)
            pendingCapturedFrames[key] = frame;
        drainCapturedFramesInSourceOrder();
    });
}

static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<CAMetalDrawable> drawable,
                                uint64_t sourceSequence,
                                id<MTLCommandBuffer> producerCommandBuffer) {
    const char *captureFlag = getenv("FGMETAL_R101_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0) return;

    // The presentation hook may reach this before the source producer has
    // finished. Defer the copy until its existing completion callback invokes
    // us again; this avoids queueing the capture behind the game's render work.
    if (producerCommandBuffer.status != MTLCommandBufferStatusCompleted ||
        producerCommandBuffer.error) {
        if (producerCommandBuffer.status == MTLCommandBufferStatusError ||
            producerCommandBuffer.error) {
            logLine("SOURCE_CAPTURE_PRODUCER_FAILED seq=%llu drawableID=%lu status=%lu error=%s",
                (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                (unsigned long)producerCommandBuffer.status,
                producerCommandBuffer.error.localizedDescription.UTF8String ?: "none");
            queueSkippedCaptureSequence(sourceSequence, "producer-command-failed");
        } else {
            logLine("R101_SOURCE_CAPTURE=DEFERRED_UNTIL_PRODUCER_COMPLETION seq=%llu drawableID=%lu producerStatus=%lu",
                (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                (unsigned long)producerCommandBuffer.status);
        }
        return;
    }

    pthread_mutex_lock(&stateLock);
    if (!capturedSourceSequences)
        capturedSourceSequences = [NSMutableSet set];
    NSNumber *sequenceKey = @(sourceSequence);
    if ([capturedSourceSequences containsObject:sequenceKey]) {
        pthread_mutex_unlock(&stateLock);
        logLine("R101_SOURCE_CAPTURE=DEDUPLICATED seq=%llu drawableID=%lu",
            (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID);
        return;
    }
    if (captureAttempts >= kCaptureLimit) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    [capturedSourceSequences addObject:sequenceKey];
    ++captureAttempts;
    pthread_mutex_unlock(&stateLock);

    id<MTLTexture> source = drawable.texture;
    id<MTLDevice> device = source.device;
    if (source.framebufferOnly || (source.usage & MTLTextureUsageShaderRead) == 0 ||
        device != queue.device || source.width != 1280 || source.height != 720 ||
        (source.pixelFormat != MTLPixelFormatBGRA8Unorm &&
         source.pixelFormat != MTLPixelFormatRGBA8Unorm)) {
        logLine("SOURCE_CAPTURE_PREFLIGHT_FAILED seq=%llu drawableID=%lu texture=%p device=%p queueDevice=%p framebufferOnly=%d usage=%lu size=%lux%lu pixelFormat=%lu",
            (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
            (__bridge void *)source, (__bridge void *)device,
            (__bridge void *)queue.device, source.framebufferOnly,
            (unsigned long)source.usage, (unsigned long)source.width,
            (unsigned long)source.height, (unsigned long)source.pixelFormat);
        queueSkippedCaptureSequence(sourceSequence, "source-preflight-failed");
        return;
    }

    pthread_mutex_lock(&stateLock);
    if (!sourceCaptureReadbackQueue || sourceCaptureReadbackQueue.device != device)
        sourceCaptureReadbackQueue = [device newCommandQueue];
    id<MTLCommandQueue> captureQueue = sourceCaptureReadbackQueue;
    pthread_mutex_unlock(&stateLock);
    if (!captureQueue) {
        logLine("SOURCE_CAPTURE_QUEUE_ALLOCATION_FAILED seq=%llu device=%p",
            (unsigned long long)sourceSequence, (__bridge void *)device);
        queueSkippedCaptureSequence(sourceSequence, "capture-queue-allocation-failed");
        return;
    }

    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:source.pixelFormat
        width:source.width height:source.height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> captureTexture = [device newTextureWithDescriptor:descriptor];
    if (!captureTexture) {
        logLine("SOURCE_CAPTURE_TEXTURE_ALLOCATION_FAILED seq=%llu device=%p",
            (unsigned long long)sourceSequence, (__bridge void *)device);
        queueSkippedCaptureSequence(sourceSequence, "capture-texture-allocation-failed");
        return;
    }
    id<MTLCommandBuffer> copyCommandBuffer = [captureQueue commandBuffer];
    id<MTLBlitCommandEncoder> blit = [copyCommandBuffer blitCommandEncoder];
    if (!copyCommandBuffer || !blit) {
        logLine("SOURCE_CAPTURE_BLIT_ALLOCATION_FAILED seq=%llu queue=%p",
            (unsigned long long)sourceSequence, (__bridge void *)queue);
        queueSkippedCaptureSequence(sourceSequence, "copy-command-allocation-failed");
        return;
    }
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0, 0, 0)
              sourceSize:MTLSizeMake(source.width, source.height, 1)
               toTexture:captureTexture destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit endEncoding];
    logLine("SOURCE_CAPTURE_COPY_ENQUEUED seq=%llu drawableID=%lu producerCommandBuffer=%p producerStatus=%lu producerQueue=%p copyQueue=%p device=%p registryID=%llu sourceTexture=%p captureTexture=%p size=%lux%lu pixelFormat=%lu",
        (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
        (__bridge void *)producerCommandBuffer,
        (unsigned long)producerCommandBuffer.status, (__bridge void *)queue,
        (__bridge void *)captureQueue,
        (__bridge void *)device, (unsigned long long)device.registryID,
        (__bridge void *)source, (__bridge void *)captureTexture,
        (unsigned long)source.width, (unsigned long)source.height,
        (unsigned long)source.pixelFormat);

    [copyCommandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completedCopy) {
        @autoreleasepool {
            if (completedCopy.status != MTLCommandBufferStatusCompleted || completedCopy.error) {
                logLine("SOURCE_CAPTURE_COPY_FAILED seq=%llu drawableID=%lu copyCommandBuffer=%p status=%lu error=%s",
                    (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                    (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                    completedCopy.error.localizedDescription.UTF8String ?: "none");
                queueSkippedCaptureSequence(sourceSequence, "copy-command-failed");
                return;
            }
            size_t bytesPerRow = (size_t)captureTexture.width * 4;
            NSMutableData *readback = [NSMutableData dataWithLength:
                bytesPerRow * (size_t)captureTexture.height];
            [captureTexture getBytes:readback.mutableBytes bytesPerRow:bytesPerRow
                fromRegion:MTLRegionMake2D(0, 0, captureTexture.width,
                                           captureTexture.height) mipmapLevel:0];
            ColorStats mainShape = {0}, marker = {0};
            BOOL backgroundMatched = NO;
            BOOL verified = verifyHarnessPixels(captureTexture,
                (const uint8_t *)readback.bytes, bytesPerRow, &mainShape,
                &marker, &backgroundMatched);
            char capturePath[512];
            const char *captureDir = getenv("FGMETAL_R103_LOG_DIR");
            if (!captureDir || !captureDir[0])
                captureDir = "/tmp/fgmetal-r103-contiguous";
            snprintf(capturePath, sizeof(capturePath),
                "%s/source-sequence-%llu.raw", captureDir,
                (unsigned long long)sourceSequence);
            FILE *captureFile = verified ? fopen(capturePath, "wb") : NULL;
            BOOL saved = captureFile &&
                fwrite(readback.bytes, 1, readback.length, captureFile) == readback.length;
            if (captureFile) fclose(captureFile);
            logLine("SOURCE_CAPTURE_READBACK seq=%llu drawableID=%lu producerCommandBuffer=%p producerStatus=%lu copyCommandBuffer=%p copyStatus=%lu sourceTexture=%p captureTexture=%p device=%p registryID=%llu size=%lux%lu pixelFormat=%lu backgroundMatched=%d mainPixels=%llu mainBounds=%u,%u-%u,%u mainCentroidX=%.2f markerPixels=%llu markerBounds=%u,%u-%u,%u markerCentroidX=%.2f verified=%d saved=%d path=%s",
                (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                (__bridge void *)producerCommandBuffer,
                (unsigned long)producerCommandBuffer.status,
                (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                (__bridge void *)source, (__bridge void *)captureTexture,
                (__bridge void *)device, (unsigned long long)device.registryID,
                (unsigned long)captureTexture.width,
                (unsigned long)captureTexture.height,
                (unsigned long)captureTexture.pixelFormat, backgroundMatched,
                (unsigned long long)mainShape.count,
                mainShape.count ? mainShape.minX : 0,
                mainShape.count ? mainShape.minY : 0,
                mainShape.count ? mainShape.maxX : 0,
                mainShape.count ? mainShape.maxY : 0,
                mainShape.count ? (double)mainShape.sumX / mainShape.count : 0.0,
                (unsigned long long)marker.count,
                marker.count ? marker.minX : 0,
                marker.count ? marker.minY : 0,
                marker.count ? marker.maxX : 0,
                marker.count ? marker.maxY : 0,
                marker.count ? (double)marker.sumX / marker.count : 0.0,
                verified, saved, saved ? capturePath : "<not-saved>");
            if (verified) {
                logLine("REAL_D3DMETAL_SOURCE_CAPTURE=VALIDATED seq=%llu drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p captureTexture=%p size=%lux%lu pixelFormat=%lu mainPixels=%llu markerPixels=%llu saved=%d",
                    (unsigned long long)sourceSequence,
                    (unsigned long)drawable.drawableID,
                    (__bridge void *)producerCommandBuffer,
                    (__bridge void *)completedCopy,
                    (__bridge void *)captureTexture,
                    (unsigned long)captureTexture.width,
                    (unsigned long)captureTexture.height,
                    (unsigned long)captureTexture.pixelFormat,
                    (unsigned long long)mainShape.count,
                    (unsigned long long)marker.count, saved);
                NSData *sourceBytes = [readback copy];
                queueCapturedFrame(sourceSequence, drawable.drawableID,
                    captureTexture.pixelFormat, sourceBytes, mainShape, marker,
                    device);
            } else {
                queueSkippedCaptureSequence(sourceSequence, "captured-image-validation-failed");
            }
        }
    }];
    [copyCommandBuffer commit];
}

static void reportCorrelation(id<MTLCommandBuffer> commandBuffer,
                              id<MTLTexture> texture,
                              NSDictionary *drawableRecord,
                              NSUInteger attachmentIndex,
                              const char *attachmentKind) {
    id<CAMetalDrawable> drawable = drawableRecord[@"drawable"];
    uint64_t sequence = [drawableRecord[@"sequence"] unsignedLongLongValue];
    uint64_t drawableID = [drawableRecord[@"drawableID"] unsignedLongLongValue];
    NSString *completionKey = [NSString stringWithFormat:@"%p:%llu", (__bridge void *)commandBuffer,
                               (unsigned long long)sequence];

    pthread_mutex_lock(&stateLock);
    if (!completedHookKeys) completedHookKeys = [NSMutableSet set];
    BOOL shouldRegister = ![completedHookKeys containsObject:completionKey];
    if (shouldRegister) [completedHookKeys addObject:completionKey];
    pthread_mutex_unlock(&stateLock);
    if (!shouldRegister) return;

    id<MTLCommandQueue> queue = commandBuffer.commandQueue;
    id<MTLDevice> device = texture.device;
    NSValue *textureKey = [NSValue valueWithPointer:(__bridge const void *)texture];
    pthread_mutex_lock(&stateLock);
    NSDictionary *currentRecord = drawablesByTexture[textureKey];
    if ([currentRecord[@"sequence"] unsignedLongLongValue] == sequence) {
        NSMutableDictionary *updatedRecord = [currentRecord mutableCopy];
        updatedRecord[@"producerCommandBuffer"] = commandBuffer;
        updatedRecord[@"producerQueue"] = queue;
        drawablesByTexture[textureKey] = updatedRecord;
    }
    NSDictionary *currentProducerRecord = drawablesByTexture[textureKey];
    if (!r102ProducerRecordsByCommandBuffer)
        r102ProducerRecordsByCommandBuffer = [NSMutableDictionary dictionary];
    NSValue *commandBufferKey = [NSValue valueWithPointer:(__bridge const void *)commandBuffer];
    r102ProducerRecordsByCommandBuffer[commandBufferKey] = currentProducerRecord;
    if (!r102ScheduledHandlerKeys) r102ScheduledHandlerKeys = [NSMutableSet set];
    BOOL addScheduledHandler = ![r102ScheduledHandlerKeys containsObject:commandBufferKey];
    if (addScheduledHandler) [r102ScheduledHandlerKeys addObject:commandBufferKey];
    pthread_mutex_unlock(&stateLock);
    logR102Event("TARGET_RENDER_ENCODER_CORRELATED", currentProducerRecord, drawable,
                 commandBuffer, nil, NULL, 0.0);
    if (addScheduledHandler) {
        [commandBuffer addScheduledHandler:^(id<MTLCommandBuffer> scheduled) {
            logR102Event("PRODUCER_SCHEDULED", r102RecordForCommandBuffer(scheduled),
                         nil, scheduled, nil, NULL, 0.0);
        }];
    }
    BOOL correlationMilestone = NO;
    pthread_mutex_lock(&stateLock);
    BOOL consecutive = lastCorrelatedSequence + 1 == sequence &&
        correlatedQueue == (__bridge void *)queue &&
        correlatedDevice == (__bridge void *)device;
    consecutiveCorrelationCount = consecutive ? consecutiveCorrelationCount + 1 : 1;
    lastCorrelatedSequence = sequence;
    correlatedQueue = (__bridge void *)queue;
    correlatedDevice = (__bridge void *)device;
    if (consecutiveCorrelationCount >= 10 && !correlationMilestoneLogged) {
        correlationMilestoneLogged = YES;
        correlationMilestone = YES;
    }
    uint64_t matchedRun = consecutiveCorrelationCount;
    pthread_mutex_unlock(&stateLock);

    logLine("DRAWABLE_COMMAND_CORRELATED pid=%d seq=%llu drawableID=%llu layer=%p texture=%p textureDevice=%p registryID=%llu framebufferOnly=%d commandBuffer=%p commandBufferClass=%s queue=%p queueClass=%s queueDevice=%p attachment=%s attachmentIndex=%lu commandBufferStatus=%lu consecutiveMatches=%llu",
        getpid(), (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)drawable.layer, (__bridge void *)texture,
        (__bridge void *)device, (unsigned long long)device.registryID,
        texture.framebufferOnly, (__bridge void *)commandBuffer,
        className(commandBuffer), (__bridge void *)queue, className(queue),
        (__bridge void *)queue.device, attachmentKind, (unsigned long)attachmentIndex,
        (unsigned long)commandBuffer.status, (unsigned long long)matchedRun);
    if (correlationMilestone) {
        logLine("DRAWABLE_PRODUCER_CORRELATION=VALIDATED pid=%d consecutiveMatches=%llu layer=%p queue=%p device=%p registryID=%llu",
            getpid(), (unsigned long long)matchedRun,
            (__bridge void *)drawable.layer, (__bridge void *)queue,
            (__bridge void *)device, (unsigned long long)device.registryID);
    }

    // Register before returning the encoder to the caller. The game's own
    // command buffer remains responsible for commit and execution ordering.
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        NSError *error = completed.error;
        BOOL completionMilestone = NO;
        BOOL deferredSourceUnlocked = NO;
        R102DeferredSourceTicket *deferredTicket = nil;
        pthread_mutex_lock(&stateLock);
        BOOL successfulCompletion = completed.status == MTLCommandBufferStatusCompleted && !error;
        R102DeferredSourceTicket *activeTicket = r102DeferredTicket;
        if (successfulCompletion && activeTicket &&
            activeTicket.producerCommandBuffer == completed &&
            activeTicket.sourceSequence == sequence &&
            activeTicket.state != R102DeferredTicketForwardingSource &&
            activeTicket.state != R102DeferredTicketEmpty &&
            !activeTicket.producerCompletedWhileDeferredLogged) {
            activeTicket.producerCompletedWhileDeferredLogged = YES;
            deferredTicket = activeTicket;
            deferredSourceUnlocked = YES;
        }
        BOOL consecutiveCompletion = lastCompletedSequence + 1 == sequence &&
            (__bridge void *)completed.commandQueue == correlatedQueue &&
            (__bridge void *)texture.device == correlatedDevice;
        if (successfulCompletion) {
            consecutiveCompletionCount = consecutiveCompletion
                ? consecutiveCompletionCount + 1 : 1;
            lastCompletedSequence = sequence;
        } else {
            consecutiveCompletionCount = 0;
            lastCompletedSequence = 0;
        }
        if (consecutiveCompletionCount >= 10 && !completionMilestoneLogged) {
            completionMilestoneLogged = YES;
            completionMilestone = YES;
        }
        uint64_t completionRun = consecutiveCompletionCount;
        pthread_mutex_unlock(&stateLock);
        logLine("DRAWABLE_PRODUCER_COMPLETED pid=%d seq=%llu drawableID=%llu commandBuffer=%p queue=%p queueDevice=%p device=%p registryID=%llu status=%lu error=%s texture=%p framebufferOnly=%d consecutiveCompletions=%llu",
            getpid(), (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)completed.commandQueue,
            (__bridge void *)completed.commandQueue.device,
            (__bridge void *)texture.device,
            (unsigned long long)texture.device.registryID,
            (unsigned long)completed.status,
            error ? (error.localizedDescription.UTF8String ?: "unknown") : "none",
            (__bridge void *)texture, texture.framebufferOnly,
            (unsigned long long)completionRun);
        logR102Event("PRODUCER_COMPLETED", r102RecordForCommandBuffer(completed),
                     drawable, completed, nil, NULL, 0.0);
        if (deferredSourceUnlocked) {
            logLine("DEFERRED_SOURCE_UNLOCKS_PRODUCER=VALIDATED ticketId=%llu appFrameId=%llu sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p status=%lu error=none state=%u wsiPresentId=UNALLOCATED pair=%llu->%llu",
                (unsigned long long)deferredTicket.ticketId,
                (unsigned long long)deferredTicket.appFrameId,
                (unsigned long long)deferredTicket.sourceSequence,
                (unsigned long long)deferredTicket.drawableID,
                (__bridge void *)completed, (unsigned long)completed.status,
                (unsigned)deferredTicket.state,
                (unsigned long long)deferredTicket.sourcePairA,
                (unsigned long long)deferredTicket.sourcePairB);
        }
        if (completionMilestone) {
            logLine("DRAWABLE_PRODUCER_COMPLETION=VALIDATED pid=%d consecutiveCompletions=%llu queue=%p device=%p registryID=%llu",
                getpid(), (unsigned long long)completionRun,
                (__bridge void *)completed.commandQueue,
                (__bridge void *)texture.device,
                (unsigned long long)texture.device.registryID);
        }
        if (completed.status == MTLCommandBufferStatusCompleted && !error) {
            captureRealDrawable(completed.commandQueue, drawable, sequence, completed);
        } else {
            r102FallbackForSequence(sequence, "producer-command-buffer-failed");
        }
        pthread_mutex_lock(&stateLock);
        [completedHookKeys removeObject:completionKey];
        pthread_mutex_unlock(&stateLock);
    }];
}

static void inspectRenderPass(id<MTLCommandBuffer> commandBuffer,
                              MTLRenderPassDescriptor *descriptor) {
    MTLRenderPassColorAttachmentDescriptorArray *colors = descriptor.colorAttachments;
    for (NSUInteger index = 0; index < 8; ++index) {
        id<MTLTexture> texture = colors[index].texture;
        NSDictionary *record = trackedDrawableForTexture(texture);
        if (record) reportCorrelation(commandBuffer, texture, record, index, "color");
    }
    id<MTLTexture> depth = descriptor.depthAttachment.texture;
    NSDictionary *depthRecord = trackedDrawableForTexture(depth);
    if (depthRecord) reportCorrelation(commandBuffer, depth, depthRecord, 0, "depth");
    id<MTLTexture> stencil = descriptor.stencilAttachment.texture;
    NSDictionary *stencilRecord = trackedDrawableForTexture(stencil);
    if (stencilRecord) reportCorrelation(commandBuffer, stencil, stencilRecord, 0, "stencil");
}

static id hookRenderCommandEncoder(id self, SEL selector, MTLRenderPassDescriptor *descriptor) {
    IMP original = originalFor(self, selector);
    id encoder = original ? ((id (*)(id, SEL, id))original)(self, selector, descriptor) : nil;
    if (encoder && isSelectedProcess() && !isInternalPresentationCall) {
        id<MTLCommandBuffer> commandBuffer = (id<MTLCommandBuffer>)self;
        inspectRenderPass(commandBuffer, descriptor);
        id<MTLTexture> texture = descriptor.colorAttachments[0].texture;
        NSDictionary *record = trackedDrawableForTexture(texture);
        if (record) {
            NSDictionary *producerRecord = r102RecordForCommandBuffer(commandBuffer) ?: record;
            NSValue *key = [NSValue valueWithPointer:(__bridge const void *)encoder];
            pthread_mutex_lock(&stateLock);
            if (!r102EncoderRecords) r102EncoderRecords = [NSMutableDictionary dictionary];
            r102EncoderRecords[key] = producerRecord;
            pthread_mutex_unlock(&stateLock);
            logR102Event("TARGET_RENDER_ENCODER_CREATED", producerRecord,
                         record[@"drawable"], commandBuffer, encoder, selector, 0.0);
        }
    }
    return encoder;
}

static void hookRenderEncoderEndEncoding(id self, SEL selector) {
    NSValue *key = [NSValue valueWithPointer:(__bridge const void *)self];
    pthread_mutex_lock(&stateLock);
    NSDictionary *record = r102EncoderRecords[key];
    pthread_mutex_unlock(&stateLock);
    if (record) logR102Event("TARGET_RENDER_ENCODER_END_ENTRY", record,
                             record[@"drawable"], record[@"producerCommandBuffer"],
                             self, selector, 0.0);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL))original)(self, selector);
    if (record) {
        logR102Event("TARGET_RENDER_ENCODER_ENDED", record,
                     record[@"drawable"], record[@"producerCommandBuffer"],
                     self, selector, 0.0);
        pthread_mutex_lock(&stateLock);
        [r102EncoderRecords removeObjectForKey:key];
        pthread_mutex_unlock(&stateLock);
    }
}

static void hookCommandBufferCommit(id self, SEL selector) {
    id<MTLCommandBuffer> commandBuffer = (id<MTLCommandBuffer>)self;
    NSDictionary *record = isSelectedProcess() && !isInternalPresentationCall
        ? r102RecordForCommandBuffer(commandBuffer) : nil;
    if (record) logR102Event("PRODUCER_COMMIT_ENTRY", record, record[@"drawable"],
                             commandBuffer, nil, selector, 0.0);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL))original)(self, selector);
    if (record) {
        logR102Event("PRODUCER_COMMIT_RETURN", record, record[@"drawable"],
                     commandBuffer, nil, selector, 0.0);
        r103CloseFrontierAfterProducerCommit(record, commandBuffer);
    }
}

static BOOL directMethod(Class cls, SEL selector, const char **types) {
    unsigned count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    BOOL found = NO;
    for (unsigned i = 0; i < count; ++i) {
        if (method_getName(methods[i]) == selector) {
            if (types) *types = method_getTypeEncoding(methods[i]);
            found = YES;
            break;
        }
    }
    free(methods);
    return found;
}

static void installHook(Class cls, SEL selector, IMP replacement) {
    const char *types = NULL;
    if (!directMethod(cls, selector, &types) || !types) return;
    pthread_mutex_lock(&hookLock);
    for (size_t i = 0; i < hookCount; ++i) {
        if (hooks[i].cls == cls && hooks[i].sel == selector) {
            pthread_mutex_unlock(&hookLock);
            return;
        }
    }
    if (hookCount < sizeof(hooks) / sizeof(hooks[0])) {
        Method method = class_getInstanceMethod(cls, selector);
        IMP original = method_setImplementation(method, replacement);
        hooks[hookCount++] = (Hook){ .cls = cls, .sel = selector, .original = original };
        logLine("HOOK_INSTALLED pid=%d class=%s selector=%s", getpid(), class_getName(cls), sel_getName(selector));
    }
    pthread_mutex_unlock(&hookLock);
}

static void scanMetalClasses(void) {
    int count = objc_getClassList(NULL, 0);
    if (count <= 0) return;
    Class __unsafe_unretained *classes = (Class __unsafe_unretained *)calloc((size_t)count, sizeof(Class));
    if (!classes) return;
    count = objc_getClassList(classes, count);
    Protocol *commandBufferProtocol = objc_getProtocol("MTLCommandBuffer");
    Protocol *renderEncoderProtocol = objc_getProtocol("MTLRenderCommandEncoder");
    SEL renderEncoderSelector = @selector(renderCommandEncoderWithDescriptor:);
    SEL commitSelector = @selector(commit);
    SEL endEncodingSelector = @selector(endEncoding);
    for (int i = 0; i < count; ++i) {
        Class cls = classes[i];
        if (commandBufferProtocol && class_conformsToProtocol(cls, commandBufferProtocol))
            installHook(cls, renderEncoderSelector, (IMP)hookRenderCommandEncoder);
        if (commandBufferProtocol && class_conformsToProtocol(cls, commandBufferProtocol))
            installHook(cls, commitSelector, (IMP)hookCommandBufferCommit);
        if (renderEncoderProtocol && class_conformsToProtocol(cls, renderEncoderProtocol))
            installHook(cls, endEncodingSelector, (IMP)hookRenderEncoderEndEncoding);
    }
    free(classes);
}

static void *pollClasses(void *unused) {
    (void)unused;
    for (int i = 0; i < 240; ++i) { scanMetalClasses(); usleep(250000); }
    while (1) { scanMetalClasses(); sleep(2); }
    return NULL;
}

__attribute__((constructor)) static void observerInit(void) {
    captureProcessingQueue = dispatch_queue_create(
        "com.fgmetal.r100.capture-processing", DISPATCH_QUEUE_SERIAL);
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=R103-contiguous-frontier-one-shot DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s capture=%s present=%s deferredSource=%s closeFrontier=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R101_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R101_CAPTURE") ?: "0",
        getenv("FGMETAL_R101_PRESENT") ?: "0",
        getenv("FGMETAL_R102_DEFER_SOURCE") ?: "0",
        getenv("FGMETAL_R103_CLOSE_FRONTIER") ?: "0",
        (__bridge void *)captureProcessingQueue);
    (void)dlopen("/System/Library/Frameworks/QuartzCore.framework/QuartzCore", RTLD_LAZY | RTLD_LOCAL);
    (void)dlopen("/System/Library/Frameworks/Metal.framework/Metal", RTLD_LAZY | RTLD_LOCAL);
    Class layerClass = objc_getClass("CAMetalLayer");
    SEL nextDrawableSelector = @selector(nextDrawable);
    Method nextDrawableMethod = layerClass ? class_getInstanceMethod(layerClass, nextDrawableSelector) : NULL;
    if (nextDrawableMethod) {
        originalNextDrawable = method_setImplementation(nextDrawableMethod, (IMP)hookNextDrawable);
        logLine("HOOK_INSTALLED pid=%d class=CAMetalLayer selector=nextDrawable", getpid());
    } else {
        logLine("HOOK_UNAVAILABLE pid=%d class=CAMetalLayer selector=nextDrawable", getpid());
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, pollClasses, NULL) == 0) pthread_detach(thread);
}

__attribute__((destructor)) static void observerFinish(void) {
    pthread_mutex_lock(&stateLock);
    uint64_t attempts = presentationAttempts;
    uint64_t successes = internalPresentationSuccesses;
    uint64_t displayed = internalPresentationDisplayedCount;
    uint64_t fallbacks = internalPresentationFallbacks;
    uint64_t duplicates = duplicateInternalClaims;
    uint64_t stale = stalePairRejections;
    uint64_t r103Attempts = r103FrontierAttempts;
    uint64_t r103Contiguous = r103FrontierContiguousSuccesses;
    uint64_t r103Fallbacks = r103FrontierFallbacks;
    uint64_t r103BlockedAcquisitions = r103FrontierBlockedAcquisitions;
    uint64_t r103BlockedDispatches = r103FrontierBlockedDispatches;
    uint64_t r103Leaks = r103FutureSourceLeaks;
    pthread_mutex_unlock(&stateLock);
    logLine("R102_RUN_SUMMARY attemptedPairs=%llu successfulAToMTuples=%llu internalDispatches=%llu internalDisplayed=%llu recoveryFallbacks=%llu stalePairRejections=%llu staleCallbacks=%llu duplicateInternalDispatches=%llu wrongPairDispatches=%llu",
        (unsigned long long)r102NextTicketId,
        (unsigned long long)r102SuccessfulTupleCount,
        (unsigned long long)successes,
        (unsigned long long)displayed,
        (unsigned long long)r102FallbackCount,
        (unsigned long long)stale,
        (unsigned long long)r102StaleCallbackCount,
        (unsigned long long)r102DuplicateInternalDispatchCount,
        (unsigned long long)r102WrongPairDispatchCount);
    logLine("R103_RUN_SUMMARY tuplesAttempted=%llu contiguousSuccesses=%llu fallbacks=%llu sourceAcquisitionsHeld=%llu sourceDispatchesHeld=%llu futureSourceLeaksWhileClosed=%llu duplicateInternalDispatches=%llu wrongPairDispatches=%llu staleCallbacks=%llu",
        (unsigned long long)r103Attempts,
        (unsigned long long)r103Contiguous,
        (unsigned long long)r103Fallbacks,
        (unsigned long long)r103BlockedAcquisitions,
        (unsigned long long)r103BlockedDispatches,
        (unsigned long long)r103Leaks,
        (unsigned long long)r102DuplicateInternalDispatchCount,
        (unsigned long long)r102WrongPairDispatchCount,
        (unsigned long long)r102StaleCallbackCount);
}
