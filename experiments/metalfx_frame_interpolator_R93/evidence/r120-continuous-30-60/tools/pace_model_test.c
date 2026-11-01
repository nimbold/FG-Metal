// R120 pacer model test.
//
// This is a MODEL of the scheduling arithmetic in
// observer-title-inprocess-r120.m (r120PaceSourcePresent), not the shipped
// function itself. It exists to catch arithmetic errors -- drift, a wrong wait
// cap, a burst after a slow frame -- before a live session is spent discovering
// them. The live run remains the authority on whether the hook back-pressures
// the game; this only proves the scheduler converges on a caller that does
// present as fast as it is allowed.
//
// The expressions below are copied from the shipped function. If the shipped
// function changes, this must change with it.
//
// Build: clang -O2 -o pace_model_test pace_model_test.c
// Run:   ./pace_model_test

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#define NS_PER_S 1000000000ULL

typedef struct {
    uint64_t periodNs;
    uint64_t nextAllowedNs;
    uint64_t blockedNs;
    uint64_t blockedCount;
    uint64_t overCap;
} Pacer;

// Mirrors r120PaceSourcePresent's arithmetic exactly.
static uint64_t pace(Pacer *pacer, uint64_t entryNs) {
    uint64_t waitNs = 0;
    if (pacer->nextAllowedNs == 0) pacer->nextAllowedNs = entryNs;
    if (entryNs < pacer->nextAllowedNs) waitNs = pacer->nextAllowedNs - entryNs;

    uint64_t cappedNs = waitNs;
    if (cappedNs > 2 * pacer->periodNs) cappedNs = 2 * pacer->periodNs;
    if (cappedNs != waitNs) ++pacer->overCap;

    const uint64_t resumeNs = entryNs + cappedNs;
    uint64_t base = resumeNs > pacer->nextAllowedNs ? resumeNs : pacer->nextAllowedNs;
    pacer->nextAllowedNs = base + pacer->periodNs;
    if (waitNs) {
        pacer->blockedCount++;
        pacer->blockedNs += waitNs;
    }
    return resumeNs;
}

typedef struct {
    const char *name;
    uint64_t producerPeriodNs;   // how fast the caller would present unpaced
    uint64_t frames;
    uint64_t expectMinNs;        // allowed range for the steady-state interval
    uint64_t expectMaxNs;
} Case;

int main(void) {
    const uint64_t period = NS_PER_S / 30;   // 33,333,333 ns
    Case cases[] = {
        // A 60 Hz producer must be throttled to ~30 Hz.
        {"60Hz-producer-throttled-to-30", 16666666, 900, period - 100000, period + 100000},
        // A producer already slower than the period must not be delayed at all.
        {"20Hz-producer-not-delayed", 50000000, 300, 50000000, 50000000},
        // A producer exactly at the period must pass through untouched.
        {"30Hz-producer-passthrough", period, 600, period - 1000, period + 1000},
    };

    int failures = 0;
    for (size_t c = 0; c < sizeof(cases) / sizeof(cases[0]); ++c) {
        Pacer pacer = {.periodNs = period};
        uint64_t now = 1000ULL * NS_PER_S;
        uint64_t first = 0, prev = 0;
        uint64_t sum = 0, count = 0, minIv = UINT64_MAX, maxIv = 0;
        for (uint64_t i = 0; i < cases[c].frames; ++i) {
            uint64_t resume = pace(&pacer, now);
            if (!first) first = resume;
            if (prev && resume > prev) {
                uint64_t iv = resume - prev;
                sum += iv;
                ++count;
                if (iv < minIv) minIv = iv;
                if (iv > maxIv) maxIv = iv;
            }
            prev = resume;
            // The caller cannot start its next frame before present returns.
            now = resume + cases[c].producerPeriodNs;
        }
        uint64_t mean = count ? sum / count : 0;
        // Ignore the first two intervals: the pacer starts unprimed.
        double rateHz = mean ? (double)NS_PER_S / (double)mean : 0.0;
        int ok = mean >= cases[c].expectMinNs && mean <= cases[c].expectMaxNs;
        printf("  %-34s meanIntervalMs=%7.3f rateHz=%7.3f minMs=%7.3f maxMs=%7.3f "
               "blocked=%llu overCap=%llu %s\n",
               cases[c].name, (double)mean / 1e6, rateHz,
               (double)minIv / 1e6, (double)maxIv / 1e6,
               (unsigned long long)pacer.blockedCount,
               (unsigned long long)pacer.overCap,
               ok ? "OK" : "FAIL");
        if (!ok) ++failures;
    }

    // No drift over a long run: the schedule must not accumulate error.
    //
    // The pacer is a rate limiter, not a frame dropper: every present call still
    // results in one present, so N calls occupy (N-1) periods. 3600 calls at a
    // requested 30 Hz must therefore span 3599 periods, whatever the caller's
    // native rate was. Getting this expectation wrong the first time is exactly
    // the kind of arithmetic slip this test exists to surface.
    {
        Pacer pacer = {.periodNs = period};
        uint64_t now = 1000ULL * NS_PER_S;
        uint64_t first = 0, last = 0;
        const uint64_t frames = 3600;
        for (uint64_t i = 0; i < frames; ++i) {
            uint64_t resume = pace(&pacer, now);
            if (!first) first = resume;
            last = resume;
            now = resume + 16666666;
        }
        uint64_t elapsed = last - first;
        uint64_t expected = (frames - 1) * period;
        double errorMs = ((double)elapsed - (double)expected) / 1e6;
        int ok = errorMs > -5.0 && errorMs < 5.0;
        printf("  %-34s elapsedS=%9.4f expectedS=%9.4f driftMs=%7.3f %s\n",
               "no-drift-3600-presents", (double)elapsed / 1e9,
               (double)expected / 1e9, errorMs, ok ? "OK" : "FAIL");
        if (!ok) ++failures;
    }

    if (failures) {
        printf("R120_PACER_MODEL=FAIL cases=%d\n", failures);
        return 1;
    }
    printf("R120_PACER_MODEL=PASS cases=4\n");
    return 0;
}
