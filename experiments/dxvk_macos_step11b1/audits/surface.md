# Native vs Wine WSI surface audit

Read-only comparison of the native Vulkan control in `step11b` and the frozen Wine/DXVK runs in `step11b1`. Identities: MoltenVK 1.4.2 `db66022459ffb663aa2b50f6b018bc2e124f5edf`; Wine 11.17 frozen runtime; DXVK `8d348236e14a3db25ffbe528a83010b3dd69a3ef`.

## Evidence and comparison

The native control (`evidence/native-clear-control.mm`) creates an AppKit `NSWindow` with an explicit `CAMetalLayer` (`BGRA8Unorm`, drawable size 640x360), then calls `vkCreateMetalSurfaceEXT` with that layer. It prefers `VK_FORMAT_B8G8R8A8_UNORM` plus `VK_COLOR_SPACE_SRGB_NONLINEAR_KHR` when enumerated, otherwise it takes the first surface format. It uses the reported current extent, three images when the capabilities permit, `TRANSFER_DST` usage, one layer, exclusive sharing, FIFO, and clipped presentation. Its transform follows `currentTransform`; alpha uses opaque when supported and otherwise inherit. The native log confirms 640x360, three images, FIFO, and successful present results for 1800 calls, but does not print the selected surface format or the queried capabilities (`evidence/native-clear.log`).

DXVK's Win32 WSI path calls `vkCreateWin32SurfaceKHR` with the DXVK `HWND`/`HINSTANCE` (`wsi_window_win32.cpp::Win32WsiDriver::createSurface` in the pinned source); the run log confirms `VK_KHR_win32_surface` is enabled. In both RGBA and BGRA A/B runs, DXVK logs the actual swapchain as `B8G8R8A8_UNORM` / `SRGB_NONLINEAR`, FIFO, 640x360, three images (`evidence/rgba8/format-ab_d3d11.log`, `evidence/bgra8/format-ab_d3d11.log`). Repository smoke used a 312x206 client area, so it is not an extent-matched comparison. DXVK's pinned presenter requests `COLOR_ATTACHMENT | TRANSFER_DST`, exclusive sharing, one layer, identity transform, opaque alpha, FIFO, and clipped presentation (`src/dxvk/dxvk_presenter.cpp`, swapchain creation).

Wine's chosen format matches the native control's preferred format, but the native log does not print which format was actually selected, so that equality remains unverified. The extent, mode, and image count match in the extent-matched A/B runs. The concrete differences are the surface creation route (native Metal surface with a caller-supplied layer vs Win32 surface through Wine Vulkan) and image usage (transfer destination only vs color attachment plus transfer destination). DXVK also fixes identity transform and opaque alpha, while the native control follows capabilities; the native capability values are absent, so whether those settings differ at runtime is unknown. Neither run records the full surface capability/format/mode lists, and the Wine logs do not expose the internal Win32-to-Metal layer mapping. No direct Vulkan clear-under-Wine result is present: the reported BLACK observations are for the DXVK D3D11 smoke/A-B applications. This evidence does not establish a WSI value mismatch that explains them.

## Minimal public diagnostics if a direct Wine WSI clear is black

Run the same tiny transfer-clear/present workload on both paths and print only:

1. Surface creation API/extension and result; physical device, graphics queue family, and `vkGetPhysicalDeviceSurfaceSupportKHR` result.
2. Surface capabilities: current/min/max extent, min/max image count, current/supported transforms, supported composite alpha, and supported usage flags; all returned format/color-space pairs and present modes.
3. The actual swapchain create values and result: format/color space, extent, image count, usage, layers, sharing mode, transform, alpha, and present mode; also the returned swapchain-image count.
4. For one clear/present iteration: acquire result and index, queue-submit result, present result, and a completion-fence result for the clear submission.

This compares public WSI inputs and execution without pixel readback. Do not infer a display/compositor cause from these values alone. Native layer pixel format and drawable size are already explicit in the control source; public Vulkan does not reveal the layer object created internally for Wine's Win32 surface.
