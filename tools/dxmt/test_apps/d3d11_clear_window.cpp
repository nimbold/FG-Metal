#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi.h>

#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>

namespace {
constexpr UINT kWidth = 1280;
constexpr UINT kHeight = 720;
constexpr UINT kLogFrameInterval = 300;

IDXGISwapChain* g_swapChain = nullptr;
ID3D11Device* g_device = nullptr;
ID3D11DeviceContext* g_context = nullptr;
ID3D11RenderTargetView* g_renderTarget = nullptr;
ID3D11VertexShader* g_vertexShader = nullptr;
ID3D11PixelShader* g_pixelShader = nullptr;
ID3D11Buffer* g_frameConstants = nullptr;
FILE* g_log = nullptr;
UINT g_clientWidth = kWidth;
UINT g_clientHeight = kHeight;
bool g_resizeFailed = false;

struct FrameConstants {
    float frame;
    float width;
    float height;
    float padding;
};

constexpr char kShaderSource[] = R"hlsl(
cbuffer FrameConstants : register(b0) {
    float frame;
    float width;
    float height;
    float padding;
};

struct VertexOutput {
    float4 position : SV_Position;
    float2 uv : TEXCOORD0;
};

VertexOutput VSMain(uint vertexId : SV_VertexID) {
    float2 p = float2((vertexId << 1) & 2, vertexId & 2);
    VertexOutput output;
    output.position = float4(p * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    output.uv = p;
    return output;
}

float4 PSMain(VertexOutput input) : SV_Target {
    float2 uv = input.uv;
    float3 color = float3(0.012, 0.020, 0.040);

    float mainX = frac(frame / 240.0 + 0.20) * 1.16 - 0.08;
    float mainRect = step(abs(uv.x - mainX), 0.105) *
                     step(0.37, uv.y) * step(uv.y, 0.63);
    color = lerp(color, float3(0.05, 0.82, 1.0), mainRect);

    float markerX = frac(frame / 97.0 + 0.25) * 1.14 - 0.07;
    float marker = step(abs(uv.x - markerX), 0.022) *
                   step(0.16, uv.y) * step(uv.y, 0.84);
    color = lerp(color, float3(1.0, 0.62, 0.08), marker);

    float phase = frac(frame / 180.0 + 0.15);
    float pulse = step(abs(uv.y - 0.10), 0.012) * step(0.04, uv.x) *
                  step(uv.x, 0.04 + phase * 0.90);
    color = lerp(color, float3(0.40, 1.0, 0.24), pulse);
    return float4(color, 1.0);
}
)hlsl";

void Log(const char* format, ...) {
    if (!g_log) return;
    va_list args;
    va_start(args, format);
    std::vfprintf(g_log, format, args);
    va_end(args);
    std::fputc('\n', g_log);
    std::fflush(g_log);
}

FILE* OpenLogBesideExecutable() {
    wchar_t executable[MAX_PATH]{};
    const DWORD length = GetModuleFileNameW(nullptr, executable, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) return nullptr;
    std::wstring path(executable, length);
    const size_t slash = path.find_last_of(L"\\/");
    if (slash == std::wstring::npos) return nullptr;
    path.resize(slash + 1);
    path += L"fgmetal-r100-d3d11-harness.log";
    return _wfopen(path.c_str(), L"wt");
}

bool CreateRenderTarget() {
    ID3D11Texture2D* backBuffer = nullptr;
    const HRESULT getBuffer = g_swapChain->GetBuffer(
        0, __uuidof(ID3D11Texture2D), reinterpret_cast<void**>(&backBuffer));
    if (FAILED(getBuffer)) {
        Log("render target GetBuffer failed hr=0x%08lx", getBuffer);
        return false;
    }
    const HRESULT createView =
        g_device->CreateRenderTargetView(backBuffer, nullptr, &g_renderTarget);
    backBuffer->Release();
    if (FAILED(createView)) {
        Log("render target creation failed hr=0x%08lx", createView);
        return false;
    }
    return true;
}

bool Resize(UINT width, UINT height) {
    if (!g_swapChain || width == 0 || height == 0) return true;
    g_context->OMSetRenderTargets(0, nullptr, nullptr);
    if (g_renderTarget) {
        g_renderTarget->Release();
        g_renderTarget = nullptr;
    }
    const HRESULT resizeResult = g_swapChain->ResizeBuffers(
        0, width, height, DXGI_FORMAT_UNKNOWN, 0);
    if (FAILED(resizeResult)) {
        Log("ResizeBuffers failed hr=0x%08lx", resizeResult);
        return false;
    }
    if (!CreateRenderTarget()) return false;
    g_clientWidth = width;
    g_clientHeight = height;
    Log("window size=%ux%u", width, height);
    return true;
}

bool CompileShader(const char* entryPoint, const char* target,
                   ID3DBlob** bytecode) {
    ID3DBlob* errors = nullptr;
    const HRESULT result = D3DCompile(
        kShaderSource, std::strlen(kShaderSource), "r100-harness.hlsl", nullptr,
        nullptr, entryPoint, target, D3DCOMPILE_OPTIMIZATION_LEVEL3, 0,
        bytecode, &errors);
    if (FAILED(result)) {
        if (errors) {
            Log("shader compile failed entry=%s hr=0x%08lx: %s", entryPoint,
                result, static_cast<const char*>(errors->GetBufferPointer()));
            errors->Release();
        } else {
            Log("shader compile failed entry=%s hr=0x%08lx", entryPoint, result);
        }
        return false;
    }
    if (errors) errors->Release();
    return true;
}

bool CreatePipeline() {
    ID3DBlob* vertexBytecode = nullptr;
    if (!CompileShader("VSMain", "vs_4_0", &vertexBytecode)) return false;
    HRESULT result = g_device->CreateVertexShader(
        vertexBytecode->GetBufferPointer(), vertexBytecode->GetBufferSize(),
        nullptr, &g_vertexShader);
    vertexBytecode->Release();
    if (FAILED(result)) {
        Log("CreateVertexShader failed hr=0x%08lx", result);
        return false;
    }

    ID3DBlob* pixelBytecode = nullptr;
    if (!CompileShader("PSMain", "ps_4_0", &pixelBytecode)) return false;
    result = g_device->CreatePixelShader(
        pixelBytecode->GetBufferPointer(), pixelBytecode->GetBufferSize(),
        nullptr, &g_pixelShader);
    pixelBytecode->Release();
    if (FAILED(result)) {
        Log("CreatePixelShader failed hr=0x%08lx", result);
        return false;
    }

    D3D11_BUFFER_DESC constantsDesc{};
    constantsDesc.ByteWidth = sizeof(FrameConstants);
    constantsDesc.Usage = D3D11_USAGE_DEFAULT;
    constantsDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    result = g_device->CreateBuffer(&constantsDesc, nullptr, &g_frameConstants);
    if (FAILED(result)) {
        Log("CreateBuffer constants failed hr=0x%08lx", result);
        return false;
    }
    return true;
}

bool InitializeGraphics(HWND window) {
    DXGI_SWAP_CHAIN_DESC swapDesc{};
    swapDesc.BufferDesc.Width = kWidth;
    swapDesc.BufferDesc.Height = kHeight;
    swapDesc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    swapDesc.BufferDesc.RefreshRate.Numerator = 60;
    swapDesc.BufferDesc.RefreshRate.Denominator = 1;
    swapDesc.SampleDesc.Count = 1;
    swapDesc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swapDesc.BufferCount = 2;
    swapDesc.OutputWindow = window;
    swapDesc.Windowed = TRUE;
    swapDesc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    D3D_FEATURE_LEVEL featureLevel{};
    const HRESULT result = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, nullptr, 0,
        D3D11_SDK_VERSION, &swapDesc, &g_swapChain, &g_device,
        &featureLevel, &g_context);
    if (FAILED(result)) {
        Log("D3D11 device/swapchain creation failed hr=0x%08lx", result);
        return false;
    }
    Log("D3D11 device created feature_level=0x%04x", featureLevel);
    Log("DXGI swapchain created size=%ux%u format=R8G8B8A8_UNORM buffers=%u",
        kWidth, kHeight, swapDesc.BufferCount);

