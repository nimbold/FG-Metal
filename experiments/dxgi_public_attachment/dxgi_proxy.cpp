#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_6.h>
#include <d3d12.h>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <new>
#include <unordered_map>
#include <vector>

extern "C" HRESULT WINAPI CreateDXGIFactory(REFIID, void **);
extern "C" HRESULT WINAPI CreateDXGIFactory1(REFIID, void **);
extern "C" HRESULT WINAPI CreateDXGIFactory2(UINT, REFIID, void **);
extern "C" HRESULT WINAPI DXGIDeclareAdapterRemovalSupport();
extern "C" HRESULT WINAPI DXGIDisableVBlankVirtualization();
extern "C" HRESULT WINAPI DXGIGetDebugInterface(REFIID, void **);
extern "C" HRESULT WINAPI DXGIGetDebugInterface1(UINT, REFIID, void **);

namespace {

SRWLOCK gProviderLock = SRWLOCK_INIT;
HMODULE gProviderModule = nullptr;
SRWLOCK gLogLock = SRWLOCK_INIT;
std::atomic<int> gLogEnabled{-1};
std::atomic<int> gCopyEnabled{-1};
std::atomic<int> gSyntheticEnabled{-1};
std::atomic<bool> gSyntheticBuildWarningLogged{false};
std::atomic<int> gTestFailGpuWork{-1};
std::atomic<bool> gTestFailpointConsumed{false};
std::atomic<ULONGLONG> gNextSwapChainId{1};
std::atomic<ULONGLONG> gSelectedSyntheticSwapChain{0};

class ExclusiveSrwGuard {
public:
    explicit ExclusiveSrwGuard(SRWLOCK &lock) noexcept : lock_(lock) {
        AcquireSRWLockExclusive(&lock_);
    }
    ~ExclusiveSrwGuard() { ReleaseSRWLockExclusive(&lock_); }
    ExclusiveSrwGuard(const ExclusiveSrwGuard &) = delete;
    ExclusiveSrwGuard &operator=(const ExclusiveSrwGuard &) = delete;

private:
    SRWLOCK &lock_;
};

class ActiveCallScope {
public:
    explicit ActiveCallScope(std::atomic<UINT> &active) noexcept
        : active_(active), previous_(active_.fetch_add(1, std::memory_order_acq_rel)) {}
    ~ActiveCallScope() { active_.fetch_sub(1, std::memory_order_acq_rel); }
    ActiveCallScope(const ActiveCallScope &) = delete;
    ActiveCallScope &operator=(const ActiveCallScope &) = delete;

    UINT previous() const noexcept { return previous_; }

private:
    std::atomic<UINT> &active_;
    UINT previous_;
};

using CreateFactoryProc = HRESULT (WINAPI *)(REFIID, void **);
using CreateFactory2Proc = HRESULT (WINAPI *)(UINT, REFIID, void **);
using NoArgProc = HRESULT (WINAPI *)();
using DebugProc = HRESULT (WINAPI *)(REFIID, void **);
using Debug1Proc = HRESULT (WINAPI *)(UINT, REFIID, void **);

void log_line(const char *format, ...);

int enabled_env(std::atomic<int> &cache, const char *name, int defaultValue) {
    int current = cache.load(std::memory_order_relaxed);
    if (current >= 0) return current;
    char value[16]{};
    const DWORD count = GetEnvironmentVariableA(name, value, static_cast<DWORD>(sizeof(value)));
    const int parsed = count == 0 ? defaultValue : (value[0] != '0' && value[0] != '\0');
    int expected = -1;
    cache.compare_exchange_strong(expected, parsed, std::memory_order_relaxed);
    return cache.load(std::memory_order_relaxed);
}

bool log_enabled() { return enabled_env(gLogEnabled, "FG_DXGI_LOG", 0) != 0; }
bool copy_enabled() { return enabled_env(gCopyEnabled, "FG_DXGI_COPY", 0) != 0; }
bool synthetic_enabled() {
    const bool requested = enabled_env(gSyntheticEnabled, "FG_DXGI_G", 0) != 0;
#if defined(FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G)
    // The controlled experiment proved that this same-chain Present can break app buffer tracking.
    return requested;
#else
    if (requested && !gSyntheticBuildWarningLogged.exchange(true, std::memory_order_acq_rel))
        log_line("synthetic_g result=disabled_build_requires_FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G");
    return false;
#endif
}
bool test_fail_gpu_work() {
    return enabled_env(gTestFailGpuWork, "FG_DXGI_TEST_FAIL_GPU_WORK", 0) != 0;
}
bool test_failpoint(const char *point) {
    char value[64]{};
    const DWORD count = GetEnvironmentVariableA("FG_DXGI_TEST_FAILPOINT", value,
                                                 static_cast<DWORD>(sizeof(value)));
    if (!count || count >= sizeof(value) || std::strcmp(value, point) != 0) return false;
    bool expected = false;
    return gTestFailpointConsumed.compare_exchange_strong(expected, true,
                                                           std::memory_order_acq_rel);
}
UINT synthetic_sync_interval() {
    char value[8]{};
    return GetEnvironmentVariableA("FG_DXGI_G_SYNC_INTERVAL", value,
                                   static_cast<DWORD>(sizeof(value))) != 0 && value[0] == '0' ? 0u : 1u;
}

void log_line(const char *format, ...) {
    if (!log_enabled()) return;
    AcquireSRWLockExclusive(&gLogLock);
    FILE *file = std::fopen("fg_dxgi_proxy.log", "a");
    if (file) {
        LARGE_INTEGER qpc{};
        QueryPerformanceCounter(&qpc);
        std::fprintf(file, "qpc=%llu tid=%lu ", static_cast<unsigned long long>(qpc.QuadPart),
                     static_cast<unsigned long>(GetCurrentThreadId()));
        va_list args;
        va_start(args, format);
        std::vfprintf(file, format, args);
        va_end(args);
        std::fputc('\n', file);
        std::fclose(file);
    }
    ReleaseSRWLockExclusive(&gLogLock);
}

void log_qi_result(const char *objectKind, REFIID iid, HRESULT hr, bool rawEscape) {
    log_line("query_interface object=%s iid=%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x "
             "hr=0x%08lx disposition=%s",
             objectKind, static_cast<unsigned long>(iid.Data1), static_cast<unsigned>(iid.Data2),
             static_cast<unsigned>(iid.Data3), static_cast<unsigned>(iid.Data4[0]),
             static_cast<unsigned>(iid.Data4[1]), static_cast<unsigned>(iid.Data4[2]),
             static_cast<unsigned>(iid.Data4[3]), static_cast<unsigned>(iid.Data4[4]),
             static_cast<unsigned>(iid.Data4[5]), static_cast<unsigned>(iid.Data4[6]),
             static_cast<unsigned>(iid.Data4[7]), static_cast<unsigned long>(hr),
             rawEscape ? "raw_escape" : "wrapped_or_unavailable");
}

HMODULE own_module() {
    HMODULE module = nullptr;
    const auto address = reinterpret_cast<LPCWSTR>(
        reinterpret_cast<const void *>(&CreateDXGIFactory2));
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                           GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       address, &module);
    return module;
}

HMODULE load_provider() {
    AcquireSRWLockExclusive(&gProviderLock);
    if (!gProviderModule) {
        HMODULE module = LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll");
        if (module && module == own_module()) {
            log_line("provider_load recursion_detected module=%p", static_cast<void *>(module));
            FreeLibrary(module);
            module = nullptr;
        }
        if (module) {
            gProviderModule = module;
            log_line("provider_load ok module=%p self=%p", static_cast<void *>(module),
                     static_cast<void *>(own_module()));
        } else {
            log_line("provider_load failed error=%lu", static_cast<unsigned long>(GetLastError()));
        }
    }
    HMODULE result = gProviderModule;
    ReleaseSRWLockExclusive(&gProviderLock);
    return result;
}

template <typename T>
T provider_export(const char *name) {
    HMODULE module = load_provider();
    return module ? reinterpret_cast<T>(GetProcAddress(module, name)) : nullptr;
}

bool is_factory_iid(REFIID iid) {
    return IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
           IsEqualIID(iid, IID_IDXGIFactory) || IsEqualIID(iid, IID_IDXGIFactory1) ||
           IsEqualIID(iid, IID_IDXGIFactory2) || IsEqualIID(iid, IID_IDXGIFactory3) ||
           IsEqualIID(iid, IID_IDXGIFactory4) || IsEqualIID(iid, IID_IDXGIFactory5) ||
           IsEqualIID(iid, IID_IDXGIFactory6) || IsEqualIID(iid, IID_IDXGIFactory7) ||
           IsEqualIID(iid, IID_IDXGIFactoryMedia);
}
bool is_adapter_iid(REFIID iid) {
    return IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
           IsEqualIID(iid, IID_IDXGIAdapter) || IsEqualIID(iid, IID_IDXGIAdapter1) ||
           IsEqualIID(iid, IID_IDXGIAdapter2) || IsEqualIID(iid, IID_IDXGIAdapter3) ||
           IsEqualIID(iid, IID_IDXGIAdapter4);
}
bool is_output_iid(REFIID iid) {
    return IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
           IsEqualIID(iid, IID_IDXGIOutput) || IsEqualIID(iid, IID_IDXGIOutput1) ||
           IsEqualIID(iid, IID_IDXGIOutput2) || IsEqualIID(iid, IID_IDXGIOutput3) ||
           IsEqualIID(iid, IID_IDXGIOutput4) || IsEqualIID(iid, IID_IDXGIOutput5) ||
           IsEqualIID(iid, IID_IDXGIOutput6);
}

enum class ProxyKind : unsigned { Factory, Adapter, Output, SwapChain };
class ProxyBase;
SRWLOCK gProxyRegistryLock = SRWLOCK_INIT;
std::unordered_map<IUnknown *, ProxyBase *> gProxyRegistry;
std::unordered_map<void *, ProxyBase *> gProxyInterfaceRegistry;

class ProxyBase {
public:
    ProxyBase(IUnknown *identity, ProxyKind kind) : identity_(identity), kind_(kind) {}
    virtual ~ProxyBase() {
        if (identity_) identity_->Release();
        identity_ = nullptr;
    }

    ProxyKind kind() const { return kind_; }
    IUnknown *identity() const { return identity_; }
    virtual void *public_interface_address() = 0;

    ULONG add_ref() { return refs_.fetch_add(1, std::memory_order_relaxed) + 1; }
    ULONG release_ref() {
        bool destroy = false;
        ULONG remaining = 0;
        AcquireSRWLockExclusive(&gProxyRegistryLock);
        remaining = refs_.fetch_sub(1, std::memory_order_acq_rel) - 1;
        if (!remaining) {
            const auto it = gProxyRegistry.find(identity_);
            if (it != gProxyRegistry.end() && it->second == this) gProxyRegistry.erase(it);
            const auto publicIt = gProxyInterfaceRegistry.find(public_interface_address());
            if (publicIt != gProxyInterfaceRegistry.end() && publicIt->second == this)
                gProxyInterfaceRegistry.erase(publicIt);
            destroy = true;
        }
        ReleaseSRWLockExclusive(&gProxyRegistryLock);
        if (destroy) delete this;
        return remaining;
    }

private:
    friend ProxyBase *registry_adopt_or_get(ProxyBase *candidate);
    void add_ref_registry_locked() { refs_.fetch_add(1, std::memory_order_relaxed); }

    std::atomic<ULONG> refs_{1};
    IUnknown *identity_ = nullptr;
    ProxyKind kind_;
};

ProxyBase *registry_adopt_or_get(ProxyBase *candidate) {
    if (!candidate || !candidate->identity()) return nullptr;
    ProxyBase *result = nullptr;
    bool inserted = false;
    AcquireSRWLockExclusive(&gProxyRegistryLock);
    const auto it = gProxyRegistry.find(candidate->identity());
    if (it != gProxyRegistry.end()) {
        if (it->second->kind() == candidate->kind()) {
            it->second->add_ref_registry_locked();
            result = it->second;
        }
    } else {
        try {
            auto [identityIt, identityInserted] =
                gProxyRegistry.emplace(candidate->identity(), candidate);
            if (identityInserted) {
                const auto [interfaceIt, interfaceInserted] =
                    gProxyInterfaceRegistry.emplace(candidate->public_interface_address(), candidate);
                (void)interfaceIt;
                if (interfaceInserted) {
                    result = candidate;
                    inserted = true;
                } else {
                    gProxyRegistry.erase(identityIt);
                }
            }
        } catch (...) {
            gProxyRegistry.erase(candidate->identity());
            result = nullptr;
        }
    }
    ReleaseSRWLockExclusive(&gProxyRegistryLock);
    if (!inserted && result != candidate) candidate->release_ref();
    return result;
}

