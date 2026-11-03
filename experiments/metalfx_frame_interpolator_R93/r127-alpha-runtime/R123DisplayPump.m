#import "R123DisplayPump.h"

#include <pthread.h>

@interface R123DisplayItem ()
@property (nonatomic) BOOL sourceReservationHeld;
@property (nonatomic) BOOL sourceReservationCommitted;
@property (nonatomic) BOOL sourceDispatched;
@property (nonatomic) BOOL hasWsiIdentity;
@property (nonatomic) uint64_t wsiIdentity;
@property (nonatomic) uint64_t priorSourceSequence;
@property (nonatomic) uint64_t priorSourcePresentOrdinal;
@end

BOOL R123OrderTrackerObserve(R123OrderTracker *tracker, uint64_t position) {
    if (!tracker) return NO;
    if (tracker->havePosition) {
        if (position <= tracker->lastPosition) {
            ++tracker->violations;
            // Keep the stored position unchanged: a reorder must not be able to
            // re-anchor the chain and hide the next one behind it.
            return NO;
        }
        if (position != tracker->lastPosition + 1) ++tracker->gaps;
    }
    tracker->lastPosition = position;
    tracker->havePosition = YES;
    return YES;
}

@implementation R123DisplayItem
- (instancetype)init {
    self = [super init];
    if (self) {
        _kind = R123ItemKindSource;
    }
    return self;
}
@end

static uint64_t r123PresentationPosition(R123DisplayItem *item) {
    if (item.kind == R123ItemKindSource && item.sourcePresentOrdinal)
        return 2 * item.sourcePresentOrdinal;
    if (item.kind == R123ItemKindInternal && item.sourcePresentOrdinalA &&
        item.sourcePresentOrdinalB == item.sourcePresentOrdinalA + 1)
        return 2 * item.sourcePresentOrdinalA + 1;
    return 2 * item.seqA + (item.kind == R123ItemKindInternal ? 1 : 0);
}

@implementation R123DisplayPump {
    pthread_mutex_t _lock;
    R123DisplayItem *_head;
    R123DisplayItem *_tail;
    R123DisplayItem *_claimedItem;
    NSUInteger _maxDepth;
    NSUInteger _sourceCapacity;
    NSUInteger _depth;
    NSUInteger _activeSourceReservations;
    uint64_t _halfPeriodNs;
    uint64_t _runtimeEpoch;
    BOOL _runtimeEnabled;
    BOOL _sourceOnly;

    uint64_t _sourceEnqueued;
    uint64_t _internalEnqueued;
    uint64_t _internalResolved;
    uint64_t _internalDroppedUnresolved;
    uint64_t _deadlineMisses;
    uint64_t _droppedInternal;
    uint64_t _droppedSource;
    uint64_t _sourceReturnedToTitle;
    uint64_t _presentedSource;
    uint64_t _sourceDispatched;
    uint64_t _presentedInternal;
    uint64_t _idleTicks;
    uint64_t _dropGuardDeferrals;
    uint64_t _maxDepthSeen;

    uint64_t _lastSourceEnqueued;
    uint64_t _lastSourcePresentOrdinalEnqueued;
    uint64_t _lastCaptureNs;
    uint64_t _sourcePeriodNs;
    uint64_t _nextSlotNs;
    uint64_t _measuredSourceDelayNs;
    uint64_t _claimedAtNs;
    R123OrderTracker _order;
}

- (instancetype)initWithMaxDepth:(NSUInteger)maxDepth
                    halfPeriodNs:(uint64_t)halfPeriodNs {
    self = [super init];
    if (self) {
        pthread_mutex_init(&_lock, NULL);
        _maxDepth = maxDepth > 0 ? maxDepth : 8;
        // Every committed SOURCE owns one slot independently of queue state.
        // The claimed/in-flight item remains in this bound after it is unlinked.
        _sourceCapacity = _maxDepth;
        _halfPeriodNs = halfPeriodNs > 0 ? halfPeriodNs : 16666667ULL;
        _runtimeEnabled = YES;
    }
    return self;
}

- (void)dealloc {
    R123DisplayItem *cursor = _head;
    while (cursor) {
        R123DisplayItem *next = cursor.next;
        cursor.next = nil;
        cursor = next;
    }
    pthread_mutex_destroy(&_lock);
}

// --- queue primitives (caller holds _lock) --------------------------------

- (void)appendLocked:(R123DisplayItem *)item {
    item.next = nil;
    if (_tail) {
        _tail.next = item;
        _tail = item;
    } else {
        _head = item;
        _tail = item;
    }
    ++_depth;
    if (_depth > _maxDepthSeen) _maxDepthSeen = _depth;
}

