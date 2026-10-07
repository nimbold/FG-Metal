#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#import <CoreGraphics/CoreGraphics.h>
#import <os/log.h>
#import <os/signpost.h>
#import <mach/mach_time.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstdio>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <string>
#include <unistd.h>
#include <vector>

namespace {
constexpr NSUInteger kDrawableWidth = 640;
constexpr NSUInteger kDrawableHeight = 360;
constexpr size_t kRecordReserve = 12000;
constexpr double kFeedbackDrainLimitSeconds = 5.0;

enum class RunMode : uint32_t {
  Motion60 = 0,
  Source30To60 = 1,
};

struct Color {
  double red;
  double green;
  double blue;
};

struct FrameRecord {
  uint64_t native_frame_id = 0;
  uint64_t callback_id = 0;
  uint64_t drawable_id = 0;
  uint64_t source_id = 0;
  uint64_t pair_a_id = 0;
  uint64_t pair_b_id = 0;
  uint64_t callback_mach_ns = 0;
  uint64_t command_buffer_commit_mach_ns = 0;
  uint64_t present_call_start_mach_ns = 0;
  uint64_t present_call_return_mach_ns = 0;
  uint64_t gpu_completion_mach_ns = 0;
  uint64_t presented_handler_mach_ns = 0;
  uint64_t presented_time_mach_ns = 0;
  double callback_ca_seconds = 0.0;
  double target_timestamp_seconds = 0.0;
  double target_presentation_timestamp_seconds = 0.0;
  double presented_time_seconds = 0.0;
  double color_red = 0.0;
  double color_green = 0.0;
  double color_blue = 0.0;
  uint32_t output_kind = 0;
  int32_t command_buffer_status = -1;
  bool gpu_completion_seen = false;
  bool presented_handler_seen = false;
};

uint64_t HostNanoseconds(mach_timebase_info_data_t timebase) {
  const long double ticks = static_cast<long double>(mach_absolute_time());
  const long double nanos = ticks * static_cast<long double>(timebase.numer) /
      static_cast<long double>(timebase.denom);
  return static_cast<uint64_t>(nanos);
}

uint64_t SplitMix64(uint64_t value) {
  value += 0x9e3779b97f4a7c15ULL;
  value = (value ^ (value >> 30)) * 0xbf58476d1ce4e5b9ULL;
  value = (value ^ (value >> 27)) * 0x94d049bb133111ebULL;
  return value ^ (value >> 31);
}

Color DistinctColor(uint64_t sequence, const Color *previous) {
  for (uint64_t salt = 0; salt < 64; ++salt) {
    const uint64_t hash = SplitMix64(sequence + salt * 0x9e3779b97f4a7c15ULL);
    Color candidate{
        static_cast<double>((hash >> 0) & 0xff) / 255.0,
        static_cast<double>((hash >> 8) & 0xff) / 255.0,
        static_cast<double>((hash >> 16) & 0xff) / 255.0};
    if (previous == nullptr) {
      return candidate;
    }
    const double dr = candidate.red - previous->red;
    const double dg = candidate.green - previous->green;
    const double db = candidate.blue - previous->blue;
    if (dr * dr + dg * dg + db * db >= 0.12 * 0.12) {
      return candidate;
    }
  }
  return {1.0 - previous->red, 1.0 - previous->green,
          1.0 - previous->blue};
}

Color SourceColor(uint64_t sourceId) {
  const uint64_t hash = SplitMix64(sourceId + 0x534f55524345ULL);
  return {static_cast<double>((hash >> 0) & 0xff) / 255.0,
          static_cast<double>((hash >> 8) & 0xff) / 255.0,
          static_cast<double>((hash >> 16) & 0xff) / 255.0};
}

Color MidpointColor(uint64_t sourceId) {
  const Color a = SourceColor(sourceId);
  const Color b = SourceColor(sourceId + 1);
  return {(a.red + b.red) * 0.5, (a.green + b.green) * 0.5,
          (a.blue + b.blue) * 0.5};
}

NSString *NSStringFromCFString(CFStringRef value) {
  if (value == nullptr) {
    return @"unset";
  }
  return CFBridgingRelease(value);
}
} // namespace

@interface NativeMetalChildView : NSView
@end

