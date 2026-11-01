// R116 bounded CAMetalLayer presentation-ordering experiment.
//
// Question: does the same CAMetalLayer preserve queued drawable presentation
// order when an INTERNAL frame (M) is presented and the retained SOURCE frame
// (B) is presented immediately afterwards, without waiting for M's presented
// callback?
//
// The experiment reproduces the real acquisition order:
//   1. acquire and present S          (ordinary source frame, establishes cadence)
//   2. acquire B and render it, hold  (the game's own frame, not yet presented)
//   3. acquire M, render it, present  (the interpolated frame)
//   4. present B                      (immediately, or after M's callback)
//
// Both modes run in the same process against the same layer, interleaved, so
// display state cannot explain a difference.
//
// Output: one JSON object per tuple event on stdout, plus a summary.

#import <Cocoa/Cocoa.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <mach/mach_time.h>

static double nowSeconds(void) {
    static mach_timebase_info_data_t timebase;
    if (timebase.denom == 0) mach_timebase_info(&timebase);
    return (double)mach_absolute_time() * (double)timebase.numer / (double)timebase.denom / 1e9;
}

@interface OrderingProbe : NSObject
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalLayer *layer;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic) NSUInteger trials;
@property(nonatomic) BOOL verbose;
@end

@implementation OrderingProbe

- (instancetype)initWithTrials:(NSUInteger)trials verbose:(BOOL)verbose {
    self = [super init];
    if (!self) return nil;
    _trials = trials;
    _verbose = verbose;
    _device = MTLCreateSystemDefaultDevice();
    _queue = [_device newCommandQueue];
    NSRect frame = NSMakeRect(80, 80, 960, 600);
    _window = [[NSWindow alloc] initWithContentRect:frame
        styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered defer:NO];
    _window.title = @"R116 presentation ordering probe";
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
    [self render:drawable grey:grey timed:NO];
}

// `timed` reproduces Elden Ring's actual present call: the game presents with
// `presentAfterMinimumDuration:1/60`, not with the command buffer's
// presentDrawable:. The two have different queueing semantics, so the ordering
// claim has to hold for the one the game actually uses.
- (void)render:(id<CAMetalDrawable>)drawable grey:(double)grey timed:(BOOL)timed {
    id<MTLCommandBuffer> commandBuffer = [self.queue commandBuffer];
    MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
    pass.colorAttachments[0].texture = drawable.texture;
    pass.colorAttachments[0].loadAction = MTLLoadActionClear;
    pass.colorAttachments[0].storeAction = MTLStoreActionStore;
    pass.colorAttachments[0].clearColor = MTLClearColorMake(grey, grey, grey, 1.0);
    id<MTLRenderCommandEncoder> encoder = [commandBuffer renderCommandEncoderWithDescriptor:pass];
    [encoder endEncoding];
    if (timed) {
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        [drawable presentAfterMinimumDuration:1.0 / 60.0];
    } else {
        [commandBuffer presentDrawable:drawable];
        [commandBuffer commit];
    }
}