- (void)unlinkLocked:(R123DisplayItem *)target {
    R123DisplayItem *previous = nil;
    R123DisplayItem *cursor = _head;
    while (cursor) {
        if (cursor == target) {
            if (previous) previous.next = cursor.next;
            else _head = cursor.next;
            if (_tail == cursor) _tail = previous;
            cursor.next = nil;
            if (_depth > 0) --_depth;
            return;
        }
        previous = cursor;
        cursor = cursor.next;
    }
}

- (void)insertLocked:(R123DisplayItem *)item before:(R123DisplayItem *)target {
    R123DisplayItem *previous = nil;
    R123DisplayItem *cursor = _head;
    while (cursor && cursor != target) {
        previous = cursor;
        cursor = cursor.next;
    }
    if (!cursor) return;
    item.next = cursor;
    if (previous) previous.next = item;
    else _head = item;
    ++_depth;
    if (_depth > _maxDepthSeen) _maxDepthSeen = _depth;
}

- (BOOL)requeueClaimedSourceLocked:(R123DisplayItem *)item {
    if (_claimedItem != item || item.kind != R123ItemKindSource ||
        !item.sourceReservationHeld || !item.sourceReservationCommitted ||
        item.sourceDispatched) return NO;
    item.next = _head;
    _head = item;
    if (!_tail) _tail = item;
    ++_depth;
    if (_depth > _maxDepthSeen) _maxDepthSeen = _depth;
    _claimedItem = nil;
    _claimedAtNs = 0;
    return YES;
}

- (R123RuntimeEpochResult)discardQueueLockedKeepingSources:(BOOL)keepSources
                                                    epoch:(uint64_t)epoch {
    R123RuntimeEpochResult result = {0};
    result.depthBefore = _depth;
    if (!keepSources) {
        for (R123DisplayItem *item = _head; item; item = item.next) {
            if (item.kind == R123ItemKindSource) {
                result.depthAfter = _depth;
                return result;
            }
        }
    }
    R123DisplayItem *previous = nil;
    R123DisplayItem *cursor = _head;
    while (cursor) {
        R123DisplayItem *next = cursor.next;
        if (!keepSources || cursor.kind != R123ItemKindSource) {
            if (previous) previous.next = next;
            else _head = next;
            if (_tail == cursor) _tail = previous;
            cursor.next = nil;
            if (_depth > 0) --_depth;
            ++result.discardedInternal;
            ++_droppedInternal;
        } else {
            // Re-epoch committed work for the active source-only drain. This
            // changes only pump dispatch context, never SOURCE ownership.
            if (cursor.sourceReservationCommitted) cursor.runtimeEpoch = epoch;
            previous = cursor;
        }
        cursor = next;
    }
    if (!_head) _tail = nil;
    _runtimeEpoch = epoch;
    _sourceOnly = keepSources;
    _lastSourceEnqueued = keepSources && _tail && _tail.kind == R123ItemKindSource
        ? _tail.seqB : 0;
    if (!keepSources) {
        _lastSourceEnqueued = 0;
        _lastCaptureNs = 0;
        _sourcePeriodNs = 0;
        _nextSlotNs = 0;
        _measuredSourceDelayNs = 0;
    }
    result.depthAfter = _depth;
    return result;
}

- (R123RuntimeEpochResult)setRuntimeEnabled:(BOOL)enabled epoch:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    R123RuntimeEpochResult result = { .depthBefore = _depth };
    if (epoch <= _runtimeEpoch) {
        result.depthAfter = _depth;
        pthread_mutex_unlock(&_lock);
        return result;
    }
    if (_activeSourceReservations != 0 || _claimedItem) {
        result.depthAfter = _depth;
        pthread_mutex_unlock(&_lock);
        return result;
    }
    result = [self discardQueueLockedKeepingSources:NO epoch:epoch];
    if (_runtimeEpoch == epoch) {
        _runtimeEnabled = enabled;
        _sourceOnly = NO;
    }
    pthread_mutex_unlock(&_lock);
    return result;
}

- (R123RuntimeEpochResult)beginSourceOnlyEpoch:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    R123RuntimeEpochResult result = { .depthBefore = _depth };
    if (epoch <= _runtimeEpoch) {
        result.depthAfter = _depth;
        pthread_mutex_unlock(&_lock);
        return result;
    }
    result = [self discardQueueLockedKeepingSources:YES epoch:epoch];
    _runtimeEnabled = YES;
    _sourceOnly = YES;
    pthread_mutex_unlock(&_lock);
    return result;
}

