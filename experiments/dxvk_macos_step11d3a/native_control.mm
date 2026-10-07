#define VK_USE_PLATFORM_METAL_EXT

#import <AppKit/AppKit.h>
#import <CoreGraphics/CoreGraphics.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#include <dispatch/dispatch.h>
#include <dlfcn.h>

#include <vulkan/vulkan.h>
#include <vulkan/vulkan_metal.h>
#include "native_trace_api.h"

#include <fcntl.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <sys/sysctl.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <iterator>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

constexpr uint32_t kDrawableWidth = 640;
constexpr uint32_t kDrawableHeight = 360;
constexpr uint32_t kSwapchainImageCount = 3;
constexpr uint32_t kFramesInFlight = 3;
constexpr uint64_t kRunSecondsDefault = 30;
constexpr uint64_t kMaximumRunSeconds = 3600;
constexpr const char *kDefaultTracePath = "step11d3a-trace.jsonl";
constexpr const char *kPortabilitySubsetExtensionName =
    "VK_KHR_portability_subset";

uint64_t MachAbsoluteNanoseconds() {
  static const mach_timebase_info_data_t timebase = [] {
    mach_timebase_info_data_t value{};
    (void)mach_timebase_info(&value);
    return value;
  }();
  if (timebase.denom == 0) {
    return 0;
  }
  const long double ticks = static_cast<long double>(mach_absolute_time());
  const long double nanoseconds =
      ticks * static_cast<long double>(timebase.numer) /
      static_cast<long double>(timebase.denom);
  return static_cast<uint64_t>(nanoseconds);
}

std::string JsonEscape(const std::string &value) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string escaped;
  escaped.reserve(value.size() + 8);
  for (unsigned char byte : value) {
    switch (byte) {
      case '"': escaped += "\\\""; break;
      case '\\': escaped += "\\\\"; break;
      case '\b': escaped += "\\b"; break;
      case '\f': escaped += "\\f"; break;
      case '\n': escaped += "\\n"; break;
      case '\r': escaped += "\\r"; break;
      case '\t': escaped += "\\t"; break;
      default:
        if (byte < 0x20) {
          escaped += "\\u00";
          escaped += kHex[(byte >> 4) & 0xf];
          escaped += kHex[byte & 0xf];
        } else {
          escaped += static_cast<char>(byte);
        }
    }
  }
  return escaped;
}

uint64_t CurrentThreadId();

const char *CompiledProcessArchitecture() {
#if defined(__x86_64__)
  return "x86_64";
#elif defined(__aarch64__) || defined(__arm64__)
  return "arm64";
#else
  return "unknown";
#endif
}

int ProcessTranslationStatus() {
  int translated = -1;
  size_t size = sizeof(translated);
  if (sysctlbyname("sysctl.proc_translated", &translated, &size, nullptr, 0) != 0 ||
      size != sizeof(translated)) {
    return -1;
  }
  return translated;
}

constexpr size_t kTraceLineCapacity = 2048;
struct TraceRecord {
  uint32_t len = 0;
  char data[kTraceLineCapacity]{};
};

class JsonObject {
 public:
  explicit JsonObject(const char *event) {
    append_char('{');
    string("origin", "APP_SOURCE");
    eventId_ = mvkNativeTraceBeginEvent();
    admitted_ = eventId_ != 0;
    number("event_id", eventId_);
    string("event", event);
    number("ts_mach_ns", MachAbsoluteNanoseconds());
    number("tid", CurrentThreadId());
  }

  JsonObject(const JsonObject &) = delete;
  JsonObject &operator=(const JsonObject &) = delete;
  JsonObject(JsonObject &&other) noexcept
      : eventId_(other.eventId_), admitted_(other.admitted_), length_(other.length_),
        first_(other.first_), valid_(other.valid_), finished_(other.finished_) {
    std::memcpy(value_, other.value_, sizeof(value_));
    other.admitted_ = false;
  }

  ~JsonObject() {
    if (admitted_) {
      mvkNativeTraceRecordSerializationFailure();
      mvkNativeTraceFinishEvent();
    }
  }

  bool admitted() const { return admitted_; }
  void releaseAdmission() { admitted_ = false; }

  void string(const char *key, const std::string &value) { string(key, value.c_str()); }

  void string(const char *key, const char *value) {
    prefix(key);
    append_char('"');
    const char *text = value == nullptr ? "" : value;
    static constexpr char hex[] = "0123456789abcdef";
    for (const unsigned char *cursor = reinterpret_cast<const unsigned char *>(text);
         *cursor != 0; ++cursor) {
      switch (*cursor) {
        case '"': append_bytes("\\\"", 2); break;
        case '\\': append_bytes("\\\\", 2); break;
        case '\b': append_bytes("\\b", 2); break;
        case '\f': append_bytes("\\f", 2); break;
        case '\n': append_bytes("\\n", 2); break;
        case '\r': append_bytes("\\r", 2); break;
        case '\t': append_bytes("\\t", 2); break;
        default:
          if (*cursor < 0x20) {
            char escaped[] = {'\\', 'u', '0', '0', hex[*cursor >> 4], hex[*cursor & 0x0f]};
            append_bytes(escaped, sizeof(escaped));
          } else {
            append_char(static_cast<char>(*cursor));
          }
      }
    }
    append_char('"');
  }

  template <typename Number>
  void number(const char *key, Number value) {
    prefix(key);
    char buffer[64]{};
    int length = 0;
    if constexpr (std::is_floating_point_v<Number>) {
      if (!std::isfinite(value)) {
        append_bytes("null", 4);
        return;
      }
      length = std::snprintf(buffer, sizeof(buffer), "%.9g", static_cast<double>(value));
    } else if constexpr (std::is_signed_v<Number>) {
      length = std::snprintf(buffer, sizeof(buffer), "%lld", static_cast<long long>(value));
    } else {
      length = std::snprintf(buffer, sizeof(buffer), "%llu", static_cast<unsigned long long>(value));
    }
    if (length <= 0 || static_cast<size_t>(length) >= sizeof(buffer)) {
      valid_ = false;
      return;
    }
    append_bytes(buffer, static_cast<size_t>(length));
  }

  void boolean(const char *key, bool value) {
    prefix(key);
    append_bytes(value ? "true" : "false", value ? 4 : 5);
  }

  void null(const char *key) {
    prefix(key);
    append_bytes("null", 4);
  }

  void raw(const char *key, const std::string &json) { raw(key, json.c_str()); }

  void raw(const char *key, const char *json) {
    prefix(key);
    append_bytes(json == nullptr ? "null" : json,
                 json == nullptr ? 4 : std::strlen(json));
  }

  bool finish(TraceRecord &record) {
    if (!valid_ || finished_ || length_ + 2 > sizeof(value_)) return false;
    append_char('}');
    append_char('\n');
    finished_ = true;
    if (!valid_ || length_ > sizeof(record.data)) return false;
    record.len = static_cast<uint32_t>(length_);
    std::memcpy(record.data, value_, length_);
    return true;
  }

 private:
  void prefix(const char *key) {
    if (!first_) append_char(',');
    first_ = false;
    append_char('"');
    append_bytes(key, std::strlen(key));
    append_bytes("\":", 2);
  }

  void append_char(char value) {
    if (length_ >= sizeof(value_)) {
      valid_ = false;
      return;
    }
    value_[length_++] = value;
  }

  void append_bytes(const char *value, size_t length) {
    if (length > sizeof(value_) - length_) {
      valid_ = false;
      return;
    }
    std::memcpy(value_ + length_, value, length);
    length_ += length;
  }

  char value_[kTraceLineCapacity]{};
  uint64_t eventId_ = 0;
  bool admitted_ = false;
  size_t length_ = 0;
  bool first_ = true;
  bool valid_ = true;
  bool finished_ = false;
};

class Trace {
 public:
  explicit Trace(const char *path) : path_(path == nullptr ? "" : path) {
    open_.store(!path_.empty() && mvkNativeTraceStart(path_.c_str()),
                std::memory_order_release);
  }

  ~Trace() { shutdown(); }

  void shutdown() {
    bool expected = true;
    if (!open_.compare_exchange_strong(expected, false, std::memory_order_acq_rel)) return;
    JsonObject event("trace_sink_stop_request");
    event.number("app_format_failure_count", formatFailures_.load(std::memory_order_relaxed));
    event.number("logger_drop_count_before_stop", mvkNativeTraceDroppedCount());
    event.number("logger_write_failure_count_before_stop", mvkNativeTraceWriteFailureCount());
    TraceRecord record{};
    if (event.admitted() && event.finish(record)) {
      event.releaseAdmission();
      (void)mvkNativeTraceEnqueueJson(record.data, record.len);
    } else {
      formatFailures_.fetch_add(1, std::memory_order_relaxed);
      if (event.admitted()) {
        event.releaseAdmission();
        mvkNativeTraceRecordSerializationFailure();
        mvkNativeTraceFinishEvent();
      }
    }
    finalHealthy_.store(mvkNativeTraceStop(), std::memory_order_release);
  }

  bool isOpen() const { return open_.load(std::memory_order_acquire); }
  bool finalHealthy() const { return finalHealthy_.load(std::memory_order_acquire); }
  bool healthy() const {
    return isOpen() && mvkNativeTraceIsHealthy();
  }
  const std::string &path() const { return path_; }

  void emit(JsonObject &&object) {
    if (!isOpen() || !object.admitted()) return;
    TraceRecord record{};
    if (!object.finish(record)) {
      formatFailures_.fetch_add(1, std::memory_order_relaxed);
      object.releaseAdmission();
      mvkNativeTraceRecordSerializationFailure();
      mvkNativeTraceFinishEvent();
      return;
    }
    object.releaseAdmission();
    (void)mvkNativeTraceEnqueueJson(record.data, record.len);
  }

 private:
  std::string path_;
  std::atomic<bool> open_{false};
  std::atomic<bool> finalHealthy_{false};
  std::atomic<uint64_t> formatFailures_{0};
};

std::string NSStringUtf8(NSString *value) {
  if (value == nil) {
    return {};
  }
  const char *utf8 = [value UTF8String];
  return utf8 == nullptr ? std::string{} : std::string(utf8);
}

template <typename Handle>
uintptr_t HandleValue(Handle handle) {
  if constexpr (std::is_pointer_v<Handle>) {
    return reinterpret_cast<uintptr_t>(handle);
  } else {
    return static_cast<uintptr_t>(handle);
  }
}

template <typename Handle>
std::string HandleString(Handle handle) {
  uintptr_t value = 0;
  if constexpr (std::is_pointer_v<Handle>) {
    value = reinterpret_cast<uintptr_t>(handle);
  } else {
    value = static_cast<uintptr_t>(handle);
  }
  char buffer[2 + sizeof(uintptr_t) * 2 + 1]{};
  std::snprintf(buffer, sizeof(buffer), "0x%llx",
                static_cast<unsigned long long>(value));
  return buffer;
}

uint64_t CurrentThreadId() {
  uint64_t threadId = 0;
  (void)pthread_threadid_np(nullptr, &threadId);
  return threadId;
}

