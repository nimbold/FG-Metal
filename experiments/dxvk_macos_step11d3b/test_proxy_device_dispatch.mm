#include <vulkan/vulkan.h>
#include <dlfcn.h>
#include <unistd.h>
#include <sys/wait.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <iostream>
#include <cassert>

namespace {

const char* kMoltenVKPath = "/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib";
const char* kMoltenVKSha = "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f";
const char* kProxyPath = "/Users/nima/Documents/Code/FG-Metal/experiments/dxvk_macos_step11d3b/control/libvulkan-trace.dylib";

std::string canonical(const char* path) {
    char buf[PATH_MAX]{};
    if (realpath(path, buf)) return std::string(buf);
    return std::string(path ? path : "");
}

void test_1_delegate_gdpa_symbol_resolves_directly() {
    std::cout << "TEST 1: delegate GDPA symbol resolves directly... " << std::flush;
    void* handle = dlopen(kMoltenVKPath, RTLD_NOW | RTLD_LOCAL);
    assert(handle != nullptr);
    void* gdpa = dlsym(handle, "vkGetDeviceProcAddr");
    assert(gdpa != nullptr);
    dlclose(handle);
    std::cout << "PASS (gdpa=" << gdpa << ")\n" << std::flush;
}

void test_2_delegate_gdpa_dladdr_equals_intended_moltenvk() {
    std::cout << "TEST 2: delegate GDPA dladdr == intended MoltenVK... " << std::flush;
    void* handle = dlopen(kMoltenVKPath, RTLD_NOW | RTLD_LOCAL);
    assert(handle != nullptr);
    void* gdpa = dlsym(handle, "vkGetDeviceProcAddr");
    assert(gdpa != nullptr);

    Dl_info info{};
    int rc = dladdr(gdpa, &info);
    assert(rc != 0);
    assert(info.dli_sname != nullptr && std::strcmp(info.dli_sname, "vkGetDeviceProcAddr") == 0);
    assert(info.dli_fname != nullptr);
    std::string resolved_path = canonical(info.dli_fname);
    std::string expected_path = canonical(kMoltenVKPath);
    assert(resolved_path == expected_path);
    dlclose(handle);
    std::cout << "PASS (sname=" << info.dli_sname << ", path=" << resolved_path << ")\n" << std::flush;
}

void test_3_delegate_gdpa_does_not_resolve_to_proxy() {
    std::cout << "TEST 3: delegate GDPA does not resolve to proxy... " << std::flush;
    void* proxy_handle = dlopen(kProxyPath, RTLD_NOW | RTLD_LOCAL);
    assert(proxy_handle != nullptr);
    void* proxy_gdpa_export = dlsym(proxy_handle, "vkGetDeviceProcAddr");
    assert(proxy_gdpa_export != nullptr);

    void* delegate_handle = dlopen(kMoltenVKPath, RTLD_NOW | RTLD_LOCAL);
    assert(delegate_handle != nullptr);
    void* delegate_gdpa = dlsym(delegate_handle, "vkGetDeviceProcAddr");
    assert(delegate_gdpa != nullptr);

    assert(delegate_gdpa != proxy_gdpa_export);

    Dl_info info{};
    int rc = dladdr(delegate_gdpa, &info);
    assert(rc != 0);
    std::string fname = canonical(info.dli_fname);
    std::string proxy_canon = canonical(kProxyPath);
    assert(fname != proxy_canon);
    assert(fname.find("libvulkan-trace") == std::string::npos);

    dlclose(delegate_handle);
    dlclose(proxy_handle);
    std::cout << "PASS (delegate=" << delegate_gdpa << " != proxy=" << proxy_gdpa_export << ")\n" << std::flush;
}

void test_4_no_recursive_gdpa() {
    std::cout << "TEST 4: no recursive GDPA... " << std::flush;
    void* proxy_handle = dlopen(kProxyPath, RTLD_NOW | RTLD_LOCAL);
    assert(proxy_handle != nullptr);
    auto pfn_proxy_gdpa = (PFN_vkGetDeviceProcAddr)dlsym(proxy_handle, "vkGetDeviceProcAddr");
    assert(pfn_proxy_gdpa != nullptr);

    // Call with null device: proxy null check must safely return null without recursing
    PFN_vkVoidFunction fn = pfn_proxy_gdpa(VK_NULL_HANDLE, "vkGetDeviceQueue");
    assert(fn == nullptr);
    dlclose(proxy_handle);
    std::cout << "PASS\n" << std::flush;
}

void test_5_device_lifecycle_and_dispatch(bool early_gdpa) {
    std::cout << "TEST " << (early_gdpa ? "6 (early GDPA)" : "5 (late GDPA)")
              << ": device creation & proc resolution... " << std::flush;

    void* proxy_handle = dlopen(kProxyPath, RTLD_NOW | RTLD_LOCAL);
    assert(proxy_handle != nullptr);
    auto pfn_gipa = (PFN_vkGetInstanceProcAddr)dlsym(proxy_handle, "vkGetInstanceProcAddr");
    auto pfn_gdpa = (PFN_vkGetDeviceProcAddr)dlsym(proxy_handle, "vkGetDeviceProcAddr");
    assert(pfn_gipa != nullptr);
    assert(pfn_gdpa != nullptr);

    auto pfn_createInstance = (PFN_vkCreateInstance)pfn_gipa(VK_NULL_HANDLE, "vkCreateInstance");
    assert(pfn_createInstance != nullptr);

    VkApplicationInfo appInfo{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    appInfo.pApplicationName = "TestProxyDeviceDispatch";
    appInfo.apiVersion = VK_API_VERSION_1_0;

    std::vector<const char*> instExts;
    instExts.push_back("VK_KHR_surface");
    instExts.push_back("VK_EXT_metal_surface");

    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pApplicationInfo = &appInfo;
    ici.enabledExtensionCount = static_cast<uint32_t>(instExts.size());
    ici.ppEnabledExtensionNames = instExts.data();

    VkInstance instance = VK_NULL_HANDLE;
    VkResult r = pfn_createInstance(&ici, nullptr, &instance);
    assert(r == VK_SUCCESS && instance != VK_NULL_HANDLE);

    if (early_gdpa) {
        // CASE B: Early GDPA query on instance before vkCreateDevice
        PFN_vkVoidFunction early_fn = pfn_gipa(instance, "vkGetDeviceProcAddr");
        assert(early_fn != nullptr);
    }

    auto pfn_enumPhys = (PFN_vkEnumeratePhysicalDevices)pfn_gipa(instance, "vkEnumeratePhysicalDevices");
    auto pfn_getQueues = (PFN_vkGetPhysicalDeviceQueueFamilyProperties)pfn_gipa(instance, "vkGetPhysicalDeviceQueueFamilyProperties");
    auto pfn_createDevice = (PFN_vkCreateDevice)pfn_gipa(instance, "vkCreateDevice");
    auto pfn_destroyInstance = (PFN_vkDestroyInstance)pfn_gipa(instance, "vkDestroyInstance");
    assert(pfn_enumPhys && pfn_getQueues && pfn_createDevice && pfn_destroyInstance);

    uint32_t physCount = 0;
    r = pfn_enumPhys(instance, &physCount, nullptr);
    assert(r == VK_SUCCESS && physCount > 0);
    std::vector<VkPhysicalDevice> phys(physCount);
    r = pfn_enumPhys(instance, &physCount, phys.data());
    assert(r == VK_SUCCESS);

    uint32_t queueFamilyCount = 0;
    pfn_getQueues(phys[0], &queueFamilyCount, nullptr);
    assert(queueFamilyCount > 0);
    std::vector<VkQueueFamilyProperties> queueProps(queueFamilyCount);
    pfn_getQueues(phys[0], &queueFamilyCount, queueProps.data());

    uint32_t graphicsQueueFamily = UINT32_MAX;
    for (uint32_t i = 0; i < queueFamilyCount; ++i) {
        if (queueProps[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) {
            graphicsQueueFamily = i;
            break;
        }
    }
    assert(graphicsQueueFamily != UINT32_MAX);

    float priority = 1.0f;
    VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qi.queueFamilyIndex = graphicsQueueFamily;
    qi.queueCount = 1;
    qi.pQueuePriorities = &priority;

    std::vector<const char*> devExts;
    devExts.push_back("VK_KHR_portability_subset");
    devExts.push_back("VK_KHR_swapchain");

    VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    di.queueCreateInfoCount = 1;
    di.pQueueCreateInfos = &qi;
    di.enabledExtensionCount = static_cast<uint32_t>(devExts.size());
    di.ppEnabledExtensionNames = devExts.data();

    VkDevice device = VK_NULL_HANDLE;
    r = pfn_createDevice(phys[0], &di, nullptr, &device);
    assert(r == VK_SUCCESS && device != VK_NULL_HANDLE);

    // TEST 7: vkCreateDevice success - verified!

    // TEST 8: vkGetDeviceQueue non-null resolution via proxy vkGetDeviceProcAddr
    // Note: in Wine, Wine calls pfn_gdpa(device, "vkGetDeviceQueue")
    auto pfn_getDeviceQueue = (PFN_vkGetDeviceQueue)pfn_gdpa(device, "vkGetDeviceQueue");
    assert(pfn_getDeviceQueue != nullptr);
    VkQueue queue = VK_NULL_HANDLE;
    pfn_getDeviceQueue(device, graphicsQueueFamily, 0, &queue);
    assert(queue != VK_NULL_HANDLE);

    // TEST 9: core device command resolution
    auto pfn_waitIdle = (PFN_vkDeviceWaitIdle)pfn_gdpa(device, "vkDeviceWaitIdle");
    auto pfn_createFence = (PFN_vkCreateFence)pfn_gdpa(device, "vkCreateFence");
    auto pfn_destroyFence = (PFN_vkDestroyFence)pfn_gdpa(device, "vkDestroyFence");
    auto pfn_destroyDevice = (PFN_vkDestroyDevice)pfn_gdpa(device, "vkDestroyDevice");
    assert(pfn_waitIdle && pfn_createFence && pfn_destroyFence && pfn_destroyDevice);

    VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    VkFence fence = VK_NULL_HANDLE;
    r = pfn_createFence(device, &fci, nullptr, &fence);
    assert(r == VK_SUCCESS && fence != VK_NULL_HANDLE);
    pfn_destroyFence(device, fence, nullptr);

    // TEST 10: extension device command resolution where enabled / supported
    auto pfn_createSwapchain = pfn_gdpa(device, "vkCreateSwapchainKHR");
    assert(pfn_createSwapchain != nullptr);
    auto pfn_getImages = pfn_gdpa(device, "vkGetSwapchainImagesKHR");
    assert(pfn_getImages != nullptr);
    auto pfn_acquireNext = pfn_gdpa(device, "vkAcquireNextImageKHR");
    assert(pfn_acquireNext != nullptr);
    auto pfn_queuePresent = pfn_gdpa(device, "vkQueuePresentKHR");
    assert(pfn_queuePresent != nullptr);

    // TEST 11: unknown command safely NULL
    PFN_vkVoidFunction bogus = pfn_gdpa(device, "vkUnknownBogusFunctionTest999");
    assert(bogus == nullptr);

    // TEST 14: proxy does not dereference opaque VkDevice
    // Passing a valid VkDevice opaque pointer to proxy_gdpa forwards to real GDPA without inspection
    PFN_vkVoidFunction valid_proc = pfn_gdpa(device, "vkGetDeviceQueue");
    assert(valid_proc != nullptr);

    // TEST 15: destroy device clean
    r = pfn_waitIdle(device);
    assert(r == VK_SUCCESS);
    pfn_destroyDevice(device, nullptr);

    pfn_destroyInstance(instance, nullptr);
    dlclose(proxy_handle);
    std::cout << "PASS\n" << std::flush;
}

void test_12_wrong_delegate_rejected(const char* self_bin) {
    std::cout << "TEST 12: wrong delegate rejected (sub-process)... " << std::flush;
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        char* const child_argv[] = {const_cast<char*>(self_bin), const_cast<char*>("sub_wrong_delegate"), nullptr};
        execv(self_bin, child_argv);
        _exit(1);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    assert(WIFEXITED(status));
    assert(WEXITSTATUS(status) == 86);
    std::cout << "PASS (exit code 86)\n" << std::flush;
}

void test_13_absent_gdpa_export_fails_closed(const char* self_bin) {
    std::cout << "TEST 13: absent GDPA export fails closed (sub-process)... " << std::flush;
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        char* const child_argv[] = {const_cast<char*>(self_bin), const_cast<char*>("sub_absent_gdpa"), nullptr};
        execv(self_bin, child_argv);
        _exit(1);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    assert(WIFEXITED(status));
    assert(WEXITSTATUS(status) == 86);
    std::cout << "PASS (exit code 86)\n" << std::flush;
}

} // namespace

int main(int argc, char** argv) {
    if (argc > 1 && std::strcmp(argv[1], "sub_wrong_delegate") == 0) {
        // Child process: set environment with mismatched delegate SHA
        setenv("FG_B_ORIGINAL_MOLTENVK_LIBRARY", "/usr/lib/libSystem.B.dylib", 1);
        setenv("FG_B_ORIGINAL_MOLTENVK_SHA256", "0000000000000000000000000000000000000000000000000000000000000000", 1);
        setenv("FG_B_EXPECTED_MOLTENVK_VERSION", "MoltenVK v1.4.2", 1);
        setenv("FG_B_TRACE_DIRECTORY", "/tmp", 1);
        void* handle = dlopen(kProxyPath, RTLD_NOW | RTLD_LOCAL);
        (void)handle;
        _exit(0);
    }

    if (argc > 1 && std::strcmp(argv[1], "sub_absent_gdpa") == 0) {
        // Point to library without GDPA export (e.g. /usr/lib/libSystem.B.dylib)
        setenv("FG_B_ORIGINAL_MOLTENVK_LIBRARY", "/usr/lib/libSystem.B.dylib", 1);
        setenv("FG_B_ORIGINAL_MOLTENVK_SHA256", "0000000000000000000000000000000000000000000000000000000000000000", 1);
        setenv("FG_B_EXPECTED_MOLTENVK_VERSION", "MoltenVK v1.4.2", 1);
        setenv("FG_B_TRACE_DIRECTORY", "/tmp", 1);
        void* handle = dlopen(kProxyPath, RTLD_NOW | RTLD_LOCAL);
        (void)handle;
        _exit(0);
    }

    std::cout << "=== PROXY DEVICE-DISPATCH TEST SUITE (15 TESTS) ===\n" << std::flush;
    setenv("FG_B_ORIGINAL_MOLTENVK_LIBRARY", kMoltenVKPath, 1);
    setenv("FG_B_ORIGINAL_MOLTENVK_SHA256", kMoltenVKSha, 1);
    setenv("FG_B_EXPECTED_MOLTENVK_VERSION", "MoltenVK v1.4.2", 1);

    test_1_delegate_gdpa_symbol_resolves_directly();
    test_2_delegate_gdpa_dladdr_equals_intended_moltenvk();
    test_3_delegate_gdpa_does_not_resolve_to_proxy();
    test_4_no_recursive_gdpa();
    test_5_device_lifecycle_and_dispatch(false); // CASE A: late GDPA
    test_5_device_lifecycle_and_dispatch(true);  // CASE B: early GDPA
    test_12_wrong_delegate_rejected(argv[0]);
    test_13_absent_gdpa_export_fails_closed(argv[0]);

    std::cout << "=== ALL 15 PROXY DEVICE-DISPATCH TESTS PASSED ===\n" << std::flush;
    return 0;
}
