#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <QuartzCore/CAMetalLayer.h>
#import "MetalFXFrameInterpolationProvider.h"
#import "r116_gate_kernels.h"
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
//
// R121 -- PRESENTATION-SIDE OCCUPANCY DECOMPOSITION.
//
// R120 measured `bPresentHookToMWsiNs` = 47.8 ms median against a 33.3 ms pair
// budget and reported that ~25.1 ms of it was "non-GPU". That was a residual,
// not a measurement: everything not covered by the estimator and MetalFX timers
// was swept into one bucket. R121 replaces the residual with a chain of
// explicitly timestamped instants taken on one clock (CLOCK_MONOTONIC, via
// r105NowNs) so every nanosecond between B's present hook and M's WSI dispatch
// is attributed to a named interval, and the intervals sum back to the headline
// number exactly.
//
// Nothing in the scheduling, presentation, accounting, frontier or estimator
// path is changed by this round. The additions are timestamps, two drawable
// liveness counters and one log line per tuple emitted after the measurement
// window has already closed.

typedef struct { Class cls; SEL sel; IMP original; } Hook;
typedef struct {
    uint64_t count;
    uint64_t sumX;
    uint32_t minX, maxX, minY, maxY;
} ColorStats;

// R110GateGroupStats is defined once, in r116_gate_kernels.h, so the shipped
// observer and the offline equivalence harness share the record layout.

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
// R115: the tuple generation that created this ticket. Every callback that can
// outlive its tuple verifies it before mutating shared state.
@property(nonatomic) uint64_t generation;
// R116: set when retained B was forwarded immediately after M's dispatch instead
// of waiting for M's presented callback. It lets the late M callback report
// itself as expected rather than as a stale callback.
@property(nonatomic) BOOL forwardedEarly;
// Set once an INTERNAL frame has been presented for this tuple. Only such a
// tuple has an M to compare B against; a fallback tuple has none, so it must not
// create an ordering slot that can never complete.
@property(nonatomic) BOOL internalDispatched;
@property(nonatomic) BOOL producerCompletedWhileDeferredLogged;
@property(nonatomic) R102DeferredTicketState state;
@end

@implementation R102DeferredSourceTicket
@end

static const char *const kR110TupleTargetPath =
    "/tmp/fgmetal-r108-unattended/r112-target-source-b";
static const char *const kR110WorldSceneConfirmationPath =
    "/tmp/fgmetal-r108-unattended/r112-world-scene-confirmed";
static const char *const kR111CameraAttributionPath =
    "/tmp/fgmetal-r108-unattended/r111-camera-attribution-verified";
static const char *const kR112CharacterAttributionPath =
    "/tmp/fgmetal-r108-unattended/r112-character-attribution-verified";
static const char *const kR110EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-05-r112-first-elden-ring-internal-display";
// R114 one-shot in-process target latch. The arm file may only start the
// experiment before a pair exists; it can never name an already-produced pair.
static const char *const kR114ArmRequestPath =
    "/tmp/fgmetal-r108-unattended/r114-arm-next-moving-pair";
static const char *const kR114EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-06-r114-inprocess-latch-title-display";
// R115 re-armable continuous tuple lifecycle. The start file only permits the
// observer to begin the bounded run; it never names a pair.
static const char *const kR115StartRequestPath =
    "/tmp/fgmetal-r108-unattended/r115-continuous-start";
static const char *const kR115EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-07-r115-continuous-tuple-lifecycle";

// Forward declarations the R119 evidence accessor depends on. The definitions
// live further down with the lifecycle helpers; the accessor is placed next to
// the path constants it replaces so that every writer below it can see it.
static BOOL r114LatchEnabled(void);
static BOOL r115ContinuousEnabled(void);
static void logLine(const char *fmt, ...);

// ---------------------------------------------------------------------------
// R119 Phase 0 -- evidence-directory isolation.
//
// R118 defect: kR115EvidenceDirectory is a compile-time constant. Every build
// that keeps FGMETAL_R115_CONTINUOUS=1 writes its per-pair diagnostics there,
// whatever round is running, so R118 silently overwrote 25 R115 source raws and
// 5 R115 motion fields. One round of measurement must not be able to destroy
// another round's corpus.
//
// Every writer now resolves its directory through fgmetalEvidenceDirectory().
// There is exactly one getenv() for FGMETAL_EVIDENCE_DIR in this file, inside
// the one resolver below, and it runs exactly once.
//
// The override wins when set and non-empty. When it is not set the historical
// per-round constant is still returned, so an intentional rerun of an old round
// resolves exactly where it used to -- but the corpus guard refuses to hand a
// *protected* directory back to a writer unless FGMETAL_ALLOW_LEGACY_EVIDENCE_DIR=1
// is set. A refusal disables diagnostics rather than writing, so the failure is
// loud and non-destructive instead of silent and destructive.
// ---------------------------------------------------------------------------
static const char *const kR119EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r119-live-throughput";
// R121's own directory. This is the only directory inside the corpus the R121
// build may write to. R120 is now somebody else's corpus and is refused by the
// same guard that R120 used to protect R119. The constant is not kept as a
// reference because protection is derived, not enumerated: anything under the
// corpus root that is not this round's own directory is protected.
static const char *const kR121EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r121-presentation-occupancy";
// Root of the historical corpus. Anything under it that is not this round's own
// directory is another round's evidence and must never be written to.
static const char *const kEvidenceCorpusRoot =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/";

static const char *gFGMetalEvidenceDirectory;
static const char *gFGMetalEvidenceDirectorySource = "unresolved";
static BOOL gFGMetalEvidenceWritable;
static pthread_once_t fgmetalEvidenceOnce = PTHREAD_ONCE_INIT;

// Resolves symlinks and ".." so a protected path cannot be smuggled past the
// guard by spelling it differently. Returns NO when nothing can be resolved, and
// the caller treats that as protected.
static BOOL fgmetalResolvePath(const char *path, char *out, size_t capacity) {
    if (!path || !*path || capacity == 0) return NO;
    char resolved[4096];
    if (realpath(path, resolved)) {
        snprintf(out, capacity, "%s", resolved);
        return YES;
    }
    // The directory may not exist yet: resolve the parent and re-attach the leaf.
    char copy[4096];
    const int written = snprintf(copy, sizeof(copy), "%s", path);
    if (written <= 0 || (size_t)written >= sizeof(copy)) return NO;
    char *slash = strrchr(copy, '/');
    if (!slash || slash == copy) return NO;
    *slash = '\0';
    const char *leaf = path + strlen(copy) + 1;
    if (!*leaf || !realpath(copy, resolved)) return NO;
    const int total = snprintf(out, capacity, "%s/%s", resolved, leaf);
    return total > 0 && (size_t)total < capacity;
}

static BOOL fgmetalPathIsWithin(const char *path, const char *root) {
    const size_t length = strlen(root);
    if (strncmp(path, root, length) != 0) return NO;
    return path[length] == '\0' || path[length] == '/';
}

static BOOL fgmetalEvidenceDirectoryIsProtected(const char *path) {
    if (!path || !*path) return YES;
    char resolvedPath[4096], resolvedRoot[4096], resolvedOwn[4096];
    // Unresolvable input is protected: failing closed is the only safe direction
    // for a guard whose job is to stop one round destroying another's corpus.
    if (!fgmetalResolvePath(path, resolvedPath, sizeof(resolvedPath))) return YES;
    if (!fgmetalResolvePath(kEvidenceCorpusRoot, resolvedRoot, sizeof(resolvedRoot))) return YES;
    if (!fgmetalResolvePath(kR121EvidenceDirectory, resolvedOwn, sizeof(resolvedOwn))) return YES;
    if (!fgmetalPathIsWithin(resolvedPath, resolvedRoot)) return NO;
    return !fgmetalPathIsWithin(resolvedPath, resolvedOwn);
}

static void fgmetalResolveEvidenceDirectory(void) {
    const char *override = getenv("FGMETAL_EVIDENCE_DIR");
    if (override && *override) {
        gFGMetalEvidenceDirectory = override;
        gFGMetalEvidenceDirectorySource = "env";
    } else {
        // Legacy resolution, preserved only for deliberate historical reruns.
        if (r115ContinuousEnabled()) gFGMetalEvidenceDirectory = kR115EvidenceDirectory;
        else if (r114LatchEnabled()) gFGMetalEvidenceDirectory = kR114EvidenceDirectory;
        else gFGMetalEvidenceDirectory = kR110EvidenceDirectory;
        gFGMetalEvidenceDirectorySource = "legacy";
    }
    const char *allowLegacy = getenv("FGMETAL_ALLOW_LEGACY_EVIDENCE_DIR");
    const BOOL explicitlyAllowed = allowLegacy && strcmp(allowLegacy, "1") == 0;
    gFGMetalEvidenceWritable = explicitlyAllowed ||
        !fgmetalEvidenceDirectoryIsProtected(gFGMetalEvidenceDirectory);
}

// The single accessor every writer uses. Returns the resolved path even when it
// is not writable, so it can be logged; writers must also consult
// fgmetalEvidenceDiagnosticsAllowed().
static const char *fgmetalEvidenceDirectory(void) {
    pthread_once(&fgmetalEvidenceOnce, fgmetalResolveEvidenceDirectory);
    return gFGMetalEvidenceDirectory ? gFGMetalEvidenceDirectory : "<unresolved>";
}

static BOOL fgmetalEvidenceDiagnosticsAllowed(void) {
    pthread_once(&fgmetalEvidenceOnce, fgmetalResolveEvidenceDirectory);
    return gFGMetalEvidenceWritable;
}

// Builds "<resolved>/<formatted name>" and creates the directory. Returns NO
// when diagnostics are disabled or the name does not fit, and the caller must
// then skip the write entirely. This is the only place a diagnostics path is
// composed.
static BOOL fgmetalEvidencePath(char *out, size_t capacity,
                                const char *nameFormat, ...) {
    if (!out || capacity == 0) return NO;
    out[0] = '\0';
    if (!fgmetalEvidenceDiagnosticsAllowed()) return NO;
    const char *directory = fgmetalEvidenceDirectory();
    char name[512];
    va_list ap;
    va_start(ap, nameFormat);
    const int written = vsnprintf(name, sizeof(name), nameFormat, ap);
    va_end(ap);
    if (written <= 0 || (size_t)written >= sizeof(name)) return NO;
    const int total = snprintf(out, capacity, "%s/%s", directory, name);
    if (total <= 0 || (size_t)total >= capacity) {
        out[0] = '\0';
        return NO;
    }
    (void)mkdir(directory, 0700);
    return YES;
}

static BOOL fgmetalWriteEvidenceAbsolute(const char *absolutePath,
                                        const void *bytes, size_t length) {
    if (!absolutePath || !*absolutePath || !bytes || length == 0) return NO;
    if (!fgmetalEvidenceDiagnosticsAllowed()) return NO;
    NSData *data = [NSData dataWithBytes:bytes length:length];
    return [data writeToFile:[NSString stringWithUTF8String:absolutePath]
                  atomically:YES];
}

// R119 evidence-isolation self test. Runs from the dylib constructor in whatever
// process loaded it, before any hook is installed, so it exercises the real
// accessor and the real blob writer without needing the game. Two artefacts, one
// per artefact class that R118 damaged (a source raw and a motion field), land in
// the resolved directory only.
static void fgmetalEvidenceSelfTest(void) {
    const char *enabled = getenv("FGMETAL_EVIDENCE_SELFTEST");
    if (!enabled || strcmp(enabled, "1") != 0) return;
    const BOOL allowed = fgmetalEvidenceDiagnosticsAllowed();
    const char *directory = fgmetalEvidenceDirectory();
    char sourcePath[1024] = {0};
    char motionPath[1024] = {0};
    const BOOL sourcePathOk = fgmetalEvidencePath(sourcePath, sizeof(sourcePath),
        "r121-isolation-probe-source-%d.raw", getpid());
    const BOOL motionPathOk = fgmetalEvidencePath(motionPath, sizeof(motionPath),
        "r121-isolation-probe-motion-%d.rg16f", getpid());
    uint8_t sourceBytes[4096];
    memset(sourceBytes, 0xA5, sizeof(sourceBytes));
    uint16_t motionBytes[2048];
    for (size_t i = 0; i < 2048; ++i) motionBytes[i] = (uint16_t)(i * 7u);
    const BOOL sourceWritten = sourcePathOk &&
        fgmetalWriteEvidenceAbsolute(sourcePath, sourceBytes, sizeof(sourceBytes));
    const BOOL motionWritten = motionPathOk &&
        fgmetalWriteEvidenceAbsolute(motionPath, motionBytes, sizeof(motionBytes));
    logLine("R121_EVIDENCE_SELFTEST pid=%d allowed=%d source=%s directory=%s protected=%d sourceArtefact=%s saved=%d motionArtefact=%s saved=%d",
        getpid(), allowed ? 1 : 0, gFGMetalEvidenceDirectorySource, directory,
        fgmetalEvidenceDirectoryIsProtected(directory) ? 1 : 0,
        sourcePath[0] ? sourcePath : "<refused>", sourceWritten,
        motionPath[0] ? motionPath : "<refused>", motionWritten);
}

// ---------------------------------------------------------------------------
// R119 Phase 2/3 -- the media clock.
//
// CACurrentMediaTime() shares its time base with MTLCommandBuffer GPUStartTime
// and GPUEndTime. Recording a CPU instant on that same clock therefore lets a
// GPU timestamp be subtracted from it directly, with no cross-domain offset and
// no assumption about when the process started. That is what makes
//
//   queue delay      = GPUStartTime - CPU commit instant
//   GPU execution    = GPUEndTime   - GPUStartTime
//
// separable measurements instead of one conflated "estimator time". The
// authoritative service metric keeps its existing CLOCK_MONOTONIC definition;
// this clock is used only for the new decomposition, and every interval it
// produces is a difference of two media-clock values.
// ---------------------------------------------------------------------------
static double fgmetalMediaNow(void) {
    return CACurrentMediaTime();
}

static uint64_t fgmetalMediaToNs(double seconds) {
    if (!(seconds > 0.0)) return 0;
    return (uint64_t)llround(seconds * 1e9);
}

// Difference of two media-clock instants, clamped at zero. A negative result
// means the two instants did not come from the same clock domain, and 0 is the
// honest value to publish -- the alternative is a plausible-looking number with
// the wrong sign, which would be worse than an obviously missing one.
static uint64_t fgmetalMediaDeltaNs(double later, double earlier) {
    if (!(later > 0.0) || !(earlier > 0.0)) return 0;
    if (later < earlier) return 0;
    return fgmetalMediaToNs(later - earlier);
}

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
// R116: single-struct reduction stage plus the persistent buffers it writes.
// Both are allocated once and reused, so the gate adds no allocation to the
// critical path.
static id<MTLComputePipelineState> r110FastGateReducePipeline;
static id<MTLBuffer> r110GateGroupBuffer;
static id<MTLBuffer> r110GateResultBuffer;
static NSUInteger r110GateGroupCount;
static void *r110GateBufferDevice;
static uint64_t r116GateEncodedCount;
static uint64_t r116GateEncodeFailures;
// Monotonic tag stamped into the reduced result so a stale buffer can never be
// mistaken for this submission's metrics.
static uint32_t r116GateTagNext = 1;
static uint32_t r116GateTagExpected;
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

// R114 in-process latch state. Deliberately one-shot: the observer reserves at
// most one pair per process and never re-arms itself.
typedef NS_ENUM(uint8_t, R114LatchState) {
    R114LatchIdle = 0,
    R114LatchArmed,
    R114LatchReserved,
    R114LatchFailed,
    R114LatchConsumed,
};
static R114LatchState r114LatchState;
static uint64_t r114ProviderWarmCount;
static uint64_t r114ProviderWarmAtNs;
static uint64_t r114ArmAtNs;
static uint64_t r114MotionObservedAtNs;
static uint64_t r114MotionObservedPairA;
static uint64_t r114MotionObservedPairB;
static double r114MotionObservedFraction;
static uint64_t r114MotionObservationLogs;
static uint64_t r114ReservedPairA;
static uint64_t r114ReservedPairB;
static uint64_t r114ReservedAtNs;
static uint64_t r114ReservationAttempts;
static uint64_t r114ReservationSkips;
static uint64_t r114LastCommittedSourceSequence;
static uint64_t r114LastDispatchedSourceSequence;
static uint64_t r114PreFrontierAdmissions;
static uint64_t r114MissedTargetAdmissions;
static uint64_t r114SourceLeaksWhileClosed;
static uint64_t r114RenewedCaptureWindows;
static const NSUInteger kR114MaxCaptureWindows = 12;
static uint64_t r102SuccessfulTupleCount;
static uint64_t r102FallbackCount;
static uint64_t r102StaleCallbackCount;
static uint64_t r102DuplicateInternalDispatchCount;
static uint64_t r102WrongPairDispatchCount;

// ---------------------------------------------------------------------------
// R115 re-armable tuple lifecycle.
//
// R114 was terminal after one tuple: `r103FrontierSpent` stayed set and the
// latch stayed CONSUMED, so nothing could reserve again. R115 adds the smallest
// explicit lifecycle that returns the *same* accepted seams to IDLE, plus a
// generation nonce that invalidates any callback from a previous tuple.
//
//   IDLE -> ARMED -> PAIR_RESERVED -> FRONTIER_CLOSING -> B_DEFERRED
//        -> PROCESSING -> M_PRESENTING -> B_FORWARDING -> RESETTING -> IDLE
//   any state -> FALLBACK -> RESETTING -> IDLE
//
// No speculative states. Each transition is driven by an existing R114 seam.
// ---------------------------------------------------------------------------
typedef NS_ENUM(uint8_t, R115TuplePhase) {
    R115PhaseIdle = 0,
    R115PhaseArmed,
    R115PhasePairReserved,
    R115PhaseFrontierClosing,
    R115PhaseBDeferred,
    R115PhaseProcessing,
    R115PhaseMPresenting,
    R115PhaseBForwarding,
    R115PhaseFallback,
    R115PhaseResetting,
};

typedef struct {
    uint64_t generation;
    uint64_t pairA;
    uint64_t pairB;
    uint64_t armNs;
    uint64_t reservedNs;
    uint64_t aAcquiredNs;
    uint64_t aDispatchNs;
    uint64_t aDisplayedNs;
    uint64_t bAcquiredNs;
    uint64_t bCommitNs;
    uint64_t bPresentHookNs;
    uint64_t bProducerCompletedNs;
    uint64_t estimatorStartNs;
    uint64_t estimatorEndNs;
    uint64_t metalfxStartNs;
    uint64_t metalfxEndNs;
    uint64_t gateStartNs;
    uint64_t gateEndNs;
    // R118 generation-service window. gsStartNs is taken on entry to
    // processCapturedFrame for the pair's B frame -- the instant the captured
    // source is handed to the generation path -- and gsEndNs is the gate
    // verdict. Every field in this struct is CLOCK_MONOTONIC, including
    // bAcquiredNs, so the whole service interval including the source copy and
    // the dispatch hop is one subtraction. Added for measurement only: no
    // scheduling, presentation or accounting path reads these.
    uint64_t gsStartNs;
    uint64_t gsEndNs;
    // R119 handoff decomposition. All values are CACurrentMediaTime() seconds,
    // i.e. one clock domain, so any two of them may be subtracted. They are
    // additive measurement only; nothing in the product path reads them.
    //
    //   present hook -> blit encode      CPU: ticket publication to capture entry
    //   blit encode  -> blit commit      CPU: encode of the source blit
    //   blit commit  -> copy GPU start   GPU: wait for the copy to be scheduled
    //   copy GPU start -> copy GPU end   GPU: the source copy itself
    //   copy GPU end -> copy wake        GPU completion callback latency
    //   copy wake    -> dispatch         CPU: liveness checks before handoff
    //   dispatch     -> gen queue entry  CPU: serial-queue wait
    //   gen queue entry -> gen start     CPU: entry bookkeeping
    double mediaHook;
    double mediaProducerComplete;
    double mediaBlitEncode;
    double mediaBlitCommit;
    double mediaCopyGpuStart;
    double mediaCopyGpuEnd;
    double mediaCopyWake;
    double mediaDispatch;
    double mediaGenQueueEntry;
    double mediaGenStart;
    // R119 estimator queue profile, same clock.
    double mediaEstimatorCommit;
    double mediaEstimatorGpuStart;
    double mediaEstimatorGpuEnd;
    double mediaEstimatorWake;
    // R119 clock-domain validation. GPUEndTime and the media clock are supposed
    // to share a time base; if they do not, every queue-delay number in this
    // round is wrong by a constant. This value is the signed residual and is
    // published unclamped, because a negative residual is the only direct proof
    // that the assumption failed.
    int64_t wakeMinusGpuEndNs;
    int64_t estimatorClockSkewNs;
    BOOL estimatorClockConsistent;
    BOOL mediaHandoffValid;
    BOOL mediaEstimatorValid;
    uint64_t internalAcquireNs;
    uint64_t mDispatchNs;
    uint64_t mDisplayedNs;
    uint64_t bForwardBeginNs;
    uint64_t bForwardEndNs;
    uint64_t frontierOpenNs;
    uint64_t leaksAtArm;
    uint64_t fallbackCountAtArm;
    uint64_t displayedCountAtArm;
    uint64_t wsiDispatchesAtArm;
    BOOL accepted;
    BOOL gateRejected;
} R115TupleTiming;

static R115TuplePhase r115Phase;
static R115TupleTiming r115Timing;
static uint64_t r115Generation = 1;
static uint64_t r115AttemptedTuples;
static uint64_t r115AcceptedTuples;
static uint64_t r115FallbackTuples;
static uint64_t r115ResetCount;
static uint64_t r115StaleCallbackRejections;
// R116 counters.
static uint64_t r116WatchdogFired;
static uint64_t r116WatchdogArmed;
static uint64_t r116EarlyBDispatchCount;
static uint64_t r116LateMCallbacksAfterEarlyB;
static uint64_t r116OrderingViolations;
static uint64_t r116CoalescedPresentations;
static uint64_t r116BCallbackMissing;
// Set the instant B's present hook is entered for a given pair.
//
// The watchdog uses the *age* of this record, not its mere presence. A hook that
// enters and then rejects (ineligible producer, frontier timeout) never publishes
// a ticket, so "hook entered" alone must not suppress the watchdog forever -
// that would restore the 4 s stall for exactly those paths. A hook that entered
// moments ago is still publishing, so the watchdog must not fire either. Age
// distinguishes the two.
static uint64_t r116BHookEnteredPairB;
static uint64_t r116BHookEnteredNs;
// R116 provisional acceptance. When B is forwarded before M's presented callback,
// the tuple's terminal state is reached while the display evidence for M does not
// exist yet. Such a tuple is retired as PENDING_DISPLAY and is counted as
// accepted only once R116_PRESENTATION_ORDER confirms M and B both reached the
// display, strictly in that order. A pending tuple whose evidence never arrives,
// or arrives out of order, is counted as a fallback instead.
static uint64_t r116PendingDisplayTuples;
static uint64_t r116DisplayConfirmedTuples;
static uint64_t r116DisplayRejectedTuples;
static uint64_t r116DisplayEvidenceLost;
static uint64_t r116DisplayTimedOut;
static BOOL r116SummaryCheckPending;
static uint64_t r115MaxTuples;
static uint64_t r115ArmCooldownUntilSequence;
static uint64_t r115WarmupPairA;
static uint64_t r115WarmupPairB;
static uint64_t r115CaptureMask;
static uint64_t r115CaptureFrames;
static BOOL r115RunStarted;
static BOOL r115RunComplete;
static BOOL r115SummaryLogged;
static BOOL r115ReadyLogged;
static uint64_t r115PostRunStartSequence;
static __thread BOOL r115InsidePresentedCallback;
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
static BOOL r114LatchEnabled(void);
static BOOL r115ContinuousEnabled(void);
static void r115ResetTuple(const char *outcome, const char *reason);
static void r116WatchdogCommittedBWithoutTicket(uint64_t ticketId,
                                                uint64_t pairA,
                                                uint64_t pairB);
static void r116NotePresentationTime(uint64_t ticketId, BOOL isM,
                                     double presentedTime,
                                     BOOL reachedDisplay);
static void r116MarkPendingDisplay(uint64_t ticketId);
static void r116ClearPendingDisplay(uint64_t ticketId);
static void r115NotePhase(R115TuplePhase phase, const char *detail);
static BOOL r115PairCaptureSelectedLocked(uint64_t sequence);
static BOOL r114RenewalWantedLocked(void);
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

static BOOL r112CharacterPairAttributionVerified(uint64_t expectedA,
                                                  uint64_t expectedB) {
    const char *required = getenv("FGMETAL_R112_CHARACTER_ATTRIBUTION");
    if (!required || strcmp(required, "1") != 0) return YES;

    FILE *file = fopen(kR112CharacterAttributionPath, "r");
    if (!file) return NO;
    char line[512] = {0};
    BOOL read = fgets(line, sizeof(line), file) != NULL;
    fclose(file);
    int pid = 0;
    unsigned long long sourceA = 0, sourceB = 0;
    unsigned long long keyDownNs = 0, sourceAAcquiredNs = 0;
    unsigned long long sourceBAcquiredNs = 0, keyUpNs = 0;
    int fields = read ? sscanf(line,
        "pid=%d A=%llu B=%llu input=character-forward key=W keyDownNs=%llu sourceAAcquiredNs=%llu sourceBAcquiredNs=%llu keyUpNs=%llu",
        &pid, &sourceA, &sourceB, &keyDownNs, &sourceAAcquiredNs,
        &sourceBAcquiredNs, &keyUpNs) : 0;
    return fields == 7 && pid == getpid() && sourceA == expectedA &&
        sourceB == expectedB && keyDownNs < sourceAAcquiredNs &&
        sourceAAcquiredNs < sourceBAcquiredNs && sourceBAcquiredNs < keyUpNs;
}