const char *VkResultName(VkResult result) {
  switch (result) {
    case VK_SUCCESS: return "VK_SUCCESS";
    case VK_NOT_READY: return "VK_NOT_READY";
    case VK_TIMEOUT: return "VK_TIMEOUT";
    case VK_EVENT_SET: return "VK_EVENT_SET";
    case VK_EVENT_RESET: return "VK_EVENT_RESET";
    case VK_INCOMPLETE: return "VK_INCOMPLETE";
    case VK_ERROR_OUT_OF_HOST_MEMORY: return "VK_ERROR_OUT_OF_HOST_MEMORY";
    case VK_ERROR_OUT_OF_DEVICE_MEMORY: return "VK_ERROR_OUT_OF_DEVICE_MEMORY";
    case VK_ERROR_INITIALIZATION_FAILED: return "VK_ERROR_INITIALIZATION_FAILED";
    case VK_ERROR_DEVICE_LOST: return "VK_ERROR_DEVICE_LOST";
    case VK_ERROR_MEMORY_MAP_FAILED: return "VK_ERROR_MEMORY_MAP_FAILED";
    case VK_ERROR_LAYER_NOT_PRESENT: return "VK_ERROR_LAYER_NOT_PRESENT";
    case VK_ERROR_EXTENSION_NOT_PRESENT: return "VK_ERROR_EXTENSION_NOT_PRESENT";
    case VK_ERROR_FEATURE_NOT_PRESENT: return "VK_ERROR_FEATURE_NOT_PRESENT";
    case VK_ERROR_INCOMPATIBLE_DRIVER: return "VK_ERROR_INCOMPATIBLE_DRIVER";
    case VK_ERROR_TOO_MANY_OBJECTS: return "VK_ERROR_TOO_MANY_OBJECTS";
    case VK_ERROR_FORMAT_NOT_SUPPORTED: return "VK_ERROR_FORMAT_NOT_SUPPORTED";
    case VK_ERROR_SURFACE_LOST_KHR: return "VK_ERROR_SURFACE_LOST_KHR";
    case VK_ERROR_NATIVE_WINDOW_IN_USE_KHR: return "VK_ERROR_NATIVE_WINDOW_IN_USE_KHR";
    case VK_SUBOPTIMAL_KHR: return "VK_SUBOPTIMAL_KHR";
    case VK_ERROR_OUT_OF_DATE_KHR: return "VK_ERROR_OUT_OF_DATE_KHR";
    default: return "VK_RESULT_OTHER";
  }
}

bool VkSucceeded(VkResult result) {
  return result == VK_SUCCESS || result == VK_SUBOPTIMAL_KHR;
}

bool HasExtension(const std::vector<VkExtensionProperties> &extensions,
                  const char *name) {
  return std::any_of(extensions.begin(), extensions.end(),
                     [name](const VkExtensionProperties &extension) {
    return std::strcmp(extension.extensionName, name) == 0;
  });
}

std::vector<std::string> ExtensionNames(
    const std::vector<VkExtensionProperties> &extensions) {
  std::vector<std::string> names;
  names.reserve(extensions.size());
  for (const auto &extension : extensions) {
    names.emplace_back(extension.extensionName);
  }
  return names;
}

std::string JsonStringArray(const std::vector<std::string> &values) {
  std::string json = "[";
  for (size_t index = 0; index < values.size(); ++index) {
    if (index != 0) {
      json += ',';
    }
    json += "\"" + JsonEscape(values[index]) + "\"";
  }
  json += ']';
  return json;
}

std::string DisplayUuid(CGDirectDisplayID displayId) {
  CFUUIDRef uuid = CGDisplayCreateUUIDFromDisplayID(displayId);
  if (uuid == nullptr) {
    return {};
  }
  CFStringRef value = CFUUIDCreateString(kCFAllocatorDefault, uuid);
  CFRelease(uuid);
  if (value == nullptr) {
    return {};
  }
  char buffer[128]{};
  const bool converted = CFStringGetCString(value, buffer, sizeof(buffer),
                                            kCFStringEncodingUTF8);
  CFRelease(value);
  return converted ? std::string(buffer) : std::string{};
}

struct DisplayFacts {
  uint32_t displayId = 0;
  std::string uuid;
  std::string name;
  uint32_t pixelWidth = 0;
  uint32_t pixelHeight = 0;
  double nominalRefreshHz = 0.0;
  double maximumFramesPerSecond = 0.0;
  double backingScale = 0.0;
  uint32_t modeId = 0;
};

DisplayFacts ReadDisplayFacts(NSScreen *screen) {
  DisplayFacts facts;
  if (screen == nil) {
    screen = [NSScreen mainScreen];
  }
  facts.name = NSStringUtf8(screen.localizedName);
  facts.backingScale = screen.backingScaleFactor;
  if ([screen respondsToSelector:@selector(maximumFramesPerSecond)]) {
    facts.maximumFramesPerSecond = screen.maximumFramesPerSecond;
  }
  NSNumber *number = screen.deviceDescription[@"NSScreenNumber"];
  const CGDirectDisplayID displayId = number == nil
      ? CGMainDisplayID()
      : static_cast<CGDirectDisplayID>(number.unsignedIntValue);
  facts.displayId = displayId;
  facts.uuid = DisplayUuid(displayId);

  CGDisplayModeRef mode = CGDisplayCopyDisplayMode(displayId);
  if (mode != nullptr) {
    facts.pixelWidth = static_cast<uint32_t>(CGDisplayModeGetPixelWidth(mode));
    facts.pixelHeight = static_cast<uint32_t>(CGDisplayModeGetPixelHeight(mode));
    facts.nominalRefreshHz = CGDisplayModeGetRefreshRate(mode);
    facts.modeId = CGDisplayModeGetIODisplayModeID(mode);
    CGDisplayModeRelease(mode);
  }
  return facts;
}

std::string QueueJson(VkQueue queue, uint32_t familyIndex, uint32_t queueIndex) {
  return std::string("{\"handle\":\"") + JsonEscape(HandleString(queue)) +
      "\",\"family_index\":" + std::to_string(familyIndex) +
      ",\"queue_index\":" + std::to_string(queueIndex) + "}";
}

struct FrameSync {
  VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
  VkSemaphore imageAvailable = VK_NULL_HANDLE;
  VkFence fence = VK_NULL_HANDLE;
};

struct FrameIdentity {
  uint64_t frameId = 0;
  uint32_t imageIndex = 0;
  uintptr_t imageHandle = 0;
};

class Renderer {
 public:
  Renderer(Trace &trace, CAMetalLayer *layer, uint64_t seconds)
      : trace_(trace), layer_(layer), seconds_(seconds) {}

  ~Renderer() {
    requestStop();
    join();
  }

  void start(CAMetalLayer *layer) {
    layer_ = layer;
    worker_ = std::thread([this] { run(); });
  }

  void requestStop(const char *reason = "external_request") {
    const bool wasRunning = stopRequested_.exchange(true, std::memory_order_relaxed);
    if (!wasRunning) {
      JsonObject event("stop_requested");
      event.string("reason", reason);
      trace_.emit(std::move(event));
    }
  }

  void join() {
    if (worker_.joinable()) {
      worker_.join();
    }
  }

  int result() const { return result_.load(std::memory_order_relaxed); }
  bool finished() const { return finished_.load(std::memory_order_relaxed); }

 private:
  struct SelectedDevice {
    VkPhysicalDevice handle = VK_NULL_HANDLE;
    VkPhysicalDeviceProperties properties{};
    VkPhysicalDeviceDriverProperties driver{};
    uint32_t queueFamilyIndex = UINT32_MAX;
    std::vector<VkExtensionProperties> extensions;
    bool googleDisplayTiming = false;
    bool portabilitySubset = false;
  };

  void run() {
    JsonObject workerEvent("renderer_thread_started");
    workerEvent.number("thread_id", CurrentThreadId());
    workerEvent.number("requested_seconds", seconds_);
    workerEvent.string("trace_path", trace_.path());
    trace_.emit(std::move(workerEvent));

    bool initialized = initialize();
    const uint64_t runStart = MachAbsoluteNanoseconds();
    if (initialized && !stopRequested_.load(std::memory_order_relaxed)) {
      JsonObject start("run_start");
      start.number("run_start_mach_ns", runStart);
      start.number("requested_seconds", seconds_);
      start.string("duration_clock", "mach_absolute_time converted with mach_timebase_info");
      trace_.emit(std::move(start));
      runFrames(runStart + seconds_ * 1000000000ULL);
      requestStop("frame_loop_completed");
    }

    if (initialized) {
      drainPresentationTiming();
    }
    cleanup();

    const uint64_t callbackDrainDeadline = MachAbsoluteNanoseconds() + 3000000000ULL;
    uint64_t pendingPresentationCallbacks =
        mvkNativeTracePendingPresentedCallbacks();
    while (pendingPresentationCallbacks != 0 &&
           MachAbsoluteNanoseconds() < callbackDrainDeadline) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
      pendingPresentationCallbacks = mvkNativeTracePendingPresentedCallbacks();
    }
    JsonObject callbackDrain("presentation_callback_drain");
    callbackDrain.number("pending_callbacks", pendingPresentationCallbacks);
    callbackDrain.boolean("complete", pendingPresentationCallbacks == 0);
    trace_.emit(std::move(callbackDrain));
    if (pendingPresentationCallbacks != 0 && result() == 0) {
      result_.store(1, std::memory_order_relaxed);
    }

    JsonObject summary("run_end");
    summary.number("run_end_mach_ns", MachAbsoluteNanoseconds());
    summary.number("app_acquire_calls", appAcquireSequence_);
    summary.number("submitted_frames", submittedFrames_);
    summary.number("present_calls", presentCalls_);
    summary.number("successful_present_calls", successfulPresents_);
    summary.number("presentation_completion_records", completionRecords_);
    summary.boolean("presentation_timing_extension_available",
                    getPastPresentationTiming_ != nullptr);
    summary.boolean("trace_healthy", trace_.healthy());
    summary.number("exit_code", result());
    trace_.emit(std::move(summary));

