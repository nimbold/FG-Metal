#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_6.h>
#include <d3d12.h>
#include <psapi.h>

#include <cstdarg>
#include <cstdio>
#include <cstdint>

namespace {

FILE *gLog = nullptr;
HWND gMainWindow = nullptr;
UINT gMainFocusLost = 0;
UINT gMainFocusGained = 0;

void log_line(const char *format, ...) {
    if (!gLog) gLog = std::fopen("unmodified_d3d12_app.log", "a");
    if (!gLog) return;
    va_list args;
    va_start(args, format);
    std::vfprintf(gLog, format, args);
    va_end(args);
    std::fputc('\n', gLog);
    std::fflush(gLog);
}

void log_interface_support(IUnknown *object, REFIID iid, const char *name) {
    IUnknown *result = nullptr;
    const HRESULT hr = object->QueryInterface(iid, reinterpret_cast<void **>(&result));
    log_line("interface name=%s hr=0x%08lx ptr=%p", name,
             static_cast<unsigned long>(hr), static_cast<void *>(result));
    if (result) result->Release();
}

template <typename T> void release(T *&value) {
    if (value) {
        value->Release();
        value = nullptr;
    }
}

UINT64 qpc_now() {
    LARGE_INTEGER value{};
    QueryPerformanceCounter(&value);
    return static_cast<UINT64>(value.QuadPart);
}

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (window == gMainWindow && message == WM_KILLFOCUS) {
        ++gMainFocusLost;
        log_line("window_event kind=WM_KILLFOCUS count=%u", gMainFocusLost);
    } else if (window == gMainWindow && message == WM_SETFOCUS) {
        ++gMainFocusGained;
        log_line("window_event kind=WM_SETFOCUS count=%u", gMainFocusGained);
    }
    if (message == WM_DESTROY) {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

HRESULT create_frame_resources(ID3D12Device *device, IDXGISwapChain1 *swapChain,
                               const DXGI_SWAP_CHAIN_DESC1 &swapDesc,
                               ID3D12DescriptorHeap **rtvHeap, ID3D12Resource **buffers,
                               ID3D12CommandAllocator **allocators,
                               ID3D12GraphicsCommandList **commandList, UINT *rtvStride) {
    D3D12_DESCRIPTOR_HEAP_DESC heapDesc{};
    heapDesc.Type = D3D12_DESCRIPTOR_HEAP_TYPE_RTV;
    heapDesc.NumDescriptors = swapDesc.BufferCount;
    HRESULT hr = device->CreateDescriptorHeap(&heapDesc, IID_ID3D12DescriptorHeap,
                                              reinterpret_cast<void **>(rtvHeap));
    if (FAILED(hr)) return hr;
    *rtvStride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    D3D12_CPU_DESCRIPTOR_HANDLE handle = (*rtvHeap)->GetCPUDescriptorHandleForHeapStart();
    for (UINT i = 0; i < swapDesc.BufferCount; ++i) {
        hr = swapChain->GetBuffer(i, IID_ID3D12Resource, reinterpret_cast<void **>(&buffers[i]));
        if (FAILED(hr)) return hr;
        device->CreateRenderTargetView(buffers[i], nullptr, handle);
        hr = device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                            IID_ID3D12CommandAllocator,
                                            reinterpret_cast<void **>(&allocators[i]));
        if (FAILED(hr)) return hr;
        handle.ptr += *rtvStride;
    }
    hr = device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocators[0], nullptr,
                                   IID_ID3D12GraphicsCommandList,
                                   reinterpret_cast<void **>(commandList));
    if (SUCCEEDED(hr)) hr = (*commandList)->Close();
    return hr;
}

HRESULT flush_queue(ID3D12CommandQueue *queue, ID3D12Fence *fence, HANDLE event, UINT64 *value) {
    const UINT64 nextValue = ++(*value);
    HRESULT hr = queue->Signal(fence, nextValue);
    if (FAILED(hr)) return hr;
    hr = fence->SetEventOnCompletion(nextValue, event);
    if (FAILED(hr)) return hr;
    return WaitForSingleObject(event, INFINITE) == WAIT_OBJECT_0 ? S_OK : E_FAIL;
}

void release_frame_resources(ID3D12GraphicsCommandList *&commandList,
                             ID3D12DescriptorHeap *&rtvHeap, ID3D12Resource **buffers,
                             ID3D12CommandAllocator **allocators, UINT bufferCount) {
    release(commandList);
    release(rtvHeap);
    for (UINT i = 0; i < bufferCount; ++i) {
        release(allocators[i]);
        release(buffers[i]);
    }
}

