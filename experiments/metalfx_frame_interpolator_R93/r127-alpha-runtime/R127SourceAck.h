#ifndef R127_SOURCE_ACK_H
#define R127_SOURCE_ACK_H

#include <stdbool.h>
#include <stdint.h>

// Bounded contiguous SOURCE-display acknowledgement tracker. A later
// presented callback cannot advance the frontier across a missing SOURCE.
#define R127_SOURCE_ACK_WINDOW 64u

typedef struct {
    uint64_t frontier;
    uint64_t pendingMask;
    uint64_t outOfWindow;
    bool initialized;
} R127SourceAckTracker;

static inline bool R127SourceSequenceAtOrAfter(uint64_t sequence,
                                               uint64_t minimumSequence) {
    return minimumSequence == 0 || sequence >= minimumSequence;
}

static inline void R127SourceAckBeginAtBoundary(R127SourceAckTracker *tracker,
                                                uint64_t boundarySequence) {
    if (!tracker) return;
    tracker->frontier = boundarySequence > 0 ? boundarySequence - 1 : 0;
    tracker->pendingMask = 0;
    tracker->initialized = boundarySequence > 0;
}

static inline bool R127SourceAckObserve(R127SourceAckTracker *tracker,
                                        uint64_t sequence) {
    if (!tracker || sequence == 0) return false;
    if (!tracker->initialized) {
        tracker->frontier = sequence - 1;
        tracker->initialized = true;
    }
    if (sequence <= tracker->frontier) return false;

    const uint64_t offset = sequence - tracker->frontier;
    if (offset > R127_SOURCE_ACK_WINDOW) {
        ++tracker->outOfWindow;
        return false;
    }
    tracker->pendingMask |= UINT64_C(1) << (offset - 1);
    const uint64_t oldFrontier = tracker->frontier;
    while (tracker->pendingMask & UINT64_C(1)) {
        ++tracker->frontier;
        tracker->pendingMask >>= 1;
    }
    return tracker->frontier != oldFrontier;
}

#endif