    finished_.store(true, std::memory_order_relaxed);
    dispatch_async(dispatch_get_main_queue(), ^{
      [NSApp terminate:nil];
    });
  }

  bool initialize() {
    if (!createInstanceAndSurface()) {
      return false;
    }
    if (!selectDeviceAndCreateLogicalDevice()) {
      return false;
    }
    if (!createSwapchain()) {
      return false;
    }
    if (!createRendererResources()) {
      return false;
    }
    return true;
  }

  bool createInstanceAndSurface() {
    uint32_t extensionCount = 0;
    VkResult result = vkEnumerateInstanceExtensionProperties(
        nullptr, &extensionCount, nullptr);
    if (result != VK_SUCCESS) {
      return fail("enumerate_instance_extensions_count", result);
    }
    std::vector<VkExtensionProperties> extensions(extensionCount);
    result = vkEnumerateInstanceExtensionProperties(
        nullptr, &extensionCount, extensions.data());
    if (result != VK_SUCCESS) {
      return fail("enumerate_instance_extensions", result);
    }
    if (!HasExtension(extensions, VK_KHR_SURFACE_EXTENSION_NAME) ||
        !HasExtension(extensions, VK_EXT_METAL_SURFACE_EXTENSION_NAME)) {
      JsonObject event("required_instance_extension_missing");
      event.raw("available_extensions", JsonStringArray(ExtensionNames(extensions)));
      event.boolean("has_khr_surface",
                    HasExtension(extensions, VK_KHR_SURFACE_EXTENSION_NAME));
      event.boolean("has_ext_metal_surface",
                    HasExtension(extensions, VK_EXT_METAL_SURFACE_EXTENSION_NAME));
      trace_.emit(std::move(event));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }

    const char *instanceExtensions[] = {
        VK_KHR_SURFACE_EXTENSION_NAME,
        VK_EXT_METAL_SURFACE_EXTENSION_NAME};
    VkApplicationInfo applicationInfo{};
    applicationInfo.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    applicationInfo.pApplicationName = "FG-Metal Step 11D.3-A native control";
    applicationInfo.applicationVersion = VK_MAKE_API_VERSION(0, 1, 0, 0);
    applicationInfo.pEngineName = "none";
    applicationInfo.engineVersion = VK_MAKE_API_VERSION(0, 0, 0, 0);
    applicationInfo.apiVersion = VK_API_VERSION_1_3;

    VkInstanceCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    createInfo.pApplicationInfo = &applicationInfo;
    createInfo.enabledExtensionCount = 2;
    createInfo.ppEnabledExtensionNames = instanceExtensions;
    VkResult createResult = vkCreateInstance(&createInfo, nullptr, &instance_);
    if (createResult != VK_SUCCESS) {
      return fail("vkCreateInstance", createResult);
    }

    auto createMetalSurface = reinterpret_cast<PFN_vkCreateMetalSurfaceEXT>(
        vkGetInstanceProcAddr(instance_, "vkCreateMetalSurfaceEXT"));
    if (createMetalSurface == nullptr) {
      return fail("vkGetInstanceProcAddr(vkCreateMetalSurfaceEXT)",
                  VK_ERROR_EXTENSION_NOT_PRESENT);
    }
    VkMetalSurfaceCreateInfoEXT metalSurfaceInfo{};
    metalSurfaceInfo.sType = VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT;
    metalSurfaceInfo.pLayer = layer_;
    createResult = createMetalSurface(instance_, &metalSurfaceInfo, nullptr,
                                      &surface_);
    if (createResult != VK_SUCCESS) {
      return fail("vkCreateMetalSurfaceEXT", createResult);
    }

    JsonObject event("vulkan_instance_surface");
    event.string("instance", HandleString(instance_));
    event.string("surface", HandleString(surface_));
    event.number("requested_api_version", VK_API_VERSION_1_3);
    event.raw("enabled_instance_extensions",
              "[\"VK_KHR_surface\",\"VK_EXT_metal_surface\"]");
    trace_.emit(std::move(event));
    return true;
  }

  bool selectDeviceAndCreateLogicalDevice() {
    uint32_t count = 0;
    VkResult result = vkEnumeratePhysicalDevices(instance_, &count, nullptr);
    if (result != VK_SUCCESS || count == 0) {
      return fail("vkEnumeratePhysicalDevices_count",
                  result == VK_SUCCESS ? VK_ERROR_INITIALIZATION_FAILED : result);
    }
    std::vector<VkPhysicalDevice> physicalDevices(count);
    result = vkEnumeratePhysicalDevices(instance_, &count, physicalDevices.data());
    if (result != VK_SUCCESS) {
      return fail("vkEnumeratePhysicalDevices", result);
    }

    bool found = false;
    for (VkPhysicalDevice candidate : physicalDevices) {
      VkPhysicalDeviceProperties2 properties{};
      properties.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
      VkPhysicalDeviceDriverProperties driver{};
      driver.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES;
      properties.pNext = &driver;
      vkGetPhysicalDeviceProperties2(candidate, &properties);
      if (properties.properties.apiVersion < VK_API_VERSION_1_3) {
        continue;
      }

      uint32_t extensionCount = 0;
      result = vkEnumerateDeviceExtensionProperties(candidate, nullptr,
                                                    &extensionCount, nullptr);
      if (result != VK_SUCCESS) {
        continue;
      }
      std::vector<VkExtensionProperties> deviceExtensions(extensionCount);
      result = vkEnumerateDeviceExtensionProperties(candidate, nullptr,
                                                    &extensionCount,
                                                    deviceExtensions.data());
      if (result != VK_SUCCESS ||
          !HasExtension(deviceExtensions, VK_KHR_SWAPCHAIN_EXTENSION_NAME)) {
        continue;
      }

      VkPhysicalDeviceVulkan13Features features13{};
      features13.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES;
      VkPhysicalDeviceFeatures2 features{};
      features.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
      features.pNext = &features13;
      vkGetPhysicalDeviceFeatures2(candidate, &features);
      if (features13.synchronization2 != VK_TRUE) {
        continue;
      }

      uint32_t queueCount = 0;
      vkGetPhysicalDeviceQueueFamilyProperties(candidate, &queueCount, nullptr);
      std::vector<VkQueueFamilyProperties> queues(queueCount);
      vkGetPhysicalDeviceQueueFamilyProperties(candidate, &queueCount,
                                                queues.data());
      uint32_t graphicsPresentFamily = UINT32_MAX;
      for (uint32_t index = 0; index < queueCount; ++index) {
        VkBool32 supportsPresent = VK_FALSE;
        result = vkGetPhysicalDeviceSurfaceSupportKHR(
            candidate, index, surface_, &supportsPresent);
        if (result == VK_SUCCESS && supportsPresent == VK_TRUE &&
            (queues[index].queueFlags & VK_QUEUE_GRAPHICS_BIT) != 0) {
          graphicsPresentFamily = index;
          break;
        }
      }
      if (graphicsPresentFamily == UINT32_MAX) {
        continue;
      }

      selected_.handle = candidate;
      selected_.properties = properties.properties;
      selected_.driver = driver;
      selected_.queueFamilyIndex = graphicsPresentFamily;
      selected_.extensions = std::move(deviceExtensions);
      selected_.googleDisplayTiming = HasExtension(
          selected_.extensions, VK_GOOGLE_DISPLAY_TIMING_EXTENSION_NAME);
      selected_.portabilitySubset = HasExtension(
          selected_.extensions, kPortabilitySubsetExtensionName);
      found = true;
      break;
    }

    if (!found) {
      JsonObject event("compatible_physical_device_not_found");
      event.string("required_features", "Vulkan 1.3, synchronization2, swapchain, graphics+present queue");
      trace_.emit(std::move(event));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }

    JsonObject deviceEvent("physical_device_selected");
    deviceEvent.string("device_name", selected_.properties.deviceName);
    deviceEvent.string("driver_name", selected_.driver.driverName);
    deviceEvent.string("driver_info", selected_.driver.driverInfo);
    deviceEvent.number("api_version", selected_.properties.apiVersion);
    deviceEvent.number("driver_version", selected_.properties.driverVersion);
    deviceEvent.number("vendor_id", selected_.properties.vendorID);
    deviceEvent.number("device_id", selected_.properties.deviceID);
    deviceEvent.number("queue_family_index", selected_.queueFamilyIndex);
    deviceEvent.raw("available_device_extensions",
                    JsonStringArray(ExtensionNames(selected_.extensions)));
    deviceEvent.boolean("google_display_timing_available",
                        selected_.googleDisplayTiming);
    trace_.emit(std::move(deviceEvent));

    const char *deviceExtensions[3] = {
        VK_KHR_SWAPCHAIN_EXTENSION_NAME,
        VK_GOOGLE_DISPLAY_TIMING_EXTENSION_NAME,
        kPortabilitySubsetExtensionName};
    std::vector<const char *> enabledExtensions;
    enabledExtensions.push_back(deviceExtensions[0]);
    if (selected_.googleDisplayTiming) {
      enabledExtensions.push_back(deviceExtensions[1]);
    }
    if (selected_.portabilitySubset) {
      enabledExtensions.push_back(deviceExtensions[2]);
    }

    const float priority = 1.0f;
    VkDeviceQueueCreateInfo queueInfo{};
    queueInfo.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
    queueInfo.queueFamilyIndex = selected_.queueFamilyIndex;
    queueInfo.queueCount = 1;
    queueInfo.pQueuePriorities = &priority;

    VkPhysicalDeviceVulkan13Features enabledFeatures13{};
    enabledFeatures13.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES;
    enabledFeatures13.synchronization2 = VK_TRUE;
    VkDeviceCreateInfo deviceInfo{};
    deviceInfo.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    deviceInfo.pNext = &enabledFeatures13;
    deviceInfo.queueCreateInfoCount = 1;
    deviceInfo.pQueueCreateInfos = &queueInfo;
    deviceInfo.enabledExtensionCount = static_cast<uint32_t>(enabledExtensions.size());
    deviceInfo.ppEnabledExtensionNames = enabledExtensions.data();
    result = vkCreateDevice(selected_.handle, &deviceInfo, nullptr, &device_);
    if (result != VK_SUCCESS) {
      return fail("vkCreateDevice", result);
    }
    vkGetDeviceQueue(device_, selected_.queueFamilyIndex, 0, &queue_);
    if (queue_ == VK_NULL_HANDLE) {
      return fail("vkGetDeviceQueue", VK_ERROR_INITIALIZATION_FAILED);
    }
    if (selected_.googleDisplayTiming) {
      getPastPresentationTiming_ =
          reinterpret_cast<PFN_vkGetPastPresentationTimingGOOGLE>(
              vkGetDeviceProcAddr(device_, "vkGetPastPresentationTimingGOOGLE"));
      if (getPastPresentationTiming_ == nullptr) {
        JsonObject warning("optional_presentation_timing_function_missing");
        warning.string("function", "vkGetPastPresentationTimingGOOGLE");
        trace_.emit(std::move(warning));
      }
    }

    JsonObject queueEvent("graphics_present_queue_selected");
    queueEvent.raw("queue", QueueJson(queue_, selected_.queueFamilyIndex, 0));
    queueEvent.string("submit_api", "vkQueueSubmit2");
    queueEvent.number("thread_id", CurrentThreadId());
    trace_.emit(std::move(queueEvent));
    return true;
  }

  bool createSwapchain() {
    VkResult result = vkGetPhysicalDeviceSurfaceCapabilitiesKHR(
        selected_.handle, surface_, &surfaceCapabilities_);
    if (result != VK_SUCCESS) {
      return fail("vkGetPhysicalDeviceSurfaceCapabilitiesKHR", result);
    }

    uint32_t formatCount = 0;
    result = vkGetPhysicalDeviceSurfaceFormatsKHR(
        selected_.handle, surface_, &formatCount, nullptr);
    if (result != VK_SUCCESS || formatCount == 0) {
      return fail("vkGetPhysicalDeviceSurfaceFormatsKHR_count",
                  result == VK_SUCCESS ? VK_ERROR_FORMAT_NOT_SUPPORTED : result);
    }
    std::vector<VkSurfaceFormatKHR> formats(formatCount);
    result = vkGetPhysicalDeviceSurfaceFormatsKHR(
        selected_.handle, surface_, &formatCount, formats.data());
    if (result != VK_SUCCESS) {
      return fail("vkGetPhysicalDeviceSurfaceFormatsKHR", result);
    }

    uint32_t modeCount = 0;
    result = vkGetPhysicalDeviceSurfacePresentModesKHR(
        selected_.handle, surface_, &modeCount, nullptr);
    if (result != VK_SUCCESS || modeCount == 0) {
      return fail("vkGetPhysicalDeviceSurfacePresentModesKHR_count",
                  result == VK_SUCCESS ? VK_ERROR_INITIALIZATION_FAILED : result);
    }
    std::vector<VkPresentModeKHR> presentModes(modeCount);
    result = vkGetPhysicalDeviceSurfacePresentModesKHR(
        selected_.handle, surface_, &modeCount, presentModes.data());
    if (result != VK_SUCCESS) {
      return fail("vkGetPhysicalDeviceSurfacePresentModesKHR", result);
    }

    const VkFormat requestedFormat = VK_FORMAT_B8G8R8A8_UNORM;
    const VkColorSpaceKHR requestedColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
    auto format = std::find_if(formats.begin(), formats.end(),
        [requestedFormat, requestedColorSpace](const VkSurfaceFormatKHR &value) {
          return value.format == requestedFormat &&
                 value.colorSpace == requestedColorSpace;
        });
    if (format == formats.end() && formats.size() == 1 &&
        formats.front().format == VK_FORMAT_UNDEFINED) {
      actualSurfaceFormat_ = {requestedFormat, requestedColorSpace};
    } else if (format != formats.end()) {
      actualSurfaceFormat_ = *format;
    } else {
      JsonObject event("requested_surface_format_unavailable");
      event.number("requested_vk_format", static_cast<int32_t>(requestedFormat));
      event.number("requested_color_space", static_cast<int32_t>(requestedColorSpace));
      event.number("advertised_format_count", formatCount);
      trace_.emit(std::move(event));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }

    if (std::find(presentModes.begin(), presentModes.end(),
                  VK_PRESENT_MODE_FIFO_KHR) == presentModes.end()) {
      return fail("FIFO_present_mode_unavailable", VK_ERROR_INITIALIZATION_FAILED);
    }
    const bool colorAttachmentSupported =
        (surfaceCapabilities_.supportedUsageFlags &
         VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) != 0;
    if (!colorAttachmentSupported) {
      JsonObject event("color_attachment_swapchain_usage_unavailable");
      event.number("supported_usage_flags",
                   static_cast<uint32_t>(surfaceCapabilities_.supportedUsageFlags));
      trace_.emit(std::move(event));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }

    VkExtent2D extent{kDrawableWidth, kDrawableHeight};
    if (surfaceCapabilities_.currentExtent.width != UINT32_MAX) {
      extent = surfaceCapabilities_.currentExtent;
    }
    JsonObject capsEvent("surface_capabilities");
    capsEvent.number("requested_drawable_width", kDrawableWidth);
    capsEvent.number("requested_drawable_height", kDrawableHeight);
    capsEvent.number("current_extent_width", surfaceCapabilities_.currentExtent.width);
    capsEvent.number("current_extent_height", surfaceCapabilities_.currentExtent.height);
    capsEvent.number("selected_extent_width", extent.width);
    capsEvent.number("selected_extent_height", extent.height);
    capsEvent.number("min_image_count", surfaceCapabilities_.minImageCount);
    capsEvent.number("max_image_count", surfaceCapabilities_.maxImageCount);
    capsEvent.number("supported_usage_flags",
                     static_cast<uint32_t>(surfaceCapabilities_.supportedUsageFlags));
    capsEvent.number("current_transform",
                     static_cast<int32_t>(surfaceCapabilities_.currentTransform));
    capsEvent.raw("advertised_vk_formats", SurfaceFormatsJson(formats));
    capsEvent.raw("advertised_present_modes", PresentModesJson(presentModes));
    trace_.emit(std::move(capsEvent));

    if (extent.width != kDrawableWidth || extent.height != kDrawableHeight) {
      JsonObject mismatch("drawable_extent_mismatch");
      mismatch.number("expected_width", kDrawableWidth);
      mismatch.number("expected_height", kDrawableHeight);
      mismatch.number("actual_width", extent.width);
      mismatch.number("actual_height", extent.height);
      trace_.emit(std::move(mismatch));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }
    if (surfaceCapabilities_.minImageCount > kSwapchainImageCount ||
        (surfaceCapabilities_.maxImageCount != 0 &&
         surfaceCapabilities_.maxImageCount < kSwapchainImageCount)) {
      JsonObject mismatch("swapchain_image_count_unsupported");
      mismatch.number("requested_image_count", kSwapchainImageCount);
      mismatch.number("min_image_count", surfaceCapabilities_.minImageCount);
      mismatch.number("max_image_count", surfaceCapabilities_.maxImageCount);
      trace_.emit(std::move(mismatch));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }

    VkSwapchainCreateInfoKHR createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    createInfo.surface = surface_;
    createInfo.minImageCount = kSwapchainImageCount;
    createInfo.imageFormat = actualSurfaceFormat_.format;
    createInfo.imageColorSpace = actualSurfaceFormat_.colorSpace;
    createInfo.imageExtent = extent;
    createInfo.imageArrayLayers = 1;
    createInfo.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    createInfo.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
    createInfo.preTransform = surfaceCapabilities_.currentTransform;
    createInfo.compositeAlpha = ChooseCompositeAlpha(
        surfaceCapabilities_.supportedCompositeAlpha);
    createInfo.presentMode = VK_PRESENT_MODE_FIFO_KHR;
    createInfo.clipped = VK_TRUE;
    result = vkCreateSwapchainKHR(device_, &createInfo, nullptr, &swapchain_);
    if (result != VK_SUCCESS) {
      return fail("vkCreateSwapchainKHR", result);
    }

    uint32_t imageCount = 0;
    result = vkGetSwapchainImagesKHR(device_, swapchain_, &imageCount, nullptr);
    if (result != VK_SUCCESS || imageCount == 0) {
      return fail("vkGetSwapchainImagesKHR_count",
                  result == VK_SUCCESS ? VK_ERROR_INITIALIZATION_FAILED : result);
    }
    swapchainImages_.resize(imageCount);
    result = vkGetSwapchainImagesKHR(device_, swapchain_, &imageCount,
                                     swapchainImages_.data());
    if (result != VK_SUCCESS) {
      return fail("vkGetSwapchainImagesKHR", result);
    }
    swapchainImages_.resize(imageCount);

    JsonObject config("swapchain_configuration");
    config.string("swapchain", HandleString(swapchain_));
    config.number("requested_image_count", kSwapchainImageCount);
    config.number("actual_image_count", imageCount);
    config.number("image_width", extent.width);
    config.number("image_height", extent.height);
    config.number("vk_format", static_cast<int32_t>(actualSurfaceFormat_.format));
    config.string("vk_format_name", "VK_FORMAT_B8G8R8A8_UNORM");
    config.number("color_space", static_cast<int32_t>(actualSurfaceFormat_.colorSpace));
    config.number("present_mode", static_cast<int32_t>(VK_PRESENT_MODE_FIFO_KHR));
    config.string("present_mode_name", "VK_PRESENT_MODE_FIFO_KHR");
    config.number("image_usage", static_cast<uint32_t>(createInfo.imageUsage));
    config.number("pre_transform",
                  static_cast<int32_t>(createInfo.preTransform));
    config.number("composite_alpha",
                  static_cast<int32_t>(createInfo.compositeAlpha));
    config.raw("image_handles", ImageHandlesJson(swapchainImages_));
    config.boolean("extent_matches_target",
                   extent.width == kDrawableWidth && extent.height == kDrawableHeight);
    config.boolean("image_count_matches_target",
                   imageCount == kSwapchainImageCount);
    trace_.emit(std::move(config));

    if (imageCount != kSwapchainImageCount) {
      JsonObject mismatch("actual_swapchain_image_count_mismatch");
      mismatch.number("requested_image_count", kSwapchainImageCount);
      mismatch.number("actual_image_count", imageCount);
      trace_.emit(std::move(mismatch));
      result_.store(1, std::memory_order_relaxed);
      return false;
    }
    return true;
  }

  bool createRendererResources() {
    VkAttachmentDescription attachment{};
    attachment.format = actualSurfaceFormat_.format;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    VkAttachmentReference colorReference{};
    colorReference.attachment = 0;
    colorReference.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorReference;

    VkSubpassDependency dependency{};
    dependency.srcSubpass = VK_SUBPASS_EXTERNAL;
    dependency.dstSubpass = 0;
    dependency.srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependency.dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependency.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;

    VkRenderPassCreateInfo renderPassInfo{};
    renderPassInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    renderPassInfo.attachmentCount = 1;
    renderPassInfo.pAttachments = &attachment;
    renderPassInfo.subpassCount = 1;
    renderPassInfo.pSubpasses = &subpass;
    renderPassInfo.dependencyCount = 1;
    renderPassInfo.pDependencies = &dependency;
    VkResult result = vkCreateRenderPass(device_, &renderPassInfo, nullptr,
                                         &renderPass_);
    if (result != VK_SUCCESS) {
      return fail("vkCreateRenderPass", result);
    }

    framebuffers_.resize(swapchainImages_.size(), VK_NULL_HANDLE);
    for (size_t index = 0; index < swapchainImages_.size(); ++index) {
      VkImageViewCreateInfo viewInfo{};
      viewInfo.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
      viewInfo.image = swapchainImages_[index];
      viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
      viewInfo.format = actualSurfaceFormat_.format;
      viewInfo.components = {VK_COMPONENT_SWIZZLE_IDENTITY,
                             VK_COMPONENT_SWIZZLE_IDENTITY,
                             VK_COMPONENT_SWIZZLE_IDENTITY,
                             VK_COMPONENT_SWIZZLE_IDENTITY};
      viewInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
      viewInfo.subresourceRange.baseMipLevel = 0;
      viewInfo.subresourceRange.levelCount = 1;
      viewInfo.subresourceRange.baseArrayLayer = 0;
      viewInfo.subresourceRange.layerCount = 1;
      VkImageView imageView = VK_NULL_HANDLE;
      result = vkCreateImageView(device_, &viewInfo, nullptr, &imageView);
      if (result != VK_SUCCESS) {
        return fail("vkCreateImageView", result);
      }
      imageViews_.push_back(imageView);

      VkFramebufferCreateInfo framebufferInfo{};
      framebufferInfo.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
      framebufferInfo.renderPass = renderPass_;
      framebufferInfo.attachmentCount = 1;
      framebufferInfo.pAttachments = &imageView;
      framebufferInfo.width = kDrawableWidth;
      framebufferInfo.height = kDrawableHeight;
      framebufferInfo.layers = 1;
      result = vkCreateFramebuffer(device_, &framebufferInfo, nullptr,
                                   &framebuffers_[index]);
      if (result != VK_SUCCESS) {
        return fail("vkCreateFramebuffer", result);
      }
    }

    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    poolInfo.queueFamilyIndex = selected_.queueFamilyIndex;
    result = vkCreateCommandPool(device_, &poolInfo, nullptr, &commandPool_);
    if (result != VK_SUCCESS) {
      return fail("vkCreateCommandPool", result);
    }

    VkCommandBufferAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocateInfo.commandPool = commandPool_;
    allocateInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocateInfo.commandBufferCount = kFramesInFlight;
    VkCommandBuffer commands[kFramesInFlight]{};
    result = vkAllocateCommandBuffers(device_, &allocateInfo, commands);
    if (result != VK_SUCCESS) {
      return fail("vkAllocateCommandBuffers", result);
    }

    VkSemaphoreCreateInfo semaphoreInfo{};
    semaphoreInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;
    for (uint32_t index = 0; index < kFramesInFlight; ++index) {
      frames_[index].commandBuffer = commands[index];
      result = vkCreateSemaphore(device_, &semaphoreInfo, nullptr,
                                 &frames_[index].imageAvailable);
      if (result != VK_SUCCESS) {
        return fail("vkCreateSemaphore(imageAvailable)", result);
      }
      result = vkCreateFence(device_, &fenceInfo, nullptr,
                             &frames_[index].fence);
      if (result != VK_SUCCESS) {
        return fail("vkCreateFence", result);
      }
    }

    renderFinished_.resize(swapchainImages_.size(), VK_NULL_HANDLE);
    imagesInFlight_.resize(swapchainImages_.size(), VK_NULL_HANDLE);
    for (VkSemaphore &semaphore : renderFinished_) {
      result = vkCreateSemaphore(device_, &semaphoreInfo, nullptr, &semaphore);
      if (result != VK_SUCCESS) {
        return fail("vkCreateSemaphore(renderFinished)", result);
      }
    }

    JsonObject event("renderer_resources_created");
    event.string("rendering_method", "Vulkan render pass clear attachment");
    event.number("frame_resources", kFramesInFlight);
    event.boolean("swapchain_transfer_usage", false);
    event.boolean("cpu_readback", false);
    event.boolean("screen_capture", false);
    trace_.emit(std::move(event));
    return true;
  }

  void runFrames(uint64_t deadlineNs) {
    static constexpr uint64_t kFenceTimeoutNs = 5000000000ULL;  // 5.0 seconds
    while (!stopRequested_.load(std::memory_order_relaxed) &&
           MachAbsoluteNanoseconds() < deadlineNs && trace_.healthy()) {
      const uint32_t frameSlot = static_cast<uint32_t>(
          appAcquireSequence_ % kFramesInFlight);
      FrameSync &frame = frames_[frameSlot];
      const uint64_t fenceWaitBegin = MachAbsoluteNanoseconds();
      VkResult result = vkWaitForFences(device_, 1, &frame.fence, VK_TRUE,
                                        kFenceTimeoutNs);
      const uint64_t fenceWaitEnd = MachAbsoluteNanoseconds();
      JsonObject fenceWait("frame_fence_wait");
      fenceWait.number("frame_slot", frameSlot);
      fenceWait.number("fence_id", HandleValue(frame.fence));
      fenceWait.number("begin_mach_ns", fenceWaitBegin);
      fenceWait.number("end_mach_ns", fenceWaitEnd);
      fenceWait.number("duration_ns", fenceWaitEnd - fenceWaitBegin);
      fenceWait.number("vk_result", static_cast<int32_t>(result));
      fenceWait.string("vk_result_name", VkResultName(result));
      trace_.emit(std::move(fenceWait));
      if (result != VK_SUCCESS) {
        if (result == VK_TIMEOUT) {
          JsonObject timeoutEvent("wait_timeout");
          timeoutEvent.string("operation", "vkWaitForFences(frame)");
          timeoutEvent.number("frame_slot", frameSlot);
          trace_.emit(std::move(timeoutEvent));
        }
        (void)fail("vkWaitForFences(frame)", result);
        break;
      }

      const uint64_t appAcquireSequence = ++appAcquireSequence_;
      const uint64_t frameId = ++frameSequence_;
      mvkNativeTraceSetCurrentAppFrameID(frameId);
      JsonObject acquireStart("acquire_begin");
      acquireStart.number("app_acquire_sequence", appAcquireSequence);
      acquireStart.number("app_frame_id", frameId);
      acquireStart.number("frame_slot", frameSlot);
      acquireStart.number("frame_id", frameId);
      acquireStart.null("image_index");
      acquireStart.null("image_id");
      acquireStart.number("swapchain_id", HandleValue(swapchain_));
      acquireStart.number("thread_id", CurrentThreadId());
      trace_.emit(std::move(acquireStart));

      const uint64_t acquireBegin = MachAbsoluteNanoseconds();
      uint32_t imageIndex = UINT32_MAX;
      result = vkAcquireNextImageKHR(device_, swapchain_,
          kFenceTimeoutNs, frame.imageAvailable,
          VK_NULL_HANDLE, &imageIndex);
      mvkNativeTraceSetCurrentAppFrameID(0);
      const uint64_t acquireEnd = MachAbsoluteNanoseconds();
      const bool acquired = VkSucceeded(result);
      const bool validImage = acquired && imageIndex < swapchainImages_.size();
      JsonObject acquireDone("acquire_end");
      acquireDone.number("app_acquire_sequence", appAcquireSequence);
      acquireDone.number("app_frame_id", frameId);
      if (validImage) {
        acquireDone.number("frame_id", frameId);
        acquireDone.number("image_index", imageIndex);
        acquireDone.number("image_id", HandleValue(swapchainImages_[imageIndex]));
      } else {
        acquireDone.null("frame_id");
        acquireDone.null("image_index");
        acquireDone.null("image_id");
      }
      acquireDone.number("swapchain_id", HandleValue(swapchain_));
      acquireDone.number("call_begin_mach_ns", acquireBegin);
      acquireDone.number("call_end_mach_ns", acquireEnd);
      acquireDone.number("duration_ns", acquireEnd - acquireBegin);
      acquireDone.number("vk_result", static_cast<int32_t>(result));
      acquireDone.string("vk_result_name", VkResultName(result));
      acquireDone.number("thread_id", CurrentThreadId());
      trace_.emit(std::move(acquireDone));
      if (result == VK_SUBOPTIMAL_KHR) {
        JsonObject subEvent("swapchain_suboptimal");
        subEvent.string("operation", "vkAcquireNextImageKHR");
        subEvent.number("image_index", imageIndex);
        trace_.emit(std::move(subEvent));
      }
      if (!acquired) {
        if (result == VK_TIMEOUT) {
          JsonObject timeoutEvent("wait_timeout");
          timeoutEvent.string("operation", "vkAcquireNextImageKHR");
          timeoutEvent.number("app_acquire_sequence", appAcquireSequence);
          trace_.emit(std::move(timeoutEvent));
        }
        (void)fail("vkAcquireNextImageKHR", result);
        break;
      }
      if (!validImage) {
        (void)fail("vkAcquireNextImageKHR_invalid_image_index",
                   VK_ERROR_INITIALIZATION_FAILED);
        break;
      }

      if (imagesInFlight_[imageIndex] != VK_NULL_HANDLE &&
          imagesInFlight_[imageIndex] != frame.fence) {
        const uint64_t imageFenceBegin = MachAbsoluteNanoseconds();
        result = vkWaitForFences(device_, 1, &imagesInFlight_[imageIndex],
                                 VK_TRUE, kFenceTimeoutNs);
        const uint64_t imageFenceEnd = MachAbsoluteNanoseconds();
        JsonObject imageFenceWait("acquired_image_fence_wait");
        imageFenceWait.number("frame_id", frameId);
        imageFenceWait.number("image_index", imageIndex);
        imageFenceWait.number("image_id", HandleValue(swapchainImages_[imageIndex]));
        imageFenceWait.number("fence_id", HandleValue(imagesInFlight_[imageIndex]));
        imageFenceWait.number("begin_mach_ns", imageFenceBegin);
        imageFenceWait.number("end_mach_ns", imageFenceEnd);
        imageFenceWait.number("duration_ns", imageFenceEnd - imageFenceBegin);
        imageFenceWait.number("vk_result", static_cast<int32_t>(result));
        imageFenceWait.string("vk_result_name", VkResultName(result));
        trace_.emit(std::move(imageFenceWait));
        if (result != VK_SUCCESS) {
          if (result == VK_TIMEOUT) {
            JsonObject timeoutEvent("wait_timeout");
            timeoutEvent.string("operation", "vkWaitForFences(acquired_image)");
            timeoutEvent.number("image_index", imageIndex);
            trace_.emit(std::move(timeoutEvent));
          }
          (void)fail("vkWaitForFences(acquired image)", result);
          break;
        }
      }

      result = vkResetCommandBuffer(frame.commandBuffer, 0);
      if (result != VK_SUCCESS) {
        (void)fail("vkResetCommandBuffer", result);
        break;
      }
      if (!recordClear(frame.commandBuffer, imageIndex, frameId)) {
        break;
      }

      result = vkResetFences(device_, 1, &frame.fence);
      if (result != VK_SUCCESS) {
        (void)fail("vkResetFences", result);
        break;
      }

      VkSemaphoreSubmitInfo waitInfo{};
      waitInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_SUBMIT_INFO;
      waitInfo.semaphore = frame.imageAvailable;
      waitInfo.value = 0;
      waitInfo.stageMask = VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT;
      waitInfo.deviceIndex = 0;

      VkCommandBufferSubmitInfo commandInfo{};
      commandInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO;
      commandInfo.commandBuffer = frame.commandBuffer;
      commandInfo.deviceMask = 0;

      VkSemaphoreSubmitInfo signalInfo{};
      signalInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_SUBMIT_INFO;
      signalInfo.semaphore = renderFinished_[imageIndex];
      signalInfo.value = 0;
      signalInfo.stageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT;
      signalInfo.deviceIndex = 0;

      VkSubmitInfo2 submitInfo{};
      submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO_2;
      submitInfo.waitSemaphoreInfoCount = 1;
      submitInfo.pWaitSemaphoreInfos = &waitInfo;
      submitInfo.commandBufferInfoCount = 1;
      submitInfo.pCommandBufferInfos = &commandInfo;
      submitInfo.signalSemaphoreInfoCount = 1;
      submitInfo.pSignalSemaphoreInfos = &signalInfo;

      const uint64_t appSubmitSequence = ++appSubmitSequence_;
      emitFrameBoundary("app_vkQueueSubmit2_begin", frameId, imageIndex,
                        appSubmitSequence, "app_submit_seq", VK_SUCCESS);
      const uint64_t submitBegin = MachAbsoluteNanoseconds();
      mvkNativeTraceSetCurrentAppFrameID(frameId);
      result = vkQueueSubmit2(queue_, 1, &submitInfo, frame.fence);
      mvkNativeTraceSetCurrentAppFrameID(0);
      const uint64_t submitEnd = MachAbsoluteNanoseconds();
      emitTimedFrameBoundary("app_vkQueueSubmit2_end", frameId, imageIndex,
                             appSubmitSequence, "app_submit_seq", submitBegin, submitEnd, result);
      if (result != VK_SUCCESS) {
        (void)fail("vkQueueSubmit2", result);
        break;
      }
      ++submittedFrames_;
      imagesInFlight_[imageIndex] = frame.fence;

      const uint64_t appPresentSequence = ++appPresentSequence_;
      const uint32_t presentIdGoogle = static_cast<uint32_t>(appPresentSequence);
      VkPresentTimeGOOGLE presentTime{};
      presentTime.presentID = presentIdGoogle;
      presentTime.desiredPresentTime = 0;  // Public API request: present ASAP.
      VkPresentTimesInfoGOOGLE presentTimes{};
      presentTimes.sType = VK_STRUCTURE_TYPE_PRESENT_TIMES_INFO_GOOGLE;
      presentTimes.swapchainCount = 1;
      presentTimes.pTimes = &presentTime;

      VkPresentInfoKHR presentInfo{};
      presentInfo.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
      presentInfo.pNext = getPastPresentationTiming_ == nullptr
          ? nullptr : &presentTimes;
      presentInfo.waitSemaphoreCount = 1;
      presentInfo.pWaitSemaphores = &renderFinished_[imageIndex];
      presentInfo.swapchainCount = 1;
      presentInfo.pSwapchains = &swapchain_;
      presentInfo.pImageIndices = &imageIndex;

      emitFrameBoundary("app_vkQueuePresentKHR_begin", frameId, imageIndex,
                        appPresentSequence, "app_present_seq", VK_SUCCESS);
      const uint64_t presentBegin = MachAbsoluteNanoseconds();
      mvkNativeTraceSetCurrentAppFrameID(frameId);
      result = vkQueuePresentKHR(queue_, &presentInfo);
      mvkNativeTraceSetCurrentAppFrameID(0);
      const uint64_t presentEnd = MachAbsoluteNanoseconds();
      emitTimedFrameBoundary("app_vkQueuePresentKHR_end", frameId, imageIndex,
                             appPresentSequence, "app_present_seq", presentBegin, presentEnd, result,
                             getPastPresentationTiming_ == nullptr ? 0 : presentIdGoogle);
      ++presentCalls_;
      if (result == VK_SUBOPTIMAL_KHR) {
        JsonObject subEvent("swapchain_suboptimal");
        subEvent.string("operation", "vkQueuePresentKHR");
        subEvent.number("image_index", imageIndex);
        trace_.emit(std::move(subEvent));
      }
      if (VkSucceeded(result)) {
        ++successfulPresents_;
        presentIdentity_[presentTime.presentID] = FrameIdentity{
            frameId, imageIndex, HandleValue(swapchainImages_[imageIndex])};
      } else {
        (void)fail("vkQueuePresentKHR", result);
        break;
      }

      if (getPastPresentationTiming_ != nullptr) {
        queryPresentationTiming();
      }
      if (!trace_.healthy()) {
        requestStop("trace_write_failure");
        break;
      }
    }
  }

  bool recordClear(VkCommandBuffer commandBuffer, uint32_t imageIndex,
                   uint64_t frameId) {
    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    VkResult result = vkBeginCommandBuffer(commandBuffer, &beginInfo);
    if (result != VK_SUCCESS) {
      return fail("vkBeginCommandBuffer", result);
    }

    static constexpr float colors[4][4] = {
        {0.88f, 0.08f, 0.08f, 1.0f},
        {0.06f, 0.76f, 0.12f, 1.0f},
        {0.08f, 0.16f, 0.90f, 1.0f},
        {0.92f, 0.92f, 0.92f, 1.0f}};
    const uint32_t paletteIndex = static_cast<uint32_t>((frameId / 30U) % 4U);
    VkClearValue clearValue{};
    std::copy(std::begin(colors[paletteIndex]), std::end(colors[paletteIndex]),
              clearValue.color.float32);
    VkRenderPassBeginInfo renderPassInfo{};
    renderPassInfo.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    renderPassInfo.renderPass = renderPass_;
    renderPassInfo.framebuffer = framebuffers_[imageIndex];
    renderPassInfo.renderArea.offset = {0, 0};
    renderPassInfo.renderArea.extent = {kDrawableWidth, kDrawableHeight};
    renderPassInfo.clearValueCount = 1;
    renderPassInfo.pClearValues = &clearValue;
    vkCmdBeginRenderPass(commandBuffer, &renderPassInfo,
                         VK_SUBPASS_CONTENTS_INLINE);
    vkCmdEndRenderPass(commandBuffer);
    result = vkEndCommandBuffer(commandBuffer);
    if (result != VK_SUCCESS) {
      return fail("vkEndCommandBuffer", result);
    }

    JsonObject event("frame_recorded");
    event.number("app_frame_id", frameId);
    event.number("frame_id", frameId);
    event.number("image_index", imageIndex);
    event.number("image_id", HandleValue(swapchainImages_[imageIndex]));
    event.number("palette_index", paletteIndex);
    event.number("clear_r", colors[paletteIndex][0]);
    event.number("clear_g", colors[paletteIndex][1]);
    event.number("clear_b", colors[paletteIndex][2]);
    event.number("clear_a", 1.0f);
    trace_.emit(std::move(event));
    return true;
  }

  void emitFrameBoundary(const char *eventName, uint64_t frameId,
                         uint32_t imageIndex, uint64_t appSequence,
                         const char *sequenceKey, VkResult result) {
    JsonObject event(eventName);
    addFrameIdentity(event, frameId, imageIndex);
    event.number(sequenceKey, appSequence);
    event.number("queue_id", HandleValue(queue_));
    event.number("queue_family_index", selected_.queueFamilyIndex);
    event.number("queue_index", 0);
    event.number("vk_result", static_cast<int32_t>(result));
    event.string("vk_result_name", VkResultName(result));
    trace_.emit(std::move(event));
  }

  void emitTimedFrameBoundary(const char *eventName, uint64_t frameId,
                              uint32_t imageIndex, uint64_t appSequence,
                              const char *sequenceKey, uint64_t beginNs,
                              uint64_t endNs, VkResult result,
                              uint32_t presentIdGoogle = 0) {
    JsonObject event(eventName);
    addFrameIdentity(event, frameId, imageIndex);
    event.number(sequenceKey, appSequence);
    if (presentIdGoogle != 0) {
      event.number("present_id_google", presentIdGoogle);
    }
    event.number("queue_id", HandleValue(queue_));
    event.number("queue_family_index", selected_.queueFamilyIndex);
    event.number("queue_index", 0);
    event.number("call_begin_mach_ns", beginNs);
    event.number("call_end_mach_ns", endNs);
    event.number("duration_ns", endNs - beginNs);
    event.number("vk_result", static_cast<int32_t>(result));
    event.string("vk_result_name", VkResultName(result));
    trace_.emit(std::move(event));
  }

  void addFrameIdentity(JsonObject &event, uint64_t frameId,
                        uint32_t imageIndex) {
    event.number("app_frame_id", frameId);
    event.number("frame_id", frameId);
    event.number("image_index", imageIndex);
    event.number("image_id", HandleValue(swapchainImages_[imageIndex]));
    event.number("swapchain_id", HandleValue(swapchain_));
  }

  void queryPresentationTiming() {
    uint32_t count = 0;
    VkResult result = getPastPresentationTiming_(device_, swapchain_, &count,
                                                 nullptr);
    if (result != VK_SUCCESS && result != VK_INCOMPLETE) {
      JsonObject event("presentation_timing_query");
      event.number("vk_result", static_cast<int32_t>(result));
      event.string("vk_result_name", VkResultName(result));
      event.number("record_count", count);
      trace_.emit(std::move(event));
      return;
    }
    if (count == 0) {
      return;
    }
    std::vector<VkPastPresentationTimingGOOGLE> timings(count);
    result = getPastPresentationTiming_(device_, swapchain_, &count,
                                        timings.data());
    if (result != VK_SUCCESS && result != VK_INCOMPLETE) {
      JsonObject event("presentation_timing_query");
      event.number("vk_result", static_cast<int32_t>(result));
      event.string("vk_result_name", VkResultName(result));
      event.number("record_count", count);
      trace_.emit(std::move(event));
      return;
    }
    const size_t available = std::min<size_t>(count, timings.size());
    for (size_t index = 0; index < available; ++index) {
      const VkPastPresentationTimingGOOGLE &timing = timings[index];
      if (!reportedPresentationIds_.insert(timing.presentID).second) {
        continue;
      }
      JsonObject event("presentation_completion_observed");
      event.number("present_id", timing.presentID);
      event.string("timestamp_domain", "VK_GOOGLE_display_timing nanoseconds");
      event.number("actual_present_time_ns", timing.actualPresentTime);
      event.number("earliest_present_time_ns", timing.earliestPresentTime);
      event.number("present_margin_ns", timing.presentMargin);
      event.number("observation_mach_ns", MachAbsoluteNanoseconds());
      auto identity = presentIdentity_.find(timing.presentID);
      if (identity != presentIdentity_.end()) {
        event.number("frame_id", identity->second.frameId);
        event.number("image_index", identity->second.imageIndex);
        event.number("image_id", identity->second.imageHandle);
      } else {
        event.null("frame_id");
        event.null("image_index");
        event.null("image_id");
      }
      trace_.emit(std::move(event));
      ++completionRecords_;
    }
  }

  void drainPresentationTiming() {
    if (getPastPresentationTiming_ == nullptr || device_ == VK_NULL_HANDLE ||
        swapchain_ == VK_NULL_HANDLE) {
      return;
    }
    const uint64_t deadline = MachAbsoluteNanoseconds() + 1500000000ULL;
    while (MachAbsoluteNanoseconds() < deadline &&
           reportedPresentationIds_.size() < presentIdentity_.size()) {
      queryPresentationTiming();
      if (reportedPresentationIds_.size() >= presentIdentity_.size()) {
        break;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    JsonObject event("presentation_timing_drain");
    event.number("present_ids_submitted",
                 static_cast<uint64_t>(presentIdentity_.size()));
    event.number("completion_records_observed",
                 static_cast<uint64_t>(reportedPresentationIds_.size()));
    event.number("unobserved_present_ids",
                 static_cast<uint64_t>(presentIdentity_.size() -
                                       reportedPresentationIds_.size()));
    trace_.emit(std::move(event));
  }

  bool fail(const char *operation, VkResult result) {
    JsonObject event("vulkan_error");
    event.string("operation", operation);
    event.number("vk_result", static_cast<int32_t>(result));
    event.string("vk_result_name", VkResultName(result));
    event.number("thread_id", CurrentThreadId());
    trace_.emit(std::move(event));
    std::fprintf(stderr, "ERROR %s: %s (%d)\n", operation,
                 VkResultName(result), static_cast<int>(result));
    result_.store(1, std::memory_order_relaxed);
    return false;
  }

  void cleanup() {
    if (device_ != VK_NULL_HANDLE) {
      const VkResult idleResult = vkDeviceWaitIdle(device_);
      JsonObject idle("device_wait_idle");
      idle.number("vk_result", static_cast<int32_t>(idleResult));
      idle.string("vk_result_name", VkResultName(idleResult));
      trace_.emit(std::move(idle));
      if (idleResult != VK_SUCCESS && result() == 0) {
        result_.store(1, std::memory_order_relaxed);
      }

      for (VkFramebuffer framebuffer : framebuffers_) {
        if (framebuffer != VK_NULL_HANDLE) {
          vkDestroyFramebuffer(device_, framebuffer, nullptr);
        }
      }
      framebuffers_.clear();
      for (VkImageView imageView : imageViews_) {
        if (imageView != VK_NULL_HANDLE) {
          vkDestroyImageView(device_, imageView, nullptr);
        }
      }
      imageViews_.clear();
      if (renderPass_ != VK_NULL_HANDLE) {
        vkDestroyRenderPass(device_, renderPass_, nullptr);
        renderPass_ = VK_NULL_HANDLE;
      }
      for (FrameSync &frame : frames_) {
        if (frame.fence != VK_NULL_HANDLE) {
          vkDestroyFence(device_, frame.fence, nullptr);
          frame.fence = VK_NULL_HANDLE;
        }
        if (frame.imageAvailable != VK_NULL_HANDLE) {
          vkDestroySemaphore(device_, frame.imageAvailable, nullptr);
          frame.imageAvailable = VK_NULL_HANDLE;
        }
        frame.commandBuffer = VK_NULL_HANDLE;
      }
      for (VkSemaphore semaphore : renderFinished_) {
        if (semaphore != VK_NULL_HANDLE) {
          vkDestroySemaphore(device_, semaphore, nullptr);
        }
      }
      renderFinished_.clear();
      if (commandPool_ != VK_NULL_HANDLE) {
        vkDestroyCommandPool(device_, commandPool_, nullptr);
        commandPool_ = VK_NULL_HANDLE;
      }
      if (swapchain_ != VK_NULL_HANDLE) {
        vkDestroySwapchainKHR(device_, swapchain_, nullptr);
        swapchain_ = VK_NULL_HANDLE;
      }
      vkDestroyDevice(device_, nullptr);
      device_ = VK_NULL_HANDLE;
      queue_ = VK_NULL_HANDLE;
    }
    if (instance_ != VK_NULL_HANDLE && surface_ != VK_NULL_HANDLE) {
      vkDestroySurfaceKHR(instance_, surface_, nullptr);
      surface_ = VK_NULL_HANDLE;
    }
    if (instance_ != VK_NULL_HANDLE) {
      vkDestroyInstance(instance_, nullptr);
      instance_ = VK_NULL_HANDLE;
    }
  }

  std::string SurfaceFormatsJson(const std::vector<VkSurfaceFormatKHR> &formats) {
    std::string json = "[";
    for (size_t index = 0; index < formats.size(); ++index) {
      if (index != 0) {
        json += ',';
      }
      json += "{\"format\":" + std::to_string(static_cast<int32_t>(formats[index].format)) +
              ",\"color_space\":" + std::to_string(static_cast<int32_t>(formats[index].colorSpace)) + "}";
    }
    json += ']';
    return json;
  }

  std::string PresentModesJson(const std::vector<VkPresentModeKHR> &modes) {
    std::string json = "[";
    for (size_t index = 0; index < modes.size(); ++index) {
      if (index != 0) {
        json += ',';
      }
      json += std::to_string(static_cast<int32_t>(modes[index]));
    }
    json += ']';
    return json;
  }

  std::string ImageHandlesJson(const std::vector<VkImage> &images) {
    std::string json = "[";
    for (size_t index = 0; index < images.size(); ++index) {
      if (index != 0) {
        json += ',';
      }
      json += "\"" + JsonEscape(HandleString(images[index])) + "\"";
    }
    json += ']';
    return json;
  }

  static VkCompositeAlphaFlagBitsKHR ChooseCompositeAlpha(
      VkCompositeAlphaFlagsKHR supported) {
    static constexpr VkCompositeAlphaFlagBitsKHR candidates[] = {
        VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
        VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR,
        VK_COMPOSITE_ALPHA_POST_MULTIPLIED_BIT_KHR,
        VK_COMPOSITE_ALPHA_INHERIT_BIT_KHR};
    for (VkCompositeAlphaFlagBitsKHR candidate : candidates) {
      if ((supported & candidate) != 0) {
        return candidate;
      }
    }
    return VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
  }

  Trace &trace_;
  CAMetalLayer *__strong layer_;
  const uint64_t seconds_;
  std::thread worker_;
  std::atomic<bool> stopRequested_{false};
  std::atomic<bool> finished_{false};
  std::atomic<int> result_{0};

  VkInstance instance_ = VK_NULL_HANDLE;
  VkSurfaceKHR surface_ = VK_NULL_HANDLE;
  SelectedDevice selected_{};
  VkDevice device_ = VK_NULL_HANDLE;
  VkQueue queue_ = VK_NULL_HANDLE;
  VkSwapchainKHR swapchain_ = VK_NULL_HANDLE;
  VkSurfaceCapabilitiesKHR surfaceCapabilities_{};
  VkSurfaceFormatKHR actualSurfaceFormat_{};
  std::vector<VkImage> swapchainImages_;
  std::vector<VkImageView> imageViews_;
  std::vector<VkFramebuffer> framebuffers_;
  std::vector<VkSemaphore> renderFinished_;
  std::vector<VkFence> imagesInFlight_;
  VkRenderPass renderPass_ = VK_NULL_HANDLE;
  VkCommandPool commandPool_ = VK_NULL_HANDLE;
  FrameSync frames_[kFramesInFlight]{};
  PFN_vkGetPastPresentationTimingGOOGLE getPastPresentationTiming_ = nullptr;
  std::unordered_map<uint32_t, FrameIdentity> presentIdentity_;
  std::unordered_set<uint32_t> reportedPresentationIds_;
  uint64_t appAcquireSequence_ = 0;
  uint64_t appSubmitSequence_ = 0;
  uint64_t appPresentSequence_ = 0;
  uint64_t frameSequence_ = 0;
  uint64_t submittedFrames_ = 0;
  uint64_t presentCalls_ = 0;
  uint64_t successfulPresents_ = 0;
  uint64_t completionRecords_ = 0;
};

}  // namespace

