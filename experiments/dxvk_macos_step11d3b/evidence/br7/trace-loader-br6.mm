#define VK_USE_PLATFORM_MACOS_MVK
#define VK_USE_PLATFORM_METAL_EXT
#include <vulkan/vulkan.h>
#include <vulkan/vulkan_metal.h>
#include <vulkan/vulkan_macos.h>

#import <AppKit/AppKit.h>
#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <objc/runtime.h>

#include <CommonCrypto/CommonDigest.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach_time.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <sys/sysctl.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits.h>
#include <mutex>
#include <new>
#include <string>
#include <strings.h>

#include "../dxvk_macos_step11d3a/trace_ring.hpp"

namespace {

constexpr size_t kTargetRingCapacity = 8192;
constexpr size_t kHelperRingCapacity = 1024;
constexpr size_t kLineCapacity = 8192;
constexpr char kExpectedMoltenVKVersion[] = "MoltenVK v1.4.2";
struct Line { uint32_t length = 0; char bytes[kLineCapacity]{}; };
using TargetRing = fgmetal::MpscRing<Line, kTargetRingCapacity>;
using HelperRing = fgmetal::MpscRing<Line, kHelperRingCapacity>;

TargetRing* g_target_ring = nullptr;
HelperRing* g_helper_ring = nullptr;
std::atomic<bool> g_running{false};
std::atomic<bool> g_enabled{false};
std::atomic<uint64_t> g_event_id{0};
std::atomic<uint64_t> g_dropped{0};
std::atomic<uint64_t> g_write_failures{0};
std::atomic<uint64_t> g_acquire_seq{0};
std::atomic<uint64_t> g_submit_seq{0};
std::atomic<uint64_t> g_present_seq{0};
std::atomic<uint64_t> g_drawable_seq{0};
std::atomic<uint64_t> g_next_drawable_call_seq{0};
struct ImageMapEntry { uint64_t swapchain; uint32_t index; uint64_t image; };
ImageMapEntry g_image_map[64]{};
std::atomic<uint32_t> g_image_map_size{0};
constexpr uint32_t kDrawableMapCapacity = 65536;
struct DrawableEntry {
    uint64_t drawable;
    uint64_t texture;
    uint64_t acquire;
    uint64_t id;
    uint64_t swapchain;
    uint64_t image;
    uint64_t submit;
    uint32_t image_index;
    uint64_t next_drawable_sequence;
    uint64_t texture_pixel_format;
    std::atomic<uint64_t> present{0};
    std::atomic<uint64_t> queue{0};
    std::atomic<uint64_t> present_swapchain{0};
    std::atomic<uint64_t> present_image{0};
    std::atomic<uint32_t> present_image_index{UINT32_MAX};
};
DrawableEntry g_drawables[kDrawableMapCapacity]{};
std::atomic<uint32_t> g_drawable_map_size{0};
std::atomic<bool> g_drawable_ready[kDrawableMapCapacity]{};
int g_fd = -1;
pid_t g_host_pid = 0;
pid_t g_target_unix_pid = -1;
const char* g_trace_role = "helper";
char g_experiment_token[33]{};
bool g_trace_requested = false;
bool g_target_process = false;
bool g_target_pid_valid = false;
pthread_t g_drain_thread{};
bool g_drain_thread_started = false;
mach_timebase_info_data_t g_timebase{};
std::atomic<uint64_t> g_process_start_mach_ns{0};
std::mutex g_layer_patch_mutex;
std::mutex g_image_map_mutex;
std::mutex g_runtime_startup_mutex;
std::atomic<int> g_runtime_startup_state{0};
std::atomic<bool> g_layer_config_emitted{false};
thread_local uint64_t t_acquire_seq = 0;
thread_local uint64_t t_swapchain = 0;
thread_local uint64_t t_image = 0;
thread_local uint32_t t_image_index = UINT32_MAX;
thread_local uint64_t t_submit_seq = 0;
thread_local uint64_t t_last_submit_seq = 0;

void layer_add_presented_handler(id self, SEL selector, void (^handler)(id));
void layer_drawable_dealloc(id self, SEL selector);
id<CAMetalDrawable> layer_next_drawable(id self, SEL selector);
void emit_layer_configuration(CAMetalLayer* layer);
void patch_wine_layer();

using PFN_GIPA = PFN_vkGetInstanceProcAddr;
using PFN_GDPA = PFN_vkGetDeviceProcAddr;
using PFN_MVKGetVersionStrings = void (VKAPI_PTR *)(char*, uint32_t, char*, uint32_t);
PFN_GIPA g_real_gipa = nullptr;
PFN_GDPA g_real_gdpa = nullptr;
PFN_MVKGetVersionStrings g_real_get_version_strings = nullptr;
PFN_vkAcquireNextImageKHR g_real_acquire = nullptr;
PFN_vkQueueSubmit2 g_real_submit2 = nullptr;
PFN_vkQueuePresentKHR g_real_present = nullptr;
PFN_vkGetSwapchainImagesKHR g_real_get_images = nullptr;
PFN_vkDestroyInstance g_real_destroy_instance = nullptr;
using PFN_CreateWin32Surface = VkResult (VKAPI_PTR *)(VkInstance, const void*,
    const VkAllocationCallbacks*, VkSurfaceKHR*);
PFN_vkCreateMetalSurfaceEXT g_real_create_metal_surface = nullptr;
PFN_vkCreateMacOSSurfaceMVK g_real_create_macos_surface = nullptr;
PFN_vkCreateSwapchainKHR g_real_create_swapchain = nullptr;
std::atomic<uint64_t> g_surface_create_seq{0};
std::atomic<uint64_t> g_swapchain_create_seq{0};
std::atomic<uint64_t> g_surface_handle{0};
std::atomic<uint64_t> g_surface_id{0};
std::atomic<uint64_t> g_surface_layer_id{0};
std::atomic<uint64_t> g_swapchain_handle{0};
std::atomic<uint64_t> g_swapchain_surface_id{0};
IMP g_real_next_drawable = nullptr;
IMP g_real_add_presented_handler = nullptr;
IMP g_real_dealloc = nullptr;
std::atomic<bool> g_layer_method_patched{false};
std::atomic<bool> g_present_method_patched{false};
std::atomic<bool> g_dealloc_method_patched{false};
std::atomic<bool> g_dispatch_identity_emitted[4]{};
void emit(const char* origin, const char* event, const char* fields);

size_t trace_ring_capacity() {
    return g_target_process ? kTargetRingCapacity : kHelperRingCapacity;
}

bool trace_ring_initialized() {
    return g_target_process ? g_target_ring != nullptr : g_helper_ring != nullptr;
}

bool trace_ring_empty() {
    return g_target_process ? (!g_target_ring || g_target_ring->empty())
        : (!g_helper_ring || g_helper_ring->empty());
}

bool trace_ring_push(const Line& line) {
    return g_target_process ? (g_target_ring && g_target_ring->try_push(line))
        : (g_helper_ring && g_helper_ring->try_push(line));
}

bool trace_ring_pop(Line& line) {
    return g_target_process ? (g_target_ring && g_target_ring->try_pop(line))
        : (g_helper_ring && g_helper_ring->try_pop(line));
}

void delete_trace_ring() {
    delete g_target_ring;
    g_target_ring = nullptr;
    delete g_helper_ring;
    g_helper_ring = nullptr;
}

void emit_dispatch_identity(const char* name, PFN_vkVoidFunction function,
                            uint32_t slot) {
    if (slot >= 4 || g_dispatch_identity_emitted[slot].exchange(true)) return;
    Dl_info info{};
    const bool resolved = dladdr((void*)function, &info) != 0;
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields),
        ",\"function\":\"%s\",\"resolved\":%s,\"library_path\":\"%s\",\"symbol_name\":\"%s\",\"function_identity\":%" PRIuPTR,
        name, resolved ? "true" : "false",
        resolved && info.dli_fname ? info.dli_fname : "",
        resolved && info.dli_sname ? info.dli_sname : "",
        (uintptr_t)function);
    emit("INSTRUMENTATION", "native_dispatch_source", fields);
}

uint64_t now_ns() {
    const uint64_t ticks = mach_absolute_time();
    return (uint64_t)(((__uint128_t)ticks * g_timebase.numer) / g_timebase.denom);
}

uint64_t tid() {
    uint64_t value = 0;
    (void)pthread_threadid_np(nullptr, &value);
    return value;
}

uint64_t host_surface_id(VkSwapchainKHR swapchain) {
    if ((uint64_t)(uintptr_t)swapchain != g_swapchain_handle.load(std::memory_order_acquire))
        return 0;
    return g_swapchain_surface_id.load(std::memory_order_acquire);
}

uint64_t image_for(uint64_t swapchain, uint32_t index) {
    const uint32_t count = g_image_map_size.load(std::memory_order_acquire);
    for (uint32_t i = 0; i < count && i < 64; ++i) {
        if (g_image_map[i].swapchain == swapchain && g_image_map[i].index == index)
            return g_image_map[i].image;
    }
    return 0;
}

uint64_t drawable_id_for(uint64_t drawable) {
    const uint32_t count = std::min(g_drawable_map_size.load(std::memory_order_acquire), kDrawableMapCapacity);
    for (uint32_t n = 0; n < count && n < kDrawableMapCapacity; ++n) {
        const uint32_t i = count - n - 1;
        if (g_drawable_ready[i].load(std::memory_order_acquire)
                && g_drawables[i].drawable == drawable) return g_drawables[i].id;
    }
    return 0;
}

const DrawableEntry* drawable_entry_for(uint64_t drawable) {
    const uint32_t count = std::min(g_drawable_map_size.load(std::memory_order_acquire), kDrawableMapCapacity);
    for (uint32_t n = 0; n < count && n < kDrawableMapCapacity; ++n) {
        const uint32_t i = count - n - 1;
        if (g_drawable_ready[i].load(std::memory_order_acquire)
                && g_drawables[i].drawable == drawable) return &g_drawables[i];
    }
    return nullptr;
}

DrawableEntry* drawable_entry_for_frame(uint64_t acquisition, uint64_t swapchain,
    uint32_t image_index, uint64_t image, uint64_t submit) {
    const uint32_t count = std::min(g_drawable_map_size.load(std::memory_order_acquire), kDrawableMapCapacity);
    DrawableEntry* match = nullptr;
    for (uint32_t n = 0; n < count && n < kDrawableMapCapacity; ++n) {
        const uint32_t i = count - n - 1;
        DrawableEntry& entry = g_drawables[i];
        if (g_drawable_ready[i].load(std::memory_order_acquire)
                && entry.acquire == acquisition && entry.swapchain == swapchain
                && entry.image_index == image_index && entry.image == image
                && entry.submit == submit && entry.texture_pixel_format != 0) {
            if (match) return nullptr;
            match = &entry;
        }
    }
    return match;
}