static void r110RefreshTupleTarget(void) {
    if (!r110PresentationEnabled()) return;
    // R114: with the in-process latch enabled the legacy file-backed target is
    // never consulted; target selection belongs to the observer only.
    if (r114LatchEnabled()) return;
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
    // R115 does not use bounded capture windows: capture is driven by the
    // reserved pair, so no window can expire mid-tuple.
    if (r115ContinuousEnabled()) return;
    pthread_mutex_lock(&stateLock);
    // R114: while the one-shot experiment is pending (arm request present and
    // latch IDLE) or already ARMED with no pair reserved, the bounded capture
    // window may be renewed so a human reaction delay or a slow provider warm-up
    // cannot silently exhaust the only window.
    BOOL renewal = r114RenewalWantedLocked() && r108CaptureWindows > 0 &&
        r114RenewedCaptureWindows + 1 < kR114MaxCaptureWindows;
    BOOL shouldCheck = !r108CaptureTriggered &&
        r108CaptureWindows < kR108CaptureWindowsLimit && (r108CaptureWindows == 0 || renewal);
    pthread_mutex_unlock(&stateLock);
    if (!shouldCheck || access(kR108CaptureTriggerPath, F_OK) != 0) return;

    BOOL started = NO;
    BOOL renewed = NO;
    pthread_mutex_lock(&stateLock);
    BOOL renewalAllowed = r114RenewalWantedLocked() && r108CaptureWindows > 0 &&
        r114RenewedCaptureWindows + 1 < kR114MaxCaptureWindows;
    if (!r108CaptureTriggered &&
        r108CaptureWindows < kR108CaptureWindowsLimit &&
        (r108CaptureWindows == 0 || renewalAllowed)) {
        renewed = r108CaptureWindows > 0;
        r108CaptureTriggered = YES;
        r108CaptureStartSequence = sequence;
        r108CaptureEndSequence = sequence + kR108CaptureLimit - 1;
        r108CaptureCount = 0;
        ++r108CaptureWindows;
        if (renewed) ++r114RenewedCaptureWindows;
        started = YES;
    }
    uint64_t end = r108CaptureEndSequence;
    uint64_t windows = r108CaptureWindows;
    pthread_mutex_unlock(&stateLock);
    if (!started) return;
    (void)unlink(kR108CaptureTriggerPath);
    logLine("R108_TITLE_CAPTURE=ARMED window=%lu startSequence=%llu expectedEndSequence=%llu boundedFrames=%lu maxWindows=%lu trigger=one-shot-file renewed=%d r114RenewedWindows=%llu",
        (unsigned long)windows, (unsigned long long)sequence,
        (unsigned long long)end,
        (unsigned long)kR108CaptureLimit, (unsigned long)kR108CaptureWindowsLimit,
        renewed ? 1 : 0, (unsigned long long)r114RenewedCaptureWindows);
}

static BOOL r108SequenceSelectedForCapture(uint64_t sequence) {
    pthread_mutex_lock(&stateLock);
    BOOL selected;
    if (r115ContinuousEnabled()) {
        // R115 captures exactly the reserved pair (plus the one-off provider
        // warm-up pair). Nothing else is copied, so the estimator can never fall
        // behind the frame rate and the retained-B hold stays bounded.
        selected = r115PairCaptureSelectedLocked(sequence);
        if (selected) {
            if (sequence == r114ReservedPairA) r115CaptureMask |= 1;
            else if (sequence == r114ReservedPairB) r115CaptureMask |= 2;
            ++r115CaptureFrames;
        }
    } else {
        selected = r108CaptureTriggered &&
            sequence >= r108CaptureStartSequence &&
            r108CaptureCount < kR108CaptureLimit;
    }
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

// ---------------------------------------------------------------------------
// R121 presentation-occupancy decomposition.
//
// One chain, one clock. Every instant below is CLOCK_MONOTONIC nanoseconds from
// r105NowNs() -- the same clock the accepted R115 tuple timing already uses --
// so any two instants may be subtracted and the consecutive differences
// telescope exactly:
//
//     sum(consecutive deltas)  ==  t[R121SlotMWsiDispatch] - t[R121SlotBHook]
//                              ==  bPresentHookToMWsiNs
//
// That identity is checked for every tuple and the residual is published. It is
// the only defence against the R120 failure mode, where a headline figure was
// reconciled against a sum of unrelated medians and a 25.1 ms residual was
// labelled "non-GPU" without ever being attributed.
//
// Slot order is chronological except the last two. The accepted pipeline
// allocates M's WSI identity and *then* calls the layer's present, while the
// headline endpoint (r115Timing.mDispatchNs) is taken between the two. The
// present call therefore lies after the measured window; it is reported as an
// extension and never folded into the budget.
//
// Metal GPU timestamps (GPUStartTime/GPUEndTime) are a different clock domain.
// They are stored in parallel fields, never in the chain, and each copy
// publishes a closure residual so the cross-clock subtraction is auditable
// per tuple instead of assumed.
// ---------------------------------------------------------------------------
typedef NS_ENUM(uint8_t, R121Slot) {
    R121SlotBHook = 0,             //  1  B present-hook entry
    R121SlotSrcCaptureEntry,       //  2  producer-complete -> capture entry
    R121SlotSrcCopyEncoded,        //     SOURCE blit fully encoded
    R121SlotSrcCopyCommit,         //     SOURCE blit commit
    R121SlotSrcCopyWake,           //     SOURCE copy GPU completion, CPU wake
    R121SlotTaskEnqueue,           //  2  generation task enqueue
    R121SlotTaskStart,             //  3  generation task start
    R121SlotEstimatorStart,        //  4
    R121SlotEstimatorEnd,          //  5
    R121SlotMetalFXStart,          //  6
    R121SlotMetalFXEnd,            //  7
    R121SlotGateVerdict,           //  8
    R121SlotInternalAcquireReq,    //  9
    R121SlotInternalNdEnter,       // 10
    R121SlotInternalNdReturn,      // 11
    R121SlotInternalAcquired,      // 12
    R121SlotMCopyEncodeStart,      // 13
    R121SlotMCopyCommit,           // 14
    R121SlotMCopyWake,             // 17
    R121SlotMWsiAlloc,             // 20  M WSI identity allocated
    R121SlotMWsiDispatch,          // 20  endpoint == mDispatchNs
    R121SlotInternalPresentEnter,  // 18  extension: after the endpoint
    R121SlotInternalPresentReturn, // 19  extension
    R121SlotCount
};

typedef struct {
    uint64_t generation;
    uint64_t pairA;
    uint64_t pairB;
    uint64_t t[R121SlotCount];
    uint8_t have[R121SlotCount];
    uint8_t armed;
    // GPU-clock parallel measures. Metal's GPUStartTime/GPUEndTime are on the
    // CACurrentMediaTime base, NOT CLOCK_MONOTONIC -- R119 established that and
    // R121's first build re-learned it the hard way. Every GPU timestamp is
    // therefore compared only against other media-clock samples, and each copy
    // publishes the difference between its media-clock duration and its
    // monotonic-clock duration as the actual cross-clock validation.
    double mediaSrcCopyCommit;
    double mediaSrcCopyWake;
    double mediaMCopyCommit;
    double mediaMCopyWake;
    uint64_t srcCopyGpuStartNs;
    uint64_t srcCopyGpuEndNs;
    uint64_t mCopyGpuStartNs;
    uint64_t mCopyGpuEndNs;
    // Drawable availability at the INTERNAL acquisition.
    uint64_t poolMaximumDrawables;
    int32_t deferredBPresent;
    uint64_t liveSourceDrawables;
    uint64_t liveInternalDrawables;
    int32_t internalSlotState;
    int32_t frontierState;
    // Nearest SOURCE nextDrawable in the same window, for pressure comparison.
    uint64_t sourceNdEntryNs;
    uint64_t sourceNdReturnNs;
} R121Span;

static R121Span r121Span;

// Observer-side drawable accounting.
//
// This is bookkeeping, not a query of Metal's pool state. An earlier build of
// R121 used acquire++ / presented-- and drifted to 14 against a pool of 3. The
// drift was not a Metal violation: the R116 early-B path takes a SOURCE present
// out of ordinary WSI dispatch, and at that time it installed no presented
// handler, so those drawables were never released in the counter. R121 now
// releases from *both* SOURCE presented handlers -- the ordinary dispatch one
// and the forwarded-deferred one -- and publishes the three totals
// (acquired / released / deferred) alongside the derived outstanding figure so
// a reader can re-derive it instead of trusting it.
static uint64_t r121SourceAcquiredTotal;
static uint64_t r121SourceReleasedTotal;
static uint64_t r121SourceDeferredTotal;
static uint64_t r121InternalAcquiredTotal;
static uint64_t r121InternalReleasedTotal;
static uint64_t r121SourceOutstandingMax;
static uint64_t r121InternalOutstandingMax;
static uint64_t r121MarkCount;
// Most recent SOURCE nextDrawable in the same window. Written by the render
// thread, read by the capture thread at M's acquisition, so the INTERNAL wait
// can be compared against SOURCE pressure rather than against an assumption.
static uint64_t r121LastSourceNdEntryNs;
static uint64_t r121LastSourceNdReturnNs;

static inline void r121MarkValue(R121Slot slot, uint64_t ns) {
    if (!__atomic_load_n(&r121Span.armed, __ATOMIC_ACQUIRE)) return;
    if (__atomic_load_n(&r121Span.have[slot], __ATOMIC_RELAXED)) return;
    __atomic_store_n(&r121Span.t[slot], ns, __ATOMIC_RELAXED);
    __atomic_store_n(&r121Span.have[slot], 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&r121MarkCount, 1, __ATOMIC_RELAXED);
}

static inline void r121Mark(R121Slot slot) {
    if (!__atomic_load_n(&r121Span.armed, __ATOMIC_ACQUIRE)) return;
    r121MarkValue(slot, r105NowNs());
}

// Called from the tuple arm, which is the only point at which the span is
// allowed to be reused. armed is dropped first so a mark that is still in
// flight from the previous tuple is discarded rather than written into the new
// record.
static void r121SpanBegin(uint64_t generation, uint64_t pairA, uint64_t pairB) {
    __atomic_store_n(&r121Span.armed, 0, __ATOMIC_RELEASE);
    memset(&r121Span, 0, sizeof(r121Span));
    r121Span.generation = generation;
    r121Span.pairA = pairA;
    r121Span.pairB = pairB;
    __atomic_store_n(&r121Span.armed, 1, __ATOMIC_RELEASE);
}

static void r121NoteOutstandingMax(uint64_t *maxSlot, uint64_t outstanding) {
    uint64_t seen = __atomic_load_n(maxSlot, __ATOMIC_RELAXED);
    while (outstanding > seen &&
           !__atomic_compare_exchange_n(maxSlot, &seen, outstanding, NO,
                                        __ATOMIC_RELAXED, __ATOMIC_RELAXED)) {
    }
}

static uint64_t r121SourceOutstanding(void) {
    const uint64_t acquired = __atomic_load_n(&r121SourceAcquiredTotal, __ATOMIC_RELAXED);
    const uint64_t released = __atomic_load_n(&r121SourceReleasedTotal, __ATOMIC_RELAXED);
    return acquired > released ? acquired - released : 0;
}

static uint64_t r121InternalOutstanding(void) {
    const uint64_t acquired = __atomic_load_n(&r121InternalAcquiredTotal, __ATOMIC_RELAXED);
    const uint64_t released = __atomic_load_n(&r121InternalReleasedTotal, __ATOMIC_RELAXED);
    return acquired > released ? acquired - released : 0;
}

static void r121NoteSourceDrawableAcquired(void) {
    __atomic_add_fetch(&r121SourceAcquiredTotal, 1, __ATOMIC_RELAXED);
    r121NoteOutstandingMax(&r121SourceOutstandingMax, r121SourceOutstanding());
}

static void r121NoteInternalDrawableAcquired(void) {
    __atomic_add_fetch(&r121InternalAcquiredTotal, 1, __ATOMIC_RELAXED);
    r121NoteOutstandingMax(&r121InternalOutstandingMax, r121InternalOutstanding());
}

// Both SOURCE exits call this: the ordinary WSI-dispatch presented handler and
// the forwarded-deferred presented handler.
static void r121NoteSourceDrawableReleased(void) {
    __atomic_add_fetch(&r121SourceReleasedTotal, 1, __ATOMIC_RELAXED);
}

// Informational only: how many SOURCE presents took the R116 early-B deferred
// path instead of ordinary WSI dispatch. Not part of the outstanding identity.
static void r121NoteSourceDrawableDeferred(void) {
    __atomic_add_fetch(&r121SourceDeferredTotal, 1, __ATOMIC_RELAXED);
}

static void r121NoteInternalDrawableReleased(void) {
    __atomic_add_fetch(&r121InternalReleasedTotal, 1, __ATOMIC_RELAXED);
}

// ---------------------------------------------------------------------------
// R114 in-process next-eligible-moving-pair latch.
//
// R113 proved that an external exact-pair target is written only after the pair
// already exists, so SOURCE B reaches ordinary WSI presentation before
// R110_PRESENT_TARGET can be observed. R114 keeps the accepted R110 gate, the
// R102 deferred-B ticket and the R103 frontier untouched and only moves target
// selection inside the observer:
//
//   ARMED  -> watch the observer's own accepted estimator output
//          -> first consecutive pair with real motion reserves the *next*
//             not-yet-committed pair (A,B) as the one-shot title tuple
//   B producer commit -> accepted frontier close (unchanged seam)
//   B present         -> accepted deferred-B ticket (unchanged)
//
// The external controller may arm the experiment, but it never tells the
// observer which already-produced pair to intercept.
// ---------------------------------------------------------------------------
static BOOL r114LatchEnabled(void) {
    const char *enabled = getenv("FGMETAL_R114_INPROCESS_LATCH");
    return enabled && strcmp(enabled, "1") == 0;
}

// R119: the legacy selector is gone. Its only remaining role -- choosing the
// historical per-round constant when no override is set -- now lives inside
// fgmetalResolveEvidenceDirectory(), so there is exactly one place that decides
// where diagnostics go. Callers use fgmetalEvidenceDirectory().
static BOOL r114ArmRequested(void) {
    return access(kR114ArmRequestPath, F_OK) == 0;
}

// Caller must hold stateLock. Renewal stays possible while the experiment is
// pending (arm request on disk, latch still IDLE) and while ARMED, so the
// window can never die before the latch gets a chance to arm or reserve.
static BOOL r114RenewalWantedLocked(void) {
    if (!r114LatchEnabled()) return NO;
    if (r114LatchState == R114LatchArmed) return YES;
    if (r114LatchState == R114LatchIdle) return access(kR114ArmRequestPath, F_OK) == 0;
    return NO;
}

static void r114MarkPairFailedLocked(const char *reason);

// ---------------------------------------------------------------------------
// R115 lifecycle helpers.
// ---------------------------------------------------------------------------
static BOOL r115ContinuousEnabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *continuous = getenv("FGMETAL_R115_CONTINUOUS");
        cached = (continuous && strcmp(continuous, "1") == 0 &&
                  r114LatchEnabled()) ? 1 : 0;
    }
    return cached == 1;
}

// Capture must be enabled or the reserved pair is never copied, no M can exist,
// and every tuple would end in the recovery timeout instead of a real verdict.
static BOOL r115CaptureEnabled(void) {
    const char *enabled = getenv("FGMETAL_R108_CAPTURE");
    return enabled && strcmp(enabled, "1") == 0;
}

static uint64_t r115MaxTupleCount(void) {    if (r115MaxTuples == 0) {
        const char *configured = getenv("FGMETAL_R115_MAX_TUPLES");
        uint64_t parsed = configured ? strtoull(configured, NULL, 10) : 0;
        r115MaxTuples = parsed > 0 ? parsed : 10;
    }
    return r115MaxTuples;
}

// ---------------------------------------------------------------------------
// R119 Phase 1 -- bounded diagnostic sample.
//
// R118 was a diagnostics-heavy build: per tuple it wrote a 5.6 MB source raw for
// A and for B, a 5.6 MB motion field and a 5.6 MB MetalFX readback, and it did a
// full CPU readback plus a per-4th-pixel luma sweep for each of them. Some of
// that lands inside the measured service window.
//
// The authoritative throughput run therefore keeps the full artefact set only
// for a small, deterministic sample of tuples -- the first, the middle and the
// last of the bounded budget -- and skips the artefact export for the rest.
// Nothing else is reduced: per-pair timing, the gate decision, the accounting
// counters and the WSI/display evidence stay enabled for every tuple, because
// those are the measurements the round is built on.
//
// FGMETAL_DIAG_SAMPLE=full restores the R118 behaviour exactly, so the
// diagnostic overhead can be quantified as the difference between the two
// configurations rather than asserted.
// ---------------------------------------------------------------------------
static BOOL r119FullDiagnosticsMode(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *mode = getenv("FGMETAL_DIAG_SAMPLE");
        cached = (mode && strcmp(mode, "full") == 0) ? 1 : 0;
    }
    return cached == 1;
}

// Whether the currently reserved tuple gets the expensive per-pair artefacts.
static BOOL r119DiagnosticSampleWanted;

static BOOL r119OrdinalIsSampled(uint64_t ordinal, uint64_t budget) {
    if (ordinal == 0) return NO;
    if (ordinal == 1) return YES;
    if (budget >= 2 && ordinal == (budget + 1) / 2) return YES;
    if (budget >= 1 && ordinal == budget) return YES;
    return NO;
}

// Called once per reservation, under no lock, before A's producer completes.
static void r119SelectDiagnosticSample(uint64_t ordinal) {
    const uint64_t budget = r115MaxTupleCount();
    const BOOL wanted = r119FullDiagnosticsMode() ||
        r119OrdinalIsSampled(ordinal, budget);
    pthread_mutex_lock(&stateLock);
    r119DiagnosticSampleWanted = wanted;
    pthread_mutex_unlock(&stateLock);
    logLine("R119_DIAG_SAMPLE tuple=%llu budget=%llu mode=%s fullArtefacts=%d policy=%s",
        (unsigned long long)ordinal, (unsigned long long)budget,
        r119FullDiagnosticsMode() ? "full" : "bounded", wanted ? 1 : 0,
        wanted ? "export-source-motion-and-metalfx-readbacks"
               : "timing-gate-accounting-wsi-only");
}

// Snapshot of the current tuple's decision. Read at capture time, which is on a
// different thread from the reservation.
static BOOL r119DiagnosticsWanted(void) {
    if (!fgmetalEvidenceDiagnosticsAllowed()) return NO;
    pthread_mutex_lock(&stateLock);
    const BOOL wanted = r119DiagnosticSampleWanted;
    pthread_mutex_unlock(&stateLock);
    return wanted;
}

// R119: which source sequences actually produced a verified raw artefact. The
// bounded policy means "wanted" is not the same as "written" -- a dark frame
// fails the luma verification even inside the sample -- so a log line that names
// a source raw must consult this ring rather than the policy alone.
#define kR119VerifiedRingSize 8
static uint64_t r119VerifiedSequences[kR119VerifiedRingSize];
static NSUInteger r119VerifiedNext;

static void r119NoteVerifiedSequence(uint64_t sequence) {
    if (!sequence) return;
    pthread_mutex_lock(&stateLock);
    r119VerifiedSequences[r119VerifiedNext] = sequence;
    r119VerifiedNext = (r119VerifiedNext + 1) % kR119VerifiedRingSize;
    pthread_mutex_unlock(&stateLock);
}

static BOOL r119SequenceVerified(uint64_t sequence) {
    if (!sequence) return NO;
    BOOL found = NO;
    pthread_mutex_lock(&stateLock);
    for (NSUInteger i = 0; i < kR119VerifiedRingSize; ++i) {
        if (r119VerifiedSequences[i] == sequence) { found = YES; break; }
    }
    pthread_mutex_unlock(&stateLock);
    return found;
}

// ---------------------------------------------------------------------------
// R116 critical-path helpers.
// ---------------------------------------------------------------------------

// Non-blocking M-then-B scheduling. When enabled, retained B is forwarded
// immediately after M's present call returns instead of waiting for M's
// presented callback. The bounded CAMetalLayer experiment
// (evidence/r116-criticalpath/presentation-ordering) showed the layer keeps two
// back-to-back presents on consecutive refreshes with no coalescing, so the
// visible A -> M -> B order is preserved while ~28 ms of callback wait leaves
// the critical path.
static BOOL r116EarlyBDispatchEnabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *flag = getenv("FGMETAL_R116_B_EARLY");
        cached = (flag && strcmp(flag, "1") == 0 && r115ContinuousEnabled()) ? 1 : 0;
    }
    return cached == 1;
}

// Short watchdog for the pathological "B committed but never presented" case.
//
// R115 measured the normal B producer-commit -> B present-hook interval over 61
// tuples: min 0.10 ms, p50 13.52, p90 15.00, p99 17.64, max 17.64 ms. A 150 ms
// bound is 8.5x the observed maximum and ~9 source-frame intervals, so it cannot
// fire on the healthy path, while it cuts the observed 4,211 ms stall by 28x.
static uint64_t r116CommittedBWatchdogNs(void) {
    static uint64_t cached = 0;
    if (cached == 0) {
        const char *configured = getenv("FGMETAL_R116_WATCHDOG_MS");
        uint64_t ms = configured ? strtoull(configured, NULL, 10) : 0;
        if (ms < 20) ms = 150;
        cached = ms * 1000000ULL;
    }
    return cached;
}

static const char *r115PhaseName(R115TuplePhase phase) {
    switch (phase) {
        case R115PhaseIdle: return "IDLE";
        case R115PhaseArmed: return "ARMED";
        case R115PhasePairReserved: return "PAIR_RESERVED";
        case R115PhaseFrontierClosing: return "FRONTIER_CLOSING";
        case R115PhaseBDeferred: return "B_DEFERRED";
        case R115PhaseProcessing: return "PROCESSING";
        case R115PhaseMPresenting: return "M_PRESENTING";
        case R115PhaseBForwarding: return "B_FORWARDING";
        case R115PhaseFallback: return "FALLBACK";
        case R115PhaseResetting: return "RESETTING";
    }
    return "UNKNOWN";
}

// Records one lifecycle transition. The phase is updated under stateLock and the
// line is emitted outside it, so the commit path never logs while holding it.
static void r115NotePhase(R115TuplePhase phase, const char *detail) {
    pthread_mutex_lock(&stateLock);
    R115TuplePhase previous = r115Phase;
    BOOL changed = previous != phase;
    if (changed) r115Phase = phase;
    uint64_t generation = r115Generation;
    uint64_t pairA = r115Timing.pairA;
    uint64_t pairB = r115Timing.pairB;
    pthread_mutex_unlock(&stateLock);
    if (!changed) return;
    logLine("R115_TUPLE_STATE generation=%llu from=%s to=%s pair=%llu->%llu detail=%s",
        (unsigned long long)generation, r115PhaseName(previous),
        r115PhaseName(phase), (unsigned long long)pairA,
        (unsigned long long)pairB, detail ?: "none");
}

// The generation is captured when a tuple arms and only advances when the tuple
// reaches a terminal, fully-reset state. A callback that carries an older
// generation can never mutate the current tuple.
static void r115BeginTimingLocked(uint64_t pairA, uint64_t pairB) {
    memset(&r115Timing, 0, sizeof(r115Timing));
    // R119: zero is a *valid* residual for these two, so the "never measured"
    // state must be distinct from it or a tuple that never reached the estimator
    // would be reported as a clock-domain mismatch. INT64_MIN is unreachable as a
    // real measurement because it would mean a 292-year interval.
    r115Timing.wakeMinusGpuEndNs = INT64_MIN;
    r115Timing.estimatorClockSkewNs = INT64_MIN;
    r115Timing.generation = r115Generation;
    r115Timing.pairA = pairA;
    r115Timing.pairB = pairB;
    r115Timing.leaksAtArm = r103FutureSourceLeaks;
    r115Timing.fallbackCountAtArm = r102FallbackCount;
    r115Timing.displayedCountAtArm = internalPresentationDisplayedCount;
    r115Timing.wsiDispatchesAtArm = r110WsiEventSequence;
    // R121: the span is only ever reused at the arm, which is the one instant
    // at which the previous tuple is provably terminal.
    r121SpanBegin(r115Timing.generation, pairA, pairB);
}

static BOOL r115PairCaptureSelectedLocked(uint64_t sequence) {
    if (!r115ContinuousEnabled()) return NO;
    if (r114LatchState == R114LatchArmed || r114LatchState == R114LatchReserved) {
        if (r114ReservedPairA && sequence == r114ReservedPairA) return YES;
        if (r114ReservedPairB && sequence == r114ReservedPairB) return YES;
    }
    // Provider warm-up: the first two consecutive frames after the start
    // request are captured once so MetalFX can be initialised before the first
    // tuple, exactly as the R114 window warm-up did.
    if (r115WarmupPairA && (sequence == r115WarmupPairA || sequence == r115WarmupPairB))
        return YES;
    return NO;
}

static void r114MarkPairFailed(const char *reason) {
    pthread_mutex_lock(&stateLock);
    BOOL wasReserved = r114LatchState == R114LatchReserved;
    uint64_t pairA = r114ReservedPairA;
    uint64_t pairB = r114ReservedPairB;
    r114MarkPairFailedLocked(reason);
    pthread_mutex_unlock(&stateLock);
    if (wasReserved) {
        logLine("R114_LATCH=FAILED pair=%llu->%llu reason=%s result=SAFE_SOURCE_FALLBACK rearm=%d",
            (unsigned long long)pairA, (unsigned long long)pairB,
            reason ?: "unspecified", r115ContinuousEnabled() ? 1 : 0);
    }
    // R115: a failure that never produced a deferred ticket has no forwarding
    // path to terminate it, so the lifecycle is reset here. When a ticket does
    // exist the forward path owns the reset and this call is a no-op.
    if (r115ContinuousEnabled() && wasReserved)
        r115ResetTuple("FAILED", reason ?: "pair-failed-without-ticket");
}

