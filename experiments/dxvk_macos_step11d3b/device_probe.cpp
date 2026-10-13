// STEP 11D.3-BR5 minimal vkCreateDevice probe (PE, no window/surface/swapchain).
// Stops after: instance, physical enumeration, queue family, vkCreateDevice,
// vkGetDeviceQueue, device-proc sanity, vkDeviceWaitIdle, destroy, exit.
// Options (argv): sync2=0|1  swapchain=0|1  portability=0|1  instext=0|1
//                 api=10|13   pnext=0|1 (VkPhysicalDeviceFeatures2 chain)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define VK_USE_PLATFORM_WIN32_KHR
#define VK_NO_PROTOTYPES
#include <windows.h>
#include <vulkan/vulkan.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static void mark(const char *step, long value = 0) {
  std::printf("[probe] %s %ld\n", step, value);
  std::fflush(stdout);
}

static bool opt(int argc, char **argv, const char *key, bool fallback) {
  const size_t n = std::strlen(key);
  for (int i = 1; i < argc; ++i)
    if (!std::strncmp(argv[i], key, n) && argv[i][n] == '=')
      return argv[i][n + 1] == '1';
  return fallback;
}

static int fail(const char *what, long code, int exitCode) {
  std::printf("[probe] FAIL %s %ld\n", what, code);
  std::fflush(stdout);
  return exitCode;
}

