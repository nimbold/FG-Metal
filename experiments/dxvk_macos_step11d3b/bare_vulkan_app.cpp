#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define VK_USE_PLATFORM_WIN32_KHR
#define VK_NO_PROTOTYPES
#include <windows.h>
#include <wingdi.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <type_traits>
#include <vector>

namespace {

constexpr uint32_t kWidth = 640;
constexpr uint32_t kHeight = 360;
constexpr uint32_t kRequestedImageCount = 3;
constexpr uint32_t kFramesInFlight = 3;
constexpr uint64_t kDefaultRunSeconds = 30;
constexpr uint64_t kMaximumRunSeconds = 3600;
constexpr uint64_t kPublicWindowStartGateTimeoutSeconds = 900;
constexpr size_t kMaxTraceEvents = 350000;
constexpr char kPortabilitySubsetExtension[] = "VK_KHR_portability_subset";

enum class EventKind : uint8_t {
  acquireBegin,
  acquireEnd,
  submitBegin,
  submitEnd,
  presentBegin,
  presentEnd,
  fenceWaitBegin,
  fenceWaitEnd,
  shutdownWaitBegin,
  shutdownWaitEnd,
  fatal,
};

struct TraceEvent {
  EventKind kind{};
  uint32_t imageIndex = UINT32_MAX;
  int32_t vkResult = std::numeric_limits<int32_t>::min();
  uint32_t threadId = 0;
  uint64_t eventId = 0;
  uint64_t tsQpcTicks = 0;
  uint64_t callBeginQpcTicks = 0;
  uint64_t callEndQpcTicks = 0;
  uint64_t durationQpcTicks = 0;
  uint64_t appFrameId = 0;
  uint64_t appAcquireSeq = 0;
  uint64_t appSubmitSeq = 0;
  uint64_t appPresentSeq = 0;
  uint64_t deviceId = 0;
  uint64_t queueId = 0;
  uint64_t swapchainId = 0;
  uint64_t imageId = 0;
  uint64_t commandBufferId = 0;
  uint64_t waitSemaphoreId = 0;
  uint64_t signalSemaphoreId = 0;
  int32_t frameSlot = -1;
  uint32_t detailCode = 0;
};

struct AppConfig {
  uint64_t qpcFrequency = 0;
  uint64_t processId = 0;
  uint64_t targetUnixPid = 0;
  uint64_t publicWindowStartGateWaitBeginQpcTicks = 0;
  uint64_t publicWindowStartGatePassedQpcTicks = 0;
  uint64_t publicWindowStartGateReleasedMonotonicNS = 0;
  uint64_t runSeconds = kDefaultRunSeconds;
  uint64_t actualRunNs = 0;
  uint64_t appFramesStarted = 0;
  uint64_t appSubmits = 0;
  uint64_t appPresentRequests = 0;
  uint64_t instanceApiVersion = VK_API_VERSION_1_0;
  uint64_t physicalApiVersion = VK_API_VERSION_1_0;
  uint64_t deviceId = 0;
  uint64_t queueId = 0;
  uint64_t swapchainId = 0;
  uint32_t requestedWidth = kWidth;
  uint32_t requestedHeight = kHeight;
  uint32_t requestedImageCount = kRequestedImageCount;
  uint32_t actualWindowWidth = 0;
  uint32_t actualWindowHeight = 0;
  uint32_t actualSwapchainWidth = 0;
  uint32_t actualSwapchainHeight = 0;
  uint32_t actualImageCount = 0;
  uint32_t surfaceMinImageCount = 0;
  uint32_t surfaceMaxImageCount = 0;
  uint32_t surfaceCurrentExtentWidth = UINT32_MAX;
  uint32_t surfaceCurrentExtentHeight = UINT32_MAX;
  uint32_t queueFamilyIndex = UINT32_MAX;
  uint32_t dpiX = 0;
  uint32_t dpiY = 0;
  uint32_t displayRefreshHz = 0;
  uint32_t selectedFormat = VK_FORMAT_UNDEFINED;
  uint32_t selectedColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
  uint32_t selectedPresentMode = UINT32_MAX;
  uint32_t actualTransform = 0;
  uint32_t compositeAlpha = 0;
  int32_t windowX = 0;
  int32_t windowY = 0;
  char experimentToken[33]{};
  char experimentRunID[128]{};
  char hostTracePath[MAX_PATH * 4]{};
  bool portabilityEnumerationEnabled = false;
  bool synchronization2Enabled = false;
  bool windowDpiAware = false;
  bool visible = false;
  bool publicWindowStartGateRequired = false;
  bool publicWindowStartGatePassed = false;
  bool hostRuntimeStartupGatePassed = false;
  bool winevulkanDllModuleLoaded = false;
  bool winevulkanDllBeforeFrameLoop = false;
  char displayName[256]{};
  char vulkanModulePath[1024]{};
  char winevulkanDllModulePath[1024]{};
  char winevulkanDllHostPath[1024]{};
  char winevulkanDllSha256[65]{};
  char publicWindowStartGatePath[MAX_PATH * 4]{};
  char physicalDeviceName[VK_MAX_PHYSICAL_DEVICE_NAME_SIZE]{};
  char fatalError[512]{};
  char submit2Entrypoint[32] = "vkQueueSubmit2";
  char stopReason[64] = "duration_elapsed";
};

static TraceEvent gTraceEvents[kMaxTraceEvents];
static size_t gTraceCount = 0;
static uint64_t gNextEventId = 1;
static uint64_t gDroppedTraceEvents = 0;
static AppConfig gConfig{};
static char gTracePath[MAX_PATH * 4] = "step11d3b-app-trace.jsonl";
static HINSTANCE gModuleInstance = nullptr;
static HWND gWindow = nullptr;
static bool gQuitRequested = false;
static int gExitCode = 0;

template <typename Handle>
uint64_t HandleValue(Handle handle) {
  if constexpr (std::is_pointer_v<Handle>) {
    return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(handle));
  } else {
    return static_cast<uint64_t>(handle);
  }
}

uint64_t QpcNow() {
  LARGE_INTEGER value{};
  QueryPerformanceCounter(&value);
  return static_cast<uint64_t>(value.QuadPart);
}

uint64_t QpcTicksToNs(uint64_t ticks) {
  if (gConfig.qpcFrequency == 0) return 0;
  const unsigned __int128 scaled_ticks =
      static_cast<unsigned __int128>(ticks) * 1000000000ULL;
  const unsigned __int128 nanoseconds = scaled_ticks / gConfig.qpcFrequency;
  const unsigned __int128 maximum = std::numeric_limits<uint64_t>::max();
  return nanoseconds > maximum ? std::numeric_limits<uint64_t>::max()
                               : static_cast<uint64_t>(nanoseconds);
}

const char *EventName(EventKind kind) {
  switch (kind) {
    case EventKind::acquireBegin: return "app_vkAcquireNextImageKHR_begin";
    case EventKind::acquireEnd: return "app_vkAcquireNextImageKHR_end";
    case EventKind::submitBegin: return "app_vkQueueSubmit2_begin";
    case EventKind::submitEnd: return "app_vkQueueSubmit2_end";
    case EventKind::presentBegin: return "app_vkQueuePresentKHR_begin";
    case EventKind::presentEnd: return "app_vkQueuePresentKHR_end";
    case EventKind::fenceWaitBegin: return "app_vkWaitForFences_begin";
    case EventKind::fenceWaitEnd: return "app_vkWaitForFences_end";
    case EventKind::shutdownWaitBegin: return "app_vkDeviceWaitIdle_begin";
    case EventKind::shutdownWaitEnd: return "app_vkDeviceWaitIdle_end";
    case EventKind::fatal: return "app_fatal";
  }
  return "unknown";
}

void RecordEvent(TraceEvent event) {
  if (gTraceCount >= kMaxTraceEvents) {
    ++gDroppedTraceEvents;
    return;
  }
  event.eventId = gNextEventId++;
  event.threadId = GetCurrentThreadId();
  gTraceEvents[gTraceCount++] = event;
}

TraceEvent MakeEvent(EventKind kind, uint64_t frameId, uint64_t ts) {
  TraceEvent event{};
  event.kind = kind;
  event.tsQpcTicks = ts;
  event.appFrameId = frameId;
  event.deviceId = gConfig.deviceId;
  event.queueId = gConfig.queueId;
  event.swapchainId = gConfig.swapchainId;
  return event;
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
    case VK_ERROR_VALIDATION_FAILED_EXT: return "VK_ERROR_VALIDATION_FAILED_EXT";
    default: return "VK_RESULT_UNKNOWN";
  }
}

void SetFatal(const char *message, int exitCode) {
  if (gConfig.fatalError[0] == '\0') {
    std::snprintf(gConfig.fatalError, sizeof(gConfig.fatalError), "%s",
                  message == nullptr ? "unspecified failure" : message);
  }
  gExitCode = exitCode == 0 ? 1 : exitCode;
  std::snprintf(gConfig.stopReason, sizeof(gConfig.stopReason), "%s", "fatal");
  TraceEvent event = MakeEvent(EventKind::fatal, 0, QpcNow());
  event.detailCode = static_cast<uint32_t>(gExitCode);
  RecordEvent(event);
}

std::string JsonEscape(const char *input) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  if (input == nullptr) return out;
  for (const unsigned char c : std::string(input)) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (c < 0x20) {
          out += "\\u00";
          out += kHex[c >> 4];
          out += kHex[c & 0x0f];
        } else {
          out += static_cast<char>(c);
        }
    }
  }
  return out;
}

std::string WideToUtf8(const wchar_t *value) {
  if (value == nullptr || value[0] == L'\0') return {};
  const int needed = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0,
                                         nullptr, nullptr);
  if (needed <= 1) return {};
  std::string result(static_cast<size_t>(needed), '\0');
  if (WideCharToMultiByte(CP_UTF8, 0, value, -1, result.data(), needed,
                          nullptr, nullptr) <= 0) {
    return {};
  }
  result.resize(static_cast<size_t>(needed - 1));
  return result;
}

void ReadEnvironment() {
  char value[256]{};
  DWORD length = GetEnvironmentVariableA("FG_APP_TRACE_PATH", gTracePath,
                                        static_cast<DWORD>(sizeof(gTracePath)));
  if (length == 0 || length >= sizeof(gTracePath)) {
    std::snprintf(gTracePath, sizeof(gTracePath), "%s",
                  "step11d3b-app-trace.jsonl");
  }
  length = GetEnvironmentVariableA("FG_B_TARGET_UNIX_PID", value,
                                   static_cast<DWORD>(sizeof(value)));
  char *targetEnd = nullptr;
  const unsigned long long targetPid = length > 0 && length < sizeof(value)
      ? std::strtoull(value, &targetEnd, 10) : 0;
  if (length == 0 || length >= sizeof(value) || targetEnd == value ||
      *targetEnd != '\0' || targetPid == 0 || targetPid > 0x7fffffffULL ||
      std::to_string(targetPid) != value) {
    SetFatal("FG_B_TARGET_UNIX_PID is missing or malformed", 4);
  } else {
    gConfig.targetUnixPid = static_cast<uint64_t>(targetPid);
  }
  char token[64]{};
  length = GetEnvironmentVariableA("FG_B_EXPERIMENT_TOKEN", token,
                                   static_cast<DWORD>(sizeof(token)));
  const bool validToken = length == 32 &&
      std::all_of(token, token + 32, [](unsigned char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
      });
  if (!validToken) {
    SetFatal("FG_B_EXPERIMENT_TOKEN is missing or malformed", 4);
  } else {
    std::memcpy(gConfig.experimentToken, token, 32);
    gConfig.experimentToken[32] = '\0';
  }
  char runID[128]{};
  length = GetEnvironmentVariableA("FG_RUN_ID", runID,
                                   static_cast<DWORD>(sizeof(runID)));
  if (length > 0 && length < sizeof(runID)) {
    std::memcpy(gConfig.experimentRunID, runID, length + 1);
  }
  char gateRequired[8]{};
  length = GetEnvironmentVariableA("FG_PUBLIC_WINDOW_START_GATE_REQUIRED", gateRequired,
                                   static_cast<DWORD>(sizeof(gateRequired)));
  if (length > 0 && length < sizeof(gateRequired)) {
    if (std::strcmp(gateRequired, "1") == 0) {
      gConfig.publicWindowStartGateRequired = true;
    } else if (std::strcmp(gateRequired, "0") != 0) {
      SetFatal("FG_PUBLIC_WINDOW_START_GATE_REQUIRED must be 0 or 1", 4);
    }
  }
  length = GetEnvironmentVariableA("FG_PUBLIC_WINDOW_START_GATE_PATH",
                                   gConfig.publicWindowStartGatePath,
                                   static_cast<DWORD>(sizeof(gConfig.publicWindowStartGatePath)));
  if (gConfig.publicWindowStartGateRequired
      && (length == 0 || length >= sizeof(gConfig.publicWindowStartGatePath))) {
    SetFatal("public window start gate path is missing or too long", 4);
  }
  if (gConfig.publicWindowStartGateRequired
      && gConfig.experimentRunID[0] == '\0') {
    SetFatal("FG_RUN_ID is missing or malformed for the public start gate", 4);
  }
  const std::string appTracePath(gTracePath);
  const size_t traceSeparator = appTracePath.find_last_of("\\/");
  if (traceSeparator == std::string::npos || gConfig.targetUnixPid == 0) {
    SetFatal("app trace path cannot resolve the paired host trace", 4);
  } else {
    std::string hostTracePath = appTracePath.substr(0, traceSeparator);
    hostTracePath += "\\host-traces\\host-trace.";
    hostTracePath += std::to_string(gConfig.targetUnixPid);
    hostTracePath += ".jsonl";
    if (hostTracePath.size() >= sizeof(gConfig.hostTracePath)) {
      SetFatal("paired host trace path exceeds the supported length", 4);
    } else {
      std::memcpy(gConfig.hostTracePath, hostTracePath.c_str(),
                  hostTracePath.size() + 1);
    }
  }
  length = GetEnvironmentVariableA("FG_RUN_SECONDS", value,
                                   static_cast<DWORD>(sizeof(value)));
  if (length > 0 && length < sizeof(value)) {
    char *end = nullptr;
    const unsigned long long parsed = std::strtoull(value, &end, 10);
    if (end != value && *end == '\0' && parsed >= 1 &&
        parsed <= kMaximumRunSeconds) {
      gConfig.runSeconds = static_cast<uint64_t>(parsed);
    } else {
      std::snprintf(gConfig.fatalError, sizeof(gConfig.fatalError),
                    "FG_RUN_SECONDS must be an integer from 1 through 3600");
      std::snprintf(gConfig.stopReason, sizeof(gConfig.stopReason), "%s", "fatal");
      gExitCode = 2;
    }
  }
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wParam,
                            LPARAM lParam) {
  switch (message) {
    case WM_CLOSE:
      gQuitRequested = true;
      DestroyWindow(window);
      return 0;
    case WM_DESTROY:
      gQuitRequested = true;
      PostQuitMessage(0);
      return 0;
    default:
      return DefWindowProcW(window, message, wParam, lParam);
  }
}