- (R123RuntimeEpochResult)endSourceOnlyEpochForTitleDirect:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    R123RuntimeEpochResult result = { .depthBefore = _depth };
    if (epoch <= _runtimeEpoch || !_sourceOnly) {
        result.depthAfter = _depth;
        pthread_mutex_unlock(&_lock);
        return result;
    }
    // A committed SOURCE reservation remains pump-owned through WSI dispatch,
    // including while claimed outside the linked queue. OFF cannot erase or
    // hand back any of that responsibility.
    if (_activeSourceReservations != 0 || _claimedItem) {
        result.depthAfter = _depth;
        pthread_mutex_unlock(&_lock);
        return result;
    }
    result.depthBefore = _depth;
    R123DisplayItem *cursor = _head;
    while (cursor) {
        R123DisplayItem *next = cursor.next;
        if (cursor.kind == R123ItemKindSource) {
            // Guard above makes this unreachable. Never convert committed
            // SOURCE ownership back to title ownership during OFF.
            pthread_mutex_unlock(&_lock);
            return result;
        } else {
            ++result.discardedInternal;
            ++_droppedInternal;
        }
        cursor.next = nil;
        cursor = next;
    }
    _head = nil;
    _tail = nil;
    _depth = 0;
    _runtimeEpoch = epoch;
    _runtimeEnabled = NO;
    _sourceOnly = NO;
    _lastSourceEnqueued = 0;
    _lastCaptureNs = 0;
    _sourcePeriodNs = 0;
    _nextSlotNs = 0;
    _measuredSourceDelayNs = 0;
    result.depthAfter = 0;
    pthread_mutex_unlock(&_lock);
    return result;
}

- (BOOL)isEnabledForEpoch:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    BOOL enabled = _runtimeEnabled && _runtimeEpoch == epoch;
    pthread_mutex_unlock(&_lock);
    return enabled;
}

- (uint64_t)runtimeEpoch {
    pthread_mutex_lock(&_lock);
    uint64_t epoch = _runtimeEpoch;
    pthread_mutex_unlock(&_lock);
    return epoch;
}

- (void)rejectUndispatchedItem:(R123DisplayItem *)item {
    if (!item) return;
    pthread_mutex_lock(&_lock);
    if (item.kind == R123ItemKindSource) {
        // A committed SOURCE cannot be rejected after its title drawable has
        // been suppressed. Keep the reservation and retry the same item first.
        (void)[self requeueClaimedSourceLocked:item];
    } else {
        if (_claimedItem == item && !item.sourceDispatched) {
            _claimedItem = nil;
            _claimedAtNs = 0;
            ++_droppedInternal;
        }
    }
    pthread_mutex_unlock(&_lock);
}

- (BOOL)requeueUndispatchedSource:(R123DisplayItem *)item
                       forEpoch:(uint64_t)epoch {
    if (!item || item.kind != R123ItemKindSource) return NO;
    pthread_mutex_lock(&_lock);
    // The ownership remains FG committed, but its retry must use the new live
    // pump epoch after OFF begins a source-only drain. The payload and logical
    // SOURCE identity are unchanged.
    if (_runtimeEnabled && _runtimeEpoch == epoch) item.runtimeEpoch = epoch;
    BOOL requeued = [self requeueClaimedSourceLocked:item];
    pthread_mutex_unlock(&_lock);
    return requeued;
}

- (BOOL)handBackUndispatchedSourceToTitle:(R123DisplayItem *)item {
    if (!item || item.kind != R123ItemKindSource) return NO;
    pthread_mutex_lock(&_lock);
    pthread_mutex_unlock(&_lock);
    return NO;
}

- (nullable R123DisplayItem *)reserveSource:(uint64_t)sequence
                                 appFrameId:(uint64_t)appFrameId
                                    payload:(nullable id)payload
                                       atNs:(uint64_t)ns
                               runtimeEpoch:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    BOOL duplicateIdentity = _claimedItem &&
        _claimedItem.kind == R123ItemKindSource && _claimedItem.seqB == sequence;
    for (R123DisplayItem *cursor = _head; cursor && !duplicateIdentity;
         cursor = cursor.next) {
        if (cursor.kind == R123ItemKindSource && cursor.seqB == sequence)
            duplicateIdentity = YES;
    }
    if (!_runtimeEnabled || _runtimeEpoch != epoch ||
        _activeSourceReservations >= _sourceCapacity || sequence == 0 ||
        duplicateIdentity) {
        pthread_mutex_unlock(&_lock);
        return nil;
    }

    // A queue slot is needed for the provisional token itself. INTERNAL work
    // is expendable and must be removed before refusing SOURCE ownership.
    const NSUInteger claimedSource = _claimedItem &&
        _claimedItem.kind == R123ItemKindSource ? 1 : 0;
    while (_depth + claimedSource >= _maxDepth) {
        R123DisplayItem *victim = _head;
        while (victim && victim.kind != R123ItemKindInternal) victim = victim.next;
        if (!victim) break;
        [self unlinkLocked:victim];
        ++_droppedInternal;
    }
    if (_depth + claimedSource >= _maxDepth) {
        pthread_mutex_unlock(&_lock);
        return nil;
    }

    R123DisplayItem *item = [R123DisplayItem new];
    item.kind = R123ItemKindSource;
    item.seqA = sequence;
    item.seqB = sequence;
    item.appFrameId = appFrameId;
    item.payload = payload;
    item.enqueueNs = ns;
    item.readyNs = ns;
    item.ready = YES;
    item.runtimeEpoch = epoch;
    item.sourceReservationHeld = YES;
    item.sourceReservationCommitted = NO;
    item.sourceDispatched = NO;
    // Standalone pump callers define their order by reservation order. The
    // observer overwrites this with its WSI-boundary ordinal before commit.
    item.sourcePresentOrdinal = _lastSourcePresentOrdinalEnqueued + 1;
    item.priorSourceSequence = _lastSourceEnqueued;
    item.priorSourcePresentOrdinal = _lastSourcePresentOrdinalEnqueued;
    [self appendLocked:item];
    ++_activeSourceReservations;
    pthread_mutex_unlock(&_lock);
    return item;
}