    if (!CreateRenderTarget() || !CreatePipeline()) return false;
    D3D11_VIEWPORT viewport{};
    viewport.Width = static_cast<float>(kWidth);
    viewport.Height = static_cast<float>(kHeight);
    viewport.MaxDepth = 1.0f;
    g_context->RSSetViewports(1, &viewport);
    g_context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    g_context->VSSetShader(g_vertexShader, nullptr, 0);
    g_context->PSSetShader(g_pixelShader, nullptr, 0);
    g_context->PSSetConstantBuffers(0, 1, &g_frameConstants);
    return true;
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wparam,
                            LPARAM lparam) {
    switch (message) {
    case WM_SIZE:
        if (g_swapChain && wparam != SIZE_MINIMIZED) {
            g_resizeFailed = !Resize(LOWORD(lparam), HIWORD(lparam));
            if (g_resizeFailed) PostQuitMessage(2);
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
    if (g_frameConstants) g_frameConstants->Release();
    if (g_pixelShader) g_pixelShader->Release();
    if (g_vertexShader) g_vertexShader->Release();
    if (g_renderTarget) g_renderTarget->Release();
    if (g_context) g_context->Release();
    if (g_device) g_device->Release();
    if (g_swapChain) g_swapChain->Release();
}
} // namespace

int WINAPI WinMain(HINSTANCE instance, HINSTANCE, LPSTR, int) {
    g_log = OpenLogBesideExecutable();
    Log("startup target=%ux%u SDR; close the window to exit", kWidth, kHeight);

    constexpr wchar_t kClassName[] = L"FGMetalR100D3D11Harness";
    constexpr wchar_t kWindowTitle[] = L"FG-Metal R100 D3D11 Harness";
    WNDCLASSEXW windowClass{};
    windowClass.cbSize = sizeof(windowClass);
    windowClass.hInstance = instance;
    windowClass.lpfnWndProc = WindowProc;
    windowClass.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    windowClass.lpszClassName = kClassName;
    if (!RegisterClassExW(&windowClass)) {
        Log("RegisterClassExW failed error=%lu", GetLastError());
        if (g_log) std::fclose(g_log);
        return 1;
    }

    RECT bounds{0, 0, static_cast<LONG>(kWidth), static_cast<LONG>(kHeight)};
    const DWORD windowStyle = WS_OVERLAPPEDWINDOW;
    if (!AdjustWindowRectEx(&bounds, windowStyle, FALSE, 0)) {
        Log("AdjustWindowRectEx failed error=%lu", GetLastError());
        UnregisterClassW(kClassName, instance);
        if (g_log) std::fclose(g_log);
        return 1;
    }
    HWND window = CreateWindowExW(
        0, kClassName, kWindowTitle, windowStyle, CW_USEDEFAULT, CW_USEDEFAULT,
        bounds.right - bounds.left, bounds.bottom - bounds.top, nullptr, nullptr,
        instance, nullptr);
    if (!window) {
        Log("CreateWindowExW failed error=%lu", GetLastError());
        UnregisterClassW(kClassName, instance);
        if (g_log) std::fclose(g_log);
        return 1;
    }
    ShowWindow(window, SW_SHOW);
    SetForegroundWindow(window);
    UpdateWindow(window);
    Log("window created client=%ux%u visible=%d", kWidth, kHeight,
        IsWindowVisible(window) ? 1 : 0);

    if (!InitializeGraphics(window)) {
        Log("graphics initialization failed");
        DestroyWindow(window);
        ReleaseGraphics();
        UnregisterClassW(kClassName, instance);
        if (g_log) std::fclose(g_log);
        return 2;
    }

    MSG message{};
    uint64_t frame = 0;
    bool running = true;
    while (running) {
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            if (message.message == WM_QUIT) {
                running = false;
                break;
            }
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        if (!running) break;
        if (g_resizeFailed) break;
        if (IsIconic(window)) {
            WaitMessage();
            continue;
        }

        const FrameConstants constants{
            static_cast<float>(frame % 1200),
            static_cast<float>(g_clientWidth),
            static_cast<float>(g_clientHeight), 0.0f};
        g_context->UpdateSubresource(g_frameConstants, 0, nullptr, &constants, 0, 0);
        g_context->OMSetRenderTargets(1, &g_renderTarget, nullptr);
        g_context->Draw(3, 0);
        const HRESULT presentResult = g_swapChain->Present(1, 0);
        if (FAILED(presentResult)) {
            Log("Present failed frame=%llu hr=0x%08lx",
                static_cast<unsigned long long>(frame), presentResult);
            running = false;
            break;
        }
        ++frame;
        if (frame % kLogFrameInterval == 0) {
            Log("frames=%llu", static_cast<unsigned long long>(frame));
        }
    }

    Log("normal exit frames=%llu", static_cast<unsigned long long>(frame));
    DestroyWindow(window);
    ReleaseGraphics();
    UnregisterClassW(kClassName, instance);
    if (g_log) std::fclose(g_log);
    return g_resizeFailed ? 2 : 0;
}
