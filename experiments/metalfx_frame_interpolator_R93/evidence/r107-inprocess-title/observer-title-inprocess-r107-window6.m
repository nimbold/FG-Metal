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
static id<MTLTexture> previousCaptureSource;
static NSUInteger r107FrameWidth;
static NSUInteger r107FrameHeight;
static id<MTLCommandQueue> r107ComputeQueue;
static id<MTLComputePipelineState> r107BlockMatchPipeline;
static id<MTLComputePipelineState> r107PropagatePipeline;
static id<MTLComputePipelineState> r107UpsamplePipeline;
static id<MTLComputePipelineState> r107PreparePipeline;
static id<MTLComputePipelineState> r107ClearMotionPipeline;
static id<MTLDevice> r107PipelineDevice;
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
    "/tmp/fgmetal-r107-inprocess-title/start-world-inventory-disabled";
static const char *const kR107CaptureTriggerPath =
    "/tmp/fgmetal-r107-inprocess-title/start-title-gpu-capture";
static const char *const kR107ConditionPath =
    "/tmp/fgmetal-r107-inprocess-title/capture-condition";
static BOOL r107CaptureTriggered;
static uint64_t r107CaptureStartSequence;
static NSUInteger r107CaptureCount;
static const NSUInteger kR107CaptureLimit = 16;
static NSUInteger r107CaptureWindows;
static const NSUInteger kR107CaptureWindowsLimit = 6;

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
    (void)mkdir("/tmp/fgmetal-r107-inprocess-title", 0700);
    char path[256];
    snprintf(path, sizeof(path), "/tmp/fgmetal-r107-inprocess-title/observer.%d.log", getpid());
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
    logLine("R107_INVENTORY_TRIGGER=ARMED startSequence=%llu frameStride=%lu boundedFrames=%lu trigger=one-shot-file",
        (unsigned long long)sequence, (unsigned long)kR105InventoryFrameStride,
        (unsigned long)kR105MaxFrames);
}

static void r107MaybeStartCapture(uint64_t sequence) {
    if (sequence < kR105TitleSampleStartSequence) return;
    pthread_mutex_lock(&stateLock);
    BOOL shouldCheck = !r107CaptureTriggered &&
        r107CaptureWindows < kR107CaptureWindowsLimit;
    pthread_mutex_unlock(&stateLock);
    if (!shouldCheck || access(kR107CaptureTriggerPath, F_OK) != 0) return;

    BOOL started = NO;
    pthread_mutex_lock(&stateLock);
    if (!r107CaptureTriggered &&
        r107CaptureWindows < kR107CaptureWindowsLimit) {
        r107CaptureTriggered = YES;
        r107CaptureStartSequence = sequence;
        r107CaptureCount = 0;
        ++r107CaptureWindows;
        started = YES;
    }
    pthread_mutex_unlock(&stateLock);
    if (!started) return;
    (void)unlink(kR107CaptureTriggerPath);
    logLine("R107_TITLE_CAPTURE=ARMED window=%lu startSequence=%llu boundedFrames=%lu maxWindows=%lu trigger=one-shot-file",
        (unsigned long)r107CaptureWindows, (unsigned long long)sequence,
        (unsigned long)kR107CaptureLimit, (unsigned long)kR107CaptureWindowsLimit);
}

static BOOL r107SequenceSelectedForCapture(uint64_t sequence) {
    pthread_mutex_lock(&stateLock);
    BOOL selected = r107CaptureTriggered &&
        sequence >= r107CaptureStartSequence &&
        r107CaptureCount < kR107CaptureLimit;
    pthread_mutex_unlock(&stateLock);
    return selected;
}