- (BOOL)commitSourceReservation:(R123DisplayItem *)reservation {
    if (!reservation || reservation.kind != R123ItemKindSource) return NO;
    pthread_mutex_lock(&_lock);
    if (!reservation.sourceReservationHeld ||
        reservation.sourceReservationCommitted ||
        reservation.sourceDispatched || !reservation.sourcePresentOrdinal ||
        reservation.sourcePresentOrdinal <= _lastSourcePresentOrdinalEnqueued) {
        pthread_mutex_unlock(&_lock);
        return NO;
    }

    // Midpoints remain optional. Insert their placeholder before this SOURCE
    // only after ownership is committed; if capacity is full, remove INTERNAL
    // work first, then omit the midpoint when SOURCE reservations fill it.
    if (!_sourceOnly && reservation.priorSourceSequence != 0 &&
        reservation.priorSourcePresentOrdinal != 0 &&
        reservation.sourcePresentOrdinal != 0 &&
        reservation.priorSourcePresentOrdinal != UINT64_MAX &&
        reservation.priorSourcePresentOrdinal + 1 ==
            reservation.sourcePresentOrdinal) {
        const NSUInteger claimedSource = _claimedItem &&
            _claimedItem.kind == R123ItemKindSource ? 1 : 0;
        while (_depth + claimedSource >= _maxDepth) {
            R123DisplayItem *victim = _head;
            while (victim && victim.kind != R123ItemKindInternal)
                victim = victim.next;
            if (!victim) break;
            [self unlinkLocked:victim];
            ++_droppedInternal;
        }
        if (_depth + claimedSource < _maxDepth) {
            R123DisplayItem *placeholder = [R123DisplayItem new];
            placeholder.kind = R123ItemKindInternal;
            placeholder.seqA = reservation.priorSourceSequence;
            placeholder.seqB = reservation.seqA;
            placeholder.sourcePresentOrdinalA =
                reservation.priorSourcePresentOrdinal;
            placeholder.sourcePresentOrdinalB =
                reservation.sourcePresentOrdinal;
            placeholder.enqueueNs = reservation.enqueueNs;
            placeholder.ready = NO;
            placeholder.runtimeEpoch = reservation.runtimeEpoch;
            [self insertLocked:placeholder before:reservation];
            ++_internalEnqueued;
        }
    }

    reservation.sourceReservationCommitted = YES;
    ++_sourceEnqueued;
    _lastSourceEnqueued = reservation.seqA;
    _lastSourcePresentOrdinalEnqueued = reservation.sourcePresentOrdinal;
    if (_lastCaptureNs != 0 && reservation.enqueueNs > _lastCaptureNs) {
        uint64_t period = reservation.enqueueNs - _lastCaptureNs;
        if (period < _halfPeriodNs) period = _halfPeriodNs;
        if (period > 4 * _halfPeriodNs) period = 4 * _halfPeriodNs;
        _sourcePeriodNs = period;
    }
    _lastCaptureNs = reservation.enqueueNs;
    pthread_mutex_unlock(&_lock);
    return YES;
}

- (BOOL)cancelSourceReservation:(R123DisplayItem *)reservation {
    if (!reservation || reservation.kind != R123ItemKindSource) return NO;
    pthread_mutex_lock(&_lock);
    if (!reservation.sourceReservationHeld ||
        reservation.sourceReservationCommitted ||
        reservation.sourceDispatched || _claimedItem == reservation) {
        pthread_mutex_unlock(&_lock);
        return NO;
    }
    BOOL hasLaterSource = NO;
    BOOL afterReservation = NO;
    for (R123DisplayItem *cursor = _head; cursor; cursor = cursor.next) {
        if (cursor == reservation) { afterReservation = YES; continue; }
        if (afterReservation && cursor.kind == R123ItemKindSource) {
            hasLaterSource = YES;
            break;
        }
    }
    if (hasLaterSource) {
        pthread_mutex_unlock(&_lock);
        return NO;
    }
    [self unlinkLocked:reservation];
    reservation.sourceReservationHeld = NO;
    if (_activeSourceReservations > 0) --_activeSourceReservations;
    pthread_mutex_unlock(&_lock);
    return YES;
}