@implementation NativeMetalChildView
- (CALayer *)makeBackingLayer {
  return [CAMetalLayer layer];
}
@end

@interface NativeControlController : NSObject <NSApplicationDelegate,
                                                  NSWindowDelegate,
                                                  CAMetalDisplayLinkDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalDisplayLink *displayLink;
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> commandQueue;
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) NSTimer *stopTimer;
@property(nonatomic, strong) NSTimer *settleTimer;
@property(nonatomic, copy) NSString *csvPath;
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *modeName;
@property(nonatomic) RunMode mode;
@property(nonatomic) NSUInteger requestedRunSeconds;
@property(nonatomic) int exitCode;
@end

@implementation NativeControlController {
  std::vector<FrameRecord> records_;
  std::mutex recordsMutex_;
  std::atomic<uint64_t> nextCallbackId_;
  std::atomic<uint64_t> pendingGpu_;
  std::atomic<uint64_t> pendingPresented_;
  std::atomic<bool> acceptingCallbacks_;
  std::atomic<bool> finalized_;
  mach_timebase_info_data_t timebase_;
  uint64_t caBridgeMachNs_;
  double caBridgeSeconds_;
  double settleStartedSeconds_;
  Color previousMotionColor_;
  bool hasPreviousMotionColor_;
  os_log_t signpostLog_;
}

- (instancetype)initWithMode:(RunMode)mode
                    modeName:(NSString *)modeName
              requestedSecs:(NSUInteger)seconds
                      csvPath:(NSString *)csvPath
                   configPath:(NSString *)configPath {
  self = [super init];
  if (self) {
    _mode = mode;
    _modeName = [modeName copy];
    _requestedRunSeconds = seconds;
    _csvPath = [csvPath copy];
    _configPath = [configPath copy];
    nextCallbackId_.store(0);
    pendingGpu_.store(0);
    pendingPresented_.store(0);
    acceptingCallbacks_.store(false);
    finalized_.store(false);
    _exitCode = 0;
    try {
      records_.reserve(kRecordReserve);
    } catch (...) {
      fprintf(stderr, "ERROR could not reserve native presentation records\n");
      _exitCode = 1;
      return nil;
    }
    signpostLog_ = os_log_create("org.fgmetal.step10b6.native",
                                 "PointsOfInterest");
  }
  return self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
  (void)notification;
  if (mach_timebase_info(&timebase_) != KERN_SUCCESS ||
      timebase_.numer == 0 || timebase_.denom == 0) {
    fprintf(stderr, "ERROR mach_timebase_info failed\n");
    self.exitCode = 1;
    [NSApp terminate:nil];
    return;
  }

  self.device = MTLCreateSystemDefaultDevice();
  if (self.device == nil) {
    fprintf(stderr, "ERROR no default Metal device\n");
    self.exitCode = 1;
    [NSApp terminate:nil];
    return;
  }
  self.commandQueue = [self.device newCommandQueue];
  if (self.commandQueue == nil) {
    fprintf(stderr, "ERROR could not create Metal command queue\n");
    self.exitCode = 1;
    [NSApp terminate:nil];
    return;
  }

  const NSRect contentRect = NSMakeRect(0, 0, kDrawableWidth, kDrawableHeight);
  self.window = [[NSWindow alloc]
      initWithContentRect:contentRect
                styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                           NSWindowStyleMaskMiniaturizable |
                           NSWindowStyleMaskResizable)
                  backing:NSBackingStoreBuffered
                    defer:NO];
  self.window.title = @"Step 10B.6 Native CAMetalDisplayLink Control";
  self.window.delegate = self;
  self.window.opaque = YES;
  self.window.backgroundColor = NSColor.blackColor;

  NSView *rootView = [[NSView alloc] initWithFrame:contentRect];
  rootView.wantsLayer = YES;
  rootView.layer.opaque = YES;
  NativeMetalChildView *metalView =
      [[NativeMetalChildView alloc] initWithFrame:rootView.bounds];
  metalView.wantsLayer = YES;
  metalView.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  [rootView addSubview:metalView];
  self.window.contentView = rootView;
  self.metalLayer = (CAMetalLayer *)metalView.layer;
  if (self.metalLayer == nil) {
    fprintf(stderr, "ERROR child view did not create a CAMetalLayer\n");
    self.exitCode = 1;
    [NSApp terminate:nil];
    return;
  }

  self.metalLayer.device = self.device;
  self.metalLayer.pixelFormat = MTLPixelFormatBGRA8Unorm;
  self.metalLayer.drawableSize =
      CGSizeMake(kDrawableWidth, kDrawableHeight);
  self.metalLayer.contentsScale = self.window.backingScaleFactor;
  self.metalLayer.opaque = YES;
  self.metalLayer.framebufferOnly = NO;
  self.metalLayer.displaySyncEnabled = NO;

  [self.window center];
  [self.window makeKeyAndOrderFront:nil];
  [NSApp activateIgnoringOtherApps:YES];
  [self.window displayIfNeeded];

  self.displayLink = [[CAMetalDisplayLink alloc]
      initWithMetalLayer:self.metalLayer];
  if (self.displayLink == nil) {
    fprintf(stderr, "ERROR could not create CAMetalDisplayLink\n");
    self.exitCode = 1;
    [NSApp terminate:nil];
    return;
  }
  self.displayLink.delegate = self;
  self.displayLink.preferredFrameLatency = 1.0F;
  self.displayLink.preferredFrameRateRange =
      CAFrameRateRangeMake(60.0F, 60.0F, 60.0F);

  caBridgeSeconds_ = CACurrentMediaTime();
  caBridgeMachNs_ = HostNanoseconds(timebase_);
  acceptingCallbacks_.store(true, std::memory_order_release);
  [self.displayLink addToRunLoop:NSRunLoop.mainRunLoop
                         forMode:NSRunLoopCommonModes];

  [self writeConfiguration];
  self.stopTimer = [NSTimer scheduledTimerWithTimeInterval:self.requestedRunSeconds
                                                    target:self
                                                  selector:@selector(stopMeasurement:)
                                                  userInfo:nil
                                                   repeats:NO];
  fprintf(stdout, "native_control_started mode=%s pid=%d seconds=%lu windowed=1\n",
          self.modeName.UTF8String, getpid(),
          static_cast<unsigned long>(self.requestedRunSeconds));
  fflush(stdout);
}