NSString *LayerSignature(CAMetalLayer *layer, NSWindow *window,
                         const DisplayFacts &display) {
  return [NSString stringWithFormat:
      @"%lu|%d|%d|%d|%.6f|%.6f|%.6f|%.6f|%lu|%u|%u|%u|%u|%.4f|%.4f|%@|%@",
      static_cast<unsigned long>(layer.maximumDrawableCount),
      layer.allowsNextDrawableTimeout ? 1 : 0,
      layer.displaySyncEnabled ? 1 : 0,
      layer.framebufferOnly ? 1 : 0,
      static_cast<double>(layer.drawableSize.width),
      static_cast<double>(layer.drawableSize.height),
      static_cast<double>(layer.contentsScale),
      static_cast<double>(window.backingScaleFactor),
      static_cast<unsigned long>(layer.pixelFormat),
      display.displayId, display.pixelWidth, display.pixelHeight, display.modeId,
      display.nominalRefreshHz, display.maximumFramesPerSecond,
      [NSString stringWithUTF8String:display.name.c_str()],
      [NSString stringWithUTF8String:display.uuid.c_str()]];
}

@interface NativeControlAppDelegate : NSObject <NSApplicationDelegate, NSWindowDelegate>
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSView *metalView;
@property(nonatomic, strong) CAMetalLayer *metalLayer;
@property(nonatomic, strong) NSTimer *stateTimer;
@property(nonatomic, copy) NSString *lastLayerSignature;
@property(nonatomic) int exitCode;
- (instancetype)initWithTrace:(Trace *)trace renderer:(Renderer *)renderer
                      seconds:(uint64_t)seconds;
