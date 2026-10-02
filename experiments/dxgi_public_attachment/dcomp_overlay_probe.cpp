#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_6.h>
#include <d3d12.h>
#include <dcomp.h>

#include <cstdarg>
#include <cstdio>

namespace {

FILE *gLog = nullptr;

void log_line(const char *format, ...) {
    if (!gLog) gLog = std::fopen("dcomp_overlay_probe.log", "a");
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

LRESULT CALLBACK window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_DESTROY) {
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

HRESULT run_probe() {
    HRESULT hr = E_FAIL;
    IDXGIFactory2 *factory = nullptr;
    ID3D12Device *device = nullptr;
    ID3D12CommandQueue *queue = nullptr;
    IDXGISwapChain1 *gameChain = nullptr;
    IDXGISwapChain1 *compositionChain = nullptr;
    IDCompositionDesktopDevice *composition = nullptr;
    IDCompositionTarget *target = nullptr;
    IDCompositionVisual2 *visual = nullptr;
    HWND window = nullptr;
    WNDCLASSW windowClass{};

    windowClass.lpfnWndProc = window_proc;
    windowClass.hInstance = GetModuleHandleW(nullptr);
    windowClass.lpszClassName = L"FGMetalStep8D1CompositionProbe";
    RegisterClassW(&windowClass);
    window = CreateWindowExW(0, windowClass.lpszClassName,
                             L"DXGI composition overlay probe", WS_OVERLAPPEDWINDOW,
                             CW_USEDEFAULT, CW_USEDEFAULT, 960, 600, nullptr, nullptr,
                             windowClass.hInstance, nullptr);
    if (!window) {
        hr = HRESULT_FROM_WIN32(GetLastError());
        log_line("step=CreateWindow hr=0x%08lx", static_cast<unsigned long>(hr));
        goto cleanup;
    }
    ShowWindow(window, SW_SHOW);
    hr = CreateDXGIFactory2(0, IID_IDXGIFactory2, reinterpret_cast<void **>(&factory));
    log_line("step=CreateDXGIFactory2 hr=0x%08lx factory=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(factory));
    if (FAILED(hr)) goto cleanup;
    hr = D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_ID3D12Device,
                           reinterpret_cast<void **>(&device));
    log_line("step=D3D12CreateDevice hr=0x%08lx device=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(device));
    if (FAILED(hr)) goto cleanup;
    {
        D3D12_COMMAND_QUEUE_DESC queueDesc{};
        queueDesc.Type = D3D12_COMMAND_LIST_TYPE_DIRECT;
        hr = device->CreateCommandQueue(&queueDesc, IID_ID3D12CommandQueue,
                                        reinterpret_cast<void **>(&queue));
    }
    log_line("step=CreateCommandQueue hr=0x%08lx queue=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(queue));
    if (FAILED(hr)) goto cleanup;
    {
        DXGI_SWAP_CHAIN_DESC1 desc{};
        desc.Width = 960;
        desc.Height = 540;
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        desc.BufferCount = 2;
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
        hr = factory->CreateSwapChainForHwnd(queue, window, &desc, nullptr, nullptr, &gameChain);
    }
    log_line("step=CreateSwapChainForHwnd hr=0x%08lx chain=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(gameChain));
    if (FAILED(hr)) goto cleanup;

    {
        HMODULE dcompModule = LoadLibraryW(L"dcomp.dll");
        using CreateDeviceProc = HRESULT (WINAPI *)(IUnknown *, REFIID, void **);
        auto createDevice3 = dcompModule
            ? reinterpret_cast<CreateDeviceProc>(
                  GetProcAddress(dcompModule, "DCompositionCreateDevice3"))
            : nullptr;
        auto createDevice2 = dcompModule
            ? reinterpret_cast<CreateDeviceProc>(
                  GetProcAddress(dcompModule, "DCompositionCreateDevice2"))
            : nullptr;
        log_line("step=LoadDComp module=%p create_device3=%p create_device2=%p",
                 static_cast<void *>(dcompModule), reinterpret_cast<void *>(createDevice3),
                 reinterpret_cast<void *>(createDevice2));
        if (!createDevice3 && !createDevice2) {
            hr = HRESULT_FROM_WIN32(ERROR_PROC_NOT_FOUND);
            goto cleanup;
        }
        if (createDevice3) {
            hr = createDevice3(nullptr, __uuidof(IDCompositionDesktopDevice),
                               reinterpret_cast<void **>(&composition));
            log_line("step=DCompositionCreateDevice3 hr=0x%08lx device=%p",
                     static_cast<unsigned long>(hr), static_cast<void *>(composition));
        }
        if (FAILED(hr) && createDevice2) {
            hr = createDevice2(nullptr, __uuidof(IDCompositionDesktopDevice),
                               reinterpret_cast<void **>(&composition));
            log_line("step=DCompositionCreateDevice2_fallback hr=0x%08lx device=%p",
                     static_cast<unsigned long>(hr), static_cast<void *>(composition));
        }
        if (FAILED(hr)) goto cleanup;
    }
    hr = composition->CreateTargetForHwnd(window, TRUE, &target);
    log_line("step=CreateTargetForHwnd hr=0x%08lx target=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(target));
    if (FAILED(hr)) goto cleanup;
    hr = composition->CreateVisual(&visual);
    log_line("step=CreateVisual hr=0x%08lx visual=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(visual));
    if (FAILED(hr)) goto cleanup;

    {
        DXGI_SWAP_CHAIN_DESC1 desc{};
        desc.Width = 960;
        desc.Height = 540;
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        desc.SampleDesc.Count = 1;
        desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
        desc.BufferCount = 2;
        desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL;
        desc.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED;
        hr = factory->CreateSwapChainForComposition(queue, &desc, nullptr, &compositionChain);
    }
    log_line("step=CreateSwapChainForComposition hr=0x%08lx chain=%p",
             static_cast<unsigned long>(hr), static_cast<void *>(compositionChain));
    if (FAILED(hr)) goto cleanup;
    hr = visual->SetContent(compositionChain);
    log_line("step=VisualSetContent hr=0x%08lx", static_cast<unsigned long>(hr));
    if (FAILED(hr)) goto cleanup;
    hr = target->SetRoot(visual);
    log_line("step=TargetSetRoot hr=0x%08lx", static_cast<unsigned long>(hr));
    if (FAILED(hr)) goto cleanup;
    hr = composition->Commit();
    log_line("step=Commit hr=0x%08lx", static_cast<unsigned long>(hr));
    if (FAILED(hr)) goto cleanup;
    for (UINT frame = 0; frame < 12; ++frame) {
        hr = compositionChain->Present(1, 0);
        if (FAILED(hr)) break;
        composition->WaitForCommitCompletion();
    }
    log_line("result=composition_present_loop hr=0x%08lx frames=12",
             static_cast<unsigned long>(hr));

cleanup:
    if (composition) composition->Commit();
    release(visual);
    release(target);
    release(composition);
    release(compositionChain);
    release(gameChain);
    release(queue);
    release(device);
    release(factory);
    if (window) DestroyWindow(window);
    if (gLog) {
        std::fclose(gLog);
        gLog = nullptr;
    }
    return hr;
}

} // namespace

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    return SUCCEEDED(run_probe()) ? 0 : 1;
}
