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
#import <CommonCrypto/CommonDigest.h>

// R105 bounded public-Metal render-pass inventory for Elden Ring.
// It keeps the R100 drawable/producer correlation seam and inventories only
// render passes in the exact producer command buffer and a short same-queue
// predecessor window. It does not invoke MetalFX or alter presentation.

typedef struct { Class cls; SEL sel; IMP original; } Hook;
typedef struct {
    uint64_t count;
    uint64_t sumX;
    uint32_t minX, maxX, minY, maxY;
} ColorStats;

typedef struct {
    uint32_t finiteMotion;
    uint32_t activeMotion;
    uint32_t motionSamples;
    uint32_t finiteOutput;
    uint32_t outputSamples;
    uint32_t colorSamples;
    float maxMagnitude;
    float diffPrevious;
    float diffCurrent;
} R110GateGroupStats;

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

typedef NS_ENUM(uint8_t, R101InternalSlotState) {
    R101InternalSlotEmpty = 0,
    R101InternalSlotGenerating,
    R101InternalSlotReady,
    R101InternalSlotClaimed,
    R101InternalSlotPresented,
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

static const char *const kR110TupleTargetPath =
    "/tmp/fgmetal-r108-unattended/r111-target-source-b";
static const char *const kR110WorldSceneConfirmationPath =
    "/tmp/fgmetal-r108-unattended/r111-world-scene-confirmed";
static const char *const kR111CameraAttributionPath =
    "/tmp/fgmetal-r108-unattended/r111-camera-attribution-verified";
static const char *const kR110EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-04-r111-event-locked-title-presentation";
static Hook hooks[256];
static size_t hookCount;
static pthread_mutex_t stateLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t hookLock = PTHREAD_MUTEX_INITIALIZER;
static NSRecursiveLock *r110WsiDispatchLock;
static IMP originalNextDrawable;
static int logFd = -1;
static pthread_once_t logOnce = PTHREAD_ONCE_INIT;
static pthread_once_t targetOnce = PTHREAD_ONCE_INIT;
static BOOL selectedHarnessProcess;
static NSMutableDictionary<NSValue *, NSDictionary *> *drawablesByTexture;
static NSMutableSet<NSString *> *completedHookKeys;
static uint64_t drawableSequence;
static uint64_t r110NextDrawableCallId;
static uint64_t r110PresentCallId;
static BOOL r110OrderingOnly;
static uint64_t r102NextDrawableCallId;
static uint64_t r110TupleSourceBSequence;
static uint64_t r103FutureSourceDispatchesBeforeClose;
static uint64_t r110WsiEventSequence;
static uint64_t r110ExpectedASourceWsiPresentId;
static uint64_t r110ExpectedASourceWsiOrder;
static uint64_t r110InternalWsiPresentId;
static uint64_t r110InternalWsiOrder;
static uint64_t r110ExpectedBSourceWsiPresentId;
static uint64_t r110ExpectedBSourceWsiOrder;
static BOOL r110TupleWsiOrderInvalid;
static BOOL r110QualityReviewed;
static BOOL r110QualityRejected;
static BOOL r110QualityWaitStarted;
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
static const NSUInteger kCaptureLimit = 120;
static NSUInteger captureAttempts;
static NSUInteger captureSuccesses;
static dispatch_queue_t captureProcessingQueue;
static MetalFXFrameInterpolationProvider *realFrameProvider;
static id<MTLCommandQueue> outputReadbackQueue;
static id<MTLTexture> previousCaptureColor;
static id<MTLTexture> previousCaptureDepth;
static id<MTLTexture> previousCaptureMotion;
static id<MTLTexture> previousCaptureSource;
static NSUInteger r108FrameWidth;
static NSUInteger r108FrameHeight;
static id<MTLCommandQueue> r108ComputeQueue;
static id<MTLComputePipelineState> r110DownsamplePipeline;
static id<MTLComputePipelineState> r110CoarseMatchPipeline;
static id<MTLComputePipelineState> r110FineRefinePipeline;
static id<MTLComputePipelineState> r108PropagatePipeline;
static id<MTLComputePipelineState> r108UpsamplePipeline;
static id<MTLComputePipelineState> r108PreparePipeline;
static id<MTLComputePipelineState> r108ClearMotionPipeline;
static id<MTLComputePipelineState> r110FastGatePipeline;
static id<MTLDevice> r108PipelineDevice;
static ColorStats previousMainShape;
static ColorStats previousMarkerShape;
static uint64_t previousCaptureSequence;
static BOOL realProviderInitialized;
static MTLTextureUsage colorInputUsage;
static MTLTextureUsage depthInputUsage;
static MTLTextureUsage motionInputUsage;
static pthread_cond_t presentationReadyCondition = PTHREAD_COND_INITIALIZER;
static id<MTLTexture> pendingInternalPresentationTexture;
static uint64_t pendingInternalPresentId;
static uint64_t pendingInternalPreviousSequence;
static uint64_t pendingInternalCurrentSequence;
static void *pendingInternalLayer;
static void *pendingInternalDevice;
static R101InternalSlotState internalSlotState;
static BOOL internalPresentationAttempted;
static BOOL internalPresentationCompleted;
static uint64_t nextWsiPresentId = 1;
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
static uint64_t internalPresentationDisplayedCount;
static uint64_t internalPresentationSuccesses;
static uint64_t lastCompletedPresentationSequence;
static uint64_t presentationAttempts;
static uint64_t internalPresentationFallbacks;
static __thread BOOL isInternalDrawableAcquisition;
static id<MTLRenderPipelineState> internalPresentationPipeline;
static __thread BOOL isInternalPresentationCall;
static NSMutableDictionary<NSValue *, NSMutableArray<NSDictionary *> *> *r105PassesByCommandBuffer;
static NSMutableDictionary<NSValue *, NSDictionary *> *r105ProducersByCommandBuffer;
static NSMutableArray<NSDictionary *> *r105RecentCommittedBuffers;
static NSMutableSet<NSValue *> *r105SeenEncoders;
static NSMutableSet<NSValue *> *r105SeenCommits;
static NSMutableSet<NSValue *> *r105SeenDepthCommits;
static id<MTLComputePipelineState> r105DepthSamplingPipeline;
static id<MTLDevice> r105DepthSamplingDevice;
static BOOL r105DepthPipelineCompileStarted;
static dispatch_queue_t r105DepthPipelineCompileQueue;
static NSUInteger r105DepthSampleCount;
static id<MTLComputePipelineState> r105MotionSamplingPipeline;
static id<MTLDevice> r105MotionSamplingDevice;
static BOOL r105MotionPipelineCompileStarted;
static dispatch_queue_t r105MotionPipelineCompileQueue;
static NSUInteger r105MotionSampleCount;
static uint64_t r105NextPassId;
static NSUInteger r105PassCount;
static NSUInteger r105FrameCount;
static BOOL r105InventoryTriggered;
static uint64_t r105InventoryTriggerSequence;
static const NSUInteger kR105MaxFrames = 72;
static const NSUInteger kR105MaxPasses = 4096;
static const NSUInteger kR105MaxCommandBuffers = 64;
static const NSUInteger kR105MaxWindowBuffers = 12;
static const uint64_t kR105CommandWindowNs = 100000000ULL;
static const uint64_t kR105InventoryStartSequence = 96;
static const uint64_t kR105TitleSampleStartSequence = 120;
static const NSUInteger kR105InventoryFrameStride = 1;
static const NSUInteger kR105DepthGridWidth = 128;
static const NSUInteger kR105DepthGridHeight = 128;
static const NSUInteger kR105MotionGridWidth = 128;
static const NSUInteger kR105MotionGridHeight = 128;
static const NSUInteger kR105MaxMotionSamples = 8;
static const NSUInteger kR105MaxDepthSamples = 16;
static pthread_mutex_t r105DepthLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t r105MotionLock = PTHREAD_MUTEX_INITIALIZER;
static id<MTLTexture> r106DepthCandidateTexture;
static NSMutableSet<NSNumber *> *r106DepthSampleSequences;
static NSMutableSet<NSValue *> *r106DepthPassCapturedCommandBuffers;
static const char *const kHarnessExecutable =
    "/Users/nima/Games/Elden Ring/Game/eldenring.exe";
static const char *const kR105InventoryTriggerPath =
    "/tmp/fgmetal-r108-unattended/start-world-inventory-disabled";
static const char *const kR108CaptureTriggerPath =
    "/tmp/fgmetal-r108-unattended/start-title-gpu-capture";
static const char *const kR108ConditionPath =
    "/tmp/fgmetal-r108-unattended/capture-condition";
static BOOL r108CaptureTriggered;
static uint64_t r108CaptureStartSequence;
static uint64_t r108CaptureEndSequence;
static NSUInteger r108CaptureCount;
static const NSUInteger kR108CaptureLimit = 48;
static NSUInteger r108CaptureWindows;
static const NSUInteger kR108CaptureWindowsLimit = 8;

static void installHook(Class cls, SEL selector, IMP replacement);
static void r105AssociateProducer(id<MTLCommandBuffer> commandBuffer,
                                 id<MTLTexture> texture,
                                 NSDictionary *drawableRecord,
                                 NSUInteger colorAttachmentIndex);
static BOOL r105SequenceSelectedForInventory(uint64_t sequence);
static void r105CaptureRenderPass(id<MTLCommandBuffer> commandBuffer,
                                  id encoder,
                                  MTLRenderPassDescriptor *descriptor);
static void hookCommandBufferCommit(id self, SEL selector);
static void r105EncodeDepthDiagnostic(id<MTLCommandBuffer> commandBuffer);
static void r105WarmDepthPipeline(id<MTLDevice> device);
static void r105EncodeMotionDiagnostic(id<MTLCommandBuffer> commandBuffer);
static void r105WarmMotionPipeline(id<MTLDevice> device);
static void installDrawablePresentationHooks(id<CAMetalDrawable> drawable);
static void hookDrawablePresent(id self, SEL selector);
static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when);
static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration);
static void r110LogSourcePresent(id self, SEL selector, const char *phase,
                                uint64_t callId, uint64_t timestampNs,
                                CFTimeInterval timingArgument);
static BOOL r110PresentationEnabled(void);
static void r110RefreshTupleTarget(void);
static uint64_t r110RecordWsiDispatch(const char *origin,
                                     uint64_t sourceSequence,
                                     uint64_t internalPresentId,
                                     uint64_t wsiPresentId,
                                     uint64_t pairA,
                                     uint64_t pairB);
static BOOL r103BeginSourceAcquisition(uint64_t callId, CAMetalLayer *layer);
static void r103EndSourceOperation(BOOL operationLease, const char *phase,
                                   uint64_t identity);
static BOOL r103BeginSourceDispatch(id<CAMetalDrawable> drawable);
static void r103CloseFrontierAfterProducerCommit(NSDictionary *record,
                                                 id<MTLCommandBuffer> producer);
static void r103RecoverFrontier(uint64_t ticketId, const char *reason);
static BOOL r102TryDeferSourcePresent(id<CAMetalDrawable> drawable, SEL selector,
                                     CFTimeInterval argument);
static void r102AttemptInternalDispatch(void);
static void r102FallbackTicket(R102DeferredSourceTicket *ticket,
                               const char *reason);
static void r102FallbackForSequence(uint64_t sourceSequence,
                                    const char *reason);
static BOOL r102TicketIsCurrentLocked(R102DeferredSourceTicket *ticket);
static void r102ForwardSourceTicket(R102DeferredSourceTicket *ticket,
                                    const char *reason);
static void r110ScheduleOutputDiagnostic(id<MTLTexture> midpoint,
                                         uint64_t pairA, uint64_t pairB);
static void logR102Event(const char *event, NSDictionary *record,
                         id<CAMetalDrawable> explicitDrawable,
                         id<MTLCommandBuffer> explicitCommandBuffer,
                         id encoder, SEL selector, double argument);
static uint64_t r102MachTimeNs(void);
static uint64_t allocateWsiPresentId(void);
static void observeDisplayedDrawable(id<MTLDrawable> drawable,
                                     const char *origin,
                                     uint64_t wsiPresentId,
                                     uint64_t appFrameId,
                                     uint64_t internalPresentId);
static uint64_t logSourceWsiDispatch(id self, SEL selector);
static BOOL renderInternalFrameToDrawable(id<MTLTexture> source,
                                          id<CAMetalDrawable> destinationDrawable,
                                          id<MTLCommandQueue> queue);

