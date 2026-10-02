#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_4.h>
#include <d3d12.h>

#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <cstring>

namespace {

struct BackBuffer {
    ID3D12Resource *resource = nullptr;
    ID3D12CommandAllocator *allocator = nullptr;
    D3D12_CPU_DESCRIPTOR_HANDLE rtv{};
    UINT64 fenceValue = 0;
};

struct AdapterState {
    IDXGIFactory2 *factory = nullptr;
    ID3D12Device *device = nullptr;
    ID3D12CommandQueue *queue = nullptr;
    IDXGISwapChain1 *swapChain = nullptr;
    IDXGISwapChain3 *swapChain3 = nullptr;
    ID3D12GraphicsCommandList *commandList = nullptr;
    ID3D12DescriptorHeap *rtvHeap = nullptr;
    ID3D12Fence *fence = nullptr;
    HANDLE fenceEvent = nullptr;
    ID3D12Resource *frameTextures[2]{};
    BackBuffer backBuffers[4]{};
    DXGI_SWAP_CHAIN_DESC1 desc{};
    UINT bufferCount = 0;
    UINT64 nextFenceValue = 0;
    UINT rtvIncrement = 0;
    UINT syncInterval = 1;
    UINT outputRate = 60;
    UINT64 qpcFrequency = 0;
    UINT64 nextPresentTarget = 0;
    UINT64 firstSourceRenderQpc = 0;
    UINT presentCalls = 0;
    UINT syntheticCalls = 0;
    UINT64 sourceFramesRendered = 0;
    bool haveStatsSample = false;
    UINT previousStatsPresentCount = 0;
    UINT previousStatsRefreshCount = 0;
    bool havePrevious = false;
    UINT64 currentFrame = 0;
    char currentSourceKind = 'A';
    float sourceColor[4]{};
    float previousColor[4]{};
};

AdapterState g;
FILE *gLog = nullptr;

void log_line(const char *format, ...) {
    if (!gLog) gLog = std::fopen("dxgi_probe.log", "a");
    if (!gLog) return;
    va_list args;
    va_start(args, format);
    std::vfprintf(gLog, format, args);
    va_end(args);
    std::fputc('\n', gLog);
    std::fflush(gLog);
}

template <typename T> void release(T *&value) {
    if (value) {
        value->Release();
        value = nullptr;
    }
}

bool succeeded(HRESULT hr, const char *operation) {
    log_line("adapter op=%s hr=0x%08lx", operation, static_cast<unsigned long>(hr));
    return SUCCEEDED(hr);
}

UINT64 qpc_now();

HRESULT wait_for_fence(UINT64 value, const char *reason) {
    if (!g.fence) return E_FAIL;
    const UINT64 startQpc = qpc_now();
    const UINT64 completed = g.fence->GetCompletedValue();
    if (completed == UINT64_MAX) {
        const UINT64 endQpc = qpc_now();
        log_line("fence_completion reason=%s target=%llu before=device_removed hr=0x%08lx qpcStart=%llu qpcEnd=%llu waitTicks=%llu",
                 reason, static_cast<unsigned long long>(value),
                 static_cast<unsigned long>(DXGI_ERROR_DEVICE_REMOVED),
                 static_cast<unsigned long long>(startQpc),
                 static_cast<unsigned long long>(endQpc),
                 static_cast<unsigned long long>(endQpc - startQpc));
        return DXGI_ERROR_DEVICE_REMOVED;
    }
    HRESULT hr = S_OK;
    if (completed < value) {
        hr = g.fence->SetEventOnCompletion(value, g.fenceEvent);
        if (SUCCEEDED(hr))
            hr = WaitForSingleObject(g.fenceEvent, INFINITE) == WAIT_OBJECT_0 ? S_OK : E_FAIL;
    }
    const UINT64 endQpc = qpc_now();
    const UINT64 finalCompleted = g.fence->GetCompletedValue();
    log_line("fence_completion reason=%s target=%llu before=%llu after=%llu hr=0x%08lx qpcStart=%llu qpcEnd=%llu waitTicks=%llu",
             reason, static_cast<unsigned long long>(value),
             static_cast<unsigned long long>(completed),
             static_cast<unsigned long long>(finalCompleted),
             static_cast<unsigned long>(hr),
             static_cast<unsigned long long>(startQpc),
             static_cast<unsigned long long>(endQpc),
             static_cast<unsigned long long>(endQpc - startQpc));
    return hr;
}

HRESULT wait_for_idle() {
    if (!g.queue || !g.fence) return E_FAIL;
    const UINT64 value = ++g.nextFenceValue;
    const UINT64 signalQpc = qpc_now();
    HRESULT hr = g.queue->Signal(g.fence, value);
    if (FAILED(hr)) return hr;
    log_line("gpu_signal op=idle fence=%llu qpc=%llu hr=0x%08lx",
             static_cast<unsigned long long>(value),
             static_cast<unsigned long long>(signalQpc), static_cast<unsigned long>(hr));
    return wait_for_fence(value, "idle");
}

UINT64 qpc_now() {
    LARGE_INTEGER value{};
    QueryPerformanceCounter(&value);
    return static_cast<UINT64>(value.QuadPart);
}

UINT64 output_interval() {
    return g.outputRate && g.qpcFrequency
        ? (g.qpcFrequency + g.outputRate / 2) / g.outputRate
        : 0;
}

void wait_until_qpc(UINT64 target, const char *kind) {
    for (;;) {
        const UINT64 now = qpc_now();
        if (now >= target) {
            log_line("present_schedule kind=%s targetQpc=%llu actualQpc=%llu lateTicks=%llu",
                     kind, static_cast<unsigned long long>(target),
                     static_cast<unsigned long long>(now),
                     static_cast<unsigned long long>(now - target));
            return;
        }
        const UINT64 remaining = target - now;
        if (remaining > g.qpcFrequency / 500) {
            const UINT64 sleepTicks = remaining - g.qpcFrequency / 500;
            const DWORD sleepMs = static_cast<DWORD>((sleepTicks * 1000) / g.qpcFrequency);
            Sleep(sleepMs ? sleepMs : 1);
        } else {
            Sleep(0);
        }
    }
}

void release_buffers() {
    for (UINT i = 0; i < 4; ++i) release(g.backBuffers[i].resource);
}

void release_frame_textures() {
    release(g.frameTextures[0]);
    release(g.frameTextures[1]);
}

HRESULT create_frame_textures() {
    if (!g.bufferCount || !g.backBuffers[0].resource) return E_FAIL;
    D3D12_RESOURCE_DESC textureDesc = g.backBuffers[0].resource->GetDesc();
    textureDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heap.CreationNodeMask = 1;
    heap.VisibleNodeMask = 1;
    for (UINT i = 0; i < 2; ++i) {
        HRESULT hr = g.device->CreateCommittedResource(
            &heap, D3D12_HEAP_FLAG_NONE, &textureDesc,
            D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
            __uuidof(ID3D12Resource), reinterpret_cast<void **>(&g.frameTextures[i]));
        if (FAILED(hr)) return hr;
        log_line("owned_texture slot=%u ptr=%p width=%llu height=%u format=%u state=COPY_DEST",
                 i, static_cast<void *>(g.frameTextures[i]),
                 static_cast<unsigned long long>(textureDesc.Width), textureDesc.Height,
                 static_cast<unsigned>(textureDesc.Format));
    }
    return S_OK;
}

HRESULT acquire_buffers() {
    release_buffers();
    if (!succeeded(g.swapChain->GetDesc1(&g.desc), "GetDesc1")) return E_FAIL;
    g.bufferCount = g.desc.BufferCount;
    if (!g.bufferCount || g.bufferCount > 4) return E_INVALIDARG;
    log_line("swapchain desc width=%u height=%u format=%u buffers=%u swapEffect=%u flags=0x%x",
             g.desc.Width, g.desc.Height, static_cast<unsigned>(g.desc.Format),
             g.desc.BufferCount, static_cast<unsigned>(g.desc.SwapEffect), g.desc.Flags);

    D3D12_CPU_DESCRIPTOR_HANDLE handle = g.rtvHeap->GetCPUDescriptorHandleForHeapStart();
    for (UINT i = 0; i < g.bufferCount; ++i) {
        BackBuffer &buffer = g.backBuffers[i];
        HRESULT hr = g.swapChain->GetBuffer(i, __uuidof(ID3D12Resource),
                                             reinterpret_cast<void **>(&buffer.resource));
        if (FAILED(hr)) return hr;
        const D3D12_RESOURCE_DESC resourceDesc = buffer.resource->GetDesc();
        IUnknown *identity = nullptr;
        buffer.resource->QueryInterface(__uuidof(IUnknown), reinterpret_cast<void **>(&identity));
        log_line("backbuffer index=%u identity=%p resource=%p width=%llu height=%u format=%u dimension=%u",
                 i, static_cast<void *>(identity), static_cast<void *>(buffer.resource),
                 static_cast<unsigned long long>(resourceDesc.Width), resourceDesc.Height,
                 static_cast<unsigned>(resourceDesc.Format), static_cast<unsigned>(resourceDesc.Dimension));
        release(identity);
        if (!buffer.allocator) {
            hr = g.device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                   __uuidof(ID3D12CommandAllocator),
                                                   reinterpret_cast<void **>(&buffer.allocator));
            if (FAILED(hr)) return hr;
        }
        buffer.fenceValue = 0;
        buffer.rtv = handle;
        g.device->CreateRenderTargetView(buffer.resource, nullptr, handle);
        handle.ptr += g.rtvIncrement;
    }
    release_frame_textures();
    return create_frame_textures();
}