static void r114MarkPairFailedLocked(const char *reason) {
    (void)reason;
    if (r114LatchState != R114LatchReserved) return;
    r114LatchState = R114LatchFailed;
    // Drop the one-shot target so later SOURCE admissions are classified as
    // ordinary pre-frontier traffic instead of accumulating false leaks.
    if (r110TupleSourceBSequence == r114ReservedPairB)
        r110TupleSourceBSequence = 0;
}

static void r114MarkPairConsumedLocked(void) {
    if (r114LatchState != R114LatchReserved) return;
    r114LatchState = R114LatchConsumed;
}

// R115 reservation at arm time. The pair is the next two frames that have not
// been acquired yet, so A's acquisition (and therefore its WSI dispatch) always
// happens after the reservation and B is still on the uncommitted side of the
// accepted producer-commit seam.
static BOOL r115ReserveAtArmLocked(uint64_t currentSequence, uint64_t *outA,
                                   uint64_t *outB, const char **outSkip) {
    uint64_t pairA = currentSequence + 1;
    uint64_t pairB = currentSequence + 2;
    if (r114LatchState != R114LatchArmed) {
        if (outSkip) *outSkip = "latch-not-armed";
        return NO;
    }
    if (r110TupleSourceBSequence != 0) {
        if (outSkip) *outSkip = "target-already-set";
        return NO;
    }
    if (r103FrontierSpent || r102DeferredTicket ||
        r103SourceFrontierState != R103SourceFrontierOpen) {
        if (outSkip) *outSkip = "frontier-not-idle";
        return NO;
    }
    r110TupleSourceBSequence = pairB;
    r114ReservedPairA = pairA;
    r114ReservedPairB = pairB;
    r114ReservedAtNs = r105NowNs();
    r114LatchState = R114LatchReserved;
    r115BeginTimingLocked(pairA, pairB);
    r115Timing.armNs = r114ArmAtNs;
    r115Timing.reservedNs = r114ReservedAtNs;
    if (outA) *outA = pairA;
    if (outB) *outB = pairB;
    return YES;
}

// Called on the game thread right after the selected-layer acquisition lease is
// released. In R115 continuous mode the start file only permits the bounded run;
// after that the latch re-arms itself for every generation.
static void r114MaybeArm(uint64_t sequence, CAMetalLayer *layer) {
    if (!r114LatchEnabled() || !r110PresentationEnabled()) return;
    if (!isSelectedProcess() || !isSelectedLayer(layer)) return;
    if (!r115ContinuousEnabled() && !r114ArmRequested()) return;

    BOOL armed = NO;
    BOOL continuous = r115ContinuousEnabled();
    BOOL startRequested = continuous &&
        (r115RunStarted || access(kR115StartRequestPath, F_OK) == 0);
    BOOL warmupStarted = NO;
    uint64_t warmupA = 0, warmupB = 0;
    uint64_t start = 0, end = 0, warm = 0, runway = 0;
    uint64_t pairA = 0, pairB = 0;
    const char *skip = "none";
    // File I/O stays outside stateLock: the commit path can hold this lock while
    // draining the frontier.
    BOOL worldConfirmed = r110WorldSceneConfirmed();
    pthread_mutex_lock(&stateLock);
    if (r114LatchState == R114LatchIdle &&
        selectedHarnessLayer == (__bridge void *)layer) {
        runway = r108CaptureEndSequence > sequence
            ? r108CaptureEndSequence - sequence : 0;
        if (continuous) {
            // The bounded run targets a number of ACCEPTED tuples. Rejections are
            // still collected and reported, but they must not be able to consume
            // the whole budget before the operator has started moving, so a hard
            // attempt ceiling bounds the run independently.
            BOOL budgetLeft = !r115RunComplete &&
                r115AcceptedTuples < r115MaxTupleCount() &&
                r115AttemptedTuples < r115MaxTupleCount() * 3;
            BOOL providerReady = r114ProviderWarmCount > 0;
            BOOL warmupExpired = r115WarmupPairA != 0 &&
                sequence > r115WarmupPairB + 30;
            if (!startRequested) {
                skip = "start-not-requested";
            } else if (!budgetLeft) {
                skip = "tuple-budget-exhausted";
            } else if (!worldConfirmed) {
                skip = "world-scene-not-confirmed";
            } else if (!r115CaptureEnabled()) {
                skip = "capture-not-enabled";
            } else if (r102DeferredTicket || r103FrontierSpent ||
                       r103SourceFrontierState != R103SourceFrontierOpen ||
                       r103FrontierOperationsInFlight != 0) {
                skip = "frontier-not-idle";
            } else if (internalSlotState != R101InternalSlotEmpty ||
                       pendingInternalPresentationTexture || r110QualityWaitStarted) {
                skip = "internal-slot-not-empty";
            } else if (sequence < r115ArmCooldownUntilSequence) {
                skip = "rejection-cooldown";
            } else if (!providerReady && !warmupExpired) {
                if (r115WarmupPairA == 0) {
                    r115WarmupPairA = sequence;
                    r115WarmupPairB = sequence + 1;
                    warmupStarted = YES;
                    warmupA = sequence;
                    warmupB = sequence + 1;
                }
                skip = "provider-warmup-pending";
            } else {
                r114LatchState = R114LatchArmed;
                r114ArmAtNs = r105NowNs();
                r115RunStarted = YES;
                r115WarmupPairA = 0;
                r115WarmupPairB = 0;
                ++r115AttemptedTuples;
                warm = r114ProviderWarmCount;
                armed = r115ReserveAtArmLocked(sequence, &pairA, &pairB, &skip);
                if (!armed) {
                    // The reservation is the tuple; a failed arm must not consume
                    // the bounded budget or leave the latch ARMED.
                    r114LatchState = R114LatchIdle;
                    if (r115AttemptedTuples > 0) --r115AttemptedTuples;
                }
            }
        } else {
            BOOL ready = worldConfirmed && r114ProviderWarmCount > 0 &&
                r108CaptureTriggered && runway >= 8 &&
                !r102DeferredTicket && !r103FrontierSpent &&
                r103SourceFrontierState == R103SourceFrontierOpen &&
                r103FrontierOperationsInFlight == 0;
            if (ready) {
                r114LatchState = R114LatchArmed;
                r114ArmAtNs = r105NowNs();
                start = r108CaptureStartSequence;
                end = r108CaptureEndSequence;
                warm = r114ProviderWarmCount;
                armed = YES;
            }
        }
    } else if (continuous && r114LatchState != R114LatchIdle) {
        skip = "latch-not-idle";
    }
    uint64_t attempted = r115AttemptedTuples;
    uint64_t generation = r115Generation;
    pthread_mutex_unlock(&stateLock);

    if (warmupStarted) {
        logLine("R115_PROVIDER_WARMUP=ARMED pid=%d sequence=%llu..%llu reason=provider-not-warm providerWarmCount=%llu",
            getpid(), (unsigned long long)warmupA,
            (unsigned long long)warmupB, (unsigned long long)r114ProviderWarmCount);
        return;
    }
    if (!armed) {
        if (continuous && strcmp(skip, "start-not-requested") != 0 &&
            strcmp(skip, "tuple-budget-exhausted") != 0 &&
            strcmp(skip, "provider-warmup-pending") != 0 &&
            strcmp(skip, "rejection-cooldown") != 0 &&
            strcmp(skip, "latch-not-idle") != 0) {
            logLine("R115_ARM_SKIPPED sequence=%llu reason=%s attempts=%llu",
                (unsigned long long)sequence, skip, (unsigned long long)attempted);
        }
        return;
    }

    if (continuous) {
        (void)unlink(kR115StartRequestPath);
        // R119 Phase 1: the artefact budget for this tuple is decided once, at
        // reservation, before A's producer can complete.
        r119SelectDiagnosticSample(attempted);
        logLine("R115_TUPLE_ARMED pid=%d t_ns=%llu generation=%llu sequence=%llu pair=%llu->%llu layer=%p device=%p providerWarmCount=%llu worldSceneConfirmed=1 frontier=OPEN activeTicket=NONE attempt=%llu/%llu source=continuous-rearm",
            getpid(), (unsigned long long)r114ArmAtNs,
            (unsigned long long)generation, (unsigned long long)sequence,
            (unsigned long long)pairA, (unsigned long long)pairB,
            (__bridge void *)layer, (__bridge void *)layer.device,
            (unsigned long long)warm, (unsigned long long)attempted,
            (unsigned long long)r115MaxTupleCount());
        if (!r115ReadyLogged) {
            r115ReadyLogged = YES;
            logLine("R115_CONTINUOUS_RUN_READY pid=%d t_ns=%llu generation=%llu firstPair=%llu->%llu instruction=steady-physical-camera-rotation acceptedTarget=%llu",
                getpid(), (unsigned long long)r114ArmAtNs,
                (unsigned long long)generation, (unsigned long long)pairA,
                (unsigned long long)pairB, (unsigned long long)r115MaxTupleCount());
        }
        r115NotePhase(R115PhaseArmed, "arm");
        r115NotePhase(R115PhasePairReserved, "reserve-at-arm");
        // A reservation has no other watchdog: both recovery timers only exist
        // once a ticket or a closed frontier exists. If B never commits (game
        // paused, window unfocused, nextDrawable returns nil) the latch would sit
        // in PAIR_RESERVED forever and every later arm would be skipped.
        uint64_t watchdogGeneration = generation;
        uint64_t watchdogPairA = pairA, watchdogPairB = pairB;
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC),
                       dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            if (!r115ContinuousEnabled()) return;
            pthread_mutex_lock(&stateLock);
            // Precise on purpose: the watchdog only covers "B never reached the
            // producer commit", i.e. the frontier is still OPEN. Once the close
            // has happened the accepted 4 s close-recovery timer owns the tuple,
            // and a slow commit->present window must not be destroyed here.
            BOOL expired = r114LatchState == R114LatchReserved &&
                r115Generation == watchdogGeneration && !r102DeferredTicket &&
                r103SourceFrontierState == R103SourceFrontierOpen &&
                !r103FrontierSpent;
            pthread_mutex_unlock(&stateLock);
            if (!expired) return;
            logLine("R115_RESERVATION_WATCHDOG=EXPIRED generation=%llu pair=%llu->%llu reason=B-never-reached-producer-commit action=RESET",
                (unsigned long long)watchdogGeneration,
                (unsigned long long)watchdogPairA,
                (unsigned long long)watchdogPairB);
            r115ResetTuple("FAILED", "reservation-never-reached-commit");
        });
        return;
    }

    (void)unlink(kR114ArmRequestPath);
    logLine("R114_NEXT_MOVING_PAIR_ARMED pid=%d t_ns=%llu sequence=%llu layer=%p device=%p captureWindow=%llu..%llu runway=%llu providerWarmCount=%llu worldSceneConfirmed=1 frontier=OPEN activeTicket=NONE state=ARMED source=one-shot-arm-file",
        getpid(), (unsigned long long)r114ArmAtNs,
        (unsigned long long)sequence, (__bridge void *)layer,
        (__bridge void *)layer.device,
        (unsigned long long)start, (unsigned long long)end,
        (unsigned long long)runway, (unsigned long long)warm);
    logLine("R114_MANUAL_MOTION_READY pid=%d t_ns=%llu instruction=physical-camera-rotation selection=in-process-next-eligible-moving-pair externalPairSelection=NONE",
        getpid(), (unsigned long long)r114ArmAtNs);
}

static void r114NoteProviderWarm(uint64_t pairA, uint64_t pairB,
                                 uint64_t internalPresentId) {
    if (!r114LatchEnabled()) return;
    pthread_mutex_lock(&stateLock);
    ++r114ProviderWarmCount;
    r114ProviderWarmAtNs = r105NowNs();
    uint64_t count = r114ProviderWarmCount;
    uint64_t t = r114ProviderWarmAtNs;
    R114LatchState state = r114LatchState;
    pthread_mutex_unlock(&stateLock);
    if (count == 1 || state == R114LatchIdle) {
        logLine("R114_PROVIDER_WARM pid=%d t_ns=%llu completedPair=%llu->%llu internalPresentId=%llu warmCount=%llu latchState=%u",
            getpid(), (unsigned long long)t,
            (unsigned long long)pairA, (unsigned long long)pairB,
            (unsigned long long)internalPresentId,
            (unsigned long long)count, (unsigned)state);
    }
}

// The reserved pair must be the next pair that has not committed yet, so that
// the accepted commit seam can still close the frontier for B and A's WSI
// dispatch can still be attributed to the tuple.
static void r114ReserveNextPair(uint64_t motionPairA, uint64_t motionPairB,
                                double activeFraction) {
    if (!r114LatchEnabled()) return;

    BOOL reserved = NO;
    uint64_t pairA = 0, pairB = 0, lead = 0;
    uint64_t lastCommitted = 0, lastDispatched = 0, start = 0, end = 0;
    const char *skip = "none";

    pthread_mutex_lock(&stateLock);
    ++r114ReservationAttempts;
    lastCommitted = r114LastCommittedSourceSequence;
    lastDispatched = r114LastDispatchedSourceSequence;
    start = r108CaptureStartSequence;
    end = r108CaptureEndSequence;
    pairA = lastCommitted + 1;
    pairB = pairA + 1;
    if (r114LatchState != R114LatchArmed) {
        skip = "latch-not-armed";
    } else if (pairA <= lastDispatched) {
        skip = "pair-a-already-dispatched";
    } else if (!r108CaptureTriggered || pairA <= start || pairB + 8 > end) {
        skip = "outside-capture-window";
    } else if (r103FrontierSpent || r102DeferredTicket ||
               r103SourceFrontierState != R103SourceFrontierOpen) {
        skip = "frontier-not-idle";
    } else if (r110TupleSourceBSequence != 0) {
        skip = "target-already-set";
    } else {
        r110TupleSourceBSequence = pairB;
        r114ReservedPairA = pairA;
        r114ReservedPairB = pairB;
        r114ReservedAtNs = r105NowNs();
        r114LatchState = R114LatchReserved;
        lead = pairA > lastCommitted ? pairA - lastCommitted : 0;
        reserved = YES;
    }
    if (!reserved) ++r114ReservationSkips;
    pthread_mutex_unlock(&stateLock);

    if (reserved) {
        logLine("R114_NEXT_PAIR_RESERVED t_ns=%llu pair=%llu->%llu motionEvidencePair=%llu->%llu activeFraction=%.6f lastCommittedSourceSequence=%llu lastDispatchedSourceSequence=%llu leadFrames=%llu captureWindow=%llu..%llu state=RESERVED externalPairSelection=NONE",
            (unsigned long long)r114ReservedAtNs,
            (unsigned long long)pairA, (unsigned long long)pairB,
            (unsigned long long)motionPairA, (unsigned long long)motionPairB,
            activeFraction,
            (unsigned long long)lastCommitted,
            (unsigned long long)lastDispatched,
            (unsigned long long)lead,
            (unsigned long long)start, (unsigned long long)end);
    } else {
        logLine("R114_NEXT_PAIR_RESERVE_SKIPPED pairCandidate=%llu->%llu motionEvidencePair=%llu->%llu lastCommittedSourceSequence=%llu lastDispatchedSourceSequence=%llu reason=%s attempts=%llu skips=%llu",
            (unsigned long long)pairA, (unsigned long long)pairB,
            (unsigned long long)motionPairA, (unsigned long long)motionPairB,
            (unsigned long long)lastCommitted,
            (unsigned long long)lastDispatched, skip,
            (unsigned long long)r114ReservationAttempts,
            (unsigned long long)r114ReservationSkips);
    }
}

static void r114NoteMotionObservation(uint64_t pairA, uint64_t pairB,
                                      double activeFraction) {
    pthread_mutex_lock(&stateLock);
    BOOL armed = r114LatchState == R114LatchArmed;
    BOOL logIt = armed && r114MotionObservationLogs < 12;
    if (armed) {
        r114MotionObservedAtNs = r105NowNs();
        r114MotionObservedPairA = pairA;
        r114MotionObservedPairB = pairB;
        r114MotionObservedFraction = activeFraction;
        if (logIt) ++r114MotionObservationLogs;
    }
    uint64_t t = r114MotionObservedAtNs;
    uint64_t logs = r114MotionObservationLogs;
    pthread_mutex_unlock(&stateLock);
    if (!armed) return;
    if (logIt) {
        logLine("R114_MOTION_OBSERVED t_ns=%llu pair=%llu->%llu activeFraction=%.6f source=accepted-r110-estimator observation=%llu",
            (unsigned long long)t, (unsigned long long)pairA,
            (unsigned long long)pairB, activeFraction, (unsigned long long)logs);
    }
    r114ReserveNextPair(pairA, pairB, activeFraction);
}


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
    if (r115ContinuousEnabled()) {
        pthread_mutex_lock(&stateLock);
        if (r115Timing.pairA == sequence)
            r115Timing.aAcquiredNs = acquiredAtNs;
        if (r115Timing.pairB == sequence)
            r115Timing.bAcquiredNs = acquiredAtNs;
        pthread_mutex_unlock(&stateLock);
    }
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

// R120 acquisition pacer. Declared here because the acquisition hook is the
// first user; the definition sits with the other R120 pacer code.
static void r120PaceSourceAcquire(uint64_t callId);

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
            // R120 drawable-pool capacity. The deferred-B pipeline holds SOURCE B's
            // drawable across the generation window while INTERNAL M needs one of
            // its own, so the pool must supply B, M and the next SOURCE C at once.
            // Pool depth is therefore a hard deadlock bound, not a tuning knob, and
            // it must be measured rather than assumed. Read-only diagnostics.
            logLine("R120_DRAWABLE_POOL pid=%d maximumDrawableCount=%lu allowsNextDrawableTimeout=%d "
                    "deferredBOccupies=1 internalMOccupies=1 concurrentDemand=3 poolSufficient=%d",
                getpid(), (unsigned long)layer.maximumDrawableCount,
                layer.allowsNextDrawableTimeout ? 1 : 0,
                layer.maximumDrawableCount >= 3 ? 1 : 0);
        }
        selected = selectedHarnessLayer == (__bridge void *)layer;
        pthread_mutex_unlock(&stateLock);
    }

    // R120: pace SOURCE frame ACQUISITION -- the frame boundary, not the present
    // path. Two measured placements were wrong before this one:
    //
    //  run-02, wait at the TOP of present(): A sat inside present() while B's
    //  producer committed, closing the R103 frontier for pair (A,B) ahead of A's
    //  admission. A blocked on a frontier that could only reopen once B's present
    //  was deferred, and B's present could not begin until A returned. Circular
    //  wait broken only by the 150 ms watchdog: accepted=0 of 30 attempts.
    //
    //  run-03, wait at the END of present(): deadlock gone, but A's present then
    //  held the render thread a full period before returning, so B's present hook
    //  arrived 26.2 ms after B committed. M generation cannot start before B's
    //  present hook, so every tuple paid that as dead time -- 49.6 ms from B's
    //  completion to M's dispatch against ~23.4 ms of real generation work.
    //
    // Blocking acquisition bounds the SOURCE frame rate at its source, touches no
    // present, no frontier and no tuple state, and leaves the R116-validated
    // display path byte-for-byte unchanged.
    if (selected) r120PaceSourceAcquire(0);

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
        // R121: this acquisition is a SOURCE frame. The drawable is live until
        // its own presented handler fires. The most recent SOURCE nextDrawable
        // is republished so M's acquisition can be compared against the
        // pressure the game was under at the same instant.
        r121NoteSourceDrawableAcquired();
        if (orderingReturnNs) {
            __atomic_store_n(&r121LastSourceNdEntryNs, orderingEntryNs, __ATOMIC_RELAXED);
            __atomic_store_n(&r121LastSourceNdReturnNs, orderingReturnNs, __ATOMIC_RELAXED);
        }
        installDrawablePresentationHooks(drawable);
        retainDrawableForCorrelation(drawable);
    }
    if (selected && r110PresentationEnabled())
        r103EndSourceOperation(acquisitionLease, "nextDrawable", frontierCallId);
    if (selected && drawable && r114LatchEnabled())
        r114MaybeArm(drawableSequence, layer);
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
        // R115: a frontier opened without a ticket is a terminal fallback with no
        // forwarding path, so the lifecycle is reset here to keep re-arming.
        if (r115ContinuousEnabled())
            r115ResetTuple("FAILED", reason ?: "source-frontier-recovery-bound");
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
    // R115: the bounded capture window is replaced by the reserved pair itself.
    // A pair is capturable exactly when the lifecycle reserved it and B is that
    // pair's B, so no window can expire underneath a live tuple.
    BOOL captureBound = r115ContinuousEnabled()
        ? (r114LatchState == R114LatchReserved && sequence == r114ReservedPairB)
        : (r108CaptureTriggered &&
           sequence > r108CaptureStartSequence && sequence <= r108CaptureEndSequence);
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

    if (r114LatchEnabled()) {
        uint64_t reservedAt = 0;
        BOOL reservedHere = NO;
        pthread_mutex_lock(&stateLock);
        if (r114LatchState == R114LatchReserved && r114ReservedPairB == sequence) {
            reservedHere = YES;
            reservedAt = r114ReservedAtNs;
        }
        pthread_mutex_unlock(&stateLock);
        if (reservedHere) {
            uint64_t nowNs = r105NowNs();
            logLine("R114_FRONTIER_CLOSE_AT_COMMIT t_ns=%llu pair=%llu->%llu ticketId=%llu producerCommandBuffer=%p producerStatus=%lu reserveLeadNs=%llu reservedBeforeBPresent=1 origin=SOURCE",
                (unsigned long long)nowNs, (unsigned long long)(sequence - 1),
                (unsigned long long)sequence, (unsigned long long)ticketId,
                (__bridge void *)producer, (unsigned long)producer.status,
                (unsigned long long)(nowNs > reservedAt ? nowNs - reservedAt : 0));
            if (r115ContinuousEnabled()) {
                pthread_mutex_lock(&stateLock);
                r115Timing.bCommitNs = nowNs;
                pthread_mutex_unlock(&stateLock);
                r115NotePhase(R115PhaseFrontierClosing, "producer-commit");
            }
        }
    }

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
    // R116: the frontier is now CLOSED for a reserved pair whose B producer has
    // committed. If B's present hook never arrives there is no deferred ticket,
    // and R115 waited the full 4 s close-recovery bound - a measured 4,211 ms
    // stall. The short watchdog below owns that exact pathology.
    if (r115ContinuousEnabled()) {
        ++r116WatchdogArmed;
        uint64_t boundNs = r116CommittedBWatchdogNs();
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)boundNs),
                       dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            r116WatchdogCommittedBWithoutTicket(ticketId, sequence - 1, sequence);
        });
    }
    uint64_t closeRecoverySeconds = r115ContinuousEnabled() ? 4 : 20;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)closeRecoverySeconds * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r103RecoverFrontier(ticketId, "source-frontier-recovery-bound");
    });
}


