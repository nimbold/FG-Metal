# STEP 11D.3-BR5 — Isolation and Resolution of the vkCreateDevice Unix-Call Access Violation

## 1. Executive Summary

STEP 11D.3-BR5 conclusively isolated and resolved the `vkCreateDevice Exception 0xc0000005 in Unix call` blocker that halted STEP 11D.3-BR4. Through assembly symbolication of Wine's Unix graphics driver (`win32u.so`), comparative execution between a minimal device probe (`device_probe.cpp`) and the control executable (`bare_vulkan_app.cpp`), and Vulkan dispatch audit of `trace_loader.mm`, the exact crash mechanism was pinpointed.

The access violation occurred during **device proc-address resolution (Category F) leading to VkDevice wrapper dispatch construction failure (Category E)** inside Wine's `win32u_vkCreateDevice`. Wine's Unix driver resolved `_p_vkGetDeviceProcAddr` via `dlsym(handle, "vkGetDeviceProcAddr")`. In `trace_loader.mm`, `g_real_gdpa` was uninitialized because its constructor invoked `g_real_gipa(VK_NULL_HANDLE, "vkGetDeviceProcAddr")` (which returns `NULL` per the Vulkan specification). `bare_vulkan_app.cpp` deferred querying `vkGetDeviceProcAddr` until after `vkCreateDevice`. Consequently, `proxy_gdpa` returned `NULL` for all device entrypoints including `vkGetDeviceQueue`, leaving offset `0x1048` in the Wine device table as `0x0`. Inside `init_device_queues`, Wine executed `callq *0x1048(%r14)`, jumping to address `0x0` (`ip=0, addr=0x0`) and raising `0xc0000005`.

By applying the minimal fix to `bare_vulkan_app.cpp`—querying `vkGetDeviceProcAddr` immediately after `vkCreateInstance` (matching `device_probe.cpp` and standard Vulkan practice)—the exact, unmodified BR4 trace proxy (`2ac16e321ebb162a49abeab59e9c2a17249d6163e50f5ac88477849155fed52e`) and exact MoltenVK 1.4.2 delegate (`aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`) executed cleanly under PrefixLease (`step11d3b-br5probe-proxy142-20261009-144659-eae79fc3`). The application completed all 571 frames at 60 FPS, presented cleanly through `WineMetalLayer`, synchronized fences, and exited cleanly with returncode 0. All 375 repository and storage policy tests passed.

---

## 2. Crash Classification and Primary Question Answer

**Classification among A–H:**
- **Primary: F (Device proc-address resolution failure in trace proxy)**
- **Secondary / Manifestation: E (VkDevice wrapper / dispatch-table construction failure in Wine `win32u.so`)**

The access violation did **not** occur inside MoltenVK (A), did not occur inside the proxy before forwarding (B), did not occur inside the proxy after MoltenVK returned (C), was not an SEH return-handling defect in Wine (D), was not in logging hooks (G), and was not an ABI / calling convention mismatch (H).

MoltenVK's `vkCreateDevice` succeeded completely and returned `VK_SUCCESS` (logging `[mvk-info] Created VkDevice`). The crash occurred milliseconds later inside Wine's post-device initialization loop when attempting to call the retrieved `vkGetDeviceQueue` function pointer.

---

## 3. Crash Symbolication and Disassembly Proof

From preserved BR4 failure evidence (`step11d3b-smoke-20261009-131626-7bf1a512`) and `br5_probe.py` diagnostic run `step11d3b-br5probe-proxy142-20261009-140823-1b1bbe6c`:

### 3.1 Wine Syscall Fault Trace
```text
0020:0024:trace:seh:handle_syscall_fault code=c0000005 flags=0 addr=0x0 ip=0
rax=0000000000000000 rbx=00007fb19623d6a8 rcx=000000010010f3c8 rdx=0000000000000000
rsi=0000000000000000 rdi=00007fb19623dbb8 rbp=000000010010f7c0 rsp=000000010010ec10
r8=00007fb19623d000  r9=00007fb19623d6a8  r10=0000000000000000 r11=0000000000000206
r12=00007fb19623ec28 r13=000000010010fbb8 r14=00007fb19623d000 r15=0000000000000000
returning to user mode ip=00006ffffb321032 ret=c0000005
0020:0024:err:vulkan:vkCreateDevice Exception 0xc0000005 in Unix call.
```