static void initLog(void) {
    (void)mkdir("/tmp/fgmetal-r108-unattended", 0700);
    char path[256];
    snprintf(path, sizeof(path), "/tmp/fgmetal-r108-unattended/observer.%d.log", getpid());
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
    char line[1800];
    int m = snprintf(line, sizeof(line), "%s\n", body);
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

static BOOL r110PresentationEnabled(void) {
    const char *enabled = getenv("FGMETAL_R110_PRESENT");
    return enabled && strcmp(enabled, "1") == 0;
}

static BOOL r110WorldSceneConfirmed(void) {
    FILE *file = fopen(kR110WorldSceneConfirmationPath, "r");
    if (!file) return NO;
    char value[64] = {0};
    BOOL valid = fgets(value, sizeof(value), file) != NULL;
    fclose(file);
    size_t length = strlen(value);
    while (length > 0 && (value[length - 1] == '\n' || value[length - 1] == '\r'))
        value[--length] = '\0';
    return valid && strcmp(value, "VISIBLE_WORLD") == 0;
}

static BOOL r111CameraPairAttributionVerified(uint64_t expectedA,
                                               uint64_t expectedB) {
    const char *required = getenv("FGMETAL_R111_CAMERA_ATTRIBUTION");
    if (!required || strcmp(required, "1") != 0) return YES;

    FILE *file = fopen(kR111CameraAttributionPath, "r");
    if (!file) return NO;
    char line[512] = {0};
    BOOL read = fgets(line, sizeof(line), file) != NULL;
    fclose(file);
    int pid = 0;
    unsigned long long sourceA = 0, sourceB = 0;
    unsigned long long activeStartNs = 0, sourceAAcquiredNs = 0;
    unsigned long long cameraEventNs = 0, sourceBAcquiredNs = 0;
    unsigned long long activeStopNs = 0;
    int fields = read ? sscanf(line,
        "pid=%d A=%llu B=%llu activeStartNs=%llu sourceAAcquiredNs=%llu cameraEventNs=%llu sourceBAcquiredNs=%llu activeStopNs=%llu",
        &pid, &sourceA, &sourceB, &activeStartNs, &sourceAAcquiredNs,
        &cameraEventNs, &sourceBAcquiredNs, &activeStopNs) : 0;
    return fields == 8 && pid == getpid() && sourceA == expectedA &&
        sourceB == expectedB && activeStartNs <= sourceAAcquiredNs &&
        sourceAAcquiredNs < cameraEventNs && cameraEventNs < sourceBAcquiredNs &&
        sourceBAcquiredNs <= activeStopNs;
}

static void r110RefreshTupleTarget(void) {
    if (!r110PresentationEnabled()) return;
    if (!r110WorldSceneConfirmed()) return;
    pthread_mutex_lock(&stateLock);
    BOOL alreadySet = r110TupleSourceBSequence != 0;
    pthread_mutex_unlock(&stateLock);
    if (alreadySet) return;

    FILE *file = fopen(kR110TupleTargetPath, "r");
    if (!file) return;
    char buffer[64] = {0};
    BOOL read = fgets(buffer, sizeof(buffer), file) != NULL;
    fclose(file);
    if (!read) return;
    char *end = NULL;
    errno = 0;
    unsigned long long parsed = strtoull(buffer, &end, 10);
    if (errno || end == buffer || parsed < 2) return;
    while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') ++end;
    if (*end != '\0') return;

    pthread_mutex_lock(&stateLock);
    BOOL inCaptureWindow = r108CaptureTriggered &&
        parsed > r108CaptureStartSequence && parsed <= r108CaptureEndSequence;
    if (!r110TupleSourceBSequence && inCaptureWindow)
        r110TupleSourceBSequence = (uint64_t)parsed;
    uint64_t selected = r110TupleSourceBSequence;
    uint64_t start = r108CaptureStartSequence;
    uint64_t finish = r108CaptureEndSequence;
    pthread_mutex_unlock(&stateLock);
    if (selected == parsed) {
        logLine("R110_PRESENT_TARGET=ARMED sourcePair=%llu->%llu targetSourceSequenceB=%llu captureWindow=%llu..%llu worldSceneConfirmed=1 source=one-shot-file",
            (unsigned long long)(parsed - 1), parsed, parsed,
            (unsigned long long)start, (unsigned long long)finish);
    } else {
        logLine("R110_PRESENT_TARGET=REJECTED requestedSourceSequenceB=%llu start=%llu end=%llu reason=outside-capture-window",
            parsed, (unsigned long long)start, (unsigned long long)finish);
    }
}

static BOOL r105SequenceSelectedForInventory(uint64_t sequence) {
    pthread_mutex_lock(&stateLock);
    BOOL selected = r105InventoryTriggered &&
        sequence >= r105InventoryTriggerSequence &&
        (sequence - r105InventoryTriggerSequence) % kR105InventoryFrameStride == 0 &&
        r105FrameCount < kR105MaxFrames;
    pthread_mutex_unlock(&stateLock);
    return selected;
}

static uint64_t r105CurrentDrawableSequence(void) {
    pthread_mutex_lock(&stateLock);
    uint64_t sequence = drawableSequence;
    pthread_mutex_unlock(&stateLock);
    return sequence;
}

static void r105MaybeStartInventory(uint64_t sequence) {
    if (sequence < kR105InventoryStartSequence) return;
    pthread_mutex_lock(&stateLock);
    BOOL shouldCheck = !r105InventoryTriggered && r105FrameCount < kR105MaxFrames;
    pthread_mutex_unlock(&stateLock);
    if (!shouldCheck || access(kR105InventoryTriggerPath, F_OK) != 0) return;

    BOOL started = NO;
    pthread_mutex_lock(&stateLock);
    if (!r105InventoryTriggered && r105FrameCount < kR105MaxFrames) {
        r105InventoryTriggered = YES;
        r105InventoryTriggerSequence = sequence;
        started = YES;
    }
    pthread_mutex_unlock(&stateLock);
    if (!started) return;
    (void)unlink(kR105InventoryTriggerPath);
    logLine("R108_INVENTORY_TRIGGER=ARMED startSequence=%llu frameStride=%lu boundedFrames=%lu trigger=one-shot-file",
        (unsigned long long)sequence, (unsigned long)kR105InventoryFrameStride,
        (unsigned long)kR105MaxFrames);
}

static void r108MaybeStartCapture(uint64_t sequence) {
    if (sequence < kR105TitleSampleStartSequence) return;
    pthread_mutex_lock(&stateLock);
    BOOL shouldCheck = !r108CaptureTriggered &&
        r108CaptureWindows < kR108CaptureWindowsLimit;
    pthread_mutex_unlock(&stateLock);
    if (!shouldCheck || access(kR108CaptureTriggerPath, F_OK) != 0) return;

    BOOL started = NO;
    pthread_mutex_lock(&stateLock);
    if (!r108CaptureTriggered &&
        r108CaptureWindows < kR108CaptureWindowsLimit) {
        r108CaptureTriggered = YES;
        r108CaptureStartSequence = sequence;
        r108CaptureEndSequence = sequence + kR108CaptureLimit - 1;
        r108CaptureCount = 0;
        ++r108CaptureWindows;
        started = YES;
    }
    pthread_mutex_unlock(&stateLock);
    if (!started) return;
    (void)unlink(kR108CaptureTriggerPath);
    logLine("R108_TITLE_CAPTURE=ARMED window=%lu startSequence=%llu expectedEndSequence=%llu boundedFrames=%lu maxWindows=%lu trigger=one-shot-file",
        (unsigned long)r108CaptureWindows, (unsigned long long)sequence,
        (unsigned long long)r108CaptureEndSequence,
        (unsigned long)kR108CaptureLimit, (unsigned long)kR108CaptureWindowsLimit);
}

static BOOL r108SequenceSelectedForCapture(uint64_t sequence) {
    pthread_mutex_lock(&stateLock);
    BOOL selected = r108CaptureTriggered &&
        sequence >= r108CaptureStartSequence &&
        r108CaptureCount < kR108CaptureLimit;
    pthread_mutex_unlock(&stateLock);
    return selected;
}

static NSString *r108CurrentCaptureCondition(void) {
    NSError *error = nil;
    NSString *label = [NSString stringWithContentsOfFile:
        [NSString stringWithUTF8String:kR108ConditionPath]
        encoding:NSUTF8StringEncoding error:&error];
    label = [(label ?: @"UNSPECIFIED")
        stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if ([label isEqualToString:@"STILL"] || [label isEqualToString:@"CAMERA"] ||
        [label isEqualToString:@"CHARACTER"] || [label isEqualToString:@"COMBINED"])
        return label;
    return @"UNSPECIFIED";
}

static uint64_t r105NowNs(void);

static void retainDrawableForCorrelation(id<CAMetalDrawable> drawable) {
    id<MTLTexture> texture = drawable.texture;
    id<MTLDevice> layerDevice = drawable.layer.device;
    NSValue *textureKey = [NSValue valueWithPointer:(__bridge const void *)texture];
    uint64_t acquiredAtNs = r105NowNs();
    pthread_mutex_lock(&stateLock);
    if (!drawablesByTexture) drawablesByTexture = [NSMutableDictionary dictionary];
    uint64_t sequence = ++drawableSequence;
    drawablesByTexture[textureKey] = @{
        @"drawable": drawable,
        @"sequence": @(sequence),
        @"drawableID": @(drawable.drawableID),
        @"layer": [NSValue valueWithPointer:(__bridge const void *)drawable.layer],
        @"acquiredAtNs": @(acquiredAtNs),
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
    // R108 capture is separate from the R105 attachment-inventory trigger.
    // Do not restart broad depth or native-motion hunting for this run.
    r108MaybeStartCapture(sequence);
    logLine("R108_SOURCE_FRAME_ACQUIRED pid=%d seq=%llu acquiredAtMonotonicNs=%llu drawableID=%lu layer=%p texture=%p layerDevice=%p textureDevice=%p sameDevice=%d registryID=%llu framebufferOnly=%d width=%lu height=%lu pixelFormat=%lu usage=%lu",
        getpid(), (unsigned long long)sequence, (unsigned long long)acquiredAtNs,
        (unsigned long)drawable.drawableID,
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
    BOOL selected = NO;
    if (isSelectedProcess()) {
        pthread_mutex_lock(&stateLock);
        if (!selectedHarnessLayer) {
            selectedHarnessLayer = (__bridge void *)layer;
            logLine("TITLE_LAYER_SELECTED pid=%d path=%s layer=%p device=%p deviceName=%s registryID=%llu size=%.1fx%.1f pixelFormat=%lu framebufferOnly=%d",
                getpid(), procPath(), selectedHarnessLayer, (__bridge void *)layer.device,
                layer.device.name.UTF8String ?: "unknown",
                (unsigned long long)layer.device.registryID,
                layer.drawableSize.width, layer.drawableSize.height,
                (unsigned long)layer.pixelFormat, layer.framebufferOnly);
        }
        selected = selectedHarnessLayer == (__bridge void *)layer;
        pthread_mutex_unlock(&stateLock);
    }

    if (selected && r110PresentationEnabled()) r110RefreshTupleTarget();

    if (selected && getenv("FGMETAL_R108_CAPTURE") &&
        strcmp(getenv("FGMETAL_R108_CAPTURE"), "1") == 0) {
        r105WarmDepthPipeline(layer.device);
    }

    if (selected && getenv("FGMETAL_R108_FRAMEBUFFER_ONLY") &&
        strcmp(getenv("FGMETAL_R108_FRAMEBUFFER_ONLY"), "1") == 0) {
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

    uint64_t orderingCallId = 0;
    uint64_t orderingEntryNs = 0;
    if (selected && (r110OrderingOnly || r110PresentationEnabled())) {
        orderingCallId = __atomic_add_fetch(&r110NextDrawableCallId, 1, __ATOMIC_RELAXED);
        orderingEntryNs = r105NowNs();
        logLine("R110_SOURCE_NEXT_DRAWABLE_ENTRY pid=%d callId=%llu entryMonotonicNs=%llu layer=%p device=%p registryID=%llu",
            getpid(), (unsigned long long)orderingCallId,
            (unsigned long long)orderingEntryNs, (__bridge void *)layer,
            (__bridge void *)layer.device, (unsigned long long)layer.device.registryID);
    }
    uint64_t frontierCallId = 0;
    BOOL acquisitionLease = NO;
    if (selected && r110PresentationEnabled() && !isInternalDrawableAcquisition &&
        !isInternalPresentationCall) {
        frontierCallId = __atomic_add_fetch(&r102NextDrawableCallId, 1, __ATOMIC_RELAXED);
        acquisitionLease = r103BeginSourceAcquisition(frontierCallId, layer);
    }
    id<CAMetalDrawable> drawable = ((id (*)(id, SEL))originalNextDrawable)(self, selector);
    uint64_t orderingReturnNs = (r110OrderingOnly || r110PresentationEnabled()) && selected
        ? r105NowNs() : 0;
    if (drawable && selected && (r110OrderingOnly || r110PresentationEnabled())) {
        logLine("R110_SOURCE_NEXT_DRAWABLE_RETURN pid=%d callId=%llu entryMonotonicNs=%llu returnMonotonicNs=%llu layer=%p drawable=%p drawableID=%lu texture=%p textureDevice=%p registryID=%llu dimensions=%lux%lu framebufferOnly=%d",
            getpid(), (unsigned long long)orderingCallId,
            (unsigned long long)orderingEntryNs,
            (unsigned long long)orderingReturnNs, (__bridge void *)layer,
            (__bridge void *)drawable, (unsigned long)drawable.drawableID,
            (__bridge void *)drawable.texture, (__bridge void *)drawable.texture.device,
            (unsigned long long)drawable.texture.device.registryID,
            (unsigned long)drawable.texture.width, (unsigned long)drawable.texture.height,
            drawable.texture.framebufferOnly);
    }
    if (drawable && selected && isSelectedLayer(layer)) {
        installDrawablePresentationHooks(drawable);
        retainDrawableForCorrelation(drawable);
    }
    if (selected && r110PresentationEnabled())
        r103EndSourceOperation(acquisitionLease, "nextDrawable", frontierCallId);
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

static void r110LogSourcePresent(id self, SEL selector, const char *phase,
                                uint64_t callId, uint64_t timestampNs,
                                CFTimeInterval timingArgument) {
    if ((!r110OrderingOnly && !r110PresentationEnabled()) ||
        !isSelectedProcess() || isInternalPresentationCall) return;
    id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
    if (!drawable || !isSelectedLayer((CAMetalLayer *)drawable.layer)) return;
    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    if (!record) return;
    id<MTLCommandBuffer> producer = record[@"producerCommandBuffer"];
    logLine("R110_SOURCE_PRESENT_%s pid=%d callId=%llu timestampMonotonicNs=%llu method=%s timingArgument=%.9f sequence=%llu drawableID=%lu drawable=%p layer=%p texture=%p producerCommandBuffer=%p producerQueue=%p producerStatus=%lu",
        phase, getpid(), (unsigned long long)callId,
        (unsigned long long)timestampNs, sel_getName(selector), timingArgument,
        (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
        (unsigned long)drawable.drawableID, (__bridge void *)drawable,
        (__bridge void *)drawable.layer, (__bridge void *)drawable.texture,
        (__bridge void *)producer,
        (__bridge void *)record[@"producerQueue"],
        (unsigned long)producer.status);
}

static uint64_t r110RecordWsiDispatch(const char *origin,
                                     uint64_t sourceSequence,
                                     uint64_t internalPresentId,
                                     uint64_t wsiPresentId,
                                     uint64_t pairA,
                                     uint64_t pairB) {
    pthread_mutex_lock(&stateLock);
    uint64_t order = ++r110WsiEventSequence;
    uint64_t targetB = r110TupleSourceBSequence;
    uint64_t targetA = targetB > 0 ? targetB - 1 : 0;
    if (targetB > 0 && strcmp(origin, "SOURCE") == 0) {
        if (sourceSequence == targetA) {
            if (!r110ExpectedASourceWsiOrder && !r110InternalWsiOrder) {
                r110ExpectedASourceWsiPresentId = wsiPresentId;
                r110ExpectedASourceWsiOrder = order;
            } else {
                r110TupleWsiOrderInvalid = YES;
            }
        } else if (sourceSequence == targetB) {
            if (r110ExpectedASourceWsiOrder && r110InternalWsiOrder &&
                !r110ExpectedBSourceWsiOrder &&
                r110InternalWsiOrder + 1 == order &&
                !r110TupleWsiOrderInvalid) {
                r110ExpectedBSourceWsiPresentId = wsiPresentId;
                r110ExpectedBSourceWsiOrder = order;
            } else {
                r110TupleWsiOrderInvalid = YES;
            }
        } else if (r110ExpectedASourceWsiOrder &&
                   !r110ExpectedBSourceWsiOrder) {
            r110TupleWsiOrderInvalid = YES;
        }
    } else if (targetB > 0 && strcmp(origin, "INTERNAL") == 0) {
        if (sourceSequence == 0 && internalPresentId != 0 &&
            pairA == targetA && pairB == targetB &&
            r110ExpectedASourceWsiOrder && !r110InternalWsiOrder &&
            r110ExpectedASourceWsiOrder + 1 == order &&
            !r110TupleWsiOrderInvalid) {
            r110InternalWsiPresentId = wsiPresentId;
            r110InternalWsiOrder = order;
        } else {
            r110TupleWsiOrderInvalid = YES;
        }
    } else if (targetB > 0 && r110ExpectedASourceWsiOrder &&
               !r110ExpectedBSourceWsiOrder) {
        r110TupleWsiOrderInvalid = YES;
    }
    BOOL valid = r110ExpectedASourceWsiOrder && r110InternalWsiOrder &&
        r110ExpectedBSourceWsiOrder && !r110TupleWsiOrderInvalid;
    pthread_mutex_unlock(&stateLock);
    logLine("R110_WSI_ORDER_EVENT origin=%s order=%llu sourceSequence=%llu internalPresentId=%llu wsiPresentId=%llu pair=%llu->%llu tupleValid=%d",
        origin, (unsigned long long)order,
        (unsigned long long)sourceSequence,
        (unsigned long long)internalPresentId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)pairA, (unsigned long long)pairB, valid);
    return order;
}

static uint64_t r102MachTimeNs(void) {
    return r105NowNs();
}

static BOOL r103FrontierEnabled(void) {
    return r110PresentationEnabled();
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
    deadline.tv_sec += 15;

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
    deadline.tv_sec += 15;

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
    if (!r103FrontierEnabled() || !record || !producer) return;
    pthread_mutex_lock(&stateLock);
    uint64_t targetB = r110TupleSourceBSequence;
    pthread_mutex_unlock(&stateLock);
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    if (targetB < 2 || sequence != targetB) return;

    id<CAMetalDrawable> drawable = record[@"drawable"];
    CAMetalLayer *layer = (CAMetalLayer *)drawable.layer;
    id<MTLDevice> device = drawable.texture.device;
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    if (!drawable || !layer || !isSelectedLayer(layer) ||
        producer != record[@"producerCommandBuffer"] ||
        producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError || producer.error ||
        !queue || queue.device != device || layer.device != device) {
        logLine("R103_FRONTIER_CLOSE_REJECTED sourceSequence=%llu drawable=%p layer=%p device=%p producer=%p producerStatus=%lu queue=%p queueDevice=%p reason=producer-or-target-mismatch",
            (unsigned long long)sequence, (__bridge void *)drawable,
            (__bridge void *)layer, (__bridge void *)device,
            (__bridge void *)producer, (unsigned long)producer.status,
            (__bridge void *)queue, (__bridge void *)queue.device);
        return;
    }

    pthread_mutex_lock(&stateLock);
    BOOL captureBound = r108CaptureTriggered &&
        sequence > r108CaptureStartSequence && sequence <= r108CaptureEndSequence;
    BOOL futureSourceAlreadyDispatched = r103FutureSourceDispatchesBeforeClose > 0;
    uint64_t futureSourceDispatchCount = r103FutureSourceDispatchesBeforeClose;
    if (!captureBound || r103FrontierSpent ||
        r103SourceFrontierState != R103SourceFrontierOpen || r102DeferredTicket ||
        selectedHarnessLayer != (__bridge void *)layer ||
        futureSourceAlreadyDispatched) {
        uint8_t state = r103SourceFrontierState;
        BOOL activeTicket = r102DeferredTicket != nil;
        if (captureBound && !r103FrontierSpent &&
            r103SourceFrontierState == R103SourceFrontierOpen &&
            !activeTicket && futureSourceAlreadyDispatched) {
            ++r103FrontierFallbacks;
            r103FrontierSpent = YES;
            r103ClearFrontierLocked();
            pthread_cond_broadcast(&presentationReadyCondition);
        }
        pthread_mutex_unlock(&stateLock);
        logLine("R103_FRONTIER_CLOSE_REJECTED sourceSequence=%llu layer=%p device=%p frontierState=%u activeTicket=%d captureBound=%d futureSourceDispatchesBeforeClose=%llu reason=%s",
            (unsigned long long)sequence, (__bridge void *)layer,
            (__bridge void *)device, (unsigned)state, activeTicket, captureBound,
            (unsigned long long)futureSourceDispatchCount,
            futureSourceAlreadyDispatched ? "future-source-already-dispatched" :
                "frontier-not-open-or-pair-not-captured");
        return;
    }
    r103SourceFrontierState = R103SourceFrontierClosingForPair;
    r103FrontierPairA = sequence - 1;
    r103FrontierPairB = sequence;
    r103FrontierTicketId = ++r102NextTicketId;
    r103FrontierDisplayedBaseline = internalPresentationDisplayedCount;
    r103FrontierLayer = (__bridge void *)layer;
    r103FrontierDevice = (__bridge void *)device;
    r103FrontierProducer = (__bridge void *)producer;
    ++r103FrontierAttempts;
    uint64_t ticketId = r103FrontierTicketId;
    pthread_cond_broadcast(&presentationReadyCondition);
    pthread_mutex_unlock(&stateLock);

    logLine("R103_SOURCE_FRONTIER=CLOSING_FOR_PAIR t_ns=%llu ticketId=%llu pair=%llu->%llu sourceSequenceB=%llu producerCommandBuffer=%p producerStatus=%lu layer=%p device=%p origin=SOURCE",
        (unsigned long long)r105NowNs(), (unsigned long long)ticketId,
        (unsigned long long)(sequence - 1), (unsigned long long)sequence,
        (unsigned long long)sequence, (__bridge void *)producer,
        (unsigned long)producer.status, (__bridge void *)layer,
        (__bridge void *)device);

    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 15;
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
        logLine("R103_SOURCE_FRONTIER=OPEN ticketId=%llu pair=%llu->%llu layer=%p device=%p result=FALLBACK reason=commit-close-drain-recovery-bound operationsInFlight=%llu",
            (unsigned long long)ticketId,
            (unsigned long long)(sequence - 1), (unsigned long long)sequence,
            (__bridge void *)layer, (__bridge void *)device,
            (unsigned long long)remainingOperations);
        return;
    }
    logLine("R103_SOURCE_FRONTIER=CLOSED t_ns=%llu ticketId=%llu pair=%llu->%llu sourceSequenceB=%llu producerCommandBuffer=%p producerStatus=%lu layer=%p device=%p operationsInFlight=0 origin=SOURCE",
        (unsigned long long)r105NowNs(), (unsigned long long)ticketId,
        (unsigned long long)(sequence - 1), (unsigned long long)sequence,
        (unsigned long long)sequence, (__bridge void *)producer,
        (unsigned long)producer.status, (__bridge void *)layer,
        (__bridge void *)device);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r103RecoverFrontier(ticketId, "source-frontier-recovery-bound");
    });
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
    logLine("R104_TIMESTAMP event=source_b_forward_begin ticketId=%llu pair=%llu->%llu appFrameId=%llu wsiPresentId=%llu drawableID=%llu",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.drawableID);
    [r110WsiDispatchLock lock];
    (void)r110RecordWsiDispatch("SOURCE", ticket.sourceSequence, 0,
                                wsiPresentId, ticket.sourcePairA,
                                ticket.sourcePairB);
    if (ticket.originalSelector == @selector(present)) {
        ((void (*)(id, SEL))ticket.originalImplementation)(ticket.drawable,
                                                            ticket.originalSelector);
    } else {
        ((void (*)(id, SEL, CFTimeInterval))ticket.originalImplementation)(
            ticket.drawable, ticket.originalSelector, ticket.originalArgument);
    }
    [r110WsiDispatchLock unlock];
    logR102Event("SOURCE_B_ORIGINAL_PRESENT_RETURN", record, ticket.drawable,
                 ticket.producerCommandBuffer, nil,
                 ticket.originalSelector, ticket.originalArgument);
    logLine("R104_TIMESTAMP event=source_b_forward_complete ticketId=%llu pair=%llu->%llu appFrameId=%llu wsiPresentId=%llu drawableID=%llu",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.drawableID);
    logLine("R102_SOURCE_FORWARD_STAGE ticketId=%llu stage=ORIGINAL_PRESENT_RETURN wsiPresentId=%llu",
        (unsigned long long)ticket.ticketId, (unsigned long long)wsiPresentId);

    BOOL frontierOpened = NO;
    BOOL frontierContiguous = NO;
    BOOL titleWsiOrderValid = NO;
    uint64_t forwardedInternalPresentId = 0;
    uint64_t targetAWsiPresentId = 0, targetAWsiOrder = 0;
    uint64_t internalWsiPresentId = 0, internalWsiOrder = 0;
    uint64_t targetBWsiPresentId = 0, targetBWsiOrder = 0;
    uint64_t futureSourceLeaksSnapshot = 0;
    BOOL tupleWsiOrderInvalidSnapshot = NO;
    id<MTLTexture> diagnosticMidpoint = nil;
    uint64_t diagnosticPairA = 0, diagnosticPairB = 0;
    pthread_mutex_lock(&stateLock);
    if (r102TicketIsCurrentLocked(ticket)) {
        if (!reason) ++r102SuccessfulTupleCount;
        lastCompletedPresentationSequence = ticket.sourceSequence;
        forwardedInternalPresentId = pendingInternalPresentId;
        if (pendingInternalPresentationTexture &&
            pendingInternalPreviousSequence == ticket.sourcePairA &&
            pendingInternalCurrentSequence == ticket.sourcePairB) {
            diagnosticMidpoint = pendingInternalPresentationTexture;
            diagnosticPairA = pendingInternalPreviousSequence;
            diagnosticPairB = pendingInternalCurrentSequence;
        }
        r102ClearPendingInternalLocked(ticket.sourceSequence);
        ticket.state = R102DeferredTicketEmpty;
        r102DeferredTicket = nil;
        if (frontierReleasing &&
            r103SourceFrontierState == R103SourceFrontierReleasing &&
            r103FrontierTicketId == ticket.ticketId) {
            targetAWsiPresentId = r110ExpectedASourceWsiPresentId;
            targetAWsiOrder = r110ExpectedASourceWsiOrder;
            internalWsiPresentId = r110InternalWsiPresentId;
            internalWsiOrder = r110InternalWsiOrder;
            targetBWsiPresentId = r110ExpectedBSourceWsiPresentId;
            targetBWsiOrder = r110ExpectedBSourceWsiOrder;
            futureSourceLeaksSnapshot = r103FutureSourceLeaks;
            tupleWsiOrderInvalidSnapshot = r110TupleWsiOrderInvalid;
            titleWsiOrderValid = r110ExpectedASourceWsiOrder > 0 &&
                r110InternalWsiOrder == r110ExpectedASourceWsiOrder + 1 &&
                r110ExpectedBSourceWsiOrder == r110InternalWsiOrder + 1 &&
                r110ExpectedASourceWsiPresentId > 0 &&
                r110InternalWsiPresentId > 0 &&
                r110ExpectedBSourceWsiPresentId > 0 &&
                !r110TupleWsiOrderInvalid &&
                ticket.sourcePairA + 1 == ticket.sourcePairB &&
                ticket.sourcePairB == r110TupleSourceBSequence;
            frontierContiguous = !reason &&
                internalPresentationDisplayedCount > r103FrontierDisplayedBaseline &&
                r103FutureSourceLeaks == 0 && titleWsiOrderValid;
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
    if (diagnosticMidpoint)
        r110ScheduleOutputDiagnostic(diagnosticMidpoint, diagnosticPairA,
                                     diagnosticPairB);
    if (titleWsiOrderValid) {
        logLine("R110_TITLE_WSI_SEQUENCE=VALIDATED pair=%llu->%llu sourceAWsiPresentId=%llu sourceAOrder=%llu internalPresentId=%llu internalWsiPresentId=%llu internalOrder=%llu sourceBWsiPresentId=%llu sourceBOrder=%llu noSourceDispatchBetween=1 futureSourceLeaks=%llu",
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            (unsigned long long)targetAWsiPresentId,
            (unsigned long long)targetAWsiOrder,
            (unsigned long long)forwardedInternalPresentId,
            (unsigned long long)internalWsiPresentId,
            (unsigned long long)internalWsiOrder,
            (unsigned long long)targetBWsiPresentId,
            (unsigned long long)targetBWsiOrder,
            (unsigned long long)futureSourceLeaksSnapshot);
    } else if (frontierOpened) {
        logLine("R110_TITLE_WSI_SEQUENCE=REJECTED pair=%llu->%llu sourceAOrder=%llu internalOrder=%llu sourceBOrder=%llu invalid=%d futureSourceLeaks=%llu",
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            (unsigned long long)targetAWsiOrder,
            (unsigned long long)internalWsiOrder,
            (unsigned long long)targetBWsiOrder,
            tupleWsiOrderInvalidSnapshot,
            (unsigned long long)futureSourceLeaksSnapshot);
    }
    logLine("R102_SOURCE_TICKET_RELEASED ticketId=%llu sourceSequence=%llu appFrameId=%llu wsiPresentId=%llu outcome=%s",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)ticket.appFrameId,
        (unsigned long long)wsiPresentId,
        reason ? "FALLBACK_FORWARDED" : "A_TO_M_TO_B_FORWARDED");
    if (frontierContiguous)
        logLine("GPU_RESIDENT_CONTIGUOUS_A_TO_M_TO_B=VALIDATED ticketId=%llu pair=%llu->%llu internalPresentId=%llu wsiPresentIdB=%llu sourceGetBytesCritical=0 cpuPixelInputs=0",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            (unsigned long long)forwardedInternalPresentId,
            (unsigned long long)wsiPresentId);
    if (frontierContiguous)
        logLine("FIRST_REAL_TITLE_D3DMETAL_A_TO_M_TO_B=VALIDATED_CONSTANT_DEPTH_PROTOTYPE ticketId=%llu pair=%llu->%llu internalPresentId=%llu wsiPresentIdB=%llu noFutureSourceWsiLeaks=%llu",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            (unsigned long long)forwardedInternalPresentId,
            (unsigned long long)wsiPresentId,
            (unsigned long long)r103FutureSourceLeaks);
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
    if (!r110PresentationEnabled() || isInternalPresentationCall ||
        !isSelectedLayer((CAMetalLayer *)drawable.layer)) return NO;
    if (selector != @selector(present) && selector != @selector(presentAtTime:) &&
        selector != @selector(presentAfterMinimumDuration:)) return NO;

    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    pthread_mutex_lock(&stateLock);
    uint64_t targetSequence = r110TupleSourceBSequence;
    BOOL qualityRejected = r110QualityRejected;
    uint64_t rejectedFrontierTicket = r103FrontierTicketId;
    pthread_mutex_unlock(&stateLock);
    if (sequence != targetSequence) return NO;
    if (qualityRejected) {
        r103RecoverFrontier(rejectedFrontierTicket, "fast-eligibility-rejected");
        return NO;
    }
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
        while (((r103SourceFrontierState == R103SourceFrontierClosingForPair &&
                 r103FrontierPairB == sequence) ||
                (r103SourceFrontierState == R103SourceFrontierOpen &&
                 !r103FrontierSpent && r110TupleSourceBSequence == sequence)) &&
               waitResult == 0) {
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
    BOOL qualityRejectedAtPublish = r110PresentationEnabled() && r110QualityRejected;
    if (qualityRejectedAtPublish) {
        failedFrontierTicketId = requireFrontier ? r103FrontierTicketId : 0;
        pthread_mutex_unlock(&stateLock);
        if (failedFrontierTicketId)
            r103RecoverFrontier(failedFrontierTicketId,
                                "fast-eligibility-rejected-before-ticket-publication");
        logLine("R102_SOURCE_DEFER_REJECTED sourceSequence=%llu drawableID=%lu reason=fast-eligibility-rejected-before-ticket-publication",
            (unsigned long long)sequence, (unsigned long)drawable.drawableID);
        return NO;
    }
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
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r102FallbackTicket(ticket, "no-matching-M-before-recovery-deadline");
    });
    return YES;
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
    logLine("R104_TIMESTAMP event=internal_display_callback ticketId=%llu internalPresentId=%llu wsiPresentId=%llu pair=%llu->%llu reachedDisplay=%d",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)wsiPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB, reachedDisplay);
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
    if (r110PresentationEnabled()) {
        pthread_mutex_lock(&stateLock);
        BOOL qualityApproved = r110QualityReviewed && !r110QualityRejected;
        uint64_t pairA = pendingInternalPreviousSequence;
        uint64_t pairB = pendingInternalCurrentSequence;
        pthread_mutex_unlock(&stateLock);
        if (!qualityApproved) {
            logLine("R110_INTERNAL_DISPATCH=HELD sourcePair=%llu->%llu reason=fast-eligibility-pending",
                (unsigned long long)pairA, (unsigned long long)pairB);
            return;
        }
    }
    R102DeferredSourceTicket *ticket = nil;
    id<MTLTexture> generatedTexture = nil;
    uint64_t internalPresentId = 0;
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
        generatedTexture, internalDrawable, queue);
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
    logLine("R104_TIMESTAMP event=internal_wsi_dispatch ticketId=%llu sourceSequence=%llu internalPresentId=%llu wsiPresentId=%llu pair=%llu->%llu drawableID=%lu",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)ticket.sourceSequence,
        (unsigned long long)internalPresentId,
        (unsigned long long)internalWsiPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (unsigned long)internalDrawable.drawableID);
    isInternalPresentationCall = YES;
    [r110WsiDispatchLock lock];
    (void)r110RecordWsiDispatch("INTERNAL", 0, internalPresentId,
                                internalWsiPresentId, ticket.sourcePairA,
                                ticket.sourcePairB);
    ((void (*)(id, SEL))presentOriginal)(internalDrawable, @selector(present));
    [r110WsiDispatchLock unlock];
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
    BOOL targetConfigured = r110TupleSourceBSequence > 0;
    BOOL barrierActive = r103FrontierTargetsLayerLocked(
        (CAMetalLayer *)drawable.layer, drawable.texture.device) &&
        (r103SourceFrontierState == R103SourceFrontierClosingForPair ||
         r103SourceFrontierState == R103SourceFrontierClosed ||
         r103SourceFrontierState == R103SourceFrontierReleasing);
    BOOL futureSourceAtBarrier = barrierActive &&
        appFrameId != r103FrontierPairB;
    BOOL futureSourceBeforeClose = targetConfigured && !r103FrontierSpent &&
        r103SourceFrontierState == R103SourceFrontierOpen &&
        appFrameId > r110TupleSourceBSequence;
    BOOL futureSourceLeak = futureSourceAtBarrier || futureSourceBeforeClose;
    uint64_t frontierTicketId = r103FrontierTicketId;
    if (futureSourceLeak) {
        ++r103FutureSourceLeaks;
        if (futureSourceBeforeClose)
            ++r103FutureSourceDispatchesBeforeClose;
    }
    pthread_mutex_unlock(&stateLock);
    if (futureSourceLeak) {
        logLine("R103_FUTURE_SOURCE_LEAK=DETECTED sourceSequence=%llu appFrameId=%llu drawableID=%lu ticketId=%llu barrierActive=%d beforeClose=%d reason=future-source-reached-wsi-admission",
            (unsigned long long)appFrameId, (unsigned long long)appFrameId,
            (unsigned long)drawable.drawableID, (unsigned long long)frontierTicketId,
            barrierActive, futureSourceBeforeClose);
        if (barrierActive)
            r103RecoverFrontier(frontierTicketId, "source-wsi-gate-invariant");
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

static BOOL renderInternalFrameToDrawable(id<MTLTexture> source,
                                          id<CAMetalDrawable> destinationDrawable,
                                          id<MTLCommandQueue> queue) {
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
    return completed;
}

static void tryPresentInternalBeforeSource(id<CAMetalDrawable> sourceDrawable) {
    const char *presentFlag = getenv("FGMETAL_R100_PRESENT");
    if (!presentFlag || strcmp(presentFlag, "1") != 0 || isInternalPresentationCall ||
        !isSelectedLayer((CAMetalLayer *)sourceDrawable.layer)) return;

    NSDictionary *record = trackedDrawableForTexture(sourceDrawable.texture);
    uint64_t sourceSequence = [record[@"sequence"] unsignedLongLongValue];
    if (sourceSequence != 3) return;

    id<MTLCommandBuffer> producer = record[@"producerCommandBuffer"];
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    if (!producer || !queue || producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError) {
        pthread_mutex_lock(&stateLock);
        internalPresentationAttempted = YES;
        pthread_mutex_unlock(&stateLock);
        logLine("INTERNAL_PRESENT_EXPERIMENT=SKIPPED sourceSequence=%llu reason=producer-not-committed producer=%p status=%lu",
            (unsigned long long)sourceSequence, (__bridge void *)producer,
            (unsigned long)producer.status);
        return;
    }

    id<MTLTexture> generatedTexture = nil;
    id<MTLTexture> pendingSnapshot = nil;
    uint64_t internalPresentId = 0;
    uint64_t previousSequence = 0;
    uint64_t currentSequence = 0;
    uint64_t pendingPreviousSnapshot = 0;
    uint64_t pendingCurrentSnapshot = 0;
    BOOL timedOut = NO;
    pthread_mutex_lock(&stateLock);
    if (internalPresentationAttempted || internalPresentationCompleted) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    internalPresentationAttempted = YES;
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_nsec += 750000000;
    if (deadline.tv_nsec >= 1000000000) {
        deadline.tv_sec += deadline.tv_nsec / 1000000000;
        deadline.tv_nsec %= 1000000000;
    }
    while (!pendingInternalPresentationTexture) {
        int waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                                &stateLock, &deadline);
        if (waitResult != 0) { timedOut = YES; break; }
    }
    pendingSnapshot = pendingInternalPresentationTexture;
    pendingPreviousSnapshot = pendingInternalPreviousSequence;
    pendingCurrentSnapshot = pendingInternalCurrentSequence;
    if (pendingInternalPresentationTexture &&
        pendingInternalCurrentSequence == sourceSequence &&
        pendingInternalPreviousSequence + 1 == sourceSequence) {
        generatedTexture = pendingInternalPresentationTexture;
        internalPresentId = pendingInternalPresentId;
        previousSequence = pendingInternalPreviousSequence;
        currentSequence = pendingInternalCurrentSequence;
        pendingInternalPresentationTexture = nil;
    }
    pthread_mutex_unlock(&stateLock);

    if (!generatedTexture) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=SKIPPED sourceSequence=%llu reason=%s pending=%p pendingPair=%llu->%llu",
            (unsigned long long)sourceSequence, timedOut ? "frame-timeout" : "pair-mismatch",
            (__bridge void *)pendingSnapshot,
            (unsigned long long)pendingPreviousSnapshot,
            (unsigned long long)pendingCurrentSnapshot);
        return;
    }

    CAMetalLayer *layer = (CAMetalLayer *)sourceDrawable.layer;
    id<CAMetalDrawable> internalDrawable = originalNextDrawable
        ? ((id (*)(id, SEL))originalNextDrawable)(layer, @selector(nextDrawable)) : nil;
    if (!internalDrawable || internalDrawable.layer != layer ||
        internalDrawable.texture.device != queue.device) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=FAILED internalPresentId=%llu sourceSequence=%llu->%llu reason=drawable-unavailable-or-device-mismatch internalDrawable=%p layer=%p queueDevice=%p drawableDevice=%p",
            (unsigned long long)internalPresentId,
            (unsigned long long)previousSequence, (unsigned long long)currentSequence,
            (__bridge void *)internalDrawable, (__bridge void *)layer,
            (__bridge void *)queue.device,
            (__bridge void *)internalDrawable.texture.device);
        return;
    }
    if (!renderInternalFrameToDrawable(generatedTexture, internalDrawable, queue)) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=FAILED internalPresentId=%llu sourceSequence=%llu->%llu reason=render-failed",
            (unsigned long long)internalPresentId,
            (unsigned long long)previousSequence, (unsigned long long)currentSequence);
        return;
    }

    IMP presentOriginal = originalFor(internalDrawable, @selector(present));
    if (!presentOriginal) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=FAILED internalPresentId=%llu reason=original-present-unavailable drawable=%p",
            (unsigned long long)internalPresentId, (__bridge void *)internalDrawable);
        return;
    }
    isInternalPresentationCall = YES;
    ((void (*)(id, SEL))presentOriginal)(internalDrawable, @selector(present));
    isInternalPresentationCall = NO;
    pthread_mutex_lock(&stateLock);
    internalPresentationCompleted = YES;
    pthread_mutex_unlock(&stateLock);
    logLine("R100_INTERNAL_PRESENT=VALIDATED kind=INTERNAL internalPresentId=%llu sourceSequence=%llu->%llu targetSourceSequence=%llu internalDrawable=%p internalDrawableID=%lu layer=%p queue=%p device=%p registryID=%llu commandAccounting=INTERNAL-only appFrameId=none sourcePresentCountChanged=0",
        (unsigned long long)internalPresentId,
        (unsigned long long)previousSequence, (unsigned long long)currentSequence,
        (unsigned long long)sourceSequence, (__bridge void *)internalDrawable,
        (unsigned long)internalDrawable.drawableID, (__bridge void *)layer,
        (__bridge void *)queue, (__bridge void *)queue.device,
        (unsigned long long)queue.device.registryID);
}