- (void)abortStartup:(NSString *)message;
- (void)pollLayerState:(NSTimer *)timer;
- (void)recordLayerState:(NSString *)reason force:(BOOL)force;
@end

@implementation NativeControlAppDelegate {
  Trace *trace_;
  Renderer *renderer_;
  uint64_t seconds_;
  bool stateMonitoringStarted_;
}

- (instancetype)initWithTrace:(Trace *)trace renderer:(Renderer *)renderer
                       seconds:(uint64_t)seconds {
  self = [super init];
  if (self) {
    trace_ = trace;
    renderer_ = renderer;
    seconds_ = seconds;
    _exitCode = 0;
  }
  return self;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
  (void)sender;
  return YES;
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
  (void)notification;
  const NSRect contentRect = NSMakeRect(0, 0, kDrawableWidth, kDrawableHeight);
  self.window = [[NSWindow alloc]
      initWithContentRect:contentRect
                styleMask:(NSWindowStyleMaskTitled |
                           NSWindowStyleMaskClosable |
                           NSWindowStyleMaskMiniaturizable)
                  backing:NSBackingStoreBuffered
                    defer:NO];
  if (self.window == nil) {
    [self abortStartup:@"NSWindow creation failed"];
    return;
  }
  self.window.title = @"STEP 11D.3-A Native MoltenVK Drawable Baseline";
  self.window.delegate = self;
  self.window.opaque = YES;
  self.window.backgroundColor = NSColor.blackColor;

  self.metalView = [[NSView alloc] initWithFrame:NSMakeRect(
      0, 0, kDrawableWidth, kDrawableHeight)];
  self.metalView.wantsLayer = YES;
  self.metalLayer = [CAMetalLayer layer];
  if (self.metalView == nil || self.metalLayer == nil) {
    [self abortStartup:@"CAMetalLayer or NSView creation failed"];
    return;
  }
  self.metalView.layer = self.metalLayer;
  self.window.contentView = self.metalView;
  [self.window center];
  [self.window makeKeyAndOrderFront:nil];
  [NSApp activateIgnoringOtherApps:YES];

  // Let AppKit attach the view to its window and establish the target screen
  // before setting the requested drawable dimensions.
  [[NSRunLoop currentRunLoop]
      runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.15]];
  [self recordLayerState:@"startup_before_control_configuration" force:YES];

  id<MTLDevice> metalDevice = MTLCreateSystemDefaultDevice();
  if (metalDevice == nil) {
    [self abortStartup:@"MTLCreateSystemDefaultDevice returned nil"];
    return;
  }
  // Only the public layer properties needed to select the target surface are
  // assigned. maximumDrawableCount, allowsNextDrawableTimeout, and
  // displaySyncEnabled remain at their defaults and are only observed.
  self.metalLayer.device = metalDevice;
  self.metalLayer.pixelFormat = MTLPixelFormatBGRA8Unorm;
  self.metalLayer.contentsScale = 1.0;
  self.metalLayer.drawableSize = CGSizeMake(kDrawableWidth, kDrawableHeight);
  [self recordLayerState:@"startup_after_control_configuration" force:YES];

  JsonObject metadata("experiment_metadata");
  metadata.string("step", "11D.3-A");
  metadata.number("requested_seconds", seconds_);
  metadata.string("trace_path", trace_->path());
  metadata.string("trace_clock", "mach_absolute_time converted to nanoseconds");
  metadata.number("requested_window_content_width_points", kDrawableWidth);
  metadata.number("requested_window_content_height_points", kDrawableHeight);
  metadata.number("requested_drawable_width_pixels", kDrawableWidth);
  metadata.number("requested_drawable_height_pixels", kDrawableHeight);
  metadata.number("requested_swapchain_image_count", kSwapchainImageCount);
  metadata.number("frames_in_flight", kFramesInFlight);
  metadata.string("frames_in_flight_policy",
      "three frame slots with per-frame acquire semaphores and fences; render-finished semaphores are indexed by acquired swapchain image");
  metadata.string("requested_pixel_format", "VK_FORMAT_B8G8R8A8_UNORM / MTLPixelFormatBGRA8Unorm");
  metadata.string("requested_present_mode", "VK_PRESENT_MODE_FIFO_KHR");
  metadata.boolean("windowed", true);
  metadata.boolean("resizable", false);
  metadata.raw("assigned_layer_properties",
      "[\"device\",\"pixelFormat\",\"contentsScale\",\"drawableSize\"]");
  metadata.raw("untouched_layer_properties",
      "[\"maximumDrawableCount\",\"allowsNextDrawableTimeout\",\"displaySyncEnabled\"]");
  metadata.string("layer_change_observation",
      "startup snapshots, window display/backing/resize delegate callbacks, and 100ms main-run-loop polling");
  metadata.raw("comparison_reference", R"json({
    "step11d2_wsi_log":"experiments/dxvk_macos_step11dR/evidence/step11d2-diagnostic/diag-60s-07-submit-reclaim-split/slow-visual_d3d11.log",
    "step11d2_run_manifest":"experiments/dxvk_macos_step11dR/evidence/step11d2-diagnostic/diag-60s-07-submit-reclaim-split/manifest.json",
    "recorded_format":"VK_FORMAT_B8G8R8A8_UNORM",
    "recorded_color_space":"VK_COLOR_SPACE_SRGB_NONLINEAR_KHR",
    "recorded_present_mode":"VK_PRESENT_MODE_FIFO_KHR",
    "recorded_buffer_size":{"width":640,"height":360},
    "recorded_image_count":3,
    "recorded_timing_extension":"unavailable",
    "window_content_points":"not recorded",
    "layer_contents_scale":"not recorded",
    "target_display_identity":"not recorded",
    "requested_refresh_hz":{"value":null,"status":"UNVERIFIED_NOT_RECORDED"}
  })json");
  metadata.raw("known_comparison_mismatches",
      "[\"step11d2 evidence does not record window content point dimensions, CAMetalLayer contentsScale, display identity, or requested refresh rate\"]");
  metadata.string("acquisition_trace_correlation",
      "app_acquire_sequence is this process call ordinal; the MoltenVK patch sequence is independent and may include construction-time acquisition. Correlate via API-call timestamps, thread identity, swapchain image index, and image identity.");
  trace_->emit(std::move(metadata));

  self.stateTimer = [NSTimer scheduledTimerWithTimeInterval:0.1
      target:self selector:@selector(pollLayerState:)
      userInfo:nil repeats:YES];
  stateMonitoringStarted_ = true;
  [self recordLayerState:@"monitoring_started" force:YES];
  renderer_->start(self.metalLayer);
}

