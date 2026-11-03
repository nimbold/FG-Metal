// R123 -- deterministic self-test for the display pump's decision logic.
//
// This host drives R123DisplayPump with synthetic timelines whose ground truth
// is known by construction, and asserts what the pump must do with them. It is
// the falsifiability evidence for the ordering and fallback rules: every case
// states an outcome the implementation could produce wrongly, and case F feeds
// the order checker a duplicate and a backwards frame to prove it can fail.
//
// It does not exercise Metal, CAMetalLayer, the title or any clock. What it
// proves is that the buffer can only ever emit A -> M(A,B) -> B -> M(B,C) -> C,
// that a lost midpoint costs exactly one interpolation, and that a SOURCE frame
// is never duplicated or reordered while the buffer stays inside its bound.
//
// Case B is the one that changed the design. The first scheduler free-ran on a
// fixed 16.667 ms grid and lost 33 of 59 midpoints against a 29.14 fps source;
// the cause was phase drift, and the anchored two-rule schedule above it is the
// fix. That case is kept because it is the regression test for that defect.
//
// Build/run: ./build_r123_tools.sh && ./r123_pump_selftest

#import <Foundation/Foundation.h>
#import "R123DisplayPump.h"

#include <stdio.h>
#include <stdlib.h>

static const uint64_t kMs = 1000000ULL;
static const uint64_t kHalfPeriodNs = 16666667ULL;
static uint64_t nextWsiIdentity = 1;
static int failures = 0;

static BOOL dispatchItem(R123DisplayPump *pump, R123DisplayItem *item) {
    if (!item) return NO;
    return [pump markItemDispatched:item wsiIdentity:nextWsiIdentity++];
}

typedef struct {
    uint64_t sourceIntervalNs;
    uint64_t generationNs;      // 0 == never resolves (warm-up / failure)
    uint64_t halfPeriodNs;
    int sourceCount;
    NSUInteger maxDepth;
    BOOL verbose;
    // R135 pipeline scenario extensions. All optional (0 = unset).
    int slowPairSeq;            // this pair's midpoint resolves late
    uint64_t slowPairGenerationNs;
    uint64_t sourceInterval2Ns; // source period changes at switchAtSeq
    int switchAtSeq;            // first sequence using sourceInterval2Ns
    // R136 capture-gap extension: the capture of gapAtSeq lands gapNs later
    // than the nominal grid (a title capture hitch), so the pair
    // (gapAtSeq-1, gapAtSeq) spans a materially abnormal source interval.
    int gapAtSeq;               // sequence whose capture is delayed
    uint64_t gapNs;             // extra capture delay for that sequence
} R123Scenario;

// Dispatch trace: every successful WSI dispatch, in order. Lets the pipeline
// cases assert realized presentation times, not just counters.
typedef struct {
    uint64_t t;
    uint64_t seqB;
    int kind;   // 0 = source, 1 = internal
} R123Dispatch;
static R123Dispatch r123DispatchTrace[8192];
static int r123DispatchCount;

static void r123RecordDispatch(uint64_t t, R123DisplayItem *item) {
    if (!item || r123DispatchCount >= (int)(sizeof(r123DispatchTrace) / sizeof(r123DispatchTrace[0]))) return;
    r123DispatchTrace[r123DispatchCount++] = (R123Dispatch){
        .t = t, .seqB = item.seqB,
        .kind = item.kind == R123ItemKindInternal ? 1 : 0 };
}

typedef struct {
    uint64_t presentedSource;
    uint64_t presentedInternal;
    uint64_t deadlineMisses;
    uint64_t droppedInternal;
    uint64_t droppedSource;
    uint64_t orderViolations;
    uint64_t orderGaps;
    uint64_t idleTicks;
    uint64_t maxDepthSeen;
    uint64_t internalEnqueued;
    uint64_t internalResolved;
    uint64_t internalDroppedUnresolved;
    uint64_t sourceEnqueued;
    uint64_t sourceRefused;
    uint64_t measuredSourceDelayNs;
    uint64_t sourcePeriodNs;
    uint64_t dropGuardDeferrals;
} R123Result;

typedef struct {
    uint64_t t;
    int kind;   // 0 = source capture, 1 = generation resolve
    uint64_t seq;
} R123Event;

static int compareEvents(const void *lhs, const void *rhs) {
    const R123Event *a = (const R123Event *)lhs;
    const R123Event *b = (const R123Event *)rhs;
    if (a->t != b->t) return a->t < b->t ? -1 : 1;
    if (a->kind != b->kind) return a->kind < b->kind ? -1 : 1;
    return 0;
}

