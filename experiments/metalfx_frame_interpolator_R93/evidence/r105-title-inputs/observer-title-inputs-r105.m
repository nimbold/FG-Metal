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
static const NSUInteger kCaptureLimit = 120;
static NSUInteger captureAttempts;
static NSUInteger captureSuccesses;
static dispatch_queue_t captureProcessingQueue;
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
static pthread_cond_t presentationReadyCondition = PTHREAD_COND_INITIALIZER;
static id<MTLTexture> pendingInternalPresentationTexture;
static uint64_t pendingInternalPresentId;
static uint64_t pendingInternalPreviousSequence;
static uint64_t pendingInternalCurrentSequence;
static BOOL internalPresentationAttempted;
static BOOL internalPresentationCompleted;
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
static const NSUInteger kR105InventoryFrameStride = 3;
static const NSUInteger kR105DepthGridWidth = 128;
static const NSUInteger kR105DepthGridHeight = 128;
static const NSUInteger kR105MotionGridWidth = 128;
static const NSUInteger kR105MotionGridHeight = 128;
static const NSUInteger kR105MaxMotionSamples = 8;
static const NSUInteger kR105MaxDepthSamples = 16;
static pthread_mutex_t r105DepthLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t r105MotionLock = PTHREAD_MUTEX_INITIALIZER;
static const char *const kHarnessExecutable =
    "/Users/nima/Games/Elden Ring/Game/eldenring.exe";
static const char *const kR105InventoryTriggerPath =
    "/tmp/fgmetal-r105-title-inputs/start-world-inventory";

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

static void initLog(void) {
    (void)mkdir("/tmp/fgmetal-r105-title-inputs", 0700);
    char path[256];
    snprintf(path, sizeof(path), "/tmp/fgmetal-r105-title-inputs/observer.%d.log", getpid());
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
    logLine("R105_INVENTORY_TRIGGER=ARMED startSequence=%llu frameStride=%lu boundedFrames=%lu trigger=one-shot-file",
        (unsigned long long)sequence, (unsigned long)kR105InventoryFrameStride,
        (unsigned long)kR105MaxFrames);
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
    r105MaybeStartInventory(sequence);
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

    if (selected && getenv("FGMETAL_R105_CAPTURE") &&
        strcmp(getenv("FGMETAL_R105_CAPTURE"), "1") == 0) {
        r105WarmDepthPipeline(layer.device);
    }

    if (selected && getenv("FGMETAL_R105_FRAMEBUFFER_ONLY") &&
        strcmp(getenv("FGMETAL_R105_FRAMEBUFFER_ONLY"), "1") == 0) {
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
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL))original)(self, selector);
}

static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when) {
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
}

static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration) {
    if (isSelectedProcess()) tryPresentInternalBeforeSource(self);
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
    const char *presentFlag = getenv("FGMETAL_R100_PRESENT");
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

    if (previousCaptureColor && previousCaptureSequence + 1 == sequence) {
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
                    uint64_t meaningfulPixels = 0;
                    for (NSUInteger i = 0; i < pixelCount; ++i) {
                        double luminance = 0.0;
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
                    logLine("REAL_METALFX_OUTPUT internalPresentId=%llu sourceSequence=%llu->%llu outputTexture=%p readbackTexture=%p readbackStatus=%lu privateOutput=%d readable=%d meaningfulPixels=%llu maePrevious=%.8f maeCurrent=%.8f distinctPrevious=%d distinctCurrent=%d",
                        (unsigned long long)internalFrame.internalPresentId,
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (__bridge void *)internalFrame.texture,
                        (__bridge void *)outputReadback,
                        (unsigned long)readbackCommandBuffer.status,
                        internalFrame.texture.storageMode == MTLStorageModePrivate,
                        outputReadable, (unsigned long long)meaningfulPixels,
                        previousError, currentError, distinctPrevious, distinctCurrent);
                    if (internalFrame.internalPresentId > 0 && outputReadable &&
                        distinctPrevious && distinctCurrent &&
                        internalFrame.texture.storageMode == MTLStorageModePrivate) {
                        logLine("REAL_METALFX_INTERNAL_TEXTURE=VALIDATED internalPresentId=%llu outputTexture=%p readbackTexture=%p sourceSequence=%llu->%llu maePrevious=%.8f maeCurrent=%.8f",
                            (unsigned long long)internalFrame.internalPresentId,
                            (__bridge void *)internalFrame.texture,
                            (__bridge void *)outputReadback,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            previousError, currentError);
                        pthread_mutex_lock(&stateLock);
                        if (internalPresentationPipeline &&
                            !internalPresentationCompleted &&
                            !pendingInternalPresentationTexture) {
                            pendingInternalPresentationTexture = outputReadback;
                            pendingInternalPresentId = internalFrame.internalPresentId;
                            pendingInternalPreviousSequence = previousCaptureSequence;
                            pendingInternalCurrentSequence = sequence;
                            pthread_cond_broadcast(&presentationReadyCondition);
                        }
                        pthread_mutex_unlock(&stateLock);
                    }
                } else {
                    logLine("REAL_METALFX_OUTPUT_READBACK=FAILED internalPresentId=%llu status=%lu error=%s",
                        (unsigned long long)internalFrame.internalPresentId,
                        (unsigned long)readbackCommandBuffer.status,
                        readbackCommandBuffer.error.localizedDescription.UTF8String ?: "none");
                }
            }
        }
    }

    previousCaptureColor = color;
    previousCaptureDepth = depth;
    previousCaptureMotion = motion;
    previousCaptureFloatPixels = currentFloatPixels;
    previousMainShape = mainShape;
    previousMarkerShape = marker;
    previousCaptureSequence = sequence;
}