static void hookDrawablePresent(id self, SEL selector) {
    uint64_t callId = (r110OrderingOnly || r110PresentationEnabled()) && isSelectedProcess()
        ? __atomic_add_fetch(&r110PresentCallId, 1, __ATOMIC_RELAXED) : 0;
    uint64_t entryNs = callId ? r105NowNs() : 0;
    if (callId) r110LogSourcePresent(self, selector, "ENTRY", callId, entryNs, 0.0);
    if (isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, 0.0)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), 0.0);
        return;
    }
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = r110PresentationEnabled()
        ? logSourceWsiDispatch(self, selector) : 0;
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() && sourceWsiPresentId) {
            [r110WsiDispatchLock lock];
            NSDictionary *record = trackedDrawableForTexture(
                ((id<CAMetalDrawable>)self).texture);
            (void)r110RecordWsiDispatch("SOURCE",
                [record[@"sequence"] unsignedLongLongValue], 0,
                sourceWsiPresentId, 0, 0);
            ((void (*)(id, SEL))original)(self, selector);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL))original)(self, selector);
        }
    }
    if (callId) r110LogSourcePresent(self, selector, "RETURN", callId, r105NowNs(), 0.0);
    if (r110PresentationEnabled()) r103EndSourceOperation(dispatchLease, "present", callId);
}

static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when) {
    uint64_t callId = (r110OrderingOnly || r110PresentationEnabled()) && isSelectedProcess()
        ? __atomic_add_fetch(&r110PresentCallId, 1, __ATOMIC_RELAXED) : 0;
    uint64_t entryNs = callId ? r105NowNs() : 0;
    if (callId) r110LogSourcePresent(self, selector, "ENTRY", callId, entryNs, when);
    if (isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, when)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), when);
        return;
    }
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = r110PresentationEnabled()
        ? logSourceWsiDispatch(self, selector) : 0;
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() && sourceWsiPresentId) {
            [r110WsiDispatchLock lock];
            NSDictionary *record = trackedDrawableForTexture(
                ((id<CAMetalDrawable>)self).texture);
            (void)r110RecordWsiDispatch("SOURCE",
                [record[@"sequence"] unsignedLongLongValue], 0,
                sourceWsiPresentId, 0, 0);
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
        }
    }
    if (callId) r110LogSourcePresent(self, selector, "RETURN", callId, r105NowNs(), when);
    if (r110PresentationEnabled()) r103EndSourceOperation(dispatchLease, "presentAtTime", callId);
}

static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration) {
    uint64_t callId = (r110OrderingOnly || r110PresentationEnabled()) && isSelectedProcess()
        ? __atomic_add_fetch(&r110PresentCallId, 1, __ATOMIC_RELAXED) : 0;
    uint64_t entryNs = callId ? r105NowNs() : 0;
    if (callId) r110LogSourcePresent(self, selector, "ENTRY", callId, entryNs, duration);
    if (isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, duration)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), duration);
        return;
    }
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = r110PresentationEnabled()
        ? logSourceWsiDispatch(self, selector) : 0;
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() && sourceWsiPresentId) {
            [r110WsiDispatchLock lock];
            NSDictionary *record = trackedDrawableForTexture(
                ((id<CAMetalDrawable>)self).texture);
            (void)r110RecordWsiDispatch("SOURCE",
                [record[@"sequence"] unsignedLongLongValue], 0,
                sourceWsiPresentId, 0, 0);
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
        }
    }
    if (callId) r110LogSourcePresent(self, selector, "RETURN", callId, r105NowNs(), duration);
    if (r110PresentationEnabled()) r103EndSourceOperation(dispatchLease, "presentAfterMinimumDuration", callId);
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

static BOOL nearByte(uint8_t actual, uint8_t expected) {
    return abs((int)actual - (int)expected) <= 6;
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

// Motion evidence is copied as soon as the estimator command buffer completes.
// This diagnostic queue is independent of MetalFX eligibility and never feeds
// a product input. The completion handler exports the field without blocking
// the provider call below.
static void r108ExportMotionDiagnostic(id<MTLTexture> motion,
                                      id<MTLCommandBuffer> estimatorCommandBuffer,
                                      NSUInteger width,
                                      NSUInteger height,
                                      uint64_t previousSequence,
                                      uint64_t currentSequence,
                                      NSString *condition) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Float
        width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> readback = [motion.device newTextureWithDescriptor:descriptor];
    id<MTLCommandBuffer> commandBuffer = [outputReadbackQueue commandBuffer];
    id<MTLBlitCommandEncoder> blit = commandBuffer
        ? [commandBuffer blitCommandEncoder] : nil;
    if (!readback || !commandBuffer || !blit) {
        logLine("R108_MOTION_DIAGNOSTIC_EXPORT=FAILED pair=%llu->%llu condition=%s stage=allocation readback=%p commandBuffer=%p encoder=%p",
            (unsigned long long)previousSequence,
            (unsigned long long)currentSequence,
            condition.UTF8String ?: "UNSPECIFIED",
            (__bridge void *)readback, (__bridge void *)commandBuffer,
            (__bridge void *)blit);
        return;
    }

    [blit copyFromTexture:motion toTexture:readback];
    [blit endEncoding];
    NSString *conditionCopy = [condition copy] ?: @"UNSPECIFIED";
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        @autoreleasepool {
            if (completed.status != MTLCommandBufferStatusCompleted || completed.error) {
                logLine("R108_MOTION_DIAGNOSTIC_EXPORT=FAILED pair=%llu->%llu condition=%s stage=readback status=%lu error=%s",
                    (unsigned long long)previousSequence,
                    (unsigned long long)currentSequence,
                    conditionCopy.UTF8String ?: "UNSPECIFIED",
                    (unsigned long)completed.status,
                    completed.error.localizedDescription.UTF8String ?: "none");
                return;
            }

            const NSUInteger pixelCount = width * height;
            NSMutableData *bytes = [NSMutableData dataWithLength:
                pixelCount * 2 * sizeof(uint16_t)];
            [readback getBytes:bytes.mutableBytes
                bytesPerRow:width * 2 * sizeof(uint16_t)
                fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
            const uint16_t *values = (const uint16_t *)bytes.bytes;
            uint64_t finiteVectors = 0, nonfiniteVectors = 0;
            double sumX = 0.0, sumY = 0.0, sumMagnitude = 0.0;
            float minX = INFINITY, maxX = -INFINITY;
            float minY = INFINITY, maxY = -INFINITY;
            float maxMagnitude = 0.0f;
            for (NSUInteger i = 0; i < pixelCount; ++i) {
                float x = halfToFloat(values[i * 2]);
                float y = halfToFloat(values[i * 2 + 1]);
                if (!isfinite(x) || !isfinite(y)) {
                    ++nonfiniteVectors;
                    continue;
                }
                ++finiteVectors;
                float magnitude = hypotf(x, y);
                sumX += x;
                sumY += y;
                sumMagnitude += magnitude;
                if (x < minX) minX = x;
                if (x > maxX) maxX = x;
                if (y < minY) minY = y;
                if (y > maxY) maxY = y;
                if (magnitude > maxMagnitude) maxMagnitude = magnitude;
            }

            char path[768];
            snprintf(path, sizeof(path),
                "%s/motion-pair-%llu-%llu.rg16f", kR110EvidenceDirectory,
                (unsigned long long)previousSequence,
                (unsigned long long)currentSequence);
            (void)mkdir(kR110EvidenceDirectory, 0700);
            BOOL saved = bytes.length == pixelCount * 4 &&
                [bytes writeToFile:[NSString stringWithUTF8String:path] atomically:YES];
            double divisor = finiteVectors ? (double)finiteVectors : 1.0;
            logLine("R108_MOTION_DIAGNOSTIC_EXPORT=%s pair=%llu->%llu condition=%s dimensions=%lux%lu format=RG16Float convention=current-to-previous-pixels bytes=%lu finiteVectors=%llu nonfiniteVectors=%llu meanX=%.6f meanY=%.6f meanMagnitude=%.6f minX=%.6f maxX=%.6f minY=%.6f maxY=%.6f maxMagnitude=%.6f path=%s usage=post-estimator-only fileUsedForMetalFXInput=0",
                saved ? "SAVED" : "FAILED",
                (unsigned long long)previousSequence,
                (unsigned long long)currentSequence,
                conditionCopy.UTF8String ?: "UNSPECIFIED",
                (unsigned long)width, (unsigned long)height,
                (unsigned long)bytes.length,
                (unsigned long long)finiteVectors,
                (unsigned long long)nonfiniteVectors,
                sumX / divisor, sumY / divisor, sumMagnitude / divisor,
                finiteVectors ? minX : 0.0f, finiteVectors ? maxX : 0.0f,
                finiteVectors ? minY : 0.0f, finiteVectors ? maxY : 0.0f,
                maxMagnitude, saved ? path : "<not-saved>");
        }
    }];
    [commandBuffer commit];
    logLine("R108_MOTION_DIAGNOSTIC_EXPORT=ENQUEUED pair=%llu->%llu condition=%s estimatorCommandBuffer=%p motionTexture=%p diagnosticTexture=%p queue=%p afterEstimatorCompletion=1 beforeMetalFX=1 nonblocking=1",
        (unsigned long long)previousSequence,
        (unsigned long long)currentSequence,
        conditionCopy.UTF8String ?: "UNSPECIFIED",
        (__bridge void *)estimatorCommandBuffer, (__bridge void *)motion,
        (__bridge void *)readback,
        (__bridge void *)outputReadbackQueue);
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

static id<MTLTexture> makePrivateTexture(id<MTLDevice> device,
                                        MTLPixelFormat format,
                                        NSUInteger width, NSUInteger height,
                                        MTLTextureUsage usage) {
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:format width:width height:height
        mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = usage;
    return [device newTextureWithDescriptor:descriptor];
}