- (BOOL)markItemDispatched:(R123DisplayItem *)item
                wsiIdentity:(uint64_t)wsiIdentity {
    if (!item) return NO;
    pthread_mutex_lock(&_lock);
    const BOOL source = item.kind == R123ItemKindSource;
    if (_claimedItem != item || item.sourceDispatched ||
        (source && (!item.sourceReservationHeld ||
                    !item.sourceReservationCommitted)) ||
        (!source && item.kind != R123ItemKindInternal) ||
        item.hasWsiIdentity) {
        pthread_mutex_unlock(&_lock);
        return NO;
    }

    const uint64_t position = r123PresentationPosition(item);
    if (_order.havePosition && position <= _order.lastPosition) {
        ++_order.violations;
        pthread_mutex_unlock(&_lock);
        return NO;
    }

    item.hasWsiIdentity = YES;
    item.wsiIdentity = wsiIdentity;
    item.sourceDispatched = YES;
    R123OrderTrackerObserve(&_order, position);
    if (source) {
        ++_sourceDispatched;
        ++_presentedSource;
        item.sourceReservationHeld = NO;
        if (_activeSourceReservations > 0) --_activeSourceReservations;
    } else {
        ++_presentedInternal;
    }
    _claimedItem = nil;
    _claimedAtNs = 0;
    pthread_mutex_unlock(&_lock);
    return YES;
}

// --- producer side -------------------------------------------------------

- (BOOL)enqueueSource:(uint64_t)sequence
           appFrameId:(uint64_t)appFrameId
              payload:(nullable id)payload
                 atNs:(uint64_t)ns {
    R123DisplayItem *reservation = [self reserveSource:sequence
        appFrameId:appFrameId payload:payload atNs:ns runtimeEpoch:self.runtimeEpoch];
    if (!reservation) return NO;
    if ([self commitSourceReservation:reservation]) return YES;
    (void)[self cancelSourceReservation:reservation];
    return NO;
}

- (BOOL)resolveInternalForA:(uint64_t)a
                          b:(uint64_t)b
           internalPresentId:(uint64_t)internalPresentId
                    payload:(nullable id)payload
                       atNs:(uint64_t)ns {
    return [self resolveInternalForA:a b:b internalPresentId:internalPresentId
                              payload:payload atNs:ns runtimeEpoch:self.runtimeEpoch];
}

- (BOOL)resolveInternalForA:(uint64_t)a
                          b:(uint64_t)b
           internalPresentId:(uint64_t)internalPresentId
                    payload:(nullable id)payload
                       atNs:(uint64_t)ns
               runtimeEpoch:(uint64_t)epoch {
    pthread_mutex_lock(&_lock);
    if (!_runtimeEnabled || _sourceOnly || _runtimeEpoch != epoch) {
        pthread_mutex_unlock(&_lock);
        return NO;
    }
    R123DisplayItem *cursor = _head;
    while (cursor) {
        if (cursor.kind == R123ItemKindInternal && !cursor.ready &&
            cursor.seqA == a && cursor.seqB == b) {
            cursor.ready = YES;
            cursor.readyNs = ns;
            cursor.internalPresentId = internalPresentId;
            cursor.payload = payload;
            ++_internalResolved;
            pthread_mutex_unlock(&_lock);
            return YES;
        }
        cursor = cursor.next;
    }
    pthread_mutex_unlock(&_lock);
    return NO;
}

// --- consumer side -------------------------------------------------------

- (void)primeAtNs:(uint64_t)ns {
    pthread_mutex_lock(&_lock);
    if (_nextSlotNs == 0) _nextSlotNs = ns;
    pthread_mutex_unlock(&_lock);
}

- (uint64_t)nextSlotNs {
    pthread_mutex_lock(&_lock);
    uint64_t value = _nextSlotNs;
    pthread_mutex_unlock(&_lock);
    return value;
}

- (uint64_t)sourcePeriodNs {
    pthread_mutex_lock(&_lock);
    uint64_t value = _sourcePeriodNs;
    pthread_mutex_unlock(&_lock);
    return value;
}

- (uint64_t)measuredSourceDelayNs {
    pthread_mutex_lock(&_lock);
    uint64_t value = _measuredSourceDelayNs;
    pthread_mutex_unlock(&_lock);
    return value;
}

