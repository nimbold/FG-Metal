// R117 Phase 1 — measure the ACTUAL display timing contract.
//
// The question R117 has to answer before any redesign:
//
//   What is the physical display refresh period, what presentation cadence does
//   a CAMetalLayer actually achieve, and what is the SOURCE presented-time
//   interval?
//
// Nothing here assumes 60 Hz or 120 Hz. Every number is measured on the host.
//
// Modes:
//   info      static facts: CGDisplayMode refresh, NSScreen max FPS, layer max
//   cadence   present as fast as the layer will let us (default presentDrawable:)
//   game      present the way Elden Ring actually does:
//             presentAfterMinimumDuration:1/60
//   refresh   present with presentAfterMinimumDuration:1/120, to test whether the
//             layer/display can be driven at 2x the 60 Hz cadence
//
// Output: one JSON line per presented frame, then a summary line.
//
// Build:
//   clang -fobjc-arc -O2 -framework Cocoa -framework Metal -framework QuartzCore \
//         -framework CoreVideo r117_display_cadence.m -o r117_display_cadence

#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <CoreVideo/CoreVideo.h>
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

static void report(const char *name, double *v, int n) {
    if (n <= 0) {
        printf("R117_CADENCE_STATS name=%s n=0\n", name);
        return;
    }
    double *s = malloc(sizeof(double) * (size_t)n);
    memcpy(s, v, sizeof(double) * (size_t)n);
    qsort(s, (size_t)n, sizeof(double), cmpDouble);
    double sum = 0.0;
    for (int i = 0; i < n; ++i) sum += s[i];
    printf("R117_CADENCE_STATS name=%s n=%d min=%.4f mean=%.4f median=%.4f "
           "p90=%.4f max=%.4f\n",
           name, n, s[0], sum / (double)n, percentile(s, n, 0.5),
           percentile(s, n, 0.9), s[n - 1]);
    free(s);
}

@interface CadenceProbe : NSObject
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) NSUInteger frames;
@property(nonatomic) double minimumDuration;
@property(nonatomic) BOOL useMinimumDuration;
@end

@implementation CadenceProbe