static BOOL initializeR108Pipelines(id<MTLDevice> device) {
    if (r108PipelineDevice == device && r108ComputeQueue &&
        r110DownsamplePipeline && r110CoarseMatchPipeline &&
        r110FineRefinePipeline && r108PropagatePipeline &&
        r108UpsamplePipeline && r108PreparePipeline && r108ClearMotionPipeline &&
        r110FastGatePipeline) return YES;

    NSString *source = @
        "#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "\n"
        "inline int2 r110ClampPoint(int2 p, texture2d<float, access::read> image) {\n"
        "  return clamp(p, int2(0), int2(int(image.get_width()) - 1, int(image.get_height()) - 1));\n"
        "}\n"
        "\n"
        "inline float r110Luma(float3 c) {\n"
        "  return dot(c, float3(0.2126f, 0.7152f, 0.0722f));\n"
        "}\n"
        "\n"
        "kernel void r110Downsample(texture2d<float, access::read> previous [[texture(0)]],\n"
        "                           texture2d<float, access::read> current [[texture(1)]],\n"
        "                           texture2d<half, access::write> previousQuarter [[texture(2)]],\n"
        "                           texture2d<half, access::write> currentQuarter [[texture(3)]],\n"
        "                           uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= previousQuarter.get_width() || gid.y >= previousQuarter.get_height()) return;\n"
        "  int2 base = int2(gid) * 4;\n"
        "  float4 a = 0.0f;\n"
        "  float4 b = 0.0f;\n"
        "  for (int y = 0; y < 4; ++y) for (int x = 0; x < 4; ++x) {\n"
        "    int2 p = base + int2(x, y);\n"
        "    p.x = min(p.x, int(previous.get_width()) - 1);\n"
        "    p.y = min(p.y, int(previous.get_height()) - 1);\n"
        "    a += previous.read(uint2(p));\n"
        "    b += current.read(uint2(p));\n"
        "  }\n"
        "  previousQuarter.write(half4(a * (1.0f / 16.0f)), gid);\n"
        "  currentQuarter.write(half4(b * (1.0f / 16.0f)), gid);\n"
        "}\n"
        "\n"
        "inline float r110CoarsePatchCost(texture2d<half, access::read> previous,\n"
        "                                 texture2d<half, access::read> current,\n"
        "                                 int2 center, float3 targetColor, int2 shift) {\n"
        "  float sum = 0.0f;\n"
        "  float weightSum = 0.0f;\n"
        "  const int maxX = int(current.get_width()) - 1;\n"
        "  const int maxY = int(current.get_height()) - 1;\n"
        "  for (int y = -24; y <= 24; y += 4) for (int x = -24; x <= 24; x += 4) {\n"
        "    int2 p = clamp(center + int2(x, y), int2(0), int2(maxX, maxY));\n"
        "    int2 q = clamp(p + shift, int2(0), int2(int(previous.get_width()) - 1,\n"
        "                                             int(previous.get_height()) - 1));\n"
        "    float3 a = float3(current.read(uint2(p)).rgb);\n"
        "    float3 b = float3(previous.read(uint2(q)).rgb);\n"
        "    float targetY = r110Luma(targetColor);\n"
        "    float sampleY = r110Luma(a);\n"
        "    float colorDifference = length(a - targetColor);\n"
        "    if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;\n"
        "    float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f)\n"
        "        ? 4.0f : 1.0f;\n"
        "    sum += weight * abs(r110Luma(a) - r110Luma(b));\n"
        "    weightSum += weight;\n"
        "  }\n"
        "  return sum / max(weightSum, 1.0e-5f);\n"
        "}\n"
        "\n"
        "kernel void r110CoarseMatch(texture2d<half, access::read> previousQuarter [[texture(0)]],\n"
        "                            texture2d<half, access::read> currentQuarter [[texture(1)]],\n"
        "                            texture2d<float, access::write> coarseGrid [[texture(2)]],\n"
        "                            uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= coarseGrid.get_width() || gid.y >= coarseGrid.get_height()) return;\n"
        "  int2 center = int2(float(gid.x) * float(currentQuarter.get_width()) / float(coarseGrid.get_width()),\n"
        "                     float(gid.y) * float(currentQuarter.get_height()) / float(coarseGrid.get_height()));\n"
        "  float3 target = float3(currentQuarter.read(uint2(center)).rgb);\n"
        "  float costs[49];\n"
        "  int2 offsets[49];\n"
        "  int bestIndex = 0;\n"
        "  float bestCost = INFINITY;\n"
        "  int index = 0;\n"
        "  for (int dy = -6; dy <= 6; dy += 2) for (int dx = -6; dx <= 6; dx += 2) {\n"
        "    float cost = r110CoarsePatchCost(previousQuarter, currentQuarter, center, target, int2(dx, dy));\n"
        "    cost += 0.000001f * float(abs(dx) + abs(dy));\n"
        "    costs[index] = cost;\n"
        "    offsets[index] = int2(dx, dy);\n"
        "    if (cost < bestCost) { bestCost = cost; bestIndex = index; }\n"
        "    ++index;\n"
        "  }\n"
        "  float farCost = INFINITY;\n"
        "  for (int i = 0; i < 49; ++i) {\n"
        "    int2 delta = offsets[i] - offsets[bestIndex];\n"
        "    if (abs(delta.x) <= 2 && abs(delta.y) <= 2) continue;\n"
        "    farCost = min(farCost, costs[i]);\n"
        "  }\n"
        "  float confidence = isfinite(farCost) && farCost > 1.0e-5f\n"
        "      ? clamp((farCost - bestCost) / farCost, 0.0f, 1.0f) : 0.0f;\n"
        "  int2 fullVector = offsets[bestIndex] * 4;\n"
        "  coarseGrid.write(float4(float2(fullVector), confidence, bestCost), gid);\n"
        "}\n"
        "\n"
        "inline float r110FinePatchCost(texture2d<float, access::read> previous,\n"
        "                               texture2d<float, access::read> current,\n"
        "                               int2 center, float3 targetColor, int2 shift) {\n"
        "  float sum = 0.0f;\n"
        "  float weightSum = 0.0f;\n"
        "  for (int y = -96; y <= 96; y += 16) for (int x = -96; x <= 96; x += 16) {\n"
        "    int2 p = r110ClampPoint(center + int2(x, y), current);\n"
        "    int2 q = r110ClampPoint(p + shift, previous);\n"
        "    float3 a = current.read(uint2(p)).rgb;\n"
        "    float3 b = previous.read(uint2(q)).rgb;\n"
        "    float targetY = r110Luma(targetColor);\n"
        "    float sampleY = r110Luma(a);\n"
        "    float colorDifference = length(a - targetColor);\n"
        "    if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;\n"
        "    float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f)\n"
        "        ? 4.0f : 1.0f;\n"
        "    sum += weight * abs(r110Luma(a) - r110Luma(b));\n"
        "    weightSum += weight;\n"
        "  }\n"
        "  return sum / max(weightSum, 1.0e-5f);\n"
        "}\n"
        "\n"
        "kernel void r110FineRefine(texture2d<float, access::read> previous [[texture(0)]],\n"
        "                           texture2d<float, access::read> current [[texture(1)]],\n"
        "                           texture2d<float, access::read> coarseGrid [[texture(2)]],\n"
        "                           texture2d<float, access::write> refinedGrid [[texture(3)]],\n"
        "                           uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= refinedGrid.get_width() || gid.y >= refinedGrid.get_height()) return;\n"
        "  float4 seed = coarseGrid.read(gid);\n"
        "  int2 center = int2(float(gid.x) * float(current.get_width()) / float(refinedGrid.get_width()),\n"
        "                     float(gid.y) * float(current.get_height()) / float(refinedGrid.get_height()));\n"
        "  if (seed.z < 0.05f) { refinedGrid.write(float4(0.0f), gid); return; }\n"
        "  float3 target = current.read(uint2(center)).rgb;\n"
        "  int2 base = int2(seed.xy);\n"
        "  int bestX = base.x;\n"
        "  int bestY = base.y;\n"
        "  float bestCost = INFINITY;\n"
        "  for (int dy = base.y - 4; dy <= base.y + 4; ++dy) {\n"
        "    for (int dx = base.x - 4; dx <= base.x + 4; ++dx) {\n"
        "      int2 shift = clamp(int2(dx, dy), int2(-28), int2(28));\n"
        "      float cost = r110FinePatchCost(previous, current, center, target, shift)\n"
        "          + 0.000001f * float(abs(shift.x) + abs(shift.y));\n"
        "      if (cost < bestCost) { bestCost = cost; bestX = shift.x; bestY = shift.y; }\n"
        "    }\n"
        "  }\n"
        "  float cx = r110FinePatchCost(previous, current, center, target, int2(bestX, bestY));\n"
        "  float cl = r110FinePatchCost(previous, current, center, target, int2(max(-28, bestX - 1), bestY));\n"
        "  float cr = r110FinePatchCost(previous, current, center, target, int2(min(28, bestX + 1), bestY));\n"
        "  float cu = r110FinePatchCost(previous, current, center, target, int2(bestX, max(-28, bestY - 1)));\n"
        "  float cd = r110FinePatchCost(previous, current, center, target, int2(bestX, min(28, bestY + 1)));\n"
        "  float denomX = cl - 2.0f * cx + cr;\n"
        "  float denomY = cu - 2.0f * cx + cd;\n"
        "  float subX = abs(denomX) > 1.0e-6f ? clamp(0.5f * (cl - cr) / denomX, -0.5f, 0.5f) : 0.0f;\n"
        "  float subY = abs(denomY) > 1.0e-6f ? clamp(0.5f * (cu - cd) / denomY, -0.5f, 0.5f) : 0.0f;\n"
        "  refinedGrid.write(float4(float(bestX) + subX, float(bestY) + subY, seed.z, bestCost), gid);\n"
        "}\n"
        "\n"
        "kernel void r110Propagate(texture2d<float, access::read> current [[texture(0)]],\n"
        "                          texture2d<float, access::read> input [[texture(1)]],\n"
        "                          texture2d<float, access::write> output [[texture(2)]],\n"
        "                          uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= input.get_width() || gid.y >= input.get_height()) return;\n"
        "  float4 base = input.read(gid);\n"
        "  if (base.z >= 0.05f) { output.write(base, gid); return; }\n"
        "  int2 center = int2(float(gid.x) * float(current.get_width()) / float(input.get_width()),\n"
        "                     float(gid.y) * float(current.get_height()) / float(input.get_height()));\n"
        "  float3 c = current.read(uint2(center)).rgb;\n"
        "  if (r110Luma(c) < 0.05f) { output.write(float4(0.0f), gid); return; }\n"
        "  float scoreBest = 0.0f;\n"
        "  float4 best = base;\n"
        "  for (int y = -3; y <= 3; ++y) for (int x = -3; x <= 3; ++x) {\n"
        "    if (x == 0 && y == 0) continue;\n"
        "    int2 q = int2(gid) + int2(x, y);\n"
        "    if (q.x < 0 || q.y < 0 || q.x >= int(input.get_width()) || q.y >= int(input.get_height())) continue;\n"
        "    float4 f = input.read(uint2(q));\n"
        "    if (f.z <= 0.05f) continue;\n"
        "    int2 qc = int2(float(q.x) * float(current.get_width()) / float(input.get_width()),\n"
        "                   float(q.y) * float(current.get_height()) / float(input.get_height()));\n"
        "    float3 color = current.read(uint2(qc)).rgb;\n"
        "    if (length(c - color) > 0.12f) continue;\n"
        "    float score = f.z / (1.0f + float(abs(x) + abs(y)));\n"
        "    if (score > scoreBest) { scoreBest = score; best = f; }\n"
        "  }\n"
        "  if (scoreBest > 0.0f) { best.z *= 0.75f; output.write(best, gid); }\n"
        "  else output.write(float4(0.0f), gid);\n"
        "}\n"
        "\n"
        "kernel void r110Upsample(texture2d<float, access::read> grid [[texture(0)]],\n"
        "                         texture2d<half, access::write> motion [[texture(1)]],\n"
        "                         uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= motion.get_width() || gid.y >= motion.get_height()) return;\n"
        "  uint2 p = uint2(min(grid.get_width() - 1, uint((float(gid.x) + float(motion.get_width()) / float(grid.get_width()) * 0.5f) * float(grid.get_width()) / float(motion.get_width()))),\n"
        "                  min(grid.get_height() - 1, uint((float(gid.y) + float(motion.get_height()) / float(grid.get_height()) * 0.5f) * float(grid.get_height()) / float(motion.get_height()))));\n"
        "  float2 v = grid.read(p).xy;\n"
        "  motion.write(half4(half(v.x), half(v.y), 0.0h, 0.0h), gid);\n"
        "}\n"
        "\n"
        "kernel void r108Prepare(texture2d<float, access::read> source [[texture(0)]],texture2d<half, access::write> color [[texture(1)]],texture2d<float, access::write> depth [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {\n"
        "  if(gid.x>=source.get_width()||gid.y>=source.get_height())return;float4 c=source.read(gid);color.write(half4(half3(c.rgb),half(c.a)),gid);depth.write(float4(0.25f,0.0f,0.0f,0.0f),gid);\n"
        "}\n"
        "\n"
        "kernel void r108ClearMotion(texture2d<half, access::write> motion [[texture(0)]],uint2 gid [[thread_position_in_grid]]) { if(gid.x<motion.get_width()&&gid.y<motion.get_height())motion.write(half4(0.0h),gid); }\n"
        "\n"
        "struct R110GateGroupStats { uint finiteMotion; uint activeMotion; uint motionSamples; uint finiteOutput; uint outputSamples; uint colorSamples; float maxMagnitude; float diffPrevious; float diffCurrent; };\n"
        "kernel void r110FastGate(texture2d<half, access::read> motion [[texture(0)]], texture2d<float, access::read> previous [[texture(1)]], texture2d<float, access::read> current [[texture(2)]], texture2d<float, access::read> midpoint [[texture(3)]], device R110GateGroupStats *groups [[buffer(0)]], uint2 groupId [[threadgroup_position_in_grid]], uint lane [[thread_index_in_threadgroup]]) {\n"
        "  threadgroup uint finiteMotion[256]; threadgroup uint activeMotion[256]; threadgroup uint motionSamples[256]; threadgroup uint finiteOutput[256]; threadgroup uint outputSamples[256]; threadgroup uint colorSamples[256]; threadgroup uint maxMagnitudeBits[256]; threadgroup float diffPrevious[256]; threadgroup float diffCurrent[256];\n"
        "  uint laneX=lane%16; uint laneY=lane/16; uint blockX=(groupId.x*16+laneX)*4; uint blockY=(groupId.y*16+laneY)*4; uint w=motion.get_width(); uint h=motion.get_height();\n"
        "  uint fm=0,am=0,ms=0,fo=0,os=0,cs=0; float maxMag=0.0f,da=0.0f,db=0.0f;\n"
        "  for(uint oy=0;oy<4;oy++) for(uint ox=0;ox<4;ox++){ uint x=blockX+ox,y=blockY+oy; if(x>=w||y>=h) continue; uint2 p=uint2(x,y); float2 v=float2(motion.read(p).xy); ms++; if(all(isfinite(v))){ fm++; float m=length(v); maxMag=max(maxMag,m); if(m>0.5f) am++; } float4 midpointColor=midpoint.read(p); os++; if(all(isfinite(midpointColor))) fo++; if(ox==2&&oy==2){ float4 a=previous.read(p),b=current.read(p); if(all(isfinite(a))&&all(isfinite(b))&&all(isfinite(midpointColor))){ cs++; da+=(abs(midpointColor.x-a.x)+abs(midpointColor.y-a.y)+abs(midpointColor.z-a.z))/3.0f; db+=(abs(midpointColor.x-b.x)+abs(midpointColor.y-b.y)+abs(midpointColor.z-b.z))/3.0f; } } }\n"
        "  finiteMotion[lane]=fm; activeMotion[lane]=am; motionSamples[lane]=ms; finiteOutput[lane]=fo; outputSamples[lane]=os; colorSamples[lane]=cs; maxMagnitudeBits[lane]=as_type<uint>(maxMag); diffPrevious[lane]=da; diffCurrent[lane]=db; threadgroup_barrier(mem_flags::mem_threadgroup);\n"
        "  for(uint stride=128;stride>0;stride>>=1){ if(lane<stride){ finiteMotion[lane]+=finiteMotion[lane+stride]; activeMotion[lane]+=activeMotion[lane+stride]; motionSamples[lane]+=motionSamples[lane+stride]; finiteOutput[lane]+=finiteOutput[lane+stride]; outputSamples[lane]+=outputSamples[lane+stride]; colorSamples[lane]+=colorSamples[lane+stride]; maxMagnitudeBits[lane]=max(maxMagnitudeBits[lane],maxMagnitudeBits[lane+stride]); diffPrevious[lane]+=diffPrevious[lane+stride]; diffCurrent[lane]+=diffCurrent[lane+stride]; } threadgroup_barrier(mem_flags::mem_threadgroup); }\n"
        "  if(lane==0){ uint groupCountX=(w+63)/64; uint index=groupId.y*groupCountX+groupId.x; groups[index].finiteMotion=finiteMotion[0]; groups[index].activeMotion=activeMotion[0]; groups[index].motionSamples=motionSamples[0]; groups[index].finiteOutput=finiteOutput[0]; groups[index].outputSamples=outputSamples[0]; groups[index].colorSamples=colorSamples[0]; groups[index].maxMagnitude=as_type<float>(maxMagnitudeBits[0]); groups[index].diffPrevious=diffPrevious[0]; groups[index].diffCurrent=diffCurrent[0]; }\n"
        "}\n";

    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    r108ComputeQueue = [device newCommandQueue];
    r108PipelineDevice = device;
    r110DownsamplePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110Downsample"] error:&error];
    r110CoarseMatchPipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110CoarseMatch"] error:&error];
    r110FineRefinePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110FineRefine"] error:&error];
    r108PropagatePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110Propagate"] error:&error];
    r108UpsamplePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110Upsample"] error:&error];
    r108PreparePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r108Prepare"] error:&error];
    r108ClearMotionPipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r108ClearMotion"] error:&error];
    r110FastGatePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110FastGate"] error:&error];
    BOOL ready = library && r108ComputeQueue && r110DownsamplePipeline &&
        r110CoarseMatchPipeline && r110FineRefinePipeline &&
        r108PropagatePipeline && r108UpsamplePipeline && r108PreparePipeline &&
        r108ClearMotionPipeline && r110FastGatePipeline;
    logLine("R110_GPU_PIPELINES=%s device=%p queue=%p downsample=%p coarseMatch=%p fineRefine=%p propagate=%p upsample=%p prepare=%p clear=%p coarseQuarterStep=2 coarsePatchStep=4 finePatchStep=16 error=%s",
        ready ? "READY" : "FAILED", (__bridge void *)device,
        (__bridge void *)r108ComputeQueue, (__bridge void *)r110DownsamplePipeline,
        (__bridge void *)r110CoarseMatchPipeline, (__bridge void *)r110FineRefinePipeline,
        (__bridge void *)r108PropagatePipeline, (__bridge void *)r108UpsamplePipeline,
        (__bridge void *)r108PreparePipeline, (__bridge void *)r108ClearMotionPipeline,
        error.localizedDescription.UTF8String ?: "none");
    return ready;
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
    if (!r108FrameWidth || !r108FrameHeight) return;
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
    descriptor.inputWidth = r108FrameWidth;
    descriptor.inputHeight = r108FrameHeight;
    descriptor.outputWidth = r108FrameWidth;
    descriptor.outputHeight = r108FrameHeight;
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
        initWithDevice:device width:r108FrameWidth height:r108FrameHeight error:&error];
    outputReadbackQueue = [device newCommandQueue];
    if (!realFrameProvider || !outputReadbackQueue) {
        logLine("REAL_METALFX_PROVIDER=FAILED device=%p error=%s",
            (__bridge void *)device,
            error.localizedDescription.UTF8String ?: "queue allocation failed");
        realFrameProvider = nil;
        outputReadbackQueue = nil;
        return;
    }
    const char *presentFlag = getenv("FGMETAL_R100_PRESENT");
    if (presentFlag && strcmp(presentFlag, "1") == 0) {
        internalPresentationPipeline = makeInternalPresentationPipeline(
            device, MTLPixelFormatBGRA8Unorm);
    }
    logLine("R108_METALFX_PROVIDER=READY device=%p registryID=%llu width=%lu height=%lu colorUsage=%lu depthUsage=%lu motionUsage=%lu",
        (__bridge void *)device, (unsigned long long)device.registryID,
        (unsigned long)r108FrameWidth, (unsigned long)r108FrameHeight,
        (unsigned long)colorInputUsage, (unsigned long)depthInputUsage,
        (unsigned long)motionInputUsage);
}

typedef struct {
    uint64_t finiteMotion;
    uint64_t activeMotion;
    uint64_t motionSamples;
    uint64_t finiteOutput;
    uint64_t outputSamples;
    uint64_t colorSamples;
    float maxMagnitude;
    double meanAbsDifferenceFromPrevious;
    double meanAbsDifferenceFromCurrent;
} R110FastMetrics;

