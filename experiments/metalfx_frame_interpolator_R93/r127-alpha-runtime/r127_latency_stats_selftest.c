#include "R127LatencyStats.h"

#include <assert.h>
#include <stdio.h>

int main(void) {
    const uint64_t empty[] = {0};
    const uint64_t one[] = {42000};
    const uint64_t even[] = {10000, 20000, 30000, 40000};
    const uint64_t odd[] = {10000, 20000, 30000, 40000, 50000};

    assert(r127MedianNs(NULL, 0) == 0);
    assert(r127NearestRankNs(NULL, 0, 90) == 0);
    assert(r127MedianNs(empty, 0) == 0);
    assert(r127MedianNs(one, 1) == 42000);
    assert(r127NearestRankNs(one, 1, 99) == 42000);

    assert(r127MedianNs(even, 4) == 25000);
    assert(r127NearestRankNs(even, 4, 50) == 20000);
    assert(r127NearestRankNs(even, 4, 90) == 40000);
    assert(r127NearestRankNs(even, 4, 99) == 40000);
    assert(r127NearestRankNs(even, 4, 0) == 10000);
    assert(r127NearestRankNs(even, 4, 101) == 40000);

    assert(r127MedianNs(odd, 5) == 30000);
    assert(r127NearestRankNs(odd, 5, 90) == 50000);
    assert(r127NearestRankNs(odd, 5, 99) == 50000);

    assert(r127ClassifyLogLine("R101_WSI_DISPLAYED origin=SOURCE wsiPresentId=7") ==
           R127LogHealthyFrame);
    assert(r127ClassifyLogLine("R123_SOURCE_PRESENT=SUPPRESSED seq=42") ==
           R127LogHealthyFrame);
    assert(r127ClassifyLogLine("DRAWABLE_COMMAND_CORRELATED pid=1 seq=9") ==
           R127LogHealthyFrame);
    assert(r127ClassifyLogLine("R108_TITLE_CRITICAL_PATH_ASSERTION=PASS source=valid") ==
           R127LogHealthyFrame);
    assert(r127ClassifyLogLine("R123_SLOT_MISSED pair=3->4") == R127LogResidual);
    assert(r127ClassifyLogLine("R123_SLOT_IDLE slotNs=12") == R127LogResidual);
    assert(r127ClassifyLogLine("R123_SLOT_ABORTED reason=copy-failed") ==
           R127LogAnomaly);
    assert(r127ClassifyLogLine("R101_WSI_DISPLAYED origin=SOURCE error=timeout") ==
           R127LogAnomaly);
    assert(r127ClassifyLogLine("R127_RUNTIME_TRANSITION requestId=9 epoch=4 from=OFF to=ON") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R108_METALFX_TRANSITION previousSourceSequence=5 currentSourceSequence=6 result=SUCCESS") ==
           R127LogHealthyFrame);
    assert(r127ClassifyLogLine("NEW_FRAME_EVENT seq=12 status=ok") ==
           R127LogUnclassifiedEvent);
    assert(r127ClassifyLogLine("R127_STALE_CALLBACK_REJECTED epoch=3") ==
           R127LogAnomaly);
    assert(r127ClassifyLogLine("R108_SOURCE_SEQUENCE_GAP previous=4 current=8") ==
           R127LogAnomaly);
    assert(r127ClassifyLogLine("NEW_FRAME_EVENT seq=13 result=INVALID") ==
           R127LogAnomaly);
    assert(r127ClassifyLogLine("R126_SELF_PRESENT_GATE=ARMED") == R127LogAlways);
    assert(r127ClassifyLogLine("R115_TUPLE_STATE generation=2 pair=8->9") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R127_RUNTIME_EPOCH_READY epoch=4") == R127LogAlways);
    assert(r127ClassifyLogLine("R127_PUMP_THREAD_QOS configured=UNSPECIFIED actual=DEFAULT") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R127_GENERATION_RECOVERED pair=9->10") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R137_TRACE outage=1 index=0 event=PUMP_TICK") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R137_OUTAGE_SUMMARY reason=runtime-off") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R123_PUMP_SUMMARY=COMPLETE sourceEnqueued=10") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R127_LATENCY_SUMMARY kind=source count=10") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R127_LATENCY_CLOCK_CHECK sourceCallbacks=10") ==
           R127LogAlways);
    assert(r127ClassifyLogLine("R124_TEST_SUMMARY=COMPLETE mode=0") ==
           R127LogHealthyFrame);

    assert(r127ClassifyLogLine("R127_UNKNOWN_DIAGNOSTIC no-frame-id=1") ==
           R127LogUnclassifiedEvent);

    puts("R127_DIAGNOSTICS_SELFTEST=PASS cases=39 percentile=nearest-rank median=even-safe log-policy=bounded-default-errors-preserved r137=always");
    return 0;
}