- (void)abortStartup:(NSString *)message {
  JsonObject event("startup_error");
  event.string("message", NSStringUtf8(message));
  trace_->emit(std::move(event));
  std::fprintf(stderr, "ERROR %s\n", [message UTF8String]);
  self.exitCode = 1;
  dispatch_async(dispatch_get_main_queue(), ^{
    [NSApp terminate:nil];
  });
}

- (void)pollLayerState:(NSTimer *)timer {
  (void)timer;
  [self recordLayerState:@"poll_100ms" force:NO];
}

- (void)windowDidResize:(NSNotification *)notification {
  (void)notification;
  [self recordLayerState:@"window_resize_callback" force:NO];
}

- (void)windowDidChangeBackingProperties:(NSNotification *)notification {
  (void)notification;
  [self recordLayerState:@"window_backing_change_callback" force:NO];
}

- (void)windowDidChangeScreen:(NSNotification *)notification {
  (void)notification;
  [self recordLayerState:@"window_screen_change_callback" force:NO];
}

- (void)windowDidChangeOcclusionState:(NSNotification *)notification {
  (void)notification;
  [self recordLayerState:@"window_occlusion_change_callback" force:NO];
}

- (void)applicationDidChangeScreenParameters:(NSNotification *)notification {
  (void)notification;
  [self recordLayerState:@"screen_parameters_change_callback" force:NO];
}