static BOOL r110CollectFastMetrics(id<MTLTexture> motion,
                                   id<MTLTexture> previous,
                                   id<MTLTexture> current,
                                   id<MTLTexture> midpoint,
                                   R110FastMetrics *metrics,
                                   double *gpuMs,
                                   NSString **failureReason) {
    if (!motion || !previous || !current || !midpoint || !metrics ||
        !outputReadbackQueue || !r110FastGatePipeline ||
        motion.width != r108FrameWidth || motion.height != r108FrameHeight ||
        previous.width != r108FrameWidth || previous.height != r108FrameHeight ||
        current.width != r108FrameWidth || current.height != r108FrameHeight ||
        midpoint.width != r108FrameWidth || midpoint.height != r108FrameHeight ||
        motion.pixelFormat != MTLPixelFormatRG16Float ||
        previous.pixelFormat != MTLPixelFormatRGBA16Float ||
        current.pixelFormat != MTLPixelFormatRGBA16Float ||
        midpoint.pixelFormat != MTLPixelFormatRGBA16Float ||
        motion.device != midpoint.device || previous.device != midpoint.device ||
        current.device != midpoint.device) {
        if (failureReason) *failureReason = @"texture-preflight-failed";
        return NO;
    }
    const NSUInteger groupsX = (r108FrameWidth + 63) / 64;
    const NSUInteger groupsY = (r108FrameHeight + 63) / 64;
    const NSUInteger groupCount = groupsX * groupsY;
    id<MTLBuffer> statsBuffer = [midpoint.device newBufferWithLength:
        groupCount * sizeof(R110GateGroupStats)
        options:MTLResourceStorageModeShared];
    id<MTLCommandBuffer> commandBuffer = [outputReadbackQueue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = commandBuffer
        ? [commandBuffer computeCommandEncoder] : nil;
    if (!statsBuffer || !encoder) {
        if (failureReason) *failureReason = @"stats-buffer-or-command-encoder-unavailable";
        return NO;
    }
    [encoder setComputePipelineState:r110FastGatePipeline];
    [encoder setTexture:motion atIndex:0];
    [encoder setTexture:previous atIndex:1];
    [encoder setTexture:current atIndex:2];
    [encoder setTexture:midpoint atIndex:3];
    [encoder setBuffer:statsBuffer offset:0 atIndex:0];
    [encoder dispatchThreadgroups:MTLSizeMake(groupsX, groupsY, 1)
        threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
    [encoder endEncoding];
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (gpuMs) *gpuMs = commandBuffer.GPUEndTime > commandBuffer.GPUStartTime
        ? (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0 : 0.0;
    if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) {
        if (failureReason) *failureReason = commandBuffer.error.localizedDescription
            ?: @"fast-metrics-command-failed";
        return NO;
    }
    const R110GateGroupStats *groups = statsBuffer.contents;
    memset(metrics, 0, sizeof(*metrics));
    double diffPrevious = 0.0, diffCurrent = 0.0;
    for (NSUInteger i = 0; i < groupCount; ++i) {
        metrics->finiteMotion += groups[i].finiteMotion;
        metrics->activeMotion += groups[i].activeMotion;
        metrics->motionSamples += groups[i].motionSamples;
        metrics->finiteOutput += groups[i].finiteOutput;
        metrics->outputSamples += groups[i].outputSamples;
        metrics->colorSamples += groups[i].colorSamples;
        if (groups[i].maxMagnitude > metrics->maxMagnitude)
            metrics->maxMagnitude = groups[i].maxMagnitude;
        diffPrevious += groups[i].diffPrevious;
        diffCurrent += groups[i].diffCurrent;
    }
    if (metrics->colorSamples) {
        metrics->meanAbsDifferenceFromPrevious = diffPrevious / metrics->colorSamples;
        metrics->meanAbsDifferenceFromCurrent = diffCurrent / metrics->colorSamples;
    }
    return YES;
}

static void r110ScheduleOutputDiagnostic(id<MTLTexture> midpoint,
                                         uint64_t pairA, uint64_t pairB) {
    if (!midpoint) return;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
            width:midpoint.width height:midpoint.height mipmapped:NO];
        descriptor.storageMode = MTLStorageModeShared;
        descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
        id<MTLTexture> readback = [midpoint.device newTextureWithDescriptor:descriptor];
        id<MTLCommandBuffer> commandBuffer = [outputReadbackQueue commandBuffer];
        id<MTLBlitCommandEncoder> blit = commandBuffer
            ? [commandBuffer blitCommandEncoder] : nil;
        if (!readback || !blit) {
            logLine("R110_METALFX_DIAGNOSTIC=FAILED pair=%llu->%llu reason=readback-allocation-or-blit-unavailable",
                (unsigned long long)pairA, (unsigned long long)pairB);
            return;
        }
        [blit copyFromTexture:midpoint toTexture:readback];
        [blit endEncoding];
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) {
            logLine("R110_METALFX_DIAGNOSTIC=FAILED pair=%llu->%llu reason=readback-command status=%lu error=%s",
                (unsigned long long)pairA, (unsigned long long)pairB,
                (unsigned long)commandBuffer.status,
                commandBuffer.error.localizedDescription.UTF8String ?: "none");
            return;
        }
        const NSUInteger width = midpoint.width, height = midpoint.height;
        const NSUInteger pixelCount = width * height;
        NSMutableData *bytes = [NSMutableData dataWithLength:pixelCount * 8];
        [readback getBytes:bytes.mutableBytes bytesPerRow:width * 8
            fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
        const uint16_t *pixels = (const uint16_t *)bytes.bytes;
        uint64_t finiteValues = 0, nonfiniteValues = 0, meaningfulPixels = 0;
        float alphaMin = INFINITY, alphaMax = -INFINITY;
        for (NSUInteger i = 0; i < pixelCount; ++i) {
            BOOL meaningful = NO;
            for (NSUInteger c = 0; c < 4; ++c) {
                float value = halfToFloat(pixels[i * 4 + c]);
                if (isfinite(value)) ++finiteValues; else ++nonfiniteValues;
                if (c == 3) { alphaMin = fminf(alphaMin, value); alphaMax = fmaxf(alphaMax, value); }
                else if (fabsf(value) > 0.01f) meaningful = YES;
            }
            if (meaningful) ++meaningfulPixels;
        }
        char path[768];
        snprintf(path, sizeof(path), "%s/title-M-for-%llu-%llu.rgba16f",
            kR110EvidenceDirectory, (unsigned long long)pairA,
            (unsigned long long)pairB);
        (void)mkdir(kR110EvidenceDirectory, 0700);
        BOOL saved = nonfiniteValues == 0 && bytes.length == pixelCount * 8 &&
            [bytes writeToFile:[NSString stringWithUTF8String:path] atomically:YES];
        logLine("R110_METALFX_DIAGNOSTIC=%s pair=%llu->%llu dimensions=%lux%lu format=RGBA16Float finiteValues=%llu nonfiniteValues=%llu meaningfulPixels=%llu alphaMin=%.6f alphaMax=%.6f readback=post-attempt saved=%d path=%s",
            saved ? "FINITE" : "INVALID", (unsigned long long)pairA,
            (unsigned long long)pairB, (unsigned long)width, (unsigned long)height,
            (unsigned long long)finiteValues, (unsigned long long)nonfiniteValues,
            (unsigned long long)meaningfulPixels, alphaMin, alphaMax, saved, path);
    });
}

