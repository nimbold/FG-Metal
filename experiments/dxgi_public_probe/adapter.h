#pragma once

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_4.h>
#include <d3d12.h>

extern "C" {
HRESULT WINAPI FG_SetSyncInterval(UINT interval);
HRESULT WINAPI FG_SetOutputRate(UINT outputFramesPerSecond);
HRESULT WINAPI FG_Attach(IDXGIFactory2 *factory, ID3D12Device *device,
                         ID3D12CommandQueue *queue, IDXGISwapChain1 *swapChain);
HRESULT WINAPI FG_RenderSource(const float color[4]);
HRESULT WINAPI FG_PresentSource();
HRESULT WINAPI FG_ResizeBuffers(UINT width, UINT height);
HRESULT WINAPI FG_SetFullscreen(BOOL fullscreen);
HRESULT WINAPI FG_Finish();
HRESULT WINAPI FG_Detach();
}
