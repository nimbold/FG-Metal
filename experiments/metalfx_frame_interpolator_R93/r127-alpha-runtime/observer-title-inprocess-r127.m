#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <MetalFX/MetalFX.h>
#import <QuartzCore/CAMetalLayer.h>
#import <CoreGraphics/CoreGraphics.h>
#import <ImageIO/ImageIO.h>
#import <QuartzCore/CATransaction.h>
#import "MetalFXFrameInterpolationProvider.h"
#import "r116_gate_kernels.h"
#import "R123DisplayPump.h"
#include "R127LatencyStats.h"
#include "R127SourceAck.h"
#include "R127SourceOwnership.h"
#import <objc/runtime.h>
#import <libproc.h>
#import <dlfcn.h>
#import <dispatch/dispatch.h>
#import <mach-o/dyld.h>
#import <fcntl.h>
#import <pthread.h>
#import <pthread/qos.h>
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
static uint64_t r127HealthyLogSuppressed;
static uint64_t r127ResidualLogSuppressed;
static uint64_t r127AnomalyLogSamples;
static uint64_t r127AnomalyLogSuppressed;
static uint64_t r127UnclassifiedLogSamples;
static uint64_t r127UnclassifiedLogSuppressed;

// R122 forward declarations. The capture-selection predicate sits above the
// R122 block in this file, so the two predicates it needs are declared here.
static BOOL r122OverlapEnabled(void);
static uint64_t r122MaxPairs(void);
static void r122MaybeArm(uint64_t sequence);
static void r122NoteSourcePresentHook(id<CAMetalDrawable> drawable, uint64_t ns);
static void r122NoteProducerCommit(id<MTLCommandBuffer> commandBuffer, uint64_t ns);
static BOOL r122TryAcquireGenerationSlot(void);
static void r122ReleaseGenerationSlot(void);
static void r122MaybeEmitSummary(void);
static void r122EmitSourceGpu(uint64_t sequence, id<MTLCommandBuffer> completed,
                              uint64_t completionNs, double mediaCompletion,
                              uint64_t acquireNs, uint64_t hookNs,
                              uint64_t commitNs, double mediaCommit);
static BOOL r122ProducerGpuEnabled(void);
static BOOL r122CaptureSelected(void);

// R132 presents one bounded capture-to-estimator reorder stage. A capture is
// staged by AcquisitionId, then bound exactly once to the SOURCE presentation
// ordinal. The stage includes queued/in-flight estimator work, so neither the
// texture set nor the serial work queue can grow without bound.
#define kR123MaxDepth 8
#define kR132EstimatorStageCapacity kR123MaxDepth
typedef struct {
    uint64_t sequence;
    uint64_t sourcePresentOrdinal;
    uint64_t captureEpoch;
    NSUInteger drawableID;
    MTLPixelFormat sourceFormat;
    __strong id<MTLTexture> texture;
    __strong id<MTLDevice> device;
    __strong NSString *condition;
    BOOL generationOnly;
    BOOL occupied;
    BOOL inFlight;
    // R133 capture-availability facts. They are independent of the ownership
    // fact and of the estimator disposition:
    //   copyCommitted -- a private copy is encoded in the producer command
    //                    buffer and that command buffer has been committed;
    //   copyComplete  -- the GPU has finished that copy;
    //   copyFailed    -- the producer command buffer reported failure.
    // Only copyCommitted is required to suppress the title present.
    BOOL copyCommitted;
    BOOL copyComplete;
    BOOL copyFailed;
    uintptr_t captureCommandBuffer;
    uint64_t copyCommittedNs;
    uint64_t copyCompleteNs;
    uint64_t presentHookNs;
} R132EstimatorStageEntry;

typedef struct {
    uint64_t sequence;
    uint64_t sourcePresentOrdinal;
    uint8_t disposition; // 1 = CAPTURE_READY, 2 = NO_CAPTURE
} R132EstimatorTerminalEntry;

static pthread_mutex_t r132EstimatorLock = PTHREAD_MUTEX_INITIALIZER;
static R132EstimatorStageEntry r132EstimatorStages[kR132EstimatorStageCapacity];
static R132EstimatorTerminalEntry r132EstimatorTerminals[R127_SOURCE_OWNERSHIP_CAPACITY];
static uint64_t r132NextEstimatorOrdinal = 1;
// Highest AcquisitionId that has reached a published presentation disposition.
// Used only to bound how long a staged capture may legitimately remain pending.
static uint64_t r132NewestPublishedSequence;
static uint64_t r132EstimatorStagePeak;
static uint64_t r132EstimatorReadyCount;
static uint64_t r132EstimatorNoCaptureCount;
static uint64_t r132EstimatorLateCaptureCount;
static uint64_t r132EstimatorStageRefusalCount;
static uint64_t r132EstimatorStageReclaimCount;
static uint64_t r132EstimatorOrderViolations;
static uint64_t r132EstimatorFgMissingCaptureCount;
static BOOL r132EstimatorTestSinkEnabled;
static uint64_t r132TestIngestSequence[1024];
static uint64_t r132TestIngestOrdinal[1024];
static uint32_t r132TestIngestOwner[1024];
static uint64_t r132TestPairA[1024];
static uint64_t r132TestPairB[1024];
static __strong id<MTLTexture> r132TestIngestTexture[1024];
static NSUInteger r132TestIngestCount;
static NSUInteger r132TestNoCaptureCount;
static uint64_t r132TestHistorySequence;
static uint64_t r132TestHistoryOrdinal;
static BOOL r132GapResetQueued;

// ---------------------------------------------------------------------------
// R133 -- capture availability scheduled before presentation.
//
// Measured fact (R132 live log, every frame): the title commits its producer
// command buffer, then calls CAMetalDrawable.present, and only afterwards does
// the producer command buffer complete. R132 encoded the private SOURCE copy
// from the producer *completion* handler, so at the present hook the copy had
// not been allocated yet. By the time it completed, the acquisition ticket was
// terminal and the ownership record was no longer Unknown, so the R130/R131
// admission predicate rejected it. Every ordinal became NO_CAPTURE and the R123
// pump received no SOURCE work.
//
// R133 encodes the same private copy inside the producer command buffer, after
// the title's last render pass and before commit. At the present hook the copy
// is then committed and owned by the GPU. Its completion is implied by the
// producer command buffer's completion, which is independent of the present
// call. That is the only condition under which FG may suppress the title:
//
//     FG_SOURCE_COMMIT_PREREQUISITE = CAPTURE_COPY_COMMITTED_IN_PRODUCER_CB
//
// Three facts stay independent:
//   1. presentation ownership  -- TITLE or FG, exactly one, R131 rules;
//   2. capture availability    -- copyCommitted / copyComplete / copyFailed;
//   3. estimator disposition   -- PENDING_CAPTURE / CAPTURE_READY / NO_CAPTURE.
// Estimator eligibility never changes presentation ownership after the SOURCE
// present decision has committed.
// ---------------------------------------------------------------------------
#define kR133MaxPendingOrdinals 4
#define kR133DefaultPendingGrace 1
#define kR133TimingSampleCapacity 4096

typedef struct {
    uint64_t ordinal;
    uint64_t sequence;
    BOOL resolved;
    BOOL captureOk;
    uint64_t presentHookNs;
    uint64_t captureCompleteNs;
} R133PendingOrdinal;

typedef struct {
    R132EstimatorStageEntry snapshot;
    R127SourceOwner owner;
    BOOL dispatch;
    BOOL gapReset;
    BOOL generationSlotHeld;
} R133Action;

#define kR133MaxActions 8
typedef struct {
    R133Action items[kR133MaxActions];
    NSUInteger count;
} R133ActionList;

static R133PendingOrdinal r133Pending[kR133MaxPendingOrdinals];
static NSUInteger r133PendingCount;
static uint64_t r133LastResolvedOrdinal;
static uint64_t r133PendingPeak;
static uint64_t r133PendingGraceBound = kR133DefaultPendingGrace;
static int r133PendingGraceCached = -1;
static uint64_t r133PendingResolvedReady;
static uint64_t r133PendingExpired;
static uint64_t r133LateExpiredCaptures;
static uint64_t r133CaptureCommittedCount;
static uint64_t r133CaptureCompleteCount;
static uint64_t r133CaptureFailedCount;
static uint64_t r133CaptureAllocationFailures;
static uint64_t r133CapturePreflightFailures;
static uint64_t r133CopyCommittedBeforePresent;
static uint64_t r133CopyCommittedAfterPresent;
static uint64_t r133CopyCompletedBeforePresent;
static uint64_t r133CommitPrerequisiteSatisfied;
static uint64_t r133CommitPrerequisiteAbsent;
static uint64_t r133ProbeSamples;
static uint64_t r133ProbeMinNs;
static uint64_t r133ProbeMaxNs;
static uint64_t r133ProbeOverflow;
static uint64_t r133ProbePresentToCompleteNs[kR133TimingSampleCapacity];
static uint64_t r133ProbePresentToCommitNs[kR133TimingSampleCapacity];

// The drawable texture is needed at producer-commit time, before the completion
// handler that R125's registry is built around. A __weak reference is the one
// form that lets the commit path read it without retaining a drawable or a
// texture, which is R125's invariant.
#define kR133TrackedTextureCapacity 16
typedef struct {
    uint64_t sequence;
    __weak id<MTLTexture> texture;
} R133TrackedTexture;
static R133TrackedTexture r133TrackedTextures[kR133TrackedTextureCapacity];
static pthread_mutex_t r133TrackedTextureLock = PTHREAD_MUTEX_INITIALIZER;

// R133 capture availability. This is the second of the three independent facts
// (ownership, capture availability, estimator disposition) and is deliberately
// NOT stored in the estimator stage table: that table is a bounded reorder
// buffer for the estimator, and the ownership decision must not be starved by
// its depth. The record is keyed by AcquisitionId with an exact-sequence match,
// so a reused modulo slot can never report availability for a different frame.
// Writers only set facts that the capture pipeline actually established.
#define kR133CaptureRecordCapacity R127_SOURCE_OWNERSHIP_CAPACITY
typedef struct {
    uint64_t sequence;
    BOOL committed;      // copy encoded in the producer command buffer
    BOOL complete;       // producer command buffer completed successfully
    BOOL failed;         // preflight/allocation/copy/epoch failure
    uint64_t committedNs;
    uint64_t completeNs;
} R133CaptureRecord;
static R133CaptureRecord r133CaptureRecords[kR133CaptureRecordCapacity];
static pthread_mutex_t r133CaptureRecordLock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t r133CaptureRecordWrites;
static uint64_t r133CaptureRecordMismatches;

static void r133NoteTrackedTexture(uint64_t sequence, id<MTLTexture> texture);
static id<MTLTexture> r133TrackedTextureForSequence(uint64_t sequence);
static void r133NoteCaptureAvailability(uint64_t sequence, int committed,
                                        int complete, int failed, uint64_t ns);
static void r133ClearCaptureAvailability(uint64_t sequence);
static BOOL r133CaptureKnownForSequence(uint64_t sequence);
static BOOL r133CaptureEnabled(void);
static BOOL r133CaptureCommittedForSequence(uint64_t sequence);
static void r133CommitCaptureForCommandBuffer(id<MTLCommandBuffer> commandBuffer);
static void r135SyncArmForSequence(uint64_t sequence, id<MTLDevice> device,
                                   id<MTLCommandBuffer> commandBuffer);
static void r135SyncEncodeWaitForSequence(uint64_t sequence,
                                          id<MTLCommandBuffer> commandBuffer);
static void r133CaptureDidComplete(uint64_t sequence, uintptr_t commandBuffer,
                                   BOOL success, uint64_t completionNs);
static void r133ExecuteActions(R133ActionList *actions, BOOL testSink);
static void r133EmitProbeSummary(const char *reason);

// R134 pair-level critical-path timeline. Instrumentation only: every entry
// point records an instant that already exists; none of them decides anything.
static void r134NoteSourcePresented(uint64_t sequence, uint64_t hookNs,
                                    uint64_t presentNs, uint64_t slotAfterNs);
static void r134NoteCaptureComplete(uint64_t sequence, uint64_t ns);
static void r134NotePumpUse(uint64_t sequence, uint64_t ns);
static void r134NoteGenerationStart(uint64_t pairA, uint64_t pairB, uint64_t ns);
static void r134NoteEstimatorSubmit(uint64_t pairB, uint64_t ns);
static void r134NoteEstimatorWake(uint64_t pairB, uint64_t ns,
                                  uint64_t gpuStartNs, uint64_t gpuEndNs);
static void r134NoteMetalFX(uint64_t pairB, uint64_t startNs, uint64_t endNs);
static void r134NoteResolved(uint64_t pairA, uint64_t pairB, uint64_t ns);
static void r134NoteMidpointPresented(uint64_t pairA, uint64_t pairB, uint64_t ns);
static void r134NoteMidpointMissed(uint64_t pairA, uint64_t pairB, uint64_t slotNs);
static void r134EmitTimeline(const char *reason);

// R136 miss-boundary classification and bounded visual evidence. Recording
// only: no scheduling decision reads any of this. The taxonomy record is
// always on; the image capture is env-gated (FGMETAL_R136_MISS_CAPTURE=1).
static void r136OnMiss(const R123DisplayItem *missed,
                       const R123DisplayItem *presentedNext, uint64_t slotNs);
static void r136OnPresented(const R123DisplayItem *item, id<MTLTexture> payload,
                            uint64_t slotNs);
static void r136OnLateDiscard(uint64_t pairA, uint64_t pairB,
                              id<MTLTexture> texture);
static void r136EmitMissSummary(const char *reason);
static void r136ResetForRuntimeOn(void);
static uint32_t r127RuntimeModeSnapshot(void);
static uint64_t r127RuntimeEpochSnapshot(void);
typedef enum {
    R137EventSourceCaptureCommitted = 1,
    R137EventCaptureComplete,
    R137EventSourceReservationCommit,
    R137EventDirectDecision,
    R137EventTitleDirectWsi,
    R137EventGenerationEntry,
    R137EventEstimatorSubmit,
    R137EventEstimatorWake,
    R137EventMetalFxStart,
    R137EventMetalFxComplete,
    R137EventInternalResolved,
    R137EventInternalLateDiscard,
    R137EventPumpWaitSlot,
    R137EventPumpSleepReturn,
    R137EventPumpSummaryDelay,
    R137EventPumpLoopLockWait,
    R137EventPumpDispatchLockWait,
    R137EventPumpTick,
    R137EventPumpIdle,
    R137EventPumpClaim,
    R137EventSharedEventWait,
    R137EventDrawableRequest,
    R137EventDrawableResult,
    R137EventGpuCopyWait,
    R137EventWsiIssue,
    R137EventPhysicalSource,
    R137EventPhysicalInternal,
    R137EventMidpointMiss,
    R137EventTitleDispatchWait,
    R137EventTitleDispatchWaitEnd,
    R137EventThreadStateSample,
    R137EventPresentationStage,
} R137TraceEvent;
enum {
    R137MetalFxHasFrame = 1u << 0,
    R137MetalFxHasError = 1u << 1,
    R137MetalFxProviderInvoked = 1u << 2,
};
enum {
    R137StageEntryPresent = 1u << 0,
    R137StageHasTexture = 1u << 1,
    R137StageHasDevice = 1u << 2,
    R137StageCopyCommitted = 1u << 3,
    R137StageCopyComplete = 1u << 4,
    R137StageCopyFailed = 1u << 5,
    R137StageReady = 1u << 6,
    R137StagePending = 1u << 7,
    R137StageGenerationOnly = 1u << 8,
    R137StageGenerationSlotHeld = 1u << 9,
    R137StageTestSink = 1u << 10,
    R137StageCommittedCopyKnown = 1u << 11,
    R137StageOrderGap = 1u << 12,
    R137StageOwnerShift = 13,
    R137StageOwnerMask = 7u << R137StageOwnerShift,
};
static uint32_t r137MetalFxResultFlags(BOOL providerInvoked,
                                       BOOL hasFrame, BOOL hasError) {
    return (providerInvoked ? R137MetalFxProviderInvoked : 0u) |
        (hasFrame ? R137MetalFxHasFrame : 0u) |
        (hasError ? R137MetalFxHasError : 0u);
}
static uint32_t r137PresentationStageFlags(
    BOOL entryPresent, BOOL hasTexture, BOOL hasDevice, BOOL copyCommitted,
    BOOL copyComplete, BOOL copyFailed, BOOL ready, BOOL pending,
    BOOL generationOnly, BOOL generationSlotHeld, BOOL testSink,
    BOOL committedCopyKnown, BOOL orderGap, R127SourceOwner owner) {
    return (entryPresent ? R137StageEntryPresent : 0u) |
        (hasTexture ? R137StageHasTexture : 0u) |
        (hasDevice ? R137StageHasDevice : 0u) |
        (copyCommitted ? R137StageCopyCommitted : 0u) |
        (copyComplete ? R137StageCopyComplete : 0u) |
        (copyFailed ? R137StageCopyFailed : 0u) |
        (ready ? R137StageReady : 0u) |
        (pending ? R137StagePending : 0u) |
        (generationOnly ? R137StageGenerationOnly : 0u) |
        (generationSlotHeld ? R137StageGenerationSlotHeld : 0u) |
        (testSink ? R137StageTestSink : 0u) |
        (committedCopyKnown ? R137StageCommittedCopyKnown : 0u) |
        (orderGap ? R137StageOrderGap : 0u) |
        (((uint32_t)owner & 7u) << R137StageOwnerShift);
}
enum {
    R137DirectOwnerAlreadyDirect = 1u << 0,
    R137DirectTestMode = 1u << 1,
    R137DirectTicketUnavailable = 1u << 2,
    R137DirectImageUnavailable = 1u << 3,
    R137DirectCaptureUncommitted = 1u << 4,
    R137DirectRuntimeNotGenerating = 1u << 5,
    R137DirectWorkerUnavailable = 1u << 6,
    R137DirectPumpUnavailable = 1u << 7,
    R137DirectReservationRefused = 1u << 8,
    R137DirectCapacityRefused = 1u << 9,
    R137DirectOwnershipAmbiguous = 1u << 10,
};
enum {
    R137PumpStateOff = 0,
    R137PumpStateWaitNextSlot = 1,
    R137PumpStateNoSlot = 2,
    R137PumpStateTick = 3,
    R137PumpStateEmptyQueue = 4,
    R137PumpStateWaitSourceCommit = 5,
    R137PumpStateWaitInternal = 6,
    R137PumpStateProcessingClaimed = 7,
    R137PumpStateDrawableAcquisition = 8,
    R137PumpStateGpuSyncWait = 9,
    R137PumpStateWsiDispatch = 10,
};
static void r137TracePoint(R137TraceEvent event, uint64_t sequence,
                           uint64_t pairA, uint64_t pairB,
                           uint64_t sourceOrdinal, uint32_t flags,
                           uint64_t value0, uint64_t value1);
static void r137ObservePhysicalPresentation(BOOL isSource, uint64_t sequence,
                                            uint64_t pairA, uint64_t pairB,
                                            uint64_t sourceOrdinal,
                                            uint64_t epoch, BOOL fromPump,
                                            uint64_t presentationHostNs,
                                            uint64_t callbackMonotonicNs);
static void r137EmitSummary(const char *reason);
static void r137ResetForRuntimeOn(void);
static void r137FinalizeWindow(const char *reason);
static void r137SetPumpState(uint32_t state, uint64_t nowNs);
// Set by the pump's copy-abort paths; read by the miss classifier to flag
// events adjacent to a presentation-failure (host-GPU-fragility proxy).
static volatile uint64_t r136LastCopyAbortNs;

static BOOL r132OrdinalIsAdjacent(uint64_t previousOrdinal,
                                 uint64_t currentOrdinal);
static BOOL r132HistoryIsAdjacent(uint64_t previousSequence,
                                  uint64_t previousOrdinal,
                                  uint64_t currentSequence,
                                  uint64_t currentOrdinal);
static NSUInteger r132OccupiedStageCountLocked(void);
static NSUInteger r132ReclaimDeadStagesLocked(void);
static BOOL r132StageCapture(uint64_t sequence, uint64_t drawableID,
                             MTLPixelFormat sourceFormat,
                             id<MTLTexture> texture, id<MTLDevice> device,
                             NSString *condition, uint64_t captureEpoch,
                             BOOL generationOnly);
static void r132PublishPresentation(uint64_t sequence,
                                    uint64_t sourcePresentOrdinal,
                                    R127SourceOwner owner);
static void r132ReleaseStage(uint64_t sequence, uint64_t sourcePresentOrdinal);
static void r132ResetForIntegrationTest(void);
static FGMetalInternalFrame *r132InterpolateAdjacentPair(
    MetalFXFrameInterpolationProvider *provider,
    id<MTLTexture> previousColor, id<MTLTexture> currentColor,
    id<MTLTexture> depthTexture, id<MTLTexture> motionTexture,
    uint64_t previousSequence, uint64_t previousOrdinal,
    uint64_t currentSequence, uint64_t currentOrdinal,
    float deltaTime, FGMetalFXAdditionalEncodingBlock additionalEncoding,
    NSError **error, BOOL *providerInvoked);
// This is the single ordered pair boundary used by production and the
// deterministic provider recorder. Keeping adjacency validation next to the
// provider call makes the tested endpoints the exact texture arguments passed
// to MetalFX, rather than a label reconstructed from an upstream test sink.
static FGMetalInternalFrame *r132InterpolateAdjacentPair(
    MetalFXFrameInterpolationProvider *provider,
    id<MTLTexture> previousColor, id<MTLTexture> currentColor,
    id<MTLTexture> depthTexture, id<MTLTexture> motionTexture,
    uint64_t previousSequence, uint64_t previousOrdinal,
    uint64_t currentSequence, uint64_t currentOrdinal,
    float deltaTime, FGMetalFXAdditionalEncodingBlock additionalEncoding,
    NSError **error, BOOL *providerInvoked) {
    if (providerInvoked) *providerInvoked = NO;
    if (!provider || !previousColor || !currentColor || !depthTexture ||
        !motionTexture ||
        !r132HistoryIsAdjacent(previousSequence, previousOrdinal,
                               currentSequence, currentOrdinal))
        return nil;
    if (providerInvoked) *providerInvoked = YES;
    return [provider interpolatePreviousColor:previousColor
        currentColor:currentColor depthTexture:depthTexture
        motionTexture:motionTexture deltaTime:deltaTime
        additionalEncoding:additionalEncoding error:error];
}

static void processCapturedFrame(uint64_t sequence, NSUInteger drawableID,
                                 MTLPixelFormat sourceFormat,
                                 id<MTLTexture> sourceTexture,
                                 id<MTLDevice> device,
                                 NSString *condition,
                                 BOOL generationOnly,
                                 uint64_t generationEpoch,
                                 uint64_t sourcePresentOrdinal);

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
// R122's own directory. R121 is now somebody else's corpus and is refused by the
// same derived rule that R121 used to protect R120. The R121 constant above is
// retained only because the self-test names it as a protected probe target.
static const char *const kR122EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r122-overlapped-gpu-capacity";
// R123's own directory. R122 is now somebody else's corpus and is refused by the
// same derived rule that R122 used to protect R121. The R122 constant above is
// retained only because the self-test names it as a protected probe target.
static const char *const kR124EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r124-drawable-retirement";
// R125's own directory. R124 is now somebody else's corpus and is refused by the
// same derived rule that R124 used to protect R123. The R124 constant above is
// retained only because the self-test names it as a protected probe target.
static const char *const kR125EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r125-retention-control";
// R126's own directory. R125 is now somebody else's corpus and is refused by the
// same derived rule that R125 used to protect R124. The R125 constant above is
// retained only because the self-test names it as a protected probe target.
static const char *const kR127EvidenceDirectory =
    "/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r127-alpha-runtime";
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
    if (!fgmetalResolvePath(kR127EvidenceDirectory, resolvedOwn, sizeof(resolvedOwn))) return YES;
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
        "r122-isolation-probe-source-%d.raw", getpid());
    const BOOL motionPathOk = fgmetalEvidencePath(motionPath, sizeof(motionPath),
        "r122-isolation-probe-motion-%d.rg16f", getpid());
    uint8_t sourceBytes[4096];
    memset(sourceBytes, 0xA5, sizeof(sourceBytes));
    uint16_t motionBytes[2048];
    for (size_t i = 0; i < 2048; ++i) motionBytes[i] = (uint16_t)(i * 7u);
    const BOOL sourceWritten = sourcePathOk &&
        fgmetalWriteEvidenceAbsolute(sourcePath, sourceBytes, sizeof(sourceBytes));
    const BOOL motionWritten = motionPathOk &&
        fgmetalWriteEvidenceAbsolute(motionPath, motionBytes, sizeof(motionBytes));
    logLine("R122_EVIDENCE_SELFTEST pid=%d allowed=%d source=%s directory=%s protected=%d sourceArtefact=%s saved=%d motionArtefact=%s saved=%d",
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
static const NSUInteger kRetainedDrawableLimit = 8;
// R125 independent stall accounting. The analyser recomputes every latency from
// the ENTRY/RETURN pair, so these counters are a second, in-process computation
// of the same quantity. Two independent counts that agree are evidence that the
// timeout count is not an artefact of the log parser; two that disagree are a
// finding in themselves.
static uint64_t r125TitleAcquisitions;
static uint64_t r125TitleStalls100Ms;
static uint64_t r125TitleStalls500Ms;
static uint64_t r125TitleStalls900Ms;

// R125 non-owning registry primitives. Declared here because the correlation
// record is read on the present and commit paths, which appear earlier in the
// file than the registry itself.
static NSNumber *r125PointerKey(const void *pointer);
static const void *r125PointerField(NSDictionary *record, NSString *key);
static NSDictionary *trackedRecordForDrawable(id<CAMetalDrawable> drawable);
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
static uint64_t previousCapturePresentOrdinal;
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

// Alpha diagnostics keep the healthy frame path quiet by default. These are
// event names that R126 proved to occur once per SOURCE/display frame; counters
// and periodic/final summaries retain the aggregate evidence. Unknown events,
// state transitions, and failures continue to be logged. FGMETAL_DIAG_SAMPLE=full
// restores the proof-round stream for focused debugging.
static BOOL r127VerboseDiagnosticsMode(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *mode = getenv("FGMETAL_DIAG_SAMPLE");
        cached = mode && strcmp(mode, "full") == 0;
    }
    return cached == 1;
}

static BOOL r127ShouldLogLine(const char *line) {
    if (r127VerboseDiagnosticsMode()) return YES;
    const R127LogClass logClass = r127ClassifyLogLine(line);
    if (logClass == R127LogAnomaly) {
        const uint64_t sample = __atomic_add_fetch(&r127AnomalyLogSamples, 1, __ATOMIC_RELAXED);
        if (sample <= 64) return YES;
        __atomic_add_fetch(&r127AnomalyLogSuppressed, 1, __ATOMIC_RELAXED);
        return NO;
    }
    if (logClass == R127LogHealthyFrame) {
        static uint64_t healthySeen[R127_HEALTHY_FRAME_PREFIX_COUNT];
        const size_t index = r127HealthyFramePrefixIndex(line);
        if (index >= R127_HEALTHY_FRAME_PREFIX_COUNT) return YES;
        const uint64_t sample = __atomic_add_fetch(&healthySeen[index], 1, __ATOMIC_RELAXED);
        if (sample <= 8) return YES;
        __atomic_add_fetch(&r127HealthyLogSuppressed, 1, __ATOMIC_RELAXED);
        return NO;
    }
    if (logClass == R127LogResidual) {
        // Midpoint misses and empty slots already have exact pump counters. Keep
        // a small sample while avoiding a log per residual event.
        static uint64_t residualSeen[2];
        const size_t index = r127LineStartsWith(line, "R123_SLOT_MISSED ") ? 0 : 1;
        const uint64_t sample = __atomic_add_fetch(&residualSeen[index], 1, __ATOMIC_RELAXED);
        if (sample <= 16) return YES;
        __atomic_add_fetch(&r127ResidualLogSuppressed, 1, __ATOMIC_RELAXED);
        return NO;
    }
    if (logClass == R127LogUnclassifiedEvent) {
        const uint64_t sample = __atomic_add_fetch(
            &r127UnclassifiedLogSamples, 1, __ATOMIC_RELAXED);
        if (sample <= 64) return YES;
        __atomic_add_fetch(&r127UnclassifiedLogSuppressed, 1, __ATOMIC_RELAXED);
        return NO;
    }
    // Lifecycle/state transitions and lines without a frame identity remain visible.
    return YES;
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
    if (!r127ShouldLogLine(body)) return;
    char line[1800];
    int m = snprintf(line, sizeof(line), "%s\n", body);
    if (m > 0) (void)write(logFd, line, (size_t)m);
}

// R127 latency timestamps use the host-time seconds domain: SOURCE availability
// and the original title present hook are sampled with CACurrentMediaTime(),
// while the physical endpoint is MTLDrawable.presentedTime. Apple documents
// presentedTime as host time in seconds and CACurrentMediaTime as the absolute
// Core Animation time in seconds. CLOCK_MONOTONIC nanoseconds used by the pump
// are kept separate and are never subtracted from these values.
#define R127_LATENCY_CAPACITY 65536u
typedef struct {
    uint64_t appFrameId;
    double sourceAvailableHostTime;
    double originalPresentHookHostTime;
} R127SourceTimeEntry;

static R127SourceTimeEntry r127SourceTimes[R127_LATENCY_CAPACITY];
static pthread_mutex_t r127SourceTimesLock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t r127SourceTimeOverwrites;

typedef enum {
    R127LatencySourceAvailable = 0,
    R127LatencySourcePresentHook,
    R127LatencyInternalDispatch,
    R127LatencyKindCount,
} R127LatencyKind;

static uint64_t r127LatencySamples[R127LatencyKindCount][R127_LATENCY_CAPACITY];
static uint32_t r127LatencySampleCounts[R127LatencyKindCount];
static uint64_t r127LatencySampleOverflows[R127LatencyKindCount];
static pthread_mutex_t r127LatencyLock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t r127NonpositivePresentedTimes;
static uint64_t r127PresentedTimeClockMismatches;
static uint64_t r127MissingSourceTimeRecords;
static uint64_t r127MissingSourcePresentHookTimes;
static uint64_t r127SourcePresentedCallbacks;
static uint64_t r127InternalPresentedCallbacks;
static uint64_t r127SourceReachedDisplay;
static uint64_t r127InternalReachedDisplay;

static void r127SetSourceTime(uint64_t appFrameId, BOOL isPresentHook, double hostTime) {
    if (appFrameId == 0 || !(hostTime > 0.0)) return;
    pthread_mutex_lock(&r127SourceTimesLock);
    R127SourceTimeEntry *entry = &r127SourceTimes[appFrameId % R127_LATENCY_CAPACITY];
    if (entry->appFrameId != appFrameId) {
        if (entry->appFrameId != 0) ++r127SourceTimeOverwrites;
        memset(entry, 0, sizeof(*entry));
        entry->appFrameId = appFrameId;
    }
    if (isPresentHook) entry->originalPresentHookHostTime = hostTime;
    else entry->sourceAvailableHostTime = hostTime;
    pthread_mutex_unlock(&r127SourceTimesLock);
}

static BOOL r127TakeSourceTimes(uint64_t appFrameId, double *availableHostTime,
                                double *presentHookHostTime) {
    if (availableHostTime) *availableHostTime = 0.0;
    if (presentHookHostTime) *presentHookHostTime = 0.0;
    if (appFrameId == 0) return NO;
    pthread_mutex_lock(&r127SourceTimesLock);
    R127SourceTimeEntry *entry = &r127SourceTimes[appFrameId % R127_LATENCY_CAPACITY];
    const BOOL found = entry->appFrameId == appFrameId;
    if (found) {
        if (availableHostTime) *availableHostTime = entry->sourceAvailableHostTime;
        if (presentHookHostTime) *presentHookHostTime = entry->originalPresentHookHostTime;
        memset(entry, 0, sizeof(*entry));
    }
    pthread_mutex_unlock(&r127SourceTimesLock);
    return found;
}

static void r127ClearSourceTimes(uint64_t appFrameId) {
    if (appFrameId == 0) return;
    pthread_mutex_lock(&r127SourceTimesLock);
    R127SourceTimeEntry *entry = &r127SourceTimes[appFrameId % R127_LATENCY_CAPACITY];
    if (entry->appFrameId == appFrameId) memset(entry, 0, sizeof(*entry));
    pthread_mutex_unlock(&r127SourceTimesLock);
}

static BOOL r127HostTimeDeltaNs(double earlierHostTime, double laterHostTime,
                                uint64_t *deltaNs) {
    if (!deltaNs || !(earlierHostTime > 0.0) || !(laterHostTime > 0.0) ||
        laterHostTime < earlierHostTime) return NO;
    const double delta = (laterHostTime - earlierHostTime) * 1e9;
    if (!(delta >= 0.0) || delta > (double)UINT64_MAX) return NO;
    *deltaNs = (uint64_t)llround(delta);
    return YES;
}

static void r127RecordLatency(R127LatencyKind kind, double startHostTime,
                              double presentedHostTime, double callbackHostTime) {
    if (kind < 0 || kind >= R127LatencyKindCount) return;
    uint64_t latencyNs = 0, callbackDelayNs = 0;
    if (!r127HostTimeDeltaNs(startHostTime, presentedHostTime, &latencyNs) ||
        !r127HostTimeDeltaNs(presentedHostTime, callbackHostTime, &callbackDelayNs)) {
        pthread_mutex_lock(&r127LatencyLock);
        ++r127PresentedTimeClockMismatches;
        pthread_mutex_unlock(&r127LatencyLock);
        return;
    }
    (void)callbackDelayNs; // validates the host-time epoch; latency is endpoint-to-endpoint.
    pthread_mutex_lock(&r127LatencyLock);
    const uint32_t count = r127LatencySampleCounts[kind];
    if (count < R127_LATENCY_CAPACITY) {
        r127LatencySamples[kind][count] = latencyNs;
        r127LatencySampleCounts[kind] = count + 1;
    } else {
        ++r127LatencySampleOverflows[kind];
    }
    pthread_mutex_unlock(&r127LatencyLock);
}

static void r127RecordPhysicalDisplay(const char *origin, uint64_t appFrameId,
                                      double internalDispatchHostTime,
                                      double presentedHostTime, double callbackHostTime) {
    const BOOL isSource = origin && strcmp(origin, "SOURCE") == 0;
    const BOOL isInternal = origin && strcmp(origin, "INTERNAL") == 0;
    pthread_mutex_lock(&r127LatencyLock);
    if (isSource) ++r127SourcePresentedCallbacks;
    else if (isInternal) ++r127InternalPresentedCallbacks;
    pthread_mutex_unlock(&r127LatencyLock);
    if (!(presentedHostTime > 0.0)) {
        pthread_mutex_lock(&r127LatencyLock);
        ++r127NonpositivePresentedTimes;
        pthread_mutex_unlock(&r127LatencyLock);
        if (isSource) {
            (void)r127TakeSourceTimes(appFrameId, NULL, NULL);
        }
        return;
    }
    pthread_mutex_lock(&r127LatencyLock);
    if (isSource) ++r127SourceReachedDisplay;
    else if (isInternal) ++r127InternalReachedDisplay;
    pthread_mutex_unlock(&r127LatencyLock);
    if (!(callbackHostTime > 0.0) || presentedHostTime > callbackHostTime) {
        pthread_mutex_lock(&r127LatencyLock);
        ++r127PresentedTimeClockMismatches;
        pthread_mutex_unlock(&r127LatencyLock);
        if (isSource) {
            (void)r127TakeSourceTimes(appFrameId, NULL, NULL);
        }
        return;
    }
    if (isSource) {
        double availableHostTime = 0.0, presentHookHostTime = 0.0;
        const BOOL found = r127TakeSourceTimes(appFrameId, &availableHostTime,
                                               &presentHookHostTime);
        if (!found || !(availableHostTime > 0.0)) {
            pthread_mutex_lock(&r127LatencyLock);
            ++r127MissingSourceTimeRecords;
            pthread_mutex_unlock(&r127LatencyLock);
        } else {
            r127RecordLatency(R127LatencySourceAvailable, availableHostTime,
                              presentedHostTime, callbackHostTime);
        }
        if (presentHookHostTime > 0.0) {
            r127RecordLatency(R127LatencySourcePresentHook, presentHookHostTime,
                              presentedHostTime, callbackHostTime);
        } else {
            pthread_mutex_lock(&r127LatencyLock);
            ++r127MissingSourcePresentHookTimes;
            pthread_mutex_unlock(&r127LatencyLock);
        }
    } else if (isInternal && internalDispatchHostTime > 0.0) {
        r127RecordLatency(R127LatencyInternalDispatch, internalDispatchHostTime,
                          presentedHostTime, callbackHostTime);
    }
}

typedef struct {
    uint32_t count;
    uint64_t overflow;
    uint64_t minNs;
    uint64_t medianNs;
    uint64_t p90Ns;
    uint64_t p99Ns;
    uint64_t maxNs;
    BOOL available;
} R127LatencySummary;

static int r127CompareLatencyNs(const void *left, const void *right) {
    const uint64_t a = *(const uint64_t *)left;
    const uint64_t b = *(const uint64_t *)right;
    return a < b ? -1 : a > b ? 1 : 0;
}

static void r127GetLatencySummary(R127LatencyKind kind, R127LatencySummary *summary) {
    if (!summary) return;
    memset(summary, 0, sizeof(*summary));
    if (kind < 0 || kind >= R127LatencyKindCount) return;
    pthread_mutex_lock(&r127LatencyLock);
    const uint32_t count = r127LatencySampleCounts[kind];
    summary->count = count;
    summary->overflow = r127LatencySampleOverflows[kind];
    uint64_t *copy = count ? malloc((size_t)count * sizeof(*copy)) : NULL;
    if (count && !copy) {
        pthread_mutex_unlock(&r127LatencyLock);
        return;
    }
    if (count) memcpy(copy, r127LatencySamples[kind], (size_t)count * sizeof(*copy));
    pthread_mutex_unlock(&r127LatencyLock);
    if (!count) { summary->available = YES; return; }
    qsort(copy, count, sizeof(*copy), r127CompareLatencyNs);
    summary->minNs = copy[0];
    summary->medianNs = r127MedianNs(copy, count);
    summary->p90Ns = r127NearestRankNs(copy, count, 90);
    summary->p99Ns = r127NearestRankNs(copy, count, 99);
    summary->maxNs = copy[count - 1];
    summary->available = YES;
    free(copy);
}

static double r127Periods(uint64_t latencyNs, uint64_t periodNs) {
    return periodNs ? (double)latencyNs / (double)periodNs : -1.0;
}

static void r127EmitLatencySummary(const char *name, R127LatencyKind kind,
                                   uint64_t sourcePeriodNs) {
    R127LatencySummary summary;
    r127GetLatencySummary(kind, &summary);
    const uint64_t refreshPeriodNs = 16666667ULL;
    logLine("R127_LATENCY_SUMMARY kind=%s domain=CACurrentMediaTime-host-seconds-to-MTLDrawable-presentedTime-host-seconds count=%u overflow=%llu available=%d minNs=%llu medianNs=%llu p90Ns=%llu p99Ns=%llu maxNs=%llu sourcePeriodNs=%llu medianSourcePeriods=%.3f p90SourcePeriods=%.3f p99SourcePeriods=%.3f median60HzPeriods=%.3f p9060HzPeriods=%.3f p9960HzPeriods=%.3f",
        name, summary.count, (unsigned long long)summary.overflow,
        summary.available ? 1 : 0,
        (unsigned long long)summary.minNs, (unsigned long long)summary.medianNs,
        (unsigned long long)summary.p90Ns, (unsigned long long)summary.p99Ns,
        (unsigned long long)summary.maxNs, (unsigned long long)sourcePeriodNs,
        r127Periods(summary.medianNs, sourcePeriodNs),
        r127Periods(summary.p90Ns, sourcePeriodNs),
        r127Periods(summary.p99Ns, sourcePeriodNs),
        r127Periods(summary.medianNs, refreshPeriodNs),
        r127Periods(summary.p90Ns, refreshPeriodNs),
        r127Periods(summary.p99Ns, refreshPeriodNs));
}

static void r127EmitLatencySummaries(uint64_t sourcePeriodNs) {
    pthread_mutex_lock(&r127LatencyLock);
    const uint64_t nonpositive = r127NonpositivePresentedTimes;
    const uint64_t clockMismatches = r127PresentedTimeClockMismatches;
    const uint64_t missingSource = r127MissingSourceTimeRecords;
    const uint64_t missingSourceHook = r127MissingSourcePresentHookTimes;
    const uint64_t sourceCallbacks = r127SourcePresentedCallbacks;
    const uint64_t sourceDisplayed = r127SourceReachedDisplay;
    const uint64_t internalCallbacks = r127InternalPresentedCallbacks;
    const uint64_t internalDisplayed = r127InternalReachedDisplay;
    pthread_mutex_unlock(&r127LatencyLock);
    pthread_mutex_lock(&r127SourceTimesLock);
    const uint64_t overwrittenSourceTimes = r127SourceTimeOverwrites;
    pthread_mutex_unlock(&r127SourceTimesLock);
    logLine("R127_LATENCY_CLOCK_CHECK domain=CACurrentMediaTime-host-seconds-and-MTLDrawable-presentedTime-host-seconds sourceCallbacks=%llu sourceReachedDisplay=%llu internalCallbacks=%llu internalReachedDisplay=%llu nonpositivePresentedTime=%llu invalidHostTimePairs=%llu missingSourceTime=%llu missingSourcePresentHook=%llu sourceTimeRingOverwrites=%llu sourceTimeCapacity=%u",
        (unsigned long long)sourceCallbacks, (unsigned long long)sourceDisplayed,
        (unsigned long long)internalCallbacks, (unsigned long long)internalDisplayed,
        (unsigned long long)nonpositive, (unsigned long long)clockMismatches,
        (unsigned long long)missingSource, (unsigned long long)missingSourceHook,
        (unsigned long long)overwrittenSourceTimes, R127_LATENCY_CAPACITY);
    r127EmitLatencySummary("source-availability-to-physical-display",
                           R127LatencySourceAvailable, sourcePeriodNs);
    r127EmitLatencySummary("original-source-present-hook-to-physical-display",
                           R127LatencySourcePresentHook, sourcePeriodNs);
    r127EmitLatencySummary("internal-wsi-dispatch-to-physical-display",
                           R127LatencyInternalDispatch, sourcePeriodNs);
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

// R123 is defined further down (the pump block is inserted after the R122
// helpers so it can use them). Capture selection is decided much earlier on the
// frame path, so the predicate is declared here.
static BOOL r123PumpEnabled(void);
static uint32_t r127RuntimeModeSnapshot(void);
static BOOL r127RuntimeCanPresent(void);
static BOOL r127RuntimeCanGenerate(void);
static uint64_t r127RuntimeEpochSnapshot(void);
static void r127ObserveWsiDisplay(id<CAMetalDrawable> drawable, const char *kind,
                                  uint64_t pairA, uint64_t pairB,
                                  uint64_t appFrameId,
                                  uint64_t sourcePresentOrdinal,
                                  uint64_t internalPresentId, uint64_t wsiPresentId,
                                  uint64_t epoch, BOOL fromPump);
static void r127PollRuntimeRequests(uint64_t sourceSequence);
static BOOL r123EnsureStarted(BOOL forEnable);

static BOOL r108SequenceSelectedForCapture(uint64_t sequence) {
    pthread_mutex_lock(&stateLock);
    BOOL selected;
    if (r123PumpEnabled()) {
        // R123: the display pump needs every SOURCE frame -- each one is shown
        // once, one source period after its capture. Thinning capture here
        // would silently thin the display, which is the one thing this round
        // must not do.
        selected = YES;
    } else if (r122OverlapEnabled()) {
        // R122: every SOURCE frame is captured while the overlap run is armed.
        // Generation is rate-limited by the single generation slot in
        // captureRealDrawable, not by capture selection -- if capture were
        // thinned here, a busy slot and a deliberately thinned capture would be
        // indistinguishable in the skip statistics.
        selected = r122CaptureSelected();
    } else if (r115ContinuousEnabled()) {
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
// R122 -- OVERLAPPED GPU CAPACITY.
//
// R121 measured the SOURCE frame's retirement relative to B's present hook and
// concluded that ~25.1 ms of every 33.3 ms period was unavailable to
// interpolation. That was a *phase* measurement, not an occupancy measurement:
// `present hook -> retirement` contains the commit->hook interval and any
// presentation-side delay, neither of which is GPU execution.
//
// R122 asks a different question and therefore measures a different interval:
//
//     SOURCE_RENDER_GPU_EXECUTION = producer.GPUEndTime - producer.GPUStartTime
//
// directly on the producer command buffer, per SOURCE frame. If that number is
// small relative to the 33.333 ms period then GPU capacity exists and R121's
// serialized arithmetic was measuring the wrong thing.
//
// The second half of the round is a generation-only overlap mode. It removes
// every presentation-side coupling -- no B deferral, no frontier close, no
// INTERNAL M dispatch -- so that the game thread cannot wait on the estimator
// or on MetalFX, and then measures whether SOURCE cadence and SOURCE producer
// execution survive while FG generation runs concurrently.
//
// Nothing here changes the estimator, MetalFX, the gate, the pacer, the tuple
// lifecycle or the estimator's inputs. The mode is off unless
// FGMETAL_R122_OVERLAP=1, and the producer instrumentation is off unless
// FGMETAL_R122_PRODUCER_GPU=1, so an R121-configured run is unaffected.
// ---------------------------------------------------------------------------
static const char *const kR122StartRequestPath =
    "/tmp/fgmetal-r108-unattended/r122-overlap-start";
static const char *const kR122StopRequestPath =
    "/tmp/fgmetal-r108-unattended/r122-overlap-stop";

static BOOL r122OverlapEnabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *flag = getenv("FGMETAL_R122_OVERLAP");
        cached = (flag && strcmp(flag, "1") == 0) ? 1 : 0;
    }
    return cached == 1;
}

// Producer instrumentation is independent of the overlap mode so the SOURCE-only
// baseline can be measured with the identical code path that measures the
// overlap run. A baseline taken with different instrumentation would not be a
// baseline.
static BOOL r122ProducerGpuEnabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *flag = getenv("FGMETAL_R122_PRODUCER_GPU");
        cached = (flag && strcmp(flag, "1") == 0) ? 1 : 0;
    }
    return cached == 1 || r122OverlapEnabled();
}

static uint64_t r122MaxPairs(void) {
    static uint64_t cached;
    if (cached == 0) {
        const char *configured = getenv("FGMETAL_R122_MAX_PAIRS");
        uint64_t parsed = configured ? strtoull(configured, NULL, 10) : 0;
        cached = parsed > 0 ? parsed : 30;
    }
    return cached;
}

// One generation slot. This is the whole of the "fixed pipeline" requirement:
// a new pair may only begin when the previous one has finished, and a pair that
// arrives while the slot is occupied is *skipped*, never queued. An unbounded
// queue would turn a capacity experiment into a latency experiment and would
// also make "backlog" unmeasurable, because the backlog would be the queue.
static uint32_t r122GenerationSlot;          // 0 = free, 1 = occupied
static uint64_t r122PairsAttempted;
static uint64_t r122PairsGenerated;
static uint64_t r122PairsSkippedBusy;
static uint64_t r122PairsFailed;
static uint64_t r122PairsWarmup;
static uint64_t r122SeedFrames;
static uint64_t r122SourceFramesMeasured;
static BOOL r122RunArmed;
static BOOL r122SummaryEmitted;
static uint64_t r122RunStartNs;

// Bounded SOURCE producer record. Written from three different threads (present
// hook, commit hook, completion handler), so every field is published with a
// release store and the emitter runs only after the completion handler has
// claimed the record.
// Present-hook and commit instants are captured before the completion handler
// runs, so they are parked in a tiny ring keyed by object identity and claimed
// by the handler that owns the frame.
#define kR122PendingLimit 64
typedef struct {
    const void *key;
    uint64_t ns;
    double media;
} R122PendingMark;
static R122PendingMark r122PendingCommit[kR122PendingLimit];
static pthread_mutex_t r122PendingLock = PTHREAD_MUTEX_INITIALIZER;

static void r122PendingStore(R122PendingMark *ring, const void *key,
                             uint64_t ns, double media) {
    if (!key) return;
    pthread_mutex_lock(&r122PendingLock);
    NSUInteger slot = 0;
    for (NSUInteger i = 0; i < kR122PendingLimit; ++i) {
        if (ring[i].key == key) { slot = i; break; }
        if (ring[i].key == NULL) { slot = i; break; }
        slot = i;
    }
    ring[slot].key = key;
    ring[slot].ns = ns;
    ring[slot].media = media;
    pthread_mutex_unlock(&r122PendingLock);
}

static BOOL r122PendingTake(R122PendingMark *ring, const void *key,
                            uint64_t *ns, double *media) {
    if (!key) return NO;
    BOOL found = NO;
    pthread_mutex_lock(&r122PendingLock);
    for (NSUInteger i = 0; i < kR122PendingLimit; ++i) {
        if (ring[i].key == key) {
            if (ns) *ns = ring[i].ns;
            if (media) *media = ring[i].media;
            ring[i].key = NULL;
            ring[i].ns = 0;
            ring[i].media = 0.0;
            found = YES;
            break;
        }
    }
    pthread_mutex_unlock(&r122PendingLock);
    return found;
}

static void r122NoteProducerCommit(id<MTLCommandBuffer> commandBuffer, uint64_t ns) {
    if (!r122ProducerGpuEnabled() || !commandBuffer) return;
    r122PendingStore(r122PendingCommit, (__bridge const void *)commandBuffer,
                     ns, fgmetalMediaNow());
}

static void r122NoteSourcePresentHook(id<CAMetalDrawable> drawable, uint64_t ns) {
    if (!r122ProducerGpuEnabled() || !drawable) return;
    // Stored on the frame's own drawable record rather than in a "most recent"
    // global: the pool holds three drawables and they are reused, so a global
    // would attribute a late present to whichever frame happened to ask next.
    id<MTLTexture> texture = drawable.texture;
    if (!texture) return;
    NSNumber *key = r125PointerKey((__bridge const void *)texture);
    pthread_mutex_lock(&stateLock);
    NSDictionary *record = drawablesByTexture[key];
    // R125: identity is the drawable's address, compared as a scalar. The
    // registry no longer holds the object, so -isEqual: is not available and is
    // not wanted: a pointer match is the same test R124 performed, without the
    // strong reference that made it possible.
    if (record &&
        r125PointerField(record, @"drawablePtr") == (__bridge const void *)drawable) {
        NSMutableDictionary *updated = [record mutableCopy];
        updated[@"presentHookNs"] = @(ns);
        drawablesByTexture[key] = updated;
    }
    pthread_mutex_unlock(&stateLock);
}

// Emits one line per SOURCE frame. GPU timestamps are never subtracted from
// monotonic instants: the GPU window is computed entirely inside the media
// domain, and the cross-clock relationship is published as the difference
// between the same interval measured on both clocks. A domain error of the kind
// R121's first build shipped shows up here as a six-figure millisecond value.
static void r122EmitSourceGpu(uint64_t sequence, id<MTLCommandBuffer> completed,
                              uint64_t completionNs, double mediaCompletion,
                              uint64_t acquireNs, uint64_t hookNs,
                              uint64_t commitNs, double mediaCommit) {
    if (!r122ProducerGpuEnabled()) return;
    const uint64_t gpuStart = fgmetalMediaToNs(completed.GPUStartTime);
    const uint64_t gpuEnd = fgmetalMediaToNs(completed.GPUEndTime);
    const uint64_t gpuExec = (gpuStart && gpuEnd > gpuStart) ? gpuEnd - gpuStart : 0;
    // Cross-clock validation: one interval, two clocks.
    const int64_t monotonicSpan = (commitNs && completionNs > commitNs)
        ? (int64_t)(completionNs - commitNs) : 0;
    const int64_t mediaSpan = (mediaCommit > 0.0 && mediaCompletion > mediaCommit)
        ? (int64_t)llround((mediaCompletion - mediaCommit) * 1e9) : 0;
    const int64_t clockSkewNs = (monotonicSpan && mediaSpan) ? mediaSpan - monotonicSpan : 0;
    __atomic_add_fetch(&r122SourceFramesMeasured, 1, __ATOMIC_RELAXED);
    logLine("R122_SOURCE_GPU seq=%llu gpuStartNs=%llu gpuEndNs=%llu gpuExecNs=%llu "
            "commitNs=%llu completionNs=%llu acquireNs=%llu hookNs=%llu "
            "mediaCommitNs=%llu mediaCompletionNs=%llu clockSkewNs=%lld "
            "commitToGpuStartNs=%lld gpuEndToCompletionNs=%lld "
            "overlap=%d armed=%d",
        (unsigned long long)sequence, (unsigned long long)gpuStart,
        (unsigned long long)gpuEnd, (unsigned long long)gpuExec,
        (unsigned long long)commitNs, (unsigned long long)completionNs,
        (unsigned long long)acquireNs, (unsigned long long)hookNs,
        (unsigned long long)fgmetalMediaToNs(mediaCommit),
        (unsigned long long)fgmetalMediaToNs(mediaCompletion),
        (long long)clockSkewNs,
        (commitNs && gpuStart) ? (long long)((int64_t)gpuStart - (int64_t)fgmetalMediaToNs(mediaCommit)) : 0LL,
        (gpuEnd && completionNs) ? (long long)((int64_t)fgmetalMediaToNs(mediaCompletion) - (int64_t)gpuEnd) : 0LL,
        r122RunArmed ? 1 : 0, r122RunArmed ? 1 : 0);
}

// Called from the capture-selection predicate. Kept as a function rather than a
// pair of globals so the selection rule lives in exactly one place.
static BOOL r122CaptureSelected(void) {
    if (!r122RunArmed) return NO;
    // The 3x ceiling is the same shape the R115 run uses: attempts may exceed
    // the accepted-pair budget by a bounded factor, but never without limit.
    return __atomic_load_n(&r122PairsAttempted, __ATOMIC_RELAXED) < r122MaxPairs() * 3;
}

static BOOL r122TryAcquireGenerationSlot(void) {
    uint32_t expected = 0;
    if (__atomic_compare_exchange_n(&r122GenerationSlot, &expected, 1, NO,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        return YES;
    }
    return NO;
}

static void r122ReleaseGenerationSlot(void) {
    __atomic_store_n(&r122GenerationSlot, 0, __ATOMIC_RELEASE);
}

static void r122MaybeEmitSummary(void) {
    if (r122SummaryEmitted) return;
    if (!r122OverlapEnabled()) return;
    uint64_t attempted = __atomic_load_n(&r122PairsAttempted, __ATOMIC_RELAXED);
    if (attempted < r122MaxPairs()) return;
    r122SummaryEmitted = YES;
    r122RunArmed = NO;
    logLine("R122_OVERLAP_SUMMARY=COMPLETE attempted=%llu generated=%llu skippedBusy=%llu "
            "failed=%llu warmup=%llu seeds=%llu sourceFrames=%llu slot=%u runStartNs=%llu",
        (unsigned long long)attempted,
        (unsigned long long)__atomic_load_n(&r122PairsGenerated, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r122PairsSkippedBusy, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r122PairsFailed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r122PairsWarmup, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r122SeedFrames, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r122SourceFramesMeasured, __ATOMIC_RELAXED),
        __atomic_load_n(&r122GenerationSlot, __ATOMIC_RELAXED),
        (unsigned long long)r122RunStartNs);
}

// Called on the SOURCE acquisition path, which is the frame boundary. Arming
// here keeps every control action on the game thread's own frame cadence rather
// than injecting a timer that could fire mid-present.
//
// The control-file probe is throttled to one frame in eight. It is an access()
// syscall on the render thread, and the baseline phase does not pay it at all
// (the whole function is unreachable unless overlap mode is on). At one probe
// per frame the asymmetry would be a few microseconds per frame in favour of the
// baseline -- small against a 33.3 ms period, but it is a difference between the
// two arms of a controlled comparison and it costs nothing to remove.
static uint64_t r122ArmProbeCounter;

static void r122MaybeArm(uint64_t sequence) {
    if (!r122OverlapEnabled()) return;
    // Both the arm probe and the stop probe are throttled, because both are
    // access() syscalls on the render thread and the measurement is taken while
    // the run is armed. Eight frames is ~0.27 s at 30 fps: irrelevant for a
    // control action, and it keeps the render-thread cost off the measured path.
    if (((++r122ArmProbeCounter) & 7u) != 0) return;
    if (r122RunArmed) {
        if (access(kR122StopRequestPath, F_OK) == 0) {
            r122RunArmed = NO;
            logLine("R122_OVERLAP=DISARMED sequence=%llu reason=stop-request",
                (unsigned long long)sequence);
        }
        return;
    }
    if (r122SummaryEmitted) return;
    if (access(kR122StartRequestPath, F_OK) != 0) return;
    if (__atomic_load_n(&r122PairsAttempted, __ATOMIC_RELAXED) >= r122MaxPairs()) return;
    r122RunArmed = YES;
    r122RunStartNs = r105NowNs();
    (void)unlink(kR122StartRequestPath);
    logLine("R122_OVERLAP=ARMED pid=%d sequence=%llu maxPairs=%llu mode=GENERATION_ONLY "
            "bDeferral=NONE frontier=OPEN internalDispatch=NONE slot=1",
        getpid(), (unsigned long long)sequence,
        (unsigned long long)r122MaxPairs());
}



// Forward declarations for symbols the R123 pump uses but which are defined
// further down this translation unit.
static IMP originalFor(id self, SEL selector);
static NSDictionary *trackedDrawableForTexture(id<MTLTexture> texture);
static id<MTLRenderPipelineState> makeInternalPresentationPipeline(
    id<MTLDevice> device, MTLPixelFormat destinationFormat);

// ---------------------------------------------------------------------------
// R123 -- BUFFERED LOGICAL SOURCE PRESENTATION
//
// R122 established that one M candidate per SOURCE pair is generated at rate
// (30.49/s against 29.14 fps SOURCE) with a 10.686 ms median / 11.587 ms p90
// service window. What it did not have was a way to *display* that stream,
// because the only presentation path in the harness held SOURCE B's original
// CAMetalLayer drawable from B's present hook until M had been displayed.
//
// R123 removes that retention. The game's SOURCE present is suppressed, the
// image is already in an FG-owned private texture (SOURCE_IMAGE_CAPTURE_
// DECOUPLING, existing), and the original drawable is dropped as soon as the
// present call returns. Every frame the viewer sees -- SOURCE or INTERNAL -- is
// copied into a *fresh* drawable acquired by the pump at its own display slot.
// That is SOURCE_PRESENTATION_DRAWABLE_DECOUPLING.
//
// Physical display order becomes
//
//     A          M(A,B)      B          M(B,C)      C
//     t          t+~16.7ms   t+~33.3ms  t+~50ms     t+~66.7ms
//
// i.e. each SOURCE frame appears one source period after its own capture, with
// its generated midpoint halfway between it and its successor. The scheduling
// rules and the fallback live in R123DisplayPump so that a host program can
// falsify them without a title (see r123_pump_selftest.m).
//
// Accounting is unchanged in kind:
//   * SOURCE keeps its AppFrameId and logical identity even though it is
//     displayed from a different drawable, and receives exactly one
//     WsiPresentId immediately before its own delayed WSI dispatch;
//   * INTERNAL keeps its InternalPresentId, no AppFrameId, and its own
//     WsiPresentId immediately before its own dispatch;
//   * a suppressed SOURCE present receives no WsiPresentId at all, because
//     nothing was dispatched.
// ---------------------------------------------------------------------------

static BOOL r123PumpEnabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *flag = getenv("FGMETAL_R123_PUMP");
        cached = (flag && strcmp(flag, "1") == 0) ? 1 : 0;
    }
    return cached == 1;
}

// Diagnostic isolation. With FGMETAL_R123_NO_PRESENT=1 the pump still runs and
// still decides, and SOURCE presents are still suppressed, but nothing is ever
// presented, so no fresh CAMetalDrawable is ever acquired. Comparing this run
// with the full pump separates two hypotheses that otherwise produce the same
// symptom -- a starved game -- but have opposite fixes:
//   game still stalls  -> suppressed drawables are not returning to the pool
//   game stops stalling -> the pump's own drawable acquisitions are the cost
static BOOL r123PresentDisabled(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *flag = getenv("FGMETAL_R123_NO_PRESENT");
        cached = (flag && strcmp(flag, "1") == 0) ? 1 : 0;
    }
    return cached == 1;
}

static uint64_t r123MaxDetailedSlots(void) {
    static uint64_t cached;
    if (cached == 0) {
        const char *value = getenv("FGMETAL_R123_MAX_SLOTS");
        cached = value ? strtoull(value, NULL, 10) : 400;
        if (cached == 0) cached = 400;
    }
    return cached;
}

static uint64_t r123HalfPeriodNs(void) {
    static uint64_t cached;
    if (cached == 0) {
        const char *value = getenv("FGMETAL_R123_HALF_PERIOD_NS");
        cached = value ? strtoull(value, NULL, 10) : 16666667ULL;
        if (cached == 0) cached = 16666667ULL;
    }
    return cached;
}

static R123DisplayPump *r123Pump;
static pthread_mutex_t r123PumpLock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t r123WorkerCondition = PTHREAD_COND_INITIALIZER;
static pthread_t r123Thread;
static BOOL r123ThreadStarted;
static BOOL r123WorkerReady;
static BOOL r123ThreadStop;
static void *r123Layer;
static id<MTLCommandQueue> r123Queue;
static id<MTLRenderPipelineState> r123Pipeline;
static uint64_t r123SlotIndex;
static uint64_t r123DetailedSlots;
static uint64_t r123SummaryEmitted;
static uint64_t r123SourceSuppressed;
static uint64_t r123PresentFailures;
static uint64_t r123DrawableFailures;
static uint64_t r123SourceDroppedByPump;
// R126: command-buffer completions that arrived on FG-Metal's own presentation
// queue and were therefore refused entry to the SOURCE capture path. Evidence
// only -- it exists to prove the gate is exercised, not to steer anything.
static uint64_t ignoredSelfPresentCompletions;
static uint64_t ignoredSelfPresentLogged;
static uint64_t r127LastPeriodicSummaryNs;

typedef NS_ENUM(uint32_t, R127RuntimeMode) {
    R127RuntimeModeOff = 0,
    R127RuntimeModeGenerating = 1,
    R127RuntimeModeSourceOnlyDrain = 2,
};

static const char *const kR127RuntimeDirectory = "/tmp/fgmetal-r127";
static const char *const kR127RuntimeControlPath = "/tmp/fgmetal-r127/control";
static const char *const kR127FaultPairsPath = "/tmp/fgmetal-r127/fault-pairs";
static pthread_mutex_t r127RuntimeLock = PTHREAD_MUTEX_INITIALIZER;
static uint32_t r127RuntimeMode = R127RuntimeModeOff;
static uint64_t r127RuntimeEpoch = 1;
static BOOL r127RuntimeControlReady;
static R123DisplayItem *r127PumpItemInFlight;
static id<MTLTexture> r127CachedSourceTexture;
static uint64_t r127CachedSourceSequence;
static uint64_t r130CachedSourceEpoch;
static uint64_t r127LastQueuedSourceSequence;
static uint64_t r127MinimumSourceSequence;
static BOOL r127EnableBoundaryResolved;
static BOOL r127EnableBoundaryLossLogged;
static R127SourceAckTracker r127DisplayedSourceAck;
static pthread_mutex_t r127SourceOwnershipLock = PTHREAD_MUTEX_INITIALIZER;
static R127SourceOwnershipTable r127SourceOwnership;
// Acquisition tickets are scalar-only: they never retain a title drawable.
// A pending FG dispatch owns an earlier presentation ordinal; a later direct
// title present waits before WSI issue so it cannot overtake that obligation.
typedef struct {
    uint64_t sequence;
    uintptr_t drawableAddress;
    uint64_t drawableID;
    BOOL active;
    BOOL titleClaimed;
} R130AcquireTicket;
static R130AcquireTicket r130AcquireTickets[R127_SOURCE_OWNERSHIP_CAPACITY];
static pthread_cond_t r130DispatchCondition = PTHREAD_COND_INITIALIZER;
static uint64_t r130TitleAcquisitionsOutstanding;
static uint64_t r130PendingFGDispatches;
// Acquisition identity and AppFrameId retain their existing meaning. This
// ordinal is assigned only at a logical SOURCE presentation boundary.
static uint64_t r130NextSourcePresentOrdinal = 1;
static uint64_t r130LastSourcePresentOrdinalDispatched;
static uint64_t r130SourcePresentOrderViolations;
static BOOL r130AcquireAccountingAmbiguous;

static void r130WaitForPriorCommittedSource(uint64_t sequence) {
    pthread_mutex_lock(&r127RuntimeLock);
    const uint64_t waitStartNs = r105NowNs();
    const uint64_t pendingAtStart = r130PendingFGDispatches;
    if (pendingAtStart)
        r137TracePoint(R137EventTitleDispatchWait, sequence, sequence, sequence,
                       0, 1u, waitStartNs, pendingAtStart);
    while (r130PendingFGDispatches != 0)
        pthread_cond_wait(&r130DispatchCondition, &r127RuntimeLock);
    const uint64_t waitEndNs = r105NowNs();
    const uint64_t pendingAtEnd = r130PendingFGDispatches;
    pthread_mutex_unlock(&r127RuntimeLock);
    if (pendingAtStart)
        r137TracePoint(R137EventTitleDispatchWaitEnd, sequence,
            sequence, sequence, 0, 0u,
            waitEndNs - waitStartNs, pendingAtEnd);
}

// Caller holds r110WsiDispatchLock. SOURCE ordinals are independent from
// acquisition identity and WsiPresentId; INTERNAL WSI calls do not consume
// this counter.
static uint64_t r130AllocateSourcePresentOrdinalLocked(void) {
    return r130NextSourcePresentOrdinal++;
}

static void r130MarkSourcePresentOrdinalDispatchedLocked(
    uint64_t sourcePresentOrdinal, uint64_t acquisitionId) {
    if (!sourcePresentOrdinal || sourcePresentOrdinal !=
        r130LastSourcePresentOrdinalDispatched + 1) {
        ++r130SourcePresentOrderViolations;
        logLine("R130_SOURCE_PRESENT_ORDER=VIOLATION acquisitionId=%llu "
                "sourcePresentOrdinal=%llu lastDispatched=%llu",
            (unsigned long long)acquisitionId,
            (unsigned long long)sourcePresentOrdinal,
            (unsigned long long)r130LastSourcePresentOrdinalDispatched);
        return;
    }
    r130LastSourcePresentOrdinalDispatched = sourcePresentOrdinal;
}

static void r130BeginTitleAcquisition(void) {
    pthread_mutex_lock(&r127RuntimeLock);
    ++r130TitleAcquisitionsOutstanding;
    pthread_mutex_unlock(&r127RuntimeLock);
}

static void r130RegisterTitleAcquisition(uint64_t sequence, id<CAMetalDrawable> drawable) {
    pthread_mutex_lock(&r127RuntimeLock);
    if (!sequence) {
        --r130TitleAcquisitionsOutstanding;
    } else {
        R130AcquireTicket *ticket = &r130AcquireTickets[
            sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
        if (ticket->active) r130AcquireAccountingAmbiguous = YES;
        else {
            ticket->sequence = sequence;
            ticket->drawableAddress = (uintptr_t)(__bridge void *)drawable;
            ticket->drawableID = drawable.drawableID;
            ticket->active = YES;
            ticket->titleClaimed = NO;
        }
    }
    pthread_mutex_unlock(&r127RuntimeLock);
}

// Caller holds r127RuntimeLock. Tickets contain only non-owning scalar
// identities and survive eviction from the separate R125 diagnostic registry.
static uint64_t r130TicketSequenceLocked(id<CAMetalDrawable> drawable) {
    if (!drawable) return 0;
    const uintptr_t address = (uintptr_t)(__bridge void *)drawable;
    const uint64_t drawableID = drawable.drawableID;
    for (NSUInteger i = 0; i < R127_SOURCE_OWNERSHIP_CAPACITY; ++i) {
        R130AcquireTicket *ticket = &r130AcquireTickets[i];
        if (ticket->sequence && ticket->drawableAddress == address &&
            ticket->drawableID == drawableID) return ticket->sequence;
    }
    return 0;
}

// A capture completion may arrive after the ownership ledger has reused the
// modulo slot for a newer terminal SOURCE. The acquisition ticket is the
// bounded, live identity proof: without it, an unknown ledger lookup is stale
// and must not repopulate readiness for an old acquisition.
static BOOL r130HasActiveTicketForSequenceLocked(uint64_t sequence) {
    if (!sequence) return NO;
    const R130AcquireTicket *ticket =
        &r130AcquireTickets[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    return ticket->active && ticket->sequence == sequence;
}

static uint64_t r130SequenceForDrawable(id<CAMetalDrawable> drawable) {
    NSDictionary *record = trackedRecordForDrawable(drawable);
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    if (sequence) return sequence;
    pthread_mutex_lock(&r127RuntimeLock);
    sequence = r130TicketSequenceLocked(drawable);
    pthread_mutex_unlock(&r127RuntimeLock);
    return sequence;
}

// Caller holds r127RuntimeLock. Missing/reused identities permanently disable
// FG admission, rather than guessing whether an acquired drawable is gone.
static BOOL r130FinishTitleAcquisitionLocked(uint64_t sequence) {
    R130AcquireTicket *ticket = &r130AcquireTickets[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (!sequence || !ticket->active || ticket->sequence != sequence ||
        !r130TitleAcquisitionsOutstanding) {
        r130AcquireAccountingAmbiguous = YES;
        return NO;
    }
    ticket->active = NO;
    --r130TitleAcquisitionsOutstanding;
    return YES;
}

static void r130FinishTitleAcquisition(id<CAMetalDrawable> drawable) {
    if (!r123PumpEnabled() || !drawable || isInternalPresentationCall ||
        !isSelectedProcess() || !isSelectedLayer((CAMetalLayer *)drawable.layer)) return;
    pthread_mutex_lock(&r127RuntimeLock);
    uint64_t sequence = r130TicketSequenceLocked(drawable);
    (void)r130FinishTitleAcquisitionLocked(sequence);
    pthread_mutex_unlock(&r127RuntimeLock);
}
static uint64_t r127LastPumpSourceDispatchedSequence;
static uint64_t r127LastPumpSourceDisplayedSequence;
static uint64_t r127LastDisplayedWsiPresentId;
static uint64_t r127LastDisplayedSequence;
static uint64_t r127LastDisplayedAppFrameId;
static uint64_t r127LastDisplayedInternalPresentId;
static double r127LastDisplayedTime;
static uint64_t r127LastPhysicalOrderPosition;
static uint64_t r127PhysicalOrderViolations;
static char r127LastDisplayedKind[16] = "NONE";
// R134: the physical-display bookkeeping below has its own lock.
//
// The presented handler runs on QuartzCore's display thread from inside
// -[CAMetalDrawable didPresentAtTime:], which already holds the drawable's
// internal os_unfair_lock. The pump calls -[CAMetalDrawable addPresentedHandler:]
// and -present while holding r127RuntimeLock, and both of those need the same
// drawable lock. If the handler also took r127RuntimeLock the two sides form a
// cycle: pump holds r127RuntimeLock and wants the drawable lock, display thread
// holds the drawable lock and wants r127RuntimeLock. That cycle was observed as
// a hard hang of the pump thread (both threads parked in the sample, no further
// slots presented, no periodic summary emitted).
//
// r127DisplayLock is taken only by the handler and by readers that hold no
// drawable lock, and nothing ever acquires a drawable lock or r127RuntimeLock
// while holding it, so the wait-for graph stays acyclic.
static pthread_mutex_t r127DisplayLock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t r127TransitionRequestId;
static uint64_t r127TransitionEpoch;
static uint64_t r127TransitionBoundarySequence;
static uint64_t r127DisableRequestId;
static uint64_t r127DisableRequestCutoffSequence;
static uint64_t r127LatestSuppressedSequence;
static uint64_t r127DisableBoundaryCount;
static BOOL r127DisableBoundLogged;
static uint64_t r127FaultPairsRemaining;
static uint64_t r127FaultPairsRefused;
static uint64_t r127FaultRequestId;
static BOOL r127FaultRecoveryPending;
static uint64_t r127StaleCallbackRejections;
static uint64_t r127GenerationEpochProcessed;

static void r123EmitSummary(const char *reason);
static void r124EmitTestSummary(const char *reason, int mode);
static int r124TestMode(void);

static R127SourceOwner r127SourceOwnerForSequence(uint64_t sequence) {
    pthread_mutex_lock(&r127SourceOwnershipLock);
    const R127SourceOwner owner = R127SourceOwnershipRead(
        &r127SourceOwnership, sequence);
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    return owner;
}

static R127SourceOwner r127RecordSourceOwnerIfUnknown(
    uint64_t sequence, R127SourceOwner owner) {
    pthread_mutex_lock(&r127SourceOwnershipLock);
    const R127SourceOwner recorded = R127SourceOwnershipSetIfUnknown(
        &r127SourceOwnership, sequence, owner);
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    return recorded;
}

// Claim the title dispatch once before entering its WSI call. The acquisition
// ticket stays outstanding until the call returns, fencing FG admission.
static BOOL r130ClaimTitleDispatch(id<CAMetalDrawable> drawable) {
    if (!r123PumpEnabled() || !drawable || isInternalPresentationCall ||
        !isSelectedProcess() || !isSelectedLayer((CAMetalLayer *)drawable.layer)) return YES;
    pthread_mutex_lock(&r127RuntimeLock);
    const uint64_t sequence = r130TicketSequenceLocked(drawable);
    if (!sequence) {
        r130AcquireAccountingAmbiguous = YES;
        pthread_mutex_unlock(&r127RuntimeLock);
        return YES;
    }
    R130AcquireTicket *ticket = &r130AcquireTickets[sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (ticket->titleClaimed) {
        pthread_mutex_unlock(&r127RuntimeLock);
        return NO;
    }
    pthread_mutex_lock(&r127SourceOwnershipLock);
    const R127SourceOwner owner = R127SourceOwnershipRead(&r127SourceOwnership, sequence);
    const BOOL claimed = owner == R127SourceOwnerUnknown || owner == R127SourceOwnerDirect;
    if (claimed) ticket->titleClaimed = YES;
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    pthread_mutex_unlock(&r127RuntimeLock);
    return claimed;
}

// Called under the WSI lock immediately before invoking the title IMP.
static void r130MarkTitleDispatched(uint64_t sequence) {
    if (!sequence) return;
    pthread_mutex_lock(&r127SourceOwnershipLock);
    (void)R127SourceOwnershipMarkTitleDispatched(&r127SourceOwnership, sequence);
    pthread_mutex_unlock(&r127SourceOwnershipLock);
}

// R126: the queue-origin gate can only be evaluated once the presentation queue
// exists, so its identity is recorded at the instant the queue is created. One
// line per process; it is the address the gate compares against.
static void r126LogSelfPresentGate(void) {
    static uint64_t logged;
    if (__atomic_exchange_n(&logged, 1, __ATOMIC_RELAXED) != 0) return;
    logLine("R126_SELF_PRESENT_GATE=ARMED pid=%d queue=%p "
            "rule=completed.commandQueue==r123Queue "
            "action=return-before-source-capture counter=ignoredSelfPresentCompletions",
        getpid(), (__bridge void *)r123Queue);
}

static void r127LogSelfPresentGateActive(void) {
    static uint64_t logged;
    if (__atomic_exchange_n(&logged, 1, __ATOMIC_RELAXED) != 0) return;
    logLine("R127_SELF_PRESENT_GATE=ACTIVE pid=%d queue=%p "
            "ignoredSelfPresentCompletions=%llu reachedSourceCapture=0 "
            "rule=completed.commandQueue==r123Queue",
        getpid(), (__bridge void *)r123Queue,
        (unsigned long long)__atomic_load_n(&ignoredSelfPresentCompletions,
                                             __ATOMIC_RELAXED));
}

// The callback records scalar display identity only. It neither stores nor
// captures a CAMetalDrawable, preserving R125's non-retention guarantee.
static void r127ObserveWsiDisplay(id<CAMetalDrawable> drawable, const char *kind,
                                  uint64_t pairA, uint64_t pairB,
                                  uint64_t appFrameId,
                                  uint64_t sourcePresentOrdinal,
                                  uint64_t internalPresentId,
                                  uint64_t wsiPresentId, uint64_t epoch,
                                  BOOL fromPump) {
    if (!drawable || ![drawable respondsToSelector:@selector(addPresentedHandler:)]) return;
    const BOOL isSource = kind && strcmp(kind, "SOURCE") == 0;
    const uint64_t position = pairA * 2 + (isSource ? 0 : 1);
    const char *kindCopy = isSource ? "SOURCE" : "INTERNAL";
    [drawable addPresentedHandler:^(id<MTLDrawable> presented) {
        if (!(presented.presentedTime > 0.0)) return;
        const uint64_t callbackMonotonicNs = r105NowNs();
        const uint64_t presentationHostNs =
            (uint64_t)(presented.presentedTime * 1000000000.0);
        BOOL orderViolation = NO;
        BOOL boundary = NO;
        pthread_mutex_lock(&r127DisplayLock);
        const BOOL isNewestPhysicalEvent = presented.presentedTime >= r127LastDisplayedTime;
        const BOOL positionMatchesTime = isNewestPhysicalEvent
            ? position > r127LastPhysicalOrderPosition
            : position < r127LastPhysicalOrderPosition;
        if (r127LastDisplayedTime > 0.0 && !positionMatchesTime) {
            ++r127PhysicalOrderViolations;
            orderViolation = YES;
        }
        if (presented.presentedTime >= r127LastDisplayedTime) {
            r127LastPhysicalOrderPosition = position;
            r127LastDisplayedTime = presented.presentedTime;
            snprintf(r127LastDisplayedKind, sizeof(r127LastDisplayedKind), "%s", kindCopy);
            r127LastDisplayedSequence = pairB;
            r127LastDisplayedAppFrameId = appFrameId;
            r127LastDisplayedInternalPresentId = internalPresentId;
            r127LastDisplayedWsiPresentId = wsiPresentId;
        }
        if (isSource &&
            R127SourceSequenceAtOrAfter(pairB, r127MinimumSourceSequence)) {
            // A later SOURCE callback cannot stand in for one that failed or
            // has not physically displayed. The disable fence consumes this
            // contiguous frontier, not a maximum sequence watermark.
            (void)R127SourceAckObserve(&r127DisplayedSourceAck, pairB);
        }
        if (isNewestPhysicalEvent && fromPump && isSource &&
            pairB > r127LastPumpSourceDisplayedSequence)
            r127LastPumpSourceDisplayedSequence = pairB;
        if (r127TransitionEpoch == epoch && isSource &&
            r127TransitionBoundarySequence == pairB)
            boundary = YES;
        pthread_mutex_unlock(&r127DisplayLock);
        r137ObservePhysicalPresentation(isSource, pairB, pairA, pairB,
            sourcePresentOrdinal, epoch, fromPump, presentationHostNs,
            callbackMonotonicNs);
        if (orderViolation) {
            logLine("R127_DISPLAY_ORDER=VIOLATION kind=%s pair=%llu->%llu "
                    "wsiPresentId=%llu position=%llu",
                kindCopy, (unsigned long long)pairA, (unsigned long long)pairB,
                (unsigned long long)wsiPresentId,
                (unsigned long long)position);
        }
        if (boundary) {
            logLine("R127_RUNTIME_BOUNDARY_DISPLAY epoch=%llu kind=%s "
                    "sequence=%llu appFrameId=%s internalPresentId=%s "
                    "wsiPresentId=%llu drawableID=%lu presentedTime=%.9f "
                    "fromPump=%d",
                (unsigned long long)epoch, kindCopy,
                (unsigned long long)pairB,
                appFrameId ? "SOURCE" : "NONE",
                internalPresentId ? "INTERNAL" : "NONE",
                (unsigned long long)wsiPresentId,
                (unsigned long)presented.drawableID, presented.presentedTime,
                fromPump ? 1 : 0);
        }
    }];
}

// R134: physical-display state is published under r127DisplayLock, so readers
// take that lock instead of r127RuntimeLock. Callers must not already hold a
// drawable lock; nothing here calls into QuartzCore.
static void r127DisplaySnapshot(uint64_t *outSequence, uint64_t *outWsi,
                                char *outKind, size_t capacity) {
    pthread_mutex_lock(&r127DisplayLock);
    if (outSequence) *outSequence = r127LastDisplayedSequence;
    if (outWsi) *outWsi = r127LastDisplayedWsiPresentId;
    if (outKind && capacity) snprintf(outKind, capacity, "%s", r127LastDisplayedKind);
    pthread_mutex_unlock(&r127DisplayLock);
}

static void r127DisplayAckResetAtBoundary(uint64_t sequence) {
    pthread_mutex_lock(&r127DisplayLock);
    R127SourceAckBeginAtBoundary(&r127DisplayedSourceAck, sequence);
    pthread_mutex_unlock(&r127DisplayLock);
}

static void r127AbandonPumpItem(R123DisplayItem *item) {
    if (!item) return;
    pthread_mutex_lock(&r127RuntimeLock);
    if (r127PumpItemInFlight == item) {
        if (r123Pump) [r123Pump rejectUndispatchedItem:item];
        r127PumpItemInFlight = nil;
    }
    pthread_mutex_unlock(&r127RuntimeLock);
}

// The pump presents through the *original* MTLDrawable present so that no hook
// in this file observes its own output as a SOURCE present.
static void r123PresentItem(R123DisplayItem *item, uint64_t slotNs) {
    // First statement: tickLateNs must measure how late the slot woke, not how
    // long the payload checks took.
    const uint64_t wakeNs = r105NowNs();
    // R134: the tick has already advanced the schedule, so the pump's next slot
    // is exactly this item's successor slot. For a SOURCE that successor slot
    // is the midpoint's intended slot, which is the deadline this round has to
    // explain. Recording it here keeps the deadline definition in one place
    // instead of re-deriving it from the period after the fact.
    if (item.kind == R123ItemKindSource)
        r134NoteSourcePresented(item.seqB, item.enqueueNs, wakeNs,
                                r123Pump.nextSlotNs);
    CAMetalLayer *layer = (__bridge CAMetalLayer *)(r123Layer);
    id<MTLTexture> source = item.payload;
    if (!layer || !source) {
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        logLine("R123_SLOT_ABORTED reason=missing-layer-or-payload kind=%s a=%llu b=%llu",
            item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE",
            (unsigned long long)item.seqA, (unsigned long long)item.seqB);
        return;
    }
    if (r123PresentDisabled()) {
        // Suppression-only probe: the frame is consumed and counted, but no
        // drawable is taken, so the pool sees exactly what suppression alone
        // does to it.
        ++r123PresentFailures;
        r127AbandonPumpItem(item);
        logLine("R123_SLOT_SUPPRESSED_ONLY kind=%s a=%llu b=%llu slotNs=%llu "
                "drawableAcquired=0 reason=FGMETAL_R123_NO_PRESENT",
            item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE",
            (unsigned long long)item.seqA, (unsigned long long)item.seqB,
            (unsigned long long)slotNs);
        return;
    }
    if (!r123Pipeline) {
        r123Pipeline = makeInternalPresentationPipeline(layer.device,
                                                        MTLPixelFormatBGRA8Unorm);
    }
    if (!r123Queue) r123Queue = [layer.device newCommandQueue];
    if (!r123Pipeline || !r123Queue) {
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        logLine("R123_SLOT_ABORTED reason=pipeline-or-queue-unavailable pipeline=%p queue=%p",
            (__bridge void *)r123Pipeline, (__bridge void *)r123Queue);
        return;
    }
    id<MTLCommandBuffer> commandBuffer = [r123Queue commandBuffer];
    // R135: a SOURCE payload is the producer's private capture copy, written
    // by a command buffer on the title's queue. Hold this presentation buffer
    // on the shared event until that copy's signal retires; INTERNAL payloads
    // are produced behind a synchronous wait and need no edge.
    if (item.kind == R123ItemKindSource)
        r135SyncEncodeWaitForSequence(item.seqB, commandBuffer);
    r126LogSelfPresentGate();
    r127LogSelfPresentGateActive();

    // Fresh drawable. The original IMP is used directly so this acquisition is
    // never counted as a SOURCE acquisition by the R110/R121 hooks.
    isInternalDrawableAcquisition = YES;
    isInternalPresentationCall = YES;
    const uint64_t ndEnterNs = r105NowNs();
    r137SetPumpState(R137PumpStateDrawableAcquisition, ndEnterNs);
    r137TracePoint(R137EventDrawableRequest, item.seqB, item.seqA, item.seqB,
                   item.sourcePresentOrdinal, item.kind == R123ItemKindInternal,
                   slotNs, ndEnterNs);
    id<CAMetalDrawable> drawable = originalNextDrawable
        ? ((id (*)(id, SEL))originalNextDrawable)(layer, @selector(nextDrawable)) : nil;
    const uint64_t ndReturnNs = r105NowNs();
    isInternalDrawableAcquisition = NO;
    r137TracePoint(R137EventDrawableResult, item.seqB, item.seqA, item.seqB,
                   item.sourcePresentOrdinal, drawable ? 1u : 0u,
                   ndReturnNs - ndEnterNs,
                   drawable ? drawable.drawableID : 0);
    r137SetPumpState(R137PumpStateProcessingClaimed, ndReturnNs);

    if (!drawable) {
        isInternalPresentationCall = NO;
        r127AbandonPumpItem(item);
        ++r123DrawableFailures;
        logLine("R123_SLOT_ABORTED reason=nextDrawable-returned-nil kind=%s a=%llu b=%llu slotNs=%llu",
            item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE",
            (unsigned long long)item.seqA, (unsigned long long)item.seqB,
            (unsigned long long)slotNs);
        return;
    }

    id<MTLTexture> destination = drawable.texture;
    const BOOL shapeOk = source.device == destination.device &&
        source.width == destination.width && source.height == destination.height &&
        !destination.framebufferOnly &&
        (source.usage & MTLTextureUsageShaderRead) != 0 &&
        (destination.usage & MTLTextureUsageRenderTarget) != 0;
    if (!shapeOk) {
        isInternalPresentationCall = NO;
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        logLine("R123_SLOT_ABORTED reason=shape-mismatch kind=%s a=%llu b=%llu "
                "source=%p %lux%lu fmt=%lu usage=%lu destination=%p %lux%lu fmt=%lu usage=%lu framebufferOnly=%d",
            item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE",
            (unsigned long long)item.seqA, (unsigned long long)item.seqB,
            (__bridge void *)source, (unsigned long)source.width, (unsigned long)source.height,
            (unsigned long)source.pixelFormat, (unsigned long)source.usage,
            (__bridge void *)destination, (unsigned long)destination.width,
            (unsigned long)destination.height, (unsigned long)destination.pixelFormat,
            (unsigned long)destination.usage, destination.framebufferOnly);
        return;
    }

    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = destination;
    pass.colorAttachments[0].loadAction = MTLLoadActionDontCare;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    const uint64_t encodeStartNs = r105NowNs();
    // R134: the presentation blit is the pump's first sample of the private
    // capture copy, so this instant is the "pump use" end of the cross-queue
    // safety margin.
    if (item.kind == R123ItemKindSource) r134NotePumpUse(item.seqB, encodeStartNs);
    id<MTLRenderCommandEncoder> encoder = commandBuffer
        ? [commandBuffer renderCommandEncoderWithDescriptor:pass] : nil;
    if (!commandBuffer || !encoder) {
        isInternalPresentationCall = NO;
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        logLine("R123_SLOT_ABORTED reason=encoder-unavailable commandBuffer=%p encoder=%p",
            (__bridge void *)commandBuffer, (__bridge void *)encoder);
        return;
    }
    [encoder setRenderPipelineState:r123Pipeline];
    [encoder setFragmentTexture:source atIndex:0];
    [encoder drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3];
    [encoder endEncoding];
    const uint64_t commitNs = r105NowNs();
    // Bounded wait. The pump thread must never hang on the GPU: a slot that
    // cannot verify its copy fails safely by dropping the frame. The completion
    // handler holds the drawable, so even on timeout the in-flight write lands
    // in a drawable the pool cannot hand out mid-write.
    dispatch_semaphore_t copyDone = dispatch_semaphore_create(0);
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        (void)completed;
        (void)drawable;
        dispatch_semaphore_signal(copyDone);
    }];
    r137SetPumpState(R137PumpStateGpuSyncWait, commitNs);
    r137TracePoint(R137EventGpuCopyWait, item.seqB, item.seqA, item.seqB,
                   item.sourcePresentOrdinal,
                   item.kind == R123ItemKindInternal ? 1u : 0u,
                   commitNs, 3ULL * NSEC_PER_SEC);
    [commandBuffer commit];
    const long copyWait = dispatch_semaphore_wait(copyDone,
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(3 * NSEC_PER_SEC)));
    const uint64_t copyWakeNs = r105NowNs();
    isInternalPresentationCall = NO;

    const uint64_t copyGpuStartNs = fgmetalMediaToNs(commandBuffer.GPUStartTime);
    const uint64_t copyGpuEndNs = fgmetalMediaToNs(commandBuffer.GPUEndTime);
    const BOOL copyOk = copyWait == 0 &&
        commandBuffer.status == MTLCommandBufferStatusCompleted &&
        commandBuffer.error == nil;
    r137TracePoint(R137EventGpuCopyWait, item.seqB, item.seqA, item.seqB,
                   item.sourcePresentOrdinal,
                   (copyWait == 0 ? 1u : 0u) |
                       (commandBuffer.error ? 2u : 0u),
                   copyWakeNs - commitNs,
                   copyGpuEndNs > copyGpuStartNs
                       ? copyGpuEndNs - copyGpuStartNs : 0);
    r137SetPumpState(R137PumpStateProcessingClaimed, copyWakeNs);
    if (!copyOk) {
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        __atomic_store_n(&r136LastCopyAbortNs, r105NowNs(), __ATOMIC_RELAXED);
        logLine("R123_SLOT_ABORTED reason=copy-%s status=%lu error=%s kind=%s a=%llu b=%llu",
            copyWait == 0 ? "failed" : "timeout",
            (unsigned long)commandBuffer.status,
            commandBuffer.error.localizedDescription.UTF8String ?: "none",
            item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE",
            (unsigned long long)item.seqA, (unsigned long long)item.seqB);
        return;
    }

    // R136: the payload is verified and the slot will present. Record the
    // present into the miss-boundary evidence (cheap when disarmed) before
    // the WSI call; the async blit runs on the capture queue, not here.
    r136OnPresented(item, source, slotNs);

    const char *origin = item.kind == R123ItemKindInternal ? "INTERNAL" : "SOURCE";
    const uint64_t appFrameId = item.appFrameId;
    const uint64_t internalPresentId = item.internalPresentId;
    __block double internalDispatchHostTime = 0.0;
    IMP presentOriginal = originalFor(drawable, @selector(present));
    const uint64_t dispatchNs = r105NowNs();
    __block uint64_t wsiPresentId = 0;
    [drawable addPresentedHandler:^(id<MTLDrawable> presented) {
        const double presentedHostTime = presented.presentedTime;
        const double callbackHostTime = fgmetalMediaNow();
        const BOOL reachedDisplay = presentedHostTime > 0.0;
        r127RecordPhysicalDisplay(origin, appFrameId, internalDispatchHostTime,
                                  presentedHostTime, callbackHostTime);
        logLine("R101_WSI_DISPLAYED origin=%s wsiPresentId=%llu appFrameId=%llu "
                "internalPresentId=%llu drawableID=%lu presentedTime=%.9f reachedDisplay=%d "
                "displaySource=FRESH-DRAWABLE",
            origin, (unsigned long long)wsiPresentId, (unsigned long long)appFrameId,
            (unsigned long long)internalPresentId, (unsigned long)presented.drawableID,
            presentedHostTime, reachedDisplay);
    }];
    const uint64_t itemEpoch = item.runtimeEpoch;
    const uint64_t itemPairA = item.seqA;
    const uint64_t itemPairB = item.seqB;
    const BOOL itemIsInternal = item.kind == R123ItemKindInternal;

    if (!presentOriginal) {
        r127AbandonPumpItem(item);
        ++r123PresentFailures;
        logLine("R123_SLOT_ABORTED reason=present-imp-unavailable kind=%s a=%llu b=%llu",
            origin, (unsigned long long)itemPairA, (unsigned long long)itemPairB);
        return;
    }

    // The final epoch check and WSI call share a short lock with runtime
    // transitions. Copy work may finish after OFF, but it cannot dispatch an
    // item from that old epoch. Source-only flush requeues a claimed SOURCE;
    // an old INTERNAL is discarded.
    BOOL dispatch = NO;
    BOOL boundary = NO;
    uint64_t boundaryRequestId = 0;
    pthread_mutex_lock(&r127RuntimeLock);
    const uint32_t liveMode = r127RuntimeModeSnapshot();
    const uint64_t liveEpoch = r127RuntimeEpochSnapshot();
    BOOL eligible = liveMode != R127RuntimeModeOff && liveEpoch == itemEpoch &&
        r127PumpItemInFlight == item && r123Pump &&
        (itemIsInternal || r127SourceOwnerForSequence(itemPairB) == R127SourceOwnerSuppressed);
    if (eligible) {
        [r110WsiDispatchLock lock];
        const uint64_t sourceOrdinal = item.sourcePresentOrdinal;
        eligible = itemIsInternal ||
            (sourceOrdinal != 0 &&
             sourceOrdinal == r130LastSourcePresentOrdinalDispatched + 1);
        if (eligible) {
            wsiPresentId = allocateWsiPresentId();
            eligible = [r123Pump markItemDispatched:item wsiIdentity:wsiPresentId];
        }
        if (!eligible) [r110WsiDispatchLock unlock];
        if (eligible && !itemIsInternal) {
            pthread_mutex_lock(&r127SourceOwnershipLock);
            BOOL marked = R127SourceOwnershipMarkDispatched(&r127SourceOwnership, itemPairB);
            pthread_mutex_unlock(&r127SourceOwnershipLock);
            assert(marked);
            r130LastSourcePresentOrdinalDispatched = sourceOrdinal;
        }
    }
    if (eligible) {
        if (itemIsInternal) {
            logLine("R101_WSI_DISPATCH origin=INTERNAL internalPresentId=%llu wsiPresentId=%llu "
                    "appFrameId=NONE sourcePair=%llu->%llu drawable=%p drawableID=%lu texture=%p layer=%p device=%p queue=%p",
                (unsigned long long)internalPresentId,
                (unsigned long long)wsiPresentId,
                (unsigned long long)itemPairA, (unsigned long long)itemPairB,
                (__bridge void *)drawable, (unsigned long)drawable.drawableID,
                (__bridge void *)destination, (__bridge void *)layer,
                (__bridge void *)layer.device, (__bridge void *)r123Queue);
        } else {
            logLine("R101_WSI_DISPATCH origin=SOURCE appFrameId=%llu wsiPresentId=%llu "
                    "drawable=%p drawableID=%lu texture=%p layer=%p device=%p queue=%p "
                    "logicalDrawable=FG-FRESH sourceCaptureTexture=%p",
                (unsigned long long)appFrameId,
                (unsigned long long)wsiPresentId,
                (__bridge void *)drawable, (unsigned long)drawable.drawableID,
                (__bridge void *)destination, (__bridge void *)layer,
                (__bridge void *)layer.device, (__bridge void *)r123Queue,
                (__bridge void *)source);
        }
        r127ObserveWsiDisplay(drawable, origin, itemPairA, itemPairB, appFrameId,
            item.sourcePresentOrdinal, internalPresentId, wsiPresentId,
            itemEpoch, YES);
        r137SetPumpState(R137PumpStateWsiDispatch, r105NowNs());
        r137TracePoint(R137EventWsiIssue, itemPairB, itemPairA, itemPairB,
                       item.sourcePresentOrdinal, itemIsInternal ? 1u : 0u,
                       wsiPresentId, dispatchNs);
        boundary = !itemIsInternal && r127TransitionEpoch == itemEpoch &&
            r127TransitionBoundarySequence == itemPairB;
        boundaryRequestId = r127TransitionRequestId;
        CAMetalLayer *presentLayer = (CAMetalLayer *)drawable.layer;
        if (presentLayer.presentsWithTransaction) {
            [CATransaction begin];
            if (itemIsInternal) internalDispatchHostTime = fgmetalMediaNow();
            ((void (*)(id, SEL))presentOriginal)(drawable, @selector(present));
            [CATransaction commit];
        } else {
            if (itemIsInternal) internalDispatchHostTime = fgmetalMediaNow();
            ((void (*)(id, SEL))presentOriginal)(drawable, @selector(present));
        }
        [r110WsiDispatchLock unlock];
        if (!itemIsInternal &&
            itemPairB > r127LastPumpSourceDispatchedSequence)
            r127LastPumpSourceDispatchedSequence = itemPairB;
        if (!itemIsInternal) {
            assert(r130PendingFGDispatches > 0);
            --r130PendingFGDispatches;
            pthread_cond_broadcast(&r130DispatchCondition);
        }
        r127PumpItemInFlight = nil;
        dispatch = YES;
        if (itemIsInternal)
            r134NoteMidpointPresented(itemPairA, itemPairB, dispatchNs);
    } else {
        if (r127PumpItemInFlight == item) {
            if (liveMode == R127RuntimeModeSourceOnlyDrain &&
                item.kind == R123ItemKindSource && r123Pump &&
                [r123Pump requeueUndispatchedSource:item forEpoch:liveEpoch]) {
                // The source remains queued under the new source-only epoch.
            } else {
                if (r123Pump) [r123Pump rejectUndispatchedItem:item];
                ++r127StaleCallbackRejections;
            }
            r127PumpItemInFlight = nil;
        }
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    if (!dispatch) {
        logLine("R127_STALE_PUMP_ITEM=REJECTED kind=%s pair=%llu->%llu "
                "itemEpoch=%llu liveEpoch=%llu liveMode=%u action=no-wsi-dispatch",
            origin, (unsigned long long)itemPairA, (unsigned long long)itemPairB,
            (unsigned long long)itemEpoch, (unsigned long long)liveEpoch, liveMode);
        return;
    }
    if (boundary) {
        logLine("R127_RUNTIME_BOUNDARY_DISPATCH requestId=%llu epoch=%llu kind=SOURCE "
                "sequence=%llu appFrameId=%llu internalPresentId=NONE "
                "wsiPresentId=%llu path=FG-PUMP",
            (unsigned long long)boundaryRequestId,
            (unsigned long long)itemEpoch, (unsigned long long)itemPairB,
            (unsigned long long)appFrameId, (unsigned long long)wsiPresentId);
    }

    const uint64_t slot = ++r123SlotIndex;
    if (slot <= r123DetailedSlots) {
        logLine("R123_SLOT index=%llu origin=%s a=%llu b=%llu appFrameId=%llu "
                "internalPresentId=%llu wsiPresentId=%llu slotNs=%llu tickLateNs=%llu "
                "nextDrawableNs=%llu nextDrawableLatencyNs=%llu encodeNs=%llu commitNs=%llu "
                "copyGpuStartNs=%llu copyGpuEndNs=%llu copyWakeNs=%llu dispatchNs=%llu "
                "schedulingDelayNs=%llu copyCpuNs=%llu depth=%lu",
            (unsigned long long)slot, origin,
            (unsigned long long)item.seqA, (unsigned long long)item.seqB,
            (unsigned long long)appFrameId, (unsigned long long)internalPresentId,
            (unsigned long long)wsiPresentId, (unsigned long long)slotNs,
            (unsigned long long)(slotNs > 0 && wakeNs > slotNs ? wakeNs - slotNs : 0),
            (unsigned long long)ndReturnNs,
            (unsigned long long)(ndReturnNs - ndEnterNs),
            (unsigned long long)(commitNs - encodeStartNs),
            (unsigned long long)commitNs,
            (unsigned long long)copyGpuStartNs, (unsigned long long)copyGpuEndNs,
            (unsigned long long)copyWakeNs, (unsigned long long)dispatchNs,
            (unsigned long long)(dispatchNs > slotNs ? dispatchNs - slotNs : 0),
            (unsigned long long)(copyWakeNs - encodeStartNs),
            (unsigned long)[r123Pump depth]);
    } else if ((slot % 60) == 0) {
        logLine("R123_SLOT_SAMPLE index=%llu origin=%s a=%llu b=%llu wsiPresentId=%llu "
                "slotNs=%llu dispatchNs=%llu depth=%lu",
            (unsigned long long)slot, origin,
            (unsigned long long)item.seqA, (unsigned long long)item.seqB,
            (unsigned long long)wsiPresentId, (unsigned long long)slotNs,
            (unsigned long long)dispatchNs, (unsigned long)[r123Pump depth]);
    }
}

static void r123EmitSummary(const char *reason) {
    if (!r123Pump) return;
    if (__atomic_exchange_n(&r123SummaryEmitted, 1, __ATOMIC_RELAXED) != 0) return;
    r124EmitTestSummary(reason, r124TestMode());
    pthread_mutex_lock(&r132EstimatorLock);
    const NSUInteger estimatorStageOccupancy = r132OccupiedStageCountLocked();
    const uint64_t estimatorStagePeak = r132EstimatorStagePeak;
    const uint64_t estimatorReady = r132EstimatorReadyCount;
    const uint64_t estimatorNoCapture = r132EstimatorNoCaptureCount;
    const uint64_t estimatorLateCapture = r132EstimatorLateCaptureCount;
    const uint64_t estimatorStageRefusals = r132EstimatorStageRefusalCount;
    const uint64_t estimatorStageReclaims = r132EstimatorStageReclaimCount;
    const uint64_t estimatorOrderViolations = r132EstimatorOrderViolations;
    const uint64_t estimatorFgMissing = r132EstimatorFgMissingCaptureCount;
    const uint64_t nextEstimatorOrdinal = r132NextEstimatorOrdinal;
    const uint64_t r133Committed = r133CaptureCommittedCount;
    const uint64_t r133Complete = r133CaptureCompleteCount;
    const uint64_t r133Failed = r133CaptureFailedCount;
    const uint64_t r133PendingReady = r133PendingResolvedReady;
    const uint64_t r133PendingExpiredCount = r133PendingExpired;
    const uint64_t r133PrereqOk = r133CommitPrerequisiteSatisfied;
    const uint64_t r133PrereqMissing = r133CommitPrerequisiteAbsent;
    const uint64_t r133PendingPeakValue = r133PendingPeak;
    pthread_mutex_unlock(&r132EstimatorLock);
    logLine("R133_CAPTURE_SUMMARY committed=%llu complete=%llu failed=%llu "
            "allocationFailures=%llu preflightFailures=%llu pendingReady=%llu "
            "pendingExpired=%llu pendingPeak=%llu pendingGrace=%llu "
            "prerequisiteSatisfied=%llu prerequisiteAbsent=%llu "
            "copyBeforePresent=%llu copyAfterPresent=%llu",
        (unsigned long long)r133Committed,
        (unsigned long long)r133Complete,
        (unsigned long long)r133Failed,
        (unsigned long long)r133CaptureAllocationFailures,
        (unsigned long long)r133CapturePreflightFailures,
        (unsigned long long)r133PendingReady,
        (unsigned long long)r133PendingExpiredCount,
        (unsigned long long)r133PendingPeakValue,
        (unsigned long long)r133PendingGraceBound,
        (unsigned long long)r133PrereqOk,
        (unsigned long long)r133PrereqMissing,
        (unsigned long long)r133CopyCommittedBeforePresent,
        (unsigned long long)r133CopyCommittedAfterPresent);
    r133EmitProbeSummary("final");
    logLine("R132_ESTIMATOR_SUMMARY capacity=%u peak=%llu current=%lu ready=%llu noCapture=%llu lateCapture=%llu stageRefusals=%llu stageReclaims=%llu orderViolations=%llu fgMissingCapture=%llu nextEstimatorOrdinal=%llu",
        kR132EstimatorStageCapacity, (unsigned long long)estimatorStagePeak,
        (unsigned long)estimatorStageOccupancy,
        (unsigned long long)estimatorReady,
        (unsigned long long)estimatorNoCapture,
        (unsigned long long)estimatorLateCapture,
        (unsigned long long)estimatorStageRefusals,
        (unsigned long long)estimatorStageReclaims,
        (unsigned long long)estimatorOrderViolations,
        (unsigned long long)estimatorFgMissing,
        (unsigned long long)nextEstimatorOrdinal);
    logLine("R123_PUMP_SUMMARY=COMPLETE reason=%s sourceEnqueued=%llu sourcePresented=%llu "
            "internalEnqueued=%llu internalPresented=%llu internalResolved=%llu "
            "deadlineMisses=%llu droppedInternal=%llu droppedSource=%llu "
            "sourceSuppressedPresents=%llu drawableFailures=%llu presentFailures=%llu "
            "idleSlots=%llu currentDepth=%lu maxDepth=%llu orderViolations=%llu orderGaps=%llu "
            "sourcePeriodNs=%llu slots=%llu dropGuardDeferrals=%llu "
            "ignoredSelfPresentCompletions=%llu "
            "healthyLogSuppressed=%llu residualLogSuppressed=%llu "
            "anomalyLogSamples=%llu anomalyLogSuppressed=%llu "
            "unclassifiedLogSamples=%llu unclassifiedLogSuppressed=%llu",
        reason,
        (unsigned long long)[r123Pump sourceEnqueued],
        (unsigned long long)[r123Pump presentedSource],
        (unsigned long long)[r123Pump internalEnqueued],
        (unsigned long long)[r123Pump presentedInternal],
        (unsigned long long)[r123Pump internalResolved],
        (unsigned long long)[r123Pump deadlineMisses],
        (unsigned long long)[r123Pump droppedInternal],
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)__atomic_load_n(&r123SourceSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r123DrawableFailures, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r123PresentFailures, __ATOMIC_RELAXED),
        (unsigned long long)[r123Pump idleTicks],
        (unsigned long)[r123Pump depth],
        (unsigned long long)[r123Pump maxDepthSeen],
        (unsigned long long)[r123Pump orderViolations],
        (unsigned long long)[r123Pump orderGaps],
        (unsigned long long)[r123Pump sourcePeriodNs],
        (unsigned long long)__atomic_load_n(&r123SlotIndex, __ATOMIC_RELAXED),
        (unsigned long long)[r123Pump dropGuardDeferrals],
        (unsigned long long)__atomic_load_n(&ignoredSelfPresentCompletions, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127HealthyLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127ResidualLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127AnomalyLogSamples, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127AnomalyLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127UnclassifiedLogSamples, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127UnclassifiedLogSuppressed, __ATOMIC_RELAXED));
    r136EmitMissSummary(reason);
    r127EmitLatencySummaries([r123Pump sourcePeriodNs]);
}

static void r127MaybeEmitPeriodicSummary(void) {
    const uint64_t nowNs = r105NowNs();
    uint64_t expected = __atomic_load_n(&r127LastPeriodicSummaryNs, __ATOMIC_RELAXED);
    if (expected == 0) {
        (void)__atomic_compare_exchange_n(&r127LastPeriodicSummaryNs, &expected,
                                          nowNs, NO, __ATOMIC_RELAXED,
                                          __ATOMIC_RELAXED);
        return;
    }
    if (nowNs <= expected || nowNs - expected < 60000000000ULL) return;
    if (!__atomic_compare_exchange_n(&r127LastPeriodicSummaryNs, &expected,
                                     nowNs, NO, __ATOMIC_RELAXED,
                                     __ATOMIC_RELAXED)) return;
    if (!r123Pump) return;
    pthread_mutex_lock(&r132EstimatorLock);
    const NSUInteger r132StageNow = r132OccupiedStageCountLocked();
    const uint64_t r132PeakNow = r132EstimatorStagePeak;
    const uint64_t r132FrontierNow = r132NextEstimatorOrdinal - 1;
    const uint64_t r132ReadyNow = r132EstimatorReadyCount;
    const uint64_t r132NoCaptureNow = r132EstimatorNoCaptureCount;
    const uint64_t r132LateNow = r132EstimatorLateCaptureCount;
    const uint64_t r132ReclaimedNow = r132EstimatorStageReclaimCount;
    const uint64_t r132OrderNow = r132EstimatorOrderViolations;
    const NSUInteger r133PendingNow = r133PendingCount;
    const uint64_t r133PendingPeakNow = r133PendingPeak;
    const uint64_t r133PendingGraceNow = r133PendingGraceBound;
    const uint64_t r133PendingReadyNow = r133PendingResolvedReady;
    const uint64_t r133PendingExpiredNow = r133PendingExpired;
    const uint64_t r133LastResolvedNow = r133LastResolvedOrdinal;
    const uint64_t r133CommittedNow = r133CaptureCommittedCount;
    const uint64_t r133CompleteNow = r133CaptureCompleteCount;
    const uint64_t r133FailedNow = r133CaptureFailedCount;
    const uint64_t r133BeforePresentNow = r133CopyCommittedBeforePresent;
    const uint64_t r133AfterPresentNow = r133CopyCommittedAfterPresent;
    const uint64_t r133PrereqOkNow = r133CommitPrerequisiteSatisfied;
    const uint64_t r133PrereqMissingNow = r133CommitPrerequisiteAbsent;
    pthread_mutex_unlock(&r132EstimatorLock);
    logLine("R127_RUNTIME_SUMMARY=PERIODIC atMonotonicNs=%llu sourceEnqueued=%llu "
            "sourcePresented=%llu internalEnqueued=%llu internalPresented=%llu "
            "deadlineMisses=%llu droppedSource=%llu droppedInternal=%llu idleSlots=%llu "
            "currentDepth=%lu "
            "maxDepth=%llu orderViolations=%llu orderGaps=%llu sourcePeriodNs=%llu "
            "slots=%llu dropGuardDeferrals=%llu healthyLogSuppressed=%llu residualLogSuppressed=%llu "
            "anomalyLogSamples=%llu anomalyLogSuppressed=%llu "
            "unclassifiedLogSamples=%llu unclassifiedLogSuppressed=%llu "
            "estimatorStage=%lu estimatorPeak=%llu estimatorFrontier=%llu "
            "estimatorReady=%llu estimatorNoCapture=%llu estimatorLate=%llu "
            "estimatorReclaimed=%llu estimatorOrderViolations=%llu "
            "pendingNow=%lu pendingPeak=%llu pendingGrace=%llu pendingReady=%llu "
            "pendingExpired=%llu lastResolvedOrdinal=%llu "
            "captureCommitted=%llu captureComplete=%llu captureFailed=%llu "
            "copyBeforePresent=%llu copyAfterPresent=%llu prerequisiteSatisfied=%llu "
            "prerequisiteAbsent=%llu",
        (unsigned long long)nowNs,
        (unsigned long long)[r123Pump sourceEnqueued],
        (unsigned long long)[r123Pump presentedSource],
        (unsigned long long)[r123Pump internalEnqueued],
        (unsigned long long)[r123Pump presentedInternal],
        (unsigned long long)[r123Pump deadlineMisses],
        (unsigned long long)[r123Pump droppedSource],
        (unsigned long long)[r123Pump droppedInternal],
        (unsigned long long)[r123Pump idleTicks],
        (unsigned long)[r123Pump depth],
        (unsigned long long)[r123Pump maxDepthSeen],
        (unsigned long long)[r123Pump orderViolations],
        (unsigned long long)[r123Pump orderGaps],
        (unsigned long long)[r123Pump sourcePeriodNs],
        (unsigned long long)__atomic_load_n(&r123SlotIndex, __ATOMIC_RELAXED),
        (unsigned long long)[r123Pump dropGuardDeferrals],
        (unsigned long long)__atomic_load_n(&r127HealthyLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127ResidualLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127AnomalyLogSamples, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127AnomalyLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127UnclassifiedLogSamples, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r127UnclassifiedLogSuppressed, __ATOMIC_RELAXED),
        (unsigned long)r132StageNow,
        (unsigned long long)r132PeakNow,
        (unsigned long long)r132FrontierNow,
        (unsigned long long)r132ReadyNow,
        (unsigned long long)r132NoCaptureNow,
        (unsigned long long)r132LateNow,
        (unsigned long long)r132ReclaimedNow,
        (unsigned long long)r132OrderNow,
        (unsigned long)r133PendingNow,
        (unsigned long long)r133PendingPeakNow,
        (unsigned long long)r133PendingGraceNow,
        (unsigned long long)r133PendingReadyNow,
        (unsigned long long)r133PendingExpiredNow,
        (unsigned long long)r133LastResolvedNow,
        (unsigned long long)r133CommittedNow,
        (unsigned long long)r133CompleteNow,
        (unsigned long long)r133FailedNow,
        (unsigned long long)r133BeforePresentNow,
        (unsigned long long)r133AfterPresentNow,
        (unsigned long long)r133PrereqOkNow,
        (unsigned long long)r133PrereqMissingNow);
    r133EmitProbeSummary("periodic");
    r134EmitTimeline("periodic");
    r136EmitMissSummary("periodic");
}

// ---------------------------------------------------------------------------
// R124 -- DRAWABLE RETIREMENT CONTEXT
//
// R123 measured the blocker precisely and could not name its cause: a fresh
// drawable acquired by the pump, written, and presented is accepted, reports
// presentedTime = 0, and never returns to the three-deep pool, so after three
// presents every later nextDrawable blocks for its one-second timeout.
//
// R124 does not rebuild the pump. It runs two bounded, single-variable
// experiments against the same live layer:
//
//   TEST 1  The replacement present is issued from inside the title's own
//           present hook -- the title's render/present thread and context --
//           instead of the pump thread. Acquisition, payload, copy, present API
//           and drawable count are unchanged. Only the issuing thread differs.
//
//   TEST 2  The replacement drawable is acquired by the title's own thread at
//           its own acquisition point, then handed to the pump and presented
//           from the pump thread one source period later. Only the drawable's
//           provenance differs.
//
// Both tests are bounded: at most FGMETAL_R124_PRESENTS replacement presents
// (default 24), and either test aborts on the first nextDrawable that takes
// longer than 50 ms, so a failing test can never stall the title repeatedly.
//
// The control arm is FGMETAL_R124_TEST=0, which is R123's suppression-only
// behaviour and must reproduce R123 run 01.
// ---------------------------------------------------------------------------

typedef NS_ENUM(int, R124TestMode) {
    // Arm 0 is R123's pump. It is the measured failure, and it is destructive:
    // the pump's presents do not retire, so the three-deep pool is gone after
    // three of them and the title never acquires a drawable again. A session
    // must not start in it.
    R124TestNone = 0,
    R124TestTitleThreadPresent = 1,
    R124TestTitleAcquiredPumpPresented = 2,
    // Arm 3 is a pass-through: no suppression, no pump, the title presents its
    // own drawables exactly as it does without FG. It exists so the title can
    // be brought into the world -- visible, at full rate, with a healthy pool --
    // and so the first arms can be entered from a known-good state instead of
    // from a title that has already lost its pool.
    R124TestPassThrough = 3,
};

static void r124EmitTestSummary(const char *reason, int mode);

// The arm can be changed while the title is running, by writing an integer to
// this file. That matters more than it looks: the control arm, Test 1 and Test 2
// then run against the *same layer, same process, same session*, so a difference
// between them cannot be a difference between two launches.
// R125 uses its own control path. R124's controller leaves "0" behind at the end
// of a session, and 0 is the destructive pump arm; a stale file at a shared path
// would start this round's process in the one configuration it must never begin
// in.
static const char *const kR124ControlPath = "/tmp/fgmetal-r108-unattended/r125-test";

static int gR124ModeCache = -1;
static uint64_t gR124ModeCacheNs;
static uint64_t gR124ModeGeneration = 1;
// Recursive on purpose. This lock is taken on the title's present thread and on
// the pump thread, and the mode-change path logs a summary. A plain mutex
// deadlocked the whole title the first time an arm was switched (the summary
// helper re-entered the reader), which cost a session and looked exactly like a
// Metal stall. The recursion is a guard, not a licence: the helper now takes the
// mode as an argument.
static pthread_mutex_t r124ModeLock;
static pthread_once_t r124ModeLockOnce = PTHREAD_ONCE_INIT;

static void r124InitModeLock(void) {
    pthread_mutexattr_t attributes;
    pthread_mutexattr_init(&attributes);
    pthread_mutexattr_settype(&attributes, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&r124ModeLock, &attributes);
    pthread_mutexattr_destroy(&attributes);
}

// R125 control file: "mode pristine capture".
//
// `mode` is R124's arm. `pristine` and `capture` exist because R124's
// "pass-through" turned out not to be pristine in two ways that the round did
// not control:
//
//   * r123EnsureStarted() forces displaySyncEnabled from NO to YES when the
//     pump starts. It did so in R124's pass-through window
//     (R123_LAYER_DISPLAY_SYNC=FORCED before=0 after=1), so that window
//     measured a layer whose display-sync contract the instrument had changed.
//   * the same call starts the pump thread and, with the pump enabled, the
//     capture path copies every SOURCE frame.
//
// `pristine=1` refuses both: the pump never starts, displaySyncEnabled is never
// written, and the capture path returns before doing any work. `capture=1`
// allows capture without the pump. Two slices in one process then separate the
// three candidate causes (retention, display-sync/pump, capture) without a
// second launch.
static int r125ReadControlFile(int *outPristine, int *outCapture) {
    FILE *file = fopen(kR124ControlPath, "r");
    if (!file) return -1;
    int value = -1, pristine = -1, capture = -1;
    const int fields = fscanf(file, "%d %d %d", &value, &pristine, &capture);
    fclose(file);
    if (fields < 1 || value < 0 || value > 3) return -1;
    // Absent fields default to the pristine, capture-off configuration: a
    // one-integer control file written by R124's controller must never silently
    // re-enable the pump.
    if (outPristine) *outPristine = (pristine < 0) ? 1 : (pristine != 0);
    if (outCapture) *outCapture = (capture < 0) ? 0 : (capture != 0);
    return value;
}

// Runtime switches, refreshed by the same 200 ms control-file poll as the arm.
// Defaults are read once from the environment so a session can start in either
// configuration without a control file.
static BOOL gR125Pristine = YES;
static BOOL gR125CaptureAllowed = NO;
static pthread_once_t r125SwitchOnce = PTHREAD_ONCE_INIT;

static void r125InitSwitches(void) {
    const char *pristine = getenv("FGMETAL_R125_PRISTINE");
    const char *capture = getenv("FGMETAL_R125_CAPTURE");
    gR125Pristine = !(pristine && strcmp(pristine, "0") == 0);
    gR125CaptureAllowed = capture && strcmp(capture, "1") == 0;
}

// Called from the acquisition path, so it must be free. The values are plain
// BOOLs written under the recursive mode lock and read without it; a torn read
// cannot occur on a byte and a one-frame stale value is harmless for a control
// that is set by an operator between slices.
static BOOL r125Pristine(void) {
    pthread_once(&r125SwitchOnce, r125InitSwitches);
    return gR125Pristine;
}

static BOOL r125CaptureEnabled(void) {
    pthread_once(&r125SwitchOnce, r125InitSwitches);
    return gR125CaptureAllowed;
}

static uint64_t r124PresentBudget(void) {
    static uint64_t cached;
    if (cached == 0) {
        const char *value = getenv("FGMETAL_R124_PRESENTS");
        cached = value ? strtoull(value, NULL, 10) : 24;
        if (cached == 0) cached = 24;
    }
    return cached;
}

static uint64_t r124PresentCount;
static uint64_t r124PresentAttempts;
static uint64_t r124PresentDisplayed;
static BOOL r124TestAborted;
static const char *r124AbortReason = "none";
static uint64_t r124LastReplacementNs;
static uint64_t r124ConsecutiveBlocks;

static int r124TestMode(void) {
    pthread_once(&r124ModeLockOnce, r124InitModeLock);
    // R125: settle the environment defaults before any control-file write, so a
    // later first call to r125Pristine() cannot overwrite an operator setting.
    pthread_once(&r125SwitchOnce, r125InitSwitches);
    pthread_mutex_lock(&r124ModeLock);
    if (gR124ModeCache < 0) {
        const char *value = getenv("FGMETAL_R124_TEST");
        int initial = value ? atoi(value) : 3;
        if (initial < 0 || initial > 3) initial = 3;
        gR124ModeCache = initial;
        gR124ModeCacheNs = r105NowNs();
    }
    const uint64_t now = r105NowNs();
    if (now - gR124ModeCacheNs > 200000000ULL) {
        gR124ModeCacheNs = now;
        int pristine = gR125Pristine ? 1 : 0;
        int capture = gR125CaptureAllowed ? 1 : 0;
        const int fromFile = r125ReadControlFile(&pristine, &capture);
        const BOOL switchesChanged = (pristine != (gR125Pristine ? 1 : 0)) ||
                                     (capture != (gR125CaptureAllowed ? 1 : 0));
        if (fromFile >= 0 && (fromFile != gR124ModeCache || switchesChanged)) {
            const int leaving = gR124ModeCache;
            if (fromFile != gR124ModeCache) r124EmitTestSummary("mode-change", leaving);
            gR124ModeCache = fromFile;
            gR125Pristine = pristine != 0;
            gR125CaptureAllowed = capture != 0;
            gR124ModeGeneration += 1;
            __atomic_store_n(&r124PresentCount, 0, __ATOMIC_RELAXED);
            __atomic_store_n(&r124PresentAttempts, 0, __ATOMIC_RELAXED);
            __atomic_store_n(&r124PresentDisplayed, 0, __ATOMIC_RELAXED);
            r124TestAborted = NO;
            r124AbortReason = "none";
            // Pacing and block counters belong to an arm, not to the process.
            r124ConsecutiveBlocks = 0;
            r124LastReplacementNs = 0;
            logLine("R124_TEST_MODE=CHANGED mode=%d generation=%llu budget=%llu "
                    "pristine=%d capture=%d displaySyncWriteAllowed=%d",
                fromFile, (unsigned long long)gR124ModeGeneration,
                (unsigned long long)r124PresentBudget(),
                gR125Pristine ? 1 : 0, gR125CaptureAllowed ? 1 : 0,
                gR125Pristine ? 0 : 1);
        }
    }
    const int mode = gR124ModeCache;
    pthread_mutex_unlock(&r124ModeLock);
    return mode;
}


// Latest FG-owned SOURCE capture texture. Retained here so a replacement
// present never depends on capture timing; when it is absent the replacement
// falls back to a clear, which is still a known FG-owned image.
static id<MTLTexture> r124PayloadTexture;
static uint64_t r124PayloadSequence;
static pthread_mutex_t r124PayloadLock = PTHREAD_MUTEX_INITIALIZER;

static void r124NoteCapture(uint64_t sequence, id<MTLTexture> texture) {
    if (r124TestMode() == R124TestNone || !texture) return;
    pthread_mutex_lock(&r124PayloadLock);
    r124PayloadTexture = texture;
    r124PayloadSequence = sequence;
    pthread_mutex_unlock(&r124PayloadLock);
}

static id<MTLTexture> r124CopyPayload(uint64_t *outSequence) {
    pthread_mutex_lock(&r124PayloadLock);
    id<MTLTexture> texture = r124PayloadTexture;
    if (outSequence) *outSequence = r124PayloadSequence;
    pthread_mutex_unlock(&r124PayloadLock);
    return texture;
}

static void r124Abort(const char *reason, uint64_t detail) {
    if (r124TestAborted) return;
    r124TestAborted = YES;
    r124AbortReason = reason;
    logLine("R124_TEST=ABORTED reason=%s detail=%llu presents=%llu attempts=%llu",
        reason, (unsigned long long)detail,
        (unsigned long long)__atomic_load_n(&r124PresentCount, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r124PresentAttempts, __ATOMIC_RELAXED));
}

// The smallest present that still exercises the whole path: a fresh drawable,
// a known FG-owned image written into it, one dispatch, and a full report of
// what the layer did with it. `context` names the issuing thread/context and is
// the independent variable of both tests.
static void r124PresentReplacement(id<CAMetalDrawable> drawable,
                                   id<MTLTexture> payload,
                                   uint64_t sequence,
                                   const char *context,
                                   int copyWaitMs,
                                   uint64_t slotNs,
                                   uint64_t ndEnterNs,
                                   uint64_t ndReturnNs) {
    CAMetalLayer *layer = (CAMetalLayer *)(__bridge id)r123Layer;
    if (!layer || !drawable) {
        ++r123PresentFailures;
        logLine("R124_PRESENT_ABORTED reason=missing-layer-or-drawable context=%s", context);
        return;
    }
    if (!r123Queue) r123Queue = [layer.device newCommandQueue];
    if (!r123Queue) {
        ++r123PresentFailures;
        logLine("R124_PRESENT_ABORTED reason=queue-unavailable context=%s", context);
        return;
    }
    r126LogSelfPresentGate();

    id<MTLTexture> destination = drawable.texture;
    const BOOL usePayload = payload && destination &&
        payload.device == destination.device &&
        payload.width == destination.width && payload.height == destination.height &&
        (payload.usage & MTLTextureUsageShaderRead) != 0;

    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = destination;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    // A colour that alternates with the frame so a displayed replacement is
    // visually distinguishable from the frozen frame it replaces.
    pass.colorAttachments[0].clearColor = (sequence & 1)
        ? MTLClearColorMake(0.05, 0.45, 0.15, 1.0)
        : MTLClearColorMake(0.45, 0.05, 0.15, 1.0);

    const uint64_t encodeStartNs = r105NowNs();
    id<MTLCommandBuffer> commandBuffer = [r123Queue commandBuffer];
    id<MTLRenderCommandEncoder> encoder = commandBuffer
        ? [commandBuffer renderCommandEncoderWithDescriptor:pass] : nil;
    if (!commandBuffer || !encoder) {
        ++r123PresentFailures;
        logLine("R124_PRESENT_ABORTED reason=encoder-unavailable context=%s", context);
        return;
    }
    [encoder endEncoding];
    if (usePayload) {
        id<MTLBlitCommandEncoder> blit = [commandBuffer blitCommandEncoder];
        if (blit) {
            [blit copyFromTexture:payload toTexture:destination];
            [blit endEncoding];
        }
    }
    const uint64_t commitNs = r105NowNs();

    dispatch_semaphore_t copyDone = dispatch_semaphore_create(0);
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        (void)completed;
        (void)drawable;
        dispatch_semaphore_signal(copyDone);
    }];
    [commandBuffer commit];
    const long copyWait = dispatch_semaphore_wait(copyDone,
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)copyWaitMs * NSEC_PER_MSEC));
    const uint64_t copyWakeNs = r105NowNs();
    const BOOL copyOk = copyWait == 0 &&
        commandBuffer.status == MTLCommandBufferStatusCompleted &&
        commandBuffer.error == nil;
    if (!copyOk) {
        ++r123PresentFailures;
        logLine("R124_PRESENT_ABORTED reason=copy-%s status=%lu error=%s context=%s waitMs=%d",
            copyWait == 0 ? "failed" : "timeout",
            (unsigned long)commandBuffer.status,
            commandBuffer.error.localizedDescription.UTF8String ?: "none",
            context, copyWaitMs);
        return;
    }

    // Accounting contract unchanged: exactly one WSI identity, allocated
    // immediately before the dispatch that uses it.
    const uint64_t wsiPresentId = allocateWsiPresentId();
    logLine("R101_WSI_DISPATCH origin=SOURCE appFrameId=%llu wsiPresentId=%llu "
            "drawable=%p drawableID=%lu texture=%p layer=%p device=%p queue=%p "
            "logicalDrawable=FG-REPLACEMENT context=%s sourceCaptureTexture=%p usedPayload=%d",
        (unsigned long long)sequence, (unsigned long long)wsiPresentId,
        (__bridge void *)drawable, (unsigned long)drawable.drawableID,
        (__bridge void *)destination, (__bridge void *)layer,
        (__bridge void *)layer.device, (__bridge void *)r123Queue,
        context, (__bridge void *)payload, usePayload ? 1 : 0);

    __atomic_add_fetch(&r124PresentAttempts, 1, __ATOMIC_RELAXED);
    [drawable addPresentedHandler:^(id<MTLDrawable> presented) {
        const BOOL reachedDisplay = presented.presentedTime > 0.0;
        if (reachedDisplay) __atomic_add_fetch(&r124PresentDisplayed, 1, __ATOMIC_RELAXED);
        logLine("R101_WSI_DISPLAYED origin=SOURCE wsiPresentId=%llu appFrameId=%llu "
                "internalPresentId=0 drawableID=%lu presentedTime=%.9f reachedDisplay=%d "
                "displaySource=REPLACEMENT context=%s",
            (unsigned long long)wsiPresentId, (unsigned long long)sequence,
            (unsigned long)presented.drawableID, presented.presentedTime,
            reachedDisplay, context);
    }];

    IMP presentOriginal = originalFor(drawable, @selector(present));
    const uint64_t dispatchNs = r105NowNs();
    if (!presentOriginal) {
        logLine("R124_PRESENT_IMP=UNAVAILABLE context=%s drawable=%p",
            context, (__bridge void *)drawable);
    } else if (layer.presentsWithTransaction) {
        [CATransaction begin];
        ((void (*)(id, SEL))presentOriginal)(drawable, @selector(present));
        [CATransaction commit];
    } else {
        ((void (*)(id, SEL))presentOriginal)(drawable, @selector(present));
    }

    const uint64_t slot = __atomic_add_fetch(&r123SlotIndex, 1, __ATOMIC_RELAXED);
    logLine("R124_REPLACEMENT_PRESENT slot=%llu context=%s appFrameId=%llu "
            "wsiPresentId=%llu drawable=%p drawableID=%lu texture=%p slotNs=%llu "
            "nextDrawableEnterNs=%llu nextDrawableNs=%llu nextDrawableLatencyNs=%llu "
            "encodeNs=%llu commitNs=%llu copyWakeNs=%llu dispatchNs=%llu copyCpuNs=%llu "
            "usedPayload=%d",
        (unsigned long long)slot, context, (unsigned long long)sequence,
        (unsigned long long)wsiPresentId, (__bridge void *)drawable,
        (unsigned long)drawable.drawableID, (__bridge void *)destination,
        (unsigned long long)slotNs,
        (unsigned long long)ndEnterNs, (unsigned long long)ndReturnNs,
        (unsigned long long)(ndReturnNs > ndEnterNs ? ndReturnNs - ndEnterNs : 0),
        (unsigned long long)(commitNs - encodeStartNs),
        (unsigned long long)commitNs, (unsigned long long)copyWakeNs,
        (unsigned long long)dispatchNs,
        (unsigned long long)(copyWakeNs - encodeStartNs),
        usePayload ? 1 : 0);

    __atomic_add_fetch(&r124PresentCount, 1, __ATOMIC_RELAXED);
}

// --- Shared pacing and identity for both replacement arms ------------------
//
// A replacement present holds its drawable for at least one display period, so
// presenting on every title frame asks a three-deep pool for a fourth drawable
// and the request is answered by the one-second timeout. The first version of
// this test treated that timeout as a fatal condition and stopped after a single
// present -- which measured the pool's depth, not its recycling. The test now
// spaces its presents and skips the frames where the pool has nothing to give,
// so repeated presents are actually observed.
//
// Identity: a drawable that comes back is the only direct evidence of
// recycling. drawableID is a monotonic acquisition counter on this OS (proved by
// the standalone probe: 40 acquisitions, 40 distinct IDs, pool healthy), so
// recycling is tracked on the drawable object's pointer.
#define kR124SeenLimit 128
static uintptr_t r124SeenDrawables[kR124SeenLimit];
static uint64_t r124SeenFirstSlot[kR124SeenLimit];
static int r124SeenCount;
static pthread_mutex_t r124SeenLock = PTHREAD_MUTEX_INITIALIZER;

static uint64_t r124MinIntervalNs(void) {
    static uint64_t cached;
    if (cached == 0) {
        const char *value = getenv("FGMETAL_R124_MIN_INTERVAL_MS");
        const double ms = value ? atof(value) : 120.0;
        cached = (uint64_t)(ms * 1e6);
        if (cached == 0) cached = 120000000ULL;
    }
    return cached;
}

static int r124MaxConsecutiveBlocks(void) {
    static int cached = -1;
    if (cached < 0) {
        const char *value = getenv("FGMETAL_R124_MAX_BLOCKS");
        cached = value ? atoi(value) : 3;
        if (cached <= 0) cached = 3;
    }
    return cached;
}

// Returns the slot at which this drawable object was first seen, or 0 when it is
// new. Logged on every replacement acquisition, so "recycled" is a measurement
// rather than an inference from a counter.
static uint64_t r124NoteReplacementDrawable(id<CAMetalDrawable> drawable) {
    const uintptr_t pointer = (uintptr_t)(__bridge void *)drawable;
    uint64_t firstSlot = 0;
    pthread_mutex_lock(&r124SeenLock);
    for (int i = 0; i < r124SeenCount; ++i) {
        if (r124SeenDrawables[i] == pointer) {
            firstSlot = r124SeenFirstSlot[i];
            pthread_mutex_unlock(&r124SeenLock);
            return firstSlot;
        }
    }
    if (r124SeenCount < kR124SeenLimit) {
        r124SeenDrawables[r124SeenCount] = pointer;
        r124SeenFirstSlot[r124SeenCount] =
            __atomic_load_n(&r124PresentCount, __ATOMIC_RELAXED) + 1;
        r124SeenCount += 1;
    }
    pthread_mutex_unlock(&r124SeenLock);
    return 0;
}

// Common gate: interval pacing, budget, abort state.
static BOOL r124ReplacementDue(void) {
    if (r124TestAborted) return NO;
    if (__atomic_load_n(&r124PresentCount, __ATOMIC_RELAXED) >= r124PresentBudget()) return NO;
    const uint64_t now = r105NowNs();
    if (r124LastReplacementNs && now - r124LastReplacementNs < r124MinIntervalNs()) return NO;
    return YES;
}

// Acquire a replacement drawable on the calling (title) thread, or report why
// not. A blocked acquisition is skipped, not fatal: the pool being short for one
// frame is the expected shape of a three-deep pool under a second presenter.
static id<CAMetalDrawable> r124AcquireReplacement(const char *tag, uint64_t sequence,
                                                  uint64_t *outEnterNs,
                                                  uint64_t *outReturnNs) {
    CAMetalLayer *layer = (CAMetalLayer *)(__bridge id)r123Layer;
    if (!layer || !originalNextDrawable) return nil;

    isInternalDrawableAcquisition = YES;
    isInternalPresentationCall = YES;
    const uint64_t ndEnterNs = r105NowNs();
    id<CAMetalDrawable> drawable =
        ((id (*)(id, SEL))originalNextDrawable)(layer, @selector(nextDrawable));
    const uint64_t ndReturnNs = r105NowNs();
    isInternalDrawableAcquisition = NO;
    isInternalPresentationCall = NO;

    if (outEnterNs) *outEnterNs = ndEnterNs;
    if (outReturnNs) *outReturnNs = ndReturnNs;

    const uint64_t latency = ndReturnNs - ndEnterNs;
    if (!drawable) {
        r124ConsecutiveBlocks += 1;
        logLine("R124_%s_DRAWABLE=NIL seq=%llu latencyNs=%llu consecutiveBlocks=%llu "
                "action=skip-frame",
            tag, (unsigned long long)sequence, (unsigned long long)latency,
            (unsigned long long)r124ConsecutiveBlocks);
        if (r124ConsecutiveBlocks >= (uint64_t)r124MaxConsecutiveBlocks()) {
            r124Abort("nextDrawable-nil", latency);
        }
        return nil;
    }
    if (latency > 50000000ULL) {
        r124ConsecutiveBlocks += 1;
        logLine("R124_%s_DRAWABLE_BLOCKED seq=%llu drawableID=%lu latencyNs=%llu "
                "consecutiveBlocks=%llu action=skip-frame",
            tag, (unsigned long long)sequence, (unsigned long)drawable.drawableID,
            (unsigned long long)latency, (unsigned long long)r124ConsecutiveBlocks);
        if (r124ConsecutiveBlocks >= (uint64_t)r124MaxConsecutiveBlocks()) {
            r124Abort("nextDrawable-blocked", latency);
        }
        return nil;
    }
    r124ConsecutiveBlocks = 0;
    return drawable;
}

// TEST 1. Called from inside the title's present hook, after the SOURCE
// presentation has been suppressed, so the issuing thread and context are
// exactly those of the title's normal present path.
static void r124MaybeTitleThreadPresent(uint64_t sequence) {
    if (r124TestMode() != R124TestTitleThreadPresent) return;
    if (!r124ReplacementDue()) return;

    uint64_t ndEnterNs = 0, ndReturnNs = 0;
    id<CAMetalDrawable> drawable = r124AcquireReplacement("TEST1", sequence,
                                                          &ndEnterNs, &ndReturnNs);
    if (!drawable) return;

    const uint64_t firstSlot = r124NoteReplacementDrawable(drawable);
    logLine("R124_TEST1_ACQUIRED seq=%llu drawable=%p drawableID=%lu reusedFromSlot=%llu",
        (unsigned long long)sequence, (__bridge void *)drawable,
        (unsigned long)drawable.drawableID, (unsigned long long)firstSlot);

    r124LastReplacementNs = r105NowNs();
    uint64_t payloadSequence = 0;
    id<MTLTexture> payload = r124CopyPayload(&payloadSequence);
    r124PresentReplacement(drawable, payload, sequence, "TITLE-PRESENT-THREAD",
                           50, r105NowNs(), ndEnterNs, ndReturnNs);
}

// TEST 2. Pre-acquire a replacement drawable on the title's own acquisition
// thread, at the title's own acquisition point, and hand it to the pump.
static id<CAMetalDrawable> r124HandoffDrawable;
static uint64_t r124HandoffSequence;
static uint64_t r124HandoffAcquireNs;
static pthread_mutex_t r124HandoffLock = PTHREAD_MUTEX_INITIALIZER;

static void r124MaybePreAcquireReplacement(void) {
    if (r124TestMode() != R124TestTitleAcquiredPumpPresented) return;
    if (!r124ReplacementDue()) return;

    pthread_mutex_lock(&r124HandoffLock);
    const BOOL occupied = r124HandoffDrawable != nil;
    pthread_mutex_unlock(&r124HandoffLock);
    if (occupied) return;

    const uint64_t sequence = __atomic_load_n(&r124PresentCount, __ATOMIC_RELAXED) + 1;
    uint64_t ndEnterNs = 0, ndReturnNs = 0;
    id<CAMetalDrawable> drawable = r124AcquireReplacement("TEST2", sequence,
                                                          &ndEnterNs, &ndReturnNs);
    if (!drawable) return;

    const uint64_t firstSlot = r124NoteReplacementDrawable(drawable);
    pthread_mutex_lock(&r124HandoffLock);
    r124HandoffDrawable = drawable;
    r124HandoffSequence = sequence;
    r124HandoffAcquireNs = ndReturnNs;
    pthread_mutex_unlock(&r124HandoffLock);
    r124LastReplacementNs = r105NowNs();

    logLine("R124_HANDOFF_ACQUIRED drawable=%p drawableID=%lu thread=TITLE "
            "acquireLatencyNs=%llu reusedFromSlot=%llu action=hand-to-pump",
        (__bridge void *)drawable, (unsigned long)drawable.drawableID,
        (unsigned long long)(ndReturnNs - ndEnterNs), (unsigned long long)firstSlot);
}

static id<CAMetalDrawable> r124TakeHandoff(uint64_t *outSequence,
                                           uint64_t *outAcquireNs) {
    pthread_mutex_lock(&r124HandoffLock);
    id<CAMetalDrawable> drawable = r124HandoffDrawable;
    if (drawable) {
        if (outSequence) *outSequence = r124HandoffSequence;
        if (outAcquireNs) *outAcquireNs = r124HandoffAcquireNs;
        r124HandoffDrawable = nil;
    }
    pthread_mutex_unlock(&r124HandoffLock);
    return drawable;
}

static void r124EmitTestSummary(const char *reason, int mode) {
    logLine("R124_TEST_SUMMARY=COMPLETE reason=%s mode=%d presents=%llu attempts=%llu "
            "displayed=%llu aborted=%d abortReason=%s budget=%llu",
        reason, mode,
        (unsigned long long)__atomic_load_n(&r124PresentCount, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r124PresentAttempts, __ATOMIC_RELAXED),
        (unsigned long long)__atomic_load_n(&r124PresentDisplayed, __ATOMIC_RELAXED),
        r124TestAborted ? 1 : 0, r124AbortReason,
        (unsigned long long)r124PresentBudget());
}

// One slot of the display pump. Every decision is taken by R123DisplayPump;
// this function only sleeps until the slot opens, presents what it was given,
// and records what happened.
static BOOL r137Enabled(void);

static const char *r123QosClassName(qos_class_t qosClass);

// R139 Phase B: optional, env-gated pump QoS raise. Unset (the release
// default) keeps the exact R138-validated inherited-DEFAULT configuration.
// A failed request degrades to that default rather than refusing the
// runtime: the raise is a bounded experiment, never a launch blocker.
static qos_class_t r123ConfiguredPumpQos(void) {
    static dispatch_once_t once;
    static qos_class_t configured = QOS_CLASS_UNSPECIFIED;
    dispatch_once(&once, ^{
        const char *requested = getenv("FGMETAL_R139_PUMP_QOS");
        if (requested && strcmp(requested, "USER_INITIATED") == 0) {
            configured = QOS_CLASS_USER_INITIATED;
        }
    });
    return configured;
}

static int r123ConfigurePumpThreadAttributes(pthread_attr_t *attributes) {
    if (!attributes) return EINVAL;
    int status = pthread_attr_setdetachstate(attributes, PTHREAD_CREATE_DETACHED);
    if (status != 0) return status;
    const qos_class_t configured = r123ConfiguredPumpQos();
    if (configured != QOS_CLASS_UNSPECIFIED) {
        const int qosStatus = pthread_attr_set_qos_class_np(attributes, configured, 0);
        if (qosStatus != 0) {
            logLine("R127_PUMP_THREAD_QOS configured=%s requested-but-failed code=%d "
                    "falling-back-to-inherited",
                r123QosClassName(configured), qosStatus);
        }
    }
    return 0;
}

static const char *r123QosClassName(qos_class_t qosClass) {
    switch (qosClass) {
        case QOS_CLASS_USER_INTERACTIVE: return "USER_INTERACTIVE";
        case QOS_CLASS_USER_INITIATED: return "USER_INITIATED";
        case QOS_CLASS_DEFAULT: return "DEFAULT";
        case QOS_CLASS_UTILITY: return "UTILITY";
        case QOS_CLASS_BACKGROUND: return "BACKGROUND";
        case QOS_CLASS_UNSPECIFIED: return "UNSPECIFIED";
        default: return "UNKNOWN";
    }
}

static void *r123PumpMain(void *context) {
    (void)context;
    @autoreleasepool {
        pthread_mutex_lock(&r123PumpLock);
        __atomic_store_n(&r123WorkerReady, YES, __ATOMIC_RELEASE);
        pthread_cond_broadcast(&r123WorkerCondition);
        pthread_mutex_unlock(&r123PumpLock);
        while (!__atomic_load_n(&r123ThreadStop, __ATOMIC_RELAXED)) {
            const BOOL traceActive = r137Enabled();
            const uint64_t summaryBeginNs = traceActive ? r105NowNs() : 0;
            r127MaybeEmitPeriodicSummary();
            const uint64_t summaryEndNs = traceActive ? r105NowNs() : 0;
            if (traceActive && summaryEndNs > summaryBeginNs + 1000000ULL) {
                r137TracePoint(R137EventPumpSummaryDelay, 0, 0, 0, 0, 0,
                               summaryBeginNs, summaryEndNs);
            }
            uint64_t slotNs = 0;
            const uint64_t loopLockBeginNs = traceActive ? r105NowNs() : 0;
            pthread_mutex_lock(&r127RuntimeLock);
            const uint64_t loopLockAcquiredNs = traceActive ? r105NowNs() : 0;
            @autoreleasepool {
                if (r127RuntimeModeSnapshot() != R127RuntimeModeOff && r123Pump)
                    slotNs = r123Pump.nextSlotNs;
            }
            pthread_mutex_unlock(&r127RuntimeLock);
            if (traceActive &&
                loopLockAcquiredNs > loopLockBeginNs + 1000000ULL) {
                r137TracePoint(R137EventPumpLoopLockWait, 0, 0, 0, 0, 0,
                    loopLockBeginNs, loopLockAcquiredNs);
            }
            if (slotNs == 0) {
                r137SetPumpState(R137PumpStateNoSlot, r105NowNs());
                usleep(2000);
                continue;
            }

            // TEST 2: the pump presents only what the title's own thread
            // acquired. It never acquires for itself in this mode, so the only
            // difference from the failing R123 path is the drawable's origin.
            if (r124TestMode() == R124TestTitleAcquiredPumpPresented) {
                uint64_t handoffSequence = 0, handoffAcquireNs = 0;
                id<CAMetalDrawable> handoff = r124TakeHandoff(&handoffSequence, &handoffAcquireNs);
                if (!handoff) { usleep(2000); continue; }
                const uint64_t nowNs = r105NowNs();
                uint64_t payloadSequence = 0;
                id<MTLTexture> payload = r124CopyPayload(&payloadSequence);
                logLine("R124_HANDOFF_PRESENT who=PUMP drawable=%p drawableID=%lu ageNs=%llu",
                    (__bridge void *)handoff, (unsigned long)handoff.drawableID,
                    (unsigned long long)(nowNs > handoffAcquireNs ? nowNs - handoffAcquireNs : 0));
                r124PresentReplacement(handoff, payload, payloadSequence,
                                       "PUMP-THREAD-HANDOFF", 3000, nowNs, 0, 0);
                continue;
            }

            // R124: while a replacement arm is active the R123 pump must not
            // also present. Two presenters would put two different drawable
            // streams into the same layer and the comparison would no longer
            // have one variable. TEST 0 is the R123 pump, unchanged.
            if (r124TestMode() != R124TestNone) { usleep(2000); continue; }

            const uint64_t now = r105NowNs();
            if (now < slotNs) {
                r137SetPumpState(R137PumpStateWaitNextSlot, now);
                r137TracePoint(R137EventPumpWaitSlot, 0, 0, 0, 0, 0,
                               slotNs, slotNs - now);
                // macOS has no clock_nanosleep. The absolute target is kept and
                // the remaining interval recomputed on every pass, so a late or
                // early wake-up cannot accumulate into cadence drift: the next
                // sleep is always measured from slotNs, not from the wake-up.
                // A short sleep is re-entered rather than trusted, because
                // nanosleep may return early on a signal.
                uint64_t remainingNs = slotNs - now;
                struct timespec request;
                request.tv_sec = (time_t)(remainingNs / 1000000000ULL);
                request.tv_nsec = (long)(remainingNs % 1000000000ULL);
                struct timespec remaining = request;
                while (nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
                    if (remaining.tv_sec == 0 && remaining.tv_nsec == 0) break;
                }
                const uint64_t sleepReturnNs = r105NowNs();
                r137TracePoint(R137EventPumpSleepReturn, 0, 0, 0, 0, 0,
                               slotNs, sleepReturnNs);
                continue;
            }

            @autoreleasepool {
                r137SetPumpState(R137PumpStateTick, now);
                r137TracePoint(R137EventPumpTick, 0, 0, 0, 0, 0, slotNs, now);
                R123DisplayItem *item = nil;
                R123DisplayItem *missed = nil;
                // The tick is handed the *actual* wake instant, not the slot it
                // was scheduled for. A late slot must resynchronise in one step:
                // if it were handed its own stale deadline, every catch-up tick
                // would advance the schedule by only one half-period while real
                // time advanced by microseconds, and the pump would crawl back
                // to the present one phantom slot at a time -- measured at 1856
                // idle ticks for 63 real presentations on the first live run.
                // tickLateNs still reports wakeNs - slotNs, so lateness stays
                // visible even though it no longer poisons the next deadline.
                R123TickOutcome outcome = R123TickIdle;
                uint32_t mode = R127RuntimeModeOff;
                uint64_t epoch = 0;
                const uint64_t dispatchLockBeginNs = traceActive
                    ? r105NowNs() : 0;
                pthread_mutex_lock(&r127RuntimeLock);
                const uint64_t dispatchLockAcquiredNs = traceActive
                    ? r105NowNs() : 0;
                mode = r127RuntimeModeSnapshot();
                epoch = r127RuntimeEpochSnapshot();
                if (mode != R127RuntimeModeOff && r123Pump) {
                    outcome = [r123Pump takeForSlotAtNs:now
                                               outItem:&item
                                             outMissed:&missed];
                    if (item && item.runtimeEpoch == epoch && !r127PumpItemInFlight) {
                        r127PumpItemInFlight = item;
                    } else if (item) {
                        [r123Pump rejectUndispatchedItem:item];
                        item = nil;
                        ++r127StaleCallbackRejections;
                    }
                }
                pthread_mutex_unlock(&r127RuntimeLock);
                if (traceActive && dispatchLockAcquiredNs >
                    dispatchLockBeginNs + 1000000ULL) {
                    r137TracePoint(R137EventPumpDispatchLockWait,
                        0, 0, 0, 0, 0,
                        dispatchLockBeginNs, dispatchLockAcquiredNs);
                }
                if (missed) {
                    r137TracePoint(R137EventMidpointMiss, missed.seqB,
                        missed.seqA, missed.seqB,
                        missed.sourcePresentOrdinalB, 0, slotNs, outcome);
                    r134NoteMidpointMissed(missed.seqA, missed.seqB, slotNs);
                    logLine("R123_SLOT_MISSED pair=%llu->%llu reason=midpoint-not-ready-at-slot "
                            "action=drop-midpoint-present-source slotNs=%llu",
                        (unsigned long long)missed.seqA,
                        (unsigned long long)missed.seqB,
                        (unsigned long long)slotNs);
                    r136OnMiss(missed, item, slotNs);
                }
                if (item) {
                    r137SetPumpState(R137PumpStateProcessingClaimed, r105NowNs());
                    r137TracePoint(R137EventPumpClaim, item.seqB,
                        item.seqA, item.seqB, item.sourcePresentOrdinal,
                        item.kind == R123ItemKindInternal ? 1u : 0u,
                        item.readyNs, item.runtimeEpoch);
                    r123PresentItem(item, slotNs);
                } else if (outcome == R123TickIdle) {
                    const uint64_t idleNs = r105NowNs();
                    const R123DiagnosticSnapshot snapshot = r123Pump
                        ? [r123Pump diagnosticSnapshotAtNs:idleNs]
                        : (R123DiagnosticSnapshot){0};
                    uint32_t idleState = R137PumpStateEmptyQueue;
                    if (snapshot.claimed.present)
                        idleState = R137PumpStateProcessingClaimed;
                    else if (snapshot.head.present &&
                             snapshot.head.kind == R123ItemKindSource &&
                             !snapshot.head.sourceReservationCommitted)
                        idleState = R137PumpStateWaitSourceCommit;
                    else if (snapshot.head.present &&
                             snapshot.head.kind == R123ItemKindInternal &&
                             !snapshot.head.ready)
                        idleState = R137PumpStateWaitInternal;
                    r137SetPumpState(idleState, idleNs);
                    r137TracePoint(R137EventPumpIdle, 0, 0, 0, 0,
                                   idleState, snapshot.nextSlotNs,
                                   snapshot.depth);
                    if (__atomic_load_n(&r123SlotIndex, __ATOMIC_RELAXED) <= r123DetailedSlots) {
                        logLine("R123_SLOT_IDLE slotNs=%llu reason=buffer-empty",
                            (unsigned long long)slotNs);
                    }
                }
            }
        }
    }
    return NULL;
}

// Called on the frame path the first time a SOURCE image is captured. The layer
// is known by then: hookNextDrawable selected it before the frame was drawn.
// R125: session identity and layer properties, emitted once on the first
// selected acquisition rather than at pump start. A pristine slice never starts
// the pump, and those are exactly the facts it has to record: which arm, which
// registry shape, and what the layer's presentation properties were before the
// observer touched anything.
static void r125LogSessionIdentity(void) {
    static int logged;
    if (__atomic_exchange_n(&logged, 1, __ATOMIC_RELAXED) != 0) return;
    logLine("R124_MODE pid=%d testMode=%d budget=%llu controlPath=%s "
            "arm0=PUMP-THREAD arm1=TITLE-PRESENT-THREAD "
            "arm2=TITLE-ACQUIRED-PUMP-PRESENTED arm3=PASS-THROUGH drawablePool=UNCHANGED",
        getpid(), r124TestMode(),
        (unsigned long long)r124PresentBudget(), kR124ControlPath);
    logLine("R125_MODE pid=%d testMode=%d arm=%s correlationRegistry=SCALAR-ONLY "
            "retainedDrawables=0 retainedTextures=0 registryLimit=%lu "
            "maximumDrawableCountWrites=0 pacingChanges=0 displaySyncChanges=%d "
            "pristine=%d capture=%d pumpPresents=%d",
        getpid(), r124TestMode(),
        r124TestMode() == R124TestPassThrough ? "PASS-THROUGH" : "REPLACEMENT",
        (unsigned long)kRetainedDrawableLimit,
        r125Pristine() ? 0 : 1,
        r125Pristine() ? 1 : 0, r125CaptureEnabled() ? 1 : 0,
        r124TestMode() == R124TestNone ? 1 : 0);
    CAMetalLayer *probe = (CAMetalLayer *)(__bridge id)selectedHarnessLayer;
    if (!probe) return;
    logLine("R123_LAYER_PROPERTIES layer=%p displaySyncEnabled=%d "
            "presentsWithTransaction=%d allowsNextDrawableTimeout=%d "
            "framebufferOnly=%d maximumDrawableCount=%lu "
            "drawableSize=%.0fx%.0f contentsScale=%.3f opaque=%d",
        (__bridge void *)probe,
        probe.displaySyncEnabled ? 1 : 0,
        probe.presentsWithTransaction ? 1 : 0,
        probe.allowsNextDrawableTimeout ? 1 : 0,
        probe.framebufferOnly ? 1 : 0,
        (unsigned long)probe.maximumDrawableCount,
        (double)probe.drawableSize.width, (double)probe.drawableSize.height,
        (double)probe.contentsScale, probe.opaque ? 1 : 0);
}

static BOOL r123EnsureStarted(BOOL forEnable) {
    const uint32_t mode = r127RuntimeModeSnapshot();
    if (!r123PumpEnabled() || (!forEnable && mode == R127RuntimeModeOff)) return NO;
    pthread_mutex_lock(&r123PumpLock);
    if (r123ThreadStarted) {
        const BOOL ready = __atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE);
        pthread_mutex_unlock(&r123PumpLock);
        return ready;
    }
    if (!selectedHarnessLayer) {
        pthread_mutex_unlock(&r123PumpLock);
        return NO;
    }

    r123Layer = selectedHarnessLayer;
    CAMetalLayer *probe = (__bridge CAMetalLayer *)r123Layer;
    id<MTLDevice> device = probe.device;
    r123Pipeline = device
        ? makeInternalPresentationPipeline(device, MTLPixelFormatBGRA8Unorm) : nil;
    r123Queue = device ? [device newCommandQueue] : nil;
    if (!r123Pipeline || !r123Queue) {
        r123Pipeline = nil;
        r123Queue = nil;
        r123Layer = NULL;
        pthread_mutex_unlock(&r123PumpLock);
        logLine("R127_RUNTIME_ENABLE=REFUSED reason=presentation-resources-unavailable");
        return NO;
    }

    r123Pump = [[R123DisplayPump alloc] initWithMaxDepth:kR123MaxDepth
                                           halfPeriodNs:r123HalfPeriodNs()];
    const uint64_t runtimeEpoch = r127RuntimeEpochSnapshot();
    if (mode == R127RuntimeModeSourceOnlyDrain)
        (void)[r123Pump beginSourceOnlyEpoch:runtimeEpoch];
    else
        (void)[r123Pump setRuntimeEnabled:mode != R127RuntimeModeOff
                                    epoch:runtimeEpoch];
    r123DetailedSlots = r123MaxDetailedSlots();
    __atomic_store_n(&r123WorkerReady, NO, __ATOMIC_RELEASE);

    pthread_attr_t attributes;
    int attributesStatus = pthread_attr_init(&attributes);
    const BOOL attributesInitialized = attributesStatus == 0;
    if (attributesStatus == 0)
        attributesStatus = r123ConfigurePumpThreadAttributes(&attributes);
    if (attributesStatus != 0) {
        if (attributesInitialized) pthread_attr_destroy(&attributes);
        r123Pump = nil;
        r123Pipeline = nil;
        r123Queue = nil;
        r123Layer = NULL;
        r123ThreadStarted = NO;
        __atomic_store_n(&r123WorkerReady, NO, __ATOMIC_RELEASE);
        pthread_mutex_unlock(&r123PumpLock);
        logLine("R127_RUNTIME_ENABLE=REFUSED reason=pump-thread-attributes-failed code=%d",
            attributesStatus);
        return NO;
    }
    const int created = pthread_create(&r123Thread, &attributes, r123PumpMain, NULL);
    pthread_attr_destroy(&attributes);
    if (created != 0) {
        r123Pump = nil;
        r123Pipeline = nil;
        r123Queue = nil;
        r123Layer = NULL;
        r123ThreadStarted = NO;
        __atomic_store_n(&r123WorkerReady, NO, __ATOMIC_RELEASE);
        pthread_mutex_unlock(&r123PumpLock);
        logLine("R127_RUNTIME_ENABLE=REFUSED reason=pthread-create-failed code=%d", created);
        return NO;
    }
    r123ThreadStarted = YES;
    qos_class_t pumpQos = QOS_CLASS_UNSPECIFIED;
    int pumpRelativePriority = 0;
    const int qosRead = pthread_get_qos_class_np(r123Thread, &pumpQos,
                                                  &pumpRelativePriority);
    logLine("R127_PUMP_THREAD_QOS configured=%s actual=%s "
            "relativePriority=%d readCode=%d",
        r123QosClassName(r123ConfiguredPumpQos()), r123QosClassName(pumpQos),
        pumpRelativePriority, qosRead);

    // Require the new thread to enter its pump loop before accepting ON. A
    // short timeout keeps the title hook bounded; a later ON request can retry
    // if startup is merely delayed.
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_nsec += 250000000L;
    if (deadline.tv_nsec >= 1000000000L) {
        deadline.tv_sec += 1;
        deadline.tv_nsec -= 1000000000L;
    }
    while (!__atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE)) {
        const int waitResult = pthread_cond_timedwait(&r123WorkerCondition,
                                                       &r123PumpLock, &deadline);
        if (waitResult == ETIMEDOUT) break;
        if (waitResult != 0) break;
    }
    const BOOL ready = __atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE);
    if (ready && !probe.displaySyncEnabled) {
        probe.displaySyncEnabled = YES;
        logLine("R123_LAYER_DISPLAY_SYNC=FORCED layer=%p before=0 after=%d "
                "reason=pump-requires-one-present-per-refresh",
            (__bridge void *)probe, probe.displaySyncEnabled ? 1 : 0);
    }
    pthread_mutex_unlock(&r123PumpLock);
    logLine("R123_PUMP=%s created=%d workerReady=%d layer=%p "
            "maximumDrawableCount=%lu halfPeriodNs=%llu maxDepth=%d "
            "detailedSlots=%llu retainedSourceDrawables=0 "
            "displayPath=fresh-drawable-per-frame",
        ready ? "STARTED" : "START_PENDING", created, ready ? 1 : 0,
        r123Layer, (unsigned long)probe.maximumDrawableCount,
        (unsigned long long)r123HalfPeriodNs(), kR123MaxDepth,
        (unsigned long long)r123DetailedSlots);
    return ready;
}

static BOOL r132OrdinalIsAdjacent(uint64_t previousOrdinal,
                                  uint64_t currentOrdinal) {
    return previousOrdinal != 0 && currentOrdinal != 0 &&
        previousOrdinal != UINT64_MAX && currentOrdinal == previousOrdinal + 1;
}

static BOOL r132HistoryIsAdjacent(uint64_t previousSequence,
                                  uint64_t previousOrdinal,
                                  uint64_t currentSequence,
                                  uint64_t currentOrdinal) {
    if (r123PumpEnabled())
        return r132OrdinalIsAdjacent(previousOrdinal, currentOrdinal);
    return previousSequence != 0 && previousSequence != UINT64_MAX &&
        currentSequence == previousSequence + 1;
}

static NSUInteger r132OccupiedStageCountLocked(void) {
    NSUInteger count = 0;
    for (NSUInteger i = 0; i < kR132EstimatorStageCapacity; ++i)
        if (r132EstimatorStages[i].occupied) ++count;
    return count;
}

static R132EstimatorStageEntry *r132FindStageLocked(uint64_t sequence) {
    if (!sequence) return NULL;
    for (NSUInteger i = 0; i < kR132EstimatorStageCapacity; ++i) {
        R132EstimatorStageEntry *entry = &r132EstimatorStages[i];
        if (entry->occupied && entry->sequence == sequence) return entry;
    }
    return NULL;
}

static void r132ClearStageEntryLocked(R132EstimatorStageEntry *entry) {
    if (!entry) return;
    entry->texture = nil;
    entry->device = nil;
    entry->condition = nil;
    entry->sequence = 0;
    entry->sourcePresentOrdinal = 0;
    entry->captureEpoch = 0;
    entry->drawableID = 0;
    entry->sourceFormat = MTLPixelFormatInvalid;
    entry->generationOnly = NO;
    entry->occupied = NO;
    entry->inFlight = NO;
    entry->copyCommitted = NO;
    entry->copyComplete = NO;
    entry->copyFailed = NO;
    entry->captureCommandBuffer = 0;
    entry->copyCommittedNs = 0;
    entry->copyCompleteNs = 0;
    entry->presentHookNs = 0;
}

static R132EstimatorStageEntry *r132FindFreeStageLocked(void) {
    for (NSUInteger i = 0; i < kR132EstimatorStageCapacity; ++i)
        if (!r132EstimatorStages[i].occupied) return &r132EstimatorStages[i];
    return NULL;
}

// A staged capture can never reach a SOURCE presentation decision for one of two
// reasons, and both must release the slot rather than pin a full-resolution
// private texture for the life of the process:
//
//   1. its acquisition ticket is already terminal -- the ownership transaction
//      moved on and no ordinal will be assigned;
//   2. the presentation frontier has advanced more than a full staging window
//      past it, so it can no longer be a legitimately pending reorder. This is
//      the acquired-but-never-presented case, which the ticket test cannot see
//      because such a ticket is never finished.
//
// An in-flight entry is exempt: its texture is already owned by the serial block
// that releases it after estimator consumption.
//
// Caller holds r132EstimatorLock. The ticket array is a plain read under the
// caller's r127RuntimeLock; that is the same order used everywhere else
// (r127RuntimeLock -> r132EstimatorLock), so this cannot invert. This function
// performs no I/O: the caller logs after dropping the lock, so the present
// thread never waits on a file write.
// Caller holds r132EstimatorLock. A stage slot that is still needed by the
// estimator pipeline must not be reclaimed even when its acquisition ticket is
// terminal: the FG commit terminalises the ticket before the GPU copy completes.
static BOOL r133StageIsPendingSequenceLocked(uint64_t sequence) {
    for (NSUInteger i = 0; i < r133PendingCount; ++i)
        if (r133Pending[i].sequence == sequence) return YES;
    return NO;
}

static NSUInteger r132ReclaimDeadStagesLocked(void) {
    NSUInteger reclaimed = 0;
    for (NSUInteger i = 0; i < kR132EstimatorStageCapacity; ++i) {
        R132EstimatorStageEntry *entry = &r132EstimatorStages[i];
        if (!entry->occupied || entry->inFlight) continue;
        if (entry->copyCommitted && !entry->copyComplete && !entry->copyFailed)
            continue;
        if (r133StageIsPendingSequenceLocked(entry->sequence)) continue;
        const BOOL ticketTerminal =
            !r130HasActiveTicketForSequenceLocked(entry->sequence);
        const BOOL behindFrontier = r132NewestPublishedSequence >
            entry->sequence + (uint64_t)kR132EstimatorStageCapacity;
        if (!ticketTerminal && !behindFrontier) continue;
        r132ClearStageEntryLocked(entry);
        ++reclaimed;
    }
    r132EstimatorStageReclaimCount += reclaimed;
    return reclaimed;
}

static BOOL r132StageCapture(uint64_t sequence, uint64_t drawableID,
                             MTLPixelFormat sourceFormat,
                             id<MTLTexture> texture, id<MTLDevice> device,
                             NSString *condition, uint64_t captureEpoch,
                             BOOL generationOnly) {
    if (!sequence || !texture || !device) return NO;
    BOOL staged = NO;
    int logKind = 0;              // 1 = terminal, 2 = already staged
    uint64_t logCount = 0;
    uint64_t logOrdinal = 0;
    BOOL logReadyDisposition = NO;
    NSUInteger reclaimed = 0;
    NSUInteger occupancyAfterReclaim = 0;

    pthread_mutex_lock(&r132EstimatorLock);
    R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (terminal->sequence == sequence && terminal->disposition != 0) {
        logCount = ++r132EstimatorLateCaptureCount;
        logKind = 1;
        logOrdinal = terminal->sourcePresentOrdinal;
        logReadyDisposition = terminal->disposition == 1;
    } else if (r132FindStageLocked(sequence)) {
        logCount = ++r132EstimatorLateCaptureCount;
        logKind = 2;
    } else {
        R132EstimatorStageEntry *slot = r132FindFreeStageLocked();
        if (!slot) {
            reclaimed = r132ReclaimDeadStagesLocked();
            if (reclaimed) {
                occupancyAfterReclaim = r132OccupiedStageCountLocked();
                slot = r132FindFreeStageLocked();
            }
        }
        if (!slot) {
            ++r132EstimatorStageRefusalCount;
        } else {
            slot->sequence = sequence;
            slot->sourcePresentOrdinal = 0;
            slot->captureEpoch = captureEpoch;
            slot->drawableID = drawableID;
            slot->sourceFormat = sourceFormat;
            slot->texture = texture;
            slot->device = device;
            slot->condition = condition ?: @"R132_PRESENTATION_ORDERED";
            slot->generationOnly = generationOnly;
            slot->occupied = YES;
            slot->inFlight = NO;
            // Legacy staging path: the texture handed in is already complete.
            // The R133 in-producer-command-buffer path stages through
            // r133StageCommittedCapture, which leaves copyComplete NO until the
            // producer command buffer's completion handler runs.
            slot->copyCommitted = YES;
            slot->copyComplete = YES;
            slot->copyFailed = NO;
            slot->captureCommandBuffer = 0;
            slot->copyCommittedNs = r105NowNs();
            slot->copyCompleteNs = slot->copyCommittedNs;
            slot->presentHookNs = 0;
            const uint64_t occupied = r132OccupiedStageCountLocked();
            if (occupied > r132EstimatorStagePeak)
                r132EstimatorStagePeak = occupied;
            staged = YES;
        }
    }
    const uint64_t reclaimTotal = r132EstimatorStageReclaimCount;
    pthread_mutex_unlock(&r132EstimatorLock);

    // Legacy staging path: the texture handed in is already a complete private
    // copy, so it establishes capture availability for this AcquisitionId even
    // if the estimator stage table refused the slot above.
    if (logKind == 1) r133ClearCaptureAvailability(sequence);
    else r133NoteCaptureAvailability(sequence, 1, 1, 0, r105NowNs());

    if (logKind == 1 && logCount <= 8)
        logLine("R132_LATE_CAPTURE=REJECTED acquisitionId=%llu "
                "reason=terminal-disposition-published disposition=%s "
                "action=no-estimator-insert ordinal=%llu count=%llu",
            (unsigned long long)sequence,
            logReadyDisposition ? "CAPTURE_READY" : "NO_CAPTURE",
            (unsigned long long)logOrdinal, (unsigned long long)logCount);
    else if (logKind == 2 && logCount <= 8)
        logLine("R132_LATE_CAPTURE=REJECTED acquisitionId=%llu "
                "reason=already-staged action=no-estimator-insert count=%llu",
            (unsigned long long)sequence, (unsigned long long)logCount);

    if (reclaimed && reclaimTotal <= (uint64_t)kR132EstimatorStageCapacity * 4)
        logLine("R132_STAGE_RECLAIM=RELEASED count=%lu capacity=%u "
                "reason=ticket-terminal-or-frontier-passed action=free-stage-slot "
                "occupancy=%lu totalReclaimed=%llu",
            (unsigned long)reclaimed, kR132EstimatorStageCapacity,
            (unsigned long)occupancyAfterReclaim, (unsigned long long)reclaimTotal);
    return staged;
}

static void r132ReleaseStage(uint64_t sequence, uint64_t sourcePresentOrdinal) {
    pthread_mutex_lock(&r132EstimatorLock);
    R132EstimatorStageEntry *entry = r132FindStageLocked(sequence);
    if (entry && entry->inFlight &&
        entry->sourcePresentOrdinal == sourcePresentOrdinal)
        r132ClearStageEntryLocked(entry);
    pthread_mutex_unlock(&r132EstimatorLock);
}

static void r132ResetEstimatorHistoryOnQueue(void) {
    if (realFrameProvider) [realFrameProvider invalidateHistory];
    previousCaptureColor = nil;
    previousCaptureDepth = nil;
    previousCaptureMotion = nil;
    previousCaptureSource = nil;
    previousCaptureSequence = 0;
    previousCapturePresentOrdinal = 0;
    pthread_mutex_lock(&r132EstimatorLock);
    r132GapResetQueued = NO;
    pthread_mutex_unlock(&r132EstimatorLock);
}

static void r132QueueHistoryGapReset(void) {
    if (!captureProcessingQueue || r132EstimatorTestSinkEnabled) return;
    BOOL shouldQueue = NO;
    pthread_mutex_lock(&r132EstimatorLock);
    if (!r132GapResetQueued) {
        r132GapResetQueued = YES;
        shouldQueue = YES;
    }
    pthread_mutex_unlock(&r132EstimatorLock);
    if (shouldQueue) {
        dispatch_async(captureProcessingQueue, ^{
            @autoreleasepool { r132ResetEstimatorHistoryOnQueue(); }
        });
    }
}

static void r132RecordTestIngestion(uint64_t sequence,
                                    uint64_t sourcePresentOrdinal,
                                    R127SourceOwner owner,
                                    id<MTLTexture> sourceTexture) {
    pthread_mutex_lock(&r132EstimatorLock);
    if (r132TestIngestCount < 1024) {
        const NSUInteger index = r132TestIngestCount++;
        r132TestIngestSequence[index] = sequence;
        r132TestIngestOrdinal[index] = sourcePresentOrdinal;
        r132TestIngestOwner[index] = (uint32_t)owner;
        const BOOL pair = r132HistoryIsAdjacent(r132TestHistorySequence,
            r132TestHistoryOrdinal, sequence, sourcePresentOrdinal);
        r132TestPairA[index] = pair ? r132TestHistorySequence : 0;
        r132TestPairB[index] = pair ? sequence : 0;
        r132TestIngestTexture[index] = sourceTexture;
        r132TestHistorySequence = sequence;
        r132TestHistoryOrdinal = sourcePresentOrdinal;
    }
    pthread_mutex_unlock(&r132EstimatorLock);
}

// The integration harness uses this reset only between isolated fake-device
// cases. Runtime startup never resets the monotonic production frontier.
static void r132ResetForIntegrationTest(void) {
    pthread_mutex_lock(&r132EstimatorLock);
    for (NSUInteger i = 0; i < kR132EstimatorStageCapacity; ++i)
        r132ClearStageEntryLocked(&r132EstimatorStages[i]);
    memset(r132EstimatorTerminals, 0, sizeof(r132EstimatorTerminals));
    memset(r132TestIngestSequence, 0, sizeof(r132TestIngestSequence));
    memset(r132TestIngestOrdinal, 0, sizeof(r132TestIngestOrdinal));
    memset(r132TestIngestOwner, 0, sizeof(r132TestIngestOwner));
    memset(r132TestPairA, 0, sizeof(r132TestPairA));
    memset(r132TestPairB, 0, sizeof(r132TestPairB));
    for (NSUInteger i = 0; i < 1024; ++i) r132TestIngestTexture[i] = nil;
    r132NextEstimatorOrdinal = 1;
    r132NewestPublishedSequence = 0;
    r132EstimatorStagePeak = 0;
    r132EstimatorReadyCount = 0;
    r132EstimatorNoCaptureCount = 0;
    r132EstimatorLateCaptureCount = 0;
    r132EstimatorStageRefusalCount = 0;
    r132EstimatorStageReclaimCount = 0;
    r132EstimatorOrderViolations = 0;
    r132EstimatorFgMissingCaptureCount = 0;
    r132TestIngestCount = 0;
    r132TestNoCaptureCount = 0;
    r132TestHistorySequence = 0;
    r132TestHistoryOrdinal = 0;
    r132GapResetQueued = NO;
    memset(r133Pending, 0, sizeof(r133Pending));
    r133PendingCount = 0;
    r133LastResolvedOrdinal = 0;
    r133PendingPeak = 0;
    r133PendingResolvedReady = 0;
    r133PendingExpired = 0;
    r133LateExpiredCaptures = 0;
    r133CaptureCommittedCount = 0;
    r133CaptureCompleteCount = 0;
    r133CaptureFailedCount = 0;
    r133CaptureAllocationFailures = 0;
    r133CapturePreflightFailures = 0;
    r133CopyCommittedBeforePresent = 0;
    r133CopyCommittedAfterPresent = 0;
    r133CommitPrerequisiteSatisfied = 0;
    r133CommitPrerequisiteAbsent = 0;
    r133ProbeSamples = 0;
    r133ProbeMinNs = 0;
    r133ProbeMaxNs = 0;
    r133ProbeOverflow = 0;
    memset(r133CaptureRecords, 0, sizeof(r133CaptureRecords));
    r133CaptureRecordWrites = 0;
    r133CaptureRecordMismatches = 0;
    r132EstimatorTestSinkEnabled = YES;
    pthread_mutex_unlock(&r132EstimatorLock);
}

// ---------------------------------------------------------------------------
// R133 pending-ordinal machinery.
//
// An ordinal whose capture copy is committed but not yet GPU-complete is
// PENDING_CAPTURE. It is estimator-only state: it never delays the title WSI,
// never retains a drawable, and never reserves FG presentation capacity.
//
// Bound: ordinal N stays eligible until N + grace is published. The default
// grace is one SOURCE interval, which the Phase-0 measurements justify; the
// value is capped by the pending array so the window is hard-bounded. When the
// window closes, N is finalized with the disposition it has earned. Resolution
// is strictly in ordinal order: a later ordinal is never dispatched across an
// unresolved earlier one.
// ---------------------------------------------------------------------------
static uint64_t r133PendingGrace(void) {
    if (r133PendingGraceCached < 0) {
        const char *value = getenv("FGMETAL_R133_PENDING_GRACE");
        uint64_t requested = value ? strtoull(value, NULL, 10)
                                   : kR133DefaultPendingGrace;
        if (requested < 1) requested = 1;
        if (requested > (uint64_t)(kR133MaxPendingOrdinals - 1))
            requested = (uint64_t)(kR133MaxPendingOrdinals - 1);
        r133PendingGraceCached = (int)requested;
        r133PendingGraceBound = requested;
    }
    return (uint64_t)r133PendingGraceCached;
}

static void r133RemovePendingLocked(NSUInteger index) {
    if (index >= r133PendingCount) return;
    for (NSUInteger i = index; i + 1 < r133PendingCount; ++i)
        r133Pending[i] = r133Pending[i + 1];
    --r133PendingCount;
    memset(&r133Pending[r133PendingCount], 0, sizeof(R133PendingOrdinal));
}

static void r133NoteTerminalLocked(uint64_t sequence, uint64_t ordinal,
                                   uint8_t disposition) {
    R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    terminal->sequence = sequence;
    terminal->sourcePresentOrdinal = ordinal;
    terminal->disposition = disposition;
    if (ordinal > r133LastResolvedOrdinal) r133LastResolvedOrdinal = ordinal;
}

static R133Action *r133AppendActionLocked(R133ActionList *actions) {
    if (!actions || actions->count >= kR133MaxActions) return NULL;
    return &actions->items[actions->count++];
}

// Caller holds r132EstimatorLock. Appends at most one action per resolved
// ordinal. `expireUnresolved` closes the window for ordinals whose grace has
// elapsed at `publishFrontier`.
static void r133DrainPendingLocked(R133ActionList *actions,
                                   BOOL expireUnresolved,
                                   uint64_t publishFrontier) {
    const uint64_t grace = r133PendingGrace();
    while (r133PendingCount > 0) {
        R133PendingOrdinal *head = &r133Pending[0];
        if (!head->resolved) {
            if (!expireUnresolved) break;
            if (head->ordinal + grace > publishFrontier) break;
            ++r132EstimatorNoCaptureCount;
            ++r133PendingExpired;
            if (r132EstimatorTestSinkEnabled) ++r132TestNoCaptureCount;
            r133NoteTerminalLocked(head->sequence, head->ordinal, 2);
            R132EstimatorStageEntry *entry = r132FindStageLocked(head->sequence);
            if (entry && !entry->inFlight) r132ClearStageEntryLocked(entry);
            R133Action *action = r133AppendActionLocked(actions);
            if (action) action->gapReset = YES;
            r133RemovePendingLocked(0);
            continue;
        }
        if (!head->captureOk) {
            ++r132EstimatorNoCaptureCount;
            if (r132EstimatorTestSinkEnabled) ++r132TestNoCaptureCount;
            r133NoteTerminalLocked(head->sequence, head->ordinal, 2);
            R132EstimatorStageEntry *entry = r132FindStageLocked(head->sequence);
            if (entry && !entry->inFlight) r132ClearStageEntryLocked(entry);
            R133Action *action = r133AppendActionLocked(actions);
            if (action) action->gapReset = YES;
            r133RemovePendingLocked(0);
            continue;
        }
        R132EstimatorStageEntry *entry = r132FindStageLocked(head->sequence);
        if (!entry || !entry->texture || !entry->device) {
            ++r132EstimatorNoCaptureCount;
            if (r132EstimatorTestSinkEnabled) ++r132TestNoCaptureCount;
            r133NoteTerminalLocked(head->sequence, head->ordinal, 2);
            R133Action *action = r133AppendActionLocked(actions);
            if (action) action->gapReset = YES;
            r133RemovePendingLocked(0);
            continue;
        }
        BOOL generationSlotHeld = NO;
        if (entry->generationOnly && !r132EstimatorTestSinkEnabled) {
            __atomic_add_fetch(&r122PairsAttempted, 1, __ATOMIC_RELAXED);
            generationSlotHeld = r122TryAcquireGenerationSlot();
            if (!generationSlotHeld) {
                __atomic_add_fetch(&r122PairsSkippedBusy, 1, __ATOMIC_RELAXED);
                r122MaybeEmitSummary();
                ++r132EstimatorNoCaptureCount;
                if (r132EstimatorTestSinkEnabled) ++r132TestNoCaptureCount;
                r133NoteTerminalLocked(head->sequence, head->ordinal, 2);
                r132ClearStageEntryLocked(entry);
                R133Action *action = r133AppendActionLocked(actions);
                if (action) action->gapReset = YES;
                r133RemovePendingLocked(0);
                continue;
            }
        }
        entry->sourcePresentOrdinal = head->ordinal;
        entry->inFlight = YES;
        R133Action *action = r133AppendActionLocked(actions);
        if (action) {
            action->snapshot = *entry;
            action->dispatch = YES;
            action->generationSlotHeld = generationSlotHeld;
        } else if (generationSlotHeld) {
            r122ReleaseGenerationSlot();
        }
        ++r132EstimatorReadyCount;
        ++r133PendingResolvedReady;
        r133NoteTerminalLocked(head->sequence, head->ordinal, 1);
        r133RemovePendingLocked(0);
    }
    if (r133PendingCount > r133PendingPeak)
        r133PendingPeak = r133PendingCount;
}

// Executes a decided action list in order. Gap resets and estimator work are
// both queued on the one serial capture queue, so a reset can never overtake
// the frame it must precede.
static void r133ExecuteActions(R133ActionList *actions, BOOL testSink) {
    if (!actions) return;
    for (NSUInteger i = 0; i < actions->count; ++i) {
        R133Action *action = &actions->items[i];
        if (action->gapReset) r132QueueHistoryGapReset();
        if (!action->dispatch) continue;
        const R132EstimatorStageEntry snapshot = action->snapshot;
        const R127SourceOwner owner = action->owner;
        const BOOL generationSlotHeld = action->generationSlotHeld;
        if (testSink) {
            r132RecordTestIngestion(snapshot.sequence,
                snapshot.sourcePresentOrdinal, owner, snapshot.texture);
            r132ReleaseStage(snapshot.sequence, snapshot.sourcePresentOrdinal);
            if (generationSlotHeld) r122ReleaseGenerationSlot();
            continue;
        }
        const double r119Dispatch = fgmetalMediaNow();
        if (r115ContinuousEnabled()) {
            pthread_mutex_lock(&stateLock);
            const BOOL r119IsPairB = r115Timing.pairB == snapshot.sequence;
            if (r119IsPairB) r115Timing.mediaDispatch = r119Dispatch;
            pthread_mutex_unlock(&stateLock);
            if (r119IsPairB) r121Mark(R121SlotTaskEnqueue);
        }
        dispatch_async(captureProcessingQueue, ^{
            @autoreleasepool {
                processCapturedFrame(snapshot.sequence, snapshot.drawableID,
                    snapshot.sourceFormat, snapshot.texture, snapshot.device,
                    snapshot.condition, snapshot.generationOnly,
                    snapshot.captureEpoch, snapshot.sourcePresentOrdinal);
                r132ReleaseStage(snapshot.sequence, snapshot.sourcePresentOrdinal);
                if (generationSlotHeld) r122ReleaseGenerationSlot();
                if (r122OverlapEnabled()) r122MaybeEmitSummary();
            }
        });
    }
}

static void r132PublishPresentation(uint64_t sequence,
                                    uint64_t sourcePresentOrdinal,
                                    R127SourceOwner owner) {
    if (!sourcePresentOrdinal) return;
    R133ActionList actions = {0};
    BOOL testSink = NO;
    BOOL orderGap = NO;
    BOOL committedCopyKnown = NO;
    uint32_t stageFlags = 0;
    uint64_t stageCopyNs = 0;
    const uint64_t presentNs = r105NowNs();
    pthread_mutex_lock(&r132EstimatorLock);
    testSink = r132EstimatorTestSinkEnabled;
    if (sourcePresentOrdinal < r132NextEstimatorOrdinal) {
        ++r132EstimatorOrderViolations;
        pthread_mutex_unlock(&r132EstimatorLock);
        return;
    }
    if (sourcePresentOrdinal > r132NextEstimatorOrdinal) {
        orderGap = YES;
        r132EstimatorOrderViolations +=
            sourcePresentOrdinal - r132NextEstimatorOrdinal;
        r132EstimatorNoCaptureCount +=
            sourcePresentOrdinal - r132NextEstimatorOrdinal;
        if (testSink)
            r132TestNoCaptureCount +=
                (NSUInteger)(sourcePresentOrdinal - r132NextEstimatorOrdinal);
        r132NextEstimatorOrdinal = sourcePresentOrdinal;
    }

    R132EstimatorStageEntry *entry = r132FindStageLocked(sequence);
    if (entry) {
        entry->presentHookNs = presentNs;
        if (entry->copyCommitted) {
            if (entry->copyCommittedNs <= presentNs)
                ++r133CopyCommittedBeforePresent;
            else
                ++r133CopyCommittedAfterPresent;
        }
    }

    // Bounded estimator grace. Publishing ordinal N makes every pending ordinal
    // whose window has now closed ineligible for a late capture; it is resolved
    // with the disposition it has earned at this boundary.
    r133DrainPendingLocked(&actions, YES, sourcePresentOrdinal);

    BOOL ready = NO;
    BOOL pending = NO;
    BOOL generationSlotHeld = NO;
    if (entry && entry->texture && entry->device) {
        if (!testSink && !captureProcessingQueue) {
            ++r132EstimatorStageRefusalCount;
        } else if (entry->copyComplete && !entry->copyFailed) {
            if (entry->generationOnly && !testSink) {
                __atomic_add_fetch(&r122PairsAttempted, 1, __ATOMIC_RELAXED);
                generationSlotHeld = r122TryAcquireGenerationSlot();
                if (!generationSlotHeld) {
                    __atomic_add_fetch(&r122PairsSkippedBusy, 1, __ATOMIC_RELAXED);
                    r122MaybeEmitSummary();
                }
            }
            ready = !entry->generationOnly || testSink || generationSlotHeld;
        } else if (entry->copyCommitted && !entry->copyFailed) {
            // Capture availability is a separate fact from ownership. The copy
            // is committed and owned by the GPU; its completion may legitimately
            // land after this present hook.
            pending = YES;
        }
    }

    const BOOL stageEntryPresent = entry != NULL;
    const BOOL stageHasTexture = entry && entry->texture;
    const BOOL stageHasDevice = entry && entry->device;
    const BOOL stageCopyCommitted = entry && entry->copyCommitted;
    const BOOL stageCopyComplete = entry && entry->copyComplete;
    const BOOL stageCopyFailed = entry && entry->copyFailed;
    const BOOL stageGenerationOnly = entry && entry->generationOnly;
    const uint64_t stageCopyCommittedNs = entry ? entry->copyCommittedNs : 0;
    const uint64_t stageCopyCompleteNs = entry ? entry->copyCompleteNs : 0;
    stageCopyNs = stageCopyComplete
        ? stageCopyCompleteNs : stageCopyCommittedNs;

    if (ready) {
        R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
            sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
        terminal->sequence = sequence;
        terminal->sourcePresentOrdinal = sourcePresentOrdinal;
        terminal->disposition = 1;
        entry->sourcePresentOrdinal = sourcePresentOrdinal;
        entry->inFlight = YES;
        R133Action *action = &actions.items[actions.count++];
        action->snapshot = *entry;
        action->owner = owner;
        action->dispatch = YES;
        action->generationSlotHeld = generationSlotHeld;
        ++r132EstimatorReadyCount;
        if (sourcePresentOrdinal > r133LastResolvedOrdinal)
            r133LastResolvedOrdinal = sourcePresentOrdinal;
    } else if (pending) {
        if (r133PendingCount < kR133MaxPendingOrdinals) {
            R133PendingOrdinal *slot = &r133Pending[r133PendingCount++];
            slot->ordinal = sourcePresentOrdinal;
            slot->sequence = sequence;
            slot->resolved = NO;
            slot->captureOk = NO;
            slot->presentHookNs = presentNs;
            slot->captureCompleteNs = 0;
            if (r133PendingCount > r133PendingPeak)
                r133PendingPeak = r133PendingCount;
        } else {
            // The bound is a hard bound. It can only be reached if the caller
            // selected a grace larger than the array; fail closed as a gap.
            pending = NO;
            ++r132EstimatorNoCaptureCount;
            if (testSink) ++r132TestNoCaptureCount;
            R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
                sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
            terminal->sequence = sequence;
            terminal->sourcePresentOrdinal = sourcePresentOrdinal;
            terminal->disposition = 2;
            if (entry) r132ClearStageEntryLocked(entry);
            R133Action *action = &actions.items[actions.count++];
            action->gapReset = YES;
            action->owner = owner;
            if (sourcePresentOrdinal > r133LastResolvedOrdinal)
                r133LastResolvedOrdinal = sourcePresentOrdinal;
        }
    } else {
        committedCopyKnown = r133CaptureKnownForSequence(sequence);
        if (entry) r132ClearStageEntryLocked(entry);
        ++r132EstimatorNoCaptureCount;
        if (owner == R127SourceOwnerSuppressed && !committedCopyKnown)
            ++r132EstimatorFgMissingCaptureCount;
        if (testSink) ++r132TestNoCaptureCount;
        R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
            sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
        terminal->sequence = sequence;
        terminal->sourcePresentOrdinal = sourcePresentOrdinal;
        terminal->disposition = 2;
        R133Action *action = &actions.items[actions.count++];
        action->gapReset = YES;
        action->owner = owner;
        if (sourcePresentOrdinal > r133LastResolvedOrdinal)
            r133LastResolvedOrdinal = sourcePresentOrdinal;
    }

    stageFlags = r137PresentationStageFlags(
        stageEntryPresent, stageHasTexture, stageHasDevice,
        stageCopyCommitted, stageCopyComplete, stageCopyFailed, ready, pending,
        stageGenerationOnly, generationSlotHeld, testSink, committedCopyKnown,
        orderGap, owner);

    ++r132NextEstimatorOrdinal;
    if (sequence > r132NewestPublishedSequence)
        r132NewestPublishedSequence = sequence;
    if (orderGap) {
        R133Action *action = &actions.items[actions.count++];
        action->gapReset = YES;
    }
    pthread_mutex_unlock(&r132EstimatorLock);

    r137TracePoint(R137EventPresentationStage, sequence, sequence, sequence,
                   sourcePresentOrdinal, stageFlags, presentNs, stageCopyNs);

    if (!ready) {
        if (generationSlotHeld) r122ReleaseGenerationSlot();
        r133ExecuteActions(&actions, testSink);
        return;
    }
    r133ExecuteActions(&actions, testSink);
}

static BOOL r132HasTerminalForSequence(uint64_t sequence) {
    if (!sequence) return NO;
    pthread_mutex_lock(&r132EstimatorLock);
    const R132EstimatorTerminalEntry *entry = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    const BOOL terminal = entry->sequence == sequence && entry->disposition != 0;
    pthread_mutex_unlock(&r132EstimatorLock);
    return terminal;
}

static void r132NoteLateCapture(uint64_t sequence) {
    if (!sequence) return;
    pthread_mutex_lock(&r132EstimatorLock);
    const R132EstimatorTerminalEntry *entry = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (entry->sequence == sequence && entry->disposition != 0)
        ++r132EstimatorLateCaptureCount;
    pthread_mutex_unlock(&r132EstimatorLock);
}

// ---------------------------------------------------------------------------
// R133 capture scheduling and completion.
// ---------------------------------------------------------------------------
static void r133NoteTrackedTexture(uint64_t sequence, id<MTLTexture> texture) {
    if (!sequence || !texture) return;
    pthread_mutex_lock(&r133TrackedTextureLock);
    NSUInteger slot = kR133TrackedTextureCapacity;
    for (NSUInteger i = 0; i < kR133TrackedTextureCapacity; ++i) {
        if (r133TrackedTextures[i].sequence == sequence) { slot = i; break; }
    }
    if (slot == kR133TrackedTextureCapacity) {
        for (NSUInteger i = 0; i < kR133TrackedTextureCapacity; ++i) {
            if (r133TrackedTextures[i].sequence == 0) { slot = i; break; }
        }
    }
    if (slot == kR133TrackedTextureCapacity)
        slot = (NSUInteger)(sequence % kR133TrackedTextureCapacity);
    r133TrackedTextures[slot].sequence = sequence;
    r133TrackedTextures[slot].texture = texture;
    pthread_mutex_unlock(&r133TrackedTextureLock);
}

static id<MTLTexture> r133TrackedTextureForSequence(uint64_t sequence) {
    if (!sequence) return nil;
    id<MTLTexture> texture = nil;
    pthread_mutex_lock(&r133TrackedTextureLock);
    for (NSUInteger i = 0; i < kR133TrackedTextureCapacity; ++i) {
        if (r133TrackedTextures[i].sequence == sequence) {
            texture = r133TrackedTextures[i].texture;
            break;
        }
    }
    pthread_mutex_unlock(&r133TrackedTextureLock);
    return texture;
}

static BOOL r133CaptureEnabled(void) {
    if (!r123PumpEnabled()) return NO;
    static int cached = -1;
    if (cached < 0) {
        const char *inCommandBuffer = getenv("FGMETAL_R133_IN_CB_CAPTURE");
        const char *capture = getenv("FGMETAL_R108_CAPTURE");
        const BOOL explicitlyDisabled = inCommandBuffer && strcmp(inCommandBuffer, "0") == 0;
        const BOOL captureRequested = capture && strcmp(capture, "1") == 0;
        cached = (!explicitlyDisabled && captureRequested && r125CaptureEnabled()) ? 1 : 0;
    }
    return cached == 1;
}

// ---- R133 capture-availability record -------------------------------------
// Leaf lock discipline: r133CaptureRecordLock is never held while taking any
// other lock, so the present hook may consult it from inside the runtime lock.
static void r133NoteCaptureAvailability(uint64_t sequence, int committed,
                                        int complete, int failed, uint64_t ns) {
    if (!sequence) return;
    pthread_mutex_lock(&r133CaptureRecordLock);
    R133CaptureRecord *record = &r133CaptureRecords[
        sequence % kR133CaptureRecordCapacity];
    if (record->sequence != sequence) {
        memset(record, 0, sizeof(*record));
        record->sequence = sequence;
    }
    if (committed >= 0) {
        record->committed = committed ? YES : NO;
        if (committed) record->committedNs = ns;
    }
    if (complete >= 0) {
        record->complete = complete ? YES : NO;
        if (complete) record->completeNs = ns;
    }
    if (failed >= 0) record->failed = failed ? YES : NO;
    ++r133CaptureRecordWrites;
    pthread_mutex_unlock(&r133CaptureRecordLock);
}

static void r133ClearCaptureAvailability(uint64_t sequence) {
    if (!sequence) return;
    pthread_mutex_lock(&r133CaptureRecordLock);
    R133CaptureRecord *record = &r133CaptureRecords[
        sequence % kR133CaptureRecordCapacity];
    if (record->sequence == sequence) memset(record, 0, sizeof(*record));
    pthread_mutex_unlock(&r133CaptureRecordLock);
}

static NSDictionary *r133SourceRecordForCommandBuffer(
    id<MTLCommandBuffer> commandBuffer) {
    NSDictionary *found = nil;
    if (!commandBuffer) return nil;
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

// Stages a capture that is committed but not yet GPU-complete. The stage slot
// holds the private copy from this instant, so the present hook can bind it to
// an ordinal and the pump can reserve it. copyComplete stays NO until the
// producer command buffer's completion handler runs.
static BOOL r133StageCommittedCapture(uint64_t sequence, uint64_t drawableID,
                                      MTLPixelFormat sourceFormat,
                                      id<MTLTexture> texture, id<MTLDevice> device,
                                      NSString *condition, uint64_t captureEpoch,
                                      BOOL generationOnly, uintptr_t commandBuffer,
                                      uint64_t committedNs, int *outReason) {
    if (!sequence || !texture || !device) return NO;
    BOOL staged = NO;
    BOOL rejectedTerminal = NO;
    BOOL refusedCapacity = NO;
    pthread_mutex_lock(&r132EstimatorLock);
    R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    if (terminal->sequence == sequence && terminal->disposition != 0) {
        rejectedTerminal = YES;
        ++r132EstimatorLateCaptureCount;
    } else {
        R132EstimatorStageEntry *slot = r132FindStageLocked(sequence);
        if (slot && slot->captureCommandBuffer != commandBuffer)
            r132ClearStageEntryLocked(slot);
        slot = r132FindStageLocked(sequence);
        if (!slot) {
            slot = r132FindFreeStageLocked();
            if (!slot) {
                (void)r132ReclaimDeadStagesLocked();
                slot = r132FindFreeStageLocked();
            }
        }
        if (!slot) {
            ++r132EstimatorStageRefusalCount;
            refusedCapacity = YES;
        } else {
            slot->sequence = sequence;
            slot->sourcePresentOrdinal = 0;
            slot->captureEpoch = captureEpoch;
            slot->drawableID = drawableID;
            slot->sourceFormat = sourceFormat;
            slot->texture = texture;
            slot->device = device;
            slot->condition = condition ?: @"R133_IN_PRODUCER_COMMAND_BUFFER";
            slot->generationOnly = generationOnly;
            slot->occupied = YES;
            slot->inFlight = NO;
            slot->copyCommitted = YES;
            slot->copyComplete = NO;
            slot->copyFailed = NO;
            slot->captureCommandBuffer = commandBuffer;
            slot->copyCommittedNs = committedNs;
            slot->copyCompleteNs = 0;
            slot->presentHookNs = 0;
            const uint64_t occupied = r132OccupiedStageCountLocked();
            if (occupied > r132EstimatorStagePeak)
                r132EstimatorStagePeak = occupied;
            staged = YES;
        }
    }
    pthread_mutex_unlock(&r132EstimatorLock);
    if (outReason) {
        *outReason = rejectedTerminal ? 1 : (refusedCapacity ? 2 : 0);
    }
    if (rejectedTerminal) {
        // The estimator already finalized this ordinal; the copy is late and is
        // released, so no capture availability may be claimed for it. The log
        // line is emitted by the caller, which does not hold a hot lock here.
        r133ClearCaptureAvailability(sequence);
    } else {
        // Availability is recorded even when the estimator stage table refused
        // the slot: the copy exists, and the ownership decision must not be
        // starved by estimator reorder depth.
        r133NoteCaptureAvailability(sequence, 1, 0, 0, committedNs);
    }
    return staged;
}

// The FG presentation prerequisite. It is deliberately narrower than "an image
// exists": the private copy must be encoded in the producer command buffer and
// that command buffer must be committed. Completion is not required, because
// the producer command buffer's completion is independent of the title present.
// The fact is read from the per-SOURCE capture record, not from the estimator
// stage table, so estimator reorder pressure can never withdraw it.
static BOOL r133CaptureCommittedForSequence(uint64_t sequence) {
    if (!sequence) return NO;
    BOOL committed = NO;
    pthread_mutex_lock(&r133CaptureRecordLock);
    const R133CaptureRecord *record = &r133CaptureRecords[
        sequence % kR133CaptureRecordCapacity];
    if (record->sequence == sequence) committed = record->committed && !record->failed;
    pthread_mutex_unlock(&r133CaptureRecordLock);
    return committed;
}

// True when a committed capture for this sequence is known even if the
// estimator never got a stage slot for it. Used only for diagnostics.
static BOOL r133CaptureKnownForSequence(uint64_t sequence) {
    if (!sequence) return NO;
    BOOL known = NO;
    pthread_mutex_lock(&r133CaptureRecordLock);
    const R133CaptureRecord *record = &r133CaptureRecords[
        sequence % kR133CaptureRecordCapacity];
    if (record->sequence == sequence)
        known = (record->committed || record->complete) && !record->failed;
    else if (record->sequence)
        ++r133CaptureRecordMismatches;
    pthread_mutex_unlock(&r133CaptureRecordLock);
    return known;
}

static void r133CaptureDidComplete(uint64_t sequence, uintptr_t commandBuffer,
                                   BOOL success, uint64_t completionNs) {
    if (!sequence) return;
    // R134: capture-ready instant for the pair timeline. Recorded before any
    // policy runs so the interval is a measurement, not a consequence.
    if (success) r134NoteCaptureComplete(sequence, completionNs);
    r137TracePoint(R137EventCaptureComplete, sequence, 0, sequence, 0,
                   success ? 1u : 0u, completionNs, commandBuffer);
    R133ActionList actions = {0};
    BOOL testSink = NO;
    pthread_mutex_lock(&r132EstimatorLock);
    testSink = r132EstimatorTestSinkEnabled;
    const R132EstimatorTerminalEntry *terminal = &r132EstimatorTerminals[
        sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
    const BOOL alreadyFinalized = terminal->sequence == sequence &&
        terminal->disposition != 0;
    R132EstimatorStageEntry *entry = r132FindStageLocked(sequence);
    if (alreadyFinalized) {
        // R132's immutability rule is preserved: a capture that arrives after
        // its ordinal was finalized is rejected, releases its private copy and
        // never rewrites history.
        ++r132EstimatorLateCaptureCount;
        ++r133LateExpiredCaptures;
        if (r133LateExpiredCaptures <= 8) {
            logLine("R133_LATE_CAPTURE=REJECTED acquisitionId=%llu "
                    "ordinal=%llu disposition=%s action=release-private-copy "
                    "count=%llu", (unsigned long long)sequence,
                (unsigned long long)terminal->sourcePresentOrdinal,
                terminal->disposition == 1 ? "CAPTURE_READY" : "NO_CAPTURE",
                (unsigned long long)r133LateExpiredCaptures);
        }
        if (entry && !entry->inFlight) r132ClearStageEntryLocked(entry);
    } else if (entry && (entry->captureCommandBuffer == 0 ||
                         entry->captureCommandBuffer == commandBuffer)) {
        entry->copyComplete = YES;
        entry->copyFailed = !success;
        entry->copyCompleteNs = completionNs;
        if (entry->presentHookNs && completionNs > entry->presentHookNs) {
            const uint64_t delta = completionNs - entry->presentHookNs;
            if (r133ProbeSamples < kR133TimingSampleCapacity) {
                r133ProbePresentToCompleteNs[r133ProbeSamples] = delta;
                r133ProbePresentToCommitNs[r133ProbeSamples] =
                    entry->presentHookNs > entry->copyCommittedNs
                        ? entry->presentHookNs - entry->copyCommittedNs : 0;
            } else {
                ++r133ProbeOverflow;
            }
            ++r133ProbeSamples;
            if (r133ProbeMinNs == 0 || delta < r133ProbeMinNs) r133ProbeMinNs = delta;
            if (delta > r133ProbeMaxNs) r133ProbeMaxNs = delta;
        } else if (!entry->presentHookNs) {
            ++r133CopyCompletedBeforePresent;
        }
    }
    if (success) ++r133CaptureCompleteCount;
    else ++r133CaptureFailedCount;
    for (NSUInteger i = 0; i < r133PendingCount; ++i) {
        if (r133Pending[i].sequence == sequence && !r133Pending[i].resolved) {
            r133Pending[i].resolved = YES;
            r133Pending[i].captureOk = success;
            r133Pending[i].captureCompleteNs = completionNs;
        }
    }
    r133DrainPendingLocked(&actions, NO, 0);
    pthread_mutex_unlock(&r132EstimatorLock);
    // Completion updates the capture-availability record for every live
    // sequence, including one whose copy was never staged into the estimator
    // table. A late completion that was already finalized is a rejection: it
    // releases its copy and must not claim availability.
    if (alreadyFinalized)
        r133ClearCaptureAvailability(sequence);
    else
        r133NoteCaptureAvailability(sequence, -1, success ? 1 : 0,
                                    success ? 0 : 1, completionNs);
    r133ExecuteActions(&actions, testSink);
}

// Encodes the private SOURCE copy inside the title's own producer command
// buffer, after every title render pass and before commit. This is Option B:
// the same bounded copy, scheduled earlier, with no extra command buffer, no
// drawable retention and no title-thread wait.
static void r133CommitCaptureForCommandBuffer(id<MTLCommandBuffer> commandBuffer) {
    if (!r133CaptureEnabled() || !commandBuffer) return;
    if (!isSelectedProcess() || isInternalPresentationCall) return;
    NSDictionary *record = r133SourceRecordForCommandBuffer(commandBuffer);
    if (!record) return;
    const uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    if (!sequence) return;
    if (!r108SequenceSelectedForCapture(sequence)) return;
    const uint64_t captureEpoch =
        [record[@"r130CaptureEpoch"] unsignedLongLongValue];
    const uint64_t drawableID = [record[@"drawableID"] unsignedLongLongValue];
    if (!r133TrackedTextureForSequence(sequence)) {
        ++r133CapturePreflightFailures;
        logLine("R133_CAPTURE_COMMIT=SKIPPED acquisitionId=%llu "
                "reason=tracked-texture-unavailable producerCommandBuffer=%p",
            (unsigned long long)sequence, (__bridge void *)commandBuffer);
        return;
    }
    id<MTLTexture> source = r133TrackedTextureForSequence(sequence);
    id<MTLDevice> device = source.device;
    const NSUInteger width = source.width;
    const NSUInteger height = source.height;
    if (!device || source.framebufferOnly ||
        (source.usage & MTLTextureUsageShaderRead) == 0 ||
        width < 64 || height < 64 || width > 4096 || height > 2160 ||
        (source.pixelFormat != MTLPixelFormatBGRA8Unorm &&
         source.pixelFormat != MTLPixelFormatRGBA8Unorm)) {
        ++r133CapturePreflightFailures;
        logLine("R133_CAPTURE_COMMIT=SKIPPED acquisitionId=%llu "
                "reason=preflight texture=%p framebufferOnly=%d usage=%lu size=%lux%lu pixelFormat=%lu",
            (unsigned long long)sequence, (__bridge void *)source,
            source.framebufferOnly, (unsigned long)source.usage,
            (unsigned long)width, (unsigned long)height,
            (unsigned long)source.pixelFormat);
        return;
    }

    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:source.pixelFormat
        width:width height:height mipmapped:NO];
    descriptor.storageMode = MTLStorageModePrivate;
    descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageShaderWrite;
    id<MTLTexture> captureTexture = [device newTextureWithDescriptor:descriptor];
    if (!captureTexture) {
        ++r133CaptureAllocationFailures;
        logLine("R133_CAPTURE_COMMIT=FAILED acquisitionId=%llu reason=allocation "
                "size=%lux%lu pixelFormat=%lu",
            (unsigned long long)sequence, (unsigned long)width,
            (unsigned long)height, (unsigned long)source.pixelFormat);
        return;
    }
    id<MTLBlitCommandEncoder> blit = [commandBuffer blitCommandEncoder];
    if (!blit) {
        ++r133CapturePreflightFailures;
        logLine("R133_CAPTURE_COMMIT=FAILED acquisitionId=%llu reason=blit-encoder "
                "producerCommandBuffer=%p", (unsigned long long)sequence,
            (__bridge void *)commandBuffer);
        return;
    }
    [blit copyFromTexture:source sourceSlice:0 sourceLevel:0
            sourceOrigin:MTLOriginMake(0, 0, 0)
              sourceSize:MTLSizeMake(width, height, 1)
               toTexture:captureTexture destinationSlice:0 destinationLevel:0
        destinationOrigin:MTLOriginMake(0, 0, 0)];
    [blit endEncoding];
    // R135: the capture copy now owns a cross-queue edge. The pump's
    // presentation command buffer waits on this signal before sampling the
    // private copy, so ordering no longer depends on callback timing.
    r135SyncArmForSequence(sequence, device, commandBuffer);
    const uint64_t committedNs = r105NowNs();
    const BOOL generationOnly = r122OverlapEnabled() || r127RuntimeCanGenerate();
    NSString *condition = r108CurrentCaptureCondition();

    pthread_mutex_lock(&r127RuntimeLock);
    int stageReason = 0;
    const BOOL staged = r133StageCommittedCapture(sequence, drawableID,
        source.pixelFormat, captureTexture, device, condition, captureEpoch,
        generationOnly, (uintptr_t)(__bridge void *)commandBuffer, committedNs,
        &stageReason);
    if (staged && sequence >= r127CachedSourceSequence) {
        r127CachedSourceTexture = captureTexture;
        r127CachedSourceSequence = sequence;
        r130CachedSourceEpoch = captureEpoch;
        r124NoteCapture(sequence, captureTexture);
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    if (!staged) {
        r137TracePoint(R137EventSourceCaptureCommitted, sequence, 0, sequence,
                       0, (uint32_t)(0x100u | (stageReason & 0xff)),
                       committedNs, drawableID);
        // Logged after the runtime lock is released: this path runs on the
        // producer thread and the pump's dispatch path takes the same lock.
        if (stageReason == 1)
            logLine("R133_CAPTURE_COMMIT=REJECTED acquisitionId=%llu "
                    "reason=terminal-estimator-disposition action=release-private-copy",
                (unsigned long long)sequence);
        else
            logLine("R133_CAPTURE_COMMIT=REFUSED acquisitionId=%llu "
                    "reason=stage-capacity action=title-direct",
                (unsigned long long)sequence);
        return;
    }
    ++r133CaptureCommittedCount;
    r137TracePoint(R137EventSourceCaptureCommitted, sequence, 0, sequence,
                   0, 1u, committedNs, drawableID);
    logLine("R133_CAPTURE_COPY_COMMITTED acquisitionId=%llu drawableID=%llu "
            "producerCommandBuffer=%p position=after-title-render-before-commit "
            "source=%p privateCopy=%p size=%lux%lu pixelFormat=%lu epoch=%llu "
            "prerequisite=CAPTURE_COPY_COMMITTED_IN_PRODUCER_CB",
        (unsigned long long)sequence, (unsigned long long)drawableID,
        (__bridge void *)commandBuffer, (__bridge void *)source,
        (__bridge void *)captureTexture, (unsigned long)width,
        (unsigned long)height, (unsigned long)source.pixelFormat,
        (unsigned long long)captureEpoch);
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        const BOOL ok = completed.status == MTLCommandBufferStatusCompleted &&
            completed.error == nil;
        r133CaptureDidComplete(sequence,
            (uintptr_t)(__bridge void *)completed, ok, r105NowNs());
    }];
}

// ---------------------------------------------------------------------------
// R135 -- cross-queue capture synchronization.
//
// The producer's capture copy is encoded in the title's own command buffer;
// the pump samples that private copy from r123Queue. Ordering was previously
// an emergent property of completion-callback timing: measured over 3430
// samples it never violated, but the minimum margin was 3-9 us, which is a
// coincidence of two nearly simultaneous events, not a design margin.
//
// One MTLSharedEvent ring closes the edge by construction. Slot = sequence %
// ring; the capture copy for `sequence` encodes a signal on its slot after the
// blit, and the pump's presentation command buffer encodes a wait on the same
// slot before it samples the private copy. Per-slot generations (sequence /
// ring + 1) keep reuse monotone: a slot is re-armed only kR135SyncEventRing
// captures later (~2 s at the target rate), by which time the previous copy's
// signal has long retired. No CPU blocking, no display wait, no drawable
// retention; the only metadata is the event ring itself.
// ---------------------------------------------------------------------------

#define kR135SyncEventRing 64
static id<MTLSharedEvent> r135SyncEvents[kR135SyncEventRing];
static pthread_mutex_t r135SyncLock = PTHREAD_MUTEX_INITIALIZER;
static uint64_t r135SyncSignalsEncoded;
static uint64_t r135SyncWaitsEncoded;
static uint64_t r135SyncWaitSkips;

// The edge is the release posture and is enabled by default. Setting
// FGMETAL_R135_CAPTURE_SYNC=0 arms nothing and encodes no waits, which is how
// the pipeline-only cadence is measured for comparison.
static BOOL r135SyncEnabled(void) {
    static int enabled = -1;
    if (enabled < 0) {
        const char *value = getenv("FGMETAL_R135_CAPTURE_SYNC");
        enabled = (!value || !*value || strcmp(value, "0") != 0) ? 1 : 0;
    }
    return enabled ? YES : NO;
}

// Producer side: signal this capture's slot after its copy. Called on the
// title's producer thread while the blit is already encoded.
static void r135SyncArmForSequence(uint64_t sequence,
                                   id<MTLDevice> device,
                                   id<MTLCommandBuffer> commandBuffer) {
    if (!sequence || !device || !commandBuffer) return;
    if (!r135SyncEnabled()) return;
    const NSUInteger slot = (NSUInteger)(sequence % kR135SyncEventRing);
    const uint64_t generation = sequence / kR135SyncEventRing + 1;
    pthread_mutex_lock(&r135SyncLock);
    id<MTLSharedEvent> event = r135SyncEvents[slot];
    if (!event) {
        event = [device newSharedEvent];
        if (!event) {
            pthread_mutex_unlock(&r135SyncLock);
            ++r135SyncWaitSkips;
            return;
        }
        r135SyncEvents[slot] = event;
    }
    pthread_mutex_unlock(&r135SyncLock);
    [commandBuffer encodeSignalEvent:event value:generation];
    ++r135SyncSignalsEncoded;
    if (r135SyncSignalsEncoded <= 4)
        logLine("R135_CAPTURE_SYNC=SIGNAL sequence=%llu slot=%lu generation=%llu "
                "commandBuffer=%p position=after-capture-blit",
            (unsigned long long)sequence, (unsigned long)slot,
            (unsigned long long)generation, (__bridge void *)commandBuffer);
}

// Pump side: wait for this SOURCE's capture copy before sampling it. Called on
// the pump thread before the presentation encoder is created.
static void r135SyncEncodeWaitForSequence(uint64_t sequence,
                                          id<MTLCommandBuffer> commandBuffer) {
    if (!sequence || !commandBuffer) return;
    if (!r135SyncEnabled()) return;
    const NSUInteger slot = (NSUInteger)(sequence % kR135SyncEventRing);
    const uint64_t generation = sequence / kR135SyncEventRing + 1;
    pthread_mutex_lock(&r135SyncLock);
    id<MTLSharedEvent> event = r135SyncEvents[slot];
    pthread_mutex_unlock(&r135SyncLock);
    if (!event) {
        ++r135SyncWaitSkips;
        r137TracePoint(R137EventSharedEventWait, sequence, 0, sequence,
                       0, 1u, generation, slot);
        return;
    }
    [commandBuffer encodeWaitForEvent:event value:generation];
    ++r135SyncWaitsEncoded;
    r137TracePoint(R137EventSharedEventWait, sequence, 0, sequence,
                   0, 0u, generation,
                   (uint64_t)(uintptr_t)(__bridge void *)event);
    if (r135SyncWaitsEncoded <= 4)
        logLine("R135_CAPTURE_SYNC=WAIT sequence=%llu slot=%lu generation=%llu "
                "commandBuffer=%p position=before-presentation-encoder",
            (unsigned long long)sequence, (unsigned long)slot,
            (unsigned long long)generation, (__bridge void *)commandBuffer);
}

static int r133CompareU64(const void *left, const void *right) {
    const uint64_t a = *(const uint64_t *)left;
    const uint64_t b = *(const uint64_t *)right;
    return a < b ? -1 : (a > b ? 1 : 0);
}

// R134 intervals are signed: "ready minus slot" is legitimately negative when
// the midpoint arrived early, and a negative value sorted as unsigned would
// land at the end of the distribution and corrupt every quantile.
static int r134CompareS64(const void *left, const void *right) {
    const int64_t a = *(const int64_t *)left;
    const int64_t b = *(const int64_t *)right;
    return a < b ? -1 : (a > b ? 1 : 0);
}

// Bounded Phase-0-equivalent probe. Every sample is a SOURCE frame whose
// present hook and capture completion both landed on the same clock, so the
// distribution answers the one question the architecture depends on: how long
// after the title present hook does the private copy complete?
static void r133EmitProbeSummary(const char *reason) {
    uint64_t count = 0, minNs = 0, maxNs = 0, overflow = 0;
    uint64_t committedBefore = 0, committedAfter = 0, completedBefore = 0;
    uint64_t *presentToComplete = NULL;
    uint64_t *presentToCommit = NULL;
    pthread_mutex_lock(&r132EstimatorLock);
    count = r133ProbeSamples;
    if (count > kR133TimingSampleCapacity) count = kR133TimingSampleCapacity;
    minNs = r133ProbeMinNs;
    maxNs = r133ProbeMaxNs;
    overflow = r133ProbeOverflow;
    committedBefore = r133CopyCommittedBeforePresent;
    committedAfter = r133CopyCommittedAfterPresent;
    completedBefore = r133CopyCompletedBeforePresent;
    if (count > 0) {
        const size_t bytes = (size_t)count * sizeof(uint64_t);
        presentToComplete = malloc(bytes);
        presentToCommit = malloc(bytes);
        if (presentToComplete)
            memcpy(presentToComplete, r133ProbePresentToCompleteNs, bytes);
        if (presentToCommit)
            memcpy(presentToCommit, r133ProbePresentToCommitNs, bytes);
    }
    pthread_mutex_unlock(&r132EstimatorLock);
    if (count == 0) {
        logLine("R133_PRESENT_TO_CAPTURE reason=%s samples=0 "
                "committedBeforePresent=%llu committedAfterPresent=%llu "
                "completedBeforePresent=%llu",
            reason, (unsigned long long)committedBefore,
            (unsigned long long)committedAfter,
            (unsigned long long)completedBefore);
        return;
    }
    if (!presentToComplete || !presentToCommit) {
        free(presentToComplete);
        free(presentToCommit);
        logLine("R133_PRESENT_TO_CAPTURE reason=%s samples=%llu "
                "quantiles=UNAVAILABLE reason=allocation",
            reason, (unsigned long long)count);
        return;
    }
    qsort(presentToComplete, count, sizeof(uint64_t), r133CompareU64);
    qsort(presentToCommit, count, sizeof(uint64_t), r133CompareU64);
    const uint64_t median = presentToComplete[count / 2];
    const uint64_t p90 = presentToComplete[(count * 9) / 10 < count
        ? (count * 9) / 10 : count - 1];
    const uint64_t p99 = presentToComplete[(count * 99) / 100 < count
        ? (count * 99) / 100 : count - 1];
    const uint64_t commitMedian = presentToCommit[count / 2];
    const uint64_t commitP90 = presentToCommit[(count * 9) / 10 < count
        ? (count * 9) / 10 : count - 1];
    const uint64_t commitP99 = presentToCommit[(count * 99) / 100 < count
        ? (count * 99) / 100 : count - 1];
    logLine("R133_PRESENT_TO_CAPTURE reason=%s samples=%llu medianNs=%llu "
            "p90Ns=%llu p99Ns=%llu minNs=%llu maxNs=%llu overflow=%llu "
            "committedBeforePresent=%llu committedAfterPresent=%llu "
            "completedBeforePresent=%llu clock=CLOCK_MONOTONIC",
        reason, (unsigned long long)count,
        (unsigned long long)median, (unsigned long long)p90,
        (unsigned long long)p99, (unsigned long long)minNs,
        (unsigned long long)maxNs, (unsigned long long)overflow,
        (unsigned long long)committedBefore,
        (unsigned long long)committedAfter,
        (unsigned long long)completedBefore);
    logLine("R133_COMMIT_TO_PRESENT reason=%s samples=%llu medianNs=%llu "
            "p90Ns=%llu p99Ns=%llu",
        reason, (unsigned long long)count,
        (unsigned long long)commitMedian, (unsigned long long)commitP90,
        (unsigned long long)commitP99);
    free(presentToComplete);
    free(presentToCommit);
}

// ---------------------------------------------------------------------------
// R134 -- pair-level critical-path timeline.
//
// One record per SOURCE acquisition, keyed by AcquisitionId. Every instant in
// this record is CLOCK_MONOTONIC (r105NowNs), so any two of them may be
// subtracted. The estimator command buffer's GPUStartTime/GPUEndTime live on
// the CACurrentMediaTime base and are therefore kept in their own two fields:
// they are only ever subtracted from each other, never from a monotonic
// instant. That is the whole point of the split -- an interval is either
// monotonic-to-monotonic or media-to-media, never mixed.
//
// Nothing here changes scheduling. The record exists so that "the midpoint is
// late" can be replaced by a measured decomposition of *where* the pair spends
// its budget, and so that a presented midpoint can be compared directly with a
// missed one on the same intervals.
//
// The retained sample is bounded: the first kR134RetainedPairs terminal pairs
// after a kR134WarmupPairs warm-up are kept, so quantiles can be recomputed
// from the log rather than trusted, and running counters cover the whole run.
//
// R135: the window starts at each runtime ON transition, so the retained
// sample describes steady state instead of being dominated by the pairs that
// happen to fill the pipeline first. A terminal record keeps receiving late
// timestamps (estimator wake, MetalFX end, resolve) after its slot
// disposition; every note re-copies a retained record in place, so the
// snapshot at emission time is the latest complete state. The window itself
// never grows past kR134RetainedPairs entries.
// ---------------------------------------------------------------------------

#define kR134PairCapacity 512
#define kR134RetainedPairs 384
#define kR134WarmupPairs 8
#define kR134PairLogLimit 24

typedef struct {
    uint64_t sequence;
    uint64_t pairANs;
    uint64_t hookNs;              // SOURCE present-hook / reservation instant
    uint64_t presentNs;           // SOURCE actually presented by the pump
    uint64_t slotAfterSourceNs;   // nextSlotNs after SOURCE: the midpoint slot
    uint64_t copyCompleteNs;      // producer command buffer completion
    uint64_t genStartNs;          // generation entry for this acquisition
    uint64_t estSubmitNs;         // immediately before estimator commit
    uint64_t estWakeNs;           // immediately after estimator completion
    uint64_t mfxStartNs;          // immediately before MetalFX transition
    uint64_t mfxEndNs;            // immediately after it returns
    uint64_t resolveNs;           // placeholder filled with a ready image
    uint64_t presentMidNs;        // INTERNAL actually presented
    uint64_t missedSlotNs;        // slot at which the midpoint was dropped
    uint64_t pumpUseNs;           // pump's first sample of the private copy
    uint64_t gpuStartNs;          // media domain, estimator command buffer
    uint64_t gpuEndNs;            // media domain
    uint8_t outcome;              // 0 pending, 1 presented, 2 missed
} R134PairRecord;

static R134PairRecord r134Ring[kR134PairCapacity];
static pthread_mutex_t r134Lock = PTHREAD_MUTEX_INITIALIZER;
static R134PairRecord r134Retained[kR134RetainedPairs];
static uint64_t r134RetainedCount;
static uint64_t r134PairsSeen;
static uint64_t r134PairsPresented;
static uint64_t r134PairsMissed;
static uint64_t r134PairLogs;
static uint64_t r134PumpUseSamples;
static uint64_t r134PumpUseViolations;
static uint64_t r134PumpUseMinMarginNs;
static uint64_t r134WarmupExcluded;
static uint64_t r134WindowStarts;

static BOOL r134Enabled(void) {
    static int enabled = -1;
    if (enabled < 0) {
        const char *value = getenv("FGMETAL_R134_TIMELINE");
        enabled = (!value || !*value || strcmp(value, "0") != 0) ? 1 : 0;
    }
    return enabled ? YES : NO;
}

// Caller holds r134Lock.
static R134PairRecord *r134RecordLocked(uint64_t sequence) {
    R134PairRecord *record = &r134Ring[sequence % kR134PairCapacity];
    if (record->sequence != sequence) {
        memset(record, 0, sizeof(*record));
        record->sequence = sequence;
    }
    return record;
}

// Caller holds r134Lock. Copies a terminal record into the bounded retained
// sample. A pair is retained once, at the moment its midpoint reaches a
// terminal disposition, so a late duplicate cannot rewrite the sample. Pairs
// inside the warm-up band are excluded and counted, never silently dropped.
static void r134RetainLocked(const R134PairRecord *record) {
    if (r134PairsSeen < kR134WarmupPairs) {
        ++r134WarmupExcluded;
        return;
    }
    if (r134RetainedCount >= kR134RetainedPairs) return;
    r134Retained[r134RetainedCount++] = *record;
}

// Caller holds r134Lock. A record retained at its slot disposition keeps
// receiving late timestamps afterwards; re-copy it in place so the emission
// snapshot is the latest complete state. Only entries already inside the fixed
// window are touched and the window never grows. If the ring slot was recycled
// (kR134PairCapacity later acquisitions), the retained copy stands unchanged.
static void r134RefreshRetainedLocked(uint64_t sequence) {
    if (!sequence || r134RetainedCount == 0) return;
    const R134PairRecord *record = &r134Ring[sequence % kR134PairCapacity];
    if (record->sequence != sequence) return;
    for (uint64_t i = 0; i < r134RetainedCount; ++i) {
        if (r134Retained[i].sequence == sequence) {
            r134Retained[i] = *record;
            return;
        }
    }
}

// R136: read-only peek at a pair record without creating one. Unlike
// r134RecordLocked this never initializes a ring slot, so looking up a
// sequence that never terminalized leaves the ring untouched.
static void r134PeekPair(uint64_t sequence, R134PairRecord *out) {
    if (out) memset(out, 0, sizeof(*out));
    if (!sequence || !out) return;
    pthread_mutex_lock(&r134Lock);
    const R134PairRecord *record = &r134Ring[sequence % kR134PairCapacity];
    if (record->sequence == sequence) *out = *record;
    pthread_mutex_unlock(&r134Lock);
}

// R135: start a fresh steady-state sample window. Called once per runtime ON
// transition so the retained sample describes steady-state operation instead
// of the first pipeline-fill pairs. r134Lock is a leaf lock: nothing under it
// ever takes r127RuntimeLock, so this is safe to call from the ON transition
// while the runtime lock is held.
static void r134ResetRetentionForRuntimeOn(void) {
    pthread_mutex_lock(&r134Lock);
    r134RetainedCount = 0;
    r134PairsSeen = 0;
    r134PairsPresented = 0;
    r134PairsMissed = 0;
    r134PairLogs = 0;
    r134WarmupExcluded = 0;
    ++r134WindowStarts;
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteSourcePresented(uint64_t sequence, uint64_t hookNs,
                                    uint64_t presentNs, uint64_t slotAfterNs) {
    if (!sequence || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(sequence);
    if (!record->hookNs || hookNs < record->hookNs) record->hookNs = hookNs;
    record->presentNs = presentNs;
    record->slotAfterSourceNs = slotAfterNs;
    r134RefreshRetainedLocked(sequence);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteCaptureComplete(uint64_t sequence, uint64_t ns) {
    if (!sequence || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    r134RecordLocked(sequence)->copyCompleteNs = ns;
    r134RefreshRetainedLocked(sequence);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NotePumpUse(uint64_t sequence, uint64_t ns) {
    if (!sequence || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(sequence);
    if (!record->pumpUseNs) {
        record->pumpUseNs = ns;
        // Cross-queue safety margin: the pump must never sample the private
        // copy before the producer command buffer that wrote it completed.
        if (record->copyCompleteNs) {
            if (ns >= record->copyCompleteNs) {
                const uint64_t margin = ns - record->copyCompleteNs;
                if (!r134PumpUseMinMarginNs || margin < r134PumpUseMinMarginNs)
                    r134PumpUseMinMarginNs = margin;
                ++r134PumpUseSamples;
            } else {
                ++r134PumpUseViolations;
            }
        }
        r134RefreshRetainedLocked(sequence);
    }
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteGenerationStart(uint64_t pairA, uint64_t pairB,
                                    uint64_t ns) {
    if (!pairB || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->pairANs = pairA;
    record->genStartNs = ns;
    if (!record->pairANs && pairA) record->pairANs = pairA;
    r134RefreshRetainedLocked(pairB);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteEstimatorSubmit(uint64_t pairB, uint64_t ns) {
    if (!pairB || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    r134RecordLocked(pairB)->estSubmitNs = ns;
    r134RefreshRetainedLocked(pairB);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteEstimatorWake(uint64_t pairB, uint64_t ns,
                                  uint64_t gpuStartNs, uint64_t gpuEndNs) {
    if (!pairB || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->estWakeNs = ns;
    record->gpuStartNs = gpuStartNs;
    record->gpuEndNs = gpuEndNs;
    r134RefreshRetainedLocked(pairB);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteMetalFX(uint64_t pairB, uint64_t startNs, uint64_t endNs) {
    if (!pairB || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->mfxStartNs = startNs;
    record->mfxEndNs = endNs;
    r134RefreshRetainedLocked(pairB);
    pthread_mutex_unlock(&r134Lock);
}

static void r134NoteResolved(uint64_t pairA, uint64_t pairB, uint64_t ns) {
    if (!pairB || !r134Enabled()) return;
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->pairANs = pairA;
    record->resolveNs = ns;
    r134RefreshRetainedLocked(pairB);
    pthread_mutex_unlock(&r134Lock);
}

// Signed difference helper: returns 0 and clears `ok` when either endpoint is
// absent, so a missing timestamp can never masquerade as a real interval.
static int64_t r134Delta(uint64_t from, uint64_t to, BOOL *ok) {
    if (!from || !to || !ok) { if (ok) *ok = NO; return 0; }
    *ok = YES;
    return (int64_t)to - (int64_t)from;
}

static void r134LogPair(const R134PairRecord *record, const char *outcome) {
    if (!record) return;
    BOOL have = NO;
    const int64_t hookToCapture = r134Delta(record->hookNs, record->copyCompleteNs, &have);
    const int64_t captureToGen = r134Delta(record->copyCompleteNs, record->genStartNs, &have);
    const int64_t genToSubmit = r134Delta(record->genStartNs, record->estSubmitNs, &have);
    const int64_t submitToWake = r134Delta(record->estSubmitNs, record->estWakeNs, &have);
    const int64_t wakeToMfx = r134Delta(record->estWakeNs, record->mfxStartNs, &have);
    const int64_t mfxSpan = r134Delta(record->mfxStartNs, record->mfxEndNs, &have);
    const int64_t captureToReady = r134Delta(record->copyCompleteNs, record->mfxEndNs, &have);
    const int64_t slotToReady = r134Delta(record->slotAfterSourceNs, record->mfxEndNs, &have);
    const int64_t captureToSlot = r134Delta(record->copyCompleteNs, record->slotAfterSourceNs, &have);
    const int64_t pumpMargin = r134Delta(record->copyCompleteNs, record->pumpUseNs, &have);
    logLine("R134_PAIR pair=%llu->%llu outcome=%s "
            "hookToCaptureNs=%lld captureToGenerationNs=%lld generationToSubmitNs=%lld "
            "submitToWakeNs=%lld wakeToMetalFXNs=%lld metalFXNs=%lld "
            "captureToReadyNs=%lld readyMinusSlotNs=%lld captureToSlotNs=%lld "
            "pumpUseMarginNs=%lld estimatorGpuNs=%lld clock=MONOTONIC",
        (unsigned long long)record->pairANs, (unsigned long long)record->sequence,
        outcome,
        (long long)hookToCapture, (long long)captureToGen,
        (long long)genToSubmit, (long long)submitToWake,
        (long long)wakeToMfx, (long long)mfxSpan,
        (long long)captureToReady, (long long)slotToReady,
        (long long)captureToSlot, (long long)pumpMargin,
        (long long)(record->gpuEndNs > record->gpuStartNs
                    ? record->gpuEndNs - record->gpuStartNs : 0));
}

static void r134NoteMidpointPresented(uint64_t pairA, uint64_t pairB,
                                      uint64_t ns) {
    if (!pairB || !r134Enabled()) return;
    R134PairRecord snapshot;
    BOOL emit = NO;
    memset(&snapshot, 0, sizeof(snapshot));
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->pairANs = pairA;
    record->presentMidNs = ns;
    if (record->outcome == 0) {
        record->outcome = 1;
        ++r134PairsSeen;
        ++r134PairsPresented;
        r134RetainLocked(record);
        if (r134PairLogs < kR134PairLogLimit) {
            ++r134PairLogs;
            snapshot = *record;
            emit = YES;
        }
    }
    pthread_mutex_unlock(&r134Lock);
    if (emit) r134LogPair(&snapshot, "PRESENTED");
}

static void r134NoteMidpointMissed(uint64_t pairA, uint64_t pairB,
                                   uint64_t slotNs) {
    if (!pairB || !r134Enabled()) return;
    R134PairRecord snapshot;
    BOOL emit = NO;
    memset(&snapshot, 0, sizeof(snapshot));
    pthread_mutex_lock(&r134Lock);
    R134PairRecord *record = r134RecordLocked(pairB);
    record->pairANs = pairA;
    record->missedSlotNs = slotNs;
    if (record->outcome == 0) {
        record->outcome = 2;
        ++r134PairsSeen;
        ++r134PairsMissed;
        r134RetainLocked(record);
        if (r134PairLogs < kR134PairLogLimit) {
            ++r134PairLogs;
            snapshot = *record;
            emit = YES;
        }
    }
    pthread_mutex_unlock(&r134Lock);
    if (emit) r134LogPair(&snapshot, "MISSED");
}

static void r134EmitInterval(const char *name, const char *klass,
                             const int64_t *values, NSUInteger count) {
    if (!count) {
        logLine("R134_CRITICAL_PATH interval=%s class=%s samples=0", name, klass);
        return;
    }
    int64_t *sorted = malloc(count * sizeof(int64_t));
    if (!sorted) return;
    memcpy(sorted, values, count * sizeof(int64_t));
    qsort(sorted, count, sizeof(int64_t), r134CompareS64);
    const NSUInteger medianIndex = count / 2;
    const NSUInteger p90Index = (count * 9) / 10 < count ? (count * 9) / 10 : count - 1;
    const NSUInteger p99Index = (count * 99) / 100 < count ? (count * 99) / 100 : count - 1;
    logLine("R134_CRITICAL_PATH interval=%s class=%s samples=%lu minNs=%lld "
            "medianNs=%lld p90Ns=%lld p99Ns=%lld maxNs=%lld clock=CLOCK_MONOTONIC",
        name, klass, (unsigned long)count,
        (long long)sorted[0], (long long)sorted[medianIndex],
        (long long)sorted[p90Index], (long long)sorted[p99Index],
        (long long)sorted[count - 1]);
    free(sorted);
}

// Emits the measured decomposition. Every interval is monotonic-to-monotonic;
// the estimator GPU window is reported separately, media-to-media.
static void r134EmitTimeline(const char *reason) {
    if (!r134Enabled()) return;
    R134PairRecord *sample = calloc(kR134RetainedPairs, sizeof(R134PairRecord));
    if (!sample) return;
    NSUInteger count = 0;
    uint64_t seen = 0, presented = 0, missed = 0;
    uint64_t pumpUseSamples = 0, pumpUseViolations = 0, pumpUseMinMargin = 0;
    uint64_t warmupExcluded = 0, windowStarts = 0;
    pthread_mutex_lock(&r134Lock);
    count = (NSUInteger)r134RetainedCount;
    if (count > kR134RetainedPairs) count = kR134RetainedPairs;
    memcpy(sample, r134Retained, count * sizeof(R134PairRecord));
    seen = r134PairsSeen;
    presented = r134PairsPresented;
    missed = r134PairsMissed;
    pumpUseSamples = r134PumpUseSamples;
    pumpUseViolations = r134PumpUseViolations;
    pumpUseMinMargin = r134PumpUseMinMarginNs;
    warmupExcluded = r134WarmupExcluded;
    windowStarts = r134WindowStarts;
    pthread_mutex_unlock(&r134Lock);

    logLine("R134_TIMELINE reason=%s retained=%lu pairsSeen=%llu presented=%llu "
            "missed=%llu warmupExcluded=%llu windowStarts=%llu "
            "pumpUseSamples=%llu pumpUseViolations=%llu "
            "pumpUseMinMarginNs=%llu syncSignals=%llu syncWaits=%llu "
            "syncWaitSkips=%llu",
        reason, (unsigned long)count, (unsigned long long)seen,
        (unsigned long long)presented, (unsigned long long)missed,
        (unsigned long long)warmupExcluded,
        (unsigned long long)windowStarts,
        (unsigned long long)pumpUseSamples,
        (unsigned long long)pumpUseViolations,
        (unsigned long long)pumpUseMinMargin,
        (unsigned long long)r135SyncSignalsEncoded,
        (unsigned long long)r135SyncWaitsEncoded,
        (unsigned long long)r135SyncWaitSkips);

    if (!count) { free(sample); return; }

    // Stack budget: six interval families x two outcome classes, each needing
    // at most `count` int64 slots. Allocated once and reused.
    int64_t *scratch = malloc(count * sizeof(int64_t));
    if (!scratch) { free(sample); return; }
    static const char *kClassNames[2] = {"presented", "missed"};
    for (int cls = 0; cls < 2; ++cls) {
        const uint8_t wanted = cls == 0 ? 1 : 2;
        NSUInteger n = 0;

#define R134_COLLECT(expr)                                                     \
        do {                                                                   \
            n = 0;                                                             \
            for (NSUInteger i = 0; i < count; ++i) {                           \
                if (sample[i].outcome != wanted) continue;                     \
                BOOL ok = NO;                                                  \
                const int64_t value = (expr);                                  \
                if (ok) scratch[n++] = value;                                  \
            }                                                                  \
        } while (0)

        R134_COLLECT(r134Delta(sample[i].hookNs, sample[i].copyCompleteNs, &ok));
        r134EmitInterval("hookToCaptureComplete", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].copyCompleteNs, sample[i].genStartNs, &ok));
        r134EmitInterval("captureCompleteToGenerationStart", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].genStartNs, sample[i].estSubmitNs, &ok));
        r134EmitInterval("generationStartToEstimatorSubmit", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].estSubmitNs, sample[i].estWakeNs, &ok));
        r134EmitInterval("estimatorSubmitToWake", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].estWakeNs, sample[i].mfxStartNs, &ok));
        r134EmitInterval("estimatorWakeToMetalFXStart", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].mfxStartNs, sample[i].mfxEndNs, &ok));
        r134EmitInterval("metalFXStartToEnd", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].copyCompleteNs, sample[i].mfxEndNs, &ok));
        r134EmitInterval("captureCompleteToInternalReady", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].slotAfterSourceNs, sample[i].mfxEndNs, &ok));
        r134EmitInterval("intendedSlotToInternalReady", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].hookNs, sample[i].slotAfterSourceNs, &ok));
        r134EmitInterval("hookToIntendedSlot", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].copyCompleteNs, sample[i].slotAfterSourceNs, &ok));
        r134EmitInterval("captureCompleteToIntendedSlot", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].estSubmitNs, sample[i].mfxEndNs, &ok));
        r134EmitInterval("estimatorSubmitToInternalReady", kClassNames[cls], scratch, n);
        R134_COLLECT(r134Delta(sample[i].pumpUseNs, sample[i].copyCompleteNs, &ok));
        r134EmitInterval("pumpUseAfterCaptureComplete", kClassNames[cls], scratch, n);
        // R135: how long a ready INTERNAL waited between resolve and its WSI
        // dispatch. Presented class only; a missed pair has no dispatch.
        R134_COLLECT(r134Delta(sample[i].resolveNs, sample[i].presentMidNs, &ok));
        r134EmitInterval("internalReadyToDispatch", kClassNames[cls], scratch, n);
#undef R134_COLLECT
    }
    free(scratch);

    // Estimator GPU window, media-to-media only.
    int64_t gpu[2][kR134RetainedPairs];
    NSUInteger gpuCount[2] = {0, 0};
    for (NSUInteger i = 0; i < count; ++i) {
        if (!sample[i].gpuStartNs || sample[i].gpuEndNs <= sample[i].gpuStartNs)
            continue;
        const int cls = sample[i].outcome == 1 ? 0 : 1;
        if (gpuCount[cls] >= kR134RetainedPairs) continue;
        gpu[cls][gpuCount[cls]++] = (int64_t)(sample[i].gpuEndNs - sample[i].gpuStartNs);
    }
    for (int cls = 0; cls < 2; ++cls)
        r134EmitInterval("estimatorGpuExec", kClassNames[cls], gpu[cls], gpuCount[cls]);
    free(sample);
}

// ---------------------------------------------------------------------------
// R136 -- miss-boundary classification and bounded visual evidence.
//
// The hardened R135 pipeline safely drops the ~5-10 % of midpoints that miss
// their presentation slot in volatile windows, and the user reports visible
// glitches at those boundaries. This section characterizes the boundary
// before any fix is chosen:
//
//   * every deadline drop is classified (always on, one bounded log line):
//     class A = generated but not ready/late at the slot; class B = never
//     generated (capture incomplete / generation never entered); flags for a
//     source-period hitch (C) and a recovery after a previous miss (D); host
//     GPU failure windows (E) are excluded at analysis by the existing
//     SubmissionsIgnored rule and each event carries recentCopyAbort;
//   * for the first N qualifying events per process the frames around the
//     hole are preserved losslessly (env-gated): SOURCE A (claimed from a
//     small shared-copy ring), the SOURCE presented at the missed slot (B,
//     further SOURCEs for consecutive misses), a generated midpoint that
//     arrived after its drop (lateM), the first recovery midpoint, and the
//     SOURCE that closes the window (C), plus a JSON sidecar with the full
//     temporal record;
//   * a per-window R136_MISS_SUMMARY line rides on both pump summaries.
//
// No scheduling decision reads any of this. Capture blits run on a dedicated
// command queue; the present path gains one encoder call. No CAMetalDrawable
// is ever retained: payloads are only ever blit *sources* inside a command
// buffer, and every claimed image is a copy in shared memory.
//
// Lock order (acyclic): r127RuntimeLock -> r134Lock / r136Lock, taken one at
// a time and always released before the next. Nothing under r136Lock ever
// takes another observer lock.
// ---------------------------------------------------------------------------

#define kR136EventTimeoutNs (4ULL * 1000000000ULL)
#define kR136MaxImagesPerEvent 8
#define kR136MaxImagesPerProcess 200
#define kR136HalfPeriodNsDefault 16666667ULL

// R137 bounded starvation trace. A small rolling ring retains the transitions
// immediately before a long no-INTERNAL interval. The first eight qualifying
// intervals copy that prelude and keep recording until the recovery INTERNAL;
// no per-frame log is emitted outside those bounded windows.
#define kR137TraceRingCapacity 512
#define kR137TracePerOutageCapacity 1024
#define kR137DetailedFgRoutineLimit 6
#define kR137DetailedFgSevereLimit 2
#define kR137DetailedFgOutageLimit \
    (kR137DetailedFgRoutineLimit + kR137DetailedFgSevereLimit)
#define kR137DetailedTitleHitchLimit 4
#define kR137DetailedOutageMinDurationNs 100000000ULL
#define kR137DetailedDirectMinAgeNs 50000000ULL
#define kR137DetailedCommittedAgeNs 100000000ULL
#define kR137DurationCapacity 65536
#define kR137TitleDirectDetailLimit 64
#define kR137ThreadSampleIntervalNs 250000000ULL

typedef struct {
    R137TraceEvent event;
    uint64_t atNs;
    uint64_t sequence;
    uint64_t pairA;
    uint64_t pairB;
    uint64_t sourceOrdinal;
    uint64_t runtimeEpoch;
    uint64_t value0;
    uint64_t value1;
    uint64_t latestGenerationNs;
    uint64_t latestEstimatorSubmitNs;
    uint64_t latestEstimatorWakeNs;
    uint64_t latestMetalFxStartNs;
    uint64_t latestMetalFxEndNs;
    uint32_t runtimeMode;
    uint32_t flags;
    uint32_t workerState;
    R123DiagnosticSnapshot pump;
} R137TraceRecord;

typedef struct {
    BOOL active;
    BOOL detailed;
    uint32_t detailTrigger;
    uint32_t detailCategory;
    uint64_t index;
    uint64_t startNs;
    uint64_t thresholdPeriodNs;
    uint64_t runtimeEpoch;
    uint64_t sourceCount;
    uint64_t titleDirectCount;
    uint64_t pumpSourceCount;
    uint64_t maxSourceGapNs;
    uint64_t missingInternalSlots;
    uint64_t sourceIntervalsWithoutInternalSlots;
    uint64_t generationAttempts;
    uint64_t generatedInternals;
    uint64_t maxCommitBacklog;
    uint64_t maxQueueDepth;
    uint64_t maxReservations;
    uint64_t maxOldestCommittedSourceAgeNs;
    uint64_t lastSourceNs;
    uint64_t lastSourceSequence;
} R137ActiveOutage;

typedef struct {
    BOOL valid;
    BOOL detailed;
    BOOL titleLowFps;
    BOOL censored;
    uint32_t detailTrigger;
    uint32_t detailCategory;
    uint64_t index;
    uint64_t startNs;
    uint64_t endNs;
    uint64_t durationNs;
    uint64_t thresholdPeriodNs;
    uint64_t sourceCount;
    uint64_t titleDirectCount;
    uint64_t pumpSourceCount;
    uint64_t maxSourceGapNs;
    uint64_t missingInternalSlots;
    uint64_t sourceIntervalsWithoutInternalSlots;
    uint64_t generationAttempts;
    uint64_t generatedInternals;
    uint64_t internalEnqueued;
    uint64_t internalResolved;
    uint64_t internalDropped;
    uint64_t deadlineMisses;
    uint64_t maxCommitBacklog;
    uint64_t maxQueueDepth;
    uint64_t maxReservations;
    uint64_t maxOldestCommittedSourceAgeNs;
    uint64_t droppedTraceRecords;
    size_t traceCount;
    R137TraceRecord *trace;
} R137ClosedOutage;

static pthread_mutex_t r137Lock = PTHREAD_MUTEX_INITIALIZER;
static R137TraceRecord r137TraceRing[kR137TraceRingCapacity];
static size_t r137TraceRingHead;
static size_t r137TraceRingCount;
static R137TraceRecord r137ActiveTrace[kR137TracePerOutageCapacity];
static size_t r137ActiveTraceCount;
static uint64_t r137ActiveTraceDropped;
static R137ActiveOutage r137ActiveOutage;
static uint64_t r137OutageSequence;
static uint64_t r137DetailedOutageCount;
static uint64_t r137DetailedFgOutageCount;
static uint64_t r137DetailedFgRoutineCount;
static uint64_t r137DetailedFgSevereCount;
static uint64_t r137DetailedTitleHitchCount;
static uint64_t r137ObservedOutageCount;
static uint64_t r137FgOutageCount;
static uint64_t r137TitleLowFpsCount;
static uint64_t r137CensoredOutageCount;
static uint64_t r137CadenceHoleCount;
static uint64_t r137TotalTimeWithoutInternalNs;
static uint64_t r137TotalFgOutageNs;
static uint64_t r137TotalTitleLowFpsNs;
static uint64_t r137TotalCensoredNs;
static uint64_t r137MaximumOutageNs;
static uint64_t r137MaximumFgOutageNs;
static uint64_t r137MaximumTitleLowFpsNs;
static uint64_t r137OutagesOver50Ms;
static uint64_t r137OutagesOver100Ms;
static uint64_t r137OutagesOver250Ms;
static uint64_t r137OutagesOver500Ms;
static uint64_t r137MaximumMissingInternalSlots;
static uint64_t r137MaximumFgMissingInternalSlots;
static uint64_t r137CadenceHolesSinceInternal;
static uint64_t r137GenerationAttemptsSinceInternal;
static uint64_t r137GeneratedInternalsSinceInternal;
static uint64_t r137MaximumCommitBacklog;
static uint64_t r137MaximumQueueDepth;
static uint64_t r137MaximumReservations;
static uint64_t r137MaximumOldestCommittedSourceAgeNs;
static uint64_t r137PhysicalSourceCount;
static uint64_t r137PhysicalInternalCount;
static uint64_t r137TitleDirectWhileOnCount;
static uint64_t r137TitleDirectDetailCount;
static uint64_t r137CompletedDurationCount;
static uint64_t r137FgDurationCount;
static uint64_t r137TitleDurationCount;
static uint64_t r137CompletedDurationsNs[kR137DurationCapacity];
static uint64_t r137FgDurationsNs[kR137DurationCapacity];
static uint64_t r137TitleDurationsNs[kR137DurationCapacity];
static uint64_t r137LastInternalNs;
static uint64_t r137LastInternalPresentId;
static uint64_t r137LastSourceNs;
static uint64_t r137LastPhysicalSourceNs;
static uint64_t r137LastPhysicalDisplayNs;
static uint64_t r137SourcesSinceInternal;
static uint64_t r137TitleDirectSinceInternal;
static uint64_t r137PumpSourcesSinceInternal;
static uint64_t r137MaxSourceGapSinceInternalNs;
static uint64_t r137MaxCommitBacklogSinceInternal;
static uint64_t r137MaxQueueDepthSinceInternal;
static uint64_t r137MaxReservationsSinceInternal;
static uint64_t r137MaxOldestCommittedAgeSinceInternalNs;
static R123DiagnosticSnapshot r137PumpAtLastInternal;
static uint64_t r137LastThreadSampleNs;
static uint64_t r137LastPumpProgressNs;
static uint32_t r137PumpWorkerState;
static uint64_t r137LatestGenerationNs;
static uint64_t r137LatestEstimatorSubmitNs;
static uint64_t r137LatestEstimatorWakeNs;
static uint64_t r137LatestMetalFxStartNs;
static uint64_t r137LatestMetalFxEndNs;
static dispatch_queue_t r137TraceWriteQueue;
static __thread uint64_t r137ThreadDirectSequence;
static __thread uint64_t r137ThreadDirectEpoch;
static __thread uint64_t r137ThreadDirectPendingDispatches;
static __thread uint32_t r137ThreadDirectReason;

static BOOL r137Enabled(void) {
    static int enabled = -1;
    if (enabled < 0) {
        const char *value = getenv("FGMETAL_R137_DIAGNOSTICS");
        enabled = value && strcmp(value, "1") == 0 ? 1 : 0;
    }
    return enabled == 1;
}

static dispatch_queue_t r137WriterQueue(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        r137TraceWriteQueue = dispatch_queue_create(
            "com.fgmetal.r137.trace-writer", DISPATCH_QUEUE_SERIAL);
    });
    return r137TraceWriteQueue;
}

static const char *r137EventName(R137TraceEvent event) {
    switch (event) {
        case R137EventSourceCaptureCommitted: return "SOURCE_CAPTURE_STAGE";
        case R137EventCaptureComplete: return "SOURCE_CAPTURE_COMPLETE";
        case R137EventSourceReservationCommit: return "SOURCE_RESERVATION_COMMIT";
        case R137EventDirectDecision: return "TITLE_DIRECT_DECISION";
        case R137EventTitleDirectWsi: return "TITLE_DIRECT_WSI";
        case R137EventGenerationEntry: return "GENERATION_ENTRY";
        case R137EventEstimatorSubmit: return "ESTIMATOR_SUBMIT";
        case R137EventEstimatorWake: return "ESTIMATOR_WAKE";
        case R137EventMetalFxStart: return "METALFX_START";
        case R137EventMetalFxComplete: return "METALFX_COMPLETE";
        case R137EventInternalResolved: return "INTERNAL_RESOLVED";
        case R137EventInternalLateDiscard: return "INTERNAL_LATE_DISCARD";
        case R137EventPumpWaitSlot: return "PUMP_WAIT_NEXT_SLOT";
        case R137EventPumpSleepReturn: return "PUMP_SLEEP_RETURN";
        case R137EventPumpSummaryDelay: return "PUMP_SUMMARY_DELAY";
        case R137EventPumpLoopLockWait: return "PUMP_LOOP_LOCK_WAIT";
        case R137EventPumpDispatchLockWait: return "PUMP_DISPATCH_LOCK_WAIT";
        case R137EventPumpTick: return "PUMP_TICK";
        case R137EventPumpIdle: return "PUMP_IDLE";
        case R137EventPumpClaim: return "PUMP_CLAIM";
        case R137EventSharedEventWait: return "SHARED_EVENT_WAIT_ENCODED";
        case R137EventDrawableRequest: return "DRAWABLE_REQUEST";
        case R137EventDrawableResult: return "DRAWABLE_RESULT";
        case R137EventGpuCopyWait: return "GPU_PRESENT_COMPLETION_WAIT";
        case R137EventWsiIssue: return "WSI_ISSUE";
        case R137EventPhysicalSource: return "PHYSICAL_SOURCE";
        case R137EventPhysicalInternal: return "PHYSICAL_INTERNAL";
        case R137EventMidpointMiss: return "MIDPOINT_MISS";
        case R137EventTitleDispatchWait: return "TITLE_WAIT_PRIOR_COMMIT_BEGIN";
        case R137EventTitleDispatchWaitEnd: return "TITLE_WAIT_PRIOR_COMMIT_END";
        case R137EventThreadStateSample: return "THREAD_STATE_SAMPLE";
        case R137EventPresentationStage: return "PRESENTATION_STAGE";
    }
    return "UNKNOWN";
}

static const char *r137DetailTriggerName(uint32_t trigger) {
    switch (trigger) {
        case 1: return "sustained-outage-100ms";
        case 2: return "title-direct-during-outage";
        case 3: return "committed-source-age-100ms";
        default: return "none";
    }
}

static const char *r137DetailCategoryName(uint32_t category) {
    switch (category) {
        case 1: return "FG";
        case 2: return "TITLE-HITCH";
        case 3: return "FG-SEVERE";
        default: return "none";
    }
}

static const char *r137MetalFxResultName(uint32_t flags) {
    if (!(flags & R137MetalFxProviderInvoked)) return "PROVIDER-SKIPPED";
    const BOOL hasFrame = (flags & R137MetalFxHasFrame) != 0;
    const BOOL hasError = (flags & R137MetalFxHasError) != 0;
    if (hasFrame && hasError) return "FRAME-AND-ERROR";
    if (hasError) return "ERROR";
    return hasFrame ? "FRAME" : "NO-OUTPUT";
}

static const char *r137PresentationDispositionName(uint32_t flags) {
    if (flags & R137StageReady) return "READY";
    if (flags & R137StagePending) return "PENDING-COPY";
    if (!(flags & R137StageEntryPresent))
        return (flags & R137StageCommittedCopyKnown)
            ? "COMMITTED-COPY-NO-STAGE" : "NO-STAGE";
    if (flags & R137StageCopyFailed) return "COPY-FAILED";
    if (!(flags & R137StageHasTexture) || !(flags & R137StageHasDevice))
        return "STAGED-INPUT-MISSING";
    if (!(flags & R137StageCopyCommitted)) return "COPY-NOT-COMMITTED";
    if ((flags & R137StageGenerationOnly) &&
        !(flags & R137StageGenerationSlotHeld) &&
        !(flags & R137StageTestSink))
        return "GENERATION-SLOT-REFUSED";
    if (flags & R137StageCopyComplete) return "COPY-COMPLETE-NOT-READY";
    return "UNCLASSIFIED";
}

static uint32_t r137TracePromotionTrigger(uint64_t outageAgeNs,
                                         uint64_t titleDirectCount,
                                         uint64_t oldestCommittedAgeNs) {
    if (outageAgeNs >= kR137DetailedOutageMinDurationNs) return 1;
    if (outageAgeNs >= kR137DetailedDirectMinAgeNs && titleDirectCount > 0)
        return 2;
    if (oldestCommittedAgeNs >= kR137DetailedCommittedAgeNs) return 3;
    return 0;
}

static uint32_t r137TracePromotionCategory(BOOL likelyTitleHitch,
                                           uint64_t fgRoutineTraceCount,
                                           uint64_t titleTraceCount,
                                           uint64_t fgSevereTraceCount,
                                           BOOL severeFgBurst) {
    if (likelyTitleHitch)
        return titleTraceCount < kR137DetailedTitleHitchLimit ? 2 : 0;
    if (severeFgBurst &&
        fgSevereTraceCount < kR137DetailedFgSevereLimit)
        return 3;
    return fgRoutineTraceCount < kR137DetailedFgRoutineLimit ? 1 : 0;
}

static const char *r137DirectReason(uint32_t flags) {
    if (flags & R137DirectOwnershipAmbiguous) return "ownership-accounting-ambiguous";
    if (flags & R137DirectTestMode) return "test-mode";
    if (flags & R137DirectTicketUnavailable) return "ownership-ticket-unavailable";
    if (flags & R137DirectImageUnavailable) return "capture-image-unavailable";
    if (flags & R137DirectCaptureUncommitted) return "capture-copy-uncommitted";
    if (flags & R137DirectRuntimeNotGenerating) return "runtime-not-generating";
    if (flags & R137DirectWorkerUnavailable) return "pump-worker-unavailable";
    if (flags & R137DirectPumpUnavailable) return "pump-unavailable";
    if (flags & R137DirectCapacityRefused) return "capacity-refusal";
    if (flags & R137DirectReservationRefused) return "reservation-refusal";
    if (flags & R137DirectOwnerAlreadyDirect) return "ownership-already-direct";
    return "unspecified-fallback";
}

static void r137SetPumpState(uint32_t state, uint64_t nowNs) {
    if (!r137Enabled()) return;
    __atomic_store_n(&r137PumpWorkerState, state, __ATOMIC_RELAXED);
    __atomic_store_n(&r137LastPumpProgressNs, nowNs, __ATOMIC_RELAXED);
}

static void r137TracePoint(R137TraceEvent event, uint64_t sequence,
                           uint64_t pairA, uint64_t pairB,
                           uint64_t sourceOrdinal, uint32_t flags,
                           uint64_t value0, uint64_t value1) {
    if (!r137Enabled()) return;
    const uint64_t nowNs = r105NowNs();
    R137TraceRecord record = {0};
    record.event = event;
    record.atNs = nowNs;
    record.sequence = sequence;
    record.pairA = pairA;
    record.pairB = pairB;
    record.sourceOrdinal = sourceOrdinal;
    record.runtimeMode = r127RuntimeModeSnapshot();
    record.runtimeEpoch = r127RuntimeEpochSnapshot();
    record.flags = flags;
    record.value0 = value0;
    record.value1 = value1;
    record.workerState = __atomic_load_n(&r137PumpWorkerState, __ATOMIC_RELAXED);
    if (r123Pump) record.pump = [r123Pump diagnosticSnapshotAtNs:nowNs];

    pthread_mutex_lock(&r137Lock);
    switch (event) {
        case R137EventGenerationEntry:
            r137LatestGenerationNs = nowNs;
            ++r137GenerationAttemptsSinceInternal;
            if (r137ActiveOutage.active)
                ++r137ActiveOutage.generationAttempts;
            break;
        case R137EventEstimatorSubmit: r137LatestEstimatorSubmitNs = nowNs; break;
        case R137EventEstimatorWake: r137LatestEstimatorWakeNs = nowNs; break;
        case R137EventMetalFxStart: r137LatestMetalFxStartNs = nowNs; break;
        case R137EventMetalFxComplete:
            r137LatestMetalFxEndNs = nowNs;
            if (record.flags & R137MetalFxHasFrame) {
                ++r137GeneratedInternalsSinceInternal;
                if (r137ActiveOutage.active)
                    ++r137ActiveOutage.generatedInternals;
            }
            break;
        case R137EventMidpointMiss:
            ++r137CadenceHoleCount;
            ++r137CadenceHolesSinceInternal;
            if (r137ActiveOutage.active)
                ++r137ActiveOutage.missingInternalSlots;
            break;
        default: break;
    }
    const uint64_t commitBacklog =
        record.pump.committedQueuedSourceCount +
        record.pump.committedClaimedSourceCount;
    if (commitBacklog > r137MaximumCommitBacklog)
        r137MaximumCommitBacklog = commitBacklog;
    if (record.pump.depth > r137MaximumQueueDepth)
        r137MaximumQueueDepth = record.pump.depth;
    if (record.pump.activeSourceReservations > r137MaximumReservations)
        r137MaximumReservations = record.pump.activeSourceReservations;
    if (record.pump.oldestUndispatchedSourceAgeNs >
        r137MaximumOldestCommittedSourceAgeNs)
        r137MaximumOldestCommittedSourceAgeNs =
            record.pump.oldestUndispatchedSourceAgeNs;
    if (commitBacklog > r137MaxCommitBacklogSinceInternal)
        r137MaxCommitBacklogSinceInternal = commitBacklog;
    if (record.pump.depth > r137MaxQueueDepthSinceInternal)
        r137MaxQueueDepthSinceInternal = record.pump.depth;
    if (record.pump.activeSourceReservations > r137MaxReservationsSinceInternal)
        r137MaxReservationsSinceInternal =
            record.pump.activeSourceReservations;
    if (record.pump.oldestUndispatchedSourceAgeNs >
        r137MaxOldestCommittedAgeSinceInternalNs)
        r137MaxOldestCommittedAgeSinceInternalNs =
            record.pump.oldestUndispatchedSourceAgeNs;
    if (r137ActiveOutage.active) {
        if (commitBacklog > r137ActiveOutage.maxCommitBacklog)
            r137ActiveOutage.maxCommitBacklog = commitBacklog;
        if (record.pump.depth > r137ActiveOutage.maxQueueDepth)
            r137ActiveOutage.maxQueueDepth = record.pump.depth;
        if (record.pump.activeSourceReservations >
            r137ActiveOutage.maxReservations)
            r137ActiveOutage.maxReservations =
                record.pump.activeSourceReservations;
        if (record.pump.oldestUndispatchedSourceAgeNs >
            r137ActiveOutage.maxOldestCommittedSourceAgeNs)
            r137ActiveOutage.maxOldestCommittedSourceAgeNs =
                record.pump.oldestUndispatchedSourceAgeNs;
    }
    record.latestGenerationNs = r137LatestGenerationNs;
    record.latestEstimatorSubmitNs = r137LatestEstimatorSubmitNs;
    record.latestEstimatorWakeNs = r137LatestEstimatorWakeNs;
    record.latestMetalFxStartNs = r137LatestMetalFxStartNs;
    record.latestMetalFxEndNs = r137LatestMetalFxEndNs;
    if (r137ActiveOutage.active && r137ActiveOutage.detailed) {
        if (r137ActiveTraceCount < kR137TracePerOutageCapacity)
            r137ActiveTrace[r137ActiveTraceCount++] = record;
        else
            ++r137ActiveTraceDropped;
    } else {
        r137TraceRing[r137TraceRingHead] = record;
        r137TraceRingHead = (r137TraceRingHead + 1) % kR137TraceRingCapacity;
        if (r137TraceRingCount < kR137TraceRingCapacity) ++r137TraceRingCount;
    }
    pthread_mutex_unlock(&r137Lock);
}

static void r137LogTraceRecord(uint64_t outageIndex,
                               const R137TraceRecord *record,
                               size_t traceIndex) {
    const R123DiagnosticSnapshot *p = &record->pump;
    const BOOL directEvent = record->event == R137EventDirectDecision ||
        record->event == R137EventTitleDirectWsi;
    const char *eventDetail = record->event == R137EventMetalFxComplete
        ? r137MetalFxResultName(record->flags)
        : (record->event == R137EventPresentationStage
            ? r137PresentationDispositionName(record->flags) : "none");
    logLine("R137_TRACE outage=%llu index=%lu atNs=%llu event=%s acquisitionId=%llu "
                "pair=%llu->%llu sourceOrdinal=%llu flags=%u eventDetail=%s "
                "directReason=%s value0=%llu value1=%llu "
            "mode=%u epoch=%llu workerState=%u "
            "latestGenerationNs=%llu estimatorSubmitNs=%llu estimatorWakeNs=%llu "
            "metalFxStartNs=%llu metalFxEndNs=%llu queueDepth=%lu "
            "head=%u:%llu->%llu:%d "
            "next=%u:%llu->%llu:%d claimed=%u:%llu->%llu:%d claimedAtNs=%llu "
            "nextSlotNs=%llu sourcePeriodNs=%llu reservations=%lu queuedCommitted=%lu "
            "claimedCommitted=%lu oldestCommitted=%llu/%llu ageNs=%llu "
            "newestCommitted=%llu/%llu ageNs=%llu placeholders=%lu readyInternal=%lu "
            "sourceEnqueued=%llu sourceDispatched=%llu internalEnqueued=%llu "
            "internalResolved=%llu internalDispatched=%llu sourceDropped=%llu "
            "internalDropped=%llu unresolvedDropped=%llu deadlineMisses=%llu",
        (unsigned long long)outageIndex, (unsigned long)traceIndex,
        (unsigned long long)record->atNs, r137EventName(record->event),
        (unsigned long long)record->sequence,
        (unsigned long long)record->pairA, (unsigned long long)record->pairB,
        (unsigned long long)record->sourceOrdinal, record->flags,
        eventDetail, directEvent ? r137DirectReason(record->flags) : "n/a",
        (unsigned long long)record->value0, (unsigned long long)record->value1,
        record->runtimeMode, (unsigned long long)record->runtimeEpoch,
        record->workerState,
        (unsigned long long)record->latestGenerationNs,
        (unsigned long long)record->latestEstimatorSubmitNs,
        (unsigned long long)record->latestEstimatorWakeNs,
        (unsigned long long)record->latestMetalFxStartNs,
        (unsigned long long)record->latestMetalFxEndNs,
        (unsigned long)p->depth, p->head.present ? (unsigned)p->head.kind + 1 : 0,
        (unsigned long long)p->head.seqA, (unsigned long long)p->head.seqB,
        p->head.ready ? 1 : 0, p->next.present ? (unsigned)p->next.kind + 1 : 0,
        (unsigned long long)p->next.seqA, (unsigned long long)p->next.seqB,
        p->next.ready ? 1 : 0, p->claimed.present ? (unsigned)p->claimed.kind + 1 : 0,
        (unsigned long long)p->claimed.seqA, (unsigned long long)p->claimed.seqB,
        p->claimed.ready ? 1 : 0, (unsigned long long)p->claimedAtNs,
        (unsigned long long)p->nextSlotNs, (unsigned long long)p->sourcePeriodNs,
        (unsigned long)p->activeSourceReservations,
        (unsigned long)p->committedQueuedSourceCount,
        (unsigned long)p->committedClaimedSourceCount,
        (unsigned long long)p->oldestUndispatchedSourceOrdinal,
        (unsigned long long)p->oldestUndispatchedSourceSequence,
        (unsigned long long)p->oldestUndispatchedSourceAgeNs,
        (unsigned long long)p->newestUndispatchedSourceOrdinal,
        (unsigned long long)p->newestUndispatchedSourceSequence,
        (unsigned long long)p->newestUndispatchedSourceAgeNs,
        (unsigned long)p->internalPlaceholderCount,
        (unsigned long)p->internalReadyCount,
        (unsigned long long)p->sourceEnqueued,
        (unsigned long long)p->sourceDispatched,
        (unsigned long long)p->internalEnqueued,
        (unsigned long long)p->internalResolved,
        (unsigned long long)p->internalDispatched,
        (unsigned long long)p->sourceDropped,
        (unsigned long long)p->internalDropped,
        (unsigned long long)p->internalDroppedUnresolved,
        (unsigned long long)p->deadlineMisses);
}

static void r137EmitClosedOutage(R137ClosedOutage closed) {
    if (!closed.valid) return;
    dispatch_queue_t queue = r137WriterQueue();
    if (!queue) {
        free(closed.trace);
        return;
    }
    dispatch_async(queue, ^{
        logLine("R137_OUTAGE_END clock=MTLDrawable.presentedTime-host-ns "
                "index=%llu startNs=%llu endNs=%llu durationNs=%llu "
                "thresholdPeriodNs=%llu missingInternalSlots=%llu sourceFrames=%llu "
                "titleDirectFrames=%llu pumpSourceFrames=%llu maxSourceGapNs=%llu "
                "sourceIntervalsWithoutInternalSlots=%llu generationAttempts=%llu "
                "generatedInternals=%llu internalEnqueued=%llu internalResolved=%llu "
                "internalDropped=%llu deadlineMisses=%llu maxCommitBacklog=%llu "
                "maxQueueDepth=%llu maxReservations=%llu "
                "oldestCommittedSourceAgeMaxNs=%llu detailTrigger=%s detailBudget=%s "
                "classification=%s detailedTrace=%d traceRecords=%lu droppedTraceRecords=%llu",
            (unsigned long long)closed.index,
            (unsigned long long)closed.startNs,
            (unsigned long long)closed.endNs,
            (unsigned long long)closed.durationNs,
            (unsigned long long)closed.thresholdPeriodNs,
            (unsigned long long)closed.missingInternalSlots,
            (unsigned long long)closed.sourceCount,
            (unsigned long long)closed.titleDirectCount,
            (unsigned long long)closed.pumpSourceCount,
            (unsigned long long)closed.maxSourceGapNs,
            (unsigned long long)closed.sourceIntervalsWithoutInternalSlots,
            (unsigned long long)closed.generationAttempts,
            (unsigned long long)closed.generatedInternals,
            (unsigned long long)closed.internalEnqueued,
            (unsigned long long)closed.internalResolved,
            (unsigned long long)closed.internalDropped,
            (unsigned long long)closed.deadlineMisses,
            (unsigned long long)closed.maxCommitBacklog,
            (unsigned long long)closed.maxQueueDepth,
            (unsigned long long)closed.maxReservations,
            (unsigned long long)closed.maxOldestCommittedSourceAgeNs,
            r137DetailTriggerName(closed.detailTrigger),
            r137DetailCategoryName(closed.detailCategory),
            closed.censored ? "RUNTIME-CENSORED" :
                (closed.titleLowFps ? "TITLE-LOW-FPS" : "FG-NO-INTERNAL"),
            closed.detailed ? 1 : 0, (unsigned long)closed.traceCount,
            (unsigned long long)closed.droppedTraceRecords);
        for (size_t i = 0; i < closed.traceCount; ++i)
            r137LogTraceRecord(closed.index, &closed.trace[i], i);
        free(closed.trace);
    });
}

static void r137OpenOutageLocked(uint64_t nowNs, uint64_t periodNs,
                                 uint64_t epoch) {
    if (r137ActiveOutage.active) return;
    r137ActiveOutage = (R137ActiveOutage){
        .active = YES,
        .detailed = NO,
        .index = ++r137OutageSequence,
        .startNs = r137LastInternalNs,
        .thresholdPeriodNs = periodNs,
        .runtimeEpoch = epoch,
        .sourceCount = r137SourcesSinceInternal,
        .titleDirectCount = r137TitleDirectSinceInternal,
        .pumpSourceCount = r137PumpSourcesSinceInternal,
        .maxSourceGapNs = r137MaxSourceGapSinceInternalNs,
        .missingInternalSlots = r137CadenceHolesSinceInternal,
        .sourceIntervalsWithoutInternalSlots = r137SourcesSinceInternal > 0
            ? r137SourcesSinceInternal - 1 : 0,
        .generationAttempts = r137GenerationAttemptsSinceInternal,
        .generatedInternals = r137GeneratedInternalsSinceInternal,
        .maxCommitBacklog = r137MaxCommitBacklogSinceInternal,
        .maxQueueDepth = r137MaxQueueDepthSinceInternal,
        .maxReservations = r137MaxReservationsSinceInternal,
        .maxOldestCommittedSourceAgeNs =
            r137MaxOldestCommittedAgeSinceInternalNs,
        .lastSourceNs = r137LastSourceNs,
        .lastSourceSequence = 0,
    };
    r137ActiveTraceCount = 0;
    r137ActiveTraceDropped = 0;
    (void)nowNs;
}

static BOOL r137ActiveOutageLikelyTitleHitchLocked(uint64_t nowNs) {
    if (!r137ActiveOutage.active || !r137ActiveOutage.thresholdPeriodNs)
        return NO;
    const uint64_t thresholdNs =
        2 * r137ActiveOutage.thresholdPeriodNs;
    return r137ActiveOutage.maxSourceGapNs > thresholdNs ||
        (r137LastPhysicalSourceNs && nowNs > r137LastPhysicalSourceNs &&
         nowNs - r137LastPhysicalSourceNs > thresholdNs);
}

static void r137PromoteActiveOutageTraceLocked(uint64_t nowNs) {
    if (!r137ActiveOutage.active || r137ActiveOutage.detailed)
        return;

    const uint64_t ageNs = nowNs > r137ActiveOutage.startNs
        ? nowNs - r137ActiveOutage.startNs : 0;
    const uint32_t trigger = r137TracePromotionTrigger(
        ageNs, r137ActiveOutage.titleDirectCount,
        r137ActiveOutage.maxOldestCommittedSourceAgeNs);
    if (!trigger) return;
    const BOOL likelyTitleHitch =
        r137ActiveOutageLikelyTitleHitchLocked(nowNs);
    const BOOL severeFgBurst = !likelyTitleHitch &&
        (r137ActiveOutage.missingInternalSlots > 2 ||
         ageNs >= 250000000ULL);
    const uint32_t category = r137TracePromotionCategory(
        likelyTitleHitch, r137DetailedFgRoutineCount,
        r137DetailedTitleHitchCount, r137DetailedFgSevereCount,
        severeFgBurst);
    if (!category) return;

    r137ActiveOutage.detailed = YES;
    r137ActiveOutage.detailTrigger = trigger;
    r137ActiveOutage.detailCategory = category;
    ++r137DetailedOutageCount;
    if (category == 1) {
        ++r137DetailedFgOutageCount;
        ++r137DetailedFgRoutineCount;
    } else if (category == 3) {
        ++r137DetailedFgOutageCount;
        ++r137DetailedFgSevereCount;
    } else {
        ++r137DetailedTitleHitchCount;
    }
    const size_t first = (r137TraceRingHead + kR137TraceRingCapacity -
                          r137TraceRingCount) % kR137TraceRingCapacity;
    for (size_t i = 0; i < r137TraceRingCount; ++i)
        r137ActiveTrace[i] =
            r137TraceRing[(first + i) % kR137TraceRingCapacity];
    r137ActiveTraceCount = r137TraceRingCount;
    r137ActiveTraceDropped = 0;
    r137TraceRingHead = 0;
    r137TraceRingCount = 0;
}

static R137ClosedOutage r137CloseOutageLocked(
    uint64_t nowNs, BOOL titleHitch, BOOL censored,
    R123DiagnosticSnapshot pump) {
    R137ClosedOutage closed = {0};
    if (!r137ActiveOutage.active) return closed;
    closed.valid = YES;
    closed.detailed = r137ActiveOutage.detailed;
    closed.titleLowFps = titleHitch;
    closed.censored = censored;
    closed.detailTrigger = r137ActiveOutage.detailTrigger;
    closed.detailCategory = r137ActiveOutage.detailCategory;
    closed.index = r137ActiveOutage.index;
    closed.startNs = r137ActiveOutage.startNs;
    closed.endNs = nowNs;
    closed.durationNs = nowNs > closed.startNs ? nowNs - closed.startNs : 0;
    closed.thresholdPeriodNs = r137ActiveOutage.thresholdPeriodNs;
    closed.sourceCount = r137ActiveOutage.sourceCount;
    closed.titleDirectCount = r137ActiveOutage.titleDirectCount;
    closed.pumpSourceCount = r137ActiveOutage.pumpSourceCount;
    closed.maxSourceGapNs = r137ActiveOutage.maxSourceGapNs;
    closed.missingInternalSlots = r137ActiveOutage.missingInternalSlots;
    closed.sourceIntervalsWithoutInternalSlots =
        r137ActiveOutage.sourceIntervalsWithoutInternalSlots;
    closed.generationAttempts = r137ActiveOutage.generationAttempts;
    closed.generatedInternals = r137ActiveOutage.generatedInternals;
    closed.internalEnqueued = pump.internalEnqueued >=
            r137PumpAtLastInternal.internalEnqueued
        ? pump.internalEnqueued - r137PumpAtLastInternal.internalEnqueued : 0;
    closed.internalResolved = pump.internalResolved >=
            r137PumpAtLastInternal.internalResolved
        ? pump.internalResolved - r137PumpAtLastInternal.internalResolved : 0;
    closed.internalDropped = pump.internalDropped >=
            r137PumpAtLastInternal.internalDropped
        ? pump.internalDropped - r137PumpAtLastInternal.internalDropped : 0;
    closed.deadlineMisses = pump.deadlineMisses >=
            r137PumpAtLastInternal.deadlineMisses
        ? pump.deadlineMisses - r137PumpAtLastInternal.deadlineMisses : 0;
    closed.maxCommitBacklog = r137ActiveOutage.maxCommitBacklog;
    closed.maxQueueDepth = r137ActiveOutage.maxQueueDepth;
    closed.maxReservations = r137ActiveOutage.maxReservations;
    closed.maxOldestCommittedSourceAgeNs =
        r137ActiveOutage.maxOldestCommittedSourceAgeNs;
    closed.droppedTraceRecords = closed.detailed
        ? r137ActiveTraceDropped : 0;
    closed.traceCount = closed.detailed ? r137ActiveTraceCount : 0;
    if (closed.detailed && closed.traceCount) {
        closed.trace = malloc(closed.traceCount * sizeof(*closed.trace));
        if (closed.trace)
            memcpy(closed.trace, r137ActiveTrace,
                   closed.traceCount * sizeof(*closed.trace));
        else
            closed.traceCount = 0;
    }

    ++r137ObservedOutageCount;
    r137TotalTimeWithoutInternalNs += closed.durationNs;
    if (closed.durationNs > r137MaximumOutageNs)
        r137MaximumOutageNs = closed.durationNs;
    if (closed.missingInternalSlots > r137MaximumMissingInternalSlots)
        r137MaximumMissingInternalSlots = closed.missingInternalSlots;
    if (!censored && !titleHitch &&
        closed.missingInternalSlots > r137MaximumFgMissingInternalSlots)
        r137MaximumFgMissingInternalSlots = closed.missingInternalSlots;
    if (censored) {
        ++r137CensoredOutageCount;
        r137TotalCensoredNs += closed.durationNs;
    } else if (titleHitch) {
        ++r137TitleLowFpsCount;
        r137TotalTitleLowFpsNs += closed.durationNs;
        if (closed.durationNs > r137MaximumTitleLowFpsNs)
            r137MaximumTitleLowFpsNs = closed.durationNs;
    } else {
        ++r137FgOutageCount;
        r137TotalFgOutageNs += closed.durationNs;
        if (closed.durationNs > r137MaximumFgOutageNs)
            r137MaximumFgOutageNs = closed.durationNs;
        if (closed.durationNs > 50000000ULL) ++r137OutagesOver50Ms;
        if (closed.durationNs > 100000000ULL) ++r137OutagesOver100Ms;
        if (closed.durationNs > 250000000ULL) ++r137OutagesOver250Ms;
        if (closed.durationNs > 500000000ULL) ++r137OutagesOver500Ms;
        if (r137FgDurationCount < kR137DurationCapacity)
            r137FgDurationsNs[r137FgDurationCount++] = closed.durationNs;
    }
    if (!censored) {
        if (r137CompletedDurationCount < kR137DurationCapacity)
            r137CompletedDurationsNs[r137CompletedDurationCount++] =
                closed.durationNs;
        if (titleHitch && r137TitleDurationCount < kR137DurationCapacity)
            r137TitleDurationsNs[r137TitleDurationCount++] = closed.durationNs;
    }
    r137ActiveOutage = (R137ActiveOutage){0};
    r137ActiveTraceCount = 0;
    r137ActiveTraceDropped = 0;
    return closed;
}

static void r137ObservePhysicalPresentation(BOOL isSource, uint64_t sequence,
                                            uint64_t pairA, uint64_t pairB,
                                            uint64_t sourceOrdinal,
                                            uint64_t epoch, BOOL fromPump,
                                            uint64_t presentationHostNs,
                                            uint64_t callbackMonotonicNs) {
    if (!r137Enabled()) return;
    const R137TraceEvent traceEvent = isSource ? R137EventPhysicalSource
                                               : R137EventPhysicalInternal;
    r137TracePoint(traceEvent, sequence, pairA, pairB, sourceOrdinal,
                   fromPump ? 1u : 0u, presentationHostNs,
                   callbackMonotonicNs);
    const R123DiagnosticSnapshot pump = r123Pump
        ? [r123Pump diagnosticSnapshotAtNs:callbackMonotonicNs]
        : (R123DiagnosticSnapshot){0};
    const uint32_t mode = r127RuntimeModeSnapshot();
    const uint64_t liveEpoch = r127RuntimeEpochSnapshot();
    const uint64_t periodNs = pump.sourcePeriodNs;
    R137ClosedOutage closed = {0};

    pthread_mutex_lock(&r137Lock);
    // Presented callbacks can arrive out of order. The physical timestamp is
    // the authority for outage intervals; retain each raw trace record above,
    // but do not let an older callback reverse the live outage state.
    if (presentationHostNs < r137LastPhysicalDisplayNs) {
        pthread_mutex_unlock(&r137Lock);
        return;
    }
    r137LastPhysicalDisplayNs = presentationHostNs;
    if (mode != R127RuntimeModeGenerating || liveEpoch != epoch) {
        if (r137ActiveOutage.active)
            closed = r137CloseOutageLocked(presentationHostNs, NO, YES, pump);
        r137LastInternalNs = 0;
        r137LastInternalPresentId = 0;
        r137LastSourceNs = 0;
        r137LastPhysicalSourceNs = 0;
        r137SourcesSinceInternal = 0;
        r137TitleDirectSinceInternal = 0;
        r137PumpSourcesSinceInternal = 0;
        r137MaxSourceGapSinceInternalNs = 0;
        r137CadenceHolesSinceInternal = 0;
        r137GenerationAttemptsSinceInternal = 0;
        r137GeneratedInternalsSinceInternal = 0;
        r137MaxCommitBacklogSinceInternal = 0;
        r137MaxQueueDepthSinceInternal = 0;
        r137MaxReservationsSinceInternal = 0;
        r137MaxOldestCommittedAgeSinceInternalNs = 0;
        r137PumpAtLastInternal = pump;
        r137LastThreadSampleNs = 0;
        pthread_mutex_unlock(&r137Lock);
        r137EmitClosedOutage(closed);
        return;
    }

    if (isSource) {
        ++r137PhysicalSourceCount;
        if (!fromPump) {
            ++r137TitleDirectWhileOnCount;
            if (r137TitleDirectSinceInternal != UINT64_MAX)
                ++r137TitleDirectSinceInternal;
        } else if (r137PumpSourcesSinceInternal != UINT64_MAX) {
            ++r137PumpSourcesSinceInternal;
        }
        if (r137SourcesSinceInternal != UINT64_MAX)
            ++r137SourcesSinceInternal;
        if (r137LastPhysicalSourceNs &&
            presentationHostNs > r137LastPhysicalSourceNs) {
            const uint64_t sourceGap =
                presentationHostNs - r137LastPhysicalSourceNs;
            if (sourceGap > r137MaxSourceGapSinceInternalNs)
                r137MaxSourceGapSinceInternalNs = sourceGap;
            if (r137ActiveOutage.active &&
                sourceGap > r137ActiveOutage.maxSourceGapNs)
                r137ActiveOutage.maxSourceGapNs = sourceGap;
        }
        r137LastSourceNs = presentationHostNs;
        r137LastPhysicalSourceNs = presentationHostNs;
        if (r137ActiveOutage.active && r137ActiveOutage.runtimeEpoch == epoch) {
            ++r137ActiveOutage.sourceCount;
            if (!fromPump) ++r137ActiveOutage.titleDirectCount;
            else ++r137ActiveOutage.pumpSourceCount;
            if (r137MaxSourceGapSinceInternalNs >
                r137ActiveOutage.maxSourceGapNs)
                r137ActiveOutage.maxSourceGapNs = r137MaxSourceGapSinceInternalNs;
            r137ActiveOutage.sourceIntervalsWithoutInternalSlots =
                r137ActiveOutage.sourceCount > 0
                    ? r137ActiveOutage.sourceCount - 1 : 0;
            r137ActiveOutage.lastSourceNs = presentationHostNs;
            r137ActiveOutage.lastSourceSequence = sequence;
        } else if (!r137ActiveOutage.active && r137LastInternalNs && periodNs &&
                   presentationHostNs > r137LastInternalNs &&
                   presentationHostNs - r137LastInternalNs > 2 * periodNs) {
            r137OpenOutageLocked(presentationHostNs, periodNs, epoch);
        }
        if (r137ActiveOutage.active &&
            r137ActiveOutage.runtimeEpoch == epoch)
            r137PromoteActiveOutageTraceLocked(presentationHostNs);
    } else {
        ++r137PhysicalInternalCount;
        if (!r137ActiveOutage.active && r137LastInternalNs &&
            r137SourcesSinceInternal && periodNs &&
            presentationHostNs > r137LastInternalNs &&
            presentationHostNs - r137LastInternalNs > 2 * periodNs) {
            r137OpenOutageLocked(presentationHostNs, periodNs, epoch);
        }
        if (r137ActiveOutage.active && r137ActiveOutage.runtimeEpoch == epoch) {
            r137PromoteActiveOutageTraceLocked(presentationHostNs);
            const uint64_t thresholdNs =
                2 * r137ActiveOutage.thresholdPeriodNs;
            const BOOL titleHitch = thresholdNs &&
                (r137ActiveOutage.maxSourceGapNs > thresholdNs ||
                 (r137LastPhysicalSourceNs &&
                  presentationHostNs > r137LastPhysicalSourceNs &&
                  presentationHostNs - r137LastPhysicalSourceNs > thresholdNs));
            closed = r137CloseOutageLocked(presentationHostNs, titleHitch,
                                           NO, pump);
        }
        r137LastInternalNs = presentationHostNs;
        r137LastInternalPresentId = sequence;
        r137SourcesSinceInternal = 0;
        r137TitleDirectSinceInternal = 0;
        r137PumpSourcesSinceInternal = 0;
        r137MaxSourceGapSinceInternalNs = 0;
        r137CadenceHolesSinceInternal = 0;
        r137GenerationAttemptsSinceInternal = 0;
        r137GeneratedInternalsSinceInternal = 0;
        r137MaxCommitBacklogSinceInternal =
            pump.committedQueuedSourceCount +
            pump.committedClaimedSourceCount;
        r137MaxQueueDepthSinceInternal = pump.depth;
        r137MaxReservationsSinceInternal = pump.activeSourceReservations;
        r137MaxOldestCommittedAgeSinceInternalNs =
            pump.oldestUndispatchedSourceAgeNs;
        r137PumpAtLastInternal = pump;
        r137LastThreadSampleNs = 0;
        r137LastSourceNs = 0;
    }

    const BOOL sampleThread = r137ActiveOutage.active &&
        (!r137LastThreadSampleNs ||
         presentationHostNs - r137LastThreadSampleNs >=
             kR137ThreadSampleIntervalNs);
    if (sampleThread) r137LastThreadSampleNs = presentationHostNs;
    const uint64_t outageAgeNs = r137ActiveOutage.active &&
        presentationHostNs > r137ActiveOutage.startNs
            ? presentationHostNs - r137ActiveOutage.startNs : 0;
    pthread_mutex_unlock(&r137Lock);

    if (sampleThread) {
        const uint32_t state = __atomic_load_n(&r137PumpWorkerState, __ATOMIC_RELAXED);
        const uint64_t progressNs = __atomic_load_n(&r137LastPumpProgressNs,
                                                      __ATOMIC_RELAXED);
        r137TracePoint(R137EventThreadStateSample, sequence, pairA, pairB,
                       sourceOrdinal, state,
                       progressNs && callbackMonotonicNs > progressNs
                           ? callbackMonotonicNs - progressNs : 0,
                       outageAgeNs);
    }
    r137EmitClosedOutage(closed);
}

static void r137EmitSummary(const char *reason) {
    if (!r137Enabled()) return;
    uint64_t *completedDurations = NULL;
    uint64_t *fgDurations = NULL;
    uint64_t *titleDurations = NULL;
    uint64_t completedSampleCount = 0, fgSampleCount = 0;
    uint64_t titleSampleCount = 0;
    uint64_t observed = 0, fgCount = 0, titleLowFps = 0, censored = 0, totalNs = 0;
    uint64_t totalFgNs = 0, totalTitleNs = 0, totalCensoredNs = 0;
    uint64_t maximumNs = 0, maximumFgNs = 0, maximumTitleNs = 0;
    uint64_t over50 = 0, over100 = 0, over250 = 0, over500 = 0;
    uint64_t missingSlots = 0, directCount = 0, sourceCount = 0, internalCount = 0;
    uint64_t fgMissingSlots = 0, maxCommitBacklog = 0, maxQueueDepth = 0;
    uint64_t maxReservations = 0, maxOldestCommittedAgeNs = 0;
    uint64_t cadenceHoles = 0, detailedBursts = 0;
    uint64_t detailedFgBursts = 0, detailedFgSevereBursts = 0;
    uint64_t detailedTitleHitches = 0;
    pthread_mutex_lock(&r137Lock);
    completedSampleCount = r137CompletedDurationCount;
    fgSampleCount = r137FgDurationCount;
    titleSampleCount = r137TitleDurationCount;
    if (completedSampleCount) {
        completedDurations = malloc(completedSampleCount *
                                    sizeof(*completedDurations));
        if (completedDurations)
            memcpy(completedDurations, r137CompletedDurationsNs,
                   completedSampleCount * sizeof(*completedDurations));
    }
    if (fgSampleCount) {
        fgDurations = malloc(fgSampleCount * sizeof(*fgDurations));
        if (fgDurations)
            memcpy(fgDurations, r137FgDurationsNs,
                   fgSampleCount * sizeof(*fgDurations));
    }
    if (titleSampleCount) {
        titleDurations = malloc(titleSampleCount * sizeof(*titleDurations));
        if (titleDurations)
            memcpy(titleDurations, r137TitleDurationsNs,
                   titleSampleCount * sizeof(*titleDurations));
    }
    observed = r137ObservedOutageCount;
    fgCount = r137FgOutageCount;
    titleLowFps = r137TitleLowFpsCount;
    censored = r137CensoredOutageCount;
    totalNs = r137TotalTimeWithoutInternalNs;
    totalFgNs = r137TotalFgOutageNs;
    totalTitleNs = r137TotalTitleLowFpsNs;
    totalCensoredNs = r137TotalCensoredNs;
    maximumNs = r137MaximumOutageNs;
    maximumFgNs = r137MaximumFgOutageNs;
    maximumTitleNs = r137MaximumTitleLowFpsNs;
    over50 = r137OutagesOver50Ms;
    over100 = r137OutagesOver100Ms;
    over250 = r137OutagesOver250Ms;
    over500 = r137OutagesOver500Ms;
    missingSlots = r137MaximumMissingInternalSlots;
    fgMissingSlots = r137MaximumFgMissingInternalSlots;
    maxCommitBacklog = r137MaximumCommitBacklog;
    maxQueueDepth = r137MaximumQueueDepth;
    maxReservations = r137MaximumReservations;
    maxOldestCommittedAgeNs = r137MaximumOldestCommittedSourceAgeNs;
    directCount = r137TitleDirectWhileOnCount;
    sourceCount = r137PhysicalSourceCount;
    internalCount = r137PhysicalInternalCount;
    cadenceHoles = r137CadenceHoleCount;
    detailedBursts = r137DetailedOutageCount;
    detailedFgBursts = r137DetailedFgOutageCount;
    detailedFgSevereBursts = r137DetailedFgSevereCount;
    detailedTitleHitches = r137DetailedTitleHitchCount;
    pthread_mutex_unlock(&r137Lock);

    const R123DiagnosticSnapshot pump = r123Pump
        ? [r123Pump diagnosticSnapshotAtNs:r105NowNs()] : (R123DiagnosticSnapshot){0};
    dispatch_queue_t queue = r137WriterQueue();
    if (!queue) {
        free(completedDurations);
        free(fgDurations);
        free(titleDurations);
        return;
    }
    dispatch_async(queue, ^{
        if (completedDurations)
            qsort(completedDurations, completedSampleCount,
                  sizeof(*completedDurations), r133CompareU64);
        if (fgDurations)
            qsort(fgDurations, fgSampleCount, sizeof(*fgDurations),
                  r133CompareU64);
        if (titleDurations)
            qsort(titleDurations, titleSampleCount, sizeof(*titleDurations),
                  r133CompareU64);
        const uint64_t p50 = completedDurations && completedSampleCount
            ? r127MedianNs(completedDurations, completedSampleCount) : 0;
        const uint64_t p90 = completedDurations && completedSampleCount
            ? r127NearestRankNs(completedDurations, completedSampleCount, 90) : 0;
        const uint64_t p99 = completedDurations && completedSampleCount
            ? r127NearestRankNs(completedDurations, completedSampleCount, 99) : 0;
        const uint64_t fgP50 = fgDurations && fgSampleCount
            ? r127MedianNs(fgDurations, fgSampleCount) : 0;
        const uint64_t fgP90 = fgDurations && fgSampleCount
            ? r127NearestRankNs(fgDurations, fgSampleCount, 90) : 0;
        const uint64_t fgP99 = fgDurations && fgSampleCount
            ? r127NearestRankNs(fgDurations, fgSampleCount, 99) : 0;
        const uint64_t titleP50 = titleDurations && titleSampleCount
            ? r127MedianNs(titleDurations, titleSampleCount) : 0;
        const uint64_t titleP90 = titleDurations && titleSampleCount
            ? r127NearestRankNs(titleDurations, titleSampleCount, 90) : 0;
        const uint64_t titleP99 = titleDurations && titleSampleCount
            ? r127NearestRankNs(titleDurations, titleSampleCount, 99) : 0;
        const double cadenceRate = sourceCount
            ? (double)cadenceHoles / (double)sourceCount : 0.0;
        logLine("R137_OUTAGE_SUMMARY reason=%s observedIntervals=%llu "
                "fgOutages=%llu titleLowFpsIntervals=%llu censoredIntervals=%llu "
                "completedIntervals=%llu p50Ns=%llu p90Ns=%llu p99Ns=%llu "
                "fgP50Ns=%llu fgP90Ns=%llu fgP99Ns=%llu "
                "titleP50Ns=%llu titleP90Ns=%llu titleP99Ns=%llu "
                "maxNs=%llu maxFgNs=%llu maxTitleLowFpsNs=%llu "
                "over50ms=%llu over100ms=%llu "
                "over250ms=%llu over500ms=%llu maxMissingInternalSlots=%llu "
                "maxFgMissingInternalSlots=%llu "
                "totalTimeWithoutInternalNs=%llu totalFgOutageNs=%llu "
                "totalTitleLowFpsNs=%llu totalCensoredNs=%llu cadenceHoleEvents=%llu "
                "physicalSource=%llu physicalInternal=%llu cadenceHoleEventRate=%.8f "
                "outageClock=MTLDrawable.presentedTime-host-ns "
                "titleDirectWhileOn=%llu sourceEnqueued=%llu sourceDispatched=%llu "
                "internalEnqueued=%llu internalResolved=%llu internalDispatched=%llu "
                "maxCommitBacklog=%llu maxQueueDepth=%llu "
                "maxReservations=%llu oldestCommittedSourceAgeMaxNs=%llu "
                "currentCommitBacklog=%lu activeReservations=%lu currentDepth=%lu "
                "oldestCommittedSource=%llu/%llu ageNs=%llu "
                "detailedBursts=%llu detailedFgBursts=%llu "
                "detailedFgSevereBursts=%llu "
                "detailedTitleHitches=%llu",
            reason ?: "unspecified", (unsigned long long)observed,
            (unsigned long long)fgCount, (unsigned long long)titleLowFps,
            (unsigned long long)censored,
            (unsigned long long)completedSampleCount,
            (unsigned long long)p50, (unsigned long long)p90,
            (unsigned long long)p99,
            (unsigned long long)fgP50, (unsigned long long)fgP90,
            (unsigned long long)fgP99,
            (unsigned long long)titleP50, (unsigned long long)titleP90,
            (unsigned long long)titleP99,
            (unsigned long long)maximumNs,
            (unsigned long long)maximumFgNs,
            (unsigned long long)maximumTitleNs,
            (unsigned long long)over50, (unsigned long long)over100,
            (unsigned long long)over250, (unsigned long long)over500,
            (unsigned long long)missingSlots,
            (unsigned long long)fgMissingSlots,
            (unsigned long long)totalNs,
            (unsigned long long)totalFgNs,
            (unsigned long long)totalTitleNs,
            (unsigned long long)totalCensoredNs,
            (unsigned long long)cadenceHoles, (unsigned long long)sourceCount,
            (unsigned long long)internalCount, cadenceRate,
            (unsigned long long)directCount,
            (unsigned long long)pump.sourceEnqueued,
            (unsigned long long)pump.sourceDispatched,
            (unsigned long long)pump.internalEnqueued,
            (unsigned long long)pump.internalResolved,
            (unsigned long long)pump.internalDispatched,
            (unsigned long long)maxCommitBacklog,
            (unsigned long long)maxQueueDepth,
            (unsigned long long)maxReservations,
            (unsigned long long)maxOldestCommittedAgeNs,
            (unsigned long)(pump.committedQueuedSourceCount +
                            pump.committedClaimedSourceCount),
            (unsigned long)pump.activeSourceReservations,
            (unsigned long)pump.depth,
            (unsigned long long)pump.oldestUndispatchedSourceOrdinal,
            (unsigned long long)pump.oldestUndispatchedSourceSequence,
            (unsigned long long)pump.oldestUndispatchedSourceAgeNs,
            (unsigned long long)detailedBursts,
            (unsigned long long)detailedFgBursts,
            (unsigned long long)detailedFgSevereBursts,
            (unsigned long long)detailedTitleHitches);
        free(completedDurations);
        free(fgDurations);
        free(titleDurations);
    });
}

// Called by the ON transition while r127RuntimeLock is held. No R137 path
// takes r127RuntimeLock while holding r137Lock, keeping this reset acyclic.
static void r137ResetForRuntimeOn(void) {
    if (!r137Enabled()) return;
    const R123DiagnosticSnapshot pumpBaseline = r123Pump
        ? [r123Pump diagnosticSnapshotAtNs:r105NowNs()]
        : (R123DiagnosticSnapshot){0};
    pthread_mutex_lock(&r137Lock);
    r137TraceRingHead = 0;
    r137TraceRingCount = 0;
    r137ActiveTraceCount = 0;
    r137ActiveTraceDropped = 0;
    r137ActiveOutage = (R137ActiveOutage){0};
    r137OutageSequence = 0;
    r137DetailedOutageCount = 0;
    r137DetailedFgOutageCount = 0;
    r137DetailedFgRoutineCount = 0;
    r137DetailedFgSevereCount = 0;
    r137DetailedTitleHitchCount = 0;
    r137ObservedOutageCount = 0;
    r137FgOutageCount = 0;
    r137TitleLowFpsCount = 0;
    r137CensoredOutageCount = 0;
    r137CadenceHoleCount = 0;
    r137TotalTimeWithoutInternalNs = 0;
    r137TotalFgOutageNs = 0;
    r137TotalTitleLowFpsNs = 0;
    r137TotalCensoredNs = 0;
    r137MaximumOutageNs = 0;
    r137MaximumFgOutageNs = 0;
    r137MaximumTitleLowFpsNs = 0;
    r137OutagesOver50Ms = 0;
    r137OutagesOver100Ms = 0;
    r137OutagesOver250Ms = 0;
    r137OutagesOver500Ms = 0;
    r137MaximumMissingInternalSlots = 0;
    r137MaximumFgMissingInternalSlots = 0;
    r137CadenceHolesSinceInternal = 0;
    r137GenerationAttemptsSinceInternal = 0;
    r137GeneratedInternalsSinceInternal = 0;
    r137MaximumCommitBacklog = 0;
    r137MaximumQueueDepth = 0;
    r137MaximumReservations = 0;
    r137MaximumOldestCommittedSourceAgeNs = 0;
    r137PhysicalSourceCount = 0;
    r137PhysicalInternalCount = 0;
    r137TitleDirectWhileOnCount = 0;
    r137TitleDirectDetailCount = 0;
    r137CompletedDurationCount = 0;
    r137FgDurationCount = 0;
    r137TitleDurationCount = 0;
    r137LastInternalNs = 0;
    r137LastInternalPresentId = 0;
    r137LastSourceNs = 0;
    r137LastPhysicalSourceNs = 0;
    r137LastPhysicalDisplayNs = 0;
    r137SourcesSinceInternal = 0;
    r137TitleDirectSinceInternal = 0;
    r137PumpSourcesSinceInternal = 0;
    r137MaxSourceGapSinceInternalNs = 0;
    r137MaxCommitBacklogSinceInternal =
        pumpBaseline.committedQueuedSourceCount +
        pumpBaseline.committedClaimedSourceCount;
    r137MaxQueueDepthSinceInternal = pumpBaseline.depth;
    r137MaxReservationsSinceInternal = pumpBaseline.activeSourceReservations;
    r137MaxOldestCommittedAgeSinceInternalNs =
        pumpBaseline.oldestUndispatchedSourceAgeNs;
    r137MaximumCommitBacklog = r137MaxCommitBacklogSinceInternal;
    r137MaximumQueueDepth = pumpBaseline.depth;
    r137MaximumReservations = pumpBaseline.activeSourceReservations;
    r137MaximumOldestCommittedSourceAgeNs =
        pumpBaseline.oldestUndispatchedSourceAgeNs;
    r137PumpAtLastInternal = pumpBaseline;
    r137LastThreadSampleNs = 0;
    r137LatestGenerationNs = 0;
    r137LatestEstimatorSubmitNs = 0;
    r137LatestEstimatorWakeNs = 0;
    r137LatestMetalFxStartNs = 0;
    r137LatestMetalFxEndNs = 0;
    pthread_mutex_unlock(&r137Lock);
    __atomic_store_n(&r137PumpWorkerState, R137PumpStateOff, __ATOMIC_RELAXED);
    __atomic_store_n(&r137LastPumpProgressNs, 0, __ATOMIC_RELAXED);
    r137ThreadDirectSequence = 0;
    r137ThreadDirectEpoch = 0;
    r137ThreadDirectPendingDispatches = 0;
    r137ThreadDirectReason = 0;
    logLine("R137_DIAGNOSTICS=ARMED outageThreshold=two-source-periods "
            "detailedFGBurstLimit=%u+%u-severe detailedTitleHitchLimit=%u "
            "detailTriggers=age-100ms,direct-after-50ms,committed-age-100ms traceLimit=%u "
            "traceClock=CLOCK_MONOTONIC outageClock=MTLDrawable.presentedTime-host-ns",
        kR137DetailedFgRoutineLimit, kR137DetailedFgSevereLimit,
        kR137DetailedTitleHitchLimit,
        kR137TracePerOutageCapacity);
}

static void r137FinalizeWindow(const char *reason) {
    if (!r137Enabled()) return;
    const R123DiagnosticSnapshot pump = r123Pump
        ? [r123Pump diagnosticSnapshotAtNs:r105NowNs()]
        : (R123DiagnosticSnapshot){0};
    R137ClosedOutage closed = {0};
    pthread_mutex_lock(&r137Lock);
    if (r137ActiveOutage.active)
        closed = r137CloseOutageLocked(r137LastPhysicalDisplayNs, NO, YES,
                                       pump);
    pthread_mutex_unlock(&r137Lock);
    r137EmitClosedOutage(closed);
    r137EmitSummary(reason);
}

// One shared-memory copy slot. The blit destination is recreated when the
// payload shape changes; `inflight` guards the copy in flight. Never memset:
// the object pointers are strong.
typedef struct {
    id<MTLTexture> tex;
    dispatch_group_t inflight;
    uint64_t seq;
    uint64_t pairA;
    uint64_t presentNs;
    BOOL valid;
} R136RingSlot;

@interface R136MissImage : NSObject
@property (nonatomic, copy) NSString *role;
@property (nonatomic) uint64_t seq;
@property (nonatomic) uint64_t pairA;
@property (nonatomic) uint64_t presentNs;
@property (nonatomic) NSUInteger width;
@property (nonatomic) NSUInteger height;
@property (nonatomic) uint32_t pixelFormat;
@property (nonatomic, strong) id<MTLTexture> tex;
@property (nonatomic, strong) dispatch_group_t inflight;
@property (nonatomic, copy) NSData *bytes;
@end
@implementation R136MissImage
@end

@interface R136MissEvent : NSObject
@property (nonatomic) uint64_t index;
@property (nonatomic) BOOL injected;
@property (nonatomic) BOOL captureArmed;
@property (nonatomic) uint64_t openNs;
@property (nonatomic) uint32_t missRun;
@property (nonatomic) uint64_t firstPairA;
@property (nonatomic) uint64_t firstPairB;
@property (nonatomic) uint64_t firstOrdinalA;
@property (nonatomic) uint64_t firstOrdinalB;
@property (nonatomic) uint64_t firstSlotNs;
@property (nonatomic) uint64_t readyNs;
@property (nonatomic) uint8_t missClass;
@property (nonatomic, copy) NSString *subReason;
@property (nonatomic) BOOL hitchFlag;
@property (nonatomic) uint32_t afterMissRun;
@property (nonatomic) BOOL historyValid;
@property (nonatomic) BOOL recentCopyAbort;
@property (nonatomic) uint64_t captureIntervalNs;
@property (nonatomic) uint64_t sourcePeriodNs;
@property (nonatomic) uint32_t depth;
@property (nonatomic) uint32_t prevOutcome;
@property (nonatomic) BOOL recoveryInternalPresented;
@property (nonatomic) uint64_t lastSourceSeq;
@property (nonatomic) NSMutableArray<R136MissImage *> *images;
@end
@implementation R136MissEvent
@end

static pthread_mutex_t r136Lock = PTHREAD_MUTEX_INITIALIZER;
static R136MissEvent *r136OpenEvent;
static R136RingSlot r136SourceRing[2];
static R136RingSlot r136InternalRing[2];
static int r136SourceRingIndex;
static int r136InternalRingIndex;
static id<MTLCommandQueue> r136CaptureQueue;
static dispatch_queue_t r136EncodeQueue;
static uint64_t r136EventCounter;

// Taxonomy counters (reset per runtime ON window).
static uint64_t r136MissEvents, r136MissNatural, r136MissInjected;
static uint64_t r136ClassA, r136ClassB, r136HitchFlags, r136AfterMissFlags;
static uint64_t r136BudgetSkips, r136LateDiscardsSeen, r136LateDiscardsCaptured;
static uint64_t r136EventsCaptured, r136ImagesWritten, r136BytesWritten;
// Capture budgets are per process (the brief bounds visual evidence per run).
static int64_t r136NaturalBudget = -1;
static int64_t r136InjectBudget = -1;
static BOOL r136CaptureStateResolved;

static void r136CaptureStateInit(void) {
    if (r136CaptureStateResolved) return;
    r136CaptureStateResolved = YES;
    const char *value = getenv("FGMETAL_R136_MISS_CAPTURE");
    const BOOL wanted = value && strcmp(value, "1") == 0;
    const char *limit = getenv("FGMETAL_R136_MISS_CAPTURE_LIMIT");
    const char *injectedLimit = getenv("FGMETAL_R136_INJECT_CAPTURE_LIMIT");
    r136NaturalBudget = limit ? atoll(limit) : 12;
    r136InjectBudget = injectedLimit ? atoll(injectedLimit) : 6;
    if (!wanted || !fgmetalEvidenceDiagnosticsAllowed()) {
        r136NaturalBudget = 0;
        r136InjectBudget = 0;
        logLine("R136_CAPTURE_STATE enabled=0 wanted=%d diagnosticsAllowed=%d",
            wanted ? 1 : 0, fgmetalEvidenceDiagnosticsAllowed() ? 1 : 0);
        return;
    }
    char probe[768] = {0};
    // The shared helper composes every file path; the one subdirectory is
    // created here so all R136 files land inside it.
    if (!fgmetalEvidencePath(probe, sizeof(probe), "miss-captures/.r136-armed-%d", getpid())) {
        r136NaturalBudget = 0;
        r136InjectBudget = 0;
        logLine("R136_CAPTURE_STATE enabled=0 reason=path-unavailable");
        return;
    }
    char directory[768] = {0};
    snprintf(directory, sizeof(directory), "%s/miss-captures",
             fgmetalEvidenceDirectory());
    if (mkdir(directory, 0700) != 0 && errno != EEXIST) {
        r136NaturalBudget = 0;
        r136InjectBudget = 0;
        logLine("R136_CAPTURE_STATE enabled=0 reason=mkdir-failed errno=%d path=%s",
            errno, directory);
        return;
    }
    r136EncodeQueue =
        dispatch_queue_create("fgmetal.r136.miss-capture-encode", DISPATCH_QUEUE_SERIAL);
    logLine("R136_CAPTURE_STATE enabled=1 dir=%s naturalBudget=%lld injectBudget=%lld",
        directory, r136NaturalBudget, r136InjectBudget);
}

static BOOL r136CaptureArmedLocked(void) {
    return (r136NaturalBudget > 0 || r136InjectBudget > 0 || r136OpenEvent != nil);
}

static dispatch_group_t r136SlotGroupLocked(R136RingSlot *slot) {
    if (!slot->inflight) slot->inflight = dispatch_group_create();
    return slot->inflight;
}

// Caller holds r136Lock. Ensures the slot holds a shared texture shaped like
// the payload; returns NO when allocation fails (the write is then skipped).
static BOOL r136SlotEnsureLocked(R136RingSlot *slot, id<MTLTexture> payload) {
    if (slot->tex && slot->tex.width == payload.width &&
        slot->tex.height == payload.height &&
        slot->tex.pixelFormat == payload.pixelFormat)
        return YES;
    MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
        texture2DDescriptorWithPixelFormat:payload.pixelFormat
        width:payload.width height:payload.height mipmapped:NO];
    descriptor.storageMode = MTLStorageModeShared;
    descriptor.usage = MTLTextureUsageShaderRead;
    id<MTLTexture> texture = [payload.device newTextureWithDescriptor:descriptor];
    if (!texture) return NO;
    slot->tex = texture;
    slot->valid = NO;
    return YES;
}

// Enqueue one payload copy. Called with no lock held; the command buffer
// retains the payload until completion, so the pump may release its queue
// item while the copy is in flight. `group` is entered here and left in the
// completion handler.
static void r136EncodeCopy(id<MTLTexture> payload, id<MTLTexture> destination,
                           dispatch_group_t group) {
    if (!payload || !destination || !group) return;
    // Final-review F5: the lazy queue init is reached from both the pump
    // thread (ring writes) and the estimator thread (late-discard claims);
    // create it under the leaf lock so the strong pointer cannot race.
    pthread_mutex_lock(&r136Lock);
    if (!r136CaptureQueue || r136CaptureQueue.device != payload.device) {
        r136CaptureQueue = [payload.device newCommandQueue];
    }
    id<MTLCommandQueue> queue = r136CaptureQueue;
    pthread_mutex_unlock(&r136Lock);
    (void)queue;
    dispatch_group_enter(group);
    id<MTLCommandBuffer> commandBuffer = queue ? [queue commandBuffer] : nil;
    id<MTLBlitCommandEncoder> encoder =
        commandBuffer ? [commandBuffer blitCommandEncoder] : nil;
    if (!commandBuffer || !encoder) {
        dispatch_group_leave(group);
        static uint64_t r136CopyFailures;
        if (++r136CopyFailures <= 8)
            logLine("R136_CAPTURE_COPY=FAILED reason=encoder-unavailable");
        return;
    }
    [encoder copyFromTexture:payload toTexture:destination];
    [encoder endEncoding];
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        (void)completed;
        dispatch_group_leave(group);
    }];
    [commandBuffer commit];
}

// Caller holds r136Lock. Decides whether this present is recorded into an
// open event and under which role. Returns nil when no image is wanted.
static R136MissImage *r136EventImageForPresentLocked(R136MissEvent *event,
                                                     const R123DisplayItem *item) {
    if (!event) return nil;
    // Final-review F1/F5: the cap is enforced here, under r136Lock, so the
    // estimator thread's late-discard claim cannot race the count; and each
    // claimed SOURCE gets its own role suffix (B, B2, B3...) so consecutive
    // pre-recovery SOURCEs can no longer collide on one PNG filename.
    if (event.images.count >= kR136MaxImagesPerEvent) return nil;
    if (item.kind == R123ItemKindInternal) {
        if (event.recoveryInternalPresented) return nil;
        event.recoveryInternalPresented = YES;
        R136MissImage *image = [R136MissImage new];
        image.role = @"M_next";
        image.seq = item.internalPresentId;
        image.pairA = item.seqA;
        return image;
    }
    if (event.recoveryInternalPresented) {
        R136MissImage *image = [R136MissImage new];
        image.role = @"C_next";
        image.seq = item.seqB;
        image.pairA = item.seqA;
        return image;
    }
    // A SOURCE presented while the hole is still open: the frame that fills
    // the missed slot (B), and further SOURCEs for consecutive misses (B2..).
    NSUInteger sourceClaims = 0;
    for (R136MissImage *existing in event.images) {
        if ([existing.role isEqualToString:@"C_next"]) break;
        if ([existing.role hasPrefix:@"B"]) ++sourceClaims;
    }
    R136MissImage *image = [R136MissImage new];
    image.role = sourceClaims == 0
        ? @"B" : [NSString stringWithFormat:@"B%lu", (unsigned long)(sourceClaims + 1)];
    image.seq = item.seqB;
    image.pairA = item.seqA;
    return image;
}

static NSString *r136ClassName(uint8_t cls) {
    return cls == 'A' ? @"A" : cls == 'B' ? @"B" : @"?";
}

// IEEE 754 half -> float, local to the capture converter (the R114 diagnostic
// has its own copy later in the file; keep the sections independent).
static float r136HalfToFloat(uint16_t value) {
    const uint32_t sign = (value & 0x8000u) << 16;
    const uint32_t exponent = (value & 0x7C00u) >> 10;
    const uint32_t mantissa = value & 0x03FFu;
    if (exponent == 0) {
        if (mantissa == 0) return (float)(sign ? -0.0f : 0.0f);
        // subnormal: normalize
        uint32_t e = 0;
        uint32_t m = mantissa;
        while (!(m & 0x0400u)) { m <<= 1; ++e; }
        m &= 0x03FFu;
        const uint32_t bits = sign | ((127 - 15 - e) << 23) | (m << 13);
        float out;
        memcpy(&out, &bits, sizeof(out));
        return out;
    }
    if (exponent == 0x1Fu) {
        const uint32_t bits = sign | 0x7F800000u | (mantissa << 13);
        float out;
        memcpy(&out, &bits, sizeof(out));
        return out;
    }
    const uint32_t bits = sign | ((exponent - 15 + 127) << 23) | (mantissa << 13);
    float out;
    memcpy(&out, &bits, sizeof(out));
    return out;
}

// Serial encode queue: claims the bytes of every claimed image, writes one
// lossless PNG per frame and the JSON sidecar, and publishes the counters.
static void r136WriteEventFiles(R136MissEvent *event) {
    @autoreleasepool {
        r136CaptureStateInit();
        NSMutableDictionary *meta = [NSMutableDictionary dictionary];
        meta[@"idx"] = @(event.index);
        meta[@"injected"] = @(event.injected);
        meta[@"missClass"] = r136ClassName(event.missClass);
        meta[@"subreason"] = event.subReason ?: @"";
        meta[@"missRun"] = @(event.missRun);
        meta[@"afterMissRun"] = @(event.afterMissRun);
        meta[@"hitch"] = @(event.hitchFlag);
        meta[@"historyValid"] = @(event.historyValid);
        meta[@"recentCopyAbort"] = @(event.recentCopyAbort);
        meta[@"pairA"] = @(event.firstPairA);
        meta[@"pairB"] = @(event.firstPairB);
        meta[@"sourcePresentOrdinalA"] = @(event.firstOrdinalA);
        meta[@"sourcePresentOrdinalB"] = @(event.firstOrdinalB);
        meta[@"slotNs"] = @(event.firstSlotNs);
        meta[@"readyNs"] = @(event.readyNs);
        meta[@"captureIntervalNs"] = @(event.captureIntervalNs);
        meta[@"sourcePeriodNs"] = @(event.sourcePeriodNs);
        meta[@"queueDepth"] = @(event.depth);
        meta[@"prevOutcome"] = @(event.prevOutcome);
        meta[@"openNs"] = @(event.openNs);
        meta[@"recoveryInternalPresented"] = @(event.recoveryInternalPresented);
        meta[@"clock"] = @"CLOCK_MONOTONIC";
        NSMutableArray *manifest = [NSMutableArray array];
        uint64_t bytes = 0;
        NSUInteger written = 0;
        for (R136MissImage *image in event.images) {
            if (image.inflight)
                dispatch_group_wait(image.inflight,
                    dispatch_time(DISPATCH_TIME_NOW, (int64_t)kR136EventTimeoutNs));
            if (!image.tex) continue;
            // The INTERNAL payloads are MetalFX outputs in RGBA16Float
            // (8 bytes/pixel, linear-light); SOURCE payloads are 8-bit BGRA.
            // The read and the conversion must follow the actual format or
            // getBytes fails validation and silently yields a black frame.
            NSUInteger bytesPerPixel = 4;
            BOOL sourceBGRA = NO;
            BOOL halfFloat = NO;
            switch (image.pixelFormat) {
                case MTLPixelFormatBGRA8Unorm:
                case MTLPixelFormatBGRA8Unorm_sRGB:
                    bytesPerPixel = 4; sourceBGRA = YES; break;
                case MTLPixelFormatRGBA8Unorm:
                case MTLPixelFormatRGBA8Unorm_sRGB:
                    bytesPerPixel = 4; break;
                case MTLPixelFormatRGBA16Float:
                    bytesPerPixel = 8; halfFloat = YES; break;
                default:
                    logLine("R136_CAPTURE_COPY=FAILED reason=unsupported-format fmt=%lu",
                        (unsigned long)image.pixelFormat);
                    continue;
            }
            const NSUInteger srcBytesPerRow = image.width * bytesPerPixel;
            NSMutableData *srcRaw = [NSMutableData dataWithLength:
                srcBytesPerRow * image.height];
            if (!srcRaw) continue;
            [image.tex getBytes:srcRaw.mutableBytes bytesPerRow:srcBytesPerRow
                     fromRegion:MTLRegionMake2D(0, 0, image.width, image.height)
                     mipmapLevel:0];
            // Normalized evidence format: RGBA8, sRGB-encoded, alpha opaque.
            // 8-bit BGRA/RGBA rows only swizzle; 16F rows are half4 linear
            // values converted through the sRGB transfer function so a
            // generated midpoint compares 1:1 against the drawable's own
            // sRGB-encoded SOURCE frames.
            const NSUInteger bytesPerRow = image.width * 4;
            NSMutableData *raw = [NSMutableData dataWithLength:
                bytesPerRow * image.height];
            if (!raw) continue;
            const uint8_t *src = (const uint8_t *)srcRaw.bytes;
            uint8_t *pixels = (uint8_t *)raw.mutableBytes;
            for (NSUInteger y = 0; y < image.height; ++y) {
                const uint8_t *row = src + y * srcBytesPerRow;
                uint8_t *out = pixels + y * bytesPerRow;
                for (NSUInteger x = 0; x < image.width; ++x) {
                    if (halfFloat) {
                        const uint16_t *px = (const uint16_t *)(row + x * 8);
                        for (int c = 0; c < 3; ++c) {
                            float v = r136HalfToFloat(px[c]);
                            v = v <= 0.0f ? 0.0f : (v >= 1.0f ? 1.0f : v);
                            // linear -> sRGB (the piecewise-approximated form)
                            const float s = v <= 0.0031308f
                                ? v * 12.92f
                                : 1.055f * powf(v, 1.0f / 2.4f) - 0.055f;
                            out[x * 4 + c] = (uint8_t)(s * 255.0f + 0.5f);
                        }
                        out[x * 4 + 3] = 255;
                    } else {
                        const uint8_t b0 = row[x * 4], b1 = row[x * 4 + 1];
                        const uint8_t b2 = row[x * 4 + 2];
                        if (sourceBGRA) {
                            out[x * 4] = b2; out[x * 4 + 1] = b1;
                            out[x * 4 + 2] = b0;
                        } else {
                            out[x * 4] = b0; out[x * 4 + 1] = b1;
                            out[x * 4 + 2] = b2;
                        }
                        out[x * 4 + 3] = 255;
                    }
                }
            }
            image.bytes = raw;
            image.tex = nil;
            char path[768] = {0};
            if (!fgmetalEvidencePath(path, sizeof(path),
                    "miss-captures/miss-%d-%02llu-%s.png", getpid(),
                    (unsigned long long)event.index,
                    image.role.UTF8String ?: "unknown"))
                continue;
            CFMutableDataRef pngData = CFDataCreateMutable(NULL, 0);
            CGDataConsumerRef pngConsumer = CGDataConsumerCreateWithCFData(pngData);
            CGColorSpaceRef space = CGColorSpaceCreateDeviceRGB();
            CGContextRef context = CGBitmapContextCreate(
                (void *)raw.bytes, image.width, image.height, 8, bytesPerRow,
                space, kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big);
            BOOL saved = NO;
            if (pngConsumer && context) {
                CGImageRef cgImage = CGBitmapContextCreateImage(context);
                if (cgImage) {
                    CGImageDestinationRef destination =
                        CGImageDestinationCreateWithData(pngData,
                            CFSTR("public.png"), 1, NULL);
                    if (destination) {
                        CGImageDestinationAddImage(destination, cgImage, NULL);
                        saved = CGImageDestinationFinalize(destination);
                        CFRelease(destination);
                    }
                    CFRelease(cgImage);
                }
            }
            if (context) CFRelease(context);
            if (space) CFRelease(space);
            if (pngConsumer) CFRelease(pngConsumer);
            // Length is read before release: the buffer must never be
            // touched after its last reference is dropped.
            const CFIndex pngLength = saved ? CFDataGetLength(pngData) : 0;
            if (saved && pngLength > 0) {
                saved = fgmetalWriteEvidenceAbsolute(path, CFDataGetBytePtr(pngData),
                    (size_t)pngLength);
            }
            CFRelease(pngData);
            bytes += saved ? (uint64_t)pngLength : 0;
            if (saved) ++written;
            [manifest addObject:@{@"role": image.role ?: @"?",
                @"seq": @(image.seq), @"pairA": @(image.pairA),
                @"presentNs": @(image.presentNs),
                @"width": @(image.width), @"height": @(image.height),
                @"file": [NSString stringWithUTF8String:path],
                @"saved": @(saved)}];
        }
        meta[@"images"] = manifest;
        char metaPath[768] = {0};
        if (fgmetalEvidencePath(metaPath, sizeof(metaPath),
                "miss-captures/miss-%d-%02llu-meta.json", getpid(),
                (unsigned long long)event.index)) {
            NSError *error = nil;
            NSData *json = [NSJSONSerialization dataWithJSONObject:meta
                options:NSJSONWritingPrettyPrinted error:&error];
            if (json)
                fgmetalWriteEvidenceAbsolute(metaPath, json.bytes, json.length);
        }
        pthread_mutex_lock(&r136Lock);
        r136ImagesWritten += written;
        r136BytesWritten += bytes;
        pthread_mutex_unlock(&r136Lock);
        logLine("R136_EVENT_FILES idx=%llu images=%lu written=%lu bytes=%llu",
            (unsigned long long)event.index, (unsigned long)event.images.count,
            (unsigned long)written, (unsigned long long)bytes);
    }
}

static void r136CloseEventLocked(R136MissEvent *event, const char *reason,
                                 uint64_t nowNs) {
    // Caller holds r136Lock and has already removed the event from the open
    // slot. Emits the recovery line and hands the files to the encode queue.
    logLine("R136_MISS_RECOVERY idx=%llu reason=%s missRun=%u recoveryInternal=%d "
            "nextSourceSeq=%llu openNs=%llu closedNs=%llu images=%lu",
        (unsigned long long)event.index, reason, event.missRun,
        event.recoveryInternalPresented ? 1 : 0,
        (unsigned long long)event.lastSourceSeq,
        (unsigned long long)event.openNs, (unsigned long long)nowNs,
        (unsigned long)event.images.count);
    if (event.captureArmed && r136EncodeQueue)
        dispatch_async(r136EncodeQueue, ^{ r136WriteEventFiles(event); });
}

static void r136OnMiss(const R123DisplayItem *missed,
                       const R123DisplayItem *presentedNext, uint64_t slotNs) {
    if (!missed) return;
    const uint64_t nowNs = r105NowNs();
    R134PairRecord recB, recA;
    r134PeekPair(missed.seqB, &recB);
    r134PeekPair(missed.seqA, &recA);
    // Fault-injection snapshot: leaf lock, taken and released alone. The
    // injected window spans consume-until-recovery: the fault counter hits
    // zero at generation time but the injected pair's miss lands at its
    // presentation slot afterwards, while recovery is still pending.
    uint64_t faultRemaining = 0;
    BOOL faultRecoveryPending = NO;
    pthread_mutex_lock(&r127RuntimeLock);
    faultRemaining = r127FaultPairsRemaining;
    faultRecoveryPending = r127FaultRecoveryPending;
    pthread_mutex_unlock(&r127RuntimeLock);
    const BOOL injected = faultRemaining > 0 || faultRecoveryPending;

    uint8_t cls = '?';
    NSString *sub = @"unknown";
    if (recB.genStartNs == 0) {
        cls = 'B';
        sub = recB.copyCompleteNs ? @"no-generation-entry" : @"capture-incomplete";
    } else {
        cls = 'A';
        if (recB.mfxEndNs)
            sub = recB.mfxEndNs > slotNs ? @"completed-late"
                                         : @"completed-before-slot";
        else if (recB.estSubmitNs) sub = @"in-flight-at-slot";
        else sub = @"started-not-submitted";
    }
    const uint64_t captureInterval =
        (recA.copyCompleteNs && recB.copyCompleteNs &&
         recB.copyCompleteNs > recA.copyCompleteNs)
            ? recB.copyCompleteNs - recA.copyCompleteNs : 0;
    uint64_t period = 0;
    NSUInteger depth = 0;
    if (r123Pump) {
        period = [r123Pump sourcePeriodNs];
        depth = [r123Pump depth];
    }
    const uint64_t hitchFloor = period ? 2 * period
                                       : 3 * kR136HalfPeriodNsDefault;
    const BOOL hitch = captureInterval > hitchFloor;
    // Recovery-after-miss depth: walk back through consecutive missed pairs.
    uint32_t afterMissRun = 0;
    if (recA.outcome == 2) {
        uint64_t seq = missed.seqA;
        for (uint32_t i = 0; i < 8; ++i) {
            ++afterMissRun;
            R134PairRecord earlier;
            r134PeekPair(seq - 1, &earlier);
            if (earlier.sequence != seq - 1 || earlier.outcome != 2) break;
            seq -= 1;
        }
    }
    const uint64_t lastAbort = __atomic_load_n(&r136LastCopyAbortNs, __ATOMIC_RELAXED);
    const BOOL recentAbort = lastAbort && nowNs > lastAbort &&
        nowNs - lastAbort < 250000000ULL;
    const BOOL historyValid = realFrameProvider ? [realFrameProvider historyValid] : NO;
    const int64_t readyMinusSlot = recB.mfxEndNs
        ? (int64_t)(recB.mfxEndNs - slotNs) : 0;

    pthread_mutex_lock(&r136Lock);
    ++r136MissEvents;
    if (cls == 'A') ++r136ClassA; else if (cls == 'B') ++r136ClassB;
    if (hitch) ++r136HitchFlags;
    if (afterMissRun) ++r136AfterMissFlags;
    if (injected) ++r136MissInjected; else ++r136MissNatural;

    R136MissEvent *event = r136OpenEvent;
    BOOL openNew = NO;
    if (event) {
        ++event.missRun;
    } else {
        const int64_t budget = injected ? r136InjectBudget : r136NaturalBudget;
        if (budget > 0) {
            if (injected) --r136InjectBudget; else --r136NaturalBudget;
            openNew = YES;
        } else {
            ++r136BudgetSkips;
        }
    }
    const BOOL captureArmed = r136CaptureArmedLocked();
    pthread_mutex_unlock(&r136Lock);

    pthread_mutex_lock(&r136Lock);
    if (openNew) {
        event = [R136MissEvent new];
        event.index = ++r136EventCounter;
        event.injected = injected;
        event.captureArmed = YES; // only opens when a capture budget existed
        event.openNs = nowNs;
        event.missRun = 1;
        event.firstPairA = missed.seqA;
        event.firstPairB = missed.seqB;
        event.firstOrdinalA = missed.sourcePresentOrdinalA;
        event.firstOrdinalB = missed.sourcePresentOrdinalB;
        event.firstSlotNs = slotNs;
        event.readyNs = recB.mfxEndNs;
        event.missClass = cls;
        event.subReason = sub;
        event.hitchFlag = hitch;
        event.afterMissRun = afterMissRun;
        event.historyValid = historyValid;
        event.recentCopyAbort = recentAbort;
        event.captureIntervalNs = captureInterval;
        event.sourcePeriodNs = period;
        event.depth = (uint32_t)depth;
        event.prevOutcome = recA.outcome;
        event.images = [NSMutableArray array];
        r136OpenEvent = event;
        ++r136EventsCaptured;
        // Claim SOURCE A from the ring: the newest valid shared copy of the
        // frame presented before the miss. Claiming evicts the slot, so the
        // live stream cannot overwrite bytes this event has not read yet.
        R136RingSlot *claimed = nil;
        for (int i = 0; i < 2; ++i) {
            R136RingSlot *slot = &r136SourceRing[i];
            if (slot->valid && slot->seq == missed.seqA) { claimed = slot; break; }
        }
        if (!claimed)
            for (int i = 0; i < 2; ++i) {
                R136RingSlot *slot = &r136SourceRing[i];
                if (slot->valid) claimed = slot;
            }
        if (claimed) {
            R136MissImage *image = [R136MissImage new];
            image.role = @"A";
            image.seq = claimed->seq;
            image.pairA = claimed->pairA;
            image.presentNs = claimed->presentNs;
            image.width = claimed->tex.width;
            image.height = claimed->tex.height;
            image.pixelFormat = claimed->tex.pixelFormat;
            image.tex = claimed->tex;
            image.inflight = claimed->inflight;
            [event.images addObject:image];
            claimed->tex = nil;
            claimed->valid = NO;
        }
    } else if (event) {
        // Consecutive miss inside an open window: keep the classification of
        // the first event, extend the run.
        event.lastSourceSeq = 0;
    }
    // Final-review F7: the record is emitted after the open/extend decision
    // so a window-opening miss logs the real event index and joins with the
    // capture sidecars.
    const uint32_t loggedMissRun = event ? event.missRun : 1u;
    const uint64_t loggedIdx = event ? event.index : 0;
    pthread_mutex_unlock(&r136Lock);

    logLine("R136_MISS_EVENT idx=%llu source=%s class=%c subreason=%s "
            "pairA=%llu pairB=%llu ordinalA=%llu ordinalB=%llu slotNs=%llu "
            "readyNs=%llu readyMinusSlotNs=%lld captureIntervalNs=%llu "
            "sourcePeriodNs=%llu depth=%lu prevOutcome=%u afterMissRun=%u "
            "hitch=%d historyValid=%d recentCopyAbort=%d missRun=%u capture=%d",
        (unsigned long long)loggedIdx,
        injected ? "INJECTED" : "NATURAL", cls, sub.UTF8String,
        (unsigned long long)missed.seqA, (unsigned long long)missed.seqB,
        (unsigned long long)missed.sourcePresentOrdinalA,
        (unsigned long long)missed.sourcePresentOrdinalB,
        (unsigned long long)slotNs, (unsigned long long)recB.mfxEndNs,
        (long long)readyMinusSlot, (unsigned long long)captureInterval,
        (unsigned long long)period, (unsigned long)depth,
        recA.outcome, afterMissRun, hitch ? 1 : 0, historyValid ? 1 : 0,
        recentAbort ? 1 : 0, loggedMissRun, captureArmed ? 1 : 0);
}

static void r136OnPresented(const R123DisplayItem *item, id<MTLTexture> payload,
                            uint64_t slotNs) {
    if (!item || !payload) return;
    const uint64_t nowNs = r105NowNs();
    r136CaptureStateInit();
    pthread_mutex_lock(&r136Lock);
    R136MissEvent *event = r136OpenEvent;
    if (event && nowNs - event.openNs > kR136EventTimeoutNs) {
        r136OpenEvent = nil;
        r136CloseEventLocked(event, "timeout", nowNs);
        event = nil;
    }
    const BOOL armed = r136CaptureArmedLocked();
    R136MissImage *wanted = event ? r136EventImageForPresentLocked(event, item) : nil;
    pthread_mutex_unlock(&r136Lock);

    if (wanted) {
        // The per-event cap was already enforced under r136Lock inside
        // r136EventImageForPresentLocked (delta-review R1: do not re-check
        // the count unlocked here).
        if (payload.width > 0) {
            MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:payload.pixelFormat
                width:payload.width height:payload.height mipmapped:NO];
            descriptor.storageMode = MTLStorageModeShared;
            descriptor.usage = MTLTextureUsageShaderRead;
            id<MTLTexture> destination =
                [payload.device newTextureWithDescriptor:descriptor];
            if (destination) {
                dispatch_group_t group = dispatch_group_create();
                wanted.seq = item.kind == R123ItemKindInternal
                    ? item.internalPresentId : item.seqB;
                wanted.pairA = item.seqA;
                wanted.presentNs = nowNs;
                wanted.width = payload.width;
                wanted.height = payload.height;
                wanted.pixelFormat = payload.pixelFormat;
                wanted.tex = destination;
                wanted.inflight = group;
                r136EncodeCopy(payload, destination, group);
                pthread_mutex_lock(&r136Lock);
                [event.images addObject:wanted];
                if (item.kind == R123ItemKindSource)
                    event.lastSourceSeq = item.seqB;
                pthread_mutex_unlock(&r136Lock);
            }
        }
        pthread_mutex_lock(&r136Lock);
        if (item.kind == R123ItemKindSource && event.recoveryInternalPresented) {
            r136OpenEvent = nil;
            r136CloseEventLocked(event, "window-complete", nowNs);
        }
        pthread_mutex_unlock(&r136Lock);
    }

    if (!armed) return;
    // Ring maintenance: one async shared copy of every presented payload so
    // the frame before a future miss is still inspectable at detection time.
    pthread_mutex_lock(&r136Lock);
    R136RingSlot *slots = item.kind == R123ItemKindSource
        ? r136SourceRing : r136InternalRing;
    int *index = item.kind == R123ItemKindSource ? &r136SourceRingIndex
                                                 : &r136InternalRingIndex;
    R136RingSlot *slot = &slots[(*index + 1) % 2];
    *index = (*index + 1) % 2;
    if (!r136SlotEnsureLocked(slot, payload)) {
        pthread_mutex_unlock(&r136Lock);
        return;
    }
    slot->seq = item.kind == R123ItemKindSource ? item.seqB : item.internalPresentId;
    slot->pairA = item.seqA;
    slot->presentNs = nowNs;
    dispatch_group_t group = r136SlotGroupLocked(slot);
    id<MTLTexture> destination = slot->tex;
    pthread_mutex_unlock(&r136Lock);
    r136EncodeCopy(payload, destination, group);
    pthread_mutex_lock(&r136Lock);
    slot->valid = YES;
    pthread_mutex_unlock(&r136Lock);
}

static void r136OnLateDiscard(uint64_t pairA, uint64_t pairB,
                              id<MTLTexture> texture) {
    if (!texture) return;
    const uint64_t nowNs = r105NowNs();
    r136CaptureStateInit();
    pthread_mutex_lock(&r136Lock);
    ++r136LateDiscardsSeen;
    R136MissEvent *event = r136OpenEvent;
    if (event && event.firstPairB == pairB &&
        event.images.count < kR136MaxImagesPerEvent) {
        R136MissImage *image = [R136MissImage new];
        image.role = @"lateM";
        image.seq = pairB;
        image.pairA = pairA;
        image.presentNs = nowNs;
        image.width = texture.width;
        image.height = texture.height;
        image.pixelFormat = texture.pixelFormat;
        dispatch_group_t group = dispatch_group_create();
        image.inflight = group;
        MTLTextureDescriptor *descriptor = [MTLTextureDescriptor
            texture2DDescriptorWithPixelFormat:texture.pixelFormat
            width:texture.width height:texture.height mipmapped:NO];
        descriptor.storageMode = MTLStorageModeShared;
        descriptor.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> destination =
            [texture.device newTextureWithDescriptor:descriptor];
        if (destination) {
            image.tex = destination;
            [event.images addObject:image];
            ++r136LateDiscardsCaptured;
            pthread_mutex_unlock(&r136Lock);
            r136EncodeCopy(texture, destination, group);
            return;
        }
    }
    pthread_mutex_unlock(&r136Lock);
}

static void r136EmitMissSummary(const char *reason) {
    pthread_mutex_lock(&r136Lock);
    const uint64_t events = r136MissEvents, natural = r136MissNatural,
        injected = r136MissInjected, classA = r136ClassA, classB = r136ClassB,
        hitch = r136HitchFlags, afterMiss = r136AfterMissFlags,
        skips = r136BudgetSkips, lateSeen = r136LateDiscardsSeen,
        lateCaptured = r136LateDiscardsCaptured,
        captured = r136EventsCaptured, images = r136ImagesWritten,
        bytes = r136BytesWritten;
    const BOOL open = r136OpenEvent != nil;
    pthread_mutex_unlock(&r136Lock);
    logLine("R136_MISS_SUMMARY reason=%s events=%llu natural=%llu injected=%llu "
            "classA=%llu classB=%llu hitchFlags=%llu afterMissFlags=%llu "
            "budgetSkips=%llu lateDiscardsSeen=%llu lateDiscardsCaptured=%llu "
            "capturedEvents=%llu imagesWritten=%llu bytesWritten=%llu openEvent=%d",
        reason, (unsigned long long)events, (unsigned long long)natural,
        (unsigned long long)injected, (unsigned long long)classA,
        (unsigned long long)classB, (unsigned long long)hitch,
        (unsigned long long)afterMiss, (unsigned long long)skips,
        (unsigned long long)lateSeen, (unsigned long long)lateCaptured,
        (unsigned long long)captured, (unsigned long long)images,
        (unsigned long long)bytes, open ? 1 : 0);
}

// Called from the runtime ON transition while r127RuntimeLock is held; the
// lock order above makes taking r136Lock here safe.
static void r136ResetForRuntimeOn(void) {
    pthread_mutex_lock(&r136Lock);
    r136OpenEvent = nil; // any open window is abandoned; its images are dropped
    r136MissEvents = 0;
    r136MissNatural = 0;
    r136MissInjected = 0;
    r136ClassA = 0;
    r136ClassB = 0;
    r136HitchFlags = 0;
    r136AfterMissFlags = 0;
    r136BudgetSkips = 0;
    r136LateDiscardsSeen = 0;
    r136LateDiscardsCaptured = 0;
    pthread_mutex_unlock(&r136Lock);
}

// Publish only a completed private copy. This is readiness, not an ownership// decision or a queue admission; the estimator receives it only after the
// presentation hook binds it to a terminal SourcePresentOrdinal.
static void r123EnqueueSourceDetailed(uint64_t sequence, uint64_t appFrameId,
                                      id<MTLTexture> texture,
                                      uint64_t captureEpoch,
                                      uint64_t drawableID,
                                      NSString *condition,
                                      BOOL generationOnly) {
    if (!r123PumpEnabled() || !texture || !sequence) return;
    pthread_mutex_lock(&r127RuntimeLock);
    const R127SourceOwner owner = r127SourceOwnerForSequence(sequence);
    const BOOL acquisitionStillActive =
        r130HasActiveTicketForSequenceLocked(sequence);
    if (owner == R127SourceOwnerUnknown && acquisitionStillActive &&
        captureEpoch == r127RuntimeEpochSnapshot()) {
        // Estimator staging is keyed only by AcquisitionId and accepts capture
        // completion in either order. The one-entry ownership cache remains a
        // separate optimization for the newest acquisition eligible for FG
        // suppression.
        (void)r132StageCapture(sequence, drawableID, texture.pixelFormat,
            texture, texture.device, condition, captureEpoch, generationOnly);
        if (sequence >= r127CachedSourceSequence) {
            r127CachedSourceTexture = texture;
            r127CachedSourceSequence = sequence;
            r130CachedSourceEpoch = captureEpoch;
            r124NoteCapture(sequence, texture);
        }
    } else {
        ++r127StaleCallbackRejections;
        r132NoteLateCapture(sequence);
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    (void)appFrameId;
}

static void r123EnqueueSource(uint64_t sequence, uint64_t appFrameId,
                              id<MTLTexture> texture, uint64_t captureEpoch) {
    r123EnqueueSourceDetailed(sequence, appFrameId, texture, captureEpoch,
        sequence, @"R132_PRESENTATION_ORDERED",
        r122OverlapEnabled() || r127RuntimeCanGenerate());
}

static void r132ClearCachedSource(uint64_t sequence) {
    if (!sequence) return;
    pthread_mutex_lock(&r127RuntimeLock);
    if (r127CachedSourceSequence == sequence) {
        r127CachedSourceTexture = nil;
        r127CachedSourceSequence = 0;
        r130CachedSourceEpoch = 0;
    }
    pthread_mutex_unlock(&r127RuntimeLock);
}

static void r123ResolveInternal(uint64_t a, uint64_t b, id<MTLTexture> texture,
                                uint64_t internalPresentId, uint64_t epoch) {
    if (!r123PumpEnabled() || !texture) return;
    const uint64_t ns = r105NowNs();
    NSUInteger depth = 0;
    pthread_mutex_lock(&r127RuntimeLock);
    const BOOL current = r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
        r127RuntimeEpochSnapshot() == epoch;
    const BOOL filled = current && r123Pump &&
        [r123Pump resolveInternalForA:a b:b internalPresentId:internalPresentId
                              payload:texture atNs:ns runtimeEpoch:epoch];
    if (!current) ++r127StaleCallbackRejections;
    if (r123Pump) depth = [r123Pump depth];
    pthread_mutex_unlock(&r127RuntimeLock);
    // R134: the instant the generated image became offerable to the pump. This
    // is the "INTERNAL offered" point in the pair timeline; whether the pump
    // then accepts it is a separate question the record also captures.
    r134NoteResolved(a, b, ns);
    r137TracePoint(R137EventInternalResolved, b, a, b, 0,
                   (current ? 1u : 0u) | (filled ? 2u : 0u),
                   internalPresentId, depth);
    logLine("R123_INTERNAL_ENQUEUED pair=%llu->%llu internalPresentId=%llu texture=%p "
            "filled=%d depth=%lu runtimeEpoch=%llu reason=%s",
        (unsigned long long)a, (unsigned long long)b,
        (unsigned long long)internalPresentId, (__bridge void *)texture,
        filled ? 1 : 0, (unsigned long)depth, (unsigned long long)epoch,
        !current ? "stale-runtime-epoch" :
            (filled ? "placeholder-resolved" : "placeholder-already-dropped"));
    // R136: a generated image that arrives after its placeholder was dropped
    // at the slot is the class-A "completed-late" evidence — preserve it if a
    // capture window for this pair is open.
    if (current && !filled) {
        r137TracePoint(R137EventInternalLateDiscard, b, a, b, 0, 0,
                       internalPresentId, ns);
        r136OnLateDiscard(a, b, texture);
    }
}

static uint32_t r127RuntimeModeSnapshot(void) {
    return __atomic_load_n(&r127RuntimeMode, __ATOMIC_ACQUIRE);
}

static BOOL r127RuntimeCanPresent(void) {
    return r123PumpEnabled() &&
        __atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE) &&
        r127RuntimeModeSnapshot() != R127RuntimeModeOff;
}

static BOOL r127RuntimeCanGenerate(void) {
    return r123PumpEnabled() &&
        __atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE) &&
        r127RuntimeModeSnapshot() == R127RuntimeModeGenerating;
}

static uint64_t r127RuntimeEpochSnapshot(void) {
    return __atomic_load_n(&r127RuntimeEpoch, __ATOMIC_ACQUIRE);
}

static BOOL r127TakeControlFile(const char *path, char *contents, size_t capacity) {
    static uint64_t claimSequence;
    if (!path || !contents || capacity < 2) return NO;
    const uint64_t claimId = __atomic_add_fetch(&claimSequence, 1, __ATOMIC_RELAXED);
    char claimed[512];
    const int written = snprintf(claimed, sizeof(claimed), "%s.claim.%d.%llu",
        path, getpid(), (unsigned long long)claimId);
    if (written <= 0 || (size_t)written >= sizeof(claimed) ||
        rename(path, claimed) != 0) return NO;
    const int fd = open(claimed, O_RDONLY);
    ssize_t readCount = fd >= 0 ? read(fd, contents, capacity - 1) : -1;
    if (fd >= 0) close(fd);
    (void)unlink(claimed);
    if (readCount <= 0) return NO;
    contents[readCount < (ssize_t)capacity ? (size_t)readCount : capacity - 1] = '\0';
    return YES;
}

static R123RuntimeEpochResult r127AdvanceRuntimeEpochLocked(uint32_t newMode) {
    R123RuntimeEpochResult result = {0};
    if (r127RuntimeEpoch == UINT64_MAX) return result;
    const uint64_t epoch = __atomic_add_fetch(&r127RuntimeEpoch, 1, __ATOMIC_ACQ_REL);
    __atomic_store_n(&r127RuntimeMode, newMode, __ATOMIC_RELEASE);
    if (r123Pump) {
        if (newMode == R127RuntimeModeSourceOnlyDrain)
            result = [r123Pump beginSourceOnlyEpoch:epoch];
        else
            result = [r123Pump setRuntimeEnabled:
                newMode == R127RuntimeModeGenerating epoch:epoch];
    }

    if (r127PumpItemInFlight && r123Pump) {
        R123DisplayItem *item = r127PumpItemInFlight;
        BOOL retained = newMode == R127RuntimeModeSourceOnlyDrain &&
            item.kind == R123ItemKindSource &&
            [r123Pump requeueUndispatchedSource:item forEpoch:epoch];
        if (!retained) {
            [r123Pump rejectUndispatchedItem:item];
            ++r127StaleCallbackRejections;
        }
        r127PumpItemInFlight = nil;
    }
    return result;
}

// Caller holds r127RuntimeLock and has verified the WSI dispatch frontier.
static R123RuntimeEpochResult r127FinishDisableToTitleLocked(uint64_t *outEpoch) {
    R123RuntimeEpochResult result = {0};
    if (r127RuntimeEpoch == UINT64_MAX || r130PendingFGDispatches ||
        r127PumpItemInFlight || (r123Pump && [r123Pump activeSourceReservations]))
        return result;
    const uint64_t epoch = r127RuntimeEpoch + 1;
    if (r123Pump) {
        result = [r123Pump endSourceOnlyEpochForTitleDirect:epoch];
        if ([r123Pump runtimeEpoch] != epoch) return result;
    }
    __atomic_store_n(&r127RuntimeEpoch, epoch, __ATOMIC_RELEASE);
    __atomic_store_n(&r127RuntimeMode, R127RuntimeModeOff, __ATOMIC_RELEASE);
    if (outEpoch) *outEpoch = epoch;
    return result;
}

static void r127PrepareRuntimeControl(void) {
    BOOL ready = mkdir(kR127RuntimeDirectory, 0700) == 0 || errno == EEXIST;
    struct stat directoryInfo;
    if (!ready || lstat(kR127RuntimeDirectory, &directoryInfo) != 0 ||
        !S_ISDIR(directoryInfo.st_mode) || directoryInfo.st_uid != getuid()) {
        ready = NO;
    } else {
        if (chmod(kR127RuntimeDirectory, 0700) != 0) ready = NO;
    }
    if (ready) {
        if (unlink(kR127RuntimeControlPath) != 0 && errno != ENOENT) ready = NO;
        if (unlink(kR127FaultPairsPath) != 0 && errno != ENOENT) ready = NO;
    }
    r127RuntimeControlReady = ready;
    logLine("R127_RUNTIME_CONTROL=%s pid=%d mode=OFF control=%s faultPairs=%s "
            "commands=ON/OFF/SUMMARY faultPairsBound=1000 staleCommands=%s",
        ready ? "READY" : "REFUSED", getpid(), kR127RuntimeControlPath,
        kR127FaultPairsPath, ready ? "cleared" : "not-cleared-runtime-disabled");
}

static void r127ArmFaultFile(void) {
    char contents[64] = {0};
    if (!r127TakeControlFile(kR127FaultPairsPath, contents, sizeof(contents))) return;
    errno = 0;
    char *end = NULL;
    const unsigned long long requested = strtoull(contents, &end, 10);
    while (end && (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')) ++end;
    if (errno != 0 || end == contents || (end && *end != '\0') ||
        requested == 0 || requested > 1000) {
        logLine("R127_GENERATION_LOSS=REJECTED reason=invalid-pair-count value=%s bound=1..1000",
            contents);
        return;
    }
    uint64_t requestId = 0;
    BOOL armed = NO;
    pthread_mutex_lock(&r127RuntimeLock);
    if (r127RuntimeModeSnapshot() == R127RuntimeModeGenerating) {
        r127FaultPairsRemaining = (uint64_t)requested;
        r127FaultPairsRefused = 0;
        r127FaultRecoveryPending = YES;
        requestId = ++r127FaultRequestId;
        armed = YES;
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    logLine("R127_GENERATION_LOSS=%s requestId=%llu pairs=%llu reason=%s "
            "injection=drop-generated-internal-before-pump",
        armed ? "ARMED" : "REJECTED", (unsigned long long)requestId,
        requested, armed ? "bounded-development-fault" : "runtime-not-generating");
}

static const char *r127RuntimeEnableRefusalReason(void) {
    if (!r123PumpEnabled()) return "pump-disabled";
    if (r125Pristine()) return "pristine-mode-enabled";
    if (!r125CaptureEnabled()) return "source-capture-disabled";
    const char *captureFlag = getenv("FGMETAL_R108_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0)
        return "r108-capture-disabled";
    if (r124TestMode() != R124TestNone) return "r124-test-arm-active";
    if (r123PresentDisabled()) return "pump-presentation-disabled";
    if (!selectedHarnessLayer) return "selected-layer-unavailable";
    return NULL;
}

static void r127PollRuntimeRequests(uint64_t sourceSequence) {
    if (!r127RuntimeControlReady) return;
    r127ArmFaultFile();
    char contents[96] = {0};
    if (!r127TakeControlFile(kR127RuntimeControlPath, contents, sizeof(contents))) return;
    char command[24] = {0};
    unsigned long long requestValue = 0;
    char trailing = '\0';
    const int fields = sscanf(contents, " %23s %llu %c", command, &requestValue, &trailing);
    if (fields != 2 || requestValue == 0) {
        logLine("R127_RUNTIME_CONTROL=REJECTED value=%s reason=expected-command-and-positive-request-id",
            contents);
        return;
    }
    const uint64_t requestId = (uint64_t)requestValue;
    if (strcmp(command, "SUMMARY") == 0) {
        uint32_t finalMode = 0;
        uint64_t finalEpoch = 0, stale = 0, physicalViolations = 0;
        uint64_t lastDisplayed = 0, lastPumpDisplayed = 0, lastPumpDispatched = 0;
        uint64_t sourceAckFrontier = 0, sourceAckOutOfWindow = 0;
        pthread_mutex_lock(&r127RuntimeLock);
        finalMode = r127RuntimeModeSnapshot();
        finalEpoch = r127RuntimeEpochSnapshot();
        stale = r127StaleCallbackRejections;
        lastPumpDispatched = r127LastPumpSourceDispatchedSequence;
        pthread_mutex_unlock(&r127RuntimeLock);
        // Physical-display state is published under r127DisplayLock by the
        // presented handler. Read it after dropping r127RuntimeLock so the two
        // locks are never held at the same time.
        pthread_mutex_lock(&r127DisplayLock);
        physicalViolations = r127PhysicalOrderViolations;
        lastDisplayed = r127LastDisplayedSequence;
        lastPumpDisplayed = r127LastPumpSourceDisplayedSequence;
        sourceAckFrontier = r127DisplayedSourceAck.frontier;
        sourceAckOutOfWindow = r127DisplayedSourceAck.outOfWindow;
        pthread_mutex_unlock(&r127DisplayLock);
        logLine("R127_RUNTIME_SUMMARY=FINAL requestId=%llu action=emit-one-shot",
            (unsigned long long)requestId);
        r137FinalizeWindow("request-final-summary");
        logLine("R127_RUNTIME_STATE=FINAL requestId=%llu mode=%u epoch=%llu "
                "lastDisplayedSequence=%llu lastPumpSourceDispatchedSequence=%llu "
                "lastPumpSourceDisplayedSequence=%llu contiguousSourceAckFrontier=%llu "
                "sourceAckOutOfWindow=%llu staleCallbackRejections=%llu "
                "physicalDisplayOrderViolations=%llu sourceReturnedToTitle=%llu",
            (unsigned long long)requestId, finalMode,
            (unsigned long long)finalEpoch, (unsigned long long)lastDisplayed,
            (unsigned long long)lastPumpDispatched,
            (unsigned long long)lastPumpDisplayed,
            (unsigned long long)sourceAckFrontier,
            (unsigned long long)sourceAckOutOfWindow,
            (unsigned long long)stale,
            (unsigned long long)physicalViolations,
            (unsigned long long)(r123Pump ? [r123Pump sourceReturnedToTitle] : 0));
        r123EmitSummary("r127-requested-final-summary");
        r134EmitTimeline("final");
        return;
    }

    if (strcmp(command, "ON") == 0 &&
        r127RuntimeModeSnapshot() == R127RuntimeModeOff) {
        const char *reason = r127RuntimeEnableRefusalReason();
        if (reason) {
            logLine("R127_RUNTIME_CONTROL=REJECTED requestId=%llu command=ON "
                    "reason=%s sourcePath=TITLE-DIRECT",
                (unsigned long long)requestId, reason);
            return;
        }
        if (!r123EnsureStarted(YES)) {
            logLine("R127_RUNTIME_CONTROL=REJECTED requestId=%llu command=ON "
                    "reason=pump-worker-not-ready sourcePath=TITLE-DIRECT",
                (unsigned long long)requestId);
            return;
        }
    }

    uint32_t mode = R127RuntimeModeOff;
    uint64_t epoch = 0;
    uint64_t beforeSequence = 0, beforeWsi = 0;
    char beforeKind[16] = "NONE";
    uint64_t queueDepthBefore = 0, discardedInternal = 0, discardedSource = 0;
    uint64_t staleCount = 0;
    BOOL activated = NO, beganDrain = NO, retryDrain = NO;
    pthread_mutex_lock(&r127RuntimeLock);
    mode = r127RuntimeModeSnapshot();
    if (strcmp(command, "ON") == 0 && mode == R127RuntimeModeOff) {
        r127DisplaySnapshot(&beforeSequence, &beforeWsi, beforeKind,
                            sizeof(beforeKind));
        queueDepthBefore = r123Pump ? [r123Pump depth] : 0;
        // The current source was directly dispatched while OFF until this
        // request is read. Ignore any older producer/copy completion that
        // arrives after the asynchronous capture finishes.
        r127MinimumSourceSequence = sourceSequence;
        r127EnableBoundaryResolved = NO;
        r127EnableBoundaryLossLogged = NO;
        r127DisplayAckResetAtBoundary(sourceSequence);
        if (r127CachedSourceSequence != sourceSequence) {
            r127CachedSourceTexture = nil;
            r127CachedSourceSequence = 0;
        }
        R123RuntimeEpochResult result = r127AdvanceRuntimeEpochLocked(
            R127RuntimeModeGenerating);
        discardedInternal = result.discardedInternal;
        discardedSource = result.discardedSource;
        // R135: each ON starts a fresh steady-state R134 sample window, so the
        // retained pairs describe the run rather than its pipeline fill.
        r134ResetRetentionForRuntimeOn();
        // R136: same per-window treatment for the miss taxonomy counters; an
        // event still open at the boundary is abandoned (never half-written).
        r136ResetForRuntimeOn();
        r137ResetForRuntimeOn();
        mode = R127RuntimeModeGenerating;
        epoch = r127RuntimeEpochSnapshot();
        r127LastQueuedSourceSequence = 0;
        r127DisableRequestId = 0;
        r127LatestSuppressedSequence = 0;
        r127DisableRequestCutoffSequence = 0;
        r127DisableBoundaryCount = 0;
        r127DisableBoundLogged = NO;
        r127TransitionRequestId = requestId;
        r127TransitionEpoch = epoch;
        r127TransitionBoundarySequence = sourceSequence;
        activated = YES;
    } else if (strcmp(command, "OFF") == 0 &&
               mode == R127RuntimeModeGenerating) {
        r127DisplaySnapshot(&beforeSequence, &beforeWsi, beforeKind,
                            sizeof(beforeKind));
        queueDepthBefore = r123Pump ? [r123Pump depth] : 0;
        R123RuntimeEpochResult result = r127AdvanceRuntimeEpochLocked(
            R127RuntimeModeSourceOnlyDrain);
        discardedInternal = result.discardedInternal;
        discardedSource = result.discardedSource;
        mode = R127RuntimeModeSourceOnlyDrain;
        epoch = r127RuntimeEpochSnapshot();
        r127DisableRequestId = requestId;
        r127DisableRequestCutoffSequence = sourceSequence;
        r127LatestSuppressedSequence = sourceSequence;
        r127DisableBoundaryCount = 0;
        r127DisableBoundLogged = NO;
        r127TransitionRequestId = requestId;
        r127TransitionEpoch = epoch;
        r127TransitionBoundarySequence = 0;
        beganDrain = YES;
    } else if (strcmp(command, "OFF") == 0 &&
               mode == R127RuntimeModeSourceOnlyDrain) {
        r127DisableRequestId = requestId;
        r127DisableRequestCutoffSequence = sourceSequence;
        r127LatestSuppressedSequence = sourceSequence;
        r127DisableBoundaryCount = 0;
        r127DisableBoundLogged = NO;
        r127TransitionRequestId = requestId;
        r127TransitionEpoch = r127RuntimeEpochSnapshot();
        r127TransitionBoundarySequence = 0;
        retryDrain = YES;
    } else {
        staleCount = r127StaleCallbackRejections;
    }
    pthread_mutex_unlock(&r127RuntimeLock);

    if (beganDrain)
        r137FinalizeWindow("runtime-off");

    if (activated) {
        (void)r123EnsureStarted(NO);
        logLine("R127_RUNTIME_TRANSITION requestId=%llu epoch=%llu from=OFF to=ON "
                "boundarySequence=%llu lastDisplayedKind=%s lastDisplayedSequence=%llu "
                "lastDisplayedWsiPresentId=%llu queueDepthBefore=%llu "
                "queueDepthAfter=%lu discardedInternal=%llu discardedSource=%llu "
                "staleCallbackRejections=%llu seed=deferred-to-source-decision",
            (unsigned long long)requestId, (unsigned long long)epoch,
            (unsigned long long)sourceSequence, beforeKind,
            (unsigned long long)beforeSequence, (unsigned long long)beforeWsi,
            (unsigned long long)queueDepthBefore,
            (unsigned long)(r123Pump ? [r123Pump depth] : 0),
            (unsigned long long)discardedInternal,
            (unsigned long long)discardedSource,
            (unsigned long long)r127StaleCallbackRejections);
    } else if (beganDrain) {
        logLine("R127_RUNTIME_DISABLE=FLUSH_STARTED requestId=%llu epoch=%llu "
                "boundarySequence=%llu lastDisplayedKind=%s lastDisplayedSequence=%llu "
                "lastDisplayedWsiPresentId=%llu queueDepthBefore=%llu queueDepthAfter=%lu "
                "discardedInternal=%llu discardedSource=%llu policy=source-only-60hz-drain",
            (unsigned long long)requestId, (unsigned long long)epoch,
            (unsigned long long)sourceSequence, beforeKind,
            (unsigned long long)beforeSequence, (unsigned long long)beforeWsi,
            (unsigned long long)queueDepthBefore,
            (unsigned long)(r123Pump ? [r123Pump depth] : 0),
            (unsigned long long)discardedInternal,
            (unsigned long long)discardedSource);
    } else if (retryDrain) {
        logLine("R127_RUNTIME_DISABLE=FLUSH_RETRY_STARTED requestId=%llu epoch=%llu "
                "boundarySequence=%llu queueDepth=%lu policy=source-only-60hz-drain",
            (unsigned long long)requestId,
            (unsigned long long)r127RuntimeEpochSnapshot(),
            (unsigned long long)sourceSequence,
            (unsigned long)(r123Pump ? [r123Pump depth] : 0));
    } else if (strcmp(command, "ON") != 0 && strcmp(command, "OFF") != 0) {
        logLine("R127_RUNTIME_CONTROL=REJECTED requestId=%llu command=%s "
                "reason=unknown-command", (unsigned long long)requestId, command);
    } else {
        logLine("R127_RUNTIME_CONTROL=NOOP requestId=%llu command=%s mode=%u "
                "reason=transition-not-applicable staleCallbackRejections=%llu",
            (unsigned long long)requestId, command, mode,
            (unsigned long long)staleCount);
    }
}

static void r127TryCompleteDisable(uint64_t sourceSequence) {
    pthread_mutex_lock(&r127RuntimeLock);
    if (r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain &&
        r127DisableRequestId && r130PendingFGDispatches == 0 &&
        (!r123Pump || [r123Pump activeSourceReservations] == 0) &&
        !r127PumpItemInFlight) {
        const uint64_t requestId = r127DisableRequestId;
        uint64_t epoch = 0;
        (void)r127FinishDisableToTitleLocked(&epoch);
        if (!epoch) {
            pthread_mutex_unlock(&r127RuntimeLock);
            return;
        }
        r127TransitionRequestId = requestId;
        r127TransitionEpoch = epoch;
        r127TransitionBoundarySequence = sourceSequence;
        r127DisableRequestId = 0;
        r127DisableRequestCutoffSequence = 0;
        r127LatestSuppressedSequence = 0;
        r127LastQueuedSourceSequence = 0;
        logLine("R130_RUNTIME_DISABLE=COMPLETE requestId=%llu epoch=%llu boundary=%llu frontier=WSI-DISPATCH pendingReservations=0",
            (unsigned long long)requestId, (unsigned long long)epoch,
            (unsigned long long)sourceSequence);
    }
    pthread_mutex_unlock(&r127RuntimeLock);
}

static BOOL r127ConsumeFaultForGeneratedFrame(uint64_t epoch,
                                              uint64_t pairA, uint64_t pairB,
                                              uint64_t internalPresentId) {
    uint64_t remaining = 0, requestId = 0, refusedPairs = 0;
    BOOL refuse = NO, recovered = NO;
    pthread_mutex_lock(&r127RuntimeLock);
    if (r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
        r127RuntimeEpochSnapshot() == epoch) {
        if (r127FaultPairsRemaining > 0) {
            --r127FaultPairsRemaining;
            ++r127FaultPairsRefused;
            r127FaultRecoveryPending = YES;
            refuse = YES;
            remaining = r127FaultPairsRemaining;
            requestId = r127FaultRequestId;
        } else if (r127FaultRecoveryPending) {
            r127FaultRecoveryPending = NO;
            recovered = YES;
            requestId = r127FaultRequestId;
        }
    }
    refusedPairs = r127FaultPairsRefused;
    pthread_mutex_unlock(&r127RuntimeLock);
    if (refuse) {
        logLine("R127_GENERATION_REFUSED requestId=%llu pair=%llu->%llu "
                "internalPresentId=%llu remaining=%llu action=drop-before-pump "
                "sourceFallback=preserved",
            (unsigned long long)requestId, (unsigned long long)pairA,
            (unsigned long long)pairB, (unsigned long long)internalPresentId,
            (unsigned long long)remaining);
    } else if (recovered) {
        logLine("R127_GENERATION_RECOVERED requestId=%llu pair=%llu->%llu "
                "internalPresentId=%llu refusedPairs=%llu recovery=automatic",
            (unsigned long long)requestId, (unsigned long long)pairA,
            (unsigned long long)pairB, (unsigned long long)internalPresentId,
            (unsigned long long)refusedPairs);
    }
    return refuse;
}

static void r130SnapshotTitleBoundary(uint64_t sequence, uint64_t *epoch,
                                     uint64_t *requestId) {
    pthread_mutex_lock(&r127RuntimeLock);
    *epoch = r127RuntimeEpochSnapshot();
    *requestId = r127TransitionEpoch == *epoch &&
        r127TransitionBoundarySequence == sequence ? r127TransitionRequestId : 0;
    pthread_mutex_unlock(&r127RuntimeLock);
}

static uint64_t r127RecordTitleSourceDispatch(id<CAMetalDrawable> drawable,
                                              SEL selector,
                                              uint64_t existingWsiPresentId,
                                              uint64_t sequence,
                                              uint64_t sourcePresentOrdinal,
                                              uint64_t epoch,
                                              uint64_t boundaryRequestId) {
    if (!r123PumpEnabled() || !isSelectedProcess() || isInternalPresentationCall ||
        !drawable || !isSelectedLayer((CAMetalLayer *)drawable.layer))
        return existingWsiPresentId;
    if (sequence == 0) return existingWsiPresentId;
    (void)r127RecordSourceOwnerIfUnknown(sequence, R127SourceOwnerDirect);
    pthread_mutex_lock(&r127SourceOwnershipLock);
    const BOOL ordinalRecorded = R127SourceOwnershipSetPresentOrdinal(
        &r127SourceOwnership, sequence, sourcePresentOrdinal);
    pthread_mutex_unlock(&r127SourceOwnershipLock);
    if (!ordinalRecorded) {
        // A newer acquisition can collide with a still-live modulo slot. The
        // title WSI call remains mandatory, but the observer must not assert,
        // relabel the older owner, or admit this SOURCE to FG without a ledger
        // record. Acquisition ambiguity already closes future FG admission.
        logLine("R130_SOURCE_LEDGER=REFUSED acquisitionId=%llu "
                "sourcePresentOrdinal=%llu action=TITLE-DIRECT-untracked "
                "reason=unresolved-slot-collision",
            (unsigned long long)sequence,
            (unsigned long long)sourcePresentOrdinal);
    }
    if (r127RuntimeModeSnapshot() == R127RuntimeModeOff)
        r127ClearSourceTimes(sequence);

    uint64_t wsiPresentId = existingWsiPresentId;
    if (wsiPresentId == 0) {
        wsiPresentId = allocateWsiPresentId();
        logLine("R101_WSI_DISPATCH origin=SOURCE appFrameId=%llu sourcePresentOrdinal=%llu wsiPresentId=%llu "
                "drawableID=%lu layer=%p texture=%p selector=%s "
                "logicalDrawable=TITLE-DIRECT runtimeMode=OFF",
            (unsigned long long)sequence,
            (unsigned long long)sourcePresentOrdinal,
            (unsigned long long)wsiPresentId,
            (unsigned long)drawable.drawableID, (__bridge void *)drawable.layer,
            (__bridge void *)drawable.texture, sel_getName(selector));
    }
    const uint32_t directReason = r137ThreadDirectSequence == sequence &&
        r137ThreadDirectEpoch == epoch ? r137ThreadDirectReason
                                      : R137DirectOwnerAlreadyDirect;
    BOOL sampleDirect = NO;
    uint64_t directSampleIndex = 0;
    const BOOL runtimeOn = r127RuntimeModeSnapshot() == R127RuntimeModeGenerating;
    if (runtimeOn)
        r137TracePoint(R137EventTitleDirectWsi, sequence, sequence, sequence,
                       sourcePresentOrdinal, directReason, wsiPresentId,
                       r137ThreadDirectPendingDispatches);
    pthread_mutex_lock(&r137Lock);
    if (runtimeOn && r137Enabled() &&
        r137TitleDirectDetailCount < kR137TitleDirectDetailLimit) {
        directSampleIndex = ++r137TitleDirectDetailCount;
        sampleDirect = YES;
    }
    pthread_mutex_unlock(&r137Lock);
    if (sampleDirect) {
        const R123DiagnosticSnapshot snapshot = r123Pump
            ? [r123Pump diagnosticSnapshotAtNs:r105NowNs()]
            : (R123DiagnosticSnapshot){0};
        logLine("R137_TITLE_DIRECT_SAMPLE sample=%llu acquisitionId=%llu "
                "sourcePresentOrdinal=%llu reason=%s reasonFlags=%u mode=%u epoch=%llu "
            "pendingFgDispatches=%llu pendingSourceReservations=%lu queueDepth=%lu claimed=%u:%llu->%llu "
                "head=%u:%llu->%llu next=%u:%llu->%llu oldestCommitted=%llu/%llu ageNs=%llu",
            (unsigned long long)directSampleIndex,
            (unsigned long long)sequence, (unsigned long long)sourcePresentOrdinal,
            r137DirectReason(directReason), directReason,
            r127RuntimeModeSnapshot(), (unsigned long long)epoch,
            (unsigned long long)r137ThreadDirectPendingDispatches,
            (unsigned long)snapshot.activeSourceReservations,
            (unsigned long)snapshot.depth,
            snapshot.claimed.present ? (unsigned)snapshot.claimed.kind + 1 : 0,
            (unsigned long long)snapshot.claimed.seqA,
            (unsigned long long)snapshot.claimed.seqB,
            snapshot.head.present ? (unsigned)snapshot.head.kind + 1 : 0,
            (unsigned long long)snapshot.head.seqA,
            (unsigned long long)snapshot.head.seqB,
            snapshot.next.present ? (unsigned)snapshot.next.kind + 1 : 0,
            (unsigned long long)snapshot.next.seqA,
            (unsigned long long)snapshot.next.seqB,
            (unsigned long long)snapshot.oldestUndispatchedSourceOrdinal,
            (unsigned long long)snapshot.oldestUndispatchedSourceSequence,
            (unsigned long long)snapshot.oldestUndispatchedSourceAgeNs);
    }
    r137ThreadDirectSequence = 0;
    r137ThreadDirectEpoch = 0;
    r137ThreadDirectPendingDispatches = 0;
    r137ThreadDirectReason = 0;
    r127ObserveWsiDisplay(drawable, "SOURCE", sequence, sequence, sequence,
                          sourcePresentOrdinal, 0, wsiPresentId, epoch, NO);

    if (boundaryRequestId) {
        logLine("R127_RUNTIME_BOUNDARY_DISPATCH requestId=%llu epoch=%llu kind=SOURCE "
                "sequence=%llu appFrameId=%llu internalPresentId=NONE "
                "wsiPresentId=%llu path=TITLE-DIRECT",
            (unsigned long long)boundaryRequestId, (unsigned long long)epoch,
            (unsigned long long)sequence, (unsigned long long)sequence,
            (unsigned long long)wsiPresentId);
    }
    return wsiPresentId;
}

// Returns YES when this SOURCE present must not be dispatched. The drawable is
// not retained: the caller returns, D3DMetal releases it, and the frame is
// displayed later from the FG-owned copy.
static BOOL r123TrySuppressSourcePresent(id<CAMetalDrawable> drawable) {
    if (!r123PumpEnabled() || isInternalPresentationCall ||
        !isSelectedProcess() || !drawable ||
        !isSelectedLayer((CAMetalLayer *)drawable.layer)) return NO;
    const uint64_t sequence = r130SequenceForDrawable(drawable);
    if (!sequence) return NO;

    r127PollRuntimeRequests(sequence);
    if (r127RuntimeModeSnapshot() == R127RuntimeModeSourceOnlyDrain)
        r127TryCompleteDisable(sequence);

    BOOL committed = NO;
    BOOL directDecision = NO;
    uint64_t committedPresentOrdinal = 0;
    uint64_t pendingBeforeDecision = 0;
    uint32_t directReasonFlags = 0;
    const uint64_t now = r105NowNs();
    pthread_mutex_lock(&r127RuntimeLock);
    const uint64_t epoch = r127RuntimeEpochSnapshot();
    const uint32_t mode = r127RuntimeModeSnapshot();
    const R127SourceOwner owner = r127SourceOwnerForSequence(sequence);
    if (owner == R127SourceOwnerTitleDispatched) {
        pthread_mutex_unlock(&r127RuntimeLock);
        return YES;
    }
    if (owner == R127SourceOwnerSuppressed || owner == R127SourceOwnerDispatched) {
        committed = YES;
    } else if (owner == R127SourceOwnerDirect) {
        directReasonFlags |= R137DirectOwnerAlreadyDirect;
    } else if (owner == R127SourceOwnerUnknown) {
        R130AcquireTicket *ticket = &r130AcquireTickets[
            sequence % R127_SOURCE_OWNERSHIP_CAPACITY];
        const BOOL ticketAvailable = !r130AcquireAccountingAmbiguous && ticket->active &&
            ticket->sequence == sequence;
        const BOOL imageReady = r127CachedSourceTexture &&
            r127CachedSourceSequence == sequence && r130CachedSourceEpoch == epoch;
        // R133: presentation may commit only on CAPTURE_COPY_COMMITTED. The
        // private copy is encoded inside this frame's producer command buffer,
        // so its completion is implied by the producer's completion and does
        // not depend on the present call being issued.
        const BOOL captureCommitted = r133CaptureCommittedForSequence(sequence);
        const BOOL testModeAllowed = r124TestMode() == R124TestNone;
        const BOOL runtimeGenerating = mode == R127RuntimeModeGenerating;
        const BOOL workerReady = __atomic_load_n(&r123WorkerReady, __ATOMIC_ACQUIRE);
        const BOOL pumpAvailable = r123Pump != nil;
        if (!ticketAvailable) {
            directReasonFlags |= R137DirectTicketUnavailable;
            if (r130AcquireAccountingAmbiguous)
                directReasonFlags |= R137DirectOwnershipAmbiguous;
        }
        if (!imageReady) directReasonFlags |= R137DirectImageUnavailable;
        if (!captureCommitted) directReasonFlags |= R137DirectCaptureUncommitted;
        if (!runtimeGenerating) directReasonFlags |= R137DirectRuntimeNotGenerating;
        if (!workerReady) directReasonFlags |= R137DirectWorkerUnavailable;
        if (!pumpAvailable) directReasonFlags |= R137DirectPumpUnavailable;
        if (!testModeAllowed) directReasonFlags |= R137DirectTestMode;
        if (captureCommitted) ++r133CommitPrerequisiteSatisfied;
        else ++r133CommitPrerequisiteAbsent;
        const BOOL canCommit = testModeAllowed && ticketAvailable && imageReady &&
            captureCommitted && runtimeGenerating && workerReady && pumpAvailable;
        R123DisplayItem *reservation = canCommit ?
            [r123Pump reserveSource:sequence appFrameId:sequence
                            payload:r127CachedSourceTexture atNs:now runtimeEpoch:epoch] : nil;
        if (canCommit && !reservation) {
            directReasonFlags |= R137DirectReservationRefused;
            const R123DiagnosticSnapshot snapshot = [r123Pump
                diagnosticSnapshotAtNs:now];
            if (snapshot.activeSourceReservations >= r123Pump.sourceCapacity)
                directReasonFlags |= R137DirectCapacityRefused;
        }
        if (reservation) {
            [r110WsiDispatchLock lock];
            const uint64_t sourcePresentOrdinal = r130NextSourcePresentOrdinal;
            reservation.sourcePresentOrdinal = sourcePresentOrdinal;
            pthread_mutex_lock(&r127SourceOwnershipLock);
            const R127SourceOwner recorded = R127SourceOwnershipSetIfUnknown(
                &r127SourceOwnership, sequence, R127SourceOwnerSuppressed);
            const BOOL ordinalRecorded = recorded == R127SourceOwnerSuppressed &&
                R127SourceOwnershipSetPresentOrdinal(&r127SourceOwnership, sequence,
                                                      sourcePresentOrdinal);
            if (ordinalRecorded &&
                [r123Pump commitSourceReservation:reservation]) {
                ++r130NextSourcePresentOrdinal;
                ++r130PendingFGDispatches;
                committedPresentOrdinal = sourcePresentOrdinal;
                (void)r130FinishTitleAcquisitionLocked(sequence);
                r127LastQueuedSourceSequence = sequence;
                r127LatestSuppressedSequence = sequence;
                r127EnableBoundaryResolved = YES;
                [r123Pump primeAtNs:now];
                committed = YES;
            } else {
                directReasonFlags |= R137DirectReservationRefused;
                if (recorded == R127SourceOwnerSuppressed)
                    (void)R127SourceOwnershipUndoProvisional(&r127SourceOwnership, sequence);
                (void)[r123Pump cancelSourceReservation:reservation];
            }
            pthread_mutex_unlock(&r127SourceOwnershipLock);
            if (committed)
                r132PublishPresentation(sequence, committedPresentOrdinal,
                                        R127SourceOwnerSuppressed);
            [r110WsiDispatchLock unlock];
        }
        if (!committed)
            (void)r127RecordSourceOwnerIfUnknown(sequence, R127SourceOwnerDirect);
    }
    pendingBeforeDecision = r130PendingFGDispatches;
    if (!committed) {
        directDecision = mode == R127RuntimeModeGenerating;
        if (directDecision && directReasonFlags == 0)
            directReasonFlags = R137DirectOwnerAlreadyDirect;
    }
    if (r127CachedSourceSequence == sequence) {
        r127CachedSourceTexture = nil;
        r127CachedSourceSequence = 0;
        r130CachedSourceEpoch = 0;
    }
    pthread_mutex_unlock(&r127RuntimeLock);
    if (directDecision) {
        r137ThreadDirectSequence = sequence;
        r137ThreadDirectEpoch = epoch;
        r137ThreadDirectPendingDispatches = pendingBeforeDecision;
        r137ThreadDirectReason = directReasonFlags;
        r137TracePoint(R137EventDirectDecision, sequence, sequence, sequence,
                       0, directReasonFlags, (uint64_t)owner,
                       pendingBeforeDecision);
    }
    if (committed) {
        r137TracePoint(R137EventSourceReservationCommit, sequence,
            sequence, sequence, committedPresentOrdinal, 1u,
            now, pendingBeforeDecision);
        r127SetSourceTime(sequence, YES, fgmetalMediaNow());
        __atomic_add_fetch(&r123SourceSuppressed, 1, __ATOMIC_RELAXED);
        logLine("R130_SOURCE_OWNERSHIP=FG_COMMITTED sequence=%llu epoch=%llu imageReady=1 reservation=1 originalDrawableRetained=0",
            (unsigned long long)sequence, (unsigned long long)epoch);
    }
    return committed;
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


// ---------------------------------------------------------------------------
// R125 -- NON-OWNING CORRELATION REGISTRY.
//
// R124 left exactly one uncontrolled variable in every live arm: this function
// stored `@"drawable": drawable`, a strong reference, for up to
// kRetainedDrawableLimit drawables on a three-deep layer, on every title
// acquisition. R125 exists to remove that variable, so the registry now holds
// scalars only and retains no drawable, no texture, no layer and no device.
//
//   sequence      acquisition counter, monotonic within the process
//   drawableID    CAMetalDrawable.drawableID. This is an acquisition counter,
//                 NOT a pool slot -- R124 §8 falsified the slot reading.
//   drawablePtr   the drawable's address as a uintptr_t. NON-OWNING.
//   texturePtr    the texture's address as a uintptr_t. NON-OWNING.
//   layer         the layer's address as a uintptr_t. NON-OWNING.
//   device        the device's address as a uintptr_t. NON-OWNING.
//   acquiredAtNs  CLOCK_MONOTONIC acquisition instant.
//
// A raw pointer is a diagnostic, never an identity. An entry is only trusted
// while the live drawable's address still matches drawablePtr; consumers that
// hold the live drawable must go through trackedRecordForDrawable(), which
// enforces that check. Consumers that only have a texture (the attachment
// inventory) still key on the texture address and treat the record as a
// diagnostic.
//
// The key is the texture address boxed as an NSNumber, so the dictionary key
// itself cannot retain anything either.
// ---------------------------------------------------------------------------
static NSNumber *r125PointerKey(const void *pointer) {
    return @((uintptr_t)pointer);
}

static void r125AuditRegistry(void);

// Reads a non-owning pointer scalar back out. Returns NULL when the field is
// absent, which keeps "no record" and "record without that field" distinct from
// a stale address of 0.
static const void *r125PointerField(NSDictionary *record, NSString *key) {
    NSNumber *boxed = record[key];
    if (![boxed isKindOfClass:[NSNumber class]]) return NULL;
    return (const void *)(uintptr_t)[boxed unsignedLongLongValue];
}

static uint64_t retainDrawableForCorrelation(id<CAMetalDrawable> drawable) {
    id<MTLTexture> texture = drawable.texture;
    id<MTLDevice> layerDevice = drawable.layer.device;
    NSNumber *textureKey = r125PointerKey((__bridge const void *)texture);
    uint64_t acquiredAtNs = r105NowNs();
    pthread_mutex_lock(&stateLock);
    if (!drawablesByTexture) drawablesByTexture = [NSMutableDictionary dictionary];
    uint64_t sequence = ++drawableSequence;
    drawablesByTexture[textureKey] = @{
        @"sequence": @(sequence),
        @"drawableID": @(drawable.drawableID),
        @"drawablePtr": @((uintptr_t)(__bridge void *)drawable),
        @"texturePtr": @((uintptr_t)(__bridge void *)texture),
        @"layer": @((uintptr_t)(__bridge void *)drawable.layer),
        @"device": @((uintptr_t)(__bridge void *)layerDevice),
        @"acquiredAtNs": @(acquiredAtNs),
        @"r130CaptureEpoch": @(r127RuntimeEpochSnapshot()),
    };
    while (drawablesByTexture.count > kRetainedDrawableLimit) {
        NSNumber *oldestKey = nil;
        uint64_t oldestSequence = UINT64_MAX;
        for (NSNumber *key in drawablesByTexture) {
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

    // R125 structural audit. The registry's non-ownership is a property of the
    // literal above, but a property asserted only by a comment is one careless
    // edit away from being false. This walks the live dictionary and fails loud
    // if any value is an Objective-C object that could own a drawable. It is
    // O(entries) on a bounded dictionary and runs once per acquisition window,
    // not once per frame.
    if ((sequence % 64) == 1) r125AuditRegistry();
    return sequence;
}

// Called from retainDrawableForCorrelation. Logs the class of every value in the
// correlation registry and counts how many are not NSNumbers. A drawable,
// texture, layer or block stored anywhere in the record would show up here as a
// non-NSNumber class, so the log is positive evidence for the review item "no
// strong drawable reference remains".
static void r125AuditRegistry(void) {
    pthread_mutex_lock(&stateLock);
    NSUInteger entries = drawablesByTexture.count;
    NSUInteger fields = 0;
    NSUInteger drawableObjects = 0;
    NSUInteger textureObjects = 0;
    NSUInteger otherObjects = 0;
    char otherClass[128] = "none";
    for (NSNumber *key in drawablesByTexture) {
        NSDictionary *record = drawablesByTexture[key];
        for (NSString *field in record) {
            id value = record[field];
            ++fields;
            if ([value isKindOfClass:[NSNumber class]]) continue;
            if ([value conformsToProtocol:@protocol(CAMetalDrawable)]) {
                ++drawableObjects;
            } else if ([value conformsToProtocol:@protocol(MTLTexture)]) {
                ++textureObjects;
            } else {
                if (otherObjects == 0)
                    snprintf(otherClass, sizeof(otherClass), "%s:%s",
                             field.UTF8String ?: "?", class_getName([value class]));
                ++otherObjects;
            }
        }
    }
    pthread_mutex_unlock(&stateLock);
    // The claim this round needs is narrow and checkable: the registry holds no
    // drawable and no texture. It may still hold the producer command buffer and
    // its queue, which are bounded by GPU completion and cannot own a drawable;
    // those are counted separately so the log states exactly what is there
    // instead of asserting a blanket "no objects".
    logLine("R125_REGISTRY_AUDIT entries=%lu fields=%lu drawableObjects=%lu "
            "textureObjects=%lu otherObjects=%lu otherClass=%s limit=%lu "
            "retention=%s",
        (unsigned long)entries, (unsigned long)fields,
        (unsigned long)drawableObjects, (unsigned long)textureObjects,
        (unsigned long)otherObjects, otherClass,
        (unsigned long)kRetainedDrawableLimit,
        (drawableObjects == 0 && textureObjects == 0) ? "DRAWABLE-AND-TEXTURE-NONE" : "PRESENT");
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
    // R125: the first selected acquisition is the earliest point at which the
    // arm, the registry shape and the layer's presentation properties can all be
    // stated. Logging them here rather than at pump start makes them available to
    // the pristine slice, which never starts a pump.
    if (selected) r125LogSessionIdentity();

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
    const BOOL r130Ticketed = selected && r123PumpEnabled() &&
        !isInternalDrawableAcquisition && !isInternalPresentationCall;
    if (r130Ticketed) r130BeginTitleAcquisition();
    id<CAMetalDrawable> drawable = ((id (*)(id, SEL))originalNextDrawable)(self, selector);
    if (r130Ticketed && !drawable) r130RegisterTitleAcquisition(0, nil);
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
    // R125: the title's own acquisition latency, counted in-process. This is the
    // primary measurement of the round. It is emitted only for acquisitions the
    // title itself made -- an FG replacement acquisition is a different event and
    // is already accounted for by R124_TEST*_ACQUIRED.
    if (drawable && selected && orderingEntryNs && orderingReturnNs &&
        !isInternalDrawableAcquisition && isSelectedLayer(layer)) {
        const uint64_t r125LatencyNs = orderingReturnNs - orderingEntryNs;
        __atomic_add_fetch(&r125TitleAcquisitions, 1, __ATOMIC_RELAXED);
        if (r125LatencyNs > 100000000ULL)
            __atomic_add_fetch(&r125TitleStalls100Ms, 1, __ATOMIC_RELAXED);
        if (r125LatencyNs > 500000000ULL)
            __atomic_add_fetch(&r125TitleStalls500Ms, 1, __ATOMIC_RELAXED);
        if (r125LatencyNs > 900000000ULL)
            __atomic_add_fetch(&r125TitleStalls900Ms, 1, __ATOMIC_RELAXED);
        logLine("R125_TITLE_ACQUISITION callId=%llu latencyNs=%llu latencyMs=%.3f "
                "bucket=%s drawable=%p drawableID=%lu texture=%p",
            (unsigned long long)orderingCallId, (unsigned long long)r125LatencyNs,
            (double)r125LatencyNs / 1e6,
            r125LatencyNs > 900000000ULL ? "GT900MS" :
            r125LatencyNs > 500000000ULL ? "GT500MS" :
            r125LatencyNs > 100000000ULL ? "GT100MS" : "FAST",
            (__bridge void *)drawable, (unsigned long)drawable.drawableID,
            (__bridge void *)drawable.texture);
        const uint64_t r125Count = __atomic_load_n(&r125TitleAcquisitions, __ATOMIC_RELAXED);
        if ((r125Count % 32) == 0) {
            logLine("R125_ACQUISITION_SUMMARY acquisitions=%llu gt100ms=%llu gt500ms=%llu "
                    "gt900ms=%llu stallRate500ms=%.4f",
                (unsigned long long)r125Count,
                (unsigned long long)__atomic_load_n(&r125TitleStalls100Ms, __ATOMIC_RELAXED),
                (unsigned long long)__atomic_load_n(&r125TitleStalls500Ms, __ATOMIC_RELAXED),
                (unsigned long long)__atomic_load_n(&r125TitleStalls900Ms, __ATOMIC_RELAXED),
                (double)__atomic_load_n(&r125TitleStalls500Ms, __ATOMIC_RELAXED) / (double)r125Count);
        }
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
        // R124 TEST 2: pre-acquire the replacement drawable here, at the
        // title's own acquisition point, and hand it to the pump.
        r124MaybePreAcquireReplacement();
        uint64_t r130Sequence = retainDrawableForCorrelation(drawable);
        if (r130Ticketed) r130RegisterTitleAcquisition(r130Sequence, drawable);
        // R122: arming happens on the acquisition path -- the frame boundary --
        // so the run starts at a frame edge and never mid-present.
        if (r122OverlapEnabled()) r122MaybeArm(drawableSequence);
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
    NSNumber *key = r125PointerKey((__bridge const void *)texture);
    pthread_mutex_lock(&stateLock);
    NSDictionary *record = drawablesByTexture[key];
    pthread_mutex_unlock(&stateLock);
    return record;
}

// R125: the drawable-aware lookup. The registry is keyed by texture address and
// stores the drawable's address as a scalar, so a hit is only a correlation when
// both addresses agree. Without this check a recycled texture address could hand
// a caller the previous frame's sequence, which is exactly the class of silent
// misattribution R124 spent a round removing.
static NSDictionary *trackedRecordForDrawable(id<CAMetalDrawable> drawable) {
    if (!drawable) return nil;
    NSDictionary *record = trackedDrawableForTexture(drawable.texture);
    if (!record) return nil;
    if (r125PointerField(record, @"drawablePtr") != (__bridge const void *)drawable) return nil;
    return record;
}

static void r110LogSourcePresent(id self, SEL selector, const char *phase,
                                uint64_t callId, uint64_t timestampNs,
                                CFTimeInterval timingArgument) {
    if ((!r110OrderingOnly && !r110PresentationEnabled()) ||
        !isSelectedProcess() || isInternalPresentationCall) return;
    id<CAMetalDrawable> drawable = (id<CAMetalDrawable>)self;
    if (!drawable || !isSelectedLayer((CAMetalLayer *)drawable.layer)) return;
    NSDictionary *record = trackedRecordForDrawable(drawable);
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

    NSDictionary *record = trackedRecordForDrawable(drawable);
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

    // R125: the registry no longer holds the drawable, so the layer and device
    // are read back from the non-owning scalars captured at acquisition. Both are
    // used for pointer comparison only; the layer itself is owned by the title's
    // window and is alive whenever this function can run.
    const void *drawablePointer = r125PointerField(record, @"drawablePtr");
    CAMetalLayer *layer = (__bridge CAMetalLayer *)r125PointerField(record, @"layer");
    id<MTLDevice> device = (__bridge id<MTLDevice>)r125PointerField(record, @"device");
    id<MTLCommandQueue> queue = record[@"producerQueue"];
    if (!drawablePointer || !layer || !device || !isSelectedLayer(layer) ||
        producer != record[@"producerCommandBuffer"] ||
        producer.status < MTLCommandBufferStatusCommitted ||
        producer.status == MTLCommandBufferStatusError || producer.error ||
        !queue || queue.device != device || layer.device != device) {
        logLine("R103_FRONTIER_CLOSE_REJECTED sourceSequence=%llu drawable=%p layer=%p device=%p producer=%p producerStatus=%lu queue=%p queueDevice=%p reason=producer-or-target-mismatch",
            (unsigned long long)sequence, drawablePointer,
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
    // R125: there is no drawable in the record any more. When the caller has the
    // live object it is used directly; otherwise the non-owning address is
    // logged, which is all this diagnostic ever needed.
    id<CAMetalDrawable> drawable = explicitDrawable;
    id<MTLTexture> texture = drawable ? drawable.texture : nil;
    id<MTLCommandBuffer> commandBuffer = explicitCommandBuffer ?: record[@"producerCommandBuffer"];
    id<MTLCommandQueue> queue = record[@"producerQueue"] ?: commandBuffer.commandQueue;
    uint64_t sequence = [record[@"sequence"] unsignedLongLongValue];
    uint64_t drawableID = drawable ? drawable.drawableID :
        [record[@"drawableID"] unsignedLongLongValue];
    const void *drawablePointer = drawable ? (__bridge void *)drawable :
        r125PointerField(record, @"drawablePtr");
    const void *texturePointer = drawable ? (__bridge void *)texture :
        r125PointerField(record, @"texturePtr");
    void *layer = drawable ? (__bridge void *)drawable.layer :
        (void *)r125PointerField(record, @"layer");
    void *device = texture ? (__bridge void *)texture.device :
        (void *)r125PointerField(record, @"device");
    logLine("R102_EVENT t_ns=%llu event=%s sourceSequence=%llu drawableID=%llu drawable=%p texture=%p commandBuffer=%p encoder=%p queue=%p layer=%p device=%p selector=%s argument=%.9f",
        (unsigned long long)r102MachTimeNs(), event,
        (unsigned long long)sequence, (unsigned long long)drawableID,
        drawablePointer, texturePointer,
        (__bridge void *)commandBuffer, (__bridge void *)encoder,
        (__bridge void *)queue, layer, device,
        selector ? sel_getName(selector) : "none", argument);
}

static void logR102DrawableEvent(const char *event, id<CAMetalDrawable> drawable,
                                 SEL selector, double argument) {
    NSDictionary *record = trackedRecordForDrawable(drawable);
    if (!record && drawable) {
        // Transient, stack-local fallback. Scalars only, for the same reason the
        // registry is scalars only: one record shape, audited once.
        record = @{@"drawablePtr": @((uintptr_t)(__bridge void *)drawable),
                   @"texturePtr": @((uintptr_t)(__bridge void *)drawable.texture),
                   @"drawableID": @(drawable.drawableID),
                   @"layer": @((uintptr_t)(__bridge void *)drawable.layer),
                   @"device": @((uintptr_t)(__bridge void *)drawable.texture.device)};
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
    NSDictionary *record = trackedRecordForDrawable(ticket.drawable);
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
    // R122: in generation-only overlap mode no SOURCE present may ever be
    // deferred. The target-sequence test below already guarantees it (the tuple
    // latch never arms in this mode, so r110TupleSourceBSequence stays zero),
    // but the refusal is stated here as well so the guarantee does not depend on
    // an invariant maintained three files away from the deferral itself.
    if (r122OverlapEnabled()) return NO;
    if (selector != @selector(present) && selector != @selector(presentAtTime:) &&
        selector != @selector(presentAfterMinimumDuration:)) return NO;

    NSDictionary *record = trackedRecordForDrawable(drawable);
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
    NSDictionary *record = trackedRecordForDrawable(drawable);
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

    NSDictionary *record = trackedRecordForDrawable(sourceDrawable);
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
    // R122: the SOURCE present-hook instant for this frame. Recorded on every
    // SOURCE present, including the ones that take ordinary WSI dispatch, so
    // the overlap run and the baseline carry the same set of instants.
    if (entryNs) r122NoteSourcePresentHook((id<CAMetalDrawable>)self, entryNs);
    // R123: SOURCE_PRESENTATION_DRAWABLE_DECOUPLING. This frame is displayed
    // later from the FG-owned copy; returning here releases the original
    // drawable immediately and allocates no WSI identity, because nothing was
    // dispatched.
    if (r123TrySuppressSourcePresent((id<CAMetalDrawable>)self)) {
        // R124 TEST 1. The replacement present is issued from here -- the
        // title's own present hook, on the title's own present thread -- and
        // not from the pump thread. Everything else about it is R123's path.
        NSDictionary *r124SuppressedRecord =
            trackedRecordForDrawable((id<CAMetalDrawable>)self);
        r124MaybeTitleThreadPresent(
            [r124SuppressedRecord[@"sequence"] unsignedLongLongValue]);
        return;
    }
    if (!r123PumpEnabled() && isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, 0.0)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), 0.0);
        return;
    }
    if (!r130ClaimTitleDispatch((id<CAMetalDrawable>)self)) return;
    const BOOL r130UsesSourceOrdinal = r123PumpEnabled() &&
        isSelectedProcess() && isSelectedLayer((CAMetalLayer *)((id<CAMetalDrawable>)self).layer);
    if (r130UsesSourceOrdinal)
        r130WaitForPriorCommittedSource(r130SequenceForDrawable((id<CAMetalDrawable>)self));
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = 0;
    const uint64_t r130Sequence = r123PumpEnabled()
        ? r130SequenceForDrawable((id<CAMetalDrawable>)self) : 0;
    uint64_t r130Epoch = 0, r130BoundaryRequest = 0;
    if (r123PumpEnabled())
        r130SnapshotTitleBoundary(r130Sequence, &r130Epoch, &r130BoundaryRequest);
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() || r123PumpEnabled()) {
            [r110WsiDispatchLock lock];
            const uint64_t sourcePresentOrdinal = r130UsesSourceOrdinal
                ? r130AllocateSourcePresentOrdinalLocked() : 0;
            if (r110PresentationEnabled())
                sourceWsiPresentId = logSourceWsiDispatch(self, selector);
            sourceWsiPresentId = r127RecordTitleSourceDispatch(
                (id<CAMetalDrawable>)self, selector, sourceWsiPresentId,
                r130Sequence, sourcePresentOrdinal, r130Epoch,
                r130BoundaryRequest);
            if (r130UsesSourceOrdinal)
                r132PublishPresentation(r130Sequence, sourcePresentOrdinal,
                                        R127SourceOwnerDirect);
            NSDictionary *record = trackedRecordForDrawable((id<CAMetalDrawable>)self);
            if (r110PresentationEnabled() && sourceWsiPresentId)
                (void)r110RecordWsiDispatch("SOURCE",
                    r130Sequence ?: [record[@"sequence"] unsignedLongLongValue], 0,
                    sourceWsiPresentId, 0, 0);
            r130MarkTitleDispatched(r130Sequence);
            if (r130UsesSourceOrdinal)
                r130MarkSourcePresentOrdinalDispatchedLocked(
                    sourcePresentOrdinal, r130Sequence);
            ((void (*)(id, SEL))original)(self, selector);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL))original)(self, selector);
        }
    }
    if (r130UsesSourceOrdinal) r132ClearCachedSource(r130Sequence);
    r130FinishTitleAcquisition((id<CAMetalDrawable>)self);
    if (callId) r110LogSourcePresent(self, selector, "RETURN", callId, r105NowNs(), 0.0);
    if (r110PresentationEnabled()) r103EndSourceOperation(dispatchLease, "present", callId);
}

static void hookDrawablePresentAtTime(id self, SEL selector, CFTimeInterval when) {
    uint64_t callId = (r110OrderingOnly || r110PresentationEnabled()) && isSelectedProcess()
        ? __atomic_add_fetch(&r110PresentCallId, 1, __ATOMIC_RELAXED) : 0;
    uint64_t entryNs = callId ? r105NowNs() : 0;
    if (callId) r110LogSourcePresent(self, selector, "ENTRY", callId, entryNs, when);
    // R122: the SOURCE present-hook instant for this frame. Recorded on every
    // SOURCE present, including the ones that take ordinary WSI dispatch, so
    // the overlap run and the baseline carry the same set of instants.
    if (entryNs) r122NoteSourcePresentHook((id<CAMetalDrawable>)self, entryNs);
    // R123: SOURCE_PRESENTATION_DRAWABLE_DECOUPLING. This frame is displayed
    // later from the FG-owned copy; returning here releases the original
    // drawable immediately and allocates no WSI identity, because nothing was
    // dispatched.
    if (r123TrySuppressSourcePresent((id<CAMetalDrawable>)self)) {
        // R124 TEST 1. The replacement present is issued from here -- the
        // title's own present hook, on the title's own present thread -- and
        // not from the pump thread. Everything else about it is R123's path.
        NSDictionary *r124SuppressedRecord =
            trackedRecordForDrawable((id<CAMetalDrawable>)self);
        r124MaybeTitleThreadPresent(
            [r124SuppressedRecord[@"sequence"] unsignedLongLongValue]);
        return;
    }
    if (!r123PumpEnabled() && isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, when)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), when);
        return;
    }
    if (!r130ClaimTitleDispatch((id<CAMetalDrawable>)self)) return;
    const BOOL r130UsesSourceOrdinal = r123PumpEnabled() &&
        isSelectedProcess() && isSelectedLayer((CAMetalLayer *)((id<CAMetalDrawable>)self).layer);
    if (r130UsesSourceOrdinal)
        r130WaitForPriorCommittedSource(r130SequenceForDrawable((id<CAMetalDrawable>)self));
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = 0;
    const uint64_t r130Sequence = r123PumpEnabled()
        ? r130SequenceForDrawable((id<CAMetalDrawable>)self) : 0;
    uint64_t r130Epoch = 0, r130BoundaryRequest = 0;
    if (r123PumpEnabled())
        r130SnapshotTitleBoundary(r130Sequence, &r130Epoch, &r130BoundaryRequest);
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() || r123PumpEnabled()) {
            [r110WsiDispatchLock lock];
            const uint64_t sourcePresentOrdinal = r130UsesSourceOrdinal
                ? r130AllocateSourcePresentOrdinalLocked() : 0;
            if (r110PresentationEnabled())
                sourceWsiPresentId = logSourceWsiDispatch(self, selector);
            sourceWsiPresentId = r127RecordTitleSourceDispatch(
                (id<CAMetalDrawable>)self, selector, sourceWsiPresentId,
                r130Sequence, sourcePresentOrdinal, r130Epoch,
                r130BoundaryRequest);
            if (r130UsesSourceOrdinal)
                r132PublishPresentation(r130Sequence, sourcePresentOrdinal,
                                        R127SourceOwnerDirect);
            NSDictionary *record = trackedRecordForDrawable((id<CAMetalDrawable>)self);
            if (r110PresentationEnabled() && sourceWsiPresentId)
                (void)r110RecordWsiDispatch("SOURCE",
                    r130Sequence ?: [record[@"sequence"] unsignedLongLongValue], 0,
                    sourceWsiPresentId, 0, 0);
            r130MarkTitleDispatched(r130Sequence);
            if (r130UsesSourceOrdinal)
                r130MarkSourcePresentOrdinalDispatchedLocked(
                    sourcePresentOrdinal, r130Sequence);
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, when);
        }
    }
    if (r130UsesSourceOrdinal) r132ClearCachedSource(r130Sequence);
    r130FinishTitleAcquisition((id<CAMetalDrawable>)self);
    if (callId) r110LogSourcePresent(self, selector, "RETURN", callId, r105NowNs(), when);
    if (r110PresentationEnabled()) r103EndSourceOperation(dispatchLease, "presentAtTime", callId);
}

static void hookDrawablePresentDuration(id self, SEL selector, CFTimeInterval duration) {
    uint64_t callId = (r110OrderingOnly || r110PresentationEnabled()) && isSelectedProcess()
        ? __atomic_add_fetch(&r110PresentCallId, 1, __ATOMIC_RELAXED) : 0;
    uint64_t entryNs = callId ? r105NowNs() : 0;
    if (callId) r110LogSourcePresent(self, selector, "ENTRY", callId, entryNs, duration);
    // R122: the SOURCE present-hook instant for this frame. Recorded on every
    // SOURCE present, including the ones that take ordinary WSI dispatch, so
    // the overlap run and the baseline carry the same set of instants.
    if (entryNs) r122NoteSourcePresentHook((id<CAMetalDrawable>)self, entryNs);
    // R123: SOURCE_PRESENTATION_DRAWABLE_DECOUPLING. This frame is displayed
    // later from the FG-owned copy; returning here releases the original
    // drawable immediately and allocates no WSI identity, because nothing was
    // dispatched.
    if (r123TrySuppressSourcePresent((id<CAMetalDrawable>)self)) {
        // R124 TEST 1. The replacement present is issued from here -- the
        // title's own present hook, on the title's own present thread -- and
        // not from the pump thread. Everything else about it is R123's path.
        NSDictionary *r124SuppressedRecord =
            trackedRecordForDrawable((id<CAMetalDrawable>)self);
        r124MaybeTitleThreadPresent(
            [r124SuppressedRecord[@"sequence"] unsignedLongLongValue]);
        return;
    }
    if (!r123PumpEnabled() && isSelectedProcess() && r110PresentationEnabled() &&
        r102TryDeferSourcePresent((id<CAMetalDrawable>)self, selector, duration)) {
        r102AttemptInternalDispatch();
        if (callId) r110LogSourcePresent(self, selector, "DEFERRED", callId, r105NowNs(), duration);
        return;
    }
    if (!r130ClaimTitleDispatch((id<CAMetalDrawable>)self)) return;
    const BOOL r130UsesSourceOrdinal = r123PumpEnabled() &&
        isSelectedProcess() && isSelectedLayer((CAMetalLayer *)((id<CAMetalDrawable>)self).layer);
    if (r130UsesSourceOrdinal)
        r130WaitForPriorCommittedSource(r130SequenceForDrawable((id<CAMetalDrawable>)self));
    BOOL dispatchLease = r110PresentationEnabled()
        ? r103BeginSourceDispatch((id<CAMetalDrawable>)self) : NO;
    uint64_t sourceWsiPresentId = 0;
    const uint64_t r130Sequence = r123PumpEnabled()
        ? r130SequenceForDrawable((id<CAMetalDrawable>)self) : 0;
    uint64_t r130Epoch = 0, r130BoundaryRequest = 0;
    if (r123PumpEnabled())
        r130SnapshotTitleBoundary(r130Sequence, &r130Epoch, &r130BoundaryRequest);
    IMP original = originalFor(self, selector);
    if (original) {
        if (r110PresentationEnabled() || r123PumpEnabled()) {
            [r110WsiDispatchLock lock];
            const uint64_t sourcePresentOrdinal = r130UsesSourceOrdinal
                ? r130AllocateSourcePresentOrdinalLocked() : 0;
            if (r110PresentationEnabled())
                sourceWsiPresentId = logSourceWsiDispatch(self, selector);
            sourceWsiPresentId = r127RecordTitleSourceDispatch(
                (id<CAMetalDrawable>)self, selector, sourceWsiPresentId,
                r130Sequence, sourcePresentOrdinal, r130Epoch,
                r130BoundaryRequest);
            if (r130UsesSourceOrdinal)
                r132PublishPresentation(r130Sequence, sourcePresentOrdinal,
                                        R127SourceOwnerDirect);
            NSDictionary *record = trackedRecordForDrawable((id<CAMetalDrawable>)self);
            if (r110PresentationEnabled() && sourceWsiPresentId)
                (void)r110RecordWsiDispatch("SOURCE",
                    r130Sequence ?: [record[@"sequence"] unsignedLongLongValue], 0,
                    sourceWsiPresentId, 0, 0);
            r130MarkTitleDispatched(r130Sequence);
            if (r130UsesSourceOrdinal)
                r130MarkSourcePresentOrdinalDispatchedLocked(
                    sourcePresentOrdinal, r130Sequence);
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
            [r110WsiDispatchLock unlock];
        } else {
            ((void (*)(id, SEL, CFTimeInterval))original)(self, selector, duration);
        }
    }
    if (r130UsesSourceOrdinal) r132ClearCachedSource(r130Sequence);
    r130FinishTitleAcquisition((id<CAMetalDrawable>)self);
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
                                 NSString *condition,
                                 BOOL generationOnly,
                                 uint64_t generationEpoch,
                                 uint64_t sourcePresentOrdinal) {
    if (r123PumpEnabled()) {
        pthread_mutex_lock(&r127RuntimeLock);
        const uint64_t minimumSequence = r127MinimumSourceSequence;
        const BOOL beforeEnableBoundary =
            !R127SourceSequenceAtOrAfter(sequence, minimumSequence);
        const BOOL missingEnableBoundary =
            r127RuntimeModeSnapshot() == R127RuntimeModeGenerating &&
            !r127EnableBoundaryResolved;
        if (beforeEnableBoundary || missingEnableBoundary)
            ++r127StaleCallbackRejections;
        pthread_mutex_unlock(&r127RuntimeLock);
        if (beforeEnableBoundary || missingEnableBoundary) {
            logLine("R127_GENERATION_CALLBACK=REJECTED sequence=%llu "
                    "minimumSequence=%llu reason=%s",
                (unsigned long long)sequence,
                (unsigned long long)minimumSequence,
                beforeEnableBoundary ? "pre-enable-source-history" :
                    "enable-boundary-unavailable");
            return;
        }
        const uint64_t currentEpoch = r127RuntimeEpochSnapshot();
        const uint32_t currentMode = r127RuntimeModeSnapshot();
        if (generationEpoch != currentEpoch) {
            pthread_mutex_lock(&r127RuntimeLock);
            ++r127StaleCallbackRejections;
            pthread_mutex_unlock(&r127RuntimeLock);
            logLine("R127_GENERATION_CALLBACK=REJECTED sequence=%llu callbackEpoch=%llu "
                    "currentEpoch=%llu reason=stale-before-processing",
                (unsigned long long)sequence, (unsigned long long)generationEpoch,
                (unsigned long long)currentEpoch);
            return;
        }
        if (r127GenerationEpochProcessed != generationEpoch) {
            if (realFrameProvider) [realFrameProvider invalidateHistory];
            realFrameProvider = nil;
            outputReadbackQueue = nil;
            realProviderInitialized = NO;
            previousCaptureColor = nil;
            previousCaptureDepth = nil;
            previousCaptureMotion = nil;
            previousCaptureSource = nil;
            previousCaptureSequence = 0;
            previousCapturePresentOrdinal = 0;
            r127GenerationEpochProcessed = generationEpoch;
            logLine("R127_GENERATION_EPOCH=RESET epoch=%llu mode=%u "
                    "providerHistory=INVALIDATED sourceHistory=EMPTY",
                (unsigned long long)generationEpoch, currentMode);
        }
        if (currentMode != R127RuntimeModeGenerating) return;
    }
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
    if (previousCaptureSource && !r132HistoryIsAdjacent(
            previousCaptureSequence, previousCapturePresentOrdinal,
            sequence, sourcePresentOrdinal)) {
        // R115: a gap is expected between tuples because only the reserved pair
        // is captured. Tearing the provider down here would rebuild MetalFX on
        // B's processing, i.e. inside the retained-B hold, once per tuple. The
        // provider holds no frame history, so only the history is dropped.
        BOOL keepProvider = r115ContinuousEnabled();
        if (realFrameProvider) [realFrameProvider invalidateHistory];
        logLine("R132_SOURCE_PRESENTATION_GAP previousAcquisitionId=%llu currentAcquisitionId=%llu previousOrdinal=%llu currentOrdinal=%llu resetProvider=%d",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            (unsigned long long)previousCapturePresentOrdinal,
            (unsigned long long)sourcePresentOrdinal,
            keepProvider ? 0 : 1);
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
        previousCapturePresentOrdinal = 0;
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
    BOOL hasPrevious = previousCaptureSource && r132HistoryIsAdjacent(
        previousCaptureSequence, previousCapturePresentOrdinal,
        sequence, sourcePresentOrdinal) &&
        !reservedSeed;
    if (generationOnly && !hasPrevious) {
        // A skipped pair leaves the history chain broken, so the next frame that
        // does get the slot can only seed. It is counted separately and excluded
        // from the generated-pair rate: a seed is not an interpolation.
        __atomic_add_fetch(&r122SeedFrames, 1, __ATOMIC_RELAXED);
        logLine("R122_FG_PAIR pair=%llu->%llu kind=SEED result=HISTORY_RESET "
                "reason=%s accounting=GENERATION_ONLY discarded=1",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            previousCaptureSequence ? "sequence-gap" : "first-capture");
    }
    id<MTLCommandBuffer> commandBuffer = [r108ComputeQueue commandBuffer];
    if (!commandBuffer) {
        logLine("R108_GPU_INPUT_ENCODING=FAILED seq=%llu stage=command-buffer",
            (unsigned long long)sequence);
        return;
    }
    // R134: generation entry. `r118GenerationEntryNs` is the monotonic instant
    // already taken at the top of this function, before any encoding.
    r134NoteGenerationStart(previousCaptureSequence, sequence,
                            r118GenerationEntryNs);
    r137TracePoint(R137EventGenerationEntry, sequence,
        previousCaptureSequence, sequence, sourcePresentOrdinal,
        hasPrevious ? 1u : 0u, r118GenerationEntryNs, generationEpoch);
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
    const uint64_t estimatorSubmitNs = r105NowNs();
    r134NoteEstimatorSubmit(sequence, estimatorSubmitNs);
    r137TracePoint(R137EventEstimatorSubmit, sequence,
        previousCaptureSequence, sequence, sourcePresentOrdinal,
        hasPrevious ? 1u : 0u, estimatorSubmitNs, 0);
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    // R119 Phase 3: the CPU wake instant. GPUEndTime -> wake is completion
    // signalling latency, also not GPU execution.
    const double r119EstimatorWake = fgmetalMediaNow();
    const uint64_t estimatorWakeNs = r105NowNs();
    r134NoteEstimatorWake(sequence, estimatorWakeNs,
                          fgmetalMediaToNs(commandBuffer.GPUStartTime),
                          fgmetalMediaToNs(commandBuffer.GPUEndTime));
    r137TracePoint(R137EventEstimatorWake, sequence,
        previousCaptureSequence, sequence, sourcePresentOrdinal,
        commandBuffer.status == MTLCommandBufferStatusCompleted ? 1u : 0u,
        estimatorWakeNs, commandBuffer.error ? 1u : 0u);
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
    logLine("R108_GPU_INPUTS_COMPLETE AcquisitionIdB=%llu SourcePresentOrdinalB=%llu pairAcquisitionId=%llu->%llu pairSourcePresentOrdinal=%llu->%llu estimatorCommandBuffer=%p sourceTexture=%p motionTexture=%p status=%lu gpuMs=%.3f error=%s motionSource=%s",
        (unsigned long long)sequence,
        (unsigned long long)sourcePresentOrdinal,
        (unsigned long long)(hasPrevious ? previousCaptureSequence : sequence),
        (unsigned long long)sequence,
        (unsigned long long)(hasPrevious ? previousCapturePresentOrdinal :
                                             sourcePresentOrdinal),
        (unsigned long long)sourcePresentOrdinal, (__bridge void *)commandBuffer,
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
        if (r123PumpEnabled() &&
            (!r127RuntimeCanGenerate() ||
             r127RuntimeEpochSnapshot() != generationEpoch)) {
            pthread_mutex_lock(&r127RuntimeLock);
            ++r127StaleCallbackRejections;
            pthread_mutex_unlock(&r127RuntimeLock);
            logLine("R127_GENERATION_CALLBACK=REJECTED sequence=%llu callbackEpoch=%llu "
                    "currentEpoch=%llu reason=runtime-disabled-before-metalfx",
                (unsigned long long)sequence, (unsigned long long)generationEpoch,
                (unsigned long long)r127RuntimeEpochSnapshot());
            return;
        }
        NSError *error = nil;
        // R116: the gate decision is needed for exactly one pair shape, and that
        // shape is known before the transition runs. Deciding it here lets the
        // metric reduction ride the MetalFX submission instead of paying its own
        // commit -> wait round trip afterwards.
        pthread_mutex_lock(&stateLock);
        BOOL r116TargetPairKnown = r110PresentationEnabled() &&
            r110TupleSourceBSequence == sequence &&
            r132HistoryIsAdjacent(previousCaptureSequence,
                                  previousCapturePresentOrdinal, sequence,
                                  sourcePresentOrdinal);
        pthread_mutex_unlock(&stateLock);
        BOOL r116WantGate = r116TargetPairKnown;
        __block BOOL r116GateEncoded = NO;
        __block uint64_t r116GateEncodeNs = 0;
        uint64_t startNs = r105NowNs();
        r134NoteMetalFX(sequence, startNs, 0);
        r137TracePoint(R137EventMetalFxStart, sequence,
            previousCaptureSequence, sequence, sourcePresentOrdinal,
            0u, startNs, 0);
        BOOL r132ProviderInvoked = NO;
        FGMetalInternalFrame *internalFrame =
            r132InterpolateAdjacentPair(realFrameProvider,
                previousCaptureColor, color, depth, motion,
                previousCaptureSequence, previousCapturePresentOrdinal,
                sequence, sourcePresentOrdinal, 1.0f / 60.0f,
                r116WantGate
                    ? ^(id<MTLCommandBuffer> gateCommandBuffer,
                        id<MTLTexture> interpolatedOutput) {
                        uint64_t encodeStartNs = r105NowNs();
                        r116GateEncoded = r116EncodeFastGate(gateCommandBuffer,
                            motion, previousCaptureColor, color,
                            interpolatedOutput);
                        r116GateEncodeNs = r105NowNs() - encodeStartNs;
                    }
                    : nil,
                &error, &r132ProviderInvoked);
        if (!r132ProviderInvoked) {
            logLine("R132_METALFX_PROVIDER=SKIPPED pairAcquisitionId=%llu->%llu pairSourcePresentOrdinal=%llu->%llu reason=non-adjacent-or-missing-input",
                (unsigned long long)previousCaptureSequence,
                (unsigned long long)sequence,
                (unsigned long long)previousCapturePresentOrdinal,
                (unsigned long long)sourcePresentOrdinal);
            return;
        }
        uint64_t elapsedNs = r105NowNs() - startNs;
        const uint64_t metalFxEndNs = r105NowNs();
        r134NoteMetalFX(sequence, startNs, metalFxEndNs);
        r137TracePoint(R137EventMetalFxComplete, sequence,
            previousCaptureSequence, sequence, sourcePresentOrdinal,
            r137MetalFxResultFlags(r132ProviderInvoked,
                internalFrame != nil, error != nil),
            metalFxEndNs, elapsedNs);
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
        logLine("R108_METALFX_TRANSITION pairAcquisitionId=%llu->%llu pairSourcePresentOrdinal=%llu->%llu condition=%s result=%s internalPresentId=%llu error=%s elapsedMs=%.3f depthQualifier=CONSTANT_DEPTH_PROTOTYPE",
            (unsigned long long)previousCaptureSequence,
            (unsigned long long)sequence,
            (unsigned long long)previousCapturePresentOrdinal,
            (unsigned long long)sourcePresentOrdinal,
            condition.UTF8String ?: "UNSPECIFIED",
            internalFrame ? "INTERNAL_FRAME" : (error ? "FAILED" : "NO_OUTPUT"),
            (unsigned long long)internalFrame.internalPresentId,
            error.localizedDescription.UTF8String ?: "none", elapsedNs / 1e6);
        if (r123PumpEnabled()) {
            // R123 pair record. Unlike R122 the midpoint is kept: it is placed
            // into the display order that was fixed when B was captured, and it
            // is the display pump -- not this thread -- that decides whether it
            // reaches the screen.
            const uint64_t r123EstimatorGpuStart =
                fgmetalMediaToNs(commandBuffer.GPUStartTime);
            const uint64_t r123EstimatorGpuEnd =
                fgmetalMediaToNs(commandBuffer.GPUEndTime);
            const BOOL r127InjectedRefusal = internalFrame && !error &&
                r127ConsumeFaultForGeneratedFrame(generationEpoch,
                    previousCaptureSequence, sequence, internalFrame.internalPresentId);
            const BOOL r127ResultEpochCurrent = !r123PumpEnabled() ||
                (r127RuntimeCanGenerate() &&
                 r127RuntimeEpochSnapshot() == generationEpoch);
            const BOOL r123PairOk = (internalFrame != nil) && (error == nil) &&
                !r127InjectedRefusal && r127ResultEpochCurrent;
            const BOOL r123Warmup = (internalFrame == nil) && (error == nil);
            logLine("R123_FG_PAIR pairAcquisitionId=%llu->%llu pairSourcePresentOrdinal=%llu->%llu kind=PAIR result=%s "
                    "estimatorGpuStartNs=%llu estimatorGpuEndNs=%llu "
                    "estimatorGpuExecNs=%llu metalFXWallNs=%llu metalFXGpuExposed=0 "
                    "internalPresentId=%llu wsiPresentId=PENDING-at-dispatch "
                    "accounting=INTERNAL error=%s",
                (unsigned long long)previousCaptureSequence,
                (unsigned long long)sequence,
                (unsigned long long)previousCapturePresentOrdinal,
                (unsigned long long)sourcePresentOrdinal,
                r127InjectedRefusal ? "INJECTED_REFUSAL" :
                    (!r127ResultEpochCurrent && internalFrame ? "STALE_EPOCH" :
                     (r123PairOk ? "GENERATED" : (r123Warmup ? "WARMUP" : "FAILED"))),
                (unsigned long long)r123EstimatorGpuStart,
                (unsigned long long)r123EstimatorGpuEnd,
                (unsigned long long)(r123EstimatorGpuEnd > r123EstimatorGpuStart
                                     ? r123EstimatorGpuEnd - r123EstimatorGpuStart : 0),
                (unsigned long long)elapsedNs,
                (unsigned long long)(internalFrame ? internalFrame.internalPresentId : 0),
                error.localizedDescription.UTF8String ?: "none");
            if (internalFrame && error == nil && !r127InjectedRefusal) {
                r123ResolveInternal(previousCaptureSequence, sequence,
                                    internalFrame.texture,
                                    internalFrame.internalPresentId,
                                    generationEpoch);
            }
        } else if (generationOnly) {
            // R122 generation-only pair record. The M candidate is *discarded*:
            // it receives no WsiPresentId, is never presented and never enters
            // public SOURCE accounting. The estimator GPU window is read from the
            // estimator command buffer, which has already completed
            // (waitUntilCompleted above), so it is a real GPU interval rather
            // than a wall-clock estimate. MetalFX does not expose GPUStartTime to
            // this caller, so its transition is published as a CPU wall interval
            // and labelled as such -- it is never presented as GPU execution.
            const uint64_t r122EstimatorGpuStart = fgmetalMediaToNs(commandBuffer.GPUStartTime);
            const uint64_t r122EstimatorGpuEnd = fgmetalMediaToNs(commandBuffer.GPUEndTime);
            const uint64_t r122EstimatorGpuExec =
                (r122EstimatorGpuStart && r122EstimatorGpuEnd > r122EstimatorGpuStart)
                ? r122EstimatorGpuEnd - r122EstimatorGpuStart : 0;
            const BOOL r122PairOk = (internalFrame != nil) && (error == nil);
            // The provider's documented first completed transition establishes
            // temporal history and returns nil WITHOUT an error. Counting that as
            // a failure would understate the generated-pair rate by one on every
            // run and would misreport a normal warm-up as a defect.
            const BOOL r122Warmup = (internalFrame == nil) && (error == nil);
            if (r122PairOk) {
                __atomic_add_fetch(&r122PairsGenerated, 1, __ATOMIC_RELAXED);
            } else if (r122Warmup) {
                __atomic_add_fetch(&r122PairsWarmup, 1, __ATOMIC_RELAXED);
            } else {
                __atomic_add_fetch(&r122PairsFailed, 1, __ATOMIC_RELAXED);
            }
            logLine("R122_FG_PAIR pair=%llu->%llu kind=PAIR result=%s "
                    "estimatorGpuStartNs=%llu estimatorGpuEndNs=%llu estimatorGpuExecNs=%llu "
                    "metalFXWallNs=%llu metalFXGpuExposed=0 gate=SKIPPED "
                    "mDispatched=0 wsiPresentId=UNALLOCATED accounting=GENERATION_ONLY "
                    "discarded=1 error=%s",
                (unsigned long long)previousCaptureSequence,
                (unsigned long long)sequence,
                r122PairOk ? "GENERATED" : (r122Warmup ? "WARMUP" : "FAILED"),
                (unsigned long long)r122EstimatorGpuStart,
                (unsigned long long)r122EstimatorGpuEnd,
                (unsigned long long)r122EstimatorGpuExec,
                (unsigned long long)elapsedNs,
                error.localizedDescription.UTF8String ?: "none");
        }
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
                r132HistoryIsAdjacent(previousCaptureSequence,
                                      previousCapturePresentOrdinal, sequence,
                                      sourcePresentOrdinal);
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
                        r132HistoryIsAdjacent(previousCaptureSequence,
                                              previousCapturePresentOrdinal,
                                              sequence, sourcePresentOrdinal) &&
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
                r132HistoryIsAdjacent(previousCaptureSequence,
                                      previousCapturePresentOrdinal, sequence,
                                      sourcePresentOrdinal);
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
    previousCapturePresentOrdinal = sourcePresentOrdinal;
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

// R125: takes the SOURCE texture and the drawable's identifier instead of the
// drawable object. The producer completion handler used to reach the drawable
// through the correlation registry; with the registry non-owning that object is
// no longer available there, and it was never needed -- the capture reads the
// colour texture and logs the identifier.
static void captureRealDrawable(id<MTLCommandQueue> queue,
                                id<MTLTexture> source,
                                uint64_t drawableID,
                                uint64_t sourceSequence,
                                uint64_t captureEpoch,
                                id<MTLCommandBuffer> producerCommandBuffer) {
    // R125: capture is instrumentation, not the title's path. A pristine slice
    // runs with it off, so the only observer work per frame is the hook, the
    // scalar registry write and the log line.
    if (!r125CaptureEnabled()) return;
    const char *captureFlag = getenv("FGMETAL_R108_CAPTURE");
    if (!captureFlag || strcmp(captureFlag, "1") != 0) return;
    if (!r108SequenceSelectedForCapture(sourceSequence)) return;
    if (!source) return;
    // R133: when the private copy was already encoded inside the producer
    // command buffer, this completion-time path must not capture a second time.
    // The in-producer copy is the one the stage, the pump and the estimator use.
    if (r133CaptureEnabled() && r133CaptureCommittedForSequence(sourceSequence)) {
        static uint64_t r133SkippedLegacyCaptures;
        const uint64_t skipped = __atomic_add_fetch(&r133SkippedLegacyCaptures, 1,
                                                    __ATOMIC_RELAXED);
        if (skipped <= 8) {
            logLine("R133_LEGACY_CAPTURE=SKIPPED seq=%llu "
                    "reason=in-producer-command-buffer-copy-present skipped=%llu",
                (unsigned long long)sourceSequence, (unsigned long long)skipped);
        }
        return;
    }

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
    // R122 overlap runs are bounded by the generation-slot budget instead, and
    // must not be silently truncated at the legacy 120-frame window limit.
    BOOL continuousCapture = r115ContinuousEnabled() || r122OverlapEnabled() ||
        r123PumpEnabled();
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
            (unsigned long long)sourceSequence, (unsigned long)drawableID,
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
        (unsigned long long)sourceSequence, (unsigned long)drawableID,
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
                    (unsigned long long)sourceSequence, (unsigned long)drawableID,
                    (__bridge void *)completedCopy, (unsigned long)completedCopy.status,
                    completedCopy.error.localizedDescription.UTF8String ?: "none");
                return;
            }
            const BOOL r123GenerationOnly = r123PumpEnabled() &&
                r127RuntimeCanGenerate();
            const BOOL r122GenerationOnly = r122OverlapEnabled() || r123GenerationOnly;
            // Stage the private image before diagnostic logging or readback can
            // delay its race with the presentation decision.
            if (r123PumpEnabled()) {
                r123EnqueueSourceDetailed(sourceSequence, sourceSequence,
                    captureTexture, captureEpoch, (uint64_t)drawableID,
                    conditionLabel, r122GenerationOnly);
            }
            // R132 admits estimator work only after a SOURCE presentation
            // decision assigns its ordinal.
            logLine("R132_CAPTURE_COMPLETED seq=%llu condition=%s drawableID=%lu producerCommandBuffer=%p copyCommandBuffer=%p gpuSourceTexture=%p drawableRetained=0",
                (unsigned long long)sourceSequence,
                conditionLabel.UTF8String ?: "UNSPECIFIED",
                (unsigned long)drawableID,
                (__bridge void *)producerCommandBuffer,
                (__bridge void *)completedCopy,
                (__bridge void *)captureTexture);
            if (!r123PumpEnabled()) {
                // Keep the pre-R132 non-pump experiment path acquisition based;
                // presentation ordinals are only defined by the R123 title gate.
                r123EnqueueSource(sourceSequence, sourceSequence,
                                  captureTexture, captureEpoch);
                const double r119Dispatch = fgmetalMediaNow();
                if (r119IsPairB) {
                    pthread_mutex_lock(&stateLock);
                    r115Timing.mediaDispatch = r119Dispatch;
                    pthread_mutex_unlock(&stateLock);
                    r121Mark(R121SlotTaskEnqueue);
                }
                BOOL skipDispatch = NO;
                if (r122GenerationOnly) {
                    __atomic_add_fetch(&r122PairsAttempted, 1, __ATOMIC_RELAXED);
                    if (!r122TryAcquireGenerationSlot()) {
                        skipDispatch = YES;
                        __atomic_add_fetch(&r122PairsSkippedBusy, 1, __ATOMIC_RELAXED);
                        if (r122OverlapEnabled()) r122MaybeEmitSummary();
                    }
                }
                if (!skipDispatch) {
                    const uint64_t generationEpoch = r127RuntimeEpochSnapshot();
                    dispatch_async(captureProcessingQueue, ^{
                        @autoreleasepool {
                            if (r119IsPairB) {
                                pthread_mutex_lock(&stateLock);
                                r115Timing.mediaGenQueueEntry = fgmetalMediaNow();
                                pthread_mutex_unlock(&stateLock);
                                r121Mark(R121SlotTaskStart);
                            }
                            processCapturedFrame(sourceSequence, drawableID,
                                source.pixelFormat, captureTexture, device,
                                conditionLabel, r122GenerationOnly,
                                generationEpoch, 0);
                            if (r122GenerationOnly) {
                                r122ReleaseGenerationSlot();
                                if (r122OverlapEnabled()) r122MaybeEmitSummary();
                            }
                        }
                    });
                }
            }
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
                (unsigned long)drawableID,
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
                    (unsigned long)drawableID,
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
                    (unsigned long)drawableID,
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
    // R125: identity check against the non-owning texture address instead of the
    // drawable object the registry no longer holds.
    if (!texture ||
        (__bridge const void *)texture != r125PointerField(drawableRecord, @"texturePtr") ||
        colorAttachmentIndex >= 8) return;
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
    // R133: encode the private SOURCE copy inside the title's own producer
    // command buffer, after every title render pass and before commit. From
    // this point the copy is scheduled and owned by the GPU, which is the only
    // prerequisite FG presentation is allowed to rely on.
    if (selected) r133CommitCaptureForCommandBuffer(commandBuffer);
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
    // R122: only the command buffer correlated to a SOURCE drawable is the
    // frame's producer. Every other commit in the process would otherwise churn
    // the pending ring -- the first build did exactly that, with ~100 buffers per
    // frame evicting the producer's entry before its completion handler ran, and
    // every frame reported commitNs=0.
    if (sourceRecord) r122NoteProducerCommit(commandBuffer, commitReturnNs);
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
    // R125: no drawable object is available from the record. The two facts this
    // function needs from it -- the layer address and the texture address -- are
    // stored as non-owning scalars.
    const void *drawableLayer = r125PointerField(drawableRecord, @"layer");
    const void *drawableTexture = r125PointerField(drawableRecord, @"texturePtr");
    uint64_t sequence = [drawableRecord[@"sequence"] unsignedLongLongValue];
    uint64_t captureEpoch = [drawableRecord[@"r130CaptureEpoch"] unsignedLongLongValue];
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
    // R125: the registry is keyed by the texture address boxed as a number, not
    // by an NSValue, so this lookup has to use the same key function.
    NSNumber *textureKey = r125PointerKey((__bridge const void *)texture);
    pthread_mutex_lock(&stateLock);
    NSDictionary *currentRecord = drawablesByTexture[textureKey];
    if ([currentRecord[@"sequence"] unsignedLongLongValue] == sequence) {
        NSMutableDictionary *updatedRecord = [currentRecord mutableCopy];
        updatedRecord[@"producerCommandBuffer"] = commandBuffer;
        updatedRecord[@"producerQueue"] = queue;
        drawablesByTexture[textureKey] = updatedRecord;
    }
    pthread_mutex_unlock(&stateLock);
    // R133: the producer-commit path needs the drawable texture before the
    // completion handler exists. A weak side table keeps R125's non-owning
    // guarantee: no drawable and no texture is retained.
    if (attachmentKind && strcmp(attachmentKind, "color") == 0)
        r133NoteTrackedTexture(sequence, texture);
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
        drawableLayer, (__bridge void *)texture,
        (__bridge void *)device, (unsigned long long)device.registryID,
        texture.framebufferOnly, (__bridge void *)commandBuffer,
        className(commandBuffer), (__bridge void *)queue, className(queue),
        (__bridge void *)queue.device, attachmentKind, (unsigned long)attachmentIndex,
        (unsigned long)commandBuffer.status, (unsigned long long)matchedRun);
    if (correlationMilestone) {
        logLine("DRAWABLE_PRODUCER_CORRELATION=VALIDATED pid=%d consecutiveMatches=%llu layer=%p queue=%p device=%p registryID=%llu",
            getpid(), (unsigned long long)matchedRun,
            drawableLayer, (__bridge void *)queue,
            (__bridge void *)device, (unsigned long long)device.registryID);
    }
    if (strcmp(attachmentKind, "color") == 0 &&
        (__bridge const void *)texture == drawableTexture)
        r105AssociateProducer(commandBuffer, texture, drawableRecord, attachmentIndex);

    // Register before returning the encoder to the caller. The game's own
    // command buffer remains responsible for commit and execution ordering.
    [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        NSError *error = completed.error;
        // R126: QUEUE-ORIGIN GATE.
        //
        // This handler is registered from reportCorrelation(), which fires for
        // any render pass whose colour attachment is a tracked drawable texture
        // -- including the pass the display pump encodes into a freshly
        // acquired drawable on its own queue. R125 section 6.2 measured what
        // that cost: 3 395 completions on r123Queue were attributed to SOURCE
        // sequence numbers that had already been captured, so FG's own
        // displayed frames were re-captured, re-enqueued with a stale sequence
        // and fed back into the pump.
        //
        // A command buffer that commits on r123Queue is FG-Metal's own
        // presentation/copy work. It is not a title SOURCE producer completion
        // and must not reach SOURCE capture, SOURCE sequence attribution, the
        // SOURCE enqueue, estimator history or MetalFX pair construction.
        //
        // Nothing about producer correlation changes: the title's own queue is
        // a different object, so every genuine producer completion still passes
        // through the identical path below.
        if (r123Queue && completed.commandQueue == r123Queue) {
            const uint64_t ignored =
                __atomic_add_fetch(&ignoredSelfPresentCompletions, 1, __ATOMIC_RELAXED);
            if (ignored <= 8) {
                __atomic_add_fetch(&ignoredSelfPresentLogged, 1, __ATOMIC_RELAXED);
                logLine("R126_SELF_PRESENT_COMPLETION=IGNORED pid=%d seq=%llu drawableID=%llu "
                        "commandBuffer=%p queue=%p reason=queue-is-fg-presentation "
                        "reachedSourceCapture=0 ordinal=%llu",
                    getpid(), (unsigned long long)sequence, (unsigned long long)drawableID,
                    (__bridge void *)completed, (__bridge void *)completed.commandQueue,
                    (unsigned long long)ignored);
            }
            // The key is registered before the commit and must be released on
            // every exit path, or a recycled command-buffer address would be
            // skipped on a later frame.
            pthread_mutex_lock(&stateLock);
            [completedHookKeys removeObject:completionKey];
            pthread_mutex_unlock(&stateLock);
            return;
        }
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
            // R122: one record per SOURCE frame. The GPU window is read here,
            // where Metal has already stamped it, and the acquire/hook instants
            // come from the frame's own drawable record rather than from a
            // "most recent" global, so a late frame cannot borrow a neighbour's
            // timestamps.
            if (r122ProducerGpuEnabled()) {
                const uint64_t r122CompletionNs = r105NowNs();
                const double r122MediaCompletion = fgmetalMediaNow();
                uint64_t r122AcquireNs = 0;
                uint64_t r122HookNs = 0;
                NSDictionary *r122Record = trackedDrawableForTexture(texture);
                if (r122Record) {
                    r122AcquireNs = [r122Record[@"acquiredAtNs"] unsignedLongLongValue];
                    r122HookNs = [r122Record[@"presentHookNs"] unsignedLongLongValue];
                }
                uint64_t r122CommitNs = 0;
                double r122MediaCommit = 0.0;
                if (!r122PendingTake(r122PendingCommit, (__bridge const void *)completed,
                                     &r122CommitNs, &r122MediaCommit)) {
                    r122CommitNs = 0;
                    r122MediaCommit = 0.0;
                }
                if (!r122HookNs) {
                    // Present-hook instant not yet published. It is recorded on
                    // the drawable record by the present selector, which for an
                    // ordinary frame runs before GPU completion; a zero here is
                    // reported as zero rather than borrowed from a neighbour.
                    r122HookNs = 0;
                }
                r122EmitSourceGpu(sequence, completed, r122CompletionNs,
                                  r122MediaCompletion, r122AcquireNs, r122HookNs,
                                  r122CommitNs, r122MediaCommit);
            }
            captureRealDrawable(completed.commandQueue, texture,
                                (uint64_t)drawableID, sequence, captureEpoch, completed);
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
    r127PrepareRuntimeControl();
    logLine("R122_EVIDENCE_DIRECTORY pid=%d source=%s path=%s writable=%d protected=%d "
            "override=%s allowLegacy=%s own=%s",
        getpid(), gFGMetalEvidenceDirectorySource, fgmetalEvidenceDirectory(),
        fgmetalEvidenceDiagnosticsAllowed() ? 1 : 0,
        fgmetalEvidenceDirectoryIsProtected(fgmetalEvidenceDirectory()) ? 1 : 0,
        getenv("FGMETAL_EVIDENCE_DIR") ?: "<unset>",
        getenv("FGMETAL_ALLOW_LEGACY_EVIDENCE_DIR") ?: "<unset>",
        kR124EvidenceDirectory);
    // R122 is now a historical corpus from R123's point of view. Logged rather
    // than assumed, so the boundary is visible in the run record.
    logLine("R124_EVIDENCE_BOUNDARY pid=%d own=%s priorRoundProtected=%s priorRoundWritable=%d",
        getpid(), kR124EvidenceDirectory, kR122EvidenceDirectory,
        fgmetalPathIsWithin(kR122EvidenceDirectory, kR124EvidenceDirectory) ? 1 : 0);
    if (!fgmetalEvidenceDiagnosticsAllowed()) {
        logLine("R122_EVIDENCE_DIRECTORY=REFUSED pid=%d path=%s reason=protected-historical-corpus "
                "action=diagnostics-disabled product-path=unaffected",
            getpid(), fgmetalEvidenceDirectory());
    }
    // R122: state the mode at load, before the game draws anything, so a run
    // that was launched with the wrong environment is identifiable from the
    // first line of its log rather than from its statistics.
    logLine("R122_MODE pid=%d overlap=%d producerGpu=%d maxPairs=%llu "
            "bDeferral=%s frontier=%s internalDispatch=%s accounting=%s",
        getpid(), r122OverlapEnabled() ? 1 : 0, r122ProducerGpuEnabled() ? 1 : 0,
        (unsigned long long)r122MaxPairs(),
        r122OverlapEnabled() ? "NONE" : "R116-UNCHANGED",
        r122OverlapEnabled() ? "OPEN-ALWAYS" : "R103-UNCHANGED",
        r122OverlapEnabled() ? "NONE" : "R102-UNCHANGED",
        r122OverlapEnabled() ? "GENERATION-ONLY" : "PUBLIC-UNCHANGED");
    logLine("R123_MODE pid=%d pump=%d bDeferral=%s frontier=%s internalDispatch=%s "
            "sourcePresent=%s displayDrawable=%s retainedSourceDrawables=0 "
            "halfPeriodNs=%llu maxDepth=%d",
        getpid(), r123PumpEnabled() ? 1 : 0,
        r123PumpEnabled() ? "NONE" : "R116-UNCHANGED",
        r123PumpEnabled() ? "OPEN-ALWAYS" : "R103-UNCHANGED",
        r123PumpEnabled() ? "PUMP-OWNED" : "R102-UNCHANGED",
        r123PumpEnabled() ? "SUPPRESSED-THEN-DELAYED" : "DIRECT",
        r123PumpEnabled() ? "FRESH-PER-FRAME" : "ORIGINAL",
        (unsigned long long)r123HalfPeriodNs(), kR123MaxDepth);
    logLine("R127_RUNTIME_MODE pid=%d mode=OFF epoch=%llu controlReady=%d "
            "transition=runtime-control-file sourceOnlyDefault=0",
        getpid(), (unsigned long long)r127RuntimeEpochSnapshot(),
        r127RuntimeControlReady ? 1 : 0);
    logLine("R119_DIAGNOSTIC_POLICY pid=%d mode=%s env=%s boundedSample=first-middle-last "
            "timingPerTuple=1 gatePerTuple=1 accountingPerTuple=1 wsiPerTuple=1",
        getpid(), r119FullDiagnosticsMode() ? "full" : "bounded",
        getenv("FGMETAL_DIAG_SAMPLE") ?: "<unset>");
    logLine("R127_DIAGNOSTICS_POLICY mode=%s healthyPerEvent=first-8 residualMissOrIdle=first-16 "
            "anomalySamples=first-64 unknownEvents=first-64 "
            "periodicSummarySeconds=60 finalLatencyQuantiles=1 "
            "verboseOverride=FGMETAL_DIAG_SAMPLE-full",
        r127VerboseDiagnosticsMode() ? "verbose" : "bounded");
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
