#ifndef FG_METAL_R127_LATENCY_STATS_H
#define FG_METAL_R127_LATENCY_STATS_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef enum {
    R127LogAlways = 0,
    R127LogHealthyFrame,
    R127LogResidual,
    R127LogAnomaly,
    R127LogUnclassifiedEvent,
} R127LogClass;

static inline int r127LineStartsWith(const char *line, const char *prefix) {
    return line && prefix && strncmp(line, prefix, strlen(prefix)) == 0;
}

#define R127_HEALTHY_FRAME_PREFIX_COUNT 29u

static inline size_t r127HealthyFramePrefixIndex(const char *line) {
    static const char *const healthyFramePrefixes[R127_HEALTHY_FRAME_PREFIX_COUNT] = {
        "DRAWABLE_COMMAND_CORRELATED ", "R108_SOURCE_PRODUCER_COMMIT ",
        "R101_WSI_DISPATCH ", "R101_WSI_DISPLAYED ", "R123_SLOT index=",
        "R123_SLOT_SAMPLE ", "R123_SOURCE_ENQUEUED ",
        "R123_INTERNAL_ENQUEUED ", "R123_SOURCE_PRESENT=SUPPRESSED ",
        "R110_SOURCE_PRODUCER_COMMIT_RETURN ", "DRAWABLE_PRODUCER_COMPLETED ",
        "R108_GPU_PROCESSING_DISPATCHED ", "R108_GPU_INPUTS_ENQUEUED ",
        "R108_GPU_INPUTS_COMPLETE ", "R108_TITLE_CRITICAL_PATH_ASSERTION=PASS ",
        "R120_PACE_ACQUIRE ", "R110_SOURCE_NEXT_DRAWABLE_ENTRY ",
        "R110_SOURCE_NEXT_DRAWABLE_RETURN ", "R125_TITLE_ACQUISITION ",
        "R108_SOURCE_FRAME_ACQUIRED ", "R110_SOURCE_PRESENT_ENTRY ",
        "ELDENRING_SOURCE_COPY_ENQUEUED ", "R108_METALFX_TRANSITION ",
        "R123_FG_PAIR ", "R108_PROVIDER_WARM_STATE ",
        "R124_TEST_SUMMARY=COMPLETE ", "R125_ACQUISITION_SUMMARY ",
        "R125_REGISTRY_AUDIT ", "R120_PACE_SUMMARY ",
    };
    for (size_t i = 0; i < R127_HEALTHY_FRAME_PREFIX_COUNT; ++i) {
        if (r127LineStartsWith(line, healthyFramePrefixes[i])) return i;
    }
    return SIZE_MAX;
}

