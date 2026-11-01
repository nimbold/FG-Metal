#!/bin/sh
# R118: restore the D3DMetal timestamp-shim links that Highball creates at launch.
#
# The engine manifest says it plainly:
#   "At launch Highball hard-links D3DMetal's d3d12.dll beside it as
#    d3d12_d3dmetal.dll and symlinks its x86_64-unix/d3d12.so, because Wine
#    resolves builtin modules by base name."
#
# Those two artefacts are created by the Highball launcher and are not part of
# the installed engine payload, so they are absent when the game is started by
# invoking wine directly -- which is what this project's harness does. Without
# them the shim cannot find the real D3DMetal d3d12.dll and the process aborts:
#
#   tsshim: cannot load the real D3DMetal d3d12.dll (d3d12_d3dmetal.dll, error 126)
#   0220:err:module:loader_init "d3d12.dll" failed to initialize, aborting
#
# This script creates exactly the two artefacts Highball would have created, from
# the D3DMetal overlay that the launch script already places on WINEDLLPATH, and
# records what it added so `--undo` removes precisely those and nothing else.
#
# It changes nothing about the renderer, the engine binaries or the bottle. It
# only restores links that the vendor launcher would have made.
set -eu

E="$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r29"
TSSHIM_WIN="$E/renderers/d3dmetal-tsshim/wine/x86_64-windows"
TSSHIM_UNIX="$E/renderers/d3dmetal-tsshim/wine/x86_64-unix"
REAL_WIN="$E/frameworks/renderer/d3dmetal/wine/x86_64-windows/d3d12.dll"
REAL_UNIX="$E/frameworks/renderer/d3dmetal/wine/x86_64-unix/d3d12.so"
MANIFEST="/tmp/fgmetal-r118-tsshim-links.txt"

if [ "${1:-}" = "--undo" ]; then
    if [ -f "$MANIFEST" ]; then
        while read -r path; do
            [ -e "$path" ] && rm -f "$path" && echo "R118_TSSHIM removed=$path"
        done < "$MANIFEST"
        rm -f "$MANIFEST"
    fi
    echo "R118_TSSHIM=UNDONE"
    exit 0
fi

for f in "$REAL_WIN" "$REAL_UNIX"; do
    [ -e "$f" ] || { echo "R118_TSSHIM=FAILED missing-source $f" >&2; exit 1; }
done

: > "$MANIFEST"

if [ ! -e "$TSSHIM_WIN/d3d12_d3dmetal.dll" ]; then
    ln "$REAL_WIN" "$TSSHIM_WIN/d3d12_d3dmetal.dll"
    echo "$TSSHIM_WIN/d3d12_d3dmetal.dll" >> "$MANIFEST"
    echo "R118_TSSHIM linked=$TSSHIM_WIN/d3d12_d3dmetal.dll -> $REAL_WIN (hard link)"
fi

if [ ! -e "$TSSHIM_UNIX/d3d12.so" ]; then
    ln -s "$REAL_UNIX" "$TSSHIM_UNIX/d3d12.so"
    echo "$TSSHIM_UNIX/d3d12.so" >> "$MANIFEST"
    echo "R118_TSSHIM linked=$TSSHIM_UNIX/d3d12.so -> $REAL_UNIX (symlink)"
fi

ls -la "$TSSHIM_WIN/d3d12_d3dmetal.dll" "$TSSHIM_UNIX/d3d12.so"
echo "R118_TSSHIM=READY"