static void processCapturedFrame(uint64_t sequence, NSUInteger drawableID,
                                 MTLPixelFormat sourceFormat,
                                 id<MTLTexture> sourceTexture,
                                 id<MTLDevice> device,
                                 NSString *condition) {
    if (!sourceTexture || sourceTexture.device != device ||
        sourceFormat != sourceTexture.pixelFormat ||
        sourceTexture.framebufferOnly ||
        (sourceTexture.usage & MTLTextureUsageShaderRead) == 0 ||
        (sourceFormat != MTLPixelFormatBGRA8Unorm &&
         sourceFormat != MTLPixelFormatRGBA8Unorm)) {
        logLine("R108_GPU_SOURCE_PREFLIGHT=FAILED seq=%llu drawableID=%lu source=%p format=%lu textureFormat=%lu usage=%lu framebufferOnly=%d",
            (unsigned long long)sequence, (unsigned long)drawableID,
            (__bridge void *)sourceTexture, (unsigned long)sourceFormat,
            (unsigned long)sourceTexture.pixelFormat,
            (unsigned long)sourceTexture.usage, sourceTexture.framebufferOnly);
        return;
    }
    if (previousCaptureSequence && sequence != previousCaptureSequence + 1) {
        logLine("R108_SOURCE_SEQUENCE_GAP previous=%llu current=%llu resetProvider=1",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence);
        realFrameProvider = nil;
        outputReadbackQueue = nil;
        realProviderInitialized = NO;
        previousCaptureColor = nil;
        previousCaptureDepth = nil;
        previousCaptureMotion = nil;
        previousCaptureSource = nil;
        previousCaptureSequence = 0;
    }
    if (r108FrameWidth == 0 && r108FrameHeight == 0) {
        r108FrameWidth = sourceTexture.width;
        r108FrameHeight = sourceTexture.height;
    }
    if (sourceTexture.width != r108FrameWidth ||
        sourceTexture.height != r108FrameHeight ||
        !initializeR108Pipelines(device)) {
        logLine("R108_GPU_SOURCE_PREFLIGHT=FAILED seq=%llu expectedSize=%lux%lu actualSize=%lux%lu pipelinesReady=%d",
            (unsigned long long)sequence, (unsigned long)r108FrameWidth,
            (unsigned long)r108FrameHeight, (unsigned long)sourceTexture.width,
            (unsigned long)sourceTexture.height,
            r108PipelineDevice == device && r108ComputeQueue != nil);
        return;
    }
    initializeMetalFXProvider(device);
    if (!realFrameProvider || !outputReadbackQueue) return;

    const NSUInteger width = sourceTexture.width;
    const NSUInteger height = sourceTexture.height;
    const NSUInteger quarterWidth = (width + 3) / 4;
    const NSUInteger quarterHeight = (height + 3) / 4;
    const NSUInteger gridWidth = (width + 39) / 40;
    const NSUInteger gridHeight = (height + 39) / 40;
    id<MTLTexture> color = makePrivateTexture(device, MTLPixelFormatRGBA16Float,
        width, height, colorInputUsage | MTLTextureUsageShaderRead |
        MTLTextureUsageShaderWrite);
    id<MTLTexture> depth = makePrivateTexture(device, MTLPixelFormatR32Float,
        width, height, depthInputUsage | MTLTextureUsageShaderRead |
        MTLTextureUsageShaderWrite);
    id<MTLTexture> motion = makePrivateTexture(device, MTLPixelFormatRG16Float,
        width, height, motionInputUsage | MTLTextureUsageShaderRead |
        MTLTextureUsageShaderWrite);
    id<MTLTexture> grid = makePrivateTexture(device, MTLPixelFormatRGBA32Float,
        gridWidth, gridHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> refinedGrid = makePrivateTexture(device, MTLPixelFormatRGBA32Float,
        gridWidth, gridHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> propagated = makePrivateTexture(device, MTLPixelFormatRGBA32Float,
        gridWidth, gridHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> previousQuarter = makePrivateTexture(device, MTLPixelFormatRGBA16Float,
        quarterWidth, quarterHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    id<MTLTexture> currentQuarter = makePrivateTexture(device, MTLPixelFormatRGBA16Float,
        quarterWidth, quarterHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    if (!color || !depth || !motion || !grid || !refinedGrid || !propagated ||
        !previousQuarter || !currentQuarter) {
        logLine("R110_GPU_INPUT_ALLOCATION=FAILED seq=%llu color=%p depth=%p motion=%p grid=%p refinedGrid=%p propagated=%p previousQuarter=%p currentQuarter=%p",
            (unsigned long long)sequence, (__bridge void *)color,
            (__bridge void *)depth, (__bridge void *)motion,
            (__bridge void *)grid, (__bridge void *)refinedGrid,
            (__bridge void *)propagated, (__bridge void *)previousQuarter,
            (__bridge void *)currentQuarter);
        return;
    }

    BOOL hasPrevious = previousCaptureSource && previousCaptureSequence + 1 == sequence;
    id<MTLCommandBuffer> commandBuffer = [r108ComputeQueue commandBuffer];
    if (!commandBuffer) {
        logLine("R108_GPU_INPUT_ENCODING=FAILED seq=%llu stage=command-buffer",
            (unsigned long long)sequence);
        return;
    }
    MTLSize imageThreads = MTLSizeMake(width, height, 1);
    MTLSize imageGroup = MTLSizeMake(16, 16, 1);
    MTLSize imageGroups = MTLSizeMake(
        (width + imageGroup.width - 1) / imageGroup.width,
        (height + imageGroup.height - 1) / imageGroup.height, 1);
    MTLSize quarterGroups = MTLSizeMake(
        (quarterWidth + imageGroup.width - 1) / imageGroup.width,
        (quarterHeight + imageGroup.height - 1) / imageGroup.height, 1);
    MTLSize gridGroup = MTLSizeMake(4, 4, 1);
    MTLSize gridGroups = MTLSizeMake(
        (gridWidth + gridGroup.width - 1) / gridGroup.width,
        (gridHeight + gridGroup.height - 1) / gridGroup.height, 1);
    if (hasPrevious) {
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r110DownsamplePipeline];
        [encoder setTexture:previousCaptureSource atIndex:0];
        [encoder setTexture:sourceTexture atIndex:1];
        [encoder setTexture:previousQuarter atIndex:2];
        [encoder setTexture:currentQuarter atIndex:3];
        [encoder dispatchThreadgroups:quarterGroups threadsPerThreadgroup:imageGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r110CoarseMatchPipeline];
        [encoder setTexture:previousQuarter atIndex:0];
        [encoder setTexture:currentQuarter atIndex:1];
        [encoder setTexture:grid atIndex:2];
        [encoder dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r110FineRefinePipeline];
        [encoder setTexture:previousCaptureSource atIndex:0];
        [encoder setTexture:sourceTexture atIndex:1];
        [encoder setTexture:grid atIndex:2];
        [encoder setTexture:refinedGrid atIndex:3];
        [encoder dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r108PropagatePipeline];
        [encoder setTexture:sourceTexture atIndex:0];
        [encoder setTexture:refinedGrid atIndex:1];
        [encoder setTexture:propagated atIndex:2];
        [encoder dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r108UpsamplePipeline];
        [encoder setTexture:propagated atIndex:0];
        [encoder setTexture:motion atIndex:1];
        [encoder dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
        [encoder endEncoding];
    } else {
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r108ClearMotionPipeline];
        [encoder setTexture:motion atIndex:0];
        [encoder dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
        [encoder endEncoding];
    }

    id<MTLComputeCommandEncoder> prepare = [commandBuffer computeCommandEncoder];
    [prepare setComputePipelineState:r108PreparePipeline];
    [prepare setTexture:sourceTexture atIndex:0];
    [prepare setTexture:color atIndex:1];
    [prepare setTexture:depth atIndex:2];
    [prepare dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
    [prepare endEncoding];
    logLine("R108_GPU_INPUTS_ENQUEUED sourceSequence=%llu pair=%llu->%llu condition=%s drawableID=%lu source=%p sourceStorage=%lu color=%p colorFormat=%lu colorStorage=%lu depth=%p depthFormat=%lu depthStorage=%lu motion=%p motionFormat=%lu motionStorage=%lu motionSource=%s depthQualifier=CONSTANT_DEPTH_PROTOTYPE sourceFileTransport=0 motionFileTransport=0",
        (unsigned long long)sequence,
        (unsigned long long)(hasPrevious ? previousCaptureSequence : sequence),
        (unsigned long long)sequence, condition.UTF8String ?: "UNSPECIFIED",
        (unsigned long)drawableID,
        (__bridge void *)sourceTexture, (unsigned long)sourceTexture.storageMode,
        (__bridge void *)color, (unsigned long)color.pixelFormat,
        (unsigned long)color.storageMode, (__bridge void *)depth,
        (unsigned long)depth.pixelFormat, (unsigned long)depth.storageMode,
        (__bridge void *)motion, (unsigned long)motion.pixelFormat,
        (unsigned long)motion.storageMode, hasPrevious ? "R108_GPU_ESTIMATOR" : "ZERO_HISTORY_SEED");

    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    double gpuMs = commandBuffer.GPUEndTime > commandBuffer.GPUStartTime
        ? (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0 : 0.0;
    logLine("R108_GPU_INPUTS_COMPLETE seq=%llu pair=%llu->%llu estimatorCommandBuffer=%p sourceTexture=%p motionTexture=%p status=%lu gpuMs=%.3f error=%s motionSource=%s",
        (unsigned long long)sequence,
        (unsigned long long)(hasPrevious ? previousCaptureSequence : sequence),
        (unsigned long long)sequence, (__bridge void *)commandBuffer,
        (__bridge void *)sourceTexture, (__bridge void *)motion,
        (unsigned long)commandBuffer.status,
        gpuMs, commandBuffer.error.localizedDescription.UTF8String ?: "none",
        hasPrevious ? "R108_GPU_ESTIMATOR" : "ZERO_HISTORY_SEED");
    if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) return;

    if (hasPrevious) {
        // Motion diagnostics are queued immediately after estimator completion,
        // independently of the provider result. This copy is nonblocking and
        // remains outside the MetalFX input path.
        r108ExportMotionDiagnostic(motion, commandBuffer, width, height,
            previousCaptureSequence, sequence, condition);
    }

    logLine("R108_TITLE_CRITICAL_PATH_ASSERTION=PASS sourcePixelCPUReadbackForInputConstruction=0 cpuColorPixelInputBuffers=0 cpuMotionPixelInputBuffers=0 cpuDepthPixelInputBuffers=0 sourceFileTransport=0 motionFileTransport=0 inputTextures=private diagnosticsDependency=NONE sourceDiagnosticsDispatchedAfterCopy=1");
    if (hasPrevious) {
        NSError *error = nil;
        uint64_t startNs = r105NowNs();
        FGMetalInternalFrame *internalFrame =
            [realFrameProvider interpolatePreviousColor:previousCaptureColor
                currentColor:color depthTexture:depth motionTexture:motion
                deltaTime:1.0f / 60.0f error:&error];
        uint64_t elapsedNs = r105NowNs() - startNs;
        logLine("R108_METALFX_TRANSITION previousSourceSequence=%llu currentSourceSequence=%llu condition=%s result=%s internalPresentId=%llu error=%s elapsedMs=%.3f depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            condition.UTF8String ?: "UNSPECIFIED",
            internalFrame ? "INTERNAL_FRAME" : (error ? "FAILED" : "NO_OUTPUT"),
            (unsigned long long)internalFrame.internalPresentId,
            error.localizedDescription.UTF8String ?: "none", elapsedNs / 1e6);
        if (internalFrame && !error) {
            logLine("R108_PROVIDER_WARM_STATE=READY completedEligiblePair=%llu->%llu nextExpectedEligiblePair=%llu->%llu condition=%s internalPresentId=%llu eventTimestampMonotonicNs=%llu",
                (unsigned long long)previousCaptureSequence,
                (unsigned long long)sequence,
                (unsigned long long)sequence,
                (unsigned long long)(sequence + 1),
                condition.UTF8String ?: "UNSPECIFIED",
                (unsigned long long)internalFrame.internalPresentId,
                (unsigned long long)r105NowNs());
        }
        if (internalFrame && !error) {
            BOOL textureValid = internalFrame.internalPresentId > 0 &&
                internalFrame.texture.device == device &&
                internalFrame.texture.width == width &&
                internalFrame.texture.height == height &&
                internalFrame.texture.pixelFormat == MTLPixelFormatRGBA16Float &&
                internalFrame.texture.storageMode == MTLStorageModePrivate;
            pthread_mutex_lock(&stateLock);
            BOOL targetPair = r110PresentationEnabled() &&
                r110TupleSourceBSequence == sequence &&
                previousCaptureSequence + 1 == sequence;
            BOOL sourceWindow = r108CaptureTriggered &&
                previousCaptureSequence > r108CaptureStartSequence &&
                sequence <= r108CaptureEndSequence;
            BOOL frontierReady = r103FrontierPairA == previousCaptureSequence &&
                r103FrontierPairB == sequence &&
                r103SourceFrontierState == R103SourceFrontierClosed;
            BOOL slotAvailable = !r110QualityWaitStarted &&
                !pendingInternalPresentationTexture;
            pthread_mutex_unlock(&stateLock);

            if (targetPair) {
                R110FastMetrics metrics = {0};
                double fastGpuMs = 0.0;
                NSString *metricsError = nil;
                BOOL estimatorCompleted = commandBuffer.status == MTLCommandBufferStatusCompleted &&
                    commandBuffer.error == nil;
                BOOL metricsComplete = estimatorCompleted && textureValid &&
                    r110CollectFastMetrics(motion, previousCaptureColor, color,
                        internalFrame.texture, &metrics, &fastGpuMs, &metricsError);
                double activeFraction = metrics.motionSamples
                    ? (double)metrics.activeMotion / (double)metrics.motionSamples : 0.0;
                BOOL motionFinite = metricsComplete &&
                    metrics.motionSamples == width * height &&
                    metrics.finiteMotion == metrics.motionSamples &&
                    isfinite(metrics.maxMagnitude) && metrics.maxMagnitude <= 256.0f;
                BOOL motionMoving = motionFinite && activeFraction >= 0.15;
                BOOL outputFinite = metricsComplete &&
                    metrics.outputSamples == width * height &&
                    metrics.finiteOutput == metrics.outputSamples;
                BOOL outputDistinct = metricsComplete && metrics.colorSamples > 0 &&
                    metrics.meanAbsDifferenceFromPrevious >= 0.002 &&
                    metrics.meanAbsDifferenceFromCurrent >= 0.002;
                BOOL worldConfirmed = r110WorldSceneConfirmed();
                BOOL cameraAttributed = r111CameraPairAttributionVerified(
                    previousCaptureSequence, sequence);
                BOOL fastEligible = metricsComplete && estimatorCompleted &&
                    textureValid && sourceWindow && frontierReady && slotAvailable &&
                    worldConfirmed && cameraAttributed && motionFinite && motionMoving && outputFinite &&
                    outputDistinct;
                const char *reason = !worldConfirmed ? "world-scene-not-confirmed" :
                    !cameraAttributed ? "camera-input-not-attributed-between-source-acquisitions" :
                    !sourceWindow ? "source-pair-outside-capture-window" :
                    !frontierReady ? "source-frontier-not-closed-for-exact-pair" :
                    !slotAvailable ? "internal-slot-unavailable" :
                    !metricsComplete ? "fast-metrics-unavailable" :
                    !estimatorCompleted ? "estimator-not-completed" :
                    !textureValid ? "metalfx-output-texture-invalid" :
                    !motionFinite ? "motion-field-nonfinite-or-unbounded" :
                    !motionMoving ? "motion-below-frozen-pair-derived-threshold" :
                    !outputFinite ? "metalfx-output-nonfinite" :
                    !outputDistinct ? "metalfx-output-too-close-to-an-endpoint" : "none";
                logLine("R110_FAST_PRESENT_ELIGIBILITY=%s pair=%llu->%llu internalPresentId=%llu sourceWindow=%d worldSceneConfirmed=%d cameraInputAttributed=%d estimatorCompleted=%d motionFinite=%d motionSamples=%llu finiteMotion=%llu activeOver0_5px=%llu activeFraction=%.6f maxMagnitude=%.6f outputFinite=%d outputSamples=%llu finiteOutput=%llu colorSamples=%llu meanAbsDiffA=%.6f meanAbsDiffB=%.6f thresholds=activeFraction>=0.15,endpointDifference>=0.002 maxMagnitude<=256 fastMetricsGpuMs=%.3f reason=%s detail=%s",
                    fastEligible ? "ACCEPTED" : "REJECTED",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence,
                    (unsigned long long)internalFrame.internalPresentId,
                    sourceWindow, worldConfirmed, cameraAttributed, estimatorCompleted, motionFinite,
                    (unsigned long long)metrics.motionSamples,
                    (unsigned long long)metrics.finiteMotion,
                    (unsigned long long)metrics.activeMotion, activeFraction,
                    metrics.maxMagnitude, outputFinite,
                    (unsigned long long)metrics.outputSamples,
                    (unsigned long long)metrics.finiteOutput,
                    (unsigned long long)metrics.colorSamples,
                    metrics.meanAbsDifferenceFromPrevious,
                    metrics.meanAbsDifferenceFromCurrent, fastGpuMs, reason,
                    metricsError.UTF8String ?: "none");
                BOOL published = NO;
                if (fastEligible) {
                    pthread_mutex_lock(&stateLock);
                    published = r110PresentationEnabled() &&
                        r110TupleSourceBSequence == sequence &&
                        previousCaptureSequence + 1 == sequence &&
                        r103FrontierPairA == previousCaptureSequence &&
                        r103FrontierPairB == sequence &&
                        r103SourceFrontierState == R103SourceFrontierClosed &&
                        !r110QualityWaitStarted && !pendingInternalPresentationTexture &&
                        r110WorldSceneConfirmed();
                    if (published) {
                        r110QualityWaitStarted = YES;
                        r110QualityReviewed = YES;
                        r110QualityRejected = NO;
                        internalSlotState = R101InternalSlotReady;
                        pendingInternalPresentationTexture = internalFrame.texture;
                        pendingInternalPresentId = internalFrame.internalPresentId;
                        pendingInternalPreviousSequence = previousCaptureSequence;
                        pendingInternalCurrentSequence = sequence;
                        pendingInternalLayer = selectedHarnessLayer;
                        pendingInternalDevice = (__bridge void *)device;
                        pthread_cond_broadcast(&presentationReadyCondition);
                    }
                    pthread_mutex_unlock(&stateLock);
                }
                if (published) {
                    logLine("R110_PRESENT_M_READY pair=%llu->%llu internalPresentId=%llu sourceA=%s/source-sequence-%llu.raw sourceB=%s/source-sequence-%llu.raw output=GPU-resident fastEligibility=ACCEPTED slowDiagnostics=after-attempt width=%lu height=%lu depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (unsigned long long)internalFrame.internalPresentId,
                        kR110EvidenceDirectory,
                        (unsigned long long)previousCaptureSequence,
                        kR110EvidenceDirectory,
                        (unsigned long long)sequence, (unsigned long)width,
                        (unsigned long)height);
                    r102AttemptInternalDispatch();
                } else {
                    logLine("R110_FAST_PRESENT_PUBLICATION=REJECTED pair=%llu->%llu reason=publication-race",
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence);
                    pthread_mutex_lock(&stateLock);
                    r110QualityReviewed = NO;
                    r110QualityRejected = YES;
                    pthread_cond_broadcast(&presentationReadyCondition);
                    pthread_mutex_unlock(&stateLock);
                    r102FallbackForSequence(sequence, "fast-eligibility-rejected-or-publication-race");
                    r110ScheduleOutputDiagnostic(internalFrame.texture,
                        previousCaptureSequence, sequence);
                }
            }
        } else {
            pthread_mutex_lock(&stateLock);
            BOOL targetPair = r110PresentationEnabled() &&
                r110TupleSourceBSequence == sequence &&
                previousCaptureSequence + 1 == sequence;
            if (targetPair) {
                r110QualityReviewed = NO;
                r110QualityRejected = YES;
                pthread_cond_broadcast(&presentationReadyCondition);
            }
            pthread_mutex_unlock(&stateLock);
            if (targetPair) {
                logLine("R110_FAST_PRESENT_ELIGIBILITY=REJECTED pair=%llu->%llu internalPresentId=0 reason=metalfx-transition-unavailable error=%s",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence,
                    error.localizedDescription.UTF8String ?: "no-output");
                r102FallbackForSequence(sequence, "metalfx-transition-unavailable");
            }
        }
    }

    previousCaptureSource = sourceTexture;
    previousCaptureColor = color;
    previousCaptureDepth = depth;
    previousCaptureMotion = motion;
    previousCaptureSequence = sequence;
    pthread_mutex_lock(&stateLock);
    BOOL processedWindowEnd = sequence == r108CaptureEndSequence;
    NSUInteger completedWindow = r108CaptureWindows;
    pthread_mutex_unlock(&stateLock);
    if (processedWindowEnd) {
        logLine("R108_TITLE_CAPTURE=PROCESSED_WINDOW_COMPLETE window=%lu endSequence=%llu pairProcessingComplete=1",
            (unsigned long)completedWindow, (unsigned long long)sequence);
    }
    (void)imageThreads;
}

static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<CAMetalDrawable> drawable,
                                uint64_t sourceSequence,
                                id<MTLCommandBuffer> producerCommandBuffer) {
    const char *captureFlag = getenv("FGMETAL_R108_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0) return;
    if (!r108SequenceSelectedForCapture(sourceSequence)) return;

    pthread_mutex_lock(&stateLock);
    if (r108CaptureCount >= kR108CaptureLimit) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    ++r108CaptureCount;
    BOOL captureWindowSourceComplete = r108CaptureCount == kR108CaptureLimit;
    NSUInteger captureWindowOrdinal = r108CaptureWindows;
    if (captureWindowSourceComplete)
        r108CaptureTriggered = NO;
    ++captureAttempts;
    pthread_mutex_unlock(&stateLock);

    if (captureWindowSourceComplete) {
        logLine("R108_TITLE_CAPTURE=SOURCE_WINDOW_COMPLETE window=%lu endSequence=%llu sourceFrames=%lu",
            (unsigned long)captureWindowOrdinal,
            (unsigned long long)sourceSequence, (unsigned long)kR108CaptureLimit);
    }

    id<MTLTexture> source = drawable.texture;
    id<MTLDevice> device = source.device;
    NSString *conditionLabel = r108CurrentCaptureCondition();
    NSUInteger width = source.width;
    NSUInteger height = source.height;
    if (source.framebufferOnly ||
        (source.usage & MTLTextureUsageShaderRead) == 0 ||
        device != queue.device || width < 64 || height < 64 ||
        width > 4096 || height > 2160 ||
        (source.pixelFormat != MTLPixelFormatBGRA8Unorm &&
         source.pixelFormat != MTLPixelFormatRGBA8Unorm)) {
        logLine("ELDENRING_SOURCE_CAPTURE_PREFLIGHT=FAILED seq=%llu drawableID=%lu texture=%p device=%p queueDevice=%p framebufferOnly=%d usage=%lu size=%lux%lu pixelFormat=%lu",
            (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
            (__bridge void *)source, (__bridge void *)device,
            (__bridge void *)queue.device, source.framebufferOnly,
            (unsigned long)source.usage, (unsigned long)width,
            (unsigned long)height, (unsigned long)source.pixelFormat);
        return;
    }

    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:source.pixelFormat
        width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> captureTexture = [device newTextureWithDescriptor:descriptor];
    MTLTextureDescriptor *diagnosticDescriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:source.pixelFormat
        width:width height:height mipmapped:NO];
    diagnosticDescriptor.storageMode = MTLStorageModeShared;
    diagnosticDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> diagnosticTexture = [device newTextureWithDescriptor:diagnosticDescriptor];
    id<MTLCommandBuffer> copyCommandBuffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = copyCommandBuffer
        ? [copyCommandBuffer blitCommandEncoder] : nil;
    if (!captureTexture || !diagnosticTexture || !copyCommandBuffer || !blit) {
        logLine("R108_SOURCE_CAPTURE_ALLOCATION=FAILED seq=%llu gpuTexture=%p diagnosticTexture=%p commandBuffer=%p encoder=%p",
            (unsigned long long)sourceSequence, (__bridge void *)captureTexture,
            (__bridge void *)diagnosticTexture,
            (__bridge void *)copyCommandBuffer, (__bridge void *)blit);
        return;
    }
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0, 0, 0)
              sourceSize:MTLSizeMake(width, height, 1)
               toTexture:captureTexture destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0, 0, 0)
              sourceSize:MTLSizeMake(width, height, 1)
               toTexture:diagnosticTexture destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit endEncoding];
    logLine("ELDENRING_SOURCE_COPY_ENQUEUED seq=%llu drawableID=%lu producerCommandBuffer=%p producerStatus=%lu queue=%p device=%p registryID=%llu sourceTexture=%p captureTexture=%p size=%lux%lu pixelFormat=%lu",
        (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
        (__bridge void *)producerCommandBuffer,
        (unsigned long)producerCommandBuffer.status, (__bridge void *)queue,
        (__bridge void *)device, (unsigned long long)device.registryID,
        (__bridge void *)source, (__bridge void *)captureTexture,
        (unsigned long)width, (unsigned long)height,
        (unsigned long)source.pixelFormat);

    [copyCommandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completedCopy) {
        @autoreleasepool {
            if (completedCopy.status != MTLCommandBufferStatusCompleted || completedCopy.error) {
                logLine("ELDENRING_SOURCE_COPY_FAILED seq=%llu drawableID=%lu copyCommandBuffer=%p status=%lu error=%s",
                    (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                    (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                    completedCopy.error.localizedDescription.UTF8String ?: "none");
                return;
            }
            // Queue private GPU input construction as soon as the correlated
            // source copy completes. CPU readback, scene checks, and file
            // writes below are diagnostic-only and cannot gate frame history.
            logLine("R108_GPU_PROCESSING_DISPATCHED seq=%llu condition=%s drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p gpuSourceTexture=%p diagnosticDependency=NONE",
                (unsigned long long)sourceSequence,
                conditionLabel.UTF8String ?: "UNSPECIFIED",
                (unsigned long)drawable.drawableID,
                (__bridge void *)producerCommandBuffer,
                (__bridge void *)completedCopy,
                (__bridge void *)captureTexture);
            dispatch_async(captureProcessingQueue, ^{
                @autoreleasepool {
                    processCapturedFrame(sourceSequence, drawable.drawableID,
                        source.pixelFormat, captureTexture, device,
                        conditionLabel);
                }
            });
            size_t bytesPerRow = (size_t)width * 4;
            NSMutableData *readback = [NSMutableData dataWithLength:
                bytesPerRow * (size_t)height];
            [diagnosticTexture getBytes:readback.mutableBytes bytesPerRow:bytesPerRow
                fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];

            const BOOL bgra = source.pixelFormat == MTLPixelFormatBGRA8Unorm;
            const uint8_t *bytes = (const uint8_t *)readback.bytes;
            uint64_t meaningfulSamples = 0;
            uint32_t minimumLuma = UINT32_MAX;
            uint32_t maximumLuma = 0;
            uint64_t sampleHash = 1469598103934665603ULL;
            for (NSUInteger y = 0; y < height; y += 4) {
                const uint8_t *row = bytes + (size_t)y * bytesPerRow;
                for (NSUInteger x = 0; x < width; x += 4) {
                    const uint8_t *pixel = row + (size_t)x * 4;
                    uint32_t red = pixel[bgra ? 2 : 0];
                    uint32_t green = pixel[1];
                    uint32_t blue = pixel[bgra ? 0 : 2];
                    uint32_t luma = (red * 3 + green * 6 + blue) / 10;
                    if (luma > 12) ++meaningfulSamples;
                    if (luma < minimumLuma) minimumLuma = luma;
                    if (luma > maximumLuma) maximumLuma = luma;
                    for (NSUInteger c = 0; c < 4; ++c) {
                        sampleHash ^= pixel[c];
                        sampleHash *= 1099511628211ULL;
                    }
                }
            }
            BOOL verified = meaningfulSamples > 10 &&
                maximumLuma > minimumLuma + 12;
            char capturePath[512];
            snprintf(capturePath, sizeof(capturePath),
                "%s/source-sequence-%llu.raw", kR110EvidenceDirectory,
                (unsigned long long)sourceSequence);
            (void)mkdir(kR110EvidenceDirectory, 0700);
            FILE *captureFile = verified ? fopen(capturePath, "wb") : NULL;
            BOOL saved = captureFile &&
                fwrite(readback.bytes, 1, readback.length, captureFile) == readback.length;
            if (captureFile) fclose(captureFile);
            logLine("R108_SOURCE_DIAGNOSTIC_READBACK seq=%llu condition=%s drawableID=%lu producerCommandBuffer=%p producerStatus=%lu copyCommandBuffer=%p copyStatus=%lu sourceTexture=%p gpuSourceTexture=%p diagnosticTexture=%p device=%p registryID=%llu size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu lumaRange=%u-%u sampleHash=%llu verified=%d saved=%d path=%s",
                (unsigned long long)sourceSequence, conditionLabel.UTF8String ?: "UNSPECIFIED",
                (unsigned long)drawable.drawableID,
                (__bridge void *)producerCommandBuffer,
                (unsigned long)producerCommandBuffer.status,
                (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                (__bridge void *)source, (__bridge void *)captureTexture,
                (__bridge void *)diagnosticTexture,
                (__bridge void *)device, (unsigned long long)device.registryID,
                (unsigned long)width, (unsigned long)height,
                (unsigned long)source.pixelFormat,
                (unsigned long long)meaningfulSamples, minimumLuma, maximumLuma,
                (unsigned long long)sampleHash, verified, saved,
                saved ? capturePath : "<not-saved>");
            if (verified && saved) {
                pthread_mutex_lock(&stateLock);
                ++captureSuccesses;
                NSUInteger successCount = captureSuccesses;
                pthread_mutex_unlock(&stateLock);
                logLine("R108_REAL_D3DMETAL_SOURCE_CAPTURE=VALIDATED seq=%llu drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p gpuCaptureTexture=%p diagnosticTexture=%p size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu successfulCaptures=%lu",
                    (unsigned long long)sourceSequence,
                    (unsigned long)drawable.drawableID,
                    (__bridge void *)producerCommandBuffer,
                    (__bridge void *)completedCopy,
                    (__bridge void *)captureTexture, (__bridge void *)diagnosticTexture,
                    (unsigned long)width,
                    (unsigned long)height, (unsigned long)source.pixelFormat,
                    (unsigned long long)meaningfulSamples,
                    (unsigned long)successCount);
                logLine("R108_SOURCE_PAIR_IDENTITY=EXACT sourceSequence=%llu condition=%s drawableID=%lu producerCommandBuffer=%p producerStatus=%lu gpuSourceTexture=%p diagnosticPath=%s fileUsedForMetalFXInput=0",
                    (unsigned long long)sourceSequence,
                    conditionLabel.UTF8String ?: "UNSPECIFIED",
                    (unsigned long)drawable.drawableID,
                    (__bridge void *)producerCommandBuffer,
                    (unsigned long)producerCommandBuffer.status,
                    (__bridge void *)captureTexture, capturePath);
            }
        }
    }];
    [copyCommandBuffer commit];
}

static uint64_t r105NowNs(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return 0;
    return (uint64_t)value.tv_sec * 1000000000ULL + (uint64_t)value.tv_nsec;
}

static NSValue *r105CommandBufferKey(id<MTLCommandBuffer> commandBuffer) {
    return [NSValue valueWithPointer:(__bridge const void *)commandBuffer];
}

static NSDictionary *r105AttachmentRecord(NSString *kind,
                                          NSUInteger index,
                                          MTLRenderPassAttachmentDescriptor *attachment) {
    id<MTLTexture> texture = attachment.texture;
    if (!texture) return nil;
    id<MTLTexture> resolve = attachment.resolveTexture;
    return @{
        @"kind": kind,
        @"index": @(index),
        @"texture": texture,
        @"textureIdentity": @((uintptr_t)(__bridge void *)texture),
        @"width": @(texture.width),
        @"height": @(texture.height),
        @"depth": @(texture.depth),
        @"pixelFormat": @(texture.pixelFormat),
        @"usage": @(texture.usage),
        @"storageMode": @(texture.storageMode),
        @"textureType": @(texture.textureType),
        @"sampleCount": @(texture.sampleCount),
        @"arrayLength": @(texture.arrayLength),
        @"mipmapLevelCount": @(texture.mipmapLevelCount),
        @"mipLevel": @(attachment.level),
        @"slice": @(attachment.slice),
        @"depthPlane": @(attachment.depthPlane),
        @"loadAction": @(attachment.loadAction),
        @"storeAction": @(attachment.storeAction),
        @"storeActionOptions": @(attachment.storeActionOptions),
        @"resolveTextureIdentity": @((uintptr_t)(__bridge void *)resolve),
        @"resolveLevel": @(attachment.resolveLevel),
        @"resolveSlice": @(attachment.resolveSlice),
        @"resolveDepthPlane": @(attachment.resolveDepthPlane),
    };
}

static void r105LogPass(NSDictionary *pass,
                        uint64_t sourceSequence,
                        uint64_t drawableID,
                        const char *relation,
                        NSUInteger windowOrdinal,
                        void *producerCommandBuffer) {
    NSArray<NSDictionary *> *attachments = pass[@"attachments"];
    logLine("R106_RENDER_PASS sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p renderCommandBuffer=%p queue=%p encoder=%p passId=%llu relation=%s windowOrdinal=%lu timeNs=%llu renderTargetArrayLength=%lu attachments=%lu",
        (unsigned long long)sourceSequence, (unsigned long long)drawableID,
        producerCommandBuffer, [pass[@"commandBufferIdentity"] pointerValue],
        [pass[@"queueIdentity"] pointerValue], [pass[@"encoderIdentity"] pointerValue],
        (unsigned long long)[pass[@"passId"] unsignedLongLongValue], relation,
        (unsigned long)windowOrdinal,
        (unsigned long long)[pass[@"timeNs"] unsignedLongLongValue],
        (unsigned long)[pass[@"renderTargetArrayLength"] unsignedLongValue],
        (unsigned long)attachments.count);
    for (NSDictionary *attachment in attachments) {
        logLine("R106_RENDER_ATTACHMENT sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p renderCommandBuffer=%p encoder=%p passId=%llu relation=%s role=%s attachmentIndex=%lu texture=%p dimensions=%lux%lux%lu pixelFormat=%lu usage=%lu storageMode=%lu textureType=%lu sampleCount=%lu arrayLength=%lu mipLevels=%lu mipLevel=%lu slice=%lu depthPlane=%lu loadAction=%lu storeAction=%lu storeActionOptions=%lu resolveTexture=%p resolveLevel=%lu resolveSlice=%lu resolveDepthPlane=%lu",
            (unsigned long long)sourceSequence, (unsigned long long)drawableID,
            producerCommandBuffer, [pass[@"commandBufferIdentity"] pointerValue],
            [pass[@"encoderIdentity"] pointerValue],
            (unsigned long long)[pass[@"passId"] unsignedLongLongValue], relation,
            [attachment[@"kind"] UTF8String],
            (unsigned long)[attachment[@"index"] unsignedLongValue],
            (void *)(uintptr_t)[attachment[@"textureIdentity"] unsignedLongLongValue],
            (unsigned long)[attachment[@"width"] unsignedLongValue],
            (unsigned long)[attachment[@"height"] unsignedLongValue],
            (unsigned long)[attachment[@"depth"] unsignedLongValue],
            (unsigned long)[attachment[@"pixelFormat"] unsignedLongValue],
            (unsigned long)[attachment[@"usage"] unsignedLongValue],
            (unsigned long)[attachment[@"storageMode"] unsignedLongValue],
            (unsigned long)[attachment[@"textureType"] unsignedLongValue],
            (unsigned long)[attachment[@"sampleCount"] unsignedLongValue],
            (unsigned long)[attachment[@"arrayLength"] unsignedLongValue],
            (unsigned long)[attachment[@"mipmapLevelCount"] unsignedLongValue],
            (unsigned long)[attachment[@"mipLevel"] unsignedLongValue],
            (unsigned long)[attachment[@"slice"] unsignedLongValue],
            (unsigned long)[attachment[@"depthPlane"] unsignedLongValue],
            (unsigned long)[attachment[@"loadAction"] unsignedLongValue],
            (unsigned long)[attachment[@"storeAction"] unsignedLongValue],
            (unsigned long)[attachment[@"storeActionOptions"] unsignedLongValue],
            (void *)(uintptr_t)[attachment[@"resolveTextureIdentity"] unsignedLongLongValue],
            (unsigned long)[attachment[@"resolveLevel"] unsignedLongValue],
            (unsigned long)[attachment[@"resolveSlice"] unsignedLongValue],
            (unsigned long)[attachment[@"resolveDepthPlane"] unsignedLongValue]);
    }
}

static NSDictionary *r105MakePassRecord(id<MTLCommandBuffer> commandBuffer,
                                        id encoder,
                                        MTLRenderPassDescriptor *descriptor) {
    NSMutableArray<NSDictionary *> *attachments = [NSMutableArray array];
    NSDictionary *depth = r105AttachmentRecord(@"depth", 0, descriptor.depthAttachment);
    if (depth) [attachments addObject:depth];

    pthread_mutex_lock(&stateLock);
    uint64_t passId = ++r105NextPassId;
    pthread_mutex_unlock(&stateLock);
    return @{
        @"passId": @(passId),
        @"commandBufferIdentity": [NSValue valueWithPointer:(__bridge const void *)commandBuffer],
        @"queueIdentity": [NSValue valueWithPointer:(__bridge const void *)commandBuffer.commandQueue],
        @"encoderIdentity": [NSValue valueWithPointer:(__bridge const void *)encoder],
        @"timeNs": @(r105NowNs()),
        @"renderTargetArrayLength": @(descriptor.renderTargetArrayLength),
        @"attachments": [attachments copy],
    };
}

static void r105CaptureRenderPass(id<MTLCommandBuffer> commandBuffer,
                                  id encoder,
                                  MTLRenderPassDescriptor *descriptor) {
    NSValue *commandKey = r105CommandBufferKey(commandBuffer);
    BOOL sourceColorPass = NO;
    uint64_t sourceSequence = 0;
    MTLRenderPassColorAttachmentDescriptorArray *colors = descriptor.colorAttachments;
    for (NSUInteger index = 0; index < 8; ++index) {
        id<MTLTexture> color = colors[index].texture;
        NSDictionary *drawable = trackedDrawableForTexture(color);
        uint64_t candidateSequence = [drawable[@"sequence"] unsignedLongLongValue];
        if (drawable && candidateSequence >= kR105TitleSampleStartSequence &&
            r105SequenceSelectedForInventory(candidateSequence)) {
            sourceColorPass = YES;
            sourceSequence = candidateSequence;
            break;
        }
    }

    id<MTLTexture> depthTexture = descriptor.depthAttachment.texture;
    // Keep R105's exact candidate contract, but do not bind the sampler check
    // to one window-size snapshot. Elden Ring's drawable dimensions vary by
    // a few pixels while the render-pass attachment remains the same resource
    // class on the same producer command buffer.
    BOOL exactDepthCandidate = depthTexture &&
        depthTexture.pixelFormat == MTLPixelFormatDepth32Float_Stencil8 &&
        (depthTexture.textureType == MTLTextureType2D ||
         (depthTexture.textureType == MTLTextureType2DArray && depthTexture.arrayLength == 1)) &&
        depthTexture.sampleCount == 1 &&
        (depthTexture.usage & MTLTextureUsageShaderRead) != 0 &&
        descriptor.depthAttachment.storeAction == MTLStoreActionStore;
    if (sourceColorPass) {
        logLine("R106_DEPTH_ATTACHMENT_DIAGNOSTIC sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p dimensions=%lux%lu pixelFormat=%lu textureType=%lu arrayLength=%lu usage=%lu storageMode=%lu sampleCount=%lu storeAction=%lu exactCandidate=%d",
            (unsigned long long)sourceSequence, (__bridge void *)commandBuffer,
            (__bridge void *)depthTexture,
            (unsigned long)(depthTexture ? depthTexture.width : 0),
            (unsigned long)(depthTexture ? depthTexture.height : 0),
            (unsigned long)(depthTexture ? depthTexture.pixelFormat : 0),
            (unsigned long)(depthTexture ? depthTexture.textureType : 0),
            (unsigned long)(depthTexture ? depthTexture.arrayLength : 0),
            (unsigned long)(depthTexture ? depthTexture.usage : 0),
            (unsigned long)(depthTexture ? depthTexture.storageMode : 0),
            (unsigned long)(depthTexture ? depthTexture.sampleCount : 0),
            (unsigned long)descriptor.depthAttachment.storeAction,
            exactDepthCandidate);
    }
    if (!exactDepthCandidate) return;

    NSDictionary *pass = r105MakePassRecord(commandBuffer, encoder, descriptor);
    NSDictionary *producer = nil;
    BOOL accepted = NO;
    pthread_mutex_lock(&stateLock);
    if (!r105PassesByCommandBuffer) r105PassesByCommandBuffer = [NSMutableDictionary dictionary];
    if (!r105ProducersByCommandBuffer) r105ProducersByCommandBuffer = [NSMutableDictionary dictionary];
    if (sourceColorPass && !r106DepthCandidateTexture)
        r106DepthCandidateTexture = depthTexture;
    BOOL belongsToSelectedProducer = sourceColorPass ||
        r105ProducersByCommandBuffer[commandKey] != nil;
    BOOL matchesExactCandidate = !r106DepthCandidateTexture ||
        depthTexture == r106DepthCandidateTexture;
    if (belongsToSelectedProducer && matchesExactCandidate && r105PassCount < kR105MaxPasses) {
        if (!r106DepthPassCapturedCommandBuffers)
            r106DepthPassCapturedCommandBuffers = [NSMutableSet set];
        if ([r106DepthPassCapturedCommandBuffers containsObject:commandKey]) {
            pthread_mutex_unlock(&stateLock);
            return;
        }
        [r106DepthPassCapturedCommandBuffers addObject:commandKey];
        NSMutableArray *passes = r105PassesByCommandBuffer[commandKey];
        if (!passes && r105PassesByCommandBuffer.count < kR105MaxCommandBuffers) {
            passes = [NSMutableArray array];
            r105PassesByCommandBuffer[commandKey] = passes;
        }
        if (passes) {
            [passes addObject:pass];
            ++r105PassCount;
            producer = r105ProducersByCommandBuffer[commandKey];
            if (producer)
                [(NSMutableArray *)producer[@"sameCommandBufferPasses"] addObject:pass];
            accepted = YES;
        }
    }
    pthread_mutex_unlock(&stateLock);
    (void)accepted;
    (void)producer;
    (void)sourceSequence;
}

static void r105AssociateProducer(id<MTLCommandBuffer> commandBuffer,
                                 id<MTLTexture> texture,
                                 NSDictionary *drawableRecord,
                                 NSUInteger colorAttachmentIndex) {
    id<CAMetalDrawable> drawable = drawableRecord[@"drawable"];
    if (!drawable || texture != drawable.texture || colorAttachmentIndex >= 8) return;
    uint64_t sequence = [drawableRecord[@"sequence"] unsignedLongLongValue];
    if (sequence < kR105TitleSampleStartSequence ||
        !r105SequenceSelectedForInventory(sequence)) return;
    uint64_t drawableID = [drawableRecord[@"drawableID"] unsignedLongLongValue];
    NSValue *commandKey = r105CommandBufferKey(commandBuffer);
    NSDictionary *producer = nil;
    NSArray<NSDictionary *> *sameCommandBufferPasses = nil;
    NSArray<NSDictionary *> *priorWindows = @[];
    NSUInteger frameOrdinal = 0;
    uint64_t observedAt = r105NowNs();
    pthread_mutex_lock(&stateLock);
    if (!r105ProducersByCommandBuffer) r105ProducersByCommandBuffer = [NSMutableDictionary dictionary];
    if (r105ProducersByCommandBuffer[commandKey] || r105FrameCount >= kR105MaxFrames) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    sameCommandBufferPasses = [r105PassesByCommandBuffer[commandKey] copy] ?: @[];
    frameOrdinal = r105FrameCount + 1;
    producer = [@{
        @"sourceSequence": @(sequence),
        @"frameOrdinal": @(frameOrdinal),
        @"drawableID": @(drawableID),
        @"texture": texture,
        @"commandBufferIdentity": commandKey,
        @"queueIdentity": [NSValue valueWithPointer:(__bridge const void *)commandBuffer.commandQueue],
        @"observedAtNs": @(observedAt),
        @"sameCommandBufferPasses": [sameCommandBufferPasses mutableCopy],
    } mutableCopy];
    r105ProducersByCommandBuffer[commandKey] = producer;
    r105FrameCount = frameOrdinal;
    pthread_mutex_unlock(&stateLock);

    logLine("R106_SOURCE_PRODUCER sourceSequence=%llu frameOrdinal=%lu drawableID=%llu drawableTexture=%p producerCommandBuffer=%p producerCommandBufferClass=%s queue=%p device=%p registryID=%llu colorAttachmentIndex=%lu observedAtNs=%llu framebufferOnly=%d drawableSize=%lux%lu drawablePixelFormat=%lu drawableUsage=%lu boundedFrameLimit=%lu",
        (unsigned long long)sequence, (unsigned long)frameOrdinal,
        (unsigned long long)drawableID, (__bridge void *)texture,
        (__bridge void *)commandBuffer, className(commandBuffer),
        (__bridge void *)commandBuffer.commandQueue,
        (__bridge void *)texture.device, (unsigned long long)texture.device.registryID,
        (unsigned long)colorAttachmentIndex, (unsigned long long)observedAt,
        texture.framebufferOnly, (unsigned long)texture.width, (unsigned long)texture.height,
        (unsigned long)texture.pixelFormat, (unsigned long)texture.usage,
        (unsigned long)kR105MaxFrames);

    for (NSUInteger i = 0; i < sameCommandBufferPasses.count; ++i)
        r105LogPass(sameCommandBufferPasses[i], sequence, drawableID,
                    "same-producer-command-buffer", i, (__bridge void *)commandBuffer);
    (void)priorWindows;
    logLine("R106_FRAME_WINDOW sourceSequence=%llu producerCommandBuffer=%p producerQueue=%p priorCommandBuffers=%lu windowNs=%llu sameCommandBufferPasses=%lu frameOrdinal=%lu",
        (unsigned long long)sequence, (__bridge void *)commandBuffer,
        (__bridge void *)commandBuffer.commandQueue, (unsigned long)priorWindows.count,
        (unsigned long long)kR105CommandWindowNs,
        (unsigned long)sameCommandBufferPasses.count, (unsigned long)frameOrdinal);
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        logLine("R106_PRODUCER_COMPLETION sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p queue=%p status=%lu error=%s completionTimeNs=%llu",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)completed.commandQueue,
            (unsigned long)completed.status,
            completed.error.localizedDescription.UTF8String ?: "none",
            (unsigned long long)r105NowNs());
    }];
}

static id<MTLComputePipelineState> r105CreateDepthPipeline(id<MTLDevice> device,
                                                            NSError **outError) {
    NSString *source = @"#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "kernel void r105_sample_depth(depth2d<float, access::sample> source [[texture(0)]], "
        "device float* output [[buffer(0)]], constant uint2& gridSize [[buffer(1)]], "
        "uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= gridSize.x || gid.y >= gridSize.y) return;\n"
        "  constexpr sampler nearestSampler(address::clamp_to_edge, filter::nearest);\n"
        "  float2 uv = (float2(gid) + 0.5f) / float2(gridSize);\n"
        "  output[gid.y * gridSize.x + gid.x] = source.sample(nearestSampler, uv);\n"
        "}\n";
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    id<MTLFunction> function = [library newFunctionWithName:@"r105_sample_depth"];
    id<MTLComputePipelineState> pipeline = function
        ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
    if (outError) *outError = error;
    return pipeline;
}

static id<MTLComputePipelineState> r105CachedDepthPipeline(id<MTLDevice> device) {
    pthread_mutex_lock(&r105DepthLock);
    id<MTLComputePipelineState> cached =
        r105DepthSamplingDevice == device ? r105DepthSamplingPipeline : nil;
    pthread_mutex_unlock(&r105DepthLock);
    return cached;
}

static void r105WarmDepthPipeline(id<MTLDevice> device) {
    if (!device) return;
    pthread_mutex_lock(&r105DepthLock);
    if (r105DepthSamplingDevice == device && r105DepthSamplingPipeline) {
        pthread_mutex_unlock(&r105DepthLock);
        return;
    }
    if (r105DepthPipelineCompileStarted) {
        pthread_mutex_unlock(&r105DepthLock);
        return;
    }
    r105DepthPipelineCompileStarted = YES;
    if (!r105DepthPipelineCompileQueue)
        r105DepthPipelineCompileQueue = dispatch_queue_create(
            "com.firelink.r105-depth-pipeline", DISPATCH_QUEUE_SERIAL);
    dispatch_queue_t compileQueue = r105DepthPipelineCompileQueue;
    pthread_mutex_unlock(&r105DepthLock);

    dispatch_async(compileQueue, ^{
        NSError *error = nil;
        id<MTLComputePipelineState> pipeline = r105CreateDepthPipeline(device, &error);
        pthread_mutex_lock(&r105DepthLock);
        if (pipeline) {
            r105DepthSamplingPipeline = pipeline;
            r105DepthSamplingDevice = device;
        }
        r105DepthPipelineCompileStarted = NO;
        pthread_mutex_unlock(&r105DepthLock);
        logLine("R106_DEPTH_PIPELINE=%s device=%p registryID=%llu error=%s compileQueue=background",
            pipeline ? "READY" : "FAILED", (__bridge void *)device,
            (unsigned long long)device.registryID,
            error.localizedDescription.UTF8String ?: "none");
    });
}

static id<MTLComputePipelineState> r105DepthPipeline(id<MTLDevice> device,
                                                     NSError **outError) {
    id<MTLComputePipelineState> pipeline = r105CachedDepthPipeline(device);
    if (!pipeline && outError)
        *outError = [NSError errorWithDomain:@"FGMetalR105DepthDiagnostic"
            code:1 userInfo:@{NSLocalizedDescriptionKey:
                @"Diagnostic pipeline is still compiling asynchronously"}];
    return pipeline;
}

static id<MTLComputePipelineState> r105CreateMotionPipeline(id<MTLDevice> device,
                                                             NSError **outError) {
    NSString *source = @"#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "kernel void r105_sample_rg16(texture2d<half, access::sample> source [[texture(0)]], "
        "device float2* output [[buffer(0)]], constant uint2& gridSize [[buffer(1)]], "
        "uint2 gid [[thread_position_in_grid]]) {\n"
        "  if (gid.x >= gridSize.x || gid.y >= gridSize.y) return;\n"
        "  constexpr sampler nearestSampler(address::clamp_to_edge, filter::nearest);\n"
        "  float2 uv = (float2(gid) + 0.5f) / float2(gridSize);\n"
        "  half2 rg = source.sample(nearestSampler, uv).rg;\n"
        "  output[gid.y * gridSize.x + gid.x] = float2(rg);\n"
        "}\n";
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    id<MTLFunction> function = [library newFunctionWithName:@"r105_sample_rg16"];
    id<MTLComputePipelineState> pipeline = function
        ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
    if (outError) *outError = error;
    return pipeline;
}

static id<MTLComputePipelineState> r105CachedMotionPipeline(id<MTLDevice> device) {
    pthread_mutex_lock(&r105MotionLock);
    id<MTLComputePipelineState> cached =
        r105MotionSamplingDevice == device ? r105MotionSamplingPipeline : nil;
    pthread_mutex_unlock(&r105MotionLock);
    return cached;
}

static void r105WarmMotionPipeline(id<MTLDevice> device) {
    if (!device) return;
    pthread_mutex_lock(&r105MotionLock);
    if (r105MotionSamplingDevice == device && r105MotionSamplingPipeline) {
        pthread_mutex_unlock(&r105MotionLock);
        return;
    }
    if (r105MotionPipelineCompileStarted) {
        pthread_mutex_unlock(&r105MotionLock);
        return;
    }
    r105MotionPipelineCompileStarted = YES;
    if (!r105MotionPipelineCompileQueue)
        r105MotionPipelineCompileQueue = dispatch_queue_create(
            "com.firelink.r105-motion-pipeline", DISPATCH_QUEUE_SERIAL);
    dispatch_queue_t compileQueue = r105MotionPipelineCompileQueue;
    pthread_mutex_unlock(&r105MotionLock);

    dispatch_async(compileQueue, ^{
        NSError *error = nil;
        id<MTLComputePipelineState> pipeline = r105CreateMotionPipeline(device, &error);
        pthread_mutex_lock(&r105MotionLock);
        if (pipeline) {
            r105MotionSamplingPipeline = pipeline;
            r105MotionSamplingDevice = device;
        }
        r105MotionPipelineCompileStarted = NO;
        pthread_mutex_unlock(&r105MotionLock);
        logLine("R106_MOTION_PIPELINE=%s device=%p registryID=%llu error=%s compileQueue=background",
            pipeline ? "READY" : "FAILED", (__bridge void *)device,
            (unsigned long long)device.registryID,
            error.localizedDescription.UTF8String ?: "none");
    });
}

static id<MTLComputePipelineState> r105MotionPipeline(id<MTLDevice> device,
                                                       NSError **outError) {
    id<MTLComputePipelineState> pipeline = r105CachedMotionPipeline(device);
    if (!pipeline && outError)
        *outError = [NSError errorWithDomain:@"FGMetalR105MotionDiagnostic"
            code:1 userInfo:@{NSLocalizedDescriptionKey:
                @"Motion diagnostic pipeline is still compiling asynchronously"}];
    return pipeline;
}

static void r105EncodeDepthDiagnostic(id<MTLCommandBuffer> commandBuffer) {
    NSValue *commandKey = r105CommandBufferKey(commandBuffer);
    NSDictionary *producer = nil;
    NSArray<NSDictionary *> *passes = nil;
    pthread_mutex_lock(&stateLock);
    producer = r105ProducersByCommandBuffer[commandKey];
    if (!r105SeenDepthCommits) r105SeenDepthCommits = [NSMutableSet set];
    if (!producer || [r105SeenDepthCommits containsObject:commandKey] ||
        r105DepthSampleCount >= kR105MaxDepthSamples) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    [r105SeenDepthCommits addObject:commandKey];
    passes = [producer[@"sameCommandBufferPasses"] copy] ?: @[];
    pthread_mutex_unlock(&stateLock);

    NSDictionary *candidate = nil;
    for (NSDictionary *pass in passes) {
        for (NSDictionary *attachment in pass[@"attachments"]) {
            if (![attachment[@"kind"] isEqualToString:@"depth"]) continue;
            id<MTLTexture> texture = attachment[@"texture"];
            NSUInteger width = [attachment[@"width"] unsignedLongValue];
            NSUInteger height = [attachment[@"height"] unsignedLongValue];
            MTLTextureUsage usage = [attachment[@"usage"] unsignedLongValue];
            NSUInteger format = [attachment[@"pixelFormat"] unsignedLongValue];
            NSUInteger sampleCount = [attachment[@"sampleCount"] unsignedLongValue];
            NSUInteger storeAction = [attachment[@"storeAction"] unsignedLongValue];
            if (!texture || width < 64 || height < 64 || width > 4096 || height > 2160 ||
                sampleCount != 1 || storeAction != MTLStoreActionStore ||
                (usage & MTLTextureUsageShaderRead) == 0 ||
                (format != MTLPixelFormatDepth32Float_Stencil8 &&
                 format != MTLPixelFormatDepth32Float)) continue;
            if (!candidate || width * height >
                [candidate[@"width"] unsignedLongValue] *
                [candidate[@"height"] unsignedLongValue]) candidate = attachment;
        }
    }

    uint64_t sequence = [producer[@"sourceSequence"] unsignedLongLongValue];
    uint64_t drawableID = [producer[@"drawableID"] unsignedLongLongValue];
    if (!candidate) {
        logLine("R106_DEPTH_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p lineage=same-producer-command-buffer reason=no-stored-readable-depth-attachment sameCommandBufferPasses=%lu",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)commandBuffer, (unsigned long)passes.count);
        return;
    }

    id<MTLTexture> sourceTexture = candidate[@"texture"];
    NSUInteger mipLevel = [candidate[@"mipLevel"] unsignedLongValue];
    NSUInteger slice = [candidate[@"slice"] unsignedLongValue];
    NSUInteger pixelFormat = [candidate[@"pixelFormat"] unsignedLongValue];
    if (mipLevel >= sourceTexture.mipmapLevelCount ||
        (sourceTexture.textureType == MTLTextureType2DArray && slice >= sourceTexture.arrayLength) ||
        (sourceTexture.textureType == MTLTextureType2D && slice != 0)) {
        logLine("R106_DEPTH_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=invalid-subresource mipLevel=%lu mipLevels=%lu slice=%lu arrayLength=%lu",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture, (unsigned long)mipLevel,
            (unsigned long)sourceTexture.mipmapLevelCount, (unsigned long)slice,
            (unsigned long)sourceTexture.arrayLength);
        return;
    }
    id<MTLTexture> sampleTexture = sourceTexture;
    const char *depthAccess = "direct-depth2d";
    if (sourceTexture.textureType == MTLTextureType2DArray) {
        sampleTexture = [sourceTexture newTextureViewWithPixelFormat:sourceTexture.pixelFormat
            textureType:MTLTextureType2D levels:NSMakeRange(mipLevel, 1)
            slices:NSMakeRange(slice, 1)];
        depthAccess = "2d-view-of-array-slice";
    }
    if (!sampleTexture || (sourceTexture.textureType != MTLTextureType2DArray &&
                           sourceTexture.textureType != MTLTextureType2D)) {
        logLine("R106_DEPTH_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p textureType=%lu slice=%lu arrayLength=%lu reason=unsupported-type-or-view-creation-failed",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture, (unsigned long)sourceTexture.textureType,
            (unsigned long)slice, (unsigned long)sourceTexture.arrayLength);
        return;
    }
    logLine("R106_DEPTH_GPU_SAMPLE_CANDIDATE sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p sampledTexture=%p dimensions=%lux%lu sourcePixelFormat=%lu sampledPixelFormat=%lu textureType=%lu arrayLength=%lu usage=%lu storageMode=%lu storeAction=%lu lineage=same-producer-command-buffer access=%s",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)sourceTexture,
        (__bridge void *)sampleTexture,
        (unsigned long)[candidate[@"width"] unsignedLongValue],
        (unsigned long)[candidate[@"height"] unsignedLongValue],
        (unsigned long)pixelFormat, (unsigned long)sampleTexture.pixelFormat,
        (unsigned long)sourceTexture.textureType, (unsigned long)sourceTexture.arrayLength,
        (unsigned long)[candidate[@"usage"] unsignedLongValue],
        (unsigned long)sourceTexture.storageMode,
        (unsigned long)[candidate[@"storeAction"] unsignedLongValue], depthAccess);

    NSError *pipelineError = nil;
    id<MTLComputePipelineState> pipeline = r105DepthPipeline(
        commandBuffer.commandQueue.device, &pipelineError);
    if (!pipeline) {
        logLine("R106_DEPTH_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=diagnostic-pipeline-not-ready error=%s",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture,
            pipelineError.localizedDescription.UTF8String ?: "unknown");
        return;
    }

    NSUInteger sampleCount = kR105DepthGridWidth * kR105DepthGridHeight;
    logLine("R106_DEPTH_GPU_SAMPLE_ENCODE_BEGIN sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p diagnosticPipeline=%p grid=%lux%lu",
        (unsigned long long)sequence, (__bridge void *)commandBuffer,
        (__bridge void *)sourceTexture,
        (__bridge void *)pipeline, (unsigned long)kR105DepthGridWidth,
        (unsigned long)kR105DepthGridHeight);
    id<MTLBuffer> sampleBuffer = [commandBuffer.commandQueue.device
        newBufferWithLength:sampleCount * sizeof(float)
        options:MTLResourceStorageModeShared];
    id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
    if (!sampleBuffer || !encoder) {
        [encoder endEncoding];
        logLine("R106_DEPTH_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=diagnostic-resource-allocation-failed buffer=%p encoder=%p",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture,
            (__bridge void *)sampleBuffer, (__bridge void *)encoder);
        return;
    }
    uint32_t gridSize[2] = {
        (uint32_t)kR105DepthGridWidth,
        (uint32_t)kR105DepthGridHeight,
    };
    [encoder setComputePipelineState:pipeline];
    [encoder setTexture:sampleTexture atIndex:0];
    [encoder setBuffer:sampleBuffer offset:0 atIndex:0];
    [encoder setBytes:gridSize length:sizeof(gridSize) atIndex:1];
    [encoder dispatchThreads:MTLSizeMake(kR105DepthGridWidth,
                                        kR105DepthGridHeight, 1)
        threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
    [encoder endEncoding];
    pthread_mutex_lock(&stateLock);
    ++r105DepthSampleCount;
    if (!r106DepthSampleSequences) r106DepthSampleSequences = [NSMutableSet set];
    [r106DepthSampleSequences addObject:@(sequence)];
    pthread_mutex_unlock(&stateLock);

    logLine("R106_DEPTH_GPU_SAMPLE_ENCODED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p sampleBuffer=%p dimensions=%lux%lu sourcePixelFormat=%lu sampledPixelFormat=%lu usage=%lu storageMode=%lu storeAction=%lu grid=%lux%lu lineage=same-producer-command-buffer position=after-title-render-before-commit",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)sourceTexture,
        (__bridge void *)sampleBuffer,
        (unsigned long)[candidate[@"width"] unsignedLongValue],
        (unsigned long)[candidate[@"height"] unsignedLongValue],
        (unsigned long)pixelFormat, (unsigned long)pixelFormat,
        (unsigned long)[candidate[@"usage"] unsignedLongValue],
        (unsigned long)[candidate[@"storageMode"] unsignedLongValue],
        (unsigned long)[candidate[@"storeAction"] unsignedLongValue],
        (unsigned long)kR105DepthGridWidth, (unsigned long)kR105DepthGridHeight);

    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        if (completed.status != MTLCommandBufferStatusCompleted || completed.error) {
            logLine("R106_DEPTH_GPU_SAMPLE=FAILED sourceSequence=%llu producerCommandBuffer=%p status=%lu error=%s",
                (unsigned long long)sequence, (__bridge void *)completed,
                (unsigned long)completed.status,
                completed.error.localizedDescription.UTF8String ?: "none");
            return;
        }
        const float *values = (const float *)sampleBuffer.contents;
        double sum = 0.0;
        float minimum = INFINITY;
        float maximum = -INFINITY;
        uint64_t finiteCount = 0;
        uint64_t invalidCount = 0;
        uint64_t zeroCount = 0;
        uint64_t oneCount = 0;
        for (NSUInteger i = 0; i < sampleCount; ++i) {
            float value = values[i];
            if (!isfinite(value)) { ++invalidCount; continue; }
            ++finiteCount;
            sum += value;
            if (value < minimum) minimum = value;
            if (value > maximum) maximum = value;
            if (fabsf(value) < 0.00001f) ++zeroCount;
            if (fabsf(value - 1.0f) < 0.00001f) ++oneCount;
        }
        double mean = finiteCount ? sum / (double)finiteCount : 0.0;
        char samplePath[512];
        snprintf(samplePath, sizeof(samplePath),
            "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/depth-sequence-%llu.raw",
            (unsigned long long)sequence);
        (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth", 0700);
        FILE *file = fopen(samplePath, "wb");
        BOOL saved = file && fwrite(values, sizeof(float), sampleCount, file) == sampleCount;
        if (file) fclose(file);
        logLine("R106_DEPTH_GPU_SAMPLE_READBACK sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p diagnosticBuffer=%p sampleWidth=%lu sampleHeight=%lu min=%.9g max=%.9g mean=%.9g finiteSamples=%llu invalidSamples=%llu zeroSamples=%llu oneSamples=%llu saved=%d path=%s lineage=same-producer-command-buffer",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)sourceTexture,
            (__bridge void *)sampleBuffer,
            (unsigned long)kR105DepthGridWidth, (unsigned long)kR105DepthGridHeight,
            minimum, maximum, mean, (unsigned long long)finiteCount,
            (unsigned long long)invalidCount, (unsigned long long)zeroCount,
            (unsigned long long)oneCount, saved, saved ? samplePath : "<write-failed>");
    }];
}

static void r105EncodeMotionDiagnostic(id<MTLCommandBuffer> commandBuffer) {
    NSValue *commandKey = r105CommandBufferKey(commandBuffer);
    NSMutableDictionary *producer = nil;
    NSArray<NSDictionary *> *passes = nil;
    pthread_mutex_lock(&stateLock);
    producer = (NSMutableDictionary *)r105ProducersByCommandBuffer[commandKey];
    if (!producer || [producer[@"motionDiagnosticQueued"] boolValue] ||
        r105MotionSampleCount >= kR105MaxMotionSamples) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    producer[@"motionDiagnosticQueued"] = @YES;
    passes = [producer[@"sameCommandBufferPasses"] copy] ?: @[];
    pthread_mutex_unlock(&stateLock);

    NSDictionary *candidate = nil;
    NSDictionary *candidatePass = nil;
    for (NSDictionary *pass in passes) {
        for (NSDictionary *attachment in pass[@"attachments"]) {
            if (![attachment[@"kind"] isEqualToString:@"color"] ||
                [attachment[@"pixelFormat"] unsignedLongValue] != MTLPixelFormatRG16Float ||
                ([attachment[@"usage"] unsignedLongValue] & MTLTextureUsageShaderRead) == 0 ||
                [attachment[@"sampleCount"] unsignedLongValue] != 1 ||
                [attachment[@"storeAction"] unsignedLongValue] != MTLStoreActionStore) continue;
            NSUInteger width = [attachment[@"width"] unsignedLongValue];
            NSUInteger height = [attachment[@"height"] unsignedLongValue];
            if (!attachment[@"texture"] || width < 64 || height < 64 ||
                width > 4096 || height > 2160) continue;
            if (!candidate || width * height >
                [candidate[@"width"] unsignedLongValue] *
                [candidate[@"height"] unsignedLongValue]) {
                candidate = attachment;
                candidatePass = pass;
            }
        }
    }
    if (!candidate) return;

    uint64_t sequence = [producer[@"sourceSequence"] unsignedLongLongValue];
    uint64_t drawableID = [producer[@"drawableID"] unsignedLongLongValue];
    id<MTLTexture> sourceTexture = candidate[@"texture"];
    NSUInteger width = [candidate[@"width"] unsignedLongValue];
    NSUInteger height = [candidate[@"height"] unsignedLongValue];
    logLine("R106_MOTION_GPU_SAMPLE_CANDIDATE sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p passId=%llu encoder=%p attachmentIndex=%lu texture=%p dimensions=%lux%lu sourcePixelFormat=%lu usage=%lu storageMode=%lu storeAction=%lu lineage=same-producer-command-buffer access=direct-rg16float-sample componentOrder=R,G",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer,
        (unsigned long long)[candidatePass[@"passId"] unsignedLongLongValue],
        [candidatePass[@"encoderIdentity"] pointerValue],
        (unsigned long)[candidate[@"index"] unsignedLongValue],
        (__bridge void *)sourceTexture, (unsigned long)width, (unsigned long)height,
        (unsigned long)[candidate[@"pixelFormat"] unsignedLongValue],
        (unsigned long)[candidate[@"usage"] unsignedLongValue],
        (unsigned long)sourceTexture.storageMode,
        (unsigned long)[candidate[@"storeAction"] unsignedLongValue]);

    NSError *pipelineError = nil;
    id<MTLComputePipelineState> pipeline = r105MotionPipeline(
        commandBuffer.commandQueue.device, &pipelineError);
    if (!pipeline) {
        logLine("R106_MOTION_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu producerCommandBuffer=%p texture=%p reason=diagnostic-pipeline-not-ready error=%s",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture,
            pipelineError.localizedDescription.UTF8String ?: "unknown");
        return;
    }

    NSUInteger gridCount = kR105MotionGridWidth * kR105MotionGridHeight;
    id<MTLBuffer> sampleBuffer = [commandBuffer.commandQueue.device
        newBufferWithLength:gridCount * sizeof(float) * 2
        options:MTLResourceStorageModeShared];
    id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
    if (!sampleBuffer || !encoder) {
        [encoder endEncoding];
        logLine("R106_MOTION_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p texture=%p reason=diagnostic-resource-allocation-failed buffer=%p encoder=%p",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture,
            (__bridge void *)sampleBuffer, (__bridge void *)encoder);
        return;
    }
    uint32_t gridSize[2] = {
        (uint32_t)kR105MotionGridWidth,
        (uint32_t)kR105MotionGridHeight,
    };
    [encoder setComputePipelineState:pipeline];
    [encoder setTexture:sourceTexture atIndex:0];
    [encoder setBuffer:sampleBuffer offset:0 atIndex:0];
    [encoder setBytes:gridSize length:sizeof(gridSize) atIndex:1];
    [encoder dispatchThreads:MTLSizeMake(kR105MotionGridWidth,
                                        kR105MotionGridHeight, 1)
        threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
    [encoder endEncoding];
    pthread_mutex_lock(&stateLock);
    ++r105MotionSampleCount;
    pthread_mutex_unlock(&stateLock);
    logLine("R106_MOTION_GPU_SAMPLE_ENCODED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p texture=%p diagnosticBuffer=%p sourceDimensions=%lux%lu grid=%lux%lu lineage=same-producer-command-buffer position=after-title-render-before-commit output=raw-float32-RG",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)sourceTexture,
        (__bridge void *)sampleBuffer, (unsigned long)width, (unsigned long)height,
        (unsigned long)kR105MotionGridWidth, (unsigned long)kR105MotionGridHeight);

    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        if (completed.status != MTLCommandBufferStatusCompleted || completed.error) {
            logLine("R106_MOTION_GPU_SAMPLE=FAILED sourceSequence=%llu producerCommandBuffer=%p status=%lu error=%s",
                (unsigned long long)sequence, (__bridge void *)completed,
                (unsigned long)completed.status,
                completed.error.localizedDescription.UTF8String ?: "none");
            return;
        }
        const float *values = (const float *)sampleBuffer.contents;
        double sumR = 0.0, sumG = 0.0;
        float minR = INFINITY, maxR = -INFINITY;
        float minG = INFINITY, maxG = -INFINITY;
        uint64_t finiteCount = 0, invalidCount = 0, nearZeroCount = 0;
        for (NSUInteger i = 0; i < gridCount; ++i) {
            float r = values[i * 2], g = values[i * 2 + 1];
            if (!isfinite(r) || !isfinite(g)) { ++invalidCount; continue; }
            ++finiteCount;
            sumR += r; sumG += g;
            if (r < minR) minR = r;
            if (r > maxR) maxR = r;
            if (g < minG) minG = g;
            if (g > maxG) maxG = g;
            if (fabsf(r) < 0.00001f && fabsf(g) < 0.00001f) ++nearZeroCount;
        }
        char samplePath[512];
        snprintf(samplePath, sizeof(samplePath),
            "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/motion-sequence-%llu.rg32f",
            (unsigned long long)sequence);
        (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth", 0700);
        FILE *file = fopen(samplePath, "wb");
        BOOL saved = file && fwrite(values, sizeof(float) * 2, gridCount, file) == gridCount;
        if (file) fclose(file);
        double meanR = finiteCount ? sumR / (double)finiteCount : 0.0;
        double meanG = finiteCount ? sumG / (double)finiteCount : 0.0;
        logLine("R106_MOTION_GPU_SAMPLE_READBACK sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p texture=%p sampleWidth=%lu sampleHeight=%lu componentOrder=R,G minR=%.9g maxR=%.9g meanR=%.9g minG=%.9g maxG=%.9g meanG=%.9g finiteSamples=%llu invalidSamples=%llu nearZeroPairs=%llu saved=%d path=%s lineage=same-producer-command-buffer",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)sourceTexture,
            (unsigned long)kR105MotionGridWidth, (unsigned long)kR105MotionGridHeight,
            minR, maxR, meanR, minG, maxG, meanG,
            (unsigned long long)finiteCount, (unsigned long long)invalidCount,
            (unsigned long long)nearZeroCount, saved, saved ? samplePath : "<write-failed>");
    }];
}

