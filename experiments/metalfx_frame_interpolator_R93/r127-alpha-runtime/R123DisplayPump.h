// R123 -- buffered logical SOURCE presentation.
//
// This file is the *decision* half of the display pump: which logical frame the
// next slot must show, what happens when a generated midpoint is not ready
// before its deadline, and how the bounded buffer behaves under pressure.
//
// It contains no Metal, no CAMetalLayer and no clock. That is deliberate: the
// ordering and accounting rules R123 must satisfy are the part that has to be
// falsifiable, so they live in a unit a host program can drive with a synthetic
// timeline and check against ground truth. The observer supplies the real clock,
// the real textures and the real drawables.
//
// BUFFER SHAPE
//
//   queue: [ M(A,B) ][ B ][ M(B,C) ][ C ] ...   head -> tail
//
//   * a committed adjacent SOURCE frame may insert a *placeholder* for
//     M(S-1,S) immediately before S; a sequence gap skips that optional item;
//   * the SOURCE reservation exists before title suppression, and the item is
//     not claimable until the caller commits ownership;
//   * generation fills the placeholder in place when it completes;
//   * a slot consumes exactly one item;
//   * a placeholder still unfilled when it reaches the head is dropped, counted
//     as a deadline miss, and the slot falls through to the next item. The
//     source frame behind it is displayed exactly once, in order. R136: the
//     drop is deferred, to capture(B) + measuredPeriod, when the slot
//     fires earlier than that (a capture hitch can push the pair's own B
//     capture past the clamped-period slot, making the slot structurally
//     unpresentable); the deferral gives the in-flight generation one
//     measured period to finish and never delays SOURCE B past its normal
//     two-period anchor. A later wake plus a raised period measurement can
//     re-defer; the [H, 4H] period clamp bounds the deferral target at
//     capture(B) + 4*halfPeriod, so the drop remains bounded.
//
// Queue depth is bounded to `maxDepth`. SOURCE reservations have the same
// explicit bound and include queued plus claimed SOURCE work, so requeueing a
// claimed SOURCE never needs a transient extra slot. Overflow removes queued
// INTERNAL work first. If committed SOURCE responsibilities fill the bound, a
// new reservation is refused while its title presentation remains available.
//
// SCHEDULING -- and why it is not a fixed 60-Hz grid
//
// The first implementation free-ran at 16.667 ms and dropped ~44 % of midpoints
// against a 29.14 fps source, which the self-test caught before any title run.
// The reason is structural, not a tuning error: with two items per source frame
// the consumer is 2.9 % faster than the producer, so the presentation phase of a
// SOURCE frame relative to its own capture slides by ~1 ms per pair until a
// midpoint's slot arrives before the midpoint can exist. A miss then drains the
// buffer, the phase jumps the other way, and the misses cascade.
//
// A 29.14 fps source cannot fill 60.0 slots per second with one midpoint each;
// the honest options are (a) 60 slots with ~3 % idle/repeat, or (b) evenly
// spaced output at twice the source rate, i.e. ~58.3 frames per second. This
// scheduler chooses (b), because a repeated frame is a visible artefact while a
// slightly slower uniform cadence is not.
//
// The two rules, applied after every presented item (both capture-anchored
// since R135):
//
//   after SOURCE k   -> next slot = capture(k) + 2*sourceDelay + halfPeriod
//                       (the midpoint for (k, k+1) targets its fixed slot)
//   after M(k, k+1)  -> next slot = capture(k+1) + 2 * sourceDelay
//                       (SOURCE k+1 appears two source periods after its own
//                        capture, the fixed pipeline delay)
//
// `sourceDelay` is measured from each SOURCE presentation, so it tracks the real
// source period instead of assuming 33.333 ms. The result is phase-locked: the
// display delay of a SOURCE frame is constant at 2*sourceDelay, every
// midpoint's slot lands `sourceDelay + halfPeriod` after its B capture -- 51.8
// ms against the measured 25.8-31.8 ms serial estimator+MetalFX service at
// ~30 fps source (R135; the R123 one-period anchor gave the midpoint only
// ~17.6 ms, which the measured service cannot fit). Both anchors are per-pair
// and capture-based, so the added latency is fixed, never accumulates, a
// dropped midpoint re-enters the schedule on the next pair without phase
// drift, and a capture hitch re-anchors instead of collapsing the schedule
// back into its pre-entry cascade.

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