int main(int argc, char **argv) {
  const bool sync2 = opt(argc, argv, "sync2", true);
  const bool swapchain = opt(argc, argv, "swapchain", true);
  const bool portability = opt(argc, argv, "portability", true);
  const bool instext = opt(argc, argv, "instext", true);
  const bool api13 = opt(argc, argv, "api13", true);
  const bool surfaceMode = opt(argc, argv, "surface", false);
  const bool lateGdpa = opt(argc, argv, "late_gdpa", false);
  const bool earlyGdpa = !lateGdpa && opt(argc, argv, "early_gdpa", true);
  mark("start");
  HWND window = nullptr;
  if (surfaceMode) {
    WNDCLASSA wc{};
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(nullptr);
    wc.lpszClassName = "FGBR5Probe";
    RegisterClassA(&wc);
    window = CreateWindowExA(0, "FGBR5Probe", "BR5 probe", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                             100, 100, 640, 360, nullptr, nullptr, wc.hInstance, nullptr);
    mark("window", window ? 1 : 0);
  }
  HMODULE module = LoadLibraryW(L"vulkan-1.dll");
  if (!module) return fail("LoadLibrary vulkan-1.dll", GetLastError(), 4);
  auto gipa = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
      reinterpret_cast<void *>(GetProcAddress(module, "vkGetInstanceProcAddr")));
  if (!gipa) return fail("vkGetInstanceProcAddr", 0, 4);
  auto createInstance = reinterpret_cast<PFN_vkCreateInstance>(
      gipa(VK_NULL_HANDLE, "vkCreateInstance"));
  auto enumVersion = reinterpret_cast<PFN_vkEnumerateInstanceVersion>(
      gipa(VK_NULL_HANDLE, "vkEnumerateInstanceVersion"));
  uint32_t loaderVersion = VK_API_VERSION_1_0;
  if (enumVersion) enumVersion(&loaderVersion);
  const uint32_t requested = api13 ? (loaderVersion < VK_API_VERSION_1_3 ? loaderVersion : VK_API_VERSION_1_3)
                                   : VK_API_VERSION_1_0;
  VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
  app.pApplicationName = "FG-Metal BR5 device probe";
  app.apiVersion = requested;
  std::vector<const char *> iext;
  if (instext) {
    iext.push_back(VK_KHR_SURFACE_EXTENSION_NAME);
    iext.push_back(VK_KHR_WIN32_SURFACE_EXTENSION_NAME);
  }
  VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
  ici.pApplicationInfo = &app;
  ici.enabledExtensionCount = static_cast<uint32_t>(iext.size());
  ici.ppEnabledExtensionNames = iext.data();
  VkInstance instance = VK_NULL_HANDLE;
  VkResult r = createInstance(&ici, nullptr, &instance);
  mark("vkCreateInstance", r);
  if (r != VK_SUCCESS) return fail("vkCreateInstance", r, 4);

  auto enumPhys = reinterpret_cast<PFN_vkEnumeratePhysicalDevices>(
      gipa(instance, "vkEnumeratePhysicalDevices"));
  auto getProps = reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
      gipa(instance, "vkGetPhysicalDeviceProperties"));
  auto getQueues = reinterpret_cast<PFN_vkGetPhysicalDeviceQueueFamilyProperties>(
      gipa(instance, "vkGetPhysicalDeviceQueueFamilyProperties"));
  auto enumDevExt = reinterpret_cast<PFN_vkEnumerateDeviceExtensionProperties>(
      gipa(instance, "vkEnumerateDeviceExtensionProperties"));
  auto createDevice = reinterpret_cast<PFN_vkCreateDevice>(gipa(instance, "vkCreateDevice"));
  PFN_vkGetDeviceProcAddr gdpa = nullptr;
  if (earlyGdpa) {
    gdpa = reinterpret_cast<PFN_vkGetDeviceProcAddr>(gipa(instance, "vkGetDeviceProcAddr"));
    std::printf("[probe] early gdpa queried=%p\n", reinterpret_cast<void *>(gdpa));
  } else {
    std::printf("[probe] late gdpa mode: omitting early gipa(\"vkGetDeviceProcAddr\")\n");
  }
  auto destroyInstance = reinterpret_cast<PFN_vkDestroyInstance>(gipa(instance, "vkDestroyInstance"));
  if (!enumPhys || !getProps || !getQueues || !enumDevExt || !createDevice || (earlyGdpa && !gdpa) || !destroyInstance)
    return fail("instance procs", 0, 4);

  uint32_t count = 0;
  r = enumPhys(instance, &count, nullptr);
  mark("vkEnumeratePhysicalDevices(count)", count);
  if (r != VK_SUCCESS || !count) return fail("enumerate", r, 5);
  std::vector<VkPhysicalDevice> phys(count);
  r = enumPhys(instance, &count, phys.data());
  if (r != VK_SUCCESS) return fail("enumerate list", r, 5);
  VkPhysicalDeviceProperties props{};
  getProps(phys[0], &props);
  std::printf("[probe] physical device=\"%s\" api=0x%x handle=%p\n", props.deviceName,
              props.apiVersion, static_cast<void *>(phys[0]));
  uint32_t qn = 0;
  getQueues(phys[0], &qn, nullptr);
  std::vector<VkQueueFamilyProperties> qf(qn);
  getQueues(phys[0], &qn, qf.data());
  VkSurfaceKHR surface = VK_NULL_HANDLE;
  if (surfaceMode) {
    auto createSurf = reinterpret_cast<PFN_vkCreateWin32SurfaceKHR>(gipa(instance, "vkCreateWin32SurfaceKHR"));
    VkWin32SurfaceCreateInfoKHR si{VK_STRUCTURE_TYPE_WIN32_SURFACE_CREATE_INFO_KHR};
    si.hinstance = GetModuleHandleA(nullptr);
    si.hwnd = window;
    r = createSurf(instance, &si, nullptr, &surface);
    mark("vkCreateWin32SurfaceKHR", r);
    if (r != VK_SUCCESS) return fail("surface", r, 4);
  }
  auto getSupport = reinterpret_cast<PFN_vkGetPhysicalDeviceSurfaceSupportKHR>(
      gipa(instance, "vkGetPhysicalDeviceSurfaceSupportKHR"));
  uint32_t family = UINT32_MAX;
  for (uint32_t i = 0; i < qn; ++i) {
    if (!((qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) && qf[i].queueCount)) continue;
    if (surfaceMode) {
      VkBool32 present = VK_FALSE;
      if (getSupport(phys[0], i, surface, &present) != VK_SUCCESS || !present) continue;
    }
    family = i; break;
  }
  mark("queue family", static_cast<long>(family));
  if (family == UINT32_MAX) return fail("queue family", 0, 5);

  uint32_t en = 0;
  enumDevExt(phys[0], nullptr, &en, nullptr);
  std::vector<VkExtensionProperties> ext(en);
  enumDevExt(phys[0], nullptr, &en, ext.data());
  auto has = [&](const char *n) {
    for (auto &e : ext) if (!std::strcmp(e.extensionName, n)) return true;
    return false;
  };
  std::vector<const char *> dext;
  if (swapchain && has(VK_KHR_SWAPCHAIN_EXTENSION_NAME)) dext.push_back(VK_KHR_SWAPCHAIN_EXTENSION_NAME);
  if (portability && has("VK_KHR_portability_subset")) dext.push_back("VK_KHR_portability_subset");

  float priority = 1.0f;
  VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
  qi.queueFamilyIndex = family;
  qi.queueCount = 1;
  qi.pQueuePriorities = &priority;
  VkPhysicalDeviceSynchronization2Features s2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SYNCHRONIZATION_2_FEATURES};
  s2.synchronization2 = VK_TRUE;
  VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
  if (sync2) di.pNext = &s2;
  di.queueCreateInfoCount = 1;
  di.pQueueCreateInfos = &qi;
  di.enabledExtensionCount = static_cast<uint32_t>(dext.size());
  di.ppEnabledExtensionNames = dext.data();
  std::printf("[probe] config sync2=%d swapchain=%d portability=%d instext=%d api13=%d exts=%u\n",
              sync2, swapchain, portability, instext, api13, di.enabledExtensionCount);
  std::fflush(stdout);

  VkDevice device = VK_NULL_HANDLE;
  mark("vkCreateDevice BEGIN");
  r = createDevice(phys[0], &di, nullptr, &device);
  mark("vkCreateDevice RETURNED", r);
  std::printf("[probe] device handle=%p\n", static_cast<void *>(device));
  std::fflush(stdout);
  if (r != VK_SUCCESS || !device) return fail("vkCreateDevice", r, 5);

  auto getDevGdpa = reinterpret_cast<PFN_vkGetDeviceProcAddr>(gipa(instance, "vkGetDeviceProcAddr"));
  const char *names[] = {"vkDestroyDevice", "vkGetDeviceQueue", "vkDeviceWaitIdle",
                         "vkCreateSwapchainKHR", "vkQueueSubmit", "vkCreateFence"};
  for (const char *n : names) {
    PFN_vkVoidFunction f = getDevGdpa(device, n);
    std::printf("[probe] gdpa %s=%s\n", n, f ? "resolved" : "NULL");
  }
  std::fflush(stdout);
  auto destroyDevice = reinterpret_cast<PFN_vkDestroyDevice>(getDevGdpa(device, "vkDestroyDevice"));
  auto getQueue = reinterpret_cast<PFN_vkGetDeviceQueue>(getDevGdpa(device, "vkGetDeviceQueue"));
  auto waitIdle = reinterpret_cast<PFN_vkDeviceWaitIdle>(getDevGdpa(device, "vkDeviceWaitIdle"));
  if (!destroyDevice || !getQueue || !waitIdle) return fail("device procs", 0, 5);
  VkQueue queue = VK_NULL_HANDLE;
  getQueue(device, family, 0, &queue);
  std::printf("[probe] queue handle=%p\n", static_cast<void *>(queue));
  std::fflush(stdout);
  if (!queue) return fail("vkGetDeviceQueue null", 0, 5);
  r = waitIdle(device);
  mark("vkDeviceWaitIdle", r);
  if (r != VK_SUCCESS) return fail("vkDeviceWaitIdle", r, 5);
  destroyDevice(device, nullptr);
  mark("vkDestroyDevice done");
  destroyInstance(instance, nullptr);
  mark("vkDestroyInstance done");
  mark("PASS");
  return 0;
}
