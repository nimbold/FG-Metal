#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_4.h>
#include <dxgi1_6.h>
#include <d3d12.h>

#include <cstdio>
#include <cstdarg>
#include <cstdlib>
#include <cstring>

namespace {

using CreateFactory2Fn = HRESULT (WINAPI *)(UINT, REFIID, void **);
using CreateDeviceFn = HRESULT (WINAPI *)(IUnknown *, D3D_FEATURE_LEVEL, REFIID, void **);

struct AdapterApi {
    HMODULE module = nullptr;
    HRESULT (WINAPI *setSyncInterval)(UINT) = nullptr;
    HRESULT (WINAPI *setOutputRate)(UINT) = nullptr;
    HRESULT (WINAPI *attach)(IDXGIFactory2 *, ID3D12Device *, ID3D12CommandQueue *, IDXGISwapChain1 *) = nullptr;
    HRESULT (WINAPI *renderSource)(const float[4]) = nullptr;
    HRESULT (WINAPI *presentSource)() = nullptr;
    HRESULT (WINAPI *resizeBuffers)(UINT, UINT) = nullptr;
    HRESULT (WINAPI *setFullscreen)(BOOL) = nullptr;
    HRESULT (WINAPI *finish)() = nullptr;
    HRESULT (WINAPI *detach)() = nullptr;
} gAdapter;

HWND gWindow = nullptr;
FILE *gLog = nullptr;
bool gQuit = false;
bool gFocused = false;
UINT gPendingWidth = 0;
UINT gPendingHeight = 0;

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

LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case WM_ACTIVATE:
        log_line("window activate hwnd=%p main=%u active=%u",
                 static_cast<void *>(hwnd), hwnd == gWindow ? 1u : 0u,
                 LOWORD(wParam) != WA_INACTIVE ? 1u : 0u);
        break;
    case WM_SETFOCUS:
        if (hwnd == gWindow) gFocused = true;
        log_line("window focus hwnd=%p main=%u focused=1 message=WM_SETFOCUS",
                 static_cast<void *>(hwnd), hwnd == gWindow ? 1u : 0u);
        break;
    case WM_KILLFOCUS:
        if (hwnd == gWindow) gFocused = false;
        log_line("window focus hwnd=%p main=%u focused=0 message=WM_KILLFOCUS",
                 static_cast<void *>(hwnd), hwnd == gWindow ? 1u : 0u);
        break;
    case WM_SIZE:
        if (wParam != SIZE_MINIMIZED) {
            gPendingWidth = LOWORD(lParam);
            gPendingHeight = HIWORD(lParam);
            log_line("window resize message=WM_SIZE width=%u height=%u", gPendingWidth, gPendingHeight);
        }
        break;
    case WM_CLOSE:
        if (hwnd == gWindow) gQuit = true;
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        if (hwnd == gWindow) PostQuitMessage(0);
        return 0;
    default:
        break;
    }
    return DefWindowProcW(hwnd, message, wParam, lParam);
}

