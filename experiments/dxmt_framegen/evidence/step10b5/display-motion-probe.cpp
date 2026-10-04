#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi.h>
#include <dxgi1_4.h>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <string>

namespace {
constexpr int kClientWidth = 640;
constexpr int kClientHeight = 360;
constexpr ULONGLONG kBaselineRunDurationMs = 10000;
constexpr ULONGLONG kFramegenRunDurationMs = 65000;

IDXGISwapChain* g_swap_chain = nullptr;
IDXGISwapChain3* g_swap_chain3 = nullptr;
ID3D11Device* g_device = nullptr;
ID3D11DeviceContext* g_context = nullptr;
ID3D11RenderTargetView* g_render_target = nullptr;
bool g_resize_succeeded = false;
FILE* g_app_log = nullptr;
FILE* g_lifecycle_log = nullptr;

ULONGLONG QueryPerformanceTicks() {
    LARGE_INTEGER value{};
    QueryPerformanceCounter(&value);
    return static_cast<ULONGLONG>(value.QuadPart);
}

ULONGLONG QueryPerformanceFrequencyValue() {
    LARGE_INTEGER value{};
    QueryPerformanceFrequency(&value);
    return static_cast<ULONGLONG>(value.QuadPart);
}

ULONGLONG ElapsedMicroseconds(ULONGLONG start, ULONGLONG frequency) {
    const ULONGLONG elapsed = QueryPerformanceTicks() - start;
    return frequency == 0 ? 0 : (elapsed * 1000000ULL) / frequency;
}

void PaceFrame30Fps(ULONGLONG start, ULONGLONG frequency,
                    ULONGLONG completed_frames) {
    if (frequency == 0) {
        Sleep(33);
        return;
    }

    const ULONGLONG target_offset = (completed_frames * frequency) / 30ULL;
    for (;;) {
        const ULONGLONG elapsed = QueryPerformanceTicks() - start;
        if (elapsed >= target_offset) {
            return;
        }
        const ULONGLONG remaining = target_offset - elapsed;
        const ULONGLONG remaining_ms = (remaining * 1000ULL) / frequency;
        if (remaining_ms > 1) {
            Sleep(static_cast<DWORD>(remaining_ms - 1));
        } else {
            SwitchToThread();
        }
    }
}

bool EnvIsOne(const char* name) {
    char value[16]{};
    const DWORD length = GetEnvironmentVariableA(name, value, sizeof(value));
    return length > 0 && value[0] == '1' && value[1] == '\0';
}

bool SetFramegenEnvironment(bool enabled) {
    const char* value = enabled ? "1" : "0";
    const errno_t crt_result = _putenv_s("DXMT_FRAMEGEN_ENABLE", value);
    const BOOL win32_result = SetEnvironmentVariableA("DXMT_FRAMEGEN_ENABLE", value);
    char observed[16]{};
    const DWORD length = GetEnvironmentVariableA(
        "DXMT_FRAMEGEN_ENABLE", observed, static_cast<DWORD>(sizeof(observed)));
    return crt_result == 0 && win32_result && length == 1 &&
           observed[0] == value[0] && observed[1] == '\0';
}

FILE* OpenApplicationLog() {
    char executable[MAX_PATH]{};
    const DWORD length = GetModuleFileNameA(nullptr, executable, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        return nullptr;
    }
    std::string path(executable, length);
    const size_t slash = path.find_last_of("\\/");
    if (slash == std::string::npos) {
        return nullptr;
    }
    path.resize(slash + 1);
    path += "d3d11_clear_window_app.csv";
    return std::fopen(path.c_str(), "wt");
}

FILE* OpenLifecycleLog() {
    char executable[MAX_PATH]{};
    const DWORD length = GetModuleFileNameA(nullptr, executable, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        return nullptr;
    }
    std::string path(executable, length);
    const size_t slash = path.find_last_of("\\/");
    if (slash == std::string::npos) {
        return nullptr;
    }
    path.resize(slash + 1);
    path += "d3d11_clear_window_lifecycle.csv";
    FILE* stream = std::fopen(path.c_str(), "wt");
    if (stream) {
        std::fprintf(stream, "event,frame,elapsed_us,result,hr\n");
        std::fflush(stream);
    }
    return stream;
}

void LogLifecycle(const char* event, ULONGLONG frame, ULONGLONG elapsed_us,
                  const char* result, HRESULT hr = S_OK) {
    if (!g_lifecycle_log) {
        return;
    }
    std::fprintf(g_lifecycle_log, "%s,%llu,%llu,%s,0x%08llx\n", event,
        static_cast<unsigned long long>(frame),
        static_cast<unsigned long long>(elapsed_us), result,
        static_cast<unsigned long long>(static_cast<unsigned long>(hr)));
    std::fflush(g_lifecycle_log);
}

bool CreateRenderTarget() {
    ID3D11Texture2D* back_buffer = nullptr;
    const HRESULT result = g_swap_chain->GetBuffer(
        0, __uuidof(ID3D11Texture2D), reinterpret_cast<void**>(&back_buffer));
    if (FAILED(result)) {
        return false;
    }

    const HRESULT view_result =
        g_device->CreateRenderTargetView(back_buffer, nullptr, &g_render_target);
    back_buffer->Release();
    return SUCCEEDED(view_result);
}

bool ResizeSwapChain(UINT width, UINT height) {
    if (!g_swap_chain || width == 0 || height == 0) {
        return true;
    }
    if (g_context) {
        g_context->OMSetRenderTargets(0, nullptr, nullptr);
    }
    if (g_render_target) {
        g_render_target->Release();
        g_render_target = nullptr;
    }
    if (FAILED(g_swap_chain->ResizeBuffers(
            0, width, height, DXGI_FORMAT_UNKNOWN, 0))) {
        return false;
    }
    return CreateRenderTarget();
}

HRESULT RecreateSwapChain(HWND window) {
    if (!g_swap_chain || !g_device) {
        return DXGI_ERROR_INVALID_CALL;
    }
    DXGI_SWAP_CHAIN_DESC description{};
    HRESULT hr = g_swap_chain->GetDesc(&description);
    if (FAILED(hr)) {
        return hr;
    }

    if (g_context) {
        g_context->OMSetRenderTargets(0, nullptr, nullptr);
    }
    if (g_render_target) {
        g_render_target->Release();
        g_render_target = nullptr;
    }
    if (g_swap_chain3) {
        g_swap_chain3->Release();
        g_swap_chain3 = nullptr;
    }
    g_swap_chain->Release();
    g_swap_chain = nullptr;

    IDXGIDevice* dxgi_device = nullptr;
    IDXGIAdapter* adapter = nullptr;
    IDXGIFactory* factory = nullptr;
    hr = g_device->QueryInterface(IID_PPV_ARGS(&dxgi_device));
    if (SUCCEEDED(hr)) {
        hr = dxgi_device->GetAdapter(&adapter);
    }
    if (SUCCEEDED(hr)) {
        hr = adapter->GetParent(IID_PPV_ARGS(&factory));
    }
    if (SUCCEEDED(hr)) {
        description.OutputWindow = window;
        hr = factory->CreateSwapChain(g_device, &description, &g_swap_chain);
    }
    if (factory) {
        factory->Release();
    }
    if (adapter) {
        adapter->Release();
    }
    if (dxgi_device) {
        dxgi_device->Release();
    }
    if (FAILED(hr)) {
        return hr;
    }
    hr = g_swap_chain->QueryInterface(IID_PPV_ARGS(&g_swap_chain3));
    if (FAILED(hr) || !g_swap_chain3) {
        return FAILED(hr) ? hr : E_NOINTERFACE;
    }
    return CreateRenderTarget() ? S_OK : E_FAIL;
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_KEYDOWN:
        if (wparam == VK_ESCAPE) {
            DestroyWindow(window);
            return 0;
        }
        break;
    case WM_SIZE:
        if (g_swap_chain && wparam != SIZE_MINIMIZED) {
            g_resize_succeeded = ResizeSwapChain(
                static_cast<UINT>(LOWORD(lparam)),
                static_cast<UINT>(HIWORD(lparam)));
            if (!g_resize_succeeded) {
                PostQuitMessage(4);
            }
            return 0;
        }
        break;
    case WM_CLOSE:
        DestroyWindow(window);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    default:
        break;
    }
    return DefWindowProcW(window, message, wparam, lparam);
}