- (instancetype)initWithFrames:(NSUInteger)frames {
    self = [super init];
    if (!self) return nil;
    _frames = frames;
    _device = MTLCreateSystemDefaultDevice();
    _queue = [_device newCommandQueue];
    NSRect frame = NSMakeRect(120, 120, 960, 600);
    _window = [[NSWindow alloc] initWithContentRect:frame
        styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered defer:NO];
    _window.title = @"R117 display cadence probe";
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

- (void)render:(id<CAMetalDrawable>)drawable grey:(double)grey {
    id<MTLCommandBuffer> cb = [self.queue commandBuffer];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = drawable.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(grey, grey, grey, 1.0);
    id<MTLRenderCommandEncoder> enc = [cb renderCommandEncoderWithDescriptor:pass];
    [enc endEncoding];
    if (self.useMinimumDuration) {
        [cb commit];
        [cb waitUntilCompleted];
        [drawable presentAfterMinimumDuration:self.minimumDuration];
    } else {
        [cb presentDrawable:drawable];
        [cb commit];
    }
}

- (int)runWithLabel:(const char *)label {
    double *presented = calloc(self.frames + 4, sizeof(double));
    double *deltas = calloc(self.frames + 4, sizeof(double));
    int nPresented = 0, nDeltas = 0;
    double dispatchStart = nowSeconds();

    for (NSUInteger i = 0; i < self.frames; ++i) {
        id<CAMetalDrawable> d = [self.layer nextDrawable];
        if (!d) {
            fprintf(stderr, "R117_CADENCE_WARN nextDrawable=nil frame=%lu\n",
                    (unsigned long)i);
            continue;
        }
        [self render:d grey:(double)(i % 16) / 16.0];
        // Drain the presented callback for this drawable by waiting on the
        // command buffer's completion and then reading presentedTime after a
        // short spin. presentedTime is only valid once the frame is on screen.
        __block double pt = 0.0;
        if (self.useMinimumDuration) {
            // presentAfterMinimumDuration: the drawable is retained by the
            // layer; poll its presentedTime.
            for (int spin = 0; spin < 2000 && pt == 0.0; ++spin) {
                pt = d.presentedTime;
                if (pt == 0.0) usleep(500);
            }
        } else {
            for (int spin = 0; spin < 2000 && pt == 0.0; ++spin) {
                pt = d.presentedTime;
                if (pt == 0.0) usleep(500);
            }
        }
        if (pt > 0.0) {
            presented[nPresented++] = pt;
            if (nPresented >= 2) {
                double delta = presented[nPresented - 1] - presented[nPresented - 2];
                if (delta > 0.0) deltas[nDeltas++] = delta * 1000.0;
            }
        }
        printf("R117_CADENCE_FRAME label=%s index=%lu presentedTime=%.6f "
               "presentedMs=%.3f\n",
               label, (unsigned long)i, pt, pt * 1000.0);
    }
    double elapsed = nowSeconds() - dispatchStart;
    printf("R117_CADENCE_RUN label=%s frames=%lu wallMs=%.2f presentedSamples=%d\n",
           label, (unsigned long)self.frames, elapsed * 1000.0, nPresented);
    report("presentedDeltaMs", deltas, nDeltas);
    free(presented);
    free(deltas);
    return nDeltas;
}

@end

static void printStaticInfo(void) {
    CGDirectDisplayID mainId = CGMainDisplayID();
    CGDisplayModeRef mode = CGDisplayCopyDisplayMode(mainId);
    double refresh = 0.0;
    if (mode) {
        refresh = CGDisplayModeGetRefreshRate(mode);
        printf("R117_DISPLAY cgRefreshHz=%.4f width=%zu height=%zu "
               "pixelWidth=%zu pixelHeight=%zu ioFlags=0x%x\n",
               refresh,
               CGDisplayModeGetWidth(mode), CGDisplayModeGetHeight(mode),
               CGDisplayModeGetPixelWidth(mode), CGDisplayModeGetPixelHeight(mode),
               (unsigned)CGDisplayModeGetIOFlags(mode));
        CFRelease(mode);
    } else {
        printf("R117_DISPLAY cgRefreshHz=unknown\n");
    }
    if (refresh > 0.0) {
        printf("R117_DISPLAY_REFRESH_PERIOD_MS=%.4f source=CGDisplayMode\n",
               1000.0 / refresh);
    } else {
        printf("R117_DISPLAY cgRefreshHz=0 (ProMotion variable refresh; "
               "CGDisplayMode does not report a fixed rate)\n");
    }

    NSScreen *screen = [NSScreen mainScreen];
    if (screen) {
        printf("R117_DISPLAY nsScreenMaxFPS=%ld backingScale=%.2f\n",
               (long)screen.maximumFramesPerSecond, screen.backingScaleFactor);
        if (screen.maximumFramesPerSecond > 0) {
            printf("R117_DISPLAY_MAX_FPS_PERIOD_MS=%.4f source=NSScreen\n",
                   1000.0 / (double)screen.maximumFramesPerSecond);
        }
    }

    // List every display mode the hardware advertises, so the available refresh
    // cadences are known rather than assumed.
    CFArrayRef modes = CGDisplayCopyAllDisplayModes(mainId, NULL);
    if (modes) {
        CFIndex count = CFArrayGetCount(modes);
        printf("R117_DISPLAY_MODE_COUNT count=%ld\n", (long)count);
        for (CFIndex i = 0; i < count; ++i) {
            CGDisplayModeRef m = (CGDisplayModeRef)CFArrayGetValueAtIndex(modes, i);
            double r = CGDisplayModeGetRefreshRate(m);
            if (r <= 0.0) continue;
            printf("R117_DISPLAY_MODE index=%ld refreshHz=%.4f periodMs=%.4f "
                   "w=%zu h=%zu pixelW=%zu pixelH=%zu\n",
                   (long)i, r, 1000.0 / r,
                   CGDisplayModeGetWidth(m), CGDisplayModeGetHeight(m),
                   CGDisplayModeGetPixelWidth(m), CGDisplayModeGetPixelHeight(m));
        }
        CFRelease(modes);
    }

    // CVDisplayLink reports the nominal output period the compositor is using.
    CVDisplayLinkRef link = NULL;
    if (CVDisplayLinkCreateWithActiveCGDisplays(&link) == kCVReturnSuccess && link) {
        CVTime t = CVDisplayLinkGetNominalOutputVideoRefreshPeriod(link);
        double hz = (t.timeScale > 0 && t.timeValue > 0)
                        ? (double)t.timeScale / (double)t.timeValue : 0.0;
        printf("R117_DISPLAY cvDisplayLinkNominalHz=%.4f nominalPeriodMs=%.4f\n",
               hz, hz > 0.0 ? 1000.0 / hz : 0.0);
        CVDisplayLinkRelease(link);
    }
}

int main(int argc, char **argv) {
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];

        NSString *mode = (argc > 1) ? [NSString stringWithUTF8String:argv[1]] : @"info";
        NSUInteger frames = (argc > 2) ? (NSUInteger)atoi(argv[2]) : 120;

        printStaticInfo();

        if ([mode isEqualToString:@"info"]) return 0;

        CadenceProbe *probe = [[CadenceProbe alloc] initWithFrames:frames];
        if (!probe.device) {
            fprintf(stderr, "R117_CADENCE_FAIL no-metal-device\n");
            return 2;
        }

        // Let the layer settle onto the display before measuring.
        for (int i = 0; i < 8; ++i) {
            id<CAMetalDrawable> d = [probe.layer nextDrawable];
            if (d) [probe render:d grey:0.0];
        }
        usleep(200000);

        if ([mode isEqualToString:@"cadence"]) {
            probe.useMinimumDuration = NO;
            [probe runWithLabel:"unthrottled-default-present"];
        } else if ([mode isEqualToString:@"game"]) {
            probe.useMinimumDuration = YES;
            probe.minimumDuration = 1.0 / 60.0;
            [probe runWithLabel:"presentAfterMinimumDuration-1of60"];
        } else if ([mode isEqualToString:@"refresh"]) {
            probe.useMinimumDuration = YES;
            probe.minimumDuration = 1.0 / 120.0;
            [probe runWithLabel:"presentAfterMinimumDuration-1of120"];
        } else if ([mode isEqualToString:@"refresh240"]) {
            probe.useMinimumDuration = YES;
            probe.minimumDuration = 1.0 / 240.0;
            [probe runWithLabel:"presentAfterMinimumDuration-1of240"];
        } else if ([mode isEqualToString:@"none"]) {
            // Present with no minimum duration at all: the layer's own cadence,
            // unconstrained. This is the purest measurement of what the display
            // and compositor will actually do.
            probe.useMinimumDuration = NO;
            [probe runWithLabel:"no-minimum-duration"];
        } else {
            fprintf(stderr, "R117_CADENCE_FAIL unknown-mode=%s\n", [mode UTF8String]);
            return 2;
        }
        printf("R117_CADENCE_DONE mode=%s\n", [mode UTF8String]);
    }
    return 0;
}