- (void)recordLayerState:(NSString *)reason force:(BOOL)force {
  if (self.metalLayer == nil || self.window == nil) {
    return;
  }
  DisplayFacts display = ReadDisplayFacts(self.window.screen);
  NSString *signature = LayerSignature(self.metalLayer, self.window, display);
  const BOOL changed = self.lastLayerSignature == nil ||
      ![self.lastLayerSignature isEqualToString:signature];
  if (!force && !changed) {
    return;
  }

  JsonObject event("layer_state");
  event.string("reason", NSStringUtf8(reason));
  event.boolean("changed", changed);
  event.number("maximum_drawable_count",
               static_cast<uint32_t>(self.metalLayer.maximumDrawableCount));
  event.boolean("allows_next_drawable_timeout",
                self.metalLayer.allowsNextDrawableTimeout);
  event.boolean("display_sync_enabled", self.metalLayer.displaySyncEnabled);
  event.number("drawable_width", self.metalLayer.drawableSize.width);
  event.number("drawable_height", self.metalLayer.drawableSize.height);
  event.number("contents_scale", self.metalLayer.contentsScale);
  event.number("pixel_format", static_cast<uint32_t>(self.metalLayer.pixelFormat));
  event.string("pixel_format_name",
      self.metalLayer.pixelFormat == MTLPixelFormatBGRA8Unorm
          ? "MTLPixelFormatBGRA8Unorm" : "MTLPixelFormatOther");
  event.boolean("framebuffer_only", self.metalLayer.framebufferOnly);
  event.number("layer_bounds_width_points", self.metalLayer.bounds.size.width);
  event.number("layer_bounds_height_points", self.metalLayer.bounds.size.height);
  event.number("window_backing_scale", self.window.backingScaleFactor);
  event.number("display_id", display.displayId);
  event.string("display_uuid", display.uuid);
  event.string("display_name", display.name);
  event.number("display_pixel_width", display.pixelWidth);
  event.number("display_pixel_height", display.pixelHeight);
  event.number("display_mode_id", display.modeId);
  event.number("display_nominal_refresh_hz", display.nominalRefreshHz);
  event.number("screen_maximum_frames_per_second",
               display.maximumFramesPerSecond);
  event.number("screen_backing_scale", display.backingScale);
  event.string("refresh_rate_source",
      "CGDisplayModeGetRefreshRate and NSScreen.maximumFramesPerSecond public APIs; zero means unavailable");
  trace_->emit(std::move(event));

  self.lastLayerSignature = signature;
  if (stateMonitoringStarted_ && changed && renderer_ != nullptr) {
    JsonObject changeEvent("layer_state_changed");
    changeEvent.string("reason", NSStringUtf8(reason));
    changeEvent.string("new_signature", NSStringUtf8(signature));
    trace_->emit(std::move(changeEvent));
  }
}