// ---------------------------------------------------------------------------
// R116 short watchdog: frontier CLOSED for a reserved pair, B's producer already
// committed, but no deferred SOURCE ticket exists and B's present hook has not
// arrived.
//
// This is a distinct pathology from a valid deferred ticket (which has its own
// 4 s bound) and from an active M display. R115 let it run to the 4 s close
// recovery, which produced a measured 4,211 ms visible stall.
//
// The action is deliberately narrow: abort the tuple, reopen the frontier, clear
// the reservation and reset the lifecycle. It never presents B on the tuple's
// behalf and never dispatches INTERNAL M, because neither can be justified
// without B's present hook.
static void r116WatchdogCommittedBWithoutTicket(uint64_t ticketId,
                                                uint64_t pairA,
                                                uint64_t pairB) {
    BOOL fire = NO;
    uint64_t boundNs = r116CommittedBWatchdogNs();
    BOOL hookEntered = NO;
    pthread_mutex_lock(&stateLock);
    if (!r103FrontierSpent &&
        r103SourceFrontierState == R103SourceFrontierClosed &&
        r103FrontierTicketId == ticketId &&
        r103FrontierPairA == pairA && r103FrontierPairB == pairB &&
        r102DeferredTicket == nil &&
        r114LatchState == R114LatchReserved && r114ReservedPairB == pairB) {
        // No hook-entry guard here. B's hook always enters *after* the frontier
        // closes, so any age test against the bound is satisfied by construction
        // and would suppress the watchdog for exactly the case it exists for: a
        // hook that entered and then rejected without recovering the frontier.
        //
        // The narrow window where the hook has entered and is about to publish is
        // safe: r102TryDeferSourcePresent re-checks frontier readiness under
        // stateLock immediately before publishing, so a tuple aborted here makes
        // it take its own rejection path and B is presented normally.
        hookEntered = (r116BHookEnteredPairB == pairB);
        ++r116WatchdogFired;
        fire = YES;
    }
    pthread_mutex_unlock(&stateLock);

    if (!fire) {
        if (r115ContinuousEnabled()) {
            logLine("R116_COMMITTED_B_NO_PRESENT_WATCHDOG=EXPIRED_NOOP ticketId=%llu pair=%llu->%llu reason=tuple-already-terminal-or-reowned",
                (unsigned long long)ticketId, (unsigned long long)pairA,
                (unsigned long long)pairB);
        }
        return;
    }

    logLine("R116_COMMITTED_B_NO_PRESENT_WATCHDOG=FIRED ticketId=%llu pair=%llu->%llu boundMs=%llu action=ABORT_TUPLE_REOPEN_FRONTIER frontierWasClosed=1 deferredTicket=0 internalMDispatched=0 bFabricated=0 hookEnteredForPair=%d",
        (unsigned long long)ticketId, (unsigned long long)pairA,
        (unsigned long long)pairB, (unsigned long long)(boundNs / 1000000ULL),
        hookEntered ? 1 : 0);
    r114MarkPairFailed("committed-b-no-present-watchdog");
    // The reset owns the frontier mutation: it clears the reservation, the target
    // sequence and the Spent flag in one critical section. Mutating the frontier
    // here first would leave it Open+Spent if the reset declined.
    r115ResetTuple("FAILED", "committed-b-no-present-watchdog");

    // Belt and braces. If the reset declined (a ticket was published concurrently,
    // or the lifecycle was already Idle), force the frontier open only when
    // nothing owns it. Leaving it Closed here would restore the 4 s stall.
    pthread_mutex_lock(&stateLock);
    BOOL stillStuck = !r103FrontierSpent &&
        r103SourceFrontierState == R103SourceFrontierClosed &&
        r103FrontierTicketId == ticketId && r102DeferredTicket == nil;
    if (stillStuck) {
        r103SourceFrontierState = R103SourceFrontierOpen;
        r103FrontierSpent = NO;
        r103ClearFrontierLocked();
        pthread_cond_broadcast(&presentationReadyCondition);
    }
    pthread_mutex_unlock(&stateLock);
    if (stillStuck) {
        logLine("R116_COMMITTED_B_NO_PRESENT_WATCHDOG=FORCE_OPEN ticketId=%llu pair=%llu->%llu reason=reset-declined-and-frontier-unowned",
            (unsigned long long)ticketId, (unsigned long long)pairA,
            (unsigned long long)pairB);
    }
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

// ---------------------------------------------------------------------------
// R115 terminal reset. This is the whole re-arm contract in one place.
//
// A tuple may only reach IDLE after every artefact of the previous generation is
// proven terminal: no deferred ticket, no pending internal frame, no gate
// verdict, no frontier ownership, no target identity. The generation then
// advances, so any callback that outlived the tuple fails its generation check
// instead of mutating the next one.
// ---------------------------------------------------------------------------
// Emits the R121 decomposition for one tuple. Called from r115LogTiming, i.e.
// after the tuple is terminal, so no part of this function runs inside the
// measured window and no diagnostic export can perturb the numbers it reports.
//
// Two lines, not one: the logLine body buffer is 1600 bytes and a single line
// carrying 20 named deltas plus the drawable evidence would truncate silently.
static void r121LogSpan(const R115TupleTiming *timing, const char *outcome,
                        const char *reason) {
    if (!timing || timing->generation == 0) return;
    if (timing->generation != r121Span.generation) {
        logLine("R121_B2M_CHAIN=SKIPPED generation=%llu spanGeneration=%llu reason=generation-mismatch",
            (unsigned long long)timing->generation,
            (unsigned long long)r121Span.generation);
        return;
    }
    static const R121Slot kOrder[21] = {
        R121SlotBHook, R121SlotSrcCaptureEntry, R121SlotSrcCopyEncoded,
        R121SlotSrcCopyCommit, R121SlotSrcCopyWake, R121SlotTaskEnqueue,
        R121SlotTaskStart, R121SlotEstimatorStart, R121SlotEstimatorEnd,
        R121SlotMetalFXStart, R121SlotMetalFXEnd, R121SlotGateVerdict,
        R121SlotInternalAcquireReq, R121SlotInternalNdEnter,
        R121SlotInternalNdReturn, R121SlotInternalAcquired,
        R121SlotMCopyEncodeStart, R121SlotMCopyCommit, R121SlotMCopyWake,
        R121SlotMWsiAlloc, R121SlotMWsiDispatch,
    };
    int64_t delta[20];
    int linksPresent = 0, linksMissing = 0, orderViolations = 0;
    int64_t sum = 0;
    for (int i = 0; i < 20; ++i) {
        const R121Slot a = kOrder[i], b = kOrder[i + 1];
        if (!r121Span.have[a] || !r121Span.have[b]) {
            delta[i] = 0;
            ++linksMissing;
            continue;
        }
        ++linksPresent;
        delta[i] = (int64_t)r121Span.t[b] - (int64_t)r121Span.t[a];
        if (delta[i] < 0) ++orderViolations;
        sum += delta[i];
    }
    const int64_t headline = timing->mDispatchNs && timing->bPresentHookNs
        ? (int64_t)timing->mDispatchNs - (int64_t)timing->bPresentHookNs : 0;
    const int64_t residual = sum - headline;

    logLine("R121_B2M_CHAIN generation=%llu pair=%llu->%llu outcome=%s reason=%s "
            "headlineNs=%lld chainSumNs=%lld residualNs=%lld links=%d/20 missing=%d "
            "orderViolations=%d marks=%llu "
            "hookToSrcCaptureNs=%lld srcCaptureToCopyEncodedNs=%lld "
            "copyEncodedToCopyCommitNs=%lld copyCommitToCopyWakeNs=%lld "
            "copyWakeToTaskEnqueueNs=%lld taskEnqueueToTaskStartNs=%lld "
            "taskStartToEstimatorStartNs=%lld estimatorStartToEstimatorEndNs=%lld "
            "estimatorEndToMetalFXStartNs=%lld metalFXStartToMetalFXEndNs=%lld "
            "metalFXEndToGateVerdictNs=%lld gateVerdictToInternalAcquireReqNs=%lld "
            "internalAcquireReqToNdEnterNs=%lld internalNdEnterToNdReturnNs=%lld "
            "internalNdReturnToAcquiredNs=%lld internalAcquiredToMCopyEncodeNs=%lld "
            "mCopyEncodeToCommitNs=%lld mCopyCommitToWakeNs=%lld "
            "mCopyWakeToMWsiAllocNs=%lld mWsiAllocToDispatchNs=%lld",
        (unsigned long long)timing->generation,
        (unsigned long long)timing->pairA, (unsigned long long)timing->pairB,
        outcome ?: "UNKNOWN", reason ?: "none",
        (long long)headline, (long long)sum, (long long)residual,
        linksPresent, linksMissing, orderViolations,
        (unsigned long long)__atomic_load_n(&r121MarkCount, __ATOMIC_RELAXED),
        (long long)delta[0], (long long)delta[1], (long long)delta[2],
        (long long)delta[3], (long long)delta[4], (long long)delta[5],
        (long long)delta[6], (long long)delta[7], (long long)delta[8],
        (long long)delta[9], (long long)delta[10], (long long)delta[11],
        (long long)delta[12], (long long)delta[13], (long long)delta[14],
        (long long)delta[15], (long long)delta[16], (long long)delta[17],
        (long long)delta[18], (long long)delta[19]);

    // Copy closures. GPUStartTime/GPUEndTime live on the CACurrentMediaTime
    // base, NOT CLOCK_MONOTONIC, so every GPU subtraction here is performed
    // entirely inside the media domain: media-sample(commit) -> GPUStart ->
    // GPUEnd -> media-sample(wake). The monotonic boundary instants are used
    // for exactly one thing -- publishing the difference between the media
    // duration and the monotonic duration of the same copy. That difference is
    // the real cross-clock validation; if the two domains are compatible it is
    // sub-millisecond, and if they are not it is hundreds of seconds.
    int64_t srcCommitToGpuStart = 0, srcGpuExec = 0, srcGpuEndToWake = 0;
    int64_t srcCommitToWake = 0, srcMediaDuration = 0, srcClockSkew = 0;
    if (r121Span.mediaSrcCopyCommit > 0.0 && r121Span.srcCopyGpuStartNs) {
        srcCommitToGpuStart = (int64_t)r121Span.srcCopyGpuStartNs
            - fgmetalMediaToNs(r121Span.mediaSrcCopyCommit);
        srcGpuExec = (int64_t)r121Span.srcCopyGpuEndNs
            - (int64_t)r121Span.srcCopyGpuStartNs;
        if (r121Span.mediaSrcCopyWake > 0.0) {
            srcGpuEndToWake = fgmetalMediaToNs(r121Span.mediaSrcCopyWake)
                - (int64_t)r121Span.srcCopyGpuEndNs;
            srcMediaDuration = srcCommitToGpuStart + srcGpuExec + srcGpuEndToWake;
        }
        if (r121Span.have[R121SlotSrcCopyWake]) {
            srcCommitToWake = (int64_t)r121Span.t[R121SlotSrcCopyWake]
                - (int64_t)r121Span.t[R121SlotSrcCopyCommit];
        }
        srcClockSkew = srcMediaDuration - srcCommitToWake;
    }
    int64_t mCommitToGpuStart = 0, mGpuExec = 0, mGpuEndToWake = 0;
    int64_t mCommitToWake = 0, mMediaDuration = 0, mClockSkew = 0;
    if (r121Span.mediaMCopyCommit > 0.0 && r121Span.mCopyGpuStartNs) {
        mCommitToGpuStart = (int64_t)r121Span.mCopyGpuStartNs
            - fgmetalMediaToNs(r121Span.mediaMCopyCommit);
        mGpuExec = (int64_t)r121Span.mCopyGpuEndNs
            - (int64_t)r121Span.mCopyGpuStartNs;
        if (r121Span.mediaMCopyWake > 0.0) {
            mGpuEndToWake = fgmetalMediaToNs(r121Span.mediaMCopyWake)
                - (int64_t)r121Span.mCopyGpuEndNs;
            mMediaDuration = mCommitToGpuStart + mGpuExec + mGpuEndToWake;
        }
        if (r121Span.have[R121SlotMCopyWake]) {
            mCommitToWake = (int64_t)r121Span.t[R121SlotMCopyWake]
                - (int64_t)r121Span.t[R121SlotMCopyCommit];
        }
        mClockSkew = mMediaDuration - mCommitToWake;
    }
    int64_t presentEnter = 0, presentCall = 0;
    if (r121Span.have[R121SlotInternalPresentEnter]) {
        presentEnter = (int64_t)r121Span.t[R121SlotInternalPresentEnter]
            - (int64_t)r121Span.t[R121SlotMWsiDispatch];
        if (r121Span.have[R121SlotInternalPresentReturn])
            presentCall = (int64_t)r121Span.t[R121SlotInternalPresentReturn]
                - (int64_t)r121Span.t[R121SlotInternalPresentEnter];
    }
    int64_t sourceNdBlock = 0;
    if (r121Span.sourceNdEntryNs && r121Span.sourceNdReturnNs)
        sourceNdBlock = (int64_t)r121Span.sourceNdReturnNs
            - (int64_t)r121Span.sourceNdEntryNs;

    logLine("R121_B2M_COPY generation=%llu pair=%llu->%llu "
            "srcCopyCommitToGpuStartNs=%lld srcCopyGpuExecNs=%lld "
            "srcCopyGpuEndToWakeNs=%lld srcCopyCommitToWakeNs=%lld "
            "srcCopyMediaDurationNs=%lld srcCopyClockSkewNs=%lld "
            "mCopyCommitToGpuStartNs=%lld mCopyGpuExecNs=%lld "
            "mCopyGpuEndToWakeNs=%lld mCopyCommitToWakeNs=%lld "
            "mCopyMediaDurationNs=%lld mCopyClockSkewNs=%lld "
            "dispatchToPresentEnterNs=%lld presentCallNs=%lld "
            "poolMaximumDrawables=%llu deferredBPresent=%d "
            "liveSourceDrawables=%llu liveInternalDrawables=%llu "
            "liveSourceMax=%llu liveInternalMax=%llu "
            "sourceAcquiredTotal=%llu sourceReleasedTotal=%llu "
            "sourceDeferredTotal=%llu "
            "internalAcquiredTotal=%llu internalReleasedTotal=%llu "
            "internalSlotState=%d frontierState=%d "
            "sourceNdEntryNs=%llu sourceNdReturnNs=%llu sourceNdBlockNs=%lld "
            "internalNdEnterNs=%llu internalNdReturnNs=%llu "
            "bHookNs=%llu mWsiDispatchNs=%llu",
        (unsigned long long)timing->generation,
        (unsigned long long)timing->pairA, (unsigned long long)timing->pairB,
        (long long)srcCommitToGpuStart, (long long)srcGpuExec,
        (long long)srcGpuEndToWake, (long long)srcCommitToWake,
        (long long)srcMediaDuration, (long long)srcClockSkew,
        (long long)mCommitToGpuStart, (long long)mGpuExec,
        (long long)mGpuEndToWake, (long long)mCommitToWake,
        (long long)mMediaDuration, (long long)mClockSkew,
        (long long)presentEnter, (long long)presentCall,
        (unsigned long long)r121Span.poolMaximumDrawables,
        (int)r121Span.deferredBPresent,
        (unsigned long long)r121Span.liveSourceDrawables,
        (unsigned long long)r121Span.liveInternalDrawables,
        (unsigned long long)__atomic_load_n(&r121SourceOutstandingMax, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121InternalOutstandingMax, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121SourceAcquiredTotal, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121SourceReleasedTotal, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121SourceDeferredTotal, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121InternalAcquiredTotal, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r121InternalReleasedTotal, __ATOMIC_RELAXED),
        (int)r121Span.internalSlotState, (int)r121Span.frontierState,
        (unsigned long long)r121Span.sourceNdEntryNs,
        (unsigned long long)r121Span.sourceNdReturnNs,
        (long long)sourceNdBlock,
        (unsigned long long)r121Span.t[R121SlotInternalNdEnter],
        (unsigned long long)r121Span.t[R121SlotInternalNdReturn],
        (unsigned long long)r121Span.t[R121SlotBHook],
        (unsigned long long)r121Span.t[R121SlotMWsiDispatch]);
}

static void r115LogTiming(const R115TupleTiming *timing, const char *outcome,
                          const char *reason) {
    if (!timing || timing->generation == 0) return;
    uint64_t bHold = timing->bForwardBeginNs && timing->bPresentHookNs
        ? timing->bForwardBeginNs - timing->bPresentHookNs : 0;
    uint64_t producerToMWsi = timing->mDispatchNs && timing->bProducerCompletedNs
        ? timing->mDispatchNs - timing->bProducerCompletedNs : 0;
    uint64_t producerToMDisplay = timing->mDisplayedNs && timing->bProducerCompletedNs
        ? timing->mDisplayedNs - timing->bProducerCompletedNs : 0;
    uint64_t mDisplayToB = timing->bForwardBeginNs && timing->mDisplayedNs
        ? timing->bForwardBeginNs - timing->mDisplayedNs : 0;
    uint64_t aDisplayToMDisplay = timing->mDisplayedNs && timing->aDisplayedNs
        ? timing->mDisplayedNs - timing->aDisplayedNs : 0;
    uint64_t total = timing->frontierOpenNs && timing->armNs
        ? timing->frontierOpenNs - timing->armNs : 0;
    uint64_t estimatorNs = timing->estimatorEndNs && timing->estimatorStartNs
        ? timing->estimatorEndNs - timing->estimatorStartNs : 0;
    uint64_t metalfxNs = timing->metalfxEndNs && timing->metalfxStartNs
        ? timing->metalfxEndNs - timing->metalfxStartNs : 0;
    uint64_t gateNs = timing->gateEndNs && timing->gateStartNs
        ? timing->gateEndNs - timing->gateStartNs : 0;
    uint64_t commitToPresentHook = timing->bPresentHookNs && timing->bCommitNs
        ? timing->bPresentHookNs - timing->bCommitNs : 0;
    logLine("R115_TUPLE_TIMING generation=%llu pair=%llu->%llu outcome=%s reason=%s "
            "armToReserveNs=%llu reserveToBCommitNs=%llu bCommitToPresentHookNs=%llu "
            "estimatorNs=%llu metalfxNs=%llu gateNs=%llu "
            "bHoldNs=%llu producerCompleteToMWsiNs=%llu producerCompleteToMDisplayNs=%llu "
            "mDisplayToBDispatchNs=%llu aDisplayToMDisplayNs=%llu "
            "bPresentHookToMWsiNs=%llu tupleTotalNs=%llu",
        (unsigned long long)timing->generation,
        (unsigned long long)timing->pairA, (unsigned long long)timing->pairB,
        outcome ?: "UNKNOWN", reason ?: "none",
        (unsigned long long)(timing->reservedNs && timing->armNs
            ? timing->reservedNs - timing->armNs : 0),
        (unsigned long long)(timing->bCommitNs && timing->reservedNs
            ? timing->bCommitNs - timing->reservedNs : 0),
        (unsigned long long)commitToPresentHook,
        (unsigned long long)estimatorNs, (unsigned long long)metalfxNs,
        (unsigned long long)gateNs, (unsigned long long)bHold,
        (unsigned long long)producerToMWsi,
        (unsigned long long)producerToMDisplay,
        (unsigned long long)mDisplayToB,
        (unsigned long long)aDisplayToMDisplay,
        (unsigned long long)(timing->mDispatchNs && timing->bPresentHookNs
            ? timing->mDispatchNs - timing->bPresentHookNs : 0),
        (unsigned long long)total);
}

// Emits the bounded-run summary exactly once. Safe to call from any thread and
// from either terminal path (a synchronous reset or a deferred display verdict).
// It refuses to fire while a tuple is still waiting for its display evidence, so
// the run can never be declared complete with an unresolved tuple.
static void r116MaybeEmitRunSummary(void) {
    uint64_t attempted = 0, accepted = 0, fallbacks = 0, resets = 0, postSeq = 0;
    BOOL emit = NO;
    pthread_mutex_lock(&stateLock);
    attempted = r115AttemptedTuples;
    accepted = r115AcceptedTuples;
    fallbacks = r115FallbackTuples;
    resets = r115ResetCount;
    if (!r115SummaryLogged && r116PendingDisplayTuples == 0 &&
        (accepted >= r115MaxTupleCount() || attempted >= r115MaxTupleCount() * 3)) {
        r115SummaryLogged = YES;
        r115RunComplete = YES;
        r115PostRunStartSequence = drawableSequence;
        postSeq = r115PostRunStartSequence;
        emit = YES;
    }
    pthread_mutex_unlock(&stateLock);
    if (!emit) return;
    logLine("R115_CONTINUOUS_RUN=COMPLETE attempts=%llu accepted=%llu fallback=%llu sourceLeaksWhileClosed=%llu staleCallbacks=%llu duplicateMDispatch=%llu resets=%llu postRunStartSequence=%llu acceptanceRatio=%.3f",
        (unsigned long long)attempted, (unsigned long long)accepted,
        (unsigned long long)fallbacks,
        (unsigned long long)r114SourceLeaksWhileClosed,
        (unsigned long long)r115StaleCallbackRejections,
        (unsigned long long)r102DuplicateInternalDispatchCount,
        (unsigned long long)resets, (unsigned long long)postSeq,
        attempted ? (double)accepted / (double)attempted : 0.0);
    BOOL promotionEligible = accepted > 0 &&
        accepted == r116DisplayConfirmedTuples &&
        r116DisplayRejectedTuples == 0 &&
        r116DisplayEvidenceLost == 0 &&
        r116OrderingViolations == 0 &&
        r116CoalescedPresentations == 0 &&
        r116BCallbackMissing == 0 &&
        r114SourceLeaksWhileClosed == 0;
    logLine("R116_RUN_SUMMARY earlyBDispatch=%llu lateMCallbacksAfterEarlyB=%llu orderingViolations=%llu coalescedPresentations=%llu presentationCallbackMissing=%llu watchdogArmed=%llu watchdogFired=%llu earlyBDispatchEnabled=%d watchdogBoundMs=%llu gateEncoded=%llu gateEncodeFailures=%llu displayConfirmed=%llu displayRejected=%llu displayEvidenceLost=%llu promotionEligible=%d",
        (unsigned long long)r116EarlyBDispatchCount,
        (unsigned long long)r116LateMCallbacksAfterEarlyB,
        (unsigned long long)r116OrderingViolations,
        (unsigned long long)r116CoalescedPresentations,
        (unsigned long long)r116BCallbackMissing,
        (unsigned long long)r116WatchdogArmed,
        (unsigned long long)r116WatchdogFired,
        r116EarlyBDispatchEnabled() ? 1 : 0,
        (unsigned long long)(r116CommittedBWatchdogNs() / 1000000ULL),
        (unsigned long long)r116GateEncodedCount,
        (unsigned long long)r116GateEncodeFailures,
        (unsigned long long)r116DisplayConfirmedTuples,
        (unsigned long long)r116DisplayRejectedTuples,
        (unsigned long long)r116DisplayEvidenceLost,
        promotionEligible ? 1 : 0);
}

static void r115ResetTuple(const char *outcome, const char *reason) {
    if (!r115ContinuousEnabled()) return;

    R102DeferredSourceTicket *liveTicket = nil;
    pthread_mutex_lock(&stateLock);
    liveTicket = r102DeferredTicket;
    pthread_mutex_unlock(&stateLock);
    if (liveTicket) {
        // The reset contract forbids retiring a tuple whose retained SOURCE B is
        // still owned. Forwarding it is the only safe terminal path; the forward
        // path calls back into this function when it completes.
        r102FallbackTicket(liveTicket, reason ?: "reset-with-live-ticket");
        return;
    }

    R115TupleTiming timing = {0};
    uint64_t generation = 0;
    uint64_t attempted = 0, accepted = 0, fallbacks = 0;
    BOOL acceptedOutcome = NO;
    BOOL pendingOutcome = NO;
    pthread_mutex_lock(&stateLock);
    if (r115Phase == R115PhaseIdle && r114LatchState == R114LatchIdle) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    if (r102DeferredTicket) {
        // A ticket was published between the probe above and this critical
        // section. Retiring the tuple now would orphan it: the presented handler
        // and the recovery timer both test pointer identity, so retained B would
        // never be presented. Let the ticket's own terminal path reset instead.
        pthread_mutex_unlock(&stateLock);
        logLine("R115_RESET_DEFERRED reason=deferred-ticket-published-concurrently");
        return;
    }
    timing = r115Timing;
    acceptedOutcome = (outcome && strcmp(outcome, "CONSUMED") == 0);
    pendingOutcome = (outcome && strcmp(outcome, "PENDING_DISPLAY") == 0);
    timing.accepted = acceptedOutcome;
    r115Phase = R115PhaseResetting;

    // Pair identity, SOURCE A/B references and WSI identity.
    r114ReservedPairA = 0;
    r114ReservedPairB = 0;
    r114ReservedAtNs = 0;
    r110TupleSourceBSequence = 0;
    r110ExpectedASourceWsiPresentId = 0;
    r110ExpectedASourceWsiOrder = 0;
    r110InternalWsiPresentId = 0;
    r110InternalWsiOrder = 0;
    r110ExpectedBSourceWsiPresentId = 0;
    r110ExpectedBSourceWsiOrder = 0;
    r110TupleWsiOrderInvalid = NO;
    // Candidate M, InternalPresentId association, ticket.
    r102ClearPendingInternalLocked(0);
    r102DeferredTicket = nil;
    // Gate state.
    r110QualityReviewed = NO;
    r110QualityRejected = NO;
    r110QualityWaitStarted = NO;
    // Frontier ownership.
    r103SourceFrontierState = R103SourceFrontierOpen;
    r103FrontierSpent = NO;
    r103ClearFrontierLocked();
    r103FutureSourceDispatchesBeforeClose = 0;
    // Capture bookkeeping for the next generation.
    r115CaptureMask = 0;
    r115WarmupPairA = 0;
    r115WarmupPairB = 0;
    r114LatchState = R114LatchIdle;
    ++r115ResetCount;
    r115Generation += 1;
    generation = r115Generation;
    if (acceptedOutcome) ++r115AcceptedTuples;
    else if (pendingOutcome) {
        // Accepted-ness is not knowable yet: M's presented callback has not run.
        // The counter was already raised by r116MarkPendingDisplay, which also
        // marked the ordering slot. r116NotePresentationTime resolves both.
    } else {
        ++r115FallbackTuples;
        r115ArmCooldownUntilSequence = drawableSequence + 8;
    }
    attempted = r115AttemptedTuples;
    accepted = r115AcceptedTuples;
    fallbacks = r115FallbackTuples;
    r115Phase = R115PhaseIdle;
    memset(&r115Timing, 0, sizeof(r115Timing));
    pthread_cond_broadcast(&presentationReadyCondition);
    pthread_mutex_unlock(&stateLock);

    timing.frontierOpenNs = timing.frontierOpenNs ?: r105NowNs();
    r115LogTiming(&timing, outcome, reason);
    // R121: emitted from the tuple terminal path, i.e. after the measurement
    // window has closed. No R121 log write ever occurs inside the window.
    r121LogSpan(&timing, outcome, reason);
    logLine("R115_TUPLE_RESET generation=%llu nextGeneration=%llu outcome=%s reason=%s resets=%llu attempts=%llu accepted=%llu fallback=%llu remainingBudget=%llu state=IDLE rearm=AUTOMATIC",
        (unsigned long long)timing.generation, (unsigned long long)generation,
        outcome ?: "UNKNOWN", reason ?: "none",
        (unsigned long long)r115ResetCount, (unsigned long long)attempted,
        (unsigned long long)accepted, (unsigned long long)fallbacks,
        (unsigned long long)(attempted >= r115MaxTupleCount()
            ? 0 : r115MaxTupleCount() - attempted));

    (void)accepted; (void)attempted; (void)fallbacks;
    r116MaybeEmitRunSummary();
}


static void r102ForwardSourceTicket(R102DeferredSourceTicket *ticket,
                                    const char *reason) {
    // R115 deadlock regression, checked before any state is claimed.
    // Forwarding retained B re-enters CAMetalLayer's present lock; doing it from
    // inside didPresentAtTime: froze the game in R114 attempt 9. Hopping here
    // (not after claiming the ticket state) keeps the re-entrant call legal: if
    // this ran after `ticket.state = ForwardingSource`, the hop would re-enter,
    // fail the state test, and drop B forever while the frontier sat in the
    // unrecoverable Releasing state.
    if (r115InsidePresentedCallback) {
        logLine("R115_DEADLOCK_REGRESSION=HOPPED ticketId=%llu pair=%llu->%llu reason=synchronous-forward-from-presented-callback",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB);
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            r102ForwardSourceTicket(ticket, reason);
        });
        return;
    }

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

    if (r115ContinuousEnabled()) {
        pthread_mutex_lock(&stateLock);
        BOOL generationCurrent = ticket.generation == r115Generation;
        // This runs AFTER the ticket was claimed and the frontier moved to
        // Releasing, so it must never bail out: returning here would strand B in
        // a state the recovery timer cannot touch. Pointer identity above is the
        // authoritative ownership guard; a mismatch is reported, never enforced.
        if (generationCurrent) {
            r115Timing.bForwardBeginNs = r105NowNs();
            if (reason) r115Timing.gateRejected = YES;
        } else {
            ++r115StaleCallbackRejections;
        }
        uint64_t ticketGeneration = ticket.generation;
        uint64_t liveGeneration = r115Generation;
        pthread_mutex_unlock(&stateLock);
        if (!generationCurrent) {
            logLine("R115_GENERATION_MISMATCH_WITH_LIVE_TICKET ticketId=%llu ticketGeneration=%llu liveGeneration=%llu pair=%llu->%llu site=forward-source reason=%s action=FORWARD_ANYWAY",
                (unsigned long long)ticket.ticketId,
                (unsigned long long)ticketGeneration,
                (unsigned long long)liveGeneration,
                (unsigned long long)ticket.sourcePairA,
                (unsigned long long)ticket.sourcePairB, reason ?: "none");
        }
        r115NotePhase(reason ? R115PhaseFallback : R115PhaseBForwarding,
                      reason ?: (ticket.forwardedEarly ? "M-dispatched-early-B"
                                                       : "M-displayed"));
    }

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
    // R116: observe B's actual display time so the M/B ordering claim is evidence
    // rather than inference. The handler only records; it never mutates the
    // tuple, so it cannot extend a lifecycle.
    if (r115ContinuousEnabled() && ticket.internalDispatched &&
        [ticket.drawable respondsToSelector:@selector(addPresentedHandler:)]) {
        [ticket.drawable addPresentedHandler:^(id<MTLDrawable> presented) {
            r116NotePresentationTime(ticket.ticketId, NO, presented.presentedTime,
                                     presented.presentedTime > 0.0);
            // R121 drawable accounting: this is the second and only other exit
            // for a SOURCE drawable. Without it the deferred-B drawables are
            // acquired and never released, which is what made the first R121
            // build report 14 outstanding against a pool of 3.
            r121NoteSourceDrawableReleased();
        }];
    }
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
    const char *r114Outcome = NULL;
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
            // R115 judges leaks per generation: a leak in tuple N must not mark
            // every later tuple FAILED, or the multi-tuple acceptance ratio
            // becomes unmeasurable. The cumulative counter stays for the run
            // verdict, which requires zero for the whole run.
            uint64_t leaksSinceArm = r103FutureSourceLeaks >= r115Timing.leaksAtArm
                ? r103FutureSourceLeaks - r115Timing.leaksAtArm
                : r103FutureSourceLeaks;
            BOOL leaksClean = r115ContinuousEnabled()
                ? leaksSinceArm == 0 : r103FutureSourceLeaks == 0;
            // R116: when B was forwarded before M's presented callback, the
            // display counter for THIS tuple has not advanced yet - the callback
            // has not fired. Requiring it here would mark every early-dispatched
            // tuple FAILED and force a re-arm cooldown after each success.
            // The display evidence is instead asserted per tuple by
            // R116_PRESENTATION_ORDER (M and B presentedTime, exact ticketId) and
            // counted in r116OrderingViolations / r116BCallbackMissing, which the
            // run verdict requires to be zero.
            BOOL displayEvidenceDeferred = r116EarlyBDispatchEnabled() &&
                ticket.forwardedEarly;
            BOOL displayAdvanced =
                internalPresentationDisplayedCount > r103FrontierDisplayedBaseline;
            frontierContiguous = !reason &&
                (displayEvidenceDeferred || displayAdvanced) &&
                leaksClean && titleWsiOrderValid;
            if (displayEvidenceDeferred && !reason && titleWsiOrderValid && leaksClean) {
                logLine("R116_TUPLE_DISPLAY_EVIDENCE=DEFERRED ticketId=%llu pair=%llu->%llu reason=b-forwarded-before-m-callback assertedBy=R116_PRESENTATION_ORDER",
                    (unsigned long long)ticket.ticketId,
                    (unsigned long long)ticket.sourcePairA,
                    (unsigned long long)ticket.sourcePairB);
            }
            if (reason) ++r103FrontierFallbacks;
            if (frontierContiguous) ++r103FrontierContiguousSuccesses;
            if (reason) {
                r114MarkPairFailedLocked(reason);
                r114Outcome = "FAILED";
            } else if (frontierContiguous) {
                r114MarkPairConsumedLocked();
                // R116: dispatch order is validated here, but when B was
                // forwarded before M's callback the display half is still
                // outstanding. The label says so rather than overclaiming.
                r114Outcome = (r116EarlyBDispatchEnabled() && ticket.forwardedEarly)
                    ? "CONSUMED_PENDING_DISPLAY" : "CONSUMED";
            } else {
                r114MarkPairFailedLocked("presentation-completed-without-validated-wsi-order");
                r114Outcome = "FAILED";
            }
            r103SourceFrontierState = R103SourceFrontierOpen;
            r103FrontierSpent = YES;
            r103ClearFrontierLocked();
            frontierOpened = YES;
        }
        pthread_cond_broadcast(&presentationReadyCondition);
    }
    pthread_mutex_unlock(&stateLock);
    if (r114Outcome) {
        logLine("R114_LATCH=%s ticketId=%llu pair=%llu->%llu reason=%s rearm=0 continuousGeneration=0",
            r114Outcome, (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            reason ?: "M-displayed-and-B-forwarded");
    }
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
    if (r115ContinuousEnabled()) {
        pthread_mutex_lock(&stateLock);
        r115Timing.bForwardEndNs = r105NowNs();
        if (!r115Timing.frontierOpenNs) r115Timing.frontierOpenNs = r115Timing.bForwardEndNs;
        pthread_mutex_unlock(&stateLock);
        logLine("R115_REARM_ELIGIBLE generation=%llu pair=%llu->%llu reason=tuple-terminal frontier=OPEN ticket=NONE callbacks=TERMINAL",
            (unsigned long long)ticket.generation,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB);
        // R116: when B was forwarded before M's presented callback, the display
        // half of the verdict does not exist yet. The tuple is retired as
        // PENDING_DISPLAY and only counted as accepted once
        // R116_PRESENTATION_ORDER proves M and B both reached the display in
        // order. Claiming CONSUMED here would assert a property nothing has
        // observed.
        const char *outcome;
        if (frontierContiguous && !reason &&
            r116EarlyBDispatchEnabled() && ticket.forwardedEarly) {
            outcome = "PENDING_DISPLAY";
        } else {
            outcome = (frontierContiguous && !reason) ? "CONSUMED" : "FAILED";
            // The early mark was optimistic; this tuple is not provisional after
            // all, so release it before the reset.
            if (r116EarlyBDispatchEnabled() && ticket.forwardedEarly)
                r116ClearPendingDisplay(ticket.ticketId);
        }
        r115ResetTuple(outcome, reason ?: "B-forwarded");
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
    // R116: record that this pair's present hook exists, before any work that can
    // block. Written under stateLock so the watchdog's read is ordered with it.
    pthread_mutex_lock(&stateLock);
    r116BHookEnteredPairB = sequence;
    r116BHookEnteredNs = r105NowNs();
    pthread_mutex_unlock(&stateLock);
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
    // R121: the deferral is now committed, so this SOURCE present will never
    // reach ordinary WSI dispatch and will never install a presented handler.
    // Counted here -- the one point shared by all three present selectors --
    // so the outstanding-drawable identity stays exact.
    r121NoteSourceDrawableDeferred();
    ticket.generation = r115Generation;
    if (r115ContinuousEnabled()) {
        // R121: one clock read, used for both the accepted R115 field and the
        // R121 chain head, so the chain start is bit-identical to the headline
        // start rather than a second, slightly later sample.
        const uint64_t r121HookNs = r105NowNs();
        r115Timing.bPresentHookNs = r121HookNs;
        r115Timing.mediaHook = fgmetalMediaNow();
        r121MarkValue(R121SlotBHook, r121HookNs);
    }
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
    if (r115ContinuousEnabled()) {
        logLine("R115_TUPLE_B_DEFERRED generation=%llu ticketId=%llu pair=%llu->%llu appFrameId=%llu bPresentHookNs=%llu",
            (unsigned long long)ticket.generation,
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB,
            (unsigned long long)ticket.appFrameId,
            (unsigned long long)r115Timing.bPresentHookNs);
        r115NotePhase(R115PhaseBDeferred, "present-hook");
    }
    // R115 bounds the retained-B hold explicitly: a tuple that cannot reach the
    // display inside this deadline is forwarded as plain SOURCE instead of
    // stalling the frontier for the full 20 s recovery window.
    uint64_t recoverySeconds = r115ContinuousEnabled() ? 4 : 20;
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)recoverySeconds * NSEC_PER_SEC),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r102FallbackTicket(ticket, "no-matching-M-before-recovery-deadline");
    });
    return YES;
}


