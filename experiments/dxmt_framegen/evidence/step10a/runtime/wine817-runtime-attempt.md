# STEP 10A.5 matched-runtime DXMT smoke evidence

Captured: 2026-10-03 00:57:52 Asia/Tehran

## Isolation

- Pristine build-input runtime source: /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain
- Disposable runtime copy: /tmp/step10a5-wine816-TorHDl/engine (823M by du -sh)
- Source directory label says v8.16-3shain, but copied bin/wine --version reports wine-8.17. Record runtime evidence under the reported version.
- Disposable, partial WINEPREFIX: /tmp/step10a5-wine816-TorHDl/prefix (286M at termination)
- DXMT outputs came from /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src.
- Original Highball engine, pristine runtime source, DXMT build outputs, and project files were not modified.

## Overlay verification

The five requested DXMT outputs were copied with ditto into the copied runtime's x86_64 Wine builtin directories. Hashes match source-to-copy:

| Output | Copied destination | SHA-256 |
| --- | --- | --- |
| d3d10core.dll | engine/lib/wine/x86_64-windows/d3d10core.dll | c07a8c7f07082444533714625173671ae3ff60dde53d99e62be515157b72ebae |
| d3d11.dll | engine/lib/wine/x86_64-windows/d3d11.dll | 5e6a5e2b9bfba3100b8cfb109b525cad7c02d4342a7b5f4121866b599ae48d00 |
| dxgi.dll | engine/lib/wine/x86_64-windows/dxgi.dll | 0e334c15785431a227e2fa9a094cbc082594771b673d47ae0e89e6bb531458f9 |
| winemetal.dll | engine/lib/wine/x86_64-windows/winemetal.dll | 7d3fa72f488826d75feaa52fc5eb3592509e9331c8547e8f3159c270f64aca7a |
| winemetal.so | engine/lib/wine/x86_64-unix/winemetal.so | 9548dc43d5c4f06d774904137941782bd608c1189e02287fa12c50f6863e051e |

## Exact commands

Runtime copy:

    STEP10A5_816_DIR=$(mktemp -d /tmp/step10a5-wine816-XXXXXX)
    ditto /tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain "$STEP10A5_816_DIR/engine"

Overlay:

    ditto /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src/d3d10/d3d10core.dll "$STEP10A5_816_DIR/engine/lib/wine/x86_64-windows/d3d10core.dll"
    ditto /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src/d3d11/d3d11.dll "$STEP10A5_816_DIR/engine/lib/wine/x86_64-windows/d3d11.dll"
    ditto /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src/dxgi/dxgi.dll "$STEP10A5_816_DIR/engine/lib/wine/x86_64-windows/dxgi.dll"
    ditto /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src/winemetal/winemetal.dll "$STEP10A5_816_DIR/engine/lib/wine/x86_64-windows/winemetal.dll"
    ditto /tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64/src/winemetal/unix/winemetal.so "$STEP10A5_816_DIR/engine/lib/wine/x86_64-unix/winemetal.so"

Fresh prefix initialization:

    env WINEPREFIX=/tmp/step10a5-wine816-TorHDl/prefix WINEARCH=win64 WINELOADER=/tmp/step10a5-wine816-TorHDl/engine/bin/wine WINESERVER=/tmp/step10a5-wine816-TorHDl/engine/bin/wineserver PATH=/tmp/step10a5-wine816-TorHDl/engine/bin:"$PATH" /tmp/step10a5-wine816-TorHDl/engine/bin/wineboot -u > /tmp/step10a5-wine816-TorHDl/wineboot.log 2>&1

The command remained active beyond 6 minutes. Its shell session was ended by SIGTERM and returned status 143; that is not a successful wineboot result. The retained 1,388-byte log at /tmp/step10a5-wine816-TorHDl/wineboot.log ends with RpcSs/COM errors, setupapi copy error 1812 for wineusb.inf, and “boot event wait timed out.”

Before termination, the prefix root contained system.reg, user.reg, userdef.reg, dosdevices, .update-timestamp, and drive_c. Captured sleeping process state at 7:08 elapsed was:

    26740 wine-preloader start.exe /exec wineboot.exe
    26755 wineserver
    26757 wine-preloader C:\windows\system32\wineboot.exe --init
    26760 wine-preloader C:\windows\system32\services.exe
    26769 wine-preloader C:\windows\system32\explorer.exe /desktop
    26774 wine-preloader C:\windows\system32\winedevice.exe
    26781 wine-preloader C:\windows\system32\rundll32.exe setupapi,InstallHinfSection DefaultInstall ... wine.inf
    26805 wine-preloader C:\windows\system32\control.exe appwiz.cpl install_mono
    26997 wine-preloader C:\windows\system32\wineboot.exe -u
    27003 wine-preloader C:\windows\system32\rundll32.exe setupapi,InstallHinfSection DefaultInstall ... wine.inf
    27006 wine-preloader C:\windows\system32\control.exe appwiz.cpl install_mono

SIGTERM was sent only to these 11 PIDs because every command path contained the disposable runtime. A subsequent process check found no process or wineserver whose command path contained /tmp/step10a5-wine816-TorHDl. The partial prefix and log were retained.

## Single app launch against partial prefix

The same controlled, auto-resizing D3D11 clear-window executable was launched once, after verifying no runtime server remained. DXMT logging used the documented settings DXMT_LOG_LEVEL=info and DXMT_LOG_PATH=/tmp/step10a5-dxmt-wine817-xqrRKf; WINEDEBUG=+loaddll preserved module-load evidence. No DXMT info file was created.

Exact command:

    env WINEPREFIX=/tmp/step10a5-wine816-TorHDl/prefix WINEARCH=win64 WINELOADER=/tmp/step10a5-wine816-TorHDl/engine/bin/wine WINESERVER=/tmp/step10a5-wine816-TorHDl/engine/bin/wineserver PATH=/tmp/step10a5-wine816-TorHDl/engine/bin:"$PATH" WINEDLLOVERRIDES='d3d10core=b;d3d11=b;dxgi=b;winemetal=b' WINEDEBUG=+loaddll DXMT_LOG_LEVEL=info DXMT_LOG_PATH=/tmp/step10a5-dxmt-wine817-xqrRKf /tmp/step10a5-wine816-TorHDl/engine/bin/wine /tmp/step10a5-d3d11-nBU7gu/d3d11_clear_window.exe > /tmp/step10a5-dxmt-wine817-xqrRKf/wine-console.log 2>&1

The app returned 66 after about 5.0 seconds. The 6,716-byte console log shows winemetal.dll, DXGI.DLL, and d3d11.dll loaded as Wine builtins, followed immediately by:

    err:module:LdrInitializeThunk "winemetal.dll" failed to initialize, aborting
    err:module:LdrInitializeThunk Initializing dlls for ...d3d11_clear_window.exe failed, status c0000142

This confirms builtin resolution but not successful DXMT initialization. No Metal device, rendering, resize, or clean application exit was reached. Treat Wine internal ABI mismatch as the leading hypothesis; this run alone does not identify the failing ABI call. No further runtime attempts or module patching were made.

## Preserved logs and resources

- Wine 8.17 prefix boot log: /tmp/step10a5-wine816-TorHDl/wineboot.log
- Single Wine 8.17 application console/load log: /tmp/step10a5-dxmt-wine817-xqrRKf/wine-console.log
- Highball 10.0 attempts/logs remain preserved at /tmp/step10a5-dxmt-info-LCXEgd and /tmp/step10a5-dxmt-info-ovr1.
- Disk after run: 460Gi total, 381Gi used, 53Gi available (88%).