HRESULT begin_commands(UINT index) {
    if (index >= g.bufferCount) return E_INVALIDARG;
    BackBuffer &buffer = g.backBuffers[index];
    if (buffer.fenceValue) {
        HRESULT hr = wait_for_fence(buffer.fenceValue, "allocator_reuse");
        if (FAILED(hr)) return hr;
    }
    HRESULT hr = buffer.allocator->Reset();
    if (FAILED(hr)) return hr;
    return g.commandList->Reset(buffer.allocator, nullptr);
}

HRESULT submit_commands(const char *operation, UINT index) {
    HRESULT hr = g.commandList->Close();
    if (FAILED(hr)) return hr;
    ID3D12CommandList *lists[] = {g.commandList};
    const UINT64 executeQpc = qpc_now();
    g.queue->ExecuteCommandLists(1, lists);
    const UINT64 executeReturnQpc = qpc_now();
    const UINT64 value = ++g.nextFenceValue;
    hr = g.queue->Signal(g.fence, value);
    if (FAILED(hr)) return hr;
    g.backBuffers[index].fenceValue = value;
    const UINT64 signalReturnQpc = qpc_now();
    log_line("gpu_submit op=%s backbuffer=%u fence=%llu qpcExecuteCall=%llu qpcExecuteReturn=%llu qpcSignalReturn=%llu hr=0x%08lx",
             operation, index, static_cast<unsigned long long>(value),
             static_cast<unsigned long long>(executeQpc),
             static_cast<unsigned long long>(executeReturnQpc),
             static_cast<unsigned long long>(signalReturnQpc), static_cast<unsigned long>(hr));
    return S_OK;
}

