#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi1_4.h>

#include <cstdio>

using CreateFactory2Proc = HRESULT (WINAPI *)(UINT, REFIID, void **);

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    FILE *file = std::fopen("exact_path_load_probe.log", "a");
    HMODULE module = LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll");
    WCHAR path[MAX_PATH]{};
    if (module) GetModuleFileNameW(module, path, MAX_PATH);
    auto createFactory = module
        ? reinterpret_cast<CreateFactory2Proc>(GetProcAddress(module, "CreateDXGIFactory2"))
        : nullptr;
    IDXGIFactory2 *factory = nullptr;
    HRESULT hr = createFactory
        ? createFactory(0, __uuidof(IDXGIFactory2), reinterpret_cast<void **>(&factory))
        : HRESULT_FROM_WIN32(GetLastError());
    if (file) {
        std::fprintf(file, "module=%p path=%ls export=%p factory_hr=0x%08lx factory=%p\n",
                     static_cast<void *>(module), path,
                     reinterpret_cast<void *>(createFactory), static_cast<unsigned long>(hr),
                     static_cast<void *>(factory));
        std::fclose(file);
    }
    if (factory) factory->Release();
    if (module) FreeLibrary(module);
    return SUCCEEDED(hr) ? 0 : 1;
}