static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<CAMetalDrawable> drawable,
                                uint64_t sourceSequence,
                                id<MTLCommandBuffer> producerCommandBuffer) {
    const char *captureFlag = getenv("FGMETAL_R105_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0) return;
    // Keep a short sequence for static and moving-scene diagnostics. Elden
    // Ring's startup frames can be black, so begin after the first two seconds.
    if (sourceSequence < kR105TitleSampleStartSequence) return;

    pthread_mutex_lock(&stateLock);
    if (captureAttempts >= kCaptureLimit || captureSuccesses >= 8) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    ++captureAttempts;
    pthread_mutex_unlock(&stateLock);

    id<MTLTexture> source = drawable.texture;
    id<MTLDevice> device = source.device;
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
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> captureTexture = [device newTextureWithDescriptor:descriptor];
    id<MTLCommandBuffer> copyCommandBuffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = copyCommandBuffer
        ? [copyCommandBuffer blitCommandEncoder] : nil;
    if (!captureTexture || !copyCommandBuffer || !blit) {
        logLine("ELDENRING_SOURCE_CAPTURE_ALLOCATION=FAILED seq=%llu texture=%p commandBuffer=%p encoder=%p",
            (unsigned long long)sourceSequence, (__bridge void *)captureTexture,
            (__bridge void *)copyCommandBuffer, (__bridge void *)blit);
        return;
    }
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0, 0, 0)
              sourceSize:MTLSizeMake(width, height, 1)
               toTexture:captureTexture destinationSlice:0 destinationLevel:0
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
            size_t bytesPerRow = (size_t)width * 4;
            NSMutableData *readback = [NSMutableData dataWithLength:
                bytesPerRow * (size_t)height];
            [captureTexture getBytes:readback.mutableBytes bytesPerRow:bytesPerRow
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
                "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe/source-sequence-%llu.raw",
                (unsigned long long)sourceSequence);
            (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe", 0700);
            FILE *captureFile = verified ? fopen(capturePath, "wb") : NULL;
            BOOL saved = captureFile &&
                fwrite(readback.bytes, 1, readback.length, captureFile) == readback.length;
            if (captureFile) fclose(captureFile);
            logLine("ELDENRING_SOURCE_READBACK seq=%llu drawableID=%lu producerCommandBuffer=%p producerStatus=%lu copyCommandBuffer=%p copyStatus=%lu sourceTexture=%p captureTexture=%p device=%p registryID=%llu size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu lumaRange=%u-%u sampleHash=%llu verified=%d saved=%d path=%s",
                (unsigned long long)sourceSequence, (unsigned long)drawable.drawableID,
                (__bridge void *)producerCommandBuffer,
                (unsigned long)producerCommandBuffer.status,
                (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                (__bridge void *)source, (__bridge void *)captureTexture,
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
                logLine("ELDENRING_REAL_D3DMETAL_SOURCE_CAPTURE=VALIDATED seq=%llu drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p captureTexture=%p size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu successfulCaptures=%lu",
                    (unsigned long long)sourceSequence,
                    (unsigned long)drawable.drawableID,
                    (__bridge void *)producerCommandBuffer,
                    (__bridge void *)completedCopy,
                    (__bridge void *)captureTexture, (unsigned long)width,
                    (unsigned long)height, (unsigned long)source.pixelFormat,
                    (unsigned long long)meaningfulSamples,
                    (unsigned long)successCount);
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
    logLine("R105_RENDER_PASS sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p renderCommandBuffer=%p queue=%p encoder=%p passId=%llu relation=%s windowOrdinal=%lu timeNs=%llu renderTargetArrayLength=%lu attachments=%lu",
        (unsigned long long)sourceSequence, (unsigned long long)drawableID,
        producerCommandBuffer, [pass[@"commandBufferIdentity"] pointerValue],
        [pass[@"queueIdentity"] pointerValue], [pass[@"encoderIdentity"] pointerValue],
        (unsigned long long)[pass[@"passId"] unsignedLongLongValue], relation,
        (unsigned long)windowOrdinal,
        (unsigned long long)[pass[@"timeNs"] unsignedLongLongValue],
        (unsigned long)[pass[@"renderTargetArrayLength"] unsignedLongValue],
        (unsigned long)attachments.count);
    for (NSDictionary *attachment in attachments) {
        logLine("R105_RENDER_ATTACHMENT sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p renderCommandBuffer=%p encoder=%p passId=%llu relation=%s role=%s attachmentIndex=%lu texture=%p dimensions=%lux%lux%lu pixelFormat=%lu usage=%lu storageMode=%lu textureType=%lu sampleCount=%lu arrayLength=%lu mipLevels=%lu mipLevel=%lu slice=%lu depthPlane=%lu loadAction=%lu storeAction=%lu storeActionOptions=%lu resolveTexture=%p resolveLevel=%lu resolveSlice=%lu resolveDepthPlane=%lu",
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
    MTLRenderPassColorAttachmentDescriptorArray *colors = descriptor.colorAttachments;
    for (NSUInteger index = 0; index < 8; ++index) {
        NSDictionary *item = r105AttachmentRecord(@"color", index, colors[index]);
        if (item) [attachments addObject:item];
    }
    NSDictionary *depth = r105AttachmentRecord(@"depth", 0, descriptor.depthAttachment);
    NSDictionary *stencil = r105AttachmentRecord(@"stencil", 0, descriptor.stencilAttachment);
    if (depth) [attachments addObject:depth];
    if (stencil) [attachments addObject:stencil];

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
    NSValue *encoderKey = [NSValue valueWithPointer:(__bridge const void *)encoder];
    NSDictionary *pass = r105MakePassRecord(commandBuffer, encoder, descriptor);
    NSDictionary *producer = nil;
    BOOL accepted = NO;
    BOOL selectedFrame = r105SequenceSelectedForInventory(
        r105CurrentDrawableSequence());
    pthread_mutex_lock(&stateLock);
    if (!r105SeenEncoders) r105SeenEncoders = [NSMutableSet set];
    if (!r105PassesByCommandBuffer) r105PassesByCommandBuffer = [NSMutableDictionary dictionary];
    if (!r105ProducersByCommandBuffer) r105ProducersByCommandBuffer = [NSMutableDictionary dictionary];
    if (![r105SeenEncoders containsObject:encoderKey] &&
        r105PassCount < kR105MaxPasses &&
        (selectedFrame || r105ProducersByCommandBuffer[commandKey])) {
        [r105SeenEncoders addObject:encoderKey];
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
    if (!accepted) return;
    if (producer) {
        r105LogPass(pass,
            [producer[@"sourceSequence"] unsignedLongLongValue],
            [producer[@"drawableID"] unsignedLongLongValue],
            "same-producer-command-buffer", 0,
            [commandKey pointerValue]);
    }
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
    NSArray<NSDictionary *> *priorWindows = nil;
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
    NSMutableArray<NSDictionary *> *prior = [NSMutableArray array];
    if (!r105RecentCommittedBuffers) r105RecentCommittedBuffers = [NSMutableArray array];
    void *queueIdentity = (__bridge void *)commandBuffer.commandQueue;
    for (NSDictionary *buffer in [r105RecentCommittedBuffers reverseObjectEnumerator]) {
        uint64_t committedAt = [buffer[@"commitTimeNs"] unsignedLongLongValue];
        if ([buffer[@"queueIdentity"] pointerValue] != queueIdentity ||
            committedAt > observedAt || observedAt - committedAt > kR105CommandWindowNs) continue;
        if ([buffer[@"passes"] count] == 0) continue;
        [prior addObject:buffer];
        if (prior.count >= kR105MaxWindowBuffers) break;
    }
    priorWindows = [prior copy];
    pthread_mutex_unlock(&stateLock);

    logLine("R105_SOURCE_PRODUCER sourceSequence=%llu frameOrdinal=%lu drawableID=%llu drawableTexture=%p producerCommandBuffer=%p producerCommandBufferClass=%s queue=%p device=%p registryID=%llu colorAttachmentIndex=%lu observedAtNs=%llu framebufferOnly=%d drawableSize=%lux%lu drawablePixelFormat=%lu drawableUsage=%lu boundedFrameLimit=%lu",
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
    for (NSUInteger i = 0; i < priorWindows.count; ++i) {
        NSDictionary *buffer = priorWindows[i];
        NSArray<NSDictionary *> *passes = buffer[@"passes"];
        void *priorCommandBuffer = [buffer[@"commandBufferIdentity"] pointerValue];
        for (NSUInteger j = 0; j < passes.count; ++j)
            r105LogPass(passes[j], sequence, drawableID,
                "preceding-same-queue-window", i + 1, priorCommandBuffer);
    }
    logLine("R105_FRAME_WINDOW sourceSequence=%llu producerCommandBuffer=%p producerQueue=%p priorCommandBuffers=%lu windowNs=%llu sameCommandBufferPasses=%lu frameOrdinal=%lu",
        (unsigned long long)sequence, (__bridge void *)commandBuffer,
        (__bridge void *)commandBuffer.commandQueue, (unsigned long)priorWindows.count,
        (unsigned long long)kR105CommandWindowNs,
        (unsigned long)sameCommandBufferPasses.count, (unsigned long)frameOrdinal);
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        logLine("R105_PRODUCER_COMPLETION sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p queue=%p status=%lu error=%s completionTimeNs=%llu",
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
        logLine("R105_DEPTH_PIPELINE=%s device=%p registryID=%llu error=%s compileQueue=background",
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
        logLine("R105_MOTION_PIPELINE=%s device=%p registryID=%llu error=%s compileQueue=background",
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
        logLine("R105_DEPTH_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p lineage=same-producer-command-buffer reason=no-stored-readable-depth-attachment sameCommandBufferPasses=%lu",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)commandBuffer, (unsigned long)passes.count);
        return;
    }

    id<MTLTexture> sourceTexture = candidate[@"texture"];
    NSUInteger mipLevel = [candidate[@"mipLevel"] unsignedLongValue];
    NSUInteger slice = [candidate[@"slice"] unsignedLongValue];
    NSUInteger pixelFormat = [candidate[@"pixelFormat"] unsignedLongValue];
    if (mipLevel >= sourceTexture.mipmapLevelCount || slice >= sourceTexture.arrayLength) {
        logLine("R105_DEPTH_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=invalid-subresource mipLevel=%lu mipLevels=%lu slice=%lu arrayLength=%lu",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture, (unsigned long)mipLevel,
            (unsigned long)sourceTexture.mipmapLevelCount, (unsigned long)slice,
            (unsigned long)sourceTexture.arrayLength);
        return;
    }
    logLine("R105_DEPTH_GPU_SAMPLE_CANDIDATE sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p dimensions=%lux%lu sourcePixelFormat=%lu sampledPixelFormat=%lu usage=%lu storageMode=%lu storeAction=%lu lineage=same-producer-command-buffer access=direct-depth2d",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)sourceTexture,
        (unsigned long)[candidate[@"width"] unsignedLongValue],
        (unsigned long)[candidate[@"height"] unsignedLongValue],
        (unsigned long)pixelFormat, (unsigned long)pixelFormat,
        (unsigned long)[candidate[@"usage"] unsignedLongValue],
        (unsigned long)sourceTexture.storageMode,
        (unsigned long)[candidate[@"storeAction"] unsignedLongValue]);

    NSError *pipelineError = nil;
    id<MTLComputePipelineState> pipeline = r105DepthPipeline(
        commandBuffer.commandQueue.device, &pipelineError);
    if (!pipeline) {
        logLine("R105_DEPTH_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=diagnostic-pipeline-not-ready error=%s",
            (unsigned long long)sequence, (__bridge void *)commandBuffer,
            (__bridge void *)sourceTexture,
            pipelineError.localizedDescription.UTF8String ?: "unknown");
        return;
    }

    NSUInteger sampleCount = kR105DepthGridWidth * kR105DepthGridHeight;
    logLine("R105_DEPTH_GPU_SAMPLE_ENCODE_BEGIN sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p diagnosticPipeline=%p grid=%lux%lu",
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
        logLine("R105_DEPTH_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p depthTexture=%p reason=diagnostic-resource-allocation-failed buffer=%p encoder=%p",
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
    [encoder setTexture:sourceTexture atIndex:0];
    [encoder setBuffer:sampleBuffer offset:0 atIndex:0];
    [encoder setBytes:gridSize length:sizeof(gridSize) atIndex:1];
    [encoder dispatchThreads:MTLSizeMake(kR105DepthGridWidth,
                                        kR105DepthGridHeight, 1)
        threadsPerThreadgroup:MTLSizeMake(8, 8, 1)];
    [encoder endEncoding];
    pthread_mutex_lock(&stateLock);
    ++r105DepthSampleCount;
    pthread_mutex_unlock(&stateLock);

    logLine("R105_DEPTH_GPU_SAMPLE_ENCODED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p sampleBuffer=%p dimensions=%lux%lu sourcePixelFormat=%lu sampledPixelFormat=%lu usage=%lu storageMode=%lu storeAction=%lu grid=%lux%lu lineage=same-producer-command-buffer position=after-title-render-before-commit",
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
            logLine("R105_DEPTH_GPU_SAMPLE=FAILED sourceSequence=%llu producerCommandBuffer=%p status=%lu error=%s",
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
            "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe/depth-sequence-%llu.raw",
            (unsigned long long)sequence);
        (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe", 0700);
        FILE *file = fopen(samplePath, "wb");
        BOOL saved = file && fwrite(values, sizeof(float), sampleCount, file) == sampleCount;
        if (file) fclose(file);
        logLine("R105_DEPTH_GPU_SAMPLE_READBACK sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p depthTexture=%p diagnosticBuffer=%p sampleWidth=%lu sampleHeight=%lu min=%.9g max=%.9g mean=%.9g finiteSamples=%llu invalidSamples=%llu zeroSamples=%llu oneSamples=%llu saved=%d path=%s lineage=same-producer-command-buffer",
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
    logLine("R105_MOTION_GPU_SAMPLE_CANDIDATE sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p passId=%llu encoder=%p attachmentIndex=%lu texture=%p dimensions=%lux%lu sourcePixelFormat=%lu usage=%lu storageMode=%lu storeAction=%lu lineage=same-producer-command-buffer access=direct-rg16float-sample componentOrder=R,G",
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
        logLine("R105_MOTION_GPU_SAMPLE=NOT_ATTEMPTED sourceSequence=%llu producerCommandBuffer=%p texture=%p reason=diagnostic-pipeline-not-ready error=%s",
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
        logLine("R105_MOTION_GPU_SAMPLE=INACCESSIBLE sourceSequence=%llu producerCommandBuffer=%p texture=%p reason=diagnostic-resource-allocation-failed buffer=%p encoder=%p",
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
    logLine("R105_MOTION_GPU_SAMPLE_ENCODED sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p texture=%p diagnosticBuffer=%p sourceDimensions=%lux%lu grid=%lux%lu lineage=same-producer-command-buffer position=after-title-render-before-commit output=raw-float32-RG",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)sourceTexture,
        (__bridge void *)sampleBuffer, (unsigned long)width, (unsigned long)height,
        (unsigned long)kR105MotionGridWidth, (unsigned long)kR105MotionGridHeight);

    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        if (completed.status != MTLCommandBufferStatusCompleted || completed.error) {
            logLine("R105_MOTION_GPU_SAMPLE=FAILED sourceSequence=%llu producerCommandBuffer=%p status=%lu error=%s",
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
            "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe/motion-sequence-%llu.rg32f",
            (unsigned long long)sequence);
        (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r105-title-inputs/run-04-depth-probe", 0700);
        FILE *file = fopen(samplePath, "wb");
        BOOL saved = file && fwrite(values, sizeof(float) * 2, gridCount, file) == gridCount;
        if (file) fclose(file);
        double meanR = finiteCount ? sumR / (double)finiteCount : 0.0;
        double meanG = finiteCount ? sumG / (double)finiteCount : 0.0;
        logLine("R105_MOTION_GPU_SAMPLE_READBACK sourceSequence=%llu drawableID=%llu producerCommandBuffer=%p texture=%p sampleWidth=%lu sampleHeight=%lu componentOrder=R,G minR=%.9g maxR=%.9g meanR=%.9g minG=%.9g maxG=%.9g meanG=%.9g finiteSamples=%llu invalidSamples=%llu nearZeroPairs=%llu saved=%d path=%s lineage=same-producer-command-buffer",
            (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)sourceTexture,
            (unsigned long)kR105MotionGridWidth, (unsigned long)kR105MotionGridHeight,
            minR, maxR, meanR, minG, maxG, meanG,
            (unsigned long long)finiteCount, (unsigned long long)invalidCount,
            (unsigned long long)nearZeroCount, saved, saved ? samplePath : "<write-failed>");
    }];
}

static void r105RecordCommittedCommandBuffer(id<MTLCommandBuffer> commandBuffer) {
    NSValue *commandKey = r105CommandBufferKey(commandBuffer);
    NSValue *queueKey = [NSValue valueWithPointer:(__bridge const void *)commandBuffer.commandQueue];
    NSArray<NSDictionary *> *passes = nil;
    NSDictionary *producer = nil;
    uint64_t commitTime = r105NowNs();
    pthread_mutex_lock(&stateLock);
    if (!r105SeenCommits) r105SeenCommits = [NSMutableSet set];
    if ([r105SeenCommits containsObject:commandKey]) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    [r105SeenCommits addObject:commandKey];
    passes = [r105PassesByCommandBuffer[commandKey] copy] ?: @[];
    producer = r105ProducersByCommandBuffer[commandKey];
    if (passes.count > 0) {
        if (!r105RecentCommittedBuffers) r105RecentCommittedBuffers = [NSMutableArray array];
        [r105RecentCommittedBuffers addObject:@{
            @"commandBufferIdentity": commandKey,
            @"queueIdentity": queueKey,
            @"commitTimeNs": @(commitTime),
            @"passes": passes,
        }];
        while (r105RecentCommittedBuffers.count > kR105MaxCommandBuffers)
            [r105RecentCommittedBuffers removeObjectAtIndex:0];
    }
    [r105PassesByCommandBuffer removeObjectForKey:commandKey];
    pthread_mutex_unlock(&stateLock);
    if (passes.count == 0) return;
    logLine("R105_COMMAND_BUFFER_COMMIT commandBuffer=%p queue=%p status=%lu commitTimeNs=%llu passCount=%lu producerSourceSequence=%llu boundedPassCount=%lu",
        (__bridge void *)commandBuffer, (__bridge void *)commandBuffer.commandQueue,
        (unsigned long)commandBuffer.status, (unsigned long long)commitTime,
        (unsigned long)passes.count,
        (unsigned long long)[producer[@"sourceSequence"] unsignedLongLongValue],
        (unsigned long)kR105MaxPasses);
}

static void hookCommandBufferCommit(id self, SEL selector) {
    if (isSelectedProcess()) {
        r105EncodeDepthDiagnostic((id<MTLCommandBuffer>)self);
    }
    IMP original = originalFor(self, selector);
    if (original) ((void (*)(id, SEL))original)(self, selector);
    if (isSelectedProcess()) r105RecordCommittedCommandBuffer((id<MTLCommandBuffer>)self);
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
    captureProcessingQueue = dispatch_queue_create(
        "com.fgmetal.r100.capture-processing", DISPATCH_QUEUE_SERIAL);
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=R105-title-render-pass-inventory DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s titleCapture=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R105_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R105_CAPTURE") ?: "0",
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