void transition(ID3D12Resource *resource, D3D12_RESOURCE_STATES before,
                D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Flags = D3D12_RESOURCE_BARRIER_FLAG_NONE;
    barrier.Transition.pResource = resource;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = before;
    barrier.Transition.StateAfter = after;
    g.commandList->ResourceBarrier(1, &barrier);
}

HRESULT capture_current(ID3D12Resource *destination) {
    const UINT index = g.swapChain3->GetCurrentBackBufferIndex();
    if (index >= g.bufferCount) return E_INVALIDARG;
    HRESULT hr = begin_commands(index);
    if (FAILED(hr)) return hr;
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_PRESENT,
               D3D12_RESOURCE_STATE_COPY_SOURCE);
    g.commandList->CopyResource(destination, g.backBuffers[index].resource);
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_COPY_SOURCE,
               D3D12_RESOURCE_STATE_PRESENT);
    return submit_commands("capture_source", index);
}

HRESULT draw_diagnostic(const float color[4], char kind, UINT64 frame) {
    const UINT index = g.swapChain3->GetCurrentBackBufferIndex();
    if (index >= g.bufferCount) return E_INVALIDARG;
    HRESULT hr = begin_commands(index);
    if (FAILED(hr)) return hr;
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_PRESENT,
               D3D12_RESOURCE_STATE_RENDER_TARGET);
    g.commandList->ClearRenderTargetView(g.backBuffers[index].rtv, color, 0, nullptr);
    const D3D12_RESOURCE_DESC desc = g.backBuffers[index].resource->GetDesc();
    const LONG width = static_cast<LONG>(desc.Width);
    const LONG height = static_cast<LONG>(desc.Height);
    const LONG marginX = width / 10;
    const LONG marginY = height / 10;
    const LONG markerWidth = width / 24 > 0 ? width / 24 : 1;
    const LONG markerHeight = height / 24 > 0 ? height / 24 : 1;
    D3D12_RECT markers[2]{};
    float markerColor[4] = {0.0f, 0.0f, 0.0f, 1.0f};
    switch (kind) {
    case 'A':
        markerColor[0] = 1.0f; markerColor[1] = 0.08f; markerColor[2] = 0.08f;
        markers[0] = D3D12_RECT{marginX, marginY, width / 2, marginY + markerHeight};
        markers[1] = D3D12_RECT{marginX, marginY, marginX + markerWidth, height / 2};
        break;
    case 'G':
        markerColor[0] = 0.08f; markerColor[1] = 1.0f; markerColor[2] = 0.12f;
        markers[0] = D3D12_RECT{width / 2 - width / 4, height / 2 - markerHeight / 2,
                                width / 2 + width / 4, height / 2 + markerHeight / 2 + 1};
        markers[1] = D3D12_RECT{width / 2 - markerWidth / 2, height / 2 - height / 4,
                                width / 2 + markerWidth / 2 + 1, height / 2 + height / 4};
        break;
    case 'B':
        markerColor[0] = 0.08f; markerColor[1] = 0.25f; markerColor[2] = 1.0f;
        markers[0] = D3D12_RECT{width / 2, height - marginY - markerHeight,
                                width - marginX, height - marginY};
        markers[1] = D3D12_RECT{width - marginX - markerWidth, height / 2,
                                width - marginX, height - marginY};
        break;
    case 'C':
        markerColor[0] = 1.0f; markerColor[1] = 0.75f; markerColor[2] = 0.05f;
        markers[0] = D3D12_RECT{width / 2, marginY, width - marginX, marginY + markerHeight};
        markers[1] = D3D12_RECT{width - marginX - markerWidth, marginY,
                                width - marginX, height / 2};
        break;
    default:
        return E_INVALIDARG;
    }
    g.commandList->ClearRenderTargetView(g.backBuffers[index].rtv, markerColor, 2, markers);
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_RENDER_TARGET,
               D3D12_RESOURCE_STATE_PRESENT);
    const UINT64 patternQpc = qpc_now();
    log_line("diagnostic_gpu_pattern kind=%c frame=%llu backbuffer=%u size=%lux%lu clear=%.4f,%.4f,%.4f marker=%.2f,%.2f,%.2f qpc=%llu",
             kind, static_cast<unsigned long long>(frame), index,
             static_cast<unsigned long>(width), static_cast<unsigned long>(height),
             color[0], color[1], color[2], markerColor[0], markerColor[1], markerColor[2],
             static_cast<unsigned long long>(patternQpc));
    const char *operation = kind == 'G' ? "draw_G" : (kind == 'A' ? "draw_A" : (kind == 'B' ? "draw_B" : "draw_C"));
    return submit_commands(operation, index);
}

