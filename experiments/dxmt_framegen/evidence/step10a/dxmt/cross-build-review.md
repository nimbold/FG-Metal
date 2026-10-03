# DXMT cross-build reuse evidence

## Pinned DXMT CI Wine artifact

The repository examined is the public DXMT commit `fb4515681daefb789a4d0f403c4bdbca88f3b3de`.

At `.github/workflows/ci.yml:21-23`, x86_64 CI sets `WINE_VERSION=v8.16-3shain` and `WINE_URL=https://github.com/3Shain/wine/releases/download/v8.16-3shain/wine.tar.gz`. The GitHub release tag resolves to commit `4dd8a2d6dca39a116d0911f247bbaf1ad5e29471`. GitHub Release API metadata reports `wine.tar.gz` size `230434672` bytes (219.76 MiB) and `digest: null`.

At `.github/workflows/ci.yml:43-50`, CI extracts that archive directly under `toolchains/wine` with no strip-components. The x86_64 cross build at lines 331-335 configures `-Dwine_install_path=toolchains/wine` and then compiles/installs. Thus the supported CI reuse path is the Wine installation prefix, not `-Dwine_build_path`.

The pinned Meson source makes the two layouts explicit (`src/winemetal/meson.build:17-32`; `src/winemetal/unix/meson.build:6-11`):

- `wine_build_path` expects build-tree artifacts under `libs/winecrt0/x86_64-windows` or `dlls/winecrt0/x86_64-windows`, `dlls/ntdll/x86_64-windows`, `dlls/dbghelp/x86_64-windows`, `tools/winebuild/winebuild`, and Unix libraries under `dlls/winemac.drv/winemac.so` and `dlls/ntdll/ntdll.so`.
- `wine_install_path` expects libraries under `lib/wine/x86_64-windows`, the tool `bin/winebuild`, and Unix libraries `lib/wine/x86_64-unix/{winemac.so,ntdll.so}`.

The release archive itself has not been listed or extracted in this agent's work (the parent is downloading it separately). To verify its exact member paths without unpacking it, after the download completes:

```sh
tar -tzf "$WINE_TAR" | rg '(^|/)(winebuild|libwinecrt0[^/]*|libntdll[^/]*|libdbghelp[^/]*|ntdll.so|winemac.so)$'
```

Do not pass this install-prefix archive as `wine_build_path`; use `wine_install_path` if the expected developer files are present.

## Installed Highball Sikarugir Wine 10.0 runtime fit

Only public Wine runtime/development filenames were checked under:

`/Users/<user>/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r15/engine`

The install contains `bin/wine`, `bin/wineserver`, `lib/wine/x86_64-unix/ntdll.so`, `lib/wine/x86_64-unix/winemac.so`, and x86_64 Windows runtime DLLs including `ntdll.dll` and `dbghelp.dll`. The x86_64 Windows directory contains no `.a`, `.dll.a`, or `.lib` development libraries. `bin/winebuild` is absent. In particular, the files Meson needs to link `winecrt0`, static `ntdll`, and `dbghelp`, plus the build tool `winebuild`, are missing. This installed Highball engine cannot serve as DXMT's `-Dwine_install_path`; it is a runtime distribution without the required build/development artifacts. No renderer payload or D3DMetal file contents were read.

## LLVM 15 x86_64 macOS artifact

The official LLVM 15.0.7 GitHub release (`llvmorg-15.0.7`) lists:

- URL: `https://github.com/llvm/llvm-project/releases/download/llvmorg-15.0.7/clang%2Bllvm-15.0.7-x86_64-apple-darwin21.0.tar.xz`
- File: `clang+llvm-15.0.7-x86_64-apple-darwin21.0.tar.xz`
- Size: `728344840` bytes (`694.60 MiB`)
- GitHub Release API `digest`: `null`; the release asset list has no checksum asset or detached signature for this binary. Therefore no official SHA-256 was found and none is claimed here.

This is the official x86_64 macOS package matching the DXMT cross build's LLVM architecture and major version. The archive was not downloaded, so its precise contents and whether its linkable libraries exactly match DXMT CI's custom LLVM build remain unverified. DXMT CI builds its own LLVM from tag `llvmorg-15.0.7` with `CMAKE_OSX_ARCHITECTURES=x86_64`, `LLVM_ENABLE_ZSTD=Off`, `LLVM_TARGETS_TO_BUILD=""`, `LLVM_BUILD_TOOLS=Off`, and other project-specific CMake flags (`.github/workflows/ci.yml`, `setup-llvm-darwin`). The direct LLVM release tarball is a candidate, not a verified drop-in for that custom install prefix. After a user-approved download, compute and record `shasum -a 256` locally; do not label that as an upstream-published checksum.

## Wine 8.16-to-10.0 ABI risk

The CI build is specifically coupled to Wine artifacts from `v8.16-3shain`: the PE target links Wine's `winecrt0`, static `ntdll`, and `dbghelp`, then runs that release's `winebuild --builtin`; the Unix target is linked with that release's `winemac.so` and `ntdll.so`. DXMT's PE/Unixlib bridge uses Wine-specific `__wine_init_unix_call`/`__wine_unix_call` entry points and generated Unix call tables (`src/winemetal/main.c`, `src/winemetal/winemetal_thunks.c`, `src/winemetal/unix/winemetal_unix.c`).