int format_trace_line(Line& line, const char* origin, const char* event,
                      const char* fields, uint64_t stamp, uint64_t event_id,
                      uint64_t thread, pid_t host_pid, pid_t target_unix_pid,
                      const char* experiment_token, const char* trace_role) {
    return std::snprintf(line.bytes, sizeof(line.bytes),
        "{\"origin\":\"%s\",\"event_id\":%" PRIu64
        ",\"event\":\"%s\",\"ts_mach_ns\":%" PRIu64
        ",\"ts_clock_domain\":\"mach_absolute_time_ns\""
        ",\"tid\":%" PRIu64 ",\"host_pid\":%ld"
        ",\"target_unix_pid\":%ld,\"experiment_token\":\"%s\""
        ",\"trace_role\":\"%s\"%s}\n", origin, event_id, event, stamp,
        thread, static_cast<long>(host_pid), static_cast<long>(target_unix_pid),
        experiment_token, trace_role, fields);
}

void emit_at(const char* origin, const char* event, const char* fields,
             uint64_t stamp) {
    if (!g_enabled.load(std::memory_order_acquire) || !trace_ring_initialized()) return;
    Line line{};
    const uint64_t event_id = g_event_id.fetch_add(1, std::memory_order_relaxed) + 1;
    const uint64_t thread = tid();
    const int size = format_trace_line(line, origin, event, fields, stamp,
        event_id, thread, g_host_pid, g_target_unix_pid, g_experiment_token,
        g_trace_role);
    if (size <= 0 || (size_t)size >= sizeof(line.bytes)) {
        g_dropped.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    line.length = (uint32_t)size;
    if (!trace_ring_push(line)) g_dropped.fetch_add(1, std::memory_order_relaxed);
}

void emit(const char* origin, const char* event, const char* fields = "") {
    emit_at(origin, event, fields, now_ns());
}

std::string canonical_path(const char* path) {
    if (!path || !*path) return {};
    char resolved[PATH_MAX]{};
    return realpath(path, resolved) ? std::string(resolved) : std::string();
}

std::string sha256_file(const char* path) {
    if (!path || !*path) return {};
    const int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return {};
    struct stat info{};
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode)) {
        close(fd);
        return {};
    }
    CC_SHA256_CTX context{};
    CC_SHA256_Init(&context);
    unsigned char buffer[64 * 1024];
    ssize_t count = 0;
    while ((count = read(fd, buffer, sizeof(buffer))) > 0)
        CC_SHA256_Update(&context, buffer, (CC_LONG)count);
    close(fd);
    if (count < 0) return {};
    unsigned char digest[CC_SHA256_DIGEST_LENGTH]{};
    CC_SHA256_Final(digest, &context);
    char hex[CC_SHA256_DIGEST_LENGTH * 2 + 1]{};
    for (size_t i = 0; i < CC_SHA256_DIGEST_LENGTH; ++i)
        std::snprintf(hex + (i * 2), sizeof(hex) - (i * 2), "%02x", digest[i]);
    return std::string(hex);
}

std::string image_for_path(const std::string& expected) {
    std::string found;
    uint32_t matches = 0;
    const uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; ++i) {
        const char* raw = _dyld_get_image_name(i);
        if (!raw) continue;
        const std::string image = canonical_path(raw);
        if (image == expected) { found = image; ++matches; }
    }
    return matches == 1 ? found : std::string();
}

uint32_t image_basename_count(const char* wanted) {
    uint32_t matches = 0;
    const uint32_t count = _dyld_image_count();
    for (uint32_t i = 0; i < count; ++i) {
        const char* raw = _dyld_get_image_name(i);
        if (!raw) continue;
        const char* slash = std::strrchr(raw, '/');
        const char* basename = slash ? slash + 1 : raw;
        if (!strcasecmp(basename, wanted)) ++matches;
    }
    return matches;
}

std::string image_for_address(const void* address) {
    Dl_info info{};
    if (!address || !dladdr(address, &info) || !info.dli_fname) return {};
    return canonical_path(info.dli_fname);
}

bool expected_file(const char* path_key, const char* hash_key,
                   std::string& path, std::string& hash) {
    const char* raw_path = std::getenv(path_key);
    const char* raw_hash = std::getenv(hash_key);
    path = canonical_path(raw_path);
    hash = raw_hash ? raw_hash : "";
    return !path.empty() && path == raw_path && hash.size() == 64
        && sha256_file(path.c_str()) == hash;
}

std::string json_quote(const std::string& value) {
    std::string result;
    result.reserve(value.size() + 2);
    result.push_back('"');
    for (const unsigned char c : value) {
        switch (c) {
            case '"': result += "\\\""; break;
            case '\\': result += "\\\\"; break;
            case '\b': result += "\\b"; break;
            case '\f': result += "\\f"; break;
            case '\n': result += "\\n"; break;
            case '\r': result += "\\r"; break;
            case '\t': result += "\\t"; break;
            default:
                if (c < 0x20) {
                    char escaped[7]{};
                    std::snprintf(escaped, sizeof(escaped), "\\u%04x", c);
                    result += escaped;
                } else {
                    result.push_back(static_cast<char>(c));
                }
        }
    }
    result.push_back('"');
    return result;
}

void emit_clock_metadata() {
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields),
        ",\"event_clock_source\":\"mach_absolute_time\",\"event_clock_domain\":\"mach_absolute_time_ns\""
        ",\"mach_timebase_numer\":%u,\"mach_timebase_denom\":%u,\"event_conversion\":\"floor(ticks*numer/denom)\""
        ",\"presented_time_source\":\"CAMetalDrawable.presentedTime\",\"presented_time_basis\":\"mach_absolute_time_seconds\""
        ",\"presented_time_clock_domain\":\"mach_absolute_time_ns\",\"presented_time_conversion\":\"floor(seconds*1e9)\""
        ",\"presented_time_clock_contract\":\"Apple Metal host-time seconds; present(at:) is Mach absolute seconds\""
        ",\"callback_delivery_clock_source\":\"mach_absolute_time\",\"callback_delivery_clock_domain\":\"mach_absolute_time_ns\"",
        g_timebase.numer, g_timebase.denom);
    emit("INSTRUMENTATION", "clock_metadata", fields);
}

void* drain(void*) {
    pthread_setname_np("step11d3b_trace");
    Line line{};
    while (g_running.load(std::memory_order_acquire) || !trace_ring_empty()) {
        if (!trace_ring_pop(line)) { usleep(500); continue; }
        size_t offset = 0;
        while (offset < line.length) {
            const ssize_t count = write(g_fd, line.bytes + offset, line.length - offset);
            if (count < 0 && errno == EINTR) continue;
            if (count <= 0) { g_write_failures.fetch_add(1, std::memory_order_relaxed); break; }
            offset += (size_t)count;
        }
    }
    return nullptr;
}

void stop_trace() {
    if (!g_enabled.exchange(false, std::memory_order_acq_rel)) return;
    g_running.store(false, std::memory_order_release);
    if (g_drain_thread_started) {
        pthread_join(g_drain_thread, nullptr);
        g_drain_thread_started = false;
    }
    if (g_fd >= 0) {
        char summary[512]{};
        const int length = std::snprintf(summary, sizeof(summary),
            "{\"origin\":\"LOGGER\",\"event_id\":%" PRIu64
            ",\"event\":\"trace_summary\",\"ts_mach_ns\":%" PRIu64
            ",\"ts_clock_domain\":\"mach_absolute_time_ns\""
            ",\"tid\":%" PRIu64 ",\"host_pid\":%ld"
            ",\"target_unix_pid\":%ld,\"experiment_token\":\"%s\""
            ",\"trace_role\":\"%s\",\"dropped_count\":%" PRIu64
            ",\"write_failure_count\":%" PRIu64 ",\"ring_capacity\":%zu}\n",
            g_event_id.fetch_add(1) + 1, now_ns(), tid(),
            static_cast<long>(g_host_pid), static_cast<long>(g_target_unix_pid),
            g_experiment_token, g_trace_role,
            g_dropped.load(), g_write_failures.load(), trace_ring_capacity());
        if (length > 0 && (size_t)length < sizeof(summary)) (void)write(g_fd, summary, (size_t)length);
        (void)fsync(g_fd);
        close(g_fd);
        g_fd = -1;
    }
    delete_trace_ring();
}

void trace_setup_error(const char* phase, int error_number) {
    std::fprintf(stderr,
        "FG_METAL_TRACE_INIT pid=%ld phase=%s errno=%d\n",
        static_cast<long>(getpid()), phase, error_number);
}

bool parse_decimal_u64(const char* raw, uint64_t& result,
                       bool allow_zero = false) {
    if (!raw || !*raw) return false;
    for (const char* cursor = raw; *cursor; ++cursor) {
        if (*cursor < '0' || *cursor > '9') return false;
    }
    errno = 0;
    char* end = nullptr;
    const unsigned long long parsed = std::strtoull(raw, &end, 10);
    if (errno || end == raw || *end != '\0' || (!allow_zero && parsed == 0))
        return false;
    if (std::to_string(parsed) != raw) return false;
    result = static_cast<uint64_t>(parsed);
    return true;
}

bool valid_experiment_token(const char* token) {
    if (!token || std::strlen(token) != 32) return false;
    for (size_t index = 0; index < 32; ++index) {
        if (!((token[index] >= '0' && token[index] <= '9')
              || (token[index] >= 'a' && token[index] <= 'f')))
            return false;
    }
    return true;
}

bool canonical_absolute_directory(const char* path) {
    if (!path || path[0] != '/') return false;
    const size_t length = strnlen(path, PATH_MAX);
    if (length == 0 || length >= PATH_MAX || (length > 1 && path[length - 1] == '/'))
        return false;
    size_t component_start = 1;
    for (size_t index = 1; index <= length; ++index) {
        if (index != length && path[index] != '/') continue;
        const size_t component_length = index - component_start;
        if (component_length == 0 && index != length) return false;
        if (component_length == 1 && path[component_start] == '.') return false;
        if (component_length == 2 && path[component_start] == '.'
                && path[component_start + 1] == '.') return false;
        component_start = index + 1;
    }
    char resolved[PATH_MAX]{};
    return realpath(path, resolved) && std::strcmp(path, resolved) == 0;
}