- (R123TickOutcome)takeForSlotAtNs:(uint64_t)nowNs
                           outItem:(R123DisplayItem * _Nullable * _Nullable)outItem
                         outMissed:(R123DisplayItem * _Nullable * _Nullable)outMissed {
    if (outItem) *outItem = nil;
    if (outMissed) *outMissed = nil;

    pthread_mutex_lock(&_lock);
    R123TickOutcome outcome = R123TickPresented;

    // There is one presentation worker. Do not let later queue entries pass a
    // SOURCE or INTERNAL item that has been claimed but not yet terminalized at
    // the WSI dispatch frontier.
    if (_claimedItem) {
        ++_idleTicks;
        _nextSlotNs = nowNs + _halfPeriodNs;
        pthread_mutex_unlock(&_lock);
        return R123TickIdle;
    }

    if (_head && _head.kind == R123ItemKindInternal && !_head.ready) {
        // R136 capture-aware drop guard. The slot for M(A,B) is anchored on
        // capture(A) + 2*period + halfPeriod, where `period` is the measured
        // (and clamped) previous source interval. When the title's capture
        // stream hitches, the true A->B gap can exceed the clamp: the slot
        // then fires BEFORE this pair's B was even captured, and generation
        // -- which cannot start until B exists -- is structurally unable to
        // make it. Dropping at that instant wastes a midpoint that would
        // have been ready one measured period after B's capture. Defer the
        // drop once, to capture(B) + period: if the midpoint resolves by
        // then it presents and rule 2 re-anchors B exactly as in the
        // no-miss schedule; if it does not, the drop takes place with B
        // still presented no later than its normal two-period anchor. The
        // guard normally fires once per placeholder (after the deferral,
        // now >= capture(B) + period). If the wake overshoots the deferred
        // slot and a commit raised the measured period in between, it can
        // re-fire; each re-fire re-anchors to capture(B) + P_current, which
        // the [H, 4H] clamp bounds at capture(B) + 4*halfPeriod, so the drop
        // stays bounded and non-accumulating. The guard never touches the
        // successful-pair schedule, whose slots are always
        // capture(B) + period + halfPeriod.
        if (_sourcePeriodNs != 0 && _head.enqueueNs != 0 &&
            nowNs < _head.enqueueNs + _sourcePeriodNs) {
            _nextSlotNs = _head.enqueueNs + _sourcePeriodNs;
            ++_dropGuardDeferrals;
            ++_idleTicks;
            pthread_mutex_unlock(&_lock);
            return R123TickIdle;
        }
        R123DisplayItem *missed = _head;
        [self unlinkLocked:missed];
        ++_deadlineMisses;
        ++_droppedInternal;
        ++_internalDroppedUnresolved;
        if (outMissed) *outMissed = missed;
        outcome = R123TickMissedInternal;
    }

    R123DisplayItem *item = _head;
    if (item && item.kind == R123ItemKindSource &&
        !item.sourceReservationCommitted) {
        ++_idleTicks;
        _nextSlotNs = nowNs + _halfPeriodNs;
        pthread_mutex_unlock(&_lock);
        return R123TickIdle;
    }
    if (!item) {
        if (outcome != R123TickMissedInternal) {
            ++_idleTicks;
            outcome = R123TickIdle;
        }
        // Nothing to show: keep the cadence rather than stalling the pump.
        _nextSlotNs = nowNs + _halfPeriodNs;
        pthread_mutex_unlock(&_lock);
        return outcome;
    }

    while (item) {
        const uint64_t itemPosition = r123PresentationPosition(item);
        if (!_order.havePosition || itemPosition > _order.lastPosition) break;

        // Mechanical guard: the item stays pending and cannot be dispatched.
        // INTERNAL is expendable; drop it and re-check the newly exposed head.
        ++_order.violations;
        if (item.kind != R123ItemKindInternal) {
            ++_idleTicks;
            _nextSlotNs = nowNs + _halfPeriodNs;
            pthread_mutex_unlock(&_lock);
            return R123TickIdle;
        }
        [self unlinkLocked:item];
        ++_droppedInternal;
        outcome = R123TickMissedInternal;
        item = _head;
        if (item && item.kind == R123ItemKindSource &&
            !item.sourceReservationCommitted) item = nil;
    }
    if (!item) {
        _nextSlotNs = nowNs + _halfPeriodNs;
        pthread_mutex_unlock(&_lock);
        return outcome;
    }

    [self unlinkLocked:item];
    item.priorOrderPosition = _order.lastPosition;
    item.hadPriorOrderPosition = _order.havePosition;
    item.priorOrderViolations = _order.violations;
    item.priorOrderGaps = _order.gaps;
    _claimedItem = item;
    _claimedAtNs = nowNs;

    if (item.kind == R123ItemKindInternal) {
        // Rule 2 (R135 fixed-latency pipeline): the SOURCE frame that ends this
        // pair appears two measured source periods after its own capture. One
        // period (the R123 anchor) left the midpoint a 17.2 ms budget against
        // 25.8-31.8 ms of serial estimator+MetalFX work; two periods give the
        // midpoint a 51.8 ms budget while rule 1 below keeps it exactly one
        // half-period after the preceding SOURCE. The anchor is per-pair and
        // capture-based, so latency never accumulates and a dropped midpoint
        // cannot shift the phase. Delaying the midpoint target instead (the
        // naive "+= sourcePeriod" on rule 1) would push every SOURCE present
        // half a period later per frame and overflow the reservation bound —
        // that variant is excluded by design.
        R123DisplayItem *next = _head;
        if (_sourcePeriodNs != 0 && next && next.kind == R123ItemKindSource &&
            next.seqB == item.seqB) {
            _nextSlotNs = next.enqueueNs + 2 * _sourcePeriodNs;
        } else {
            _nextSlotNs = nowNs + _halfPeriodNs;
        }
    } else {
        // Rule 1 (R135, capture-anchored): the midpoint for (this, next)
        // targets its fixed capture-based slot, capture(this) + 2*period +
        // halfPeriod — the same slot it holds in the locked steady state.
        // Anchoring on the present time (the R123 form) makes the schedule
        // unable to enter its own steady state. In the pre-entry cascade the
        // realized SOURCE present is p_n ≈ capture(n) + 8.4 ms (the drop tick
        // precedes B's commit, so B waits one idle tick), which puts the
        // midpoint slot for pair (n, n+1) at p_n + H ≈ capture(n) + 25 ms —
        // ~9 ms before capture(n+1). The same cascade shape is visible in the
        // pair record keyed by B: M(B,C)'s slot sits capture(B) + 25 ms while
        // M(B,C) is ready at capture(B) + P + W, i.e. ready − slot = +3.5 ms
        // median (R135 field window, n = 380). Every midpoint is structurally
        // dropped and no rule-2 anchor ever fires. Capture-anchoring makes
        // entry deterministic (pair 2 locks) and keeps capture hitches from
        // knocking the schedule back into the cascade: a late or early tick
        // shifts only that item's own present, never the next slot.
        if (_sourcePeriodNs != 0) {
            _nextSlotNs = item.enqueueNs + 2 * _sourcePeriodNs + _halfPeriodNs;
        } else {
            _nextSlotNs = nowNs + _halfPeriodNs;
        }
        // Rule 2's input: how far behind its own capture this SOURCE frame was.
        // The pipeline delay is two source periods, so the realized delay sits
        // near 2*period + tick lateness; the band must cover it without
        // saturating (R135: was [H, 3H], which would clamp every frame).
        if (nowNs > item.enqueueNs) {
            uint64_t delay = nowNs - item.enqueueNs;
            if (delay < _halfPeriodNs) delay = _halfPeriodNs;
            if (delay > 6 * _halfPeriodNs) delay = 6 * _halfPeriodNs;
            _measuredSourceDelayNs = delay;
        }
    }

    pthread_mutex_unlock(&_lock);

    if (outItem) *outItem = item;
    return outcome;
}