- (void)windowDidResize:(NSNotification *)notification {
  (void)notification;
  if (self.metalLayer != nil) {
    self.metalLayer.drawableSize =
        CGSizeMake(kDrawableWidth, kDrawableHeight);
  }
}

- (void)metalDisplayLink:(CAMetalDisplayLink *)link
              needsUpdate:(CAMetalDisplayLinkUpdate *)update {
  if (!acceptingCallbacks_.load(std::memory_order_acquire)) {
    return;
  }

  id<CAMetalDrawable> drawable = update.drawable;
  const uint64_t callbackId = nextCallbackId_.fetch_add(1) + 1;
  FrameRecord record{};
  record.native_frame_id = callbackId;
  record.callback_id = callbackId;
  record.callback_mach_ns = HostNanoseconds(timebase_);
  record.callback_ca_seconds = CACurrentMediaTime();
  record.target_timestamp_seconds = update.targetTimestamp;
  record.target_presentation_timestamp_seconds =
      update.targetPresentationTimestamp;
  record.drawable_id = drawable ? drawable.drawableID : 0;

  Color color{};
  if (self.mode == RunMode::Motion60) {
    color = DistinctColor(callbackId, hasPreviousMotionColor_
                                          ? &previousMotionColor_
                                          : nullptr);
    previousMotionColor_ = color;
    hasPreviousMotionColor_ = true;
    record.output_kind = 0;
    record.source_id = callbackId;
    record.pair_a_id = callbackId;
    record.pair_b_id = callbackId;
  } else {
    const uint64_t zeroBasedCallback = callbackId - 1;
    if ((zeroBasedCallback & 1U) == 0) {
      const uint64_t sourceId = zeroBasedCallback / 2;
      color = SourceColor(sourceId);
      record.source_id = sourceId;
      record.pair_a_id = sourceId;
      record.pair_b_id = sourceId;
      record.output_kind = (sourceId & 1U) == 0 ? 1 : 3;
    } else {
      const uint64_t sourceId = zeroBasedCallback / 2;
      color = MidpointColor(sourceId);
      record.source_id = sourceId;
      record.pair_a_id = sourceId;
      record.pair_b_id = sourceId + 1;
      record.output_kind = 2;
    }
  }
  record.color_red = color.red;
  record.color_green = color.green;
  record.color_blue = color.blue;

  size_t recordIndex = 0;
  {
    std::lock_guard<std::mutex> lock(recordsMutex_);
    if (records_.size() >= kRecordReserve) {
      acceptingCallbacks_.store(false, std::memory_order_release);
      self.exitCode = 2;
      [link invalidate];
      return;
    }
    try {
      records_.push_back(record);
    } catch (...) {
      acceptingCallbacks_.store(false, std::memory_order_release);
      self.exitCode = 2;
      [link invalidate];
      return;
    }
    recordIndex = records_.size() - 1;
  }

  const os_signpost_id_t signpostId =
      static_cast<os_signpost_id_t>(callbackId);
  os_signpost_event_emit(
      signpostLog_, signpostId, "DISPLAY_CALLBACK",
      "native_frame_id=%{public}llu callback_id=%{public}llu drawable_id=%{public}llu output_kind=%{public}u source_id=%{public}llu pair_A=%{public}llu pair_B=%{public}llu",
      callbackId, callbackId, record.drawable_id, record.output_kind,
      record.source_id, record.pair_a_id, record.pair_b_id);

  if (drawable == nil) {
    return;
  }
  id<MTLCommandBuffer> commandBuffer = [self.commandQueue commandBuffer];
  if (commandBuffer == nil) {
    self.exitCode = 3;
    return;
  }
  MTLRenderPassDescriptor *pass = [MTLRenderPassDescriptor renderPassDescriptor];
  pass.colorAttachments[0].texture = drawable.texture;
  pass.colorAttachments[0].loadAction = MTLLoadActionClear;
  pass.colorAttachments[0].storeAction = MTLStoreActionStore;
  pass.colorAttachments[0].clearColor =
      MTLClearColorMake(color.red, color.green, color.blue, 1.0);
  id<MTLRenderCommandEncoder> encoder =
      [commandBuffer renderCommandEncoderWithDescriptor:pass];
  if (encoder == nil) {
    self.exitCode = 4;
    return;
  }
  [encoder endEncoding];

  NativeControlController *owner = self;
  pendingGpu_.fetch_add(1, std::memory_order_acq_rel);
  pendingPresented_.fetch_add(1, std::memory_order_acq_rel);
  [drawable addPresentedHandler:^(id<MTLDrawable> presented) {
    const uint64_t handlerNs = HostNanoseconds(owner->timebase_);
    const double presentedTime = presented.presentedTime;
    const uint64_t presentedTimeNs = presentedTime > 0.0
        ? owner->caBridgeMachNs_ + static_cast<uint64_t>(
              std::max(0.0, presentedTime - owner->caBridgeSeconds_) * 1.0e9)
        : 0;
    {
      std::lock_guard<std::mutex> lock(owner->recordsMutex_);
      if (recordIndex < owner->records_.size()) {
        FrameRecord &saved = owner->records_[recordIndex];
        saved.presented_handler_mach_ns = handlerNs;
        saved.presented_time_seconds = presentedTime;
        saved.presented_time_mach_ns = presentedTimeNs;
        saved.presented_handler_seen = true;
      }
    }
    owner->pendingPresented_.fetch_sub(1, std::memory_order_acq_rel);
    os_signpost_event_emit(
        owner->signpostLog_, signpostId, "DRAWABLE_FEEDBACK",
        "callback_id=%{public}llu drawable_id=%{public}llu presented_time=%{public}.9f",
        callbackId, record.drawable_id, presentedTime);
  }];
  [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
    const uint64_t completionNs = HostNanoseconds(owner->timebase_);
    {
      std::lock_guard<std::mutex> lock(owner->recordsMutex_);
      if (recordIndex < owner->records_.size()) {
        FrameRecord &saved = owner->records_[recordIndex];
        saved.gpu_completion_mach_ns = completionNs;
        saved.command_buffer_status = static_cast<int32_t>(completed.status);
        saved.gpu_completion_seen = true;
      }
    }
    owner->pendingGpu_.fetch_sub(1, std::memory_order_acq_rel);
    os_signpost_event_emit(
        owner->signpostLog_, signpostId, "PRESENT_GPU_COMPLETED",
        "callback_id=%{public}llu drawable_id=%{public}llu command_buffer_status=%{public}d",
        callbackId, record.drawable_id, static_cast<int32_t>(completed.status));
  }];

  const uint64_t commitNs = HostNanoseconds(timebase_);
  {
    std::lock_guard<std::mutex> lock(recordsMutex_);
    records_[recordIndex].command_buffer_commit_mach_ns = commitNs;
  }
  os_signpost_event_emit(signpostLog_, signpostId, "COMMAND_COMMITTED",
                         "callback_id=%{public}llu drawable_id=%{public}llu",
                         callbackId, record.drawable_id);
  [commandBuffer commit];

  const uint64_t presentStartNs = HostNanoseconds(timebase_);
  {
    std::lock_guard<std::mutex> lock(recordsMutex_);
    records_[recordIndex].present_call_start_mach_ns = presentStartNs;
  }
  os_signpost_event_emit(signpostLog_, signpostId, "DRAWABLE_PRESENT_BEGIN",
                         "callback_id=%{public}llu drawable_id=%{public}llu",
                         callbackId, record.drawable_id);
  [drawable present];
  const uint64_t presentReturnNs = HostNanoseconds(timebase_);
  {
    std::lock_guard<std::mutex> lock(recordsMutex_);
    records_[recordIndex].present_call_return_mach_ns = presentReturnNs;
  }
  os_signpost_event_emit(signpostLog_, signpostId, "DRAWABLE_PRESENT_END",
                         "callback_id=%{public}llu drawable_id=%{public}llu",
                         callbackId, record.drawable_id);
}