HRESULT restore_saved_source(ID3D12Resource *source) {
    const UINT index = g.swapChain3->GetCurrentBackBufferIndex();
    if (index >= g.bufferCount) return E_INVALIDARG;
    HRESULT hr = begin_commands(index);
    if (FAILED(hr)) return hr;
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_PRESENT,
               D3D12_RESOURCE_STATE_COPY_DEST);
    transition(source, D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_COPY_SOURCE);
    g.commandList->CopyResource(g.backBuffers[index].resource, source);
    transition(g.backBuffers[index].resource, D3D12_RESOURCE_STATE_COPY_DEST,
               D3D12_RESOURCE_STATE_PRESENT);
    transition(source, D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COPY_DEST);
    return submit_commands("restore_source", index);
}

HRESULT present_frame(char kind, bool usePresent1) {
    const UINT index = g.swapChain3->GetCurrentBackBufferIndex();
    if (index >= g.bufferCount) return E_INVALIDARG;
    const UINT64 fenceValue = g.backBuffers[index].fenceValue;
    if (fenceValue) {
        HRESULT waitHr = wait_for_fence(fenceValue, "before_present");
        if (FAILED(waitHr)) return waitHr;
    }
    const UINT64 qpcBefore = qpc_now();
    HRESULT hr;
    if (usePresent1) {
        DXGI_PRESENT_PARAMETERS parameters{};
        hr = g.swapChain->Present1(g.syncInterval, 0, &parameters);
    } else {
        hr = g.swapChain->Present(g.syncInterval, 0);
    }
    const UINT64 qpcAfter = qpc_now();
    ++g.presentCalls;
    if (kind == 'G') ++g.syntheticCalls;
    UINT lastCount = 0;
    const HRESULT countHr = g.swapChain->GetLastPresentCount(&lastCount);
    DXGI_FRAME_STATISTICS stats{};
    const HRESULT statsHr = g.swapChain->GetFrameStatistics(&stats);
    const UINT64 qpcStats = qpc_now();
    const long long presentDelta = SUCCEEDED(statsHr) && g.haveStatsSample
        ? static_cast<long long>(stats.PresentCount) - static_cast<long long>(g.previousStatsPresentCount)
        : -1;
    const long long refreshDelta = SUCCEEDED(statsHr) && g.haveStatsSample
        ? static_cast<long long>(stats.PresentRefreshCount) - static_cast<long long>(g.previousStatsRefreshCount)
        : -1;
    if (SUCCEEDED(statsHr)) {
        g.previousStatsPresentCount = stats.PresentCount;
        g.previousStatsRefreshCount = stats.PresentRefreshCount;
        g.haveStatsSample = true;
    }
    log_line("present sequence=%u kind=%c api=%s sync=%u hr=0x%08lx qpcBefore=%llu qpcAfter=%llu apiTicks=%llu lastPresentCountHr=0x%08lx lastPresentCount=%u frameStatsHr=0x%08lx monitorPresentCount=%u presentRefreshCount=%u presentCountDelta=%lld refreshCountDelta=%lld qpcStats=%llu sourceFramesRendered=%llu sourceFramePair=%llu",
             g.presentCalls, kind, usePresent1 ? "Present1" : "Present",
             g.syncInterval, static_cast<unsigned long>(hr),
             static_cast<unsigned long long>(qpcBefore), static_cast<unsigned long long>(qpcAfter),
             static_cast<unsigned long long>(qpcAfter - qpcBefore),
             static_cast<unsigned long>(countHr), lastCount, static_cast<unsigned long>(statsHr),
             stats.PresentCount, stats.PresentRefreshCount, presentDelta, refreshDelta,
             static_cast<unsigned long long>(qpcStats),
             static_cast<unsigned long long>(g.sourceFramesRendered),
             static_cast<unsigned long long>(g.currentFrame));
    return hr;
}

} // namespace