- (NSUInteger)depth {
    pthread_mutex_lock(&_lock);
    NSUInteger value = _depth;
    pthread_mutex_unlock(&_lock);
    return value;
}

static R123DisplayItemSnapshot r123SnapshotItem(R123DisplayItem *item) {
    R123DisplayItemSnapshot snapshot = {0};
    if (!item) return snapshot;
    snapshot.present = YES;
    snapshot.kind = item.kind;
    snapshot.seqA = item.seqA;
    snapshot.seqB = item.seqB;
    snapshot.sourcePresentOrdinal = item.sourcePresentOrdinal;
    snapshot.sourcePresentOrdinalA = item.sourcePresentOrdinalA;
    snapshot.sourcePresentOrdinalB = item.sourcePresentOrdinalB;
    snapshot.runtimeEpoch = item.runtimeEpoch;
    snapshot.ready = item.ready;
    snapshot.sourceReservationCommitted = item.sourceReservationCommitted;
    snapshot.sourceDispatched = item.sourceDispatched;
    return snapshot;
}

static uint64_t r123AgeAt(uint64_t nowNs, uint64_t thenNs) {
    return nowNs > thenNs ? nowNs - thenNs : 0;
}

- (R123DiagnosticSnapshot)diagnosticSnapshotAtNs:(uint64_t)nowNs {
    pthread_mutex_lock(&_lock);
    R123DiagnosticSnapshot snapshot = {0};
    snapshot.depth = _depth;
    snapshot.head = r123SnapshotItem(_head);
    snapshot.next = r123SnapshotItem(_head ? _head.next : nil);
    snapshot.claimed = r123SnapshotItem(_claimedItem);
    snapshot.claimedAtNs = _claimedAtNs;
    snapshot.nextSlotNs = _nextSlotNs;
    snapshot.sourcePeriodNs = _sourcePeriodNs;
    snapshot.activeSourceReservations = _activeSourceReservations;

    R123DisplayItem *items[2] = { _head, _claimedItem };
    for (NSUInteger group = 0; group < 2; ++group) {
        for (R123DisplayItem *item = items[group]; item;
             item = group == 0 ? item.next : nil) {
            if (item.kind == R123ItemKindSource) {
                if (item.sourceReservationCommitted && !item.sourceDispatched) {
                    const uint64_t ordinal = item.sourcePresentOrdinal;
                    const uint64_t age = r123AgeAt(nowNs, item.enqueueNs);
                    if (group == 0) ++snapshot.committedQueuedSourceCount;
                    else ++snapshot.committedClaimedSourceCount;
                    if (!snapshot.hasUndispatchedSource || ordinal <
                        snapshot.oldestUndispatchedSourceOrdinal) {
                        snapshot.oldestUndispatchedSourceOrdinal = ordinal;
                        snapshot.oldestUndispatchedSourceSequence = item.seqB;
                        snapshot.oldestUndispatchedSourceAgeNs = age;
                    }
                    if (!snapshot.hasUndispatchedSource || ordinal >
                        snapshot.newestUndispatchedSourceOrdinal) {
                        snapshot.newestUndispatchedSourceOrdinal = ordinal;
                        snapshot.newestUndispatchedSourceSequence = item.seqB;
                        snapshot.newestUndispatchedSourceAgeNs = age;
                    }
                    snapshot.hasUndispatchedSource = YES;
                }
            } else if (item.kind == R123ItemKindInternal) {
                if (item.ready) ++snapshot.internalReadyCount;
                else ++snapshot.internalPlaceholderCount;
            }
        }
    }

    snapshot.sourceEnqueued = _sourceEnqueued;
    snapshot.internalEnqueued = _internalEnqueued;
    snapshot.internalResolved = _internalResolved;
    snapshot.sourceDispatched = _sourceDispatched;
    snapshot.internalDispatched = _presentedInternal;
    snapshot.presentedSource = _presentedSource;
    snapshot.presentedInternal = _presentedInternal;
    snapshot.sourceDropped = _droppedSource;
    snapshot.internalDropped = _droppedInternal;
    snapshot.internalDroppedUnresolved = _internalDroppedUnresolved;
    snapshot.deadlineMisses = _deadlineMisses;
    pthread_mutex_unlock(&_lock);
    return snapshot;
}