- (void)stopMeasurement:(NSTimer *)timer {
  (void)timer;
  [self.stopTimer invalidate];
  self.stopTimer = nil;
  acceptingCallbacks_.store(false, std::memory_order_release);
  [self.displayLink invalidate];
  settleStartedSeconds_ = CACurrentMediaTime();
  self.settleTimer = [NSTimer scheduledTimerWithTimeInterval:0.1
                                                      target:self
                                                    selector:@selector(checkFeedbackDrain:)
                                                    userInfo:nil
                                                     repeats:YES];
  [self checkFeedbackDrain:nil];
}

- (void)checkFeedbackDrain:(NSTimer *)timer {
  (void)timer;
  const bool drained = pendingGpu_.load(std::memory_order_acquire) == 0 &&
      pendingPresented_.load(std::memory_order_acquire) == 0;
  const bool timedOut = CACurrentMediaTime() - settleStartedSeconds_ >=
      kFeedbackDrainLimitSeconds;
  if (!drained && !timedOut) {
    return;
  }
  [self.settleTimer invalidate];
  self.settleTimer = nil;
  [self finalizeMeasurement];
}

- (void)writeConfiguration {
  std::ofstream out(self.configPath.fileSystemRepresentation,
                    std::ios::out | std::ios::trunc);
  if (!out) {
    fprintf(stderr, "ERROR could not create native control configuration file\n");
    self.exitCode = 5;
    return;
  }
  CGDisplayModeRef displayMode = CGDisplayCopyDisplayMode(CGMainDisplayID());
  const double refreshHz = displayMode ? CGDisplayModeGetRefreshRate(displayMode) : 0.0;
  const size_t displayWidth = displayMode ? CGDisplayModeGetPixelWidth(displayMode) : 0;
  const size_t displayHeight = displayMode ? CGDisplayModeGetPixelHeight(displayMode) : 0;
  if (displayMode != nullptr) {
    CFRelease(displayMode);
  }
  const CGSize drawableSize = self.metalLayer.drawableSize;
  out << "mode=" << self.modeName.UTF8String << '\n'
      << "pid=" << getpid() << '\n'
      << "display_id=" << CGMainDisplayID() << '\n'
      << "display_pixel_width=" << displayWidth << '\n'
      << "display_pixel_height=" << displayHeight << '\n'
      << "display_refresh_hz=" << std::fixed << std::setprecision(3) << refreshHz << '\n'
      << "window_style_mask=" << static_cast<unsigned long>(self.window.styleMask) << '\n'
      << "window_backing_type=" << static_cast<unsigned long>(self.window.backingType) << '\n'
      << "window_is_opaque=" << (self.window.opaque ? "true" : "false") << '\n'
      << "window_is_fullscreen=" << (self.window.styleMask & NSWindowStyleMaskFullScreen ? "true" : "false") << '\n'
      << "root_view_class=" << NSStringFromClass(self.window.contentView.class).UTF8String << '\n'
      << "root_view_wants_layer=" << (self.window.contentView.wantsLayer ? "true" : "false") << '\n'
      << "root_layer_class=" << NSStringFromClass(self.window.contentView.layer.class).UTF8String << '\n'
      << "metal_layer_class=" << NSStringFromClass(self.metalLayer.class).UTF8String << '\n'
      << "device_name=" << self.device.name.UTF8String << '\n'
      << "device_registry_id=" << self.device.registryID << '\n'
      << "pixel_format=" << static_cast<unsigned long>(self.metalLayer.pixelFormat) << " (BGRA8Unorm=80)\n"
      << "drawable_width=" << static_cast<size_t>(drawableSize.width) << '\n'
      << "drawable_height=" << static_cast<size_t>(drawableSize.height) << '\n'
      << "contents_scale=" << self.metalLayer.contentsScale << '\n'
      << "opaque=" << (self.metalLayer.opaque ? "true" : "false") << '\n'
      << "framebuffer_only=" << (self.metalLayer.framebufferOnly ? "true" : "false") << '\n'
      << "display_sync_enabled=" << (self.metalLayer.displaySyncEnabled ? "true" : "false") << '\n'
      << "maximum_drawable_count=" << self.metalLayer.maximumDrawableCount << '\n'
      << "allows_next_drawable_timeout=" << (self.metalLayer.allowsNextDrawableTimeout ? "true" : "false") << '\n'
      << "presents_with_transaction=" << (self.metalLayer.presentsWithTransaction ? "true" : "false") << '\n'
      << "colorspace=" << NSStringFromCFString(self.metalLayer.colorspace ? CGColorSpaceCopyName(self.metalLayer.colorspace) : nullptr).UTF8String << '\n'
      << "wants_extended_dynamic_range_content=" << (self.metalLayer.wantsExtendedDynamicRangeContent ? "true" : "false") << '\n'
      << "edr_metadata_set=" << (self.metalLayer.EDRMetadata != nil ? "true" : "false") << '\n'
      << "preferred_frame_latency_requested=" << self.displayLink.preferredFrameLatency << '\n'
      << "preferred_frame_rate_min=" << self.displayLink.preferredFrameRateRange.minimum << '\n'
      << "preferred_frame_rate_max=" << self.displayLink.preferredFrameRateRange.maximum << '\n'
      << "preferred_frame_rate_preferred=" << self.displayLink.preferredFrameRateRange.preferred << '\n'
      << "measurement_requested_seconds=" << self.requestedRunSeconds << '\n';
  out.close();
  if (!out) {
    fprintf(stderr, "ERROR writing native control configuration failed\n");
    self.exitCode = 5;
  }
}