HRESULT transition_fullscreen_and_rebind(IDXGISwapChain1 *swapChain, ID3D12Device *device,
                                         ID3D12CommandQueue *queue, ID3D12Fence *fence,
                                         IDXGIOutput *targetOutput,
                                         HANDLE fenceEvent, UINT64 *nextFence,
                                         ID3D12GraphicsCommandList **commandList,
                                         ID3D12DescriptorHeap **rtvHeap,
                                         ID3D12Resource **buffers,
                                         ID3D12CommandAllocator **allocators,
                                         UINT64 *bufferFence, DXGI_SWAP_CHAIN_DESC1 *swapDesc,
                                         UINT *rtvStride, WINBOOL target,
                                         const char *label, UINT frame) {
    HRESULT hr = flush_queue(queue, fence, fenceEvent, nextFence);
    log_line("lifecycle event=%s_prepare frame=%u flush_hr=0x%08lx",
             label, frame, static_cast<unsigned long>(hr));
    if (FAILED(hr)) return hr;
    release_frame_resources(*commandList, *rtvHeap, buffers, allocators,
                            swapDesc->BufferCount);
    ZeroMemory(bufferFence, sizeof(UINT64) * 3);

    const HRESULT setHr = swapChain->SetFullscreenState(target, targetOutput);
    WINBOOL actual = !target;
    const HRESULT queryHr = swapChain->GetFullscreenState(&actual, nullptr);
    const HRESULT resizeHr = FAILED(setHr)
        ? setHr
        : swapChain->ResizeBuffers(swapDesc->BufferCount, swapDesc->Width,
                                   swapDesc->Height, swapDesc->Format, 0);
    log_line("lifecycle event=%s_transition frame=%u set_hr=0x%08lx "
             "query_hr=0x%08lx fullscreen=%u resize_hr=0x%08lx",
             label, frame, static_cast<unsigned long>(setHr),
             static_cast<unsigned long>(queryHr), actual,
             static_cast<unsigned long>(resizeHr));
    if (FAILED(setHr)) return setHr;
    if (FAILED(resizeHr)) return resizeHr;
    if (FAILED(queryHr)) return queryHr;
    if (actual != target) return E_FAIL;

    hr = swapChain->GetDesc1(swapDesc);
    if (SUCCEEDED(hr)) {
        hr = create_frame_resources(device, swapChain, *swapDesc, rtvHeap, buffers,
                                    allocators, commandList, rtvStride);
    }
    log_line("lifecycle event=%s_rebind frame=%u hr=0x%08lx size=%ux%u",
             label, frame, static_cast<unsigned long>(hr), swapDesc->Width,
             swapDesc->Height);
    return hr;
}