int open_bound_trace_directory(const char* path, uint64_t expected_device,
                               uint64_t expected_inode) {
    if (!canonical_absolute_directory(path)) {
        errno = EINVAL;
        return -1;
    }
    const int flags = O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC;
    int directory_fd = open("/", flags);
    if (directory_fd < 0) return -1;
    const std::string selected(path);
    size_t start = 1;
    while (start < selected.size()) {
        const size_t end = selected.find('/', start);
        const std::string component = selected.substr(
            start, end == std::string::npos ? std::string::npos : end - start);
        const int next_fd = openat(directory_fd, component.c_str(), flags);
        const int saved_errno = errno;
        close(directory_fd);
        if (next_fd < 0) {
            errno = saved_errno;
            return -1;
        }
        directory_fd = next_fd;
        if (end == std::string::npos) break;
        start = end + 1;
    }
    struct stat info{};
    if (fstat(directory_fd, &info) != 0) {
        const int saved_errno = errno;
        close(directory_fd);
        errno = saved_errno;
        return -1;
    }
    if (!S_ISDIR(info.st_mode) || info.st_uid != geteuid()
            || (info.st_mode & 0022) != 0
            || static_cast<uint64_t>(info.st_dev) != expected_device
            || static_cast<uint64_t>(info.st_ino) != expected_inode) {
        close(directory_fd);
        errno = ESTALE;
        return -1;
    }
    return directory_fd;
}

bool initialize_trace_identity() {
    g_host_pid = getpid();
    const char* directory = std::getenv("FG_B_TRACE_DIRECTORY");
    const char* raw_target_pid = std::getenv("FG_B_TARGET_UNIX_PID");
    const char* token = std::getenv("FG_B_EXPERIMENT_TOKEN");
    g_trace_requested = directory || raw_target_pid || token
        || std::getenv("FG_B_TRACE_DIRECTORY_DEVICE")
        || std::getenv("FG_B_TRACE_DIRECTORY_INODE");
    uint64_t target_pid = 0;
    if (!parse_decimal_u64(raw_target_pid, target_pid)
            || target_pid > static_cast<uint64_t>(std::numeric_limits<pid_t>::max())) {
        if (g_trace_requested) trace_setup_error("target_pid", EINVAL);
        return false;
    }
    g_target_unix_pid = static_cast<pid_t>(target_pid);
    g_target_process = g_host_pid == g_target_unix_pid;
    g_trace_role = g_target_process ? "target" : "helper";
    // A valid wrapper-provided PID is enough to classify this process. A
    // helper with a damaged trace token/path must still reach its exact
    // delegate; only the target requires a complete trace for authorization.
    g_target_pid_valid = true;
    if (!valid_experiment_token(token)) {
        if (g_trace_requested) trace_setup_error("experiment_token", EINVAL);
        return false;
    }
    std::memcpy(g_experiment_token, token, sizeof(g_experiment_token));
    return true;
}

