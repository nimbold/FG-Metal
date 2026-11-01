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
static void logSourceWsiDispatch(id self, SEL selector);
static void releaseClaimedSlot(uint64_t sourceSequence, const char *reason);
static void hookDrawablePresent(id self, SEL selector);
static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when);
static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration);

static void initLog(void) {
    (void)mkdir("/tmp/fgmetal-r101-harness", 0700);
    char path[256];
    snprintf(path, sizeof(path), "/tmp/fgmetal-r101-harness/correlation.%d.log", getpid());
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
    BOOL selected = NO;
    if (isSelectedProcess()) {
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

    id<CAMetalDrawable> drawable = ((id (*)(id, SEL))originalNextDrawable)(self, selector);
    if (drawable && selected && isSelectedLayer(layer)) {
        installDrawablePresentationHooks(drawable);
        retainDrawableForCorrelation(drawable);
    }
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

static void tryPresentInternalBeforeSource(id<CAMetalDrawable> sourceDrawable) {
    const char *presentFlag = getenv("FGMETAL_R101_PRESENT");
    if (!presentFlag || strcmp(presentFlag, "1") != 0 || isInternalPresentationCall ||
        !isSelectedLayer((CAMetalLayer *)sourceDrawable.layer)) return;

    NSDictionary *record = trackedDrawableForTexture(sourceDrawable.texture);
    uint64_t sourceSequence = [record[@"sequence"] unsignedLongLongValue];
    if (sourceSequence < 3 || sourceSequence > kCaptureLimit) return;

    id<MTLCommandBuffer> producer = record[@"producerCommandBuffer"];
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    if (!producer || !queue || producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError) {
        if (sourceSequence >= 4) {
            pthread_mutex_lock(&stateLock);
            if (lastPresentationAttemptSequence != sourceSequence) {
                lastPresentationAttemptSequence = sourceSequence;
                internalPresentationAttempted = YES;
                ++presentationAttempts;
                ++internalPresentationFallbacks;
                lastCompletedPresentationSequence = sourceSequence;
            }
            pthread_mutex_unlock(&stateLock);
            logLine("R101_INTERNAL_PRESENT=FALLBACK appFrameIdB=%llu reason=producer-not-committed producer=%p status=%lu",
                (unsigned long long)sourceSequence, (__bridge void *)producer,
                (unsigned long)producer.status);
        }
        return;
    }

    // Capture source 3 early, but do not wait on its provider history-only
    // transition. The first presentation opportunity with a publishable M is B=4.
    captureRealDrawable(queue, sourceDrawable, sourceSequence, producer);
    if (sourceSequence < 4) return;

    id<MTLTexture> generatedTexture = nil;
    id<MTLTexture> pendingSnapshot = nil;
    uint64_t internalPresentId = 0;
    uint64_t previousSequence = 0;
    uint64_t currentSequence = 0;
    uint64_t pendingPreviousSnapshot = 0;
    uint64_t pendingCurrentSnapshot = 0;
    double claimedExpectedMainX = NAN;
    double claimedExpectedMarkerX = NAN;
    BOOL timedOut = NO;
    pthread_mutex_lock(&stateLock);
    if (lastPresentationAttemptSequence == sourceSequence) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    lastPresentationAttemptSequence = sourceSequence;
    internalPresentationAttempted = YES;
    ++presentationAttempts;
    pthread_mutex_unlock(&stateLock);

    // B's copy is already queued behind its committed producer; wait only for
    // the matching READY transition, with safe SOURCE fallback on timeout.
    pthread_mutex_lock(&stateLock);
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_nsec += 400000000;
    if (deadline.tv_nsec >= 1000000000) {
        deadline.tv_sec += deadline.tv_nsec / 1000000000;
        deadline.tv_nsec %= 1000000000;
    }
    while (internalSlotState != R101InternalSlotReady ||
           pendingInternalCurrentSequence != sourceSequence) {
        int waitResult = pthread_cond_timedwait(&presentationReadyCondition,
                                                &stateLock, &deadline);
        if (waitResult != 0) { timedOut = YES; break; }
    }
    pendingSnapshot = pendingInternalPresentationTexture;
    pendingPreviousSnapshot = pendingInternalPreviousSequence;
    pendingCurrentSnapshot = pendingInternalCurrentSequence;
    R101InternalSlotState stateSnapshot = internalSlotState;
    void *pendingLayerSnapshot = pendingInternalLayer;
    void *pendingDeviceSnapshot = pendingInternalDevice;
    if (internalSlotState == R101InternalSlotReady &&
        pendingInternalPresentationTexture &&
        pendingInternalCurrentSequence == sourceSequence &&
        pendingInternalPreviousSequence + 1 == sourceSequence &&
        pendingInternalLayer == (__bridge void *)sourceDrawable.layer &&
        pendingInternalDevice == (__bridge void *)sourceDrawable.texture.device &&
        pendingInternalPresentationTexture.device == sourceDrawable.texture.device &&
        pendingInternalPresentationTexture.width == sourceDrawable.texture.width &&
        pendingInternalPresentationTexture.height == sourceDrawable.texture.height &&
        pendingInternalPresentationTexture.pixelFormat == MTLPixelFormatRGBA16Float &&
        (pendingInternalPresentationTexture.usage & MTLTextureUsageShaderRead) != 0) {
        generatedTexture = pendingInternalPresentationTexture;
        internalPresentId = pendingInternalPresentId;
        previousSequence = pendingInternalPreviousSequence;
        currentSequence = pendingInternalCurrentSequence;
        claimedExpectedMainX = pendingExpectedMainX;
        claimedExpectedMarkerX = pendingExpectedMarkerX;
        internalSlotState = R101InternalSlotClaimed;
    } else if (internalSlotState == R101InternalSlotClaimed ||
               internalSlotState == R101InternalSlotPresented) {
        ++duplicateInternalClaims;
    } else if (internalSlotState == R101InternalSlotReady) {
        ++stalePairRejections;
        internalSlotState = R101InternalSlotEmpty;
        pendingInternalPresentationTexture = nil;
    }
    pthread_mutex_unlock(&stateLock);

    if (!generatedTexture) {
        pthread_mutex_lock(&stateLock);
        ++internalPresentationFallbacks;
        lastCompletedPresentationSequence = sourceSequence;
        pthread_mutex_unlock(&stateLock);
        logLine("R101_INTERNAL_PRESENT=FALLBACK appFrameIdB=%llu reason=%s state=%u pending=%p pendingPair=%llu->%llu layer=%p pendingLayer=%p device=%p pendingDevice=%p waitBoundMs=400",
            (unsigned long long)sourceSequence,
            timedOut ? "ready-timeout" : "pair-or-target-mismatch",
            (unsigned)stateSnapshot,
            (__bridge void *)pendingSnapshot,
            (unsigned long long)pendingPreviousSnapshot,
            (unsigned long long)pendingCurrentSnapshot,
            (__bridge void *)sourceDrawable.layer, pendingLayerSnapshot,
            (__bridge void *)sourceDrawable.texture.device, pendingDeviceSnapshot);
        return;
    }

    logLine("R101_INTERNAL_SLOT=CLAIMED internalPresentId=%llu pair=%llu->%llu targetAppFrameIdB=%llu texture=%p layer=%p device=%p",
        (unsigned long long)internalPresentId,
        (unsigned long long)previousSequence,
        (unsigned long long)currentSequence,
        (unsigned long long)sourceSequence, (__bridge void *)generatedTexture,
        (__bridge void *)sourceDrawable.layer,
        (__bridge void *)sourceDrawable.texture.device);

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
        releaseClaimedSlot(sourceSequence, "drawable-unavailable-or-device-mismatch");
        return;
    }
    if (!renderInternalFrameToDrawable(generatedTexture, internalDrawable, queue,
                                       internalPresentId,
                                       claimedExpectedMainX,
                                       claimedExpectedMarkerX)) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=FAILED internalPresentId=%llu sourceSequence=%llu->%llu reason=render-failed",
            (unsigned long long)internalPresentId,
            (unsigned long long)previousSequence, (unsigned long long)currentSequence);
        releaseClaimedSlot(sourceSequence, "render-failed");
        return;
    }

    installDrawablePresentationHooks(internalDrawable);
    IMP presentOriginal = originalFor(internalDrawable, @selector(present));
    if (!presentOriginal) {
        logLine("INTERNAL_PRESENT_EXPERIMENT=FAILED internalPresentId=%llu reason=original-present-unavailable drawable=%p",
            (unsigned long long)internalPresentId, (__bridge void *)internalDrawable);
        releaseClaimedSlot(sourceSequence, "original-present-unavailable");
        return;
    }
    uint64_t internalWsiPresentId = allocateWsiPresentId();
    observeDisplayedDrawable(internalDrawable, "INTERNAL", internalWsiPresentId,
                             0, internalPresentId);
    logLine("R101_WSI_DISPATCH origin=INTERNAL internalPresentId=%llu wsiPresentId=%llu sourcePair=%llu->%llu drawableID=%lu layer=%p texture=%p",
        (unsigned long long)internalPresentId,
        (unsigned long long)internalWsiPresentId,
        (unsigned long long)previousSequence,
        (unsigned long long)currentSequence,
        (unsigned long)internalDrawable.drawableID,
        (__bridge void *)layer, (__bridge void *)internalDrawable.texture);
    pthread_mutex_lock(&stateLock);
    internalSlotState = R101InternalSlotPresented;
    pthread_mutex_unlock(&stateLock);
    isInternalPresentationCall = YES;
    ((void (*)(id, SEL))presentOriginal)(internalDrawable, @selector(present));
    isInternalPresentationCall = NO;
    pthread_mutex_lock(&stateLock);
    ++internalPresentationSuccesses;
    lastCompletedPresentationSequence = sourceSequence;
    internalSlotState = R101InternalSlotEmpty;
    pendingInternalPresentationTexture = nil;
    pendingInternalLayer = NULL;
    pendingInternalDevice = NULL;
    pthread_mutex_unlock(&stateLock);
    logLine("R101_INTERNAL_PRESENT_DISPATCH=SUBMITTED kind=INTERNAL internalPresentId=%llu sourceSequence=%llu->%llu targetAppFrameIdB=%llu wsiPresentId=%llu internalDrawable=%p internalDrawableID=%lu layer=%p queue=%p device=%p registryID=%llu sourceAppFrameIdA=%llu sourceAppFrameIdB=%llu sourcePresentCountChanged=0",
        (unsigned long long)internalPresentId,
        (unsigned long long)previousSequence, (unsigned long long)currentSequence,
        (unsigned long long)sourceSequence,
        (unsigned long long)internalWsiPresentId,
        (__bridge void *)internalDrawable,
        (unsigned long)internalDrawable.drawableID, (__bridge void *)layer,
        (__bridge void *)queue, (__bridge void *)queue.device,
        (unsigned long long)queue.device.registryID,
        (unsigned long long)previousSequence,
        (unsigned long long)currentSequence);
}

