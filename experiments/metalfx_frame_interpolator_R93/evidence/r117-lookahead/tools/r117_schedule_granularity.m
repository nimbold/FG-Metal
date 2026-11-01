// R117 Phase 2b — presentation scheduling granularity sweep.
//
// The half-interval question reduces to one measurable property: what is the
// smallest spacing the compositor will actually produce between two presented
// frames, and what does it do with a requested presentation time that falls
// inside a refresh interval?
//
// This tool presents ONE drawable at a time (so the drawable pool can never be
// the explanation) and sweeps the requested offset inside the refresh interval:
//
//     target = last_presented + q + offset * q,   offset in [0, 1)
//
// For each request it records where the frame ACTUALLY landed. If the mapping
// from requested offset to realised offset is a step function that jumps a whole
// refresh, then sub-refresh scheduling does not exist on this display and no
// amount of queueing can create a midpoint.
//
// Build:
//   clang -fobjc-arc -O2 -framework Cocoa -framework Metal -framework QuartzCore \
//         r117_schedule_granularity.m -o r117_schedule_granularity

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

@interface GranProbe : NSObject
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) double quantum;
@end

@implementation GranProbe

- (instancetype)init {
    self = [super init];
    if (!self) return nil;
    _quantum = 1.0 / 60.0;
    _device = MTLCreateSystemDefaultDevice();
    _queue = [_device newCommandQueue];
    NSRect frame = NSMakeRect(80, 80, 800, 500);
    _window = [[NSWindow alloc] initWithContentRect:frame
        styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered defer:NO];
    _window.title = @"R117 scheduling granularity probe";
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
    _layer.drawableSize = CGSizeMake(800, 500);
    _window.contentView.layer = _layer;
    _window.contentView.wantsLayer = YES;
    [_window makeKeyAndOrderFront:nil];
    [_window orderFrontRegardless];
    return self;
}

// Present one frame and return its presentedTime (0 if it was never displayed).
- (double)presentOneAtOffset:(double)offset
                      anchor:(double)anchor
                        grey:(double)grey
                     dropped:(BOOL *)dropped {
    id<CAMetalDrawable> d = [self.layer nextDrawable];
    if (!d) { if (dropped) *dropped = YES; return 0.0; }

    __block double pt = 0.0;
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    [d addPresentedHandler:^(id<MTLDrawable> p) {
        pt = p.presentedTime;
        dispatch_semaphore_signal(sem);
    }];

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

    double target = anchor + offset * self.quantum;
    [d presentAtTime:target];
    dispatch_semaphore_wait(sem, dispatch_time(DISPATCH_TIME_NOW, 300 * NSEC_PER_MSEC));
    if (dropped) *dropped = (pt <= 0.0);
    return pt;
}

- (void)run {
    const double q = self.quantum;

    // The very first present after the window is created is never displayed
    // (presentedTime stays 0), so warm the layer up before measuring anything.
    for (int i = 0; i < 10; ++i) {
        id<CAMetalDrawable> dw = [self.layer nextDrawable];
        if (!dw) continue;
        id<MTLCommandBuffer> cbw = [self.queue commandBuffer];
        MTLRenderPassDescriptor *pw = [MTLRenderPassDescriptor renderPassDescriptor];
        pw.colorAttachments[0].texture = dw.texture;
        pw.colorAttachments[0].loadAction = MTLLoadActionClear;
        pw.colorAttachments[0].storeAction = MTLStoreActionStore;
        [[cbw renderCommandEncoderWithDescriptor:pw] endEncoding];
        [cbw presentDrawable:dw];
        [cbw commit];
        usleep(17000);
    }
    usleep(100000);

    // Establish the display phase: present one frame normally and read where it
    // landed. Everything else is expressed relative to that boundary.
    id<CAMetalDrawable> d0 = [self.layer nextDrawable];
    if (!d0) { fprintf(stderr, "no drawable\n"); return; }
    __block double phase = 0.0;
    dispatch_semaphore_t sem0 = dispatch_semaphore_create(0);
    [d0 addPresentedHandler:^(id<MTLDrawable> p) {
        phase = p.presentedTime;
        dispatch_semaphore_signal(sem0);
    }];
    id<MTLCommandBuffer> cb0 = [self.queue commandBuffer];
    MTLRenderPassDescriptor *pass0 = [MTLRenderPassDescriptor renderPassDescriptor];
    pass0.colorAttachments[0].texture = d0.texture;
    pass0.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass0.colorAttachments[0].storeAction = MTLStoreActionStore;
    [[cb0 renderCommandEncoderWithDescriptor:pass0] endEncoding];
    [cb0 presentDrawable:d0];
    [cb0 commit];
    dispatch_semaphore_wait(sem0, dispatch_time(DISPATCH_TIME_NOW, 300 * NSEC_PER_MSEC));
    if (phase <= 0.0) {
        fprintf(stderr, "R117_GRAN_FAIL no-phase\n");
        return;
    }
    printf("R117_GRAN_PHASE presentedTime=%.6f quantumMs=%.4f\n", phase, q * 1000.0);

    // Sweep the offset inside one refresh interval, several passes, alternating
    // direction so a monotonic drift cannot masquerade as a pattern.
    int offsets = 10;
    int passes = 3;
    double anchor = phase;
    for (int pass = 0; pass < passes; ++pass) {
        for (int i = 0; i < offsets; ++i) {
            int idx = (pass % 2 == 0) ? i : (offsets - 1 - i);
            double offset = (double)idx / (double)offsets;   // 0.0 .. 0.9
            // Advance one full refresh past the last observed presentation, then
            // request `offset` of the way into the following interval.
            anchor = ceil((anchor + q) / q) * q;
            BOOL dropped = NO;
            double pt = [self presentOneAtOffset:offset anchor:anchor grey:0.3 dropped:&dropped];
            if (dropped) {
                printf("R117_GRAN_SAMPLE pass=%d offset=%.2f requestedOffsetMs=%.4f "
                       "realised=DROP\n", pass, offset, offset * q * 1000.0);
                fflush(stdout);
                usleep(40000);
                continue;
            }
            double realised = (pt - anchor) / q;
            printf("R117_GRAN_SAMPLE pass=%d offset=%.2f requestedOffsetMs=%.4f "
                   "realisedOffsetMs=%.4f realisedOffsetInRefresh=%.4f "
                   "errorMs=%.4f\n",
                   pass, offset, offset * q * 1000.0,
                   (pt - anchor) * 1000.0, realised, (pt - anchor - offset * q) * 1000.0);
            fflush(stdout);
            anchor = pt;
            usleep(25000);
        }
    }
    printf("R117_GRAN_DONE\n");
}

@end

int main(void) {
    @autoreleasepool {
        CGDisplayModeRef m = CGDisplayCopyDisplayMode(CGMainDisplayID());
        double hz = 0.0;
        if (m) { hz = CGDisplayModeGetRefreshRate(m); CFRelease(m); }
        double q = (hz > 1.0) ? 1.0 / hz : (1.0 / 60.0);
        printf("R117_GRAN_CONFIG displayHz=%.4f quantumMs=%.4f\n", hz, q * 1000.0);

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        GranProbe *probe = [[GranProbe alloc] init];
        probe.quantum = q;
        [NSApp finishLaunching];
        for (int i = 0; i < 40; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
        [probe run];
        for (int i = 0; i < 20; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
    }
    return 0;
}