ProxyBase *registry_find_public_interface(void *interfaceAddress) {
    if (!interfaceAddress) return nullptr;
    ProxyBase *result = nullptr;
    AcquireSRWLockShared(&gProxyRegistryLock);
    const auto it = gProxyInterfaceRegistry.find(interfaceAddress);
    if (it != gProxyInterfaceRegistry.end()) result = it->second;
    ReleaseSRWLockShared(&gProxyRegistryLock);
    return result;
}

class FactoryProxy;
class AdapterProxy;
class OutputProxy;
class SwapChainProxy;
HRESULT wrap_factory_result(REFIID iid, void **result);
HRESULT wrap_adapter_result(REFIID iid, void **result);
HRESULT wrap_output_result(REFIID iid, void **result);

template <typename T>
void wrap_swapchain_result(IUnknown *device, T **result);

template <typename T>
T *unwrap_dxgi_object(T *object);

template <typename T>
void release_com(T *&value) {
    if (value) {
        value->Release();
        value = nullptr;
    }
}

struct CopySlot {
    ID3D12CommandAllocator *allocator = nullptr;
    ID3D12DescriptorHeap *rtvHeap = nullptr;
    ID3D12Resource *source = nullptr;
    ID3D12Resource *destination = nullptr;
    UINT64 fenceValue = 0;
    D3D12_RESOURCE_DESC description{};
    bool hasDescription = false;
};

enum class FenceWaitResult { Complete, DeviceRemoved, Timeout, Error };
enum class DrainResult { Complete, DeviceRemoved, Unresolved };

class SwapChainProxy final : public IDXGISwapChain4, public IDXGISwapChainMedia, public ProxyBase {
public:
    SwapChainProxy(IUnknown *identity, IDXGISwapChain4 *inner, IUnknown *deviceArgument)
        : ProxyBase(identity, ProxyKind::SwapChain), inner_(inner),
          id_(gNextSwapChainId.fetch_add(1, std::memory_order_relaxed)) {
        inner_->QueryInterface(IID_IDXGISwapChainMedia, reinterpret_cast<void **>(&media_));
        if (deviceArgument) {
            const HRESULT queueHr = deviceArgument->QueryInterface(
                IID_ID3D12CommandQueue, reinterpret_cast<void **>(&queue_));
            if (SUCCEEDED(queueHr)) {
                const D3D12_COMMAND_QUEUE_DESC queueDesc = queue_->GetDesc();
                if (queueDesc.Type == D3D12_COMMAND_LIST_TYPE_DIRECT) {
                    const HRESULT deviceHr = queue_->GetDevice(
                        IID_ID3D12Device, reinterpret_cast<void **>(&device_));
                    const UINT nodeCount = SUCCEEDED(deviceHr) && device_ ? device_->GetNodeCount() : 0;
                    d3d12_ = SUCCEEDED(deviceHr) && device_ && nodeCount == 1 &&
                             (queueDesc.NodeMask == 0 || queueDesc.NodeMask == 1);
                    if (d3d12_) {
                        log_line("swapchain_create id=%llu this=%p inner=%p device_arg=%p queue=%p "
                                 "queue_type=%u node_mask=0x%08x node_count=%u device=%p "
                                 "device_hr=0x%08lx d3d12=%u",
                                 static_cast<unsigned long long>(id_), static_cast<void *>(this),
                                 static_cast<void *>(inner_), static_cast<void *>(deviceArgument),
                                 static_cast<void *>(queue_), static_cast<unsigned>(queueDesc.Type),
                                 queueDesc.NodeMask, nodeCount, static_cast<void *>(device_),
                                 static_cast<unsigned long>(deviceHr), d3d12_);
                    } else {
                        log_line("swapchain_create id=%llu unsupported_queue_device_config=%u "
                                 "node_mask=0x%08x node_count=%u device_hr=0x%08lx",
                                 static_cast<unsigned long long>(id_),
                                 static_cast<unsigned>(queueDesc.Type), queueDesc.NodeMask,
                                 nodeCount, static_cast<unsigned long>(deviceHr));
                        release_com(device_);
                        release_com(queue_);
                    }
                } else {
                    log_line("swapchain_create id=%llu unsupported_queue_type=%u node_mask=0x%08x "
                             "device_arg=%p",
                             static_cast<unsigned long long>(id_),
                             static_cast<unsigned>(queueDesc.Type), queueDesc.NodeMask,
                             static_cast<void *>(deviceArgument));
                    release_com(queue_);
                }
            } else {
                log_line("swapchain_create id=%llu unsupported_device_path device_arg=%p "
                         "queue_qi_hr=0x%08lx",
                         static_cast<unsigned long long>(id_), static_cast<void *>(deviceArgument),
                         static_cast<unsigned long>(queueHr));
            }
        }
    }

    void *public_interface_address() override { return static_cast<IDXGISwapChain4 *>(this); }