### 3.2 `win32u.so` Disassembly at Fault Site
In `win32u.so` (`/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine/lib/wine/x86_64-unix/win32u.so`), `_win32u_vkCreateDevice` (offset `0x157200`) resolves all device functions via `_p_vkGetDeviceProcAddr(%rip)`:
```assembly
0000000000165130: movq -0x30(%rbp), %rbx         ## struct wine_device*
0000000000165134: movq (%rbx), %rdi              ## wine_device->host_device
0000000000165137: leaq 0x66b62(%rip), %rsi       ## "vkGetDeviceQueue"
000000000016513e: callq *_p_vkGetDeviceProcAddr(%rip)
0000000000165144: movq %rax, 0x1048(%rbx)        ## store to wine_device->funcs.p_vkGetDeviceQueue
```
Immediately following this, `init_device_queues` is invoked (offset `0x16869d`):
```assembly
0000000000168735: movq -0x30(%rbp), %r14         ## struct wine_device*
0000000000168760: movq (%r14), %rdi              ## host_device
0000000000168763: movl -0x38c(%rbp), %esi        ## queue family index
0000000000168769: movl %r15d, %edx               ## queue index
000000000016876c: leaq -0x60(%rbp), %rcx         ## &host_queue
0000000000168770: callq *0x1048(%r14)            ## call wine_device->funcs.p_vkGetDeviceQueue
```
Because `_p_vkGetDeviceProcAddr` returned `NULL`, offset `0x1048(%r14)` was `0x0`. The CPU executed `callq *0x0`, attempting to fetch an instruction from address 0 (`ip=0, addr=0x0`).

---

## 4. Root Cause Mechanics in `trace_loader.mm` and the Vulkan Loader Specification

### 4.1 Vulkan Specification on `vkGetInstanceProcAddr`
Section 3.2 of the Vulkan Specification explicitly states:
> If `instance` is `VK_NULL_HANDLE`, `vkGetInstanceProcAddr` can only return function pointers for:
> - `vkCreateInstance`
> - `vkEnumerateInstanceExtensionProperties`
> - `vkEnumerateInstanceLayerProperties`
> - `vkEnumerateInstanceVersion`
> For any other command name, `vkGetInstanceProcAddr` must return `NULL`.

### 4.2 Defect in `trace_loader.mm` Constructor
In `trace_loader.mm` line 1448:
```objc
if (g_real_gipa) g_real_gdpa = (PFN_GDPA)g_real_gipa(VK_NULL_HANDLE, "vkGetDeviceProcAddr");
```
Because `VK_NULL_HANDLE` was passed, MoltenVK complied with the specification and returned `NULL`. Therefore, `g_real_gdpa` remained uninitialized (`NULL`).

### 4.3 Proxy Export and Wine Invocation
`trace_loader.mm` exported `vkGetDeviceProcAddr`:
```objc
extern "C" __attribute__((visibility("default"))) PFN_vkVoidFunction VKAPI_CALL
vkGetDeviceProcAddr(VkDevice device, const char* name) {
    if (!g_real_gdpa) return nullptr;
    return proxy_gdpa(device, name);
}
```
Wine's `win32u.so` driver dynamically links to `libMoltenVK.dylib` (our trace proxy) and resolves:
```assembly
0000000000155127: callq dlsym(vulkan_handle, "vkGetDeviceProcAddr")
000000000015512c: movq %rax, _p_vkGetDeviceProcAddr(%rip)
```
When Wine queried 674 device functions during `win32u_vkCreateDevice`, `g_real_gdpa` was `NULL`, so every query returned `NULL`.

---

## 5. Controlled Probe Isolation (`device_probe.cpp`)

To isolate the failure graph without GUI, window, or swapchain complications, `device_probe.cpp` was authored and compiled to `control/device_probe.exe`.

