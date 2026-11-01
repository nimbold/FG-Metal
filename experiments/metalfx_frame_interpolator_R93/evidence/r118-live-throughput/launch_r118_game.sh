#!/bin/sh
# R118 launch: start Elden Ring under the play bottle's Sikarugir engine with the
# R118 generation-throughput observer.
#
# Identical to the R116 launch in every environment variable and in the D3DMetal
# route. The only difference is the injected library, which is the R117 estimator
# plus the R118 service instrumentation. The R116 presentation path is frozen and
# still runs, deliberately, so the measurement is taken under real game and
# display contention rather than in a quiet process.
#
# No synthetic gameplay input is sent by this script or by the controller. The
# camera rotation is the operator's.
set -eu

ENGINE="$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r29"
FRAMEWORKS="$ENGINE/frameworks"
PROBE="/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/libfgmetal_r118_throughput_probe.dylib"
GAME="/Users/nima/Games/Elden Ring/Game/eldenring.exe"
LOG="/tmp/fgmetal-r108-unattended/r118-game-launch.log"

mkdir -p /tmp/fgmetal-r108-unattended

if [ ! -f "$PROBE" ]; then
    echo "R118_LAUNCH=FAILED reason=probe-missing path=$PROBE" >&2
    exit 1
fi

# Refuse to launch a library that is not the one that was verified. The hash is
# printed by build_r118_tools.sh and checked by tools/verify_r118_estimator.py.
EXPECTED_SHA="ccc0035d1602eeefded4003b2050724d7b35ca43cc6a3694efec0c3707a5ccbe"
ACTUAL_SHA="$(shasum -a 256 "$PROBE" | cut -d' ' -f1)"
if [ "$EXPECTED_SHA" != "$ACTUAL_SHA" ]; then
    echo "R118_LAUNCH=FAILED reason=probe-hash-mismatch expected=$EXPECTED_SHA actual=$ACTUAL_SHA" >&2
    exit 1
fi

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
FGMETAL_R100_PRESENT=1 \
FGMETAL_R110_PRESENT=1 \
FGMETAL_R108_CAPTURE=1 \
FGMETAL_R108_FRAMEBUFFER_ONLY=1 \
FGMETAL_R114_INPROCESS_LATCH=1 \
FGMETAL_R115_CONTINUOUS=1 \
FGMETAL_R115_MAX_TUPLES="${FGMETAL_R115_MAX_TUPLES:-20}" \
FGMETAL_R116_B_EARLY="${FGMETAL_R116_B_EARLY:-1}" \
FGMETAL_R116_WATCHDOG_MS="${FGMETAL_R116_WATCHDOG_MS:-150}" \
"$ENGINE/engine/bin/wine" "$GAME" >>"$LOG" 2>&1