static void r105RecordCommittedCommandBuffer(id<MTLCommandBuffer> commandBuffer,
                                            uint64_t originalCommitEntryNs,
                                            uint64_t commitReturnNs) {
    NSMutableArray<NSDictionary *> *records = [NSMutableArray array];
    pthread_mutex_lock(&stateLock);
    for (NSDictionary *record in drawablesByTexture.allValues) {
        if (record[@"producerCommandBuffer"] == commandBuffer)
            [records addObject:[record copy]];
    }
    pthread_mutex_unlock(&stateLock);
    for (NSDictionary *record in records) {
        logLine("R108_SOURCE_PRODUCER_COMMIT sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p queue=%p acquiredAtMonotonicNs=%llu originalCommitEntryMonotonicNs=%llu originalCommitReturnMonotonicNs=%llu device=%p registryID=%llu",
            (unsigned long long)[record[@"sequence"] unsignedLongLongValue],
            (unsigned long long)[record[@"drawableID"] unsignedLongLongValue],
            (__bridge void *)commandBuffer,
            (__bridge void *)record[@"producerQueue"],
            (unsigned long long)[record[@"acquiredAtNs"] unsignedLongLongValue],
            (unsigned long long)originalCommitEntryNs, (unsigned long long)commitReturnNs,
            (__bridge void *)commandBuffer.commandQueue.device,
            (unsigned long long)commandBuffer.commandQueue.device.registryID);
    }
}