- (void)run {
    fprintf(stdout, "{\"event\":\"layer\",\"maximumDrawableCount\":%lu,\"displaySyncEnabled\":%d,"
                    "\"presentsWithTransaction\":%d,\"drawableSize\":\"%ldx%ld\",\"device\":\"%s\"}\n",
            (unsigned long)self.layer.maximumDrawableCount,
            self.layer.displaySyncEnabled, self.layer.presentsWithTransaction,
            (long)self.layer.drawableSize.width, (long)self.layer.drawableSize.height,
            self.device.name.UTF8String);

    for (NSUInteger trial = 0; trial < self.trials; ++trial) {
        @autoreleasepool {
            const NSUInteger modeIndex = trial % 4;
            BOOL early = modeIndex != 0;
            BOOL nextSourceAfterB = modeIndex == 2;
            BOOL timedPresent = modeIndex == 3;
            const char *modeName = modeIndex == 0 ? "callback-gated"
                : (modeIndex == 1 ? "early-b"
                   : (modeIndex == 2 ? "early-b-then-next-source"
                                     : "early-b-timed-present"));
            // 1. ordinary source frame keeps the layer's queue moving
            id<CAMetalDrawable> source = [self.layer nextDrawable];
            if (!source) { fprintf(stderr, "no source drawable\n"); continue; }
            [self render:source grey:0.05];
            usleep(8000);

            // 2. the game's own frame B: acquired and rendered, deliberately held
            id<CAMetalDrawable> drawableB = [self.layer nextDrawable];
            if (!drawableB) { fprintf(stderr, "no B drawable\n"); continue; }
            id<MTLCommandBuffer> commandBufferB = [self.queue commandBuffer];
            MTLRenderPassDescriptor *passB = [MTLRenderPassDescriptor renderPassDescriptor];
            passB.colorAttachments[0].texture = drawableB.texture;
            passB.colorAttachments[0].loadAction = MTLLoadActionClear;
            passB.colorAttachments[0].storeAction = MTLStoreActionStore;
            passB.colorAttachments[0].clearColor = MTLClearColorMake(0.0, 0.0, 1.0, 1.0);
            id<MTLRenderCommandEncoder> encoderB =
                [commandBufferB renderCommandEncoderWithDescriptor:passB];
            [encoderB endEncoding];
            [commandBufferB commit];
            [commandBufferB waitUntilCompleted];

            // 3. the INTERNAL frame M
            id<CAMetalDrawable> drawableM = [self.layer nextDrawable];
            if (!drawableM) { fprintf(stderr, "no M drawable\n"); continue; }
            __block double mCallbackSeconds = 0.0;
            __block double mPresentedTime = 0.0;
            __block BOOL mReachedDisplay = NO;
            dispatch_semaphore_t mSemaphore = dispatch_semaphore_create(0);
            [drawableM addPresentedHandler:^(id<MTLDrawable> presented) {
                mCallbackSeconds = nowSeconds();
                mPresentedTime = presented.presentedTime;
                mReachedDisplay = presented.presentedTime > 0.0;
                dispatch_semaphore_signal(mSemaphore);
            }];
            double mDispatchSeconds = nowSeconds();
            [self render:drawableM grey:0.5 timed:timedPresent];

            double bDispatchSeconds = 0.0;
            if (!early) {
                // callback-gated: the R115 behaviour
                dispatch_semaphore_wait(mSemaphore,
                    dispatch_time(DISPATCH_TIME_NOW, (int64_t)(200 * NSEC_PER_MSEC)));
            }

            __block double bCallbackSeconds = 0.0;
            __block double bPresentedTime = 0.0;
            __block BOOL bReachedDisplay = NO;
            dispatch_semaphore_t bSemaphore = dispatch_semaphore_create(0);
            [drawableB addPresentedHandler:^(id<MTLDrawable> presented) {
                bCallbackSeconds = nowSeconds();
                bPresentedTime = presented.presentedTime;
                bReachedDisplay = presented.presentedTime > 0.0;
                dispatch_semaphore_signal(bSemaphore);
            }];
            bDispatchSeconds = nowSeconds();
            [self render:drawableB grey:1.0 timed:timedPresent];

            // Realistic post-forward race: the game thread unblocks as soon as B
            // is queued, so the next SOURCE frame can reach the layer before M or
            // B have been displayed. Measure whether that disturbs their order.
            double nextSourceAcquireMs = -1.0;
            double nextSourceDispatchSeconds = 0.0;
            if (nextSourceAfterB) {
                usleep(2000);
                double acquireStart = nowSeconds();
                id<CAMetalDrawable> nextSource = [self.layer nextDrawable];
                nextSourceAcquireMs = (nowSeconds() - acquireStart) * 1000.0;
                if (nextSource) {
                    nextSourceDispatchSeconds = nowSeconds();
                    [self render:nextSource grey:0.25];
                }
            }

            dispatch_semaphore_wait(bSemaphore,
                dispatch_time(DISPATCH_TIME_NOW, (int64_t)(300 * NSEC_PER_MSEC)));
            if (early) {
                dispatch_semaphore_wait(mSemaphore,
                    dispatch_time(DISPATCH_TIME_NOW, (int64_t)(300 * NSEC_PER_MSEC)));
            }
            usleep(20000);

            BOOL orderPreserved = mReachedDisplay && bReachedDisplay &&
                mPresentedTime <= bPresentedTime;
            BOOL sameDisplayTime = mReachedDisplay && bReachedDisplay &&
                mPresentedTime == bPresentedTime;
            fprintf(stdout,
                "{\"event\":\"tuple\",\"trial\":%lu,\"mode\":\"%s\","
                "\"mDrawableID\":%lu,\"bDrawableID\":%lu,"
                "\"mDispatchSeconds\":%.9f,\"bDispatchSeconds\":%.9f,"
                "\"dispatchGapMs\":%.3f,"
                "\"mCallbackMsAfterDispatch\":%.3f,\"bCallbackMsAfterDispatch\":%.3f,"
                "\"mPresentedTime\":%.9f,\"bPresentedTime\":%.9f,"
                "\"presentedTimeDeltaMs\":%.3f,"
                "\"mReachedDisplay\":%d,\"bReachedDisplay\":%d,"
                "\"orderPreserved\":%d,\"sameDisplayTime\":%d,"
                "\"nextSourceAcquireMs\":%.3f,\"nextSourceDispatchSeconds\":%.9f,"
                "\"drawablesDistinct\":%d}\n",
                (unsigned long)trial, modeName,
                (unsigned long)drawableM.drawableID, (unsigned long)drawableB.drawableID,
                mDispatchSeconds, bDispatchSeconds,
                (bDispatchSeconds - mDispatchSeconds) * 1000.0,
                mCallbackSeconds > 0 ? (mCallbackSeconds - mDispatchSeconds) * 1000.0 : -1.0,
                bCallbackSeconds > 0 ? (bCallbackSeconds - bDispatchSeconds) * 1000.0 : -1.0,
                mPresentedTime, bPresentedTime,
                (bPresentedTime - mPresentedTime) * 1000.0,
                mReachedDisplay, bReachedDisplay,
                orderPreserved, sameDisplayTime,
                nextSourceAcquireMs, nextSourceDispatchSeconds,
                drawableM.drawableID != drawableB.drawableID);
            fflush(stdout);
        }
    }
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSUInteger trials = 24;
        BOOL verbose = NO;
        for (int i = 1; i < argc; ++i) {
            NSString *flag = @(argv[i]);
            if ([flag isEqualToString:@"--trials"] && i + 1 < argc)
                trials = strtoul(argv[++i], NULL, 10);
            else if ([flag isEqualToString:@"--verbose"]) verbose = YES;
        }
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        OrderingProbe *probe = [[OrderingProbe alloc] initWithTrials:trials verbose:verbose];
        // Give the window server a moment to put the layer on screen.
        [NSApp finishLaunching];
        for (int i = 0; i < 30; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
        [probe run];
        for (int i = 0; i < 20; ++i) {
            [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.01]];
        }
    }
    return 0;
}
