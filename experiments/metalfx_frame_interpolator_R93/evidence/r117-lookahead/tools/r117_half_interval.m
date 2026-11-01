// R117 Phase 2 — half-interval presentation experiment.
//
// Question: on THIS display, can a CAMetalLayer place an INTERNAL frame M at
// approximately HALF the SOURCE interval between A and B?
//
// The desired pattern is
//
//     A at t,  M at t + T/2,  B at t + T
//
// Phase 1 measured T == one display refresh, so this asks whether M can be
// scheduled inside a refresh the source does not already occupy.
//
// The acquisition/present sequence is the one the R116 ordering probe validated
// against a real layer (86/86 ordered): acquire A and present it, acquire B and
// hold it un-presented, acquire M and present it, then present B. That ordering
// is retained so the only variable is the scheduling call.
//
// Modes:
//   baseline          M and B use presentAfterMinimumDuration:1/60 (what the game
//                     and the current R116 pipeline do)
//   minduration-half  M and B use presentAfterMinimumDuration:T/2
//   attime-half       A, M, B scheduled with absolute presentAtTime at t, t+T/2, t+T
//   attime-same       control: M targeted at A's own refresh, to prove the probe
//                     detects coalescing when it is asked for
//   attime-one        control: M targeted one full refresh after A (the current
//                     R116 spacing), to prove absolute scheduling works at all
//
// Only presentedTime is treated as evidence. Dispatch order is not.
//
// Build:
//   clang -fobjc-arc -O2 -framework Cocoa -framework Metal -framework QuartzCore \
//         r117_half_interval.m -o r117_half_interval

#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <mach/mach_time.h>

static double nowSeconds(void) {
    static mach_timebase_info_data_t timebase;
    if (timebase.denom == 0) mach_timebase_info(&timebase);
    return (double)mach_absolute_time() * (double)timebase.numer /
           (double)timebase.denom / 1e9;
}

static int cmpDouble(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x < y) ? -1 : ((x > y) ? 1 : 0);
}

static double percentile(double *v, int n, double p) {
    if (n <= 0) return NAN;
    double idx = p * (double)(n - 1);
    int lo = (int)floor(idx), hi = (int)ceil(idx);
    if (lo < 0) lo = 0;
    if (hi >= n) hi = n - 1;
    double f = idx - (double)lo;
    return v[lo] * (1.0 - f) + v[hi] * f;
}

static void reportStats(const char *name, double *v, int n) {
    if (n <= 0) {
        printf("R117_HALF_STATS name=%s n=0\n", name);
        return;
    }
    double *s = malloc(sizeof(double) * (size_t)n);
    memcpy(s, v, sizeof(double) * (size_t)n);
    qsort(s, (size_t)n, sizeof(double), cmpDouble);
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += s[i];
    printf("R117_HALF_STATS name=%s n=%d min=%.4f mean=%.4f median=%.4f "
           "p90=%.4f max=%.4f\n",
           name, n, s[0], sum / (double)n, percentile(s, n, 0.5),
           percentile(s, n, 0.9), s[n - 1]);
    free(s);
}

@interface HalfProbe : NSObject
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) double quantum;
@property(nonatomic) NSUInteger trials;
@property(nonatomic, copy) NSString *mode;
@end

@implementation HalfProbe

- (instancetype)initWithTrials:(NSUInteger)trials mode:(NSString *)mode {
    self = [super init];
    if (!self) return nil;
    _trials = trials;
    _mode = [mode copy];
    _quantum = 1.0 / 60.0;
    _device = MTLCreateSystemDefaultDevice();
    _queue = [_device newCommandQueue];
    NSRect frame = NSMakeRect(80, 80, 960, 600);
    _window = [[NSWindow alloc] initWithContentRect:frame
        styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered defer:NO];
    _window.title = @"R117 half-interval probe";
    _window.level = NSFloatingWindowLevel;
    _window.opaque = YES;
    _window.backgroundColor = [NSColor blackColor];
    _layer = [CAMetalLayer layer];
    _layer.device = _device;
    _layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    _layer.framebufferOnly = NO;
    _layer.displaySyncEnabled = YES;
    _layer.presentsWithTransaction = NO;
    _layer.frame = frame;
    _layer.drawableSize = CGSizeMake(960, 600);
    _window.contentView.layer = _layer;
    _window.contentView.wantsLayer = YES;
    [_window makeKeyAndOrderFront:nil];
    [_window orderFrontRegardless];
    return self;
}