bool CreateControlWindow() {
  WNDCLASSEXW klass{};
  klass.cbSize = sizeof(klass);
  klass.style = CS_HREDRAW | CS_VREDRAW;
  klass.lpfnWndProc = WindowProc;
  klass.hInstance = gModuleInstance;
  klass.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
  klass.hbrBackground = static_cast<HBRUSH>(GetStockObject(BLACK_BRUSH));
  klass.lpszClassName = L"FGMetalBareVulkanControl";
  if (RegisterClassExW(&klass) == 0 && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
    SetFatal("RegisterClassExW failed", 3);
    return false;
  }

  const DWORD style = WS_OVERLAPPEDWINDOW;
  RECT outer{0, 0, static_cast<LONG>(kWidth), static_cast<LONG>(kHeight)};
  if (!AdjustWindowRectEx(&outer, style, FALSE, 0)) {
    SetFatal("AdjustWindowRectEx failed", 3);
    return false;
  }
  const int outerWidth = outer.right - outer.left;
  const int outerHeight = outer.bottom - outer.top;
  gWindow = CreateWindowExW(0, klass.lpszClassName, L"FG-Metal Step 11D.3-B Bare Vulkan",
                            style, 64, 64, outerWidth, outerHeight, nullptr,
                            nullptr, gModuleInstance, nullptr);
  if (gWindow == nullptr) {
    SetFatal("CreateWindowExW failed", 3);
    return false;
  }

  ShowWindow(gWindow, SW_SHOWNORMAL);
  UpdateWindow(gWindow);
  SetForegroundWindow(gWindow);
  BringWindowToTop(gWindow);
  SetActiveWindow(gWindow);
  SetFocus(gWindow);
  RECT client{};
  if (!GetClientRect(gWindow, &client)) {
    SetFatal("GetClientRect failed", 3);
    return false;
  }
  gConfig.actualWindowWidth = static_cast<uint32_t>(std::max<LONG>(0, client.right - client.left));
  gConfig.actualWindowHeight = static_cast<uint32_t>(std::max<LONG>(0, client.bottom - client.top));
  RECT windowRect{};
  if (GetWindowRect(gWindow, &windowRect)) {
    gConfig.windowX = windowRect.left;
    gConfig.windowY = windowRect.top;
  }
  gConfig.visible = IsWindowVisible(gWindow) != FALSE && IsIconic(gWindow) == FALSE;

  HDC dc = GetDC(gWindow);
  if (dc != nullptr) {
    gConfig.dpiX = static_cast<uint32_t>(std::max(0, GetDeviceCaps(dc, LOGPIXELSX)));
    gConfig.dpiY = static_cast<uint32_t>(std::max(0, GetDeviceCaps(dc, LOGPIXELSY)));
    ReleaseDC(gWindow, dc);
  }
  HMONITOR monitor = MonitorFromWindow(gWindow, MONITOR_DEFAULTTONEAREST);
  MONITORINFOEXW monitorInfo{};
  monitorInfo.cbSize = sizeof(monitorInfo);
  if (monitor != nullptr && GetMonitorInfoW(monitor, &monitorInfo)) {
    const std::string display = WideToUtf8(monitorInfo.szDevice);
    std::snprintf(gConfig.displayName, sizeof(gConfig.displayName), "%s", display.c_str());
    DEVMODEW mode{};
    mode.dmSize = sizeof(mode);
    if (EnumDisplaySettingsW(monitorInfo.szDevice, ENUM_CURRENT_SETTINGS, &mode)) {
      gConfig.displayRefreshHz = mode.dmDisplayFrequency;
    }
  }
  return true;
}

bool HasInstanceExtension(const std::vector<VkExtensionProperties> &props,
                          const char *name) {
  return std::any_of(props.begin(), props.end(), [name](const auto &prop) {
    return std::strcmp(prop.extensionName, name) == 0;
  });
}

bool HasDeviceExtension(VkPhysicalDevice physical,
                        PFN_vkEnumerateDeviceExtensionProperties enumerate,
                        const char *name) {
  uint32_t count = 0;
  if (enumerate(physical, nullptr, &count, nullptr) != VK_SUCCESS) return false;
  std::vector<VkExtensionProperties> props(count);
  if (enumerate(physical, nullptr, &count, props.data()) != VK_SUCCESS) return false;
  props.resize(count);
  return HasInstanceExtension(props, name);
}

struct VulkanState {
  HMODULE vulkanModule = nullptr;
  PFN_vkGetInstanceProcAddr getInstanceProcAddr = nullptr;
  PFN_vkGetDeviceProcAddr getDeviceProcAddr = nullptr;
  VkInstance instance = VK_NULL_HANDLE;
  VkSurfaceKHR surface = VK_NULL_HANDLE;
  VkPhysicalDevice physicalDevice = VK_NULL_HANDLE;
  VkDevice device = VK_NULL_HANDLE;
  VkQueue queue = VK_NULL_HANDLE;
  VkSwapchainKHR swapchain = VK_NULL_HANDLE;
  VkRenderPass renderPass = VK_NULL_HANDLE;
  VkCommandPool commandPool = VK_NULL_HANDLE;
  std::vector<VkImage> images;
  std::vector<VkImageView> imageViews;
  std::vector<VkFramebuffer> framebuffers;
  std::vector<VkCommandBuffer> commandBuffers;
  std::array<VkSemaphore, kFramesInFlight> imageAvailable{};
  std::vector<VkSemaphore> renderFinished;
  std::array<VkFence, kFramesInFlight> frameFences{};
  uint32_t graphicsQueueFamily = UINT32_MAX;
  VkFormat format = VK_FORMAT_UNDEFINED;
  VkColorSpaceKHR colorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
  VkExtent2D extent{};

  PFN_vkCreateInstance createInstance = nullptr;
  PFN_vkEnumerateInstanceExtensionProperties enumerateInstanceExtensions = nullptr;
  PFN_vkEnumerateInstanceVersion enumerateInstanceVersion = nullptr;
  PFN_vkDestroyInstance destroyInstance = nullptr;
  PFN_vkEnumeratePhysicalDevices enumeratePhysicalDevices = nullptr;
  PFN_vkGetPhysicalDeviceProperties getPhysicalDeviceProperties = nullptr;
  PFN_vkGetPhysicalDeviceQueueFamilyProperties getQueueFamilyProperties = nullptr;
  PFN_vkEnumerateDeviceExtensionProperties enumerateDeviceExtensions = nullptr;
  PFN_vkGetPhysicalDeviceFeatures2 getPhysicalDeviceFeatures2 = nullptr;
  PFN_vkGetPhysicalDeviceSurfaceSupportKHR getSurfaceSupport = nullptr;
  PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR getSurfaceCapabilities = nullptr;
  PFN_vkGetPhysicalDeviceSurfaceFormatsKHR getSurfaceFormats = nullptr;
  PFN_vkGetPhysicalDeviceSurfacePresentModesKHR getSurfacePresentModes = nullptr;
  PFN_vkCreateWin32SurfaceKHR createWin32Surface = nullptr;
  PFN_vkDestroySurfaceKHR destroySurface = nullptr;
  PFN_vkCreateDevice createDevice = nullptr;
  PFN_vkDestroyDevice destroyDevice = nullptr;
  PFN_vkGetDeviceQueue getDeviceQueue = nullptr;
  PFN_vkCreateSwapchainKHR createSwapchain = nullptr;
  PFN_vkDestroySwapchainKHR destroySwapchain = nullptr;
  PFN_vkGetSwapchainImagesKHR getSwapchainImages = nullptr;
  PFN_vkAcquireNextImageKHR acquireNextImage = nullptr;
  PFN_vkQueuePresentKHR queuePresent = nullptr;
  PFN_vkQueueSubmit2 queueSubmit2 = nullptr;
  PFN_vkDeviceWaitIdle deviceWaitIdle = nullptr;
  PFN_vkQueueWaitIdle queueWaitIdle = nullptr;
  PFN_vkCreateRenderPass createRenderPass = nullptr;
  PFN_vkDestroyRenderPass destroyRenderPass = nullptr;
  PFN_vkCreateImageView createImageView = nullptr;
  PFN_vkDestroyImageView destroyImageView = nullptr;
  PFN_vkCreateFramebuffer createFramebuffer = nullptr;
  PFN_vkDestroyFramebuffer destroyFramebuffer = nullptr;
  PFN_vkCreateCommandPool createCommandPool = nullptr;
  PFN_vkDestroyCommandPool destroyCommandPool = nullptr;
  PFN_vkAllocateCommandBuffers allocateCommandBuffers = nullptr;
  PFN_vkResetCommandBuffer resetCommandBuffer = nullptr;
  PFN_vkBeginCommandBuffer beginCommandBuffer = nullptr;
  PFN_vkEndCommandBuffer endCommandBuffer = nullptr;
  PFN_vkCmdBeginRenderPass cmdBeginRenderPass = nullptr;
  PFN_vkCmdEndRenderPass cmdEndRenderPass = nullptr;
  PFN_vkCreateSemaphore createSemaphore = nullptr;
  PFN_vkDestroySemaphore destroySemaphore = nullptr;
  PFN_vkCreateFence createFence = nullptr;
  PFN_vkDestroyFence destroyFence = nullptr;
  PFN_vkWaitForFences waitForFences = nullptr;
  PFN_vkResetFences resetFences = nullptr;
};

VulkanState gVk{};

template <typename Proc>
bool LoadInstanceProc(Proc &destination, const char *name) {
  destination = reinterpret_cast<Proc>(gVk.getInstanceProcAddr(gVk.instance, name));
  if (destination != nullptr) return true;
  char message[256]{};
  std::snprintf(message, sizeof(message), "missing Vulkan instance procedure: %s", name);
  SetFatal(message, 4);
  return false;
}

template <typename Proc>
bool LoadDeviceProc(Proc &destination, const char *name) {
  destination = reinterpret_cast<Proc>(gVk.getDeviceProcAddr(gVk.device, name));
  if (destination != nullptr) return true;
  char message[256]{};
  std::snprintf(message, sizeof(message), "missing Vulkan device procedure: %s", name);
  SetFatal(message, 4);
  return false;
}

bool LoadVulkanModule() {
  gVk.vulkanModule = LoadLibraryW(L"vulkan-1.dll");
  if (gVk.vulkanModule == nullptr) {
    SetFatal("LoadLibraryW(vulkan-1.dll) failed", 4);
    return false;
  }
  const FARPROC getProc = GetProcAddress(gVk.vulkanModule, "vkGetInstanceProcAddr");
  static_assert(sizeof(getProc) == sizeof(gVk.getInstanceProcAddr));
  std::memcpy(&gVk.getInstanceProcAddr, &getProc, sizeof(getProc));
  if (gVk.getInstanceProcAddr == nullptr) {
    SetFatal("vulkan-1.dll does not export vkGetInstanceProcAddr", 4);
    return false;
  }
  wchar_t path[1024]{};
  const DWORD n = GetModuleFileNameW(gVk.vulkanModule, path,
                                    static_cast<DWORD>(sizeof(path) / sizeof(path[0])));
  path[(sizeof(path) / sizeof(path[0])) - 1] = L'\0';
  const std::string utf8 = n > 0 ? WideToUtf8(path) : std::string();
  std::snprintf(gConfig.vulkanModulePath, sizeof(gConfig.vulkanModulePath), "%s",
                utf8.c_str());
  return true;
}