void start_trace() {
    if (g_enabled.load()) return;
    if (!initialize_trace_identity()) return;
    if (!g_trace_requested) return;
    if (!std::getenv("FG_B_TRACE_DIRECTORY")) {
        trace_setup_error("trace_directory", EINVAL);
        return;
    }
    uint64_t expected_device = 0;
    uint64_t expected_inode = 0;
    if (!parse_decimal_u64(std::getenv("FG_B_TRACE_DIRECTORY_DEVICE"), expected_device, true)
            || !parse_decimal_u64(std::getenv("FG_B_TRACE_DIRECTORY_INODE"), expected_inode)) {
        trace_setup_error("directory_identity", EINVAL);
        return;
    }
    const char* directory = std::getenv("FG_B_TRACE_DIRECTORY");
    const int directory_fd = open_bound_trace_directory(
        directory, expected_device, expected_inode);
    if (directory_fd < 0) {
        const int saved_errno = errno;
        trace_setup_error("directory_open", saved_errno);
        return;
    }
    if (mach_timebase_info(&g_timebase) != KERN_SUCCESS || !g_timebase.denom) {
        close(directory_fd);
        trace_setup_error("mach_timebase", EINVAL);
        return;
    }
    char name[64]{};
    const int name_length = std::snprintf(name, sizeof(name),
        "host-trace.%ld.jsonl", static_cast<long>(g_host_pid));
    if (name_length <= 0 || static_cast<size_t>(name_length) >= sizeof(name)) {
        close(directory_fd);
        trace_setup_error("trace_filename", EOVERFLOW);
        return;
    }
    g_fd = openat(directory_fd, name,
        O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
    const int open_errno = errno;
    close(directory_fd);
    if (g_fd < 0) {
        trace_setup_error("trace_file_create", open_errno);
        return;
    }
    if (fchmod(g_fd, 0600) != 0) {
        const int saved_errno = errno;
        close(g_fd);
        g_fd = -1;
        trace_setup_error("trace_file_mode", saved_errno);
        return;
    }
    struct stat file_info{};
    if (fstat(g_fd, &file_info) != 0) {
        const int saved_errno = errno;
        close(g_fd);
        g_fd = -1;
        trace_setup_error("trace_file_identity", saved_errno);
        return;
    }
    if (!S_ISREG(file_info.st_mode) || file_info.st_nlink != 1
            || file_info.st_uid != geteuid() || (file_info.st_mode & 0777) != 0600) {
        close(g_fd);
        g_fd = -1;
        trace_setup_error("trace_file_identity", EINVAL);
        return;
    }
    bool ring_ready = false;
    if (g_target_process) {
        g_target_ring = new (std::nothrow) TargetRing();
        ring_ready = g_target_ring && g_target_ring->initialized();
    } else {
        g_helper_ring = new (std::nothrow) HelperRing();
        ring_ready = g_helper_ring && g_helper_ring->initialized();
    }
    if (!ring_ready) {
        close(g_fd);
        g_fd = -1;
        delete_trace_ring();
        trace_setup_error("trace_ring", ENOMEM);
        return;
    }
    g_running.store(true, std::memory_order_release);
    const int thread_error = pthread_create(&g_drain_thread, nullptr, drain, nullptr);
    if (thread_error != 0) {
        g_running.store(false);
        close(g_fd);
        g_fd = -1;
        delete_trace_ring();
        trace_setup_error("drain_thread", thread_error);
        return;
    }
    g_drain_thread_started = true;
    g_enabled.store(true, std::memory_order_release);
    emit_clock_metadata();
    std::atexit(stop_trace);
}

bool trace_initialization_must_abort() {
    return g_trace_requested && (!g_target_pid_valid
        || (g_target_process && !g_enabled.load(std::memory_order_acquire)));
}

void emit_startup_module(const char* role, const std::string& path,
                         const std::string& digest, const char* identity_kind,
                         bool loaded_in_process, const std::string& observed_path,
                         const std::string& observed_digest, bool identity_match) {
    char startup_id[48]{};
    std::snprintf(startup_id, sizeof(startup_id), "pid-%d", getpid());
    const std::string fields =
        ",\"startup_id\":" + json_quote(startup_id)
        + ",\"startup_phase\":true,\"role\":" + json_quote(role)
        + ",\"path\":" + json_quote(path)
        + ",\"sha256\":" + json_quote(digest)
        + ",\"identity_kind\":" + json_quote(identity_kind)
        + ",\"loaded_in_process\":" + (loaded_in_process ? "true" : "false")
        + ",\"observed_path\":" + json_quote(observed_path)
        + ",\"observed_sha256\":" + json_quote(observed_digest)
        + ",\"identity_match\":" + (identity_match ? "true" : "false");
    emit("INSTRUMENTATION", "startup_module_identity", fields.c_str());
}

bool emit_runtime_startup() {
    char executable_buffer[PATH_MAX]{};
    uint32_t executable_buffer_size = sizeof(executable_buffer);
    if (_NSGetExecutablePath(executable_buffer, &executable_buffer_size) != 0) return false;
    const std::string executable = canonical_path(executable_buffer);
    const std::string executable_sha = sha256_file(executable.c_str());
    std::string expected_wine_path, expected_wine_sha;
    std::string wine_launcher_path, wine_launcher_sha;
    std::string proxy_path, proxy_sha;
    std::string dll_path, dll_sha;
    std::string winevulkan_path, winevulkan_sha;
    std::string host_loader_path, host_loader_sha;
    std::string winemac_path, winemac_sha;
    std::string moltenvk_path, moltenvk_sha;
    std::string wineserver_path, wineserver_sha;
    const char* expected_version = std::getenv("FG_B_EXPECTED_MOLTENVK_VERSION");
    const bool files_valid =
        expected_file("FG_B_EXPECTED_WINE_PATH", "FG_B_EXPECTED_WINE_SHA256",
                      expected_wine_path, expected_wine_sha)
        && expected_file("FG_B_WINE_LAUNCHER_PATH", "FG_B_WINE_LAUNCHER_SHA256",
                         wine_launcher_path, wine_launcher_sha)
        && expected_file("FG_B_TRACE_PROXY_PATH", "FG_B_TRACE_PROXY_SHA256",
                         proxy_path, proxy_sha)
        && expected_file("FG_B_WINEVULKAN_DLL_PATH", "FG_B_WINEVULKAN_DLL_SHA256",
                         dll_path, dll_sha)
        && expected_file("FG_B_WINEVULKAN_SO_PATH", "FG_B_WINEVULKAN_SO_SHA256",
                         winevulkan_path, winevulkan_sha)
        && expected_file("FG_B_HOST_LOADER_PATH", "FG_B_HOST_LOADER_SHA256",
                         host_loader_path, host_loader_sha)
        && expected_file("FG_B_WINEMAC_PATH", "FG_B_WINEMAC_SHA256",
                         winemac_path, winemac_sha)
        && expected_file("FG_B_WINESERVER_PATH", "FG_B_WINESERVER_SHA256",
                         wineserver_path, wineserver_sha)
        && expected_file("FG_B_ORIGINAL_MOLTENVK_LIBRARY", "FG_B_ORIGINAL_MOLTENVK_SHA256",
                         moltenvk_path, moltenvk_sha);
    const char* wine_launcher_env = std::getenv("WINELOADER");
    const char* wineserver_env = std::getenv("WINESERVER");
    const bool launcher_environment_matches = wine_launcher_env
        && canonical_path(wine_launcher_env) == wine_launcher_path
        && sha256_file(wine_launcher_path.c_str()) == wine_launcher_sha;
    const bool wineserver_environment_matches = wineserver_env
        && canonical_path(wineserver_env) == wineserver_path
        && sha256_file(wineserver_path.c_str()) == wineserver_sha;
    const std::string proxy_image = image_for_address((const void*)&emit_runtime_startup);
    const std::string proxy_image_sha = sha256_file(proxy_image.c_str());
    const std::string delegate_image = g_real_gipa
        ? image_for_address((const void*)g_real_gipa) : std::string();
    const std::string delegate_image_sha = sha256_file(delegate_image.c_str());
    const std::string gdpa_image = g_real_gdpa
        ? image_for_address((const void*)g_real_gdpa) : std::string();
    const std::string gdpa_image_sha = sha256_file(gdpa_image.c_str());
    const std::string winevulkan_image = image_for_path(winevulkan_path);
    const std::string winevulkan_image_sha = sha256_file(winevulkan_image.c_str());
    const std::string host_loader_image = image_for_path(host_loader_path);
    const std::string host_loader_image_sha = sha256_file(host_loader_image.c_str());
    const std::string winemac_image = image_for_path(winemac_path);
    const std::string winemac_image_sha = sha256_file(winemac_image.c_str());
    const std::string moltenvk_image = image_for_path(moltenvk_path);
    const std::string moltenvk_image_sha = sha256_file(moltenvk_image.c_str());
    const std::string architecture = [] {
        struct utsname info{};
        return uname(&info) == 0 ? std::string(info.machine) : std::string();
    }();
    int translated = -1;
    size_t translated_size = sizeof(translated);
    if (sysctlbyname("sysctl.proc_translated", &translated, &translated_size, nullptr, 0) != 0)
        translated = -1;
    uint32_t vulkan_api_version = 0;
    if (g_real_gipa) {
        const auto enumerate_version = (PFN_vkEnumerateInstanceVersion)
            g_real_gipa(VK_NULL_HANDLE, "vkEnumerateInstanceVersion");
        if (enumerate_version) (void)enumerate_version(&vulkan_api_version);
    }
    char moltenvk_version_buffer[128]{};
    char vulkan_version_buffer[128]{};
    if (g_real_get_version_strings) {
        g_real_get_version_strings(moltenvk_version_buffer,
            sizeof(moltenvk_version_buffer), vulkan_version_buffer,
            sizeof(vulkan_version_buffer));
    }
    const std::string moltenvk_runtime_version(moltenvk_version_buffer,
        strnlen(moltenvk_version_buffer, sizeof(moltenvk_version_buffer)));
    const std::string vulkan_runtime_version(vulkan_version_buffer,
        strnlen(vulkan_version_buffer, sizeof(vulkan_version_buffer)));
    const bool runtime_version_valid = moltenvk_runtime_version == "1.4.2"
        && vulkan_runtime_version == "1.4.357";
    const bool loaded_modules = files_valid
        && launcher_environment_matches
        && wineserver_environment_matches
        && executable == expected_wine_path
        && executable_sha == expected_wine_sha
        && image_for_path(expected_wine_path) == expected_wine_path
        && sha256_file(expected_wine_path.c_str()) == expected_wine_sha
        && proxy_image == proxy_path
        && proxy_image_sha == proxy_sha
        && winevulkan_image == winevulkan_path
        && winevulkan_image_sha == winevulkan_sha
        && host_loader_image == host_loader_path
        && host_loader_image_sha == host_loader_sha
        && winemac_image == winemac_path
        && winemac_image_sha == winemac_sha
        && moltenvk_image == moltenvk_path
        && moltenvk_image_sha == moltenvk_sha
        && delegate_image == moltenvk_path
        && delegate_image_sha == moltenvk_sha
        && gdpa_image == moltenvk_path
        && gdpa_image_sha == moltenvk_sha
        && image_basename_count("winevulkan.so") == 1
        && image_basename_count("win32u.so") == 1
        && image_basename_count("winemac.so") == 1
        && image_basename_count("libMoltenVK.dylib") == 1
        && image_basename_count("libMoltenVK.1.dylib") == 1
        && image_basename_count("libvulkan.dylib") == 0;
    const bool valid = loaded_modules && architecture == "x86_64" && translated == 1
        && expected_version && std::strcmp(expected_version, kExpectedMoltenVKVersion) == 0
        && g_real_gipa && g_real_gdpa && (const void*)g_real_gdpa != (const void*)&vkGetDeviceProcAddr
        && g_real_create_metal_surface && g_real_get_version_strings
        && runtime_version_valid && vulkan_api_version != 0;

    char startup_id[48]{};
    std::snprintf(startup_id, sizeof(startup_id), "pid-%d", getpid());
    const uint64_t process_start = g_process_start_mach_ns.load(std::memory_order_acquire);
    char process_fields_prefix[256]{};
    std::snprintf(process_fields_prefix, sizeof(process_fields_prefix),
        ",\"pid\":%d,\"compiled_architecture\":\"x86_64\",\"process_architecture\":%s,\"process_translated\":%d,\"process_start_monotonic_ns\":%" PRIu64,
        getpid(), json_quote(architecture).c_str(), translated, process_start);
    const std::string process_fields =
        ",\"startup_id\":" + json_quote(startup_id) + process_fields_prefix
        + ",\"main_executable_path\":" + json_quote(executable)
        + ",\"main_executable_sha256\":" + json_quote(executable_sha)
        + ",\"wine_launcher_path\":" + json_quote(wine_launcher_path)
        + ",\"wine_launcher_sha256\":" + json_quote(wine_launcher_sha)
        + ",\"process_start_clock_domain\":\"mach_absolute_time_ns\""
        + ",\"source\":\"in-process uname, sysctl.proc_translated, _NSGetExecutablePath, dyld\"";
    emit("INSTRUMENTATION", "process_identity", process_fields.c_str());

    emit_startup_module("wine", expected_wine_path, expected_wine_sha,
                        "dyld-main-executable", true, executable, executable_sha,
                        executable == expected_wine_path && executable_sha == expected_wine_sha);
    emit_startup_module("wine_launcher", wine_launcher_path, wine_launcher_sha,
                        "prefixlease-spawn-executable", false, wine_launcher_path,
                        wine_launcher_sha, files_valid && launcher_environment_matches);
    emit_startup_module("winevulkan_dll", dll_path, dll_sha,
                        "win32-hmodule-proof-in-paired-app-trace", true, dll_path,
                        sha256_file(dll_path.c_str()),
                        !dll_path.empty() && sha256_file(dll_path.c_str()) == dll_sha);
    emit_startup_module("winevulkan_so", winevulkan_path, winevulkan_sha,
                        "dyld-loaded-image", winevulkan_image == winevulkan_path,
                        winevulkan_image, winevulkan_image_sha,
                        winevulkan_image == winevulkan_path && winevulkan_image_sha == winevulkan_sha);
    emit_startup_module("wine_host_loader", host_loader_path, host_loader_sha,
                        "dyld-loaded-image", host_loader_image == host_loader_path,
                        host_loader_image, host_loader_image_sha,
                        host_loader_image == host_loader_path && host_loader_image_sha == host_loader_sha);
    emit_startup_module("winemac_so", winemac_path, winemac_sha,
                        "dyld-loaded-image", winemac_image == winemac_path,
                        winemac_image, winemac_image_sha,
                        winemac_image == winemac_path && winemac_image_sha == winemac_sha);
    emit_startup_module("trace_proxy", proxy_path, proxy_sha,
                        "dyld-loaded-image", proxy_image == proxy_path,
                        proxy_image, proxy_image_sha,
                        proxy_image == proxy_path && proxy_image_sha == proxy_sha);
    emit_startup_module("moltenvk", moltenvk_path, moltenvk_sha,
                        "dyld-loaded-image", moltenvk_image == moltenvk_path,
                        moltenvk_image, moltenvk_image_sha,
                        moltenvk_image == moltenvk_path && moltenvk_image_sha == moltenvk_sha);
    emit_startup_module("wineserver", wineserver_path, wineserver_sha,
                        "prefixlease-bound-executable", false, wineserver_path,
                        wineserver_sha, files_valid && wineserver_environment_matches);

    const std::string proxy_fields =
        ",\"startup_id\":" + json_quote(startup_id)
        + ",\"real_gipa_available\":" + (g_real_gipa ? "true" : "false")
        + ",\"metal_surface_dlsym_available\":" + (g_real_create_metal_surface ? "true" : "false")
        + ",\"original_vulkan_library\":" + json_quote(moltenvk_path)
        + ",\"original_vulkan_library_sha256\":" + json_quote(moltenvk_sha)
        + ",\"loaded_delegate_path\":" + json_quote(delegate_image)
        + ",\"loaded_delegate_sha256\":" + json_quote(delegate_image_sha)
        + ",\"trace_proxy_path\":" + json_quote(proxy_path)
        + ",\"trace_proxy_sha256\":" + json_quote(proxy_sha)
        + ",\"loaded_proxy_path\":" + json_quote(proxy_image)
        + ",\"loaded_proxy_sha256\":" + json_quote(proxy_image_sha)
        + ",\"process_start_monotonic_ns\":" + std::to_string(process_start)
        + ",\"process_start_clock_domain\":\"mach_absolute_time_ns\"";
    emit("INSTRUMENTATION", "vulkan_proxy_identity", proxy_fields.c_str());

    const std::string startup_fields =
        ",\"startup_id\":" + json_quote(startup_id)
        + ",\"startup_valid\":" + (valid ? "true" : "false")
        + ",\"process_architecture\":" + json_quote(architecture)
        + ",\"process_translated\":" + std::to_string(translated)
        + ",\"process_start_monotonic_ns\":" + std::to_string(process_start)
        + ",\"process_start_clock_domain\":\"mach_absolute_time_ns\""
        + ",\"host_loader_role\":\"win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan\""
        + ",\"winevulkan_so_path\":" + json_quote(winevulkan_path)
        + ",\"winevulkan_so_sha256\":" + json_quote(winevulkan_sha)
        + ",\"wine_host_loader_path\":" + json_quote(host_loader_path)
        + ",\"wine_host_loader_sha256\":" + json_quote(host_loader_sha)
        + ",\"trace_proxy_path\":" + json_quote(proxy_path)
        + ",\"trace_proxy_sha256\":" + json_quote(proxy_sha)
        + ",\"moltenvk_path\":" + json_quote(moltenvk_path)
        + ",\"moltenvk_sha256\":" + json_quote(moltenvk_sha)
        + ",\"moltenvk_version\":" + json_quote("MoltenVK v" + moltenvk_runtime_version)
        + ",\"moltenvk_version_source\":\"vkGetVersionStringsMVK from loaded delegate\""
        + ",\"vulkan_runtime_version\":" + json_quote(vulkan_runtime_version)
        + ",\"vulkan_api_version\":" + std::to_string(vulkan_api_version)
        + ",\"loaded_moltenvk_count\":" + std::to_string(image_basename_count("libMoltenVK.1.dylib"))
        + ",\"loaded_proxy_count\":" + std::to_string(image_basename_count("libMoltenVK.dylib"))
        + ",\"loaded_winevulkan_count\":" + std::to_string(image_basename_count("winevulkan.so"))
        + ",\"loaded_host_loader_count\":" + std::to_string(image_basename_count("win32u.so"))
        + ",\"loaded_libvulkan_dylib_count\":" + std::to_string(image_basename_count("libvulkan.dylib"));
    emit("INSTRUMENTATION", "host_runtime_startup", startup_fields.c_str());
    return valid;
}

bool ensure_runtime_startup() {
    std::lock_guard<std::mutex> guard(g_runtime_startup_mutex);
    const int state = g_runtime_startup_state.load(std::memory_order_acquire);
    if (state == 2) return true;
    if (state == 3) return false;
    if (!g_target_process) {
        emit("INSTRUMENTATION", "helper_startup_skipped",
             ",\"startup_authorization\":false");
        g_runtime_startup_state.store(2, std::memory_order_release);
        return true;
    }
    const bool valid = emit_runtime_startup();
    g_runtime_startup_state.store(valid ? 2 : 3, std::memory_order_release);
    if (!valid) {
        emit("INSTRUMENTATION", "startup_identity_failure",
             ",\"reason\":\"loaded_module_or_Rosetta_identity_mismatch\"");
    }
    return valid;
}

VkResult VKAPI_CALL wrapped_acquire(VkDevice device, VkSwapchainKHR swapchain,
    uint64_t timeout, VkSemaphore semaphore, VkFence fence, uint32_t* image_index) {
    if (!g_real_acquire) return VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t seq = g_acquire_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    char fields[512]{};
    const uint64_t sw = (uint64_t)(uintptr_t)swapchain;
    const uint64_t surface_id = host_surface_id(swapchain);
    const VkResult result = g_real_acquire(device, swapchain, timeout, semaphore, fence, image_index);
    const uint64_t end = now_ns();
    const uint32_t idx = image_index ? *image_index : UINT32_MAX;
    const uint64_t image = image_for(sw, idx);
    t_acquire_seq = seq; t_swapchain = sw; t_image_index = idx; t_image = image;
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"device_id\":%" PRIuPTR ",\"call_begin_mach_ns\":%" PRIu64,
        seq, sw, surface_id, (uintptr_t)device, start);
    emit_at("WINE_SOURCE", "vkAcquireNextImageKHR_begin", fields, start);
    emit_at("MVK_BOUNDARY", "vkAcquireNextImageKHR_begin", fields, start);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"device_id\":%" PRIuPTR ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64,
        seq, sw, surface_id, (uintptr_t)device, idx, image, (int)result, start, end, end - start);
    emit_at("WINE_SOURCE", "vkAcquireNextImageKHR_end", fields, end);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64,
        seq, seq, sw, surface_id, idx, image, (int)result, start, end);
    emit_at("MVK_BOUNDARY", "vkAcquireNextImageKHR_end", fields, end);
    return result;
}

