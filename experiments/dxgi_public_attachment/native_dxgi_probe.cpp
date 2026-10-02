#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <dxgi.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>

extern "C" __declspec(dllexport) HRESULT WINAPI CreateDXGIFactory2(UINT, REFIID, void **);

namespace {

using CreateFactory2Proc = HRESULT (WINAPI *)(UINT, REFIID, void **);
HMODULE gProviderModule = nullptr;
CreateFactory2Proc gProviderFactory2 = nullptr;

void log_call(const char *name) {
    FILE *file = std::fopen("native_dxgi_probe.log", "a");
    if (!file) return;
    std::fprintf(file, "native_export=%s result=E_NOTIMPL\n", name);
    std::fclose(file);
}

CreateFactory2Proc find_provider_factory2() {
    if (gProviderFactory2) return gProviderFactory2;
    HMODULE current = GetModuleHandleW(L"dxgi.dll");
    HMODULE same_name = LoadLibraryW(L"dxgi.dll");
    HMODULE exact_system_path = LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll");
    FARPROC same_name_export = same_name ? GetProcAddress(same_name, "CreateDXGIFactory2") : nullptr;
    FARPROC exact_path_export = exact_system_path
        ? GetProcAddress(exact_system_path, "CreateDXGIFactory2") : nullptr;
    const FARPROC proxy_export = reinterpret_cast<FARPROC>(&CreateDXGIFactory2);
    WCHAR currentPath[MAX_PATH]{};
    WCHAR sameNamePath[MAX_PATH]{};
    WCHAR exactPath[MAX_PATH]{};
    if (current) GetModuleFileNameW(current, currentPath, MAX_PATH);
    if (same_name) GetModuleFileNameW(same_name, sameNamePath, MAX_PATH);
    if (exact_system_path) GetModuleFileNameW(exact_system_path, exactPath, MAX_PATH);

    FILE *file = std::fopen("native_dxgi_probe.log", "a");
    if (file) {
        std::fprintf(file,
            "provider_lookup self=%p same_name=%p same_export=%p exact_path=%p exact_export=%p "
            "self_path=%ls same_path=%ls exact_file_path=%ls same_is_self=%d exact_is_self=%d "
            "exact_export_is_proxy=%d\n",
            static_cast<void *>(current), static_cast<void *>(same_name),
            reinterpret_cast<void *>(same_name_export), static_cast<void *>(exact_system_path),
            reinterpret_cast<void *>(exact_path_export), currentPath, sameNamePath, exactPath,
            same_name == current, exact_system_path == current, exact_path_export == proxy_export);
        std::fclose(file);
    }
    if (same_name) FreeLibrary(same_name);
    if (exact_path_export && exact_path_export != proxy_export) {
        gProviderModule = exact_system_path;
        gProviderFactory2 = reinterpret_cast<CreateFactory2Proc>(exact_path_export);
        return gProviderFactory2;
    }
    if (exact_system_path) FreeLibrary(exact_system_path);
    return nullptr;
}

HRESULT factory_stub(const char *name, REFIID, void **factory) {
    if (factory) *factory = nullptr;
    log_call(name);
    return E_NOTIMPL;
}

} // namespace

extern "C" __declspec(dllexport) HRESULT WINAPI CreateDXGIFactory(REFIID iid, void **factory) {
    return factory_stub("CreateDXGIFactory", iid, factory);
}

extern "C" __declspec(dllexport) HRESULT WINAPI CreateDXGIFactory1(REFIID iid, void **factory) {
    return factory_stub("CreateDXGIFactory1", iid, factory);
}

extern "C" __declspec(dllexport) HRESULT WINAPI CreateDXGIFactory2(UINT flags, REFIID iid, void **factory) {
    if (factory) *factory = nullptr;
    CreateFactory2Proc provider = find_provider_factory2();
    if (!provider) {
        log_call("CreateDXGIFactory2_no_provider_E_NOTIMPL");
        return E_NOTIMPL;
    }
    const HRESULT hr = provider(flags, iid, factory);
    FILE *file = std::fopen("native_dxgi_probe.log", "a");
    if (file) {
        std::fprintf(file, "forwarded_export=CreateDXGIFactory2 hr=0x%08lx factory=%p\n",
                     static_cast<unsigned long>(hr), factory ? *factory : nullptr);
        std::fclose(file);
    }
    return hr;
}

extern "C" __declspec(dllexport) HRESULT WINAPI DXGIDeclareAdapterRemovalSupport() {
    log_call("DXGIDeclareAdapterRemovalSupport");
    return E_NOTIMPL;
}

BOOL WINAPI DllMain(HINSTANCE, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        FILE *file = std::fopen("native_dxgi_probe.log", "a");
        if (file) {
            std::fprintf(file, "probe_build=step8d-provider-forwarding-20261002b\n");
            std::fclose(file);
        }
    }
    return TRUE;
}