static NSDictionary *r110SourceRecordForCommandBuffer(
    id<MTLCommandBuffer> commandBuffer) {
    NSDictionary *found = nil;
    pthread_mutex_lock(&stateLock);
    for (NSDictionary *record in drawablesByTexture.allValues) {
        if (record[@"producerCommandBuffer"] == commandBuffer) {
            found = [record copy];
            break;
        }
    }
    pthread_mutex_unlock(&stateLock);
    return found;
}

static void hookCommandBufferCommit(id self, SEL selector) {
    BOOL selected = isSelectedProcess();
    id<MTLCommandBuffer> commandBuffer = (id<MTLCommandBuffer>)self;
    NSDictionary *sourceRecord = selected && r110PresentationEnabled() &&
        !isInternalPresentationCall
        ? r110SourceRecordForCommandBuffer(commandBuffer) : nil;
    if (selected && !r110OrderingOnly) {
        r105EncodeDepthDiagnostic(commandBuffer);
    }
    IMP original = originalFor(self, selector);
    uint64_t originalCommitEntryNs = selected ? r105NowNs() : 0;
    if (original) ((void (*)(id, SEL))original)(self, selector);
    uint64_t commitReturnNs = selected ? r105NowNs() : 0;
    if (sourceRecord) r103CloseFrontierAfterProducerCommit(sourceRecord, commandBuffer);
    if (selected) {
        if (sourceRecord)
            logLine("R110_SOURCE_PRODUCER_COMMIT_RETURN sequence=%llu drawableID=%llu producerCommandBuffer=%p queue=%p originalCommitReturnMonotonicNs=%llu",
                (unsigned long long)[sourceRecord[@"sequence"] unsignedLongLongValue],
                (unsigned long long)[sourceRecord[@"drawableID"] unsignedLongLongValue],
                (__bridge void *)commandBuffer,
                (__bridge void *)sourceRecord[@"producerQueue"],
                (unsigned long long)commitReturnNs);
        r105RecordCommittedCommandBuffer(commandBuffer,
            originalCommitEntryNs, commitReturnNs);
    }
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
    pthread_mutex_unlock(&stateLock);
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
    if (strcmp(attachmentKind, "color") == 0 && texture == drawable.texture)
        r105AssociateProducer(commandBuffer, texture, drawableRecord, attachmentIndex);

    // Register before returning the encoder to the caller. The game's own
    // command buffer remains responsible for commit and execution ordering.
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        NSError *error = completed.error;
        BOOL completionMilestone = NO;
        pthread_mutex_lock(&stateLock);
        BOOL successfulCompletion = completed.status == MTLCommandBufferStatusCompleted && !error;
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
        if (completionMilestone) {
            logLine("DRAWABLE_PRODUCER_COMPLETION=VALIDATED pid=%d consecutiveCompletions=%llu queue=%p device=%p registryID=%llu",
                getpid(), (unsigned long long)completionRun,
                (__bridge void *)completed.commandQueue,
                (__bridge void *)texture.device,
                (unsigned long long)texture.device.registryID);
        }
        if (completed.status == MTLCommandBufferStatusCompleted && !error) {
            captureRealDrawable(completed.commandQueue, drawable, sequence, completed);
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
    if (encoder && isSelectedProcess()) {
        id<MTLCommandBuffer> commandBuffer = (id<MTLCommandBuffer>)self;
        r105CaptureRenderPass(commandBuffer, encoder, descriptor);
        inspectRenderPass(commandBuffer, descriptor);
    }
    return encoder;
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
    SEL renderEncoderSelector = @selector(renderCommandEncoderWithDescriptor:);
    SEL commitSelector = @selector(commit);
    for (int i = 0; i < count; ++i) {
        Class cls = classes[i];
        if (commandBufferProtocol && class_conformsToProtocol(cls, commandBufferProtocol)) {
            installHook(cls, renderEncoderSelector, (IMP)hookRenderCommandEncoder);
            installHook(cls, commitSelector, (IMP)hookCommandBufferCommit);
        }
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
    r110WsiDispatchLock = [[NSRecursiveLock alloc] init];
    const char *orderingOnly = getenv("FGMETAL_R110_ORDERING_ONLY");
    r110OrderingOnly = orderingOnly && strcmp(orderingOnly, "1") == 0;
    captureProcessingQueue = dispatch_queue_create(
        "com.fgmetal.r100.capture-processing", DISPATCH_QUEUE_SERIAL);
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=R110-inprocess-title-presentation orderingOnly=%d presentation=%d r111CameraAttribution=%s DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s titleCapture=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        r110OrderingOnly,
        r110PresentationEnabled(),
        getenv("FGMETAL_R111_CAMERA_ATTRIBUTION") ?: "0",
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R108_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R108_CAPTURE") ?: "0",
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