VkResult VKAPI_CALL wrapped_submit2(VkQueue queue, uint32_t count,
    const VkSubmitInfo2* submits, VkFence fence) {
    if (!g_real_submit2) return VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t seq = g_submit_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    char fields[512]{};
    const uint64_t previous = t_submit_seq;
    t_submit_seq = seq;
    const VkResult result = g_real_submit2(queue, count, submits, fence);
    const uint64_t end = now_ns();
    t_submit_seq = previous;
    t_last_submit_seq = seq;
    const uint64_t surface_id = host_surface_id((VkSwapchainKHR)(uintptr_t)t_swapchain);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"submit_count\":%u,\"call_begin_mach_ns\":%" PRIu64,
        seq, (uintptr_t)queue, t_acquire_seq, t_swapchain, surface_id, t_image_index, t_image, count, start);
    emit_at("WINE_SOURCE", "vkQueueSubmit2_begin", fields, start);
    emit_at("MVK_BOUNDARY", "vkQueueSubmit2_begin", fields, start);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64,
        seq, (uintptr_t)queue, t_acquire_seq, t_swapchain, surface_id, t_image_index, t_image, (int)result, start, end, end - start);
    emit_at("WINE_SOURCE", "vkQueueSubmit2_end", fields, end);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64,
        seq, seq, (uintptr_t)queue, t_acquire_seq, t_swapchain, surface_id, t_image_index, t_image, (int)result, start, end);
    emit_at("MVK_BOUNDARY", "vkQueueSubmit2_end", fields, end);
    return result;
}

VkResult VKAPI_CALL wrapped_present(VkQueue queue, const VkPresentInfoKHR* info) {
    if (!g_real_present || !info || !info->swapchainCount) return g_real_present ? g_real_present(queue, info) : VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t seq = g_present_seq.fetch_add(1) + 1;
    const uint64_t sw = (uint64_t)(uintptr_t)info->pSwapchains[0];
    const uint32_t idx = info->pImageIndices[0];
    const uint64_t image = image_for(sw, idx);
    const uint64_t surface_id = host_surface_id(info->pSwapchains[0]);
    const uint64_t start = now_ns();
    const uint64_t acquisition = t_acquire_seq;
    const uint64_t submit = t_last_submit_seq;
    const uint64_t present_image = image;
    DrawableEntry* drawable_entry = info->swapchainCount == 1
        ? drawable_entry_for_frame(acquisition, sw, idx, image, submit) : nullptr;
    if (drawable_entry) {
        drawable_entry->present.store(seq, std::memory_order_relaxed);
        drawable_entry->queue.store((uint64_t)(uintptr_t)queue, std::memory_order_relaxed);
        drawable_entry->present_swapchain.store(sw, std::memory_order_relaxed);
        drawable_entry->present_image.store(image, std::memory_order_relaxed);
        drawable_entry->present_image_index.store(idx, std::memory_order_release);
    }
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields), ",\"present_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"surface_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"submit_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"texture_id\":%" PRIu64 ",\"texture_pixel_format\":%" PRIu64 ",\"matched\":%s,\"swapchain_count\":%u,\"call_begin_mach_ns\":%" PRIu64,
        seq, (uintptr_t)queue, surface_id, acquisition, submit, sw, idx, image,
        drawable_entry ? drawable_entry->id : 0,
        drawable_entry ? drawable_entry->texture : 0,
        drawable_entry ? drawable_entry->texture_pixel_format : 0,
        drawable_entry ? "true" : "false", info->swapchainCount, start);
    emit_at("LAYER_SOURCE", "present_drawable_join", fields, start);
    const VkResult result = g_real_present(queue, info);
    const uint64_t end = now_ns();
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"present_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"call_begin_mach_ns\":%" PRIu64,
        seq, seq, (uintptr_t)queue, sw, surface_id, idx, image, acquisition, submit, start);
    emit_at("WINE_SOURCE", "vkQueuePresentKHR_begin", fields, start);
    emit_at("MVK_BOUNDARY", "vkQueuePresentKHR_begin", fields, start);
    std::snprintf(fields, sizeof(fields), ",\"driver_present_seq\":%" PRIu64 ",\"present_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64,
        seq, seq, (uintptr_t)queue, sw, surface_id, idx, present_image, acquisition, submit);
    emit_at("MVK_BOUNDARY", "present_request", fields, start);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"present_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64,
        seq, seq, (uintptr_t)queue, sw, surface_id, idx, present_image, acquisition, submit, (int)result, start, end, end - start);
    emit_at("WINE_SOURCE", "vkQueuePresentKHR_end", fields, end);
    std::snprintf(fields, sizeof(fields), ",\"wine_call_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_seq\":%" PRIu64 ",\"queue_id\":%" PRIuPTR ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64,
        seq, seq, seq, (uintptr_t)queue, sw, surface_id, idx, present_image, acquisition, submit, (int)result, start, end);
    emit_at("MVK_BOUNDARY", "vkQueuePresentKHR_end", fields, end);
    return result;
}

VkResult VKAPI_CALL wrapped_get_images(VkDevice device, VkSwapchainKHR swapchain,
    uint32_t* count, VkImage* images) {
    if (!g_real_get_images) return VK_ERROR_INITIALIZATION_FAILED;
    const VkResult result = g_real_get_images(device, swapchain, count, images);
    if (result == VK_SUCCESS && count && images) {
        std::lock_guard<std::mutex> guard(g_image_map_mutex);
        const uint64_t sw = (uint64_t)(uintptr_t)swapchain;
        for (uint32_t i = 0; i < *count && g_image_map_size.load() < 64; ++i) {
            const uint32_t slot = g_image_map_size.fetch_add(1);
            g_image_map[slot] = {sw, i, (uint64_t)(uintptr_t)images[i]};
            char fields[256]{};
            std::snprintf(fields, sizeof(fields), ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIuPTR,
                sw, host_surface_id(swapchain), i, (uintptr_t)images[i]);
            emit("MVK_BOUNDARY", "swapchain_image_identity", fields);
        }
    }
    return result;
}

void emit_surface_creation(const char* api_name, const char* phase,
    VkInstance instance, uint64_t layer_id, uint64_t surface_id,
    VkResult result, uint64_t start, uint64_t end, uint64_t sequence) {
    char event[96]{};
    std::snprintf(event, sizeof(event), "%s_%s", api_name, phase);
    char fields[1024]{};
    if (!std::strcmp(phase, "begin")) {
        std::snprintf(fields, sizeof(fields),
            ",\"wine_call_seq\":%" PRIu64 ",\"instance_id\":%" PRIuPTR ",\"layer_id\":%" PRIu64 ",\"call_begin_mach_ns\":%" PRIu64,
            sequence, (uintptr_t)instance, layer_id, start);
    } else {
        std::snprintf(fields, sizeof(fields),
            ",\"wine_call_seq\":%" PRIu64 ",\"instance_id\":%" PRIuPTR ",\"layer_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64,
            sequence, (uintptr_t)instance, layer_id, surface_id, (int)result,
            start, end, end >= start ? end - start : 0);
    }
    const uint64_t event_ns = !std::strcmp(phase, "begin") ? start : end;
    emit_at("WINE_SOURCE", event, fields, event_ns);
    emit_at("MVK_BOUNDARY", event, fields, event_ns);
}

void record_surface_creation(const char* api_name, VkInstance instance, uint64_t layer_id,
    VkResult result, VkSurfaceKHR* surface, uint64_t start, uint64_t end,
    uint64_t sequence) {
    const uint64_t handle = result == VK_SUCCESS && surface
        ? (uint64_t)(uintptr_t)(*surface) : 0;
    if (handle) {
        g_surface_handle.store(handle, std::memory_order_release);
        g_surface_id.store(handle, std::memory_order_release);
        g_surface_layer_id.store(layer_id, std::memory_order_release);
    }
    emit_surface_creation(api_name, "end", instance, layer_id, handle,
        result, start, end, sequence);
}