void ReleaseGraphics() {
    if (g_render_target) {
        g_render_target->Release();
        g_render_target = nullptr;
    }
    if (g_context) {
        g_context->Release();
        g_context = nullptr;
    }
    if (g_device) {
        g_device->Release();
        g_device = nullptr;
    }
    if (g_swap_chain3) {
        g_swap_chain3->Release();
        g_swap_chain3 = nullptr;
    }
    if (g_swap_chain) {
        g_swap_chain->Release();
        g_swap_chain = nullptr;
    }
}

bool InitializeGraphics(HWND window) {
    DXGI_SWAP_CHAIN_DESC swap_desc{};
    swap_desc.BufferDesc.Width = kClientWidth;
    swap_desc.BufferDesc.Height = kClientHeight;
    swap_desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    swap_desc.BufferDesc.RefreshRate.Numerator = 60;
    swap_desc.BufferDesc.RefreshRate.Denominator = 1;
    swap_desc.SampleDesc.Count = 1;
    swap_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swap_desc.BufferCount = 2;
    swap_desc.OutputWindow = window;
    swap_desc.Windowed = TRUE;
    swap_desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    D3D_FEATURE_LEVEL feature_level{};
    HRESULT result = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
        D3D11_SDK_VERSION, &swap_desc, &g_swap_chain, &g_device,
        &feature_level, &g_context);
    if (FAILED(result)) {
        return false;
    }

    result = g_swap_chain->QueryInterface(
        IID_PPV_ARGS(&g_swap_chain3));
    if (FAILED(result) || !g_swap_chain3) {
        return false;
    }

    return CreateRenderTarget();
}
} // namespace

