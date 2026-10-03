# STEP 10A.4 disposable Highball runtime boot evidence

Captured: 2026-10-03 00:38:57 Asia/Tehran

## Isolation and artifacts

- Installed engine source: /Users/<user>/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r15/engine
- Isolated copy: /tmp/step10a4-highball-V5DiZw/engine
- Disposable WINEPREFIX used for the successful run: /tmp/step10a4-highball-V5DiZw/prefix-clean
- Failed first-attempt prefix (isolated, partial): /tmp/step10a4-highball-V5DiZw/prefix
- Evidence directory: /tmp/step10a4-highball-V5DiZw
- Engine version from the copied loader: wine-10.0 (Sikarugir)
- Toolchain executable: /tmp/step10a4-highball-V5DiZw/engine/bin/wine; matching wineserver: /tmp/step10a4-highball-V5DiZw/engine/bin/wineserver
- Engine copy size: 750M by du -sh.
- Successful prefix size: 337M by du -sh; system.reg and drive_c were present.
- Required host helper: engine/lib/libinotify.0.dylib is a relative symlink (../../frameworks/libinotify.0.dylib). The first boot could not resolve it from an engine-only copy. I copied only its exact target from the same Highball version's sibling frameworks directory to /tmp/step10a4-highball-V5DiZw/frameworks/libinotify.0.dylib (48K). The copied engine symlink then resolved. This is a macOS host dylib, not a Windows DLL.

## Exact commands

Created the unique temp directory and copied the engine with:

    STEP10A4_DIR=$(mktemp -d /tmp/step10a4-highball-XXXXXX)
    ditto '/Users/<user>/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r15/engine' "$STEP10A4_DIR/engine"

Version query (no prefix set):

    "$STEP10A4_DIR/engine/bin/wine" --version

After confirming the source symlink target exists, staged only that host dependency:

    mkdir -p "$STEP10A4_DIR/frameworks"
    ditto '/Users/<user>/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r15/frameworks/libinotify.0.dylib' "$STEP10A4_DIR/frameworks/libinotify.0.dylib"

The successful initialization command was:

    env WINEPREFIX=/tmp/step10a4-highball-V5DiZw/prefix-clean WINEARCH=win64 WINELOADER=/tmp/step10a4-highball-V5DiZw/engine/bin/wine WINESERVER=/tmp/step10a4-highball-V5DiZw/engine/bin/wineserver PATH=/tmp/step10a4-highball-V5DiZw/engine/bin:"$PATH" /tmp/step10a4-highball-V5DiZw/engine/bin/wine /tmp/step10a4-highball-V5DiZw/engine/lib/wine/x86_64-windows/wineboot.exe -u

The copied bundle has no host bin/wineboot wrapper; the command above invokes its bundled x86-64 wineboot.exe through the copied Wine loader, with only -u. It is the sole prefix-mutating Wine command used. No application under test was launched. Wineboot itself started its bundled prefix-setup helper programs as part of initialization.

## Results and limits

- Initial wineboot attempt used WINEPREFIX=/tmp/step10a4-highball-V5DiZw/prefix and the same copied loader/wineboot.exe -u. It exited 1 because dyld could not resolve the copied engine's libinotify symlink target. It left only an isolated partial prefix. I did not remove it.
- Successful wineboot -u used the fresh WINEPREFIX /tmp/step10a4-highball-V5DiZw/prefix-clean, created the prefix, and exited 0 after about 25.5 seconds.
- The successful run logged non-fatal or subsystem errors/warnings despite exit 0: FreeType missing; libMoltenVK.dylib missing; GnuTLS missing (no encryption/PFX support); RpcSs startup/marshal failures; winebth.sys and wineusb.sys setup copy errors (1812); missing Microsoft.Windows.Common-Controls manifest; and Sikarugir's testing-version notice. Prefix creation is demonstrated, but this does not establish that those optional services/features work.
- No normal game prefix was set or used. No application was launched. No Windows DLL was installed or replaced manually. The installed engine and DXMT component were left unchanged. I did not inspect or enumerate D3DMetal/private files or symbols.

## Disk snapshots

- Before copying: df -h /tmp reported 460Gi total, 379Gi used, 55Gi available (88%).
- After prefix initialization: df -h /tmp reported 460Gi total, 381Gi used, 53Gi available (88%).