VkResult VKAPI_CALL wrapped_create_metal_surface(VkInstance instance,
    const VkMetalSurfaceCreateInfoEXT* create_info,
    const VkAllocationCallbacks* allocator, VkSurfaceKHR* surface) {
    if (!g_real_create_metal_surface) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ensure_runtime_startup()) return VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t sequence = g_surface_create_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    const uint64_t layer_id = create_info
        ? (uint64_t)(uintptr_t)create_info->pLayer : 0;
    emit_surface_creation("vkCreateMetalSurfaceEXT", "begin", instance,
        layer_id, 0, VK_SUCCESS, start, start, sequence);
    const VkResult result = g_real_create_metal_surface(instance, create_info, allocator, surface);
    const uint64_t end = now_ns();
    if (result == VK_SUCCESS && create_info && create_info->pLayer)
        emit_layer_configuration((CAMetalLayer*)create_info->pLayer);
    record_surface_creation("vkCreateMetalSurfaceEXT", instance, layer_id,
        result, surface, start, end, sequence);
    return result;
}

void VKAPI_CALL wrapped_destroy_instance(VkInstance instance,
    const VkAllocationCallbacks* allocator) {
    if (g_real_destroy_instance)
        g_real_destroy_instance(instance, allocator);
    stop_trace();
}

VkResult VKAPI_CALL wrapped_create_macos_surface(VkInstance instance,
    const VkMacOSSurfaceCreateInfoMVK* create_info,
    const VkAllocationCallbacks* allocator, VkSurfaceKHR* surface) {
    if (!g_real_create_macos_surface) return VK_ERROR_INITIALIZATION_FAILED;
    if (!ensure_runtime_startup()) return VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t sequence = g_surface_create_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    const uint64_t layer_id = create_info
        ? (uint64_t)(uintptr_t)create_info->pView : 0;
    emit_surface_creation("vkCreateMacOSSurfaceMVK", "begin", instance,
        layer_id, 0, VK_SUCCESS, start, start, sequence);
    const VkResult result = g_real_create_macos_surface(instance, create_info, allocator, surface);
    const uint64_t end = now_ns();
    // The pinned 11.17 route selects VK_EXT_metal_surface and passes a CAMetalLayer
    // directly. Keep a deterministic view identity for the MVK fallback path.
    record_surface_creation("vkCreateMacOSSurfaceMVK", instance, layer_id,
        result, surface, start, end, sequence);
    return result;
}

VkResult VKAPI_CALL wrapped_create_swapchain(VkDevice device,
    const VkSwapchainCreateInfoKHR* create_info,
    const VkAllocationCallbacks* allocator, VkSwapchainKHR* swapchain) {
    if (!g_real_create_swapchain || !create_info)
        return VK_ERROR_INITIALIZATION_FAILED;
    const uint64_t sequence = g_swapchain_create_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    const uint64_t surface = (uint64_t)(uintptr_t)create_info->surface;
    const uint64_t surface_id = surface == g_surface_handle.load(std::memory_order_acquire)
        ? g_surface_id.load(std::memory_order_acquire) : 0;
    const VkResult result = g_real_create_swapchain(device, create_info, allocator, swapchain);
    const uint64_t end = now_ns();
    const uint64_t handle = result == VK_SUCCESS && swapchain
        ? (uint64_t)(uintptr_t)(*swapchain) : 0;
    if (handle) {
        g_swapchain_handle.store(handle, std::memory_order_release);
        g_swapchain_surface_id.store(surface_id, std::memory_order_release);
    }
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields),
        ",\"wine_call_seq\":%" PRIu64 ",\"device_id\":%" PRIuPTR ",\"surface_id\":%" PRIu64 ",\"surface_handle\":%" PRIu64 ",\"requested_image_count\":%u,\"format\":%u,\"color_space\":%u,\"width\":%u,\"height\":%u,\"present_mode\":%u,\"call_begin_mach_ns\":%" PRIu64,
        sequence, (uintptr_t)device, surface_id, surface,
        create_info->minImageCount, (uint32_t)create_info->imageFormat,
        (uint32_t)create_info->imageColorSpace, create_info->imageExtent.width,
        create_info->imageExtent.height, (uint32_t)create_info->presentMode, start);
    emit_at("WINE_SOURCE", "vkCreateSwapchainKHR_begin", fields, start);
    emit_at("MVK_BOUNDARY", "vkCreateSwapchainKHR_begin", fields, start);
    std::snprintf(fields, sizeof(fields),
        ",\"wine_call_seq\":%" PRIu64 ",\"device_id\":%" PRIuPTR ",\"surface_id\":%" PRIu64 ",\"surface_handle\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"requested_image_count\":%u,\"format\":%u,\"color_space\":%u,\"width\":%u,\"height\":%u,\"present_mode\":%u,\"vk_result\":%d,\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64,
        sequence, (uintptr_t)device, surface_id, surface, handle,
        create_info->minImageCount, (uint32_t)create_info->imageFormat,
        (uint32_t)create_info->imageColorSpace, create_info->imageExtent.width,
        create_info->imageExtent.height, (uint32_t)create_info->presentMode,
        (int)result, start, end, end - start);
    emit_at("WINE_SOURCE", "vkCreateSwapchainKHR_end", fields, end);
    emit_at("MVK_BOUNDARY", "vkCreateSwapchainKHR_end", fields, end);
    return result;
}

PFN_vkVoidFunction wrap_instance_function(const char* name, PFN_vkVoidFunction fn) {
    if (!g_enabled.load(std::memory_order_acquire) || !name || !fn) return fn;
    if (!std::strcmp(name, "vkDestroyInstance")) {
        g_real_destroy_instance = (PFN_vkDestroyInstance)fn;
        return (PFN_vkVoidFunction)wrapped_destroy_instance;
    }
    if (!std::strcmp(name, "vkCreateMetalSurfaceEXT")) {
        g_real_create_metal_surface = (PFN_vkCreateMetalSurfaceEXT)fn;
        return (PFN_vkVoidFunction)wrapped_create_metal_surface;
    }
    if (!std::strcmp(name, "vkCreateMacOSSurfaceMVK")) {
        g_real_create_macos_surface = (PFN_vkCreateMacOSSurfaceMVK)fn;
        return (PFN_vkVoidFunction)wrapped_create_macos_surface;
    }
    return fn;
}

PFN_vkVoidFunction wrap_device_function(const char* name, PFN_vkVoidFunction fn) {
    if (!g_enabled.load(std::memory_order_acquire) || !name || !fn) return fn;
    if (!std::strcmp(name, "vkCreateSwapchainKHR")) {
        g_real_create_swapchain = (PFN_vkCreateSwapchainKHR)fn;
        return (PFN_vkVoidFunction)wrapped_create_swapchain;
    }
    if (!std::strcmp(name, "vkAcquireNextImageKHR")) { emit_dispatch_identity(name, fn, 0); g_real_acquire = (PFN_vkAcquireNextImageKHR)fn; return (PFN_vkVoidFunction)wrapped_acquire; }
    if (!std::strcmp(name, "vkQueueSubmit2") || !std::strcmp(name, "vkQueueSubmit2KHR")) { emit_dispatch_identity(name, fn, 1); g_real_submit2 = (PFN_vkQueueSubmit2)fn; return (PFN_vkVoidFunction)wrapped_submit2; }
    if (!std::strcmp(name, "vkQueuePresentKHR")) { emit_dispatch_identity(name, fn, 2); g_real_present = (PFN_vkQueuePresentKHR)fn; return (PFN_vkVoidFunction)wrapped_present; }
    if (!std::strcmp(name, "vkGetSwapchainImagesKHR")) { emit_dispatch_identity(name, fn, 3); g_real_get_images = (PFN_vkGetSwapchainImagesKHR)fn; return (PFN_vkVoidFunction)wrapped_get_images; }
    return fn;
}

PFN_vkVoidFunction VKAPI_CALL proxy_gdpa(VkDevice device, const char* name) {
    patch_wine_layer();
    if (!g_real_gdpa || !device || !name) return nullptr;
    return wrap_device_function(name, g_real_gdpa(device, name));
}

PFN_vkVoidFunction VKAPI_CALL proxy_gipa(VkInstance instance, const char* name) {
    patch_wine_layer();
    if (!g_real_gipa || !name) return nullptr;
    PFN_vkVoidFunction fn = g_real_gipa(instance, name);
    if (!std::strcmp(name, "vkGetDeviceProcAddr")) {
        if (fn && (const void*)fn != (const void*)&vkGetDeviceProcAddr) {
            g_real_gdpa = (PFN_GDPA)fn;
        }
        return (PFN_vkVoidFunction)proxy_gdpa;
    }
    return wrap_instance_function(name, wrap_device_function(name, fn));
}