// Classify known per-frame successes, residual slot events, and failures before
// applying the counters in the observer's logLine(). Unknown records get their
// own bounded sample class; lifecycle records remain visible.
static inline R127LogClass r127ClassifyLogLine(const char *line) {
    if (!line) return R127LogAlways;
    static const char *const anomalyMarkers[] = {
        "=FAILED", "_FAILED ", "_ABORTED ", "=REFUSED", "_REFUSED ",
        "=REJECTED", "_REJECTED ", "=INACCESSIBLE", "_VIOLATION ",
        "TIMEOUT", "_FALLBACK ", "=BUSY", "_SKIPPED ", "_GAP ",
        "_DUPLICATE ", "=INVALID", "_INVALID ", "=MISMATCH", "_MISMATCH ",
        "=MISSING", "_MISSING ", "=DROPPED", "_DROPPED ",
    };
    for (size_t i = 0; i < sizeof(anomalyMarkers) / sizeof(anomalyMarkers[0]); ++i) {
        if (strstr(line, anomalyMarkers[i])) return R127LogAnomaly;
    }
    if (strstr(line, "error=") && !strstr(line, "error=none")) return R127LogAnomaly;

    if (r127LineStartsWith(line, "R123_SLOT_MISSED ") ||
        r127LineStartsWith(line, "R123_SLOT_IDLE ")) return R127LogResidual;

    // These R127 final/periodic diagnostics must survive after the capped
    // sample budget for unfamiliar prefixes is spent. Other legacy records
    // containing SUMMARY may still be per-frame and use their normal caps.
    if (r127LineStartsWith(line, "R123_PUMP_SUMMARY=COMPLETE") ||
        r127LineStartsWith(line, "R127_RUNTIME_SUMMARY=") ||
        r127LineStartsWith(line, "R127_LATENCY_")) return R127LogAlways;

    // R134 critical-path records are bounded at the source rather than here:
    // at most kR134PairLogLimit pair records per process, one timeline record
    // per periodic summary, and a fixed interval table per timeline. They must
    // not fall into the unclassified sample, which is a first-64 budget shared
    // with every other unfamiliar prefix and is exhausted long before the
    // measured window opens.
    if (r127LineStartsWith(line, "R134_")) return R127LogAlways;

    // R136 miss-boundary records are bounded by construction (one line per
    // miss event, per event window, and per summary window) and are the
    // round's primary evidence: they must never fall into the shared
    // unclassified sample, which per-frame stream lines exhaust.
    if (r127LineStartsWith(line, "R136_")) return R127LogAlways;

    // R137 outage summaries and detailed traces are bounded by the outage
    // count/record caps at their source. Preserve them independently of the
    // shared unknown-event budget so unrelated per-frame diagnostics cannot
    // hide the starvation timeline.
    if (r127LineStartsWith(line, "R137_")) return R127LogAlways;

    // This is a bounded, one-time worker lifecycle record. Keep it visible
    // even if hook events exhausted the shared unknown-event sample budget.
    if (r127LineStartsWith(line, "R127_PUMP_THREAD_QOS ")) return R127LogAlways;

    if (r127HealthyFramePrefixIndex(line) != SIZE_MAX) return R127LogHealthyFrame;

    // Keep lifecycle/state-transition records visible even when they carry a
    // frame identifier. New unclassified per-frame events are sampled.
    if (r127LineStartsWith(line, "R127_RUNTIME_TRANSITION ") ||
        strstr(line, "BOUNDARY") ||
        strstr(line, "_STATE ") || strstr(line, "MODE=CHANGED") ||
        strstr(line, "=ARMED") || strstr(line, "=DISARMED") ||
        strstr(line, "=ENABLED") || strstr(line, "=DISABLED") ||
        strstr(line, "RECOVERED") || strstr(line, "EPOCH") ||
        (r127LineStartsWith(line, "R127_RUNTIME_DISABLE="))) return R127LogAlways;
    // Every other unrecognized record is sampled, including records without an
    // obvious sequence field. That keeps a newly added default log prefix from
    // silently becoming an unbounded per-frame stream.
    return R127LogUnclassifiedEvent;
}

// These helpers operate on an already sorted bounded sample copy. Nearest-rank
// percentiles use ceil(N * p / 100), matching the R127 report convention.
static inline uint64_t r127NearestRankNs(const uint64_t *sorted, size_t count,
                                         unsigned percentile) {
    if (!sorted || count == 0) return 0;
    if (percentile == 0) percentile = 1;
    if (percentile > 100) percentile = 100;
    const uint64_t rank = ((uint64_t)count * percentile + 99) / 100;
    const uint64_t index = rank ? rank - 1 : 0;
    return sorted[index < count ? index : count - 1];
}

static inline uint64_t r127MedianNs(const uint64_t *sorted, size_t count) {
    if (!sorted || count == 0) return 0;
    if (count & 1u) return sorted[count / 2];
    const uint64_t lower = sorted[count / 2 - 1];
    const uint64_t upper = sorted[count / 2];
    return lower / 2 + upper / 2 + ((lower & 1u) + (upper & 1u)) / 2;
}

#endif