HRESULT run_app() {
    HRESULT hr = S_OK;
    IDXGIFactory2 *factory = nullptr;
    ID3D12Device *device = nullptr;
    ID3D12CommandQueue *queue = nullptr;
    IDXGISwapChain1 *swapChain = nullptr;
    IDXGISwapChain3 *swapChain3 = nullptr;
    IDXGIOutput *targetOutput = nullptr;
    ID3D12DescriptorHeap *rtvHeap = nullptr;
    ID3D12GraphicsCommandList *commandList = nullptr;
    ID3D12Fence *fence = nullptr;
    HANDLE fenceEvent = nullptr;
    ID3D12Resource *buffers[3]{};
    ID3D12CommandAllocator *allocators[3]{};
    UINT64 bufferFence[3]{};
    UINT64 nextFence = 0;
    HWND window = nullptr;
    HWND focusWindow = nullptr;
    WNDCLASSW windowClass{};
    D3D12_COMMAND_QUEUE_DESC queueDesc{};
    DXGI_SWAP_CHAIN_DESC1 swapDesc{};
    D3D12_CPU_DESCRIPTOR_HANDLE handle{};
    LARGE_INTEGER frequency{};
    DXGI_FRAME_STATISTICS frameStats{};
    HRESULT statsHr = S_OK;
    UINT lastPresentCount = 0;
    HRESULT lastPresentHr = S_OK;
    PROCESS_MEMORY_COUNTERS memoryCounters{};
    SIZE_T maxWorkingSet = 0;
    SIZE_T maxPagefile = 0;
    HRESULT memoryHr = E_FAIL;
    UINT rtvStride = 0;
    UINT64 start = 0;
    UINT64 framePeriod = 0;

    WCHAR dxgiPath[MAX_PATH]{};
    HMODULE dxgiModule = GetModuleHandleW(L"dxgi.dll");
    const DWORD pathLength = dxgiModule ? GetModuleFileNameW(dxgiModule, dxgiPath, MAX_PATH) : 0;
    log_line("start dxgi_module=%p dxgi_path_chars=%lu dxgi_path=%ls",
             static_cast<void *>(dxgiModule), static_cast<unsigned long>(pathLength), dxgiPath);

    windowClass.lpfnWndProc = window_proc;
    windowClass.hInstance = GetModuleHandleW(nullptr);
    windowClass.lpszClassName = L"FGMetalStep8DUnmodifiedApp";
    RegisterClassW(&windowClass);
    window = CreateWindowExW(0, windowClass.lpszClassName, L"Unmodified D3D12 DXGI probe",
                             WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT, 960, 600,
                             nullptr, nullptr, windowClass.hInstance, nullptr);
    if (!window) {
        log_line("failure operation=CreateWindowExW error=%lu", GetLastError());
        hr = HRESULT_FROM_WIN32(GetLastError());
        goto cleanup;
    }
    gMainWindow = window;
    focusWindow = CreateWindowExW(0, windowClass.lpszClassName, L"Focus recovery helper",
                                  WS_OVERLAPPEDWINDOW, 40, 40, 320, 220,
                                  nullptr, nullptr, windowClass.hInstance, nullptr);
    if (!focusWindow) {
        hr = HRESULT_FROM_WIN32(GetLastError());
        log_line("failure operation=CreateWindowExW_focus_helper hr=0x%08lx",
                 static_cast<unsigned long>(hr));
        goto cleanup;
    }
    ShowWindow(window, SW_SHOW);

    hr = CreateDXGIFactory2(0, __uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    log_line("call=CreateDXGIFactory2 hr=0x%08lx factory=%p", static_cast<unsigned long>(hr),
             static_cast<void *>(factory));
    if (FAILED(hr)) goto cleanup;
    log_interface_support(factory, __uuidof(IDXGIFactory), "IDXGIFactory");
    log_interface_support(factory, __uuidof(IDXGIFactory1), "IDXGIFactory1");
    log_interface_support(factory, __uuidof(IDXGIFactory2), "IDXGIFactory2");
    log_interface_support(factory, __uuidof(IDXGIFactory3), "IDXGIFactory3");
    log_interface_support(factory, __uuidof(IDXGIFactory4), "IDXGIFactory4");
    log_interface_support(factory, __uuidof(IDXGIFactory5), "IDXGIFactory5");
    log_interface_support(factory, __uuidof(IDXGIFactory6), "IDXGIFactory6");
    log_interface_support(factory, __uuidof(IDXGIFactory7), "IDXGIFactory7");
    log_interface_support(factory, __uuidof(IDXGIFactoryMedia), "IDXGIFactoryMedia");

    hr = D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device),
                           reinterpret_cast<void **>(&device));
    log_line("call=D3D12CreateDevice hr=0x%08lx device=%p", static_cast<unsigned long>(hr),
             static_cast<void *>(device));
    if (FAILED(hr)) goto cleanup;

    queueDesc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    hr = device->CreateCommandQueue(&queueDesc, __uuidof(ID3D12CommandQueue),
                                    reinterpret_cast<void **>(&queue));
    log_line("call=CreateCommandQueue hr=0x%08lx queue=%p", static_cast<unsigned long>(hr),
             static_cast<void *>(queue));
    if (FAILED(hr)) goto cleanup;

    swapDesc.Width = 960;
    swapDesc.Height = 540;
    swapDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    swapDesc.SampleDesc.Count = 1;
    swapDesc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swapDesc.BufferCount = 3;
    swapDesc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    hr = factory->CreateSwapChainForHwnd(queue, window, &swapDesc, nullptr, nullptr, &swapChain);
    log_line("call=CreateSwapChainForHwnd hr=0x%08lx swapchain=%p", static_cast<unsigned long>(hr),
             static_cast<void *>(swapChain));
    if (FAILED(hr)) goto cleanup;
    factory->MakeWindowAssociation(window, DXGI_MWA_NO_ALT_ENTER);
    {
        const HRESULT outputHr = swapChain->GetContainingOutput(&targetOutput);
        log_line("call=GetContainingOutput hr=0x%08lx output=%p",
                 static_cast<unsigned long>(outputHr), static_cast<void *>(targetOutput));
    }

    hr = swapChain->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&swapChain3));
    log_line("call=QueryInterface_IDXGISwapChain3 hr=0x%08lx swapchain3=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(swapChain3));
    if (FAILED(hr)) goto cleanup;
    log_interface_support(swapChain, __uuidof(IDXGISwapChain), "IDXGISwapChain");
    log_interface_support(swapChain, __uuidof(IDXGISwapChain1), "IDXGISwapChain1");
    log_interface_support(swapChain, __uuidof(IDXGISwapChain2), "IDXGISwapChain2");
    log_interface_support(swapChain, __uuidof(IDXGISwapChain3), "IDXGISwapChain3");
    log_interface_support(swapChain, __uuidof(IDXGISwapChain4), "IDXGISwapChain4");

    hr = create_frame_resources(device, swapChain, swapDesc, &rtvHeap, buffers,
                                allocators, &commandList, &rtvStride);
    if (FAILED(hr)) goto cleanup;
    hr = device->CreateFence(0, D3D12_FENCE_FLAG_NONE, __uuidof(ID3D12Fence),
                             reinterpret_cast<void **>(&fence));
    if (FAILED(hr)) goto cleanup;
    fenceEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!fenceEvent) {
        hr = HRESULT_FROM_WIN32(GetLastError());
        goto cleanup;
    }

    QueryPerformanceFrequency(&frequency);
    log_line("timing qpc_frequency=%lld target_fps=30", static_cast<long long>(frequency.QuadPart));
    start = qpc_now();
    framePeriod = static_cast<UINT64>(frequency.QuadPart / 30);
    for (UINT frame = 0; frame < 90; ++frame) {
        if (frame == 30) {
            SetWindowPos(window, nullptr, 80, 80, 820, 500,
                         SWP_NOZORDER | SWP_NOACTIVATE);
            hr = flush_queue(queue, fence, fenceEvent, &nextFence);
            log_line("lifecycle event=window_resize frame=%u flush_hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
            release_frame_resources(commandList, rtvHeap, buffers, allocators,
                                    swapDesc.BufferCount);
            ZeroMemory(bufferFence, sizeof(bufferFence));
            swapDesc.Width = 800;
            swapDesc.Height = 450;
            hr = swapChain->ResizeBuffers(swapDesc.BufferCount, swapDesc.Width,
                                          swapDesc.Height, swapDesc.Format, 0);
            log_line("lifecycle event=ResizeBuffers frame=%u hr=0x%08lx size=%ux%u",
                     frame, static_cast<unsigned long>(hr), swapDesc.Width, swapDesc.Height);
            if (FAILED(hr)) goto cleanup;
            hr = create_frame_resources(device, swapChain, swapDesc, &rtvHeap, buffers,
                                        allocators, &commandList, &rtvStride);
            log_line("lifecycle event=rebind_after_resize frame=%u hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
        }
        if (frame == 45) {
            hr = transition_fullscreen_and_rebind(swapChain, device, queue, fence,
                                                  targetOutput, fenceEvent, &nextFence, &commandList,
                                                  &rtvHeap, buffers, allocators,
                                                  bufferFence, &swapDesc, &rtvStride,
                                                  TRUE, "fullscreen_enter", frame);
            if (FAILED(hr)) goto cleanup;
            hr = transition_fullscreen_and_rebind(swapChain, device, queue, fence,
                                                  targetOutput, fenceEvent, &nextFence, &commandList,
                                                  &rtvHeap, buffers, allocators,
                                                  bufferFence, &swapDesc, &rtvStride,
                                                  FALSE, "fullscreen_leave", frame);
            if (FAILED(hr)) goto cleanup;
        }
        if (frame == 60) {
            hr = flush_queue(queue, fence, fenceEvent, &nextFence);
            log_line("lifecycle event=ResizeBuffers1_prepare frame=%u flush_hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
            release_frame_resources(commandList, rtvHeap, buffers, allocators,
                                    swapDesc.BufferCount);
            ZeroMemory(bufferFence, sizeof(bufferFence));
            const UINT nodeMasks[] = {0, 0, 0};
            IUnknown *presentQueues[] = {queue, queue, queue};
            hr = swapChain3->ResizeBuffers1(swapDesc.BufferCount, swapDesc.Width,
                                            swapDesc.Height, swapDesc.Format, 0,
                                            nodeMasks, presentQueues);
            log_line("lifecycle event=ResizeBuffers1 frame=%u hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) {
                hr = swapChain->ResizeBuffers(swapDesc.BufferCount, swapDesc.Width,
                                              swapDesc.Height, swapDesc.Format, 0);
                log_line("lifecycle event=ResizeBuffers1_fallback frame=%u hr=0x%08lx",
                         frame, static_cast<unsigned long>(hr));
                if (FAILED(hr)) goto cleanup;
            }
            hr = create_frame_resources(device, swapChain, swapDesc, &rtvHeap, buffers,
                                        allocators, &commandList, &rtvStride);
            log_line("lifecycle event=rebind_after_resize1 frame=%u hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
        }
        if (frame == 75) {
            ShowWindow(window, SW_MINIMIZE);
            ShowWindow(focusWindow, SW_SHOW);
            const BOOL helperForegroundHr = SetForegroundWindow(focusWindow);
            SetFocus(focusWindow);
            const BOOL helperFocused = GetFocus() == focusWindow;
            ShowWindow(window, SW_RESTORE);
            const BOOL foregroundHr = SetForegroundWindow(window);
            SetFocus(window);
            const BOOL mainFocused = GetFocus() == window;
            ShowWindow(focusWindow, SW_HIDE);
            MSG focusMessage{};
            while (PeekMessageW(&focusMessage, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&focusMessage);
                DispatchMessageW(&focusMessage);
            }
            log_line("lifecycle event=focus_recovery frame=%u helper_foreground=%u "
                     "helper_focused=%u main_foreground=%u main_focused=%u "
                     "main_focus_lost=%u main_focus_gained=%u",
                     frame, helperForegroundHr, helperFocused, foregroundHr, mainFocused,
                     gMainFocusLost, gMainFocusGained);
        }
        if (frame == 80) {
            hr = flush_queue(queue, fence, fenceEvent, &nextFence);
            log_line("lifecycle event=recreate_prepare frame=%u flush_hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
            release_frame_resources(commandList, rtvHeap, buffers, allocators,
                                    swapDesc.BufferCount);
            ZeroMemory(bufferFence, sizeof(bufferFence));
            release(swapChain3);
            release(swapChain);
            hr = factory->CreateSwapChainForHwnd(queue, window, &swapDesc, nullptr,
                                                  nullptr, &swapChain);
            log_line("lifecycle event=swapchain_recreate frame=%u create_hr=0x%08lx chain=%p",
                     frame, static_cast<unsigned long>(hr), static_cast<void *>(swapChain));
            if (FAILED(hr)) goto cleanup;
            hr = swapChain->QueryInterface(__uuidof(IDXGISwapChain3),
                                           reinterpret_cast<void **>(&swapChain3));
            if (FAILED(hr)) goto cleanup;
            hr = create_frame_resources(device, swapChain, swapDesc, &rtvHeap, buffers,
                                        allocators, &commandList, &rtvStride);
            log_line("lifecycle event=rebind_after_recreate frame=%u hr=0x%08lx",
                     frame, static_cast<unsigned long>(hr));
            if (FAILED(hr)) goto cleanup;
        }
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        const UINT index = swapChain3->GetCurrentBackBufferIndex();
        if (bufferFence[index] && fence->GetCompletedValue() < bufferFence[index]) {
            hr = fence->SetEventOnCompletion(bufferFence[index], fenceEvent);
            if (FAILED(hr) || WaitForSingleObject(fenceEvent, INFINITE) != WAIT_OBJECT_0) {
                if (SUCCEEDED(hr)) hr = E_FAIL;
                goto cleanup;
            }
        }
        hr = allocators[index]->Reset();
        if (FAILED(hr)) goto cleanup;
        hr = commandList->Reset(allocators[index], nullptr);
        if (FAILED(hr)) goto cleanup;

        D3D12_RESOURCE_BARRIER toTarget{};
        toTarget.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        toTarget.Transition.pResource = buffers[index];
        toTarget.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        toTarget.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        toTarget.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        commandList->ResourceBarrier(1, &toTarget);
        handle = rtvHeap->GetCPUDescriptorHandleForHeapStart();
        handle.ptr += static_cast<SIZE_T>(index) * rtvStride;
        commandList->OMSetRenderTargets(1, &handle, FALSE, nullptr);
        const float phase = static_cast<float>(frame % 90) / 89.0f;
        const float color[] = {phase, 0.20f, 1.0f - phase, 1.0f};
        commandList->ClearRenderTargetView(handle, color, 0, nullptr);
        D3D12_RESOURCE_BARRIER toPresent = toTarget;
        toPresent.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        toPresent.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        commandList->ResourceBarrier(1, &toPresent);
        hr = commandList->Close();
        if (FAILED(hr)) goto cleanup;
        ID3D12CommandList *lists[] = {commandList};
        queue->ExecuteCommandLists(1, lists);
        const UINT64 fenceValue = ++nextFence;
        hr = queue->Signal(fence, fenceValue);
        if (FAILED(hr)) goto cleanup;
        bufferFence[index] = fenceValue;

        const UINT64 callQpc = qpc_now();
        if ((frame % 2) == 0) {
            DXGI_PRESENT_PARAMETERS parameters{};
            hr = swapChain->Present1(1, 0, &parameters);
        } else {
            hr = swapChain->Present(1, 0);
        }
        const UINT64 returnQpc = qpc_now();
        log_line("present frame=%u index=%u qpc=%llu duration_ticks=%llu hr=0x%08lx",
                 frame, index, static_cast<unsigned long long>(callQpc),
                 static_cast<unsigned long long>(returnQpc - callQpc), static_cast<unsigned long>(hr));
        if (FAILED(hr)) goto cleanup;
        if ((frame % 30) == 0) {
            memoryCounters.cb = sizeof(memoryCounters);
            memoryHr = GetProcessMemoryInfo(GetCurrentProcess(), &memoryCounters,
                                            sizeof(memoryCounters)) ? S_OK : HRESULT_FROM_WIN32(GetLastError());
            if (SUCCEEDED(memoryHr)) {
                if (memoryCounters.WorkingSetSize > maxWorkingSet)
                    maxWorkingSet = memoryCounters.WorkingSetSize;
                if (memoryCounters.PagefileUsage > maxPagefile)
                    maxPagefile = memoryCounters.PagefileUsage;
            }
        }
        const UINT64 deadline = start + static_cast<UINT64>(frame + 1) * framePeriod;
        while (qpc_now() < deadline) Sleep(1);
    }
    log_line("complete frames=90 elapsed_ticks=%llu", static_cast<unsigned long long>(qpc_now() - start));
    statsHr = swapChain->GetFrameStatistics(&frameStats);
    log_line("frame_statistics hr=0x%08lx present_count=%u present_refresh=%u sync_refresh=%u",
             static_cast<unsigned long>(statsHr), frameStats.PresentCount,
             frameStats.PresentRefreshCount, frameStats.SyncRefreshCount);
    lastPresentHr = swapChain->GetLastPresentCount(&lastPresentCount);
    log_line("last_present_count hr=0x%08lx count=%u", static_cast<unsigned long>(lastPresentHr),
             lastPresentCount);
    log_line("memory_sample hr=0x%08lx current_working_set=%llu current_private=%llu "
             "max_sampled_working_set=%llu max_sampled_private=%llu sample_interval_frames=30",
             static_cast<unsigned long>(memoryHr),
             static_cast<unsigned long long>(memoryCounters.WorkingSetSize),
             static_cast<unsigned long long>(memoryCounters.PagefileUsage),
             static_cast<unsigned long long>(maxWorkingSet),
             static_cast<unsigned long long>(maxPagefile));

cleanup:
    if (FAILED(hr)) log_line("failure operation=run_app hr=0x%08lx", static_cast<unsigned long>(hr));
    if (queue && fence && nextFence) {
        const UINT64 value = ++nextFence;
        if (SUCCEEDED(queue->Signal(fence, value)) && SUCCEEDED(fence->SetEventOnCompletion(value, fenceEvent)))
            WaitForSingleObject(fenceEvent, INFINITE);
    }
    if (fenceEvent) CloseHandle(fenceEvent);
    release(commandList);
    for (UINT i = 0; i < 3; ++i) {
        release(allocators[i]);
        release(buffers[i]);
    }
    release(fence);
    release(rtvHeap);
    release(targetOutput);
    release(swapChain3);
    release(swapChain);
    release(queue);
    release(device);
    release(factory);
    if (focusWindow) DestroyWindow(focusWindow);
    if (window) DestroyWindow(window);
    gMainWindow = nullptr;
    if (gLog) {
        std::fclose(gLog);
        gLog = nullptr;
    }
    return hr;
}

} // namespace

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    return SUCCEEDED(run_app()) ? 0 : 1;
}