// Render into the drawable. Presentation is issued by the caller, because the
// whole point is to vary the presentation call while holding everything else
// identical.
- (void)encode:(id<CAMetalDrawable>)d grey:(double)grey {
    id<MTLCommandBuffer> cb = [self.queue commandBuffer];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = d.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(grey, grey, grey, 1.0);
    id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:pass];
    [enc endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
}

- (void)run {
    const double q = self.quantum;
    const double T = q;
    NSString *mode = self.mode;

    int nAM = 0, nMB = 0, nAB = 0, nCoalesced = 0, nMissing = 0, nReversed = 0;
    double aToM[1024], mToB[1024], aToB[1024];

    for (NSUInteger trial = 0; trial < self.trials; ++trial) {
        @autoreleasepool {
            // Absolute targets are computed before anything is acquired, so they
            // are all comfortably in the future when the present calls happen.
            double now = nowSeconds();
            double base = ceil((now + 3.0 * q) / q) * q;

            // ---- A: an ordinary source frame, presented the way the game does ----
            id<CAMetalDrawable> dA = [self.layer nextDrawable];
            if (!dA) { fprintf(stderr, "no A drawable\n"); continue; }
            __block double pA = 0.0;
            dispatch_semaphore_t semA = dispatch_semaphore_create(0);
            [dA addPresentedHandler:^(id<MTLDrawable> p) {
                pA = p.presentedTime;
                dispatch_semaphore_signal(semA);
            }];
            [self encode:dA grey:0.05];
            BOOL absoluteMode = ([mode isEqualToString:@"attime-half"] ||
                                 [mode isEqualToString:@"attime-same"] ||
                                 [mode isEqualToString:@"attime-one"]);
            if (!absoluteMode) {
                [dA presentAfterMinimumDuration:q];
            }
            usleep(8000);

            // ---- B: the game's own next frame, acquired and held un-presented ----
            id<CAMetalDrawable> dB = [self.layer nextDrawable];
            if (!dB) { fprintf(stderr, "no B drawable\n"); continue; }
            __block double pB = 0.0;
            dispatch_semaphore_t semB = dispatch_semaphore_create(0);
            [dB addPresentedHandler:^(id<MTLDrawable> p) {
                pB = p.presentedTime;
                dispatch_semaphore_signal(semB);
            }];
            [self encode:dB grey:1.0];

            // ---- M: the INTERNAL frame under test ----
            id<CAMetalDrawable> dM = [self.layer nextDrawable];
            if (!dM) { fprintf(stderr, "no M drawable\n"); continue; }
            __block double pM = 0.0;
            dispatch_semaphore_t semM = dispatch_semaphore_create(0);
            [dM addPresentedHandler:^(id<MTLDrawable> p) {
                pM = p.presentedTime;
                dispatch_semaphore_signal(semM);
            }];
            [self encode:dM grey:0.5];

            double targetM = 0.0;
            if ([mode isEqualToString:@"baseline"]) {
                [dM presentAfterMinimumDuration:q];
                [dB presentAfterMinimumDuration:q];
            } else if ([mode isEqualToString:@"minduration-half"]) {
                [dM presentAfterMinimumDuration:q * 0.5];
                [dB presentAfterMinimumDuration:q * 0.5];
            } else if ([mode isEqualToString:@"attime-half"]) {
                // `presentAtTime:` only accepts a target that is still in the
                // future when the call is made, and the layer DROPS a drawable
                // whose target is too far ahead. Anchor on the next refresh
                // boundary from *now*, not from the start of the trial, and
                // issue the three presents back to back so the anchor is still
                // ahead for all of them.
                double anchor = ceil((nowSeconds() + 1.2 * q) / q) * q;
                [dA presentAtTime:anchor];
                targetM = anchor + T * 0.5;
                [dM presentAtTime:targetM];
                [dB presentAtTime:anchor + T];
            } else if ([mode isEqualToString:@"attime-same"]) {
                double anchor = ceil((nowSeconds() + 1.2 * q) / q) * q;
                [dA presentAtTime:anchor];
                targetM = anchor;
                [dM presentAtTime:targetM];
                [dB presentAtTime:anchor + T];
            } else if ([mode isEqualToString:@"attime-one"]) {
                double anchor = ceil((nowSeconds() + 1.2 * q) / q) * q;
                [dA presentAtTime:anchor];
                targetM = anchor + T;
                [dM presentAtTime:targetM];
                [dB presentAtTime:anchor + 2.0 * T];
            } else {
                [dM present];
                [dB present];
            }

            dispatch_semaphore_wait(semM, dispatch_time(DISPATCH_TIME_NOW, 400 * NSEC_PER_MSEC));
            dispatch_semaphore_wait(semB, dispatch_time(DISPATCH_TIME_NOW, 400 * NSEC_PER_MSEC));
            dispatch_semaphore_wait(semA, dispatch_time(DISPATCH_TIME_NOW, 400 * NSEC_PER_MSEC));
            usleep(20000);

            if (pA <= 0.0 || pM <= 0.0 || pB <= 0.0) {
                nMissing++;
                printf("R117_HALF_TUPLE mode=%s trial=%lu a=%.6f m=%.6f b=%.6f "
                       "missing=1\n", [mode UTF8String], (unsigned long)trial, pA, pM, pB);
                fflush(stdout);
                continue;
            }

            double am = (pM - pA) * 1000.0;
            double mb = (pB - pM) * 1000.0;
            double ab = (pB - pA) * 1000.0;
            BOOL coalesced = (pM == pB);
            BOOL reversed = (pM < pA || pB < pM);
            if (coalesced) nCoalesced++;
            if (reversed) nReversed++;
            if (am > 0.0) aToM[nAM++] = am;
            if (mb > 0.0) mToB[nMB++] = mb;
            aToB[nAB++] = ab;

            printf("R117_HALF_TUPLE mode=%s trial=%lu a=%.6f m=%.6f b=%.6f "
                   "aToMMs=%.4f mToBMs=%.4f aToBMs=%.4f targetM=%.6f "
                   "coalesced=%d reversed=%d\n",
                   [mode UTF8String], (unsigned long)trial, pA, pM, pB,
                   am, mb, ab, targetM, coalesced ? 1 : 0, reversed ? 1 : 0);
            fflush(stdout);
        }
    }

    printf("R117_HALF_SUMMARY mode=%s trials=%lu aToM=%d mToB=%d coalesced=%d "
           "reversed=%d missing=%d\n",
           [mode UTF8String], (unsigned long)self.trials,
           nAM, nMB, nCoalesced, nReversed, nMissing);
    reportStats("aToMMs", aToM, nAM);
    reportStats("mToBMs", mToB, nMB);
    reportStats("aToBMs", aToB, nAB);

    int halfCount = 0;
    for (int i = 0; i < nAM; ++i) {
        double ratio = aToM[i] / (q * 1000.0);
        if (ratio > 0.25 && ratio < 0.75) halfCount++;
    }
    printf("R117_HALF_VERDICT mode=%s halfIntervalSamples=%d of %d "
           "targetHalfMs=%.4f displayQuantumMs=%.4f\n",
           [mode UTF8String], halfCount, nAM, q * 500.0, q * 1000.0);
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *mode = (argc > 1) ? @(argv[1]) : @"attime-half";
        NSUInteger trials = (argc > 2) ? (NSUInteger)atoi(argv[2]) : 24;

        CGDisplayModeRef m = CGDisplayCopyDisplayMode(CGMainDisplayID());
        double hz = 0.0;
        if (m) { hz = CGDisplayModeGetRefreshRate(m); CFRelease(m); }
        double quantum = (hz > 1.0) ? 1.0 / hz : (1.0 / 60.0);
        printf("R117_HALF_CONFIG mode=%s trials=%lu displayHz=%.4f quantumMs=%.4f\n",
               [mode UTF8String], (unsigned long)trials, hz, quantum * 1000.0);

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        HalfProbe *probe = [[HalfProbe alloc] initWithTrials:trials mode:mode];
        if (!probe.device) {
            fprintf(stderr, "R117_HALF_FAIL no-metal-device\n");
            return 2;
        }
        probe.quantum = quantum;
        [NSApp finishLaunching];
        for (int i = 0; i < 40; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
        [probe run];
        for (int i = 0; i < 20; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
        printf("R117_HALF_DONE mode=%s\n", [mode UTF8String]);
    }
    return 0;
}