- (void)finalizeMeasurement {
  if (finalized_.exchange(true, std::memory_order_acq_rel)) {
    return;
  }
  std::vector<FrameRecord> snapshot;
  {
    std::lock_guard<std::mutex> lock(recordsMutex_);
    snapshot = records_;
  }
  std::ofstream out(self.csvPath.fileSystemRepresentation,
                    std::ios::out | std::ios::trunc);
  if (!out) {
    fprintf(stderr, "ERROR could not create native measurement CSV\n");
    self.exitCode = 6;
    [NSApp terminate:nil];
    return;
  }
  out << "native_frame_id,callback_id,drawable_id,output_kind,source_id,pair_A,pair_B,"
         "callback_mach_ns,callback_ca_seconds,target_timestamp_seconds,target_presentation_timestamp_seconds,"
         "command_buffer_commit_mach_ns,present_call_start_mach_ns,present_call_return_mach_ns,"
         "gpu_completion_mach_ns,command_buffer_status,presented_handler_mach_ns,presented_time_seconds,"
         "presented_time_mach_ns,color_red,color_green,color_blue,gpu_completion_seen,presented_handler_seen\n";
  out << std::fixed << std::setprecision(9);
  for (const FrameRecord &r : snapshot) {
    out << r.native_frame_id << ',' << r.callback_id << ',' << r.drawable_id << ','
        << r.output_kind << ',' << r.source_id << ',' << r.pair_a_id << ','
        << r.pair_b_id << ',' << r.callback_mach_ns << ','
        << r.callback_ca_seconds << ',' << r.target_timestamp_seconds << ','
        << r.target_presentation_timestamp_seconds << ','
        << r.command_buffer_commit_mach_ns << ',' << r.present_call_start_mach_ns << ','
        << r.present_call_return_mach_ns << ',' << r.gpu_completion_mach_ns << ','
        << r.command_buffer_status << ',' << r.presented_handler_mach_ns << ','
        << r.presented_time_seconds << ',' << r.presented_time_mach_ns << ','
        << r.color_red << ',' << r.color_green << ',' << r.color_blue << ','
        << (r.gpu_completion_seen ? 1 : 0) << ','
        << (r.presented_handler_seen ? 1 : 0) << '\n';
  }
  out.close();
  if (!out) {
    fprintf(stderr, "ERROR writing native measurement CSV failed\n");
    self.exitCode = 6;
    [NSApp terminate:nil];
    return;
  }
  uint64_t positivePresented = 0;
  uint64_t zeroPresented = 0;
  uint64_t incompleteGpu = 0;
  uint64_t incompletePresented = 0;
  for (const FrameRecord &r : snapshot) {
    if (r.presented_time_seconds > 0.0) {
      ++positivePresented;
    } else if (r.presented_handler_seen) {
      ++zeroPresented;
    }
    incompleteGpu += r.gpu_completion_seen ? 0 : 1;
    incompletePresented += r.presented_handler_seen ? 0 : 1;
  }
  fprintf(stdout,
          "native_control_result mode=%s callbacks=%zu positive_presented_time=%llu zero_presented_time=%llu incomplete_gpu=%llu incomplete_presented_handler=%llu pending_gpu=%llu pending_presented=%llu csv=%s config=%s\n",
          self.modeName.UTF8String, snapshot.size(),
          static_cast<unsigned long long>(positivePresented),
          static_cast<unsigned long long>(zeroPresented),
          static_cast<unsigned long long>(incompleteGpu),
          static_cast<unsigned long long>(incompletePresented),
          static_cast<unsigned long long>(pendingGpu_.load()),
          static_cast<unsigned long long>(pendingPresented_.load()),
          self.csvPath.fileSystemRepresentation,
          self.configPath.fileSystemRepresentation);
  fflush(stdout);
  [NSApp terminate:nil];
}
@end

