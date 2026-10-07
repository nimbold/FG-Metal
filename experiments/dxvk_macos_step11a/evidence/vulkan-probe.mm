#define VK_USE_PLATFORM_METAL_EXT
#define VK_ENABLE_BETA_EXTENSIONS
#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#include <vulkan/vulkan.h>
#include <vulkan/vulkan_metal.h>
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

static bool hasExt(const std::vector<VkExtensionProperties>& xs, const char* name) {
  return std::any_of(xs.begin(), xs.end(), [&](const auto& x) { return std::strcmp(x.extensionName, name) == 0; });
}
static const char* modeName(VkPresentModeKHR m) {
  switch (m) {
    case VK_PRESENT_MODE_IMMEDIATE_KHR: return "IMMEDIATE";
    case VK_PRESENT_MODE_MAILBOX_KHR: return "MAILBOX";
    case VK_PRESENT_MODE_FIFO_KHR: return "FIFO";
    case VK_PRESENT_MODE_FIFO_RELAXED_KHR: return "FIFO_RELAXED";
    default: return "OTHER";
  }
}
static bool check(VkResult r, const char* what) {
  if (r == VK_SUCCESS) return true;
  std::fprintf(stderr, "FAIL %s VkResult=%d\n", what, int(r));
  return false;
}
static uint32_t findMemoryType(VkPhysicalDevice p, uint32_t bits) {
  VkPhysicalDeviceMemoryProperties mp{};
  vkGetPhysicalDeviceMemoryProperties(p, &mp);
  for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
    if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) return i;
  for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
    if (bits & (1u << i)) return i;
  return UINT32_MAX;
}