Wine 10.0 Sikarugir is a newer runtime, but it is a distinct downstream Wine build. Windows PE imports are designed around the Windows ABI; the Wine builtin glue and PE-to-Unix bridge additionally depend on Wine's internal runtime conventions and matching ntdll/macOS Wine libraries. Upstream Wine 7.0 release notes describe changes to the PE/Unix interface, while Wine 8.16 release notes still mention keeping Unixlib function tables and enums synchronized. This is a meaningful cross-version ABI risk, not proof of incompatibility. Treat execution against Sikarugir 10.0 as unverified until a disposable Wine 10.0 smoke run checks DXMT loading and the Unixlib handshake. For the first build, use the same Wine 8.16 install archive as the build inputs, then use the 10.0 engine only as the separately labeled compatibility test.

## Evidence files

- `cross-build-reuse.txt`: pinned CI extraction/setup and Meson option consumers.
- `cross-artifact-metadata.txt`: release/tag/API asset metadata and byte sizes.
- `highball-r15-public-wine-files.txt`, `highball-r15-installpath-fit.txt`, `highball-r15-devfiles.txt`: filtered Highball Wine filenames and fit checks.
- `wine-abi-crosslink.txt`: pinned Wine target and Unixlib integration references.

## Independent review of completed x64 cross build

Build tree: `/tmp/dxmt-fb451568-evidence.opkf8x/build-wine-x64`; source remains the pinned commit. The setup log identifies build machine `aarch64`, host/target machine `x86_64`, 16 build targets, `build-win64.txt`, and Wine install prefix `/tmp/dxmt-fb451568-evidence.opkf8x/wine-install-v8.16-3shain`. The configured defaults were `enable_nvapi=false`, `enable_nvngx=false`, `enable_d3d12=false`, `build_airconv_for_windows=false`; therefore this is the core target set, not the full pinned CI invocation (which enables NVAPI and NVNGX).

The compile log reaches `[161/161]` and contains no `error:`, `ERROR:`, or `FAILED:` markers. Parent reports the compile command exited successfully. It has 146 compiler-warning lines and 2 linker-warning lines (many are repeated across translation units): non-void functions/lambdas without return on all paths, possible uninitialized values in switch defaults, a `dxgi_adapter.cpp` missing return, macOS 27 deprecations for `MTLStorageModeManaged`/`didModifyRange:`, and two linker warnings about directly linking the LLVM archive's re-exported `libunwind.1.dylib`. These are warnings, not a failed compile.

`file` confirms these runtime outputs:

- `src/d3d10/d3d10core.dll`: PE32+ x86-64, 13,651,419 bytes, SHA-256 `c07a8c7f07082444533714625173671ae3ff60dde53d99e62be515157b72ebae`
- `src/d3d11/d3d11.dll`: PE32+ x86-64, 23,103,357 bytes, SHA-256 `5e6a5e2b9bfba3100b8cfb109b525cad7c02d4342a7b5f4121866b599ae48d00`
- `src/dxgi/dxgi.dll`: PE32+ x86-64, 14,662,018 bytes, SHA-256 `0e334c15785431a227e2fa9a094cbc082594771b673d47ae0e89e6bb531458f9`
- `src/winemetal/winemetal.dll`: PE32+ x86-64, 180,354 bytes, SHA-256 `7d3fa72f488826d75feaa52fc5eb3592509e9331c8547e8f3159c270f64aca7a`
- `src/winemetal/unix/winemetal.so`: Mach-O x86_64, 27,463,704 bytes, SHA-256 `9548dc43d5c4f06d774904137941782bd608c1189e02287fa12c50f6863e051e`

The install plan places four PE runtime DLLs under `{prefix}/x86_64-windows/`; `.dll.a` siblings are development import libraries and are not runtime replacements. The pinned install script places `winemetal.so` under `{prefix}/x86_64-unix/`. The plan has 8 entries (4 runtime DLLs plus 4 `.dll.a` files). There is no install-tree output at the inspected expected paths and no `meson install` log was supplied; these are build-tree outputs. `dxmt_command.metallib` is a generated intermediate consumed through a generated header by the `libdxmt.a` static target, not a separate runtime replacement.

The core smoke replacement set for a copied x64 Wine engine is therefore the four `.dll` files above under its `lib/wine/x86_64-windows/`, plus paired `winemetal.so` under `lib/wine/x86_64-unix/`. Do not copy `.dll.a`, `libdxmt.a`, `airconv`, or the intermediate metallib. Do not replace Wine 10's `ntdll.so`/`winemac.so`: `otool -L` shows the built Unixlib depends on `@rpath/ntdll.so` and `@rpath/winemac.so`, and `otool -l` shows relative run paths `@loader_path/` and `@loader_path/../../`, so it will resolve those from the runtime engine layout.

Architecture compatibility is positive for the disposable x64 engine: the installed Highball Sikarugir r15 `wine`, `wineserver`, `ntdll.so`, and `winemac.so` are x86_64, as are the new DXMT PE DLLs and Unixlib. An `arch -x86_64 /usr/bin/true` probe exited 0 on the arm64 host, confirming x86_64 execution is available. This does not validate the Wine 8.16-to-10.0 internal ABI; retain the earlier ABI caveat. A smoke is operationally bounded if it uses a genuinely copied engine and a disposable Wine prefix, replaces only the five runtime outputs, and leaves the original engine/prefix untouched. No runtime copy was modified and no smoke was launched in this review.

Detailed read-only review artifacts: `cross-build-review.txt`, `cross-build-output-inventory.txt`, `cross-build-target-details.txt`, `cross-build-binary-review.txt`, and `cross-build-install-plan-review.txt`.