typedef NS_ENUM(uint8_t, R123ItemKind) {
    R123ItemKindSource = 0,
    R123ItemKindInternal = 1,
};

typedef NS_ENUM(uint8_t, R123TickOutcome) {
    /// One item was popped and must be presented.
    R123TickPresented = 0,
    /// Nothing displayable this slot. The layer keeps its current content.
    R123TickIdle = 1,
    /// A midpoint reached the head unfilled. It was dropped; `outItem` is either
    /// the following SOURCE frame (present it) or nil (idle this slot).
    R123TickMissedInternal = 2,
};

/// Timeline position of a display item, as a strictly increasing integer:
/// SOURCE ordinal S is 2S, the midpoint between adjacent ordinals A and B is
/// 2A+1. Acquisition identity and AppFrameId do not define this ordering.
///
/// It is a separate C type on purpose: the negative control in
/// r123_pump_selftest.m feeds it a decreasing sequence and requires it to fail,
/// which is what makes "no reorder" in the positive cases mean anything.
typedef struct {
    uint64_t lastPosition;
    uint64_t violations;
    uint64_t gaps;
    BOOL havePosition;
} R123OrderTracker;

/// Returns YES when `position` continues the chain, NO when it does not.
/// A gap (position jumps by more than one) is counted but is not a violation:
/// it is the expected shape of a dropped midpoint.
BOOL R123OrderTrackerObserve(R123OrderTracker *tracker, uint64_t position);

/// One entry in the display queue. `payload` is opaque to this unit: the
/// observer stores the FG-owned MTLTexture it must copy from.
@interface R123DisplayItem : NSObject
@property (nonatomic) R123ItemKind kind;
@property (nonatomic) uint64_t seqA;
@property (nonatomic) uint64_t seqB;
@property (nonatomic) uint64_t appFrameId;
/// Logical SOURCE presentation position, independent of acquisition identity.
/// The observer assigns it at the title-present or FG-commit boundary.
@property (nonatomic) uint64_t sourcePresentOrdinal;
/// Presentation ordinals of the logical SOURCE endpoints for an INTERNAL.
@property (nonatomic) uint64_t sourcePresentOrdinalA;
@property (nonatomic) uint64_t sourcePresentOrdinalB;
@property (nonatomic) uint64_t internalPresentId;
@property (nonatomic, strong, nullable) id payload;
@property (nonatomic) uint64_t enqueueNs;
@property (nonatomic) uint64_t readyNs;
@property (nonatomic) BOOL ready;
/// Runtime epoch that accepted this item. The real presenter checks it again
/// immediately before WSI dispatch so work claimed by an old epoch cannot leak
/// across an enable/disable boundary.
@property (nonatomic) uint64_t runtimeEpoch;
@property (nonatomic) uint64_t priorOrderPosition;
@property (nonatomic) uint64_t priorOrderViolations;
@property (nonatomic) uint64_t priorOrderGaps;
@property (nonatomic) BOOL hadPriorOrderPosition;
@property (nonatomic, strong, nullable) R123DisplayItem *next;
@end

typedef struct {
    NSUInteger depthBefore;
    NSUInteger discardedSource;
    NSUInteger discardedInternal;
    NSUInteger depthAfter;
} R123RuntimeEpochResult;

/// Scalar identity copied while the pump lock is held. `present` distinguishes
/// an empty queue position from the zero-valued identity fields.
typedef struct {
    BOOL present;
    R123ItemKind kind;
    uint64_t seqA;
    uint64_t seqB;
    uint64_t sourcePresentOrdinal;
    uint64_t sourcePresentOrdinalA;
    uint64_t sourcePresentOrdinalB;
    uint64_t runtimeEpoch;
    BOOL ready;
    BOOL sourceReservationCommitted;
    BOOL sourceDispatched;
} R123DisplayItemSnapshot;