int main() {
  setbuf(stdout, nullptr);
  @autoreleasepool {
    [NSApplication sharedApplication];
    NSRect frame = NSMakeRect(80, 80, 640, 360);
    NSWindow* window = [[NSWindow alloc] initWithContentRect:frame
        styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable)
        backing:NSBackingStoreBuffered defer:NO];
    [window setTitle:@"MoltenVK audit probe"];
    NSView* view = [[NSView alloc] initWithFrame:frame];
    CAMetalLayer* layer = [CAMetalLayer layer];
    id<MTLDevice> defaultDevice = MTLCreateSystemDefaultDevice();
    if (!defaultDevice) { std::fprintf(stderr, "FAIL MTLCreateSystemDefaultDevice\n"); return 2; }
    layer.device = defaultDevice;
    layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
    layer.drawableSize = CGSizeMake(640, 360);
    view.wantsLayer = YES;
    view.layer = layer;
    [window setContentView:view];
    [window orderFrontRegardless];
    [NSApp activateIgnoringOtherApps:YES];
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.15]];

    uint32_t nInst = 0;
    if (!check(vkEnumerateInstanceExtensionProperties(nullptr, &nInst, nullptr), "enumerate instance extension count")) return 3;
    std::vector<VkExtensionProperties> instExts(nInst);
    check(vkEnumerateInstanceExtensionProperties(nullptr, &nInst, instExts.data()), "enumerate instance extensions");
    std::printf("instance_extensions=%u\n", nInst);
    for (const char* name : {"VK_KHR_surface", "VK_EXT_metal_surface", "VK_KHR_portability_enumeration", "VK_EXT_metal_objects"}) {
      auto it = std::find_if(instExts.begin(), instExts.end(), [&](const auto& x){ return std::strcmp(x.extensionName,name)==0; });
      std::printf("instance_ext %s specVersion=%u\n", name, it == instExts.end() ? 0u : it->specVersion);
    }
    if (!hasExt(instExts, VK_KHR_SURFACE_EXTENSION_NAME) || !hasExt(instExts, VK_EXT_METAL_SURFACE_EXTENSION_NAME)) return 4;

    std::vector<const char*> enabledInstance{VK_KHR_SURFACE_EXTENSION_NAME, VK_EXT_METAL_SURFACE_EXTENSION_NAME};
    VkInstanceCreateFlags iflags = 0;
    if (hasExt(instExts, VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME)) {
      enabledInstance.push_back(VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME);
      iflags |= VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR;
    }
    VkExportMetalObjectCreateInfoEXT exportDevice{};
    exportDevice.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT;
    exportDevice.exportObjectType = VK_EXPORT_METAL_OBJECT_TYPE_METAL_DEVICE_BIT_EXT;
    VkExportMetalObjectCreateInfoEXT exportQueue{};
    exportQueue.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT;
    exportQueue.pNext = &exportDevice;
    exportQueue.exportObjectType = VK_EXPORT_METAL_OBJECT_TYPE_METAL_COMMAND_QUEUE_BIT_EXT;
    VkApplicationInfo app{};
    app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
    app.pApplicationName = "fgmetal-mvk-probe";
    app.apiVersion = VK_API_VERSION_1_3;
    VkInstanceCreateInfo ici{};
    ici.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    ici.pNext = hasExt(instExts, VK_EXT_METAL_OBJECTS_EXTENSION_NAME) ? &exportQueue : nullptr;
    ici.flags = iflags;
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = (uint32_t)enabledInstance.size();
    ici.ppEnabledExtensionNames = enabledInstance.data();
    VkInstance instance = VK_NULL_HANDLE;
    if (!check(vkCreateInstance(&ici, nullptr, &instance), "create instance")) return 5;

    auto createMetalSurface = (PFN_vkCreateMetalSurfaceEXT)vkGetInstanceProcAddr(instance, "vkCreateMetalSurfaceEXT");
    if (!createMetalSurface) { std::fprintf(stderr, "FAIL vkCreateMetalSurfaceEXT missing\n"); return 6; }
    VkMetalSurfaceCreateInfoEXT msci{};
    msci.sType = VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT;
    msci.pLayer = layer;
    VkSurfaceKHR surface = VK_NULL_HANDLE;
    if (!check(createMetalSurface(instance, &msci, nullptr, &surface), "create metal surface")) return 7;

    uint32_t ndev = 0;
    if (!check(vkEnumeratePhysicalDevices(instance, &ndev, nullptr), "physical device count") || ndev == 0) return 8;
    std::vector<VkPhysicalDevice> physicals(ndev);
    vkEnumeratePhysicalDevices(instance, &ndev, physicals.data());
    VkPhysicalDevice physical = physicals[0];
    VkPhysicalDeviceProperties2 props{};
    props.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
    VkPhysicalDeviceDriverProperties driver{};
    driver.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES;
    props.pNext = &driver;
    vkGetPhysicalDeviceProperties2(physical, &props);
    std::printf("device=%s vendor=0x%04x deviceId=0x%08x api=%u.%u.%u driverId=%u driver=%s driverInfo=%s\n",
      props.properties.deviceName, props.properties.vendorID, props.properties.deviceID,
      VK_VERSION_MAJOR(props.properties.apiVersion), VK_VERSION_MINOR(props.properties.apiVersion), VK_VERSION_PATCH(props.properties.apiVersion),
      driver.driverID, driver.driverName, driver.driverInfo);

    uint32_t ndext = 0;
    vkEnumerateDeviceExtensionProperties(physical, nullptr, &ndext, nullptr);
    std::vector<VkExtensionProperties> devExts(ndext);
    vkEnumerateDeviceExtensionProperties(physical, nullptr, &ndext, devExts.data());
    for (const char* name : {"VK_KHR_swapchain", "VK_KHR_present_id", "VK_KHR_present_wait", "VK_KHR_present_wait2", "VK_KHR_present_id2", "VK_EXT_swapchain_maintenance1", "VK_KHR_swapchain_maintenance1", "VK_GOOGLE_display_timing", "VK_EXT_metal_objects", "VK_KHR_portability_subset", "VK_EXT_present_timing"}) {
      auto it = std::find_if(devExts.begin(), devExts.end(), [&](const auto& x){ return std::strcmp(x.extensionName,name)==0; });
      std::printf("device_ext %s specVersion=%u\n", name, it == devExts.end() ? 0u : it->specVersion);
    }

    uint32_t qcount = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &qcount, nullptr);
    std::vector<VkQueueFamilyProperties> qprops(qcount);
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &qcount, qprops.data());
    uint32_t queueFamily = UINT32_MAX;
    for (uint32_t i = 0; i < qcount; ++i) {
      VkBool32 present = VK_FALSE;
      VkResult pr = vkGetPhysicalDeviceSurfaceSupportKHR(physical, i, surface, &present);
      std::printf("queue_family=%u flags=0x%x count=%u present=%s result=%d\n", i, qprops[i].queueFlags, qprops[i].queueCount, present ? "yes" : "no", int(pr));
      if (queueFamily == UINT32_MAX && (qprops[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) && present) queueFamily = i;
    }
    if (queueFamily == UINT32_MAX) { std::fprintf(stderr, "FAIL no graphics+present queue\n"); return 9; }

    VkPhysicalDevicePresentIdFeaturesKHR fPid{}; fPid.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR;
    VkPhysicalDevicePresentWaitFeaturesKHR fPwait{}; fPwait.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR;
    VkPhysicalDevicePresentWait2FeaturesKHR fPwait2{}; fPwait2.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_2_FEATURES_KHR;
    VkPhysicalDeviceSwapchainMaintenance1FeaturesKHR fMaint{}; fMaint.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR;
    VkPhysicalDeviceVulkan12Features f12{}; f12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES;
    void* head = nullptr; void** tail = &head;
    auto addFeature = [&](const char* ext, void* p, void** next) { if (hasExt(devExts, ext)) { *tail = p; tail = next; *next = nullptr; } };
    addFeature("VK_KHR_present_id", &fPid, &fPid.pNext);
    addFeature("VK_KHR_present_wait", &fPwait, &fPwait.pNext);
    addFeature("VK_KHR_present_wait2", &fPwait2, &fPwait2.pNext);
    addFeature("VK_EXT_swapchain_maintenance1", &fMaint, &fMaint.pNext);
    *tail = &f12; f12.pNext = nullptr;
    VkPhysicalDeviceFeatures2 features{}; features.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2; features.pNext = head;
    vkGetPhysicalDeviceFeatures2(physical, &features);
    std::printf("features presentId=%u presentWait=%u presentWait2=%u swapchainMaintenance1=%u timelineSemaphore=%u\n",
      fPid.presentId, fPwait.presentWait, fPwait2.presentWait2, fMaint.swapchainMaintenance1, f12.timelineSemaphore);

    VkSurfaceCapabilitiesKHR caps{};
    if (check(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps), "surface capabilities")) {
      std::printf("surface imageCount min=%u max=%u extent=%ux%u minExtent=%ux%u maxExtent=%ux%u usage=0x%x transforms=0x%x compositeAlpha=0x%x\n",
        caps.minImageCount, caps.maxImageCount, caps.currentExtent.width, caps.currentExtent.height,
        caps.minImageExtent.width, caps.minImageExtent.height, caps.maxImageExtent.width, caps.maxImageExtent.height,
        caps.supportedUsageFlags, caps.supportedTransforms, caps.supportedCompositeAlpha);
    }
    uint32_t nmode = 0; vkGetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &nmode, nullptr);
    std::vector<VkPresentModeKHR> modes(nmode); vkGetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &nmode, modes.data());
    std::printf("present_modes=%u", nmode); for (auto m : modes) std::printf(" %s(%d)", modeName(m), int(m)); std::printf("\n");
    uint32_t nfmt = 0; vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &nfmt, nullptr);
    std::vector<VkSurfaceFormatKHR> formats(nfmt); vkGetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &nfmt, formats.data());
    std::printf("surface_formats=%u", nfmt); for (auto f : formats) std::printf(" format=%d colorspace=%d", int(f.format), int(f.colorSpace)); std::printf("\n");

    std::vector<const char*> enabledDevice;
    for (const char* name : {"VK_KHR_swapchain", "VK_EXT_metal_objects", "VK_KHR_portability_subset"})
      if (hasExt(devExts, name)) enabledDevice.push_back(name);
    if (!hasExt(devExts, VK_KHR_SWAPCHAIN_EXTENSION_NAME)) { std::fprintf(stderr, "FAIL swapchain extension missing\n"); return 10; }
    float priority = 1.0f;
    VkDeviceQueueCreateInfo qci{}; qci.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO; qci.queueFamilyIndex = queueFamily; qci.queueCount = 1; qci.pQueuePriorities = &priority;
    VkPhysicalDeviceVulkan12Features enable12{}; enable12.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES; enable12.timelineSemaphore = f12.timelineSemaphore;
    VkDeviceCreateInfo dci{}; dci.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO; dci.pNext = f12.timelineSemaphore ? &enable12 : nullptr;
    dci.queueCreateInfoCount = 1; dci.pQueueCreateInfos = &qci; dci.enabledExtensionCount = (uint32_t)enabledDevice.size(); dci.ppEnabledExtensionNames = enabledDevice.data();
    VkDevice device = VK_NULL_HANDLE;
    if (!check(vkCreateDevice(physical, &dci, nullptr, &device), "create device")) return 11;
    VkQueue queue = VK_NULL_HANDLE; vkGetDeviceQueue(device, queueFamily, 0, &queue);

    std::vector<uint32_t> swapImgs;
    if (!formats.empty() && !modes.empty()) {
      VkExtent2D extent = caps.currentExtent;
      if (extent.width == UINT32_MAX) extent = {640,360};
      VkSurfaceFormatKHR sf = formats[0];
      for (auto f : formats) if (f.format == VK_FORMAT_B8G8R8A8_UNORM) { sf = f; break; }
      uint32_t imageCount = caps.minImageCount + 1;
      if (caps.maxImageCount && imageCount > caps.maxImageCount) imageCount = caps.maxImageCount;
      VkSwapchainCreateInfoKHR sci{}; sci.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR; sci.surface = surface;
      sci.minImageCount = imageCount; sci.imageFormat = sf.format; sci.imageColorSpace = sf.colorSpace; sci.imageExtent = extent;
      sci.imageArrayLayers = 1; sci.imageUsage = (caps.supportedUsageFlags & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) ? VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT : VK_IMAGE_USAGE_TRANSFER_DST_BIT;
      sci.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE; sci.preTransform = caps.currentTransform;
      sci.compositeAlpha = (caps.supportedCompositeAlpha & VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR) ? VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR : VK_COMPOSITE_ALPHA_INHERIT_BIT_KHR;
      sci.presentMode = std::find(modes.begin(), modes.end(), VK_PRESENT_MODE_FIFO_KHR) != modes.end() ? VK_PRESENT_MODE_FIFO_KHR : modes[0];
      sci.clipped = VK_TRUE;
      VkSwapchainKHR swap = VK_NULL_HANDLE;
      if (check(vkCreateSwapchainKHR(device, &sci, nullptr, &swap), "create swapchain")) {
        uint32_t count = 0; vkGetSwapchainImagesKHR(device, swap, &count, nullptr);
        std::printf("swapchain mode=%s requested=%u actual_images=%u extent=%ux%u format=%d\n", modeName(sci.presentMode), imageCount, count, extent.width, extent.height, int(sf.format));
        vkDestroySwapchainKHR(device, swap, nullptr);
      }
    }

    if (hasExt(devExts, VK_EXT_METAL_OBJECTS_EXTENSION_NAME)) {
      auto exportObjects = (PFN_vkExportMetalObjectsEXT)vkGetDeviceProcAddr(device, "vkExportMetalObjectsEXT");
      if (!exportObjects) { std::printf("metal_objects_fn=missing\n"); }
      else {
        VkExportMetalDeviceInfoEXT mdev{}; mdev.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_DEVICE_INFO_EXT;
        VkExportMetalCommandQueueInfoEXT mqueue{}; mqueue.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_COMMAND_QUEUE_INFO_EXT; mqueue.queue = queue; mqueue.pNext = &mdev;
        VkExportMetalObjectsInfoEXT minfo{}; minfo.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECTS_INFO_EXT; minfo.pNext = &mqueue;
        exportObjects(device, &minfo);
        id<MTLDevice> exportedDevice = mdev.mtlDevice;
        id<MTLCommandQueue> exportedQueue = mqueue.mtlCommandQueue;
        std::printf("metal_device=%s same_default=%s metal_queue=%s queue_device_match=%s\n",
          exportedDevice ? [[exportedDevice name] UTF8String] : "nil", exportedDevice == defaultDevice ? "yes" : "no",
          exportedQueue ? "present" : "nil", exportedQueue && exportedQueue.device == exportedDevice ? "yes" : "no");

        VkExportMetalObjectCreateInfoEXT textureIntent{}; textureIntent.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT; textureIntent.exportObjectType = VK_EXPORT_METAL_OBJECT_TYPE_METAL_TEXTURE_BIT_EXT;
        VkImageCreateInfo imgInfo{}; imgInfo.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO; imgInfo.pNext = &textureIntent;
        imgInfo.imageType = VK_IMAGE_TYPE_2D; imgInfo.format = VK_FORMAT_B8G8R8A8_UNORM; imgInfo.extent = {256,144,1}; imgInfo.mipLevels=1; imgInfo.arrayLayers=1;
        imgInfo.samples = VK_SAMPLE_COUNT_1_BIT; imgInfo.tiling = VK_IMAGE_TILING_OPTIMAL; imgInfo.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        imgInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE; imgInfo.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        VkImage image = VK_NULL_HANDLE;
        VkResult imageResult = vkCreateImage(device, &imgInfo, nullptr, &image);
        std::printf("exportable_image_create=%d format=VK_FORMAT_B8G8R8A8_UNORM extent=256x144\n", int(imageResult));
        if (imageResult == VK_SUCCESS) {
          VkMemoryRequirements req{}; vkGetImageMemoryRequirements(device, image, &req);
          uint32_t mt = findMemoryType(physical, req.memoryTypeBits);
          VkDeviceMemory memory = VK_NULL_HANDLE;
          VkMemoryAllocateInfo mai{}; mai.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO; mai.allocationSize = req.size; mai.memoryTypeIndex = mt;
          VkResult allocResult = mt == UINT32_MAX ? VK_ERROR_FEATURE_NOT_PRESENT : vkAllocateMemory(device, &mai, nullptr, &memory);
          std::printf("image_memory_allocate=%d allocation_size=%llu memory_type=%u\n", int(allocResult), (unsigned long long)req.size, mt);
          if (allocResult == VK_SUCCESS && check(vkBindImageMemory(device, image, memory, 0), "bind image memory")) {
            VkExportMetalTextureInfoEXT tex{}; tex.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_TEXTURE_INFO_EXT; tex.image = image; tex.plane = VK_IMAGE_ASPECT_PLANE_0_BIT;
            VkExportMetalObjectsInfoEXT ti{}; ti.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECTS_INFO_EXT; ti.pNext = &tex;
            exportObjects(device, &ti);
            id<MTLTexture> texture = tex.mtlTexture;
            VkExportMetalTextureInfoEXT texAgain{}; texAgain.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_TEXTURE_INFO_EXT; texAgain.image = image; texAgain.plane = VK_IMAGE_ASPECT_PLANE_0_BIT;
            VkExportMetalObjectsInfoEXT tai{}; tai.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECTS_INFO_EXT; tai.pNext = &texAgain;
            exportObjects(device, &tai);
            id<MTLTexture> textureAgain = texAgain.mtlTexture;
            if (texture) std::printf("texture_export=present device_match=%s size=%lux%lu pixelFormat=%lu sampleCount=%lu type=%lu identity_stable=%s\n",
              texture.device == defaultDevice ? "yes" : "no", (unsigned long)texture.width, (unsigned long)texture.height, (unsigned long)texture.pixelFormat,
              (unsigned long)texture.sampleCount, (unsigned long)texture.textureType, texture == textureAgain ? "yes" : "no");
            else std::printf("texture_export=nil\n");
          }
          if (memory) vkFreeMemory(device, memory, nullptr);
          vkDestroyImage(device, image, nullptr);
        }

        if (f12.timelineSemaphore) {
          VkSemaphoreTypeCreateInfo typeInfo{}; typeInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO; typeInfo.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE; typeInfo.initialValue = 0;
          VkExportMetalObjectCreateInfoEXT semIntent{}; semIntent.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT; semIntent.pNext = &typeInfo; semIntent.exportObjectType = VK_EXPORT_METAL_OBJECT_TYPE_METAL_SHARED_EVENT_BIT_EXT;
          VkSemaphoreCreateInfo semInfo{}; semInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO; semInfo.pNext = &semIntent;
          VkSemaphore semaphore = VK_NULL_HANDLE;
          VkResult semResult = vkCreateSemaphore(device, &semInfo, nullptr, &semaphore);
          std::printf("exportable_timeline_semaphore_create=%d\n", int(semResult));
          if (semResult == VK_SUCCESS) {
            VkExportMetalSharedEventInfoEXT shared{}; shared.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_SHARED_EVENT_INFO_EXT; shared.semaphore = semaphore;
            VkExportMetalObjectsInfoEXT sei{}; sei.sType = VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECTS_INFO_EXT; sei.pNext = &shared;
            exportObjects(device, &sei);
            id<MTLSharedEvent> event = shared.mtlSharedEvent;
            std::printf("shared_event_export=%s initial=%llu\n", event ? "present" : "nil", event ? (unsigned long long)event.signaledValue : 0ull);
            if (event && exportedQueue) {
              id<MTLCommandBuffer> cb = [exportedQueue commandBuffer];
              [cb encodeSignalEvent:event value:7]; [cb commit]; [cb waitUntilCompleted];
              uint64_t observed = 0; VkResult qr = vkGetSemaphoreCounterValue(device, semaphore, &observed);
              std::printf("metal_to_vulkan_signal result=%d value=%llu event_value=%llu\n", int(qr), (unsigned long long)observed, (unsigned long long)event.signaledValue);
              VkSemaphoreSignalInfo signal{}; signal.sType = VK_STRUCTURE_TYPE_SEMAPHORE_SIGNAL_INFO; signal.semaphore = semaphore; signal.value = 9;
              VkResult sr = vkSignalSemaphore(device, &signal);
              std::printf("vulkan_to_metal_signal result=%d event_value=%llu\n", int(sr), (unsigned long long)event.signaledValue);
              VkImportMetalSharedEventInfoEXT import{}; import.sType = VK_STRUCTURE_TYPE_IMPORT_METAL_SHARED_EVENT_INFO_EXT; import.mtlSharedEvent = event;
              VkSemaphoreTypeCreateInfo importType{}; importType.sType = VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO; importType.pNext = &import; importType.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE; importType.initialValue = 0;
              VkSemaphoreCreateInfo importInfo{}; importInfo.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO; importInfo.pNext = &importType;
              VkSemaphore imported = VK_NULL_HANDLE; VkResult ir = vkCreateSemaphore(device, &importInfo, nullptr, &imported);
              std::printf("import_shared_event_semaphore_create=%d\n", int(ir));
              if (ir == VK_SUCCESS) {
                id<MTLCommandBuffer> cb2 = [exportedQueue commandBuffer]; [cb2 encodeSignalEvent:event value:11]; [cb2 commit]; [cb2 waitUntilCompleted];
                VkSemaphoreWaitInfo wi{}; wi.sType = VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO; wi.semaphoreCount = 1; wi.pSemaphores = &imported; uint64_t target = 11; wi.pValues = &target;
                VkResult wr = vkWaitSemaphores(device, &wi, 1000000000ull);
                uint64_t iv = 0; vkGetSemaphoreCounterValue(device, imported, &iv);
                std::printf("metal_to_imported_vulkan_wait result=%d value=%llu event_value=%llu\n", int(wr), (unsigned long long)iv, (unsigned long long)event.signaledValue);
                vkDestroySemaphore(device, imported, nullptr);
              }
            }
            vkDestroySemaphore(device, semaphore, nullptr);
          }
        }
      }
    }
    vkDeviceWaitIdle(device);
    vkDestroyDevice(device, nullptr);
    vkDestroySurfaceKHR(instance, surface, nullptr);
    vkDestroyInstance(instance, nullptr);
    [window close];
  }
  return 0;
}