- (void)windowWillClose:(NSNotification *)notification {
  (void)notification;
  renderer_->requestStop("window_closed");
}

- (void)applicationWillTerminate:(NSNotification *)notification {
  (void)notification;
  [self.stateTimer invalidate];
  self.stateTimer = nil;
  if (renderer_ != nullptr) {
    renderer_->requestStop("application_terminating");
    renderer_->join();
    self.exitCode = renderer_->result();
  }
}

@end

namespace {

bool LoaderProbe() {
  Dl_info info{};
  const void *symbol = reinterpret_cast<const void *>(&vkCreateInstance);
  if (dladdr(symbol, &info) == 0 || info.dli_fname == nullptr) {
    std::fprintf(stderr, "LOADER_PROBE=FAIL reason=dladdr_failed\n");
    return false;
  }
  std::printf("LOADER_PROBE=PASS symbol=vkCreateInstance path=%s compiled_arch=%s process_translated=%d\n",
              info.dli_fname, CompiledProcessArchitecture(), ProcessTranslationStatus());
  return true;
}

bool ParseArguments(int argc, char **argv, uint64_t *seconds,
                    std::string *tracePath) {
  *seconds = kRunSecondsDefault;
  *tracePath = kDefaultTracePath;
  for (int index = 1; index < argc; ++index) {
    if (std::strcmp(argv[index], "--help") == 0) {
      std::fprintf(stdout,
          "Usage: native_control [--seconds N] [--trace PATH]\n"
          "Default: --seconds %llu --trace %s\n",
          static_cast<unsigned long long>(kRunSecondsDefault), kDefaultTracePath);
      return false;
    }
    if (std::strcmp(argv[index], "--seconds") == 0 && index + 1 < argc) {
      char *end = nullptr;
      const unsigned long long parsed = std::strtoull(argv[++index], &end, 10);
      if (end == argv[index] || *end != '\0' || parsed == 0 ||
          parsed > kMaximumRunSeconds) {
        std::fprintf(stderr, "ERROR --seconds must be between 1 and %llu\n",
                     static_cast<unsigned long long>(kMaximumRunSeconds));
        return false;
      }
      *seconds = static_cast<uint64_t>(parsed);
      continue;
    }
    if (std::strcmp(argv[index], "--trace") == 0 && index + 1 < argc) {
      *tracePath = argv[++index];
      if (tracePath->empty()) {
        std::fprintf(stderr, "ERROR --trace requires a non-empty path\n");
        return false;
      }
      continue;
    }
    std::fprintf(stderr, "ERROR unknown or incomplete argument: %s\n", argv[index]);
    return false;
  }
  return true;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc == 2 && std::strcmp(argv[1], "--loader-probe") == 0) {
    return LoaderProbe() ? 0 : 3;
  }
  uint64_t seconds = kRunSecondsDefault;
  std::string tracePath;
  if (!ParseArguments(argc, argv, &seconds, &tracePath)) {
    return 2;
  }

  Trace trace(tracePath.c_str());
  if (!trace.isOpen()) {
    std::fprintf(stderr, "ERROR could not open trace file: %s\n",
                 tracePath.c_str());
    return 2;
  }
  JsonObject launch("process_start");
  launch.number("pid", static_cast<uint64_t>(getpid()));
  launch.number("requested_seconds", seconds);
  launch.string("trace_path", tracePath);
  launch.string("clock", "mach_absolute_time converted with mach_timebase_info");
  trace.emit(std::move(launch));

  Dl_info moltenvkInfo{};
  const void *vkCreateInstanceSymbol = reinterpret_cast<const void *>(&vkCreateInstance);
  if (dladdr(vkCreateInstanceSymbol, &moltenvkInfo) == 0 ||
      moltenvkInfo.dli_fname == nullptr) {
    std::fprintf(stderr, "ERROR could not resolve the loaded vkCreateInstance image\n");
    trace.shutdown();
    return 3;
  }
  JsonObject architecture("execution_architecture");
  architecture.string("compiled_architecture", CompiledProcessArchitecture());
  architecture.number("process_translated", ProcessTranslationStatus());
  architecture.string("rosetta_query", "sysctl.proc_translated; -1 means unavailable");
  architecture.string("moltenvk_loader_path", moltenvkInfo.dli_fname);
  architecture.boolean("loader_path_resolved", true);
  trace.emit(std::move(architecture));

  @autoreleasepool {
    [NSApplication sharedApplication];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    Renderer renderer(trace, nil, seconds);
    NativeControlAppDelegate *delegate = [[NativeControlAppDelegate alloc]
        initWithTrace:&trace renderer:&renderer seconds:seconds];
    NSApp.delegate = delegate;
    [NSApp run];
    renderer.requestStop("application_event_loop_returned");
    renderer.join();
    const int runResult = delegate.exitCode != 0 ? delegate.exitCode : renderer.result();
    trace.shutdown();
    return trace.finalHealthy() ? runResult : 3;
  }
}