/// One-lock, read-only diagnostic copy of current pump state. SOURCE age is
/// measured from enqueue/capture time to the caller-supplied monotonic `nowNs`;
/// it is clamped to zero if the supplied instant precedes the item's timestamp.
/// Undispatched SOURCE extrema include committed queued and claimed items, and
/// oldest/newest select the minimum/maximum presentation ordinal respectively.
typedef struct {
    NSUInteger depth;
    R123DisplayItemSnapshot head;
    R123DisplayItemSnapshot next;
    R123DisplayItemSnapshot claimed;
    uint64_t claimedAtNs;
    uint64_t nextSlotNs;
    uint64_t sourcePeriodNs;
    NSUInteger activeSourceReservations;
    NSUInteger committedQueuedSourceCount;
    NSUInteger committedClaimedSourceCount;
    BOOL hasUndispatchedSource;
    uint64_t oldestUndispatchedSourceOrdinal;
    uint64_t oldestUndispatchedSourceSequence;
    uint64_t oldestUndispatchedSourceAgeNs;
    uint64_t newestUndispatchedSourceOrdinal;
    uint64_t newestUndispatchedSourceSequence;
    uint64_t newestUndispatchedSourceAgeNs;
    NSUInteger internalPlaceholderCount;
    NSUInteger internalReadyCount;
    uint64_t sourceEnqueued;
    uint64_t internalEnqueued;
    uint64_t internalResolved;
    uint64_t sourceDispatched;
    uint64_t internalDispatched;
    uint64_t presentedSource;
    uint64_t presentedInternal;
    uint64_t sourceDropped;
    uint64_t internalDropped;
    uint64_t internalDroppedUnresolved;
    uint64_t deadlineMisses;
} R123DiagnosticSnapshot;

@interface R123DisplayPump : NSObject

/// `halfPeriodNs` is the nominal spacing between two displayed frames. It is
/// only used to place the midpoint between its two SOURCE endpoints; the SOURCE
/// spacing itself is taken from the measured capture instants.
- (instancetype)initWithMaxDepth:(NSUInteger)maxDepth
                    halfPeriodNs:(uint64_t)halfPeriodNs NS_DESIGNATED_INITIALIZER;
- (instancetype)init NS_UNAVAILABLE;

/// Starts a clean runtime epoch only when no queued SOURCE remains. Pending
/// INTERNAL items are discarded; lifetime accounting and the monotonic order
/// tracker are retained. On refusal the runtime epoch does not advance.
- (R123RuntimeEpochResult)setRuntimeEnabled:(BOOL)enabled epoch:(uint64_t)epoch;
- (R123RuntimeEpochResult)beginSourceOnlyEpoch:(uint64_t)epoch;
/// Stops an empty source-only drain. Refuses while any SOURCE remains queued;
/// the observer must also fence claimed and dispatched SOURCE items.
- (R123RuntimeEpochResult)endSourceOnlyEpochForTitleDirect:(uint64_t)epoch;
- (BOOL)isEnabledForEpoch:(uint64_t)epoch;
- (uint64_t)runtimeEpoch;

/// Reserves one bounded SOURCE responsibility before the title present hook is
/// allowed to suppress the title drawable. The returned item is private pump
/// state and is not claimable until `commitSourceReservation:` succeeds. The
/// reservation counts against `activeSourceReservations` immediately and must
/// either be committed or canceled while still provisional.
- (nullable R123DisplayItem *)reserveSource:(uint64_t)sequence
                                 appFrameId:(uint64_t)appFrameId
                                    payload:(nullable id)payload
                                       atNs:(uint64_t)ns
                               runtimeEpoch:(uint64_t)epoch;
- (BOOL)commitSourceReservation:(R123DisplayItem *)reservation;
- (BOOL)cancelSourceReservation:(R123DisplayItem *)reservation;

/// The WSI dispatch frontier. A claimed item becomes terminal here, after an
/// identity has been allocated exactly once and immediately before dispatch.
/// It rejects duplicate, unclaimed, uncommitted, and out-of-order dispatches.
- (BOOL)markItemDispatched:(R123DisplayItem *)item
                wsiIdentity:(uint64_t)wsiIdentity;

/// Discards INTERNAL work rejected before dispatch. If passed a committed
/// SOURCE, it retries that same item at the head and retains its reservation.
- (void)rejectUndispatchedItem:(R123DisplayItem *)item;
/// Returns a claimed committed SOURCE to the queue head after a recoverable
/// pre-WSI failure. The reservation remains held and the item keeps priority.
- (BOOL)requeueUndispatchedSource:(R123DisplayItem *)item
                       forEpoch:(uint64_t)epoch;
/// Compatibility entrypoint. A committed SOURCE cannot be handed back to the
/// title; this method always returns NO.
- (BOOL)handBackUndispatchedSourceToTitle:(R123DisplayItem *)item;

// --- producer side -------------------------------------------------------