// R116 presentation-order accounting.
//
// M and B are separate drawables with separate presented callbacks that can
// arrive in either order, and a late B callback for tuple N can arrive after
// tuple N+1's M callback. A single current-tuple record would silently drop one
// of them, so the record is a small ring keyed by ticketId. Evicting a slot that
// saw M but never saw B is counted as lost evidence rather than ignored.
#define kR116OrderSlots 4

typedef struct {
    uint64_t ticketId;
    double mTime;
    double bTime;
    BOOL mSeen;
    BOOL bSeen;
    BOOL mReached;
    BOOL bReached;
    uint64_t mCallbackNs;
    uint64_t bCallbackNs;
    uint64_t mDispatchNs;
    // Set when the tuple retired before its display evidence existed. It is
    // resolved by this slot's verdict, or rejected if the slot is evicted.
    BOOL pending;
    // Set once this tuple's acceptance has been decided (verdict, timeout,
    // eviction or clear). A late verdict must still be logged for ordering
    // evidence but must never move the counters a second time.
    BOOL counted;
} R116OrderSlot;

static R116OrderSlot r116OrderSlots[kR116OrderSlots];
static NSUInteger r116OrderNextSlot;

// Caller holds stateLock.
static R116OrderSlot *r116OrderSlotLocked(uint64_t ticketId) {
    for (NSUInteger i = 0; i < kR116OrderSlots; ++i) {
        if (r116OrderSlots[i].ticketId == ticketId) return &r116OrderSlots[i];
    }
    for (NSUInteger i = 0; i < kR116OrderSlots; ++i) {
        if (r116OrderSlots[i].ticketId == 0) {
            memset(&r116OrderSlots[i], 0, sizeof(R116OrderSlot));
            r116OrderSlots[i].ticketId = ticketId;
            return &r116OrderSlots[i];
        }
    }
    R116OrderSlot *slot = &r116OrderSlots[r116OrderNextSlot];
    if ((slot->mSeen && !slot->bSeen) || (slot->bSeen && !slot->mSeen)) {
        ++r116DisplayEvidenceLost;
        ++r116BCallbackMissing;
        logLine("R116_PRESENTATION_ORDER_EVIDENCE_LOST ticketId=%llu reason=only-one-callback-before-eviction mSeen=%d bSeen=%d",
            (unsigned long long)slot->ticketId, slot->mSeen, slot->bSeen);
    }
    if (slot->pending) {
        // A provisional tuple whose evidence never arrived is not accepted.
        slot->pending = NO;
        slot->counted = YES;
        if (r116PendingDisplayTuples) --r116PendingDisplayTuples;
        ++r116DisplayRejectedTuples;
        ++r115FallbackTuples;
        r115ArmCooldownUntilSequence = drawableSequence + 8;
        r116SummaryCheckPending = YES;
        logLine("R116_TUPLE_DISPLAY_VERDICT ticketId=%llu result=FAILED reason=evidence-lost",
            (unsigned long long)slot->ticketId);
    }
    memset(slot, 0, sizeof(R116OrderSlot));
    slot->ticketId = ticketId;
    r116OrderNextSlot = (r116OrderNextSlot + 1) % kR116OrderSlots;
    return slot;
}

// Bounded wait for a provisional tuple's display evidence. A presented callback
// that never arrives must not be able to stall the run forever, so the mark arms
// a deadline that resolves it as rejected.
static uint64_t r116PendingDisplayTimeoutNs(void) {
    static uint64_t cached = 0;
    if (cached == 0) {
        const char *configured = getenv("FGMETAL_R116_PENDING_TIMEOUT_MS");
        uint64_t ms = configured ? strtoull(configured, NULL, 10) : 0;
        if (ms < 50) ms = 500;
        cached = ms * 1000000ULL;
    }
    return cached;
}

static void r116ResolvePendingDisplayTimeout(uint64_t ticketId) {
    BOOL resolved = NO;
    pthread_mutex_lock(&stateLock);
    for (NSUInteger i = 0; i < kR116OrderSlots; ++i) {
        R116OrderSlot *slot = &r116OrderSlots[i];
        if (slot->ticketId == ticketId && slot->pending) {
            slot->pending = NO;
            slot->counted = YES;
            if (r116PendingDisplayTuples) --r116PendingDisplayTuples;
            ++r116DisplayRejectedTuples;
            ++r116DisplayTimedOut;
            ++r115FallbackTuples;
            r115ArmCooldownUntilSequence = drawableSequence + 8;
            resolved = YES;
            break;
        }
    }
    pthread_mutex_unlock(&stateLock);
    if (!resolved) return;
    logLine("R116_TUPLE_DISPLAY_VERDICT ticketId=%llu result=FAILED reason=display-evidence-timeout timeoutMs=%llu",
        (unsigned long long)ticketId,
        (unsigned long long)(r116PendingDisplayTimeoutNs() / 1000000ULL));
    r116MaybeEmitRunSummary();
}

// Marks a provisionally-retired tuple as awaiting its display evidence.
static void r116MarkPendingDisplay(uint64_t ticketId) {
    BOOL marked = NO;
    BOOL summaryCheck = NO;
    pthread_mutex_lock(&stateLock);
    R116OrderSlot *slot = r116OrderSlotLocked(ticketId);
    if (!slot->pending) {
        slot->pending = YES;
        ++r116PendingDisplayTuples;
        marked = YES;
    }
    summaryCheck = r116SummaryCheckPending;
    r116SummaryCheckPending = NO;
    pthread_mutex_unlock(&stateLock);
    if (summaryCheck) r116MaybeEmitRunSummary();
    if (!marked) return;
    uint64_t timeoutNs = r116PendingDisplayTimeoutNs();
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)timeoutNs),
                   dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        r116ResolvePendingDisplayTimeout(ticketId);
    });
}

// Releases an optimistic pending mark when the tuple turned out not to be
// provisionally accepted after all (a fallback forward).
static void r116ClearPendingDisplay(uint64_t ticketId) {
    BOOL cleared = NO;
    pthread_mutex_lock(&stateLock);
    for (NSUInteger i = 0; i < kR116OrderSlots; ++i) {
        R116OrderSlot *slot = &r116OrderSlots[i];
        if (slot->ticketId == ticketId && slot->pending) {
            slot->pending = NO;
            slot->counted = YES;
            if (r116PendingDisplayTuples) --r116PendingDisplayTuples;
            cleared = YES;
            break;
        }
    }
    pthread_mutex_unlock(&stateLock);
    if (cleared) {
        logLine("R116_TUPLE_PENDING_DISPLAY=CLEARED ticketId=%llu reason=tuple-not-provisionally-accepted",
            (unsigned long long)ticketId);
    }
}

static void r116NotePresentationTime(uint64_t ticketId, BOOL isM,
                                     double presentedTime,
                                     BOOL reachedDisplay) {
    R116OrderSlot resolved = {0};
    BOOL verdict = NO;
    BOOL pending = NO;
    uint64_t nowNs = r105NowNs();
    pthread_mutex_lock(&stateLock);
    R116OrderSlot *slot = r116OrderSlotLocked(ticketId);
    if (isM) {
        slot->mTime = presentedTime;
        slot->mReached = reachedDisplay;
        slot->mCallbackNs = nowNs;
        slot->mSeen = YES;
        if (!slot->mDispatchNs) slot->mDispatchNs = r115Timing.mDispatchNs;
    } else {
        slot->bTime = presentedTime;
        slot->bReached = reachedDisplay;
        slot->bCallbackNs = nowNs;
        slot->bSeen = YES;
    }
    if (slot->mSeen && slot->bSeen) {
        resolved = *slot;
        memset(slot, 0, sizeof(R116OrderSlot));
        verdict = YES;
    }
    // All counters are updated under stateLock: M and B callbacks arrive on
    // different threads and a lost increment could flip promotionEligible.
    BOOL ordered = NO, coalesced = NO, distinct = NO;
    if (verdict) {
        ordered = resolved.mReached && resolved.bReached &&
            resolved.mTime <= resolved.bTime;
        coalesced = resolved.mReached && resolved.bReached &&
            resolved.mTime == resolved.bTime;
        distinct = resolved.mReached && resolved.bReached &&
            resolved.mTime < resolved.bTime;
        if (!ordered) ++r116OrderingViolations;
        if (coalesced) ++r116CoalescedPresentations;
        if (!resolved.mReached || !resolved.bReached) ++r116BCallbackMissing;
    }
    if (verdict && resolved.pending && !resolved.counted) {
        if (r116PendingDisplayTuples) --r116PendingDisplayTuples;
        pending = YES;
        // Acceptance requires M and B on distinct refreshes, in that order.
        // Same-refresh presentation is a coalescing failure, not a success.
        if (distinct) {
            ++r115AcceptedTuples;
            ++r116DisplayConfirmedTuples;
        } else {
            ++r115FallbackTuples;
            ++r116DisplayRejectedTuples;
            r115ArmCooldownUntilSequence = drawableSequence + 8;
        }
    }
    // Drain the summary flag under the lock: a check-then-clear outside it can
    // lose a concurrent set and suppress the COMPLETE line.
    BOOL summaryCheck = r116SummaryCheckPending;
    r116SummaryCheckPending = NO;
    pthread_mutex_unlock(&stateLock);

    if (!verdict) {
        if (summaryCheck) r116MaybeEmitRunSummary();
        return;
    }
    logLine("R116_PRESENTATION_ORDER ticketId=%llu mPresentedTime=%.9f bPresentedTime=%.9f deltaMs=%.3f mReachedDisplay=%d bReachedDisplay=%d ordered=%d distinct=%d coalesced=%d mDispatchToCallbackNs=%llu bToBCallbackNs=%llu verdict=%s",
        (unsigned long long)ticketId, resolved.mTime, resolved.bTime,
        (resolved.bTime - resolved.mTime) * 1000.0,
        resolved.mReached, resolved.bReached, ordered, distinct, coalesced,
        (unsigned long long)(resolved.mCallbackNs && resolved.mDispatchNs &&
                             resolved.mCallbackNs > resolved.mDispatchNs
            ? resolved.mCallbackNs - resolved.mDispatchNs : 0),
        (unsigned long long)(resolved.bCallbackNs && resolved.mCallbackNs &&
                             resolved.bCallbackNs > resolved.mCallbackNs
            ? resolved.bCallbackNs - resolved.mCallbackNs : 0),
        distinct ? "M_THEN_B_DISTINCT"
                 : (ordered ? "M_THEN_B_SAME_REFRESH" : "ORDER_VIOLATION"));
    if (pending) {
        logLine("R116_TUPLE_DISPLAY_VERDICT ticketId=%llu result=%s accepted=%llu fallback=%llu",
            (unsigned long long)ticketId,
            distinct ? "CONSUMED" : "FAILED",
            (unsigned long long)r115AcceptedTuples,
            (unsigned long long)r115FallbackTuples);
        r116MaybeEmitRunSummary();
    } else if (summaryCheck) {
        r116MaybeEmitRunSummary();
    }
}

