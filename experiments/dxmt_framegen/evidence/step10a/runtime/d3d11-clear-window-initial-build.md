# STEP 10A.5 D3D11 clear-window cross-compile evidence

Captured: 2026-10-03 00:24:03 Asia/Tehran

## Artifacts

- Source: /tmp/step10a5-d3d11-nBU7gu/d3d11_clear_window.cpp
- Windows executable: /tmp/step10a5-d3d11-nBU7gu/d3d11_clear_window.exe
- Source SHA-256: b1fa27bf9f1830fe5d3d87f8f39b8d3701841f5f8ec27e012d56490c465be949
- Executable SHA-256: d698562c311b6ff0500c3ddfbb76ae0624f6f94f23e17d7cbc0eb38b43d8c313

The source defines a fixed 640x360 Win32 window and uses D3D11 to clear/present a four-color cycle (one color per 60 presents, vertical sync enabled). It closes after 10 seconds or on Escape/window close. Graphics objects and the window class are released on exit and on initialization failure. It has no custom shader, assets, or non-system runtime component.

## Toolchain and exact compile command

- Compiler: /opt/homebrew/bin/x86_64-w64-mingw32-g++
- Version: x86_64-w64-mingw32-g++ (GCC) 16.2.0
- Homebrew package: mingw-w64 14.0.0_3
- Target sysroot: /opt/homebrew/Cellar/mingw-w64/14.0.0_3/toolchain-x86_64

    x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -Wpedantic -static-libgcc -static-libstdc++ -mwindows /tmp/step10a5-d3d11-nBU7gu/d3d11_clear_window.cpp -o /tmp/step10a5-d3d11-nBU7gu/d3d11_clear_window.exe -ld3d11 -luser32 -lkernel32

Final compile exited 0 without warnings. The file utility identified the output as PE32+ executable (GUI) x86-64, for MS Windows (152 KiB).

## Dependencies and verification boundary

- Build-time: MinGW-w64 Windows headers/import libraries, including D3D11; explicit link libraries are d3d11, user32, and kernel32.
- PE imports: d3d11.dll, USER32.dll, KERNEL32.dll, plus Windows API-set Universal CRT DLLs (api-ms-win-crt-*). GCC and C++ runtimes were statically linked. The executable imports no third-party DLL and no direct dxgi.dll symbol; DXGI declarations are used for the swap-chain COM interface.
- This is compile-only evidence. The executable was not launched locally or under Wine, no Wine prefix was accessed, and no capture was taken.

An initial compile caught an ANSI/Unicode resource-ID mismatch and an unused temporary variable; both were corrected in this /tmp source and the final warning-clean compile above succeeded. All artifacts are confined to the unique temporary directory /tmp/step10a5-d3d11-nBU7gu; remove it with rm -rf /tmp/step10a5-d3d11-nBU7gu when no longer needed.