/// Convenience API for nontransactional callers: reserve and immediately
/// commit a SOURCE using the current runtime epoch. Title suppression paths
/// must use the explicit reservation/commit API above.
- (BOOL)enqueueSource:(uint64_t)sequence
           appFrameId:(uint64_t)appFrameId
              payload:(nullable id)payload
                 atNs:(uint64_t)ns;

/// Fills the placeholder for the pair (a, b). Returns NO when no unfilled
/// placeholder exists for that pair -- it was already dropped at a deadline, or
/// the queue never contained it -- in which case the caller must discard the
/// generated texture rather than present it out of order.
- (BOOL)resolveInternalForA:(uint64_t)a
                          b:(uint64_t)b
           internalPresentId:(uint64_t)internalPresentId
                    payload:(nullable id)payload
                       atNs:(uint64_t)ns;

/// Epoch-fenced form used by asynchronous generation completions.
- (BOOL)resolveInternalForA:(uint64_t)a
                          b:(uint64_t)b
           internalPresentId:(uint64_t)internalPresentId
                    payload:(nullable id)payload
                       atNs:(uint64_t)ns
               runtimeEpoch:(uint64_t)epoch;

// --- consumer side -------------------------------------------------------

/// Starts the slot sequence at `ns`. Idempotent: later calls are ignored.
- (void)primeAtNs:(uint64_t)ns;

/// The instant the next slot opens. Zero until primed.
@property (atomic, readonly) uint64_t nextSlotNs;

/// Pops at most one item for the slot opening at `nowNs`, and schedules the
/// following slot.
///
/// `outItem` is the item to present (nil for idle). `outMissed` is the dropped
/// midpoint, if a deadline was missed, so the caller can log its identity.
- (R123TickOutcome)takeForSlotAtNs:(uint64_t)nowNs
                           outItem:(R123DisplayItem * _Nullable * _Nullable)outItem
                         outMissed:(R123DisplayItem * _Nullable * _Nullable)outMissed;

/// How far behind its own capture the most recent SOURCE frame was displayed.
/// Diagnostic only; the schedule uses `sourcePeriodNs`.
@property (atomic, readonly) uint64_t measuredSourceDelayNs;

/// The SOURCE period measured from capture instants. This is what places the
/// next SOURCE frame relative to its own capture, so the logical display delay
/// is one source frame and does not drift with missed midpoints.
@property (atomic, readonly) uint64_t sourcePeriodNs;

- (NSUInteger)depth;

/// Copies queue shape, ownership counts, age extrema, and lifetime totals under
/// a single lock. The returned value contains no item/object references.
- (R123DiagnosticSnapshot)diagnosticSnapshotAtNs:(uint64_t)nowNs;

@property (atomic, readonly) uint64_t sourceEnqueued;
@property (atomic, readonly) uint64_t internalEnqueued;
@property (atomic, readonly) uint64_t internalResolved;
@property (atomic, readonly) uint64_t internalDroppedUnresolved;
@property (atomic, readonly) uint64_t deadlineMisses;
@property (atomic, readonly) uint64_t droppedInternal;
@property (atomic, readonly) uint64_t droppedSource;
@property (atomic, readonly) uint64_t sourceReturnedToTitle;
@property (atomic, readonly) uint64_t presentedSource;
/// Bounded SOURCE responsibilities, including provisional, queued, and
/// claimed items. A reservation is released only by cancellation before
/// commit or by `markItemDispatched:` after WSI dispatch becomes terminal.
@property (atomic, readonly) NSUInteger activeSourceReservations;
@property (atomic, readonly) NSUInteger sourceCapacity;
@property (atomic, readonly) uint64_t sourceDispatched;
@property (atomic, readonly) uint64_t presentedInternal;
@property (atomic, readonly) uint64_t idleTicks;
/// R136 capture-aware drop guard: how many times an unfilled midpoint's
/// deadline was deferred to capture(B) + measuredPeriod because the slot
/// fired before the pair's B could plausibly have been generated (the
/// capture-gap hitch shape). A placeholder may be re-deferred if the wake
/// overshoots the deferred slot while the measured period rose; every
/// target stays within capture(B) + 4*halfPeriod.
@property (atomic, readonly) uint64_t dropGuardDeferrals;
@property (atomic, readonly) uint64_t maxDepthSeen;
@property (atomic, readonly) uint64_t orderViolations;
@property (atomic, readonly) uint64_t orderGaps;

@end

NS_ASSUME_NONNULL_END