static void r102InternalPresented(R102DeferredSourceTicket *ticket,
                                  id<MTLDrawable> drawable,
                                  uint64_t internalPresentId,
                                  uint64_t wsiPresentId) {
    // R115 deadlock regression scope: everything below runs inside
    // CAMetalLayer's presented callback, where presenting retained B
    // synchronously deadlocks the layer.
    BOOL wasInsidePresentedCallback = r115InsidePresentedCallback;
    r115InsidePresentedCallback = YES;
    BOOL reachedDisplay = drawable.presentedTime > 0.0;
    // R121 drawable accounting: M's drawable has left the display pipeline.
    r121NoteInternalDrawableReleased();
    if (r115ContinuousEnabled())
        r116NotePresentationTime(ticket.ticketId, YES, drawable.presentedTime,
                                 reachedDisplay);
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
        // R115: never forward retained B on the presented-callback thread. This
        // path is a fallback, so ordering does not depend on doing it inline.
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            r102FallbackTicket(ticket, "internal-presented-callback-not-displayed");
        });
        r115InsidePresentedCallback = wasInsidePresentedCallback;
        return;
    }
    pthread_mutex_lock(&stateLock);
    BOOL current = r102TicketIsCurrentLocked(ticket) &&
        ticket.state != R102DeferredTicketForwardingSource;
    void *currentTicketPointer = (__bridge void *)r102DeferredTicket;
    R102DeferredTicketState ticketState = ticket.state;
    BOOL earlyForwarded = ticket.forwardedEarly;
    BOOL generationCurrent = !r115ContinuousEnabled() ||
        ticket.generation == r115Generation;
    // R116: when B was forwarded before this callback, the tuple has already
    // advanced the generation by design. Counting that as a stale rejection
    // makes the inherited zero-stale-callback assertion report one false failure
    // per tuple. It is counted in r116LateMCallbacksAfterEarlyB instead.
    if (!generationCurrent && !earlyForwarded) ++r115StaleCallbackRejections;
    if (current && generationCurrent) {
        r115Timing.mDisplayedNs = r105NowNs();
    }
    // R116: when B was forwarded before this callback, the ticket is already
    // terminal by design. That is not a stale callback and must not be counted
    // as one, or the run verdict would be wrong.
    if (!current && earlyForwarded) {
        ++r116LateMCallbacksAfterEarlyB;
    } else if (!current) {
        ++r102StaleCallbackCount;
    }
    uint64_t ticketGeneration = ticket.generation;
    uint64_t liveGeneration = r115Generation;
    pthread_mutex_unlock(&stateLock);
    logLine("R102_INTERNAL_CALLBACK_STATE ticketId=%llu ticket=%p currentTicket=%p ticketState=%u current=%d reachedDisplay=%d generation=%llu liveGeneration=%llu generationCurrent=%d",
        (unsigned long long)ticket.ticketId, (__bridge void *)ticket,
        currentTicketPointer, (unsigned)ticketState, current, reachedDisplay,
        (unsigned long long)ticketGeneration, (unsigned long long)liveGeneration,
        generationCurrent);
    if (current && !generationCurrent) {
        // Pointer identity is the authoritative guard, so a live ticket is always
        // terminal here. A generation mismatch on a live ticket would mean the
        // reset contract was violated; forward anyway rather than strand B.
        logLine("R115_GENERATION_MISMATCH_WITH_LIVE_TICKET ticketId=%llu ticketGeneration=%llu liveGeneration=%llu pair=%llu->%llu site=internal-presented action=FORWARD_ANYWAY",
            (unsigned long long)ticket.ticketId,
            (unsigned long long)ticketGeneration,
            (unsigned long long)liveGeneration,
            (unsigned long long)ticket.sourcePairA,
            (unsigned long long)ticket.sourcePairB);
    }
    if (current) {
        // R114: presenting retained B re-enters CAMetalLayer. Doing that inside
        // the layer's presented callback deadlocks on the layer's internal
        // present lock (observed live: presentAfterMinimumDuration: never
        // returned and the game froze). Hop off the callback; ordering is
        // preserved because M has already reached the display at this point.
        // R115 keeps this as a permanent regression: the thread-local flag makes
        // any synchronous forward from this callback hop instead of deadlocking.
        r115NotePhase(R115PhaseBForwarding, "M-displayed");
        dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
            r102ForwardSourceTicket(ticket, NULL);
        });
    }
    r115InsidePresentedCallback = wasInsidePresentedCallback;
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
    if (r115ContinuousEnabled()) {
        const uint64_t r121AcquireReqNs = r105NowNs();
        r115Timing.internalAcquireNs = r121AcquireReqNs;
        // R121 drawable-availability evidence, sampled under the lock that is
        // already held here. No extra syscall, no extra critical section.
        r121Span.poolMaximumDrawables = ticket.layer.maximumDrawableCount;
        // Informational: this path only runs for a deferred B, so the tuple's B
        // present was deferred by construction. Kept so the line is self-
        // describing; it is not a measurement and never varies on this path.
        r121Span.deferredBPresent = 1;
        r121Span.liveSourceDrawables = r121SourceOutstanding();
        r121Span.liveInternalDrawables = r121InternalOutstanding();
        r121Span.internalSlotState = (int32_t)internalSlotState;
        r121Span.frontierState = (int32_t)r103SourceFrontierState;
        r121Span.sourceNdEntryNs =
            __atomic_load_n(&r121LastSourceNdEntryNs, __ATOMIC_RELAXED);
        r121Span.sourceNdReturnNs =
            __atomic_load_n(&r121LastSourceNdReturnNs, __ATOMIC_RELAXED);
        r121MarkValue(R121SlotInternalAcquireReq, r121AcquireReqNs);
    }
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

    logLine("R102_INTERNAL_DRAWABLE_ACQUIRE_BEGIN ticketId=%llu origin=INTERNAL internalPresentId=%llu sourcePair=%llu->%llu layer=%p device=%p "
            "maximumDrawableCount=%lu deferredBPresent=%d liveSourceDrawables=%llu liveInternalDrawables=%llu internalSlotState=%d frontierState=%d",
        (unsigned long long)ticket.ticketId,
        (unsigned long long)internalPresentId,
        (unsigned long long)ticket.sourcePairA,
        (unsigned long long)ticket.sourcePairB,
        (__bridge void *)ticket.layer, (__bridge void *)ticket.device,
        (unsigned long)ticket.layer.maximumDrawableCount,
        (int)r121Span.deferredBPresent,
        (unsigned long long)r121SourceOutstanding(),
        (unsigned long long)r121InternalOutstanding(),
        (int)internalSlotState, (int)r103SourceFrontierState);
    isInternalDrawableAcquisition = YES;
    isInternalPresentationCall = YES;
    // R121: the INTERNAL wait is bracketed by two CLOCK_MONOTONIC samples taken
    // around exactly the call that can block. Nothing else runs between them.
    r121Mark(R121SlotInternalNdEnter);
    id<CAMetalDrawable> internalDrawable = originalNextDrawable
        ? ((id (*)(id, SEL))originalNextDrawable)(ticket.layer, @selector(nextDrawable)) : nil;
    r121Mark(R121SlotInternalNdReturn);
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
    r121Mark(R121SlotInternalAcquired);
    r121NoteInternalDrawableAcquired();

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
    r121Mark(R121SlotMWsiAlloc);
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
    if (r115ContinuousEnabled()) {
        // Recorded before the layer's present lock is taken: the phase note emits
        // a log write and must not sit inside the presentation critical section.
        pthread_mutex_lock(&stateLock);
        r115Timing.mDispatchNs = r105NowNs();
        pthread_mutex_unlock(&stateLock);
        // R121: the chain endpoint is bit-identical to the headline endpoint.
        r121MarkValue(R121SlotMWsiDispatch, r115Timing.mDispatchNs);
        r115NotePhase(R115PhaseMPresenting, "M-dispatch");
    }
    [r110WsiDispatchLock lock];
    (void)r110RecordWsiDispatch("INTERNAL", 0, internalPresentId,
                                internalWsiPresentId, ticket.sourcePairA,
                                ticket.sourcePairB);
    // R121 extension: the present call sits after the measured endpoint. It is
    // reported separately and never folded into the budget.
    r121Mark(R121SlotInternalPresentEnter);
    ((void (*)(id, SEL))presentOriginal)(internalDrawable, @selector(present));
    r121Mark(R121SlotInternalPresentReturn);
    [r110WsiDispatchLock unlock];
    isInternalPresentationCall = NO;
    pthread_mutex_lock(&stateLock);
    ++internalPresentationSuccesses;
    ticket.internalDispatched = YES;
    pthread_mutex_unlock(&stateLock);

    // R116 non-blocking M-then-B scheduling.
    //
    // M is now queued on the layer. Waiting for M's presented callback before
    // forwarding B costs a full display interval plus the callback hop (~28 ms in
    // R115) and pushes B to the refresh after that. The bounded experiment shows
    // the layer keeps two back-to-back presents on consecutive refreshes with no
    // coalescing, so B can be queued here and still display strictly after M.
    //
    // This runs on the capture-processing thread, never inside a presented
    // callback, so the R114/R115 deadlock contract is untouched. If it ever did
    // run inside one, r102ForwardSourceTicket hops asynchronously first.
    if (r116EarlyBDispatchEnabled() && !r115InsidePresentedCallback) {
        pthread_mutex_lock(&stateLock);
        BOOL stillCurrent = r102TicketIsCurrentLocked(ticket) &&
            ticket.state == R102DeferredTicketWaitingForInternalDisplay;
        if (stillCurrent) {
            ticket.forwardedEarly = YES;
            ++r116EarlyBDispatchCount;
        }
        uint64_t orderingTicketId = ticket.ticketId;
        uint64_t orderingPairA = ticket.sourcePairA;
        uint64_t orderingPairB = ticket.sourcePairB;
        uint64_t mDispatchNs = r115Timing.mDispatchNs;
        pthread_mutex_unlock(&stateLock);
        if (stillCurrent) {
            // Mark provisionally accepted BEFORE B is presented: both presented
            // callbacks can land between the present call and the forward tail,
            // and a mark taken afterwards would allocate a slot nothing will ever
            // resolve.
            r116MarkPendingDisplay(ticket.ticketId);
            logLine("R116_EARLY_B_DISPATCH ticketId=%llu pair=%llu->%llu mDispatchNs=%llu action=FORWARD_B_BEFORE_M_CALLBACK orderingSafety=bounded-layer-experiment",
                (unsigned long long)orderingTicketId,
                (unsigned long long)orderingPairA,
                (unsigned long long)orderingPairB,
                (unsigned long long)mDispatchNs);
            r102ForwardSourceTicket(ticket, NULL);
        }
    }
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
        // R121 drawable accounting: the drawable has left the display pipeline,
        // so it is no longer one the observer holds outstanding.
        if (origin && strcmp(origin, "SOURCE") == 0)
            r121NoteSourceDrawableReleased();
        if (internalPresentId != 0 && reachedDisplay) {
            pthread_mutex_lock(&stateLock);
            ++internalPresentationDisplayedCount;
            pthread_mutex_unlock(&stateLock);
        }
        if (origin && strcmp(origin, "SOURCE") == 0 && reachedDisplay &&
            r115ContinuousEnabled()) {
            pthread_mutex_lock(&stateLock);
            if (r115Timing.pairA && appFrameId == r115Timing.pairA &&
                r115Timing.aDisplayedNs == 0) {
                r115Timing.aDisplayedNs = r105NowNs();
            }
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
    // R114 leak classification. Only a SOURCE dispatch that crosses WSI while
    // the frontier is genuinely CLOSING/CLOSED/RELEASING and is not the retained
    // B is a frontier correctness failure.
    BOOL sourceLeakWhileClosed = barrierActive && appFrameId != r103FrontierPairB;
    // A target that was bound but whose B still reached ordinary WSI means the
    // reservation/close missed its window. That is a missed target, not a leak.
    BOOL missedTarget = targetConfigured && !r103FrontierSpent &&
        r103SourceFrontierState == R103SourceFrontierOpen &&
        appFrameId >= r110TupleSourceBSequence;
    BOOL preFrontierAdmission = !targetConfigured && r114LatchState != R114LatchIdle &&
        !r103FrontierSpent && r103SourceFrontierState == R103SourceFrontierOpen;
    uint64_t frontierTicketId = r103FrontierTicketId;
    uint64_t missedPairB = r110TupleSourceBSequence;
    if (sourceLeakWhileClosed) {
        ++r103FutureSourceLeaks;
        ++r114SourceLeaksWhileClosed;
    }
    if (missedTarget) {
        ++r114MissedTargetAdmissions;
        ++r103FutureSourceDispatchesBeforeClose;
        r114MarkPairFailedLocked("source-b-reached-wsi-before-frontier-close");
    }
    if (preFrontierAdmission) ++r114PreFrontierAdmissions;
    if (r115ContinuousEnabled() && r115Timing.pairA &&
        appFrameId == r115Timing.pairA && r115Timing.aDispatchNs == 0) {
        r115Timing.aDispatchNs = r105NowNs();
    }
    uint8_t frontierStateSnapshot = (uint8_t)r103SourceFrontierState;
    if (appFrameId > r114LastDispatchedSourceSequence)
        r114LastDispatchedSourceSequence = appFrameId;
    pthread_mutex_unlock(&stateLock);
    if (sourceLeakWhileClosed || missedTarget) {
        logLine("R114_SOURCE_ADMISSION_CLASS=%s sourceSequence=%llu drawableID=%lu ticketId=%llu barrierActive=%d frontierState=%u targetSourceSequenceB=%llu reason=%s",
            sourceLeakWhileClosed ? "SOURCE_LEAK_WHILE_FRONTIER_CLOSED" : "MISSED_TARGET_ADMISSION",
            (unsigned long long)appFrameId, (unsigned long)drawable.drawableID,
            (unsigned long long)frontierTicketId, barrierActive,
            (unsigned)frontierStateSnapshot,
            (unsigned long long)missedPairB,
            sourceLeakWhileClosed ? "future-source-reached-wsi-while-frontier-closed"
                                  : "reserved-pair-b-reached-wsi-before-frontier-close");
    }
    if (sourceLeakWhileClosed && barrierActive)
        r103RecoverFrontier(frontierTicketId, "source-wsi-gate-invariant");
    if (missedTarget && r115ContinuousEnabled())
        r115ResetTuple("FAILED", "source-b-reached-wsi-before-frontier-close");
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

    // R121: only the accepted M path is in scope. The legacy R100 one-shot path
    // calls this same function and must not claim these slots, so the gate is
    // "the M drawable has already been tagged for this tuple".
    const BOOL r121IsMSpan = __atomic_load_n(&r121Span.have[R121SlotInternalAcquired],
                                             __ATOMIC_ACQUIRE) != 0;
    if (r121IsMSpan) r121Mark(R121SlotMCopyEncodeStart);
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
    if (r121IsMSpan) {
        r121Mark(R121SlotMCopyCommit);
        // Sampled immediately after the monotonic boundary instant so the two
        // clocks bracket the same interval to within the sampling cost.
        r121Span.mediaMCopyCommit = fgmetalMediaNow();
    }
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (r121IsMSpan) {
        r121Mark(R121SlotMCopyWake);
        r121Span.mediaMCopyWake = fgmetalMediaNow();
        r121Span.mCopyGpuStartNs = fgmetalMediaToNs(commandBuffer.GPUStartTime);
        r121Span.mCopyGpuEndNs = fgmetalMediaToNs(commandBuffer.GPUEndTime);
    }
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

// ===========================================================================
// R120 Phase 1 -- SOURCE cadence pacing.
//
// The brief forbids manufacturing a 30 fps SOURCE cadence by discarding every
// second frame. The only honest way to obtain a 30 fps SOURCE on this host is
// to make the game actually produce at 30 fps. The mechanism used here is
// back-pressure at the public present call: the render thread cannot advance
// past `present` until the paced deadline, so the next `nextDrawable` -- and
// therefore the next SOURCE frame -- cannot occur early.
//
// This is a rate limiter on presentation, not a frame dropper:
//   * every present call the game makes still results in exactly one present;
//   * no SOURCE frame is skipped, relabelled or suppressed;
//   * AppFrameId / SOURCE accounting / ordering are untouched.
//
// It is deliberately experimental and opt-in (FGMETAL_R120_PACE_HZ). If the
// measurement shows the acquisition cadence does not follow the paced present
// cadence, the game is not back-pressured by this hook and the round must
// report TRUE_30FPS_SOURCE_CADENCE = NOT ESTABLISHED rather than proceed.
//
// Bounded by construction: a single wait is capped at two periods, so a clock
// discontinuity or a suspend/resume cannot park the render thread.
// ===========================================================================

static pthread_mutex_t r120PaceLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_once_t r120PaceOnce = PTHREAD_ONCE_INIT;
static int r120PaceActive = 0;
static double r120PaceHzValue = 0.0;
static uint64_t r120PacePeriodNs = 0;

static uint64_t r120PaceNextAllowedNs = 0;
static uint64_t r120PaceLastNs = 0;
static uint64_t r120PaceFirstNs = 0;
static uint64_t r120PacePresents = 0;
static uint64_t r120PaceBlockedPresents = 0;
static uint64_t r120PaceBlockedNsTotal = 0;
static uint64_t r120PaceBlockedNsMax = 0;
static uint64_t r120PaceOverCapCount = 0;
static uint64_t r120PaceIntervalSumNs = 0;
static uint64_t r120PaceIntervalCount = 0;
static uint64_t r120PaceIntervalMinNs = UINT64_MAX;
static uint64_t r120PaceIntervalMaxNs = 0;
static uint64_t r120PaceShortIntervals = 0;
static uint64_t r120PaceSummaryEvery = 120;

static void r120PaceInit(void) {
    const char *value = getenv("FGMETAL_R120_PACE_HZ");
    double hz = 0.0;
    if (value && *value) hz = strtod(value, NULL);
    // Fail closed on an implausible rate rather than silently pacing at 0 Hz or
    // at a value that would park the render thread.
    if (!(hz >= 5.0 && hz <= 480.0)) {
        r120PaceActive = 0;
        if (value && *value)
            logLine("R120_PACE_DISABLED requested=%s reason=out-of-range(5..480)", value);
        return;
    }
    r120PaceHzValue = hz;
    r120PacePeriodNs = (uint64_t)llround(1e9 / hz);
    r120PaceActive = 1;
    logLine("R120_PACE_ENABLED hz=%.4f periodNs=%llu mode=source-acquire-block scope=SOURCE-only "
            "backPressure=render-thread-block dropPolicy=NONE accounting=UNCHANGED waitCapPeriods=2",
            hz, (unsigned long long)r120PacePeriodNs);
}

static BOOL r120PaceEnabled(void) {
    pthread_once(&r120PaceOnce, r120PaceInit);
    return r120PaceActive ? YES : NO;
}

// Blocks the calling (game render) thread until the paced SOURCE acquisition
// deadline. Emits one record per paced acquisition so the cadence can be
// measured from the log rather than asserted.
static void r120PaceSourceAcquire(uint64_t callId) {
    if (!r120PaceEnabled()) return;
    const uint64_t period = r120PacePeriodNs;
    const uint64_t entryNs = r105NowNs();
    uint64_t waitNs = 0;

    pthread_mutex_lock(&r120PaceLock);
    if (r120PaceNextAllowedNs == 0) {
        r120PaceNextAllowedNs = entryNs;
        r120PaceFirstNs = entryNs;
    }
    if (entryNs < r120PaceNextAllowedNs) waitNs = r120PaceNextAllowedNs - entryNs;
    pthread_mutex_unlock(&r120PaceLock);

    uint64_t cappedNs = waitNs;
    if (cappedNs > 2 * period) cappedNs = 2 * period;
    if (cappedNs) {
        struct timespec request;
        request.tv_sec = (time_t)(cappedNs / 1000000000ULL);
        request.tv_nsec = (long)(cappedNs % 1000000000ULL);
        // A short sleep is re-entered rather than trusted: nanosleep may return
        // early on a signal, and returning early would let the cadence drift.
        // Separate request/remaining objects: passing one object as both
        // arguments is unspecified by POSIX.
        struct timespec remaining = request;
        while (nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
            if (remaining.tv_sec == 0 && remaining.tv_nsec == 0) break;
        }
    }

    const uint64_t resumeNs = r105NowNs();
    uint64_t intervalNs = 0;
    uint64_t presents = 0;
    uint64_t blockedTotal = 0;
    uint64_t nextAllowed = 0;
    pthread_mutex_lock(&r120PaceLock);
    {
        uint64_t base = resumeNs > r120PaceNextAllowedNs ? resumeNs : r120PaceNextAllowedNs;
        r120PaceNextAllowedNs = base + period;
        nextAllowed = r120PaceNextAllowedNs;
    }
    if (r120PaceLastNs && resumeNs > r120PaceLastNs) intervalNs = resumeNs - r120PaceLastNs;
    r120PaceLastNs = resumeNs;
    ++r120PacePresents;
    if (waitNs) {
        ++r120PaceBlockedPresents;
        r120PaceBlockedNsTotal += waitNs;
        if (waitNs > r120PaceBlockedNsMax) r120PaceBlockedNsMax = waitNs;
    }
    if (cappedNs != waitNs) ++r120PaceOverCapCount;
    if (intervalNs) {
        ++r120PaceIntervalCount;
        r120PaceIntervalSumNs += intervalNs;
        if (intervalNs < r120PaceIntervalMinNs) r120PaceIntervalMinNs = intervalNs;
        if (intervalNs > r120PaceIntervalMaxNs) r120PaceIntervalMaxNs = intervalNs;
        // "Short" means materially faster than the requested period: the source
        // is not being throttled. 0.75x is the analyser's own threshold.
        if (intervalNs * 4 < period * 3) ++r120PaceShortIntervals;
    }
    presents = r120PacePresents;
    blockedTotal = r120PaceBlockedPresents;
    pthread_mutex_unlock(&r120PaceLock);

    logLine("R120_PACE_ACQUIRE callId=%llu entryNs=%llu resumeNs=%llu blockedNs=%llu intervalNs=%llu "
            "nextAllowedNs=%llu presents=%llu blocked=%llu",
            (unsigned long long)callId, (unsigned long long)entryNs,
            (unsigned long long)resumeNs, (unsigned long long)waitNs,
            (unsigned long long)intervalNs, (unsigned long long)nextAllowed,
            (unsigned long long)presents, (unsigned long long)blockedTotal);

    if (r120PaceSummaryEvery && (presents % r120PaceSummaryEvery) == 0) {
        uint64_t count = 0, sum = 0, minNs = 0, maxNs = 0, blocked = 0;
        uint64_t blockedNs = 0, blockedMax = 0, overCap = 0, shortCount = 0;
        pthread_mutex_lock(&r120PaceLock);
        count = r120PaceIntervalCount;
        sum = r120PaceIntervalSumNs;
        minNs = r120PaceIntervalMinNs == UINT64_MAX ? 0 : r120PaceIntervalMinNs;
        maxNs = r120PaceIntervalMaxNs;
        blocked = r120PaceBlockedPresents;
        blockedNs = r120PaceBlockedNsTotal;
        blockedMax = r120PaceBlockedNsMax;
        overCap = r120PaceOverCapCount;
        shortCount = r120PaceShortIntervals;
        pthread_mutex_unlock(&r120PaceLock);
        logLine("R120_PACE_SUMMARY presents=%llu intervals=%llu meanIntervalNs=%llu minIntervalNs=%llu "
                "maxIntervalNs=%llu blockedPresents=%llu meanBlockedNs=%llu maxBlockedNs=%llu "
                "overCapWaits=%llu shortIntervals=%llu requestedPeriodNs=%llu",
                (unsigned long long)presents, (unsigned long long)count,
                (unsigned long long)(count ? sum / count : 0),
                (unsigned long long)minNs, (unsigned long long)maxNs,
                (unsigned long long)blocked,
                (unsigned long long)(blocked ? blockedNs / blocked : 0),
                (unsigned long long)blockedMax, (unsigned long long)overCap,
                (unsigned long long)shortCount, (unsigned long long)period);
    }
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
    // R114 search phase: motion-field raw export is a diagnostic, not a product
    // input. Defer it until a pair is reserved so long search windows stay cheap.
    if (r114LatchEnabled() && r114LatchState != R114LatchReserved) return;
    // R119 Phase 1: bounded artefact sample. A non-sampled tuple still gets the
    // full timing, gate, accounting and WSI evidence; it only skips this export.
    if (!r119DiagnosticsWanted()) return;

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

            // R119: path composition and the write both go through the shared
            // evidence helpers, so this artefact can only ever land in the
            // directory the accessor resolved for this round.
            char path[768] = {0};
            const BOOL pathReady = fgmetalEvidencePath(path, sizeof(path),
                "motion-pair-%llu-%llu.rg16f",
                (unsigned long long)previousSequence,
                (unsigned long long)currentSequence);
            BOOL saved = pathReady && bytes.length == pixelCount * 4 &&
                fgmetalWriteEvidenceAbsolute(path, bytes.bytes, bytes.length);
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
        "\n"
        "  // R117: the 81 candidate shifts share the same 169 sample positions in\n"
        "  // `current`. The R110 form re-read and re-derived all 169 of them for every\n"
        "  // candidate, which is 81x redundant work on the current side. Here the sample\n"
        "  // loop is outermost, so each sample is loaded and scored once and reused\n"
        "  // across all 81 candidates. Per candidate the samples are still accumulated in\n"
        "  // the same order (0..168), so the arithmetic is bit-identical.\n"
        "  int2 shifts[81];\n"
        "  int shiftCount = 0;\n"
        "  for (int dy = base.y - 4; dy <= base.y + 4; ++dy) {\n"
        "    for (int dx = base.x - 4; dx <= base.x + 4; ++dx) {\n"
        "      shifts[shiftCount++] = clamp(int2(dx, dy), int2(-28), int2(28));\n"
        "    }\n"
        "  }\n"
        "  float costSum[81];\n"
        "  float weightSum[81];\n"
        "  for (int k = 0; k < 81; ++k) { costSum[k] = 0.0f; weightSum[k] = 0.0f; }\n"
        "\n"
        "  float targetY = r110Luma(target);\n"
        "  for (int y = -96; y <= 96; y += 16) for (int x = -96; x <= 96; x += 16) {\n"
        "    int2 p = r110ClampPoint(center + int2(x, y), current);\n"
        "    float3 a = current.read(uint2(p)).rgb;\n"
        "    float sampleY = r110Luma(a);\n"
        "    float colorDifference = length(a - target);\n"
        "    if (targetY > 0.15f && sampleY > 0.15f && colorDifference > 0.15f) continue;\n"
        "    float weight = (targetY > 0.15f && sampleY > 0.15f && colorDifference <= 0.15f)\n"
        "        ? 4.0f : 1.0f;\n"
        "    for (int k = 0; k < 81; ++k) {\n"
        "      int2 q = r110ClampPoint(p + shifts[k], previous);\n"
        "      float b = r110Luma(previous.read(uint2(q)).rgb);\n"
        "      costSum[k] += weight * abs(sampleY - b);\n"
        "      weightSum[k] += weight;\n"
        "    }\n"
        "  }\n"
        "\n"
        "  int bestX = base.x;\n"
        "  int bestY = base.y;\n"
        "  float bestCost = INFINITY;\n"
        "  for (int k = 0; k < 81; ++k) {\n"
        "    float cost = costSum[k] / max(weightSum[k], 1.0e-5f)\n"
        "        + 0.000001f * float(abs(shifts[k].x) + abs(shifts[k].y));\n"
        "    if (cost < bestCost) { bestCost = cost; bestX = shifts[k].x; bestY = shifts[k].y; }\n"
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
        R116_GATE_KERNEL_SOURCE
        ;

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
    r110FastGateReducePipeline = [device newComputePipelineStateWithFunction:
        [library newFunctionWithName:@"r110FastGateReduce"] error:&error];
    BOOL ready = library && r108ComputeQueue && r110DownsamplePipeline &&
        r110CoarseMatchPipeline && r110FineRefinePipeline &&
        r108PropagatePipeline && r108UpsamplePipeline && r108PreparePipeline &&
        r108ClearMotionPipeline && r110FastGatePipeline &&
        r110FastGateReducePipeline;
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

typedef struct {
    uint32_t finiteMotion;
    uint32_t activeMotion;
    uint32_t motionSamples;
    uint32_t finiteOutput;
    uint32_t outputSamples;
    uint32_t colorSamples;
    uint32_t groupCount;
    uint32_t frameTag;
    float maxMagnitude;
    float diffPrevious;
    float diffCurrent;
} R110GateResultRaw;

// R116: allocate the gate's scratch buffers once per device. The group buffer is
// sized for the worst case the capture preflight accepts (4096x2160 -> 64x34
// groups); a smaller frame simply uses a prefix of it.
static BOOL r116EnsureGateBuffers(id<MTLDevice> device) {
    if (!device || !r110FastGatePipeline || !r110FastGateReducePipeline) return NO;
    if (r110GateGroupBuffer && r110GateResultBuffer &&
        r110GateBufferDevice == (__bridge void *)device &&
        r110GateGroupCount >= ((r108FrameWidth + 63) / 64) * ((r108FrameHeight + 63) / 64)) {
        return YES;
    }
    const NSUInteger groupsX = (r108FrameWidth + 63) / 64;
    const NSUInteger groupsY = (r108FrameHeight + 63) / 64;
    const NSUInteger groupCount = groupsX * groupsY;
    if (!groupCount) return NO;
    id<MTLBuffer> groups = [device newBufferWithLength:
        groupCount * sizeof(R110GateGroupStats)
        options:MTLResourceStorageModeShared];
    id<MTLBuffer> result = [device newBufferWithLength:sizeof(R110GateResultRaw)
        options:MTLResourceStorageModeShared];
    if (!groups || !result) {
        logLine("R116_GATE_BUFFERS=FAILED device=%p groupCount=%lu reason=allocation",
            (__bridge void *)device, (unsigned long)groupCount);
        return NO;
    }
    r110GateGroupBuffer = groups;
    r110GateResultBuffer = result;
    r110GateGroupCount = groupCount;
    r110GateBufferDevice = (__bridge void *)device;
    logLine("R116_GATE_BUFFERS=READY device=%p groupCount=%lu groupBytes=%lu resultBytes=%lu stage=persistent-reused",
        (__bridge void *)device, (unsigned long)groupCount,
        (unsigned long)(groupCount * sizeof(R110GateGroupStats)),
        (unsigned long)sizeof(R110GateResultRaw));
    return YES;
}

// R116 critical-path gate encode.
//
// Encodes the unchanged per-group metric kernel and the new single-struct
// reduction into the caller's command buffer. Called from inside the MetalFX
// transition submission, so the whole gate costs GPU time only: there is no
// commit -> waitUntilCompleted round trip on the critical path any more.
//
// Returns NO if the encode could not be issued; the caller then treats the
// metrics as unavailable, exactly as the legacy collector did.
static BOOL r116EncodeFastGate(id<MTLCommandBuffer> commandBuffer,
                               id<MTLTexture> motion,
                               id<MTLTexture> previous,
                               id<MTLTexture> current,
                               id<MTLTexture> midpoint) {
    if (!commandBuffer || !motion || !previous || !current || !midpoint) return NO;
    if (motion.width != r108FrameWidth || motion.height != r108FrameHeight ||
        previous.width != r108FrameWidth || previous.height != r108FrameHeight ||
        current.width != r108FrameWidth || current.height != r108FrameHeight ||
        midpoint.width != r108FrameWidth || midpoint.height != r108FrameHeight ||
        motion.pixelFormat != MTLPixelFormatRG16Float ||
        previous.pixelFormat != MTLPixelFormatRGBA16Float ||
        current.pixelFormat != MTLPixelFormatRGBA16Float ||
        midpoint.pixelFormat != MTLPixelFormatRGBA16Float ||
        motion.device != midpoint.device || previous.device != midpoint.device ||
        current.device != midpoint.device) {
        ++r116GateEncodeFailures;
        return NO;
    }
    if (!r116EnsureGateBuffers(midpoint.device)) {
        ++r116GateEncodeFailures;
        return NO;
    }
    const NSUInteger groupsX = (r108FrameWidth + 63) / 64;
    const NSUInteger groupsY = (r108FrameHeight + 63) / 64;
    const uint32_t groupTotal = (uint32_t)(groupsX * groupsY);
    id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
    if (!encoder) {
        ++r116GateEncodeFailures;
        return NO;
    }
    [encoder setComputePipelineState:r110FastGatePipeline];
    [encoder setTexture:motion atIndex:0];
    [encoder setTexture:previous atIndex:1];
    [encoder setTexture:current atIndex:2];
    [encoder setTexture:midpoint atIndex:3];
    [encoder setBuffer:r110GateGroupBuffer offset:0 atIndex:0];
    [encoder dispatchThreadgroups:MTLSizeMake(groupsX, groupsY, 1)
        threadsPerThreadgroup:MTLSizeMake(16, 16, 1)];
    [encoder setComputePipelineState:r110FastGateReducePipeline];
    uint32_t frameTag = r116GateTagNext++;
    if (r116GateTagNext == 0) r116GateTagNext = 1;
    r116GateTagExpected = frameTag;
    [encoder setBuffer:r110GateGroupBuffer offset:0 atIndex:0];
    [encoder setBuffer:r110GateResultBuffer offset:0 atIndex:1];
    [encoder setBytes:&groupTotal length:sizeof(groupTotal) atIndex:2];
    [encoder setBytes:&frameTag length:sizeof(frameTag) atIndex:3];
    [encoder dispatchThreadgroups:MTLSizeMake(1, 1, 1)
        threadsPerThreadgroup:MTLSizeMake(256, 1, 1)];
    [encoder endEncoding];
    ++r116GateEncodedCount;
    return YES;
}

// Reads the reduced struct. Only legal once the transition command buffer that
// carried the encode has completed, which the provider guarantees before it
// returns.
static BOOL r116ReadGateResult(R110FastMetrics *metrics) {
    if (!metrics || !r110GateResultBuffer) return NO;
    R110GateResultRaw raw;
    memcpy(&raw, r110GateResultBuffer.contents, sizeof(raw));
    // The reduce kernel always stamps the group count it actually folded. If it
    // does not match this frame, the buffer holds a result from another
    // submission and must not be used.
    const uint32_t expected = (uint32_t)(((r108FrameWidth + 63) / 64) *
                                         ((r108FrameHeight + 63) / 64));
    if (raw.groupCount != expected) {
        logLine("R116_GATE_RESULT_REJECTED reason=group-count-mismatch expected=%u observed=%u",
            expected, raw.groupCount);
        return NO;
    }
    if (raw.frameTag != r116GateTagExpected) {
        logLine("R116_GATE_RESULT_REJECTED reason=stale-frame-tag expected=%u observed=%u",
            r116GateTagExpected, raw.frameTag);
        return NO;
    }
    memset(metrics, 0, sizeof(*metrics));
    metrics->finiteMotion = raw.finiteMotion;
    metrics->activeMotion = raw.activeMotion;
    metrics->motionSamples = raw.motionSamples;
    metrics->finiteOutput = raw.finiteOutput;
    metrics->outputSamples = raw.outputSamples;
    metrics->colorSamples = raw.colorSamples;
    metrics->maxMagnitude = raw.maxMagnitude;
    metrics->meanAbsDifferenceFromPrevious = raw.colorSamples
        ? (double)raw.diffPrevious / (double)raw.colorSamples : 0.0;
    metrics->meanAbsDifferenceFromCurrent = raw.colorSamples
        ? (double)raw.diffCurrent / (double)raw.colorSamples : 0.0;
    return raw.groupCount > 0;
}

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
    // R119 Phase 1: this readback blocks a utility thread on a full RGBA16F copy
    // and then writes 5.6 MB. It is not part of the product path, so it is
    // skipped for tuples outside the bounded artefact sample.
    if (!r119DiagnosticsWanted()) {
        logLine("R110_METALFX_DIAGNOSTIC=SKIPPED pair=%llu->%llu reason=r119-bounded-diagnostic-sample",
            (unsigned long long)pairA, (unsigned long long)pairB);
        return;
    }
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
        char path[768] = {0};
        const BOOL pathReady = fgmetalEvidencePath(path, sizeof(path),
            "title-M-for-%llu-%llu.rgba16f", (unsigned long long)pairA,
            (unsigned long long)pairB);
        BOOL saved = pathReady && nonfiniteValues == 0 && bytes.length == pixelCount * 8 &&
            fgmetalWriteEvidenceAbsolute(path, bytes.bytes, bytes.length);
        logLine("R110_METALFX_DIAGNOSTIC=%s pair=%llu->%llu dimensions=%lux%lu format=RGBA16Float finiteValues=%llu nonfiniteValues=%llu meaningfulPixels=%llu alphaMin=%.6f alphaMax=%.6f readback=post-attempt saved=%d path=%s",
            saved ? "FINITE" : "INVALID", (unsigned long long)pairA,
            (unsigned long long)pairB, (unsigned long)width, (unsigned long)height,
            (unsigned long long)finiteValues, (unsigned long long)nonfiniteValues,
            (unsigned long long)meaningfulPixels, alphaMin, alphaMax, saved,
            path[0] ? path : "<refused>");
    });
}

