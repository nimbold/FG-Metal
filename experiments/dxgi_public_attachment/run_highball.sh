#!/usr/bin/env bash
set -euo pipefail

FG_STEP8D_ENGINE_ROOT="${FG_STEP8D_ENGINE_ROOT:-$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r14}"
FG_STEP8D_PREFIX="${FG_STEP8D_PREFIX:-$HOME/Library/Caches/FG-Metal-Step8D-20261002/transparent-prefix}"

export WINEPREFIX="$FG_STEP8D_PREFIX"
export WINEARCH=win64
export WINEMSYNC=1
export WINEESYNC=0
export CX_FWD_COMPAT_GL_CTX=1
export D3DM_MTL4=0
export WINEDLLPATH_PREPEND="$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal-tsshim/wine:$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal/wine:$FG_STEP8D_ENGINE_ROOT/renderers/dxmt/wine:$FG_STEP8D_ENGINE_ROOT/frameworks/renderer/d9vk/wine"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-dxgi=n,b;d3d11,d3d10core,d3d12,d3d12core=n,b;winemenubuilder.exe=d}"
export HB_D3D12_REAL="Z:$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal-tsshim/wine/x86_64-windows/apd12.dll"
export CX_D3DMETALPATH="$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal/external"
export DYLD_FALLBACK_FRAMEWORK_PATH="$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal/external:$FG_STEP8D_ENGINE_ROOT/frameworks"
export DYLD_FALLBACK_LIBRARY_PATH="$FG_STEP8D_ENGINE_ROOT/renderers/d3dmetal/external:$FG_STEP8D_ENGINE_ROOT/frameworks:$FG_STEP8D_ENGINE_ROOT/frameworks/GStreamer.framework/Versions/1.0/lib"
export GST_PLUGIN_PATH="$FG_STEP8D_ENGINE_ROOT/frameworks/GStreamer.framework/Versions/1.0/lib/gstreamer-1.0"
export WINEDEBUG="${FG_STEP8D_WINEDEBUG:-+loaddll}"

exec "$FG_STEP8D_ENGINE_ROOT/engine/bin/wine" "$@"