static R123Result runScenario(R123Scenario scenario) {
    R123DisplayPump *pump = [[R123DisplayPump alloc] initWithMaxDepth:scenario.maxDepth
                                                         halfPeriodNs:scenario.halfPeriodNs];
    r123DispatchCount = 0;

    const int n = scenario.sourceCount;
    R123Event *events = calloc((size_t)n * 2 + 16, sizeof(R123Event));
    size_t count = 0;

    const uint64_t firstSourceNs = 100 * kMs;
    for (int i = 0; i < n; ++i) {
        uint64_t t;
        if (scenario.switchAtSeq > 1 && scenario.sourceInterval2Ns != 0 &&
            (uint64_t)(i + 1) >= (uint64_t)scenario.switchAtSeq) {
            // Phase 2 anchors on the last phase-1 capture so the period change
            // is a clean source-boundary step.
            const int lastPhase1Index = scenario.switchAtSeq - 2;
            const uint64_t base = firstSourceNs +
                (uint64_t)lastPhase1Index * scenario.sourceIntervalNs;
            t = base + (uint64_t)(i - lastPhase1Index) * scenario.sourceInterval2Ns;
        } else {
            t = firstSourceNs + (uint64_t)i * scenario.sourceIntervalNs;
        }
        if (scenario.gapAtSeq > 0 && scenario.gapNs != 0 &&
            (uint64_t)(i + 1) >= (uint64_t)scenario.gapAtSeq) {
            // The hitch delays this capture and everything after it: the
            // source stream stalls for gapNs, then resumes its grid.
            t += scenario.gapNs;
        }
        events[count++] = (R123Event){ .t = t, .kind = 0, .seq = (uint64_t)(i + 1) };
        if (scenario.generationNs > 0 && i > 0) {
            const BOOL slow = scenario.slowPairSeq > 0 &&
                (uint64_t)(i + 1) == (uint64_t)scenario.slowPairSeq;
            const uint64_t gen = slow && scenario.slowPairGenerationNs != 0
                ? scenario.slowPairGenerationNs : scenario.generationNs;
            events[count++] = (R123Event){ .t = t + gen, .kind = 1,
                                           .seq = (uint64_t)(i + 1) };
        }
    }
    qsort(events, count, sizeof(R123Event), compareEvents);

    // Horizon covers the real last capture (two-phase scenarios extend past
    // the phase-1 grid) plus drain room for the two-period pipeline delay.
    uint64_t lastCaptureNs = firstSourceNs + (uint64_t)(n - 1) * scenario.sourceIntervalNs;
    if (scenario.switchAtSeq > 1 && scenario.sourceInterval2Ns != 0) {
        lastCaptureNs = firstSourceNs + (uint64_t)(scenario.switchAtSeq - 2) *
            scenario.sourceIntervalNs +
            (uint64_t)(n - (scenario.switchAtSeq - 1)) * scenario.sourceInterval2Ns;
    }
    const uint64_t horizonNs = lastCaptureNs + 300 * kMs;
    size_t index = 0;
    uint64_t now = 0;
    BOOL primed = NO;
    uint64_t sourceRefused = 0;

    while (YES) {
        // Stop only once every event has been consumed *and* the buffer has
        // drained. Checking after the slot test would keep the clock running
        // past the last frame and manufacture tail idles.
        if (index >= count && (!primed || [pump depth] == 0)) break;

        // After the last synthetic event, drain only scheduled slots through
        // the finite horizon. A mechanically blocked pump must fail its
        // accounting checks instead of reading beyond the event array.
        if (index >= count) {
            if (!primed || pump.nextSlotNs == 0 || pump.nextSlotNs > horizonNs) break;
            now = pump.nextSlotNs;
            R123DisplayItem *drainItem = nil;
            R123DisplayItem *drainMissed = nil;
            (void)[pump takeForSlotAtNs:now outItem:&drainItem outMissed:&drainMissed];
            if (drainItem) {
                if (!dispatchItem(pump, drainItem)) ++failures;
                r123RecordDispatch(now, drainItem);
            }
            continue;
        }

        const uint64_t nextEventNs = index < count ? events[index].t : UINT64_MAX;
        const uint64_t nextSlot = primed ? pump.nextSlotNs : 0;
        const BOOL slotDue = primed && nextSlot != 0 && nextSlot <= nextEventNs &&
                             nextSlot <= horizonNs;

        if (slotDue) {
            now = nextSlot;
            R123DisplayItem *item = nil;
            R123DisplayItem *missed = nil;
            R123TickOutcome outcome = [pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            if (scenario.verbose) {
                if (missed) {
                    printf("  t=%9.3fms MISS  M(%llu,%llu)\n", (double)now / 1e6,
                           (unsigned long long)missed.seqA,
                           (unsigned long long)missed.seqB);
                }
                if (item) {
                    if (item.kind == R123ItemKindInternal) {
                        printf("  t=%9.3fms SLOT  M(%llu,%llu)\n", (double)now / 1e6,
                               (unsigned long long)item.seqA,
                               (unsigned long long)item.seqB);
                    } else {
                        printf("  t=%9.3fms SLOT  SOURCE %llu\n", (double)now / 1e6,
                               (unsigned long long)item.seqB);
                    }
                } else if (outcome == R123TickIdle) {
                    printf("  t=%9.3fms SLOT  idle\n", (double)now / 1e6);
                }
            }
            if (item && !dispatchItem(pump, item)) {
                fprintf(stderr, "pump rejected WSI dispatch for item at t=%llu\n",
                        (unsigned long long)now);
                ++failures;
            }
            if (item) r123RecordDispatch(now, item);
            continue;
        }

        now = nextEventNs;
        R123Event event = events[index++];
        if (event.kind == 0) {
            if (![pump enqueueSource:event.seq appFrameId:event.seq payload:@(event.seq) atNs:event.t])
                ++sourceRefused;
            if (scenario.verbose) {
                printf("  t=%9.3fms CAP   SOURCE %llu (depth %lu)\n", (double)event.t / 1e6,
                       (unsigned long long)event.seq, (unsigned long)[pump depth]);
            }
            // The buffer is primed the instant one full frame of logical delay
            // exists, i.e. when the second SOURCE frame arrives.
            if (!primed && event.seq >= 2) {
                [pump primeAtNs:event.t];
                primed = YES;
                if (scenario.verbose) printf("  t=%9.3fms PRIME\n", (double)event.t / 1e6);
            }
        } else {
            if (event.seq > 1) {
                [pump resolveInternalForA:event.seq - 1 b:event.seq
                        internalPresentId:event.seq * 1000
                                 payload:@(event.seq) atNs:event.t];
            }
        }
    }

    free(events);

    return (R123Result){
        .presentedSource = [pump presentedSource],
        .presentedInternal = [pump presentedInternal],
        .deadlineMisses = [pump deadlineMisses],
        .droppedInternal = [pump droppedInternal],
        .droppedSource = [pump droppedSource],
        .orderViolations = [pump orderViolations],
        .orderGaps = [pump orderGaps],
        .idleTicks = [pump idleTicks],
        .maxDepthSeen = [pump maxDepthSeen],
        .internalEnqueued = [pump internalEnqueued],
        .internalResolved = [pump internalResolved],
        .internalDroppedUnresolved = [pump internalDroppedUnresolved],
        .sourceEnqueued = [pump sourceEnqueued],
        .sourceRefused = sourceRefused,
        .measuredSourceDelayNs = [pump measuredSourceDelayNs],
        .sourcePeriodNs = [pump sourcePeriodNs],
        .dropGuardDeferrals = [pump dropGuardDeferrals],
    };
}

static void report(const char *label, R123Result r) {
    printf("  [%s] src=%llu/%llu(+%llu refused) int=%llu miss=%llu dropInt=%llu dropSrc=%llu "
           "gaps=%llu viol=%llu idle=%llu depth=%llu period=%.3fms delay=%.3fms\n",
           label,
           (unsigned long long)r.presentedSource, (unsigned long long)r.sourceEnqueued,
           (unsigned long long)r.sourceRefused,
           (unsigned long long)r.presentedInternal, (unsigned long long)r.deadlineMisses,
           (unsigned long long)r.droppedInternal, (unsigned long long)r.droppedSource,
           (unsigned long long)r.orderGaps, (unsigned long long)r.orderViolations,
           (unsigned long long)r.idleTicks, (unsigned long long)r.maxDepthSeen,
           (double)r.sourcePeriodNs / 1e6, (double)r.measuredSourceDelayNs / 1e6);
}

static void check(const char *name, BOOL condition, const char *detail) {
    printf("  %-48s %s%s%s\n", name, condition ? "PASS" : "FAIL",
           detail && detail[0] ? "  " : "", detail ? detail : "");
    if (!condition) ++failures;
}

int main(void) {
    @autoreleasepool {
        printf("R123 display pump self-test\n");

        // --- Case A: exactly 30 fps SOURCE ------------------------------
        // The nominal case. Two items per frame and two slots per frame line up
        // exactly, so the output is a true 60 Hz with nothing lost.
        printf("\nCase A  30.000 fps SOURCE, generation 10.686 ms\n");
        {
            R123Scenario s = { .sourceIntervalNs = 33333333, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 60,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("A", r);
            check("every midpoint generated and shown", r.presentedInternal == 59, NULL);
            check("every SOURCE frame shown exactly once", r.presentedSource == 60, NULL);
            check("no deadline miss", r.deadlineMisses == 0, NULL);
            check("no idle slot", r.idleTicks == 0, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("no gap in the display chain", r.orderGaps == 0, NULL);
            check("buffer stayed inside its bound", r.maxDepthSeen <= 8, NULL);
            check("SOURCE accounting closes",
                  r.presentedSource + r.droppedSource == r.sourceEnqueued, NULL);
            check("INTERNAL accounting closes",
                  r.presentedInternal + r.droppedInternal == r.internalEnqueued, NULL);
        }

        // --- Case B: the measured 29.14 fps SOURCE ----------------------
        // This is the live title's real cadence (34.3 ms acquisition). Output
        // must stay uniform at twice the source rate with no lost midpoint;
        // this is the case the fixed-grid scheduler failed.
        printf("\nCase B  29.14 fps SOURCE (measured title cadence)\n");
        {
            R123Scenario s = { .sourceIntervalNs = 34317000, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 120,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("B", r);
            check("every midpoint generated and shown", r.presentedInternal == 119, NULL);
            check("every SOURCE frame shown exactly once", r.presentedSource == 120, NULL);
            check("no deadline miss", r.deadlineMisses == 0, NULL);
            check("no idle slot", r.idleTicks == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("no gap in the display chain", r.orderGaps == 0, NULL);
            check("source period measured from captures",
                  r.sourcePeriodNs == 34317000, NULL);
            // R135: the pipeline delay is two source periods; the realized
            // dispatch sits at capture + 2*period, inside the widened band.
            check("logical SOURCE delay is the two-period pipeline delay",
                  r.measuredSourceDelayNs == 68634000, NULL);
            check("SOURCE accounting closes",
                  r.presentedSource + r.droppedSource == r.sourceEnqueued, NULL);
            check("INTERNAL accounting closes",
                  r.presentedInternal + r.droppedInternal == r.internalEnqueued, NULL);
        }

        // --- Case C: generation slower than the midpoint slot -----------
        // Under the R135 two-period pipeline the midpoint budget is
        // 2*period - halfPeriod = 50.0 ms at 30 fps, so the miss case needs a
        // generation cost that cannot fit 50 ms: 70 ms. The rule under test is
        // not "no misses" but "misses are safe": SOURCE is still shown exactly
        // once each, in order, with no duplication and no blocking.
        printf("\nCase C  generation 70 ms, midpoints cannot make their slot\n");
        {
            R123Scenario s = { .sourceIntervalNs = 33333333, .generationNs = 70000000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 60,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("C", r);
            check("midpoints missed rather than blocking", r.deadlineMisses > 0, NULL);
            check("every SOURCE frame still shown exactly once", r.presentedSource == 60, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("each dropped midpoint is exactly one gap", r.orderGaps == r.deadlineMisses, NULL);
            check("buffer stayed inside its bound", r.maxDepthSeen <= 8, NULL);
        }

        // --- Case D: the provider never produces an M -------------------
        // The warm-up shape: the first completed transition returns nil. No
        // midpoint may be fabricated and the SOURCE stream must be unaffected.
        printf("\nCase D  no midpoint ever resolves (warm-up / failure)\n");
        {
            R123Scenario s = { .sourceIntervalNs = 33333333, .generationNs = 0,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 40,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("D", r);
            check("no midpoint presented", r.presentedInternal == 0, NULL);
            check("every SOURCE frame shown exactly once", r.presentedSource == 40, NULL);
            check("every midpoint accounted as missed",
                  r.deadlineMisses == r.internalEnqueued, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);
        }

        // --- Case E: overflow pressure ----------------------------------
        // A producer far faster than the display. The bound must hold and
        // midpoints must be sacrificed first; SOURCE frames beyond the display
        // rate are refused before ownership transfers to the pump. This test
        // alone does not model the ordered title-direct fallback.
        printf("\nCase E  overflow: producer outruns the display\n");
        {
            R123Scenario s = { .sourceIntervalNs = 5000000, .generationNs = 1000000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 80,
                               .maxDepth = 4, .verbose = NO };
            R123Result r = runScenario(s);
            report("E", r);
            check("buffer bound respected", r.maxDepthSeen <= 4, NULL);
            check("overload refuses before ownership transfer", r.sourceRefused > 0, NULL);
            check("no accepted SOURCE dropped", r.droppedSource == 0, NULL);
            check("all offers accounted as accepted or refused",
                  r.sourceEnqueued + r.sourceRefused == 80, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("SOURCE accounting closes",
                  r.presentedSource + r.droppedSource == r.sourceEnqueued, NULL);
            check("INTERNAL accounting closes",
                  r.presentedInternal + r.droppedInternal == r.internalEnqueued, NULL);
        }

        // --- Case F: the order checker can fail --------------------------
        // Negative control. Without this, "no reorder" in cases A-E would be
        // unfalsifiable: a checker that cannot fail proves nothing.
        printf("\nCase F  negative control, the order check must be able to fail\n");
        {
            R123OrderTracker tracker = {0};
            const uint64_t good[] = { 10, 11, 12, 13, 14 };  // S5 M(5,6) S6 M(6,7) S7
            BOOL accepted = YES;
            for (size_t i = 0; i < sizeof(good) / sizeof(good[0]); ++i) {
                if (!R123OrderTrackerObserve(&tracker, good[i])) accepted = NO;
            }
            check("a correct chain is accepted", accepted, NULL);
            check("a correct chain reports no violation", tracker.violations == 0, NULL);
            check("a correct chain reports no gap", tracker.gaps == 0, NULL);

            R123OrderTracker repeat = {0};
            R123OrderTrackerObserve(&repeat, 10);
            R123OrderTrackerObserve(&repeat, 11);
            R123OrderTrackerObserve(&repeat, 11);   // duplicate frame
            check("a repeated frame is rejected", repeat.violations == 1, NULL);

            R123OrderTracker backwards = {0};
            R123OrderTrackerObserve(&backwards, 10);
            R123OrderTrackerObserve(&backwards, 12);
            R123OrderTrackerObserve(&backwards, 11); // M after its B
            check("a backwards frame is rejected", backwards.violations == 1, NULL);

            R123OrderTracker gapped = {0};
            R123OrderTrackerObserve(&gapped, 10);
            R123OrderTrackerObserve(&gapped, 12);   // midpoint dropped
            check("a dropped midpoint is a gap, not a violation",
                  gapped.gaps == 1 && gapped.violations == 0, NULL);
        }

        // --- Case G: runtime epoch transition and SOURCE-only drain ------
        // OFF invalidates generation immediately but preserves every queued
        // SOURCE. The pump drains those sources at one half-period intervals;
        // stale INTERNAL resolution is rejected and cannot mutate the new
        // epoch. Re-enabling starts a fresh pair history.
        printf("\nCase G  runtime epoch, stale resolution and source-only drain\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:8 halfPeriodNs:kHalfPeriodNs];
            R123RuntimeEpochResult enabled = [pump setRuntimeEnabled:YES epoch:1];
            [pump enqueueSource:10 appFrameId:10 payload:@(10) atNs:100 * kMs];
            [pump enqueueSource:11 appFrameId:11 payload:@(11) atNs:134 * kMs];
            [pump enqueueSource:12 appFrameId:12 payload:@(12) atNs:168 * kMs];
            BOOL oldPairResolved = [pump resolveInternalForA:10 b:11
                internalPresentId:110 payload:@(11) atNs:140 * kMs runtimeEpoch:1];

            R123RuntimeEpochResult flushing = [pump beginSourceOnlyEpoch:2];
            BOOL stalePairResolved = [pump resolveInternalForA:11 b:12
                internalPresentId:120 payload:@(12) atNs:175 * kMs runtimeEpoch:1];
            [pump enqueueSource:13 appFrameId:13 payload:@(13) atNs:202 * kMs];
            [pump primeAtNs:200 * kMs];
            const uint64_t internalAfterDrainSetup = pump.internalEnqueued;

            uint64_t sourceOrder[4] = {0};
            NSUInteger sourceCount = 0;
            uint64_t now = 200 * kMs;
            for (NSUInteger i = 0; i < 4; ++i) {
                R123DisplayItem *item = nil;
                R123DisplayItem *missed = nil;
                R123TickOutcome outcome = [pump takeForSlotAtNs:now
                    outItem:&item outMissed:&missed];
                if (item && item.kind == R123ItemKindSource)
                    sourceOrder[sourceCount++] = item.seqB;
                else if (item || outcome != R123TickPresented)
                    break;
                if (item && !dispatchItem(pump, item)) ++failures;
                now += kHalfPeriodNs;
            }

            R123RuntimeEpochResult disabled = [pump setRuntimeEnabled:NO epoch:3];
            BOOL rejectedWhileOff = ![pump enqueueSource:14 appFrameId:14
                payload:@(14) atNs:236 * kMs];
            R123RuntimeEpochResult reenabled = [pump setRuntimeEnabled:YES epoch:4];
            const uint64_t internalBeforeNewPair = pump.internalEnqueued;
            [pump enqueueSource:15 appFrameId:15 payload:@(15) atNs:270 * kMs];
            [pump enqueueSource:16 appFrameId:16 payload:@(16) atNs:304 * kMs];
            BOOL stalePairResolvedAfterReenable = [pump resolveInternalForA:11 b:12
                internalPresentId:121 payload:@(12) atNs:310 * kMs runtimeEpoch:1];
            R123RuntimeEpochResult finalDrain = [pump setRuntimeEnabled:NO epoch:5];
            R123RuntimeEpochResult preservedDrain = [pump beginSourceOnlyEpoch:5];
            R123DisplayItem *finalItem = nil;
            R123DisplayItem *finalMissed = nil;
            (void)[pump takeForSlotAtNs:350 * kMs outItem:&finalItem outMissed:&finalMissed];
            BOOL finalFirst = finalItem && finalItem.seqB == 15;
            if (finalItem && !dispatchItem(pump, finalItem)) ++failures;
            (void)[pump takeForSlotAtNs:370 * kMs outItem:&finalItem outMissed:&finalMissed];
            BOOL finalSecond = finalItem && finalItem.seqB == 16;
            if (finalItem && !dispatchItem(pump, finalItem)) ++failures;
            (void)[pump endSourceOnlyEpochForTitleDirect:6];

            check("first runtime epoch is accepted", enabled.depthAfter == 0, NULL);
            check("active epoch resolves its pair", oldPairResolved, NULL);
            check("OFF source-only epoch discards pending INTERNAL only",
                flushing.discardedInternal == 2 && flushing.discardedSource == 0 &&
                flushing.depthAfter == 3, NULL);
            check("stale generation completion is rejected", !stalePairResolved, NULL);
            check("source-only epoch enqueues no midpoint",
                internalAfterDrainSetup == 2, NULL);
            check("source-only drain keeps SOURCE sequence intact",
                sourceCount == 4 && sourceOrder[0] == 10 && sourceOrder[1] == 11 &&
                sourceOrder[2] == 12 && sourceOrder[3] == 13, NULL);
            check("OFF clears no already displayed source and blocks new enqueue",
                disabled.depthBefore == 0 && disabled.depthAfter == 0 &&
                rejectedWhileOff, NULL);
            check("re-enable starts a new source pairing history",
                reenabled.depthAfter == 0 && pump.internalEnqueued == internalBeforeNewPair + 1,
                NULL);
            check("old completion cannot resolve into a later epoch",
                !stalePairResolvedAfterReenable, NULL);
            check("final OFF preserves pending SOURCE frames",
                finalDrain.discardedSource == 0 && finalDrain.depthAfter == 3 &&
                preservedDrain.discardedInternal == 1 && finalFirst && finalSecond, NULL);
            check("epoch source accounting closes",
                pump.presentedSource + pump.droppedSource == pump.sourceEnqueued, NULL);
            check("epoch INTERNAL accounting closes",
                pump.presentedInternal + pump.droppedInternal == pump.internalEnqueued, NULL);
        }

        // --- Case H: source-only drain preserves claimed SOURCE ----------
        printf("\nCase H  source-only drain retains queued and claimed SOURCE\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:8 halfPeriodNs:kHalfPeriodNs];
            (void)[pump setRuntimeEnabled:YES epoch:1];
            [pump enqueueSource:20 appFrameId:20 payload:@(20) atNs:100 * kMs];
            [pump enqueueSource:21 appFrameId:21 payload:@(21) atNs:134 * kMs];
            [pump enqueueSource:22 appFrameId:22 payload:@(22) atNs:168 * kMs];
            (void)[pump beginSourceOnlyEpoch:2];

            R123DisplayItem *item = nil;
            R123DisplayItem *missed = nil;
            (void)[pump takeForSlotAtNs:200 * kMs outItem:&item outMissed:&missed];
            const BOOL firstSourceClaimed = item && item.seqB == 20;
            if (item && !dispatchItem(pump, item)) ++failures;
            item = nil;
            (void)[pump takeForSlotAtNs:200 * kMs + kHalfPeriodNs
                outItem:&item outMissed:&missed];
            const BOOL currentSourceClaimed = item && item.seqB == 21;
            const BOOL currentSourceRequeued =
                [pump requeueUndispatchedSource:item forEpoch:2];
            R123RuntimeEpochResult finished =
                [pump endSourceOnlyEpochForTitleDirect:3];
            const BOOL retained = finished.depthAfter == 2 && pump.runtimeEpoch == 2;
            (void)[pump takeForSlotAtNs:220 * kMs outItem:&item outMissed:&missed];
            const BOOL retriedFirst = item && item.seqB == 21;
            if (item && !dispatchItem(pump, item)) ++failures;
            (void)[pump takeForSlotAtNs:240 * kMs outItem:&item outMissed:&missed];
            const BOOL drainedLast = item && item.seqB == 22;
            if (item && !dispatchItem(pump, item)) ++failures;
            (void)[pump endSourceOnlyEpochForTitleDirect:3];
            const BOOL lateProducerRejected = ![pump enqueueSource:23 appFrameId:23
                payload:@(23) atNs:250 * kMs];

            check("prior SOURCE was physically consumed before handoff", firstSourceClaimed,
                  NULL);
            check("producer-before-hook current SOURCE can be claimed", currentSourceClaimed,
                  NULL);
            check("claimed SOURCE requeues before later SOURCE", currentSourceRequeued &&
                  retriedFirst && drainedLast, NULL);
            check("disable refuses while SOURCE remains queued", retained &&
                  finished.discardedSource == 0 && pump.droppedSource == 0, NULL);
            check("hook-before-producer enqueue cannot revive the pump", lateProducerRejected,
                  NULL);
            check("handoff source accounting closes",
                  pump.sourceEnqueued == pump.presentedSource + pump.droppedSource +
                      pump.sourceReturnedToTitle, NULL);
        }

        // --- Case H2: a claimed SOURCE survives a full queue at OFF -----
        // The claimed item is still part of source capacity while it is outside
        // the linked queue. A producer cannot consume the claimed item's slot.
        printf("\nCase H2  claimed SOURCE remains inside the reservation bound\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:3 halfPeriodNs:kHalfPeriodNs];
            (void)[pump setRuntimeEnabled:YES epoch:1];
            const BOOL first = [pump enqueueSource:30 appFrameId:30 payload:@(30)
                                               atNs:100 * kMs];
            const BOOL second = [pump enqueueSource:31 appFrameId:31 payload:@(31)
                                                atNs:134 * kMs];
            [pump primeAtNs:200 * kMs];
            R123DisplayItem *claimed = nil;
            R123DisplayItem *missed = nil;
            (void)[pump takeForSlotAtNs:200 * kMs outItem:&claimed outMissed:&missed];
            const BOOL claimedSource = claimed && claimed.seqB == 30;
            const BOOL third = [pump enqueueSource:32 appFrameId:32 payload:@(32)
                                               atNs:168 * kMs];
            (void)[pump beginSourceOnlyEpoch:2];
            const BOOL fourth = [pump enqueueSource:33 appFrameId:33 payload:@(33)
                                                atNs:202 * kMs];
            const BOOL fullQueue = pump.activeSourceReservations == 3 && pump.depth == 2;
            const BOOL requeued = [pump requeueUndispatchedSource:claimed forEpoch:2];
            check("fixture fills queue after claimed SOURCE", first && second && claimedSource &&
                  third && !fourth && fullQueue, NULL);
            check("OFF requeue restores claimed SOURCE inside the configured bound",
                  requeued && pump.depth == 3 && pump.maxDepthSeen <= 3, NULL);
            check("full queue transition does not drop SOURCE",
                  pump.droppedSource == 0 && pump.sourceEnqueued == 3 &&
                  pump.activeSourceReservations == 3, NULL);
            check("claimed SOURCE is re-epoched for post-OFF retry",
                  claimed.runtimeEpoch == 2, NULL);
        }

        // --- Case I: continuous 29.14-fps producer catches 60-Hz drain -----
        // Start with a small buffered backlog, then continue adding SOURCE at
        // the measured R126 cadence while the source-only consumer gets a slot
        // every half refresh. The disable cutoff must be physically consumable
        // within a bounded number of producer boundaries.
        printf("\nCase I  source-only drain catches a continuous measured-rate producer\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:16 halfPeriodNs:kHalfPeriodNs];
            (void)[pump setRuntimeEnabled:YES epoch:1];
            [pump enqueueSource:100 appFrameId:100 payload:@(100) atNs:0];
            [pump enqueueSource:101 appFrameId:101 payload:@(101) atNs:34317000];
            [pump enqueueSource:102 appFrameId:102 payload:@(102) atNs:68634000];
            [pump enqueueSource:103 appFrameId:103 payload:@(103) atNs:102951000];
            (void)[pump beginSourceOnlyEpoch:2];
            [pump enqueueSource:104 appFrameId:104 payload:@(104) atNs:137268000];
            [pump primeAtNs:0];

            const uint64_t sourcePeriodNs = 34317000ULL;
            uint64_t latestSuppressed = 104;
            uint64_t currentSource = 104;
            uint64_t lastDisplayed = 0;
            uint64_t nextPumpNs = 0;
            uint64_t nextProducerNs = sourcePeriodNs;
            uint64_t producerBoundaries = 0;
            BOOL handoffPossible = NO;
            for (uint64_t events = 0; events < 400; ++events) {
                if (nextPumpNs <= nextProducerNs) {
                    R123DisplayItem *item = nil;
                    R123DisplayItem *missed = nil;
                    (void)[pump takeForSlotAtNs:nextPumpNs
                        outItem:&item outMissed:&missed];
                    if (item && item.kind == R123ItemKindSource) {
                        lastDisplayed = item.seqB;
                        if (!dispatchItem(pump, item)) ++failures;
                    }
                    nextPumpNs += kHalfPeriodNs;
                } else {
                    ++currentSource;
                    ++producerBoundaries;
                    if (lastDisplayed >= latestSuppressed &&
                        lastDisplayed < currentSource) {
                        handoffPossible = YES;
                        break;
                    }
                    [pump enqueueSource:currentSource appFrameId:currentSource
                        payload:@(currentSource) atNs:nextProducerNs];
                    latestSuppressed = currentSource;
                    nextProducerNs += sourcePeriodNs;
                }
            }
            check("physical source watermark catches the moving drain cutoff",
                  handoffPossible && lastDisplayed >= latestSuppressed - 1, NULL);
            check("continuous-producer drain completes within twelve source boundaries",
                  handoffPossible && producerBoundaries < 12, NULL);
            check("source-only run preserves monotonic pump order",
                  pump.orderViolations == 0 && pump.droppedSource == 0, NULL);
        }

        // --- Case J: transactional reservation and dispatch terminality ---
        printf("\nCase J  reserved, committed, retried and dispatched SOURCE\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:2 halfPeriodNs:kHalfPeriodNs];
            (void)[pump setRuntimeEnabled:YES epoch:1];
            R123DisplayItem *first = [pump reserveSource:1 appFrameId:1
                payload:@(1) atNs:100 * kMs runtimeEpoch:1];
            [pump primeAtNs:100 * kMs];
            R123DisplayItem *claimed = nil;
            R123DisplayItem *missed = nil;
            (void)[pump takeForSlotAtNs:100 * kMs outItem:&claimed outMissed:&missed];
            const BOOL provisionalNotClaimable = claimed == nil;
            const BOOL firstCommitted = [pump commitSourceReservation:first];
            (void)[pump takeForSlotAtNs:101 * kMs outItem:&claimed outMissed:&missed];
            const BOOL firstClaimed = claimed == first;
            R123DisplayItem *second = [pump reserveSource:2 appFrameId:2
                payload:@(2) atNs:134 * kMs runtimeEpoch:1];
            const BOOL secondCommitted = second && [pump commitSourceReservation:second];
            const BOOL queuedAndClaimedCounted = pump.activeSourceReservations == 2 &&
                pump.activeSourceReservations <= pump.sourceCapacity;
            const BOOL thirdRefused = [pump reserveSource:3 appFrameId:3
                payload:@(3) atNs:168 * kMs runtimeEpoch:1] == nil;
            [pump rejectUndispatchedItem:claimed];
            const BOOL retryRetainsReservation = pump.activeSourceReservations == 2 &&
                pump.depth == 2 && pump.droppedSource == 0;
            claimed = nil;
            (void)[pump takeForSlotAtNs:120 * kMs outItem:&claimed outMissed:&missed];
            const BOOL retriedSameSource = claimed == first;
            const BOOL firstDispatched = [pump markItemDispatched:claimed wsiIdentity:501];
            const BOOL duplicateRejected = ![pump markItemDispatched:claimed
                wsiIdentity:501];
            claimed = nil;
            (void)[pump takeForSlotAtNs:140 * kMs outItem:&claimed outMissed:&missed];
            const BOOL secondDispatched = claimed == second &&
                [pump markItemDispatched:claimed wsiIdentity:502];
            check("provisional reservation cannot be claimed", provisionalNotClaimable,
                  NULL);
            check("ready SOURCE reserves and commits before dispatch", firstCommitted &&
                  firstClaimed && secondCommitted, NULL);
            check("queued plus claimed SOURCE stays inside capacity",
                  queuedAndClaimedCounted && thirdRefused, NULL);
            check("pre-WSI rejection retries same SOURCE without dropping it",
                  retryRetainsReservation && retriedSameSource, NULL);
            check("WSI frontier terminalizes each SOURCE once",
                  firstDispatched && secondDispatched && duplicateRejected &&
                  pump.sourceDispatched == 2 && pump.activeSourceReservations == 0,
                  NULL);

            R123DisplayPump *cancelPump = [[R123DisplayPump alloc]
                initWithMaxDepth:2 halfPeriodNs:kHalfPeriodNs];
            (void)[cancelPump setRuntimeEnabled:YES epoch:1];
            R123DisplayItem *provisional = [cancelPump reserveSource:7 appFrameId:7
                payload:@(7) atNs:0 runtimeEpoch:1];
            const BOOL canceled = provisional &&
                [cancelPump cancelSourceReservation:provisional];
            check("pre-commit cancel releases provisional capacity", canceled &&
                cancelPump.activeSourceReservations == 0 && cancelPump.depth == 0,
                NULL);

            R123DisplayPump *orderPump = [[R123DisplayPump alloc]
                initWithMaxDepth:2 halfPeriodNs:kHalfPeriodNs];
            (void)[orderPump setRuntimeEnabled:YES epoch:1];
            (void)[orderPump enqueueSource:10 appFrameId:10 payload:@(10)
                atNs:100 * kMs];
            (void)[orderPump enqueueSource:11 appFrameId:11 payload:@(11)
                atNs:134 * kMs];
            [orderPump primeAtNs:100 * kMs];
            R123DisplayItem *ordered = nil;
            R123DisplayItem *orderedMissed = nil;
            (void)[orderPump takeForSlotAtNs:100 * kMs
                outItem:&ordered outMissed:&orderedMissed];
            const BOOL orderedFirstDispatched = dispatchItem(orderPump, ordered);
            (void)[orderPump beginSourceOnlyEpoch:2];
            (void)[orderPump takeForSlotAtNs:120 * kMs
                outItem:&ordered outMissed:&orderedMissed];
            if (ordered) {
                ordered.seqA = 10; // stale acquisition identity
                ordered.sourcePresentOrdinal = 1; // duplicate logical position
            }
            const BOOL rejectedOutOfOrderDispatch = ordered && ordered.seqB == 11 &&
                ![orderPump markItemDispatched:ordered wsiIdentity:503];
            (void)[orderPump requeueUndispatchedSource:ordered forEpoch:2];
            ordered = nil;
            (void)[orderPump takeForSlotAtNs:140 * kMs
                outItem:&ordered outMissed:&orderedMissed];
            check("invalid SOURCE order remains pending at the pump head",
                orderedFirstDispatched && rejectedOutOfOrderDispatch && ordered == nil &&
                orderPump.depth == 1 && orderPump.sourceDispatched == 1 &&
                orderPump.activeSourceReservations == 1 &&
                orderPump.orderViolations > 0, NULL);
        }

        // --- Case K: R135 steady alternation and fixed latency ----------
        // The delayed-pipeline contract at the measured title cadence: every
        // SOURCE presents at capture + 2*period, every midpoint exactly one
        // half-period after the preceding SOURCE, with zero misses and zero
        // latency accumulation across a long synthetic sequence.
        printf("\nCase K  29.14 fps SOURCE, fixed two-period pipeline latency\n");
        {
            const uint64_t period = 34317000ULL;
            const int count = 240;
            R123Scenario s = { .sourceIntervalNs = period, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = count,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("K", r);
            check("every midpoint generated and shown", r.presentedInternal == count - 1, NULL);
            check("every SOURCE frame shown exactly once", r.presentedSource == count, NULL);
            check("no deadline miss", r.deadlineMisses == 0, NULL);
            check("no idle slot", r.idleTicks == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);
            check("buffer stayed inside its bound", r.maxDepthSeen <= 8, NULL);

            const uint64_t firstSourceNs = 100 * kMs;
            uint64_t (^captureOf)(uint64_t) = ^uint64_t(uint64_t seq) {
                return firstSourceNs + (seq - 1) * period;
            };
            check("first dispatch is SOURCE 1 at the prime instant",
                  r123DispatchCount > 1 && r123DispatchTrace[0].kind == 0 &&
                  r123DispatchTrace[0].seqB == 1 &&
                  r123DispatchTrace[0].t == captureOf(2), NULL);
            // M(1,2) carries the same capture-anchored slot as every other
            // midpoint: capture(1) + 2*period + halfPeriod. At SOURCE 1's
            // present the period is not yet measured, so its slot falls back
            // to one half-period on and M(1,2) presents early -- the one
            // priming exception.
            check("first midpoint lands on its anchor or the priming fallback",
                  r123DispatchCount > 1 && r123DispatchTrace[1].kind == 1 &&
                  r123DispatchTrace[1].seqB == 2 &&
                  (r123DispatchTrace[1].t == captureOf(1) + 2 * period + kHalfPeriodNs ||
                   r123DispatchTrace[1].t == captureOf(2) + kHalfPeriodNs), NULL);
            BOOL alternating = r123DispatchCount >= 8;
            for (int i = 0; i < r123DispatchCount; ++i) {
                if (r123DispatchTrace[i].kind != (i % 2 == 0 ? 0 : 1)) { alternating = NO; break; }
            }
            check("output alternates SOURCE, INTERNAL from the first slot",
                  alternating, NULL);
            BOOL fixedLatency = YES;
            for (int i = 0; i < r123DispatchCount; ++i) {
                const R123Dispatch *d = &r123DispatchTrace[i];
                if (d->kind != 0) continue;
                const uint64_t expected = d->seqB == 1
                    ? captureOf(2)                       // primed before pair 2 exists
                    : captureOf(d->seqB) + 2 * period;   // the pipeline delay
                if (d->t != expected) { fixedLatency = NO; break; }
            }
            check("every SOURCE presents at capture + 2*period (no accumulation)",
                  fixedLatency, NULL);
            BOOL midpointAnchored = YES;
            for (int i = 0; i < r123DispatchCount; ++i) {
                const R123Dispatch *d = &r123DispatchTrace[i];
                if (d->kind != 1 || d->seqB == 2) continue;  // M(1,2) covered above
                if (d->t != captureOf(d->seqB - 1) + 2 * period + kHalfPeriodNs) {
                    midpointAnchored = NO; break;
                }
            }
            check("every midpoint presents at capture + 2*period + halfPeriod",
                  midpointAnchored, NULL);
        }

        // --- Case L: one missing INTERNAL, then re-entry -----------------
        // Pair (9,10) resolves too late and is dropped. The contract: SOURCE
        // 10 presents early (same tick), SOURCE 11 re-anchors at its own
        // capture + 2*period, the recovery midpoint fits its pulled-in budget,
        // and no duplicate or reorder appears anywhere.
        printf("\nCase L  one missing INTERNAL, immediate re-entry\n");
        {
            const uint64_t period = 34317000ULL;
            const int count = 120;
            R123Scenario s = { .sourceIntervalNs = period, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = count,
                               .maxDepth = 8, .verbose = NO,
                               .slowPairSeq = 10, .slowPairGenerationNs = 70000000 };
            R123Result r = runScenario(s);
            report("L", r);
            check("exactly one midpoint missed", r.deadlineMisses == 1, NULL);
            check("all other midpoints shown", r.presentedInternal == count - 2, NULL);
            check("every SOURCE frame still shown exactly once",
                  r.presentedSource == count, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("INTERNAL accounting closes",
                  r.presentedInternal + r.droppedInternal == r.internalEnqueued, NULL);

            const uint64_t firstSourceNs = 100 * kMs;
            uint64_t (^captureOf)(uint64_t) = ^uint64_t(uint64_t seq) {
                return firstSourceNs + (seq - 1) * period;
            };
            uint64_t sourceTime[16] = {0};
            uint64_t recoveryMidTime = 0, steadyMidTime = 0;
            for (int i = 0; i < r123DispatchCount; ++i) {
                const R123Dispatch *d = &r123DispatchTrace[i];
                if (d->kind == 0 && d->seqB >= 9 && d->seqB <= 12)
                    sourceTime[d->seqB] = d->t;
                // M(9,10) is the dropped midpoint; the first surviving one is
                // M(10,11), which inherits SOURCE 10's pulled-in anchor.
                if (d->kind == 1 && d->seqB == 11) recoveryMidTime = d->t;
                if (d->kind == 1 && d->seqB == 12) steadyMidTime = d->t;
            }
            check("recovery SOURCE 10 presents early at the missed slot",
                  sourceTime[10] == captureOf(10) + period + kHalfPeriodNs, NULL);
            // Capture-anchored rule 1: the recovery midpoint targets the same
            // fixed slot as any steady pair, capture(10) + 2*period + H, which
            // the pulled-in SOURCE 10 reaches one half-period after its early
            // present.
            check("recovery midpoint returns to the fixed capture anchor",
                  recoveryMidTime == captureOf(10) + 2 * period + kHalfPeriodNs, NULL);
            check("SOURCE 11 re-anchors at capture + 2*period",
                  sourceTime[11] == captureOf(11) + 2 * period, NULL);
            check("midpoint (11,12) is back on the steady anchor",
                  steadyMidTime == captureOf(11) + 2 * period + kHalfPeriodNs, NULL);
        }

        // --- Case M: dynamic source period change ------------------------
        // The title slows from 33.333 ms to 40.000 ms at sequence 61. The
        // pipeline delay tracks the measured period and applies at the next
        // SOURCE boundary; no drop, no reorder, no drift either side.
        printf("\nCase M  source period 33.333 -> 40.000 ms mid-run\n");
        {
            const uint64_t period1 = 33333333ULL;
            const uint64_t period2 = 40000000ULL;
            const int count = 120;
            const int switchAt = 61;
            R123Scenario s = { .sourceIntervalNs = period1, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = count,
                               .maxDepth = 8, .verbose = NO,
                               .sourceInterval2Ns = period2, .switchAtSeq = switchAt };
            R123Result r = runScenario(s);
            report("M", r);
            check("no deadline miss across the period step", r.deadlineMisses == 0, NULL);
            check("every enqueued midpoint shown",
                  r.presentedInternal == r.internalEnqueued, NULL);
            // The period step compresses one capture gap transiently, so the
            // reservation bound may refuse a few SOURCE frames — the pump must
            // refuse them before ownership transfer instead of dropping them.
            check("every offered SOURCE is accepted or refused, never dropped",
                  r.presentedSource + r.sourceRefused == count && r.droppedSource == 0,
                  NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("queue bound respected", r.maxDepthSeen <= 8, NULL);

            const uint64_t firstSourceNs = 100 * kMs;
            // Phase boundary: the first slot set after the new period was
            // measured already uses it, so the last phase-1 capture presents
            // at the phase-2 delay (changes apply at a clean SOURCE boundary).
            BOOL phase1Fixed = YES, phase2Fixed = YES;
            for (int i = 0; i < r123DispatchCount; ++i) {
                const R123Dispatch *d = &r123DispatchTrace[i];
                if (d->kind != 0 || d->seqB == 1) continue;
                uint64_t expected;
                if (d->seqB < (uint64_t)(switchAt - 1)) {
                    expected = firstSourceNs + (d->seqB - 1) * period1 + 2 * period1;
                } else {
                    const uint64_t capture =
                        firstSourceNs + (uint64_t)(switchAt - 2) * period1 +
                        (d->seqB - (uint64_t)(switchAt - 1)) * period2;
                    expected = capture + 2 * period2;
                }
                if (d->seqB < (uint64_t)(switchAt - 1) && d->t != expected) phase1Fixed = NO;
                if (d->seqB >= (uint64_t)(switchAt - 1) && d->t != expected) phase2Fixed = NO;
            }
            check("phase-1 SOURCE delay is 2*33.333 ms", phase1Fixed, NULL);
            check("phase-2 SOURCE delay is 2*40.000 ms from the first post-step slot",
                  phase2Fixed, NULL);
            check("phase-2 midpoint budget covers generation",
                  r.deadlineMisses == 0 && 2 * period2 - kHalfPeriodNs > 10686000, NULL);
        }

        // --- Case N: OFF/ON restart re-establishes the phase -------------
        // Disable mid-stream, re-enable with a fresh epoch and pair history;
        // the two-period anchor must re-establish from the new prime without
        // carries from the previous epoch.
        printf("\nCase N  OFF/ON restart re-anchors the delayed pipeline\n");
        {
            const uint64_t period = 34317000ULL;
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:8 halfPeriodNs:kHalfPeriodNs];
            r123DispatchCount = 0;
            (void)[pump setRuntimeEnabled:YES epoch:1];
            const uint64_t t0 = 100 * kMs;
            [pump enqueueSource:10 appFrameId:10 payload:@(10) atNs:t0];
            [pump enqueueSource:11 appFrameId:11 payload:@(11) atNs:t0 + period];
            const uint64_t primeNs = t0 + period;
            [pump primeAtNs:primeNs];
            uint64_t now = primeNs;
            R123DisplayItem *item = nil;
            R123DisplayItem *missed = nil;
            // SOURCE 10 at the prime instant; the midpoint targets its
            // capture-anchored slot, capture(10) + 2*period + halfPeriod.
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL s10First = item && item.kind == R123ItemKindSource &&
                item.seqB == 10;
            if (item && !dispatchItem(pump, item)) ++failures;
            const uint64_t epoch1MidSlot = t0 + 2 * period + kHalfPeriodNs;
            const BOOL resolved11 = [pump resolveInternalForA:10 b:11
                internalPresentId:1100 payload:@(11) atNs:primeNs + 10686000
                                    runtimeEpoch:1];
            now = epoch1MidSlot;
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL midPresented = item && item.kind == R123ItemKindInternal &&
                item.seqA == 10 && item.seqB == 11;
            if (item && !dispatchItem(pump, item)) ++failures;
            // Rule 2 after the midpoint anchors SOURCE 11 two periods out.
            const uint64_t epoch1Anchor = pump.nextSlotNs;
            const uint64_t expectedS11 = (t0 + period) + 2 * period;
            // OFF before SOURCE 11 presents: it must survive for the drain,
            // taken at its rule-2 anchor.
            (void)[pump beginSourceOnlyEpoch:2];
            now = expectedS11;
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL s11Drained = item && item.kind == R123ItemKindSource &&
                item.seqB == 11;
            if (item && !dispatchItem(pump, item)) ++failures;

            // Re-enable with a fresh epoch and pair history.
            (void)[pump setRuntimeEnabled:YES epoch:3];
            [pump enqueueSource:20 appFrameId:20 payload:@(20) atNs:t0 + 10 * period];
            [pump enqueueSource:21 appFrameId:21 payload:@(21) atNs:t0 + 11 * period];
            const uint64_t prime2 = t0 + 11 * period;
            [pump primeAtNs:prime2];
            now = prime2;
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL s20First = item && item.kind == R123ItemKindSource &&
                item.seqB == 20;
            if (item && !dispatchItem(pump, item)) ++failures;
            const BOOL resolved21 = [pump resolveInternalForA:20 b:21
                internalPresentId:2100 payload:@(21) atNs:prime2 + 10686000
                                    runtimeEpoch:3];
            // Capture-anchored midpoint slot for the re-enabled epoch:
            // capture(20) + 2*period + halfPeriod.
            now = (t0 + 10 * period) + 2 * period + kHalfPeriodNs;
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL mid2Presented = item && item.kind == R123ItemKindInternal &&
                item.seqA == 20 && item.seqB == 21;
            if (item && !dispatchItem(pump, item)) ++failures;
            // Rule 2 after the midpoint: capture(21) + 2*period. Sampled
            // before any further tick so the anchor itself is asserted.
            const uint64_t s21Slot = pump.nextSlotNs;
            const uint64_t expectedS21 = prime2 + 2 * period;
            now = s21Slot;
            (void)[pump takeForSlotAtNs:now outItem:&item outMissed:&missed];
            const BOOL s21DispatchedAtAnchor = item &&
                item.kind == R123ItemKindSource && item.seqB == 21;
            if (item && !dispatchItem(pump, item)) ++failures;

            check("epoch 1 opens with SOURCE 10 at the prime instant", s10First, NULL);
            check("first-epoch pair resolves in its own epoch", resolved11, NULL);
            check("first-epoch midpoint lands one half-period on", midPresented, NULL);
            check("epoch-1 SOURCE 11 anchors at capture + 2*period",
                  epoch1Anchor == expectedS11, NULL);
            check("OFF drain keeps pending SOURCE 11", s11Drained, NULL);
            check("re-enabled epoch opens with SOURCE 20", s20First, NULL);
            check("new-epoch pair resolves only in its own epoch", resolved21, NULL);
            check("re-enabled epoch midpoint lands one half-period on",
                  mid2Presented, NULL);
            check("post-restart SOURCE 21 re-anchors at capture + 2*period",
                  s21Slot == expectedS21 && s21DispatchedAtAnchor, NULL);

            check("epoch 1 opens with SOURCE 10 at the prime instant", s10First, NULL);
            check("first-epoch midpoint lands one half-period on", midPresented, NULL);
            check("OFF drain keeps pending SOURCE 11", s11Drained, NULL);
            check("re-enabled epoch opens with SOURCE 20", s20First, NULL);
            check("new-epoch pair resolves only in its own epoch", resolved21, NULL);
            check("re-enabled epoch midpoint lands one half-period on",
                  mid2Presented, NULL);
            check("restart drops no SOURCE and violates no order",
                  pump.droppedSource == 0 && pump.orderViolations == 0, NULL);
            check("restart accounting closes",
                  pump.presentedSource + pump.droppedSource == pump.sourceEnqueued &&
                  pump.presentedInternal + pump.droppedInternal == pump.internalEnqueued,
                  NULL);
        }

        // --- Case O: R136 guard recovers a capture-gap midpoint ---------
        // A title capture hitch delays capture(30) by 100 ms against a
        // 34.3 ms grid. The clamped-period slot for M(29,30) then fires
        // before capture(30) itself; without the guard the midpoint would
        // be dropped while still generateable. The guard defers the drop
        // once to capture(30) + period, the midpoint (28 ms generation)
        // resolves in time, presents, and SOURCE 30 lands back on its
        // normal two-period anchor.
        printf("\nCase O  capture hitch: guard defers the drop, midpoint presents\n");
        {
            const uint64_t period = 34317000ULL;
            const uint64_t generation = 28000000ULL;
            const int count = 60;
            R123Scenario s = { .sourceIntervalNs = period, .generationNs = generation,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = count,
                               .maxDepth = 8, .verbose = NO,
                               .gapAtSeq = 30, .gapNs = 100 * kMs };
            R123Result r = runScenario(s);
            report("O", r);
            check("no deadline miss (guard recovered the gap pair)",
                  r.deadlineMisses == 0, NULL);
            check("every midpoint generated and shown",
                  r.presentedInternal == count - 1, NULL);
            check("every SOURCE frame shown exactly once",
                  r.presentedSource == count, NULL);
            check("no gap in the display chain", r.orderGaps == 0, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("guard deferred exactly once", r.dropGuardDeferrals == 1, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);

            const uint64_t firstSourceNs = 100 * kMs;
            const uint64_t capture30 = firstSourceNs + 29 * period + 100 * kMs;
            __block uint64_t m30At = 0, s30At = 0;
            for (int i = 0; i < r123DispatchCount; ++i) {
                if (m30At == 0 && r123DispatchTrace[i].kind == 1 &&
                    r123DispatchTrace[i].seqB == 30) m30At = r123DispatchTrace[i].t;
                if (s30At == 0 && r123DispatchTrace[i].kind == 0 &&
                    r123DispatchTrace[i].seqB == 30) s30At = r123DispatchTrace[i].t;
            }
            // The deferral target uses the period measured at the guarded
            // tick; the 134 ms hitch clamps to the 4H ceiling (66.67 ms).
            check("guarded midpoint presented at capture(30) + clamped period",
                  m30At == capture30 + 4 * kHalfPeriodNs, NULL);
            check("SOURCE 30 back on the two-period anchor",
                  s30At == capture30 + 2 * period, NULL);
            check("INTERNAL accounting closes",
                  r.presentedInternal + r.droppedInternal == r.internalEnqueued, NULL);
        }

        // --- Case P: guard defers once, the midpoint still misses -------
        // Same capture hitch, but the gap pair's generation (70 ms via the
        // slow-pair extension; the grid pairs stay at 10.7 ms) cannot finish
        // even within the clamped-period deferred window. The drop then
        // happens at exactly capture(30) + clamped period, SOURCE 30
        // presents there (earlier than its two-period anchor, never later),
        // and the next pair re-anchors.
        printf("\nCase P  capture hitch: guarded drop, SOURCE early-present\n");
        {
            const uint64_t period = 34317000ULL;
            const int count = 60;
            R123Scenario s = { .sourceIntervalNs = period, .generationNs = 10686000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = count,
                               .maxDepth = 8, .verbose = NO,
                               .slowPairSeq = 30, .slowPairGenerationNs = 70 * kMs,
                               .gapAtSeq = 30, .gapNs = 100 * kMs };
            R123Result r = runScenario(s);
            report("P", r);
            check("exactly one deadline miss", r.deadlineMisses == 1, NULL);
            check("exactly one display gap", r.orderGaps == 1, NULL);
            check("guard deferred exactly once", r.dropGuardDeferrals == 1, NULL);
            check("every SOURCE frame shown exactly once",
                  r.presentedSource == count, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
            check("no SOURCE dropped", r.droppedSource == 0, NULL);

            const uint64_t firstSourceNs = 100 * kMs;
            const uint64_t capture30 = firstSourceNs + 29 * period + 100 * kMs;
            __block uint64_t s30At = 0;
            for (int i = 0; i < r123DispatchCount; ++i) {
                if (s30At == 0 && r123DispatchTrace[i].kind == 0 &&
                    r123DispatchTrace[i].seqB == 30) s30At = r123DispatchTrace[i].t;
            }
            check("SOURCE 30 presented at the guarded slot capture(30)+clamped period",
                  s30At == capture30 + 4 * kHalfPeriodNs, NULL);
            // Only the gap pair missed: 59 pairs minus the gap pair.
            check("all other midpoints presented",
                  r.presentedInternal == count - 2, NULL);
        }

        // --- Case Q: guard must not fire on ordinary late misses --------
        // Regression against accidental normal-path change: a 70 ms
        // generation under a steady grid misses at slots that are already
        // past capture(B) + period; the guard must stay silent and the
        // miss shape must equal the pre-R136 behaviour.
        printf("\nCase Q  steady-grid overrun: guard silent, shape unchanged\n");
        {
            R123Scenario s = { .sourceIntervalNs = 33333333, .generationNs = 70000000,
                               .halfPeriodNs = kHalfPeriodNs, .sourceCount = 60,
                               .maxDepth = 8, .verbose = NO };
            R123Result r = runScenario(s);
            report("Q", r);
            check("midpoints missed rather than blocking", r.deadlineMisses > 0, NULL);
            check("guard never fired", r.dropGuardDeferrals == 0, NULL);
            check("every SOURCE frame still shown exactly once",
                  r.presentedSource == 60, NULL);
            check("each dropped midpoint is exactly one gap",
                  r.orderGaps == r.deadlineMisses, NULL);
            check("no reorder", r.orderViolations == 0, NULL);
        }

        // --- Case R137: one-lock scalar diagnostic snapshot ------------
        printf("\nCase R137  scalar diagnostic snapshot follows ownership\n");
        {
            R123DisplayPump *pump = [[R123DisplayPump alloc]
                initWithMaxDepth:4 halfPeriodNs:kHalfPeriodNs];
            (void)[pump setRuntimeEnabled:YES epoch:1];
            R123DisplayItem *first = [pump reserveSource:1 appFrameId:1
                payload:@(1) atNs:100 * kMs runtimeEpoch:1];
            R123DiagnosticSnapshot provisional =
                [pump diagnosticSnapshotAtNs:99 * kMs];
            const BOOL provisionalSnapshot = first && provisional.depth == 1 &&
                provisional.head.present && provisional.head.seqB == 1 &&
                provisional.head.ready && provisional.activeSourceReservations == 1 &&
                provisional.head.runtimeEpoch == 1 &&
                !provisional.head.sourceReservationCommitted &&
                provisional.committedQueuedSourceCount == 0 &&
                !provisional.hasUndispatchedSource &&
                provisional.oldestUndispatchedSourceAgeNs == 0;

            const BOOL firstCommitted = [pump commitSourceReservation:first];
            R123DisplayItem *second = [pump reserveSource:2 appFrameId:2
                payload:@(2) atNs:134 * kMs runtimeEpoch:1];
            const BOOL secondCommitted = second && [pump commitSourceReservation:second];
            R123DiagnosticSnapshot queued = [pump diagnosticSnapshotAtNs:150 * kMs];
            const BOOL queueShape = queued.depth == 3 && queued.head.present &&
                queued.head.kind == R123ItemKindSource && queued.head.seqB == 1 &&
                queued.next.present && queued.next.kind == R123ItemKindInternal &&
                !queued.next.ready && queued.committedQueuedSourceCount == 2 &&
                queued.head.runtimeEpoch == 1 &&
                queued.head.sourceReservationCommitted &&
                queued.internalPlaceholderCount == 1 && queued.internalReadyCount == 0 &&
                queued.oldestUndispatchedSourceOrdinal == 1 &&
                queued.oldestUndispatchedSourceSequence == 1 &&
                queued.oldestUndispatchedSourceAgeNs == 50 * kMs &&
                queued.newestUndispatchedSourceOrdinal == 2 &&
                queued.newestUndispatchedSourceSequence == 2 &&
                queued.newestUndispatchedSourceAgeNs == 16 * kMs &&
                queued.sourcePeriodNs == 34 * kMs && queued.sourceEnqueued == 2 &&
                queued.internalEnqueued == 1;
            const BOOL resolved = [pump resolveInternalForA:1 b:2
                internalPresentId:12 payload:@(12) atNs:145 * kMs];
            R123DiagnosticSnapshot ready = [pump diagnosticSnapshotAtNs:150 * kMs];
            const BOOL resolvedSnapshot = resolved && ready.internalPlaceholderCount == 0 &&
                ready.internalReadyCount == 1 && ready.internalResolved == 1 &&
                ready.next.ready;

            [pump primeAtNs:200 * kMs];
            R123DisplayItem *claimed = nil;
            R123DisplayItem *missed = nil;
            (void)[pump takeForSlotAtNs:200 * kMs outItem:&claimed outMissed:&missed];
            R123DiagnosticSnapshot inFlight = [pump diagnosticSnapshotAtNs:210 * kMs];
            const BOOL claimSnapshot = claimed == first && inFlight.depth == 2 &&
                inFlight.claimed.present && inFlight.claimed.seqB == 1 &&
                inFlight.claimed.ready && inFlight.claimedAtNs == 200 * kMs &&
                inFlight.claimed.runtimeEpoch == 1 &&
                inFlight.claimed.sourceReservationCommitted &&
                inFlight.committedQueuedSourceCount == 1 &&
                inFlight.committedClaimedSourceCount == 1 &&
                inFlight.oldestUndispatchedSourceAgeNs == 110 * kMs &&
                inFlight.newestUndispatchedSourceAgeNs == 76 * kMs &&
                inFlight.nextSlotNs == 100 * kMs + 2 * 34 * kMs + kHalfPeriodNs;
            const BOOL requeued = [pump requeueUndispatchedSource:claimed forEpoch:1];
            R123DiagnosticSnapshot retry = [pump diagnosticSnapshotAtNs:220 * kMs];
            const BOOL requeueSnapshot = requeued && !retry.claimed.present &&
                retry.claimedAtNs == 0 && retry.head.present && retry.head.seqB == 1 &&
                retry.depth == 3 && retry.activeSourceReservations == 2;
            claimed = nil;
            (void)[pump takeForSlotAtNs:220 * kMs outItem:&claimed outMissed:&missed];
            const BOOL dispatched = claimed == first &&
                [pump markItemDispatched:claimed wsiIdentity:7001];
            R123DiagnosticSnapshot afterDispatch =
                [pump diagnosticSnapshotAtNs:221 * kMs];
            const BOOL dispatchSnapshot = dispatched && !afterDispatch.claimed.present &&
                afterDispatch.claimedAtNs == 0 && afterDispatch.sourceDispatched == 1 &&
                afterDispatch.presentedSource == 1 &&
                afterDispatch.committedQueuedSourceCount == 1 &&
                afterDispatch.activeSourceReservations == 1;
            claimed = nil;
            (void)[pump takeForSlotAtNs:240 * kMs outItem:&claimed outMissed:&missed];
            const BOOL internalDispatched = claimed &&
                claimed.kind == R123ItemKindInternal &&
                [pump markItemDispatched:claimed wsiIdentity:7002];
            R123DiagnosticSnapshot afterInternalDispatch =
                [pump diagnosticSnapshotAtNs:241 * kMs];
            const BOOL internalTotals = internalDispatched &&
                afterInternalDispatch.internalEnqueued == 1 &&
                afterInternalDispatch.internalResolved == 1 &&
                afterInternalDispatch.internalDispatched == 1 &&
                afterInternalDispatch.presentedInternal == 1 &&
                afterInternalDispatch.internalDropped == 0 &&
                afterInternalDispatch.deadlineMisses == 0;

            R123DisplayPump *dropPump = [[R123DisplayPump alloc]
                initWithMaxDepth:3 halfPeriodNs:kHalfPeriodNs];
            (void)[dropPump setRuntimeEnabled:YES epoch:1];
            (void)[dropPump enqueueSource:10 appFrameId:10 payload:@(10)
                atNs:100 * kMs];
            (void)[dropPump enqueueSource:11 appFrameId:11 payload:@(11)
                atNs:134 * kMs];
            [dropPump primeAtNs:100 * kMs];
            R123DisplayItem *dropItem = nil;
            R123DisplayItem *dropMissed = nil;
            (void)[dropPump takeForSlotAtNs:100 * kMs
                outItem:&dropItem outMissed:&dropMissed];
            (void)[dropPump markItemDispatched:dropItem wsiIdentity:7010];
            dropItem = nil;
            (void)[dropPump takeForSlotAtNs:100 * kMs + 2 * 34 * kMs +
                kHalfPeriodNs outItem:&dropItem outMissed:&dropMissed];
            R123DiagnosticSnapshot dropped =
                [dropPump diagnosticSnapshotAtNs:200 * kMs];
            const BOOL droppedPlaceholder = dropMissed &&
                dropMissed.kind == R123ItemKindInternal && dropMissed.seqA == 10 &&
                dropped.internalPlaceholderCount == 0 &&
                dropped.internalEnqueued == 1 && dropped.internalResolved == 0 &&
                dropped.internalDropped == 1 &&
                dropped.internalDroppedUnresolved == 1 && dropped.deadlineMisses == 1 &&
                dropped.claimed.present && dropped.claimed.seqB == 11;
            const BOOL sourceRetry = [dropPump requeueUndispatchedSource:dropItem
                forEpoch:1];
            R123DiagnosticSnapshot afterDropRetry =
                [dropPump diagnosticSnapshotAtNs:201 * kMs];
            const BOOL dropRequeueSnapshot = sourceRetry &&
                afterDropRetry.head.present && afterDropRetry.head.seqB == 11 &&
                !afterDropRetry.claimed.present &&
                afterDropRetry.internalPlaceholderCount == 0 &&
                afterDropRetry.committedQueuedSourceCount == 1 &&
                afterDropRetry.committedClaimedSourceCount == 0;

            check("reservation snapshot excludes provisional SOURCE from committed work",
                  provisionalSnapshot, NULL);
            check("commit snapshot reports queue shape, SOURCE age bounds and totals",
                  firstCommitted && secondCommitted && queueShape, NULL);
            check("resolve snapshot moves placeholder into ready INTERNAL count",
                  resolvedSnapshot, NULL);
            check("claim snapshot copies identity, readiness, slot and claim time",
                  claimSnapshot, NULL);
            check("requeue snapshot returns claimed SOURCE to queue without releasing it",
                  requeueSnapshot, NULL);
            check("dispatch snapshot clears claim and advances terminal totals",
                  dispatchSnapshot, NULL);
            check("INTERNAL dispatch snapshot advances resolved and presented totals",
                  internalTotals, NULL);
            check("drop snapshot removes unresolved placeholder and preserves SOURCE claim",
                  droppedPlaceholder, NULL);
            check("post-drop SOURCE requeue remains visible and bounded",
                  dropRequeueSnapshot, NULL);
        }

        printf("\nSELFTEST=%s cases=19\n", failures == 0 ? "PASS" : "FAIL");
        return failures == 0 ? 0 : 1;
    }
}