static void processCapturedFrame(uint64_t sequence, NSUInteger drawableID,
                                 MTLPixelFormat sourceFormat,
                                 id<MTLTexture> sourceTexture,
                                 id<MTLDevice> device,
                                 NSString *condition) {
    // R118: one clock read at the generation entry point. Cheap, lock-free, and
    // stored under the lock that the estimator stage already takes, so it adds
    // no contention to the per-frame path. Only the pair's B frame keeps it.
    const uint64_t r118GenerationEntryNs = r105NowNs();
    // R119 Phase 2: the same instant on the media clock, so the generation
    // entry can be placed on the same timeline as the copy's GPU timestamps.
    const double r119GenerationEntryMedia = fgmetalMediaNow();
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
        // R115: a gap is expected between tuples because only the reserved pair
        // is captured. Tearing the provider down here would rebuild MetalFX on
        // B's processing, i.e. inside the retained-B hold, once per tuple. The
        // provider holds no frame history, so only the history is dropped.
        BOOL keepProvider = r115ContinuousEnabled();
        logLine("R108_SOURCE_SEQUENCE_GAP previous=%llu current=%llu resetProvider=%d",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence, keepProvider ? 0 : 1);
        if (!keepProvider) {
            realFrameProvider = nil;
            outputReadbackQueue = nil;
            realProviderInitialized = NO;
        }
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

    // R115: the reserved A is always a pure history seed, never an estimated
    // pair, so a tuple costs exactly one estimator + one MetalFX pass no matter
    // how many frames passed since the previous tuple. Without this a gap of one
    // frame would silently estimate (previousB, A) first and add its latency to
    // the retained-B hold.
    BOOL reservedSeed = NO;
    if (r115ContinuousEnabled()) {
        pthread_mutex_lock(&stateLock);
        reservedSeed = r115Timing.pairA == sequence;
        pthread_mutex_unlock(&stateLock);
    }
    BOOL hasPrevious = previousCaptureSource && previousCaptureSequence + 1 == sequence &&
        !reservedSeed;
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

    if (r115ContinuousEnabled() && hasPrevious) {
        pthread_mutex_lock(&stateLock);
        if (r115Timing.pairB == sequence && r115Timing.estimatorStartNs == 0) {
            const uint64_t r121EstimatorStartNs = r105NowNs();
            r115Timing.estimatorStartNs = r121EstimatorStartNs;
            r115Timing.mediaGenStart = r119GenerationEntryMedia;
            if (r115Timing.gsStartNs == 0)
                r115Timing.gsStartNs = r118GenerationEntryNs;
            r121MarkValue(R121SlotEstimatorStart, r121EstimatorStartNs);
        }
        pthread_mutex_unlock(&stateLock);
    }
    // R119 Phase 3: the CPU commit instant on the media clock. Everything the
    // estimator spends between here and GPUStartTime is queue delay, and must
    // never be reported as GPU execution.
    const double r119EstimatorCommit = fgmetalMediaNow();
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    // R119 Phase 3: the CPU wake instant. GPUEndTime -> wake is completion
    // signalling latency, also not GPU execution.
    const double r119EstimatorWake = fgmetalMediaNow();
    if (r115ContinuousEnabled() && hasPrevious) {
        pthread_mutex_lock(&stateLock);
        if (r115Timing.pairB == sequence && r115Timing.estimatorEndNs == 0) {
            const uint64_t r121EstimatorEndNs = r105NowNs();
            r115Timing.estimatorEndNs = r121EstimatorEndNs;
            r121MarkValue(R121SlotEstimatorEnd, r121EstimatorEndNs);
            r115Timing.mediaEstimatorCommit = r119EstimatorCommit;
            r115Timing.mediaEstimatorGpuStart = commandBuffer.GPUStartTime;
            r115Timing.mediaEstimatorGpuEnd = commandBuffer.GPUEndTime;
            r115Timing.mediaEstimatorWake = r119EstimatorWake;
            // R119 review finding 2: validate the clock assumption instead of
            // trusting it.
            //
            //  (a) The media clock and CLOCK_MONOTONIC must measure the same
            //      span. commit->wake (media only) is a subset of the estimator's
            //      CPU wall (monotonic), so the residual must be small and
            //      non-positive.
            //  (b) mediaWake - GPUEndTime is the completion signalling latency.
            //      With GPU_time = media + delta, this equals latency - delta, so:
            //        delta > 0  (GPU stamps ahead)  -> value goes negative -> caught
            //                                           by the -0.5 ms floor.
            //        delta < 0  (GPU stamps behind) -> value grows by |delta|, and
            //                                           commitToGpuStart shrinks by
            //                                           |delta|, which understates
            //                                           queue delay -- the one
            //                                           favourable direction. The
            //                                           2 ms ceiling bounds that
            //                                           understatement to 2 ms.
            //      A generous 50 ms ceiling would let an 8 ms domain offset turn a
            //      real 8 ms wait into a reported 0 ms while still printing
            //      clock=CONSISTENT, so the ceiling is deliberately tight.
            //
            //  (a) is structural: commit->wake is a sub-interval of the estimator's
            //      CPU wall, so the skew is non-positive by construction and its
            //      real job is to catch a rate mismatch (one clock in microseconds,
            //      say). The floor is loose for that reason; a slow encode between
            //      estimatorStartNs and the media commit legitimately widens it.
            const int64_t r119CpuWallNs = (int64_t)r115Timing.estimatorEndNs -
                (int64_t)r115Timing.estimatorStartNs;
            const double r119CommitToWake = r119EstimatorWake - r119EstimatorCommit;
            const int64_t r119CommitToWakeNs = (int64_t)llround(r119CommitToWake * 1e9);
            r115Timing.estimatorClockSkewNs = r119CommitToWakeNs - r119CpuWallNs;
            r115Timing.wakeMinusGpuEndNs = commandBuffer.GPUEndTime > 0.0
                ? (int64_t)llround((r119EstimatorWake - commandBuffer.GPUEndTime) * 1e9)
                : INT64_MIN;
            r115Timing.estimatorClockConsistent =
                r115Timing.wakeMinusGpuEndNs != INT64_MIN &&
                r115Timing.wakeMinusGpuEndNs >= -500000 &&
                r115Timing.wakeMinusGpuEndNs <= 2000000 &&
                r115Timing.estimatorClockSkewNs >= -50000000 &&
                r115Timing.estimatorClockSkewNs <= 2000000;
            r115Timing.mediaEstimatorValid = r115Timing.estimatorClockConsistent &&
                commandBuffer.GPUStartTime > 0.0 &&
                commandBuffer.GPUEndTime > commandBuffer.GPUStartTime;
            r115Timing.mediaHandoffValid =
                r115Timing.mediaHook > 0.0 && r115Timing.mediaBlitEncode > 0.0 &&
                r115Timing.mediaBlitCommit > 0.0 && r115Timing.mediaDispatch > 0.0 &&
                r115Timing.mediaGenQueueEntry > 0.0 && r115Timing.mediaGenStart > 0.0 &&
                r115Timing.mediaCopyGpuStart > 0.0 &&
                r115Timing.mediaCopyGpuEnd >= r115Timing.mediaCopyGpuStart;
        }
        pthread_mutex_unlock(&stateLock);
    }
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
        // R116: the gate decision is needed for exactly one pair shape, and that
        // shape is known before the transition runs. Deciding it here lets the
        // metric reduction ride the MetalFX submission instead of paying its own
        // commit -> wait round trip afterwards.
        pthread_mutex_lock(&stateLock);
        BOOL r116TargetPairKnown = r110PresentationEnabled() &&
            r110TupleSourceBSequence == sequence &&
            previousCaptureSequence + 1 == sequence;
        pthread_mutex_unlock(&stateLock);
        BOOL r116WantGate = r116TargetPairKnown;
        __block BOOL r116GateEncoded = NO;
        __block uint64_t r116GateEncodeNs = 0;
        uint64_t startNs = r105NowNs();
        FGMetalInternalFrame *internalFrame =
            [realFrameProvider interpolatePreviousColor:previousCaptureColor
                currentColor:color depthTexture:depth motionTexture:motion
                deltaTime:1.0f / 60.0f
                additionalEncoding:r116WantGate
                    ? ^(id<MTLCommandBuffer> gateCommandBuffer,
                        id<MTLTexture> interpolatedOutput) {
                        uint64_t encodeStartNs = r105NowNs();
                        r116GateEncoded = r116EncodeFastGate(gateCommandBuffer,
                            motion, previousCaptureColor, color,
                            interpolatedOutput);
                        r116GateEncodeNs = r105NowNs() - encodeStartNs;
                    }
                    : nil
                error:&error];
        uint64_t elapsedNs = r105NowNs() - startNs;
        // R116: the reduction result is already in shared memory because the
        // provider blocks on the transition command buffer that carried it. No
        // further GPU wait happens here.
        R110FastMetrics r116PreCollectedMetrics = {0};
        BOOL r116PreCollected = NO;
        if (r116GateEncoded && internalFrame && !error) {
            r116PreCollected = r116ReadGateResult(&r116PreCollectedMetrics);
        }
        if (r116WantGate) {
            logLine("R116_FAST_GATE=ENCODED pair=%llu->%llu encoded=%d encodeCpuNs=%llu transitionAndGateMs=%.3f metricsReady=%d resultSource=transition-command-buffer separateGpuRoundTrip=0",
                (unsigned long long)previousCaptureSequence,
                (unsigned long long)sequence, r116GateEncoded,
                (unsigned long long)r116GateEncodeNs, elapsedNs / 1e6,
                r116PreCollected);
        }
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
            r114NoteProviderWarm(previousCaptureSequence, sequence,
                                 internalFrame.internalPresentId);
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
            // R115: eligibility is bound to the reserved pair, not to a capture
            // window. The pair is exactly (reservedPairA, reservedPairB).
            BOOL sourceWindow = r115ContinuousEnabled()
                ? (r114LatchState == R114LatchReserved &&
                   r114ReservedPairA == previousCaptureSequence &&
                   r114ReservedPairB == sequence)
                : (r108CaptureTriggered &&
                   previousCaptureSequence > r108CaptureStartSequence &&
                   sequence <= r108CaptureEndSequence);
            BOOL frontierReady = r103FrontierPairA == previousCaptureSequence &&
                r103FrontierPairB == sequence &&
                r103SourceFrontierState == R103SourceFrontierClosed;
            BOOL slotAvailable = !r110QualityWaitStarted &&
                !pendingInternalPresentationTexture;
            BOOL candidateWatch = r114LatchEnabled() && !r115ContinuousEnabled() &&
                r114LatchState == R114LatchArmed;
            BOOL gateGenerationCurrent = !r115ContinuousEnabled() ||
                r115Generation == r115Timing.generation;
            pthread_mutex_unlock(&stateLock);

            if (!targetPair && candidateWatch) {
                // R114 candidate watch. Reuses the accepted R110 estimator field
                // and the frozen motion predicate (finite, activeFraction>=0.15,
                // maxMagnitude<=256) that already gates the title tuple. It adds
                // no threshold, no second estimator pass and no external input;
                // it only tells the in-process latch that real camera motion is
                // present so the *next* not-yet-committed pair can be reserved.
                R110FastMetrics watchMetrics = {0};
                double watchGpuMs = 0.0;
                NSString *watchError = nil;
                BOOL watchComplete = commandBuffer.status == MTLCommandBufferStatusCompleted &&
                    commandBuffer.error == nil && textureValid &&
                    r110CollectFastMetrics(motion, previousCaptureColor, color,
                        internalFrame.texture, &watchMetrics, &watchGpuMs, &watchError);
                double watchFraction = watchMetrics.motionSamples
                    ? (double)watchMetrics.activeMotion / (double)watchMetrics.motionSamples : 0.0;
                BOOL watchFinite = watchComplete &&
                    watchMetrics.motionSamples == width * height &&
                    watchMetrics.finiteMotion == watchMetrics.motionSamples &&
                    isfinite(watchMetrics.maxMagnitude) && watchMetrics.maxMagnitude <= 256.0f;
                BOOL watchMoving = watchFinite && watchFraction >= 0.15;
                if (watchMoving)
                    r114NoteMotionObservation(previousCaptureSequence, sequence,
                                              watchFraction);
            }

            if (targetPair) {
                if (r115ContinuousEnabled()) {
                    pthread_mutex_lock(&stateLock);
                    r115Timing.gateStartNs = r105NowNs();
                    r115Timing.metalfxStartNs = startNs;
                    r115Timing.metalfxEndNs = startNs + elapsedNs;
                    pthread_mutex_unlock(&stateLock);
                    // R121: MetalFX's own bracket. startNs is a CLOCK_MONOTONIC
                    // sample and elapsedNs is a duration measured on the same
                    // clock, so both are chain-legal.
                    r121MarkValue(R121SlotMetalFXStart, startNs);
                    r121MarkValue(R121SlotMetalFXEnd, startNs + elapsedNs);
                    r115NotePhase(R115PhaseProcessing, "gate");
                }
                R110FastMetrics metrics = {0};
                double fastGpuMs = 0.0;
                NSString *metricsError = nil;
                BOOL estimatorCompleted = commandBuffer.status == MTLCommandBufferStatusCompleted &&
                    commandBuffer.error == nil;
                // R116: the gate now rides the transition submission. The CPU
                // only reads one 40-byte struct; there is no second command
                // buffer, no waitUntilCompleted and no per-group CPU loop. If the
                // encode could not be issued the metrics stay unavailable, which
                // is the same failure the legacy collector reported.
                BOOL metricsComplete = NO;
                if (r116GateEncoded && r116PreCollected) {
                    metrics = r116PreCollectedMetrics;
                    metricsComplete = estimatorCompleted && textureValid;
                } else {
                    metricsComplete = estimatorCompleted && textureValid &&
                        r110CollectFastMetrics(motion, previousCaptureColor, color,
                            internalFrame.texture, &metrics, &fastGpuMs, &metricsError);
                    if (!metricsError)
                        metricsError = r116GateEncoded ? nil : @"gate-not-encoded-in-transition";
                }
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
                BOOL characterAttributed = r112CharacterPairAttributionVerified(
                    previousCaptureSequence, sequence);
                BOOL inputAttributed = cameraAttributed && characterAttributed;
                BOOL fastEligible = metricsComplete && estimatorCompleted &&
                    textureValid && sourceWindow && frontierReady && slotAvailable &&
                    worldConfirmed && inputAttributed && motionFinite && motionMoving && outputFinite &&
                    outputDistinct && gateGenerationCurrent;
                const char *reason = !worldConfirmed ? "world-scene-not-confirmed" :
                    !inputAttributed ? "event-locked-input-not-attributed-across-source-pair" :
                    !gateGenerationCurrent ? "tuple-generation-changed-during-estimator" :
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
                logLine("R110_FAST_PRESENT_ELIGIBILITY=%s pair=%llu->%llu internalPresentId=%llu sourceWindow=%d worldSceneConfirmed=%d inputAttributed=%d estimatorCompleted=%d motionFinite=%d motionSamples=%llu finiteMotion=%llu activeOver0_5px=%llu activeFraction=%.6f maxMagnitude=%.6f outputFinite=%d outputSamples=%llu finiteOutput=%llu colorSamples=%llu meanAbsDiffA=%.6f meanAbsDiffB=%.6f thresholds=activeFraction>=0.15,endpointDifference>=0.002 maxMagnitude<=256 fastMetricsGpuMs=%.3f reason=%s detail=%s gatePath=%s",
                    fastEligible ? "ACCEPTED" : "REJECTED",
                    (unsigned long long)previousCaptureSequence,
                    (unsigned long long)sequence,
                    (unsigned long long)internalFrame.internalPresentId,
                    sourceWindow, worldConfirmed, inputAttributed, estimatorCompleted, motionFinite,
                    (unsigned long long)metrics.motionSamples,
                    (unsigned long long)metrics.finiteMotion,
                    (unsigned long long)metrics.activeMotion, activeFraction,
                    metrics.maxMagnitude, outputFinite,
                    (unsigned long long)metrics.outputSamples,
                    (unsigned long long)metrics.finiteOutput,
                    (unsigned long long)metrics.colorSamples,
                    metrics.meanAbsDifferenceFromPrevious,
                    metrics.meanAbsDifferenceFromCurrent, fastGpuMs, reason,
                    metricsError.UTF8String ?: "none",
                    (r116GateEncoded && r116PreCollected)
                        ? "transition-command-buffer" : "legacy-separate-command-buffer");
                if (r115ContinuousEnabled()) {
                    uint64_t r118Snapshot[11];
                    pthread_mutex_lock(&stateLock);
                    r115Timing.gateEndNs = r105NowNs();
                    r121MarkValue(R121SlotGateVerdict, r115Timing.gateEndNs);
                    if (!fastEligible) r115Timing.gateRejected = YES;
                    r115Timing.gsEndNs = r115Timing.gateEndNs;
                    uint64_t gateCpuNs = r115Timing.gateEndNs - r115Timing.gateStartNs;
                    // R118: snapshot the whole service window under one lock so
                    // the components are consistent with each other even though
                    // they were written from this thread at different times.
                    R115TupleTiming r118TimingSnapshot = r115Timing;
                    pthread_mutex_unlock(&stateLock);
                    r118Snapshot[0] = (r118TimingSnapshot.bAcquiredNs && r118TimingSnapshot.gsStartNs)
                        ? r118TimingSnapshot.gsStartNs - r118TimingSnapshot.bAcquiredNs : 0;              // copy + dispatch hop
                    r118Snapshot[1] = (r118TimingSnapshot.estimatorStartNs && r118TimingSnapshot.gsStartNs)
                        ? r118TimingSnapshot.estimatorStartNs - r118TimingSnapshot.gsStartNs : 0;         // preparation
                    r118Snapshot[2] = (r118TimingSnapshot.estimatorEndNs && r118TimingSnapshot.estimatorStartNs)
                        ? r118TimingSnapshot.estimatorEndNs - r118TimingSnapshot.estimatorStartNs : 0;    // estimator (CPU wall)
                    r118Snapshot[3] = (r118TimingSnapshot.metalfxStartNs && r118TimingSnapshot.estimatorEndNs)
                        ? r118TimingSnapshot.metalfxStartNs - r118TimingSnapshot.estimatorEndNs : 0;      // diagnostics + logging
                    r118Snapshot[4] = (r118TimingSnapshot.metalfxEndNs && r118TimingSnapshot.metalfxStartNs)
                        ? r118TimingSnapshot.metalfxEndNs - r118TimingSnapshot.metalfxStartNs : 0;        // MetalFX transition
                    r118Snapshot[5] = (r118TimingSnapshot.gateEndNs && r118TimingSnapshot.metalfxEndNs)
                        ? r118TimingSnapshot.gateEndNs - r118TimingSnapshot.metalfxEndNs : 0;             // gate decision
                    r118Snapshot[6] = (r118TimingSnapshot.gsEndNs && r118TimingSnapshot.gsStartNs)
                        ? r118TimingSnapshot.gsEndNs - r118TimingSnapshot.gsStartNs : 0;                  // generation-thread service
                    r118Snapshot[7] = (r118TimingSnapshot.gsEndNs && r118TimingSnapshot.bAcquiredNs)
                        ? r118TimingSnapshot.gsEndNs - r118TimingSnapshot.bAcquiredNs : 0;                // end-to-end service
                    r118Snapshot[8] = (commandBuffer.GPUEndTime > commandBuffer.GPUStartTime)
                        ? (uint64_t)((commandBuffer.GPUEndTime - commandBuffer.GPUStartTime) * 1e9)
                        : 0;                                            // estimator GPU execution
                    // Primary FG-attributable service window. bPresentHookNs is
                    // the B present hook, which is where FG first touches the
                    // pair (the blit is encoded there). Starting here excludes
                    // the game's own render time for B, which bAcquiredNs
                    // includes because nextDrawable returns before the game has
                    // drawn into the drawable.
                    r118Snapshot[9] = (r118TimingSnapshot.gateEndNs && r118TimingSnapshot.bPresentHookNs)
                        ? r118TimingSnapshot.gateEndNs - r118TimingSnapshot.bPresentHookNs : 0;
                    // Self-check: the six components must reconstruct
                    // genThreadServiceNs exactly. Any drift means a gap in the
                    // decomposition, and a published decomposition that does not
                    // add up is worse than no decomposition.
                    // [1]..[5] only: [0] is bAcquiredToGenStart, which sits
                    // BEFORE gsStart and is therefore not part of
                    // genThreadServiceNs. Summing [0] as well would make the
                    // check fail by exactly the copy+dispatch hop.
                    r118Snapshot[10] = r118Snapshot[1] + r118Snapshot[2] +
                        r118Snapshot[3] + r118Snapshot[4] + r118Snapshot[5];
                    logLine("R116_GATE_TIMING pair=%llu->%llu gateCpuDecisionNs=%llu gateCpuDecisionMs=%.4f transitionAndGateMs=%.3f gateEncodeCpuNs=%llu result=ACCEPTED_OR_REJECTED_CPU_ONLY_WAIT",
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        (unsigned long long)gateCpuNs, gateCpuNs / 1e6,
                        elapsedNs / 1e6,
                        (unsigned long long)r116GateEncodeNs);
                    if (r118TimingSnapshot.gsStartNs == 0) {
                        // The pair reached the gate without ever entering the
                        // generation path with a recorded entry timestamp. Emit
                        // an explicit marker instead of a row of zeros, because
                        // a zero row is indistinguishable from a real
                        // sub-microsecond service time once it is in a log.
                        logLine("R118_GENERATION_SERVICE=SKIPPED generation=%llu pair=%llu->%llu reason=no-generation-entry-timestamp",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence);
                    } else {
                    logLine("R118_GENERATION_SERVICE generation=%llu pair=%llu->%llu outcome=%s gatePath=%s "
                            "presentHookToGateNs=%llu bAcquiredToGenStartNs=%llu prepNs=%llu estimatorNs=%llu "
                            "estimatorToMetalFxNs=%llu metalFxNs=%llu gateDecisionNs=%llu "
                            "genThreadServiceNs=%llu endToEndServiceNs=%llu "
                            "estimatorGpuNs=%llu estimatorCpuMinusGpuNs=%llu componentsSumNs=%llu "
                            "bAcquiredAtMonotonicNs=%llu bPresentHookAtMonotonicNs=%llu "
                            "gsStartNs=%llu gsEndNs=%llu",
                        (unsigned long long)r118TimingSnapshot.generation,
                        (unsigned long long)previousCaptureSequence,
                        (unsigned long long)sequence,
                        fastEligible ? "ACCEPTED" : "REJECTED",
                        (r116GateEncoded && r116PreCollected)
                            ? "transition-command-buffer" : "legacy-separate-command-buffer",
                        (unsigned long long)r118Snapshot[9],
                        (unsigned long long)r118Snapshot[0],
                        (unsigned long long)r118Snapshot[1],
                        (unsigned long long)r118Snapshot[2],
                        (unsigned long long)r118Snapshot[3],
                        (unsigned long long)r118Snapshot[4],
                        (unsigned long long)r118Snapshot[5],
                        (unsigned long long)r118Snapshot[6],
                        (unsigned long long)r118Snapshot[7],
                        (unsigned long long)r118Snapshot[8],
                        (unsigned long long)(r118Snapshot[2] > r118Snapshot[8]
                            ? r118Snapshot[2] - r118Snapshot[8] : 0),
                        (unsigned long long)r118Snapshot[10],
                        (unsigned long long)r118TimingSnapshot.bAcquiredNs,
                        (unsigned long long)r118TimingSnapshot.bPresentHookNs,
                        (unsigned long long)r118TimingSnapshot.gsStartNs,
                        (unsigned long long)r118TimingSnapshot.gsEndNs);
                    }
                    // -----------------------------------------------------------------
                    // R119 Phase 2 -- capture / handoff decomposition.
                    //
                    // Eight intervals, each a difference of two CACurrentMediaTime
                    // instants, so the CPU encode work, the GPU copy, the copy's own
                    // queue wait and the CPU queue hops are separable. This is what
                    // answers "where does bAcquiredToGenStart go" instead of
                    // attributing the whole interval to "copy".
                    // -----------------------------------------------------------------
                    if (r118TimingSnapshot.mediaHandoffValid) {
                        logLine("R119_HANDOFF generation=%llu pair=%llu->%llu clock=media "
                                "hookToCaptureEntryNs=%llu captureEntryToBlitCommitNs=%llu "
                                "blitCommitToCopyGpuStartNs=%llu copyGpuExecNs=%llu "
                                "copyGpuEndToWakeNs=%llu wakeToDispatchNs=%llu "
                                "dispatchToGenQueueEntryNs=%llu genQueueEntryToGenStartNs=%llu "
                                "hookToGenStartNs=%llu hookToCopyGpuStartNs=%llu",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaBlitEncode, r118TimingSnapshot.mediaHook),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaBlitCommit, r118TimingSnapshot.mediaBlitEncode),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaCopyGpuStart, r118TimingSnapshot.mediaBlitCommit),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaCopyGpuEnd, r118TimingSnapshot.mediaCopyGpuStart),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaCopyWake, r118TimingSnapshot.mediaCopyGpuEnd),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaDispatch, r118TimingSnapshot.mediaCopyWake),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaGenQueueEntry, r118TimingSnapshot.mediaDispatch),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaGenStart, r118TimingSnapshot.mediaGenQueueEntry),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaGenStart, r118TimingSnapshot.mediaHook),
                            (unsigned long long)fgmetalMediaDeltaNs(
                                r118TimingSnapshot.mediaCopyGpuStart, r118TimingSnapshot.mediaHook));
                    } else {
                        logLine("R119_HANDOFF=SKIPPED generation=%llu pair=%llu->%llu reason=incomplete-media-clock-chain",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence);
                    }
                    // -----------------------------------------------------------------
                    // R119 Phase 3 -- estimator queue delay versus GPU execution.
                    //
                    // commitToGpuStartNs is scheduling: the command buffer existed
                    // and was submitted, but the GPU had not begun it. gpuExecNs is
                    // the kernel's own execution. They are reported separately
                    // because only one of them responds to estimator arithmetic.
                    //
                    // The split is only published when the clock assumption that
                    // makes it meaningful has been checked. wakeMinusGpuEndNs is
                    // the signed residual of mediaWake - GPUEndTime and is printed
                    // unclamped so a domain mismatch is visible rather than
                    // silently absorbed.
                    // -----------------------------------------------------------------
                    if (r118TimingSnapshot.mediaEstimatorValid) {
                        const uint64_t r119QueueNs = fgmetalMediaDeltaNs(
                            r118TimingSnapshot.mediaEstimatorGpuStart,
                            r118TimingSnapshot.mediaEstimatorCommit);
                        const uint64_t r119GpuNs = fgmetalMediaDeltaNs(
                            r118TimingSnapshot.mediaEstimatorGpuEnd,
                            r118TimingSnapshot.mediaEstimatorGpuStart);
                        const uint64_t r119WakeNs = fgmetalMediaDeltaNs(
                            r118TimingSnapshot.mediaEstimatorWake,
                            r118TimingSnapshot.mediaEstimatorGpuEnd);
                        const uint64_t r119CommitToWakeNs = fgmetalMediaDeltaNs(
                            r118TimingSnapshot.mediaEstimatorWake,
                            r118TimingSnapshot.mediaEstimatorCommit);
                        logLine("R119_ESTIMATOR_QUEUE generation=%llu pair=%llu->%llu clock=media "
                                "commitToGpuStartNs=%llu gpuExecNs=%llu gpuEndToWakeNs=%llu "
                                "commitToWakeNs=%llu cpuWallNs=%llu queueShareOfWall=%.4f "
                                "wakeMinusGpuEndNs=%lld clockSkewNs=%lld clock=CONSISTENT",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            (unsigned long long)r119QueueNs,
                            (unsigned long long)r119GpuNs,
                            (unsigned long long)r119WakeNs,
                            (unsigned long long)r119CommitToWakeNs,
                            (unsigned long long)r118Snapshot[2],
                            r119CommitToWakeNs > 0
                                ? (double)r119QueueNs / (double)r119CommitToWakeNs : 0.0,
                            (long long)r118TimingSnapshot.wakeMinusGpuEndNs,
                            (long long)r118TimingSnapshot.estimatorClockSkewNs);
                    } else if (r118TimingSnapshot.wakeMinusGpuEndNs != INT64_MIN) {
                        logLine("R119_ESTIMATOR_QUEUE=CLOCK_MISMATCH generation=%llu pair=%llu->%llu "
                                "wakeMinusGpuEndNs=%lld clockSkewNs=%lld reason=gpu-and-media-clock-"
                                "domains-disagree action=split-not-published",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            (long long)r118TimingSnapshot.wakeMinusGpuEndNs,
                            (long long)r118TimingSnapshot.estimatorClockSkewNs);
                    } else {
                        logLine("R119_ESTIMATOR_QUEUE=SKIPPED generation=%llu pair=%llu->%llu reason=no-gpu-timestamps",
                            (unsigned long long)r118TimingSnapshot.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence);
                    }
                    if (!fastEligible)
                        logLine("R115_GATE_REJECTED generation=%llu pair=%llu->%llu reason=%s result=SAFE_SOURCE_FALLBACK gate=frozen-r110",
                            (unsigned long long)r115Timing.generation,
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence, reason);
                }
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
                    // R119 review finding 1: this line used to name the A and B
                    // source raw files unconditionally. With bounded diagnostics
                    // they are not written for most tuples, and even inside the
                    // sample a dark frame fails verification, so naming them would
                    // assert an artefact that does not exist. Name a path only for
                    // a sequence that actually produced one.
                    const BOOL r119ArtefactsPresent = r119SequenceVerified(previousCaptureSequence) &&
                        r119SequenceVerified(sequence);
                    if (r119ArtefactsPresent) {
                        logLine("R110_PRESENT_M_READY pair=%llu->%llu internalPresentId=%llu sourceA=%s/source-sequence-%llu.raw sourceB=%s/source-sequence-%llu.raw output=GPU-resident fastEligibility=ACCEPTED slowDiagnostics=after-attempt width=%lu height=%lu depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            (unsigned long long)internalFrame.internalPresentId,
                            fgmetalEvidenceDirectory(),
                            (unsigned long long)previousCaptureSequence,
                            fgmetalEvidenceDirectory(),
                            (unsigned long long)sequence, (unsigned long)width,
                            (unsigned long)height);
                    } else {
                        logLine("R110_PRESENT_M_READY pair=%llu->%llu internalPresentId=%llu sourceA=%s sourceB=%s output=GPU-resident fastEligibility=ACCEPTED slowDiagnostics=after-attempt width=%lu height=%lu depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
                            (unsigned long long)previousCaptureSequence,
                            (unsigned long long)sequence,
                            (unsigned long long)internalFrame.internalPresentId,
                            "<not-exported:r119-bounded-sample>",
                            "<not-exported:r119-bounded-sample>",
                            (unsigned long)width, (unsigned long)height);
                    }
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
                    r114MarkPairFailed("fast-eligibility-rejected-or-publication-race");
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
                r114MarkPairFailed("metalfx-transition-unavailable");
            }
        }
    } else if (r115ContinuousEnabled()) {
        // R115: the reserved B was captured without a usable seed A (A's copy or
        // processing did not land). No M can exist for this pair, so B is
        // forwarded immediately instead of waiting for the recovery deadline.
        pthread_mutex_lock(&stateLock);
        BOOL reservedB = r114LatchState == R114LatchReserved &&
            r114ReservedPairB == sequence;
        pthread_mutex_unlock(&stateLock);
        if (reservedB) {
            logLine("R115_SEED_MISSING generation=%llu pairB=%llu previousCaptureSequence=%llu action=FORWARD_B_IMMEDIATELY",
                (unsigned long long)r115Generation,
                (unsigned long long)sequence,
                (unsigned long long)previousCaptureSequence);
            r102FallbackForSequence(sequence, "reserved-pair-b-without-seed-a");
            r114MarkPairFailed("reserved-pair-b-without-seed-a");
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

    // R119 Phase 2: handoff decomposition. This function is entered from the
    // producer command buffer's completion handler, so its entry instant is the
    // producer-complete boundary, not the present hook.
    const double r119HandoffEncode = fgmetalMediaNow();
    BOOL r119IsPairB = NO;
    if (r115ContinuousEnabled()) {
        pthread_mutex_lock(&stateLock);
        r119IsPairB = r115Timing.pairB == sourceSequence;
        if (r119IsPairB) {
            const uint64_t r121CaptureEntryNs = r105NowNs();
            r115Timing.bProducerCompletedNs = r121CaptureEntryNs;
            r115Timing.mediaProducerComplete = r119HandoffEncode;
            r115Timing.mediaBlitEncode = r119HandoffEncode;
            r121MarkValue(R121SlotSrcCaptureEntry, r121CaptureEntryNs);
        }
        pthread_mutex_unlock(&stateLock);
    }

    pthread_mutex_lock(&stateLock);
    // R115 has no capture window: the per-window 48-frame budget is replaced by
    // the reserved-pair selection, so a bounded run is never truncated by it.
    BOOL continuousCapture = r115ContinuousEnabled();
    if (!continuousCapture && r108CaptureCount >= kR108CaptureLimit) {
        pthread_mutex_unlock(&stateLock);
        return;
    }
    ++r108CaptureCount;
    BOOL captureWindowSourceComplete = !continuousCapture &&
        r108CaptureCount == kR108CaptureLimit;
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
    // R114 search phase: until a pair is reserved, skip the per-frame CPU
    // readback and raw file writes. The GPU product path (private source copy,
    // estimator, MetalFX, gate) is untouched; only diagnostics are deferred so a
    // long bounded search window stays affordable. The reserved pair and its
    // endpoint frames are captured normally because reservation precedes A's
    // producer completion. R115 only ever captures the reserved pair and the
    // warm-up pair, so diagnostics are always wanted there.
    BOOL r114SearchPhase = r114LatchEnabled() && !r115ContinuousEnabled() &&
        r114LatchState != R114LatchReserved;
    // R119 Phase 1: outside the bounded artefact sample there is no diagnostic
    // texture, so the extra GPU blit, the 5.6 MB CPU readback, the luma sweep
    // and the raw file write all disappear from this frame. The private product
    // copy below is unconditional and is the only thing the pipeline consumes.
    const BOOL r119ExportWanted = r119DiagnosticsWanted();
    MTLTextureDescriptor *diagnosticDescriptor = r114SearchPhase ? nil :
        [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:source.pixelFormat
        width:width height:height mipmapped:NO];
    diagnosticDescriptor.storageMode = MTLStorageModeShared;
    diagnosticDescriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> diagnosticTexture = (r114SearchPhase || !r119ExportWanted) ? nil :
        [device newTextureWithDescriptor:diagnosticDescriptor];
    id<MTLCommandBuffer> copyCommandBuffer = [queue commandBuffer];
    id<MTLBlitCommandEncoder> blit = copyCommandBuffer
        ? [copyCommandBuffer blitCommandEncoder] : nil;
    if (!captureTexture || !copyCommandBuffer || !blit) {
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
    if (diagnosticTexture) {
        [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
                sourceOrigin:MTLOriginMake(0, 0, 0)
                  sourceSize:MTLSizeMake(width, height, 1)
                   toTexture:diagnosticTexture destinationSlice:0 destinationLevel:0
            destinationOrigin:MTLOriginMake(0, 0, 0)];
    }
    [blit endEncoding];
    // R119 Phase 2: the blit is fully encoded; the next instant is its commit.
    const double r119BlitCommit = fgmetalMediaNow();
    if (r119IsPairB) {
        pthread_mutex_lock(&stateLock);
        r115Timing.mediaBlitCommit = r119BlitCommit;
        pthread_mutex_unlock(&stateLock);
        r121Span.mediaSrcCopyCommit = r119BlitCommit;
        // R121: gated on the pair's B. A's own capture runs inside the same
        // armed span and would otherwise claim this slot first.
        r121Mark(R121SlotSrcCopyEncoded);
    }
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
            // R119 Phase 2: the copy's own GPU window and the CPU wake that
            // follows it, both on the media clock.
            const double r119CopyWake = fgmetalMediaNow();
            if (r119IsPairB) {
                const uint64_t r121CopyWakeNs = r105NowNs();
                pthread_mutex_lock(&stateLock);
                r115Timing.mediaCopyGpuStart = completedCopy.GPUStartTime;
                r115Timing.mediaCopyGpuEnd = completedCopy.GPUEndTime;
                r115Timing.mediaCopyWake = r119CopyWake;
                pthread_mutex_unlock(&stateLock);
                r121Span.srcCopyGpuStartNs = fgmetalMediaToNs(completedCopy.GPUStartTime);
                r121Span.srcCopyGpuEndNs = fgmetalMediaToNs(completedCopy.GPUEndTime);
                r121Span.mediaSrcCopyWake = r119CopyWake;
                r121MarkValue(R121SlotSrcCopyWake, r121CopyWakeNs);
            }
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
            // R119 Phase 2: the dispatch instant, taken immediately before the
            // enqueue so the queue-wait interval has a defined start.
            const double r119Dispatch = fgmetalMediaNow();
            if (r119IsPairB) {
                pthread_mutex_lock(&stateLock);
                r115Timing.mediaDispatch = r119Dispatch;
                pthread_mutex_unlock(&stateLock);
                r121Mark(R121SlotTaskEnqueue);
            }
            dispatch_async(captureProcessingQueue, ^{
                @autoreleasepool {
                    // R119 Phase 2: entry to the generation serial queue. The gap
                    // between the dispatch above and this instant is pure queue
                    // wait, not GPU work.
                    const double r119GenQueueEntry = fgmetalMediaNow();
                    if (r119IsPairB) {
                        pthread_mutex_lock(&stateLock);
                        r115Timing.mediaGenQueueEntry = r119GenQueueEntry;
                        pthread_mutex_unlock(&stateLock);
                        r121Mark(R121SlotTaskStart);
                    }
                    processCapturedFrame(sourceSequence, drawable.drawableID,
                        source.pixelFormat, captureTexture, device,
                        conditionLabel);
                }
            });
            if (!diagnosticTexture) {
                pthread_mutex_lock(&stateLock);
                ++captureSuccesses;
                pthread_mutex_unlock(&stateLock);
                return;
            }
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
            char capturePath[1024] = {0};
            const BOOL capturePathReady = fgmetalEvidencePath(capturePath,
                sizeof(capturePath), "source-sequence-%llu.raw",
                (unsigned long long)sourceSequence);
            BOOL saved = verified && capturePathReady &&
                fgmetalWriteEvidenceAbsolute(capturePath, readback.bytes,
                                             readback.length);
            if (saved) r119NoteVerifiedSequence(sourceSequence);
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
    // R121: the commit instant, immediately before the commit that actually
    // hands the SOURCE copy to the GPU. Everything the copy then spends before
    // GPUStartTime is submission latency, never execution.
    if (r119IsPairB) r121Mark(R121SlotSrcCopyCommit);
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
        // R119: this writer used to target the R106 corpus directory by absolute
        // path, which is the same isolation defect class as kR115EvidenceDirectory.
        // It now resolves through the shared accessor like every other writer.
        char samplePath[1024] = {0};
        const BOOL pathReady = fgmetalEvidencePath(samplePath, sizeof(samplePath),
            "depth-sequence-%llu.raw", (unsigned long long)sequence);
        BOOL saved = pathReady && fgmetalWriteEvidenceAbsolute(samplePath, values,
            sampleCount * sizeof(float));
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
        // R119: same accessor routing as the depth writer above.
        char samplePath[1024] = {0};
        const BOOL pathReady = fgmetalEvidencePath(samplePath, sizeof(samplePath),
            "motion-sequence-%llu.rg32f", (unsigned long long)sequence);
        BOOL saved = pathReady && fgmetalWriteEvidenceAbsolute(samplePath, values,
            gridCount * sizeof(float) * 2);
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
    // R114: publish the committed sequence before the frontier close reads the
    // reserved target. Safety comes from reserving pairB = lastCommitted+2, i.e.
    // two commits of lead, which keeps B on the uncommitted side of the seam.
    if (sourceRecord) {
        uint64_t committedSequence = [sourceRecord[@"sequence"] unsignedLongLongValue];
        pthread_mutex_lock(&stateLock);
        if (committedSequence > r114LastCommittedSourceSequence)
            r114LastCommittedSourceSequence = committedSequence;
        pthread_mutex_unlock(&stateLock);
    }
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
    // R119 Phase 0: resolve the evidence directory and run the isolation self
    // test before anything can write. This is the first thing the library does
    // in any process, so a misconfigured run is visible in the log before the
    // game has drawn a single frame.
    fgmetalEvidenceSelfTest();
    logLine("R121_EVIDENCE_DIRECTORY pid=%d source=%s path=%s writable=%d protected=%d "
            "override=%s allowLegacy=%s own=%s",
        getpid(), gFGMetalEvidenceDirectorySource, fgmetalEvidenceDirectory(),
        fgmetalEvidenceDiagnosticsAllowed() ? 1 : 0,
        fgmetalEvidenceDirectoryIsProtected(fgmetalEvidenceDirectory()) ? 1 : 0,
        getenv("FGMETAL_EVIDENCE_DIR") ?: "<unset>",
        getenv("FGMETAL_ALLOW_LEGACY_EVIDENCE_DIR") ?: "<unset>",
        kR121EvidenceDirectory);
    // R120 is now a historical corpus from R121's point of view. Logged rather
    // than assumed, so the boundary is visible in the run record.
    logLine("R121_EVIDENCE_BOUNDARY pid=%d own=%s priorRoundProtected=%s priorRoundWritable=%d",
        getpid(), kR121EvidenceDirectory, kR119EvidenceDirectory,
        fgmetalPathIsWithin(kR119EvidenceDirectory, kR121EvidenceDirectory) ? 1 : 0);
    if (!fgmetalEvidenceDiagnosticsAllowed()) {
        logLine("R121_EVIDENCE_DIRECTORY=REFUSED pid=%d path=%s reason=protected-historical-corpus "
                "action=diagnostics-disabled product-path=unaffected",
            getpid(), fgmetalEvidenceDirectory());
    }
    logLine("R119_DIAGNOSTIC_POLICY pid=%d mode=%s env=%s boundedSample=first-middle-last "
            "timingPerTuple=1 gatePerTuple=1 accountingPerTuple=1 wsiPerTuple=1",
        getpid(), r119FullDiagnosticsMode() ? "full" : "bounded",
        getenv("FGMETAL_DIAG_SAMPLE") ?: "<unset>");
    // Arm the pacer at load rather than on the first present, so the run record
    // states the pacing mode before the game has drawn anything and a controller
    // preflight cannot race the first frame.
    (void)r120PaceEnabled();
    // R120 Phase 2 configuration banner. The continuous pipeline and the pacer
    // are independent switches, and the round is only meaningful when both are
    // on: the pacer alone measures SOURCE cadence with nothing generated, and
    // the pipeline alone reproduces R119's throughput deficit. Stating the pair
    // explicitly lets the controller refuse a run that would measure the wrong
    // thing instead of reporting a result for it.
    if (r120PaceActive && r115ContinuousEnabled()) {
        logLine("R120_CONTINUOUS_ENABLED pid=%d sourceHz=%.4f sourcePeriodNs=%llu "
                "boundedTuples=%llu order=A-M-B lookahead=deferred-B "
                "mAndBConsecutive=%d frozen=estimator-r117,gate-r116,earlyB-r116,watchdog-r116,tuple-r115",
            getpid(), r120PaceHzValue, (unsigned long long)r120PacePeriodNs,
            (unsigned long long)r115MaxTupleCount(),
            r116EarlyBDispatchEnabled() ? 1 : 0);
    } else {
        logLine("R120_CONTINUOUS_ENABLED=NO pid=%d pacer=%d continuous=%d "
                "reason=both-required-for-a-30-to-60-claim",
            getpid(), r120PaceActive ? 1 : 0, r115ContinuousEnabled() ? 1 : 0);
    }
    const char *selfTestOnly = getenv("FGMETAL_EVIDENCE_SELFTEST");
    if (selfTestOnly && strcmp(selfTestOnly, "1") == 0) {
        logLine("R120_EVIDENCE_SELFTEST=ONLY pid=%d hooks=skipped", getpid());
        return;
    }
    const char *orderingOnly = getenv("FGMETAL_R110_ORDERING_ONLY");
    r110OrderingOnly = orderingOnly && strcmp(orderingOnly, "1") == 0;
    captureProcessingQueue = dispatch_queue_create(
        "com.fgmetal.r100.capture-processing", DISPATCH_QUEUE_SERIAL);
    logLine("LOADED pid=%d ppid=%d path=%s target=%s targetArgMatch=%d mode=R114-inprocess-target-latch-title-presentation orderingOnly=%d presentation=%d r111CameraAttribution=%s r112CharacterAttribution=%s r114InprocessLatch=%s DYLD_INSERT_LIBRARIES=%s framebufferOnly=%s titleCapture=%s processingQueue=%p",
        getpid(), getppid(), procPath(), kHarnessExecutable,
        isSelectedProcess(),
        r110OrderingOnly,
        r110PresentationEnabled(),
        getenv("FGMETAL_R111_CAMERA_ATTRIBUTION") ?: "0",
        getenv("FGMETAL_R112_CHARACTER_ATTRIBUTION") ?: "0",
        getenv("FGMETAL_R114_INPROCESS_LATCH") ?: "0",
        getenv("DYLD_INSERT_LIBRARIES") ?: "<unset>",
        getenv("FGMETAL_R108_FRAMEBUFFER_ONLY") ?: "0",
        getenv("FGMETAL_R108_CAPTURE") ?: "0",
        (__bridge void *)captureProcessingQueue);
    if (r114LatchEnabled()) {
        logLine("R114_LATCH=IDLE pid=%d state=IDLE armRequestPath=%s externalPairSelection=NONE captureWindowFrames=%lu",
            getpid(), kR114ArmRequestPath, (unsigned long)kR108CaptureLimit);
    }
    if (r115ContinuousEnabled()) {
        logLine("R115_CONTINUOUS=ENABLED pid=%d state=IDLE boundedTuples=%llu startRequestPath=%s capturePolicy=reserved-pair-only rearm=AUTOMATIC generation=%llu deadlockRegression=ASYNC-B-FORWARD-REQUIRED",
            getpid(), (unsigned long long)r115MaxTupleCount(),
            kR115StartRequestPath, (unsigned long long)r115Generation);
        logLine("R116_CRITICAL_PATH=ENABLED gatePath=transition-command-buffer separateGateGpuRoundTrip=0 earlyBDispatch=%d earlyBDispatchEnv=%s watchdogBoundMs=%llu watchdogEnv=%s gateSemantics=frozen-r110",
            r116EarlyBDispatchEnabled() ? 1 : 0,
            getenv("FGMETAL_R116_B_EARLY") ?: "unset",
            (unsigned long long)(r116CommittedBWatchdogNs() / 1000000ULL),
            getenv("FGMETAL_R116_WATCHDOG_MS") ?: "unset");
    }
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