    ~SwapChainProxy() {
        const DrainResult drain = drain_copy_work("destroy", 250);
        if (drain == DrainResult::Complete || drain == DrainResult::DeviceRemoved) {
            release_gpu_state();
            release_com(device_);
            release_com(queue_);
        } else {
            log_line("gpu_state id=%llu result=quarantined_until_process_exit reason=destroy_drain_unresolved "
                     "queue=%p device=%p", static_cast<unsigned long long>(id_),
                     static_cast<void *>(queue_), static_cast<void *>(device_));
            // Raw COM references intentionally remain unreleased when submitted work cannot be retired.
            device_ = nullptr;
            queue_ = nullptr;
            commandList_ = nullptr;
            fence_ = nullptr;
            fenceEvent_ = nullptr;
            for (CopySlot &slot : slots_) {
                slot.source = nullptr;
                slot.destination = nullptr;
                slot.allocator = nullptr;
                slot.rtvHeap = nullptr;
            }
        }
        release_com(media_);
        release_com(inner_);
        ULONGLONG expected = id_;
        if (gSelectedSyntheticSwapChain.compare_exchange_strong(expected, 0,
                                                                 std::memory_order_acq_rel))
            log_line("synthetic_g_select id=%llu result=released_on_destroy",
                     static_cast<unsigned long long>(id_));
        log_line("swapchain_destroy id=%llu this=%p", static_cast<unsigned long long>(id_),
                 static_cast<void *>(this));
    }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
            IsEqualIID(iid, IID_IDXGIDeviceSubObject) || IsEqualIID(iid, IID_IDXGISwapChain) ||
            IsEqualIID(iid, IID_IDXGISwapChain1) || IsEqualIID(iid, IID_IDXGISwapChain2) ||
            IsEqualIID(iid, IID_IDXGISwapChain3) || IsEqualIID(iid, IID_IDXGISwapChain4)) {
            void *check = nullptr;
            const HRESULT hr = inner_->QueryInterface(iid, &check);
            if (FAILED(hr)) {
                log_qi_result("swapchain", iid, hr, false);
                return hr;
            }
            static_cast<IUnknown *>(check)->Release();
            *object = static_cast<IDXGISwapChain4 *>(this);
            AddRef();
            log_qi_result("swapchain", iid, S_OK, false);
            return S_OK;
        }
        if (IsEqualIID(iid, IID_IDXGISwapChainMedia) && media_) {
            *object = static_cast<IDXGISwapChainMedia *>(this);
            AddRef();
            log_qi_result("swapchain", iid, S_OK, false);
            return S_OK;
        }
        const HRESULT hr = inner_->QueryInterface(iid, object);
        log_qi_result("swapchain", iid, hr, SUCCEEDED(hr));
        return hr;
    }

    ULONG STDMETHODCALLTYPE AddRef() override { return add_ref(); }
    ULONG STDMETHODCALLTYPE Release() override { return release_ref(); }

    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID guid, UINT size, const void *data) override {
        return inner_->SetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID guid, const IUnknown *object) override {
        return inner_->SetPrivateDataInterface(guid, object);
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID guid, UINT *size, void *data) override {
        return inner_->GetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID iid, void **parent) override {
        const HRESULT hr = inner_->GetParent(iid, parent);
        if (SUCCEEDED(hr) && parent && *parent && is_factory_iid(iid))
            return wrap_factory_result(iid, parent);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetDevice(REFIID iid, void **device) override {
        return inner_->GetDevice(iid, device);
    }

    HRESULT STDMETHODCALLTYPE Present(UINT syncInterval, UINT flags) override {
        if (!log_enabled() && !copy_enabled() && !synthetic_enabled())
            return inner_->Present(syncInterval, flags);
        ActiveCallScope activeCall(activePresentCalls_);
        const bool concurrentCall = activeCall.previous() != 0;
        if (concurrentCall) {
            concurrentPresentCalls_.store(true, std::memory_order_release);
            ULONGLONG expected = id_;
            gSelectedSyntheticSwapChain.compare_exchange_strong(expected, 0,
                                                                 std::memory_order_acq_rel);
        }

        UINT index = UINT_MAX;
        DXGI_SWAP_CHAIN_DESC1 desc{};
        HRESULT descHr = DXGI_ERROR_INVALID_CALL;
        ULONGLONG resizeGeneration = 0;
        bool canAttemptAdapterWork = false;
        void *queueAddress = nullptr;
        void *deviceAddress = nullptr;
        if (!concurrentCall) {
            ExclusiveSrwGuard guard(mutex_);
            resizeGeneration = resizeGeneration_;
            queueAddress = queue_;
            deviceAddress = device_;
            if (!resizeInProgress_ && !concurrentPresentCalls_.load(std::memory_order_acquire)) {
                index = current_index();
                descHr = inner_->GetDesc1(&desc);
                canAttemptAdapterWork = true;
                if (flags & DXGI_PRESENT_TEST) {
                    if (copy_enabled())
                        log_line("gpu_copy id=%llu call=Present result=skipped_test_present",
                                 static_cast<unsigned long long>(id_));
                } else {
                    try {
                        copy_current_buffer(index, descHr == S_OK ? &desc : nullptr, "Present");
                    } catch (...) {
                        disable_adapter_work("copy_exception");
                        log_line("gpu_copy id=%llu result=disabled_exception",
                                 static_cast<unsigned long long>(id_));
                    }
                }
            } else if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
                disable_adapter_work("concurrent_present_calls");
            } else {
                log_line("adapter_work id=%llu result=skipped_resize_in_progress",
                         static_cast<unsigned long long>(id_));
            }
        }
        LARGE_INTEGER before{}, after{};
        QueryPerformanceCounter(&before);
        const HRESULT hr = inner_->Present(syncInterval, flags);
        QueryPerformanceCounter(&after);
        if (canAttemptAdapterWork && hr == S_OK && flags == 0) {
            try {
                ExclusiveSrwGuard guard(mutex_);
                if (resizeInProgress_ || resizeGeneration_ != resizeGeneration) {
                    log_line("synthetic_g id=%llu source=Present result=skipped_swapchain_lifecycle_change",
                             static_cast<unsigned long long>(id_));
                } else if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
                    disable_adapter_work("concurrent_present_calls");
                } else {
                    present_diagnostic_g("Present");
                }
            } catch (...) {
                ExclusiveSrwGuard guard(mutex_);
                disable_adapter_work("synthetic_exception");
                log_line("synthetic_g id=%llu result=disabled_exception",
                         static_cast<unsigned long long>(id_));
            }
        }
        const ULONGLONG sequence = presentCount_.fetch_add(1, std::memory_order_relaxed) + 1;
        log_line("present id=%llu kind=Present sequence=%llu index=%u width=%u height=%u format=%u "
                 "sync=%u flags=0x%08x queue=%p device=%p qpc=%llu duration_ticks=%llu hr=0x%08lx",
                 static_cast<unsigned long long>(id_), static_cast<unsigned long long>(sequence),
                 index, desc.Width, desc.Height, static_cast<unsigned>(desc.Format), syncInterval, flags,
                 queueAddress, deviceAddress,
                 static_cast<unsigned long long>(before.QuadPart),
                 static_cast<unsigned long long>(after.QuadPart - before.QuadPart),
                 static_cast<unsigned long>(hr));
        return hr;
    }

    HRESULT STDMETHODCALLTYPE GetBuffer(UINT index, REFIID iid, void **surface) override {
        return inner_->GetBuffer(index, iid, surface);
    }
    HRESULT STDMETHODCALLTYPE SetFullscreenState(WINBOOL fullscreen, IDXGIOutput *target) override {
        log_line("fullscreen id=%llu requested=%d target=%p", static_cast<unsigned long long>(id_),
                 fullscreen, static_cast<void *>(target));
        return inner_->SetFullscreenState(fullscreen, unwrap_dxgi_object(target));
    }
    HRESULT STDMETHODCALLTYPE GetFullscreenState(WINBOOL *fullscreen, IDXGIOutput **target) override {
        const HRESULT hr = inner_->GetFullscreenState(fullscreen, target);
        if (SUCCEEDED(hr) && target && *target) wrap_output_result(IID_IDXGIOutput,
                                                                   reinterpret_cast<void **>(target));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetDesc(DXGI_SWAP_CHAIN_DESC *desc) override { return inner_->GetDesc(desc); }
    HRESULT STDMETHODCALLTYPE ResizeBuffers(UINT count, UINT width, UINT height, DXGI_FORMAT format,
                                              UINT flags) override {
        bool ownsResize = false;
        bool drainUnresolved = false;
        {
            ExclusiveSrwGuard guard(mutex_);
            ++activeResizeCalls_;
            ++resizeGeneration_;
            if (activeResizeCalls_ == 1) {
                resizeInProgress_ = true;
                ownsResize = true;
                const DrainResult drain = drain_copy_work("ResizeBuffers", 1000);
                if (drain == DrainResult::Unresolved) {
                    end_resize_locked();
                    drainUnresolved = true;
                } else {
                    release_gpu_state();
                }
            } else {
                resizeInProgress_ = true;
            }
        }
        if (drainUnresolved) {
            log_line("resize_buffers id=%llu result=refused_unresolved_gpu_work",
                     static_cast<unsigned long long>(id_));
            return DXGI_ERROR_WAS_STILL_DRAWING;
        }
        if (!ownsResize) {
            log_line("resize_buffers id=%llu result=forwarded_while_resize_in_progress",
                     static_cast<unsigned long long>(id_));
            const HRESULT hr = inner_->ResizeBuffers(count, width, height, format, flags);
            ExclusiveSrwGuard guard(mutex_);
            end_resize_locked();
            return hr;
        }
        log_line("resize_buffers_begin id=%llu count=%u width=%u height=%u format=%u flags=0x%08x",
                 static_cast<unsigned long long>(id_), count, width, height,
                 static_cast<unsigned>(format), flags);
        const HRESULT hr = inner_->ResizeBuffers(count, width, height, format, flags);
        {
            ExclusiveSrwGuard guard(mutex_);
            if (SUCCEEDED(hr) && !copyDisabled_) syntheticDisabled_ = false;
            if (FAILED(hr)) copyDisabled_ = true;
            end_resize_locked();
        }
        log_line("resize_buffers_end id=%llu hr=0x%08lx", static_cast<unsigned long long>(id_),
                 static_cast<unsigned long>(hr));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE ResizeTarget(const DXGI_MODE_DESC *mode) override {
        log_line("resize_target id=%llu", static_cast<unsigned long long>(id_));
        return inner_->ResizeTarget(mode);
    }
    HRESULT STDMETHODCALLTYPE GetContainingOutput(IDXGIOutput **output) override {
        const HRESULT hr = inner_->GetContainingOutput(output);
        if (SUCCEEDED(hr) && output && *output) wrap_output_result(IID_IDXGIOutput,
                                                                   reinterpret_cast<void **>(output));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetFrameStatistics(DXGI_FRAME_STATISTICS *stats) override {
        return inner_->GetFrameStatistics(stats);
    }
    HRESULT STDMETHODCALLTYPE GetLastPresentCount(UINT *count) override {
        return inner_->GetLastPresentCount(count);
    }
    HRESULT STDMETHODCALLTYPE GetDesc1(DXGI_SWAP_CHAIN_DESC1 *desc) override { return inner_->GetDesc1(desc); }
    HRESULT STDMETHODCALLTYPE GetFullscreenDesc(DXGI_SWAP_CHAIN_FULLSCREEN_DESC *desc) override {
        return inner_->GetFullscreenDesc(desc);
    }
    HRESULT STDMETHODCALLTYPE GetHwnd(HWND *window) override { return inner_->GetHwnd(window); }
    HRESULT STDMETHODCALLTYPE GetCoreWindow(REFIID iid, void **window) override {
        return inner_->GetCoreWindow(iid, window);
    }
    HRESULT STDMETHODCALLTYPE Present1(UINT syncInterval, UINT flags,
                                         const DXGI_PRESENT_PARAMETERS *parameters) override {
        if (!log_enabled() && !copy_enabled() && !synthetic_enabled())
            return inner_->Present1(syncInterval, flags, parameters);
        ActiveCallScope activeCall(activePresentCalls_);
        const bool concurrentCall = activeCall.previous() != 0;
        if (concurrentCall) {
            concurrentPresentCalls_.store(true, std::memory_order_release);
            ULONGLONG expected = id_;
            gSelectedSyntheticSwapChain.compare_exchange_strong(expected, 0,
                                                                 std::memory_order_acq_rel);
        }

        UINT index = UINT_MAX;
        DXGI_SWAP_CHAIN_DESC1 desc{};
        HRESULT descHr = DXGI_ERROR_INVALID_CALL;
        ULONGLONG resizeGeneration = 0;
        bool canAttemptAdapterWork = false;
        void *queueAddress = nullptr;
        void *deviceAddress = nullptr;
        const UINT dirtyCount = parameters ? parameters->DirtyRectsCount : 0;
        if (!concurrentCall) {
            ExclusiveSrwGuard guard(mutex_);
            resizeGeneration = resizeGeneration_;
            queueAddress = queue_;
            deviceAddress = device_;
            if (!resizeInProgress_ && !concurrentPresentCalls_.load(std::memory_order_acquire)) {
                index = current_index();
                descHr = inner_->GetDesc1(&desc);
                canAttemptAdapterWork = true;
                if (flags & DXGI_PRESENT_TEST) {
                    if (copy_enabled())
                        log_line("gpu_copy id=%llu call=Present1 result=skipped_test_present",
                                 static_cast<unsigned long long>(id_));
                } else {
                    try {
                        copy_current_buffer(index, descHr == S_OK ? &desc : nullptr, "Present1");
                    } catch (...) {
                        disable_adapter_work("copy_exception");
                        log_line("gpu_copy id=%llu result=disabled_exception",
                                 static_cast<unsigned long long>(id_));
                    }
                }
            } else if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
                disable_adapter_work("concurrent_present_calls");
            } else {
                log_line("adapter_work id=%llu result=skipped_resize_in_progress",
                         static_cast<unsigned long long>(id_));
            }
        }
        LARGE_INTEGER before{}, after{};
        QueryPerformanceCounter(&before);
        const HRESULT hr = inner_->Present1(syncInterval, flags, parameters);
        QueryPerformanceCounter(&after);
        if (canAttemptAdapterWork && hr == S_OK && flags == 0 &&
            (!parameters || (!parameters->DirtyRectsCount && !parameters->pScrollRect)))
        {
            try {
                ExclusiveSrwGuard guard(mutex_);
                if (resizeInProgress_ || resizeGeneration_ != resizeGeneration) {
                    log_line("synthetic_g id=%llu source=Present1 "
                             "result=skipped_swapchain_lifecycle_change",
                             static_cast<unsigned long long>(id_));
                } else if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
                    disable_adapter_work("concurrent_present_calls");
                } else {
                    present_diagnostic_g("Present1");
                }
            } catch (...) {
                ExclusiveSrwGuard guard(mutex_);
                disable_adapter_work("synthetic_exception");
                log_line("synthetic_g id=%llu result=disabled_exception",
                         static_cast<unsigned long long>(id_));
            }
        }
        const ULONGLONG sequence = presentCount_.fetch_add(1, std::memory_order_relaxed) + 1;
        log_line("present id=%llu kind=Present1 sequence=%llu index=%u width=%u height=%u format=%u "
                 "sync=%u flags=0x%08x dirty_rects=%u scroll_rect=%u queue=%p device=%p qpc=%llu "
                 "duration_ticks=%llu hr=0x%08lx",
                 static_cast<unsigned long long>(id_), static_cast<unsigned long long>(sequence),
                 index, desc.Width, desc.Height, static_cast<unsigned>(desc.Format), syncInterval, flags,
                 dirtyCount, parameters && parameters->pScrollRect ? 1u : 0u,
                 queueAddress, deviceAddress,
                 static_cast<unsigned long long>(before.QuadPart),
                 static_cast<unsigned long long>(after.QuadPart - before.QuadPart),
                 static_cast<unsigned long>(hr));
        return hr;
    }
    WINBOOL STDMETHODCALLTYPE IsTemporaryMonoSupported() override { return inner_->IsTemporaryMonoSupported(); }
    HRESULT STDMETHODCALLTYPE GetRestrictToOutput(IDXGIOutput **output) override {
        const HRESULT hr = inner_->GetRestrictToOutput(output);
        if (SUCCEEDED(hr) && output && *output) wrap_output_result(IID_IDXGIOutput,
                                                                   reinterpret_cast<void **>(output));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE SetBackgroundColor(const DXGI_RGBA *color) override {
        return inner_->SetBackgroundColor(color);
    }
    HRESULT STDMETHODCALLTYPE GetBackgroundColor(DXGI_RGBA *color) override {
        return inner_->GetBackgroundColor(color);
    }
    HRESULT STDMETHODCALLTYPE SetRotation(DXGI_MODE_ROTATION rotation) override {
        return inner_->SetRotation(rotation);
    }
    HRESULT STDMETHODCALLTYPE GetRotation(DXGI_MODE_ROTATION *rotation) override {
        return inner_->GetRotation(rotation);
    }
    HRESULT STDMETHODCALLTYPE SetSourceSize(UINT width, UINT height) override {
        return inner_->SetSourceSize(width, height);
    }
    HRESULT STDMETHODCALLTYPE GetSourceSize(UINT *width, UINT *height) override {
        return inner_->GetSourceSize(width, height);
    }
    HRESULT STDMETHODCALLTYPE SetMaximumFrameLatency(UINT latency) override {
        return inner_->SetMaximumFrameLatency(latency);
    }
    HRESULT STDMETHODCALLTYPE GetMaximumFrameLatency(UINT *latency) override {
        return inner_->GetMaximumFrameLatency(latency);
    }
    HANDLE STDMETHODCALLTYPE GetFrameLatencyWaitableObject() override {
        return inner_->GetFrameLatencyWaitableObject();
    }
    HRESULT STDMETHODCALLTYPE SetMatrixTransform(const DXGI_MATRIX_3X2_F *matrix) override {
        return inner_->SetMatrixTransform(matrix);
    }
    HRESULT STDMETHODCALLTYPE GetMatrixTransform(DXGI_MATRIX_3X2_F *matrix) override {
        return inner_->GetMatrixTransform(matrix);
    }
    UINT STDMETHODCALLTYPE GetCurrentBackBufferIndex() override {
        return inner_->GetCurrentBackBufferIndex();
    }
    HRESULT STDMETHODCALLTYPE CheckColorSpaceSupport(DXGI_COLOR_SPACE_TYPE colorSpace,
                                                       UINT *support) override {
        return inner_->CheckColorSpaceSupport(colorSpace, support);
    }
    HRESULT STDMETHODCALLTYPE SetColorSpace1(DXGI_COLOR_SPACE_TYPE colorSpace) override {
        return inner_->SetColorSpace1(colorSpace);
    }
    HRESULT STDMETHODCALLTYPE ResizeBuffers1(UINT count, UINT width, UINT height, DXGI_FORMAT format,
                                               UINT flags, const UINT *nodeMask,
                                               IUnknown *const *presentQueue) override {
        UINT queueCount = count;
        if (!queueCount) {
            DXGI_SWAP_CHAIN_DESC1 currentDesc{};
            if (SUCCEEDED(inner_->GetDesc1(&currentDesc))) queueCount = currentDesc.BufferCount;
        }
        ID3D12CommandQueue *nextQueue = nullptr;
        ID3D12Device *nextDevice = nullptr;
        bool compatibleQueueSet = presentQueue && queueCount > 0 && queueCount <= 16 && nodeMask;
        IUnknown *firstIdentity = nullptr;
        if (compatibleQueueSet) {
            for (UINT i = 0; i < queueCount; ++i) {
                if (!presentQueue[i] || nodeMask[i] != nodeMask[0]) {
                    compatibleQueueSet = false;
                    break;
                }
                IUnknown *identity = nullptr;
                const HRESULT identityHr = presentQueue[i]->QueryInterface(
                    IID_IUnknown, reinterpret_cast<void **>(&identity));
                if (FAILED(identityHr) || !identity) {
                    release_com(identity);
                    compatibleQueueSet = false;
                    break;
                }
                if (!firstIdentity) {
                    firstIdentity = identity;
                    identity = nullptr;
                } else if (identity != firstIdentity) {
                    compatibleQueueSet = false;
                }
                release_com(identity);
                if (!compatibleQueueSet) break;
            }
        }
        if (compatibleQueueSet && (nodeMask[0] == 0 || nodeMask[0] == 1)) {
            const HRESULT queueHr = presentQueue[0]->QueryInterface(
                IID_ID3D12CommandQueue, reinterpret_cast<void **>(&nextQueue));
            const D3D12_COMMAND_QUEUE_DESC nextQueueDesc =
                SUCCEEDED(queueHr) && nextQueue ? nextQueue->GetDesc() : D3D12_COMMAND_QUEUE_DESC{};
            const UINT creationNode = nodeMask[0] ? nodeMask[0] : 1;
            const UINT queueNode = nextQueueDesc.NodeMask ? nextQueueDesc.NodeMask : 1;
            if (SUCCEEDED(queueHr) && nextQueue &&
                nextQueueDesc.Type == D3D12_COMMAND_LIST_TYPE_DIRECT &&
                (nextQueueDesc.NodeMask == 0 || nextQueueDesc.NodeMask == 1) &&
                queueNode == creationNode) {
                const HRESULT deviceHr = nextQueue->GetDevice(
                    IID_ID3D12Device, reinterpret_cast<void **>(&nextDevice));
                compatibleQueueSet = SUCCEEDED(deviceHr) && nextDevice &&
                                     nextDevice->GetNodeCount() == 1;
                if (!compatibleQueueSet) release_com(nextDevice);
            } else {
                compatibleQueueSet = false;
            }
        } else {
            compatibleQueueSet = false;
        }
        release_com(firstIdentity);

        bool ownsResize = false;
        bool drainUnresolved = false;
        bool wasDisabled = true;
        {
            ExclusiveSrwGuard guard(mutex_);
            ++activeResizeCalls_;
            ++resizeGeneration_;
            if (activeResizeCalls_ == 1) {
                resizeInProgress_ = true;
                ownsResize = true;
                wasDisabled = copyDisabled_;
                const DrainResult drain = drain_copy_work("ResizeBuffers1", 1000);
                if (drain == DrainResult::Unresolved) {
                    end_resize_locked();
                    drainUnresolved = true;
                } else {
                    release_gpu_state();
                }
            } else {
                resizeInProgress_ = true;
            }
        }
        if (drainUnresolved) {
            release_com(nextDevice);
            release_com(nextQueue);
            log_line("resize_buffers1 id=%llu result=refused_unresolved_gpu_work",
                     static_cast<unsigned long long>(id_));
            return DXGI_ERROR_WAS_STILL_DRAWING;
        }
        if (!ownsResize) {
            release_com(nextDevice);
            release_com(nextQueue);
            log_line("resize_buffers1 id=%llu result=forwarded_while_resize_in_progress",
                     static_cast<unsigned long long>(id_));
            const HRESULT hr = inner_->ResizeBuffers1(count, width, height, format, flags, nodeMask,
                                                      presentQueue);
            ExclusiveSrwGuard guard(mutex_);
            end_resize_locked();
            return hr;
        }
        log_line("resize_buffers1_begin id=%llu count=%u width=%u height=%u format=%u flags=0x%08x "
                 "node_mask=%p queues=%p compatible_queue_set=%u",
                 static_cast<unsigned long long>(id_), count, width, height,
                 static_cast<unsigned>(format), flags, static_cast<const void *>(nodeMask),
                 static_cast<const void *>(presentQueue), compatibleQueueSet);
        const HRESULT hr = inner_->ResizeBuffers1(count, width, height, format, flags, nodeMask,
                                                  presentQueue);
        {
            ExclusiveSrwGuard guard(mutex_);
            if (SUCCEEDED(hr)) {
                release_com(device_);
                release_com(queue_);
                if (compatibleQueueSet) {
                    queue_ = nextQueue;
                    device_ = nextDevice;
                    nextQueue = nullptr;
                    nextDevice = nullptr;
                    d3d12_ = true;
                    copyDisabled_ = wasDisabled;
                } else {
                    d3d12_ = false;
                    copyDisabled_ = true;
                }
                if (!copyDisabled_) syntheticDisabled_ = false;
            }
            end_resize_locked();
        }
        release_com(nextDevice);
        release_com(nextQueue);
        log_line("resize_buffers1_end id=%llu hr=0x%08lx d3d12=%u adapter_work_disabled=%u "
                 "queue=%p device=%p", static_cast<unsigned long long>(id_),
                 static_cast<unsigned long>(hr), d3d12_, copyDisabled_,
                 static_cast<void *>(queue_), static_cast<void *>(device_));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE SetHDRMetaData(DXGI_HDR_METADATA_TYPE type, UINT size,
                                               void *metadata) override {
        return inner_->SetHDRMetaData(type, size, metadata);
    }
    HRESULT STDMETHODCALLTYPE GetFrameStatisticsMedia(DXGI_FRAME_STATISTICS_MEDIA *stats) override {
        return media_ ? media_->GetFrameStatisticsMedia(stats) : E_NOINTERFACE;
    }
    HRESULT STDMETHODCALLTYPE SetPresentDuration(UINT duration) override {
        return media_ ? media_->SetPresentDuration(duration) : E_NOINTERFACE;
    }
    HRESULT STDMETHODCALLTYPE CheckPresentDurationSupport(UINT desired, UINT *smaller,
                                                           UINT *larger) override {
        return media_ ? media_->CheckPresentDurationSupport(desired, smaller, larger) : E_NOINTERFACE;
    }

private:
    void end_resize_locked() noexcept {
        if (activeResizeCalls_) --activeResizeCalls_;
        resizeInProgress_ = activeResizeCalls_ != 0;
    }

    void disable_adapter_work(const char *reason) {
        copyDisabled_ = true;
        ULONGLONG expected = id_;
        if (gSelectedSyntheticSwapChain.compare_exchange_strong(expected, 0,
                                                                 std::memory_order_acq_rel))
            log_line("synthetic_g_select id=%llu result=released_on_disable reason=%s",
                     static_cast<unsigned long long>(id_), reason);
        log_line("adapter_work id=%llu result=disabled reason=%s source_present=preserved",
                 static_cast<unsigned long long>(id_), reason);
    }

    UINT current_index() {
        const UINT index = inner_->GetCurrentBackBufferIndex();
        return index;
    }

    FenceWaitResult wait_for_fence(UINT64 value, DWORD timeoutMs) {
        if (!fence_ || !value) return FenceWaitResult::Complete;
        if (test_failpoint("fence_event")) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu result=injected_event_error",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value));
            return FenceWaitResult::Error;
        }
        if (test_failpoint("fence_timeout")) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu timeout_ms=%lu result=injected_timeout",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(timeoutMs));
            return FenceWaitResult::Timeout;
        }
        const UINT64 completed = fence_->GetCompletedValue();
        if (completed == UINT64_MAX) {
            const HRESULT removed = device_ ? device_->GetDeviceRemovedReason() : E_FAIL;
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu completed=UINT64_MAX device_removed_reason=0x%08lx "
                     "result=device_removed",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(removed));
            return FenceWaitResult::DeviceRemoved;
        }
        if (completed >= value) return FenceWaitResult::Complete;
        if (!fenceEvent_) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu completed=%llu result=event_unavailable",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long long>(completed));
            return FenceWaitResult::Error;
        }
        const HRESULT eventHr = fence_->SetEventOnCompletion(value, fenceEvent_);
        if (FAILED(eventHr)) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu operation=SetEventOnCompletion hr=0x%08lx "
                     "result=error",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(eventHr));
            return FenceWaitResult::Error;
        }
        const DWORD wait = WaitForSingleObject(fenceEvent_, timeoutMs);
        if (wait == WAIT_TIMEOUT) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu timeout_ms=%lu result=timeout",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(timeoutMs));
            return FenceWaitResult::Timeout;
        }
        if (wait != WAIT_OBJECT_0) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu wait_result=%lu result=error",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(wait));
            return FenceWaitResult::Error;
        }
        const UINT64 afterWait = fence_->GetCompletedValue();
        if (afterWait == UINT64_MAX) {
            const HRESULT removed = device_ ? device_->GetDeviceRemovedReason() : E_FAIL;
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu completed_after_wait=UINT64_MAX "
                     "device_removed_reason=0x%08lx result=device_removed",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long>(removed));
            return FenceWaitResult::DeviceRemoved;
        }
        if (afterWait < value) {
            copyDisabled_ = true;
            log_line("gpu_fence id=%llu requested=%llu completed_after_wait=%llu result=error",
                     static_cast<unsigned long long>(id_), static_cast<unsigned long long>(value),
                     static_cast<unsigned long long>(afterWait));
            return FenceWaitResult::Error;
        }
        return FenceWaitResult::Complete;
    }

    DrainResult drain_copy_work(const char *reason, DWORD timeoutMs) {
        if (!fence_ || !nextFenceValue_) return DrainResult::Complete;
        const UINT64 flushValue = ++nextFenceValue_;
        const HRESULT signalHr = queue_ ? queue_->Signal(fence_, flushValue) : E_FAIL;
        if (FAILED(signalHr)) {
            const HRESULT removed = device_ ? device_->GetDeviceRemovedReason() : E_FAIL;
            log_line("gpu_drain id=%llu reason=%s signal_hr=0x%08lx wait_hr=not_attempted "
                     "device_removed_reason=0x%08lx result=%s",
                     static_cast<unsigned long long>(id_), reason,
                     static_cast<unsigned long>(signalHr), static_cast<unsigned long>(removed),
                     FAILED(removed) ? "device_removed" : "unresolved");
            copyDisabled_ = true;
            return FAILED(removed) ? DrainResult::DeviceRemoved : DrainResult::Unresolved;
        }
        const FenceWaitResult wait = wait_for_fence(flushValue, timeoutMs);
        if (wait == FenceWaitResult::Complete) {
            log_line("gpu_drain id=%llu reason=%s fence=%llu result=complete",
                     static_cast<unsigned long long>(id_), reason,
                     static_cast<unsigned long long>(flushValue));
            return DrainResult::Complete;
        }
        log_line("gpu_drain id=%llu reason=%s signal_hr=0x%08lx wait_hr=0x%08lx "
                 "result=%s",
                 static_cast<unsigned long long>(id_), reason, static_cast<unsigned long>(signalHr),
                 static_cast<unsigned long>(wait),
                 wait == FenceWaitResult::DeviceRemoved ? "device_removed" : "unresolved");
        copyDisabled_ = true;
        return wait == FenceWaitResult::DeviceRemoved ? DrainResult::DeviceRemoved
                                                      : DrainResult::Unresolved;
    }

    void release_gpu_state() {
        for (CopySlot &slot : slots_) {
            release_com(slot.source);
            release_com(slot.destination);
            release_com(slot.allocator);
            release_com(slot.rtvHeap);
        }
        slots_.clear();
        release_com(commandList_);
        release_com(fence_);
        if (fenceEvent_) {
            CloseHandle(fenceEvent_);
            fenceEvent_ = nullptr;
        }
        nextFenceValue_ = 0;
    }

    bool ensure_gpu_objects(CopySlot &slot) {
        if (!device_ || !queue_ || !d3d12_ || copyDisabled_) return false;
        if (!fence_) {
            HRESULT hr = test_failpoint("fence_create")
                             ? E_FAIL
                             : device_->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_ID3D12Fence,
                                                    reinterpret_cast<void **>(&fence_));
            if (FAILED(hr)) {
                copyDisabled_ = true;
                log_line("gpu_init id=%llu operation=CreateFence hr=0x%08lx",
                         static_cast<unsigned long long>(id_), static_cast<unsigned long>(hr));
                return false;
            }
            fenceEvent_ = test_failpoint("event_create") ? nullptr : CreateEventW(nullptr, FALSE, FALSE,
                                                                                    nullptr);
            if (!fenceEvent_) {
                copyDisabled_ = true;
                log_line("gpu_init id=%llu operation=CreateEvent error=%lu",
                         static_cast<unsigned long long>(id_),
                         static_cast<unsigned long>(GetLastError()));
                return false;
            }
        }
        if (!slot.allocator) {
            HRESULT hr = test_failpoint("allocator_create")
                             ? E_FAIL
                             : device_->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                               IID_ID3D12CommandAllocator,
                                                               reinterpret_cast<void **>(&slot.allocator));
            if (FAILED(hr)) {
                copyDisabled_ = true;
                log_line("gpu_init id=%llu operation=CreateCommandAllocator hr=0x%08lx",
                         static_cast<unsigned long long>(id_), static_cast<unsigned long>(hr));
                return false;
            }
        }
        if (!commandList_) {
            HRESULT hr = test_failpoint("commandlist_create")
                             ? E_FAIL
                             : device_->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                          slot.allocator, nullptr,
                                                          IID_ID3D12GraphicsCommandList,
                                                          reinterpret_cast<void **>(&commandList_));
            if (SUCCEEDED(hr)) hr = commandList_->Close();
            if (FAILED(hr)) {
                copyDisabled_ = true;
                log_line("gpu_init id=%llu operation=CreateCommandList hr=0x%08lx",
                         static_cast<unsigned long long>(id_), static_cast<unsigned long>(hr));
                return false;
            }
        }
        return true;
    }

    void present_diagnostic_g(const char *sourceCall) {
        // This path is only enabled in the explicitly unsafe controlled-test build.
        if (!synthetic_enabled() || syntheticDisabled_ || !d3d12_ || copyDisabled_) return;
        if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
            disable_adapter_work("concurrent_present_calls");
            return;
        }
        DXGI_SWAP_CHAIN_DESC1 desc{};
        HRESULT hr = inner_->GetDesc1(&desc);
        const bool protectedChain = (desc.Flags & DXGI_SWAP_CHAIN_FLAG_HW_PROTECTED) != 0;
        if (FAILED(hr) || protectedChain ||
            (desc.SwapEffect != DXGI_SWAP_EFFECT_FLIP_DISCARD &&
             desc.SwapEffect != DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL) ||
            !desc.BufferCount || desc.BufferCount > 16) {
            log_line("synthetic_g id=%llu source=%s result=skip_ineligible_desc hr=0x%08lx "
                     "protected=%u effect=%u buffer_count=%u",
                     static_cast<unsigned long long>(id_), sourceCall, static_cast<unsigned long>(hr),
                     protectedChain, static_cast<unsigned>(desc.SwapEffect), desc.BufferCount);
            syntheticDisabled_ = true;
            return;
        }
        if (slots_.size() != desc.BufferCount) slots_.resize(desc.BufferCount);
        const UINT index = current_index();
        if (index >= desc.BufferCount) {
            disable_adapter_work("synthetic_invalid_backbuffer_index");
            return;
        }
        CopySlot &slot = slots_[index];
        if (slot.fenceValue && wait_for_fence(slot.fenceValue, 1) != FenceWaitResult::Complete) {
            disable_adapter_work("synthetic_slot_wait_failed");
            log_line("synthetic_g id=%llu source=%s index=%u result=disabled_slot_wait_failed fence=%llu",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long long>(slot.fenceValue));
            return;
        }
        release_com(slot.source);
        ID3D12Resource *target = nullptr;
        hr = test_failpoint("getbuffer")
                 ? E_FAIL
                 : inner_->GetBuffer(index, IID_ID3D12Resource, reinterpret_cast<void **>(&target));
        if (FAILED(hr) || !target) {
            disable_adapter_work("synthetic_getbuffer_failed");
            log_line("synthetic_g id=%llu source=%s index=%u operation=GetBuffer hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        const D3D12_RESOURCE_DESC targetDesc = target->GetDesc();
        if (!(targetDesc.Flags & D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET)) {
            target->Release();
            syntheticDisabled_ = true;
            log_line("synthetic_g id=%llu source=%s index=%u result=skip_no_rtv_capability",
                     static_cast<unsigned long long>(id_), sourceCall, index);
            return;
        }
        if (!ensure_gpu_objects(slot)) {
            target->Release();
            disable_adapter_work("synthetic_gpu_objects_initialization_failed");
            return;
        }
        if (!slot.rtvHeap) {
            D3D12_DESCRIPTOR_HEAP_DESC heapDesc{};
            heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
            heapDesc.NumDescriptors = 1;
            hr = test_failpoint("descriptor_heap")
                     ? E_FAIL
                     : device_->CreateDescriptorHeap(&heapDesc, IID_ID3D12DescriptorHeap,
                                                      reinterpret_cast<void **>(&slot.rtvHeap));
            if (FAILED(hr)) {
                target->Release();
                disable_adapter_work("synthetic_descriptor_heap_failed");
                log_line("synthetic_g id=%llu source=%s operation=CreateDescriptorHeap hr=0x%08lx",
                         static_cast<unsigned long long>(id_), sourceCall,
                         static_cast<unsigned long>(hr));
                return;
            }
        }

        ULONGLONG selectedId = gSelectedSyntheticSwapChain.load(std::memory_order_acquire);
        if (selectedId && selectedId != id_) {
            target->Release();
            return;
        }
        if (!selectedId) {
            ULONGLONG expected = 0;
            if (gSelectedSyntheticSwapChain.compare_exchange_strong(expected, id_,
                                                                     std::memory_order_acq_rel)) {
                selectedId = id_;
                log_line("synthetic_g_select id=%llu result=selected_for_process",
                         static_cast<unsigned long long>(id_));
            } else {
                selectedId = expected;
            }
            if (selectedId != id_) {
                target->Release();
                return;
            }
        }
        const D3D12_CPU_DESCRIPTOR_HANDLE rtv = slot.rtvHeap->GetCPUDescriptorHandleForHeapStart();
        device_->CreateRenderTargetView(target, nullptr, rtv);
        hr = test_failpoint("allocator_reset") ? E_FAIL : slot.allocator->Reset();
        if (SUCCEEDED(hr))
            hr = test_failpoint("commandlist_reset") ? E_FAIL : commandList_->Reset(slot.allocator, nullptr);
        if (FAILED(hr)) {
            target->Release();
            disable_adapter_work("synthetic_reset_failed");
            log_line("synthetic_g id=%llu source=%s index=%u operation=Reset hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        D3D12_RESOURCE_BARRIER toTarget{};
        toTarget.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        toTarget.Transition.pResource = target;
        toTarget.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        toTarget.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        toTarget.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        commandList_->ResourceBarrier(1, &toTarget);
        commandList_->OMSetRenderTargets(1, &rtv, FALSE, nullptr);
        const float magenta[] = {1.0f, 0.0f, 1.0f, 1.0f};
        commandList_->ClearRenderTargetView(rtv, magenta, 0, nullptr);
        D3D12_RESOURCE_BARRIER restore = toTarget;
        restore.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        restore.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        commandList_->ResourceBarrier(1, &restore);
        hr = test_failpoint("commandlist_close") ? E_FAIL : commandList_->Close();
        if (FAILED(hr)) {
            target->Release();
            disable_adapter_work("synthetic_close_failed");
            log_line("synthetic_g id=%llu source=%s index=%u operation=Close hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        ID3D12CommandList *lists[] = {commandList_};
        queue_->ExecuteCommandLists(1, lists);
        const UINT64 fenceValue = ++nextFenceValue_;
        hr = test_failpoint("queue_signal") ? E_FAIL : queue_->Signal(fence_, fenceValue);
        if (FAILED(hr)) {
            slot.source = target;
            slot.fenceValue = 0;
            disable_adapter_work("synthetic_queue_signal_failed");
            log_line("synthetic_g id=%llu source=%s index=%u operation=Signal result=disabled "
                     "hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        slot.source = target;
        slot.fenceValue = fenceValue;
        const UINT beforeIndex = current_index();
        LARGE_INTEGER before{}, after{};
        QueryPerformanceCounter(&before);
        const UINT syncInterval = synthetic_sync_interval();
        const HRESULT presentHr = test_failpoint("synthetic_present")
                                      ? E_FAIL
                                      : inner_->Present(syncInterval, DXGI_PRESENT_DO_NOT_SEQUENCE);
        QueryPerformanceCounter(&after);
        const UINT afterIndex = current_index();
        if (FAILED(presentHr)) disable_adapter_work("synthetic_present_failed");
        log_line("synthetic_g id=%llu generated=%llu source=%s buffer=%u before_index=%u "
                 "after_index=%u color=magenta state=PRESENT->RENDER_TARGET->PRESENT sync=%u queue=%p "
                 "qpc=%llu present_duration_ticks=%llu present_hr=0x%08lx result=%s",
                 static_cast<unsigned long long>(id_),
                 static_cast<unsigned long long>(++generatedCount_), sourceCall, index,
                 beforeIndex, afterIndex, syncInterval, static_cast<void *>(queue_),
                 static_cast<unsigned long long>(before.QuadPart),
                 static_cast<unsigned long long>(after.QuadPart - before.QuadPart),
                 static_cast<unsigned long>(presentHr),
                 SUCCEEDED(presentHr) ? "presented_request" : "failed");
    }

    void copy_current_buffer(UINT index, const DXGI_SWAP_CHAIN_DESC1 *swapDesc, const char *sourceCall) {
        if (!copy_enabled() || copyDisabled_ || !d3d12_ || !swapDesc) return;
        if (concurrentPresentCalls_.load(std::memory_order_acquire)) {
            disable_adapter_work("concurrent_present_calls");
            return;
        }
        if (test_fail_gpu_work()) {
            disable_adapter_work("injected_gpu_work_failure");
            log_line("gpu_copy id=%llu call=%s result=injected_failure adapter_work=disabled "
                     "source_present=forwarded_unchanged",
                     static_cast<unsigned long long>(id_), sourceCall);
            return;
        }
        if (!swapDesc->BufferCount || swapDesc->BufferCount > 16 || index >= swapDesc->BufferCount) {
            disable_adapter_work("invalid_copy_buffer_index");
            log_line("gpu_copy id=%llu call=%s result=skip_invalid_buffer_count count=%u index=%u",
                     static_cast<unsigned long long>(id_), sourceCall, swapDesc->BufferCount, index);
            return;
        }
        if (slots_.size() != swapDesc->BufferCount) slots_.resize(swapDesc->BufferCount);
        CopySlot &slot = slots_[index];
        if (slot.fenceValue && wait_for_fence(slot.fenceValue, 1) != FenceWaitResult::Complete) {
            disable_adapter_work("copy_slot_wait_failed");
            log_line("gpu_copy id=%llu call=%s index=%u result=disabled_slot_wait_failed fence=%llu",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long long>(slot.fenceValue));
            return;
        }
        release_com(slot.source);
        ID3D12Resource *source = nullptr;
        HRESULT hr = test_failpoint("getbuffer")
                         ? E_FAIL
                         : inner_->GetBuffer(index, IID_ID3D12Resource,
                                             reinterpret_cast<void **>(&source));
        if (FAILED(hr) || !source) {
            disable_adapter_work("copy_getbuffer_failed");
            log_line("gpu_copy id=%llu call=%s index=%u operation=GetBuffer hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        const D3D12_RESOURCE_DESC sourceDesc = source->GetDesc();
        const bool descMatches = slot.hasDescription &&
            slot.description.Width == sourceDesc.Width && slot.description.Height == sourceDesc.Height &&
            slot.description.Format == sourceDesc.Format && slot.description.MipLevels == sourceDesc.MipLevels &&
            slot.description.SampleDesc.Count == sourceDesc.SampleDesc.Count;
        if (!slot.destination || !descMatches) {
            release_com(slot.destination);
            D3D12_RESOURCE_DESC copyDesc = sourceDesc;
            copyDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
            D3D12_HEAP_PROPERTIES heap{};
            heap.Type = D3D12_HEAP_TYPE_DEFAULT;
            heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
            heap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
            heap.CreationNodeMask = 1;
            heap.VisibleNodeMask = 1;
            hr = test_failpoint("resource_create")
                     ? E_FAIL
                     : device_->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &copyDesc,
                                                        D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                                        IID_ID3D12Resource,
                                                        reinterpret_cast<void **>(&slot.destination));
            if (FAILED(hr)) {
                source->Release();
                disable_adapter_work("copy_resource_allocation_failed");
                log_line("gpu_copy id=%llu call=%s index=%u operation=CreateCommittedResource "
                         "width=%llu height=%u format=%u hr=0x%08lx",
                         static_cast<unsigned long long>(id_), sourceCall, index,
                         static_cast<unsigned long long>(sourceDesc.Width), sourceDesc.Height,
                         static_cast<unsigned>(sourceDesc.Format), static_cast<unsigned long>(hr));
                return;
            }
            slot.description = sourceDesc;
            slot.hasDescription = true;
        }
        if (!ensure_gpu_objects(slot)) {
            source->Release();
            disable_adapter_work("copy_gpu_objects_initialization_failed");
            return;
        }
        hr = test_failpoint("allocator_reset") ? E_FAIL : slot.allocator->Reset();
        if (SUCCEEDED(hr))
            hr = test_failpoint("commandlist_reset") ? E_FAIL : commandList_->Reset(slot.allocator, nullptr);
        if (FAILED(hr)) {
            source->Release();
            disable_adapter_work("copy_reset_failed");
            log_line("gpu_copy id=%llu call=%s index=%u operation=Reset hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        D3D12_RESOURCE_BARRIER toCopy{};
        toCopy.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        toCopy.Transition.pResource = source;
        toCopy.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        toCopy.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        toCopy.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
        commandList_->ResourceBarrier(1, &toCopy);
        commandList_->CopyResource(slot.destination, source);
        D3D12_RESOURCE_BARRIER restore = toCopy;
        restore.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_SOURCE;
        restore.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        commandList_->ResourceBarrier(1, &restore);
        hr = test_failpoint("commandlist_close") ? E_FAIL : commandList_->Close();
        if (FAILED(hr)) {
            source->Release();
            disable_adapter_work("copy_close_failed");
            log_line("gpu_copy id=%llu call=%s index=%u operation=Close hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        ID3D12CommandList *lists[] = {commandList_};
        queue_->ExecuteCommandLists(1, lists);
        const UINT64 fenceValue = ++nextFenceValue_;
        hr = test_failpoint("queue_signal") ? E_FAIL : queue_->Signal(fence_, fenceValue);
        if (FAILED(hr)) {
            slot.source = source;
            slot.fenceValue = 0;
            disable_adapter_work("copy_queue_signal_failed");
            log_line("gpu_copy id=%llu call=%s index=%u operation=Signal result=disabled "
                     "hr=0x%08lx",
                     static_cast<unsigned long long>(id_), sourceCall, index,
                     static_cast<unsigned long>(hr));
            return;
        }
        slot.source = source;
        slot.fenceValue = fenceValue;
        log_line("gpu_copy id=%llu call=%s index=%u source=%p destination=%p width=%llu height=%u "
                 "format=%u source_state=PRESENT->COPY_SOURCE->PRESENT fence=%llu result=queued",
                 static_cast<unsigned long long>(id_), sourceCall, index, static_cast<void *>(source),
                 static_cast<void *>(slot.destination), static_cast<unsigned long long>(sourceDesc.Width),
                 sourceDesc.Height, static_cast<unsigned>(sourceDesc.Format),
                 static_cast<unsigned long long>(fenceValue));
    }

    IDXGISwapChain4 *inner_ = nullptr;
    IDXGISwapChainMedia *media_ = nullptr;
    ID3D12CommandQueue *queue_ = nullptr;
    ID3D12Device *device_ = nullptr;
    ID3D12GraphicsCommandList *commandList_ = nullptr;
    ID3D12Fence *fence_ = nullptr;
    HANDLE fenceEvent_ = nullptr;
    std::vector<CopySlot> slots_;
    SRWLOCK mutex_ = SRWLOCK_INIT;
    std::atomic<UINT> activePresentCalls_{0};
    UINT activeResizeCalls_ = 0;
    std::atomic<bool> concurrentPresentCalls_{false};
    ULONGLONG id_ = 0;
    std::atomic<ULONGLONG> presentCount_{0};
    UINT64 generatedCount_ = 0;
    UINT64 nextFenceValue_ = 0;
    ULONGLONG resizeGeneration_ = 0;
    bool d3d12_ = false;
    bool copyDisabled_ = false;
    bool syntheticDisabled_ = false;
    bool resizeInProgress_ = false;
};

class AdapterProxy final : public IDXGIAdapter4, public ProxyBase {
public:
    AdapterProxy(IUnknown *identity, IDXGIAdapter4 *inner)
        : ProxyBase(identity, ProxyKind::Adapter), inner_(inner) {}
    ~AdapterProxy() override { release_com(inner_); }
    void *public_interface_address() override { return static_cast<IDXGIAdapter4 *>(this); }

    IDXGIAdapter4 *inner() const { return inner_; }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
            IsEqualIID(iid, IID_IDXGIAdapter) || IsEqualIID(iid, IID_IDXGIAdapter1) ||
            IsEqualIID(iid, IID_IDXGIAdapter2) || IsEqualIID(iid, IID_IDXGIAdapter3) ||
            IsEqualIID(iid, IID_IDXGIAdapter4)) {
            void *check = nullptr;
            const HRESULT hr = inner_->QueryInterface(iid, &check);
            if (FAILED(hr)) {
                log_qi_result("adapter", iid, hr, false);
                return hr;
            }
            static_cast<IUnknown *>(check)->Release();
            *object = static_cast<IDXGIAdapter4 *>(this);
            AddRef();
            log_qi_result("adapter", iid, S_OK, false);
            return S_OK;
        }
        const HRESULT hr = inner_->QueryInterface(iid, object);
        log_qi_result("adapter", iid, hr, SUCCEEDED(hr));
        return hr;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return add_ref(); }
    ULONG STDMETHODCALLTYPE Release() override { return release_ref(); }

    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID guid, UINT size, const void *data) override {
        return inner_->SetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID guid, const IUnknown *object) override {
        return inner_->SetPrivateDataInterface(guid, object);
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID guid, UINT *size, void *data) override {
        return inner_->GetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID iid, void **parent) override {
        if (parent) *parent = nullptr;
        const HRESULT hr = inner_->GetParent(iid, parent);
        if (SUCCEEDED(hr) && parent && *parent && is_factory_iid(iid))
            return wrap_factory_result(iid, parent);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE EnumOutputs(UINT index, IDXGIOutput **output) override {
        if (output) *output = nullptr;
        const HRESULT hr = inner_->EnumOutputs(index, output);
        if (SUCCEEDED(hr) && output && *output)
            wrap_output_result(IID_IDXGIOutput, reinterpret_cast<void **>(output));
        log_line("adapter_enum_outputs this=%p index=%u hr=0x%08lx wrapped=%u",
                 static_cast<void *>(this), index, static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && output && *output);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetDesc(DXGI_ADAPTER_DESC *desc) override { return inner_->GetDesc(desc); }
    HRESULT STDMETHODCALLTYPE CheckInterfaceSupport(REFGUID guid, LARGE_INTEGER *version) override {
        return inner_->CheckInterfaceSupport(guid, version);
    }
    HRESULT STDMETHODCALLTYPE GetDesc1(DXGI_ADAPTER_DESC1 *desc) override { return inner_->GetDesc1(desc); }
    HRESULT STDMETHODCALLTYPE GetDesc2(DXGI_ADAPTER_DESC2 *desc) override { return inner_->GetDesc2(desc); }
    HRESULT STDMETHODCALLTYPE RegisterHardwareContentProtectionTeardownStatusEvent(
        HANDLE event, DWORD *cookie) override {
        return inner_->RegisterHardwareContentProtectionTeardownStatusEvent(event, cookie);
    }
    void STDMETHODCALLTYPE UnregisterHardwareContentProtectionTeardownStatus(DWORD cookie) override {
        inner_->UnregisterHardwareContentProtectionTeardownStatus(cookie);
    }
    HRESULT STDMETHODCALLTYPE QueryVideoMemoryInfo(UINT node, DXGI_MEMORY_SEGMENT_GROUP group,
                                                     DXGI_QUERY_VIDEO_MEMORY_INFO *info) override {
        return inner_->QueryVideoMemoryInfo(node, group, info);
    }
    HRESULT STDMETHODCALLTYPE SetVideoMemoryReservation(UINT node, DXGI_MEMORY_SEGMENT_GROUP group,
                                                         UINT64 reservation) override {
        return inner_->SetVideoMemoryReservation(node, group, reservation);
    }
    HRESULT STDMETHODCALLTYPE RegisterVideoMemoryBudgetChangeNotificationEvent(
        HANDLE event, DWORD *cookie) override {
        return inner_->RegisterVideoMemoryBudgetChangeNotificationEvent(event, cookie);
    }
    void STDMETHODCALLTYPE UnregisterVideoMemoryBudgetChangeNotification(DWORD cookie) override {
        inner_->UnregisterVideoMemoryBudgetChangeNotification(cookie);
    }
    HRESULT STDMETHODCALLTYPE GetDesc3(DXGI_ADAPTER_DESC3 *desc) override { return inner_->GetDesc3(desc); }

private:
    IDXGIAdapter4 *inner_ = nullptr;
};

class OutputProxy final : public IDXGIOutput6, public ProxyBase {
public:
    OutputProxy(IUnknown *identity, IDXGIOutput6 *inner)
        : ProxyBase(identity, ProxyKind::Output), inner_(inner) {}
    ~OutputProxy() override { release_com(inner_); }
    void *public_interface_address() override { return static_cast<IDXGIOutput6 *>(this); }

    IDXGIOutput6 *inner() const { return inner_; }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
            IsEqualIID(iid, IID_IDXGIOutput) || IsEqualIID(iid, IID_IDXGIOutput1) ||
            IsEqualIID(iid, IID_IDXGIOutput2) || IsEqualIID(iid, IID_IDXGIOutput3) ||
            IsEqualIID(iid, IID_IDXGIOutput4) || IsEqualIID(iid, IID_IDXGIOutput5) ||
            IsEqualIID(iid, IID_IDXGIOutput6)) {
            void *check = nullptr;
            const HRESULT hr = inner_->QueryInterface(iid, &check);
            if (FAILED(hr)) {
                log_qi_result("output", iid, hr, false);
                return hr;
            }
            static_cast<IUnknown *>(check)->Release();
            *object = static_cast<IDXGIOutput6 *>(this);
            AddRef();
            log_qi_result("output", iid, S_OK, false);
            return S_OK;
        }
        const HRESULT hr = inner_->QueryInterface(iid, object);
        log_qi_result("output", iid, hr, SUCCEEDED(hr));
        return hr;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return add_ref(); }
    ULONG STDMETHODCALLTYPE Release() override { return release_ref(); }

    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID guid, UINT size, const void *data) override {
        return inner_->SetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID guid, const IUnknown *object) override {
        return inner_->SetPrivateDataInterface(guid, object);
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID guid, UINT *size, void *data) override {
        return inner_->GetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID iid, void **parent) override {
        if (parent) *parent = nullptr;
        const HRESULT hr = inner_->GetParent(iid, parent);
        if (SUCCEEDED(hr) && parent && *parent)
            return wrap_adapter_result(iid, parent);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetDesc(DXGI_OUTPUT_DESC *desc) override { return inner_->GetDesc(desc); }
    HRESULT STDMETHODCALLTYPE GetDisplayModeList(DXGI_FORMAT format, UINT flags, UINT *count,
                                                  DXGI_MODE_DESC *modes) override {
        return inner_->GetDisplayModeList(format, flags, count, modes);
    }
    HRESULT STDMETHODCALLTYPE FindClosestMatchingMode(const DXGI_MODE_DESC *mode,
                                                        DXGI_MODE_DESC *closest,
                                                        IUnknown *device) override {
        return inner_->FindClosestMatchingMode(mode, closest, device);
    }
    HRESULT STDMETHODCALLTYPE WaitForVBlank() override { return inner_->WaitForVBlank(); }
    HRESULT STDMETHODCALLTYPE TakeOwnership(IUnknown *device, WINBOOL exclusive) override {
        return inner_->TakeOwnership(device, exclusive);
    }
    void STDMETHODCALLTYPE ReleaseOwnership() override { inner_->ReleaseOwnership(); }
    HRESULT STDMETHODCALLTYPE GetGammaControlCapabilities(DXGI_GAMMA_CONTROL_CAPABILITIES *caps) override {
        return inner_->GetGammaControlCapabilities(caps);
    }
    HRESULT STDMETHODCALLTYPE SetGammaControl(const DXGI_GAMMA_CONTROL *control) override {
        return inner_->SetGammaControl(control);
    }
    HRESULT STDMETHODCALLTYPE GetGammaControl(DXGI_GAMMA_CONTROL *control) override {
        return inner_->GetGammaControl(control);
    }
    HRESULT STDMETHODCALLTYPE SetDisplaySurface(IDXGISurface *surface) override {
        return inner_->SetDisplaySurface(surface);
    }
    HRESULT STDMETHODCALLTYPE GetDisplaySurfaceData(IDXGISurface *surface) override {
        return inner_->GetDisplaySurfaceData(surface);
    }
    HRESULT STDMETHODCALLTYPE GetFrameStatistics(DXGI_FRAME_STATISTICS *stats) override {
        return inner_->GetFrameStatistics(stats);
    }
    HRESULT STDMETHODCALLTYPE GetDisplayModeList1(DXGI_FORMAT format, UINT flags, UINT *count,
                                                    DXGI_MODE_DESC1 *modes) override {
        return inner_->GetDisplayModeList1(format, flags, count, modes);
    }
    HRESULT STDMETHODCALLTYPE FindClosestMatchingMode1(const DXGI_MODE_DESC1 *mode,
                                                        DXGI_MODE_DESC1 *closest,
                                                        IUnknown *device) override {
        return inner_->FindClosestMatchingMode1(mode, closest, device);
    }
    HRESULT STDMETHODCALLTYPE GetDisplaySurfaceData1(IDXGIResource *destination) override {
        return inner_->GetDisplaySurfaceData1(destination);
    }
    HRESULT STDMETHODCALLTYPE DuplicateOutput(IUnknown *device,
                                                IDXGIOutputDuplication **duplication) override {
        const HRESULT hr = inner_->DuplicateOutput(device, duplication);
        log_line("output_duplicate_output this=%p hr=0x%08lx disposition=raw_non_swapchain",
                 static_cast<void *>(this), static_cast<unsigned long>(hr));
        return hr;
    }
    WINBOOL STDMETHODCALLTYPE SupportsOverlays() override { return inner_->SupportsOverlays(); }
    HRESULT STDMETHODCALLTYPE CheckOverlaySupport(DXGI_FORMAT format, IUnknown *device,
                                                   UINT *flags) override {
        return inner_->CheckOverlaySupport(format, device, flags);
    }
    HRESULT STDMETHODCALLTYPE CheckOverlayColorSpaceSupport(DXGI_FORMAT format,
                                                             DXGI_COLOR_SPACE_TYPE colorSpace,
                                                             IUnknown *device, UINT *flags) override {
        return inner_->CheckOverlayColorSpaceSupport(format, colorSpace, device, flags);
    }
    HRESULT STDMETHODCALLTYPE DuplicateOutput1(IUnknown *device, UINT flags, UINT formatCount,
                                                const DXGI_FORMAT *formats,
                                                IDXGIOutputDuplication **duplication) override {
        const HRESULT hr = inner_->DuplicateOutput1(device, flags, formatCount, formats, duplication);
        log_line("output_duplicate_output1 this=%p hr=0x%08lx disposition=raw_non_swapchain",
                 static_cast<void *>(this), static_cast<unsigned long>(hr));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetDesc1(DXGI_OUTPUT_DESC1 *desc) override { return inner_->GetDesc1(desc); }
    HRESULT STDMETHODCALLTYPE CheckHardwareCompositionSupport(UINT *flags) override {
        return inner_->CheckHardwareCompositionSupport(flags);
    }

private:
    IDXGIOutput6 *inner_ = nullptr;
};

template <typename T>
T *unwrap_dxgi_object(T *object) {
    if (!object) return nullptr;
    auto *base = registry_find_public_interface(static_cast<void *>(object));
    if (!base || base->kind() != ProxyKind::Output) {
        log_line("unwrap_dxgi_output object=%p disposition=provider_passthrough",
                 static_cast<void *>(object));
        return object;
    }
    log_line("unwrap_dxgi_output object=%p proxy=%p disposition=provider_object",
             static_cast<void *>(object), static_cast<void *>(base));
    return static_cast<T *>(static_cast<OutputProxy *>(base)->inner());
}

HRESULT wrap_adapter_result(REFIID iid, void **result) {
    if (!result || !*result) return S_OK;
    IUnknown *original = static_cast<IUnknown *>(*result);
    if (!is_adapter_iid(iid)) {
        void *probe = nullptr;
        const HRESULT probeHr = original->QueryInterface(iid, &probe);
        if (probe) static_cast<IUnknown *>(probe)->Release();
        log_qi_result("adapter_enumeration", iid, probeHr, SUCCEEDED(probeHr));
        return S_OK;
    }
    IDXGIAdapter4 *inner = nullptr;
    HRESULT hr = original->QueryInterface(IID_IDXGIAdapter4, reinterpret_cast<void **>(&inner));
    if (FAILED(hr) || !inner) {
        log_line("adapter_wrap result=pass_through reason=IDXGIAdapter4_unavailable iid_data1=%08lx "
                 "hr=0x%08lx", static_cast<unsigned long>(iid.Data1), static_cast<unsigned long>(hr));
        return S_OK;
    }
    IUnknown *identity = nullptr;
    hr = inner->QueryInterface(IID_IUnknown, reinterpret_cast<void **>(&identity));
    if (FAILED(hr) || !identity) {
        inner->Release();
        log_line("adapter_wrap result=pass_through reason=canonical_identity_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return S_OK;
    }
    auto *candidate = new (std::nothrow) AdapterProxy(identity, inner);
    if (!candidate) {
        identity->Release();
        inner->Release();
        log_line("adapter_wrap result=pass_through reason=allocation_failure");
        return S_OK;
    }
    ProxyBase *registered = registry_adopt_or_get(candidate);
    if (!registered) {
        log_line("adapter_wrap result=pass_through reason=registry_failure");
        return S_OK;
    }
    auto *proxy = static_cast<AdapterProxy *>(registered);
    void *wrapped = nullptr;
    const HRESULT wrapHr = proxy->QueryInterface(iid, &wrapped);
    proxy->Release();
    if (FAILED(wrapHr) || !wrapped) {
        log_line("adapter_wrap result=pass_through reason=requested_iid_unavailable iid_data1=%08lx "
                 "hr=0x%08lx", static_cast<unsigned long>(iid.Data1),
                 static_cast<unsigned long>(wrapHr));
        return S_OK;
    }
    original->Release();
    *result = wrapped;
    log_line("adapter_wrap result=wrapped iid_data1=%08lx proxy=%p", static_cast<unsigned long>(iid.Data1),
             static_cast<void *>(proxy));
    return S_OK;
}

HRESULT wrap_output_result(REFIID iid, void **result) {
    if (!result || !*result) return S_OK;
    IUnknown *original = static_cast<IUnknown *>(*result);
    if (!is_output_iid(iid)) {
        void *probe = nullptr;
        const HRESULT probeHr = original->QueryInterface(iid, &probe);
        if (probe) static_cast<IUnknown *>(probe)->Release();
        log_qi_result("output_enumeration", iid, probeHr, SUCCEEDED(probeHr));
        return S_OK;
    }
    IDXGIOutput6 *inner = nullptr;
    HRESULT hr = original->QueryInterface(IID_IDXGIOutput6, reinterpret_cast<void **>(&inner));
    if (FAILED(hr) || !inner) {
        log_line("output_wrap result=pass_through reason=IDXGIOutput6_unavailable iid_data1=%08lx "
                 "hr=0x%08lx", static_cast<unsigned long>(iid.Data1), static_cast<unsigned long>(hr));
        return S_OK;
    }
    IUnknown *identity = nullptr;
    hr = inner->QueryInterface(IID_IUnknown, reinterpret_cast<void **>(&identity));
    if (FAILED(hr) || !identity) {
        inner->Release();
        log_line("output_wrap result=pass_through reason=canonical_identity_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return S_OK;
    }
    auto *candidate = new (std::nothrow) OutputProxy(identity, inner);
    if (!candidate) {
        identity->Release();
        inner->Release();
        log_line("output_wrap result=pass_through reason=allocation_failure");
        return S_OK;
    }
    ProxyBase *registered = registry_adopt_or_get(candidate);
    if (!registered) {
        log_line("output_wrap result=pass_through reason=registry_failure");
        return S_OK;
    }
    auto *proxy = static_cast<OutputProxy *>(registered);
    void *wrapped = nullptr;
    const HRESULT wrapHr = proxy->QueryInterface(iid, &wrapped);
    proxy->Release();
    if (FAILED(wrapHr) || !wrapped) {
        log_line("output_wrap result=pass_through reason=requested_iid_unavailable iid_data1=%08lx "
                 "hr=0x%08lx", static_cast<unsigned long>(iid.Data1),
                 static_cast<unsigned long>(wrapHr));
        return S_OK;
    }
    original->Release();
    *result = wrapped;
    log_line("output_wrap result=wrapped iid_data1=%08lx proxy=%p", static_cast<unsigned long>(iid.Data1),
             static_cast<void *>(proxy));
    return S_OK;
}

class FactoryProxy final : public IDXGIFactory7, public IDXGIFactoryMedia, public ProxyBase {
public:
    FactoryProxy(IUnknown *identity, IDXGIFactory7 *inner)
        : ProxyBase(identity, ProxyKind::Factory), inner_(inner) {
        inner_->QueryInterface(IID_IDXGIFactoryMedia, reinterpret_cast<void **>(&media_));
    }
    ~FactoryProxy() {
        release_com(media_);
        release_com(inner_);
        log_line("factory_destroy this=%p", static_cast<void *>(this));
    }
    void *public_interface_address() override { return static_cast<IDXGIFactory7 *>(this); }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (IsEqualIID(iid, IID_IUnknown) || IsEqualIID(iid, IID_IDXGIObject) ||
            IsEqualIID(iid, IID_IDXGIFactory) || IsEqualIID(iid, IID_IDXGIFactory1) ||
            IsEqualIID(iid, IID_IDXGIFactory2) || IsEqualIID(iid, IID_IDXGIFactory3) ||
            IsEqualIID(iid, IID_IDXGIFactory4) || IsEqualIID(iid, IID_IDXGIFactory5) ||
            IsEqualIID(iid, IID_IDXGIFactory6) || IsEqualIID(iid, IID_IDXGIFactory7)) {
            void *check = nullptr;
            const HRESULT hr = inner_->QueryInterface(iid, &check);
            if (FAILED(hr)) return hr;
            static_cast<IUnknown *>(check)->Release();
            *object = static_cast<IDXGIFactory7 *>(this);
            AddRef();
            log_qi_result("factory", iid, S_OK, false);
            return S_OK;
        }
        if (IsEqualIID(iid, IID_IDXGIFactoryMedia) && media_) {
            *object = static_cast<IDXGIFactoryMedia *>(this);
            AddRef();
            log_qi_result("factory", iid, S_OK, false);
            return S_OK;
        }
        const HRESULT hr = inner_->QueryInterface(iid, object);
        log_qi_result("factory", iid, hr, SUCCEEDED(hr));
        return hr;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return add_ref(); }
    ULONG STDMETHODCALLTYPE Release() override { return release_ref(); }

    HRESULT STDMETHODCALLTYPE SetPrivateData(REFGUID guid, UINT size, const void *data) override {
        return inner_->SetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE SetPrivateDataInterface(REFGUID guid, const IUnknown *object) override {
        return inner_->SetPrivateDataInterface(guid, object);
    }
    HRESULT STDMETHODCALLTYPE GetPrivateData(REFGUID guid, UINT *size, void *data) override {
        return inner_->GetPrivateData(guid, size, data);
    }
    HRESULT STDMETHODCALLTYPE GetParent(REFIID iid, void **parent) override {
        const HRESULT hr = inner_->GetParent(iid, parent);
        if (SUCCEEDED(hr) && parent && *parent && is_factory_iid(iid))
            return wrap_factory_result(iid, parent);
        return hr;
    }

    HRESULT STDMETHODCALLTYPE EnumAdapters(UINT index, IDXGIAdapter **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->EnumAdapters(index, adapter);
        if (SUCCEEDED(hr) && adapter && *adapter)
            wrap_adapter_result(IID_IDXGIAdapter, reinterpret_cast<void **>(adapter));
        log_line("factory_enum_adapters index=%u hr=0x%08lx wrapped=%u", index,
                 static_cast<unsigned long>(hr), SUCCEEDED(hr) && adapter && *adapter);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE MakeWindowAssociation(HWND window, UINT flags) override {
        return inner_->MakeWindowAssociation(window, flags);
    }
    HRESULT STDMETHODCALLTYPE GetWindowAssociation(HWND *window) override {
        return inner_->GetWindowAssociation(window);
    }
    HRESULT STDMETHODCALLTYPE CreateSwapChain(IUnknown *device, DXGI_SWAP_CHAIN_DESC *desc,
                                                IDXGISwapChain **swapchain) override {
        if (swapchain) *swapchain = nullptr;
        const HRESULT hr = inner_->CreateSwapChain(device, desc, swapchain);
        if (SUCCEEDED(hr)) wrap_swapchain_result(device, swapchain);
        log_line("factory_create_swapchain kind=CreateSwapChain device=%p hr=0x%08lx wrapped=%u",
                 static_cast<void *>(device), static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && swapchain && *swapchain);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE CreateSoftwareAdapter(HMODULE swrast, IDXGIAdapter **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->CreateSoftwareAdapter(swrast, adapter);
        if (SUCCEEDED(hr) && adapter && *adapter)
            wrap_adapter_result(IID_IDXGIAdapter, reinterpret_cast<void **>(adapter));
        return hr;
    }
    HRESULT STDMETHODCALLTYPE EnumAdapters1(UINT index, IDXGIAdapter1 **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->EnumAdapters1(index, adapter);
        if (SUCCEEDED(hr) && adapter && *adapter)
            wrap_adapter_result(IID_IDXGIAdapter1, reinterpret_cast<void **>(adapter));
        log_line("factory_enum_adapters1 index=%u hr=0x%08lx wrapped=%u", index,
                 static_cast<unsigned long>(hr), SUCCEEDED(hr) && adapter && *adapter);
        return hr;
    }
    WINBOOL STDMETHODCALLTYPE IsCurrent() override { return inner_->IsCurrent(); }
    WINBOOL STDMETHODCALLTYPE IsWindowedStereoEnabled() override {
        return inner_->IsWindowedStereoEnabled();
    }
    HRESULT STDMETHODCALLTYPE CreateSwapChainForHwnd(IUnknown *device, HWND window,
                                                       const DXGI_SWAP_CHAIN_DESC1 *desc,
                                                       const DXGI_SWAP_CHAIN_FULLSCREEN_DESC *fullscreenDesc,
                                                       IDXGIOutput *restrictOutput,
                                                       IDXGISwapChain1 **swapchain) override {
        if (swapchain) *swapchain = nullptr;
        const HRESULT hr = inner_->CreateSwapChainForHwnd(device, window, desc, fullscreenDesc,
                                                          unwrap_dxgi_object(restrictOutput), swapchain);
        if (SUCCEEDED(hr)) wrap_swapchain_result(device, swapchain);
        log_line("factory_create_swapchain kind=CreateSwapChainForHwnd device=%p window=%p hr=0x%08lx "
                 "wrapped=%u width=%u height=%u format=%u buffer_count=%u effect=%u",
                 static_cast<void *>(device), static_cast<void *>(window), static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && swapchain && *swapchain, desc ? desc->Width : 0,
                 desc ? desc->Height : 0, desc ? static_cast<unsigned>(desc->Format) : 0,
                 desc ? desc->BufferCount : 0, desc ? static_cast<unsigned>(desc->SwapEffect) : 0);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE CreateSwapChainForCoreWindow(IUnknown *device, IUnknown *window,
                                                            const DXGI_SWAP_CHAIN_DESC1 *desc,
                                                            IDXGIOutput *restrictOutput,
                                                            IDXGISwapChain1 **swapchain) override {
        if (swapchain) *swapchain = nullptr;
        const HRESULT hr = inner_->CreateSwapChainForCoreWindow(device, window, desc,
                                                                unwrap_dxgi_object(restrictOutput),
                                                                swapchain);
        if (SUCCEEDED(hr)) wrap_swapchain_result(device, swapchain);
        log_line("factory_create_swapchain kind=CreateSwapChainForCoreWindow device=%p window=%p "
                 "hr=0x%08lx wrapped=%u",
                 static_cast<void *>(device), static_cast<void *>(window), static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && swapchain && *swapchain);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetSharedResourceAdapterLuid(HANDLE resource, LUID *luid) override {
        return inner_->GetSharedResourceAdapterLuid(resource, luid);
    }
    HRESULT STDMETHODCALLTYPE RegisterStereoStatusWindow(HWND window, UINT message, DWORD *cookie) override {
        return inner_->RegisterStereoStatusWindow(window, message, cookie);
    }
    HRESULT STDMETHODCALLTYPE RegisterStereoStatusEvent(HANDLE event, DWORD *cookie) override {
        return inner_->RegisterStereoStatusEvent(event, cookie);
    }
    void STDMETHODCALLTYPE UnregisterStereoStatus(DWORD cookie) override {
        inner_->UnregisterStereoStatus(cookie);
    }
    HRESULT STDMETHODCALLTYPE RegisterOcclusionStatusWindow(HWND window, UINT message,
                                                              DWORD *cookie) override {
        return inner_->RegisterOcclusionStatusWindow(window, message, cookie);
    }
    HRESULT STDMETHODCALLTYPE RegisterOcclusionStatusEvent(HANDLE event, DWORD *cookie) override {
        return inner_->RegisterOcclusionStatusEvent(event, cookie);
    }
    void STDMETHODCALLTYPE UnregisterOcclusionStatus(DWORD cookie) override {
        inner_->UnregisterOcclusionStatus(cookie);
    }
    HRESULT STDMETHODCALLTYPE CreateSwapChainForComposition(IUnknown *device,
                                                             const DXGI_SWAP_CHAIN_DESC1 *desc,
                                                             IDXGIOutput *restrictOutput,
                                                             IDXGISwapChain1 **swapchain) override {
        if (swapchain) *swapchain = nullptr;
        const HRESULT hr = inner_->CreateSwapChainForComposition(device, desc,
                                                                  unwrap_dxgi_object(restrictOutput),
                                                                  swapchain);
        if (SUCCEEDED(hr)) wrap_swapchain_result(device, swapchain);
        log_line("factory_create_swapchain kind=CreateSwapChainForComposition device=%p hr=0x%08lx "
                 "wrapped=%u",
                 static_cast<void *>(device), static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && swapchain && *swapchain);
        return hr;
    }
    UINT STDMETHODCALLTYPE GetCreationFlags() override { return inner_->GetCreationFlags(); }
    HRESULT STDMETHODCALLTYPE EnumAdapterByLuid(LUID luid, REFIID iid, void **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->EnumAdapterByLuid(luid, iid, adapter);
        if (SUCCEEDED(hr)) wrap_adapter_result(iid, adapter);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE EnumWarpAdapter(REFIID iid, void **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->EnumWarpAdapter(iid, adapter);
        if (SUCCEEDED(hr)) wrap_adapter_result(iid, adapter);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE CheckFeatureSupport(DXGI_FEATURE feature, void *supportData,
                                                    UINT supportDataSize) override {
        return inner_->CheckFeatureSupport(feature, supportData, supportDataSize);
    }
    HRESULT STDMETHODCALLTYPE EnumAdapterByGpuPreference(UINT index, DXGI_GPU_PREFERENCE preference,
                                                           REFIID iid, void **adapter) override {
        if (adapter) *adapter = nullptr;
        const HRESULT hr = inner_->EnumAdapterByGpuPreference(index, preference, iid, adapter);
        if (SUCCEEDED(hr)) wrap_adapter_result(iid, adapter);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE RegisterAdaptersChangedEvent(HANDLE event, DWORD *cookie) override {
        return inner_->RegisterAdaptersChangedEvent(event, cookie);
    }
    HRESULT STDMETHODCALLTYPE UnregisterAdaptersChangedEvent(DWORD cookie) override {
        return inner_->UnregisterAdaptersChangedEvent(cookie);
    }
    HRESULT STDMETHODCALLTYPE CreateSwapChainForCompositionSurfaceHandle(
        IUnknown *device, HANDLE surface, const DXGI_SWAP_CHAIN_DESC1 *desc,
        IDXGIOutput *restrictOutput, IDXGISwapChain1 **swapchain) override {
        if (!media_) return E_NOINTERFACE;
        if (swapchain) *swapchain = nullptr;
        const HRESULT hr = media_->CreateSwapChainForCompositionSurfaceHandle(
            device, surface, desc, unwrap_dxgi_object(restrictOutput), swapchain);
        if (SUCCEEDED(hr)) wrap_swapchain_result(device, swapchain);
        log_line("factory_create_swapchain kind=CreateSwapChainForCompositionSurfaceHandle "
                 "device=%p surface=%p hr=0x%08lx wrapped=%u",
                 static_cast<void *>(device), surface, static_cast<unsigned long>(hr),
                 SUCCEEDED(hr) && swapchain && *swapchain);
        return hr;
    }
    HRESULT STDMETHODCALLTYPE CreateDecodeSwapChainForCompositionSurfaceHandle(
        IUnknown *device, HANDLE surface, DXGI_DECODE_SWAP_CHAIN_DESC *desc,
        IDXGIResource *buffer, IDXGIOutput *restrictOutput,
        IDXGIDecodeSwapChain **swapchain) override {
        return media_ ? media_->CreateDecodeSwapChainForCompositionSurfaceHandle(
                            device, surface, desc, buffer, restrictOutput, swapchain)
                      : E_NOINTERFACE;
    }

private:
    IDXGIFactory7 *inner_ = nullptr;
    IDXGIFactoryMedia *media_ = nullptr;
};

template <typename T>
void wrap_swapchain_result(IUnknown *device, T **result) {
    if (!result || !*result) return;
    T *original = *result;
    IDXGISwapChain4 *inner = nullptr;
    HRESULT hr = original->QueryInterface(IID_IDXGISwapChain4, reinterpret_cast<void **>(&inner));
    if (FAILED(hr) || !inner) {
        log_line("swapchain_wrap result=pass_through reason=IDXGISwapChain4_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return;
    }
    IUnknown *identity = nullptr;
    hr = inner->QueryInterface(IID_IUnknown, reinterpret_cast<void **>(&identity));
    if (FAILED(hr) || !identity) {
        inner->Release();
        log_line("swapchain_wrap result=pass_through reason=canonical_identity_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return;
    }
    auto *candidate = new (std::nothrow) SwapChainProxy(identity, inner, device);
    if (!candidate) {
        identity->Release();
        inner->Release();
        log_line("swapchain_wrap result=pass_through reason=allocation_failure");
        return;
    }
    ProxyBase *registered = registry_adopt_or_get(candidate);
    if (!registered) {
        log_line("swapchain_wrap result=pass_through reason=registry_failure");
        return;
    }
    auto *proxy = static_cast<SwapChainProxy *>(registered);
    void *wrapped = nullptr;
    const HRESULT wrapHr = proxy->QueryInterface(__uuidof(T), &wrapped);
    proxy->Release();
    if (FAILED(wrapHr) || !wrapped) {
        log_line("swapchain_wrap result=pass_through reason=requested_iid_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(wrapHr));
        return;
    }
    original->Release();
    *result = static_cast<T *>(wrapped);
    log_line("swapchain_wrap result=wrapped proxy=%p", static_cast<void *>(proxy));
}

HRESULT wrap_factory_result(REFIID iid, void **result) {
    if (!result || !*result) return S_OK;
    if (!is_factory_iid(iid)) {
        log_qi_result("factory_creation", iid, S_OK, true);
        return S_OK;
    }
    IUnknown *original = static_cast<IUnknown *>(*result);
    IDXGIFactory7 *inner = nullptr;
    HRESULT hr = original->QueryInterface(IID_IDXGIFactory7,
                                          reinterpret_cast<void **>(&inner));
    if (FAILED(hr) || !inner) {
        log_line("factory_wrap result=pass_through reason=IDXGIFactory7_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return S_OK;
    }
    IUnknown *identity = nullptr;
    hr = inner->QueryInterface(IID_IUnknown, reinterpret_cast<void **>(&identity));
    if (FAILED(hr) || !identity) {
        inner->Release();
        log_line("factory_wrap result=pass_through reason=canonical_identity_unavailable hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        return S_OK;
    }
    auto *candidate = new (std::nothrow) FactoryProxy(identity, inner);
    if (!candidate) {
        identity->Release();
        inner->Release();
        log_line("factory_wrap result=pass_through reason=allocation_failure");
        return S_OK;
    }
    ProxyBase *registered = registry_adopt_or_get(candidate);
    if (!registered) {
        log_line("factory_wrap result=pass_through reason=registry_failure");
        return S_OK;
    }
    auto *proxy = static_cast<FactoryProxy *>(registered);
    void *wrapped = nullptr;
    const HRESULT wrappedHr = proxy->QueryInterface(iid, &wrapped);
    proxy->Release();
    if (FAILED(wrappedHr)) {
        original->Release();
        *result = nullptr;
        log_line("factory_wrap result=failed requested_iid=%p hr=0x%08lx",
                 static_cast<const void *>(&iid), static_cast<unsigned long>(wrappedHr));
        return wrappedHr;
    }
    original->Release();
    *result = wrapped;
    log_line("factory_wrap result=wrapped iid=%p proxy=%p", static_cast<const void *>(&iid), wrapped);
    return S_OK;
}

HRESULT WINAPI forward_factory(REFIID iid, void **factory, const char *name) {
    if (factory) *factory = nullptr;
    CreateFactoryProc function = provider_export<CreateFactoryProc>(name);
    if (!function) {
        log_line("export_forward name=%s result=provider_export_missing", name);
        return DXGI_ERROR_UNSUPPORTED;
    }
    const HRESULT hr = function(iid, factory);
    if (SUCCEEDED(hr)) wrap_factory_result(iid, factory);
    log_line("export_forward name=%s hr=0x%08lx factory=%p wrapped=%u", name,
             static_cast<unsigned long>(hr), factory ? *factory : nullptr,
             SUCCEEDED(hr) && factory && *factory);
    return hr;
}

} // namespace

extern "C" HRESULT WINAPI CreateDXGIFactory(REFIID iid, void **factory) {
    return forward_factory(iid, factory, "CreateDXGIFactory");
}

extern "C" HRESULT WINAPI CreateDXGIFactory1(REFIID iid, void **factory) {
    return forward_factory(iid, factory, "CreateDXGIFactory1");
}

extern "C" HRESULT WINAPI CreateDXGIFactory2(UINT flags, REFIID iid, void **factory) {
    if (factory) *factory = nullptr;
    CreateFactory2Proc function = provider_export<CreateFactory2Proc>("CreateDXGIFactory2");
    if (!function) {
        log_line("export_forward name=CreateDXGIFactory2 result=provider_export_missing");
        return DXGI_ERROR_UNSUPPORTED;
    }
    const HRESULT hr = function(flags, iid, factory);
    if (SUCCEEDED(hr)) wrap_factory_result(iid, factory);
    log_line("export_forward name=CreateDXGIFactory2 flags=0x%08x hr=0x%08lx factory=%p wrapped=%u",
             flags, static_cast<unsigned long>(hr), factory ? *factory : nullptr,
             SUCCEEDED(hr) && factory && *factory);
    return hr;
}

extern "C" HRESULT WINAPI DXGIDeclareAdapterRemovalSupport() {
    NoArgProc function = provider_export<NoArgProc>("DXGIDeclareAdapterRemovalSupport");
    return function ? function() : DXGI_ERROR_UNSUPPORTED;
}

extern "C" HRESULT WINAPI DXGIDisableVBlankVirtualization() {
    NoArgProc function = provider_export<NoArgProc>("DXGIDisableVBlankVirtualization");
    return function ? function() : DXGI_ERROR_UNSUPPORTED;
}

extern "C" HRESULT WINAPI DXGIGetDebugInterface(REFIID iid, void **debug) {
    DebugProc function = provider_export<DebugProc>("DXGIGetDebugInterface");
    return function ? function(iid, debug) : DXGI_ERROR_UNSUPPORTED;
}

extern "C" HRESULT WINAPI DXGIGetDebugInterface1(UINT flags, REFIID iid, void **debug) {
    Debug1Proc function = provider_export<Debug1Proc>("DXGIGetDebugInterface1");
    return function ? function(flags, iid, debug) : DXGI_ERROR_UNSUPPORTED;
}