void add_drawable_mapping(id<CAMetalDrawable> drawable, uint64_t id,
    uint64_t next_drawable_sequence, uint64_t texture_pixel_format) {
    if (!drawable) return;
    uint32_t slot = g_drawable_map_size.load(std::memory_order_relaxed);
    while (slot < kDrawableMapCapacity
           && !g_drawable_map_size.compare_exchange_weak(slot, slot + 1,
                std::memory_order_relaxed, std::memory_order_relaxed)) {}
    if (slot >= kDrawableMapCapacity) {
        g_dropped.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    DrawableEntry& entry = g_drawables[slot];
    entry.drawable = (uint64_t)(uintptr_t)drawable;
    entry.texture = (uint64_t)(uintptr_t)drawable.texture;
    entry.acquire = t_acquire_seq;
    entry.id = id;
    entry.swapchain = t_swapchain;
    entry.image = t_image;
    entry.submit = t_submit_seq;
    entry.image_index = t_image_index;
    entry.next_drawable_sequence = next_drawable_sequence;
    entry.texture_pixel_format = texture_pixel_format;
    g_drawable_ready[slot].store(true, std::memory_order_release);
}

id<CAMetalDrawable> layer_next_drawable(id self, SEL selector) {
    auto* layer = (CAMetalLayer*)self;
    const uint64_t layer_id = (uint64_t)(uintptr_t)self;
    char fields[1024]{};
    if (!g_layer_config_emitted.load(std::memory_order_acquire))
        emit_layer_configuration(layer);
    const uint64_t next_drawable_sequence = g_next_drawable_call_seq.fetch_add(1) + 1;
    const uint64_t start = now_ns();
    id<CAMetalDrawable> result = ((id<CAMetalDrawable>(*)(id, SEL))g_real_next_drawable)(self, selector);
    const uint64_t end = now_ns();
    const uint64_t drawable_id = result ? g_drawable_seq.fetch_add(1) + 1 : 0;
    const uint64_t texture_pixel_format = result
        ? (uint64_t)result.texture.pixelFormat : 0;
    add_drawable_mapping(result, drawable_id, next_drawable_sequence,
        texture_pixel_format);
    const uint64_t surface_id = host_surface_id((VkSwapchainKHR)(uintptr_t)t_swapchain);
    std::snprintf(fields, sizeof(fields), ",\"next_drawable_sequence\":%" PRIu64 ",\"layer_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"call_begin_mach_ns\":%" PRIu64,
        next_drawable_sequence, layer_id, surface_id, t_acquire_seq, t_swapchain,
        t_image_index, t_image, t_submit_seq, start);
    emit_at("LAYER_SOURCE", "next_drawable_begin", fields, start);
    std::snprintf(fields, sizeof(fields),
        ",\"next_drawable_sequence\":%" PRIu64 ",\"layer_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIuPTR ",\"texture_id\":%" PRIuPTR ",\"texture_pixel_format\":%" PRIu64 ",\"call_begin_mach_ns\":%" PRIu64 ",\"call_end_mach_ns\":%" PRIu64 ",\"duration_ns\":%" PRIu64
        ",\"detail\":\"%s\",",
        next_drawable_sequence, layer_id, surface_id, t_acquire_seq, t_swapchain,
        t_image_index, t_image, t_submit_seq, drawable_id, (uintptr_t)result,
        (uintptr_t)result.texture, texture_pixel_format, start, end, end - start,
        result ? "acquired" : "nil_return");
    const size_t length = std::strlen(fields);
    if (length && fields[length - 1] == ',') fields[length - 1] = '\0';
    emit_at("LAYER_SOURCE", "next_drawable_end", fields, end);
    if (result && (!g_present_method_patched.load(std::memory_order_acquire)
                   || !g_dealloc_method_patched.load(std::memory_order_acquire))) {
        Class cls = object_getClass(result);
        std::lock_guard<std::mutex> guard(g_layer_patch_mutex);
        if (!g_present_method_patched.exchange(true)) {
            SEL add = sel_registerName("addPresentedHandler:");
            Method m = class_getInstanceMethod(cls, add);
            if (m) {
                IMP original = method_getImplementation(m);
                const char* types = method_getTypeEncoding(m);
                (void)class_addMethod(cls, add, original, types);
                Method local = class_getInstanceMethod(cls, add);
                g_real_add_presented_handler = method_setImplementation(local, (IMP)layer_add_presented_handler);
                if (!g_real_add_presented_handler) g_present_method_patched.store(false);
            } else g_present_method_patched.store(false);
        }
        if (!g_dealloc_method_patched.exchange(true)) {
            SEL dealloc = sel_registerName("dealloc");
            Method m = class_getInstanceMethod(cls, dealloc);
            if (m) {
                IMP original = method_getImplementation(m);
                const char* types = method_getTypeEncoding(m);
                (void)class_addMethod(cls, dealloc, original, types);
                Method local = class_getInstanceMethod(cls, dealloc);
                g_real_dealloc = method_setImplementation(local, (IMP)layer_drawable_dealloc);
                if (!g_real_dealloc) g_dealloc_method_patched.store(false);
            } else g_dealloc_method_patched.store(false);
        }
    }
    return result;
}

void emit_layer_configuration(CAMetalLayer* layer) {
    if (!layer || g_layer_config_emitted.exchange(true, std::memory_order_acq_rel)) return;
    if (layer.drawableSize.width <= 0 || layer.drawableSize.height <= 0) {
        const CGFloat scale = layer.contentsScale > 0 ? layer.contentsScale : 1.0;
        const CGSize boundsSize = layer.bounds.size;
        const CGFloat w = boundsSize.width > 0 ? boundsSize.width : 640.0;
        const CGFloat h = boundsSize.height > 0 ? boundsSize.height : 360.0;
        layer.drawableSize = CGSizeMake(w * scale, h * scale);
    }
    const CGSize size = layer.drawableSize;
    static std::atomic<bool> s_window_floated{false};
    if (!s_window_floated.exchange(true, std::memory_order_acq_rel)) {
        dispatch_async(dispatch_get_main_queue(), ^{
            @autoreleasepool {
                [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
                [NSApp activateIgnoringOtherApps:YES];
                for (NSWindow* win in [NSApp windows]) {
                    win.level = NSNormalWindowLevel;
                    [win setHidesOnDeactivate:NO];
                    [win makeKeyAndOrderFront:nil];
                    [win orderFrontRegardless];
                }
            }
        });
    }
    const CGRect bounds = layer.bounds;
    const char* class_name = object_getClassName(layer);
    const char* class_image = class_getImageName(object_getClass(layer));
    const std::string layer_image = canonical_path(class_image);
    const std::string layer_image_sha = sha256_file(layer_image.c_str());
    const bool is_metal_layer = [layer isKindOfClass:[CAMetalLayer class]];
    char fields[1400]{};
    std::snprintf(fields, sizeof(fields),
        ",\"layer_id\":%" PRIu64 ",\"layer_class\":\"%s\",\"layer_is_cametal_layer\":%s"
        ",\"layer_class_image_path\":\"%s\",\"layer_class_image_sha256\":\"%s\""
        ",\"maximum_drawable_count\":%lu,\"allows_next_drawable_timeout\":%s,\"display_sync_enabled\":%s"
        ",\"drawable_width\":%.3f,\"drawable_height\":%.3f,\"contents_scale\":%.3f,\"pixel_format\":%lu,\"framebuffer_only\":%s"
        ",\"layer_bounds_width_points\":%.3f,\"layer_bounds_height_points\":%.3f",
        (uint64_t)(uintptr_t)layer, class_name ? class_name : "",
        is_metal_layer ? "true" : "false", layer_image.c_str(), layer_image_sha.c_str(),
        (unsigned long)layer.maximumDrawableCount,
        layer.allowsNextDrawableTimeout ? "true" : "false",
        layer.displaySyncEnabled ? "true" : "false", size.width, size.height,
        layer.contentsScale, (unsigned long)layer.pixelFormat,
        layer.framebufferOnly ? "true" : "false",
        bounds.size.width, bounds.size.height);
    emit("LAYER_SOURCE", "layer_configuration", fields);
}

void patch_wine_layer() {
    if (!g_enabled.load(std::memory_order_acquire)) return;
    if (g_layer_method_patched.load(std::memory_order_acquire)) return;
    std::lock_guard<std::mutex> guard(g_layer_patch_mutex);
    if (g_layer_method_patched.load(std::memory_order_relaxed)) return;
    Class cls = objc_getClass("WineMetalLayer");
    if (!cls) return;
    SEL selector = sel_registerName("nextDrawable");
    Method method = class_getInstanceMethod(cls, selector);
    if (!method) return;
    IMP original = method_getImplementation(method);
    const char* types = method_getTypeEncoding(method);
    if (!class_addMethod(cls, selector, original, types)) {
        Method own = class_getInstanceMethod(cls, selector);
        if (!own || method_getImplementation(own) == (IMP)layer_next_drawable) return;
    }
    Method local = class_getInstanceMethod(cls, selector);
    if (!local) return;
    g_real_next_drawable = method_setImplementation(local, (IMP)layer_next_drawable);
    if (g_real_next_drawable) g_layer_method_patched.store(true, std::memory_order_release);
}

void layer_add_presented_handler(id self, SEL selector, void (^handler)(id)) {
    const uint64_t drawable = (uint64_t)(uintptr_t)self;
    const DrawableEntry* entry = drawable_entry_for(drawable);
    const uint64_t drawable_id = entry ? entry->id : 0;
    const uint64_t acquisition = entry ? entry->acquire : 0;
    const uint64_t swapchain = entry ? entry->swapchain : 0;
    const uint64_t surface_id = entry
        ? host_surface_id((VkSwapchainKHR)(uintptr_t)entry->swapchain) : 0;
    const uint64_t image = entry ? entry->image : 0;
    const uint64_t submit = entry ? entry->submit : 0;
    const uint32_t drawable_image_index = entry ? entry->image_index : UINT32_MAX;
    const uint32_t present_image_index = entry
        ? entry->present_image_index.load(std::memory_order_acquire) : UINT32_MAX;
    const uint64_t present_swapchain = entry
        ? entry->present_swapchain.load(std::memory_order_relaxed) : 0;
    const uint64_t present_image = entry
        ? entry->present_image.load(std::memory_order_relaxed) : 0;
    const uint64_t present = entry ? entry->present.load(std::memory_order_relaxed) : 0;
    const uint64_t present_queue = entry ? entry->queue.load(std::memory_order_relaxed) : 0;
    const uint64_t request_ns = now_ns();
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields), ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_queue_id\":%" PRIu64 ",\"present_swapchain_id\":%" PRIu64 ",\"present_image_index\":%u,\"present_image_id\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIuPTR ",\"texture_id\":%" PRIu64,
        acquisition, swapchain, surface_id, drawable_image_index, image, submit, present,
        present_queue, present_swapchain, present_image_index, present_image, drawable_id,
        (uintptr_t)drawable, entry ? entry->texture : 0);
    void (^wrapped)(id) = ^(id presented) {
        const uint64_t callback_delivery_ns = now_ns();
        const CFTimeInterval seconds = ((id<CAMetalDrawable>)presented).presentedTime;
        const uint64_t presented_ns = seconds > 0 ? (uint64_t)(seconds * 1.0e9) : 0;
        char callback_fields[1536]{};
        std::snprintf(callback_fields, sizeof(callback_fields), ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_queue_id\":%" PRIu64 ",\"present_swapchain_id\":%" PRIu64 ",\"present_image_index\":%u,\"present_image_id\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIuPTR ",\"texture_id\":%" PRIu64 ",\"presented_time_mach_ns\":%" PRIu64 ",\"presentation_clock_domain\":\"mach_absolute_time_ns\",\"presented_time_basis\":\"mach_absolute_time_seconds\",\"presented_time_conversion\":\"floor(seconds*1e9)\",\"callback_delivery_clock_domain\":\"mach_absolute_time_ns\",\"is_displayed\":%s,",
            acquisition, swapchain, surface_id, drawable_image_index, image, submit, present,
            present_queue, present_swapchain, present_image_index, present_image, drawable_id,
            (uintptr_t)presented, entry ? entry->texture : 0, presented_ns,
            presented_ns ? "true" : "false");
        const size_t n = std::strlen(callback_fields);
        if (n && callback_fields[n - 1] == ',') callback_fields[n - 1] = '\0';
        if (handler) handler(presented);
        emit_at("LAYER_SOURCE", "presented_callback", callback_fields, callback_delivery_ns);
        std::snprintf(callback_fields, sizeof(callback_fields), ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_queue_id\":%" PRIu64 ",\"present_swapchain_id\":%" PRIu64 ",\"present_image_index\":%u,\"present_image_id\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIuPTR ",\"texture_id\":%" PRIu64,
            acquisition, swapchain, surface_id, drawable_image_index, image, submit, present,
            present_queue, present_swapchain, present_image_index, present_image, drawable_id,
            (uintptr_t)presented, entry ? entry->texture : 0);
        emit("LAYER_SOURCE", "availability_after_presented_handler", callback_fields);
    };
    ((void(*)(id, SEL, void (^)(id)))g_real_add_presented_handler)(self, selector, wrapped);
    std::snprintf(fields, sizeof(fields), ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_queue_id\":%" PRIu64 ",\"present_swapchain_id\":%" PRIu64 ",\"present_image_index\":%u,\"present_image_id\":%" PRIu64 ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIuPTR ",\"texture_id\":%" PRIu64,
        acquisition, swapchain, surface_id, drawable_image_index, image, submit, present,
        present_queue, present_swapchain, present_image_index, present_image, drawable_id,
        (uintptr_t)drawable, entry ? entry->texture : 0);
    emit_at("LAYER_SOURCE", "presented_handler_registered", fields, request_ns);
}