- (uint64_t)sourceEnqueued { pthread_mutex_lock(&_lock); uint64_t v = _sourceEnqueued; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)internalEnqueued { pthread_mutex_lock(&_lock); uint64_t v = _internalEnqueued; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)internalResolved { pthread_mutex_lock(&_lock); uint64_t v = _internalResolved; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)internalDroppedUnresolved { pthread_mutex_lock(&_lock); uint64_t v = _internalDroppedUnresolved; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)deadlineMisses { pthread_mutex_lock(&_lock); uint64_t v = _deadlineMisses; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)droppedInternal { pthread_mutex_lock(&_lock); uint64_t v = _droppedInternal; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)droppedSource { pthread_mutex_lock(&_lock); uint64_t v = _droppedSource; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)sourceReturnedToTitle { pthread_mutex_lock(&_lock); uint64_t v = _sourceReturnedToTitle; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)presentedSource { pthread_mutex_lock(&_lock); uint64_t v = _presentedSource; pthread_mutex_unlock(&_lock); return v; }
- (NSUInteger)activeSourceReservations { pthread_mutex_lock(&_lock); NSUInteger v = _activeSourceReservations; pthread_mutex_unlock(&_lock); return v; }
- (NSUInteger)sourceCapacity { return _sourceCapacity; }
- (uint64_t)sourceDispatched { pthread_mutex_lock(&_lock); uint64_t v = _sourceDispatched; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)presentedInternal { pthread_mutex_lock(&_lock); uint64_t v = _presentedInternal; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)idleTicks { pthread_mutex_lock(&_lock); uint64_t v = _idleTicks; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)dropGuardDeferrals { pthread_mutex_lock(&_lock); uint64_t v = _dropGuardDeferrals; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)maxDepthSeen { pthread_mutex_lock(&_lock); uint64_t v = _maxDepthSeen; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)orderViolations { pthread_mutex_lock(&_lock); uint64_t v = _order.violations; pthread_mutex_unlock(&_lock); return v; }
- (uint64_t)orderGaps { pthread_mutex_lock(&_lock); uint64_t v = _order.gaps; pthread_mutex_unlock(&_lock); return v; }

@end
