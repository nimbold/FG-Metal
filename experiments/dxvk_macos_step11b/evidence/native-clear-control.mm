#define VK_USE_PLATFORM_METAL_EXT
#define VK_ENABLE_BETA_EXTENSIONS
#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#include <vulkan/vulkan.h>
#include <vulkan/vulkan_metal.h>
#include <mach/mach_time.h>
#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static bool hasExt(const std::vector<VkExtensionProperties>& xs, const char* name) {
  return std::any_of(xs.begin(), xs.end(), [&](const auto& x) { return std::strcmp(x.extensionName, name) == 0; });
}
static bool ok(VkResult r, const char* op) { if (r == VK_SUCCESS || r == VK_SUBOPTIMAL_KHR) return true; std::fprintf(stderr,"FAIL %s=%d\n",op,int(r)); return false; }
static uint64_t nowNs() { static mach_timebase_info_data_t tb=[] { mach_timebase_info_data_t x{}; mach_timebase_info(&x); return x; }(); return (mach_absolute_time() * tb.numer) / tb.denom; }

int main() {
  setbuf(stdout, nullptr);
  @autoreleasepool {
    [NSApplication sharedApplication];
    NSRect r = NSMakeRect(120, 120, 640, 360);
    NSWindow* w = [[NSWindow alloc] initWithContentRect:r styleMask:(NSWindowStyleMaskTitled|NSWindowStyleMaskClosable) backing:NSBackingStoreBuffered defer:NO];
    [w setReleasedWhenClosed:NO];
    [w setTitle:@"Step 11B NATIVE Vulkan clear control"];
    NSView* v = [[NSView alloc] initWithFrame:r];
    CAMetalLayer* layer = [CAMetalLayer layer]; layer.device = MTLCreateSystemDefaultDevice(); layer.pixelFormat = MTLPixelFormatBGRA8Unorm; layer.drawableSize = CGSizeMake(640,360); v.wantsLayer = YES; v.layer = layer; [w setContentView:v]; [w orderFrontRegardless]; [NSApp activateIgnoringOtherApps:YES];
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];

    uint32_t ic = 0; vkEnumerateInstanceExtensionProperties(nullptr,&ic,nullptr); std::vector<VkExtensionProperties> ie(ic); vkEnumerateInstanceExtensionProperties(nullptr,&ic,ie.data());
    const char* reqi[] = {VK_KHR_SURFACE_EXTENSION_NAME,VK_EXT_METAL_SURFACE_EXTENSION_NAME};
    VkExportMetalObjectCreateInfoEXT deviceIntent{}; deviceIntent.sType=VK_STRUCTURE_TYPE_EXPORT_METAL_OBJECT_CREATE_INFO_EXT; deviceIntent.exportObjectType=VK_EXPORT_METAL_OBJECT_TYPE_METAL_DEVICE_BIT_EXT;
    VkApplicationInfo ai{}; ai.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO; ai.pApplicationName="fgmetal-vk-timing"; ai.apiVersion=VK_API_VERSION_1_3;
    VkInstanceCreateInfo ci{}; ci.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO; ci.pNext=hasExt(ie,VK_EXT_METAL_OBJECTS_EXTENSION_NAME)?&deviceIntent:nullptr; ci.pApplicationInfo=&ai; ci.enabledExtensionCount=2; ci.ppEnabledExtensionNames=reqi;
    VkInstance inst{}; if(!ok(vkCreateInstance(&ci,nullptr,&inst),"vkCreateInstance")) return 2;
    auto createMetalSurface=(PFN_vkCreateMetalSurfaceEXT)vkGetInstanceProcAddr(inst,"vkCreateMetalSurfaceEXT");
    VkMetalSurfaceCreateInfoEXT ms{}; ms.sType=VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT; ms.pLayer=layer; VkSurfaceKHR surface{}; if(!createMetalSurface||!ok(createMetalSurface(inst,&ms,nullptr,&surface),"create surface")) return 3;
    uint32_t pc=0; vkEnumeratePhysicalDevices(inst,&pc,nullptr); std::vector<VkPhysicalDevice> ps(pc); vkEnumeratePhysicalDevices(inst,&pc,ps.data()); VkPhysicalDevice p=ps[0];
    VkPhysicalDeviceProperties2 p2{}; p2.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2; VkPhysicalDeviceDriverProperties dp{}; dp.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DRIVER_PROPERTIES; p2.pNext=&dp; vkGetPhysicalDeviceProperties2(p,&p2);
    std::printf("driver=%s %s device=%s api=%u.%u.%u\n",dp.driverName,dp.driverInfo,p2.properties.deviceName,VK_VERSION_MAJOR(p2.properties.apiVersion),VK_VERSION_MINOR(p2.properties.apiVersion),VK_VERSION_PATCH(p2.properties.apiVersion));
    uint32_t ec=0; vkEnumerateDeviceExtensionProperties(p,nullptr,&ec,nullptr); std::vector<VkExtensionProperties> de(ec); vkEnumerateDeviceExtensionProperties(p,nullptr,&ec,de.data());
    for (const char* n : {VK_KHR_SWAPCHAIN_EXTENSION_NAME,VK_KHR_PRESENT_ID_EXTENSION_NAME,VK_KHR_PRESENT_WAIT_EXTENSION_NAME,VK_KHR_SWAPCHAIN_MAINTENANCE_1_EXTENSION_NAME,VK_GOOGLE_DISPLAY_TIMING_EXTENSION_NAME,VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME}) std::printf("ext %s %u\n",n,[&]{auto it=std::find_if(de.begin(),de.end(),[&](auto& x){return !std::strcmp(x.extensionName,n);});return it==de.end()?0u:it->specVersion;}());
    VkPhysicalDevicePresentIdFeaturesKHR pidf{}; pidf.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR;
    VkPhysicalDevicePresentWaitFeaturesKHR pwf{}; pwf.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR;
    VkPhysicalDeviceSwapchainMaintenance1FeaturesKHR smf{}; smf.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR;
    pidf.pNext=&pwf; pwf.pNext=&smf; VkPhysicalDeviceFeatures2 f2{}; f2.sType=VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2; f2.pNext=&pidf; vkGetPhysicalDeviceFeatures2(p,&f2);
    std::printf("features presentId=%u presentWait=%u maintenance1=%u\n",pidf.presentId,pwf.presentWait,smf.swapchainMaintenance1);
    uint32_t qc=0; vkGetPhysicalDeviceQueueFamilyProperties(p,&qc,nullptr); std::vector<VkQueueFamilyProperties> qp(qc); vkGetPhysicalDeviceQueueFamilyProperties(p,&qc,qp.data()); uint32_t qf=UINT32_MAX;
    for(uint32_t i=0;i<qc;i++){VkBool32 s=0;vkGetPhysicalDeviceSurfaceSupportKHR(p,i,surface,&s);if(s&&(qp[i].queueFlags&VK_QUEUE_GRAPHICS_BIT)){qf=i;break;}} if(qf==UINT32_MAX)return 4;
    std::vector<const char*> exts{VK_KHR_SWAPCHAIN_EXTENSION_NAME,VK_GOOGLE_DISPLAY_TIMING_EXTENSION_NAME};
    if(hasExt(de,VK_KHR_PRESENT_ID_EXTENSION_NAME)&&pidf.presentId)exts.push_back(VK_KHR_PRESENT_ID_EXTENSION_NAME);
    if(hasExt(de,VK_KHR_PRESENT_WAIT_EXTENSION_NAME)&&pwf.presentWait)exts.push_back(VK_KHR_PRESENT_WAIT_EXTENSION_NAME);
    if(hasExt(de,VK_KHR_SWAPCHAIN_MAINTENANCE_1_EXTENSION_NAME)&&smf.swapchainMaintenance1)exts.push_back(VK_KHR_SWAPCHAIN_MAINTENANCE_1_EXTENSION_NAME);
    if(hasExt(de,VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME))exts.push_back(VK_KHR_PORTABILITY_SUBSET_EXTENSION_NAME);
    float pri=1.f; VkDeviceQueueCreateInfo qi{}; qi.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO; qi.queueFamilyIndex=qf; qi.queueCount=1; qi.pQueuePriorities=&pri;
    VkDeviceCreateInfo dci{}; dci.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO; dci.pNext=&pidf; dci.queueCreateInfoCount=1; dci.pQueueCreateInfos=&qi; dci.enabledExtensionCount=(uint32_t)exts.size(); dci.ppEnabledExtensionNames=exts.data();
    VkDevice d{}; if(!ok(vkCreateDevice(p,&dci,nullptr,&d),"vkCreateDevice"))return 5; VkQueue q{}; vkGetDeviceQueue(d,qf,0,&q);
    VkSurfaceCapabilitiesKHR caps{}; vkGetPhysicalDeviceSurfaceCapabilitiesKHR(p,surface,&caps); uint32_t mc=0; vkGetPhysicalDeviceSurfacePresentModesKHR(p,surface,&mc,nullptr); std::vector<VkPresentModeKHR> modes(mc);vkGetPhysicalDeviceSurfacePresentModesKHR(p,surface,&mc,modes.data());
    uint32_t fc=0;vkGetPhysicalDeviceSurfaceFormatsKHR(p,surface,&fc,nullptr);std::vector<VkSurfaceFormatKHR> fs(fc);vkGetPhysicalDeviceSurfaceFormatsKHR(p,surface,&fc,fs.data());
    VkSurfaceFormatKHR sf=fs[0]; for(auto x:fs)if(x.format==VK_FORMAT_B8G8R8A8_UNORM&&x.colorSpace==VK_COLOR_SPACE_SRGB_NONLINEAR_KHR){sf=x;break;}
    VkExtent2D extent=caps.currentExtent; if(extent.width==UINT32_MAX)extent={640,360}; uint32_t count=std::min(caps.minImageCount+1,caps.maxImageCount?caps.maxImageCount:UINT32_MAX);
    VkSwapchainCreateInfoKHR sci{};sci.sType=VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;sci.surface=surface;sci.minImageCount=count;sci.imageFormat=sf.format;sci.imageColorSpace=sf.colorSpace;sci.imageExtent=extent;sci.imageArrayLayers=1;sci.imageUsage=VK_IMAGE_USAGE_TRANSFER_DST_BIT;sci.imageSharingMode=VK_SHARING_MODE_EXCLUSIVE;sci.preTransform=caps.currentTransform;sci.compositeAlpha=(caps.supportedCompositeAlpha&VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR)?VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR:VK_COMPOSITE_ALPHA_INHERIT_BIT_KHR;sci.presentMode=VK_PRESENT_MODE_FIFO_KHR;sci.clipped=VK_TRUE;
    VkSwapchainKHR sw{};if(!ok(vkCreateSwapchainKHR(d,&sci,nullptr,&sw),"vkCreateSwapchain"))return 6;uint32_t nimg=0;vkGetSwapchainImagesKHR(d,sw,&nimg,nullptr);std::vector<VkImage> imgs(nimg);vkGetSwapchainImagesKHR(d,sw,&nimg,imgs.data());std::vector<bool> initialized(nimg,false);
    auto getRefresh=(PFN_vkGetRefreshCycleDurationGOOGLE)vkGetDeviceProcAddr(d,"vkGetRefreshCycleDurationGOOGLE");auto getPast=(PFN_vkGetPastPresentationTimingGOOGLE)vkGetDeviceProcAddr(d,"vkGetPastPresentationTimingGOOGLE");auto waitPresent=(PFN_vkWaitForPresentKHR)vkGetDeviceProcAddr(d,"vkWaitForPresentKHR");
    VkRefreshCycleDurationGOOGLE refresh{};VkResult rr=getRefresh?getRefresh(d,sw,&refresh):VK_ERROR_EXTENSION_NOT_PRESENT;std::printf("surface=%ux%u images=%u mode=FIFO refresh_query=%d refresh_ns=%llu presentId=%s presentWait=%s google=%s\n",extent.width,extent.height,nimg,int(rr),(unsigned long long)refresh.refreshDuration,pidf.presentId?"yes":"no",pwf.presentWait?"yes":"no",getPast?"yes":"no");
    VkCommandPoolCreateInfo pci{};pci.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;pci.queueFamilyIndex=qf;pci.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;VkCommandPool pool{};if(!ok(vkCreateCommandPool(d,&pci,nullptr,&pool),"create cmd pool"))return 7;
    VkCommandBufferAllocateInfo cai{};cai.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;cai.commandPool=pool;cai.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;cai.commandBufferCount=1;VkCommandBuffer cb{};vkAllocateCommandBuffers(d,&cai,&cb);
    VkSemaphoreCreateInfo semci{};semci.sType=VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;VkSemaphore acquired{},rendered{};vkCreateSemaphore(d,&semci,nullptr,&acquired);vkCreateSemaphore(d,&semci,nullptr,&rendered);
    VkFenceCreateInfo fci{};fci.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;VkFence submitFence{};vkCreateFence(d,&fci,nullptr,&submitFence);
    VkPipelineStageFlags waitStage=VK_PIPELINE_STAGE_TRANSFER_BIT;
    const uint32_t frames=1800;uint64_t start=nowNs();uint64_t period=refresh.refreshDuration?refresh.refreshDuration:16666667ull;uint64_t totalWait=0,totalFence=0;uint32_t okPresents=0;uint64_t lastId=0;
    for(uint32_t i=0;i<frames;i++){
      uint32_t ix=0;VkResult ar=vkAcquireNextImageKHR(d,sw,UINT64_MAX,acquired,VK_NULL_HANDLE,&ix);if(!ok(ar,"acquire"))break;
      vkResetCommandBuffer(cb,0);VkCommandBufferBeginInfo bi{};bi.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;bi.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;vkBeginCommandBuffer(cb,&bi);
      VkImageMemoryBarrier b1{};b1.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;b1.srcAccessMask=0;b1.dstAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;b1.oldLayout=initialized[ix]?VK_IMAGE_LAYOUT_PRESENT_SRC_KHR:VK_IMAGE_LAYOUT_UNDEFINED;b1.newLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;b1.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;b1.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;b1.image=imgs[ix];b1.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&b1);
      VkClearColorValue color{};bool generated=((i/120)%2)==1;color.float32[0]=generated?0.02f:0.85f;color.float32[1]=generated?0.8f:0.08f;color.float32[2]=generated?0.2f:0.1f;color.float32[3]=1.f;VkImageSubresourceRange range{VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};vkCmdClearColorImage(cb,imgs[ix],VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,&color,1,&range);
      VkImageMemoryBarrier b2{};b2.sType=VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;b2.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;b2.dstAccessMask=0;b2.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;b2.newLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;b2.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;b2.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;b2.image=imgs[ix];b2.subresourceRange=range;vkCmdPipelineBarrier(cb,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,0,0,nullptr,0,nullptr,1,&b2);vkEndCommandBuffer(cb);
      vkResetFences(d,1,&submitFence);VkSubmitInfo si{};si.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO;si.waitSemaphoreCount=1;si.pWaitSemaphores=&acquired;si.pWaitDstStageMask=&waitStage;si.commandBufferCount=1;si.pCommandBuffers=&cb;si.signalSemaphoreCount=1;si.pSignalSemaphores=&rendered;if(!ok(vkQueueSubmit(q,1,&si,submitFence),"submit"))break;
      uint32_t id=i+1;uint64_t desired=start+period*(uint64_t)(i+2);VkPresentIdKHR pid{};pid.sType=VK_STRUCTURE_TYPE_PRESENT_ID_KHR;pid.swapchainCount=1;uint64_t id64=id;pid.pPresentIds=&id64;
      VkPresentTimeGOOGLE tm{};tm.presentID=id;tm.desiredPresentTime=0;VkPresentTimesInfoGOOGLE ti{};ti.sType=VK_STRUCTURE_TYPE_PRESENT_TIMES_INFO_GOOGLE;ti.pNext=nullptr;ti.swapchainCount=1;ti.pTimes=&tm;
      
      VkPresentInfoKHR pi{};pi.sType=VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;pi.pNext=&ti;pi.waitSemaphoreCount=1;pi.pWaitSemaphores=&rendered;pi.swapchainCount=1;pi.pSwapchains=&sw;pi.pImageIndices=&ix;
      uint64_t before=nowNs();VkResult pr=vkQueuePresentKHR(q,&pi);uint64_t after=nowNs();if(pr==VK_SUCCESS||pr==VK_SUBOPTIMAL_KHR)okPresents++;
      VkResult sfw=vkWaitForFences(d,1,&submitFence,VK_TRUE,2000000000ull);uint64_t submitDone=nowNs();VkResult pfw=VK_ERROR_EXTENSION_NOT_PRESENT;uint64_t fenceDone=after;
      VkResult wpr=VK_ERROR_EXTENSION_NOT_PRESENT;uint64_t presentDone=after;
      initialized[ix]=true;
      std::printf("present id=%u class=%s result=%d submit_ns=%llu desired_ns=%llu present_call_ns=%llu submit_fence=%d present_fence=%d present_wait=%d fence_elapsed_ns=%llu wait_elapsed_ns=%llu\n",id,generated?"synthetic":"source",int(pr),(unsigned long long)before,(unsigned long long)desired,(unsigned long long)(after-before),int(sfw),int(pfw),int(wpr),(unsigned long long)(fenceDone-after),(unsigned long long)(presentDone-after));
    }
    vkDeviceWaitIdle(d);uint32_t tc=0;VkResult tr=getPast?getPast(d,sw,&tc,nullptr):VK_ERROR_EXTENSION_NOT_PRESENT;std::vector<VkPastPresentationTimingGOOGLE> timing(tc);if(tc&&getPast)tr=getPast(d,sw,&tc,timing.data());
    std::printf("summary attempted=%u presented=%u timing_result=%d actual_count=%u refresh_ns=%llu avg_present_fence_ns=%llu avg_present_wait_ns=%llu\n",frames,okPresents,int(tr),tc,(unsigned long long)period,(unsigned long long)(totalFence/(okPresents?okPresents:1)),(unsigned long long)(totalWait/(okPresents?okPresents:1)));
    for(uint32_t i=0;i<tc;i++)std::printf("actual id=%u desired_ns=%llu actual_ns=%llu earliest_ns=%llu margin_ns=%llu delta_ns=%lld\n",timing[i].presentID,(unsigned long long)timing[i].desiredPresentTime,(unsigned long long)timing[i].actualPresentTime,(unsigned long long)timing[i].earliestPresentTime,(unsigned long long)timing[i].presentMargin,(long long)(timing[i].actualPresentTime-timing[i].desiredPresentTime));
    std::printf("elapsed_ns=%llu\n",(unsigned long long)(nowNs()-start));
    vkDestroyFence(d,submitFence,nullptr);vkDestroySemaphore(d,acquired,nullptr);vkDestroySemaphore(d,rendered,nullptr);vkDestroyCommandPool(d,pool,nullptr);vkDestroySwapchainKHR(d,sw,nullptr);vkDestroyDevice(d,nullptr);vkDestroySurfaceKHR(inst,surface,nullptr);vkDestroyInstance(inst,nullptr);[w close];
  }
  std::fflush(stdout);return 0;
}