int main(int argc, const char *argv[]) {
  @autoreleasepool {
    if (argc != 5) {
      fprintf(stderr,
              "usage: %s MODE(motion60|source30-60) RUN_SECONDS OUTPUT_CSV CONFIG_TXT\n",
              argv[0]);
      return 2;
    }
    const std::string modeName(argv[1]);
    RunMode mode;
    if (modeName == "motion60") {
      mode = RunMode::Motion60;
    } else if (modeName == "source30-60") {
      mode = RunMode::Source30To60;
    } else {
      fprintf(stderr, "ERROR unsupported run mode: %s\n", argv[1]);
      return 2;
    }
    char *end = nullptr;
    const unsigned long seconds = std::strtoul(argv[2], &end, 10);
    if (end == argv[2] || *end != '\0' || seconds < 5 || seconds > 600) {
      fprintf(stderr, "ERROR RUN_SECONDS must be an integer from 5 through 600\n");
      return 2;
    }
    for (int i = 3; i <= 4; ++i) {
      if (std::ifstream(argv[i]).good()) {
        fprintf(stderr, "ERROR refusing to overwrite existing output: %s\n", argv[i]);
        return 2;
      }
    }

    [NSApplication sharedApplication];
    NSApp.activationPolicy = NSApplicationActivationPolicyRegular;
    NativeControlController *controller = [[NativeControlController alloc]
        initWithMode:mode
            modeName:[NSString stringWithUTF8String:modeName.c_str()]
       requestedSecs:static_cast<NSUInteger>(seconds)
             csvPath:[NSString stringWithUTF8String:argv[3]]
          configPath:[NSString stringWithUTF8String:argv[4]]];
    if (controller == nil) {
      return 1;
    }
    NSApp.delegate = controller;
    [NSApp run];
    return controller.exitCode;
  }
}
