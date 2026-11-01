// R117 Phase 2c — does a sub-refresh presentation offset have any effect?
//
// The first attempt at this (r117_schedule_granularity) was confounded: it
// recomputed its anchor with ceil() and slept 25 ms between samples, so every
// requested time was already in the past and every frame resolved to "the next
// refresh after now" regardless of offset. That measures the probe's pacing, not
// the compositor.
//
// This version removes the confound. For each offset it:
//
//   1. computes a base on a refresh boundary ~2 refreshes in the FUTURE,
//   2. presents A with presentAtTime: base,
//   3. immediately presents M with presentAtTime: base + offset*q,
//   4. reads both presentedTimes.
//
// Both targets are comfortably in the future when issued, and nothing sleeps
// between them. If the compositor can represent sub-refresh times, (M - A) must
// grow with offset. If it snaps to refresh boundaries, (M - A) is pinned at one
// refresh for every offset and the offset has no effect.
//
// Build:
//   clang -fobjc-arc -O2 -framework Cocoa -framework Metal -framework QuartzCore \
//         r117_subrefresh_probe.m -o r117_subrefresh_probe

#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <mach/mach_time.h>

static double nowSeconds(void) {
    static mach_timebase_info_data_t tb;
    if (tb.denom == 0) mach_timebase_info(&tb);
    return (double)mach_absolute_time() * (double)tb.numer / (double)tb.denom / 1e9;
}

@interface SubProbe : NSObject
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) double q;
@end

@implementation SubProbe

- (instancetype)init {
    self = [super init];
    if (!self) return nil;
    _q = 1.0 / 60.0;
    _device = MTLCreateSystemDefaultDevice();
    _queue = [_device newCommandQueue];
    NSRect f = NSMakeRect(80, 80, 800, 500);
    _window = [[NSWindow alloc] initWithContentRect:f styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered defer:NO];
    _window.level = NSFloatingWindowLevel;
    _window.backgroundColor = [NSColor blackColor];
    _layer = [CAMetalLayer layer];
    _layer.device = _device;
    _layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    _layer.framebufferOnly = NO;
    _layer.displaySyncEnabled = YES;
    _layer.frame = f;
    _layer.drawableSize = CGSizeMake(800, 500);
    _window.contentView.layer = _layer;
    _window.contentView.wantsLayer = YES;
    [_window makeKeyAndOrderFront:nil];
    [_window orderFrontRegardless];
    return self;
}

- (void)encode:(id<CAMetalDrawable>)d grey:(double)g {
    id<MTLCommandBuffer> cb = [self.queue commandBuffer];
    MTLRenderPassDescriptor *p = [MTLRenderPassDescriptor renderPassDescriptor];
    p.colorAttachments[0].texture = d.texture;
    p.colorAttachments[0].loadAction = MTLLoadActionClear;
    p.colorAttachments[0].storeAction = MTLStoreActionStore;
    p.colorAttachments[0].clearColor = MTLClearColorMake(g, g, g, 1.0);
    [[cb renderCommandEncoderWithDescriptor:p] endEncoding];
    [cb commit];
    [cb waitUntilCompleted];
}

- (void)run {
    const double q = self.q;

    // Warm up: the first present after window creation is never displayed.
    for (int i = 0; i < 12; ++i) {
        id<CAMetalDrawable> d = [self.layer nextDrawable];
        if (!d) continue;
        [self encode:d grey:0.0];
        [d presentAfterMinimumDuration:q];
        usleep(17000);
    }
    usleep(120000);

    const int offsets = 10;
    const int passes = 3;
    double aToM[64], err[64];
    double offs[64];
    int n = 0, dropped = 0;

    for (int pass = 0; pass < passes; ++pass) {
        for (int i = 0; i < offsets; ++i) {
            int idx = (pass % 2 == 0) ? i : (offsets - 1 - i);
            double offset = (double)idx / (double)offsets;

            // Base on a boundary two refreshes out, computed immediately before
            // the presents so both targets are still in the future.
            double base = ceil((nowSeconds() + 2.0 * q) / q) * q;

            id<CAMetalDrawable> dA = [self.layer nextDrawable];
            if (!dA) { dropped++; continue; }
            __block double pA = 0.0;
            dispatch_semaphore_t sA = dispatch_semaphore_create(0);
            [dA addPresentedHandler:^(id<MTLDrawable> p) {
                pA = p.presentedTime; dispatch_semaphore_signal(sA);
            }];
            [self encode:dA grey:0.2];

            id<CAMetalDrawable> dM = [self.layer nextDrawable];
            if (!dM) { dropped++; continue; }
            __block double pM = 0.0;
            dispatch_semaphore_t sM = dispatch_semaphore_create(0);
            [dM addPresentedHandler:^(id<MTLDrawable> p) {
                pM = p.presentedTime; dispatch_semaphore_signal(sM);
            }];
            [self encode:dM grey:0.8];

            [dA presentAtTime:base];
            [dM presentAtTime:base + offset * q];

            dispatch_semaphore_wait(sA, dispatch_time(DISPATCH_TIME_NOW, 400 * NSEC_PER_MSEC));
            dispatch_semaphore_wait(sM, dispatch_time(DISPATCH_TIME_NOW, 400 * NSEC_PER_MSEC));
            usleep(30000);

            if (pA <= 0.0 || pM <= 0.0) {
                dropped++;
                printf("R117_SUB_SAMPLE pass=%d offset=%.2f dropped=1\n", pass, offset);
                fflush(stdout);
                continue;
            }
            double am = (pM - pA) * 1000.0;
            double e = (pM - base - offset * q) * 1000.0;
            offs[n] = offset;
            aToM[n] = am;
            err[n] = e;
            n++;
            printf("R117_SUB_SAMPLE pass=%d offset=%.2f requestedOffsetMs=%.4f "
                   "aToMMs=%.4f realisedOffsetMs=%.4f errorMs=%.4f\n",
                   pass, offset, offset * q * 1000.0, am, (pM - base) * 1000.0, e);
            fflush(stdout);
        }
    }

    printf("R117_SUB_SUMMARY samples=%d dropped=%d quantumMs=%.4f\n", n, dropped, q * 1000.0);
    if (n > 1) {
        double mn = aToM[0], mx = aToM[0];
        for (int i = 1; i < n; ++i) { if (aToM[i] < mn) mn = aToM[i]; if (aToM[i] > mx) mx = aToM[i]; }
        printf("R117_SUB_VERDICT aToMminMs=%.4f aToMmaxMs=%.4f aToMspreadMs=%.4f "
               "requestedOffsetRangeMs=%.4f subRefreshRepresentable=%d\n",
               mn, mx, mx - mn, (offsets - 1) / (double)offsets * q * 1000.0,
               (mx - mn) > 1.0 ? 1 : 0);
    }
}

@end

int main(void) {
    @autoreleasepool {
        CGDisplayModeRef m = CGDisplayCopyDisplayMode(CGMainDisplayID());
        double hz = 0.0;
        if (m) { hz = CGDisplayModeGetRefreshRate(m); CFRelease(m); }
        double q = (hz > 1.0) ? 1.0 / hz : (1.0 / 60.0);
        printf("R117_SUB_CONFIG displayHz=%.4f quantumMs=%.4f\n", hz, q * 1000.0);
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        SubProbe *probe = [[SubProbe alloc] init];
        probe.q = q;
        [NSApp finishLaunching];
        for (int i = 0; i < 40; ++i)
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        [probe run];
        for (int i = 0; i < 20; ++i)
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
    }
    return 0;
}
