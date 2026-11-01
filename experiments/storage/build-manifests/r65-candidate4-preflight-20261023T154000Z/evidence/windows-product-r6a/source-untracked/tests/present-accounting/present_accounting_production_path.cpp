#include "../../src/d3d11/d3d11_swapchain.h"
#include "../../src/dxgi/dxgi_swapchain_dispatcher.h"
#include "../../src/dxvk/dxvk_device.h"
#include "../../src/dxvk/dxvk_instance.h"
#include "../../src/dxvk/dxvk_latency_builtin.h"
#include "../../src/dxvk/dxvk_present_accounting_test.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <initializer_list>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace dxvk {

  namespace {

    void require(bool condition, const char* message) {
      if (!condition)
        throw std::runtime_error(message);
    }

    template<typename Handle>
    Handle makeHandle(uint64_t value) {
      if constexpr (std::is_pointer_v<Handle>)
        return reinterpret_cast<Handle>(uintptr_t(value));
      else
        return static_cast<Handle>(value);
    }

    template<typename Handle>
    uint64_t handleKey(Handle handle) {
      if constexpr (std::is_pointer_v<Handle>)
        return uint64_t(reinterpret_cast<uintptr_t>(handle));
      else
        return uint64_t(handle);
    }

    class FakeVulkan;

    std::mutex g_fakeRegistryMutex;
    std::unordered_map<uint64_t, FakeVulkan*> g_fakeDevices;
    std::unordered_map<uint64_t, FakeVulkan*> g_fakeQueues;
    std::unordered_map<uint64_t, FakeVulkan*> g_fakePhysicalDevices;
    std::unordered_map<uint64_t, FakeVulkan*> g_fakeInstances;
    std::atomic<uint64_t> g_fakeHandleSerial { 0x1000u };

    FakeVulkan* fakeForDevice(VkDevice device);
    FakeVulkan* fakeForQueue(VkQueue queue);
    FakeVulkan* fakeForPhysicalDevice(VkPhysicalDevice physicalDevice);
    FakeVulkan* fakeForInstance(VkInstance instance);

    class FakeVulkan {
    public:
      FakeVulkan() {
        uint64_t serial = g_fakeHandleSerial.fetch_add(0x100u);
        m_instance = makeHandle<VkInstance>(serial + 1u);
        m_physicalDevice = makeHandle<VkPhysicalDevice>(serial + 2u);
        m_device = makeHandle<VkDevice>(serial + 3u);
        m_queue = makeHandle<VkQueue>(serial + 4u);
        m_surface = makeHandle<VkSurfaceKHR>(serial + 5u);

        std::lock_guard lock(g_fakeRegistryMutex);
        g_fakeDevices.emplace(handleKey(m_device), this);
        g_fakeQueues.emplace(handleKey(m_queue), this);
        g_fakePhysicalDevices.emplace(handleKey(m_physicalDevice), this);
        g_fakeInstances.emplace(handleKey(m_instance), this);
      }

      ~FakeVulkan() {
        std::lock_guard lock(g_fakeRegistryMutex);
        g_fakeDevices.erase(handleKey(m_device));
        g_fakeQueues.erase(handleKey(m_queue));
        g_fakePhysicalDevices.erase(handleKey(m_physicalDevice));
        g_fakeInstances.erase(handleKey(m_instance));
      }

      VkInstance instance() const { return m_instance; }
      VkPhysicalDevice physicalDevice() const { return m_physicalDevice; }
      VkDevice device() const { return m_device; }
      VkQueue queue() const { return m_queue; }
      VkSurfaceKHR surface() const { return m_surface; }

      void setAcquireResults(std::initializer_list<VkResult> results) {
        std::lock_guard lock(m_mutex);
        m_acquireResults.assign(results.begin(), results.end());
      }

      void setPresentResults(std::initializer_list<VkResult> results) {
        std::lock_guard lock(m_mutex);
        m_presentResults.assign(results.begin(), results.end());
      }

      void enableTimingReports(bool enable) {
        std::lock_guard lock(m_mutex);
        m_timingReportsEnabled = enable;
      }

      void synchronize(VkSemaphore acquire, VkSemaphore present, bool dropPresentSignal) {
        std::lock_guard lock(m_mutex);
        auto acquireToken = m_binarySemaphores.find(handleKey(acquire));
        auto presentToken = m_binarySemaphores.find(handleKey(present));

        if (acquireToken == m_binarySemaphores.end() || !acquireToken->second)
          m_semaphoreViolation = true;
        else
          acquireToken->second = false;

        if (presentToken == m_binarySemaphores.end()) {
          m_semaphoreViolation = true;
        } else {
          presentToken->second = !dropPresentSignal;
        }

        if (dropPresentSignal)
          m_droppedPresentSignals += 1u;
      }

      void dropNextPresentSignal() {
        std::lock_guard lock(m_mutex);
        m_dropNextPresentSignal = true;
      }

      void synchronize(VkSemaphore acquire, VkSemaphore present) {
        bool dropSignal = false;
        {
          std::lock_guard lock(m_mutex);
          dropSignal = std::exchange(m_dropNextPresentSignal, false);
        }
        synchronize(acquire, present, dropSignal);
      }

      std::vector<uint64_t> presentIds() const {
        std::lock_guard lock(m_mutex);
        return m_presentIds;
      }

      std::vector<uint64_t> timingReportIds() const {
        std::lock_guard lock(m_mutex);
        return m_timingReportIds;
      }

      bool semaphoreViolation() const {
        std::lock_guard lock(m_mutex);
        return m_semaphoreViolation;
      }

      uint32_t droppedPresentSignals() const {
        std::lock_guard lock(m_mutex);
        return m_droppedPresentSignals;
      }

      uint32_t timingQueryCount() const {
        std::lock_guard lock(m_mutex);
        return m_timingQueryCount;
      }

      uint32_t importedImageCount() const {
        std::lock_guard lock(m_mutex);
        return m_importedImageCount;
      }

      uint32_t swapchainCreateCount() const {
        std::lock_guard lock(m_mutex);
        return m_swapchainCreateCount;
      }

      uint32_t enumeratedImageCount() const {
        std::lock_guard lock(m_mutex);
        return m_enumeratedImageCount;
      }

      void noteImportedImage() {
        std::lock_guard lock(m_mutex);
        m_importedImageCount += 1u;
      }

      void enableDispatch(const Rc<vk::InstanceFn>& vki, const Rc<vk::DeviceFn>& vkd) {
        vki->vkDestroySurfaceKHR = &destroySurface;
        vki->vkGetPhysicalDeviceSurfaceCapabilities2KHR = &getSurfaceCapabilities2;
        vki->vkGetPhysicalDeviceSurfaceFormats2KHR = &getSurfaceFormats2;
        vki->vkGetPhysicalDeviceSurfaceCapabilitiesKHR = &getSurfaceCapabilities;
        vki->vkGetPhysicalDeviceSurfaceFormatsKHR = &getSurfaceFormats;
        vki->vkGetPhysicalDeviceSurfacePresentModesKHR = &getPresentModes;
        vki->vkGetPhysicalDeviceCalibrateableTimeDomainsKHR = &getCalibrateableTimeDomains;

        vkd->vkCreateSemaphore = &createSemaphore;
        vkd->vkDestroySemaphore = &destroySemaphore;
        vkd->vkCreateFence = &createFence;
        vkd->vkDestroyFence = &destroyFence;
        vkd->vkWaitForFences = &waitForFences;
        vkd->vkResetFences = &resetFences;
        vkd->vkCreateSwapchainKHR = &createSwapchain;
        vkd->vkDestroySwapchainKHR = &destroySwapchain;
        vkd->vkGetSwapchainImagesKHR = &getSwapchainImages;
        vkd->vkAcquireNextImageKHR = &acquireNextImage;
        vkd->vkQueuePresentKHR = &queuePresent;
        vkd->vkWaitForPresent2KHR = &waitForPresent2;
        vkd->vkSetSwapchainPresentTimingQueueSizeEXT = &setTimingQueueSize;
        vkd->vkGetSwapchainTimingPropertiesEXT = &getSwapchainTimingProperties;
        vkd->vkGetSwapchainTimeDomainPropertiesEXT = &getSwapchainTimeDomainProperties;
        vkd->vkGetCalibratedTimestampsKHR = &getCalibratedTimestamps;
        vkd->vkGetPastPresentationTimingEXT = &getPastPresentationTiming;
        vkd->vkGetDeviceQueue = &getDeviceQueue;
        vkd->vkQueueSubmit = &queueSubmit;
        vkd->vkQueueSubmit2 = &queueSubmit2;
        vkd->vkQueueWaitIdle = &queueWaitIdle;
        vkd->vkDeviceWaitIdle = &deviceWaitIdle;
        vkd->vkWaitSemaphores = &waitSemaphores;
        vkd->vkCreateDescriptorSetLayout = &createDescriptorSetLayout;
        vkd->vkDestroyDescriptorSetLayout = &destroyDescriptorSetLayout;
        vkd->vkCreateDescriptorPool = &createDescriptorPool;
        vkd->vkDestroyDescriptorPool = &destroyDescriptorPool;
        vkd->vkAllocateDescriptorSets = &allocateDescriptorSets;
        vkd->vkCreateSampler = &createSampler;
        vkd->vkDestroySampler = &destroySampler;
        vkd->vkUpdateDescriptorSets = &updateDescriptorSets;
        vkd->vkGetDeviceBufferMemoryRequirements = &getDeviceBufferMemoryRequirements;
        vkd->vkGetDeviceImageMemoryRequirements = &getDeviceImageMemoryRequirements;
        vkd->vkGetBufferMemoryRequirements = &getBufferMemoryRequirements;
        vkd->vkGetBufferMemoryRequirements2 = &getBufferMemoryRequirements2;
        vkd->vkGetImageMemoryRequirements = &getImageMemoryRequirements;
        vkd->vkGetImageMemoryRequirements2 = &getImageMemoryRequirements2;
      }

      static PFN_vkVoidFunction VKAPI_CALL getInstanceProcAddr(VkInstance, const char* name) {
        if (std::strcmp(name, "vkGetDeviceProcAddr") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getDeviceProcAddr);
        return nullptr;
      }

      static PFN_vkVoidFunction VKAPI_CALL getDeviceProcAddr(VkDevice, const char* name) {
        if (std::strcmp(name, "vkGetDeviceBufferMemoryRequirements") == 0
         || std::strcmp(name, "vkGetDeviceBufferMemoryRequirementsKHR") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getDeviceBufferMemoryRequirements);
        if (std::strcmp(name, "vkGetDeviceImageMemoryRequirements") == 0
         || std::strcmp(name, "vkGetDeviceImageMemoryRequirementsKHR") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getDeviceImageMemoryRequirements);
        if (std::strcmp(name, "vkGetBufferMemoryRequirements") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getBufferMemoryRequirements);
        if (std::strcmp(name, "vkGetBufferMemoryRequirements2") == 0
         || std::strcmp(name, "vkGetBufferMemoryRequirements2KHR") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getBufferMemoryRequirements2);
        if (std::strcmp(name, "vkGetImageMemoryRequirements") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getImageMemoryRequirements);
        if (std::strcmp(name, "vkGetImageMemoryRequirements2") == 0
         || std::strcmp(name, "vkGetImageMemoryRequirements2KHR") == 0)
          return reinterpret_cast<PFN_vkVoidFunction>(&getImageMemoryRequirements2);
        return nullptr;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createSurface(
              VkInstance     instance,
              VkSurfaceKHR*  surface) {
        auto* fake = fakeForInstance(instance);
        if (!fake || !surface)
          return VK_ERROR_INITIALIZATION_FAILED;
        *surface = fake->m_surface;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL destroySurface(
              VkInstance,
              VkSurfaceKHR,
        const VkAllocationCallbacks*) {
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSurfaceCapabilities2(
              VkPhysicalDevice,
        const VkPhysicalDeviceSurfaceInfo2KHR*,
              VkSurfaceCapabilities2KHR* capabilities) {
        if (!capabilities)
          return VK_ERROR_INITIALIZATION_FAILED;

        auto& surface = capabilities->surfaceCapabilities;
        surface.minImageCount = 2u;
        surface.maxImageCount = 3u;
        surface.currentExtent = { UINT32_MAX, UINT32_MAX };
        surface.minImageExtent = { 1u, 1u };
        surface.maxImageExtent = { 4096u, 4096u };
        surface.maxImageArrayLayers = 1u;
        surface.supportedTransforms = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
        surface.currentTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
        surface.supportedCompositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
        surface.supportedUsageFlags = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT
                                    | VK_IMAGE_USAGE_TRANSFER_DST_BIT;

        auto* next = static_cast<VkBaseOutStructure*>(capabilities->pNext);
        while (next) {
          if (next->sType == VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_PRESENT_ID_2_KHR) {
            auto* presentId = reinterpret_cast<VkSurfaceCapabilitiesPresentId2KHR*>(next);
            presentId->presentId2Supported = VK_TRUE;
          } else if (next->sType == VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_PRESENT_WAIT_2_KHR) {
            auto* presentWait = reinterpret_cast<VkSurfaceCapabilitiesPresentWait2KHR*>(next);
            presentWait->presentWait2Supported = VK_TRUE;
          } else if (next->sType == VK_STRUCTURE_TYPE_PRESENT_TIMING_SURFACE_CAPABILITIES_EXT) {
            auto* timing = reinterpret_cast<VkPresentTimingSurfaceCapabilitiesEXT*>(next);
            timing->presentTimingSupported = VK_TRUE;
            timing->presentAtAbsoluteTimeSupported = VK_TRUE;
            timing->presentAtRelativeTimeSupported = VK_TRUE;
            timing->presentStageQueries = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
          }
          next = static_cast<VkBaseOutStructure*>(next->pNext);
        }

        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSurfaceCapabilities(
              VkPhysicalDevice,
              VkSurfaceKHR,
              VkSurfaceCapabilitiesKHR* capabilities) {
        if (!capabilities)
          return VK_ERROR_INITIALIZATION_FAILED;
        VkSurfaceCapabilities2KHR output = { VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_2_KHR };
        VkResult result = getSurfaceCapabilities2(VK_NULL_HANDLE, nullptr, &output);
        *capabilities = output.surfaceCapabilities;
        return result;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSurfaceFormats2(
              VkPhysicalDevice,
        const VkPhysicalDeviceSurfaceInfo2KHR*,
              uint32_t*                       count,
              VkSurfaceFormat2KHR*            formats) {
        if (!count)
          return VK_ERROR_INITIALIZATION_FAILED;
        if (!formats) {
          *count = 1u;
          return VK_SUCCESS;
        }

        uint32_t written = std::min(*count, 1u);
        for (uint32_t i = 0; i < written; i++) {
          formats[i].surfaceFormat.format = VK_FORMAT_R8G8B8A8_UNORM;
          formats[i].surfaceFormat.colorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
        }
        *count = written;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSurfaceFormats(
              VkPhysicalDevice,
              VkSurfaceKHR,
              uint32_t*      count,
              VkSurfaceFormatKHR* formats) {
        if (!count)
          return VK_ERROR_INITIALIZATION_FAILED;
        if (!formats) {
          *count = 1u;
          return VK_SUCCESS;
        }
        if (*count) {
          formats[0] = { VK_FORMAT_R8G8B8A8_UNORM, VK_COLOR_SPACE_SRGB_NONLINEAR_KHR };
          *count = 1u;
        }
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getPresentModes(
              VkPhysicalDevice,
              VkSurfaceKHR,
              uint32_t*      count,
              VkPresentModeKHR* modes) {
        if (!count)
          return VK_ERROR_INITIALIZATION_FAILED;
        if (!modes) {
          *count = 1u;
          return VK_SUCCESS;
        }
        if (*count) {
          modes[0] = VK_PRESENT_MODE_FIFO_KHR;
          *count = 1u;
        }
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getCalibrateableTimeDomains(
              VkPhysicalDevice,
              uint32_t*     count,
              VkTimeDomainKHR* domains) {
        if (!count)
          return VK_ERROR_INITIALIZATION_FAILED;
        if (!domains) {
          *count = 1u;
          return VK_SUCCESS;
        }
        if (*count) {
          domains[0] = VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR;
          *count = 1u;
        }
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createSemaphore(
              VkDevice device,
        const VkSemaphoreCreateInfo*,
        const VkAllocationCallbacks*,
              VkSemaphore* semaphore) {
        auto* fake = fakeForDevice(device);
        return fake ? fake->createSemaphoreImpl(semaphore) : VK_ERROR_INITIALIZATION_FAILED;
      }

      static VKAPI_ATTR void VKAPI_CALL destroySemaphore(
              VkDevice device,
              VkSemaphore semaphore,
        const VkAllocationCallbacks*) {
        if (auto* fake = fakeForDevice(device)) {
          std::lock_guard lock(fake->m_mutex);
          fake->m_binarySemaphores.erase(handleKey(semaphore));
        }
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createFence(
              VkDevice device,
        const VkFenceCreateInfo*,
        const VkAllocationCallbacks*,
              VkFence* fence) {
        if (auto* fake = fakeForDevice(device)) {
          *fence = makeHandle<VkFence>(g_fakeHandleSerial.fetch_add(1u));
          return VK_SUCCESS;
        }
        return VK_ERROR_INITIALIZATION_FAILED;
      }

      static VKAPI_ATTR void VKAPI_CALL destroyFence(
              VkDevice,
              VkFence,
        const VkAllocationCallbacks*) {
      }

      static VKAPI_ATTR VkResult VKAPI_CALL waitForFences(
              VkDevice,
              uint32_t,
        const VkFence*,
              VkBool32,
              uint64_t) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL resetFences(
              VkDevice,
              uint32_t,
        const VkFence*) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createSwapchain(
              VkDevice device,
        const VkSwapchainCreateInfoKHR* info,
        const VkAllocationCallbacks*,
              VkSwapchainKHR* swapchain) {
        auto* fake = fakeForDevice(device);
        if (!fake || !info || !swapchain)
          return VK_ERROR_INITIALIZATION_FAILED;

        *swapchain = makeHandle<VkSwapchainKHR>(g_fakeHandleSerial.fetch_add(1u));
        std::lock_guard lock(fake->m_mutex);
        auto& images = fake->m_swapchainImages[handleKey(*swapchain)];
        images.clear();
        uint32_t count = std::max(3u, info->minImageCount);
        for (uint32_t i = 0; i < count; i++)
          images.push_back(makeHandle<VkImage>(g_fakeHandleSerial.fetch_add(1u)));
        fake->m_swapchainCreateCount += 1u;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL destroySwapchain(
              VkDevice device,
              VkSwapchainKHR swapchain,
        const VkAllocationCallbacks*) {
        if (auto* fake = fakeForDevice(device)) {
          std::lock_guard lock(fake->m_mutex);
          fake->m_swapchainImages.erase(handleKey(swapchain));
        }
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSwapchainImages(
              VkDevice device,
              VkSwapchainKHR swapchain,
              uint32_t* count,
              VkImage* images) {
        auto* fake = fakeForDevice(device);
        if (!fake || !count)
          return VK_ERROR_INITIALIZATION_FAILED;

        std::lock_guard lock(fake->m_mutex);
        auto iter = fake->m_swapchainImages.find(handleKey(swapchain));
        if (iter == fake->m_swapchainImages.end())
          return VK_ERROR_OUT_OF_DATE_KHR;

        if (!images) {
          *count = uint32_t(iter->second.size());
          return VK_SUCCESS;
        }

        uint32_t written = std::min(*count, uint32_t(iter->second.size()));
        std::copy_n(iter->second.begin(), written, images);
        *count = written;
        fake->m_enumeratedImageCount += written;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL acquireNextImage(
              VkDevice device,
              VkSwapchainKHR swapchain,
              uint64_t,
              VkSemaphore semaphore,
              VkFence,
              uint32_t* imageIndex) {
        auto* fake = fakeForDevice(device);
        if (!fake || !imageIndex)
          return VK_ERROR_INITIALIZATION_FAILED;

        std::lock_guard lock(fake->m_mutex);
        auto result = fake->m_acquireResults.empty()
          ? VK_SUCCESS : fake->m_acquireResults.front();
        if (!fake->m_acquireResults.empty())
          fake->m_acquireResults.pop_front();

        auto images = fake->m_swapchainImages.find(handleKey(swapchain));
        if (images == fake->m_swapchainImages.end())
          return VK_ERROR_OUT_OF_DATE_KHR;

        if (result == VK_SUCCESS || result == VK_SUBOPTIMAL_KHR) {
          *imageIndex = fake->m_nextImageIndex++ % uint32_t(images->second.size());
          auto token = fake->m_binarySemaphores.find(handleKey(semaphore));
          if (token == fake->m_binarySemaphores.end())
            fake->m_semaphoreViolation = true;
          else
            token->second = true;
        }

        return result;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL queuePresent(
              VkQueue queue,
        const VkPresentInfoKHR* presentInfo) {
        if (!presentInfo)
          return VK_ERROR_INITIALIZATION_FAILED;

        uint64_t presentId = 0u;
        auto* next = reinterpret_cast<const VkBaseInStructure*>(presentInfo->pNext);
        while (next) {
          if (next->sType == VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR) {
            auto* ids = reinterpret_cast<const VkPresentId2KHR*>(next);
            if (ids->swapchainCount && ids->pPresentIds)
              presentId = ids->pPresentIds[0];
          } else if (next->sType == VK_STRUCTURE_TYPE_PRESENT_ID_KHR) {
            auto* ids = reinterpret_cast<const VkPresentIdKHR*>(next);
            if (ids->swapchainCount && ids->pPresentIds)
              presentId = ids->pPresentIds[0];
          }
          next = reinterpret_cast<const VkBaseInStructure*>(next->pNext);
        }

        // The real VkPresentInfoKHR wait semaphore is validated and consumed here.
        auto* fake = fakeForQueue(queue);
        if (!fake)
          return VK_ERROR_INITIALIZATION_FAILED;

        std::lock_guard lock(fake->m_mutex);
        fake->m_presentIds.push_back(presentId);
        if (presentId == 0u || presentInfo->waitSemaphoreCount != 1u
         || !presentInfo->pWaitSemaphores) {
          fake->m_semaphoreViolation = true;
          return VK_ERROR_UNKNOWN;
        }

        auto token = fake->m_binarySemaphores.find(handleKey(presentInfo->pWaitSemaphores[0]));
        if (token == fake->m_binarySemaphores.end() || !token->second) {
          fake->m_semaphoreViolation = true;
          return VK_ERROR_UNKNOWN;
        }
        token->second = false;

        auto result = fake->m_presentResults.empty()
          ? VK_SUCCESS : fake->m_presentResults.front();
        if (!fake->m_presentResults.empty())
          fake->m_presentResults.pop_front();
        return result;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL waitForPresent2(
              VkDevice,
              VkSwapchainKHR,
        const VkPresentWait2InfoKHR*) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL setTimingQueueSize(
              VkDevice,
              VkSwapchainKHR,
              uint32_t) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSwapchainTimingProperties(
              VkDevice,
              VkSwapchainKHR,
              VkSwapchainTimingPropertiesEXT* properties,
              uint64_t* updateCounter) {
        if (!properties)
          return VK_ERROR_INITIALIZATION_FAILED;
        properties->refreshDuration = 16666667u;
        properties->refreshInterval = 16666667u;
        if (updateCounter)
          *updateCounter = 1u;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getSwapchainTimeDomainProperties(
              VkDevice,
              VkSwapchainKHR,
              VkSwapchainTimeDomainPropertiesEXT* properties,
              uint64_t* updateCounter) {
        if (!properties)
          return VK_ERROR_INITIALIZATION_FAILED;
        if (!properties->pTimeDomains) {
          properties->timeDomainCount = 1u;
        } else if (properties->timeDomainCount) {
          properties->pTimeDomains[0] = VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
          properties->pTimeDomainIds[0] = 42u;
          properties->timeDomainCount = 1u;
        }
        if (updateCounter)
          *updateCounter = 1u;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getCalibratedTimestamps(
              VkDevice,
              uint32_t count,
        const VkCalibratedTimestampInfoKHR* infos,
              uint64_t* timestamps,
              uint64_t* maxDeviation) {
        if (!infos || !timestamps)
          return VK_ERROR_INITIALIZATION_FAILED;
        for (uint32_t i = 0; i < count; i++) {
          timestamps[i] = infos[i].timeDomain == VK_TIME_DOMAIN_QUERY_PERFORMANCE_COUNTER_KHR
            ? 5000000000ull : 10000000000ull;
        }
        if (maxDeviation)
          *maxDeviation = 0u;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL getPastPresentationTiming(
              VkDevice device,
        const VkPastPresentationTimingInfoEXT*,
              VkPastPresentationTimingPropertiesEXT* properties) {
        auto* fake = fakeForDevice(device);
        if (!fake || !properties)
          return VK_ERROR_INITIALIZATION_FAILED;

        std::lock_guard lock(fake->m_mutex);
        fake->m_timingQueryCount += 1u;
        const uint32_t capacity = properties->presentationTimingCount;
        properties->timingPropertiesCounter = 1u;
        properties->timeDomainsCounter = 1u;
        properties->presentationTimingCount = 0u;

        if (!fake->m_timingReportsEnabled || fake->m_presentIds.size() < 3u
         || !properties->pPresentationTimings)
          return VK_SUCCESS;

        // Echo WSI identities observed in actual VkPresentInfoKHR chains. This
        // returns a reordered batch with one duplicate and one unknown report.
        const uint64_t unknown = *std::max_element(fake->m_presentIds.begin(), fake->m_presentIds.end()) + 996u;
        const std::array<uint64_t, 5> reports = {
          fake->m_presentIds[2], fake->m_presentIds[0], fake->m_presentIds[1],
          fake->m_presentIds[0], unknown,
        };
        uint32_t count = std::min<uint32_t>(uint32_t(reports.size()), capacity);
        for (uint32_t i = 0; i < count; i++) {
          fake->m_timingReportIds.push_back(reports[i]);
          auto& report = properties->pPresentationTimings[i];
          report.presentId = reports[i];
          report.presentStageCount = 1u;
          report.timeDomain = VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
          report.timeDomainId = 42u;
          report.reportComplete = VK_TRUE;
          if (report.pPresentStages) {
            report.pPresentStages[0].stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
            report.pPresentStages[0].time = 10000000001ull + i;
          }
        }
        properties->presentationTimingCount = count;
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL getDeviceQueue(
              VkDevice device,
              uint32_t,
              uint32_t,
              VkQueue* queue) {
        if (auto* fake = fakeForDevice(device); fake && queue)
          *queue = fake->m_queue;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL queueSubmit(
              VkQueue,
              uint32_t,
        const VkSubmitInfo*,
              VkFence) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL queueSubmit2(
              VkQueue,
              uint32_t,
        const VkSubmitInfo2*,
              VkFence) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL queueWaitIdle(VkQueue) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL deviceWaitIdle(VkDevice) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL waitSemaphores(
              VkDevice,
        const VkSemaphoreWaitInfo*,
              uint64_t) {
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createDescriptorSetLayout(
              VkDevice device,
        const VkDescriptorSetLayoutCreateInfo*,
        const VkAllocationCallbacks*,
              VkDescriptorSetLayout* layout) {
        if (!fakeForDevice(device) || !layout)
          return VK_ERROR_INITIALIZATION_FAILED;
        *layout = makeHandle<VkDescriptorSetLayout>(g_fakeHandleSerial.fetch_add(1u));
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL destroyDescriptorSetLayout(
              VkDevice,
              VkDescriptorSetLayout,
        const VkAllocationCallbacks*) {
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createDescriptorPool(
              VkDevice device,
        const VkDescriptorPoolCreateInfo*,
        const VkAllocationCallbacks*,
              VkDescriptorPool* pool) {
        if (!fakeForDevice(device) || !pool)
          return VK_ERROR_INITIALIZATION_FAILED;
        *pool = makeHandle<VkDescriptorPool>(g_fakeHandleSerial.fetch_add(1u));
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL destroyDescriptorPool(
              VkDevice,
              VkDescriptorPool,
        const VkAllocationCallbacks*) {
      }

      static VKAPI_ATTR VkResult VKAPI_CALL allocateDescriptorSets(
              VkDevice device,
        const VkDescriptorSetAllocateInfo* info,
              VkDescriptorSet* sets) {
        if (!fakeForDevice(device) || !info || !sets)
          return VK_ERROR_INITIALIZATION_FAILED;
        for (uint32_t i = 0; i < info->descriptorSetCount; i++)
          sets[i] = makeHandle<VkDescriptorSet>(g_fakeHandleSerial.fetch_add(1u));
        return VK_SUCCESS;
      }

      static VKAPI_ATTR VkResult VKAPI_CALL createSampler(
              VkDevice device,
        const VkSamplerCreateInfo*,
        const VkAllocationCallbacks*,
              VkSampler* sampler) {
        if (!fakeForDevice(device) || !sampler)
          return VK_ERROR_INITIALIZATION_FAILED;
        *sampler = makeHandle<VkSampler>(g_fakeHandleSerial.fetch_add(1u));
        return VK_SUCCESS;
      }

      static VKAPI_ATTR void VKAPI_CALL destroySampler(
              VkDevice,
              VkSampler,
        const VkAllocationCallbacks*) {
      }

      static VKAPI_ATTR void VKAPI_CALL updateDescriptorSets(
              VkDevice,
              uint32_t,
        const VkWriteDescriptorSet*,
              uint32_t,
        const VkCopyDescriptorSet*) {
      }

      static VKAPI_ATTR void VKAPI_CALL getBufferMemoryRequirements(
              VkDevice,
              VkBuffer,
              VkMemoryRequirements* requirements) {
        if (!requirements)
          return;
        requirements->size = 65536u;
        requirements->alignment = 64u;
        requirements->memoryTypeBits = ~0u;
      }

      static VKAPI_ATTR void VKAPI_CALL getImageMemoryRequirements(
              VkDevice,
              VkImage,
              VkMemoryRequirements* requirements) {
        if (!requirements)
          return;
        requirements->size = 65536u;
        requirements->alignment = 64u;
        requirements->memoryTypeBits = ~0u;
      }

      static VKAPI_ATTR void VKAPI_CALL getDeviceBufferMemoryRequirements(
              VkDevice,
        const VkDeviceBufferMemoryRequirements* info,
              VkMemoryRequirements2* requirements) {
        if (!requirements)
          return;
        requirements->memoryRequirements.size = (info && info->pCreateInfo) ? info->pCreateInfo->size : 65536u;
        requirements->memoryRequirements.alignment = 64u;
        requirements->memoryRequirements.memoryTypeBits = ~0u;
      }

      static VKAPI_ATTR void VKAPI_CALL getDeviceImageMemoryRequirements(
              VkDevice,
        const VkDeviceImageMemoryRequirements*,
              VkMemoryRequirements2* requirements) {
        if (!requirements)
          return;
        requirements->memoryRequirements.size = 65536u;
        requirements->memoryRequirements.alignment = 64u;
        requirements->memoryRequirements.memoryTypeBits = ~0u;
      }

      static VKAPI_ATTR void VKAPI_CALL getBufferMemoryRequirements2(
              VkDevice,
        const VkBufferMemoryRequirementsInfo2*,
              VkMemoryRequirements2* requirements) {
        if (!requirements)
          return;
        requirements->memoryRequirements.size = 65536u;
        requirements->memoryRequirements.alignment = 64u;
        requirements->memoryRequirements.memoryTypeBits = ~0u;
      }

      static VKAPI_ATTR void VKAPI_CALL getImageMemoryRequirements2(
              VkDevice,
        const VkImageMemoryRequirementsInfo2*,
              VkMemoryRequirements2* requirements) {
        if (!requirements)
          return;
        requirements->memoryRequirements.size = 65536u;
        requirements->memoryRequirements.alignment = 64u;
        requirements->memoryRequirements.memoryTypeBits = ~0u;
      }

    private:
      friend FakeVulkan* fakeForDevice(VkDevice device);
      friend FakeVulkan* fakeForPhysicalDevice(VkPhysicalDevice physicalDevice);
      friend FakeVulkan* fakeForInstance(VkInstance instance);

      VkResult createSemaphoreImpl(VkSemaphore* semaphore) {
        if (!semaphore)
          return VK_ERROR_INITIALIZATION_FAILED;
        *semaphore = makeHandle<VkSemaphore>(g_fakeHandleSerial.fetch_add(1u));
        m_binarySemaphores.emplace(handleKey(*semaphore), false);
        return VK_SUCCESS;
      }

      VkInstance m_instance = VK_NULL_HANDLE;
      VkPhysicalDevice m_physicalDevice = VK_NULL_HANDLE;
      VkDevice m_device = VK_NULL_HANDLE;
      VkQueue m_queue = VK_NULL_HANDLE;
      VkSurfaceKHR m_surface = VK_NULL_HANDLE;

      mutable std::mutex m_mutex;
      std::deque<VkResult> m_acquireResults;
      std::deque<VkResult> m_presentResults;
      std::unordered_map<uint64_t, bool> m_binarySemaphores;
      std::unordered_map<uint64_t, std::vector<VkImage>> m_swapchainImages;
      std::vector<uint64_t> m_presentIds;
      std::vector<uint64_t> m_timingReportIds;
      uint32_t m_nextImageIndex = 0u;
      uint32_t m_droppedPresentSignals = 0u;
      uint32_t m_timingQueryCount = 0u;
      uint32_t m_importedImageCount = 0u;
      uint32_t m_swapchainCreateCount = 0u;
      uint32_t m_enumeratedImageCount = 0u;
      bool m_timingReportsEnabled = false;
      bool m_semaphoreViolation = false;
      bool m_dropNextPresentSignal = false;

    };

    FakeVulkan* fakeForDevice(VkDevice device) {
      std::lock_guard lock(g_fakeRegistryMutex);
      auto iter = g_fakeDevices.find(handleKey(device));
      return iter == g_fakeDevices.end() ? nullptr : iter->second;
    }

    FakeVulkan* fakeForQueue(VkQueue queue) {
      std::lock_guard lock(g_fakeRegistryMutex);
      auto iter = g_fakeQueues.find(handleKey(queue));
      return iter == g_fakeQueues.end() ? nullptr : iter->second;
    }

    FakeVulkan* fakeForPhysicalDevice(VkPhysicalDevice physicalDevice) {
      std::lock_guard lock(g_fakeRegistryMutex);
      auto iter = g_fakePhysicalDevices.find(handleKey(physicalDevice));
      return iter == g_fakePhysicalDevices.end() ? nullptr : iter->second;
    }

    FakeVulkan* fakeForInstance(VkInstance instance) {
      std::lock_guard lock(g_fakeRegistryMutex);
      auto iter = g_fakeInstances.find(handleKey(instance));
      return iter == g_fakeInstances.end() ? nullptr : iter->second;
    }

  }

  class PresentAccountingProductionFixture {
  public:
    PresentAccountingProductionFixture() {
      m_dependencies.beforeWsiCall = [this] (const PresentRequest& request) {
        bool failBeforeWsi = m_failInternalIdTwo.load(std::memory_order_acquire)
          && request.origin == PresentOrigin::INTERNAL
          && std::holds_alternative<InternalPresentId>(request.identity)
          && std::get<InternalPresentId>(request.identity).value() == 2u;
        {
          std::lock_guard lock(m_hookMutex);
          m_wsiHookRequests.push_back(request);
          if (failBeforeWsi)
            m_preWsiThreadId = std::this_thread::get_id();
        }

        if (failBeforeWsi) {
          m_preWsiFailures.fetch_add(1u, std::memory_order_relaxed);
          return true;
        }
        return false;
      };

      m_dependencies.synchronizeWsi = [this] (VkSemaphore acquire, VkSemaphore present) {
        m_fake.synchronize(acquire, present);
      };

      m_dependencies.beforeFrameProcessing = [this] (WsiPresentId wsiId) {
        std::unique_lock lock(m_frameGateMutex);
        m_frameIds.push_back(wsiId.value());
        m_frameGateCond.notify_all();
        if (wsiId.value() == 3u && m_gateWsiThree)
          m_frameGateCond.wait(lock, [this] { return m_releaseWsiThree; });
      };

      m_dependencies.importSwapchainImage = [this] (
        const DxvkImageCreateInfo&, VkImage) {
        m_fake.noteImportedImage();
      };

      m_handleDependencies.create = [this] (uint32_t, uint32_t) {
        m_handleCreates.fetch_add(1u, std::memory_order_relaxed);
        m_primaryHandle = makeHandle<HANDLE>(g_fakeHandleSerial.fetch_add(1u));
        return m_primaryHandle;
      };
      // These opaque tokens model only the production method calls and
      // ownership counts. They do not model Win32 kernel signaling/waits,
      // native handle ABI properties, or QPC behavior.
      m_handleDependencies.release = [this] (HANDLE handle, uint32_t count) {
        if (!handle || !count || handle != m_primaryHandle)
          m_handleFailures.fetch_add(1u, std::memory_order_relaxed);
        m_handleReleases.fetch_add(count, std::memory_order_relaxed);
        return true;
      };
      m_handleDependencies.duplicate = [this] (HANDLE source, HANDLE* duplicate) {
        if (!source || !duplicate || source != m_primaryHandle)
          return false;
        *duplicate = makeHandle<HANDLE>(g_fakeHandleSerial.fetch_add(1u));
        m_handleDuplicates.fetch_add(1u, std::memory_order_relaxed);
        return true;
      };
      m_handleDependencies.close = [this] (HANDLE handle) {
        if (!handle)
          m_handleFailures.fetch_add(1u, std::memory_order_relaxed);
        m_handleCloses.fetch_add(1u, std::memory_order_relaxed);
      };

      m_dxgiDependencies.isWindow = [] (HWND) { return false; };
      m_dxgiDependencies.isMinimized = [] (HWND) { return false; };
      m_dxgiDependencies.isOccluded = [] (HWND) { return false; };

      m_mainThreadId = std::this_thread::get_id();
    }

    ~PresentAccountingProductionFixture() {
      releaseWsiThree();
      if (m_device)
        m_device->m_submissionQueue.waitForIdle();
      if (m_d3d11) {
        // Keep cleanup non-throwing so an earlier assertion remains visible.
        try {
          waitForFrameDrain();
        } catch (...) {
        }
      }

      delete m_dispatcher;
      m_dispatcher = nullptr;
      if (m_dxgi) {
        m_dxgi->Release();
        m_dxgi = nullptr;
      }
      if (m_d3d11) {
        m_d3d11->Release();
        m_d3d11 = nullptr;
      }
      m_device = nullptr;
      m_vkd = nullptr;
      m_adapter = nullptr;
      m_instance = nullptr;
      m_vki = nullptr;
      m_library = nullptr;
    }

    void initialize() {
      m_library = new vk::LibraryLoader(&FakeVulkan::getInstanceProcAddr);
      m_vki = new vk::InstanceFn(m_library, false, m_fake.instance());
      m_instance = new DxvkInstance(m_vki, PresentAccountingProductionPathTag { });
      m_adapter = new DxvkAdapter(*m_instance, m_fake.physicalDevice(),
        PresentAccountingProductionPathTag { });

      Rc<vk::InstanceLoader> instanceLoader = m_vki;
      m_vkd = new vk::DeviceFn(instanceLoader, false, m_fake.device());
      m_fake.enableDispatch(m_vki, m_vkd);

      DxvkDeviceFeatures features = { };
      features.khrSwapchain = VK_TRUE;
      features.khrPresentId2.presentId2 = VK_TRUE;
      features.khrPresentWait2.presentWait2 = VK_TRUE;
      features.extPresentTiming.presentTiming = VK_TRUE;
      features.extPresentTiming.presentAtAbsoluteTime = VK_TRUE;
      features.extPresentTiming.presentAtRelativeTime = VK_TRUE;
      features.khrCalibratedTimestamps = VK_TRUE;

      DxvkDeviceInfo properties = { };
      DxvkDeviceQueueSet queues = { };
      queues.graphics.queueHandle = m_fake.queue();
      queues.transfer.queueHandle = m_fake.queue();
      queues.sparse.queueHandle = m_fake.queue();
      DxvkQueueCallback queueCallback = { };
      m_device = new DxvkDevice(m_instance, m_adapter, m_vkd,
        features, properties, queues, queueCallback, &m_dependencies,
        PresentAccountingProductionPathTag { });

      DXGI_SWAP_CHAIN_DESC1 desc = { };
      desc.Width = 640u;
      desc.Height = 480u;
      desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
      desc.SampleDesc.Count = 1u;
      desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
      desc.BufferCount = 3u;
      desc.Scaling = DXGI_SCALING_STRETCH;
      desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
      desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
      desc.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;

      PresenterSurfaceProc surfaceProc = [this] (VkSurfaceKHR* surface) {
        if (!surface)
          return VK_ERROR_INITIALIZATION_FAILED;
        *surface = m_fake.surface();
        return VK_SUCCESS;
      };

      m_d3d11 = new D3D11SwapChain(m_device, desc, std::move(surfaceProc),
        &m_dependencies, &m_handleDependencies);
      require(m_d3d11->m_latency != nullptr,
        "test-only D3D11 construction obtains the production built-in latency tracker");
      m_dxgi = new DxgiSwapChain(m_d3d11, nullptr, desc,
        &m_dxgiDependencies, DxgiSwapChainProductionPathTag { });
      m_dispatcher = new DxgiSwapChainDispatcher(m_dxgi, nullptr);
    }

    void runMixedTrace() {
      // A0 acquires once, then production eagerly acquires the next image.
      // I0 consumes that image. A1 exhausts the current and replacement
      // swapchains; I1 acquires after recovery; A2 reacquires after the real
      // pre-WSI cleanup/recreate path and eagerly acquires once more.
      m_fake.setAcquireResults({
        VK_SUCCESS, VK_SUCCESS,
        VK_ERROR_OUT_OF_DATE_KHR, VK_ERROR_OUT_OF_DATE_KHR,
        VK_SUCCESS, VK_SUCCESS, VK_SUCCESS,
      });
      m_fake.setPresentResults({ VK_SUCCESS, VK_SUCCESS, VK_SUCCESS });

      const uint64_t initialSourceOrdinal = m_d3d11->m_sourcePresentOrdinal;
      const PresenterTimingFeedback initialTiming =
        m_d3d11->m_presenter->queryPresentTiming();
      DXGI_FRAME_STATISTICS initialStats = { };
      require(m_dispatcher->GetFrameStatistics(&initialStats)
          == DXGI_ERROR_FRAME_STATISTICS_DISJOINT,
        "the actual first dispatcher statistics call consumes the new-swapchain disjoint result");
      const PresenterTimingFeedback timingAfterFirstDisjoint =
        m_d3d11->m_presenter->queryPresentTiming();
      require(initialStats.PresentCount == m_dxgi->m_presentId
        && m_dxgi->m_presentId == 0u
        && m_d3d11->m_sourcePresentOrdinal == initialSourceOrdinal
        && initialSourceOrdinal == DXGI_MAX_SWAP_CHAIN_BUFFERS
        && initialTiming.appFrameId == AppFrameId()
        && initialTiming.sourcePresentOrdinal == 0u
        && initialTiming.presentTime == 0u
        && timingAfterFirstDisjoint.appFrameId == initialTiming.appFrameId
        && timingAfterFirstDisjoint.sourcePresentOrdinal == initialTiming.sourcePresentOrdinal
        && timingAfterFirstDisjoint.presentTime == initialTiming.presentTime
        && !request(PresentRequest::source(AppFrameId(1u)))
        && !request(PresentRequest::internal(InternalPresentId(1u)))
        && !m_d3d11->m_presenter->m_presentAccounting->lookupWsi(WsiPresentId(1u)),
        "consuming first-disjoint DXGI statistics leaves request accounting, WSI identity, and SOURCE projection untouched");

      require(m_dispatcher->Present(1u, 0u) == S_OK,
        "A0 public DXGI Present succeeds");
      waitForQueue();
      waitForFrameDrain();

      auto a0LatencyCallbacks = latencyCallbackCounts();
      require(a0LatencyCallbacks.cpuPresentBegin == 1u
        && a0LatencyCallbacks.cpuPresentEnd == 1u
        && a0LatencyCallbacks.queuePresentBegin == 1u
        && a0LatencyCallbacks.queuePresentEnd == 1u
        && a0LatencyCallbacks.gpuPresentEnd == 1u
        && a0LatencyCallbacks.cpuPresentBeginFrameId == 17u
        && a0LatencyCallbacks.cpuPresentEndFrameId == 17u
        && a0LatencyCallbacks.queuePresentBeginFrameId == 17u
        && a0LatencyCallbacks.queuePresentEndFrameId == 17u
        && a0LatencyCallbacks.gpuPresentEndFrameId == 17u,
        "A0 reaches the real built-in tracker CPU, queue-worker, and Presenter-frame callbacks");

      auto a0 = request(PresentRequest::source(AppFrameId(1u)));
      require(a0 && a0->wsiId && *a0->wsiId == WsiPresentId(1u)
        && a0->sourcePresentOrdinal == 17u
        && a0->sourceSignalValue == 17u
        && a0->terminal == PresentRequestTerminal::DispatchCompleted
        && a0->result == VK_SUCCESS,
        "A0 resolves AppFrameId(1) to WsiPresentId(1) at source ordinal 17");
      require(m_fake.presentIds() == std::vector<uint64_t> { 1u },
        "A0 reaches actual vkQueuePresentKHR with WSI identity 1");
      require(m_fake.swapchainCreateCount() == 1u
        && m_fake.importedImageCount() == m_fake.enumeratedImageCount()
        && m_fake.importedImageCount() >= 2u,
        "A0 uses production swapchain allocation and image enumeration/import dependencies");

      DXGI_FRAME_STATISTICS beforeInternal = { };
      require(m_dispatcher->GetFrameStatistics(&beforeInternal) == S_OK,
        "public frame statistics are queryable after A0");
      UINT countBeforeInternal = 0u;
      require(m_dispatcher->GetLastPresentCount(&countBeforeInternal) == S_OK
        && countBeforeInternal == 1u,
        "public last-present count reports one SOURCE frame after A0");
      UINT64 d3d11CountBeforeInternal = 0u;
      DXGI_VK_FRAME_STATISTICS d3d11StatsBeforeInternal = { };
      m_d3d11->GetLastPresentCount(&d3d11CountBeforeInternal);
      m_d3d11->GetFrameStatistics(&d3d11StatsBeforeInternal);
      require(d3d11CountBeforeInternal == 1u
        && d3d11StatsBeforeInternal.PresentCount == 1u
        && d3d11StatsBeforeInternal.PresentQPCTime != 0u,
        "D3D11 public SOURCE projections expose A0 before INTERNAL I0");
      const PresenterTimingFeedback timingBeforeInternal =
        m_d3d11->m_presenter->queryPresentTiming();
      const uint64_t signalBeforeInternal = m_d3d11->m_frameLatencySignal->value();
      const uint32_t releasesBeforeInternal = m_handleReleases.load(std::memory_order_relaxed);

      auto i0 = submitInternalPresent(false);
      require(i0 && i0->value() == 1u, "I0 uses InternalPresentId(1)");
      waitForQueue();
      auto i0LatencyCallbacks = latencyCallbackCounts();
      require(i0LatencyCallbacks.cpuPresentBegin == a0LatencyCallbacks.cpuPresentBegin
        && i0LatencyCallbacks.cpuPresentEnd == a0LatencyCallbacks.cpuPresentEnd
        && i0LatencyCallbacks.queuePresentBegin == a0LatencyCallbacks.queuePresentBegin
        && i0LatencyCallbacks.queuePresentEnd == a0LatencyCallbacks.queuePresentEnd
        && i0LatencyCallbacks.gpuPresentEnd == a0LatencyCallbacks.gpuPresentEnd
        && i0LatencyCallbacks.queuePresentBeginFrameId == 17u
        && i0LatencyCallbacks.queuePresentEndFrameId == 17u
        && i0LatencyCallbacks.gpuPresentEndFrameId == 17u,
        "INTERNAL I0 receives no SOURCE latency tracker callbacks");

      auto internal1 = request(PresentRequest::internal(*i0));
      require(internal1 && internal1->wsiId
        && *internal1->wsiId == WsiPresentId(2u)
        && !internal1->sourcePresentOrdinal
        && !internal1->sourceSignalValue
        && internal1->terminal == PresentRequestTerminal::DispatchCompleted
        && internal1->result == VK_SUCCESS,
        "I0 resolves InternalPresentId(1) to completed WsiPresentId(2)");
      require(m_fake.presentIds() == std::vector<uint64_t> { 1u, 2u },
        "I0 reaches actual vkQueuePresentKHR after A0");
      DXGI_FRAME_STATISTICS afterInternal = { };
      require(m_dispatcher->GetFrameStatistics(&afterInternal) == S_OK,
        "public frame statistics remain queryable after I0");
      UINT countAfterInternal = 0u;
      UINT64 d3d11CountAfterInternal = 0u;
      DXGI_VK_FRAME_STATISTICS d3d11StatsAfterInternal = { };
      m_d3d11->GetLastPresentCount(&d3d11CountAfterInternal);
      m_d3d11->GetFrameStatistics(&d3d11StatsAfterInternal);
      const PresenterTimingFeedback timingAfterInternal =
        m_d3d11->m_presenter->queryPresentTiming();
      require(m_dispatcher->GetLastPresentCount(&countAfterInternal) == S_OK
        && countAfterInternal == countBeforeInternal
        && afterInternal.PresentCount == beforeInternal.PresentCount
        && afterInternal.SyncQPCTime.QuadPart == beforeInternal.SyncQPCTime.QuadPart
        && d3d11CountAfterInternal == d3d11CountBeforeInternal
        && d3d11StatsAfterInternal.PresentCount == d3d11StatsBeforeInternal.PresentCount
        && d3d11StatsAfterInternal.PresentQPCTime == d3d11StatsBeforeInternal.PresentQPCTime
        && timingAfterInternal.appFrameId == timingBeforeInternal.appFrameId
        && timingAfterInternal.sourcePresentOrdinal == timingBeforeInternal.sourcePresentOrdinal
        && timingAfterInternal.presentTime == timingBeforeInternal.presentTime
        && m_d3d11->m_frameLatencySignal->value() == signalBeforeInternal
        && m_handleReleases.load(std::memory_order_relaxed) == releasesBeforeInternal,
        "INTERNAL I0 leaves DXGI and D3D11 SOURCE projections, signal, and releases unchanged");

      require(m_dispatcher->Present1(1u, 0u, nullptr) == E_FAIL,
        "A1 public DXGI Present1 fails during acquisition before WSI");
      waitForQueue();
      auto a1 = request(PresentRequest::source(AppFrameId(2u)));
      require(a1 && !a1->wsiId
        && a1->terminal == PresentRequestTerminal::PreDispatchFailure,
        "A1 AppFrameId(2) terminalizes before WSI without consuming an ID");
      require(a1->result == E_FAIL && !a1->sourcePresentOrdinal
        && !a1->sourceSignalValue,
        "A1 acquire failure retains no source ordinal, signal, or WSI identity");
      require(m_fake.presentIds() == std::vector<uint64_t> { 1u, 2u },
        "A1's acquire failures do not enter vkQueuePresentKHR");
      require(m_fake.swapchainCreateCount() == 2u,
        "A1 acquisition failure runs the production replacement-swapchain path");
      auto a1LatencyCallbacks = latencyCallbackCounts();
      require(a1LatencyCallbacks.cpuPresentBegin == 2u
        && a1LatencyCallbacks.cpuPresentEnd == 1u
        && a1LatencyCallbacks.queuePresentBegin == 1u
        && a1LatencyCallbacks.queuePresentEnd == 1u
        && a1LatencyCallbacks.gpuPresentEnd == 1u
        && a1LatencyCallbacks.cpuPresentBeginFrameId == 18u
        && a1LatencyCallbacks.queuePresentBeginFrameId == 17u
        && a1LatencyCallbacks.queuePresentEndFrameId == 17u
        && a1LatencyCallbacks.gpuPresentEndFrameId == 17u,
        "A1 reaches real CPU-present-begin and acquire-failure timing cleanup without queue callbacks");

      m_failInternalIdTwo.store(true, std::memory_order_release);
      auto i1 = submitInternalPresent(true);
      require(i1 && i1->value() == 2u, "I1 uses InternalPresentId(2)");
      waitForQueue();
      auto internal2 = request(PresentRequest::internal(*i1));
      require(internal2 && !internal2->wsiId
        && internal2->terminal == PresentRequestTerminal::DispatchUnavailable
        && !internal2->sourcePresentOrdinal
        && !internal2->sourceSignalValue
        && internal2->result == VK_ERROR_OUT_OF_DATE_KHR,
        "I1 terminalizes inside real Presenter before beginWsiCall without a WSI ID");
      auto i1LatencyCallbacks = latencyCallbackCounts();
      require(i1LatencyCallbacks.cpuPresentBegin == a1LatencyCallbacks.cpuPresentBegin
        && i1LatencyCallbacks.cpuPresentEnd == a1LatencyCallbacks.cpuPresentEnd
        && i1LatencyCallbacks.queuePresentBegin == a1LatencyCallbacks.queuePresentBegin
        && i1LatencyCallbacks.queuePresentEnd == a1LatencyCallbacks.queuePresentEnd
        && i1LatencyCallbacks.gpuPresentEnd == a1LatencyCallbacks.gpuPresentEnd,
        "INTERNAL I1 receives no SOURCE latency tracker callbacks");
      require(m_preWsiFailures.load(std::memory_order_relaxed) == 1u
        && m_fake.droppedPresentSignals() == 1u
        && !m_fake.semaphoreViolation(),
        "I1's fake synchronization consumes acquire and leaves the dropped present token unconsumed");
      require(m_fake.presentIds() == std::vector<uint64_t> { 1u, 2u },
        "I1 aborts before beginWsiCall and never reaches the real vkQueuePresentKHR");
      require(m_device->getDeviceStatus() == VK_SUCCESS,
        "non-device-loss I1 result remains forwarded without poisoning queue status");
      require(m_fake.swapchainCreateCount() == 3u,
        "I1 acquires from the real swapchain recreated after A1's failed acquisition");
      {
        std::unique_lock<dxvk::mutex> lock(m_d3d11->m_presenter->m_surfaceMutex);
        require(m_d3d11->m_presenter->m_dirtySwapchain,
          "production I1 cleanup leaves Presenter swapchain dirty for A2");
      }

      require(m_dispatcher->Present1(1u, 0u, nullptr) == S_OK,
        "A2 public DXGI Present1 succeeds after production dirty-swapchain recovery");
      waitForQueue();
      {
        std::lock_guard lock(m_hookMutex);
        const std::vector<PresentRequest> expectedHookRequests = {
          PresentRequest::source(AppFrameId(1u)),
          PresentRequest::internal(InternalPresentId(1u)),
          PresentRequest::internal(InternalPresentId(2u)),
          PresentRequest::source(AppFrameId(3u)),
        };
        require(m_wsiHookRequests == expectedHookRequests
          && m_preWsiThreadId != m_mainThreadId,
          "real queue worker reaches the WSI hook for A0, I0, I1, and A2 in order");
      }
      require(waitForFrameEntry(3u),
        "A2's real SOURCE frame reaches the Presenter worker for WSI 3");
      require(m_fake.presentIds() == std::vector<uint64_t> { 1u, 2u, 3u },
        "A2 reaches actual vkQueuePresentKHR with the next WSI identity");
      require(m_fake.swapchainCreateCount() == 4u
        && m_fake.importedImageCount() == m_fake.enumeratedImageCount(),
        "A2 uses production dirty-swapchain destroy/recreate and image import paths");
      auto a2QueueLatencyCallbacks = latencyCallbackCounts();
      require(a2QueueLatencyCallbacks.cpuPresentBegin == 3u
        && a2QueueLatencyCallbacks.cpuPresentEnd == 2u
        && a2QueueLatencyCallbacks.queuePresentBegin == 2u
        && a2QueueLatencyCallbacks.queuePresentEnd == 2u
        && a2QueueLatencyCallbacks.gpuPresentEnd == 1u
        && a2QueueLatencyCallbacks.cpuPresentBeginFrameId == 18u
        && a2QueueLatencyCallbacks.cpuPresentEndFrameId == 18u
        && a2QueueLatencyCallbacks.queuePresentBeginFrameId == 18u
        && a2QueueLatencyCallbacks.queuePresentEndFrameId == 18u
        && a2QueueLatencyCallbacks.gpuPresentEndFrameId == 17u,
        "A2 SOURCE reaches real queue callbacks before the gated Presenter-frame callback");
      auto a2BeforeFeedback = request(PresentRequest::source(AppFrameId(3u)));
      const PresenterTimingFeedback feedbackBeforeA2 =
        m_d3d11->m_presenter->queryPresentTiming();
      const uint32_t releasesBeforeA2Pacing =
        m_handleReleases.load(std::memory_order_relaxed);
      UINT completedDxgiCountBeforeA2Pacing = 0u;
      DXGI_FRAME_STATISTICS completedDxgiStatsBeforeA2Pacing = { };
      UINT64 completedD3d11CountBeforeA2Pacing = 0u;
      DXGI_VK_FRAME_STATISTICS completedD3d11StatsBeforeA2Pacing = { };
      m_dispatcher->GetLastPresentCount(&completedDxgiCountBeforeA2Pacing);
      m_dispatcher->GetFrameStatistics(&completedDxgiStatsBeforeA2Pacing);
      m_d3d11->GetLastPresentCount(&completedD3d11CountBeforeA2Pacing);
      m_d3d11->GetFrameStatistics(&completedD3d11StatsBeforeA2Pacing);
      require(a2BeforeFeedback && a2BeforeFeedback->wsiId
        && *a2BeforeFeedback->wsiId == WsiPresentId(3u)
        && !a2BeforeFeedback->terminal
        && feedbackBeforeA2.appFrameId == AppFrameId(1u)
        && feedbackBeforeA2.sourcePresentOrdinal == 17u
        && feedbackBeforeA2.presentTime == timingBeforeInternal.presentTime
        && m_d3d11->m_frameLatencySignal->value() == 17u
        && completedDxgiCountBeforeA2Pacing == 2u
        && completedDxgiStatsBeforeA2Pacing.PresentCount == 1u
        && completedDxgiStatsBeforeA2Pacing.SyncQPCTime.QuadPart
             == beforeInternal.SyncQPCTime.QuadPart
        && completedD3d11CountBeforeA2Pacing == 2u
        && completedD3d11StatsBeforeA2Pacing.PresentCount == 1u
        && completedD3d11StatsBeforeA2Pacing.PresentQPCTime
             == d3d11StatsBeforeInternal.PresentQPCTime
        && m_handleReleases.load(std::memory_order_relaxed) == releasesBeforeA2Pacing,
        "queued A2 advances last-present count while stats and waitable release remain at completed A0");
      m_fake.enableTimingReports(true);
      releaseWsiThree();
      waitForFrameDrain();
      auto a2LatencyCallbacks = latencyCallbackCounts();
      require(a2LatencyCallbacks.cpuPresentBegin == 3u
        && a2LatencyCallbacks.cpuPresentEnd == 2u
        && a2LatencyCallbacks.queuePresentBegin == 2u
        && a2LatencyCallbacks.queuePresentEnd == 2u
        && a2LatencyCallbacks.gpuPresentEnd == 2u
        && a2LatencyCallbacks.gpuPresentEndFrameId == 18u,
        "A2's real Presenter-frame completion invokes the built-in latency tracker after queue callbacks");

      auto a2 = request(PresentRequest::source(AppFrameId(3u)));
      require(a2 && a2->wsiId && *a2->wsiId == WsiPresentId(3u)
        && a2->sourcePresentOrdinal == 18u
        && a2->sourceSignalValue == 18u
        && a2->terminal == PresentRequestTerminal::DispatchCompleted
        && a2->result == VK_SUCCESS,
        "A2 resolves AppFrameId(3) to completed WsiPresentId(3) and source ordinal 18");
      require(m_fake.timingReportIds() == std::vector<uint64_t> { 3u, 1u, 2u, 1u, 999u },
        "delayed timing batch is reordered and contains one duplicate and one unknown WSI ID");

      auto wsi1 = m_d3d11->m_presenter->m_presentAccounting->lookupWsi(WsiPresentId(1u));
      auto wsi2 = m_d3d11->m_presenter->m_presentAccounting->lookupWsi(WsiPresentId(2u));
      auto wsi3 = m_d3d11->m_presenter->m_presentAccounting->lookupWsi(WsiPresentId(3u));
      require(wsi1 && wsi1->request == PresentRequest::source(AppFrameId(1u))
        && wsi2 && wsi2->request == PresentRequest::internal(InternalPresentId(1u))
        && wsi3 && wsi3->request == PresentRequest::source(AppFrameId(3u)),
        "typed AppFrameId/InternalPresentId lineage cannot cross-resolve through WSI identities");
      require(!m_d3d11->m_presenter->m_presentAccounting->lookupWsi(WsiPresentId(999u)),
        "unknown delayed WSI identity is harmless");

      PresenterTimingFeedback feedback = m_d3d11->m_presenter->queryPresentTiming();
      require(feedback.appFrameId == AppFrameId(3u)
        && feedback.sourcePresentOrdinal == 18u
        && feedback.presentTime != 0u,
        "reordered duplicate/unknown batch leaves the latest SOURCE projection at A2");
      require(m_d3d11->m_frameLatencySignal->value() == 18u,
        "queue completion and real frame-worker pacing retire SOURCE tokens in order");
      require(m_handleReleases.load(std::memory_order_relaxed)
          == releasesBeforeA2Pacing + 1u,
        "A2's SOURCE waitable callback fires after frame-worker pacing completes");
      require(!m_fake.semaphoreViolation(),
        "fake Vulkan consumed each actual queue present wait semaphore exactly once");

      UINT publicCount = 0u;
      require(m_dispatcher->GetLastPresentCount(&publicCount) == S_OK
        && publicCount == 2u,
        "public GetLastPresentCount excludes INTERNAL I0 and failed A1");
      UINT64 d3d11PublicCount = 0u;
      DXGI_VK_FRAME_STATISTICS d3d11PublicStats = { };
      m_d3d11->GetLastPresentCount(&d3d11PublicCount);
      m_d3d11->GetFrameStatistics(&d3d11PublicStats);
      require(d3d11PublicCount == 2u
        && d3d11PublicStats.PresentCount == 2u
        && d3d11PublicStats.PresentQPCTime != 0u,
        "D3D11 public statistics and last-present count expose only completed SOURCE feedback");
      DXGI_FRAME_STATISTICS publicStats = { };
      require(m_dispatcher->GetFrameStatistics(&publicStats) == S_OK
        && publicStats.PresentCount == 2u
        && publicStats.SyncQPCTime.QuadPart != 0,
        "public GetFrameStatistics reflects only completed SOURCE feedback");
      checkWaitableProjection();
    }

    void runPostAllocationFailure() {
      m_fake.setPresentResults({ VK_ERROR_OUT_OF_DATE_KHR, VK_SUCCESS });
      auto failedId = submitInternalPresent(false);
      require(failedId && failedId->value() == 1u,
        "post-allocation failure fixture starts with InternalPresentId(1)");
      waitForQueue();

      auto succeededId = submitInternalPresent(false);
      require(succeededId && succeededId->value() == 2u,
        "post-allocation recovery fixture allocates InternalPresentId(2)");
      waitForQueue();

      require(m_fake.presentIds() == std::vector<uint64_t> { 1u, 2u },
        "failed real Vulkan present consumes WsiPresentId(1), recovery consumes WsiPresentId(2)");
      auto failed = request(PresentRequest::internal(*failedId));
      auto succeeded = request(PresentRequest::internal(*succeededId));
      require(failed && failed->wsiId && *failed->wsiId == WsiPresentId(1u)
        && failed->terminal == PresentRequestTerminal::DispatchFailed
        && failed->result == VK_ERROR_OUT_OF_DATE_KHR,
        "post-allocation Vulkan error preserves its consumed WSI identity and terminal");
      require(succeeded && succeeded->wsiId && *succeeded->wsiId == WsiPresentId(2u)
        && succeeded->terminal == PresentRequestTerminal::DispatchCompleted
        && succeeded->result == VK_SUCCESS,
        "next internal request succeeds with a non-reused WSI identity");
      require(!m_fake.semaphoreViolation(),
        "post-allocation failure still consumes the submitted present semaphore");
    }

    void runDirectD3D11Present() {
      require(m_d3d11->Present(1u, 0u, nullptr) == S_OK,
        "isolated direct D3D11 ordinary Present succeeds");
      waitForQueue();
      waitForFrameDrain();

      auto source = request(PresentRequest::source(AppFrameId(1u)));
      require(source && source->wsiId && *source->wsiId == WsiPresentId(1u)
        && source->terminal == PresentRequestTerminal::DispatchCompleted,
        "direct D3D11 Present reaches production accounting through the real queue");
      DXGI_VK_FRAME_STATISTICS stats = { };
      m_d3d11->GetFrameStatistics(&stats);
      require(stats.PresentCount == 1u && stats.PresentQPCTime != 0u,
        "direct D3D11 public frame statistics expose its SOURCE timing");
    }

    void runTestPresentFrontends() {
      DXGI_FRAME_STATISTICS initialDisjointStats = { };
      require(m_dispatcher->GetFrameStatistics(&initialDisjointStats)
          == DXGI_ERROR_FRAME_STATISTICS_DISJOINT,
        "TEST frontend fixture consumes its new-swapchain DXGI statistics disjoint result before baseline");

      const UINT dxgiPresentIdBefore = m_dxgi->m_presentId;
      const uint64_t sourcePresentOrdinalBefore = m_d3d11->m_sourcePresentOrdinal;
      UINT dxgiLastPresentCountBefore = 0u;
      UINT64 d3d11LastPresentCountBefore = 0u;
      require(m_dispatcher->GetLastPresentCount(&dxgiLastPresentCountBefore) == S_OK,
        "TEST frontend baseline reads the public DXGI last-present count");
      m_d3d11->GetLastPresentCount(&d3d11LastPresentCountBefore);

      DXGI_FRAME_STATISTICS dxgiStatsBefore = { };
      require(m_dispatcher->GetFrameStatistics(&dxgiStatsBefore) == S_OK,
        "TEST frontend baseline reads DXGI statistics after consuming DISJOINT");
      DXGI_VK_FRAME_STATISTICS d3d11StatsBefore = { };
      m_d3d11->GetFrameStatistics(&d3d11StatsBefore);

      const PresenterTimingFeedback presenterTimingBefore =
        m_d3d11->m_presenter->queryPresentTiming();
      const auto latencyCallbacksBefore = latencyCallbackCounts();
      const uint64_t signalBefore = m_d3d11->m_frameLatencySignal->value();
      const uint32_t releasesBefore = m_handleReleases.load(std::memory_order_relaxed);
      require(dxgiPresentIdBefore == 0u
        && sourcePresentOrdinalBefore == DXGI_MAX_SWAP_CHAIN_BUFFERS
        && dxgiLastPresentCountBefore == 0u
        && d3d11LastPresentCountBefore == 0u
        && dxgiStatsBefore.PresentCount == 0u
        && d3d11StatsBefore.PresentCount == 0u
        && d3d11StatsBefore.PresentQPCTime == 0u
        && sameLatencyCallbackObservations(latencyCallbacksBefore,
          DxvkBuiltInLatencyTrackerTestCallbackCounts { }),
        "TEST frontend baseline has no SOURCE presents or completed feedback");

      require(m_dispatcher->Present(1u, DXGI_PRESENT_TEST) == S_OK,
        "DXGI ordinary Present(TEST) reports actual swapchain status");
      require(m_dispatcher->Present1(1u, DXGI_PRESENT_TEST, nullptr) == S_OK,
        "DXGI Present1(TEST) reports actual swapchain status");
      require(m_d3d11->Present(1u, DXGI_PRESENT_TEST, nullptr) == S_OK,
        "direct D3D11 Present(TEST) reports actual swapchain status");

      UINT dxgiLastPresentCountAfter = 0u;
      UINT64 d3d11LastPresentCountAfter = 0u;
      require(m_dispatcher->GetLastPresentCount(&dxgiLastPresentCountAfter) == S_OK,
        "TEST frontend post-state reads the public DXGI last-present count");
      m_d3d11->GetLastPresentCount(&d3d11LastPresentCountAfter);
      DXGI_FRAME_STATISTICS dxgiStatsAfter = { };
      require(m_dispatcher->GetFrameStatistics(&dxgiStatsAfter) == S_OK,
        "TEST frontend post-state reads the real DXGI statistics projection");
      DXGI_VK_FRAME_STATISTICS d3d11StatsAfter = { };
      m_d3d11->GetFrameStatistics(&d3d11StatsAfter);
      const PresenterTimingFeedback presenterTimingAfter =
        m_d3d11->m_presenter->queryPresentTiming();
      const auto latencyCallbacksAfter = latencyCallbackCounts();

      require(m_dxgi->m_presentId == dxgiPresentIdBefore
        && m_d3d11->m_sourcePresentOrdinal == sourcePresentOrdinalBefore
        && dxgiLastPresentCountAfter == dxgiLastPresentCountBefore
        && d3d11LastPresentCountAfter == d3d11LastPresentCountBefore
        && dxgiStatsAfter.PresentCount == dxgiStatsBefore.PresentCount
        && dxgiStatsAfter.PresentRefreshCount == dxgiStatsBefore.PresentRefreshCount
        && dxgiStatsAfter.SyncGPUTime.QuadPart == dxgiStatsBefore.SyncGPUTime.QuadPart
        && d3d11StatsAfter.PresentCount == d3d11StatsBefore.PresentCount
        && d3d11StatsAfter.PresentQPCTime == d3d11StatsBefore.PresentQPCTime,
        "DXGI and direct D3D11 TEST calls leave present IDs, public last-present counts, and stable statistics feedback unchanged");
      require(!request(PresentRequest::source(AppFrameId(1u)))
        && !request(PresentRequest::internal(InternalPresentId(1u)))
        && m_fake.presentIds().empty()
        && presenterTimingAfter.appFrameId == presenterTimingBefore.appFrameId
        && presenterTimingAfter.sourcePresentOrdinal == presenterTimingBefore.sourcePresentOrdinal
        && presenterTimingAfter.presentTime == presenterTimingBefore.presentTime
        && sameLatencyCallbackObservations(latencyCallbacksBefore, latencyCallbacksAfter)
        && m_d3d11->m_frameLatencySignal->value() == signalBefore
        && m_handleReleases.load(std::memory_order_relaxed) == releasesBefore,
        "TEST frontends allocate no request/WSI identity and mutate no Presenter timing, tracker callbacks, CallbackFence, or waitable release");
    }

    void runInvalidSyncPrecedence() {
      require(m_dispatcher->Present(5u, DXGI_PRESENT_TEST) == DXGI_ERROR_INVALID_CALL,
        "ordinary DXGI Present rejects invalid sync before TEST status handling");
      require(m_dispatcher->Present1(5u, DXGI_PRESENT_TEST, nullptr) == DXGI_ERROR_INVALID_CALL,
        "DXGI Present1 rejects invalid sync before TEST status handling");
      require(!request(PresentRequest::source(AppFrameId(1u)))
        && m_fake.presentIds().empty()
        && m_d3d11->m_presenter->queryPresentTiming().appFrameId == AppFrameId(),
        "invalid-sync precedence occurs before request creation or public projection changes");
    }

  private:
    static DXGI_SWAP_CHAIN_DESC1 makeDesc() {
      DXGI_SWAP_CHAIN_DESC1 desc = { };
      desc.Width = 640u;
      desc.Height = 480u;
      desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
      desc.SampleDesc.Count = 1u;
      desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
      desc.BufferCount = 3u;
      desc.Scaling = DXGI_SCALING_STRETCH;
      desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
      desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
      desc.Flags = DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT;
      return desc;
    }

    std::optional<PresentRequestSnapshot> request(const PresentRequest& request) const {
      return m_d3d11->m_presenter->m_presentAccounting->lookupRequest(request);
    }

    DxvkBuiltInLatencyTrackerTestCallbackCounts latencyCallbackCounts() const {
      auto* tracker = static_cast<const DxvkBuiltInLatencyTracker*>(m_d3d11->m_latency.ptr());
      return tracker->testCallbackCounts();
    }

    static bool sameLatencyCallbackObservations(
      const DxvkBuiltInLatencyTrackerTestCallbackCounts& a,
      const DxvkBuiltInLatencyTrackerTestCallbackCounts& b) {
      return a.cpuPresentBegin == b.cpuPresentBegin
          && a.cpuPresentEnd == b.cpuPresentEnd
          && a.queuePresentBegin == b.queuePresentBegin
          && a.queuePresentEnd == b.queuePresentEnd
          && a.gpuPresentEnd == b.gpuPresentEnd
          && a.cpuPresentBeginFrameId == b.cpuPresentBeginFrameId
          && a.cpuPresentEndFrameId == b.cpuPresentEndFrameId
          && a.queuePresentBeginFrameId == b.queuePresentBeginFrameId
          && a.queuePresentEndFrameId == b.queuePresentEndFrameId
          && a.gpuPresentEndFrameId == b.gpuPresentEndFrameId;
    }

    std::optional<InternalPresentId> submitInternalPresent(bool dropSignal) {
      m_d3d11->m_presenter->setSyncInterval(1u);
      PresenterSync sync;
      Rc<DxvkImage> image;
      VkResult acquireResult = m_d3d11->m_presenter->acquireNextImage(sync, image);
      if (acquireResult != VK_SUCCESS)
        return std::nullopt;

      auto id = m_d3d11->m_presenter->makeInternalPresentRequest();
      if (!id)
        return std::nullopt;

      if (dropSignal)
        m_fake.dropNextPresentSignal();
      if (!m_d3d11->SubmitTestPresentRequest(sync,
          PresentRequest::internal(*id), 0u, 0u)) {
        m_d3d11->m_presenter->terminalizePresentRequest(
          PresentRequest::internal(*id), PresentRequestTerminal::PreDispatchFailure);
        return std::nullopt;
      }
      return id;
    }

    void waitForQueue() {
      m_device->m_submissionQueue.waitForIdle();
    }

    bool waitForFrameEntry(uint64_t wsiId) {
      std::unique_lock lock(m_frameGateMutex);
      return m_frameGateCond.wait_for(lock, std::chrono::seconds(5), [this, wsiId] {
        return std::find(m_frameIds.begin(), m_frameIds.end(), wsiId) != m_frameIds.end();
      });
    }

    void releaseWsiThree() {
      {
        std::lock_guard lock(m_frameGateMutex);
        m_releaseWsiThree = true;
      }
      m_frameGateCond.notify_all();
    }

    void waitForFrameDrain() {
      if (!m_d3d11)
        return;
      auto* presenter = m_d3d11->m_presenter.ptr();
      std::unique_lock<dxvk::mutex> lock(presenter->m_frameMutex);
      // The production worker advances pop only after SOURCE pacing and all
      // resulting signal callbacks return; this equality is the full drain
      // boundary, not just a dequeue or CallbackFence-value observation.
      bool drained = presenter->m_frameDrain.wait_for(lock, std::chrono::seconds(5), [presenter] {
        return presenter->m_frameQueuePopId == presenter->m_frameQueuePushId;
      });
      require(drained, "actual Presenter frame worker drained within the fixture bound");
    }

    void checkWaitableProjection() {
      UINT latency = 0u;
      require(m_dispatcher->GetMaximumFrameLatency(&latency) == S_OK && latency == 1u,
        "public waitable getter returns production maximum frame latency");
      require(m_dispatcher->SetMaximumFrameLatency(2u) == S_OK,
        "public setter uses production D3D11 frame-latency path");
      require(m_dispatcher->GetMaximumFrameLatency(&latency) == S_OK && latency == 2u,
        "public maximum frame latency reflects production setter");
      HANDLE waitable = m_dispatcher->GetFrameLatencyWaitableObject();
      require(waitable != nullptr
        && m_handleDuplicates.load(std::memory_order_relaxed) == 1u,
        "public waitable-object getter reaches production duplicate dependency");
      m_handleDependencies.close(waitable);
      require(m_handleFailures.load(std::memory_order_relaxed) == 0u
        && m_handleCreates.load(std::memory_order_relaxed) == 1u
        && m_handleCloses.load(std::memory_order_relaxed) == 1u
        && m_handleReleases.load(std::memory_order_relaxed) >= 3u,
        "native handle shim observed create/release/duplicate/close methods");
    }

    FakeVulkan m_fake;
    PresentAccountingProductionPathDependencies m_dependencies;
    D3D11PresentPathHandleDependencies m_handleDependencies;
    DxgiSwapChainProductionPathDependencies m_dxgiDependencies;
    Rc<vk::LibraryLoader> m_library;
    Rc<vk::InstanceFn> m_vki;
    Rc<DxvkInstance> m_instance;
    Rc<DxvkAdapter> m_adapter;
    Rc<vk::DeviceFn> m_vkd;
    Rc<DxvkDevice> m_device;
    D3D11SwapChain* m_d3d11 = nullptr;
    DxgiSwapChain* m_dxgi = nullptr;
    DxgiSwapChainDispatcher* m_dispatcher = nullptr;
    HANDLE m_primaryHandle = nullptr;
    std::atomic<uint32_t> m_handleCreates { 0u };
    std::atomic<uint32_t> m_handleReleases { 0u };
    std::atomic<uint32_t> m_handleDuplicates { 0u };
    std::atomic<uint32_t> m_handleCloses { 0u };
    std::atomic<uint32_t> m_handleFailures { 0u };
    std::atomic<uint32_t> m_preWsiFailures { 0u };
    std::atomic<bool> m_failInternalIdTwo { false };
    std::mutex m_hookMutex;
    std::vector<PresentRequest> m_wsiHookRequests;
    std::mutex m_frameGateMutex;
    std::condition_variable m_frameGateCond;
    std::vector<uint64_t> m_frameIds;
    bool m_gateWsiThree = true;
    bool m_releaseWsiThree = false;
    std::thread::id m_mainThreadId;
    std::thread::id m_preWsiThreadId;
  };

}

int main() {
  try {
    dxvk::PresentAccountingProductionFixture mixedTrace;
    mixedTrace.initialize();
    mixedTrace.runMixedTrace();

    dxvk::PresentAccountingProductionFixture postAllocationFailure;
    postAllocationFailure.initialize();
    postAllocationFailure.runPostAllocationFailure();

    dxvk::PresentAccountingProductionFixture directD3D11;
    directD3D11.initialize();
    directD3D11.runDirectD3D11Present();

    dxvk::PresentAccountingProductionFixture testFrontends;
    testFrontends.initialize();
    testFrontends.runTestPresentFrontends();

    dxvk::PresentAccountingProductionFixture invalidSync;
    invalidSync.initialize();
    invalidSync.runInvalidSyncPrecedence();

    std::cout << "PRODUCTION_PATH: real DXGI/D3D11/DXVK queue and Presenter fixture completed\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "PRODUCTION_PATH assertion failed: " << error.what() << '\n';
    return 1;
  }
}