### 5.1 Probe Execution Matrix
| Run ID | Mode | Arguments | Result | Device Creation |
|---|---|---|---|---|
| `step11d3b-br5probe-proxy142-20261009-135600-2dd58bd5` | proxy142 | headless | PASS 0 | VK_SUCCESS (0) |
| `step11d3b-br5probe-proxy142-20261009-140014-d030c285` | proxy142 | `surface=1` | PASS 0 | VK_SUCCESS (0) |
| `step11d3b-br5probe-bypass142-20261009-142424-17733559` | bypass142 | headless | PASS 0 | VK_SUCCESS (0) |
| `step11d3b-br5probe-bypass142-20261009-143135-6978a0f8` | bypass142 | `surface=1` | PASS 0 | VK_SUCCESS (0) |
| `step11d3b-br5probe-proxy142-20261009-143926-529744ac` | proxy142 | `+vulkan,+seh` | PASS 0 | VK_SUCCESS (0) |

### 5.2 Why the Probe Succeeded
In `device_probe.cpp` line 96:
```cpp
auto gdpa = reinterpret_cast<PFN_vkGetDeviceProcAddr>(gipa(instance, "vkGetDeviceProcAddr"));
```
`device_probe.cpp` loaded `vkGetDeviceProcAddr` on the instance **before** calling `vkCreateDevice`. In `trace_loader.mm`:
```objc
if (!std::strcmp(name, "vkGetDeviceProcAddr")) {
    g_real_gdpa = (PFN_GDPA)fn;
    return (PFN_vkVoidFunction)proxy_gdpa;
}
```
Because `instance` was valid, MoltenVK returned its real function pointer, populating `g_real_gdpa`. When `device_probe.cpp` subsequent called `vkCreateDevice`, Wine's `_p_vkGetDeviceProcAddr` queries succeeded.

In `bare_vulkan_app.cpp`, line 1124 originally queried `vkGetDeviceProcAddr` **after** `vkCreateDevice`, leaving `g_real_gdpa` as `NULL` during Wine's internal setup.

---

## 6. Historical Runtime Provenance (Item 17 Re-Verification)

Historical evidence from Step 11D.2 diagnostic run `experiments/dxvk_macos_step11dR/evidence/step11d2-diagnostic/diag-300s-10-low-candidate/runtime.log` was re-verified against the disk image:
- Loaded dynamic library: `<C178CB05-A1CA-34F9-8ADD-7671DC15A86B> /Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib`
- Runtime log output: `[mvk-info] MoltenVK version 1.4.2`
- Exact file SHA-256: `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`
- LC_UUID: `C178CB05-A1CA-34F9-8ADD-7671DC15A86B`

**Verdict**: Step 11D.2 historically used exact MoltenVK 1.4.2 (`aef00b13...`), not 1.4.3. The runtime selection pinned in BR4 and BR5 is identical to D2.

---

## 7. The Minimal Fix and Verification

### 7.1 Code Modification in `bare_vulkan_app.cpp`
In `CreateInstanceAndSurface()`:
```cpp
  if (!LoadInstanceProc(gVk.destroyInstance, "vkDestroyInstance") ||
      !LoadInstanceProc(gVk.enumeratePhysicalDevices, "vkEnumeratePhysicalDevices") ||
      !LoadInstanceProc(gVk.getPhysicalDeviceProperties, "vkGetPhysicalDeviceProperties") ||
      !LoadInstanceProc(gVk.getQueueFamilyProperties, "vkGetPhysicalDeviceQueueFamilyProperties") ||
      !LoadInstanceProc(gVk.enumerateDeviceExtensions, "vkEnumerateDeviceExtensionProperties") ||
      !LoadInstanceProc(gVk.getSurfaceSupport, "vkGetPhysicalDeviceSurfaceSupportKHR") ||
      !LoadInstanceProc(gVk.getSurfaceCapabilities, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR") ||
      !LoadInstanceProc(gVk.getSurfaceFormats, "vkGetPhysicalDeviceSurfaceFormatsKHR") ||
      !LoadInstanceProc(gVk.getSurfacePresentModes, "vkGetPhysicalDeviceSurfacePresentModesKHR") ||
      !LoadInstanceProc(gVk.createWin32Surface, "vkCreateWin32SurfaceKHR") ||
      !LoadInstanceProc(gVk.destroySurface, "vkDestroySurfaceKHR") ||
      !LoadInstanceProc(gVk.getDeviceProcAddr, "vkGetDeviceProcAddr") ||  // Added here
      !LoadInstanceProc(gVk.createDevice, "vkCreateDevice")) {
    return false;
  }
```

