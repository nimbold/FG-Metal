#import <AppKit/AppKit.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#import <CoreGraphics/CoreGraphics.h>
#import <mach/mach_time.h>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <filesystem>
#include <system_error>
#include <atomic>
#include <cerrno>
#include <cstring>
#include <mutex>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace {
std::atomic<int> g_probe_exit_code{0};
}

struct Sample {
  uint64_t hostTicks;
  double caNow;
  double target;
  double targetPresentation;
};

@interface ProbeController : NSObject <NSApplicationDelegate, CAMetalDisplayLinkDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) CAMetalDisplayLink *displayLink;
@property(nonatomic, strong) NSTimer *stopTimer;
@property(nonatomic, copy) NSString *outputPath;
@property(nonatomic, copy) NSString *finalOutputPath;
@end

@implementation ProbeController {
  std::vector<Sample> samples_;
  std::mutex samples_mutex_;
  bool accepting_samples_;
  mach_timebase_info_data_t timebase_;
  uint64_t startTicks_;
}

- (instancetype)initWithTemporaryOutputPath:(NSString *)temporaryPath
                             finalOutputPath:(NSString *)finalPath {
  self = [super init];
  if (self) {
    _outputPath = [temporaryPath copy];
    _finalOutputPath = [finalPath copy];
    accepting_samples_ = true;
    try {
      samples_.reserve(4096);
    } catch (...) {
      fprintf(stderr, "ERROR could not reserve display-link sample storage\n");
      g_probe_exit_code.store(1);
      return nil;
    }
  }
  return self;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
  (void)notification;
  const kern_return_t timebase_status = mach_timebase_info(&timebase_);
  if (timebase_status != KERN_SUCCESS || timebase_.numer == 0 || timebase_.denom == 0) {
    fprintf(stderr, "ERROR mach_timebase_info failed: %d\n", timebase_status);
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }

  id<MTLDevice> device = MTLCreateSystemDefaultDevice();
  if (!device) {
    fprintf(stderr, "ERROR no default Metal device\n");
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }

  NSRect frame = NSMakeRect(0, 0, 640, 360);
  self.window = [[NSWindow alloc] initWithContentRect:frame
                                            styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                                              backing:NSBackingStoreBuffered
                                                defer:NO];
  [self.window setTitle:@"CAMetalDisplayLink timing probe"];
  NSView *view = [[NSView alloc] initWithFrame:frame];
  CAMetalLayer *layer = [CAMetalLayer layer];
  layer.device = device;
  layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
  layer.framebufferOnly = YES;
  layer.frame = view.bounds;
  layer.contentsScale = NSScreen.mainScreen.backingScaleFactor;
  view.wantsLayer = YES;
  view.layer = layer;
  self.window.contentView = view;
  [self.window center];
  [self.window makeKeyAndOrderFront:nil];
  [NSApp activateIgnoringOtherApps:YES];

  self.displayLink = [[CAMetalDisplayLink alloc] initWithMetalLayer:layer];
  if (self.displayLink == nil) {
    fprintf(stderr, "ERROR could not create CAMetalDisplayLink\n");
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }
  self.displayLink.delegate = self;
  startTicks_ = mach_absolute_time();
  [self.displayLink addToRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
  self.stopTimer = [NSTimer scheduledTimerWithTimeInterval:15.0
                                                    target:self
                                                  selector:@selector(finishMeasurement:)
                                                  userInfo:nil
                                                   repeats:NO];
}

- (void)metalDisplayLink:(CAMetalDisplayLink *)link needsUpdate:(CAMetalDisplayLinkUpdate *)update {
  const Sample sample{mach_absolute_time(), CACurrentMediaTime(),
                      update.targetTimestamp, update.targetPresentationTimestamp};
  bool storage_failed = false;
  {
    std::lock_guard<std::mutex> lock(samples_mutex_);
    if (!accepting_samples_) {
      return;
    }
    try {
      samples_.push_back(sample);
    } catch (...) {
      accepting_samples_ = false;
      storage_failed = true;
    }
  }
  if (storage_failed) {
    fprintf(stderr, "ERROR could not store CAMetalDisplayLink sample\n");
    g_probe_exit_code.store(1);
    [link invalidate];
    if ([NSThread isMainThread]) {
      [NSApp terminate:nil];
    } else {
      [NSApp performSelectorOnMainThread:@selector(terminate:) withObject:nil waitUntilDone:NO];
    }
  }
}

- (void)finishMeasurement:(NSTimer *)timer {
  (void)timer;
  [self.stopTimer invalidate];
  self.stopTimer = nil;
  [self.displayLink invalidate];
  std::vector<Sample> samples;
  {
    std::lock_guard<std::mutex> lock(samples_mutex_);
    accepting_samples_ = false;
    samples.swap(samples_);
  }
  uint64_t endTicks = mach_absolute_time();
  double scale = (double)timebase_.numer / (double)timebase_.denom;
  double elapsed = ((double)(endTicks - startTicks_) * scale) / 1.0e9;

  std::ofstream out(self.outputPath.fileSystemRepresentation, std::ios::out | std::ios::trunc);
  if (!out) {
    fprintf(stderr, "ERROR could not create probe CSV: %s\n",
            self.finalOutputPath.fileSystemRepresentation);
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }
  out << "callback_host_ns_since_mach_epoch,ca_now_seconds,target_seconds,target_presentation_seconds,target_minus_callback_ms,presentation_minus_callback_ms\n";
  out << std::fixed << std::setprecision(9);
  for (const Sample &s : samples) {
    double hostNs = (double)s.hostTicks * scale;
    out << std::setprecision(0) << hostNs << ','
        << std::setprecision(9) << s.caNow << ',' << s.target << ',' << s.targetPresentation << ','
        << ((s.target - s.caNow) * 1000.0) << ','
        << ((s.targetPresentation - s.caNow) * 1000.0) << '\n';
  }
  out.close();
  if (!out) {
    fprintf(stderr, "ERROR writing probe CSV failed: %s\n",
            self.finalOutputPath.fileSystemRepresentation);
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }
  if (samples.empty()) {
    fprintf(stderr, "ERROR CAMetalDisplayLink delivered no samples\n");
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }

  // Publish only the complete CSV, without ever replacing a path created by
  // another process after the initial existence check.
  if (link(self.outputPath.fileSystemRepresentation,
           self.finalOutputPath.fileSystemRepresentation) != 0) {
    fprintf(stderr, "ERROR could not publish probe CSV %s: %s\n",
            self.finalOutputPath.fileSystemRepresentation, strerror(errno));
    g_probe_exit_code.store(1);
    [NSApp terminate:nil];
    return;
  }

  CGDisplayModeRef mode = CGDisplayCopyDisplayMode(CGMainDisplayID());
  double cgHz = mode ? CGDisplayModeGetRefreshRate(mode) : 0.0;
  if (mode) CFRelease(mode);
  fprintf(stdout, "probe_result path=%s samples=%zu elapsed_seconds=%.6f main_screen_max_fps=%ld cg_mode_refresh_hz=%.3f mach_timebase_numer=%u mach_timebase_denom=%u\n",
          self.finalOutputPath.fileSystemRepresentation, samples.size(), elapsed,
          (long)NSScreen.mainScreen.maximumFramesPerSecond, cgHz,
          timebase_.numer, timebase_.denom);
  fflush(stdout);
  [NSApp terminate:nil];
}
@end

int main(int argc, const char * argv[]) {
  @autoreleasepool {
    if (argc != 2) {
      fprintf(stderr, "usage: %s OUTPUT_CSV\n", argv[0]);
      return 2;
    }
    const std::filesystem::path output_path(argv[1]);
    std::error_code path_error;
    const auto output_status = std::filesystem::symlink_status(output_path, path_error);
    if ((path_error && path_error != std::errc::no_such_file_or_directory) ||
        (!path_error && output_status.type() != std::filesystem::file_type::not_found)) {
      fprintf(stderr, "ERROR refusing to overwrite existing probe output: %s\n", argv[1]);
      return 2;
    }
    std::filesystem::path output_parent = output_path.parent_path();
    if (output_parent.empty()) {
      output_parent = ".";
    }
    std::string temporary_pattern =
        (output_parent / ".cametal-displaylink.XXXXXX").string();
    std::vector<char> temporary_template(temporary_pattern.begin(), temporary_pattern.end());
    temporary_template.push_back('\0');
    const int temporary_fd = mkstemp(temporary_template.data());
    if (temporary_fd == -1) {
      fprintf(stderr, "ERROR could not reserve a temporary CSV beside %s: %s\n",
              argv[1], strerror(errno));
      return 1;
    }
    const mode_t old_umask = umask(0);
    umask(old_umask);
    const int chmod_status = fchmod(
        temporary_fd, static_cast<mode_t>(0666 & ~old_umask));
    const int chmod_errno = errno;
    const int close_status = close(temporary_fd);
    const int close_errno = errno;
    if (chmod_status != 0 || close_status != 0) {
      const int saved_errno = chmod_status != 0 ? chmod_errno : close_errno;
      unlink(temporary_template.data());
      fprintf(stderr, "ERROR could not prepare temporary CSV for %s: %s\n",
              argv[1], strerror(saved_errno));
      return 1;
    }
    NSString *temporary_path =
        [NSString stringWithUTF8String:temporary_template.data()];
    NSString *final_path = [NSString stringWithUTF8String:argv[1]];
    if (temporary_path == nil || final_path == nil) {
      unlink(temporary_template.data());
      fprintf(stderr, "ERROR output path is not valid UTF-8\n");
      return 2;
    }
    [NSApplication sharedApplication];
    NSApp.activationPolicy = NSApplicationActivationPolicyRegular;
    ProbeController *controller = [[ProbeController alloc]
        initWithTemporaryOutputPath:temporary_path finalOutputPath:final_path];
    if (controller == nil) {
      unlink(temporary_template.data());
      return g_probe_exit_code.load();
    }
    NSApp.delegate = controller;
    [NSApp run];
    unlink(temporary_template.data());
  }
  return g_probe_exit_code.load();
}