int WINAPI WinMain(HINSTANCE instance, HINSTANCE, LPSTR, int show_command) {
    constexpr wchar_t kClassName[] = L"FramegenD3D11BaselineWindow";
    constexpr wchar_t kWindowTitle[] = L"Framegen D3D11 clear-color baseline";

    WNDCLASSEXW window_class{};
    window_class.cbSize = sizeof(window_class);
    window_class.hInstance = instance;
    window_class.lpfnWndProc = WindowProc;
    window_class.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    window_class.lpszClassName = kClassName;
    if (!RegisterClassExW(&window_class)) {
        return 1;
    }

    constexpr DWORD window_style = WS_OVERLAPPEDWINDOW;
    RECT bounds{0, 0, kClientWidth, kClientHeight};
    if (!AdjustWindowRectEx(&bounds, window_style, FALSE, 0)) {
        UnregisterClassW(kClassName, instance);
        return 1;
    }

    HWND window = CreateWindowExW(
        0, kClassName, kWindowTitle, window_style, CW_USEDEFAULT, CW_USEDEFAULT,
        bounds.right - bounds.left, bounds.bottom - bounds.top, nullptr, nullptr,
        instance, nullptr);
    if (!window) {
        UnregisterClassW(kClassName, instance);
        return 1;
    }

    const bool framegen_mode = EnvIsOne("DXMT_FRAMEGEN_ENABLE");
    ShowWindow(window, show_command);
    if (framegen_mode) {
        ShowWindow(window, SW_SHOW);
        SetForegroundWindow(window);
    }
    UpdateWindow(window);

    if (!InitializeGraphics(window)) {
        ReleaseGraphics();
        DestroyWindow(window);
        UnregisterClassW(kClassName, instance);
        return 2;
    }

    constexpr float colors[][4] = {
        {0.08f, 0.22f, 0.52f, 1.0f},
        {0.52f, 0.12f, 0.08f, 1.0f},
        {0.08f, 0.42f, 0.18f, 1.0f},
        {0.34f, 0.12f, 0.48f, 1.0f},
    };
    constexpr unsigned kFramesPerColor = 1;
    constexpr unsigned kColorCount = sizeof(colors) / sizeof(colors[0]);

    const bool static_texture_mode =
        EnvIsOne("DXMT_FRAMEGEN_STATIC_TEXTURE");
    const bool skip_resize_test = EnvIsOne("DXMT_FRAMEGEN_SKIP_RESIZE");
    const bool lifecycle_test = EnvIsOne("DXMT_FRAMEGEN_LIFECYCLE_TEST");
    const bool toggle_test = EnvIsOne("DXMT_FRAMEGEN_TOGGLE_TEST") ||
                             lifecycle_test;
    int exit_code = 0;
    bool runtime_framegen_enabled = framegen_mode;
    ULONGLONG run_duration_ms = framegen_mode
        ? kFramegenRunDurationMs
        : kBaselineRunDurationMs;
    char run_duration_text[32]{};
    const DWORD run_duration_length = GetEnvironmentVariableA(
        "DXMT_FRAMEGEN_RUN_SECONDS", run_duration_text,
        static_cast<DWORD>(sizeof(run_duration_text)));
    if (framegen_mode && run_duration_length > 0 &&
        run_duration_length < sizeof(run_duration_text)) {
        const unsigned long requested_seconds = std::strtoul(run_duration_text, nullptr, 10);
        if (requested_seconds >= 1 && requested_seconds <= 1800) {
            run_duration_ms = static_cast<ULONGLONG>(requested_seconds) * 1000;
        }
    }
    if (framegen_mode) {
        g_app_log = OpenApplicationLog();
        if (lifecycle_test) {
            g_lifecycle_log = OpenLifecycleLog();
        }
        if (!g_app_log) {
            std::fprintf(stderr, "could not create the D3D11 application log\n");
            exit_code = 8;
        }
        if (lifecycle_test && !g_lifecycle_log) {
            std::fprintf(stderr, "could not create the D3D11 lifecycle log\n");
            exit_code = 9;
        }
        if (g_app_log) {
            std::fprintf(g_app_log,
                "event,app_present_seq,elapsed_us,present_hr,requested_buffer_index,"
                "current_backbuffer_index_before,current_backbuffer_index_after,"
                "current_index_query_hr,buffer0_query_hr,color_id,"
                "static_texture_mode,window_visible,window_iconic,"
                "window_foreground\n");
            std::fflush(g_app_log);
        }
    }

    MSG message{};
    ULONGLONG frame = 0;
    const ULONGLONG start_time = GetTickCount64();
    const ULONGLONG qpc_start = QueryPerformanceTicks();
    const ULONGLONG qpc_frequency = QueryPerformanceFrequencyValue();
    bool running = exit_code == 0;
    bool resize_requested = false;
    bool focus_loss_requested = false;
    bool focus_recovery_requested = false;
    bool fullscreen_attempted = false;
    bool fullscreen_entered = false;
    bool fullscreen_exit_attempted = false;
    bool swapchain_recreated = false;
    while (running && GetTickCount64() - start_time < run_duration_ms) {
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            if (message.message == WM_QUIT) {
                running = false;
                break;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if (!running) {
            break;
        }

        if (!skip_resize_test && !resize_requested && frame >= 90) {
            RECT resized_bounds{0, 0, 800, 450};
            const ULONGLONG elapsed_us =
                ElapsedMicroseconds(qpc_start, qpc_frequency);
            LogLifecycle("resize_begin", frame, elapsed_us, "800x450");
            const bool resize_requested_ok =
                AdjustWindowRectEx(&resized_bounds, window_style, FALSE, 0) &&
                SetWindowPos(window, nullptr, 0, 0,
                              resized_bounds.right - resized_bounds.left,
                              resized_bounds.bottom - resized_bounds.top,
                              SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
            LogLifecycle("resize_complete", frame,
                         ElapsedMicroseconds(qpc_start, qpc_frequency),
                         g_resize_succeeded ? "succeeded" : "failed",
                         resize_requested_ok && g_resize_succeeded ? S_OK : E_FAIL);
            if (!resize_requested_ok || !g_resize_succeeded) {
                exit_code = 4;
                break;
            }
            resize_requested = true;
        }

        const unsigned color_index = static_texture_mode ? 0
            : static_cast<unsigned>((frame / kFramesPerColor) % kColorCount);
        g_context->ClearRenderTargetView(g_render_target, colors[color_index]);
            const UINT current_index_before =
                g_swap_chain3 ? g_swap_chain3->GetCurrentBackBufferIndex() : 0xFFFFFFFFu;
            const HRESULT present_result = g_swap_chain->Present(1, 0);
            const UINT current_index_after =
                g_swap_chain3 ? g_swap_chain3->GetCurrentBackBufferIndex() : 0xFFFFFFFFu;
            const BOOL window_visible = IsWindowVisible(window);
            const BOOL window_iconic = IsIconic(window);
            const BOOL window_foreground = GetForegroundWindow() == window;
            if (g_app_log) {
            ID3D11Texture2D* probe_buffer = nullptr;
            const HRESULT buffer0_result = g_swap_chain->GetBuffer(
                0, __uuidof(ID3D11Texture2D),
                reinterpret_cast<void**>(&probe_buffer));
            if (probe_buffer) {
                probe_buffer->Release();
            }
            std::fprintf(g_app_log, "present,%llu,%llu,0x%08llx,0,%u,%u,0x%08llx,0x%08llx,%u,%u,%u,%u,%u\n",
                static_cast<unsigned long long>(frame + 1),
                static_cast<unsigned long long>(
                    ElapsedMicroseconds(qpc_start, qpc_frequency)),
                static_cast<unsigned long long>(present_result),
                current_index_before,
                current_index_after,
                static_cast<unsigned long long>(
                    g_swap_chain3 ? S_OK : E_NOINTERFACE),
                static_cast<unsigned long long>(buffer0_result), color_index,
                static_texture_mode ? 1u : 0u,
                window_visible ? 1u : 0u,
                window_iconic ? 1u : 0u,
                window_foreground ? 1u : 0u);
            if ((frame + 1) % 30 == 0) {
                std::fflush(g_app_log);
            }
        }
        if (FAILED(present_result)) {
            exit_code = 3;
            break;
        }
        ++frame;

        const ULONGLONG elapsed_ms = GetTickCount64() - start_time;
        const ULONGLONG elapsed_us =
            ElapsedMicroseconds(qpc_start, qpc_frequency);
        if (lifecycle_test && elapsed_ms >= 25000 && !focus_loss_requested) {
            ShowWindow(window, SW_MINIMIZE);
            focus_loss_requested = true;
            LogLifecycle("focus_loss", frame, elapsed_us, "minimized");
        }
        if (lifecycle_test && focus_loss_requested &&
            elapsed_ms >= 26000 && !focus_recovery_requested) {
            ShowWindow(window, SW_RESTORE);
            SetForegroundWindow(window);
            focus_recovery_requested = true;
            LogLifecycle("focus_recovery", frame,
                         ElapsedMicroseconds(qpc_start, qpc_frequency),
                         "restore_requested");
        }
        if (lifecycle_test && elapsed_ms >= 35000 && !fullscreen_attempted) {
            const HRESULT hr = g_swap_chain->SetFullscreenState(TRUE, nullptr);
            fullscreen_attempted = true;
            fullscreen_entered = SUCCEEDED(hr);
            LogLifecycle("fullscreen_enter", frame, elapsed_us,
                         fullscreen_entered ? "succeeded" : "unsupported", hr);
        }
        if (lifecycle_test && fullscreen_entered &&
            elapsed_ms >= 36500 && !fullscreen_exit_attempted) {
            const HRESULT hr = g_swap_chain->SetFullscreenState(FALSE, nullptr);
            fullscreen_exit_attempted = true;
            fullscreen_entered = FAILED(hr);
            LogLifecycle("fullscreen_exit", frame,
                         ElapsedMicroseconds(qpc_start, qpc_frequency),
                         SUCCEEDED(hr) ? "succeeded" : "failed", hr);
        }
        if (lifecycle_test && elapsed_ms >= 45000 && !swapchain_recreated) {
            const HRESULT hr = RecreateSwapChain(window);
            swapchain_recreated = SUCCEEDED(hr);
            LogLifecycle("swapchain_recreation", frame,
                         ElapsedMicroseconds(qpc_start, qpc_frequency),
                         swapchain_recreated ? "succeeded" : "unsupported", hr);
            if (FAILED(hr)) {
                exit_code = 5;
                break;
            }
        }

        if (framegen_mode && toggle_test) {
            if (elapsed_ms >= 15000 && elapsed_ms < 20000 &&
                runtime_framegen_enabled) {
                if (!SetFramegenEnvironment(false)) {
                    LogLifecycle("framegen_disable_failed", frame, elapsed_us,
                                 "environment_readback_failed", E_FAIL);
                    exit_code = 6;
                    break;
                }
                runtime_framegen_enabled = false;
                if (g_app_log) {
                    std::fprintf(g_app_log, "framegen_disabled,%llu,%llu,0,0,0\n",
                        static_cast<unsigned long long>(frame),
                        static_cast<unsigned long long>(elapsed_ms));
                    std::fflush(g_app_log);
                }
                LogLifecycle("framegen_disabled", frame, elapsed_us,
                             "verified_env_0");
            } else if (elapsed_ms >= 20000 && !runtime_framegen_enabled) {
                if (!SetFramegenEnvironment(true)) {
                    LogLifecycle("framegen_enable_failed", frame, elapsed_us,
                                 "environment_readback_failed", E_FAIL);
                    exit_code = 7;
                    break;
                }
                runtime_framegen_enabled = true;
                if (g_app_log) {
                    std::fprintf(g_app_log, "framegen_enabled,%llu,%llu,0,0,0\n",
                        static_cast<unsigned long long>(frame),
                        static_cast<unsigned long long>(elapsed_ms));
                    std::fflush(g_app_log);
                }
                LogLifecycle("framegen_enabled", frame, elapsed_us,
                             "verified_env_1");
            }
        }
        if (framegen_mode) {
            PaceFrame30Fps(qpc_start, qpc_frequency, frame);
        }
    }

    if (framegen_mode && exit_code == 0 &&
        GetTickCount64() - start_time < run_duration_ms) {
        std::fprintf(stderr, "D3D11 framegen run ended before its requested duration\n");
        exit_code = 10;
    }

    if (g_app_log) {
        std::fprintf(g_app_log, "exit,%llu,%llu,0x%08x,0,0\n",
            static_cast<unsigned long long>(frame),
            static_cast<unsigned long long>(
                ElapsedMicroseconds(qpc_start, qpc_frequency)),
            static_cast<unsigned>(exit_code));
        if (std::fclose(g_app_log) != 0 && exit_code == 0) {
            exit_code = 11;
        }
        g_app_log = nullptr;
    }
    if (g_lifecycle_log) {
        LogLifecycle("exit", frame,
                     ElapsedMicroseconds(qpc_start, qpc_frequency),
                     exit_code == 0 ? "success" : "failure",
                     exit_code == 0 ? S_OK : E_FAIL);
        if (std::fclose(g_lifecycle_log) != 0 && exit_code == 0) {
            exit_code = 12;
        }
        g_lifecycle_log = nullptr;
    }

    if (IsWindow(window)) {
        DestroyWindow(window);
    }
    ReleaseGraphics();
    UnregisterClassW(kClassName, instance);
    if (!skip_resize_test && !g_resize_succeeded && exit_code == 0) {
        exit_code = 4;
    }
    return exit_code;
}