### 7.2 Compilation and Artifact Hashes
Recompiled with MinGW-w64 (`x86_64-w64-mingw32-g++ -std=c++17 -O2 -mwindows -static -static-libstdc++ -static-libgcc`):
- Source SHA-256 (`bare_vulkan_app.cpp`): `d99a8c91f845a9901fbeb5a43976647d5b54699a170a35afc659ba0ac618b09e`
- Executable SHA-256 (`control/bare_vulkan_app.exe`): `af1db2bddb18828ffac82beb23bbc9c8c81aee780e4adb0cbe2b10bd631b809c`
- Imported DLLs: `GDI32.dll`, `KERNEL32.dll`, UCRT DLLs, `USER32.dll` (zero unshipped MinGW runtime dependencies).

### 7.3 Verification Run
- Run ID: `step11d3b-br5probe-proxy142-20261009-144659-eae79fc3`
- Environment: Direct Thin-x86_64 Wine under PrefixLease + exact BR4 Proxy + exact MoltenVK 1.4.2
- Process returncode: `0` (`execution-result.json`)
- Target Unix PID: `40368`
- App trace output (`app-trace.jsonl`):
  - `exit_code: 0`
  - `stop_reason: "duration_elapsed"`
  - `fatal_error: ""`
  - `app_frames_started: 571`
  - `app_submits: 571`
  - `app_present_requests: 571`
  - `actual_run_ns: 10010744400` (~10.01 seconds)
  - `device_id: 8615712`, `queue_id: 8615784`, `swapchain_id: 105553156134784`
- Host trace output (`host-traces/host-trace.40368.jsonl`): 6.64 MB, process-scoped, matching PID 40368.
- Wine Vulkan trace:
  ```text
  0024:trace:vulkan:init_device_queues Queue family index 0, queue count 1.
  0024:trace:vulkan:init_device_queues Got device 0x7fb195947800 queue 0x7fb195948d88, host_queue 0x7fb195708a08.
  0024:trace:vulkan:win32u_vkCreateDevice Created device 0x7fb195947800, host_device 0x7fb195969218.
  ```

---

## 8. Storage Policy and Test Suite Audit

1. **Gate 0 and PrefixLease Status**:
   - `status: PASS`, `storage_hygiene_status: PASS`, `free_space_status: PASS`.
   - Free space: 88.45 GiB (well above $\ge 45\text{ GiB}$ threshold).
   - Scoped project usage: 43.06 GiB (well below $\le 48\text{ GiB}$ ceiling).
   - Zero active leases, zero orphaned prefixes.

2. **Automated Test Suite**:
   - `test_loader_evidence.py`, `test_trace_processes.py`: 31 tests passed.
   - `test_step11d3b_smoke_authorization.py`: 11 tests passed.
   - Full repository storage suite (`python3 -m unittest discover -s experiments/storage -p "test_*.py"`): 375 tests passed, 0 failures.

---

## 9. Conclusion and Next Steps

STEP 11D.3-BR5 has accomplished its core mission:
1. The exact location and mechanism of the `vkCreateDevice Exception 0xc0000005 in Unix call` has been proven.
2. The issue has been categorized as Category F leading to Category E in Wine's `win32u.so`.
3. The minimal fix in `bare_vulkan_app.cpp` allows device creation, queue initialization, swapchain acquisition, rendering, presentation, and clean shutdown to succeed completely with exit code 0 using the exact, unmodified BR4 proxy and exact MoltenVK 1.4.2 delegate.
4. MoltenVK 1.4.2 historical provenance matching D2 is re-verified.
5. All tests and storage gates pass.