bool CreateInstanceAndSurface() {
  if (!LoadVulkanModule()) return false;
  gVk.enumerateInstanceExtensions = reinterpret_cast<PFN_vkEnumerateInstanceExtensionProperties>(
      gVk.getInstanceProcAddr(VK_NULL_HANDLE, "vkEnumerateInstanceExtensionProperties"));
  gVk.createInstance = reinterpret_cast<PFN_vkCreateInstance>(
      gVk.getInstanceProcAddr(VK_NULL_HANDLE, "vkCreateInstance"));
  gVk.enumerateInstanceVersion = reinterpret_cast<PFN_vkEnumerateInstanceVersion>(
      gVk.getInstanceProcAddr(VK_NULL_HANDLE, "vkEnumerateInstanceVersion"));
  if (gVk.enumerateInstanceExtensions == nullptr || gVk.createInstance == nullptr) {
    SetFatal("Vulkan loader is missing required global procedures", 4);
    return false;
  }

  uint32_t extensionCount = 0;
  VkResult result = gVk.enumerateInstanceExtensions(nullptr, &extensionCount, nullptr);
  if (result != VK_SUCCESS) {
    SetFatal("vkEnumerateInstanceExtensionProperties(count) failed", 4);
    return false;
  }
  std::vector<VkExtensionProperties> extensions(extensionCount);
  result = gVk.enumerateInstanceExtensions(nullptr, &extensionCount, extensions.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkEnumerateInstanceExtensionProperties(list) failed", 4);
    return false;
  }
  extensions.resize(extensionCount);
  if (!HasInstanceExtension(extensions, VK_KHR_SURFACE_EXTENSION_NAME) ||
      !HasInstanceExtension(extensions, VK_KHR_WIN32_SURFACE_EXTENSION_NAME)) {
    SetFatal("VK_KHR_surface or VK_KHR_win32_surface is unavailable", 4);
    return false;
  }
  std::vector<const char *> enabledExtensions = {
      VK_KHR_SURFACE_EXTENSION_NAME, VK_KHR_WIN32_SURFACE_EXTENSION_NAME};
#ifdef VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME
  if (HasInstanceExtension(extensions, VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME)) {
    enabledExtensions.push_back(VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME);
    gConfig.portabilityEnumerationEnabled = true;
  }
#endif

  uint32_t loaderVersion = VK_API_VERSION_1_0;
  if (gVk.enumerateInstanceVersion != nullptr) {
    (void)gVk.enumerateInstanceVersion(&loaderVersion);
  }
  const uint32_t requestedApi = std::min(loaderVersion, VK_API_VERSION_1_3);
  gConfig.instanceApiVersion = requestedApi;
  VkApplicationInfo appInfo{VK_STRUCTURE_TYPE_APPLICATION_INFO};
  appInfo.pApplicationName = "FG-Metal Bare Wine Vulkan Control";
  appInfo.applicationVersion = VK_MAKE_API_VERSION(0, 1, 0, 0);
  appInfo.pEngineName = "none";
  appInfo.engineVersion = VK_MAKE_API_VERSION(0, 1, 0, 0);
  appInfo.apiVersion = requestedApi;
  VkInstanceCreateInfo instanceInfo{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
  instanceInfo.pApplicationInfo = &appInfo;
  instanceInfo.enabledExtensionCount = static_cast<uint32_t>(enabledExtensions.size());
  instanceInfo.ppEnabledExtensionNames = enabledExtensions.data();
#ifdef VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR
  if (gConfig.portabilityEnumerationEnabled) {
    instanceInfo.flags |= VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR;
  }
#endif
  result = gVk.createInstance(&instanceInfo, nullptr, &gVk.instance);
  if (result != VK_SUCCESS) {
    char message[256]{};
    std::snprintf(message, sizeof(message), "vkCreateInstance failed: %s (%d)",
                  VkResultName(result), static_cast<int>(result));
    SetFatal(message, 4);
    return false;
  }

  if (!LoadInstanceProc(gVk.destroyInstance, "vkDestroyInstance") ||
      !LoadInstanceProc(gVk.enumeratePhysicalDevices, "vkEnumeratePhysicalDevices") ||
      !LoadInstanceProc(gVk.getPhysicalDeviceProperties, "vkGetPhysicalDeviceProperties") ||
      !LoadInstanceProc(gVk.getQueueFamilyProperties, "vkGetPhysicalDeviceQueueFamilyProperties") ||
      !LoadInstanceProc(gVk.enumerateDeviceExtensions, "vkEnumerateDeviceExtensionProperties") ||
      !LoadInstanceProc(gVk.getSurfaceSupport, "vkGetPhysicalDeviceSurfaceSupportKHR") ||
      !LoadInstanceProc(gVk.getSurfaceCapabilities, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR") ||
      !LoadInstanceProc(gVk.getSurfaceFormats, "vkGetPhysicalDeviceSurfaceFormatsKHR") ||
      !LoadInstanceProc(gVk.getSurfacePresentModes, "vkGetPhysicalDeviceSurfacePresentModesKHR") ||
      !LoadInstanceProc(gVk.createWin32Surface, "vkCreateWin32SurfaceKHR") ||
      !LoadInstanceProc(gVk.destroySurface, "vkDestroySurfaceKHR") ||
      !LoadInstanceProc(gVk.createDevice, "vkCreateDevice")) {
    return false;
  }

  VkWin32SurfaceCreateInfoKHR surfaceInfo{VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR};
  surfaceInfo.hinstance = gModuleInstance;
  surfaceInfo.hwnd = gWindow;
  result = gVk.createWin32Surface(gVk.instance, &surfaceInfo, nullptr, &gVk.surface);
  if (result != VK_SUCCESS) {
    SetFatal("vkCreateWin32SurfaceKHR failed", 4);
    return false;
  }
  return true;
}

std::string EnvironmentString(const char *name) {
  char value[4096]{};
  const DWORD length = GetEnvironmentVariableA(name, value,
                                                static_cast<DWORD>(sizeof(value)));
  if (length == 0 || length >= sizeof(value)) return {};
  return std::string(value, length);
}

bool HasJsonString(const std::string &line, const char *key,
                   const std::string &value) {
  if (value.empty()) return false;
  const std::string needle = std::string("\"") + key + "\":\"" + value + "\"";
  return line.find(needle) != std::string::npos;
}

std::string JsonStringField(const std::string &line, const char *key) {
  const std::string marker = std::string("\"") + key + "\":\"";
  const size_t start = line.find(marker);
  if (start == std::string::npos) return {};
  const size_t valueStart = start + marker.size();
  const size_t valueEnd = line.find('"', valueStart);
  if (valueEnd == std::string::npos) return {};
  return line.substr(valueStart, valueEnd - valueStart);
}

bool JsonUnsignedFieldEquals(const std::string &line, const char *key,
                             uint64_t expected) {
  const std::string marker = std::string("\"") + key + "\":";
  const size_t start = line.find(marker);
  if (start == std::string::npos ||
      line.find(marker, start + marker.size()) != std::string::npos) return false;
  const size_t valueStart = start + marker.size();
  size_t valueEnd = valueStart;
  while (valueEnd < line.size() && line[valueEnd] >= '0' && line[valueEnd] <= '9')
    ++valueEnd;
  if (valueEnd == valueStart || valueEnd >= line.size() ||
      (line[valueEnd] != ',' && line[valueEnd] != '}')) return false;
  return line.substr(valueStart, valueEnd - valueStart) == std::to_string(expected);
}

bool JsonStringFieldEqualsUnique(const std::string &line, const char *key,
                                 const std::string &expected) {
  const std::string marker = std::string("\"") + key + "\":\"";
  const size_t start = line.find(marker);
  return start != std::string::npos &&
      line.find(marker, start + marker.size()) == std::string::npos &&
      JsonStringField(line, key) == expected;
}

bool JsonUnsignedField(const std::string &line, const char *key,
                       uint64_t *value) {
  const std::string marker = std::string("\"") + key + "\":";
  const size_t start = line.find(marker);
  if (start == std::string::npos ||
      line.find(marker, start + marker.size()) != std::string::npos) return false;
  const size_t valueStart = start + marker.size();
  size_t valueEnd = valueStart;
  while (valueEnd < line.size() && line[valueEnd] >= '0' && line[valueEnd] <= '9')
    ++valueEnd;
  if (valueEnd == valueStart || valueEnd >= line.size() ||
      (line[valueEnd] != ',' && line[valueEnd] != '}')) return false;
  char *end = nullptr;
  const unsigned long long parsed = std::strtoull(line.c_str() + valueStart, &end, 10);
  if (end != line.c_str() + valueEnd) return false;
  *value = static_cast<uint64_t>(parsed);
  return true;
}

bool JsonBooleanFieldEqualsUnique(const std::string &line, const char *key,
                                  bool expected) {
  const std::string marker = std::string("\"") + key + "\":";
  const size_t start = line.find(marker);
  if (start == std::string::npos ||
      line.find(marker, start + marker.size()) != std::string::npos) return false;
  const size_t valueStart = start + marker.size();
  const std::string expectedText = expected ? "true" : "false";
  return line.compare(valueStart, expectedText.size(), expectedText) == 0
      && valueStart + expectedText.size() < line.size()
      && (line[valueStart + expectedText.size()] == ','
          || line[valueStart + expectedText.size()] == '}');
}

bool ReadStartGateSignal(std::string *contents) {
  const DWORD attributes = GetFileAttributesA(gConfig.publicWindowStartGatePath);
  if (attributes == INVALID_FILE_ATTRIBUTES) {
    const DWORD error = GetLastError();
    if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) return false;
    SetFatal("public window start gate signal could not be inspected", 4);
    return false;
  }
  if ((attributes & FILE_ATTRIBUTE_DIRECTORY) != 0
      || (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
    SetFatal("public window start gate signal is not a regular file", 4);
    return false;
  }
  HANDLE file = CreateFileA(gConfig.publicWindowStartGatePath, GENERIC_READ,
      FILE_SHARE_READ, nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file == INVALID_HANDLE_VALUE) {
    SetFatal("public window start gate signal could not be opened", 4);
    return false;
  }
  LARGE_INTEGER size{};
  bool valid = GetFileSizeEx(file, &size) != 0 && size.QuadPart > 0
      && size.QuadPart <= 64 * 1024;
  if (valid) {
    contents->resize(static_cast<size_t>(size.QuadPart));
    DWORD readBytes = 0;
    valid = ReadFile(file, contents->data(), static_cast<DWORD>(contents->size()),
                     &readBytes, nullptr) != 0
        && readBytes == contents->size();
  }
  CloseHandle(file);
  if (!valid) SetFatal("public window start gate signal is unreadable or oversized", 4);
  return valid;
}

bool WaitForPublicWindowStartGate() {
  if (!gConfig.publicWindowStartGateRequired) return true;
  if (gConfig.publicWindowStartGatePath[0] == '\0'
      || gConfig.experimentRunID[0] == '\0'
      || gConfig.targetUnixPid == 0
      || gConfig.experimentToken[0] == '\0'
      || gConfig.qpcFrequency == 0) {
    SetFatal("public window start gate identity is incomplete", 4);
    return false;
  }
  gConfig.publicWindowStartGateWaitBeginQpcTicks = QpcNow();
  const uint64_t timeoutTicks = kPublicWindowStartGateTimeoutSeconds * gConfig.qpcFrequency;
  while (!gQuitRequested) {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
      if (message.message == WM_QUIT) gQuitRequested = true;
      TranslateMessage(&message);
      DispatchMessageW(&message);
    }
    if (gQuitRequested) break;
    std::string signal;
    if (ReadStartGateSignal(&signal)) {
      uint64_t stableStart = 0;
      uint64_t attainedAt = 0;
      uint64_t stableDuration = 0;
      uint64_t sampleCount = 0;
      uint64_t targetWindowID = 0;
      uint64_t releasedAt = 0;
      const bool identityAndStateValid =
          JsonStringFieldEqualsUnique(signal, "status", "CANONICAL_UNCOVERED_ATTAINED")
          && JsonStringFieldEqualsUnique(signal, "run_id", gConfig.experimentRunID)
          && JsonUnsignedFieldEquals(signal, "target_unix_pid", gConfig.targetUnixPid)
          && JsonStringFieldEqualsUnique(signal, "experiment_token", gConfig.experimentToken)
          && JsonBooleanFieldEqualsUnique(signal, "canonical_uncovered_attained", true)
          && JsonBooleanFieldEqualsUnique(signal, "title_matched", true)
          && JsonBooleanFieldEqualsUnique(signal, "target_process_identity_matches", true)
          && JsonBooleanFieldEqualsUnique(signal, "target_visible", true)
          && JsonBooleanFieldEqualsUnique(signal, "window_unoccluded", true)
          && JsonBooleanFieldEqualsUnique(signal, "app_active", true)
          && JsonBooleanFieldEqualsUnique(signal, "telemetry_progress_required", true)
          && JsonBooleanFieldEqualsUnique(signal, "telemetry_progress_verified", true)
          && JsonUnsignedField(signal, "stable_start_monotonic_ns", &stableStart)
          && JsonUnsignedField(signal, "stability_proven_monotonic_ns", &attainedAt)
          && JsonUnsignedField(signal, "stable_duration_ns", &stableDuration)
          && JsonUnsignedField(signal, "sample_count", &sampleCount)
          && JsonUnsignedField(signal, "target_window_id", &targetWindowID)
          && JsonUnsignedField(signal, "released_monotonic_ns", &releasedAt)
          && JsonUnsignedFieldEquals(signal, "neutral_coverer_process_count", 0)
          && JsonUnsignedFieldEquals(signal, "neutral_coverer_window_count", 0)
          && attainedAt >= stableStart
          && attainedAt - stableStart >= 2'000'000'000ULL
          && stableDuration >= 2'000'000'000ULL
          && sampleCount >= 3
          && targetWindowID > 0
          && releasedAt >= attainedAt;
      if (!identityAndStateValid) {
        SetFatal("public window start gate signal identity or state is invalid", 4);
        return false;
      }
      gConfig.publicWindowStartGateReleasedMonotonicNS = releasedAt;
      gConfig.publicWindowStartGatePassedQpcTicks = QpcNow();
      gConfig.publicWindowStartGatePassed = true;
      return true;
    }
    if (gExitCode != 0) return false;
    const uint64_t now = QpcNow();
    if (now - gConfig.publicWindowStartGateWaitBeginQpcTicks >= timeoutTicks) {
      SetFatal("public window start gate safety timeout expired", 4);
      return false;
    }
    Sleep(25);
  }
  SetFatal("public window start gate was interrupted before release", 4);
  return false;
}

bool HostTraceOwnershipMatches(const std::string &contents,
                               uint64_t targetUnixPid,
                               const std::string &experimentToken) {
  if (contents.empty() || contents.back() != '\n' || targetUnixPid == 0 ||
      experimentToken.size() != 32) return false;
  size_t lineStart = 0;
  size_t lineCount = 0;
  while (lineStart < contents.size()) {
    const size_t lineEnd = contents.find('\n', lineStart);
    if (lineEnd == std::string::npos) return false;
    const std::string line = contents.substr(lineStart, lineEnd - lineStart);
    if (!line.empty()) {
      if (!JsonUnsignedFieldEquals(line, "host_pid", targetUnixPid) ||
          !JsonUnsignedFieldEquals(line, "target_unix_pid", targetUnixPid) ||
          !JsonStringFieldEqualsUnique(line, "experiment_token", experimentToken) ||
          !JsonStringFieldEqualsUnique(line, "trace_role", "target")) return false;
      ++lineCount;
    }
    lineStart = lineEnd + 1;
  }
  return lineCount > 0;
}

std::string NormalizePathSeparators(std::string path) {
  std::replace(path.begin(), path.end(), '\\', '/');
  return path;
}

size_t JsonEventCount(const std::string &contents, const char *eventName) {
  const std::string marker = std::string("\"event\":\"") + eventName + "\"";
  size_t matches = 0;
  size_t offset = 0;
  while ((offset = contents.find(marker, offset)) != std::string::npos) {
    ++matches;
    offset += marker.size();
  }
  return matches;
}

std::string JsonEventLine(const std::string &contents, const char *eventName) {
  const std::string marker = std::string("\"event\":\"") + eventName + "\"";
  const size_t eventAt = contents.find(marker);
  if (eventAt == std::string::npos) return {};
  const size_t lineStart = contents.rfind('\n', eventAt);
  const size_t lineEnd = contents.find('\n', eventAt);
  const size_t begin = lineStart == std::string::npos ? 0 : lineStart + 1;
  const size_t end = lineEnd == std::string::npos ? contents.size() : lineEnd;
  return contents.substr(begin, end - begin);
}

bool HasStartupModule(const std::string &contents, const char *role,
                      const char *pathEnvironment, const char *hashEnvironment,
                      const std::string &startupId) {
  const std::string roleNeedle = std::string("\"role\":\"") + role + "\"";
  const std::string path = EnvironmentString(pathEnvironment);
  const std::string hash = EnvironmentString(hashEnvironment);
  if (path.empty() || hash.size() != 64) return false;
  const std::string pathNeedle = std::string("\"path\":\"") + path + "\"";
  const std::string hashNeedle = std::string("\"sha256\":\"") + hash + "\"";
  const std::string kind = std::strcmp(role, "winevulkan_dll") == 0
      ? "win32-hmodule-proof-in-paired-app-trace"
      : std::strcmp(role, "wine_launcher") == 0
          ? "prefixlease-spawn-executable"
          : std::strcmp(role, "wineserver") == 0
              ? "prefixlease-bound-executable"
              : std::strcmp(role, "wine") == 0
                  ? "dyld-main-executable" : "dyld-loaded-image";
  const std::string kindNeedle = std::string("\"identity_kind\":\"") + kind + "\"";
  const bool externalBinding = std::strcmp(role, "wineserver") == 0 ||
      std::strcmp(role, "wine_launcher") == 0;
  const char *loadedNeedle = externalBinding
      ? "\"loaded_in_process\":false" : "\"loaded_in_process\":true";
  const std::string observedPathNeedle = std::string("\"observed_path\":\"") + path + "\"";
  const std::string observedHashNeedle = std::string("\"observed_sha256\":\"") + hash + "\"";
  size_t roleMatches = 0;
  size_t exactMatches = 0;
  size_t offset = 0;
  while ((offset = contents.find("\"event\":\"startup_module_identity\"", offset))
         != std::string::npos) {
    const size_t lineStart = contents.rfind('\n', offset);
    const size_t lineEnd = contents.find('\n', offset);
    const std::string line = contents.substr(
        lineStart == std::string::npos ? 0 : lineStart + 1,
        (lineEnd == std::string::npos ? contents.size() : lineEnd) -
            (lineStart == std::string::npos ? 0 : lineStart + 1));
    if (line.find(roleNeedle) != std::string::npos) {
      ++roleMatches;
      if (line.find(pathNeedle) != std::string::npos &&
          line.find(hashNeedle) != std::string::npos &&
          line.find(observedPathNeedle) != std::string::npos &&
          line.find(observedHashNeedle) != std::string::npos &&
          line.find("\"identity_match\":true") != std::string::npos &&
          line.find(kindNeedle) != std::string::npos &&
          line.find(loadedNeedle) != std::string::npos &&
          line.find("\"startup_phase\":true") != std::string::npos &&
          JsonStringField(line, "startup_id") == startupId) {
        ++exactMatches;
      }
    }
    offset = lineEnd == std::string::npos ? contents.size() : lineEnd + 1;
  }
  return roleMatches == 1 && exactMatches == 1;
}

bool WaitForHostRuntimeStartupIdentity() {
  HMODULE winevulkan = GetModuleHandleA("winevulkan.dll");
  if (winevulkan == nullptr) {
    SetFatal("builtin winevulkan.dll is not loaded before the frame loop", 4);
    return false;
  }
  char modulePath[MAX_PATH * 4]{};
  const DWORD modulePathLength = GetModuleFileNameA(
      winevulkan, modulePath, static_cast<DWORD>(sizeof(modulePath)));
  if (modulePathLength == 0 || modulePathLength >= sizeof(modulePath)) {
    SetFatal("GetModuleFileNameA(winevulkan.dll) failed before the frame loop", 4);
    return false;
  }
  std::snprintf(gConfig.winevulkanDllModulePath,
                sizeof(gConfig.winevulkanDllModulePath), "%s", modulePath);
  std::string modulePathLower = NormalizePathSeparators(modulePath);
  std::transform(modulePathLower.begin(), modulePathLower.end(),
                 modulePathLower.begin(), [](unsigned char c) {
                   return static_cast<char>(std::tolower(c));
                 });
  const std::string prefixPath = NormalizePathSeparators(
      EnvironmentString("WINEPREFIX"));
  const std::string hostDllPath = NormalizePathSeparators(
      EnvironmentString("FG_B_WINEVULKAN_DLL_PATH"));
  const std::string hostDllSha = EnvironmentString("FG_B_WINEVULKAN_DLL_SHA256");
  std::string expectedHostDllPath = prefixPath;
  while (expectedHostDllPath.size() > 1 && expectedHostDllPath.back() == '/')
    expectedHostDllPath.pop_back();
  expectedHostDllPath += "/drive_c/windows/system32/winevulkan.dll";
  const bool validDllSha = hostDllSha.size() == 64 &&
      std::all_of(hostDllSha.begin(), hostDllSha.end(), [](unsigned char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
      });
  if (modulePathLower != "c:/windows/system32/winevulkan.dll" ||
      prefixPath.empty() || prefixPath[0] != '/' || hostDllPath != expectedHostDllPath ||
      !validDllSha) {
    SetFatal("loaded winevulkan.dll does not bind to the exact PrefixLease DLL path and SHA-256", 4);
    return false;
  }
  std::snprintf(gConfig.winevulkanDllHostPath,
                sizeof(gConfig.winevulkanDllHostPath), "%s", hostDllPath.c_str());
  std::snprintf(gConfig.winevulkanDllSha256,
                sizeof(gConfig.winevulkanDllSha256), "%s", hostDllSha.c_str());
  gConfig.winevulkanDllModuleLoaded = true;

  const std::string hostTracePath(gConfig.hostTracePath);
  const std::string experimentToken(gConfig.experimentToken);
  if (hostTracePath.empty() || gConfig.targetUnixPid == 0 ||
      experimentToken.size() != 32) {
    SetFatal("app trace path cannot resolve the paired host trace", 4);
    return false;
  }
  const std::string expectedWinePath = EnvironmentString("FG_B_EXPECTED_WINE_PATH");
  const std::string expectedWineSha = EnvironmentString("FG_B_EXPECTED_WINE_SHA256");
  const std::string expectedWineLauncherPath = EnvironmentString("FG_B_WINE_LAUNCHER_PATH");
  const std::string expectedWineLauncherSha = EnvironmentString("FG_B_WINE_LAUNCHER_SHA256");
  const std::string expectedProxyPath = EnvironmentString("FG_B_TRACE_PROXY_PATH");
  const std::string expectedProxySha = EnvironmentString("FG_B_TRACE_PROXY_SHA256");
  const std::string expectedMoltenVKPath =
      EnvironmentString("FG_B_ORIGINAL_MOLTENVK_LIBRARY");
  const std::string expectedMoltenVKSha =
      EnvironmentString("FG_B_ORIGINAL_MOLTENVK_SHA256");
  const std::string expectedHostLoaderPath = EnvironmentString("FG_B_HOST_LOADER_PATH");
  const std::string expectedHostLoaderSha = EnvironmentString("FG_B_HOST_LOADER_SHA256");
  const std::string expectedWinemacPath = EnvironmentString("FG_B_WINEMAC_PATH");
  const std::string expectedWinemacSha = EnvironmentString("FG_B_WINEMAC_SHA256");
  const ULONGLONG deadline = GetTickCount64() + 15000;
  bool identityReady = false;
  while (GetTickCount64() < deadline) {
    HANDLE trace = CreateFileA(hostTracePath.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (trace != INVALID_HANDLE_VALUE) {
      LARGE_INTEGER size{};
      if (GetFileSizeEx(trace, &size) && size.QuadPart > 0 &&
          size.QuadPart <= 1024 * 1024) {
        std::string contents(static_cast<size_t>(size.QuadPart), '\0');
        DWORD bytesRead = 0;
        if (ReadFile(trace, contents.data(), static_cast<DWORD>(contents.size()),
                     &bytesRead, nullptr)) {
          contents.resize(bytesRead);
          const bool ownershipMatches = HostTraceOwnershipMatches(
              contents, gConfig.targetUnixPid, experimentToken);
          const bool uniqueStartupRows =
              JsonEventCount(contents, "process_identity") == 1 &&
              JsonEventCount(contents, "vulkan_proxy_identity") == 1 &&
              JsonEventCount(contents, "host_runtime_startup") == 1 &&
              JsonEventCount(contents, "layer_configuration") == 1 &&
              JsonEventCount(contents, "clock_metadata") == 1;
          const std::string processLine = JsonEventLine(contents, "process_identity");
          const std::string proxyLine = JsonEventLine(contents, "vulkan_proxy_identity");
          const std::string startupLine = JsonEventLine(contents, "host_runtime_startup");
          const std::string layerLine = JsonEventLine(contents, "layer_configuration");
          const std::string clockLine = JsonEventLine(contents, "clock_metadata");
          const std::string startupId = JsonStringField(processLine, "startup_id");
          const bool processMatches =
              !startupId.empty() &&
              JsonUnsignedFieldEquals(processLine, "host_pid", gConfig.targetUnixPid) &&
              JsonUnsignedFieldEquals(processLine, "target_unix_pid", gConfig.targetUnixPid) &&
              JsonStringFieldEqualsUnique(processLine, "experiment_token", experimentToken) &&
              JsonStringFieldEqualsUnique(processLine, "trace_role", "target") &&
              JsonStringField(proxyLine, "startup_id") == startupId &&
              JsonStringField(startupLine, "startup_id") == startupId &&
              HasJsonString(processLine, "main_executable_path", expectedWinePath) &&
              HasJsonString(processLine, "main_executable_sha256", expectedWineSha) &&
              HasJsonString(processLine, "wine_launcher_path", expectedWineLauncherPath) &&
              HasJsonString(processLine, "wine_launcher_sha256", expectedWineLauncherSha) &&
              processLine.find("\"process_architecture\":\"x86_64\"") != std::string::npos &&
              processLine.find("\"process_translated\":1") != std::string::npos;
          const bool proxyMatches =
              HasJsonString(proxyLine, "trace_proxy_path", expectedProxyPath) &&
              HasJsonString(proxyLine, "trace_proxy_sha256", expectedProxySha) &&
              HasJsonString(proxyLine, "loaded_proxy_path", expectedProxyPath) &&
              HasJsonString(proxyLine, "loaded_proxy_sha256", expectedProxySha) &&
              HasJsonString(proxyLine, "original_vulkan_library", expectedMoltenVKPath) &&
              HasJsonString(proxyLine, "original_vulkan_library_sha256", expectedMoltenVKSha) &&
              HasJsonString(proxyLine, "loaded_delegate_path", expectedMoltenVKPath) &&
              HasJsonString(proxyLine, "loaded_delegate_sha256", expectedMoltenVKSha) &&
              proxyLine.find("\"real_gipa_available\":true") != std::string::npos &&
              proxyLine.find("\"metal_surface_dlsym_available\":true") != std::string::npos;
          const bool startupMatches =
              startupLine.find("\"startup_valid\":true") != std::string::npos &&
              startupLine.find("\"host_loader_role\":\"win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan\"") !=
                  std::string::npos &&
              startupLine.find("\"process_architecture\":\"x86_64\"") != std::string::npos &&
              startupLine.find("\"process_translated\":1") != std::string::npos &&
              HasJsonString(startupLine, "moltenvk_path", expectedMoltenVKPath) &&
              HasJsonString(startupLine, "moltenvk_sha256", expectedMoltenVKSha) &&
              HasJsonString(startupLine, "trace_proxy_path", expectedProxyPath) &&
              HasJsonString(startupLine, "trace_proxy_sha256", expectedProxySha) &&
              HasJsonString(startupLine, "winevulkan_so_path",
                            EnvironmentString("FG_B_WINEVULKAN_SO_PATH")) &&
              HasJsonString(startupLine, "winevulkan_so_sha256",
                            EnvironmentString("FG_B_WINEVULKAN_SO_SHA256")) &&
              HasJsonString(startupLine, "wine_host_loader_path",
                            EnvironmentString("FG_B_HOST_LOADER_PATH")) &&
              HasJsonString(startupLine, "wine_host_loader_sha256",
                            EnvironmentString("FG_B_HOST_LOADER_SHA256")) &&
              HasJsonString(startupLine, "moltenvk_version", "MoltenVK v1.4.2") &&
              HasJsonString(startupLine, "moltenvk_version_source",
                            "vkGetVersionStringsMVK from loaded delegate") &&
              HasJsonString(startupLine, "vulkan_runtime_version", "1.4.357") &&
              startupLine.find("\"loaded_moltenvk_count\":1") != std::string::npos &&
              startupLine.find("\"loaded_proxy_count\":1") != std::string::npos &&
              startupLine.find("\"loaded_winevulkan_count\":1") != std::string::npos &&
              startupLine.find("\"loaded_host_loader_count\":1") != std::string::npos &&
              startupLine.find("\"loaded_libvulkan_dylib_count\":0") != std::string::npos;
          const bool layerMatches =
              HasJsonString(layerLine, "layer_class_image_path", expectedWinemacPath) &&
              HasJsonString(layerLine, "layer_class_image_sha256", expectedWinemacSha) &&
              layerLine.find("\"layer_class\":\"WineMetalLayer\"") != std::string::npos &&
              layerLine.find("\"layer_is_cametal_layer\":true") != std::string::npos;
          const bool modulesMatch =
              HasStartupModule(contents, "wine", "FG_B_EXPECTED_WINE_PATH",
                               "FG_B_EXPECTED_WINE_SHA256", startupId) &&
              HasStartupModule(contents, "wine_launcher", "FG_B_WINE_LAUNCHER_PATH",
                               "FG_B_WINE_LAUNCHER_SHA256", startupId) &&
              HasStartupModule(contents, "winevulkan_dll", "FG_B_WINEVULKAN_DLL_PATH",
                               "FG_B_WINEVULKAN_DLL_SHA256", startupId) &&
              HasStartupModule(contents, "winevulkan_so", "FG_B_WINEVULKAN_SO_PATH",
                               "FG_B_WINEVULKAN_SO_SHA256", startupId) &&
              HasStartupModule(contents, "wine_host_loader", "FG_B_HOST_LOADER_PATH",
                               "FG_B_HOST_LOADER_SHA256", startupId) &&
              HasStartupModule(contents, "winemac_so", "FG_B_WINEMAC_PATH",
                               "FG_B_WINEMAC_SHA256", startupId) &&
              HasStartupModule(contents, "trace_proxy", "FG_B_TRACE_PROXY_PATH",
                               "FG_B_TRACE_PROXY_SHA256", startupId) &&
              HasStartupModule(contents, "moltenvk", "FG_B_ORIGINAL_MOLTENVK_LIBRARY",
                               "FG_B_ORIGINAL_MOLTENVK_SHA256", startupId) &&
              HasStartupModule(contents, "wineserver", "FG_B_WINESERVER_PATH",
                               "FG_B_WINESERVER_SHA256", startupId);
          const bool clockMatches =
              clockLine.find("\"event_clock_source\":\"mach_absolute_time\"") !=
                  std::string::npos &&
              clockLine.find("\"event_clock_domain\":\"mach_absolute_time_ns\"") !=
                  std::string::npos &&
              clockLine.find("\"mach_timebase_numer\":") != std::string::npos &&
              clockLine.find("\"mach_timebase_denom\":") != std::string::npos &&
              clockLine.find("\"presented_time_basis\":\"mach_absolute_time_seconds\"") !=
                  std::string::npos &&
              clockLine.find("\"presented_time_clock_domain\":\"mach_absolute_time_ns\"") !=
                  std::string::npos &&
              clockLine.find("\"callback_delivery_clock_domain\":\"mach_absolute_time_ns\"") !=
                  std::string::npos;
          identityReady = ownershipMatches && uniqueStartupRows && processMatches && proxyMatches && startupMatches &&
                          layerMatches && modulesMatch && clockMatches;
        }
      }
      CloseHandle(trace);
    }
    if (identityReady) break;
    Sleep(25);
  }
  if (!identityReady) {
    SetFatal("host runtime startup identity gate failed before swapchain creation", 4);
    return false;
  }
  gConfig.hostRuntimeStartupGatePassed = true;
  gConfig.winevulkanDllBeforeFrameLoop = true;
  return true;
}

bool SelectPhysicalDeviceAndCreateLogicalDevice() {
  uint32_t physicalCount = 0;
  VkResult result = gVk.enumeratePhysicalDevices(gVk.instance, &physicalCount, nullptr);
  if (result != VK_SUCCESS || physicalCount == 0) {
    SetFatal("no Vulkan physical device was enumerated", 5);
    return false;
  }
  std::vector<VkPhysicalDevice> physicalDevices(physicalCount);
  result = gVk.enumeratePhysicalDevices(gVk.instance, &physicalCount, physicalDevices.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkEnumeratePhysicalDevices(list) failed", 5);
    return false;
  }

  for (VkPhysicalDevice candidate : physicalDevices) {
    VkPhysicalDeviceProperties props{};
    gVk.getPhysicalDeviceProperties(candidate, &props);
    uint32_t queueCount = 0;
    gVk.getQueueFamilyProperties(candidate, &queueCount, nullptr);
    std::vector<VkQueueFamilyProperties> queues(queueCount);
    gVk.getQueueFamilyProperties(candidate, &queueCount, queues.data());
    for (uint32_t family = 0; family < queueCount; ++family) {
      if ((queues[family].queueFlags & VK_QUEUE_GRAPHICS_BIT) == 0 ||
          queues[family].queueCount == 0) continue;
      VkBool32 supportsPresent = VK_FALSE;
      result = gVk.getSurfaceSupport(candidate, family, gVk.surface,
                                     &supportsPresent);
      if (result == VK_SUCCESS && supportsPresent) {
        gVk.physicalDevice = candidate;
        gVk.graphicsQueueFamily = family;
        gConfig.physicalApiVersion = props.apiVersion;
        std::snprintf(gConfig.physicalDeviceName, sizeof(gConfig.physicalDeviceName),
                      "%s", props.deviceName);
        break;
      }
    }
    if (gVk.physicalDevice != VK_NULL_HANDLE) break;
  }
  if (gVk.physicalDevice == VK_NULL_HANDLE) {
    SetFatal("no physical device has one graphics and presentation queue family", 5);
    return false;
  }

  const bool hasSwapchain = HasDeviceExtension(gVk.physicalDevice,
      gVk.enumerateDeviceExtensions, VK_KHR_SWAPCHAIN_EXTENSION_NAME);
  if (!hasSwapchain) {
    SetFatal("VK_KHR_swapchain is unavailable", 5);
    return false;
  }
  const bool hasPortabilitySubset = HasDeviceExtension(gVk.physicalDevice,
      gVk.enumerateDeviceExtensions, kPortabilitySubsetExtension);
  const bool hasSync2Extension = HasDeviceExtension(gVk.physicalDevice,
      gVk.enumerateDeviceExtensions, VK_KHR_SYNCHRONIZATION_2_EXTENSION_NAME);
  const bool sync2Core = VK_API_VERSION_MAJOR(gConfig.physicalApiVersion) > 1 ||
      (VK_API_VERSION_MAJOR(gConfig.physicalApiVersion) == 1 &&
       VK_API_VERSION_MINOR(gConfig.physicalApiVersion) >= 3);
  if (!sync2Core && !hasSync2Extension) {
    SetFatal("synchronization2 is unavailable; vkQueueSubmit2 cannot be used", 5);
    return false;
  }

  gVk.getPhysicalDeviceFeatures2 = reinterpret_cast<PFN_vkGetPhysicalDeviceFeatures2>(
      gVk.getInstanceProcAddr(gVk.instance, "vkGetPhysicalDeviceFeatures2"));
  if (gVk.getPhysicalDeviceFeatures2 == nullptr) {
    gVk.getPhysicalDeviceFeatures2 = reinterpret_cast<PFN_vkGetPhysicalDeviceFeatures2>(
        gVk.getInstanceProcAddr(gVk.instance, "vkGetPhysicalDeviceFeatures2KHR"));
  }
  if (gVk.getPhysicalDeviceFeatures2 == nullptr) {
    SetFatal("vkGetPhysicalDeviceFeatures2 is unavailable", 5);
    return false;
  }
  VkPhysicalDeviceSynchronization2Features sync2Available{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES};
  VkPhysicalDeviceFeatures2 available{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};
  available.pNext = &sync2Available;
  gVk.getPhysicalDeviceFeatures2(gVk.physicalDevice, &available);
  if (sync2Available.synchronization2 != VK_TRUE) {
    SetFatal("physical device does not support synchronization2", 5);
    return false;
  }

  float priority = 1.0f;
  VkDeviceQueueCreateInfo queueInfo{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
  queueInfo.queueFamilyIndex = gVk.graphicsQueueFamily;
  queueInfo.queueCount = 1;
  queueInfo.pQueuePriorities = &priority;
  std::vector<const char *> deviceExtensions = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
  if (hasPortabilitySubset) deviceExtensions.push_back(kPortabilitySubsetExtension);
  if (!sync2Core) deviceExtensions.push_back(VK_KHR_SYNCHRONIZATION_2_EXTENSION_NAME);
  VkPhysicalDeviceSynchronization2Features sync2Enabled{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES};
  sync2Enabled.synchronization2 = VK_TRUE;
  VkDeviceCreateInfo deviceInfo{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
  deviceInfo.pNext = &sync2Enabled;
  deviceInfo.queueCreateInfoCount = 1;
  deviceInfo.pQueueCreateInfos = &queueInfo;
  deviceInfo.enabledExtensionCount = static_cast<uint32_t>(deviceExtensions.size());
  deviceInfo.ppEnabledExtensionNames = deviceExtensions.data();
  result = gVk.createDevice(gVk.physicalDevice, &deviceInfo, nullptr, &gVk.device);
  if (result != VK_SUCCESS) {
    SetFatal("vkCreateDevice failed", 5);
    return false;
  }
  gConfig.synchronization2Enabled = true;
  gConfig.deviceId = HandleValue(gVk.device);
  gVk.getDeviceProcAddr = reinterpret_cast<PFN_vkGetDeviceProcAddr>(
      gVk.getInstanceProcAddr(gVk.instance, "vkGetDeviceProcAddr"));
  if (gVk.getDeviceProcAddr == nullptr) {
    SetFatal("vkGetDeviceProcAddr is unavailable", 5);
    return false;
  }

  if (!LoadDeviceProc(gVk.destroyDevice, "vkDestroyDevice") ||
      !LoadDeviceProc(gVk.getDeviceQueue, "vkGetDeviceQueue") ||
      !LoadDeviceProc(gVk.deviceWaitIdle, "vkDeviceWaitIdle") ||
      !LoadDeviceProc(gVk.queueWaitIdle, "vkQueueWaitIdle") ||
      !LoadDeviceProc(gVk.createSwapchain, "vkCreateSwapchainKHR") ||
      !LoadDeviceProc(gVk.destroySwapchain, "vkDestroySwapchainKHR") ||
      !LoadDeviceProc(gVk.getSwapchainImages, "vkGetSwapchainImagesKHR") ||
      !LoadDeviceProc(gVk.acquireNextImage, "vkAcquireNextImageKHR") ||
      !LoadDeviceProc(gVk.queuePresent, "vkQueuePresentKHR") ||
      !LoadDeviceProc(gVk.createRenderPass, "vkCreateRenderPass") ||
      !LoadDeviceProc(gVk.destroyRenderPass, "vkDestroyRenderPass") ||
      !LoadDeviceProc(gVk.createImageView, "vkCreateImageView") ||
      !LoadDeviceProc(gVk.destroyImageView, "vkDestroyImageView") ||
      !LoadDeviceProc(gVk.createFramebuffer, "vkCreateFramebuffer") ||
      !LoadDeviceProc(gVk.destroyFramebuffer, "vkDestroyFramebuffer") ||
      !LoadDeviceProc(gVk.createCommandPool, "vkCreateCommandPool") ||
      !LoadDeviceProc(gVk.destroyCommandPool, "vkDestroyCommandPool") ||
      !LoadDeviceProc(gVk.allocateCommandBuffers, "vkAllocateCommandBuffers") ||
      !LoadDeviceProc(gVk.resetCommandBuffer, "vkResetCommandBuffer") ||
      !LoadDeviceProc(gVk.beginCommandBuffer, "vkBeginCommandBuffer") ||
      !LoadDeviceProc(gVk.endCommandBuffer, "vkEndCommandBuffer") ||
      !LoadDeviceProc(gVk.createSemaphore, "vkCreateSemaphore") ||
      !LoadDeviceProc(gVk.destroySemaphore, "vkDestroySemaphore") ||
      !LoadDeviceProc(gVk.createFence, "vkCreateFence") ||
      !LoadDeviceProc(gVk.destroyFence, "vkDestroyFence") ||
      !LoadDeviceProc(gVk.waitForFences, "vkWaitForFences") ||
      !LoadDeviceProc(gVk.resetFences, "vkResetFences")) {
    return false;
  }
  gVk.queueSubmit2 = reinterpret_cast<PFN_vkQueueSubmit2>(
      gVk.getDeviceProcAddr(gVk.device, "vkQueueSubmit2"));
  if (gVk.queueSubmit2 == nullptr && !sync2Core) {
    gVk.queueSubmit2 = reinterpret_cast<PFN_vkQueueSubmit2>(
        gVk.getDeviceProcAddr(gVk.device, "vkQueueSubmit2KHR"));
    if (gVk.queueSubmit2 != nullptr) {
      std::snprintf(gConfig.submit2Entrypoint, sizeof(gConfig.submit2Entrypoint),
                    "%s", "vkQueueSubmit2KHR");
    }
  }
  if (gVk.queueSubmit2 == nullptr) {
    SetFatal("vkQueueSubmit2 entrypoint is unavailable", 5);
    return false;
  }
  gVk.cmdBeginRenderPass = reinterpret_cast<PFN_vkCmdBeginRenderPass>(
      gVk.getDeviceProcAddr(gVk.device, "vkCmdBeginRenderPass"));
  gVk.cmdEndRenderPass = reinterpret_cast<PFN_vkCmdEndRenderPass>(
      gVk.getDeviceProcAddr(gVk.device, "vkCmdEndRenderPass"));
  if (gVk.cmdBeginRenderPass == nullptr || gVk.cmdEndRenderPass == nullptr) {
    SetFatal("render pass command procedures are unavailable", 5);
    return false;
  }
  gVk.getDeviceQueue(gVk.device, gVk.graphicsQueueFamily, 0, &gVk.queue);
  if (gVk.queue == VK_NULL_HANDLE) {
    SetFatal("vkGetDeviceQueue returned a null queue", 5);
    return false;
  }
  gConfig.queueId = HandleValue(gVk.queue);
  gConfig.queueFamilyIndex = gVk.graphicsQueueFamily;
  return true;
}

bool CreateSwapchainAndRenderResources() {
  VkSurfaceCapabilitiesKHR capabilities{};
  VkResult result = gVk.getSurfaceCapabilities(
      gVk.physicalDevice, gVk.surface, &capabilities);
  if (result != VK_SUCCESS) {
    SetFatal("vkGetPhysicalDeviceSurfaceCapabilitiesKHR failed", 6);
    return false;
  }
  if ((capabilities.supportedUsageFlags & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) == 0) {
    SetFatal("surface does not support color-attachment swapchain images", 6);
    return false;
  }
  gConfig.surfaceMinImageCount = capabilities.minImageCount;
  gConfig.surfaceMaxImageCount = capabilities.maxImageCount;
  gConfig.surfaceCurrentExtentWidth = capabilities.currentExtent.width;
  gConfig.surfaceCurrentExtentHeight = capabilities.currentExtent.height;
  if (capabilities.minImageCount > kRequestedImageCount ||
      (capabilities.maxImageCount != 0 &&
       capabilities.maxImageCount < kRequestedImageCount)) {
    SetFatal("surface image-count limits do not permit the requested three images", 6);
    return false;
  }

  uint32_t formatCount = 0;
  result = gVk.getSurfaceFormats(gVk.physicalDevice, gVk.surface,
                                               &formatCount, nullptr);
  if (result != VK_SUCCESS || formatCount == 0) {
    SetFatal("surface has no Vulkan formats", 6);
    return false;
  }
  std::vector<VkSurfaceFormatKHR> formats(formatCount);
  result = gVk.getSurfaceFormats(gVk.physicalDevice, gVk.surface,
                                               &formatCount, formats.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkGetPhysicalDeviceSurfaceFormatsKHR(list) failed", 6);
    return false;
  }
  formats.resize(formatCount);
  auto selectedFormat = std::find_if(formats.begin(), formats.end(), [](const auto &format) {
    return format.format == VK_FORMAT_B8G8R8A8_UNORM &&
           format.colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
  });
  if (selectedFormat == formats.end() && formats.size() == 1 &&
      formats[0].format == VK_FORMAT_UNDEFINED) {
    gVk.format = VK_FORMAT_B8G8R8A8_UNORM;
    gVk.colorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
  } else if (selectedFormat != formats.end()) {
    gVk.format = selectedFormat->format;
    gVk.colorSpace = selectedFormat->colorSpace;
  } else {
    SetFatal("BGRA8 UNORM with SRGB_NONLINEAR color space is unavailable", 6);
    return false;
  }

  uint32_t presentCount = 0;
  result = gVk.getSurfacePresentModes(gVk.physicalDevice, gVk.surface,
                                                   &presentCount, nullptr);
  if (result != VK_SUCCESS || presentCount == 0) {
    SetFatal("surface has no Vulkan present modes", 6);
    return false;
  }
  std::vector<VkPresentModeKHR> presentModes(presentCount);
  result = gVk.getSurfacePresentModes(gVk.physicalDevice, gVk.surface,
                                                   &presentCount, presentModes.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkGetPhysicalDeviceSurfacePresentModesKHR(list) failed", 6);
    return false;
  }
  if (std::find(presentModes.begin(), presentModes.end(), VK_PRESENT_MODE_FIFO_KHR) ==
      presentModes.end()) {
    SetFatal("VK_PRESENT_MODE_FIFO_KHR is unavailable", 6);
    return false;
  }

  VkExtent2D extent{};
  if (capabilities.currentExtent.width != UINT32_MAX) {
    if (capabilities.currentExtent.width != kWidth ||
        capabilities.currentExtent.height != kHeight) {
      SetFatal("surface fixed extent does not permit the requested 640x360 swapchain", 6);
      return false;
    }
    extent = {kWidth, kHeight};
  } else {
    if (kWidth < capabilities.minImageExtent.width ||
        kWidth > capabilities.maxImageExtent.width ||
        kHeight < capabilities.minImageExtent.height ||
        kHeight > capabilities.maxImageExtent.height) {
      SetFatal("surface extent limits do not permit the requested 640x360 swapchain", 6);
      return false;
    }
    extent = {kWidth, kHeight};
  }
  const uint32_t imageCount = kRequestedImageCount;
  VkCompositeAlphaFlagBitsKHR composite = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
  if ((capabilities.supportedCompositeAlpha & composite) == 0) {
    const std::array<VkCompositeAlphaFlagBitsKHR, 4> options = {
        VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR,
        VK_COMPOSITE_ALPHA_POST_MULTIPLIED_BIT_KHR,
        VK_COMPOSITE_ALPHA_INHERIT_BIT_KHR,
        VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR};
    auto found = std::find_if(options.begin(), options.end(), [&capabilities](auto value) {
      return (capabilities.supportedCompositeAlpha & value) != 0;
    });
    if (found == options.end()) {
      SetFatal("surface has no supported composite alpha mode", 6);
      return false;
    }
    composite = *found;
  }
  VkSwapchainCreateInfoKHR swapInfo{VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR};
  swapInfo.surface = gVk.surface;
  swapInfo.minImageCount = imageCount;
  swapInfo.imageFormat = gVk.format;
  swapInfo.imageColorSpace = gVk.colorSpace;
  swapInfo.imageExtent = extent;
  swapInfo.imageArrayLayers = 1;
  swapInfo.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
  swapInfo.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
  swapInfo.preTransform = capabilities.currentTransform;
  swapInfo.compositeAlpha = composite;
  swapInfo.presentMode = VK_PRESENT_MODE_FIFO_KHR;
  swapInfo.clipped = VK_TRUE;
  result = gVk.createSwapchain(gVk.device, &swapInfo, nullptr, &gVk.swapchain);
  if (result != VK_SUCCESS) {
    SetFatal("vkCreateSwapchainKHR failed", 6);
    return false;
  }
  gConfig.swapchainId = HandleValue(gVk.swapchain);
  gConfig.actualSwapchainWidth = extent.width;
  gConfig.actualSwapchainHeight = extent.height;
  gConfig.selectedFormat = static_cast<uint32_t>(gVk.format);
  gConfig.selectedColorSpace = static_cast<uint32_t>(gVk.colorSpace);
  gConfig.selectedPresentMode = static_cast<uint32_t>(VK_PRESENT_MODE_FIFO_KHR);
  gConfig.actualTransform = static_cast<uint32_t>(capabilities.currentTransform);
  gConfig.compositeAlpha = static_cast<uint32_t>(composite);

  uint32_t actualCount = 0;
  result = gVk.getSwapchainImages(gVk.device, gVk.swapchain, &actualCount, nullptr);
  if (result != VK_SUCCESS || actualCount == 0) {
    SetFatal("vkGetSwapchainImagesKHR(count) failed", 6);
    return false;
  }
  gVk.images.resize(actualCount);
  result = gVk.getSwapchainImages(gVk.device, gVk.swapchain, &actualCount, gVk.images.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkGetSwapchainImagesKHR(list) failed", 6);
    return false;
  }
  gVk.images.resize(actualCount);
  gConfig.actualImageCount = actualCount;

  VkAttachmentDescription attachment{};
  attachment.format = gVk.format;
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
  VkRenderPassCreateInfo renderInfo{VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO};
  renderInfo.attachmentCount = 1;
  renderInfo.pAttachments = &attachment;
  renderInfo.subpassCount = 1;
  renderInfo.pSubpasses = &subpass;
  renderInfo.dependencyCount = 1;
  renderInfo.pDependencies = &dependency;
  result = gVk.createRenderPass(gVk.device, &renderInfo, nullptr, &gVk.renderPass);
  if (result != VK_SUCCESS) {
    SetFatal("vkCreateRenderPass failed", 6);
    return false;
  }

  gVk.imageViews.resize(actualCount);
  gVk.framebuffers.resize(actualCount);
  for (uint32_t index = 0; index < actualCount; ++index) {
    VkImageViewCreateInfo viewInfo{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    viewInfo.image = gVk.images[index];
    viewInfo.viewType = VK_IMAGE_VIEW_TYPE_2D;
    viewInfo.format = gVk.format;
    viewInfo.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    viewInfo.subresourceRange.baseMipLevel = 0;
    viewInfo.subresourceRange.levelCount = 1;
    viewInfo.subresourceRange.baseArrayLayer = 0;
    viewInfo.subresourceRange.layerCount = 1;
    result = gVk.createImageView(gVk.device, &viewInfo, nullptr, &gVk.imageViews[index]);
    if (result != VK_SUCCESS) {
      SetFatal("vkCreateImageView failed", 6);
      return false;
    }
    VkFramebufferCreateInfo framebufferInfo{VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO};
    framebufferInfo.renderPass = gVk.renderPass;
    framebufferInfo.attachmentCount = 1;
    framebufferInfo.pAttachments = &gVk.imageViews[index];
    framebufferInfo.width = extent.width;
    framebufferInfo.height = extent.height;
    framebufferInfo.layers = 1;
    result = gVk.createFramebuffer(gVk.device, &framebufferInfo, nullptr,
                                  &gVk.framebuffers[index]);
    if (result != VK_SUCCESS) {
      SetFatal("vkCreateFramebuffer failed", 6);
      return false;
    }
  }

  VkCommandPoolCreateInfo poolInfo{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
  poolInfo.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
  poolInfo.queueFamilyIndex = gVk.graphicsQueueFamily;
  result = gVk.createCommandPool(gVk.device, &poolInfo, nullptr, &gVk.commandPool);
  if (result != VK_SUCCESS) {
    SetFatal("vkCreateCommandPool failed", 6);
    return false;
  }
  gVk.commandBuffers.resize(actualCount);
  VkCommandBufferAllocateInfo commandInfo{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
  commandInfo.commandPool = gVk.commandPool;
  commandInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
  commandInfo.commandBufferCount = actualCount;
  result = gVk.allocateCommandBuffers(gVk.device, &commandInfo, gVk.commandBuffers.data());
  if (result != VK_SUCCESS) {
    SetFatal("vkAllocateCommandBuffers failed", 6);
    return false;
  }
  return true;
}

bool CreateFrameSynchronization() {
  VkSemaphoreCreateInfo semaphoreInfo{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
  VkFenceCreateInfo fenceInfo{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
  fenceInfo.flags = VK_FENCE_CREATE_SIGNALED_BIT;
  for (uint32_t slot = 0; slot < kFramesInFlight; ++slot) {
    VkResult result = gVk.createSemaphore(gVk.device, &semaphoreInfo, nullptr,
                                          &gVk.imageAvailable[slot]);
    if (result != VK_SUCCESS) {
      SetFatal("vkCreateSemaphore(imageAvailable) failed", 7);
      return false;
    }
    result = gVk.createFence(gVk.device, &fenceInfo, nullptr, &gVk.frameFences[slot]);
    if (result != VK_SUCCESS) {
      SetFatal("vkCreateFence failed", 7);
      return false;
    }
  }
  gVk.renderFinished.resize(gVk.images.size());
  for (VkSemaphore &semaphore : gVk.renderFinished) {
    const VkResult result = gVk.createSemaphore(gVk.device, &semaphoreInfo, nullptr,
                                                &semaphore);
    if (result != VK_SUCCESS) {
      SetFatal("vkCreateSemaphore(renderFinished) failed", 7);
      return false;
    }
  }
  return true;
}

bool RecordClearCommand(uint32_t imageIndex, uint64_t frameId) {
  VkCommandBuffer command = gVk.commandBuffers[imageIndex];
  VkResult result = gVk.resetCommandBuffer(command, 0);
  if (result != VK_SUCCESS) {
    SetFatal("vkResetCommandBuffer failed", 8);
    return false;
  }
  VkCommandBufferBeginInfo beginInfo{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
  beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
  result = gVk.beginCommandBuffer(command, &beginInfo);
  if (result != VK_SUCCESS) {
    SetFatal("vkBeginCommandBuffer failed", 8);
    return false;
  }
  const double frame = static_cast<double>(frameId);
  VkClearValue clear{};
  clear.color.float32[0] = static_cast<float>(std::fmod(frame * 0.61803398875, 1.0));
  clear.color.float32[1] = static_cast<float>(std::fmod(frame * 0.41421356237, 1.0));
  clear.color.float32[2] = static_cast<float>(std::fmod(frame * 0.73205080757, 1.0));
  clear.color.float32[3] = 1.0f;
  VkRenderPassBeginInfo renderBegin{VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO};
  renderBegin.renderPass = gVk.renderPass;
  renderBegin.framebuffer = gVk.framebuffers[imageIndex];
  renderBegin.renderArea.offset = {0, 0};
  renderBegin.renderArea.extent = gVk.extent;
  renderBegin.clearValueCount = 1;
  renderBegin.pClearValues = &clear;
  gVk.cmdBeginRenderPass(command, &renderBegin, VK_SUBPASS_CONTENTS_INLINE);
  gVk.cmdEndRenderPass(command);
  result = gVk.endCommandBuffer(command);
  if (result != VK_SUCCESS) {
    SetFatal("vkEndCommandBuffer failed", 8);
    return false;
  }
  return true;
}

bool RenderFor(uint64_t seconds) {
  if (!CreateFrameSynchronization()) return false;
  const uint64_t start = QpcNow();
  const uint64_t runTicks = static_cast<uint64_t>(
      static_cast<long double>(seconds) * gConfig.qpcFrequency);
  uint64_t frameId = 0;
  uint64_t acquireSeq = 0;
  uint64_t submitSeq = 0;
  uint64_t presentSeq = 0;
  uint32_t frameSlot = 0;
  while (!gQuitRequested && QpcNow() - start < runTicks) {
    MSG message{};
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
      if (message.message == WM_QUIT) gQuitRequested = true;
      TranslateMessage(&message);
      DispatchMessageW(&message);
    }
    if (gQuitRequested) break;
    ++frameId;
    ++acquireSeq;
    gConfig.appFramesStarted = frameId;
    VkFence fence = gVk.frameFences[frameSlot];
    uint64_t begin = QpcNow();
    TraceEvent waitBegin = MakeEvent(EventKind::fenceWaitBegin, frameId, begin);
    waitBegin.frameSlot = static_cast<int32_t>(frameSlot);
    waitBegin.appAcquireSeq = acquireSeq;
    RecordEvent(waitBegin);
    VkResult result = gVk.waitForFences(gVk.device, 1, &fence, VK_TRUE,
                                        UINT64_MAX);
    uint64_t end = QpcNow();
    TraceEvent waitEnd = MakeEvent(EventKind::fenceWaitEnd, frameId, end);
    waitEnd.callBeginQpcTicks = begin;
    waitEnd.callEndQpcTicks = end;
    waitEnd.durationQpcTicks = end - begin;
    waitEnd.vkResult = static_cast<int32_t>(result);
    waitEnd.frameSlot = static_cast<int32_t>(frameSlot);
    waitEnd.appAcquireSeq = acquireSeq;
    RecordEvent(waitEnd);
    if (result != VK_SUCCESS) {
      SetFatal("vkWaitForFences before acquire failed", 8);
      break;
    }

    uint32_t imageIndex = UINT32_MAX;
    begin = QpcNow();
    TraceEvent acquireBegin = MakeEvent(EventKind::acquireBegin, frameId, begin);
    acquireBegin.callBeginQpcTicks = begin;
    acquireBegin.appAcquireSeq = acquireSeq;
    acquireBegin.frameSlot = static_cast<int32_t>(frameSlot);
    acquireBegin.waitSemaphoreId = HandleValue(gVk.imageAvailable[frameSlot]);
    RecordEvent(acquireBegin);
    result = gVk.acquireNextImage(gVk.device, gVk.swapchain, UINT64_MAX,
                                  gVk.imageAvailable[frameSlot], VK_NULL_HANDLE,
                                  &imageIndex);
    end = QpcNow();
    VkImage image = imageIndex < gVk.images.size() ? gVk.images[imageIndex] : VK_NULL_HANDLE;
    TraceEvent acquireEnd = MakeEvent(EventKind::acquireEnd, frameId, end);
    acquireEnd.callBeginQpcTicks = begin;
    acquireEnd.callEndQpcTicks = end;
    acquireEnd.durationQpcTicks = end - begin;
    acquireEnd.vkResult = static_cast<int32_t>(result);
    acquireEnd.appAcquireSeq = acquireSeq;
    acquireEnd.imageIndex = imageIndex;
    acquireEnd.imageId = HandleValue(image);
    acquireEnd.frameSlot = static_cast<int32_t>(frameSlot);
    acquireEnd.waitSemaphoreId = HandleValue(gVk.imageAvailable[frameSlot]);
    RecordEvent(acquireEnd);
    if (result != VK_SUCCESS && result != VK_SUBOPTIMAL_KHR) {
      SetFatal("vkAcquireNextImageKHR failed", 8);
      break;
    }
    if (imageIndex >= gVk.images.size()) {
      SetFatal("vkAcquireNextImageKHR returned an invalid image index", 8);
      break;
    }
    if (!RecordClearCommand(imageIndex, frameId)) break;
    ++submitSeq;
    gConfig.appSubmits = submitSeq;
    VkCommandBuffer command = gVk.commandBuffers[imageIndex];
    VkSemaphore waitSemaphore = gVk.imageAvailable[frameSlot];
    VkSemaphore signalSemaphore = gVk.renderFinished[imageIndex];
    VkSemaphoreSubmitInfo waitInfo{VK_STRUCTURE_TYPE_SEMAPHORE_SUBMIT_INFO};
    waitInfo.semaphore = waitSemaphore;
    waitInfo.stageMask = VK_PIPELINE_STAGE_2_COLOR_ATTACHMENT_OUTPUT_BIT;
    VkCommandBufferSubmitInfo commandInfo{VK_STRUCTURE_TYPE_COMMAND_BUFFER_SUBMIT_INFO};
    commandInfo.commandBuffer = command;
    VkSemaphoreSubmitInfo signalInfo{VK_STRUCTURE_TYPE_SEMAPHORE_SUBMIT_INFO};
    signalInfo.semaphore = signalSemaphore;
    signalInfo.stageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT;
    VkSubmitInfo2 submitInfo{VK_STRUCTURE_TYPE_SUBMIT_INFO_2};
    submitInfo.waitSemaphoreInfoCount = 1;
    submitInfo.pWaitSemaphoreInfos = &waitInfo;
    submitInfo.commandBufferInfoCount = 1;
    submitInfo.pCommandBufferInfos = &commandInfo;
    submitInfo.signalSemaphoreInfoCount = 1;
    submitInfo.pSignalSemaphoreInfos = &signalInfo;
    result = gVk.resetFences(gVk.device, 1, &fence);
    if (result != VK_SUCCESS) {
      SetFatal("vkResetFences before queue submission failed", 8);
      break;
    }
    begin = QpcNow();
    TraceEvent submitBegin = MakeEvent(EventKind::submitBegin, frameId, begin);
    submitBegin.callBeginQpcTicks = begin;
    submitBegin.appAcquireSeq = acquireSeq;
    submitBegin.appSubmitSeq = submitSeq;
    submitBegin.imageIndex = imageIndex;
    submitBegin.imageId = HandleValue(image);
    submitBegin.commandBufferId = HandleValue(command);
    submitBegin.waitSemaphoreId = HandleValue(waitSemaphore);
    submitBegin.signalSemaphoreId = HandleValue(signalSemaphore);
    submitBegin.frameSlot = static_cast<int32_t>(frameSlot);
    RecordEvent(submitBegin);
    result = gVk.queueSubmit2(gVk.queue, 1, &submitInfo, fence);
    end = QpcNow();
    TraceEvent submitEnd = MakeEvent(EventKind::submitEnd, frameId, end);
    submitEnd.callBeginQpcTicks = begin;
    submitEnd.callEndQpcTicks = end;
    submitEnd.durationQpcTicks = end - begin;
    submitEnd.vkResult = static_cast<int32_t>(result);
    submitEnd.appAcquireSeq = acquireSeq;
    submitEnd.appSubmitSeq = submitSeq;
    submitEnd.imageIndex = imageIndex;
    submitEnd.imageId = HandleValue(image);
    submitEnd.commandBufferId = HandleValue(command);
    submitEnd.waitSemaphoreId = HandleValue(waitSemaphore);
    submitEnd.signalSemaphoreId = HandleValue(signalSemaphore);
    submitEnd.frameSlot = static_cast<int32_t>(frameSlot);
    RecordEvent(submitEnd);
    if (result != VK_SUCCESS) {
      SetFatal("vkQueueSubmit2 or preceding vkResetFences failed", 8);
      break;
    }

    ++presentSeq;
    gConfig.appPresentRequests = presentSeq;
    VkPresentInfoKHR presentInfo{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};
    presentInfo.waitSemaphoreCount = 1;
    presentInfo.pWaitSemaphores = &signalSemaphore;
    presentInfo.swapchainCount = 1;
    presentInfo.pSwapchains = &gVk.swapchain;
    presentInfo.pImageIndices = &imageIndex;
    begin = QpcNow();
    TraceEvent presentBegin = MakeEvent(EventKind::presentBegin, frameId, begin);
    presentBegin.callBeginQpcTicks = begin;
    presentBegin.appAcquireSeq = acquireSeq;
    presentBegin.appSubmitSeq = submitSeq;
    presentBegin.appPresentSeq = presentSeq;
    presentBegin.imageIndex = imageIndex;
    presentBegin.imageId = HandleValue(image);
    presentBegin.commandBufferId = HandleValue(command);
    presentBegin.waitSemaphoreId = HandleValue(signalSemaphore);
    presentBegin.frameSlot = static_cast<int32_t>(frameSlot);
    RecordEvent(presentBegin);
    result = gVk.queuePresent(gVk.queue, &presentInfo);
    end = QpcNow();
    TraceEvent presentEnd = MakeEvent(EventKind::presentEnd, frameId, end);
    presentEnd.callBeginQpcTicks = begin;
    presentEnd.callEndQpcTicks = end;
    presentEnd.durationQpcTicks = end - begin;
    presentEnd.vkResult = static_cast<int32_t>(result);
    presentEnd.appAcquireSeq = acquireSeq;
    presentEnd.appSubmitSeq = submitSeq;
    presentEnd.appPresentSeq = presentSeq;
    presentEnd.imageIndex = imageIndex;
    presentEnd.imageId = HandleValue(image);
    presentEnd.commandBufferId = HandleValue(command);
    presentEnd.waitSemaphoreId = HandleValue(signalSemaphore);
    presentEnd.frameSlot = static_cast<int32_t>(frameSlot);
    RecordEvent(presentEnd);
    if (result != VK_SUCCESS && result != VK_SUBOPTIMAL_KHR) {
      SetFatal("vkQueuePresentKHR failed", 8);
      break;
    }
    frameSlot = (frameSlot + 1) % kFramesInFlight;
  }
  gConfig.actualRunNs = QpcTicksToNs(QpcNow() - start);
  if (gQuitRequested && gExitCode == 0) {
    std::snprintf(gConfig.stopReason, sizeof(gConfig.stopReason), "%s", "window_closed");
  }
  return gExitCode == 0;
}

void WriteNullableNumber(FILE *file, uint64_t value) {
  if (value == 0) std::fputs("null", file);
  else std::fprintf(file, "%llu", static_cast<unsigned long long>(value));
}

void WriteConfig(FILE *file) {
  const std::string tracePath = JsonEscape(gTracePath);
  const std::string experimentToken = JsonEscape(gConfig.experimentToken);
  const std::string publicWindowStartGatePath = JsonEscape(gConfig.publicWindowStartGatePath);
  const std::string hostTracePath = JsonEscape(gConfig.hostTracePath);
  const std::string modulePath = JsonEscape(gConfig.vulkanModulePath);
  const std::string winevulkanDllModulePath = JsonEscape(gConfig.winevulkanDllModulePath);
  const std::string deviceName = JsonEscape(gConfig.physicalDeviceName);
  const std::string displayName = JsonEscape(gConfig.displayName);
  const std::string fatalError = JsonEscape(gConfig.fatalError);
  std::fprintf(file,
      "{\"origin\":\"APP_CONFIG\",\"event_id\":0,\"event\":\"app_configuration\","
      "\"process_id\":%llu,\"process_architecture\":\"x86_64\",\"run_seconds_requested\":%llu,"
      "\"qpc_frequency\":%llu,\"timestamp_clock_domain\":\"windows_qpc_ticks\","
      "\"event_timestamp_clock_source\":\"QueryPerformanceCounter\","
      "\"timestamp_conversion\":\"floor(ticks*1e9/qpc_frequency)\","
      "\"trace_path\":\"%s\",\"target_unix_pid\":%llu,"
      "\"experiment_token\":\"%s\",\"host_trace_path\":\"%s\","
      "\"run_id\":\"%s\",\"public_window_start_gate_required\":%s,"
      "\"public_window_start_gate_passed\":%s,"
      "\"public_window_start_gate_wait_begin_qpc_ticks\":%llu,"
      "\"public_window_start_gate_passed_qpc_ticks\":%llu,"
      "\"public_window_start_gate_released_monotonic_ns\":%llu,"
      "\"public_window_start_gate_path\":\"%s\","
      "\"vulkan_module_path\":\"%s\","
      "\"host_runtime_startup_gate_passed\":%s,"
      "\"winevulkan_dll_module_loaded\":%s,"
      "\"winevulkan_dll_before_frame_loop\":%s,"
      "\"winevulkan_dll_module_path\":\"%s\","
      "\"winevulkan_dll_host_path\":\"%s\","
      "\"winevulkan_dll_sha256\":\"%s\","
      "\"requested_window_client_width\":%u,\"requested_window_client_height\":%u,"
      "\"actual_window_client_width\":%u,\"actual_window_client_height\":%u,"
      "\"window_x\":%d,\"window_y\":%d,\"window_visible_at_start\":%s,"
      "\"window_dpi_aware\":%s,\"dpi_x\":%u,\"dpi_y\":%u,"
      "\"window_mode\":\"windowed\",\"window_logical_dpi_scale_x\":%.6f,"
      "\"window_logical_dpi_scale_y\":%.6f,"
      "\"display_name\":\"%s\",\"display_refresh_hz\":%u,"
      "\"requested_format\":\"VK_FORMAT_B8G8R8A8_UNORM\",\"actual_format\":%u,"
      "\"actual_format_name\":\"%s\","
      "\"actual_color_space\":%u,\"requested_present_mode\":\"VK_PRESENT_MODE_FIFO_KHR\","
      "\"actual_present_mode\":%u,\"actual_present_mode_name\":\"VK_PRESENT_MODE_FIFO_KHR\","
      "\"requested_swapchain_width\":%u,"
      "\"requested_swapchain_height\":%u,\"actual_swapchain_width\":%u,"
      "\"actual_swapchain_height\":%u,\"requested_image_count\":%u,\"actual_image_count\":%u,"
      "\"surface_min_image_count\":%u,\"surface_max_image_count\":%u,"
      "\"surface_current_extent_width\":%u,\"surface_current_extent_height\":%u,"
      "\"actual_surface_transform\":%u,\"actual_composite_alpha\":%u,"
      "\"instance_api_version\":%llu,\"physical_api_version\":%llu,"
      "\"submit2_entrypoint\":\"%s\","
      "\"physical_device_name\":\"%s\",\"queue_family_index\":%u,"
      "\"device_id\":%llu,\"queue_id\":%llu,\"swapchain_id\":%llu,"
      "\"portability_enumeration_enabled\":%s,\"synchronization2_enabled\":%s,"
      "\"trace_events_written\":%llu,\"trace_events_dropped\":%llu,"
      "\"actual_run_ns\":%llu,\"app_frames_started\":%llu,\"app_submits\":%llu,"
      "\"app_present_requests\":%llu,"
      "\"exit_code\":%d,\"stop_reason\":\"%s\",\"fatal_error\":\"%s\"}\n",
      static_cast<unsigned long long>(gConfig.processId),
      static_cast<unsigned long long>(gConfig.runSeconds),
      static_cast<unsigned long long>(gConfig.qpcFrequency), tracePath.c_str(),
      static_cast<unsigned long long>(gConfig.targetUnixPid),
      experimentToken.c_str(), hostTracePath.c_str(), gConfig.experimentRunID,
      gConfig.publicWindowStartGateRequired ? "true" : "false",
      gConfig.publicWindowStartGatePassed ? "true" : "false",
      static_cast<unsigned long long>(gConfig.publicWindowStartGateWaitBeginQpcTicks),
      static_cast<unsigned long long>(gConfig.publicWindowStartGatePassedQpcTicks),
      static_cast<unsigned long long>(gConfig.publicWindowStartGateReleasedMonotonicNS),
      publicWindowStartGatePath.c_str(), modulePath.c_str(),
      gConfig.hostRuntimeStartupGatePassed ? "true" : "false",
      gConfig.winevulkanDllModuleLoaded ? "true" : "false",
      gConfig.winevulkanDllBeforeFrameLoop ? "true" : "false",
      winevulkanDllModulePath.c_str(), gConfig.winevulkanDllHostPath,
      gConfig.winevulkanDllSha256, gConfig.requestedWidth, gConfig.requestedHeight,
      gConfig.actualWindowWidth, gConfig.actualWindowHeight, gConfig.windowX,
      gConfig.windowY, gConfig.visible ? "true" : "false",
      gConfig.windowDpiAware ? "true" : "false", gConfig.dpiX, gConfig.dpiY,
      gConfig.dpiX ? static_cast<double>(gConfig.dpiX) / 96.0 : 0.0,
      gConfig.dpiY ? static_cast<double>(gConfig.dpiY) / 96.0 : 0.0,
      displayName.c_str(), gConfig.displayRefreshHz,
      gConfig.selectedFormat,
      gConfig.selectedFormat == VK_FORMAT_B8G8R8A8_UNORM
          ? "VK_FORMAT_B8G8R8A8_UNORM" : "VK_FORMAT_UNDEFINED",
      gConfig.selectedColorSpace, gConfig.selectedPresentMode,
      gConfig.requestedWidth, gConfig.requestedHeight,
      gConfig.actualSwapchainWidth, gConfig.actualSwapchainHeight,
      gConfig.requestedImageCount, gConfig.actualImageCount,
      gConfig.surfaceMinImageCount, gConfig.surfaceMaxImageCount,
      gConfig.surfaceCurrentExtentWidth, gConfig.surfaceCurrentExtentHeight,
      gConfig.actualTransform, gConfig.compositeAlpha,
      static_cast<unsigned long long>(gConfig.instanceApiVersion),
      static_cast<unsigned long long>(gConfig.physicalApiVersion),
      gConfig.submit2Entrypoint,
      deviceName.c_str(), gConfig.queueFamilyIndex,
      static_cast<unsigned long long>(gConfig.deviceId),
      static_cast<unsigned long long>(gConfig.queueId),
      static_cast<unsigned long long>(gConfig.swapchainId),
      gConfig.portabilityEnumerationEnabled ? "true" : "false",
      gConfig.synchronization2Enabled ? "true" : "false",
      static_cast<unsigned long long>(gTraceCount),
      static_cast<unsigned long long>(gDroppedTraceEvents),
      static_cast<unsigned long long>(gConfig.actualRunNs),
      static_cast<unsigned long long>(gConfig.appFramesStarted),
      static_cast<unsigned long long>(gConfig.appSubmits),
      static_cast<unsigned long long>(gConfig.appPresentRequests),
      gExitCode, gConfig.stopReason,
      fatalError.c_str());
}

void WriteTraceEvent(FILE *file, const TraceEvent &event) {
  const uint64_t callBeginNs = QpcTicksToNs(event.callBeginQpcTicks);
  const uint64_t callEndNs = QpcTicksToNs(event.callEndQpcTicks);
  const uint64_t durationNs = QpcTicksToNs(event.durationQpcTicks);
  std::fprintf(file,
      "{\"origin\":\"APP_SOURCE\",\"event_id\":%llu,\"event\":\"%s\","
      "\"ts_qpc_ns\":%llu,\"ts_qpc_ticks\":%llu,\"qpc_frequency\":%llu,"
      "\"ts_clock_domain\":\"windows_qpc_ticks\",\"timestamp_clock_source\":\"QueryPerformanceCounter\","
      "\"tid\":%u,\"app_frame_id\":%llu,\"app_acquire_seq\":",
      static_cast<unsigned long long>(event.eventId), EventName(event.kind),
      static_cast<unsigned long long>(QpcTicksToNs(event.tsQpcTicks)),
      static_cast<unsigned long long>(event.tsQpcTicks),
      static_cast<unsigned long long>(gConfig.qpcFrequency), event.threadId,
      static_cast<unsigned long long>(event.appFrameId));
  WriteNullableNumber(file, event.appAcquireSeq);
  std::fputs(",\"app_submit_seq\":", file);
  WriteNullableNumber(file, event.appSubmitSeq);
  std::fputs(",\"app_present_seq\":", file);
  WriteNullableNumber(file, event.appPresentSeq);
  std::fputs(",\"image_index\":", file);
  if (event.imageIndex == UINT32_MAX) std::fputs("null", file);
  else std::fprintf(file, "%u", event.imageIndex);
  std::fputs(",\"image_id\":", file);
  WriteNullableNumber(file, event.imageId);
  std::fputs(",\"device_id\":", file);
  WriteNullableNumber(file, event.deviceId);
  std::fputs(",\"queue_id\":", file);
  WriteNullableNumber(file, event.queueId);
  std::fputs(",\"swapchain_id\":", file);
  WriteNullableNumber(file, event.swapchainId);
  std::fputs(",\"command_buffer_id\":", file);
  WriteNullableNumber(file, event.commandBufferId);
  std::fputs(",\"wait_semaphore_id\":", file);
  WriteNullableNumber(file, event.waitSemaphoreId);
  std::fputs(",\"signal_semaphore_id\":", file);
  WriteNullableNumber(file, event.signalSemaphoreId);
  std::fputs(",\"frame_slot\":", file);
  if (event.frameSlot < 0) std::fputs("null", file);
  else std::fprintf(file, "%d", event.frameSlot);
  std::fputs(",\"call_begin_qpc_ticks\":", file);
  WriteNullableNumber(file, event.callBeginQpcTicks);
  std::fputs(",\"call_end_qpc_ticks\":", file);
  WriteNullableNumber(file, event.callEndQpcTicks);
  std::fputs(",\"call_begin_qpc_ns\":", file);
  WriteNullableNumber(file, callBeginNs);
  std::fputs(",\"call_end_qpc_ns\":", file);
  WriteNullableNumber(file, callEndNs);
  std::fputs(",\"duration_qpc_ticks\":", file);
  WriteNullableNumber(file, event.durationQpcTicks);
  std::fputs(",\"duration_ns\":", file);
  WriteNullableNumber(file, durationNs);
  std::fputs(",\"vk_result\":", file);
  if (event.vkResult == std::numeric_limits<int32_t>::min()) std::fputs("null", file);
  else std::fprintf(file, "%d", event.vkResult);
  std::fputs(",\"vk_result_name\":", file);
  if (event.vkResult == std::numeric_limits<int32_t>::min()) std::fputs("null", file);
  else std::fprintf(file, "\"%s\"", VkResultName(static_cast<VkResult>(event.vkResult)));
  std::fprintf(file, ",\"detail_code\":%u}\n", event.detailCode);
}

void WriteTrace() {
  FILE *file = std::fopen(gTracePath, "wb");
  if (file == nullptr) {
    gExitCode = gExitCode == 0 ? 9 : gExitCode;
    return;
  }
  WriteConfig(file);
  for (size_t index = 0; index < gTraceCount; ++index) {
    WriteTraceEvent(file, gTraceEvents[index]);
  }
  std::fflush(file);
  std::fclose(file);
}

void DestroyVulkan() {
  if (gVk.device != VK_NULL_HANDLE) {
    if (gVk.deviceWaitIdle != nullptr) {
      const uint64_t begin = QpcNow();
      TraceEvent waitBegin = MakeEvent(EventKind::shutdownWaitBegin, 0, begin);
      waitBegin.callBeginQpcTicks = begin;
      RecordEvent(waitBegin);
      const VkResult idleResult = gVk.deviceWaitIdle(gVk.device);
      const uint64_t end = QpcNow();
      TraceEvent waitEnd = MakeEvent(EventKind::shutdownWaitEnd, 0, end);
      waitEnd.callBeginQpcTicks = begin;
      waitEnd.callEndQpcTicks = end;
      waitEnd.durationQpcTicks = end - begin;
      waitEnd.vkResult = static_cast<int32_t>(idleResult);
      RecordEvent(waitEnd);
    }
    for (VkFramebuffer framebuffer : gVk.framebuffers) {
      if (framebuffer != VK_NULL_HANDLE && gVk.destroyFramebuffer != nullptr)
        gVk.destroyFramebuffer(gVk.device, framebuffer, nullptr);
    }
    for (VkImageView view : gVk.imageViews) {
      if (view != VK_NULL_HANDLE && gVk.destroyImageView != nullptr)
        gVk.destroyImageView(gVk.device, view, nullptr);
    }
    if (gVk.renderPass != VK_NULL_HANDLE && gVk.destroyRenderPass != nullptr)
      gVk.destroyRenderPass(gVk.device, gVk.renderPass, nullptr);
    for (VkCommandBuffer command : gVk.commandBuffers) {
      (void)command;
    }
    if (gVk.commandPool != VK_NULL_HANDLE && gVk.destroyCommandPool != nullptr)
      gVk.destroyCommandPool(gVk.device, gVk.commandPool, nullptr);
    for (VkSemaphore semaphore : gVk.imageAvailable) {
      if (semaphore != VK_NULL_HANDLE && gVk.destroySemaphore != nullptr)
        gVk.destroySemaphore(gVk.device, semaphore, nullptr);
    }
    for (VkSemaphore semaphore : gVk.renderFinished) {
      if (semaphore != VK_NULL_HANDLE && gVk.destroySemaphore != nullptr)
        gVk.destroySemaphore(gVk.device, semaphore, nullptr);
    }
    for (VkFence fence : gVk.frameFences) {
      if (fence != VK_NULL_HANDLE && gVk.destroyFence != nullptr)
        gVk.destroyFence(gVk.device, fence, nullptr);
    }
    if (gVk.swapchain != VK_NULL_HANDLE && gVk.destroySwapchain != nullptr)
      gVk.destroySwapchain(gVk.device, gVk.swapchain, nullptr);
    if (gVk.destroyDevice != nullptr) gVk.destroyDevice(gVk.device, nullptr);
    gVk.device = VK_NULL_HANDLE;
  }
  if (gVk.surface != VK_NULL_HANDLE && gVk.destroySurface != nullptr &&
      gVk.instance != VK_NULL_HANDLE) {
    gVk.destroySurface(gVk.instance, gVk.surface, nullptr);
  }
  if (gVk.instance != VK_NULL_HANDLE && gVk.destroyInstance != nullptr) {
    gVk.destroyInstance(gVk.instance, nullptr);
  }
  if (gVk.vulkanModule != nullptr) FreeLibrary(gVk.vulkanModule);
  if (gWindow != nullptr && IsWindow(gWindow)) DestroyWindow(gWindow);
  UnregisterClassW(L"FGMetalBareVulkanControl", gModuleInstance);
}

}  // namespace

int WINAPI WinMain(HINSTANCE instance, HINSTANCE, LPSTR, int) {
  gModuleInstance = instance;
  gConfig.processId = GetCurrentProcessId();
  LARGE_INTEGER frequency{};
  if (QueryPerformanceFrequency(&frequency) && frequency.QuadPart > 0) {
    gConfig.qpcFrequency = static_cast<uint64_t>(frequency.QuadPart);
  } else {
    SetFatal("QueryPerformanceFrequency failed", 2);
  }
  ReadEnvironment();
  if (gExitCode == 0 && CreateControlWindow() && CreateInstanceAndSurface() &&
      WaitForHostRuntimeStartupIdentity() &&
      SelectPhysicalDeviceAndCreateLogicalDevice() &&
      CreateSwapchainAndRenderResources() &&
      WaitForPublicWindowStartGate()) {
    gVk.extent = {gConfig.actualSwapchainWidth, gConfig.actualSwapchainHeight};
    if (gVk.images.size() != 0 && RenderFor(gConfig.runSeconds)) gExitCode = 0;
  }
  DestroyVulkan();
  WriteTrace();
  return gExitCode;
}