static void hookDrawablePresent(id self, SEL selector) {
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
    logSourceWsiDispatch(self, selector);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL))original)(self, selector);
}

static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when) {
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
    logSourceWsiDispatch(self, selector);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
}

static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration) {
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
    logSourceWsiDispatch(self, selector);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
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

static void logSourceWsiDispatch(id self, SEL selector) {
    if (!isSelectedProcess() || isInternalPresentationCall) return;
    id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
    if (!isSelectedLayer((CAMetalLayer *)drawable.layer)) return;
    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    uint64_t appFrameId = [record[@"sequence"] unsignedLongLongValue];
    uint64_t wsiPresentId = allocateWsiPresentId();
    logLine("R101_WSI_DISPATCH origin=SOURCE appFrameId=%llu wsiPresentId=%llu drawableID=%lu layer=%p texture=%p selector=%s",
        (unsigned long long)appFrameId, (unsigned long long)wsiPresentId,
        (unsigned long)drawable.drawableID, (__bridge void *)drawable.layer,
        (__bridge void *)drawable.texture, sel_getName(selector));
    observeDisplayedDrawable(drawable, "SOURCE", wsiPresentId, appFrameId, 0);
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
        sequence >= 3 && sequence <= kCaptureLimit) {
        pthread_mutex_lock(&stateLock);
        if (sequence > lastCompletedPresentationSequence &&
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
            snprintf(capturePath, sizeof(capturePath),
                "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r101-internal-presentation/source-sequence-%llu.raw",
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
    if (encoder && isSelectedProcess()) inspectRenderPass((id<MTLCommandBuffer>)self, descriptor);
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
    for (int i = 0; i < count; ++i) {
        Class cls = classes[i];
        if (commandBufferProtocol && class_conformsToProtocol(cls, commandBufferProtocol))
            installHook(cls, renderEncoderSelector, (IMP)hookRenderCommandEncoder);
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
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=public-api-drawable-command-correlation DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s capture=%s present=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R101_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R101_CAPTURE") ?: "0",
        getenv("FGMETAL_R101_PRESENT") ?: "0",
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
    pthread_mutex_unlock(&stateLock);
    logLine("R101_RUN_SUMMARY attemptedSourcePairs=%llu successfulInternalPresentations=%llu internalPresentationsDisplayed=%llu fallbackMisses=%llu duplicateClaims=%llu stalePairRejections=%llu",
        (unsigned long long)attempts, (unsigned long long)successes,
        (unsigned long long)displayed,
        (unsigned long long)fallbacks, (unsigned long long)duplicates,
        (unsigned long long)stale);
}