static NSString *r107CurrentCaptureCondition(void) {
    NSError *error = nil;
    NSString *label = [NSString stringWithContentsOfFile:
        [NSString stringWithUTF8String:kR107ConditionPath]
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
    // R107 capture is separate from the R105 attachment-inventory trigger.
    // Do not restart broad depth or native-motion hunting for this run.
    r107MaybeStartCapture(sequence);
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

    if (selected && getenv("FGMETAL_R107_CAPTURE") &&
        strcmp(getenv("FGMETAL_R107_CAPTURE"), "1") == 0) {
        r105WarmDepthPipeline(layer.device);
    }

    if (selected && getenv("FGMETAL_R107_FRAMEBUFFER_ONLY") &&
        strcmp(getenv("FGMETAL_R107_FRAMEBUFFER_ONLY"), "1") == 0) {
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

static BOOL initializeR107Pipelines(id<MTLDevice> device) {
    if (r107PipelineDevice == device && r107ComputeQueue &&
        r107BlockMatchPipeline && r107PropagatePipeline &&
        r107UpsamplePipeline && r107PreparePipeline && r107ClearMotionPipeline) return YES;

    NSString *source = @"#include <metal_stdlib>\n"
        "using namespace metal;\n"
        "inline float r107PatchCost(texture2d<float, access::read> previous, texture2d<float, access::read> current, int2 center, float3 targetColor, int2 shift, int sampleStep) {\n"
        "  float sum = 0.0f; float weightSum = 0.0f;\n"
        "  for (int y=-96; y<=96; y+=sampleStep) for (int x=-96; x<=96; x+=sampleStep) {\n"
        "    int2 p=center+int2(x,y);\n"
        "    float3 a=current.read(uint2(clamp(p,int2(0),int2(int(current.get_width())-1,int(current.get_height())-1)))).rgb;\n"
        "    float3 b=previous.read(uint2(clamp(p+shift,int2(0),int2(int(previous.get_width())-1,int(previous.get_height())-1)))).rgb;\n"
        "    float targetY=dot(targetColor,float3(0.2126f,0.7152f,0.0722f));\n"
        "    float sampleY=dot(a,float3(0.2126f,0.7152f,0.0722f));\n"
        "    float colorDifference=length(a-targetColor);\n"
        "    if(targetY>0.15f && sampleY>0.15f && colorDifference>0.15f) continue;\n"
        "    float weight=(targetY>0.15f && sampleY>0.15f && colorDifference<=0.15f)?4.0f:1.0f;\n"
        "    sum+=weight*abs(dot(a,float3(0.2126f,0.7152f,0.0722f))-dot(b,float3(0.2126f,0.7152f,0.0722f))); weightSum+=weight;\n"
        "  }\n"
        "  return sum/max(weightSum,1e-5f);\n"
        "}\n"
        "kernel void r107BlockMatch(texture2d<float, access::read> previous [[texture(0)]], texture2d<float, access::read> current [[texture(1)]], texture2d<float, access::write> grid [[texture(2)]], uint2 gid [[thread_position_in_grid]]) {\n"
        "  if(gid.x>=grid.get_width()||gid.y>=grid.get_height()) return;\n"
        "  int2 center=int2(float(gid.x)*float(current.get_width())/float(grid.get_width()),float(gid.y)*float(current.get_height())/float(grid.get_height()));\n"
        "  float3 target=current.read(uint2(center)).rgb; int bestX=0; float bestXCost=INFINITY;\n"
        "  for(int dx=-20;dx<=20;dx+=2){float c=r107PatchCost(previous,current,center,target,int2(dx,0),8)+0.000001f*float(abs(dx));if(c<bestXCost){bestXCost=c;bestX=dx;}}\n"
        "  bestXCost=INFINITY; for(int dx=max(-20,bestX-4);dx<=min(20,bestX+4);++dx){float c=r107PatchCost(previous,current,center,target,int2(dx,0),4)+0.000001f*float(abs(dx));if(c<bestXCost){bestXCost=c;bestX=dx;}}\n"
        "  int bestY=0; float bestYCost=INFINITY; for(int dy=-20;dy<=20;dy+=2){float c=r107PatchCost(previous,current,center,target,int2(0,dy),8)+0.000001f*float(abs(dy));if(c<bestYCost){bestYCost=c;bestY=dy;}}\n"
        "  bestYCost=INFINITY; for(int dy=max(-20,bestY-4);dy<=min(20,bestY+4);++dy){float c=r107PatchCost(previous,current,center,target,int2(0,dy),4)+0.000001f*float(abs(dy));if(c<bestYCost){bestYCost=c;bestY=dy;}}\n"
        "  float cx=r107PatchCost(previous,current,center,target,int2(bestX,0),4); float cl=r107PatchCost(previous,current,center,target,int2(bestX-1,0),4); float cr=r107PatchCost(previous,current,center,target,int2(bestX+1,0),4);\n"
        "  float cy=r107PatchCost(previous,current,center,target,int2(0,bestY),4); float cu=r107PatchCost(previous,current,center,target,int2(0,bestY-1),4); float cd=r107PatchCost(previous,current,center,target,int2(0,bestY+1),4);\n"
        "  float rx=abs(cl-2.0f*cx+cr)>1e-6f?clamp(0.5f*(cl-cr)/(cl-2.0f*cx+cr),-0.5f,0.5f):0.0f; float ry=abs(cu-2.0f*cy+cd)>1e-6f?clamp(0.5f*(cu-cd)/(cu-2.0f*cy+cd),-0.5f,0.5f):0.0f;\n"
        "  float farX=INFINITY,farY=INFINITY; for(int d=-20;d<=20;d+=2){if(abs(d-bestX)>10){float c=r107PatchCost(previous,current,center,target,int2(d,0),8);if(c<farX)farX=c;}if(abs(d-bestY)>10){float c=r107PatchCost(previous,current,center,target,int2(0,d),8);if(c<farY)farY=c;}}\n"
        "  float bx=r107PatchCost(previous,current,center,target,int2(bestX,0),8); float by=r107PatchCost(previous,current,center,target,int2(0,bestY),8);\n"
        "  float qx=isfinite(farX)&&farX>1e-5f?clamp((farX-bx)/farX,0.0f,1.0f):0.0f; float qy=isfinite(farY)&&farY>1e-5f?clamp((farY-by)/farY,0.0f,1.0f):0.0f;\n"
        "  grid.write(float4(qx>0.05f?float(bestX)+rx:0.0f,qy>0.05f?float(bestY)+ry:0.0f,max(qx,qy),min(bx,by)),gid);\n"
        "}\n"
        "kernel void r107Propagate(texture2d<float, access::read> current [[texture(0)]],texture2d<float, access::read> input [[texture(1)]],texture2d<float, access::write> output [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {\n"
        "  if(gid.x>=input.get_width()||gid.y>=input.get_height())return; float4 base=input.read(gid);if(base.z>=0.05f){output.write(base,gid);return;}\n"
        "  int2 center=int2(float(gid.x)*float(current.get_width())/float(input.get_width()),float(gid.y)*float(current.get_height())/float(input.get_height()));float3 c=current.read(uint2(center)).rgb;if(dot(c,float3(0.2126f,0.7152f,0.0722f))<0.05f){output.write(float4(0.0f),gid);return;}\n"
        "  float scoreBest=0.0f;float4 best=base;for(int y=-3;y<=3;++y)for(int x=-3;x<=3;++x){if(x==0&&y==0)continue;int2 q=int2(gid)+int2(x,y);if(q.x<0||q.y<0||q.x>=int(input.get_width())||q.y>=int(input.get_height()))continue;float4 f=input.read(uint2(q));if(f.z<=0.05f)continue;int2 qc=int2(float(q.x)*float(current.get_width())/float(input.get_width()),float(q.y)*float(current.get_height())/float(input.get_height()));float3 color=current.read(uint2(qc)).rgb;if(length(c-color)>0.12f)continue;float score=f.z/(1.0f+float(abs(x)+abs(y)));if(score>scoreBest){scoreBest=score;best=f;}}\n"
        "  if(scoreBest>0.0f){best.z*=0.75f;output.write(best,gid);}else output.write(float4(0.0f),gid);\n"
        "}\n"
        "kernel void r107Upsample(texture2d<float, access::read> grid [[texture(0)]],texture2d<half, access::write> motion [[texture(1)]],uint2 gid [[thread_position_in_grid]]) {\n"
        "  if(gid.x>=motion.get_width()||gid.y>=motion.get_height())return;uint2 p=uint2(min(grid.get_width()-1,uint((float(gid.x)+float(motion.get_width())/float(grid.get_width())*0.5f)*float(grid.get_width())/float(motion.get_width()))),min(grid.get_height()-1,uint((float(gid.y)+float(motion.get_height())/float(grid.get_height())*0.5f)*float(grid.get_height())/float(motion.get_height()))));float2 v=grid.read(p).xy;motion.write(half4(half(v.x),half(v.y),half(0.0f),half(0.0f)),gid);\n"
        "}\n"
        "kernel void r107Prepare(texture2d<float, access::read> source [[texture(0)]],texture2d<half, access::write> color [[texture(1)]],texture2d<float, access::write> depth [[texture(2)]],uint2 gid [[thread_position_in_grid]]) {\n"
        "  if(gid.x>=source.get_width()||gid.y>=source.get_height())return;float4 c=source.read(gid);color.write(half4(half3(c.rgb),half(c.a)),gid);depth.write(float4(0.25f,0.0f,0.0f,0.0f),gid);\n"
        "}\n"
        "kernel void r107ClearMotion(texture2d<half, access::write> motion [[texture(0)]],uint2 gid [[thread_position_in_grid]]) { if(gid.x<motion.get_width()&&gid.y<motion.get_height())motion.write(half4(0.0h),gid); }\n";

    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    r107ComputeQueue = [device newCommandQueue];
    r107PipelineDevice = device;
    r107BlockMatchPipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107BlockMatch"] error:&error];
    r107PropagatePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107Propagate"] error:&error];
    r107UpsamplePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107Upsample"] error:&error];
    r107PreparePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107Prepare"] error:&error];
    r107ClearMotionPipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r107ClearMotion"] error:&error];
    BOOL ready = library && r107ComputeQueue && r107BlockMatchPipeline &&
        r107PropagatePipeline && r107UpsamplePipeline && r107PreparePipeline &&
        r107ClearMotionPipeline;
    logLine("R107_GPU_PIPELINES=%s device=%p queue=%p blockMatch=%p propagate=%p upsample=%p prepare=%p clear=%p localSearchStep=4 parabolaStep=4 coarseAndConfidenceStep=8 error=%s",
        ready ? "READY" : "FAILED", (__bridge void *)device,
        (__bridge void *)r107ComputeQueue, (__bridge void *)r107BlockMatchPipeline,
        (__bridge void *)r107PropagatePipeline, (__bridge void *)r107UpsamplePipeline,
        (__bridge void *)r107PreparePipeline, (__bridge void *)r107ClearMotionPipeline,
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
    if (!r107FrameWidth || !r107FrameHeight) return;
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
    descriptor.inputWidth = r107FrameWidth;
    descriptor.inputHeight = r107FrameHeight;
    descriptor.outputWidth = r107FrameWidth;
    descriptor.outputHeight = r107FrameHeight;
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
        initWithDevice:device width:r107FrameWidth height:r107FrameHeight error:&error];
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
    logLine("R107_METALFX_PROVIDER=READY device=%p registryID=%llu width=%lu height=%lu colorUsage=%lu depthUsage=%lu motionUsage=%lu",
        (__bridge void *)device, (unsigned long long)device.registryID,
        (unsigned long)r107FrameWidth, (unsigned long)r107FrameHeight,
        (unsigned long)colorInputUsage, (unsigned long)depthInputUsage,
        (unsigned long)motionInputUsage);
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
        logLine("R107_GPU_SOURCE_PREFLIGHT=FAILED seq=%llu drawableID=%lu source=%p format=%lu textureFormat=%lu usage=%lu framebufferOnly=%d",
            (unsigned long long)sequence, (unsigned long)drawableID,
            (__bridge void *)sourceTexture, (unsigned long)sourceFormat,
            (unsigned long)sourceTexture.pixelFormat,
            (unsigned long)sourceTexture.usage, sourceTexture.framebufferOnly);
        return;
    }
    if (previousCaptureSequence && sequence != previousCaptureSequence + 1) {
        logLine("R107_SOURCE_SEQUENCE_GAP previous=%llu current=%llu resetProvider=1",
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
    if (r107FrameWidth == 0 && r107FrameHeight == 0) {
        r107FrameWidth = sourceTexture.width;
        r107FrameHeight = sourceTexture.height;
    }
    if (sourceTexture.width != r107FrameWidth ||
        sourceTexture.height != r107FrameHeight ||
        !initializeR107Pipelines(device)) {
        logLine("R107_GPU_SOURCE_PREFLIGHT=FAILED seq=%llu expectedSize=%lux%lu actualSize=%lux%lu pipelinesReady=%d",
            (unsigned long long)sequence, (unsigned long)r107FrameWidth,
            (unsigned long)r107FrameHeight, (unsigned long)sourceTexture.width,
            (unsigned long)sourceTexture.height,
            r107PipelineDevice == device && r107ComputeQueue != nil);
        return;
    }
    initializeMetalFXProvider(device);
    if (!realFrameProvider || !outputReadbackQueue) return;

    const NSUInteger width = sourceTexture.width;
    const NSUInteger height = sourceTexture.height;
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
    id<MTLTexture> propagated = makePrivateTexture(device, MTLPixelFormatRGBA32Float,
        gridWidth, gridHeight, MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite);
    if (!color || !depth || !motion || !grid || !propagated) {
        logLine("R107_GPU_INPUT_ALLOCATION=FAILED seq=%llu color=%p depth=%p motion=%p grid=%p propagated=%p",
            (unsigned long long)sequence, (__bridge void *)color,
            (__bridge void *)depth, (__bridge void *)motion,
            (__bridge void *)grid, (__bridge void *)propagated);
        return;
    }

    BOOL hasPrevious = previousCaptureSource && previousCaptureSequence + 1 == sequence;
    id<MTLCommandBuffer> commandBuffer = [r107ComputeQueue commandBuffer];
    if (!commandBuffer) {
        logLine("R107_GPU_INPUT_ENCODING=FAILED seq=%llu stage=command-buffer",
            (unsigned long long)sequence);
        return;
    }
    MTLSize imageThreads = MTLSizeMake(width, height, 1);
    MTLSize imageGroup = MTLSizeMake(16, 16, 1);
    MTLSize imageGroups = MTLSizeMake(
        (width + imageGroup.width - 1) / imageGroup.width,
        (height + imageGroup.height - 1) / imageGroup.height, 1);
    if (hasPrevious) {
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r107BlockMatchPipeline];
        [encoder setTexture:previousCaptureSource atIndex:0];
        [encoder setTexture:sourceTexture atIndex:1];
        [encoder setTexture:grid atIndex:2];
        MTLSize gridGroup = MTLSizeMake(4, 4, 1);
        MTLSize gridGroups = MTLSizeMake(
            (gridWidth + gridGroup.width - 1) / gridGroup.width,
            (gridHeight + gridGroup.height - 1) / gridGroup.height, 1);
        [encoder dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r107PropagatePipeline];
        [encoder setTexture:sourceTexture atIndex:0];
        [encoder setTexture:grid atIndex:1];
        [encoder setTexture:propagated atIndex:2];
        [encoder dispatchThreadgroups:gridGroups threadsPerThreadgroup:gridGroup];
        [encoder endEncoding];

        encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r107UpsamplePipeline];
        [encoder setTexture:propagated atIndex:0];
        [encoder setTexture:motion atIndex:1];
        [encoder dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
        [encoder endEncoding];
    } else {
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:r107ClearMotionPipeline];
        [encoder setTexture:motion atIndex:0];
        [encoder dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
        [encoder endEncoding];
    }

    id<MTLComputeCommandEncoder> prepare = [commandBuffer computeCommandEncoder];
    [prepare setComputePipelineState:r107PreparePipeline];
    [prepare setTexture:sourceTexture atIndex:0];
    [prepare setTexture:color atIndex:1];
    [prepare setTexture:depth atIndex:2];
    [prepare dispatchThreadgroups:imageGroups threadsPerThreadgroup:imageGroup];
    [prepare endEncoding];
    logLine("R107_GPU_INPUTS_ENQUEUED sourceSequence=%llu pair=%llu->%llu condition=%s drawableID=%lu source=%p sourceStorage=%lu color=%p colorFormat=%lu colorStorage=%lu depth=%p depthFormat=%lu depthStorage=%lu motion=%p motionFormat=%lu motionStorage=%lu motionSource=%s depthQualifier=CONSTANT_DEPTH_PROTOTYPE sourceFileTransport=0 motionFileTransport=0",
        (unsigned long long)sequence,
        (unsigned long long)(hasPrevious ? previousCaptureSequence : sequence),
        (unsigned long long)sequence, condition.UTF8String ?: "UNSPECIFIED",
        (unsigned long)drawableID,
        (__bridge void *)sourceTexture, (unsigned long)sourceTexture.storageMode,
        (__bridge void *)color, (unsigned long)color.pixelFormat,
        (unsigned long)color.storageMode, (__bridge void *)depth,
        (unsigned long)depth.pixelFormat, (unsigned long)depth.storageMode,
        (__bridge void *)motion, (unsigned long)motion.pixelFormat,
        (unsigned long)motion.storageMode, hasPrevious ? "R107_GPU_ESTIMATOR" : "ZERO_HISTORY_SEED");

    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    double gpuMs = commandBuffer.GPUEndTime > commandBuffer.GPUStartTime
        ? (commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1000.0 : 0.0;
    logLine("R107_GPU_INPUTS_COMPLETE seq=%llu pair=%llu->%llu status=%lu gpuMs=%.3f error=%s motionSource=%s",
        (unsigned long long)sequence,
        (unsigned long long)(hasPrevious ? previousCaptureSequence : sequence),
        (unsigned long long)sequence, (unsigned long)commandBuffer.status,
        gpuMs, commandBuffer.error.localizedDescription.UTF8String ?: "none",
        hasPrevious ? "R107_GPU_ESTIMATOR" : "ZERO_HISTORY_SEED");
    if (commandBuffer.status != MTLCommandBufferStatusCompleted || commandBuffer.error) return;

    logLine("R107_TITLE_CRITICAL_PATH_ASSERTION=PASS sourcePixelCPUReadbackForInputConstruction=0 cpuColorPixelInputBuffers=0 cpuMotionPixelInputBuffers=0 cpuDepthPixelInputBuffers=0 sourceFileTransport=0 motionFileTransport=0 inputTextures=private diagnosticsDependency=NONE sourceDiagnosticsDispatchedAfterCopy=1");
    if (hasPrevious) {
        NSError *error = nil;
        uint64_t startNs = r105NowNs();
        FGMetalInternalFrame *internalFrame =
            [realFrameProvider interpolatePreviousColor:previousCaptureColor
                currentColor:color depthTexture:depth motionTexture:motion
                deltaTime:1.0f / 60.0f error:&error];
        uint64_t elapsedNs = r105NowNs() - startNs;
        logLine("R107_METALFX_TRANSITION previousSourceSequence=%llu currentSourceSequence=%llu condition=%s result=%s internalPresentId=%llu error=%s elapsedMs=%.3f depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            condition.UTF8String ?: "UNSPECIFIED",
            internalFrame ? "INTERNAL_FRAME" : (error ? "FAILED" : "NO_OUTPUT"),
            (unsigned long long)internalFrame.internalPresentId,
            error.localizedDescription.UTF8String ?: "none", elapsedNs / 1e6);
        if (internalFrame && !error) {
            BOOL textureValid = internalFrame.internalPresentId > 0 &&
                internalFrame.texture.device == device &&
                internalFrame.texture.width == width &&
                internalFrame.texture.height == height &&
                internalFrame.texture.pixelFormat == MTLPixelFormatRGBA16Float &&
                internalFrame.texture.storageMode == MTLStorageModePrivate;
            MTLTextureDescriptor *readbackDescriptor = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA16Float
                width:width height:height mipmapped:NO];
            readbackDescriptor.storageMode = MTLStorageModeShared;
            readbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
            id<MTLTexture> readback = [device newTextureWithDescriptor:readbackDescriptor];
            MTLTextureDescriptor *motionReadbackDescriptor = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatRG16Float
                width:width height:height mipmapped:NO];
            motionReadbackDescriptor.storageMode = MTLStorageModeShared;
            motionReadbackDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
            id<MTLTexture> motionReadback = [device newTextureWithDescriptor:
                motionReadbackDescriptor];
            id<MTLCommandBuffer> diagnosticCB = [outputReadbackQueue commandBuffer];
            id<MTLBlitCommandEncoder> blit = diagnosticCB ?
                [diagnosticCB blitCommandEncoder] : nil;
            if (readback && motionReadback && blit) {
                [blit copyFromTexture:internalFrame.texture toTexture:readback];
                // This motion readback occurs only after MetalFX has consumed
                // the GPU-resident input; it is evidence export, never input.
                [blit copyFromTexture:motion toTexture:motionReadback];
                [blit endEncoding];
                [diagnosticCB commit];
                [diagnosticCB waitUntilCompleted];
            }
            if (!readback || !motionReadback || !diagnosticCB ||
                diagnosticCB.status != MTLCommandBufferStatusCompleted ||
                diagnosticCB.error) {
                logLine("R107_METALFX_DIAGNOSTIC_READBACK=FAILED pair=%llu->%llu output=%p commandBuffer=%p status=%lu error=%s",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence, (__bridge void *)readback,
                    (__bridge void *)diagnosticCB, (unsigned long)diagnosticCB.status,
                    diagnosticCB.error.localizedDescription.UTF8String ?: "none");
            } else {
                const NSUInteger pixelCount = width * height;
                NSMutableData *outputBytes = [NSMutableData dataWithLength:
                    pixelCount * 4 * sizeof(uint16_t)];
                NSMutableData *motionBytes = [NSMutableData dataWithLength:
                    pixelCount * 2 * sizeof(uint16_t)];
                [readback getBytes:outputBytes.mutableBytes
                    bytesPerRow:width * 4 * sizeof(uint16_t)
                    fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
                [motionReadback getBytes:motionBytes.mutableBytes
                    bytesPerRow:width * 2 * sizeof(uint16_t)
                    fromRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0];
                const uint16_t *pixels = (const uint16_t *)outputBytes.bytes;
                uint64_t finiteValues = 0, nonfiniteValues = 0, meaningfulPixels = 0;
                float alphaMin = INFINITY, alphaMax = -INFINITY;
                for (NSUInteger i = 0; i < pixelCount; ++i) {
                    BOOL meaningful = NO;
                    for (NSUInteger c = 0; c < 4; ++c) {
                        float value = halfToFloat(pixels[i * 4 + c]);
                        if (isfinite(value)) ++finiteValues; else ++nonfiniteValues;
                        if (c == 3) {
                            if (value < alphaMin) alphaMin = value;
                            if (value > alphaMax) alphaMax = value;
                        } else if (fabsf(value) > 0.01f) meaningful = YES;
                    }
                    if (meaningful) ++meaningfulPixels;
                }
                char outputPath[768];
                snprintf(outputPath, sizeof(outputPath),
                    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/run-01/title-M-for-%llu-%llu.rgba16f",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence);
                (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/run-01", 0700);
                BOOL saved = textureValid && nonfiniteValues == 0 && outputBytes.length == pixelCount * 8 &&
                    [outputBytes writeToFile:[NSString stringWithUTF8String:outputPath] atomically:YES];
                char motionPath[768];
                snprintf(motionPath, sizeof(motionPath),
                    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/run-01/motion-pair-%llu-%llu.rg16f",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence);
                BOOL motionSaved = motionBytes.length == pixelCount * 4 &&
                    [motionBytes writeToFile:[NSString stringWithUTF8String:motionPath] atomically:YES];
                logLine("R107_MOTION_DIAGNOSTIC_READBACK=%s pair=%llu->%llu condition=%s dimensions=%lux%lu format=RG16Float convention=current-to-previous-pixels bytes=%lu path=%s usage=post-generation-only fileUsedForMetalFXInput=0",
                    motionSaved ? "SAVED" : "FAILED",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence, condition.UTF8String ?: "UNSPECIFIED",
                    (unsigned long)width,
                    (unsigned long)height, (unsigned long)motionBytes.length,
                    motionSaved ? motionPath : "<not-saved>");
                logLine("R107_METALFX_OUTPUT=%s pair=%llu->%llu condition=%s internalPresentId=%llu output=%p private=%d dimensions=%lux%lu format=%lu finiteValues=%llu nonfiniteValues=%llu meaningfulPixels=%llu alphaMin=%.6f alphaMax=%.6f readback=post-generation saved=%d path=%s depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
                    saved ? "FINITE" : "INVALID", (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence, condition.UTF8String ?: "UNSPECIFIED",
                    (unsigned long long)internalFrame.internalPresentId,
                    (__bridge void *)internalFrame.texture,
                    internalFrame.texture.storageMode == MTLStorageModePrivate,
                    (unsigned long)width, (unsigned long)height,
                    (unsigned long)internalFrame.texture.pixelFormat,
                    (unsigned long long)finiteValues,
                    (unsigned long long)nonfiniteValues,
                    (unsigned long long)meaningfulPixels, alphaMin, alphaMax,
                    saved, saved ? outputPath : "<not-saved>");
                if (saved && textureValid && meaningfulPixels > 0 && alphaMin > 0.99f && alphaMax < 1.01f) {
                    logLine("R107_GPU_RESIDENT_REAL_TITLE_METALFX_INTERNAL_TEXTURE=PARTIAL pair=%llu->%llu internalPresentId=%llu motionPath=in-process-gpu depthQualifier=CONSTANT_DEPTH_PROTOTYPE endpointDifference=not-yet-measured artifactReview=not-yet-performed outputPath=%s",
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (unsigned long long)internalFrame.internalPresentId,
                        outputPath);
                }
            }
        }
    }

    previousCaptureSource = sourceTexture;
    previousCaptureColor = color;
    previousCaptureDepth = depth;
    previousCaptureMotion = motion;
    previousCaptureSequence = sequence;
    (void)imageThreads;
}

static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<CAMetalDrawable> drawable,
                                uint64_t sourceSequence,
                                id<MTLCommandBuffer> producerCommandBuffer) {
    const char *captureFlag = getenv("FGMETAL_R107_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0) return;
    if (!r107SequenceSelectedForCapture(sourceSequence)) return;

    pthread_mutex_lock(&stateLock);
    if (r107CaptureCount >= kR107CaptureLimit) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    ++r107CaptureCount;
    if (r107CaptureCount == kR107CaptureLimit)
        r107CaptureTriggered = NO;
    ++captureAttempts;
    pthread_mutex_unlock(&stateLock);

    id<MTLTexture> source = drawable.texture;
    id<MTLDevice> device = source.device;
    NSString *conditionLabel = r107CurrentCaptureCondition();
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
        logLine("R107_SOURCE_CAPTURE_ALLOCATION=FAILED seq=%llu gpuTexture=%p diagnosticTexture=%p commandBuffer=%p encoder=%p",
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
            logLine("R107_GPU_PROCESSING_DISPATCHED seq=%llu condition=%s drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p gpuSourceTexture=%p diagnosticDependency=NONE",
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
                "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/run-01/source-sequence-%llu.raw",
                (unsigned long long)sourceSequence);
            (void)mkdir("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/run-01", 0700);
            FILE *captureFile = verified ? fopen(capturePath, "wb") : NULL;
            BOOL saved = captureFile &&
                fwrite(readback.bytes, 1, readback.length, captureFile) == readback.length;
            if (captureFile) fclose(captureFile);
            logLine("R107_SOURCE_DIAGNOSTIC_READBACK seq=%llu condition=%s drawableID=%lu producerCommandBuffer=%p producerStatus=%lu copyCommandBuffer=%p copyStatus=%lu sourceTexture=%p gpuSourceTexture=%p diagnosticTexture=%p device=%p registryID=%llu size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu lumaRange=%u-%u sampleHash=%llu verified=%d saved=%d path=%s",
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
                logLine("R107_REAL_D3DMETAL_SOURCE_CAPTURE=VALIDATED seq=%llu drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p gpuCaptureTexture=%p diagnosticTexture=%p size=%lux%lu pixelFormat=%lu meaningfulSamples=%llu successfulCaptures=%lu",
                    (unsigned long long)sourceSequence,
                    (unsigned long)drawable.drawableID,
                    (__bridge void *)producerCommandBuffer,
                    (__bridge void *)completedCopy,
                    (__bridge void *)captureTexture, (__bridge void *)diagnosticTexture,
                    (unsigned long)width,
                    (unsigned long)height, (unsigned long)source.pixelFormat,
                    (unsigned long long)meaningfulSamples,
                    (unsigned long)successCount);
                logLine("R107_SOURCE_PAIR_IDENTITY=EXACT sourceSequence=%llu condition=%s drawableID=%lu producerCommandBuffer=%p producerStatus=%lu gpuSourceTexture=%p diagnosticPath=%s fileUsedForMetalFXInput=0",
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

static void r105RecordCommittedCommandBuffer(id<MTLCommandBuffer> commandBuffer) {
    (void)commandBuffer;
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
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=R107-inprocess-title-gpu-motion DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s titleCapture=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R107_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R107_CAPTURE") ?: "0",
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
