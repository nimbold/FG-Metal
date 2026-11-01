#!/bin/sh
# R116 launch: start Elden Ring under the play bottle's Sikarugir engine with the
# R116 critical-path observer (async GPU fast gate + non-blocking M-then-B
# scheduling + committed-B watchdog).
#
# The D3DMetal CAMetalLayer path is the one under test. No synthetic gameplay
# input is sent by this script or by the controller.
set -eu

ENGINE="$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r29"
FRAMEWORKS="$ENGINE/frameworks"
PROBE="/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/libfgmetal_r116_criticalpath_probe.dylib"
GAME="/Users/nima/Games/Elden Ring/Game/eldenring.exe"
LOG="/tmp/fgmetal-r108-unattended/r116-game-launch.log"

mkdir -p /tmp/fgmetal-r108-unattended

WINEPREFIX="$HOME/Library/Application Support/Highball/bottles/Games" \
DYLD_FALLBACK_FRAMEWORK_PATH="$FRAMEWORKS:$FRAMEWORKS/renderer/d3dmetal/external" \
DYLD_FALLBACK_LIBRARY_PATH="$FRAMEWORKS:$FRAMEWORKS/renderer/d3dmetal/external:$FRAMEWORKS/GStreamer.framework/Versions/1.0/lib" \
GST_PLUGIN_PATH="$FRAMEWORKS/GStreamer.framework/Versions/1.0/lib/gstreamer-1.0" \
WINEDEBUG=fixme-all \
WINEMSYNC=1 \
HB_D3D12_TSSHIM="Z:\\Users\\nima\\Library\\Application Support\\Highball\\engines\\x64-sikarugir10.0_6-r29\\frameworks\\renderer\\d3dmetal\\wine\\x86_64-windows\\d3d12.dll" \
HB_TSSHIM_DEBUG=1 \
WINEDLLPATH_PREPEND="$ENGINE/renderers/d3dmetal-tsshim/wine:$ENGINE/frameworks/renderer/d3dmetal/wine:$ENGINE/renderers/dxmt/wine:$ENGINE/frameworks/renderer/d9vk/wine" \
WINEDLLOVERRIDES="winemenubuilder.exe=d" \
ROSETTA_ADVERTISE_AVX=1 \
CX_FWD_COMPAT_GL_CTX=1 \
DXVK_CONFIG_FILE='C:\highball\dxvk.conf' \
DXVK_LOG_PATH='C:\highball\logs' \
DYLD_INSERT_LIBRARIES="$FRAMEWORKS/libhbaudiobuf.dylib:$PROBE" \
FGMETAL_R110_PRESENT=1 \
FGMETAL_R108_CAPTURE=1 \
FGMETAL_R108_FRAMEBUFFER_ONLY=1 \
FGMETAL_R114_INPROCESS_LATCH=1 \
FGMETAL_R115_CONTINUOUS=1 \
FGMETAL_R115_MAX_TUPLES="${FGMETAL_R115_MAX_TUPLES:-10}" \
FGMETAL_R116_B_EARLY="${FGMETAL_R116_B_EARLY:-1}" \
FGMETAL_R116_WATCHDOG_MS="${FGMETAL_R116_WATCHDOG_MS:-150}" \
"$ENGINE/engine/bin/wine" "$GAME" >>"$LOG" 2>&1