extern "C" __declspec(dllexport) HRESULT WINAPI FG_SetSyncInterval(UINT interval) {
    if (interval > 4) return E_INVALIDARG;
    g.syncInterval = interval;
    log_line("sync_interval=%u", interval);
    return S_OK;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_SetOutputRate(UINT outputFramesPerSecond) {
    if (outputFramesPerSecond != 60 && outputFramesPerSecond != 120) return E_INVALIDARG;
    g.outputRate = outputFramesPerSecond;
    log_line("output_rate=%u qpcInterval=%llu", outputFramesPerSecond,
             static_cast<unsigned long long>(output_interval()));
    return S_OK;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_Attach(
    IDXGIFactory2 *factory, ID3D12Device *device, ID3D12CommandQueue *queue,
    IDXGISwapChain1 *swapChain) {
    if (!factory || !device || !queue || !swapChain) return E_POINTER;
    g.factory = factory; g.factory->AddRef();
    g.device = device; g.device->AddRef();
    g.queue = queue; g.queue->AddRef();
    g.swapChain = swapChain; g.swapChain->AddRef();
    HRESULT hr = swapChain->QueryInterface(__uuidof(IDXGISwapChain3),
                                            reinterpret_cast<void **>(&g.swapChain3));
    if (!succeeded(hr, "QueryInterface(IDXGISwapChain3)")) return hr;
    IDXGIFactory4 *factory4 = nullptr;
    hr = factory->QueryInterface(__uuidof(IDXGIFactory4), reinterpret_cast<void **>(&factory4));
    log_line("factory interface=IDXGIFactory2 factory4_hr=0x%08lx ptr=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(factory4));
    release(factory4);
    IUnknown *chainIdentity = nullptr;
    swapChain->QueryInterface(__uuidof(IUnknown), reinterpret_cast<void **>(&chainIdentity));
    log_line("attach swapchain_identity=%p swapchain=%p queue=%p device=%p",
             static_cast<void *>(chainIdentity), static_cast<void *>(swapChain),
             static_cast<void *>(queue), static_cast<void *>(device));
    release(chainIdentity);

    D3D12_COMMAND_QUEUE_DESC queueDesc = queue->GetDesc();
    log_line("command_queue type=%u priority=%d flags=0x%x nodeMask=%u",
             static_cast<unsigned>(queueDesc.Type), queueDesc.Priority,
             static_cast<unsigned>(queueDesc.Flags), queueDesc.NodeMask);
    LARGE_INTEGER qpcFrequency{};
    QueryPerformanceFrequency(&qpcFrequency);
    g.qpcFrequency = static_cast<UINT64>(qpcFrequency.QuadPart);
    log_line("clock qpcFrequency=%lld", static_cast<long long>(qpcFrequency.QuadPart));
    g.rtvIncrement = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc{};
    heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heapDesc.NumDescriptors = 4;
    heapDesc.Flags = D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
    hr = device->CreateDescriptorHeap(&heapDesc, __uuidof(ID3D12DescriptorHeap),
                                       reinterpret_cast<void **>(&g.rtvHeap));
    if (FAILED(hr)) return hr;
    hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence),
                             reinterpret_cast<void **>(&g.fence));
    if (FAILED(hr)) return hr;
    g.fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!g.fenceEvent) return HRESULT_FROM_WIN32(GetLastError());
    hr = acquire_buffers();
    if (FAILED(hr)) return hr;
    hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
                                   g.backBuffers[0].allocator, nullptr,
                                   __uuidof(ID3D12GraphicsCommandList),
                                   reinterpret_cast<void **>(&g.commandList));
    if (FAILED(hr)) return hr;
    hr = g.commandList->Close();
    log_line("adapter_attach hr=0x%08lx interfaces=IDXGIFactory2,IDXGISwapChain1,IDXGISwapChain3,ID3D12Device,ID3D12CommandQueue",
             static_cast<unsigned long>(hr));
    return hr;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_RenderSource(const float color[4]) {
    if (!color || !g.swapChain3 || !g.commandList) return E_POINTER;
    std::memcpy(g.sourceColor, color, sizeof(g.sourceColor));
    if (!g.firstSourceRenderQpc) g.firstSourceRenderQpc = qpc_now();
    const UINT64 ordinal = g.sourceFramesRendered;
    const char kind = ordinal == 0 ? 'A' : ((ordinal & 1) ? 'B' : 'C');
    g.currentSourceKind = kind;
    const UINT64 renderQpc = qpc_now();
    log_line("source_render ordinal=%llu kind=%c framePair=%llu color=%.4f,%.4f,%.4f,%.4f qpc=%llu",
             static_cast<unsigned long long>(ordinal), kind,
             static_cast<unsigned long long>(g.currentFrame),
             color[0], color[1], color[2], color[3],
             static_cast<unsigned long long>(renderQpc));
    HRESULT hr = draw_diagnostic(color, kind, ordinal);
    if (SUCCEEDED(hr)) ++g.sourceFramesRendered;
    return hr;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_PresentSource() {
    if (!g.swapChain3 || !g.commandList) return E_POINTER;
    HRESULT hr;
    if (!g.havePrevious) {
        hr = capture_current(g.frameTextures[0]);
        if (FAILED(hr)) return hr;
        std::memcpy(g.previousColor, g.sourceColor, sizeof(g.previousColor));
        g.havePrevious = true;
        g.nextPresentTarget = g.firstSourceRenderQpc + output_interval();
        wait_until_qpc(g.nextPresentTarget, "source");
        hr = present_frame('A', false);
        if (SUCCEEDED(hr)) g.nextPresentTarget += output_interval();
        return hr;
    }

    // This spike measures presentation ownership and cadence only. A/B image
    // contents are controlled colors; G's base is their scalar midpoint, and
    // the GPU marker makes the extra diagnostic presentation recognizable.
    // Snapshot B into library-owned GPU memory before replacing the backbuffer with G.
    hr = capture_current(g.frameTextures[1]);
    if (FAILED(hr)) return hr;
    float midpoint[4];
    for (UINT i = 0; i < 4; ++i) midpoint[i] = 0.5f * g.previousColor[i] + 0.5f * g.sourceColor[i];
    log_line("synthetic framePair=%llu kind=G diagnosticOnly=1 baseFormula=0.5*A+0.5*B A=%.4f,%.4f,%.4f,%.4f B=%.4f,%.4f,%.4f,%.4f base=%.4f,%.4f,%.4f,%.4f gpuMarker=center_cross",
             static_cast<unsigned long long>(g.currentFrame),
             g.previousColor[0], g.previousColor[1], g.previousColor[2], g.previousColor[3],
             g.sourceColor[0], g.sourceColor[1], g.sourceColor[2], g.sourceColor[3],
             midpoint[0], midpoint[1], midpoint[2], midpoint[3]);
    hr = draw_diagnostic(midpoint, 'G', g.currentFrame);
    if (FAILED(hr)) return hr;
    wait_until_qpc(g.nextPresentTarget, "G");
    hr = present_frame('G', true);
    if (FAILED(hr)) return hr;
    g.nextPresentTarget += output_interval();
    hr = restore_saved_source(g.frameTextures[1]);
    if (FAILED(hr)) return hr;
    wait_until_qpc(g.nextPresentTarget, "source");
    hr = present_frame(g.currentSourceKind, (g.sourceFramesRendered & 1) != 0);
    if (FAILED(hr)) return hr;
    g.nextPresentTarget += output_interval();
    ID3D12Resource *oldPrevious = g.frameTextures[0];
    g.frameTextures[0] = g.frameTextures[1];
    g.frameTextures[1] = oldPrevious;
    std::memcpy(g.previousColor, g.sourceColor, sizeof(g.previousColor));
    ++g.currentFrame;
    return S_OK;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_ResizeBuffers(UINT width, UINT height) {
    if (!g.swapChain || !width || !height) return E_INVALIDARG;
    HRESULT hr = wait_for_idle();
    if (FAILED(hr)) return hr;
    // Reset and close the adapter command list so it no longer retains backbuffer references.
    if (g.commandList && g.backBuffers[0].allocator) {
        hr = g.commandList->Reset(g.backBuffers[0].allocator, nullptr);
        if (FAILED(hr)) return hr;
        hr = g.commandList->Close();
        if (FAILED(hr)) return hr;
    }
    release_buffers();
    release_frame_textures();
    hr = g.swapChain->ResizeBuffers(g.desc.BufferCount, width, height, g.desc.Format, g.desc.Flags);
    log_line("resize_buffers requested=%ux%u hr=0x%08lx", width, height, static_cast<unsigned long>(hr));
    if (FAILED(hr)) return hr;
    return acquire_buffers();
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_SetFullscreen(BOOL fullscreen) {
    if (!g.swapChain) return E_POINTER;
    HRESULT hr = wait_for_idle();
    if (FAILED(hr)) return hr;
    hr = g.swapChain->SetFullscreenState(fullscreen, nullptr);
    BOOL actual = FALSE;
    HRESULT queryHr = g.swapChain->GetFullscreenState(&actual, nullptr);
    log_line("fullscreen requested=%u hr=0x%08lx actual=%u queryHr=0x%08lx",
             fullscreen ? 1u : 0u, static_cast<unsigned long>(hr), actual ? 1u : 0u,
             static_cast<unsigned long>(queryHr));
    if (SUCCEEDED(hr)) {
        DXGI_SWAP_CHAIN_DESC1 desc{};
        if (SUCCEEDED(g.swapChain->GetDesc1(&desc)) && desc.Width && desc.Height)
            return FG_ResizeBuffers(desc.Width, desc.Height);
    }
    return hr;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_Finish() {
    if (!g.swapChain) return E_POINTER;
    HRESULT idleHr = wait_for_idle();
    log_line("finish_wait_idle hr=0x%08lx", static_cast<unsigned long>(idleHr));
    if (FAILED(idleHr)) return idleHr;
    DXGI_FRAME_STATISTICS stats{};
    HRESULT statsHr = g.swapChain->GetFrameStatistics(&stats);
    LARGE_INTEGER qpc{};
    QueryPerformanceCounter(&qpc);
    UINT lastCount = 0;
    HRESULT countHr = g.swapChain->GetLastPresentCount(&lastCount);
    log_line("summary sourceFrames=%llu syntheticFrames=%u presentCalls=%u lastCountHr=0x%08lx lastCount=%u frameStatsHr=0x%08lx monitorPresentCount=%u presentRefreshCount=%u syncRefreshCount=%u qpcEnd=%lld",
             static_cast<unsigned long long>(g.sourceFramesRendered),
             g.syntheticCalls, g.presentCalls, static_cast<unsigned long>(countHr), lastCount,
             static_cast<unsigned long>(statsHr), stats.PresentCount,
             stats.PresentRefreshCount, stats.SyncRefreshCount,
             static_cast<long long>(qpc.QuadPart));
    return statsHr;
}

extern "C" __declspec(dllexport) HRESULT WINAPI FG_Detach() {
    HRESULT idleHr = S_OK;
    if (g.queue && g.fence) idleHr = wait_for_idle();
    log_line("detach_wait_idle hr=0x%08lx", static_cast<unsigned long>(idleHr));
    release_buffers();
    release_frame_textures();
    release(g.commandList);
    for (UINT i = 0; i < 4; ++i) release(g.backBuffers[i].allocator);
    release(g.rtvHeap);
    release(g.fence);
    release(g.swapChain3);
    release(g.swapChain);
    release(g.queue);
    release(g.device);
    release(g.factory);
    if (g.fenceEvent) CloseHandle(g.fenceEvent);
    g.fenceEvent = nullptr;
    if (gLog) { std::fclose(gLog); gLog = nullptr; }
    g = AdapterState{};
    return idleHr;
}