void layer_drawable_dealloc(id self, SEL selector) {
    const uint64_t drawable = (uint64_t)(uintptr_t)self;
    const uint64_t release_ns = now_ns();
    const DrawableEntry* entry = drawable_entry_for(drawable);
    const uint64_t surface_id = entry
        ? host_surface_id((VkSwapchainKHR)(uintptr_t)entry->swapchain) : 0;
    const uint32_t present_image_index = entry
        ? entry->present_image_index.load(std::memory_order_acquire) : UINT32_MAX;
    char fields[1024]{};
    std::snprintf(fields, sizeof(fields), ",\"drawable_id\":%" PRIu64 ",\"drawable_identity\":%" PRIu64 ",\"texture_id\":%" PRIu64 ",\"acquisition_sequence\":%" PRIu64 ",\"swapchain_id\":%" PRIu64 ",\"surface_id\":%" PRIu64 ",\"image_index\":%u,\"image_id\":%" PRIu64 ",\"driver_submit_seq\":%" PRIu64 ",\"driver_present_seq\":%" PRIu64 ",\"present_queue_id\":%" PRIu64 ",\"present_swapchain_id\":%" PRIu64 ",\"present_image_index\":%u,\"present_image_id\":%" PRIu64,
        entry ? entry->id : 0, drawable, entry ? entry->texture : 0,
        entry ? entry->acquire : 0, entry ? entry->swapchain : 0,
        surface_id, entry ? entry->image_index : UINT32_MAX, entry ? entry->image : 0,
        entry ? entry->submit : 0,
        entry ? entry->present.load(std::memory_order_relaxed) : 0,
        entry ? entry->queue.load(std::memory_order_relaxed) : 0,
        entry ? entry->present_swapchain.load(std::memory_order_relaxed) : 0,
        present_image_index,
        entry ? entry->present_image.load(std::memory_order_relaxed) : 0);
    ((void(*)(id, SEL))g_real_dealloc)(self, selector);
    emit_at("LAYER_SOURCE", "drawable_release", fields, release_ns);
}

#ifndef FG_TRACE_LOADER_OFFLINE_TEST
__attribute__((constructor)) void initialize_proxy() {
    start_trace();
    if (g_enabled.load(std::memory_order_acquire))
        g_process_start_mach_ns.store(now_ns(), std::memory_order_release);
    const char* original_path = std::getenv("FG_B_ORIGINAL_MOLTENVK_LIBRARY");
    const bool require_trace = g_trace_requested;
    if (trace_initialization_must_abort()) _exit(86);
    if (require_trace) {
        std::string bound_path, bound_sha;
        const char* expected_version = std::getenv("FG_B_EXPECTED_MOLTENVK_VERSION");
        if (!expected_file("FG_B_ORIGINAL_MOLTENVK_LIBRARY", "FG_B_ORIGINAL_MOLTENVK_SHA256",
                           bound_path, bound_sha)
                || !expected_version || std::strcmp(expected_version, kExpectedMoltenVKVersion) != 0) {
            emit("INSTRUMENTATION", "startup_identity_failure",
                 ",\"startup_id\":\"process\",\"reason\":\"delegate_binding_invalid\"");
            stop_trace();
            _exit(86);
        }
    }
    void* original_handle = nullptr;
    if (original_path && *original_path) {
        original_handle = dlopen(original_path, RTLD_NOW | RTLD_LOCAL);
        if (original_handle) {
            g_real_gipa = (PFN_GIPA)dlsym(original_handle, "vkGetInstanceProcAddr");
            g_real_create_metal_surface = (PFN_vkCreateMetalSurfaceEXT)
                dlsym(original_handle, "vkCreateMetalSurfaceEXT");
            g_real_create_macos_surface = (PFN_vkCreateMacOSSurfaceMVK)
                dlsym(original_handle, "vkCreateMacOSSurfaceMVK");
            g_real_get_version_strings = (PFN_MVKGetVersionStrings)
                dlsym(original_handle, "vkGetVersionStringsMVK");
            g_real_gdpa = (PFN_GDPA)dlsym(original_handle, "vkGetDeviceProcAddr");
        }
    }
    if (require_trace) {
        std::string bound_path, bound_sha;
        const std::string proxy_image = image_for_address((const void*)&initialize_proxy);
        const std::string proxy_image_sha = sha256_file(proxy_image.c_str());
        const std::string delegate_image = g_real_gipa
            ? image_for_address((const void*)g_real_gipa) : std::string();
        const std::string delegate_image_sha = sha256_file(delegate_image.c_str());
        const std::string gdpa_image = g_real_gdpa
            ? image_for_address((const void*)g_real_gdpa) : std::string();
        const std::string gdpa_image_sha = sha256_file(gdpa_image.c_str());
        char delegate_moltenvk_version[128]{};
        char delegate_vulkan_version[128]{};
        if (g_real_get_version_strings) {
            g_real_get_version_strings(delegate_moltenvk_version,
                sizeof(delegate_moltenvk_version), delegate_vulkan_version,
                sizeof(delegate_vulkan_version));
        }
        const std::string moltenvk_runtime_version(delegate_moltenvk_version,
            strnlen(delegate_moltenvk_version, sizeof(delegate_moltenvk_version)));
        const std::string vulkan_runtime_version(delegate_vulkan_version,
            strnlen(delegate_vulkan_version, sizeof(delegate_vulkan_version)));
        const char* expected_version = std::getenv("FG_B_EXPECTED_MOLTENVK_VERSION");
        const bool delegate_version_matches =
            moltenvk_runtime_version == "1.4.2"
            && vulkan_runtime_version == "1.4.357"
            && expected_version
            && std::strcmp(expected_version, kExpectedMoltenVKVersion) == 0;
        std::string expected_proxy_path, expected_proxy_sha;
        std::string expected_mvk_path, expected_mvk_sha;
        const bool proxy_expected_ok = expected_file("FG_B_TRACE_PROXY_PATH", "FG_B_TRACE_PROXY_SHA256",
                                                     expected_proxy_path, expected_proxy_sha);
        const bool mvk_expected_ok = expected_file("FG_B_ORIGINAL_MOLTENVK_LIBRARY", "FG_B_ORIGINAL_MOLTENVK_SHA256",
                                                   expected_mvk_path, expected_mvk_sha);
        const bool gdpa_valid = g_real_gdpa
            && (const void*)g_real_gdpa != (const void*)&vkGetDeviceProcAddr
            && gdpa_image == expected_mvk_path && gdpa_image_sha == expected_mvk_sha;
        if (!proxy_expected_ok || proxy_image != expected_proxy_path || proxy_image_sha != expected_proxy_sha
                || !mvk_expected_ok || delegate_image != expected_mvk_path || delegate_image_sha != expected_mvk_sha
                || !g_real_gipa || !g_real_create_metal_surface
                || !g_real_get_version_strings || !delegate_version_matches
                || !gdpa_valid) {
            emit("INSTRUMENTATION", "startup_identity_failure",
                 ",\"startup_id\":\"process\",\"reason\":\"proxy_or_delegate_load_identity_mismatch\"");
            stop_trace();
            if (!g_target_process)
                trace_setup_error("delegate_identity", EINVAL);
            _exit(86);
        }
        if (!g_target_process) {
            const std::string helper_fields =
                ",\"startup_authorization\":false"
                ",\"moltenvk_path\":" + json_quote(expected_mvk_path)
                + ",\"moltenvk_sha256\":" + json_quote(expected_mvk_sha)
                + ",\"loaded_delegate_path\":" + json_quote(delegate_image)
                + ",\"loaded_delegate_sha256\":" + json_quote(delegate_image_sha)
                + ",\"moltenvk_version\":"
                + json_quote("MoltenVK v" + moltenvk_runtime_version)
                + ",\"moltenvk_runtime_version\":"
                + json_quote(moltenvk_runtime_version)
                + ",\"vulkan_runtime_version\":" + json_quote(vulkan_runtime_version)
                + ",\"identity_match\":true";
            emit("INSTRUMENTATION", "helper_delegate_identity", helper_fields.c_str());
        }
    }
    patch_wine_layer();
}
#endif

}  // namespace

extern "C" __attribute__((visibility("default"))) PFN_vkVoidFunction VKAPI_CALL
vkGetInstanceProcAddr(VkInstance instance, const char* name) {
    if (!g_real_gipa) return nullptr;
    return proxy_gipa(instance, name);
}

extern "C" __attribute__((visibility("default"))) PFN_vkVoidFunction VKAPI_CALL
vkGetDeviceProcAddr(VkDevice device, const char* name) {
    if (!g_real_gdpa) return nullptr;
    return proxy_gdpa(device, name);
}

// winemac.drv probes this symbol with dlsym on its Vulkan loader handle.
// Exporting a forwarding wrapper keeps the pinned VK_EXT_metal_surface route
// selected while preserving the layer and native-surface trace boundary.
extern "C" __attribute__((visibility("default"))) VkResult VKAPI_CALL
vkCreateMetalSurfaceEXT(VkInstance instance,
    const VkMetalSurfaceCreateInfoEXT* create_info,
    const VkAllocationCallbacks* allocator, VkSurfaceKHR* surface) {
    return wrapped_create_metal_surface(instance, create_info, allocator, surface);
}

extern "C" __attribute__((visibility("default"))) VkResult VKAPI_CALL
vkCreateMacOSSurfaceMVK(VkInstance instance,
    const VkMacOSSurfaceCreateInfoMVK* create_info,
    const VkAllocationCallbacks* allocator, VkSurfaceKHR* surface) {
    return wrapped_create_macos_surface(instance, create_info, allocator, surface);
}