void pump_messages() {
    MSG message;
    while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
        if (message.message == WM_QUIT) gQuit = true;
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

HRESULT exercise_focus_loss_recovery() {
    const HWND focusWindow = CreateWindowExW(
        WS_EX_TOOLWINDOW, L"FGDXGIPublicProbe", L"Framegen focus lifecycle probe",
        WS_OVERLAPPEDWINDOW, 1080, 120, 360, 240, nullptr, nullptr,
        GetModuleHandleW(nullptr), nullptr);
    if (!focusWindow) {
        const HRESULT hr = HRESULT_FROM_WIN32(GetLastError());
        log_line("lifecycle focus phase=create_helper hr=0x%08lx", static_cast<unsigned long>(hr));
        return hr;
    }

    log_line("lifecycle focus phase=before mainActive=%u mainFocused=%u activeWindow=%p focusWindow=%p foregroundWindow=%p",
             GetActiveWindow() == gWindow ? 1u : 0u, GetFocus() == gWindow ? 1u : 0u,
             static_cast<void *>(GetActiveWindow()), static_cast<void *>(GetFocus()),
             static_cast<void *>(GetForegroundWindow()));

    ShowWindow(focusWindow, SW_SHOW);
    const BOOL foregroundResult = SetForegroundWindow(focusWindow);
    SetActiveWindow(focusWindow);
    SetFocus(focusWindow);
    pump_messages();
    const bool focusTransferred = GetFocus() == focusWindow && GetFocus() != gWindow;
    log_line("lifecycle focus phase=loss requested=1 foregroundResult=%u helperFocused=%u mainFocused=%u activeWindow=%p focusWindow=%p foregroundWindow=%p",
             foregroundResult ? 1u : 0u, GetFocus() == focusWindow ? 1u : 0u,
             GetFocus() == gWindow ? 1u : 0u, static_cast<void *>(GetActiveWindow()),
             static_cast<void *>(GetFocus()), static_cast<void *>(GetForegroundWindow()));

    const BOOL restoreForegroundResult = SetForegroundWindow(gWindow);
    SetActiveWindow(gWindow);
    SetFocus(gWindow);
    pump_messages();
    const bool focusRecovered = GetFocus() == gWindow;
    log_line("lifecycle focus phase=recovery foregroundResult=%u mainFocused=%u focusRecovered=%u activeWindow=%p focusWindow=%p foregroundWindow=%p",
             restoreForegroundResult ? 1u : 0u, GetFocus() == gWindow ? 1u : 0u,
             focusRecovered ? 1u : 0u, static_cast<void *>(GetActiveWindow()),
             static_cast<void *>(GetFocus()), static_cast<void *>(GetForegroundWindow()));

    DestroyWindow(focusWindow);
    pump_messages();
    if (!focusTransferred || !focusRecovered) return E_FAIL;
    return S_OK;
}

bool load_adapter() {
    gAdapter.module = LoadLibraryW(L"fg_probe.dll");
    if (!gAdapter.module) {
        log_line("adapter_load name=fg_probe.dll error=%lu", GetLastError());
        return false;
    }
#define LOAD_API(field, exportName) \
    gAdapter.field = reinterpret_cast<decltype(gAdapter.field)>(GetProcAddress(gAdapter.module, exportName))
    LOAD_API(setSyncInterval, "FG_SetSyncInterval");
    LOAD_API(setOutputRate, "FG_SetOutputRate");
    LOAD_API(attach, "FG_Attach");
    LOAD_API(renderSource, "FG_RenderSource");
    LOAD_API(presentSource, "FG_PresentSource");
    LOAD_API(resizeBuffers, "FG_ResizeBuffers");
    LOAD_API(setFullscreen, "FG_SetFullscreen");
    LOAD_API(finish, "FG_Finish");
    LOAD_API(detach, "FG_Detach");
#undef LOAD_API
    if (!gAdapter.attach || !gAdapter.renderSource || !gAdapter.presentSource ||
        !gAdapter.setSyncInterval || !gAdapter.setOutputRate || !gAdapter.resizeBuffers || !gAdapter.setFullscreen ||
        !gAdapter.finish || !gAdapter.detach) {
        log_line("adapter_load missing_export=1");
        return false;
    }
    WCHAR path[MAX_PATH]{};
    const DWORD pathLength = GetModuleFileNameW(gAdapter.module, path, MAX_PATH);
    log_line("adapter_load name=fg_probe.dll mechanism=LoadLibraryW path_chars=%lu path=%ls",
             static_cast<unsigned long>(pathLength), path);
    return true;
}

UINT64 qpc_now() {
    LARGE_INTEGER value{};
    QueryPerformanceCounter(&value);
    return static_cast<UINT64>(value.QuadPart);
}

void wait_until_qpc(UINT64 target, UINT64 frequency, UINT frame) {
    for (;;) {
        const UINT64 now = qpc_now();
        if (now >= target) {
            log_line("source_schedule frame=%u targetQpc=%llu actualQpc=%llu lateTicks=%llu",
                     frame, static_cast<unsigned long long>(target),
                     static_cast<unsigned long long>(now),
                     static_cast<unsigned long long>(now - target));
            return;
        }
        const UINT64 remaining = target - now;
        if (remaining > frequency / 500) {
            const UINT64 sleepTicks = remaining - frequency / 500;
            const DWORD sleepMs = static_cast<DWORD>((sleepTicks * 1000) / frequency);
            Sleep(sleepMs ? sleepMs : 1);
        } else {
            Sleep(0);
        }
    }
}

template <typename T> void release_com(T *&value) {
    if (value) {
        value->Release();
        value = nullptr;
    }
}

float source_channel(UINT frame, UINT channel) {
    const UINT value = (frame * (37u + channel * 18u) + channel * 59u) % 192u;
    return static_cast<float>(32u + value) / 255.0f;
}

HRESULT create_test_window(HINSTANCE instance) {
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = window_proc;
    wc.hInstance = instance;
    wc.lpszClassName = L"FGDXGIPublicProbe";
    wc.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    if (!RegisterClassExW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return HRESULT_FROM_WIN32(GetLastError());
    gWindow = CreateWindowExW(0, wc.lpszClassName, L"Framegen DXGI public API probe",
                              WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT,
                              960, 540, nullptr, nullptr, instance, nullptr);
    if (!gWindow) return HRESULT_FROM_WIN32(GetLastError());
    ShowWindow(gWindow, SW_SHOW);
    UpdateWindow(gWindow);
    pump_messages();
    return S_OK;
}

HRESULT run_probe(UINT sourceFps, UINT seconds, bool lifecycle) {
    // A synchronized Present lets DXGI frame statistics describe monitor
    // presentations instead of a stream of interval-zero calls that may drop.
    const UINT syncInterval = 1;
    HMODULE dxgiModule = LoadLibraryW(L"dxgi.dll");
    HMODULE d3d12Module = LoadLibraryW(L"d3d12.dll");
    if (!dxgiModule || !d3d12Module) {
        log_line("runtime_dll_load dxgi=%p d3d12=%p error=%lu",
                 static_cast<void *>(dxgiModule), static_cast<void *>(d3d12Module), GetLastError());
        return E_FAIL;
    }
    auto createFactory = reinterpret_cast<CreateFactory2Fn>(GetProcAddress(dxgiModule, "CreateDXGIFactory2"));
    auto createDevice = reinterpret_cast<CreateDeviceFn>(GetProcAddress(d3d12Module, "D3D12CreateDevice"));
    if (!createFactory || !createDevice) {
        log_line("runtime_exports createFactory2=%p d3d12CreateDevice=%p",
                 reinterpret_cast<void *>(createFactory), reinterpret_cast<void *>(createDevice));
        return E_FAIL;
    }
    WCHAR dxgiPath[MAX_PATH]{};
    WCHAR d3d12Path[MAX_PATH]{};
    const DWORD dxgiPathLength = GetModuleFileNameW(dxgiModule, dxgiPath, MAX_PATH);
    const DWORD d3d12PathLength = GetModuleFileNameW(d3d12Module, d3d12Path, MAX_PATH);
    log_line("runtime_modules dxgi_chars=%lu dxgi=%ls d3d12_chars=%lu d3d12=%ls",
             static_cast<unsigned long>(dxgiPathLength), dxgiPath,
             static_cast<unsigned long>(d3d12PathLength), d3d12Path);

    IDXGIFactory2 *factory = nullptr;
    ID3D12Device *device = nullptr;
    ID3D12CommandQueue *queue = nullptr;
    IDXGISwapChain1 *swapChain = nullptr;
    HRESULT hr = createFactory(0, __uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory));
    log_line("call CreateDXGIFactory2 interface=IDXGIFactory2 hr=0x%08lx factory=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(factory));
    if (FAILED(hr)) return hr;
    hr = createDevice(nullptr, D3D_FEATURE_LEVEL_11_0, __uuidof(ID3D12Device), reinterpret_cast<void **>(&device));
    log_line("call D3D12CreateDevice interface=ID3D12Device hr=0x%08lx device=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(device));
    if (FAILED(hr)) return hr;

    D3D12_COMMAND_QUEUE_DESC queueDesc{};
    queueDesc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    queueDesc.Priority = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    hr = device->CreateCommandQueue(&queueDesc, __uuidof(ID3D12CommandQueue), reinterpret_cast<void **>(&queue));
    log_line("call ID3D12Device::CreateCommandQueue type=DIRECT hr=0x%08lx queue=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(queue));
    if (FAILED(hr)) return hr;

    DXGI_SWAP_CHAIN_DESC1 desc{};
    desc.Width = 960;
    desc.Height = 540;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 3;
    desc.Scaling = DXGI_SCALING_STRETCH;
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
    hr = factory->CreateSwapChainForHwnd(queue, gWindow, &desc, nullptr, nullptr, &swapChain);
    log_line("call IDXGIFactory2::CreateSwapChainForHwnd pDevice=ID3D12CommandQueue buffers=%u format=%u hr=0x%08lx swapchain=%p",
             desc.BufferCount, static_cast<unsigned>(desc.Format), static_cast<unsigned long>(hr),
             static_cast<void *>(swapChain));
    if (FAILED(hr)) return hr;

    IDXGISwapChain3 *swapChain3 = nullptr;
    IDXGISwapChain4 *swapChain4 = nullptr;
    HRESULT q3 = swapChain->QueryInterface(__uuidof(IDXGISwapChain3), reinterpret_cast<void **>(&swapChain3));
    HRESULT q4 = swapChain->QueryInterface(__uuidof(IDXGISwapChain4), reinterpret_cast<void **>(&swapChain4));
    log_line("swapchain_interfaces IDXGISwapChain1=yes IDXGISwapChain3_hr=0x%08lx IDXGISwapChain4_hr=0x%08lx currentIndex=%u",
             static_cast<unsigned long>(q3), static_cast<unsigned long>(q4),
             swapChain3 ? swapChain3->GetCurrentBackBufferIndex() : 0u);
    release_com(swapChain4);
    release_com(swapChain3);

    if (!load_adapter()) return E_FAIL;
    hr = gAdapter.attach(factory, device, queue, swapChain);
    log_line("adapter_attach hr=0x%08lx", static_cast<unsigned long>(hr));
    if (FAILED(hr)) return hr;
    hr = gAdapter.setSyncInterval(syncInterval);
    if (FAILED(hr)) return hr;
    hr = gAdapter.setOutputRate(sourceFps * 2);
    if (FAILED(hr)) return hr;
    log_line("experiment mode sourceFps=%u targetFps=%u syncInterval=%u durationSeconds=%u displayEvidence=DXGI_FRAME_STATISTICS_no_pixel_capture",
             sourceFps, sourceFps * 2, syncInterval, seconds);

    const UINT totalSourceFrames = sourceFps * seconds;
    LARGE_INTEGER qpcFrequency{}, qpcStart{}, qpcEnd{};
    QueryPerformanceFrequency(&qpcFrequency);
    QueryPerformanceCounter(&qpcStart);
    for (UINT frame = 0; frame < totalSourceFrames && !gQuit; ++frame) {
        if (frame) {
            const UINT64 target = static_cast<UINT64>(qpcStart.QuadPart)
                + (static_cast<UINT64>(qpcFrequency.QuadPart) * frame + sourceFps / 2) / sourceFps;
            wait_until_qpc(target, static_cast<UINT64>(qpcFrequency.QuadPart), frame);
        }
        pump_messages();
        const float color[4] = {
            source_channel(frame, 0), source_channel(frame, 1),
            source_channel(frame, 2), 1.0f
        };
        hr = gAdapter.renderSource(color);
        if (FAILED(hr)) break;
        hr = gAdapter.presentSource();
        if (FAILED(hr)) break;

        if (lifecycle && frame == totalSourceFrames / 2) {
            SetWindowPos(gWindow, nullptr, 80, 80, 1024, 576, SWP_NOZORDER | SWP_NOACTIVATE);
            pump_messages();
            hr = gAdapter.resizeBuffers(1024, 576);
            log_line("lifecycle resize windowed hr=0x%08lx", static_cast<unsigned long>(hr));
            if (FAILED(hr)) break;
        }
    }
    QueryPerformanceCounter(&qpcEnd);
    const double elapsedSeconds = qpcFrequency.QuadPart
        ? static_cast<double>(qpcEnd.QuadPart - qpcStart.QuadPart) / static_cast<double>(qpcFrequency.QuadPart)
        : 0.0;
    log_line("timing sourceFramesRequested=%u elapsedSeconds=%.6f qpcStart=%lld qpcEnd=%lld",
             totalSourceFrames, elapsedSeconds,
             static_cast<long long>(qpcStart.QuadPart), static_cast<long long>(qpcEnd.QuadPart));

    if (SUCCEEDED(hr) && lifecycle) {
        HRESULT focusHr = exercise_focus_loss_recovery();
        log_line("lifecycle focus exercise hr=0x%08lx", static_cast<unsigned long>(focusHr));
        if (FAILED(focusHr)) hr = focusHr;
    }

    if (SUCCEEDED(hr) && lifecycle) {
        hr = gAdapter.setFullscreen(TRUE);
        log_line("lifecycle fullscreen enter hr=0x%08lx", static_cast<unsigned long>(hr));
        if (SUCCEEDED(hr)) {
            Sleep(1000);
            hr = gAdapter.setFullscreen(FALSE);
            log_line("lifecycle fullscreen exit hr=0x%08lx", static_cast<unsigned long>(hr));
        }
        SetWindowPos(gWindow, nullptr, 80, 80, 960, 540, SWP_NOZORDER | SWP_NOACTIVATE);
        pump_messages();
        HRESULT resizeHr = gAdapter.resizeBuffers(960, 540);
        log_line("lifecycle resize restored hr=0x%08lx", static_cast<unsigned long>(resizeHr));
        if (SUCCEEDED(hr) && FAILED(resizeHr)) hr = resizeHr;
    }

    const HRESULT statsHr = gAdapter.finish();
    log_line("frame_statistics_final provider_hr=0x%08lx",
             static_cast<unsigned long>(statsHr));
    gAdapter.detach();
    if (gAdapter.module) FreeLibrary(gAdapter.module);
    gAdapter = AdapterApi{};
    release_com(swapChain);
    release_com(queue);
    release_com(device);
    release_com(factory);
    FreeLibrary(d3d12Module);
    FreeLibrary(dxgiModule);
    return hr;
}

} // namespace

int main(int argc, char **argv) {
    UINT sourceFps = 30;
    UINT seconds = 10;
    bool lifecycle = false;
    for (int i = 1; i < argc; ++i) {
        if (std::strncmp(argv[i], "--source=", 9) == 0) sourceFps = static_cast<UINT>(std::atoi(argv[i] + 9));
        else if (std::strncmp(argv[i], "--seconds=", 10) == 0) seconds = static_cast<UINT>(std::atoi(argv[i] + 10));
        else if (std::strcmp(argv[i], "--lifecycle") == 0) lifecycle = true;
    }
    if (sourceFps != 30 && sourceFps != 60) sourceFps = 30;
    if (!seconds || seconds > 600) seconds = 10;
    gLog = std::fopen("dxgi_probe.log", "a");
    log_line("---- new run sourceFps=%u seconds=%u lifecycle=%u ----", sourceFps, seconds, lifecycle ? 1u : 0u);
    HRESULT hr = create_test_window(GetModuleHandleW(nullptr));
    if (SUCCEEDED(hr)) hr = run_probe(sourceFps, seconds, lifecycle);
    log_line("run_result hr=0x%08lx focusAtExit=%u", static_cast<unsigned long>(hr), gFocused ? 1u : 0u);
    if (gWindow) DestroyWindow(gWindow);
    if (gLog) { std::fclose(gLog); gLog = nullptr; }
    return FAILED(hr) ? 1 : 0;
}
